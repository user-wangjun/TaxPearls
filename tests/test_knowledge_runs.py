"""Real HTTP/storage concurrency, snapshot isolation and resumable check runs."""
from copy import deepcopy
import asyncio
import json
from threading import Barrier, Event, Lock
import time
import unittest
from unittest.mock import patch

from src.kg_retrieval import semantic_search
from tests.enterprise_support import material_key
from tests import test_kg_retrieval as retrieval_tests
from webapp import knowledge_evidence as service, knowledge_runs, material_batches as batches
from webapp.access import AccessDenied


@material_key
class CheckRunTests(unittest.TestCase):
    def setUp(self):
        # This suite supplies its own explicit policy corpus. Public-package
        # startup has separate acceptance tests and would replace these fixtures.
        startup = patch('src.public_policy_package.bootstrap', return_value={'status': 'fixture'})
        startup.start()
        self.addCleanup(startup.stop)
        retrieval_tests.RetrievalHTTPTests.setUp(self)

    login = retrieval_tests.RetrievalHTTPTests.login
    task = retrieval_tests.RetrievalHTTPTests.task

    def run_body(self, task, rules=('R01', 'R02', 'R03')):
        return {'task_id': task['id'], 'expected_revision': task['revision'],
                'rule_ids': list(rules), 'region_code': 'CN-33', 'use_ai': True}

    def events(self, body):
        response = self.client.post('/api/knowledge/check-runs', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('application/x-ndjson', response.headers['content-type'])
        return [json.loads(line) for line in response.text.splitlines()]

    def test_one_failed_check_does_not_skip_other_requested_checks(self):
        task = self.task()
        def analyze(package):
            failed = package['check']['rule_id'] == 'R01'
            return {'validated': not failed, 'assessments': [], 'reason': '模拟单项失败' if failed else '',
                    'batches': [{'status': 'failed' if failed else 'completed'}]}
        rules = tuple(f'R{i:02}' for i in range(1, 7))
        with patch.object(service, 'analyze', side_effect=analyze):
            events = self.events(self.run_body(task, rules))
        self.assertEqual({e['rule_id'] for e in events if e['type'] == 'result'}, set(rules))
        self.assertEqual(events[-1]['type'], 'error')

    @staticmethod
    def answer(system, payload, failure):
        evidence = [*payload['policy_evidence'], *payload['policy_leads']]
        return {'assessments': [{'type': payload['response_constraints']['required_type'],
                                'text': '纯合成测试说明，请核对原始依据及缺失材料。',
                                'rule_id': payload['check']['rule_id'],
                                'clause_ids': [evidence[0]['clause_id']] if evidence else [],
                                'source_ids': [], 'amounts': [], 'dates': [], 'required_materials': []}]}, 'unit-test-provider'

    def gate(self):
        model, _ = semantic_search.available()
        return patch.object(service, 'acceptance_gate', return_value={
            'passed': True, 'model_fingerprints': [model.fingerprint if model else None]})

    def test_three_checks_really_overlap_and_save_without_lost_updates(self):
        task = self.task()
        barrier, lock = Barrier(3), Lock()
        first_saved = Event()
        active = peak = 0
        append = batches._append
        def save(*args, **kwargs):
            result = append(*args, **kwargs)
            if 'R02' in args[4].get('tax_checks', {}):
                first_saved.set()
            return result
        def provider(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=10)
            # Prove that one result is saved before its siblings finish, without
            # depending on sleep precision or the order of simultaneous futures.
            if args[1]['check']['rule_id'] != 'R02':
                self.assertTrue(first_saved.wait(timeout=10), 'first result must be saved while siblings still run')
            with lock:
                active -= 1
            return self.answer(*args)
        with self.gate(), patch('webapp.knowledge._chat_json', side_effect=provider), \
                patch.object(batches, '_append', side_effect=save), \
                patch.object(batches, 'read', side_effect=AssertionError('full history must not be loaded')), \
                patch.object(batches, 'read_current', wraps=batches.read_current) as read:
            events = self.events(self.run_body(task))
            self.assertEqual(read.call_count, 1)
        self.assertEqual(peak, 3)
        self.assertEqual(events[-1]['type'], 'complete', events)
        results = [event for event in events if event['type'] == 'result']
        self.assertEqual(results[0]['rule_id'], 'R02')
        self.assertEqual({r['rule_id'] for r in results}, {'R01', 'R02', 'R03'})
        self.assertEqual([r['result']['revision'] for r in results], list(range(task['revision'] + 1, task['revision'] + 4)))
        saved = self.client.get('/api/enterprise/input-tasks/' + task['id']).json()
        self.assertTrue(all(saved['tax_checks'][rid]['ai']['validated'] for rid in ('R01', 'R02', 'R03')))
        self.assertEqual({s['input_revision'] for s in saved['tax_checks'].values()}, {task['revision']})

    def test_completed_results_are_reused_on_next_run(self):
        task = self.task()
        with self.gate(), patch('webapp.knowledge._chat_json', side_effect=self.answer) as ai:
            first = self.events(self.run_body(task))
            self.assertEqual(first[-1]['type'], 'complete', first)
            count = ai.call_count
            second = self.events({**self.run_body(task), 'expected_revision': first[-1]['revision']})
            self.assertEqual(second[-1]['type'], 'complete', second)
            self.assertEqual(ai.call_count, count)
            self.assertEqual(second[-1]['revision'], first[-1]['revision'])
            self.assertTrue(all(e['result']['cached'] for e in second if e['type'] == 'result'))

    def test_larger_run_stays_bounded_and_duplicate_start_is_rejected(self):
        task = self.task()
        barrier, lock = Barrier(3), Lock()
        active = peak = 0
        attempts = []
        def analyze(package):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=10)
            if package['check']['rule_id'] == 'R01':
                duplicate = self.client.post('/api/knowledge/check-runs', json=self.run_body(task))
                single = self.client.post('/api/knowledge/retrieve', json={
                    'task_id': task['id'], 'expected_revision': task['revision'], 'rule_id': 'R01', 'use_ai': True})
                attempts.extend([duplicate.status_code, single.status_code])
            time.sleep(.03)
            with lock:
                active -= 1
            return {'validated': True, 'batches': [], 'assessments': []}
        with patch.object(service, 'analyze', side_effect=analyze):
            events = self.events(self.run_body(task, tuple(f'R{i:02}' for i in range(1, 7))))
        self.assertEqual(events[-1]['type'], 'complete', events)
        self.assertEqual(len(events[-1]['completed_rule_ids']), 6)
        self.assertEqual(peak, 3)
        self.assertEqual(attempts, [409, 409])

    def test_disconnect_stops_queued_checks_and_releases_the_task(self):
        task = self.task()
        token = self.store.authenticate('kg-admin', 'Knowledge-test-2026!')[1]
        endpoint = next(route.endpoint for route in self.client.app.routes if route.path == '/api/knowledge/check-runs')
        barrier = Barrier(3)
        def analyze(package):
            barrier.wait(timeout=10)
            return {'validated': True, 'batches': [], 'assessments': []}
        async def disconnect():
            response = endpoint(knowledge_runs.CheckRunBody(**self.run_body(task, tuple(f'R{i:02}' for i in range(1, 7)))), token)
            stream = response.body_iterator
            self.assertEqual(json.loads(await anext(stream))['type'], 'started')
            self.assertEqual(json.loads(await anext(stream))['type'], 'result')
            await stream.aclose()
        with patch.object(service, 'analyze', side_effect=analyze) as ai:
            asyncio.run(disconnect())
        self.assertEqual(ai.call_count, 3, 'disconnect must not submit the other three checks')
        self.assertFalse(knowledge_runs._ACTIVE)
        saved = self.client.get('/api/enterprise/input-tasks/' + task['id']).json()
        self.assertEqual(len(saved['tax_checks']), 1, 'only results saved before disconnect remain current')

    def test_material_edit_during_parallel_ai_cannot_be_overwritten(self):
        task = self.task()
        barrier, lock = Barrier(3), Lock()
        edited = False
        actor = self.store.authenticate('kg-admin', 'Knowledge-test-2026!')[0]
        def analyze(package):
            nonlocal edited
            barrier.wait(timeout=10)
            with lock:
                if not edited:
                    current = batches.read_current(self.store, actor, task['id'])
                    payload = deepcopy(current['versions'][-1]['payload'])
                    payload['task']['company_name'] = '纯合成测试修改后的企业'
                    batches.append_revision(self.store, actor, task['id'], task['revision'], 'edit', payload)
                    edited = True
            return {'validated': True, 'batches': [], 'assessments': []}
        with patch.object(service, 'analyze', side_effect=analyze):
            events = self.events(self.run_body(task))
        self.assertEqual(events[-1]['type'], 'error', events)
        self.assertEqual(events[-1]['status'], 409)
        self.assertFalse(any(e['type'] == 'result' for e in events))
        saved = self.client.get('/api/enterprise/input-tasks/' + task['id']).json()
        self.assertEqual(saved['task']['company_name'], '纯合成测试修改后的企业')
        self.assertFalse(saved.get('tax_checks'))

    def test_failed_rule_saves_other_inflight_and_queued_results(self):
        task = self.task()
        barrier = Barrier(3)
        def analyze(package):
            rid = package['check']['rule_id']
            if rid != 'R04':
                barrier.wait(timeout=10)
            if rid == 'R01':
                return {'validated': False, 'reason': '合成失败，保留已完成批次', 'batches': [{'status': 'failed'}],
                        'batch_results': [{'batch_id': 'synthetic', 'status': 'completed'}], 'assessments': []}
            time.sleep(.04)
            return {'validated': True, 'batches': [], 'assessments': []}
        with patch.object(service, 'analyze', side_effect=analyze) as ai:
            events = self.events(self.run_body(task, ('R01', 'R02', 'R03', 'R04')))
        self.assertEqual(ai.call_count, 4)
        self.assertEqual(events[-1]['type'], 'error', events)
        saved = self.client.get('/api/enterprise/input-tasks/' + task['id']).json()
        self.assertEqual(set(saved['tax_checks']), {'R01', 'R02', 'R03', 'R04'})
        self.assertEqual(saved['tax_checks']['R01']['ai']['batch_results'][0]['batch_id'], 'synthetic')

    def test_permission_revocation_stops_before_another_batch_or_result_write(self):
        task = self.task()
        def analyze(package):
            with self.store.connect() as db:
                db.execute("UPDATE users SET active=0 WHERE username='kg-admin'")
            service._AUTHORIZE_BATCH.get()()
            self.fail('revoked actor must never reach the next request')
        with patch.object(service, 'analyze', side_effect=analyze):
            events = self.events(self.run_body(task, ('R01',)))
        self.assertEqual(events[-1]['type'], 'error', events)
        self.assertEqual(events[-1]['status'], 403)
        self.assertFalse(any(e['type'] == 'result' for e in events))

    def test_foreign_task_stale_version_and_duplicate_rules_never_call_ai(self):
        task = self.task()
        with patch.object(service, 'analyze') as ai:
            stale = self.client.post('/api/knowledge/check-runs', json={**self.run_body(task), 'expected_revision': 0})
            self.assertEqual(stale.status_code, 409)
            duplicate = self.client.post('/api/knowledge/check-runs', json=self.run_body(task, ('R01', 'R01')))
            self.assertEqual(duplicate.status_code, 422)
            self.login('kg-other')
            foreign = self.client.post('/api/knowledge/check-runs', json=self.run_body(task))
            self.assertEqual(foreign.status_code, 404)
            ai.assert_not_called()
        self.assertFalse(knowledge_runs._ACTIVE)

    def test_busy_task_does_not_disclose_state_to_unassigned_accountant(self):
        task = self.task()
        admin = self.store.authenticate('kg-admin', 'Knowledge-test-2026!')[0]
        person = self.store.create_user('unassigned', 'Knowledge-test-2026!', '会计', 'accountant', admin['org_id'])
        token = self.store.authenticate(person['username'], 'Knowledge-test-2026!')[1]
        self.client.cookies.set('taxpearls_session', token)
        key = (admin['org_id'], task['id'])
        with knowledge_runs._LOCK:
            knowledge_runs._ACTIVE.add(key)
        try:
            with patch.object(service, 'analyze') as ai:
                run = self.client.post('/api/knowledge/check-runs', json=self.run_body(task))
                single = self.client.post('/api/knowledge/retrieve', json={
                    'task_id': task['id'], 'expected_revision': task['revision'], 'rule_id': 'R01', 'use_ai': True})
                self.assertEqual(run.status_code, 404, run.text)
                self.assertEqual(single.status_code, 404, single.text)
                ai.assert_not_called()
        finally:
            with knowledge_runs._LOCK:
                knowledge_runs._ACTIVE.discard(key)

    def test_current_read_does_not_decrypt_old_versions_or_change_audit_history(self):
        task = self.task()
        actor = self.store.authenticate('kg-admin', 'Knowledge-test-2026!')[0]
        view = batches.read_current(self.store, actor, task['id'])
        revision = task['revision']
        for _ in range(8):
            revision = batches.append_revision(self.store, actor, task['id'], revision, 'edit', view['versions'][-1]['payload'])
        opened = []
        original = batches._open
        def tracked(cipher, context):
            opened.append(json.loads(context))
            return original(cipher, context)
        with patch.object(batches, '_open', side_effect=tracked):
            current = batches.read_current(self.store, actor, task['id'])
        self.assertEqual(len(current['versions']), 1)
        self.assertEqual(current['revision'], revision)
        revisions = [row for row in opened if row[3].startswith('revision:')]
        self.assertEqual([row[4] for row in revisions], [str(revision)])
        history = batches.read(self.store, actor, task['id'])
        self.assertGreater(len(history['versions']), 8)
        self.assertTrue(history['events'])
        self.assertEqual(history['versions'][-1]['payload'], current['versions'][-1]['payload'])
        self.login('kg-other')
        with self.assertRaises(AccessDenied):
            other = self.store.authenticate('kg-other', 'Knowledge-test-2026!')[0]
            batches.read_current(self.store, other, task['id'])
