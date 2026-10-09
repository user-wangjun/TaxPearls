"""知识点关联（FR-K05）与自主刷题闭环（FR-K06）接口测试。

规则来自仓库 rules/ 目录真实仿真题引擎：出题确定性（种子复现）、
判分解析判分后开放、重复提交不重复计数、错题清单与覆盖统计。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from src import engine
from webapp.storage import Store
from webapp.training_portal import RULES_DIR, register, today_cst

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_training_cookie"
STAFF_COOKIE = "taxpearls_staff_session"
NOW = "2026-10-09T21:40:00"


class TrainingPracticeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self.rules = engine.load_rules(RULES_DIR)
        self.rule_id = self.rules[0].id
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        staff_id, student_id, cert, cert2 = (uuid.uuid4().hex for _ in range(4))
        kp, kp2 = uuid.uuid4().hex, uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_id, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("study-pass"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert2, "empty", "无关联证书", "", "[]", "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
                       (kp, cert, "KP-01", "收入确认", "初级会计实务", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
                       (kp2, cert2, "KP-X", "占位知识点", "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point_mark (id,knowledge_point_id,mark_type,level,basis_ref,basis_version,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, kp, "high_freq", "high", "考纲第三章", "2027版", staff_id, NOW))
        self.staff_id, self.student_id, self.cert, self.cert2 = staff_id, student_id, cert, cert2
        self.kp, self.kp2 = kp, kp2

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _student_login(self):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "study-pass", "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _link(self, rule_id=None, kp=None):
        return self.client.post(f"/api/training/staff/knowledge-points/{kp or self.kp}/links", json={
            "target_type": "rule", "target_id": rule_id or self.rule_id})

    # ---- FR-K05 关联 ----------------------------------------------------

    def test_link_crud_and_validation(self):
        self._staff_login()
        self.assertEqual(self._link().status_code, 200)
        self.assertEqual(self._link().status_code, 409)  # 重复关联
        bad_rule = self._link(rule_id="no-such-rule")
        self.assertEqual(bad_rule.status_code, 422)
        question = self.client.post(f"/api/training/staff/knowledge-points/{self.kp}/links", json={
            "target_type": "question", "target_id": "q1"})
        self.assertEqual(question.status_code, 422)
        self.assertIn("题库尚未接入", question.json()["detail"])
        bad_task = self.client.post(f"/api/training/staff/knowledge-points/{self.kp}/links", json={
            "target_type": "task", "target_id": "no-such-task"})
        self.assertEqual(bad_task.status_code, 422)
        links = self.client.get(
            f"/api/training/staff/certificates/{self.cert}/knowledge-points"
        ).json()["knowledge_points"][0]["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["target_type"], "rule")
        link_id = links[0]["id"]
        self.assertEqual(self.client.delete(f"/api/training/staff/links/{link_id}").status_code, 200)
        self.assertEqual(self._link().status_code, 200)  # 删除后可重建

    def test_student_kp_list_shows_high_freq_and_links(self):
        self._staff_login()
        self._link()
        self._student_login()
        kps = self.client.get(f"/api/training/my/knowledge-points?certificate_id={self.cert}"
                              ).json()["knowledge_points"]
        self.assertEqual(len(kps), 1)
        self.assertEqual(kps[0]["rule_links"], 1)
        self.assertEqual(kps[0]["high_freq"][0]["basis_ref"], "考纲第三章")
        self.assertEqual(kps[0]["attempts"], 0)

    # ---- FR-K06 刷题闭环 ------------------------------------------------

    def _start(self, **kwargs):
        body = {"certificate_id": self.cert, "knowledge_point_id": self.kp}
        body.update(kwargs)
        return self.client.post("/api/training/my/practice/start", json=body)

    def test_practice_flow_score_and_analysis(self):
        self._staff_login()
        self._link()
        self._student_login()
        res = self._start()
        self.assertEqual(res.status_code, 200, res.text)
        attempt = res.json()["attempt"]
        self.assertEqual(attempt["status"], "open")
        self.assertNotIn("rule_id", attempt)  # 作答中不下发答案
        self.assertTrue(attempt["materials"]["accounts"])
        self.assertTrue(attempt["rule_catalog"])
        detail = self.client.get(f"/api/training/my/practice/{attempt['id']}").json()["attempt"]
        self.assertNotIn("rule_id", detail)
        scored = self.client.post(f"/api/training/my/practice/{attempt['id']}/submit", json={
            "selected_rule_ids": [self.rule_id]})
        self.assertEqual(scored.status_code, 200, scored.text)
        body = scored.json()["attempt"]
        self.assertEqual(body["status"], "scored")
        self.assertIn(self.rule_id, body["result"]["standard_answer"])
        self.assertTrue(body["result"]["correct"])  # 命中了目标规则
        # 重复提交不重复计数
        again = self.client.post(f"/api/training/my/practice/{attempt['id']}/submit", json={
            "selected_rule_ids": [self.rule_id]})
        self.assertEqual(again.status_code, 409)

    def test_practice_perfect_on_same_seed_with_standard_answer(self):
        self._staff_login()
        self._link()
        self._student_login()
        first = self._start(seed=42).json()["attempt"]
        first_result = self.client.post(
            f"/api/training/my/practice/{first['id']}/submit", json={
                "selected_rule_ids": [self.rule_id]}).json()["attempt"]["result"]
        # 同种子原题重做，按上一题解析中的标准答案作答 → 满分
        redo = self._start(seed=42, rule_id=self.rule_id).json()["attempt"]
        perfect = self.client.post(f"/api/training/my/practice/{redo['id']}/submit", json={
            "selected_rule_ids": first_result["standard_answer"]}).json()["attempt"]
        self.assertTrue(perfect["perfect"])
        self.assertEqual(perfect["score"], 100.0)

    def test_practice_wrong_answer_goes_to_wrong_list(self):
        self._staff_login()
        self._link()
        self._student_login()
        attempt = self._start().json()["attempt"]
        # 未作答（空选择）：得分 0、解析完整，计入错题
        wrong = self.client.post(f"/api/training/my/practice/{attempt['id']}/submit", json={
            "selected_rule_ids": []})
        self.assertEqual(wrong.status_code, 200)
        body = wrong.json()["attempt"]
        self.assertEqual(body["score"], 0.0)
        self.assertFalse(body["perfect"])
        self.assertTrue(body["result"]["missed"])
        wrong_list = self.client.get("/api/training/my/practice/wrong").json()["attempts"]
        self.assertIn(attempt["id"], [a["id"] for a in wrong_list])
        # 错题再练：按 rule_id 开新题
        redo = self._start(rule_id=body["rule_id"])
        self.assertEqual(redo.status_code, 200)
        self.assertNotEqual(redo.json()["attempt"]["id"], attempt["id"])

    def test_practice_submit_unknown_rule_keeps_attempt_open(self):
        self._staff_login()
        self._link()
        self._student_login()
        attempt = self._start().json()["attempt"]
        bad = self.client.post(f"/api/training/my/practice/{attempt['id']}/submit", json={
            "selected_rule_ids": ["no-such-rule"]})
        self.assertEqual(bad.status_code, 422)
        listing = self.client.get("/api/training/my/practice?status=open").json()["attempts"]
        self.assertIn(attempt["id"], [a["id"] for a in listing])  # 作答保留，可重交

    def test_start_without_links_rejected(self):
        self._student_login()
        res = self.client.post("/api/training/my/practice/start", json={
            "certificate_id": self.cert2})
        self.assertEqual(res.status_code, 422)
        self.assertIn("关联规则", res.json()["detail"])

    def test_start_kp_scope_requires_linked_rule(self):
        self._staff_login()
        self._link()
        self._student_login()
        other = [r.id for r in self.rules if r.id != self.rule_id][0]
        res = self._start(rule_id=other)
        self.assertEqual(res.status_code, 422)
        # 错题重练例外：练过的规则即使取消关联也可再练
        self.assertEqual(self._start(rule_id=self.rule_id).status_code, 200)

    def test_coverage_counts(self):
        self._staff_login()
        self._link()
        self._student_login()
        first = self._start(seed=7).json()["attempt"]
        result = self.client.post(f"/api/training/my/practice/{first['id']}/submit", json={
            "selected_rule_ids": [self.rule_id]}).json()["attempt"]["result"]
        if not result["perfect"]:
            redo = self._start(seed=7, rule_id=self.rule_id).json()["attempt"]
            self.client.post(f"/api/training/my/practice/{redo['id']}/submit", json={
                "selected_rule_ids": result["standard_answer"]})
        cov = self.client.get(f"/api/training/my/coverage?certificate_id={self.cert}"
                              ).json()["coverage"]
        row = next(k for k in cov if k["code"] == "KP-01")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["perfect"], 1)
        self.assertEqual(row["rule_links"], 1)


if __name__ == "__main__":
    unittest.main()
