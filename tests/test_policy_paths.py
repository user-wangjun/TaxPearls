"""Retained policy paths survive relocation without changing source/review identities."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from scripts.relativize_policy_paths import migrate
from src import policy_paths as paths, policy_corpus as corpus
from src.kg_retrieval import applicability
from webapp import policy_graph as pg


class PolicyPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'original'
        self.home.mkdir()
        self.path = self.home / 'public.sqlite'
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        pg.migrate(self.db)
        self.raw = self.home / 'policy-originals' / 'source.html'
        self.raw.parent.mkdir()
        self.raw.write_bytes(b'<p>fictional engineering source</p>')
        self.text = self.raw.with_suffix('.txt')
        self.content = '第一条 纯合成工程测试，不是税法。\n'
        self.text.write_bytes(self.content.encode('utf-8'))
        self.location = {'raw_path': 'collection/source.html', 'text_path': 'collection/source.txt',
                         'raw_sha256': self.sha(self.raw), 'text_sha256': self.sha(self.text),
                         'retained_raw_path': str(self.raw), 'retained_text_path': str(self.text),
                         'start_char': 0, 'end_char_exclusive': len(self.content)}
        pg.import_bundle(self.db, {'Policy': [{'policy_id': 'P', 'title': '纯合成测试', 'issuer': '测试机关',
                          'source_url': 'https://example.test/source', 'raw_file_path': str(self.raw),
                          'file_sha256': self.sha(self.raw)}],
                          'Clause': [{'clause_id': 'C', 'policy_id': 'P', 'clause_label': '第一条',
                          'sort_index': 0, 'content': self.content, 'source_location': json.dumps(self.location),
                          'extraction_status': 'needs_review'}]})
        self.db.commit()

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_dry_run_changes_nothing_and_apply_makes_readable_backup(self):
        before = dict(pg.get(self.db, 'Policy', 'P'))
        self.assertFalse(migrate(self.path)['applied'])
        self.assertEqual(pg.get(self.db, 'Policy', 'P'), before)
        result = migrate(self.path, apply=True)
        self.assertTrue(result['applied'])
        with closing(sqlite3.connect(result['backup'])) as backup:
            self.assertEqual(backup.execute("SELECT raw_file_path FROM Policy WHERE policy_id='P'").fetchone()[0],
                             before['raw_file_path'])
        after = pg.get(self.db, 'Policy', 'P')
        self.assertEqual(after['raw_file_path'], 'policy-originals/source.html')
        self.assertEqual({k: v for k, v in after.items() if k != 'raw_file_path'},
                         {k: v for k, v in before.items() if k != 'raw_file_path'})
        self.assertEqual(migrate(self.path, apply=True)['policies_changed'], 0)

    def test_relocated_database_reimport_review_and_source_check_use_database_root(self):
        migrate(self.path, apply=True)
        moved = self.root / 'relocated'
        shutil.copytree(self.home, moved)
        self.db.close()
        shutil.rmtree(self.home)  # This test owns its synthetic temporary directory.
        db = sqlite3.connect(moved / self.path.name)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        pg.verify_source(db, 'P', issuer='测试机关', validity_status='in_force', effective_from='2026-01-01',
                         effective_to=None, clause_ids=['C'])
        reasons = applicability.source_check(pg.get(db, 'Policy', 'P'), pg.get(db, 'Clause', 'C'),
                                             {'data_scope': 'synthetic'}, {}, source_root=paths.database_directory(db))
        self.assertEqual(reasons, [])
        pg._verified_source(db, 'C')
        location = json.loads(pg.get(db, 'Clause', 'C')['source_location'])
        self.assertEqual(paths.resolve_path(location['retained_text_path'], moved).read_text(encoding='utf-8'),
                         self.content)

    def test_corrupt_text_or_char_coordinates_stop_all_updates(self):
        before = pg.get(self.db, 'Policy', 'P')['raw_file_path']
        self.text.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, '哈希'):
            migrate(self.path, apply=True)
        self.assertEqual(pg.get(self.db, 'Policy', 'P')['raw_file_path'], before)
        self.text.write_bytes(self.content.encode('utf-8'))
        with self.db:
            self.db.execute("UPDATE Clause SET content='wrong coordinates' WHERE clause_id='C'")
        with self.assertRaisesRegex(ValueError, '字符定位'):
            migrate(self.path, apply=True)
        self.assertEqual(pg.get(self.db, 'Policy', 'P')['raw_file_path'], before)

    def test_relative_path_cannot_depend_on_cwd_or_escape_database_root(self):
        for value in ['../outside.txt', 'nested/../../outside.txt', r'..\outside.txt']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                paths.resolve_path(value, self.home)
        with self.assertRaises(ValueError):
            paths.resolve_path('policy-originals/source.html')
        with self.assertRaises(ValueError):
            paths.stored_path(self.db, self.root / 'outside.txt')
        self.assertEqual(paths.resolve_path(str(self.raw)), self.raw)

    def test_source_identity_ignores_only_physical_locations(self):
        relative = {**self.location, 'retained_raw_path': 'policy-originals/source.html',
                    'retained_text_path': 'policy-originals/source.txt'}
        self.assertEqual(paths.source_identity(json.dumps(self.location)), paths.source_identity(json.dumps(relative)))
        relative['text_sha256'] = '0' * 64
        self.assertNotEqual(paths.source_identity(json.dumps(self.location)), paths.source_identity(json.dumps(relative)))
        self.assertEqual(paths.source_identity('原文第1行'), '原文第1行')

    def test_new_import_uses_relative_paths_and_reimport_after_move_is_idempotent(self):
        source = self.root / 'collection'
        source.mkdir()
        raw, text = source / 'one.html', source / 'one.txt'
        raw.write_bytes(self.raw.read_bytes())
        text.write_bytes(self.text.read_bytes())
        manifest = source / 'manifest.json'
        manifest.write_text(json.dumps({'documents': [{'id': 'ONE', 'title': '合成测试',
            'classification': '增值税', 'raw_path': raw.name, 'text_path': text.name,
            'raw_sha256': self.sha(raw), 'text_sha256': self.sha(text), 'source_url': 'https://example.test/one',
            'fetched_at': '2026-01-01T00:00:00Z'}],
            'graph_preparation': {'format': corpus.FORMAT, 'documents': {'ONE': {'issuer': '测试机关'}}}}), encoding='utf-8')
        plan = corpus.prepare(manifest)
        with self.db:
            corpus.import_plan(self.db, plan, self.home / 'retained')
        policy = self.db.execute("SELECT * FROM Policy WHERE policy_id!='P'").fetchone()
        self.assertFalse(Path(policy['raw_file_path']).is_absolute())
        self.db.close()
        moved = self.root / 'moved'
        shutil.copytree(self.home, moved)
        shutil.rmtree(self.home)
        with closing(sqlite3.connect(moved / self.path.name)) as db:
            db.row_factory = sqlite3.Row
            before = [tuple(r) for r in db.execute('SELECT * FROM Clause')]
            corpus.import_plan(db, plan, moved / 'another-retention-dir')
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM Clause')])

    def test_catalog_locations_verify_fields_instead_of_raw_file_offsets(self):
        topic = {'id': 'T', 'description': '合成目录解释'}
        text = json.dumps(topic, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        location = {'provenance_kind': 'catalog_annotation', 'catalog_field': 'policies[id=T]',
                    'annotation_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'start_char': 0, 'end_char_exclusive': len(text)}
        data = json.dumps({'policies': [topic]}, ensure_ascii=False).encode()
        paths.check_text_location(location, data, text)
        with self.assertRaisesRegex(ValueError, '字段'):
            paths.check_text_location({**location, 'catalog_field': 'policies[id=other]'}, data, text)
        with self.assertRaisesRegex(ValueError, '哈希'):
            paths.check_text_location(location, data, text + 'tamper')
        source = '纯合成原文快照\n'
        snapshot = {'provenance_kind': 'retained_catalog_snapshot', 'snapshot_field': 'sources[id=S].snapshot.text',
                    'snapshot_sha256': hashlib.sha256(''.join(source.split()).encode()).hexdigest()}
        data = json.dumps({'sources': [{'id': 'S', 'snapshot': {'text': source}}]}).encode()
        paths.check_text_location(snapshot, data, source)
        with self.assertRaisesRegex(ValueError, '快照'):
            paths.check_text_location(snapshot, data, source + 'tamper')

    def test_manifest_annotations_keep_document_identity_and_content_hash(self):
        text = json.dumps({'document_id': 'DOC', 'note': '明确的整理说明'}, ensure_ascii=False)
        location = {'provenance_kind': 'manifest_annotation', 'manifest_document_id': 'DOC',
                    'annotation_sha256': hashlib.sha256(text.encode()).hexdigest()}
        data = json.dumps({'documents': [{'id': 'DOC'}]}).encode()
        paths.check_text_location(location, data, text)
        with self.assertRaisesRegex(ValueError, '整理记录'):
            paths.check_text_location({**location, 'manifest_document_id': 'OTHER'}, data, text)

    def test_legacy_manifest_import_uses_relative_path(self):
        source = self.home / 'legacy.json'
        source.write_text(json.dumps({'documents': [{'id': 'LEGACY', 'title': '合成旧清单',
            'raw_path': 'policy-originals/source.html', 'text_path': 'policy-originals/source.txt',
            'raw_sha256': self.sha(self.raw), 'text_sha256': self.sha(self.text),
            'source_url': 'https://example.test/legacy'}]}), encoding='utf-8')
        pg.import_corpus(self.db, source, ['LEGACY'], self.home / 'legacy-retained')
        row = self.db.execute("SELECT raw_file_path FROM Policy WHERE policy_id!='P'").fetchone()
        self.assertTrue(row[0].startswith('legacy-retained/'))
