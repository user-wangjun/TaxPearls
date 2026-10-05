"""Durable enterprise queue acceptance, including real local HTTP transport."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import engine, materials
from src.ai_transport import REQUEST_GUARD, chat_content
from src.settings import AISettings
from tests.test_materials import accounts, COMPANY, zip_bytes, workbook
from webapp import app as module, material_batches as batches, material_jobs as jobs
from webapp.access import AccessDenied
from webapp.storage import Store


class MaterialJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY':base64.b64encode(b'q'*32).decode(),
            'TAXPEARLS_AI_ENABLED':'0','TAXPEARLS_MATERIAL_QUEUE_ENABLED':'1',
            'TAXPEARLS_MATERIAL_WORKERS':'4','TAXPEARLS_AI_CONCURRENCY':'2',
            'TAXPEARLS_MATERIAL_ORG_CONCURRENCY':'2','TAXPEARLS_MATERIAL_QUEUE_CAPACITY':'100',
            'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED':'0'})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Path(self.tmp.name)/'queue.db')
        self.admin = self.store.create_user('queue-admin','Queue-tests-2026!','管理员','org_admin','queue')
        self.foreign = self.store.create_user('queue-other','Queue-tests-2026!','其他机构','org_admin','other')
        replacement = patch.object(module,'store',self.store)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.worker = module.app.state.material_worker
        start = patch.object(self.worker,'start')
        start.start()
        self.addCleanup(start.stop)
        self.client = TestClient(module.app,raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.login(self.admin)
        self.rules = engine.load_rules(module.RULES_DIR)
        self.config = jobs.QueueSettings.from_env()

    def login(self, actor):
        self.client.cookies.clear()
        self.client.cookies.set(module.COOKIE_NAME,self.store.authenticate(actor['username'],'Queue-tests-2026!')[1])

    def upload(self, raw=None, name='账.xlsx'):
        response = self.client.post('/api/enterprise/materials',files={'files':(name,raw or accounts())})
        self.assertEqual(response.status_code,202,response.text)
        return response.json()

    def url(self, item, suffix=''):
        return '/api/enterprise/materials/'+item['id']+suffix

    def get(self, item):
        response = self.client.get(self.url(item))
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def enqueue(self, actor=None):
        return jobs.submit(self.store,actor or self.admin,[('synthetic.xlsx',accounts())],'local',self.rules)

    def test_queue_omits_all_credentials_and_uses_the_current_key_pool(self):
        from src.ai_extraction import Extraction
        from tests.test_materials import FIXTURES
        queued = AISettings(enabled=True, api_key='synthetic-queued-primary',
                            backup_api_keys=('synthetic-retired-backup',))
        current = replace(queued, api_key='synthetic-current-primary',
                          backup_api_keys=('synthetic-current-backup',))
        observed = []

        def answer(settings, _messages, _timeout):
            observed.append(settings.api_keys)
            return Extraction(company={}, rows=[], warnings=[])

        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                with patch('webapp.enterprise_materials.AISettings.from_env', return_value=queued):
                    response = self.client.post('/api/enterprise/materials', data={'extraction': 'ai'},
                        files={'files': ('扫描.pdf', (FIXTURES / 'materials-scanned.pdf').read_bytes())})
                self.assertEqual(response.status_code, 202, response.text)
                item = response.json()
                job_id = item['jobs'][0]['id']
                row = self.row(job_id)
                context = batches._context(row['org_id'], row['batch_id'], 'job_input', job_id)
                snapshot = json.loads(batches._open(row['input_cipher'], context))
                self.assertNotIn('api_key', snapshot['settings'])
                self.assertNotIn('backup_api_keys', snapshot['settings'])
                for secret in queued.api_keys:
                    self.assertNotIn(secret, json.dumps(snapshot))
                if legacy:
                    snapshot['settings'].update(api_key=queued.api_key,
                                                backup_api_keys=list(queued.backup_api_keys))
                    with self.store.connect() as db:
                        db.execute('UPDATE material_jobs SET input_cipher=? WHERE id=?',
                            (batches._seal(batches._json(snapshot), context), job_id))
                with patch('webapp.enterprise_materials.AISettings.from_env', return_value=current), \
                        patch('src.ai_extraction.call_model', side_effect=answer):
                    self.assertTrue(self.worker.run_once())
                completed = self.row(job_id)
                self.assertEqual(completed['state'], 'done')
                cached = json.loads(batches._open(completed['result_cipher'],
                    batches._context(row['org_id'], row['batch_id'], 'job_result', job_id)))
                self.assertNotIn('api_key', cached['signature']['settings'])
                self.assertNotIn('backup_api_keys', cached['signature']['settings'])
                for secret in (*queued.api_keys, *current.api_keys):
                    self.assertNotIn(secret, json.dumps(cached))
        self.assertEqual(observed, [current.api_keys, current.api_keys])

    def test_failed_ai_candidate_cannot_confirm_or_be_unblocked_by_value_edits(self):
        from src.ai_extraction import ExtractionError
        from tests.test_materials import FIXTURES
        settings = AISettings(enabled=True, api_key='synthetic-gate-key', vision=True)
        with patch('webapp.enterprise_materials.AISettings.from_env', return_value=settings), \
                patch('src.ai_extraction.call_model', side_effect=ExtractionError('synthetic schema failure', code='schema')):
            response = self.client.post('/api/enterprise/materials', data={'extraction': 'ai'}, files=[
                ('files', ('账.xlsx', accounts())),
                ('files', ('扫描.pdf', (FIXTURES/'materials-scanned.pdf').read_bytes()))])
            self.assertEqual(response.status_code, 202, response.text)
            item = response.json()
            self.assertTrue(self.worker.run_once())
        view = self.get(item)
        self.assertEqual(view['jobs'][0]['state'], 'done')
        self.assertFalse(view['analysis']['can_confirm'])
        failed = next(d for d in view['documents'] if d.get('extraction', {}).get('status') == 'failed')
        denied = self.client.post(self.url(item, '/confirm'), json={'expected_revision': view['revision']})
        self.assertEqual(denied.status_code, 409, denied.text[:500])
        edited = self.client.post(self.url(item, '/analyze'), json={'expected_revision': view['revision'],
            'selections': {failed['id']: {'rows': []}}})
        self.assertEqual(edited.status_code, 200, edited.text[:500])
        self.assertFalse(edited.json()['analysis']['can_confirm'])
        self.assertEqual(self.client.post(self.url(item, '/confirm'),
            json={'expected_revision': edited.json()['revision']}).status_code, 409)
        ready = self.client.post(self.url(item, '/analyze'), json={'expected_revision': edited.json()['revision'],
            'selections': {failed['id']: {'purpose': 'excluded'}}})
        self.assertEqual(ready.status_code, 200, ready.text[:500])
        self.assertTrue(ready.json()['analysis']['can_confirm'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audits').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM material_executions').fetchone()[0], 0)

    def row(self, job_id):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM material_jobs WHERE id=?',(job_id,)).fetchone())

    def test_http_upload_returns_saved_queue_before_parse_and_status_does_not_log(self):
        with patch('webapp.enterprise_materials.materials.preview') as parser:
            item = self.upload()
        parser.assert_not_called()
        self.assertEqual(item['jobs'][0]['state'],'queued')
        self.assertFalse(item['analysis']['can_confirm'])
        with self.store.connect() as db:
            job = db.execute('SELECT * FROM material_jobs').fetchone()
            self.assertEqual(db.execute('SELECT state FROM material_operations WHERE id=?',(job['operation_id'],)).fetchone()[0],'committed')
            before = db.execute('SELECT COUNT(*) FROM material_events').fetchone()[0]
            self.assertNotIn(b'synthetic.xlsx',job['input_cipher'])
            original = db.execute('SELECT * FROM material_originals').fetchone()
            self.assertNotEqual(original['bytes_cipher'],accounts())
        for _ in range(4):
            self.assertEqual(self.client.get(self.url(item,'/status')).json()['jobs'][0]['state'],'queued')
        with self.store.connect() as db:
            self.assertEqual(before,db.execute('SELECT COUNT(*) FROM material_events').fetchone()[0])
        self.assertTrue(self.worker.run_once())
        done = self.get(item)
        self.assertEqual(done['jobs'][0]['state'],'done')
        self.assertEqual(len(done['documents']),1)
        trace = self.client.get(self.url(item,'/trace')).json()
        events = [e for e in trace['events'] if e['job_id']==job['id']]
        self.assertEqual([e['action'] for e in events],['job_queued','job_started','analysis','job_done'])
        self.assertTrue(all(e['system'] for e in events))
        self.assertEqual(len({e['seq'] for e in trace['events']}),len(trace['events']))
        confirmation = self.client.post(self.url(item,'/confirm'),json={'expected_revision':done['revision']})
        self.assertEqual(confirmation.status_code,200,confirmation.text)

    def test_queue_capacity_and_transaction_failure_leave_no_partial_batch(self):
        self.upload()
        with patch.dict(os.environ,{'TAXPEARLS_MATERIAL_QUEUE_CAPACITY':'1'}):
            rejected = self.client.post('/api/enterprise/materials',files={'files':('second.xlsx',accounts())})
        self.assertEqual(rejected.status_code,429)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM material_batches').fetchone()[0],1)
            db.execute("CREATE TRIGGER fail_enqueue BEFORE INSERT ON material_jobs BEGIN SELECT RAISE(ABORT,'test'); END")
        failed = self.client.post('/api/enterprise/materials',files={'files':('rollback.xlsx',accounts())})
        self.assertEqual(failed.status_code,500)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM material_batches').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM material_originals').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT state FROM material_operations WHERE id=?',(failed.headers['x-material-operation'],)).fetchone()[0],'failed')

    def test_full_queue_can_replace_same_batch_queued_input(self):
        entry = self.enqueue()
        with patch.dict(os.environ,{'TAXPEARLS_MATERIAL_QUEUE_CAPACITY':'1'}):
            replacement = jobs.submit(self.store,self.admin,[('C.xlsx',accounts())],'local',self.rules,
                                      batch_id=entry['id'],revision=entry['revision'])
        self.assertEqual(self.row(entry['job_id'])['state'],'superseded')
        self.assertEqual(self.row(replacement['job_id'])['state'],'queued')

    def test_parallel_claims_respect_shared_worker_org_and_batch_limits(self):
        for _ in range(4):
            self.enqueue()
            self.enqueue(self.foreign)
        reopened = Store(self.store.path)
        config = replace(self.config,workers=3,per_org=1)
        with self.store.connect() as db:
            jobs.limits(db,config,configure=True)
        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(lambda i:jobs.claim(self.store if i%2 else reopened,self.config),range(12)))
        claimed = [j for j in claims if j]
        self.assertEqual(len(claimed),2)
        self.assertEqual(len({j['id'] for j in claimed}),2)
        self.assertEqual(len({j['org_id'] for j in claimed}),2)
        self.assertEqual(len({j['batch_id'] for j in claimed}),2)

    def test_supplement_during_running_job_fences_result_and_reuses_old_parse(self):
        item = self.upload()
        old = jobs.claim(self.store,self.config)
        signature, previous, reusable, uploads = jobs.inputs(self.store,old)
        payload = self.worker.process(old,signature,previous,reusable,uploads)
        added = self.client.post(self.url(item,'/supplement'),data={'expected_revision':str(item['revision'])},
                                 files={'files':('补传.xlsx',accounts(amount=200000))})
        self.assertEqual(added.status_code,202,added.text)
        self.assertIsNone(jobs.claim(self.store,self.config))
        self.assertTrue(jobs.finish(self.store,old,payload,signature,config=self.config))
        self.assertEqual(self.row(old['id'])['state'],'superseded')
        self.assertFalse(jobs.finish(self.store,old,payload,signature,config=self.config))
        successor = jobs.claim(self.store,self.config)
        snapshot, previous, documents, uploads = jobs.inputs(self.store,successor)
        self.assertIsNone(previous)
        self.assertEqual(len(documents),1)
        self.assertEqual([f[1] for f in uploads],['补传.xlsx'])
        result = self.worker.process(successor,snapshot,previous,documents,uploads)
        self.assertTrue(jobs.finish(self.store,successor,result,snapshot,config=self.config))
        done = self.get(item)
        self.assertEqual(len(done['documents']),2)
        self.assertEqual(done['analysis_revision'],done['revision'])

    def test_queued_supersession_and_simultaneous_supplements_are_atomic(self):
        first = self.enqueue()
        def supplement(name):
            try:
                return jobs.submit(self.store,self.admin,[(name,accounts())],'local',self.rules,
                                   batch_id=first['id'],revision=first['revision'])
            except AccessDenied as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(supplement,['B.xlsx','C.xlsx']))
        self.assertEqual(responses.count(409),1)
        self.assertEqual(self.row(first['job_id'])['state'],'superseded')
        view = batches.read(self.store,self.admin,first['id'])
        self.assertEqual(len(view['files']),2)
        self.assertTrue(any(e['action']=='job_superseded' and e['job_id']==first['job_id'] for e in view['events']))

    def test_restart_expired_lease_reclaims_and_old_owner_cannot_finish_or_renew(self):
        entry = self.enqueue()
        old = jobs.claim(self.store,self.config)
        with self.store.connect() as db:
            db.execute('UPDATE material_jobs SET lease_until=0 WHERE id=?',(old['id'],))
        reopened = Store(self.store.path)
        new = jobs.claim(reopened,self.config)
        self.assertEqual(new['id'],old['id'])
        self.assertEqual(new['attempts'],2)
        lease = self.row(old['id'])['lease_until']
        jobs.heartbeat(self.store,old,self.config)
        self.assertEqual(self.row(old['id'])['lease_until'],lease)
        self.assertFalse(jobs.finish(self.store,old,failure='stale',config=self.config))
        with self.store.connect() as db:
            db.execute('UPDATE material_jobs SET lease_until=0 WHERE id=?',(new['id'],))
        self.assertIsNone(jobs.claim(reopened,self.config))
        self.assertEqual(self.row(new['id'])['state'],'failed')
        self.assertEqual(jobs.status(reopened,self.admin,entry['id'])['revision'],entry['revision'])

    def test_broken_expired_input_fails_closed_without_stalling_other_batches(self):
        first = self.enqueue()
        old = jobs.claim(self.store, self.config)
        second = self.enqueue(self.foreign)
        with self.store.connect() as db:
            db.execute('UPDATE material_jobs SET lease_until=0,input_cipher=? WHERE id=?',
                       (b'corrupted', old['id']))
        healthy = jobs.claim(self.store, self.config)
        self.assertEqual(healthy['id'], second['job_id'])
        self.assertEqual(self.row(first['job_id'])['state'], 'failed')
        self.assertEqual(self.row(first['job_id'])['failure_code'], 'input')
        self.assertIsNone(self.row(first['job_id'])['lease_token'])

    def test_inputs_are_encrypted_frozen_and_cannot_be_swapped_between_jobs(self):
        first,second = self.enqueue(),self.enqueue(self.foreign)
        one,two = jobs.claim(self.store,self.config),jobs.claim(self.store,self.config)
        frozen = jobs.inputs(self.store,one)[0]
        with patch.dict(os.environ,{'TAXPEARLS_AI_MODEL':'changed-after-submit'}):
            self.assertEqual(jobs.inputs(self.store,one)[0],frozen)
        with self.store.connect() as db:
            db.execute('UPDATE material_jobs SET input_cipher=? WHERE id=?',(one['input_cipher'],two['id']))
        two = self.row(two['id'])
        with self.assertRaises(batches.MaterialStorageError):
            jobs.inputs(self.store,two)
        self.assertNotEqual(first['id'],second['id'])

    def test_retry_limit_and_partially_failed_zip_reprocess_the_whole_original(self):
        entry = jobs.submit(self.store,self.admin,[('batch.zip',zip_bytes([('A.xlsx',accounts()),('B.xlsx',accounts())]))],'local',self.rules)
        old = jobs.claim(self.store,self.config)
        snapshot,previous,docs,uploads = jobs.inputs(self.store,old)
        payload = self.worker.process(old,snapshot,previous,docs,uploads)
        payload['documents'][1]['extraction'] = {'method':'ai_failed','failure_code':'network'}
        jobs.finish(self.store,old,payload,snapshot,failure='provider_unavailable',retry=True,config=self.config)
        with self.store.connect() as db:
            db.execute('UPDATE material_jobs SET available_at=0 WHERE id=?',(old['id'],))
        second = jobs.claim(self.store,self.config)
        _,_,reused,pending = jobs.inputs(self.store,second)
        self.assertFalse(reused)
        self.assertEqual([u[1] for u in pending],['batch.zip'])
        jobs.finish(self.store,second,failure='provider_unavailable',retry=True,config=self.config)
        self.assertEqual(self.row(entry['job_id'])['state'],'failed')
        trace = batches.read(self.store,self.admin,entry['id'])
        attempt = next(e for e in trace['events'] if e['action']=='job_retry')
        self.assertEqual(attempt['detail']['extractions'][1]['extraction']['failure_code'],'network')

    def test_ai_call_budget_is_shared_by_all_new_originals_in_one_task(self):
        from src.ai_extraction import Extraction
        raw = workbook('非标准附表',[['项目','数值'],['销售额',100000]])
        settings = {'TAXPEARLS_AI_ENABLED':'1','TAXPEARLS_AI_API_KEY':'synthetic',
                    'TAXPEARLS_AI_BASE_URL':'http://127.0.0.1:1','TAXPEARLS_AI_MAX_CALLS':'1'}
        with patch.dict(os.environ,settings):
            entry = jobs.submit(self.store,self.admin,[('A.xlsx',raw),('B.xlsx',raw)],'ai',self.rules)
            with patch('src.ai_extraction.call_model',return_value=Extraction(company=COMPANY,rows=[],warnings=[])) as call:
                self.worker.run_once()
            self.assertEqual(call.call_count,1)
        job = self.row(entry['job_id'])
        result = batches.read(self.store,self.admin,entry['id'])['versions'][-1]['payload']
        self.assertEqual(job['state'],'done')
        self.assertEqual(result['documents'][1]['extraction']['failure_code'],'limit')

    def test_model_429_retries_with_attempt_trace_but_401_does_not(self):
        from src.ai_extraction import Extraction, ExtractionError
        raw = workbook('非标准附表',[['项目','数值'],['销售额',100000]])
        settings = {'TAXPEARLS_AI_ENABLED':'1','TAXPEARLS_AI_API_KEY':'synthetic',
                    'TAXPEARLS_AI_BASE_URL':'http://127.0.0.1:1'}
        with patch.dict(os.environ,settings):
            entry = jobs.submit(self.store,self.admin,[('A.xlsx',raw)],'ai',self.rules)
            limited = ExtractionError('synthetic 429',code='http',status=429)
            answer = Extraction(company=COMPANY,rows=[],warnings=[])
            with patch('src.ai_extraction.call_model',side_effect=[limited,answer]) as call:
                self.worker.run_once()
                self.assertEqual(self.row(entry['job_id'])['state'],'queued')
                with self.store.connect() as db:
                    db.execute('UPDATE material_jobs SET available_at=0 WHERE id=?',(entry['job_id'],))
                self.worker.run_once()
            self.assertEqual(call.call_count,2)
            self.assertEqual(self.row(entry['job_id'])['state'],'done')
            view = batches.read(self.store,self.admin,entry['id'])
            first = next(e for e in view['events'] if e['action']=='job_retry')
            self.assertEqual(first['detail']['extractions'][0]['extraction']['http_status'],429)
            refused = jobs.submit(self.store,self.admin,[('B.xlsx',raw)],'ai',self.rules)
            with patch('src.ai_extraction.call_model',side_effect=ExtractionError('synthetic 401',code='http',status=401)) as call:
                self.worker.run_once()
            self.assertEqual(call.call_count,1)
            self.assertEqual(self.row(refused['job_id'])['state'],'failed')
            self.assertEqual(self.row(refused['job_id'])['failure_code'],'provider_error')

    def test_revoked_user_is_failed_before_parsing_and_slot_requires_valid_lease(self):
        first = self.enqueue()
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?',(self.admin['id'],))
        with patch.object(self.worker,'process') as parser:
            self.assertFalse(self.worker.run_once())
        parser.assert_not_called()
        self.assertEqual(self.row(first['job_id'])['failure_code'],'permission')
        self.enqueue(self.foreign)
        claimed = jobs.claim(self.store,self.config)
        with self.store.connect() as db:
            db.execute('UPDATE material_jobs SET lease_until=0 WHERE id=?',(claimed['id'],))
        with self.assertRaises(AccessDenied),jobs.ai_slot(self.store,claimed,self.config):
            self.fail('expired owner obtained a slot')

    def test_failed_task_manual_retry_keeps_originals_and_is_journalled(self):
        item = self.upload()
        old = jobs.claim(self.store,self.config)
        context = batches._context(old['org_id'],old['batch_id'],'job_input',old['id'])
        snapshot = json.loads(batches._open(old['input_cipher'],context))
        snapshot['settings'].update(api_key='synthetic-retired-primary', backup_api_keys=['synthetic-retired-backup'])
        with self.store.connect() as db:
            db.execute('UPDATE material_jobs SET input_cipher=? WHERE id=?',
                       (batches._seal(batches._json(snapshot),context),old['id']))
        jobs.finish(self.store,old,failure='processing',config=self.config)
        response = self.client.post(self.url(item,'/retry'),json={'expected_revision':item['revision'],'job_id':old['id']})
        self.assertEqual(response.status_code,202,response.text)
        new = response.json()['jobs'][0]
        self.assertNotEqual(new['id'],old['id'])
        copied = self.row(new['id'])
        retried = json.loads(batches._open(copied['input_cipher'],
            batches._context(copied['org_id'],copied['batch_id'],'job_input',copied['id'])))
        self.assertNotIn('api_key', retried['settings'])
        self.assertNotIn('backup_api_keys', retried['settings'])
        self.assertNotIn('synthetic-retired',json.dumps(retried))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM material_originals').fetchone()[0],1)
            record = db.execute('SELECT * FROM material_operations WHERE id=?',(response.headers['x-material-operation'],)).fetchone()
            self.assertEqual((record['action'],record['state']),('retry','committed'))
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.row(new['id'])['state'],'done')

    def test_failed_supplement_cannot_promote_old_analysis_by_manual_edit(self):
        item = self.upload()
        self.worker.run_once()
        ready = self.get(item)
        added = self.client.post(self.url(item,'/supplement'),
            data={'expected_revision':str(ready['revision'])},files={'files':('C.xlsx',accounts(amount=200000))})
        self.assertEqual(added.status_code,202,added.text)
        pending = added.json()
        with patch.object(self.worker,'process',side_effect=RuntimeError('synthetic failure')):
            self.worker.run_once()
            with self.store.connect() as db:
                db.execute('UPDATE material_jobs SET available_at=0 WHERE batch_id=? AND state=?',(item['id'],'queued'))
            self.worker.run_once()
        self.assertEqual(self.get(item)['jobs'][0]['state'],'failed')
        response = self.client.post(self.url(item,'/analyze'),json={
            'expected_revision':pending['revision'],'company':{'industry':'attempted stale edit'}})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.get(item)['revision'],pending['revision'])
        self.assertEqual(self.client.post(self.url(item,'/confirm'),json={
            'expected_revision':ready['revision']}).status_code,409)
        retried = self.client.post(self.url(item,'/retry'),json={
            'expected_revision':pending['revision'],'job_id':pending['jobs'][0]['id']})
        self.assertEqual(retried.status_code,202,retried.text)
        self.worker.run_once()
        completed = self.get(item)
        self.assertEqual(completed['analysis_revision'],completed['revision'])
        self.assertEqual(len(completed['documents']),2)

    def test_deleted_old_original_stays_visible_until_explicit_exclusion(self):
        item = self.upload()
        self.worker.run_once()
        ready = self.get(item)
        old_doc = ready['documents'][0]
        removed = self.client.request('DELETE',self.url(item,'/originals/'+ready['files'][0]['id']),
                                      json={'expected_revision':ready['revision']})
        self.assertEqual(removed.status_code,200,removed.text)
        changed = self.get(item)
        added = self.client.post(self.url(item,'/supplement'),
            data={'expected_revision':str(changed['revision'])},files={'files':('C.xlsx',accounts())})
        self.assertEqual(added.status_code,202,added.text)
        self.worker.run_once()
        candidate = self.get(item)
        self.assertEqual(len(candidate['documents']),2)
        old = next(d for d in candidate['documents'] if d['id']==old_doc['id'])
        self.assertIn('原件已删除',old['error'])
        self.assertFalse(candidate['analysis']['can_confirm'])
        saved = self.client.post(self.url(item,'/analyze'),json={'expected_revision':candidate['revision'],
            'selections':{old_doc['id']:{'purpose':'excluded'}}})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertTrue(saved.json()['analysis']['can_confirm'])

    def test_cross_tenant_status_retry_and_permission_revocation(self):
        item = self.upload()
        old = jobs.claim(self.store,self.config)
        snapshot, previous, docs, uploads = jobs.inputs(self.store,old)
        result = self.worker.process(old,snapshot,previous,docs,uploads)
        self.login(self.foreign)
        for suffix in ('','/status','/trace'):
            self.assertEqual(self.client.get(self.url(item,suffix)).status_code,404)
        self.assertEqual(self.client.post(self.url(item,'/retry'),json={'expected_revision':item['revision'],'job_id':old['id']}).status_code,404)
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?',(self.admin['id'],))
        jobs.finish(self.store,old,result,snapshot,config=self.config)
        self.assertEqual(self.row(old['id'])['state'],'failed')
        self.assertIsNone(self.row(old['id'])['result_cipher'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM material_revisions WHERE kind='analysis'").fetchone()[0],0)

    def test_finish_event_failure_rolls_back_result_and_queue_state(self):
        entry = self.enqueue()
        job = jobs.claim(self.store,self.config)
        snapshot, previous, docs, uploads = jobs.inputs(self.store,job)
        result = self.worker.process(job,snapshot,previous,docs,uploads)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_done BEFORE INSERT ON material_events WHEN NEW.action='job_done' BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            jobs.finish(self.store,job,result,snapshot,config=self.config)
        row = self.row(job['id'])
        self.assertEqual(row['state'],'running')
        self.assertIsNone(row['result_cipher'])
        self.assertEqual(jobs.status(self.store,self.admin,entry['id'])['revision'],entry['revision'])

    def test_local_http_calls_use_global_slots_and_isolated_messages(self):
        observed, errors = [], []
        lock = threading.Lock()
        active = peak = 0
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_POST(provider):
                nonlocal active,peak
                body = json.loads(provider.rfile.read(int(provider.headers['Content-Length'])))
                marker = body['messages'][-1]['content']
                with lock:
                    active += 1
                    peak = max(peak,active)
                    observed.append(marker)
                time.sleep(.1)
                raw = json.dumps({'choices':[{'finish_reason':'stop','message':{'content':marker}}]}).encode()
                provider.send_response(200)
                provider.send_header('Content-Length',str(len(raw)))
                provider.end_headers()
                provider.wfile.write(raw)
                with lock:
                    active -= 1
        server = ThreadingHTTPServer(('127.0.0.1',0),Provider)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = replace(self.config,workers=6,per_org=6,ai_concurrency=2)
        with self.store.connect() as db:
            jobs.limits(db,config,configure=True)
        for _ in range(6):
            self.enqueue()
        claimed = [jobs.claim(self.store,config) for _ in range(6)]
        settings = AISettings(enabled=True,api_key='synthetic',base_url=f'http://127.0.0.1:{server.server_port}',model='synthetic')
        def call(job):
            token = REQUEST_GUARD.set(lambda timeout:jobs.ai_slot(self.store,job,config,timeout))
            try:
                marker = job['id']
                answer = chat_content(settings,[{'role':'user','content':marker}],5)
                if answer!=marker:
                    errors.append(marker)
            finally:
                REQUEST_GUARD.reset(token)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(call,claimed))
        self.assertEqual(peak,2)
        self.assertFalse(errors)
        self.assertEqual(set(observed),{j['id'] for j in claimed})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM material_ai_slots').fetchone()[0],0)

    def test_supplement_preserves_user_edit_and_frozen_confirmed_result(self):
        item = self.upload()
        self.worker.run_once()
        ready = self.get(item)
        edited = self.client.post(self.url(item,'/analyze'),json={'expected_revision':ready['revision'],
            'company':{'industry':'用户行业'},'selections':{ready['documents'][0]['id']:{'standard_edits':{'科目余额表!E2':'150000'}}}})
        self.assertEqual(edited.status_code,200,edited.text)
        edited = edited.json()
        confirmed = self.client.post(self.url(item,'/confirm'),json={'expected_revision':edited['revision']})
        self.assertEqual(confirmed.status_code,200,confirmed.text)
        audit_id = confirmed.json()['audit_id']
        frozen = self.client.get('/api/audits/'+audit_id+'/materials').json()['confirmation']
        current = self.get(item)
        changed_company = {**COMPANY,'industry':'新增候选行业'}
        added = self.client.post(self.url(item,'/supplement'),data={'expected_revision':str(current['revision'])},
                                 files={'files':('补传.xlsx',accounts(company=changed_company))})
        self.assertEqual(added.status_code,202,added.text)
        with patch('webapp.enterprise_materials.materials.preview',wraps=materials.preview) as parse:
            self.worker.run_once()
        self.assertEqual(parse.call_count,1)
        newest = self.get(item)
        self.assertEqual(newest['company']['industry'],'用户行业')
        self.assertEqual(newest['selections'],edited['selections'])
        historic = self.client.get('/api/audits/'+audit_id+'/materials').json()['confirmation']
        for key in ('analysis_sha256','documents','company','analysis_revision','confirmation_revision'):
            self.assertEqual(historic[key],frozen[key],key)


if __name__=='__main__':
    unittest.main()
