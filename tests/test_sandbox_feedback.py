from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import engine, sandbox_feedback
from src.exercise_generator import generate
from src.models import Metric
from webapp import app as module
from webapp.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class SandboxFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = engine.load_rules(ROOT / "rules")

    def setUp(self):
        self.case = generate(self.rules, "R-020", 17)

    def feedback(self, **body):
        return sandbox_feedback.feedback(self.case.dataset, self.rules, **body)

    def test_mark_risk_coaches_evidence_for_all_rules_without_assessing_answers(self):
        before = deepcopy(self.case.dataset)
        for rule in self.rules:
            with self.subTest(rule=rule.id):
                result = self.feedback(action="mark_risk", rule_id=rule.id)
                self.assertEqual(len(result["guide"]["inputs"]), len(rule.inputs))
                self.assertTrue(all(m["code"] == "unlocated_evidence" for m in result["messages"]))
                located = self.feedback(action="mark_risk", rule_id=rule.id, evidence_metrics=list(rule.inputs))
                self.assertEqual([m["code"] for m in located["messages"]], ["evidence_located"])
                self.assertIsNone(located["calculation"])
                for forbidden in ("standard_answer", "status", "measured", "conclusion", "threshold_desc"):
                    self.assertNotIn(forbidden, str(result))
        self.assertEqual(self.case.dataset, before)

    def test_missing_material_and_foreign_evidence_are_not_silently_zero(self):
        del self.case.dataset.metrics["利润表.营业成本"]
        result = self.feedback(action="mark_risk", rule_id="R-020")
        self.assertIn("missing_material", [m["code"] for m in result["messages"]])
        self.assertFalse(next(x for x in result["guide"]["inputs"] if x["name"]=="利润表.营业成本")["present"])
        with self.assertRaises(ValueError):
            self.feedback(action="mark_risk", rule_id="R-020", evidence_metrics=["参考.税负率上限"])
        with self.assertRaises(ValueError):
            self.feedback(action="mark_risk", rule_id="R-999")

    def test_difference_ratio_percent_and_negative_cash_amounts(self):
        result = self.feedback(action="calculate", left="利润表.营业收入", right="利润表.营业成本")
        self.assertEqual(Decimal(result["calculation"]["value"]), Decimal("730000"))
        ratio = self.feedback(action="calculate", left="利润表.营业成本", right="利润表.营业收入", operation="ratio")
        self.assertEqual(Decimal(ratio["calculation"]["value"]), Decimal(".5"))
        self.assertEqual(Decimal(ratio["calculation"]["percent"]), Decimal("50"))
        self.assertEqual(ratio["messages"][0]["code"], "ratio_unit")
        signed = self.feedback(action="calculate", left="现金流量表.投资净额", right="现金流量表.经营净额", operation="ratio")
        self.assertLess(Decimal(signed["calculation"]["value"]), 0)

    def test_zero_denominator_missing_operands_units_and_nonfinite(self):
        for body, code in [({"left":"营业收入", "right":"销售.其他调节", "operation":"ratio"}, "zero_denominator"),
                           ({}, "missing_operand"),
                           ({"left":"人力.个税申报人数", "right":"营业收入"}, "unit_mismatch"),
                           ({"left":"参考.毛利率下限", "right":"营业收入"}, "unit_mismatch")]:
            result = self.feedback(action="calculate", **body)
            self.assertIsNone(result["calculation"])
            self.assertIn(code, [m["code"] for m in result["messages"]])
        self.case.dataset.metrics["营业收入"].value = Decimal("NaN")
        result = self.feedback(action="calculate", left="营业收入", right="营业成本")
        self.assertEqual(result["messages"][0]["code"], "invalid_value")
        with self.assertRaises(ValueError):
            self.feedback(action="calculate", left="other-client.metric", right="营业收入")

    def test_explicit_periods_same_period_year_on_year_and_missing_provenance(self):
        body = {"action":"calculate", "left":"利润表.营业收入", "right":"历史.上年同期收入"}
        mismatch = self.feedback(**body)
        self.assertEqual(mismatch["messages"][0]["code"], "period_mismatch")
        self.assertIsNone(mismatch["calculation"])
        valid = self.feedback(**body, period_mode="year_on_year")
        self.assertIsNotNone(valid["calculation"])
        invalid = self.feedback(action="calculate", left="年度.本年净利润", right="年度.前年净利润", period_mode="year_on_year")
        self.assertEqual(invalid["messages"][0]["code"], "invalid_year_on_year")
        self.case.dataset.metrics["历史.上年同期收入"] = Metric("历史.上年同期收入", Decimal(1), "来源未说明", "没有实际期间")
        self.assertEqual(self.feedback(**body, period_mode="year_on_year")["messages"][0]["code"], "unknown_period")

    def test_period_granularity_is_not_guessed_from_names_and_path_rule_not_numeric(self):
        metric = self.case.dataset.metrics["历史.上年同期收入"]
        metric.source = "历史指标!B2 ← 原始表；实际期间 2025H1"
        metric.detail = "明确半年数据"
        result = self.feedback(action="calculate", left="利润表.营业收入", right=metric.name, period_mode="year_on_year")
        self.assertEqual(result["messages"][0]["code"], "invalid_year_on_year")
        # Graph rules require path evidence; do not claim a numeric test graded it.
        rule = replace(self.rules[0], id="G-001", category="关联方图", logic={}, inputs={})
        result = sandbox_feedback.feedback(self.case.dataset, [rule], action="mark_risk", rule_id=rule.id)
        self.assertIsNone(result["calculation"])
        self.assertIn("路径", result["guide"]["method"])


class SandboxFeedbackWebTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED":"0", "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED":"0"})
        self.env.start(); self.tmp = TemporaryDirectory(); self.old_store = module.store
        self.store = Store(Path(self.tmp.name) / "sandbox.db"); module.store = self.store
        self.password = "Sandbox-feedback-2026!"
        self.teacher = self.store.create_user("sandbox-teacher", self.password, "教师", "teacher", "school-a")
        self.student = self.store.create_user("sandbox-student", self.password, "学生", "student", "school-a")
        self.client = TestClient(module.app); self.client.__enter__(); self.login(self.teacher)
        self.case = self.client.post("/api/exercises", json={"rule_id":"R-020", "expected_version":"2.0", "seed":17}).json()
        self.audit_id = self.case["audit_id"]
        self.assignment = self.client.post("/api/assignments", json={"title":"即时提示", "audit_id":self.audit_id,
            "target_student_id":self.student["id"]}).json()["id"]
        self.url = f"/api/assignments/{self.assignment}/feedback"
        self.login(self.student)

    def tearDown(self):
        self.client.__exit__(None, None, None); module.store = self.old_store; self.tmp.cleanup(); self.env.stop()

    def login(self, user):
        _, token = self.store.authenticate(user["username"], self.password)
        self.client.cookies.clear(); self.client.cookies.set(module.COOKIE_NAME, token)

    def test_feedback_does_not_use_finding_answers_and_never_submits_or_changes_materials(self):
        body = {"action":"mark_risk", "rule_id":"R-020"}
        first = self.client.post(self.url, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["Cache-Control"], "private, no-store")
        entry = self.store.get_audit(self.audit_id)
        altered = {**entry, "findings":[replace(f, status="pass" if f.status=="hit" else "hit", measured=999,
            calculation="FORBIDDEN", conclusion="FORBIDDEN", skip_reason="FORBIDDEN") for f in entry["findings"]]}
        with patch.object(self.store, "get_audit", return_value=altered), \
             patch.object(module, "_audit_rules", side_effect=AssertionError("frozen only")), \
             patch("src.engine.run", side_effect=AssertionError("no grading before submit")):
            self.assertEqual(self.client.post(self.url, json=body).json(), first.json())
        self.assertIsNone(self.store.get_submission(self.assignment, self.student["id"]))
        self.assertEqual(self.store.get_audit(self.audit_id), entry)
        for forbidden in ("standard_answer", "FORBIDDEN", "requested_rule_id", "changed_metrics", "seed", "case_sha256"):
            self.assertNotIn(forbidden, first.text)
        score = self.client.post(f"/api/assignments/{self.assignment}/submit",
            json={"selected_rule_ids":self.case["metadata"]["standard_answer"]})
        self.assertEqual(score.json()["score"], 100)

    def test_roles_organizations_targets_publication_and_unauthenticated(self):
        body={"action":"mark_risk", "rule_id":"R-001"}
        for username, role, org, expected in [("other-student","student","school-a",404),
                ("foreign-teacher","teacher","school-b",404), ("accountant","accountant","school-a",403),
                ("admin","org_admin","school-a",403), ("platform","platform_admin","school-a",403)]:
            user=self.store.create_user(username,self.password,username,role,org);self.login(user)
            self.assertEqual(self.client.post(self.url,json=body).status_code,expected)
        self.client.cookies.clear();self.assertEqual(self.client.post(self.url,json=body).status_code,401)
        self.login(self.student)
        with self.store.connect() as db:
            db.execute("UPDATE assignments SET published=0 WHERE id=?", (self.assignment,))
        self.assertEqual(self.client.post(self.url,json=body).status_code,404)
        self.login(self.teacher);self.assertEqual(self.client.post(self.url,json=body).status_code,200)

    def test_validation_unknown_ids_oversized_and_source_identity(self):
        for body in ({"action":"grade"},{"action":"mark_risk","rule_id":"R-999"},
                {"action":"calculate","left":"foreign.metric","right":"营业收入"},
                {"action":"calculate","operation":"eval"},{"action":"calculate","period_mode":"monthly"},
                {"action":"mark_risk","rule_id":"R-020","evidence_metrics":["营业收入"]},
                {"action":"calculate","conclusion":"hit"},{"action":"calculate","left":"a"*161},
                {"action":"mark_risk","evidence_metrics":["x"]*101}):
            self.assertEqual(self.client.post(self.url,json=body).status_code,422)
        entry=self.store.get_audit(self.audit_id)
        with patch.object(self.store,"get_audit",return_value={**entry,"org_id":"school-b"}):
            self.assertEqual(self.client.post(self.url,json={"action":"calculate"}).status_code,404)

    def test_frozen_feedback_restart_backup_preserves_periods_and_legacy_missing_material(self):
        from scripts.ops_db import create_backup, restore_backup
        body={"action":"calculate","left":"利润表.营业收入","right":"历史.上年同期收入","period_mode":"year_on_year"}
        original=self.client.post(self.url,json=body).json()
        backup=Path(self.tmp.name)/"backup.db"; restored=Path(self.tmp.name)/"restored.db"
        create_backup(self.store.path,backup);restore_backup(backup,restored);module.store=Store(restored)
        with patch.object(module,"_audit_rules",side_effect=AssertionError("no current rules")):
            self.assertEqual(self.client.post(self.url,json=body).json(),original)
        entry=module.store.get_audit(self.audit_id);del entry["dataset"].metrics["利润表.营业成本"]
        with patch.object(module.store,"get_audit",return_value=entry):
            result=self.client.post(self.url,json={"action":"mark_risk","rule_id":"R-020"})
            self.assertEqual(result.status_code,200)
            self.assertIn("missing_material",result.text)


if __name__ == "__main__":
    unittest.main()
