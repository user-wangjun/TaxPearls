import base64
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from src import engine, loader
from webapp import app as application
from webapp.storage import Store


ROOT = Path(__file__).resolve().parents[1]


class EnterpriseGraphTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {
            'TAXPEARLS_FIELD_KEY': base64.b64encode(b'g' * 32).decode(),
            'TAXPEARLS_MATERIAL_KEY': base64.b64encode(b'm' * 32).decode(),
            'TAXPEARLS_BACKUP_KEY': base64.b64encode(b'b' * 32).decode(),
            'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '0',
        })
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Path(self.directory.name) / 'graph.db')
        self.old_store = application.store
        application.store = self.store
        self.addCleanup(setattr, application, 'store', self.old_store)
        self.password = 'GraphVerify!2026'
        self.admin = self.store.create_user('graphadmin', self.password, '管理员', 'org_admin', 'org-a')
        self.accountant = self.store.create_user('graphaccountant', self.password, '会计', 'accountant', 'org-a')
        self.outside = self.store.create_user('graphoutside', self.password, '外部管理员', 'org_admin', 'org-b')
        self.store.create_user('graphstudent', self.password, '学生', 'student', 'org-a')
        self.alpha = self.store.upsert_client(self.admin, '望星科技', 'TESTALPHA', self.accountant['id'])
        self.beta = self.store.upsert_client(self.admin, '望星科技', 'TESTBETA')
        self.store.upsert_client(self.admin, '待审企业', 'TESTEMPTY')
        outside_client = self.store.upsert_client(self.outside, '其他机构同税号企业', 'TESTALPHA')
        self.sample = loader.load(ROOT / 'samples/样例企业-审计材料.xlsx')
        self.rules = engine.load_rules(ROOT / 'rules')
        self.save('old', self.admin, None, '望星旧名称', ' test alpha ', '2024', 100, 'old-version')
        self.save('new', self.admin, self.alpha['id'], '望星科技', 'TESTALPHA', '2025', 200, 'new-version')
        self.save('recheck', self.admin, self.alpha['id'], '望星科技', 'TESTALPHA', '2025', 300, 'new-version')
        self.save('beta', self.admin, self.beta['id'], '望星科技', 'TESTBETA', '2025', 400, 'new-version')
        self.save('foreign', self.outside, outside_client['id'], '其他机构同税号企业', 'TESTALPHA', '2025', 999, 'new-version')
        self.client = self.enterContext(TestClient(application.app))
        self.login('graphadmin')

    def save(self, key, owner, customer, name, tax_id, period, revenue, version):
        data = copy.deepcopy(self.sample)
        data.company.name, data.company.taxpayer_id, data.company.period = name, tax_id, period
        data.metrics['营业收入'].value = revenue
        findings = copy.deepcopy(engine.run(self.rules, data))
        for finding in findings:
            finding.rule.version = version
        summary = {key: sum(f.status == key for f in findings) for key in ('hit', 'pass', 'skipped')}
        summary['total'] = len(findings)
        self.store.save_audit(key, owner, customer, data, findings, summary,
                              '2026-10-08T09:00:00+00:00')

    def login(self, username):
        result = self.client.post('/api/login', json={'username': username, 'password': self.password})
        self.assertEqual(result.status_code, 200, result.text)

    def graph(self, tax_id='TESTALPHA', **params):
        return self.client.get('/api/knowledge/graph', params={'taxpayer_id': tax_id, **params})

    def test_tax_identity_history_versions_and_immutable_snapshots(self):
        with self.store.connect() as db:
            before = [tuple(row) for row in db.execute('SELECT id,dataset_json,findings_json FROM audits ORDER BY id')]
        companies = self.client.get('/api/knowledge/enterprises').json()
        self.assertEqual(len(companies), 3)
        alpha = next(row for row in companies if row['taxpayer_id'] == 'TESTALPHA')
        self.assertEqual(len(alpha['audits']), 3)
        self.assertIn('望星旧名称', alpha['names'])
        self.assertFalse(any('其他机构' in name for row in companies for name in row['names']))
        graph = self.graph(' tEsT aLpHa ').json()
        ids = {node['id'] for node in graph['nodes']}
        self.assertEqual(len(ids), len(graph['nodes']))
        self.assertTrue(all(edge['source'] in ids and edge['target'] in ids for edge in graph['edges']))
        self.assertEqual(len([n for n in graph['nodes'] if n['kind'] == 'company']), 1)
        self.assertEqual({n['audit_id'] for n in graph['nodes'] if n['kind'] == 'audit'}, {'old', 'new', 'recheck'})
        self.assertEqual({n['value'] for n in graph['nodes'] if n['kind'] == 'metric' and n['label'] == '营业收入'}, {'100', '200', '300'})
        self.assertEqual({n['version'] for n in graph['nodes'] if n['kind'] == 'rule'}, {'old-version', 'new-version'})
        self.assertTrue(any(edge['label'] == '同项历史记录' for edge in graph['edges']))
        single = self.graph(audit_id='old').json()
        self.assertEqual({n['audit_id'] for n in single['nodes'] if 'audit_id' in n}, {'old'})
        self.assertEqual(single['nodes'][0]['id'], graph['nodes'][0]['id'])
        with self.store.connect() as db:
            after = [tuple(row) for row in db.execute('SELECT id,dataset_json,findings_json FROM audits ORDER BY id')]
        self.assertEqual(before, after)

    def test_permissions_empty_public_and_mismatched_record(self):
        self.assertEqual(self.graph(audit_id='beta').status_code, 404)
        self.assertEqual(self.graph(audit_id='foreign').status_code, 404)
        self.assertEqual(self.graph('missing').status_code, 404)
        self.assertEqual(self.graph('  ').status_code, 422)
        self.assertTrue(self.graph('TESTEMPTY').json()['empty'])
        public = self.client.get('/api/knowledge/graph').json()
        self.assertFalse(any(n['kind'] in {'company', 'risk', 'audit'} for n in public['nodes']))
        self.login('graphaccountant')
        self.assertEqual([row['taxpayer_id'] for row in self.client.get('/api/knowledge/enterprises').json()], ['TESTALPHA'])
        self.assertEqual({n['audit_id'] for n in self.graph().json()['nodes'] if 'audit_id' in n}, {'new', 'recheck'})
        self.assertEqual(self.graph('TESTBETA').status_code, 404)
        self.assertEqual(self.graph(audit_id='old').status_code, 404)
        self.login('graphoutside')
        self.assertEqual({n['audit_id'] for n in self.graph().json()['nodes'] if 'audit_id' in n}, {'foreign'})
        self.login('graphstudent')
        self.assertEqual(self.graph().status_code, 403)
        self.assertEqual(self.client.get('/api/knowledge/enterprises').status_code, 403)
        self.assertEqual(self.client.get('/api/knowledge/graph').status_code, 200)

    def test_question_context_and_permission_recheck(self):
        graph = self.graph().json()
        selected = next(n for n in graph['nodes'] if n['kind'] == 'risk')
        with patch('webapp.app.ask_graph', return_value={'answer': '历史解释', 'citations': [selected]}) as ask:
            answer = self.client.post('/api/knowledge/ask', json={'taxpayer_id': 'TESTALPHA', 'node_id': selected['id'], 'question': '比较历史'})
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertEqual(ask.call_args.args[0]['taxpayer_id'], 'TESTALPHA')
            self.assertEqual(len(ask.call_args.args[0]['audits']), 3)
        self.login('graphaccountant')

        def revoke(*args):
            with self.store.connect() as db:
                db.execute('UPDATE clients SET accountant_id=NULL WHERE id=?', (self.alpha['id'],))
            return {'answer': '不可返回', 'citations': []}

        with patch('webapp.app.ask_graph', side_effect=revoke):
            answer = self.client.post('/api/knowledge/ask', json={'taxpayer_id': 'TESTALPHA', 'node_id': selected['id'], 'question': '比较历史'})
            self.assertEqual(answer.status_code, 404, answer.text)

    def test_question_rejects_partial_history_revocation(self):
        selected = next(n for n in self.graph().json()['nodes'] if n['kind']=='risk' and n['audit_id']=='recheck')

        def remove_one_record(*args):
            with self.store.connect() as db:
                db.execute('DELETE FROM audits WHERE id=?', ('recheck',))
            return {'answer':'不应返回已失去访问权的记录内容','citations':[selected]}

        with patch('webapp.app.ask_graph',side_effect=remove_one_record):
            response=self.client.post('/api/knowledge/ask',json={'taxpayer_id':'TESTALPHA','node_id':selected['id'],'question':'解释这条历史记录'})
        self.assertEqual(response.status_code,409,response.text)
        self.assertNotIn('不应返回',response.text)
        self.assertEqual(self.graph().status_code,200)
