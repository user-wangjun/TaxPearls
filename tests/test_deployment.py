"""F05 deployment bindings, production prerequisites and recovery boundaries."""
import base64
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import ops_db
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
