"""Public snapshot delivery preserves local school data and explicit source reviews."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from src.public_policy_package import load_package, import_package
from webapp import policy_graph as pg

ROOT = Path(__file__).resolve().parents[1]


class PublicPolicyPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load_package(ROOT/'data/public-policy/snapshot.json.gz', ROOT)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.baseline = Path(cls.tmp.name)/'baseline'
        cls.baseline.mkdir()
        db = sqlite3.connect(cls.baseline/'school.sqlite')
        db.row_factory = sqlite3.Row
        try:
            pg.migrate(db)
            db.execute('CREATE TABLE local_accounts(id TEXT PRIMARY KEY,secret TEXT)')
            db.execute("INSERT INTO local_accounts VALUES ('teacher','private-sentinel')")
            db.execute('CREATE TABLE knowledge_point(id INTEGER PRIMARY KEY,description TEXT)')
            db.executemany('INSERT INTO knowledge_point VALUES (?,?)', [(i, 'local teaching content') for i in range(113)])
            db.commit()
            with db:
                cls.first = import_package(db, cls.payload, ROOT)
        finally:
            db.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/'relocated-school'
        shutil.copytree(self.baseline, self.home)
        self.db = sqlite3.connect(self.home/'school.sqlite')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)

    def test_snapshot_is_complete_and_contains_only_public_graph(self):
        self.assertEqual(set(self.payload['tables']), set(pg.IDS))
        self.assertEqual(len(self.payload['originals']), 75)
        self.assertEqual(len(self.payload['tables']['Policy']), 37)
        self.assertEqual(len(self.payload['tables']['Clause']), 1333)
        self.assertEqual(self.db.execute('SELECT count(*) FROM knowledge_point').fetchone()[0], 113)
        self.assertEqual(self.db.execute('SELECT secret FROM local_accounts').fetchone()[0], 'private-sentinel')
        self.assertFalse(self.first['business_tables_changed'])

    def test_repeat_after_relocation_preserves_reviews_and_local_school_data(self):
        key = self.payload['tables']['Policy'][0]['policy_id']
        with self.db:
            self.db.execute("UPDATE Policy SET validity_status='in_force',issuer='人工复核机关' WHERE policy_id=?", (key,))
        before = [tuple(r) for r in self.db.execute('SELECT * FROM knowledge_point')]
        with self.db:
            result = import_package(self.db, self.payload, ROOT)
        self.assertEqual(sum(result['inserted'].values()), 0)
        self.assertEqual(self.db.execute('SELECT validity_status FROM Policy WHERE policy_id=?', (key,)).fetchone()[0], 'in_force')
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM knowledge_point')], before)
        self.assertEqual(self.db.execute('SELECT secret FROM local_accounts').fetchone()[0], 'private-sentinel')

    def test_content_conflict_stops_without_inserting_other_nodes(self):
        modified = deepcopy(self.payload)
        modified['tables']['Clause'][0]['content'] += ' conflicting edit'
        before = self.db.total_changes
        with self.assertRaises(ValueError), self.db:
            import_package(self.db, modified, ROOT)
        self.assertEqual(self.db.total_changes, before)

    def test_retained_file_tamper_stops_without_database_changes(self):
        file = self.home/self.payload['originals'][0]['path']
        file.write_bytes(b'tampered-engineering-fixture')
        before = self.db.total_changes
        with self.assertRaisesRegex(ValueError, '原文内容冲突'), self.db:
            import_package(self.db, self.payload, ROOT)
        self.assertEqual(self.db.total_changes, before)

    def test_late_relationship_failure_rolls_back_rows_and_new_files(self):
        modified = deepcopy(self.payload)
        edge = modified['tables']['Relationship'][0]
        edge['relationship_id'] = 'synthetic-invalid-edge'
        edge['to_id'] = 'synthetic-missing-endpoint'
        fresh = self.home/'empty'
        fresh.mkdir()
        db = sqlite3.connect(fresh/'empty.sqlite')
        db.row_factory = sqlite3.Row
        try:
            pg.migrate(db)
            with self.assertRaises(ValueError), db:
                import_package(db, modified, ROOT)
            self.assertEqual(db.execute('SELECT count(*) FROM Policy').fetchone()[0], 0)
            self.assertEqual(list((fresh/'policy-originals').iterdir()), [])
        finally:
            db.close()

    def test_checked_in_package_hash_and_relative_paths_are_stable(self):
        manifest = json.loads((ROOT/'data/public-policy/manifest.json').read_text(encoding='utf-8'))
        raw = (ROOT/'data/public-policy/snapshot.json.gz').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['compressed_sha256'])
        self.assertTrue(all(not Path(r['raw_file_path']).is_absolute() for r in self.payload['tables']['Policy']))
