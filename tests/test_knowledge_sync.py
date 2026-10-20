"""Real retained sources + maintained rules, isolated from the business DB."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from argon2 import PasswordHasher
from src import engine
from src.rule_conversion import compile_rule, effective_rules
from src import policy_corpus
from webapp import knowledge_sync as sync, training_portal as portal, training_content as content
from webapp.storage import Store


class KnowledgeSynchronizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = engine.load_rules(sync.ROOT / 'rules')
        cls.plan = policy_corpus.prepare(sync.MANIFEST)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'business.db')
        self.originals = Path(self.temp.name) / 'originals'

    def update(self, rules=None, plan=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return sync.synchronize(db, rules or self.rules, plan or self.plan, self.originals)

    def counts(self):
        with self.store.connect() as db:
            return {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                    for table in ['Policy', 'Clause', 'TaxRule', 'Condition', 'Metric', 'Relationship',
                                  'knowledge_point', 'knowledge_point_relation', 'training_content_version']}

    def publish(self, cert_id, rules):
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO college_user VALUES ('staff','teacher','unused','测试教师','测试院校',NULL,NULL,NULL,'teacher',1,'now')")
            label = '教学发布 ' + str(db.execute('SELECT count(*) FROM training_content_version').fetchone()[0])
        with patch('webapp.training_content.load_rules', return_value=rules):
            return content.create_version(self.store, {'id': 'staff'}, cert_id, label=label, source_ref='测试教学依据')

    def test_sources_current_rules_and_teaching_are_connected_without_approval(self):
        result = self.update()
        self.assertEqual(result['current_rule_count'], len(self.rules))
        self.assertEqual(result['teaching']['rule_links'], len(self.rules))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM Policy WHERE validity_status<>'unverified'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM TaxRule WHERE review_status='approved'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_point_mark').fetchone()[0], 0)
            for rule in self.rules:
                refs = sync.rule_knowledge(db, sync.PREFIX + rule.id, self.rules)
                self.assertEqual(refs[0]['rule_id'], compile_rule(rule)['rule_id'])
                self.assertTrue(refs[0]['sources'], rule.id)
            for group in sync.GROUPS:
                self.assertTrue(portal._candidate_rules(db, result['teaching']['certificate_id'], sync.PREFIX + group))

    def test_cli_sync_includes_guangdong_topics_and_the_full_policy_migration(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        from src.kg_retrieval.cli import main
        output = StringIO()
        with redirect_stdout(output):
            status = main(['sync-knowledge', '--db', str(self.store.path),
                           '--manifest', str(sync.MANIFEST), '--originals', str(self.originals)])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['formal_approvals'], 0)
        self.assertEqual(result['teaching']['rule_links'], len(self.rules))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM TaxRule WHERE rule_code IN ('GD-DEED-BASE','GD-PROPERTY-BASE')").fetchone()[0], 2)
            self.assertGreater(db.execute('SELECT count(*) FROM Exception').fetchone()[0], 0)

    def test_repeat_preserves_review_custom_content_and_version_identity(self):
        first = self.update()
        with self.store.connect() as db:
            db.execute("INSERT INTO certificate(id,code,name,created_at,updated_at) VALUES ('own','own','教师自己的内容','now','now')")
            pid = db.execute('SELECT policy_id FROM Policy ORDER BY policy_id LIMIT 1').fetchone()[0]
            db.execute("UPDATE Policy SET validity_status='in_force',issuer='人工核对的发布机关' WHERE policy_id=?", (pid,))
            db.execute("UPDATE Clause SET extraction_status='verified' WHERE policy_id=?", (pid,))
        before = self.counts()
        again = self.update()
        self.assertEqual(self.counts(), before)
        self.assertEqual(first['teaching']['content_version_id'], again['teaching']['content_version_id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT validity_status FROM Policy WHERE policy_id=?', (pid,)).fetchone()[0], 'in_force')
            self.assertEqual(db.execute("SELECT name FROM certificate WHERE id='own'").fetchone()[0], '教师自己的内容')

    def test_new_rule_version_waits_for_explicit_teaching_publication(self):
        first = self.update()
        old_rule = self.rules[0]
        changed = replace(old_rule, version='99.0', threshold_basis=old_rule.threshold_basis + '；测试修订')
        rules = [changed if r.id == old_rule.id else r for r in self.rules]
        with self.store.connect() as db:
            frozen = db.execute('SELECT rules_snapshot_json FROM training_content_version WHERE id=?',
                                (first['teaching']['content_version_id'],)).fetchone()[0]
        updated = self.update(rules)
        self.assertEqual(first['teaching']['content_version_id'], updated['teaching']['content_version_id'])
        with self.store.connect() as db:
            active = content.resolve_active_version(db, first['teaching']['certificate_id'])
            self.assertTrue(content.update_status(db, active, rules)['pending'])
        published = self.publish(first['teaching']['certificate_id'], rules)
        self.assertNotEqual(first['teaching']['content_version_id'], published['id'])
        with self.store.connect() as db:
            old = db.execute('SELECT * FROM training_content_version WHERE id=?', (first['teaching']['content_version_id'],)).fetchone()
            self.assertEqual(old['rules_snapshot_json'], frozen)
            self.assertEqual(old['status'], 'retired')
            self.assertEqual(sync.rule_knowledge(db, sync.PREFIX + old_rule.id, rules)[0]['rule_version'], '99.0')
            self.assertTrue(sync.rule_knowledge(db, sync.PREFIX + old_rule.id, rules)[0]['sources'])

    def test_changed_source_rolls_back_both_rules_and_teaching(self):
        before = self.counts()
        broken = deepcopy(self.plan)
        key = next(iter(broken['files']))
        bad = Path(self.temp.name) / 'changed.txt'
        bad.write_text('tampered source', encoding='utf-8')
        broken['files'][key]['path'] = str(bad)
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.update(plan=broken)
        self.assertEqual(self.counts(), before)

    def test_open_attempt_keeps_original_material_and_grading_after_rule_update(self):
        result = self.update()
        with self.store.connect() as db:
            db.execute("INSERT INTO student_info VALUES ('student','测试院校','S1','测试学生',NULL,?,NULL,1,'now')",
                       (PasswordHasher().hash('test-password'),))
        cert = result['teaching']['certificate_id']
        rule = self.rules[0]
        attempt = portal.start_practice(self.store, 'student', cert, sync.PREFIX + rule.id, seed=42)
        changed = replace(rule, version='99.0', threshold_basis=rule.threshold_basis + '；测试修订')
        newer = [changed if r.id == rule.id else r for r in self.rules]
        self.update(newer)
        from webapp.training_tutor import _build_context
        with self.store.connect() as db:
            nodes, _ = _build_context(db, 'student', cert, sync.PREFIX + rule.id, None, newer)
        point = next(n for n in nodes if n['kind'] == 'knowledge_point')
        self.assertNotIn('测试修订', str(point['details']))
        with patch('webapp.training_portal._load_rules', return_value=newer):
            resumed = portal.get_attempt(self.store, 'student', attempt['id'])
            self.assertEqual(attempt['materials'], resumed['materials'])
            scored = portal.submit_practice(self.store, 'student', attempt['id'], [rule.id])
            waiting = portal.start_practice(self.store, 'student', cert, sync.PREFIX + rule.id, seed=42)
        self.assertEqual(waiting['content_version']['id'], attempt['content_version']['id'])
        self.assertEqual(waiting['materials'], attempt['materials'])
        self.publish(cert, newer)
        with patch('webapp.training_portal._load_rules', return_value=newer):
            fresh = portal.start_practice(self.store, 'student', cert, sync.PREFIX + rule.id, seed=42)
            scored = portal.get_attempt(self.store, 'student', attempt['id'])
        self.assertEqual(scored['status'], 'scored')
        self.assertNotEqual(fresh['content_version']['id'], attempt['content_version']['id'])
        self.assertIn('轮换', scored['applicability']['notice'])
        with self.store.connect() as db:
            db.execute('UPDATE knowledge_point_link SET active=0 WHERE knowledge_point_id=?', (sync.PREFIX + rule.id,))
            nodes, _ = _build_context(db, 'student', cert, None, attempt['id'], newer)
        self.assertEqual(nodes[0]['kind'], 'attempt_analysis')
        brief = next(n for n in nodes if n['id'] == 'rule:' + rule.id)
        self.assertEqual(brief['details']['version'], rule.version)
        point = next(n for n in nodes if n['kind'] == 'knowledge_point')
        self.assertNotIn('测试修订', str(point['details']))
        self.assertEqual(point['details']['outline_version'], attempt['content_version']['label'])
        self.assertTrue(any(n['kind'] == 'policy_clause' for n in nodes))
        clause = next(n for n in nodes if n['kind'] == 'policy_clause')
        self.assertTrue(clause['details']['content'])
        self.assertIn('extraction_status', clause['details'])

    def test_published_policy_text_is_frozen_and_changes_require_new_version(self):
        result = self.update()
        cert, key = result['teaching']['certificate_id'], sync.PREFIX + self.rules[0].id
        with self.store.connect() as db:
            active = content.resolve_active_version(db, cert)
            original = sync.rule_knowledge(db, key, self.rules, active)[0]['sources'][0]
            self.assertTrue(original['content'])
            db.execute("UPDATE Clause SET content='测试修订原文' WHERE clause_id=?", (original['clause_id'],))
            db.execute("UPDATE Policy SET validity_status='repealed' WHERE policy_id=?", (original['policy_id'],))
            frozen = sync.rule_knowledge(db, key, self.rules, active)[0]['sources'][0]
            self.assertEqual(frozen['content'], original['content'])
            self.assertEqual(frozen['current_validity_status'], 'repealed')
            self.assertTrue(content.update_status(db, active, self.rules)['evidence_changed'])
        self.publish(cert, self.rules)
        with self.store.connect() as db:
            active = content.resolve_active_version(db, cert)
            revised = sync.rule_knowledge(db, key, self.rules, active)[0]['sources'][0]
            self.assertEqual(revised['content'], '测试修订原文')
            self.assertFalse(content.update_status(db, active, self.rules)['pending'])

    def test_teaching_uses_the_same_published_override_as_enterprise_knowledge(self):
        rule = self.rules[0]
        override = {'version': '99.0', 'logic': rule.logic, 'threshold_basis': '测试发布口径',
                    'effective_from': '2026-01-01', 'effective_to': None}
        with patch.object(self.store, 'rule_overrides', return_value={rule.id: override}), \
                patch.object(self.store, 'rule_versions', return_value={}):
            enterprise = effective_rules(self.store, sync.ROOT / 'rules')
            teaching = portal._load_rules(self.store)
        self.assertEqual(compile_rule(enterprise[0])['fingerprint'], compile_rule(teaching[0])['fingerprint'])
        self.assertEqual(teaching[0].version, '99.0')

    def test_new_maintained_rule_is_not_omitted_by_the_curriculum_mapping(self):
        extra = replace(self.rules[0], id='R-NEW', name='新增规则仿真')
        result = self.update([*self.rules, extra])
        self.assertEqual(result['teaching']['rule_links'], len(self.rules) + 1)
        with self.store.connect() as db:
            self.assertTrue(db.execute('SELECT 1 FROM knowledge_point WHERE id=?', (sync.PREFIX + 'R-NEW',)).fetchone())

    def test_disabled_teaching_link_is_not_used_for_new_questions(self):
        result = self.update()
        rid = self.rules[0].id
        key = sync.PREFIX + rid
        with self.store.connect() as db:
            db.execute("UPDATE knowledge_point_link SET active=0 WHERE knowledge_point_id=?", (key,))
            self.assertEqual(portal._candidate_rules(db, result['teaching']['certificate_id'], key), [])
            self.assertEqual(sync.rule_knowledge(db, key, self.rules), [])

    def test_repeat_with_another_retention_directory_keeps_verified_old_locations(self):
        self.update()
        before = self.counts()
        with self.store.connect() as db:
            paths = [(r['policy_id'], r['raw_file_path']) for r in db.execute('SELECT policy_id,raw_file_path FROM Policy ORDER BY policy_id')]
        self.originals = Path(self.temp.name) / 'another-originals'
        self.update()
        self.assertEqual(self.counts(), before)
        with self.store.connect() as db:
            self.assertEqual([(r['policy_id'], r['raw_file_path']) for r in db.execute('SELECT policy_id,raw_file_path FROM Policy ORDER BY policy_id')], paths)
