from copy import deepcopy
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from src import engine, loader, render, snapshots
from webapp import app as module
from webapp import risk_alerts
from webapp.access import AccessDenied
from webapp.dashboard import collect
from webapp.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def test_client_name_does_not_hide_audit_company_name(self):
        from webapp.dashboard import collect
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            owner = store.create_user('owner', 'Review-Probe-2026!', '机构', 'org_admin', 'a')
            outsider = store.create_user('other', 'Review-Probe-2026!', '外部', 'org_admin', 'b')
            data = loader.load(ROOT / 'samples/样例企业-审计材料.xlsx')
            data.company.name = '仿真审计企业'
            customer = store.upsert_client(owner, '模拟商贸企业02（纯合成测试）', data.company.taxpayer_id)
            findings = engine.run(engine.load_rules(ROOT / 'rules'), data)
            summary = render.build_view_model(data, findings)['summary']
            for month in range(1, 9):
                snapshot = deepcopy(data)
                snapshot.company.period = f'2026-{month:02}'
                store.save_audit(f'period-{month}', owner, customer['id'], snapshot, findings, summary,
                                 f'2026-{month:02}-28T12:00:00')
            result = collect(store, owner, data.company.taxpayer_id)
            self.assertEqual(result['companies'], [{'key': data.company.taxpayer_id,
                'name': '仿真审计企业（客户档案：模拟商贸企业02（纯合成测试））'}])
            self.assertEqual(result['total_periods'], 8)
            self.assertEqual(len(result['records']), 8)
            self.assertEqual(collect(store, outsider)['companies'], [])
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT name FROM clients WHERE id=?', (customer['id'],)).fetchone()[0],
                                 '模拟商贸企业02（纯合成测试）')

    def test_latest_period_snapshot_and_permissions(self):
        with TemporaryDirectory() as tmp:
            old = module.store
            module.store = Store(Path(tmp) / "test.db")
            try:
                store = module.store
                owner = store.create_user("owner", "test-password-2026", "会计", "accountant", "a")
                other = store.create_user("other", "test-password-2026", "同机构会计", "accountant", "a")
                outsider = store.create_user("outsider", "test-password-2026", "外部", "org_admin", "b")
                student = store.create_user("student", "test-password-2026", "学生", "student", "a")
                data = loader.load(ROOT / "samples/样例企业-审计材料.xlsx")
                client_id = store.upsert_client(owner, data.company.name, data.company.taxpayer_id, owner["id"])
                rules = engine.load_rules(ROOT / "rules")
                findings = engine.run(rules, data)
                summary = render.build_view_model(data, findings)["summary"]
                for audit_id, period in [("old", "2026-01"), ("new", "2026-01"), ("next", "2026-02")]:
                    snapshot = deepcopy(data)
                    snapshot.company.period = period
                    store.save_audit(audit_id, owner, client_id["id"], snapshot, findings, summary, "2026-01-01T12:00:00")
                with TestClient(module.app) as client:
                    self.assertEqual(client.get("/api/dashboard").status_code, 401)
                    self.assertEqual(client.get("/api/knowledge").status_code, 401)
                    for user in [owner, other, outsider, student]:
                        client.post("/api/login", json={"username": user["username"], "password": "test-password-2026"})
                        response = client.get("/api/dashboard")
                        scoped_graph = client.get("/api/knowledge/graph?audit_id=new")
                        self.assertEqual(scoped_graph.status_code, 200 if user == owner else 403 if user == student else 404)
                        if user != owner:
                            scoped_ask = client.post("/api/knowledge/ask", json={"audit_id":"new", "node_id":"R-001", "question":"解释"})
                            self.assertEqual(scoped_ask.status_code, 403 if user == student else 404)
                        if user == student:
                            self.assertEqual(response.status_code, 403)
                        elif user == owner:
                            body = response.json()
                            self.assertEqual([r["id"] for r in body["records"]], ["next", "new"])
                            self.assertEqual(len(body["history"]), 3)
                            self.assertEqual(body["records"][0]["metrics"]["营业收入"]["value"], str(data.metrics["营业收入"].value))
                            self.assertNotIn("利润表.净利润", body["records"][0]["metrics"])
                        else:
                            self.assertEqual(response.json()["records"], [])
                        graph = client.get("/api/knowledge")
                        self.assertEqual(graph.status_code, 200)
                        self.assertEqual(len(graph.json()), len(rules))
                        self.assertEqual(graph.json()[0]["inputs"], rules[0].inputs)
                        self.assertEqual(graph.json()[0]["legal_basis"], rules[0].legal_basis)
                        client.post("/api/logout")
            finally:
                module.store = old


class RiskAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'risks.db')
        self.owner = self.store.create_user('reviewer', 'Review-Probe-2026!', '会计', 'accountant', 'a')
        self.other = self.store.create_user('other', 'Review-Probe-2026!', '其他会计', 'accountant', 'a')
        self.outsider = self.store.create_user('outsider', 'Review-Probe-2026!', '外部', 'org_admin', 'b')
        self.data = loader.load(ROOT / 'samples/样例企业-审计材料.xlsx')
        self.client = self.store.upsert_client(self.owner, self.data.company.name,
                                              self.data.company.taxpayer_id, self.owner['id'])
        self.findings = engine.run(engine.load_rules(ROOT / 'rules'), self.data)
        self.hit = next(item for item in self.findings if item.status == 'hit')
        self.save('original')

    def save(self, audit_id, data=None):
        data = data or self.data
        self.store.save_audit(audit_id, self.owner, self.client['id'], data, self.findings,
                             render.build_view_model(data, self.findings)['summary'], '2026-11-15T12:00:00')

    def record(self):
        return collect(self.store, self.owner)['records'][0]

    def body(self, status='done', note='已核对原始材料，差异已说明。', revision=0):
        return risk_alerts.ReviewBody(status=status, note=note, revision=revision)

    def test_reviews_persist_and_identical_saves_share_review_without_changing_results(self):
        before = self.store.get_audit('original')
        total = sum(item.status == 'hit' for item in self.findings)
        self.assertEqual(self.record()['alerts']['pending_count'], total)
        result = risk_alerts.save_review(self.store, self.owner, 'original', self.hit.rule.id, self.body())
        self.assertEqual(result['revision'], 1)
        self.save('identical')
        record = self.record()
        self.assertEqual(record['alerts']['pending_count'], total - 1)
        self.assertEqual(next(f['review'] for f in record['risks'] if f['id'] == self.hit.rule.id)['status'], 'done')
        self.assertEqual(record['summary']['hit'], total)
        self.assertEqual(self.store.get_audit('original'), before)
        with closing(sqlite3.connect(Path(self.tmp.name) / 'risks.db')) as db:
            raw = db.execute('SELECT review_note FROM risk_reviews').fetchone()[0]
            self.assertTrue(raw.startswith('tp-field-v1:'))
            self.assertNotIn('已核对', raw)
        # A changed input snapshot cannot inherit a closed review.
        changed = deepcopy(self.data)
        changed.metrics[next(iter(changed.metrics))].source += '（补充材料）'
        self.save('changed', changed)
        self.assertEqual(self.record()['alerts']['pending_count'], total)

    def test_concurrent_updates_and_reopening(self):
        risk_alerts.save_review(self.store, self.owner, 'original', self.hit.rule.id, self.body())
        with self.assertRaises(HTTPException) as conflict:
            risk_alerts.save_review(self.store, self.owner, 'original', self.hit.rule.id, self.body())
        self.assertEqual(conflict.exception.status_code, 409)
        reopened = risk_alerts.save_review(self.store, self.owner, 'original', self.hit.rule.id,
                                          self.body('reviewing', '补充核查', 1))
        self.assertEqual(reopened['status'], 'reviewing')
        self.assertEqual(reopened['revision'], 2)

    def test_reviews_require_authorized_hit_and_completion_note(self):
        for actor in (self.other, self.outsider):
            with self.assertRaises(AccessDenied):
                risk_alerts.save_review(self.store, actor, 'original', self.hit.rule.id, self.body())
        with self.assertRaises(HTTPException) as missing:
            risk_alerts.save_review(self.store, self.owner, 'original', self.hit.rule.id, self.body(note='  '))
        self.assertEqual(missing.exception.status_code, 422)
        non_hit = next(item for item in self.findings if item.status != 'hit')
        with self.assertRaises(AccessDenied):
            risk_alerts.save_review(self.store, self.owner, 'original', non_hit.rule.id, self.body())
        # Reassignment revokes the old accountant even with a known audit ID.
        admin = self.store.create_user('manager', 'Review-Probe-2026!', '管理员', 'org_admin', 'a')
        self.store.upsert_client(admin, self.data.company.name, self.data.company.taxpayer_id, self.other['id'])
        with self.assertRaises(AccessDenied):
            risk_alerts.save_review(self.store, self.owner, 'original', self.hit.rule.id, self.body())
        self.assertEqual(risk_alerts.save_review(self.store, self.other, 'original', self.hit.rule.id,
                                               self.body())['status'], 'done')

    def test_rule_reminder_respects_period_inputs_disabled_rules_and_no_automatic_audit(self):
        base = self.hit.rule
        dataset = snapshots.serialize_dataset(self.data)
        findings = snapshots.serialize_findings(self.findings)
        edition = {'version': '9.0', 'effective_from': '2090-01-01', 'effective_to': None,
                   'logic': base.logic, 'threshold_basis': base.threshold_basis, 'rule': asdict(base)}
        context = ([base], {base.id: [edition]}, None)
        self.assertEqual(risk_alerts.rule_updates(dataset, findings, context), [])
        edition['effective_from'] = None
        with patch.object(engine, 'run', side_effect=AssertionError('reminders must not run audits')):
            updates = risk_alerts.rule_updates(dataset, findings, context)
        self.assertEqual([(item['id'], item['after']) for item in updates], [(base.id, '9.0')])
        self.assertEqual(risk_alerts.rule_updates(dataset, findings, ([base], context[1], set())), [])
        missing_input = deepcopy(dataset)
        missing_input['metrics'].pop(next(iter(base.inputs)))
        self.assertEqual(risk_alerts.rule_updates(missing_input, findings, context), [])
        # A version crossing the original period calls for a period review.
        edition['effective_from'] = self.data.company.period[:4] + '-12-31'
        edition['effective_to'] = self.data.company.period[:4] + '-12-31'
        # Use an explicit full year so the selected interval crosses a boundary.
        annual = deepcopy(dataset)
        annual['company']['period'] = self.data.company.period[:4]
        split = risk_alerts.rule_updates(annual, findings, context)
        self.assertIsNone(split[0]['after'])
        self.assertIn('生效期', split[0]['reason'])
        # Same version with a changed definition still requires review.
        changed = replace(base, threshold_basis=base.threshold_basis + '（更新）')
        self.assertEqual(len(risk_alerts.rule_updates(dataset, findings, ([changed], {}, None))), 1)

    def test_api_authentication_validation_and_persistence(self):
        with patch.object(module, 'store', self.store), TestClient(module.app) as client:
            url = f'/api/audits/original/risks/{self.hit.rule.id}/review'
            self.assertEqual(client.put(url, json=self.body().model_dump()).status_code, 401)
            token = self.store.authenticate(self.owner['username'], 'Review-Probe-2026!')[1]
            client.cookies.set(module.COOKIE_NAME, token)
            response = client.put(url, json=self.body().model_dump())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get('/api/dashboard').json()['records'][0]['alerts']['pending_count'],
                             sum(item.status == 'hit' for item in self.findings) - 1)
            self.assertEqual(client.put(url, json=self.body().model_dump()).status_code, 409)
            invalid = {**self.body().model_dump(), 'status': 'dismissed'}
            self.assertEqual(client.put(url, json=invalid).status_code, 422)
            client.cookies.set(module.COOKIE_NAME, self.store.authenticate(self.outsider['username'], 'Review-Probe-2026!')[1])
            self.assertEqual(client.put(url, json=self.body().model_dump()).status_code, 404)
