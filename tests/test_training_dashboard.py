"""学生目标工作台（FR-K07）聚合端点测试。

覆盖：空目标可恢复状态、倒计时与覆盖统计、推荐优先级（续作 >
错题重练 > 常考未练 > 未练知识点 > 巩固最弱）、未登录拒绝。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import timedelta
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
NOW = "2026-10-09T22:20:00"


class TrainingDashboardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "td.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self.rules = engine.load_rules(RULES_DIR)
        self.rule_id = self.rules[0].id
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        staff_id, student_id, cert = (uuid.uuid4().hex for _ in range(3))
        kp1, kp2, kp3 = (uuid.uuid4().hex for _ in range(3))
        future = (today_cst() + timedelta(days=90)).isoformat()
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_id, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("study-pass"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,active,created_at,updated_at) VALUES (?,?,?,?,1,?,?)",
                       (kp1, cert, "KP-01", "收入确认", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,active,created_at,updated_at) VALUES (?,?,?,?,1,?,?)",
                       (kp2, cert, "KP-02", "费用确认", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,active,created_at,updated_at) VALUES (?,?,?,?,1,?,?)",
                       (kp3, cert, "KP-03", "无题知识点", NOW, NOW))
            db.execute("INSERT INTO knowledge_point_mark (id,knowledge_point_id,mark_type,level,basis_ref,basis_version,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, kp1, "high_freq", "high", "考纲第三章", "2027版", staff_id, NOW))
            for kp in (kp1, kp2):
                db.execute("INSERT INTO knowledge_point_link (id,knowledge_point_id,target_type,target_id,created_by,created_at) VALUES (?,?,?,?,?,?)",
                           (uuid.uuid4().hex, kp, "rule", self.rule_id, staff_id, NOW))
            date_id = uuid.uuid4().hex
            db.execute("INSERT INTO exam_date (id,certificate_id,round_label,date_type,exam_date,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (date_id, cert, "2027年第一批", "official", future, NOW, NOW))
            goal_id = uuid.uuid4().hex
            db.execute("INSERT INTO student_goal (id,student_id,certificate_id,official_date_id,status,created_at,updated_at) VALUES (?,?,?,?,'active',?,?)",
                       (goal_id, student_id, cert, date_id, NOW, NOW))
        self.staff_id, self.student_id, self.cert = staff_id, student_id, cert
        self.kp1, self.kp2, self.kp3 = kp1, kp2, kp3
        self.future = future

    def _student_login(self):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "study-pass", "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _dashboard(self):
        res = self.client.get("/api/training/my/dashboard")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def _start_and_submit(self, seed, selected):
        started = self.client.post("/api/training/my/practice/start", json={
            "certificate_id": self.cert, "knowledge_point_id": self.kp1,
            "rule_id": self.rule_id, "seed": seed}).json()["attempt"]
        return self.client.post(f"/api/training/my/practice/{started['id']}/submit", json={
            "selected_rule_ids": selected}).json()["attempt"]

    def test_dashboard_without_goal_shows_recoverable_hint(self):
        with self.store.connect() as db:
            db.execute("UPDATE student_goal SET status='archived'")
        self._student_login()
        data = self._dashboard()
        self.assertEqual(data["goals"], [])
        self.assertIn("目标", data["hint"])

    def test_dashboard_goal_countdown_coverage_and_hot_recommendation(self):
        self._student_login()
        data = self._dashboard()
        self.assertEqual(len(data["goals"]), 1)
        goal = data["goals"][0]
        self.assertEqual(goal["certificate_name"], "初级会计职称")
        self.assertEqual(goal["countdown"]["days_left"], 90)
        self.assertEqual(goal["coverage"]["total"], 3)
        self.assertEqual(goal["coverage"]["covered"], 0)
        self.assertTrue(goal["high_freq"])
        rec = goal["recommendation"]
        self.assertEqual(rec["kind"], "practice")
        self.assertEqual(rec["knowledge_point_id"], self.kp1)  # 常考优先
        self.assertIn("常考", rec["reason"])

    def test_dashboard_resume_open_attempt_first(self):
        self._student_login()
        self.client.post("/api/training/my/practice/start", json={
            "certificate_id": self.cert, "knowledge_point_id": self.kp1, "rule_id": self.rule_id})
        rec = self._dashboard()["goals"][0]["recommendation"]
        self.assertEqual(rec["kind"], "resume")
        self.assertIn("未完成", rec["reason"])

    def test_dashboard_redo_after_wrong(self):
        self._student_login()
        scored = self._start_and_submit(11, [])  # 未作答：0 分未满分
        self.assertFalse(scored["result"]["perfect"])
        rec = self._dashboard()["goals"][0]["recommendation"]
        self.assertEqual(rec["kind"], "practice")
        self.assertEqual(rec["rule_id"], self.rule_id)
        self.assertIn("未满分", rec["reason"])
        weak = self._dashboard()["goals"][0]["weak"]
        self.assertIn(self.kp1, [w["id"] for w in weak])

    def test_dashboard_moves_to_untried_after_perfect(self):
        self._student_login()
        first = self._start_and_submit(7, [self.rule_id])["result"]
        redo = self.client.post("/api/training/my/practice/start", json={
            "certificate_id": self.cert, "knowledge_point_id": self.kp1,
            "rule_id": self.rule_id, "seed": 7}).json()["attempt"]
        self.client.post(f"/api/training/my/practice/{redo['id']}/submit", json={
            "selected_rule_ids": first["standard_answer"]})
        rec = self._dashboard()["goals"][0]["recommendation"]
        self.assertEqual(rec["knowledge_point_id"], self.kp2)  # kp1 已满分 → 推荐未练的 kp2
        self.assertIn("未练", rec["reason"])

    def test_dashboard_unauthenticated(self):
        self.assertEqual(self.client.get("/api/training/my/dashboard").status_code, 401)


if __name__ == "__main__":
    unittest.main()
