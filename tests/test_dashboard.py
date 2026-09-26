from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from src import engine, loader, render
from webapp import app as module
from webapp.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
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
                    store.save_audit(audit_id, owner, client_id["id"], snapshot, findings, summary, "2026-09-21T12:00:00")
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
