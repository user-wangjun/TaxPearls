"""Policy qualification stays separate from VAT facts and follows retained evidence."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import unittest

from src import policies
from tests import test_kg_retrieval as kg
from tests.test_policies import vat, facts
from tests import test_vat_reconciliation as vat_tests
from tests.test_vat_reconciliation import invoices
from webapp import core_policies


@kg.material_key
class CorePolicyTests(unittest.TestCase):
    setUp = kg.RetrievalHTTPTests.setUp
    login = kg.RetrievalHTTPTests.login
    task = kg.RetrievalHTTPTests.task
    upload = vat_tests.VATReconciliationTests.upload
    review = vat_tests.VATReconciliationTests.review
    run_check = vat_tests.VATReconciliationTests.run_check

    def url(self, task, suffix=''):
        return '/api/enterprise/input-tasks/' + task['id'] + suffix

    def save(self, task, inputs=None, **extra):
        body = {'expected_revision': task['revision'], 'mapping_mode': 'local', **extra}
        if inputs is not None:
            body['policy_input'] = inputs
        response = self.client.post(self.url(task, '/prepare'), json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def candidate(self, task=None):
        return self.save(task or self.task(), {'selected': ['VAT-THRESHOLD-2026'], 'facts': vat()})

    def acknowledge(self, task):
        inputs = deepcopy(task['policy_input'])
        inputs['reviews'] = {r['id']: r['review_token'] for r in task['policy_assessment']['results'] if r['review_token']}
        return self.save(task, inputs)

    def test_material_version_task_binding_and_forged_tokens(self):
        task = self.candidate()
        self.assertEqual(task['policy_assessment']['results'][0]['status'], 'needs_review')
        raw = policies.assess(task['policy_assessment']['scope'], {**task['policy_input'], 'reviews': {}})
        inputs = deepcopy(task['policy_input'])
        inputs['reviews'] = {r['id']: r['review_token'] for r in raw['results']}
        task = self.save(task, inputs)
        self.assertFalse(task['policy_assessment']['results'][0]['reviewed'])
        task = self.acknowledge(task)
        self.assertEqual(task['policy_assessment']['results'][0]['status'], 'applicable')
        other = self.save(self.task(), task['policy_input'])
        self.assertFalse(other['policy_assessment']['results'][0]['reviewed'])
        task = self.upload(task, invoices([('P1', '1', '2', '蓝字', '正常')]))
        self.assertFalse(task['policy_assessment']['results'][0]['reviewed'])
        self.assertNotEqual(task['policy_assessment']['results'][0]['review_token'], other['policy_assessment']['results'][0]['review_token'])

    def test_conflict_correction_requires_new_review_and_does_not_change_metrics(self):
        inputs = {'selected': ['VAT-THRESHOLD-2026', 'SMALL-SIX-TWO'], 'facts': {
            **vat(), **facts(six_tax='property', six_eligibility='new_general', six_new='yes',
                six_before_settlement='yes', cit_nonrestricted='yes', six_people='100',
                six_assets='1000000', six_other_reliefs='yes')}}
        task = self.save(self.task(), inputs)
        records = deepcopy(task['processing']['records'])
        self.assertEqual(task['policy_assessment']['consistency']['status'], 'conflict')
        self.assertTrue(all(r['review_token'] is None for r in task['policy_assessment']['results']))
        inputs['facts'].update(facts(six_eligibility='small_vat'))
        task = self.save(task, inputs)
        self.assertTrue(all(not r['reviewed'] for r in task['policy_assessment']['results']))
        task = self.acknowledge(task)
        self.assertTrue(all(r['status'] == 'applicable' for r in task['policy_assessment']['results']))
        self.assertEqual(task['processing']['records'], records)
        self.assertFalse(task['policy_assessment']['automatic_calculation_enabled'])

    def test_metadata_change_same_save_invalidates_review_and_clear_selection(self):
        task = self.acknowledge(self.candidate())
        task = self.save(task, task['policy_input'], source_options={task['documents'][0]['id']: {
            'confirmed_metadata': {'unit': '万元'}}})
        self.assertFalse(task['policy_assessment']['results'][0]['reviewed'])
        task = self.save(task, {'selected': [], 'facts': {}, 'reviews': {}})
        self.assertIsNone(task['policy_assessment'])

    def test_vat_fact_and_policy_review_coexist_in_frozen_report_and_isolation(self):
        task = self.upload(self.task(), invoices([('P1', '1', '200.0123', '蓝字', '正常')]))
        task = self.review(self.acknowledge(self.candidate(task)))
        checked = self.run_check(task)
        self.assertEqual(checked['bundle']['fact_assessment']['amount'], '100.0000')
        self.assertEqual(checked['bundle']['risk_assessment']['status'], 'NEEDS_REVIEW')
        task = self.client.get(self.url(task)).json()
        self.assertTrue(task['policy_assessment']['results'][0]['reviewed'])
        url = self.url(task, '/reports')
        response = self.client.post(url, json={'expected_revision': task['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        old = self.client.get(url + '/1?format=json').json()
        html = self.client.get(url + '/1').text
        self.assertIn('政策适用与复核依据', html)
        self.assertIn('合成业务依据', html)
        self.assertIn('100.0000', html)
        self.assertIn('风险仍待核查', html)
        self.assertEqual(old['policy_assessment'], task['policy_assessment'])
        inputs = deepcopy(task['policy_input'])
        inputs['facts']['vat_sales']['value'] = '100000'
        task = self.save(task, inputs)
        self.assertEqual(task['policy_assessment']['results'][0]['status'], 'not_applicable')
        response = self.client.post(self.url(task, '/recheck'), json={'expected_revision': task['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(url, json={'expected_revision': response.json()['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['version'], 2)
        self.assertEqual(self.client.get(url + '/1?format=json').json(), old)
        self.login('kg-other')
        self.assertEqual(self.client.get(self.url(task)).status_code, 404)
        self.assertEqual(self.client.get(url + '/1').status_code, 404)
        self.assertEqual(self.client.post(self.url(task, '/prepare'), json={'expected_revision': task['revision'], 'policy_input': inputs}).status_code, 404)

    def test_catalog_change_projects_stale_blocks_archive_and_rechecks(self):
        task = self.acknowledge(self.candidate())
        result = self.run_check(task)
        task = self.client.get(self.url(task)).json()
        target = Path(self.tmp.name) / 'policy-catalog.yaml'
        target.write_bytes(policies.CATALOG_PATH.read_bytes() + b'\n ')
        with patch.object(policies, 'CATALOG_PATH', target):
            projected = self.client.get(self.url(task)).json()
            self.assertFalse(projected['policy_result_current'])
            self.assertFalse(projected['policy_assessment']['results'][0]['reviewed'])
            response = self.client.post(self.url(task, '/reports'), json={'expected_revision': result['revision']})
            self.assertEqual(response.status_code, 409, response.text)
            response = self.client.post(self.url(task, '/recheck'), json={'expected_revision': result['revision']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['policy_changed'])
            self.assertFalse(response.json()['policy_assessment']['results'][0]['reviewed'])

    def test_archive_revalidates_policy_after_template_render(self):
        task = self.acknowledge(self.candidate())
        checked = self.run_check(task)
        original = core_policies.current_for_report
        calls = []

        def race(payload):
            calls.append(True)
            if len(calls) == 2:
                from webapp.access import AccessDenied
                raise AccessDenied('政策版本变化', 409)
            return original(payload)

        with patch.object(core_policies, 'current_for_report', side_effect=race):
            response = self.client.post(self.url(task, '/reports'), json={'expected_revision': checked['revision']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get(self.url(task, '/reports')).json(), [])

    def test_invalid_input_stale_write_and_catalog_unavailable(self):
        task = self.candidate()
        for value in ({'policy_assessment': {}}, {'policy_input': {'selected': ['unknown']}},
                      {'policy_input': {'selected': ['VAT-THRESHOLD-2026'], 'facts': vat(vat_sales='NaN')}}):
            response = self.client.post(self.url(task, '/prepare'), json={'expected_revision': task['revision'], **value})
            self.assertEqual(response.status_code, 422, response.text)
        response = self.client.post(self.url(task, '/prepare'), json={'expected_revision': task['revision']-1, 'policy_input': task['policy_input']})
        self.assertEqual(response.status_code, 409)
        checked = self.run_check(task)
        with patch.object(policies, 'program_current', return_value=False):
            response = self.client.post(self.url(task, '/recheck'), json={'expected_revision': checked['revision']})
            self.assertEqual(response.status_code, 409, response.text)
