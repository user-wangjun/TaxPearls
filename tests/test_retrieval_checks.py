"""Durable platform checks, authorization, scheduling and real offline execution."""
import json
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.kg_retrieval import semantic_search
from webapp import retrieval_checks as checks
from webapp.access import AccessDenied
from webapp.storage import Store


def passing_report():
    return {'gate_passed': True, 'data_scope': 'synthetic', 'model_fingerprints': ['synthetic-model'],
            'implementation_fingerprint': checks.implementation_fingerprint(), 'gold_cases_hash': checks.cases_version(),
            'case_count': 13, 'passed_count': 13,
            'cases': [{'id': f'K{i:02}', 'passed': True, 'failures': [], 'expected': ['synthetic'], 'actual': ['synthetic']} for i in range(1, 14)],
            'metrics': {'recall': 1, 'precision': 1, 'exception_coverage': 1, 'change_relation_coverage': 1,
                        'source_traceability': 1, 'wrong_period_region_uses': 0, 'invented_references': 0,
                        'duplicate_clauses': 0, 'semantic_ready': True, 'external_api_calls': 0}}


class RetrievalCheckTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'TAXPEARLS_FIELD_KEY': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
                                             'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '0'})
        environment.start(); self.addCleanup(environment.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'test.db')
        self.admin = self.store.create_initial_admin('platform', 'Test-checks-2026!', '平台', 'platform')
        self.other = self.store.create_user('business', 'Test-checks-2026!', '机构', 'org_admin', 'business')
        self.actor = self.admin
        state = patch.object(checks, '_store_getter', lambda: self.store)
        state.start(); self.addCleanup(state.stop)
        model = patch.object(checks.semantic_search, 'available', return_value=(SimpleNamespace(fingerprint='synthetic-model', manifest={'model_id': 'synthetic-model'}), ''))
        model.start(); self.addCleanup(model.stop)
        app = FastAPI()

        @app.exception_handler(AccessDenied)
        def denied(request, error):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': str(error)}, status_code=error.status)

        def allow(actor, *roles):
            if actor['role'] not in roles:
                raise HTTPException(403, '无权访问')

        checks.register(app, lambda: self.store, lambda session: self.actor, allow)
        self.client = TestClient(app)

    def test_only_live_platform_admin_can_read_start_or_configure(self):
        for actor in (self.other, {**self.admin, 'role': 'accountant'}):
            self.actor = actor
            self.assertEqual(self.client.get('/api/platform/retrieval-checks').status_code, 403)
            self.assertEqual(self.client.post('/api/platform/retrieval-checks').status_code, 403)
            self.assertEqual(self.client.put('/api/platform/retrieval-checks/schedule', json={'enabled': True, 'interval_hours': 24, 'revision': 0}).status_code, 403)
        self.actor = self.admin
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (self.admin['id'],))
        self.assertEqual(self.client.post('/api/platform/retrieval-checks').status_code, 403)

    def test_manual_check_is_async_single_flight_and_survives_new_store(self):
        queued = self.client.post('/api/platform/retrieval-checks')
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(self.client.post('/api/platform/retrieval-checks').status_code, 409)
        checks.Worker(lambda: self.store, runner=lambda stop: passing_report()).tick()
        result = self.client.get('/api/platform/retrieval-checks/' + queued.json()['id']).json()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['passed_count'], 13)
        self.assertTrue(checks.current_gate(Store(self.store.path))['passed'])
        state = self.client.get('/api/platform/retrieval-checks').json()
        self.assertTrue(state['gate']['passed'])
        self.assertEqual(state['history'][0]['id'], result['id'])

    def test_failed_case_is_preserved_and_cannot_enable_gate(self):
        report = passing_report()
        report['cases'][0].update(passed=False, failures=['缺少期望条款：synthetic'])
        report.update(passed_count=12, gate_passed=False)
        identity = checks.enqueue(self.store, self.admin)['id']
        checks.Worker(lambda: self.store, runner=lambda stop: report).tick()
        result = self.client.get('/api/platform/retrieval-checks/' + identity).json()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['report']['cases'][0]['failures'], ['缺少期望条款：synthetic'])
        self.assertFalse(checks.current_gate(self.store)['passed'])

    def test_schedule_is_persistent_due_only_and_uses_revision(self):
        body = {'enabled': True, 'interval_hours': 24, 'revision': 0}
        self.assertEqual(self.client.put('/api/platform/retrieval-checks/schedule', json=body).status_code, 200)
        self.assertEqual(self.client.put('/api/platform/retrieval-checks/schedule', json=body).status_code, 409)
        for invalid in (0, 721, 1.5):
            self.assertEqual(self.client.put('/api/platform/retrieval-checks/schedule', json={**body, 'interval_hours': invalid}).status_code, 422)
        calls = []
        worker = checks.Worker(lambda: Store(self.store.path), runner=lambda stop: calls.append(True) or passing_report())
        worker.tick(); self.assertEqual(calls, [])
        with self.store.connect() as db:
            db.execute('UPDATE retrieval_check_settings SET next_due=0')
        worker.tick(); worker.tick()
        self.assertEqual(len(calls), 1)
        state = self.client.get('/api/platform/retrieval-checks').json()
        self.assertEqual(state['history'][0]['trigger'], 'scheduled')
        self.assertTrue(state['schedule']['next_due'])
        self.assertEqual(self.client.put('/api/platform/retrieval-checks/schedule', json={'enabled': False, 'interval_hours': 24, 'revision': 1}).status_code, 200)

    def test_two_workers_share_lease_and_do_not_repeat_a_job(self):
        checks.enqueue(self.store, self.admin)
        entered, release = Event(), Event()
        calls = []

        def runner(stop):
            calls.append(True); entered.set()
            release.wait(5)
            return passing_report()

        first = checks.Worker(lambda: self.store, runner=runner)
        thread = Thread(target=first.tick); thread.start()
        try:
            self.assertTrue(entered.wait(5))
            checks.Worker(lambda: self.store, runner=runner).tick()
            self.assertEqual(len(calls), 1)
        finally:
            release.set(); thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_version_drift_missing_model_and_revoked_actor_never_publish_pass(self):
        for kind in ('code', 'cases', 'revoke', 'model'):
            with self.subTest(kind=kind):
                with self.store.connect() as db:
                    db.execute('UPDATE users SET active=1 WHERE id=?', (self.admin['id'],))
                checks.enqueue(self.store, self.admin)

                def runner(stop):
                    report = passing_report()
                    if kind == 'code':
                        report['implementation_fingerprint'] = 'old'
                    elif kind == 'cases':
                        report['gold_cases_hash'] = 'old'
                    elif kind == 'revoke':
                        with self.store.connect() as db:
                            db.execute('UPDATE users SET active=0 WHERE id=?', (self.admin['id'],))
                    return report

                checks.Worker(lambda: self.store, runner=runner).tick()
                if kind == 'model':
                    with patch.object(checks.semantic_search, 'available', return_value=(None, 'missing')):
                        self.assertEqual(checks.current_gate(self.store)['code'], 'model_unavailable')
                    with patch.object(checks.semantic_search, 'available', return_value=(SimpleNamespace(fingerprint='other'), '')):
                        self.assertEqual(checks.current_gate(self.store)['code'], 'model_changed')
                else:
                    self.assertFalse(checks.current_gate(self.store)['passed'])

    def test_stale_running_job_is_recorded_as_interrupted(self):
        identity = checks.enqueue(self.store, self.admin)['id']
        with self.store.connect() as db:
            db.execute("UPDATE retrieval_check_runs SET status='running',lease_until=0 WHERE id=?", (identity,))
        checks.Worker(lambda: self.store, runner=lambda stop: self.fail('must not run an expired claim')).tick()
        result = self.client.get('/api/platform/retrieval-checks/' + identity).json()
        self.assertEqual(result['status'], 'interrupted')
        self.assertFalse(checks.current_gate(self.store)['passed'])

    def test_missing_test_files_are_reported_without_endless_queued_job(self):
        identity = checks.enqueue(self.store, self.admin)['id']
        with patch.object(checks, 'CASES', Path('missing-synthetic-cases.json')):
            checks.Worker(lambda: self.store, runner=lambda stop: self.fail('must not run')).tick()
            result = self.client.get('/api/platform/retrieval-checks/' + identity).json()
            self.assertEqual(result['status'], 'error')
            self.assertEqual(checks.current_gate(self.store)['code'], 'test_files_missing')
            self.assertEqual(self.client.get('/api/platform/retrieval-checks').status_code, 200)


class OfflineExecutionTests(unittest.TestCase):
    def test_real_isolated_suite_never_calls_external_llm(self):
        model, _ = semantic_search.available()
        if model is None:
            self.skipTest('optional local embedding model is not installed')
        with tempfile.TemporaryDirectory() as directory, patch('src.ai_transport.chat_content', side_effect=AssertionError('no external LLM')):
            target = Path(directory) / 'result.json'
            checks.evaluate_child(target)
            report = json.loads(target.read_text(encoding='utf-8'))
        self.assertTrue(checks.report_gate(report), report)
        self.assertEqual(report['case_count'], 13)
        self.assertEqual(report['metrics']['external_api_calls'], 0)


if __name__ == '__main__':
    unittest.main()
