from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from webapp import app as app_module
from webapp.storage import Store


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "样例企业-审计材料.xlsx"


class RuleManagementTests(unittest.TestCase):
    def test_parameter_edit_trial_permissions_versions_snapshots_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rule-management.db"
            old_store = app_module.store
            app_module.store = Store(db_path)
            try:
                with TestClient(app_module.app) as client:
                    self.assertEqual(client.post("/api/setup", json={
                        "username": "rootadmin", "password": "platform-pass-2026",
                        "display_name": "平台管理员", "org_id": "org-a",
                    }).status_code, 200)
                    self.assertEqual(self._login(client, "rootadmin", "platform-pass-2026").status_code, 200)
                    created_users = {}
                    for username, role, org_id in (
                        ("teachera", "teacher", "org-a"),
                        ("accountanta", "accountant", "org-a"),
                        ("studenta", "student", "org-a"),
                        ("teacherb", "teacher", "org-b"),
                    ):
                        response = client.post("/api/users", json={
                            "username": username, "password": f"{username}-pass-2026",
                            "display_name": username, "role": role, "org_id": org_id,
                        })
                        self.assertEqual(response.status_code, 200, response.text)
                        created_users[username] = response.json()

                    assigned_client = client.post("/api/clients", json={
                        "name": "东莞市启明商贸有限公司（仿真样例）",
                        "taxpayer_id": "91441900MA5TEST0X0",
                        "accountant_id": created_users["accountanta"]["id"],
                    })
                    self.assertEqual(assigned_client.status_code, 200, assigned_client.text)
                    assigned_client_id = assigned_client.json()["id"]

                    first_audit = self._upload(client)
                    rule = self._rule(client, "R-001")
                    self.assertEqual(rule["version"], "2.0")
                    self.assertFalse(rule["customized"])
                    self.assertEqual(rule["logic"]["threshold"], 0.1)

                    draft_logic = deepcopy(rule["logic"])
                    draft_logic["threshold"] = 0.4
                    draft = {
                        "audit_id": first_audit,
                        "expected_version": "2.0", "new_version": "2.1",
                        "logic": draft_logic,
                        "threshold_basis": "相对偏离超过40%为本次试跑参数。",
                    }
                    trial = client.post("/api/rules/R-001/trial", json=draft)
                    self.assertEqual(trial.status_code, 200, trial.text)
                    self.assertEqual(trial.json()["status"], "pass")
                    self.assertEqual(trial.json()["version"], "2.1")

                    # A trial is non-destructive: the persisted audit retains its original rule snapshot.
                    old_result = client.get(f"/api/audits/{first_audit}").json()
                    old_r001 = next(item for item in old_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((old_r001["status"], old_r001["version"]), ("hit", "2.0"))

                    invalid = deepcopy(draft)
                    invalid["logic"] = {"type": "python_eval", "expression": "__import__('os')"}
                    response = client.post("/api/rules/R-001/trial", json=invalid)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn("不支持", response.text)

                    unknown = deepcopy(draft)
                    unknown["logic"]["left"] = "未知.指标"
                    response = client.post("/api/rules/R-001/trial", json=unknown)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn("未知指标", response.text)

                    not_advanced = deepcopy(draft)
                    not_advanced["new_version"] = "2.0"
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=not_advanced).status_code, 422)
                    not_advanced["new_version"] = "2.0.0"
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=not_advanced).status_code, 422)

                    client.post("/api/logout")
                    self._login(client, "studenta", "studenta-pass-2026")
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=draft).status_code, 403)
                    self.assertEqual(client.put("/api/rules/R-001/parameters", json=draft).status_code, 403)

                    client.post("/api/logout")
                    self._login(client, "teacherb", "teacherb-pass-2026")
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=draft).status_code, 404)

                    client.post("/api/logout")
                    self._login(client, "teachera", "teachera-pass-2026")
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=draft).status_code, 200)
                    self.assertEqual(client.put("/api/rules/R-001/parameters", json=draft).status_code, 403)

                    client.post("/api/logout")
                    self._login(client, "accountanta", "accountanta-pass-2026")
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=draft).status_code, 404)
                    accountant_audit = self._upload(client, assigned_client_id)
                    accountant_draft = {**draft, "audit_id": accountant_audit}
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=accountant_draft).status_code, 200)
                    self.assertEqual(client.put("/api/rules/R-001/parameters", json=accountant_draft).status_code, 403)

                    client.post("/api/logout")
                    self._login(client, "rootadmin", "platform-pass-2026")
                    saved = client.put("/api/rules/R-001/parameters", json=draft)
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertEqual(saved.json()["version"], "2.1")
                    self.assertTrue(saved.json()["customized"])

                    stale = deepcopy(draft)
                    stale["new_version"] = "2.2"
                    self.assertEqual(client.put("/api/rules/R-001/parameters", json=stale).status_code, 409)

                    second_audit = self._upload(client)
                    new_result = client.get(f"/api/audits/{second_audit}").json()
                    new_r001 = next(item for item in new_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((new_r001["status"], new_r001["version"]), ("pass", "2.1"))
                    old_result = client.get(f"/api/audits/{first_audit}").json()
                    old_r001 = next(item for item in old_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((old_r001["status"], old_r001["version"]), ("hit", "2.0"))

                    # Reopening the repository proves rule parameters and sessions survive restart.
                    app_module.store = Store(db_path)
                    reopened = self._rule(client, "R-001")
                    self.assertEqual(reopened["version"], "2.1")
                    self.assertEqual(reopened["logic"]["threshold"], 0.4)
                    actions = [row["action"] for row in client.get("/api/audit-log").json()]
                    self.assertIn("trial_rule_parameters", actions)
                    self.assertIn("update_rule_parameters", actions)
            finally:
                app_module.store = old_store

    @staticmethod
    def _login(client: TestClient, username: str, password: str):
        return client.post("/api/login", json={"username": username, "password": password})

    @staticmethod
    def _upload(client: TestClient, client_id: str | None = None) -> str:
        with SAMPLE.open("rb") as stream:
            response = client.post("/api/audit", files={
                "file": (SAMPLE.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            }, data={"client_id": client_id} if client_id else None)
        if response.status_code != 200:
            raise AssertionError(response.text)
        return response.json()["audit_id"]

    @staticmethod
    def _rule(client: TestClient, rule_id: str) -> dict:
        response = client.get("/api/rules")
        if response.status_code != 200:
            raise AssertionError(response.text)
        return next(item for item in response.json() if item["id"] == rule_id)
