"""FR-U01 第二期测试：学期名册快照（冻结在读归属，重拍覆盖）。

覆盖：生成快照（在读且已分班的学生入册，停用/未分班不入）；重拍覆盖
更新（班级归属变更后 reflected）、停用学生重拍后移出；查看端点按班级
分组；权限（teacher 拍 403 / 读 200 / 未登录 401）；学期不存在 404。
快照与 roster_class_history 审计链互不影响。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

import webapp.training_portal as training_portal_module
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from webapp.login_guard import LoginGuard

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import register

_passwords = PasswordHasher()
COOKIE_NAME = "test_snapshot_cookie"
NOW = "2026-10-14T15:00:00"
COLLEGE = "演示财经职业学院"


class RosterSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "snap.db")
        self._orig_guard = training_portal_module.login_guard
        training_portal_module.login_guard = LoginGuard()
        app = FastAPI()
        register(app, lambda: self.store, COOKIE_NAME)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        training_portal_module.login_guard = self._orig_guard
        self._tmp.cleanup()

    def _seed(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO college_user (id, username, password_hash, display_name, college,"
                " department, email, phone, role, active, created_at)"
                " VALUES (?,?,?,?,?,NULL,NULL,NULL,'admin',1,?)",
                (uuid.uuid4().hex, "admin01", _passwords.hash("AdminPass1!"), "管理员", COLLEGE, NOW))
            db.execute(
                "INSERT INTO college_user (id, username, password_hash, display_name, college,"
                " department, email, phone, role, active, created_at)"
                " VALUES (?,?,?,?,?,NULL,NULL,NULL,'teacher',1,?)",
                (uuid.uuid4().hex, "teacher01", _passwords.hash("TeacherPass1!"), "现教师", COLLEGE, NOW))
            self.students = {}
            for no, name, cls, active in (
                    ("2026001", "张三", "会计2601", 1), ("2026002", "李四", "会计2601", 1),
                    ("2026003", "赵六", "会计2602", 1),
                    ("2026004", "王五", None, 1),        # 未分班：不入快照
                    ("2026005", "孙六", "会计2601", 0)):  # 停用：不入快照
                sid = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO student_info (id, college, student_no, name, class_name,"
                    " password_hash, email, active, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, COLLEGE, no, name, cls, _passwords.hash("StudentPass1!"), None, active, NOW))
                self.students[no] = sid
            self.term_id = uuid.uuid4().hex
            db.execute("INSERT INTO college_term (id, college, name, start_date, end_date,"
                       " is_current, archived, created_at) VALUES (?,?,?,NULL,NULL,1,0,?)",
                       (self.term_id, COLLEGE, "2026-2027-1", NOW))

    def _login(self, username: str, password: str):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": username, "password": password})
        self.assertEqual(res.status_code, 200, res.text)

    def test_take_and_view_snapshot(self):
        self._login("admin01", "AdminPass1!")
        res = self.client.post(f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot")
        self.assertEqual(res.status_code, 200, res.text)
        snap = res.json()["snapshot"]
        self.assertEqual(snap["snapshotted"], 3)  # 3 名在读且已分班；王五未分班、孙六停用不入
        self.assertEqual(snap["inserted"], 3)
        view = self.client.get(
            f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot").json()["snapshot"]
        self.assertEqual(view["total"], 3)
        by_class = {c["class_name"]: c for c in view["classes"]}
        self.assertEqual({s["student_no"] for s in by_class["会计2601"]["students"]},
                         {"2026001", "2026002"})
        self.assertEqual({s["student_no"] for s in by_class["会计2602"]["students"]}, {"2026003"})

    def test_retake_updates_and_purges(self):
        self._login("admin01", "AdminPass1!")
        self.client.post(f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot")
        # 学期中归属变化：张三整建制转班、赵六停用
        with self.store.connect() as db:
            db.execute("UPDATE student_info SET class_name='会计2701' WHERE id=?",
                       (self.students["2026001"],))
            db.execute("UPDATE student_info SET active=0 WHERE id=?", (self.students["2026003"],))
        res = self.client.post(f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot")
        snap = res.json()["snapshot"]
        self.assertEqual(snap["snapshotted"], 2)   # 张三/李四在读；赵六停用、王五未分班均不计
        self.assertEqual(snap["updated"], 2)       # 张三（改班）、李四（不变亦更新 taken_at）
        self.assertEqual(snap["removed"], 1)       # 赵六停用 → 移出快照
        view = self.client.get(
            f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot").json()["snapshot"]
        self.assertEqual(view["total"], 2)  # 赵六已移出快照
        flat = {s["student_no"]: c["class_name"] for c in view["classes"] for s in c["students"]}
        self.assertEqual(flat["2026001"], "会计2701")
        self.assertNotIn("2026003", flat)
        # 归属审计链不受快照影响（无 roster_class_history 行产生）
        with self.store.connect() as db:
            hist = db.execute("SELECT count(*) n FROM roster_class_history").fetchone()["n"]
        self.assertEqual(hist, 0)

    def test_snapshot_permissions(self):
        self._login("teacher01", "TeacherPass1!")
        self.assertEqual(self.client.post(
            f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot").status_code, 403)
        self.assertEqual(self.client.get(
            f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot").status_code, 200)

    def test_snapshot_requires_login(self):
        self.assertEqual(self.client.post(
            f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot").status_code, 401)
        self.assertEqual(self.client.get(
            f"/api/training/staff/org/terms/{self.term_id}/roster-snapshot").status_code, 401)

    def test_snapshot_unknown_term(self):
        self._login("admin01", "AdminPass1!")
        self.assertEqual(self.client.post(
            "/api/training/staff/org/terms/nonexistent/roster-snapshot").status_code, 404)
        self.assertEqual(self.client.get(
            "/api/training/staff/org/terms/nonexistent/roster-snapshot").status_code, 404)


if __name__ == "__main__":
    unittest.main()
