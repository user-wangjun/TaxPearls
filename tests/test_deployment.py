"""F05 deployment bindings, production prerequisites and recovery boundaries."""
import base64
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from scripts import check_deployment, ops_db
from webapp import deployment
from webapp.sensitive_storage import SensitiveStorageError
from webapp.storage import Store


class DeploymentBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'environment.db'
        self.env = patch.dict(os.environ, {
            'TAXPEARLS_ENVIRONMENT': 'local', 'TAXPEARLS_INSTANCE_ID': 'local',
            'TAXPEARLS_COOKIE_SECURE': '1', 'TAXPEARLS_PUBLIC_BASE_URL': 'https://prod.example.test',
            'TAXPEARLS_TRUSTED_PROXY_IPS': '127.0.0.1',
            **{name: base64.b64encode(os.urandom(32)).decode() for name in
               ('TAXPEARLS_FIELD_KEY', 'TAXPEARLS_MATERIAL_KEY', 'TAXPEARLS_BACKUP_KEY')}})
        self.env.start(); self.addCleanup(self.env.stop)

    def mode(self, mode, instance='installation-one'):
        return patch.dict(os.environ, TAXPEARLS_ENVIRONMENT=mode, TAXPEARLS_INSTANCE_ID=instance)

    def populated(self):
        store = Store(self.path)
        store.create_user('deployment-user', 'Deployment-binding-2026!', '仿真用户', 'teacher', 'school')
        return store

    def test_explicit_instance_and_production_prerequisites_fail_before_creation(self):
        for values in ({'TAXPEARLS_ENVIRONMENT': 'unknown'}, {'TAXPEARLS_INSTANCE_ID': ''},
                       {'TAXPEARLS_INSTANCE_ID': '../production'}):
            with patch.dict(os.environ, values), self.assertRaises(SensitiveStorageError):
                Store(self.path)
            self.assertFalse(self.path.exists())
        with self.mode('production'):
            for values in ({'TAXPEARLS_COOKIE_SECURE': '0'}, {'TAXPEARLS_PUBLIC_BASE_URL': ''},
                           {'TAXPEARLS_PUBLIC_BASE_URL': 'http://prod.example.test'},
                           {'TAXPEARLS_PUBLIC_BASE_URL': 'https://user:password@prod.example.test'},
                           {'TAXPEARLS_PUBLIC_BASE_URL': 'https://prod.example.test/a'},
                           {'TAXPEARLS_TRUSTED_PROXY_IPS': '*'}, {'TAXPEARLS_MATERIAL_KEY': ''},
                           {'TAXPEARLS_BACKUP_KEY': os.environ['TAXPEARLS_FIELD_KEY']}):
                with patch.dict(os.environ, values), self.assertRaises(SensitiveStorageError):
                    Store(self.path)
                self.assertFalse(self.path.exists())

    def test_new_training_and_production_have_distinct_identity(self):
        for mode in ('training', 'production'):
            with self.mode(mode, 'instance-' + mode):
                store = Store(Path(self.tmp.name) / (mode + '.db'))
                with store.connect() as db:
                    row = db.execute('SELECT * FROM deployment_identity').fetchone()
                    self.assertEqual((row['mode'], row['instance']), (mode, 'instance-' + mode))
                self.assertEqual(store.deployment, deployment.configured())

    def test_wrong_environment_and_instance_refuse_even_with_same_field_key(self):
        with self.mode('training', 'training-one'):
            self.populated()
        before = self.path.read_bytes()
        for mode, instance in (('production', 'training-one'), ('training', 'training-two'), ('local', 'local')):
            with self.mode(mode, instance), self.assertRaises(SensitiveStorageError):
                Store(self.path)
            self.assertEqual(before, self.path.read_bytes())

    def test_existing_local_store_keeps_pinned_context_after_environment_change(self):
        store = self.populated()
        with self.mode('training'):
            self.assertEqual(store.list_users()[0]['username'], 'deployment-user')
            with self.assertRaises(SensitiveStorageError):
                Store(self.path)

    def test_old_business_database_requires_explicit_offline_binding(self):
        self.populated()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE deployment_identity'); db.commit()
        before = self.path.read_bytes()
        with self.mode('production'):
            with self.assertRaises(SensitiveStorageError):
                Store(self.path)
            self.assertEqual(before, self.path.read_bytes())
            bound = ops_db.migrate_sensitive_fields(self.path, safety_retention_days=7, bind_environment=True)
            self.assertTrue(bound['environment_bound']); self.assertTrue(bound['safety_encrypted'])
            self.assertEqual(Store(self.path).list_users()[0]['username'], 'deployment-user')

    def test_explicit_binding_cannot_relabel_another_nonlocal_installation(self):
        with self.mode('production', 'production-one'):
            self.populated()
        before = self.path.read_bytes()
        with self.mode('training', 'training-one'), self.assertRaises(SensitiveStorageError):
            ops_db.migrate_sensitive_fields(self.path, safety_retention_days=7, bind_environment=True)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(list(Path(self.tmp.name).glob('*.pre-fields-*')))

    def test_tampered_or_removed_runtime_identity_fails_closed(self):
        store = self.populated()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE deployment_identity SET mode='training'"); db.commit()
        for read in (store.list_users, lambda: Store(self.path)):
            with self.assertRaises(SensitiveStorageError): read()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE deployment_identity'); db.commit()
        with self.assertRaises(SensitiveStorageError): store.list_users()

    def test_cross_environment_backup_rejected_before_target_mutation(self):
        with self.mode('training', 'training-one'):
            self.populated()
            backup = Path(self.tmp.name) / 'training.tpbackup'
            ops_db.create_encrypted_backup(self.path, backup, retention_days=7)
        target = Path(self.tmp.name) / 'prod.db'
        with self.mode('production', 'production-one'):
            Store(target)
            before = target.read_bytes()
            with self.assertRaises(SensitiveStorageError):
                ops_db.restore_encrypted_backup(backup, target, safety_retention_days=7)
            self.assertEqual(before, target.read_bytes())
        self.assertFalse(list(Path(self.tmp.name).glob('*.pre-restore-*')))

    def test_valid_source_cannot_overwrite_other_installations_target(self):
        with self.mode('training', 'training-one'):
            self.populated()
            backup = Path(self.tmp.name) / 'training.tpbackup'
            ops_db.create_encrypted_backup(self.path, backup, retention_days=7)
        target = Path(self.tmp.name) / 'other.db'
        with self.mode('production', 'production-one'): Store(target)
        before = target.read_bytes()
        with self.mode('training', 'training-one'), self.assertRaises(SensitiveStorageError):
            ops_db.restore_encrypted_backup(backup, target, safety_retention_days=7)
        self.assertEqual(before, target.read_bytes())

    def test_checkpoint_preserves_committed_crash_wal_and_allows_restore_preflight(self):
        self.populated()
        crashed = subprocess.run([sys.executable, '-c',
            "import sqlite3,os,sys; d=sqlite3.connect(sys.argv[1]); d.execute('PRAGMA journal_mode=WAL'); "
            "d.execute('CREATE TABLE checkpoint_fixture(value TEXT)'); "
            "d.execute(\"INSERT INTO checkpoint_fixture VALUES ('committed-wal')\"); d.commit(); os._exit(0)",
            str(self.path)], check=True)
        self.assertEqual(crashed.returncode, 0)
        self.assertTrue(Path(str(self.path) + '-wal').exists())
        result = ops_db.checkpoint_database(self.path)
        self.assertTrue(result['checkpointed']); self.assertFalse(result['application_stopped_verified'])
        ops_db._closed_database(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT value FROM checkpoint_fixture').fetchone()[0], 'committed-wal')
        self.assertEqual(Store(self.path).list_users()[0]['username'], 'deployment-user')

    def test_checkpoint_refuses_busy_writer_and_wrong_installation(self):
        self.populated()
        with closing(sqlite3.connect(self.path)) as busy:
            busy.execute('BEGIN IMMEDIATE')
            busy.execute("UPDATE users SET display_name='uncommitted'")
            with self.assertRaises(ops_db.BackupError): ops_db.checkpoint_database(self.path)
            busy.rollback()
        with self.mode('training'), self.assertRaises(SensitiveStorageError):
            ops_db.checkpoint_database(self.path)
        self.assertEqual(Store(self.path).list_users()[0]['display_name'], '仿真用户')

    def test_checkpoint_cli_requires_explicit_shutdown_confirmation(self):
        self.populated()
        before = self.path.read_bytes()
        refused = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(ops_db.ROOT / 'scripts/ops_db.py'), 'checkpoint',
                                  '--database', str(self.path)], capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(before, self.path.read_bytes())


class DeploymentQueueContractTests(unittest.TestCase):
    """Exercise the checker's HTTP flow, including refusal before confirmation."""

    def setUp(self):
        self.states = ['queued', 'running', 'done']
        self.revision = 2
        self.confirmable = True
        self.omit_job = False
        self.upload_status = 202
        self.status_delay = 0
        self.confirmations = []
        self.reads = []
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # The timeout test deliberately closes its socket.

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path == '/api/enterprise/materials':
                    self.reply(case.upload_status, {'id': 'batch', 'revision': 1,
                        'jobs': [{'id': 'target', 'batch_id': 'batch', 'state': 'queued'}]})
                elif self.path == '/api/enterprise/materials/batch/confirm':
                    body = json.loads(raw)
                    case.confirmations.append(body)
                    self.reply(200 if body['expected_revision'] == 2 else 409, {'audit_id': 'audit'})
                else:
                    self.reply(404, {})

            def do_GET(self):
                case.reads.append(self.path)
                if self.path.endswith('/status'):
                    time.sleep(case.status_delay)
                    state = case.states[0]
                    if len(case.states) > 1:
                        case.states.pop(0)
                    jobs = [{'id': 'unrelated', 'batch_id': 'batch', 'state': 'done', 'result_revision': 99}]
                    if not case.omit_job:
                        jobs.append({'id': 'target', 'batch_id': 'batch', 'state': state, 'result_revision': 2})
                    self.reply(200, {'id': 'batch', 'jobs': jobs})
                else:
                    self.reply(200, {'id': 'batch', 'revision': case.revision, 'analysis_revision': 2,
                                     'analysis': {'can_confirm': case.confirmable}})

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()

        def stop():
            server.shutdown(); thread.join(); server.server_close()
        self.addCleanup(stop)
        self.ctx = {'url': 'http://127.0.0.1:' + str(server.server_port), 'token': 'synthetic-test-token'}

    def run_upload(self, **kwargs):
        return check_deployment.upload_and_confirm(self.ctx, b'synthetic multipart',
                                                   'multipart/form-data; boundary=synthetic', poll=.001, **kwargs)

    def test_waits_for_own_job_and_confirms_latest_saved_analysis(self):
        batch, result = self.run_upload()
        self.assertEqual(result['audit_id'], 'audit')
        self.assertEqual(batch['revision'], 2)
        self.assertEqual(self.confirmations, [{'expected_revision': 2}])
        self.assertEqual(self.reads, ['/api/enterprise/materials/batch/status'] * 3
                         + ['/api/enterprise/materials/batch'])

    def test_failed_superseded_missing_or_unknown_job_never_confirms(self):
        for state in ('failed', 'superseded', 'unexpected'):
            with self.subTest(state=state), self.assertRaisesRegex(RuntimeError, 'without a usable analysis'):
                self.states = [state]
                self.run_upload()
        self.omit_job = True
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            self.run_upload()
        self.assertEqual(self.confirmations, [])

    def test_timeout_bounds_queue_wait_and_never_confirms(self):
        self.states = ['running']
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            self.run_upload(timeout=.05)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.confirmations, [])

    def test_slow_status_request_is_bounded_by_remaining_deadline(self):
        self.status_delay = .2
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'request failed or timed out'):
            self.run_upload(timeout=.05)
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(self.confirmations, [])

    def test_done_with_stale_or_blocked_analysis_never_confirms(self):
        self.states = ['done']
        self.revision = 3
        with self.assertRaisesRegex(RuntimeError, 'revision'):
            self.run_upload()
        self.revision = 2
        self.confirmable = False
        with self.assertRaisesRegex(RuntimeError, 'not confirmable'):
            self.run_upload()
        self.assertEqual(self.confirmations, [])

    def test_upload_must_use_queue_contract(self):
        self.upload_status = 200
        with self.assertRaisesRegex(RuntimeError, 'expected 202, got 200'):
            self.run_upload()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.confirmations, [])


class DeploymentQueueRuntimeTests(unittest.TestCase):
    def test_real_service_queue_confirmation_and_original_with_production_config(self):
        from io import BytesIO
        import uuid
        from tests.test_materials import accounts

        with TemporaryDirectory(prefix='taxpearls-deployment-queue-') as directory:
            values = {'TAXPEARLS_ENVIRONMENT': 'production', 'TAXPEARLS_INSTANCE_ID': 'checker-runtime-test',
                      'TAXPEARLS_DB': str(Path(directory) / 'runtime.db'),
                      'TAXPEARLS_COOKIE_SECURE': '1', 'TAXPEARLS_PUBLIC_BASE_URL': 'https://synthetic.example.test',
                      'TAXPEARLS_TRUSTED_PROXY_IPS': '127.0.0.1', 'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '1',
                      'TAXPEARLS_MATERIAL_WORKERS': '4', 'TAXPEARLS_AI_ENABLED': '0',
                      'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0', 'TAXPEARLS_AI_API_KEY': '',
                      'TAXPEARLS_AI_BACKUP_API_KEYS': '', 'TAXPEARLS_RESEND_API_KEY': '',
                      **{name: base64.b64encode(os.urandom(32)).decode() for name in
                         ('TAXPEARLS_FIELD_KEY', 'TAXPEARLS_MATERIAL_KEY', 'TAXPEARLS_BACKUP_KEY')}}
            with patch.dict(os.environ, values):
                store = Store()
                store.create_user('checker-owner', 'Synthetic-checker-2026!', '仿真管理员', 'org_admin', 'synthetic')
                token = store.authenticate('checker-owner', 'Synthetic-checker-2026!')[1]
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            ctx = {'url': 'http://127.0.0.1:' + str(port), 'token': token}
            stop_file = Path(directory) / 'stop'
            server_code = """import pathlib, sys, threading, time, uvicorn
server = uvicorn.Server(uvicorn.Config('webapp.app:app', host='127.0.0.1', port=int(sys.argv[1]), log_level='warning'))
def stop():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not pathlib.Path(sys.argv[2]).exists():
        time.sleep(.05)
    server.should_exit = True
threading.Thread(target=stop, daemon=True).start()
server.run()
"""
            # Logs stay in the temporary test directory; no credentials are printed.
            with open(Path(directory) / 'server.log', 'wb') as log:
                process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-c', server_code,
                                            str(port), str(stop_file)],
                                           cwd=check_deployment.ROOT, env={**os.environ, **values}, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        self.assertIsNone(process.poll(), 'Isolated service exited before readiness')
                        try:
                            check_deployment.request(ctx, '/healthz', timeout=1)
                            break
                        except (OSError, RuntimeError):
                            if time.monotonic() >= deadline:
                                self.fail('Isolated service startup timed out')
                            time.sleep(.1)
                    raw = accounts()
                    boundary = uuid.uuid4().hex
                    stream = BytesIO()
                    stream.write((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                                  'filename="synthetic.xlsx"\r\nContent-Type: application/vnd.openxmlformats-officedocument.'
                                  'spreadsheetml.sheet\r\n\r\n').encode())
                    stream.write(raw); stream.write(f'\r\n--{boundary}--\r\n'.encode())
                    batch, result = check_deployment.upload_and_confirm(
                        ctx, stream.getvalue(), 'multipart/form-data; boundary=' + boundary, timeout=20, poll=.01)
                    self.assertEqual(batch['jobs'][0]['state'], 'done')
                    self.assertGreater(batch['revision'], batch['jobs'][0]['input_revision'])
                    self.assertTrue(batch['analysis']['can_confirm'])
                    self.assertTrue(result['audit_id'])
                    path = '/api/enterprise/materials/' + batch['id'] + '/originals/' + batch['files'][0]['id']
                    self.assertEqual(check_deployment.request(ctx, path), raw)
                    check_deployment.request(ctx, '/api/me', token='foreign-synthetic-token', expected=401)
                finally:
                    stop_file.touch()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.terminate(); process.wait(timeout=5)
                        self.fail('Isolated service did not stop gracefully')
