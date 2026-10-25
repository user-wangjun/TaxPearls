"""A downloaded checkout serves the full campus graph without an import command."""
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.public_policy_package import bootstrap
from webapp import app as main, university_members
from webapp.knowledge_sync import PREFIX
from webapp.login_guard import LoginGuard
from webapp.storage import Store, _now, _passwords


class PublicKnowledgeStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'campus.sqlite')
        self.addCleanup(self.store.close)
        self.addCleanup(patch.stopall)
        # 本模块断言的就是"启动即安装"这条链路，不能被本地提速开关
        # TAXPEARLS_PUBLIC_PACKAGE_INSTALL=0 影响。
        patch.dict(os.environ, {'TAXPEARLS_PUBLIC_PACKAGE_INSTALL': '1'}).start()
        patch.object(main, 'store', self.store).start()
        patch.object(main, 'login_guard', LoginGuard()).start()

    def test_first_start_teacher_student_source_chain_and_restart(self):
        password = 'Campus-startup-2026!'
        with TestClient(main.app) as client:
            self.assertEqual(main.app.state.public_knowledge['status'], 'installed')
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM Policy').fetchone()[0], 37)
                self.assertEqual(db.execute('SELECT count(*) FROM Clause').fetchone()[0], 1333)
                self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT count(*) FROM knowledge_point').fetchone()[0], 28)
                self.assertEqual(db.execute("SELECT count(*) FROM TaxRule WHERE review_status='approved'").fetchone()[0], 0)
            teacher = self.store.create_user('teacher@startup.test', password, '启动测试教师', 'org_admin', 'campus')
            student = self.store.create_user('student@startup.test', password, '启动测试学生', 'student', 'campus')
            with self.store.connect() as db:
                db.execute("INSERT INTO org_settings(org_id,org_type,display_name,updated_at) VALUES ('campus','university','启动测试高校',?)", (_now(),))
                university_members.provision_founder(db, teacher['id'], 'campus', 5, 10)
                db.execute("INSERT INTO student_info VALUES ('learner','campus','S001','学生','教学一班',?,NULL,1,?)", (_passwords.hash(password), _now()))
                db.execute("INSERT INTO university_identity_links VALUES (?,NULL,'learner')", (student['id'],))
            graphs = []
            for username in (teacher['username'], student['username']):
                client.cookies.clear()
                response = client.post('/api/login', json={'username': username, 'password': password})
                self.assertEqual(response.status_code, 200, response.text)
                response = client.get('/api/training/knowledge/graph')
                self.assertEqual(response.status_code, 200, response.text)
                graph = response.json()
                graphs.append(graph)
                self.assertEqual(graph['public_source'], 'shared_policy_catalog')
                self.assertEqual(sum(n['schema_type'] == 'Policy' for n in graph['nodes']), 37)
                self.assertEqual(sum(n['schema_type'] == 'Clause' for n in graph['nodes']), 1333)
                kp = next(n for n in graph['nodes'] if n['record_id'] == PREFIX + 'R-001')
                self.assertTrue(any(e['source'] == kp['id'] and e['target'] == 'R-001' for e in graph['edges']))
                sources = [e['source'] for e in graph['edges'] if e['target'] == 'R-001' and e.get('relation_type') == 'PROVIDES_CONTEXT']
                self.assertTrue(sources)
                clause = next(n for n in graph['nodes'] if n['id'] == sources[0])
                self.assertTrue(clause['source_location'])
                self.assertTrue(any(n['schema_type'] == 'Policy' and n['policy_id'] == clause['policy_id'] for n in graph['nodes']))
                self.assertFalse(any(n['kind'] in {'company', 'risk', 'audit'} for n in graph['nodes']))
            self.assertEqual(graphs[0], graphs[1])
            with self.store.connect() as db:
                policy = db.execute('SELECT policy_id FROM Policy ORDER BY policy_id LIMIT 1').fetchone()[0]
                db.execute("UPDATE Policy SET validity_status='in_force',issuer='已人工核实' WHERE policy_id=?", (policy,))
                frozen = [tuple(r) for r in db.execute('SELECT * FROM training_content_version ORDER BY id')]
        backups = list(Path(self.temp.name).glob('*.before-public-*.sqlite'))
        with patch('src.public_policy_package.import_package', side_effect=AssertionError('restart must skip import')):
            with TestClient(main.app):
                self.assertEqual(main.app.state.public_knowledge['status'], 'current')
        self.assertEqual(list(Path(self.temp.name).glob('*.before-public-*.sqlite')), backups)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT issuer FROM Policy WHERE policy_id=?', (policy,)).fetchone()[0], '已人工核实')
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM training_content_version ORDER BY id')], frozen)
        # A -> B -> A is a real change each time; an old marker must not leave
        # B's maintained knowledge-point description active after returning to A.
        from src.rule_conversion import effective_rules
        rules = effective_rules(self.store, main.RULES_DIR)
        changed = [replace(r, scope=r.scope + ' 启动版本切换验证') if r.id == 'R-001' else r for r in rules]
        with patch('src.rule_conversion.effective_rules', return_value=changed):
            self.assertEqual(bootstrap(self.store, main.RULES_DIR)['status'], 'installed')
        self.assertEqual(bootstrap(self.store, main.RULES_DIR)['status'], 'installed')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM public_knowledge_installation').fetchone()[0], 1)
            description = db.execute('SELECT description FROM knowledge_point WHERE id=?', (PREFIX + 'R-001',)).fetchone()[0]
            self.assertNotIn('启动版本切换验证', description)
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM training_content_version ORDER BY id')], frozen)

    def test_invalid_package_blocks_startup_before_serving_an_empty_graph(self):
        with patch('src.public_policy_package.load_package', side_effect=ValueError('原文校验失败')):
            with self.assertRaisesRegex(ValueError, '原文校验失败'):
                with TestClient(main.app):
                    self.fail('must not serve requests after failed initialization')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM Policy').fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='public_knowledge_installation'").fetchone())

    def test_teaching_failure_rolls_back_and_next_start_can_retry(self):
        with patch('webapp.knowledge_sync.teaching', side_effect=ValueError('教学初始化失败')):
            with self.assertRaisesRegex(ValueError, '教学初始化失败'):
                bootstrap(self.store, main.RULES_DIR)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM Policy').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_point').fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='public_knowledge_installation'").fetchone())
        self.assertEqual(bootstrap(self.store, main.RULES_DIR)['status'], 'installed')
