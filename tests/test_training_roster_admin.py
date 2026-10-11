"""FR-U03 第一期测试：教师批量导入（admin-only）+ 班级统计/整建制迁移/归属追溯。

覆盖：教师账号批量建号（重复/错误行带原因、不静默）、只建 teacher 角色、
非管理员 403；班级聚合统计；整建制迁移只动在读学生且逐生写追溯、
原班级为空 404、同班级拒绝；未登录 401。
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
from webapp.storage import Store
from webapp.training_portal import register

_passwords = PasswordHasher()
COOKIE_NAME = "test_roster_cookie"
NOW = "2026-10-11T21:00:00"
COLLEGE = "演示财经职业学院"


class RosterAdminTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "roster.db")
        app = FastAPI()
        register(app, lambda: self.store, COOKIE_NAME)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
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
                    ("2026003", "王五", "会计2601", 0),   # 停用：不随迁移走
                    ("2026004", "赵六", "会计2602", 1)):
                sid = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO student_info (id, college, student_no, name, class_name,"
                    " password_hash, email, active, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, COLLEGE, no, name, cls, _passwords.hash("StudentPass1!"), None, active, NOW))
                self.students[no] = sid

    def _login(self, username, password):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": username, "password": password})
        self.assertEqual(res.status_code, 200, res.text)

    # ---- 教师批量导入 -------------------------------------------------------

    def test_admin_import_teachers(self):
        self._login("admin01", "AdminPass1!")
        res = self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "NewStaff2026!",
            "rows": [
                {"username": "teacher02", "display_name": "王老师",
                 "email": "w@demo.edu"},
                {"username": "teacher01", "display_name": "重复登录名"},
                {"username": "", "display_name": "缺登录名"},
                {"username": "teacher03", "display_name": ""},
            ]})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["import"]
        self.assertEqual([c["username"] for c in data["created"]], ["teacher02"])
        self.assertEqual(len(data["skipped"]), 3)
        reasons = {s["username"]: s["reason"] for s in data["skipped"]}
        self.assertIn("登录名已存在", reasons["teacher01"])
        self.assertIn("登录名必填", reasons[""])
        self.assertIn("姓名必填", reasons["teacher03"])
        # 建号可登录且角色为 teacher，密码哈希非明文
        self._login("teacher02", "NewStaff2026!")
        me = self.client.get("/api/training/staff/auth/me").json()["staff"]
        self.assertEqual(me["role"], "teacher")
        with self.store.connect() as db:
            row = db.execute("SELECT password_hash FROM college_user WHERE username=?",
                             ("teacher02",)).fetchone()
        self.assertNotIn("NewStaff2026!", row["password_hash"])

    def test_teacher_cannot_import_teachers(self):
        self._login("teacher01", "TeacherPass1!")
        res = self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "NewStaff2026!",
            "rows": [{"username": "teacher09", "display_name": "越权"}]})
        self.assertEqual(res.status_code, 403)
        with self.store.connect() as db:
            n = db.execute("SELECT count(*) n FROM college_user WHERE username=?",
                           ("teacher09",)).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_teacher_import_short_password_422(self):
        self._login("admin01", "AdminPass1!")
        res = self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "123",
            "rows": [{"username": "teacher04", "display_name": "短密码"}]})
        self.assertEqual(res.status_code, 422)

    # ---- 班级统计与整建制迁移 -------------------------------------------------

    def test_class_listing_counts(self):
        self._login("teacher01", "TeacherPass1!")
        data = self.client.get(
            f"/api/training/staff/classes?college={COLLEGE}").json()["classes"]
        by = {c["class_name"]: c for c in data}
        self.assertEqual(by["会计2601"]["students"], 3)
        self.assertEqual(by["会计2601"]["active_students"], 2)
        self.assertEqual(by["会计2602"]["active_students"], 1)

    def test_class_migration_with_history(self):
        self._login("teacher01", "TeacherPass1!")
        res = self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "会计2601", "to_class": "会计2701"})
        self.assertEqual(res.status_code, 200, res.text)
        mig = res.json()["migration"]
        self.assertEqual(mig["migrated"], 2)  # 只动在读学生
        with self.store.connect() as db:
            moved = {r["student_no"]: r["class_name"] for r in db.execute(
                "SELECT student_no, class_name FROM student_info WHERE college=?", (COLLEGE,))}
            self.assertEqual(moved["2026001"], "会计2701")
            self.assertEqual(moved["2026002"], "会计2701")
            self.assertEqual(moved["2026003"], "会计2601")  # 停用学生保持原班级
            hist = db.execute(
                """SELECT h.student_id, h.from_class, h.to_class, h.changed_by, u.username
                   FROM roster_class_history h LEFT JOIN college_user u ON u.id=h.changed_by""").fetchall()
            self.assertEqual(len(hist), 2)
            self.assertEqual({h["to_class"] for h in hist}, {"会计2701"})
            self.assertEqual({h["username"] for h in hist}, {"teacher01"})
            self.assertEqual({h["student_id"] for h in hist},
                             {self.students["2026001"], self.students["2026002"]})
        # 追溯端点可见，带学号姓名与操作人
        his = self.client.get(
            f"/api/training/staff/class-history?college={COLLEGE}").json()["history"]
        self.assertEqual(len(his), 2)
        self.assertEqual({r["student_no"] for r in his}, {"2026001", "2026002"})
        self.assertEqual({r["changed_by_name"] for r in his}, {"现教师"})

    def test_class_migration_rejections(self):
        self._login("teacher01", "TeacherPass1!")
        # 同班级
        self.assertEqual(self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "会计2601", "to_class": "会计2601"}).status_code, 422)
        # 空班级（pydantic 422）
        self.assertEqual(self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "会计2601", "to_class": ""}).status_code, 422)
        # 不存在的班级 → 404
        self.assertEqual(self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "不存在的班", "to_class": "会计2701"}).status_code, 404)

    def test_roster_auth_required(self):
        self.assertEqual(self.client.get(
            f"/api/training/staff/classes?college={COLLEGE}").status_code, 401)
        self.assertEqual(self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "a", "to_class": "b"}).status_code, 401)
        self.assertEqual(self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "x1Pass!!",
            "rows": [{"username": "someone", "display_name": "某人"}]}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
