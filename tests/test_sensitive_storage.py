"""F03: authenticated columns, raw-disk confidentiality and offline migration."""
import base64
from contextlib import closing
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import ops_db
from src import engine, loader
from webapp import sensitive_storage as fields
from webapp.storage import Store

ROOT = Path(__file__).resolve().parents[1]
SECRET = '110101199001017654-13800138765-6222021234567890123'


class SensitiveColumnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fields.db'
        self.key = base64.b64encode(os.urandom(32)).decode()
        self.env = patch.dict(os.environ, {'TAXPEARLS_FIELD_KEY': self.key,
                              'TAXPEARLS_BACKUP_KEY': base64.b64encode(os.urandom(32)).decode(),
                              'TAXPEARLS_MATERIAL_KEY': '', 'TAXPEARLS_AI_ENABLED': '0'})
        self.env.start(); self.addCleanup(self.env.stop)

    def populated(self):
        store = Store(self.path)
        user = store.create_user('fields-owner', 'Column-protection-2026!', '管理员', 'org_admin', 'a')
        dataset = deepcopy(loader.load(ROOT / 'samples/样例企业-审计材料.xlsx'))
        dataset.company.taxpayer_id = SECRET
        dataset.metrics['营业收入'].source = SECRET
        client = store.upsert_client(user, dataset.company.name, SECRET)
        findings = [engine.evaluate(engine.load_rules(ROOT / 'rules')[0], dataset)]
        store.save_audit('audit-one', user, client['id'], dataset, findings, {'hit': 1}, '2026-10-01')
        store.save_org_report(user, {'secret': SECRET, 'id': 'report-one', 'org_id': user['org_id'],
                              'created_at': '2026-10-01', 'rows': [], 'period': '2026'}, '<html>' + SECRET + '</html>')
        store.attach_org_report_pdf('report-one', user, b'%PDF-' + SECRET.encode())
        return store, user, client

    def legacy(self):
        store, user, client = self.populated()
        # Simulate a pre-F03 database, including old freed plaintext pages.
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            for table, (keys, columns) in fields.REGISTRY.items():
                for event in ('insert', 'update'):
                    db.execute('DROP TRIGGER tp_fields_' + table + '_' + event)
                for row in db.execute('SELECT * FROM ' + table).fetchall():
                    for column in columns:
                        if row[column] is not None:
                            plain = store._field_codec.open(row[column], table, column, [row[k] for k in keys])[0]
                            db.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE ' + ' AND '.join(k + '=?' for k in keys),
                                       [plain, *[row[k] for k in keys]])
            db.execute('DROP TABLE sensitive_storage_state')
            db.commit()
        return user, client

    def test_mandatory_keys_and_independence_fail_before_schema(self):
        for value in ('', 'bad!', base64.b64encode(b'short').decode()):
            with patch.dict(os.environ, TAXPEARLS_FIELD_KEY=value), self.assertRaises(fields.SensitiveStorageError):
                Store(self.path)
            self.assertFalse(self.path.exists())
        for name in ('TAXPEARLS_BACKUP_KEY', 'TAXPEARLS_MATERIAL_KEY'):
            with patch.dict(os.environ, {name: self.key}), self.assertRaises(fields.SensitiveStorageError):
                Store(self.path)

    def test_every_registered_context_roundtrip_type_nonce_and_replay(self):
        codec = fields.Codec(os.urandom(32))
        for table, (keys, columns) in fields.REGISTRY.items():
            identity = ['object-' + str(i) for i in range(len(keys))]
            for column in columns:
                for value in (SECRET, SECRET.encode()):
                    sealed = codec.seal(value, table, column, *identity)
                    self.assertNotEqual(sealed, codec.seal(value, table, column, *identity))
                    self.assertEqual(codec.open(sealed, table, column, identity)[0], value)
                    self.assertNotIn(SECRET.encode(), sealed.encode() if isinstance(sealed, str) else sealed)
                    with self.assertRaises(fields.SensitiveStorageError):
                        codec.open(sealed, table, column, ['other', *identity[1:]])
                    with self.assertRaises(fields.SensitiveStorageError):
                        fields.Codec(os.urandom(32)).open(sealed)
                    damaged = sealed[:-3] + ('AAA' if isinstance(sealed, str) else b'AAA')
                    with self.assertRaises(fields.SensitiveStorageError):
                        codec.open(damaged)

    def test_new_writes_not_in_database_wal_reports_or_log_pages(self):
        store, user, client = self.populated()
        with store.connect() as db:
            Store._log(db, user, 'test', 'audit', 'audit-one', SECRET)
            self.assertEqual(db.execute('SELECT detail,id FROM audit_log WHERE action=?', ('test',)).fetchone()[0], SECRET)
        self.assertEqual(store.get_client(client['id'])['taxpayer_id'], SECRET)
        self.assertEqual(store.get_audit('audit-one')['dataset'].metrics['营业收入'].source, SECRET)
        for file in Path(self.tmp.name).iterdir():
            self.assertNotIn(SECRET.encode(), file.read_bytes(), str(file))
        again = store.upsert_client(user, '改名', SECRET)
        self.assertEqual(again['id'], client['id'])
        self.assertEqual(store.search_audits(user, query='13800138765')['total'], 1)

    def test_plaintext_cross_record_and_bad_index_writes_rejected(self):
        store, _, client = self.populated()
        for column, value in (('findings_json', '[]'), ('dataset_json', '{}')):
            with self.assertRaises(sqlite3.OperationalError), store.connect() as db:
                db.execute('UPDATE audits SET ' + column + '=? WHERE id=?', (value, 'audit-one'))
        with self.assertRaises(sqlite3.OperationalError), store.connect() as db:
            value = store._field_codec.seal('{}', 'audits', 'dataset_json', 'another-object')
            db.execute("UPDATE audits SET dataset_json=? WHERE id='audit-one'", (value,))
        with self.assertRaises(sqlite3.IntegrityError), store.connect() as db:
            db.execute("UPDATE clients SET taxpayer_lookup='forged' WHERE id=?", (client['id'],))

    def test_raw_tampering_and_missing_identity_fail_closed(self):
        store, _, _ = self.populated()
        with self.assertRaises(fields.SensitiveStorageError), store.connect() as db:
            db.execute('SELECT dataset_json FROM audits').fetchone()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TRIGGER tp_fields_audits_update')
            db.execute("UPDATE audits SET dataset_json='plaintext' WHERE id='audit-one'")
            db.commit()
        with self.assertRaises(fields.SensitiveStorageError):
            store.get_audit('audit-one')
        Store(self.path)  # does not silently re-encrypt corrupted committed state
        with self.assertRaises(fields.SensitiveStorageError):
            Store(self.path).get_audit('audit-one')

    def test_wrong_key_startup_is_nonmutating_and_existing_store_key_pinned(self):
        store, _, client = self.populated()
        before = self.path.read_bytes()
        with patch.dict(os.environ, TAXPEARLS_FIELD_KEY=base64.b64encode(os.urandom(32)).decode()):
            with self.assertRaises(fields.SensitiveStorageError):
                Store(self.path)
            self.assertEqual(store.get_client(client['id'])['taxpayer_id'], SECRET)
        self.assertEqual(before, self.path.read_bytes())

    def test_offline_legacy_migration_compacts_and_preserves_business_bytes(self):
        user, client = self.legacy()
        self.assertIn(SECRET.encode(), self.path.read_bytes())
        result = ops_db.migrate_sensitive_fields(self.path, safety_retention_days=7)
        self.assertTrue(result['compacted']); self.assertTrue(result['safety_encrypted'])
        for file in Path(self.tmp.name).iterdir():
            self.assertNotIn(SECRET.encode(), file.read_bytes(), str(file))
        reopened = Store(self.path)
        self.assertEqual(reopened.get_client(client['id'])['taxpayer_id'], SECRET)
        report = reopened.list_org_reports(user)[0]
        self.assertEqual(reopened.get_org_report(report['id'], user)['html'], '<html>' + SECRET + '</html>')
        self.assertEqual(reopened.get_org_report(report['id'], user)['pdf_bytes'], b'%PDF-' + SECRET.encode())
        ops_db.migrate_sensitive_fields(self.path, safety_retention_days=7)
        self.assertEqual(Store(self.path).get_audit('audit-one')['dataset'].company.taxpayer_id, SECRET)

    def test_offline_failure_keeps_source_and_creates_no_plaintext_temp(self):
        self.legacy()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch.object(fields.Codec, 'seal', side_effect=fields.SensitiveStorageError('simulated')):
            with self.assertRaises(fields.SensitiveStorageError):
                ops_db.migrate_sensitive_fields(self.path, safety_retention_days=7)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ['fields.db'])

    def test_encrypted_backup_restore_retains_reports_and_requires_field_key(self):
        store, user, client = self.populated()
        backup = Path(self.tmp.name) / 'copy.tpbackup'
        ops_db.create_encrypted_backup(self.path, backup, retention_days=7)
        restored = Path(self.tmp.name) / 'restore.db'
        ops_db.restore_encrypted_backup(backup, restored, safety_retention_days=7)
        self.assertEqual(Store(restored).get_client(client['id'])['taxpayer_id'], SECRET)
        with patch.dict(os.environ, TAXPEARLS_FIELD_KEY=''), self.assertRaises(fields.SensitiveStorageError):
            Store(restored)
        self.assertEqual(Store(restored).list_org_reports(user), store.list_org_reports(user))

    def test_restore_wrong_field_key_fails_before_target_backup_or_replace(self):
        self.populated()
        backup = Path(self.tmp.name) / 'copy.tpbackup'
        ops_db.create_encrypted_backup(self.path, backup, retention_days=7)
        before = self.path.read_bytes()
        with patch.dict(os.environ, TAXPEARLS_FIELD_KEY=base64.b64encode(os.urandom(32)).decode()):
            with self.assertRaises(fields.SensitiveStorageError):
                ops_db.restore_encrypted_backup(backup, self.path, safety_retention_days=7)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(list(Path(self.tmp.name).glob('*.pre-restore-*')))

    def test_cli_offline_migration_from_other_cwd_and_no_secret_output(self):
        self.legacy()
        command = [sys.executable, '-B', '-X', 'utf8', str(ROOT / 'scripts/ops_db.py'), 'migrate-fields',
                   '--database', str(self.path), '--safety-retention-days', '7']
        refused = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', cwd=self.tmp.name)
        self.assertNotEqual(refused.returncode, 0)
        completed = subprocess.run([*command, '--yes'], capture_output=True, text=True, encoding='utf-8', cwd=self.tmp.name)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(self.key, completed.stdout + completed.stderr)
        self.assertNotIn(SECRET, completed.stdout + completed.stderr)
        self.assertEqual(Store(self.path).get_audit('audit-one')['dataset'].company.taxpayer_id, SECRET)

    def test_legacy_encrypted_restore_publishes_only_compacted_ciphertext(self):
        self.legacy()
        backup = Path(self.tmp.name) / 'legacy.tpbackup'
        ops_db.create_encrypted_backup(self.path, backup, retention_days=7)
        restored = Path(self.tmp.name) / 'restored.db'
        ops_db.restore_encrypted_backup(backup, restored, safety_retention_days=7)
        self.assertNotIn(SECRET.encode(), restored.read_bytes())
        self.assertEqual(Store(restored).get_audit('audit-one')['dataset'].company.taxpayer_id, SECRET)
