"""考证刷题线学生端（FR-K01/K02）接口测试：证书目录、目标与倒计时。

不导入完整 webapp.app（其全局 Store 指向默认库）；用临时库 + 迷你 FastAPI
只挂 training_portal 路由，覆盖登录、目录、目标 CRUD 与倒计时验收条件。
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
from webapp.storage import Store
from webapp.training_portal import (
    TrainingPortalError, countdown, register, set_goal_status, today_cst,
)

_passwords = PasswordHasher()
COOKIE = "test_training_cookie"
NOW = "2026-10-09T20:00:00"


def _iso(day) -> str:
    return day.isoformat()


class TrainingPortalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register(app, lambda: self.store, COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    # ---- 种子数据 -------------------------------------------------------

    def _seed(self):
        teacher, student_a, student_b = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        cert, cert2 = uuid.uuid4().hex, uuid.uuid4().hex
        future = _iso(today_cst() + timedelta(days=90))
        past = _iso(today_cst() - timedelta(days=30))
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'teacher',1,?)",
                       (teacher, "t1", "x", "王老师", "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_a, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("secret123"), None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_b, "示例大学", "2026002", "张同学", None,
                        _passwords.hash("secret123"), None, NOW))
            # 同学号跨校（歧义登录用例）
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (uuid.uuid4().hex, "另一大学", "2026001", "赵同学", None,
                        _passwords.hash("secret123"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "会计专业技术资格入门证书",
                        '["初级会计实务","经济法基础"]', "依据人社部考试公告", NOW, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert2, "cns", "纳税实务证书", "仿真教学证书", '["纳税实务"]', "", NOW, NOW))
            for label, dtype, day in (("2027年第一批", "official", future),
                                      ("补考批次", "expected", future),
                                      ("2020年批次", "official", past)):
                db.execute("INSERT INTO exam_date (id,certificate_id,round_label,date_type,exam_date,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, cert, label, dtype, day, NOW, NOW))
        self.teacher, self.student_a, self.student_b = teacher, student_a, student_b
        self.cert, self.cert2, self.future, self.past = cert, cert2, future, past

    def _login(self, student_no="2026001", password="secret123", college="示例大学"):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": student_no, "password": password, "college": college})
        self.assertEqual(res.status_code, 200, res.text)
        return res

    def _goal_id(self, res):
        return res.json()["goal"]["id"]

    # ---- 登录与会话 -----------------------------------------------------

    def test_login_and_me_and_logout(self):
        res = self._login()
        self.assertEqual(res.json()["student"]["name"], "李同学")
        me = self.client.get("/api/training/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["student"]["student_no"], "2026001")
        self.client.post("/api/training/auth/logout")
        self.assertEqual(self.client.get("/api/training/auth/me").status_code, 401)

    def test_login_wrong_password_and_unauthenticated(self):
        self.assertEqual(self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "bad"}).status_code, 401)
        self.assertEqual(self.client.get("/api/training/certificates").status_code, 401)

    def test_login_ambiguous_student_no_needs_college(self):
        res = self.client.post("/api/training/auth/login",
                               json={"student_no": "2026001", "password": "secret123"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("院校", res.json()["detail"])
        ok = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "secret123", "college": "示例大学"})
        self.assertEqual(ok.status_code, 200)

    def test_login_rejects_disabled_student(self):
        with self.store.connect() as db:
            db.execute("UPDATE student_info SET active=0 WHERE student_no='2026002'")
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026002", "password": "secret123"})
        self.assertEqual(res.status_code, 401)

    # ---- 证书目录（FR-K01）---------------------------------------------

    def test_catalog_lists_subjects_and_dates(self):
        self._login()
        data = self.client.get("/api/training/certificates").json()["certificates"]
        self.assertEqual(len(data), 2)
        cert = next(c for c in data if c["code"] == "cjkj")
        self.assertEqual(cert["subjects"], ["初级会计实务", "经济法基础"])
        self.assertEqual(len(cert["exam_dates"]), 3)
        by_day = {d["exam_date"]: d for d in cert["exam_dates"]}
        self.assertEqual(by_day[self.future]["countdown"]["days_left"], 90)
        self.assertTrue(by_day[self.past]["countdown"]["expired"])

    # ---- 目标与倒计时（FR-K02）-----------------------------------------

    def test_goal_create_with_official_date(self):
        self._login()
        dates = next(c for c in self.client.get("/api/training/certificates").json()["certificates"]
                     if c["code"] == "cjkj")["exam_dates"]
        official = next(d for d in dates if d["date_type"] == "official"
                        and not d["countdown"]["expired"])
        res = self.client.post("/api/training/my/goals", json={
            "certificate_id": self.cert, "official_date_id": official["id"]})
        self.assertEqual(res.status_code, 200, res.text)
        goal = res.json()["goal"]
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["countdown_source"], "official")
        self.assertEqual(goal["countdown"]["days_left"], 90)
        self.assertFalse(goal["countdown"]["expired"])

    def test_goal_planned_only_source(self):
        self._login()
        planned = _iso(today_cst() + timedelta(days=10))
        goal = self.client.post("/api/training/my/goals", json={
            "certificate_id": self.cert, "planned_date": planned}).json()["goal"]
        self.assertEqual(goal["countdown_source"], "planned")
        self.assertEqual(goal["countdown"]["days_left"], 10)

    def test_goal_rejects_past_planned_date(self):
        self._login()
        res = self.client.post("/api/training/my/goals", json={
            "certificate_id": self.cert, "planned_date": self.past})
        self.assertEqual(res.status_code, 422)
        self.assertIn("过期", res.json()["detail"])

    def test_goal_rejects_expired_official_date(self):
        self._login()
        dates = next(c for c in self.client.get("/api/training/certificates").json()["certificates"]
                     if c["code"] == "cjkj")["exam_dates"]
        expired = next(d for d in dates if d["countdown"]["expired"])
        res = self.client.post("/api/training/my/goals", json={
            "certificate_id": self.cert, "official_date_id": expired["id"]})
        self.assertEqual(res.status_code, 422)
        self.assertIn("过期", res.json()["detail"])

    def test_goal_duplicate_active_conflict(self):
        self._login()
        first = self.client.post("/api/training/my/goals", json={"certificate_id": self.cert})
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/training/my/goals", json={"certificate_id": self.cert})
        self.assertEqual(second.status_code, 409)

    def test_goal_update_preserves_created_at(self):
        self._login()
        goal_id = self._goal_id(self.client.post("/api/training/my/goals", json={
            "certificate_id": self.cert, "planned_date": _iso(today_cst() + timedelta(days=30))}))
        created = self.client.get("/api/training/my/goals").json()["goals"][0]["created_at"]
        patched = self.client.patch(f"/api/training/my/goals/{goal_id}", json={
            "planned_date": _iso(today_cst() + timedelta(days=60))})
        self.assertEqual(patched.status_code, 200, patched.text)
        body = patched.json()["goal"]
        self.assertEqual(body["created_at"], created)
        self.assertEqual(body["planned_date"], _iso(today_cst() + timedelta(days=60)))
        self.assertEqual(body["countdown"]["days_left"], 60)

    def test_goal_status_transitions_and_resume_conflict(self):
        self._login()
        first = self._goal_id(self.client.post("/api/training/my/goals", json={"certificate_id": self.cert}))
        self.assertEqual(self.client.post(f"/api/training/my/goals/{first}/pause").status_code, 200)
        # first 已暂停 → 允许在同证书再建目标
        second = self._goal_id(self.client.post("/api/training/my/goals", json={"certificate_id": self.cert}))
        # 恢复 first 与 second 的 active 冲突 → 409
        res = self.client.post(f"/api/training/my/goals/{first}/resume")
        self.assertEqual(res.status_code, 409, res.text)
        # 归档 second 后 first 可恢复；归档目标历史保留
        self.assertEqual(self.client.post(f"/api/training/my/goals/{second}/archive").status_code, 200)
        visible = [g["id"] for g in self.client.get("/api/training/my/goals").json()["goals"]]
        self.assertNotIn(second, visible)
        all_rows = [g["id"] for g in self.client.get("/api/training/my/goals?include_archived=1").json()["goals"]]
        self.assertIn(second, all_rows)
        self.assertEqual(self.client.post(f"/api/training/my/goals/{first}/resume").status_code, 200)

    def test_goal_not_owned_hidden(self):
        self._login()
        goal_id = self._goal_id(self.client.post("/api/training/my/goals", json={"certificate_id": self.cert}))
        self.client.post("/api/training/auth/logout")
        self._login(student_no="2026002")
        mine = [g["id"] for g in self.client.get("/api/training/my/goals?include_archived=1").json()["goals"]]
        self.assertNotIn(goal_id, mine)
        res = self.client.patch(f"/api/training/my/goals/{goal_id}", json={"planned_date": None})
        self.assertEqual(res.status_code, 404)

    def test_no_path_sets_achieved(self):
        """验收：不自动判定目标达成——不存在任何 achieved 设置路径。"""
        self._login()
        goal_id = self._goal_id(self.client.post("/api/training/my/goals", json={"certificate_id": self.cert}))
        with self.store.connect() as db:
            row = db.execute("SELECT student_id FROM student_goal WHERE id=?", (goal_id,)).fetchone()
        with self.assertRaises(TrainingPortalError):
            set_goal_status(self.store, row["student_id"], goal_id, "achieved")

    def test_countdown_unit(self):
        self.assertIsNone(countdown(None))
        self.assertIsNone(countdown("not-a-date"))
        past = countdown(_iso(today_cst() - timedelta(days=3)))
        self.assertEqual(past["days_left"], 0)
        self.assertTrue(past["expired"])
        future = countdown(_iso(today_cst() + timedelta(days=1)))
        self.assertEqual(future["days_left"], 1)
        self.assertFalse(future["expired"])


if __name__ == "__main__":
    unittest.main()
