"""SQLite acceptance with synthetic workbooks and explicitly simulated AI delays."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from src import core_materials
from src.settings import AISettings
from tests import test_core_inputs as fixtures
from tests.test_core_inputs import book
from webapp import app as module, core_material_jobs as core_jobs, material_jobs as jobs
from webapp.storage import Store


def resource_process(path, user, ready, release, output):
    store = Store(path)
    tokens = [jobs.reserve_resource(store, user, 'parse') for _ in range(2)]
    output.put((user['org_id'], sum(bool(token) for token in tokens)))
    ready.set()
    release.wait(20)
    for token in tokens:
        if token:
            jobs.release_resource(store, token)
    store.close()


class CoreConcurrencyTests(unittest.TestCase):
    create = fixtures.CoreAPITests.create
    login = fixtures.CoreAPITests.login

    def setUp(self):
        fixtures.CoreAPITests.setUp(self)
        self.store._open_keepalive()
        self.addCleanup(self.store.close)
        self.worker = module.app.state.material_worker
        env = patch.dict(os.environ, {'TAXPEARLS_MATERIAL_QUEUE_ENABLED':'1'})
        env.start();self.addCleanup(env.stop)

    def enqueue(self, task=None, *, names=('one.xlsx','two.xlsx','three.xlsx','four.xlsx'), ai=False, raw=None, ocr=False):
        task = task or self.create()
        response = self.client.post('/api/enterprise/input-tasks/' + task['id'] + '/files',
            headers={'Prefer':'respond-async'},
            data={'expected_revision':str(task['revision']),'material_type':'vat_declaration','mapping_mode':'ai' if ai else 'local','ocr':'1' if ocr else '0'},
            files=[('files',(name,(raw[name] if isinstance(raw,dict) else raw) or book('vat_declaration'))) for name in names])
        self.assertEqual(response.status_code,202,response.text)
        saved = response.json()
        self.assertEqual(saved['jobs'][0]['state'],'queued')
        self.assertEqual(len(saved['files']),len(task['files']) + len(names))
        return saved

    def read(self, task):
        response = self.client.get('/api/enterprise/input-tasks/' + task['id'])
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_two_files_overlap_and_database_is_writable_during_parse(self):
        task = self.enqueue()
        original = core_materials.parse
        lock = threading.Lock()
        active, peak = 0, 0

        def parse(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak,active)
            # A separate SQLite writer succeeds while the parser is still running.
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE material_jobs SET available_at=available_at WHERE id=?", (task['jobs'][0]['id'],))
            time.sleep(.18)  # Simulated expensive parser/remote wait; no throughput claim.
            try:
                return original(*args,**kwargs)
            finally:
                with lock:
                    active -= 1

        with patch.object(core_materials,'parse',side_effect=parse):
            self.worker.run_once()
        result = self.read(task)
        self.assertEqual(peak,2)
        self.assertEqual(result['jobs'][0]['state'],'done')
        states = result['jobs'][0]['files']
        self.assertTrue(states[0]['started_at'] < states[1]['finished_at'] and states[1]['started_at'] < states[0]['finished_at'])
        self.assertTrue(all(d['records'][0]['field_sources']['declared_value']['position'] for d in result['documents']))
        # Arrival order and dedup/source aggregation stay deterministic.
        self.assertEqual([d['name'] for d in result['documents']],['one.xlsx','two.xlsx','three.xlsx','four.xlsx'])
        self.assertEqual(len(result['processing']['records']),2)
        self.assertTrue(any(i['code']=='duplicate_file' for i in result['processing']['notices']))

    def test_partial_failure_retry_reuses_success_and_configuration_invalidates_cache(self):
        task = self.enqueue(names=('good.xlsx','fail.xlsx'))
        original = core_materials.parse
        calls = []

        def parse(raw,name,*args,**kwargs):
            calls.append(name)
            if name == 'fail.xlsx':
                raise RuntimeError('synthetic parser failure')
            return original(raw,name,*args,**kwargs)

        with patch.object(core_materials,'parse',side_effect=parse):
            self.worker.run_once()
        partial = self.read(task)
        self.assertEqual(partial['jobs'][0]['state'],'failed')
        self.assertEqual({f['state'] for f in partial['jobs'][0]['files']},{'done','failed'})
        self.assertTrue(partial['documents'][0]['records'])
        retry = self.client.post('/api/enterprise/input-tasks/' + task['id'] + '/jobs/' + task['jobs'][0]['id'] + '/retry',json={'expected_revision':partial['revision']})
        self.assertEqual(retry.status_code,202,retry.text)
        calls.clear()
        with patch.object(core_materials,'parse',wraps=original) as parser:
            self.worker.run_once()
        result = self.read(task)
        self.assertEqual([c.args[1] for c in parser.call_args_list],['fail.xlsx'])
        self.assertEqual(result['jobs'][0]['state'],'done')
        self.assertEqual(sum(f['reused'] for f in result['jobs'][0]['files']),1)
        file_id=result['files'][0]['id']
        # Human confirmation is an overlay on cached raw evidence.
        change={'expected_revision':result['revision'],'source_options':{file_id:{'source_reviewed':True}}}
        prepared=self.client.post('/api/enterprise/input-tasks/'+task['id']+'/prepare',headers={'Prefer':'respond-async'},json=change).json()
        with patch.object(core_materials,'parse',wraps=original) as parser:
            self.worker.run_once()
        reviewed=self.read(prepared)
        self.assertEqual(parser.call_count,0)
        self.assertTrue(reviewed['source_options'][file_id]['source_reviewed'])
        with patch.object(core_materials,'PARSER_VERSION','synthetic-new-parser'):
            core_jobs.submit(self.store,self.admin,task['id'],reviewed['revision'])
            with patch.object(core_materials,'parse',wraps=original) as parser:
                self.worker.run_once()
            self.assertEqual(parser.call_count,2)

    def test_supplement_delete_scope_and_permission_fence_stale_results(self):
        for mutation in ('supplement','delete','scope','permission'):
            with self.subTest(mutation=mutation):
                task = self.enqueue(names=('one.xlsx','two.xlsx'))
                entered,release = threading.Event(),threading.Event()
                original = core_materials.parse

                def parse(*args,**kwargs):
                    entered.set()
                    self.assertTrue(release.wait(10))
                    return original(*args,**kwargs)

                with patch.object(core_materials,'parse',side_effect=parse), ThreadPoolExecutor(1) as executor:
                    running=executor.submit(self.worker.run_once)
                    self.assertTrue(entered.wait(10))
                    if mutation == 'supplement':
                        changed=self.enqueue(task,names=('new.xlsx',))
                    elif mutation == 'delete':
                        response=self.client.request('DELETE','/api/enterprise/input-tasks/'+task['id']+'/files/'+task['files'][0]['id'],json={'expected_revision':task['revision'],'confirm':True})
                        self.assertEqual(response.status_code,200,response.text)
                        changed=response.json()
                    elif mutation == 'scope':
                        response=self.client.post('/api/enterprise/input-tasks/'+task['id']+'/scope',json={**{k:task['task'][k] for k in core_materials.SCOPE_FIELDS},'expected_revision':task['revision'],'company_name':'手工修改企业','confirm':True})
                        self.assertEqual(response.status_code,200,response.text)
                        changed=response.json()
                    else:
                        with self.store.connect() as db:
                            db.execute('UPDATE users SET active=0 WHERE id=?',(self.admin['id'],))
                    release.set();running.result(timeout=15)
                with self.store.connect() as db:
                    state=db.execute('SELECT state FROM material_jobs WHERE id=?',(task['jobs'][0]['id'],)).fetchone()[0]
                    if mutation == 'permission':
                        self.assertEqual(state,'failed')
                        self.assertEqual(db.execute('SELECT revision FROM material_batches WHERE id=?',(task['id'],)).fetchone()[0],task['revision'])
                        db.execute('UPDATE users SET active=1 WHERE id=?',(self.admin['id'],))
                    else:
                        self.assertEqual(state,'superseded')
                if mutation != 'permission':
                    self.assertEqual(self.read(task)['revision'],changed['revision'])
                if mutation == 'supplement':
                    self.worker.run_once()
                    self.assertEqual(len(self.read(task)['documents']),3)

    def test_multiple_users_and_database_wide_process_limits(self):
        first=self.enqueue(names=('a.xlsx','b.xlsx'))
        self.login(self.foreign)
        second=self.enqueue(names=('c.xlsx','d.xlsx'))
        entered=threading.Barrier(4)
        original=core_materials.parse
        def parse(*args,**kwargs):
            entered.wait(timeout=10)
            return original(*args,**kwargs)
        with patch.object(core_materials,'parse',side_effect=parse), ThreadPoolExecutor(2) as executor:
            list(executor.map(lambda _: self.worker.run_once(), range(2)))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM material_jobs WHERE state='done'").fetchone()[0],2)
        self.assertEqual(self.client.get('/api/enterprise/input-tasks/'+first['id']+'/jobs').status_code,404)
        self.assertEqual(self.read(second)['jobs'][0]['state'],'done')
        third=self.store.create_user('core-third','Core-test-2026!','第三机构','org_admin','third-org')
        ctx=multiprocessing.get_context('spawn')
        output,release=ctx.Queue(),ctx.Event()
        processes=[]
        try:
            for user in (self.admin,self.foreign,third):
                ready=ctx.Event()
                process=ctx.Process(target=resource_process,args=(self.store.path,user,ready,release,output))
                process.start();processes.append(process)
                self.assertTrue(ready.wait(15))
            counts=[output.get(timeout=5) for _ in processes]
            self.assertEqual(sum(n for _,n in counts),4)
            self.assertTrue(all(n<=2 for _,n in counts))
        finally:
            release.set()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode,0)

    def test_ai_budget_is_shared_and_retries_do_not_reset_it(self):
        settings=AISettings(enabled=True,api_key='synthetic-key')
        wb=load_workbook(BytesIO(book('vat_declaration',alias='未知数值')))
        for n in range(3):
            clone=wb.copy_worksheet(wb.active);clone.title='主表'+str(n)
        buffer=BytesIO();wb.save(buffer);wb.close()
        def answer(*_args,**_kwargs):
            time.sleep(.03)
            return json.dumps({'mappings':[{'source_column':2,'target_field':'declared_value','status':'mapped'}]})
        with patch.dict(os.environ,{'TAXPEARLS_AI_ENABLED':'1','TAXPEARLS_AI_API_KEY':'synthetic-key'}),patch('src.core_mapping.chat_content',side_effect=answer) as model:
            # Different labels/contexts force independent mapping calls within files.
            task=self.enqueue(ai=True,raw=buffer.getvalue())
            self.worker.run_once()
            with self.store.connect() as db:
                used=db.execute('SELECT ai_calls FROM material_jobs WHERE id=?',(task['jobs'][0]['id'],)).fetchone()[0]
            self.assertLessEqual(model.call_count,8)
            self.assertEqual(used,model.call_count)
            # Concurrent reservations themselves exercise the hard total even if
            # learned mappings reduce the number needed by this synthetic workbook.
            job=jobs.claim(self.store,jobs.QueueSettings.from_env())
            self.assertIsNone(job)
            completed = self.read(task)
            with self.store.connect() as db:
                db.execute("UPDATE material_jobs SET state='failed',failure_code='synthetic_retry' WHERE id=?",(task['jobs'][0]['id'],))
            response = self.client.post('/api/enterprise/input-tasks/'+task['id']+'/jobs/'+task['jobs'][0]['id']+'/retry',json={'expected_revision':completed['revision']})
            self.assertEqual(response.status_code,202,response.text)
            job=jobs.claim(self.store,jobs.QueueSettings.from_env())
            self.assertEqual(job['budget_id'],task['jobs'][0]['id'])
            with ThreadPoolExecutor(8) as executor:
                reserved=list(executor.map(lambda _:core_jobs.reserve_call(self.store,job),range(16)))
            self.assertEqual(sum(reserved),8-used)
            self.assertFalse(core_jobs.reserve_call(self.store,job))
            self.assertFalse(settings.problem())
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        db.close()

    def test_local_http_mapping_and_ocr_overlap_with_shared_slots(self):
        lock=threading.Lock()
        active=peak=calls=0
        answer={}
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(provider):
                nonlocal active,peak,calls
                provider.rfile.read(int(provider.headers['Content-Length']))
                with lock:
                    active+=1;peak=max(peak,active);calls+=1
                time.sleep(.25)  # Simulated provider; actual loopback HTTP transport.
                raw=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer)}}]}).encode()
                provider.send_response(200);provider.send_header('Content-Length',str(len(raw)));provider.end_headers()
                provider.wfile.write(raw)
                with lock:
                    active-=1
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        env={'TAXPEARLS_AI_ENABLED':'1','TAXPEARLS_AI_API_KEY':'synthetic-key',
             'TAXPEARLS_AI_BASE_URL':f'http://127.0.0.1:{server.server_port}','TAXPEARLS_AI_MODEL':'synthetic'}
        with patch.dict(os.environ,env):
            for scenario in ('mapping','ocr','duplicate_mapping'):
                ocr = scenario == 'ocr'
                with self.subTest(scenario=scenario):
                    active=peak=calls=0
                    answer=({'form_name':'增值税纳税申报表主表','form_type':'main','tax_no':fixtures.TAX,
                        'period_start':'2026-01-01','period_end':'2026-01-31','unit':'元','quote':'合成申报主表',
                        'rows':[{'item_label':'销售额','declared_value':'100','column_scope':'本期数','quote':'销售额 100'}]}
                        if ocr else {'mappings':[{'source_column':2,'target_field':'declared_value','status':'mapped'}]})
                    raw=(Path(__file__).parent/'fixtures/materials-scanned.pdf').read_bytes() if ocr else book('vat_declaration',alias='未知金额')
                    if scenario == 'mapping':
                        originals={}
                        for i in range(4):
                            workbook=load_workbook(BytesIO(raw));workbook.properties.title=f'synthetic-{i}'
                            out=BytesIO();workbook.save(out);workbook.close();originals[f'{i}.xlsx']=out.getvalue()
                        raw=originals
                    task=self.enqueue(names=tuple(f'{i}.pdf' if ocr else f'{i}.xlsx' for i in range(4)),ai=True,raw=raw,ocr=ocr)
                    self.worker.run_once()
                    result=self.read(task)
                    self.assertEqual(result['jobs'][0]['state'],'done',result['jobs'][0])
                    self.assertEqual(peak,1 if scenario=='duplicate_mapping' else 2)
                    self.assertEqual(calls,1 if scenario=='duplicate_mapping' else 4)
                    with self.store.connect() as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM material_ai_slots').fetchone()[0],0)
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM material_resource_slots').fetchone()[0],0)

    def test_configured_one_to_four_channels_and_single_file_uses_existing_worker(self):
        original = core_materials.parse
        for width in (1, 2, 3, 4):
            with self.subTest(width=width), patch.dict(os.environ, {
                'TAXPEARLS_MATERIAL_FILES_PER_TASK':str(width),
                'TAXPEARLS_MATERIAL_ORG_FILE_CONCURRENCY':'4',
                'TAXPEARLS_MATERIAL_USER_FILE_CONCURRENCY':'4',
            }):
                with self.store.connect() as db:
                    jobs.configure_resources(db)
                task = self.enqueue(names=tuple(f'channel-{i}.xlsx' for i in range(8)))
                lock, first_wave = threading.Lock(), threading.Barrier(width)
                active = peak = entered = 0

                def parse(*args, **kwargs):
                    nonlocal active, peak, entered
                    with lock:
                        active += 1;entered += 1;number = entered;peak = max(peak,active)
                    try:
                        if number <= width:
                            first_wave.wait(timeout=10)
                        return original(*args, **kwargs)
                    finally:
                        with lock:
                            active -= 1

                with patch.object(core_materials, 'parse', side_effect=parse):
                    self.worker.run_once()
                self.assertEqual(peak, width)
                self.assertEqual(self.read(task)['jobs'][0]['state'], 'done')
        task = self.enqueue(names=('single.xlsx',))
        with patch('webapp.core_material_jobs.ThreadPoolExecutor', side_effect=AssertionError('single file started a pool')):
            self.worker.run_once()
        self.assertEqual(self.read(task)['jobs'][0]['state'], 'done')

    def test_full_resource_wait_does_not_compete_for_sqlite_writer(self):
        tokens = [jobs.reserve_resource(self.store, self.admin, 'parse') for _ in range(2)]
        self.assertTrue(all(tokens))
        try:
            with ThreadPoolExecutor(1) as executor:
                with self.store.connect() as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    waiting = executor.submit(jobs.reserve_resource, self.store, self.admin, 'parse')
                    try:
                        self.assertIsNone(waiting.result(timeout=2))
                    finally:
                        writer.rollback()
        finally:
            for token in tokens:
                jobs.release_resource(self.store, token)
