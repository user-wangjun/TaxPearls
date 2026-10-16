"""Policy UI/API inputs, protected revisions and frozen reports, no real client."""
from copy import deepcopy
import json
import os
from unittest.mock import patch

from src import policies, materials
from tests.test_policies import vat, cit, rd, facts
from tests.test_enterprise_materials import EnterpriseMaterialFixture
from tests.test_materials import accounts, COMPANY
from webapp import app as module, material_batches


class EnterprisePolicyTests(EnterpriseMaterialFixture):
    def policy_upload(self, value=None, raw=None):
        value=value if value is not None else {'selected':['VAT-THRESHOLD-2026'],'facts':vat()}
        response=self.client.post('/api/enterprise/materials',data={'policy_input':json.dumps(value)},files={'files':('合成政策测试.xlsx',raw or accounts())})
        self.assertIn(response.status_code,(200,202),response.text)
        return response.json()

    def acknowledge(self, item):
        value=deepcopy(item['policy_input'])
        value['reviews']={r['id']:r['review_token'] for r in item['analysis']['policy_assessment']['results'] if r['review_token']}
        response=self.client.post(self.url(item,'/analyze'),json={'expected_revision':item['revision'],'policy_input':value})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_upload_before_analysis_explicit_review_frozen_report_and_original_24(self):
        item=self.policy_upload()
        self.assertEqual(item['analysis']['policy_assessment']['results'][0]['status'],'needs_review')
        self.assertTrue(item['analysis']['can_confirm'])
        item=self.acknowledge(item)
        assessment=item['analysis']['policy_assessment']
        self.assertEqual(assessment['results'][0]['status'],'applicable')
        result=self.confirm(item)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['policy_assessment'],assessment)
        self.assertEqual(len([r for r in result.json()['findings'] if r['id'].startswith('R-')]),24)
        audit_id=result.json()['audit_id']
        frozen=material_batches.confirmed(self.store,self.admin,audit_id)
        self.assertEqual(frozen['policy_assessment'],assessment)
        archived=self.store.get_report_version(audit_id,1)
        self.assertIn('政策适用与复核依据',archived['html'])
        self.assertIn('所采条件符合（已复核）',archived['html'])
        self.assertIn('合成业务依据',archived['html'])
        self.assertEqual(self.confirm(item).json()['audit_id'],audit_id)
        inputs=deepcopy(item['policy_input']);inputs['facts']['vat_sales']['value']='100000'
        changed=self.client.post(self.url(item,'/analyze'),json={'expected_revision':self.client.get(self.url(item)).json()['revision'],'policy_input':inputs})
        self.assertEqual(changed.status_code,200,changed.text)
        self.assertEqual(changed.json()['analysis']['policy_assessment']['results'][0]['status'],'not_applicable')
        self.assertEqual(self.store.get_report_version(audit_id,1)['html'],archived['html'])
        self.assertEqual(material_batches.confirmed(self.store,self.admin,audit_id)['policy_assessment'],assessment)

    def test_missing_facts_can_continue_rules_but_never_grant_eligibility(self):
        item=self.policy_upload({'selected':['CIT-SMALL']})
        self.assertEqual(item['analysis']['policy_assessment']['results'][0]['status'],'needs_review')
        result=self.confirm(item)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['policy_assessment']['results'][0]['status'],'needs_review')

    def test_identity_conflict_survives_forged_review_and_freezes_without_blocking_24_rules(self):
        values = {**vat(), **facts(six_tax='property', six_eligibility='new_general',
            six_new='yes', six_before_settlement='yes', cit_nonrestricted='yes',
            six_people='100', six_assets='1000000', six_other_reliefs='yes')}
        item = self.policy_upload({'selected': ['VAT-THRESHOLD-2026', 'SMALL-SIX-TWO'], 'facts': values})
        inputs = deepcopy(item['policy_input'])
        inputs['reviews'] = {key: 'a' * 64 for key in inputs['selected']}
        response = self.client.post(self.url(item, '/analyze'), json={
            'expected_revision': item['revision'], 'policy_input': inputs})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()
        assessment = item['analysis']['policy_assessment']
        self.assertEqual(assessment['consistency']['status'], 'conflict')
        self.assertTrue(item['analysis']['can_confirm'])
        self.assertTrue(all(r['status'] == 'needs_review' and r['review_token'] is None
            and not r['reviewed'] for r in assessment['results']))
        result = self.confirm(item)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['policy_assessment'], assessment)
        self.assertEqual(len([r for r in result.json()['findings'] if r['id'].startswith('R-')]), 24)
        audit_id = result.json()['audit_id']
        archived = self.store.get_report_version(audit_id, 1)
        self.assertIn('跨政策核对：存在业务事实冲突', archived['html'])
        self.assertNotIn('所采条件符合（已复核）', archived['html'])
        # Correct the qualification route; require a fresh acknowledgement.
        current = self.client.get(self.url(item)).json()
        inputs['facts'].update(facts(six_eligibility='small_vat'))
        response = self.client.post(self.url(item, '/analyze'), json={
            'expected_revision': current['revision'], 'policy_input': inputs})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()
        self.assertEqual(item['analysis']['policy_assessment']['consistency']['status'], 'needs_review')
        item = self.acknowledge(item)
        self.assertEqual(item['analysis']['policy_assessment']['consistency']['status'], 'checked')
        self.assertTrue(all(r['status'] == 'applicable' for r in item['analysis']['policy_assessment']['results']))
        self.assertEqual(self.store.get_report_version(audit_id, 1)['html'], archived['html'])
        self.assertEqual(material_batches.confirmed(self.store, self.admin, audit_id)['policy_assessment'], assessment)

    def test_independent_qualifications_keep_unresolved_combination_visible_in_report(self):
        item = self.policy_upload({'selected': ['CIT-SMALL', 'CIT-RD'], 'facts': {**cit(), **rd()}},
            accounts({**COMPANY, 'period': '2026'}))
        item = self.acknowledge(item)
        assessment = item['analysis']['policy_assessment']
        self.assertTrue(all(r['status'] == 'applicable' for r in assessment['results']))
        self.assertEqual(assessment['consistency']['status'], 'needs_review')
        self.assertFalse(assessment['consistency']['combination_conclusion_enabled'])
        result = self.confirm(item)
        self.assertEqual(result.status_code, 200, result.text)
        html = self.store.get_report_version(result.json()['audit_id'], 1)['html']
        self.assertIn('跨政策核对：仍有待核对事项，不能形成组合适用结论', html)
        self.assertIn('所采条件符合（已复核）', html)

    def test_queued_analysis_also_enforces_cross_policy_consistency(self):
        values = {**vat(), **facts(six_tax='property', six_eligibility='new_general',
            six_new='yes', six_before_settlement='yes', cit_nonrestricted='yes',
            six_people='100', six_assets='1000000', six_other_reliefs='yes')}
        worker = module.app.state.material_worker
        with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '1'}), patch.object(worker, 'start'):
            item = self.policy_upload({'selected': ['VAT-THRESHOLD-2026', 'SMALL-SIX-TWO'], 'facts': values})
            self.assertIsNone(item['analysis_revision'])
            self.assertTrue(worker.run_once())
            current = self.client.get(self.url(item)).json()
            self.assertEqual(current['analysis']['policy_assessment']['consistency']['status'], 'conflict')
            self.assertTrue(all(r['review_token'] is None
                for r in current['analysis']['policy_assessment']['results']))

    def test_metadata_changes_clear_qualification_and_trace_records_policy_edits(self):
        item=self.acknowledge(self.policy_upload())
        inputs=deepcopy(item['policy_input']);inputs['facts']['vat_sales']['source']='另一个合成材料页'
        edited=self.client.post(self.url(item,'/analyze'),json={'expected_revision':item['revision'],'policy_input':inputs})
        self.assertEqual(edited.status_code,200,edited.text)
        item=edited.json()
        self.assertEqual(item['analysis']['policy_assessment']['results'][0]['status'],'needs_review')
        trace=self.client.get(self.url(item,'/trace')).json()
        self.assertIn('policy_input',[c['field'] for c in trace['versions'][-1]['detail']['changes']])
        self.assertEqual(self.confirm({**item,'revision':item['revision']-1}).status_code,409)

    def test_catalog_updates_block_new_confirmation_but_not_idempotent_frozen_report(self):
        item=self.acknowledge(self.policy_upload())
        with patch.object(policies,'fingerprint',return_value='f'*64):
            loaded=self.client.get(self.url(item)).json()
            self.assertTrue(loaded['policy_reanalysis_required'])
            self.assertFalse(loaded['analysis']['can_confirm'])
            self.assertEqual(self.confirm(item).status_code,409)
            saved=self.client.post(self.url(item,'/analyze'),json={'expected_revision':item['revision']})
            self.assertEqual(saved.status_code,200,saved.text)
            self.assertEqual(saved.json()['analysis']['policy_assessment']['results'][0]['status'],'needs_review')
        # Reanalyze on current code then explicitly review before confirming.
        item=self.client.get(self.url(item)).json()
        item=self.client.post(self.url(item,'/analyze'),json={'expected_revision':item['revision']}).json()
        item=self.acknowledge(item)
        result=self.confirm(item)
        self.assertEqual(result.status_code,200,result.text)
        with patch.object(policies,'fingerprint',return_value='e'*64):
            self.assertEqual(self.confirm(item).json()['audit_id'],result.json()['audit_id'])

    def test_catalog_change_in_commit_transaction_rolls_back_audit_and_report(self):
        item=self.acknowledge(self.policy_upload())
        original=material_batches.record_execution
        def during_commit(*args,**kwargs):
            with patch.object(policies,'fingerprint',return_value='a'*64):
                return original(*args,**kwargs)
        before=self.counts()
        with patch.object(material_batches,'record_execution',side_effect=during_commit):
            self.assertEqual(self.confirm(item).status_code,409)
        self.assertEqual(self.counts(),before)
        # The catalog can also change between assertion and recomputation.
        # Fail as a version conflict, not a server error, and roll back all rows.
        from src.input_errors import InputError
        def during_reassessment(*args,**kwargs):
            with patch.object(policies,'assess',side_effect=InputError('目录读取期间变化')):
                return original(*args,**kwargs)
        with patch.object(material_batches,'record_execution',side_effect=during_reassessment):
            self.assertEqual(self.confirm(item).status_code,409)
        self.assertEqual(self.counts(),before)

    def test_queue_and_supplement_preserve_encrypted_facts_without_leaking_eligibility_to_archive(self):
        worker=module.app.state.material_worker
        with patch.dict(os.environ,{'TAXPEARLS_MATERIAL_QUEUE_ENABLED':'1'}),patch.object(worker,'start'):
            item=self.policy_upload()
            self.assertEqual(item['analysis_revision'],None)
            with self.store.connect() as db:
                ciphertext=db.execute('SELECT input_cipher FROM material_jobs').fetchone()[0]
                self.assertNotIn('合成业务依据'.encode(),ciphertext)
            self.assertTrue(worker.run_once())
            item=self.client.get(self.url(item)).json()
            self.assertEqual(item['policy_input']['facts'],vat())
            item=self.acknowledge(item)
            supplemented=self.client.post(self.url(item,'/supplement'),data={'expected_revision':str(item['revision'])},files={'files':('补传政策测试.xlsx',accounts())})
            self.assertEqual(supplemented.status_code,202,supplemented.text)
            self.assertTrue(worker.run_once())
            current=self.client.get(self.url(item)).json()
            self.assertEqual(current['analysis']['policy_assessment']['results'][0]['status'],'applicable')
            confirmed=self.confirm(current)
            self.assertEqual(confirmed.status_code,200,confirmed.text)
            archive=self.store.list_clients(self.admin)[0]
            profile=self.client.get('/api/enterprise/client-profile/'+archive['id']).json()
            self.assertNotIn('policy_input',profile)
            self.assertNotIn('policy_assessment',profile)

    def test_untrusted_output_rejected_before_parse_and_cross_tenant_denied(self):
        for inputs in [{'selected':['FAKE']},{'selected':['CIT-SMALL'],'status':'applicable'}, {'selected':['CIT-SMALL'],'facts':{'cit_income':{'value':True,'source':'x'}}}]:
            with patch.object(materials,'preview') as parser:
                response=self.client.post('/api/enterprise/materials',data={'policy_input':json.dumps(inputs)},files={'files':('bad.xlsx',accounts())})
                self.assertEqual(response.status_code,422,response.text)
                parser.assert_not_called()
        item=self.policy_upload()
        for actor in [self.foreign,self.teacher]:
            self.login(actor)
            self.assertIn(self.client.get(self.url(item)).status_code,(403,404))
            self.assertIn(self.client.post(self.url(item,'/analyze'),json={'expected_revision':item['revision'],'policy_input':{}}).status_code,(403,404))
        self.login(self.admin)
        response=self.client.post(self.url(item,'/analyze'),json={'expected_revision':item['revision'],'policy_assessment':{'status':'applicable'}})
        self.assertEqual(response.status_code,422,response.text)
