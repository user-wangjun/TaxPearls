"""教师端学生名册管理测试（小范围真实使用准备）。

覆盖：staff 认证门禁、单个添加（显式/默认密码、重复拒绝）、批量导入
（逐行跳过不中断、默认密码可登录）、重置密码（旧会话失效）、停用启用
（停用后登录拒绝且会话失效）。
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
STUDENT_COOKIE = "test_student_mgmt_cookie"
NOW = "2026-10-11T20:00:00"
COLLEGE = "示例大学"


class StaffStudentManagementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "sm.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        staff_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        COLLEGE, None, None, None, NOW))
        self.staff_id = staff_id

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    # ---- 认证门禁 -------------------------------------------------------

    def test_students_requires_staff_auth(self):
        res = self.client.get("/api/training/staff/students")
        self.assertEqual(res.status_code, 401)
        res = self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2026100", "name": "张一"})
        self.assertEqual(res.status_code, 401)

    # ---- 单个添加 -------------------------------------------------------

    def test_create_student_with_explicit_password(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2026101", "name": "张一",
            "class_name": "会计2601", "initial_password": "Abcd1234!"})
        self.assertEqual(res.status_code, 200, res.text)
        stu = res.json()["student"]
        self.assertEqual(stu["student_no"], "2026101")
        self.assertEqual(stu["class_name"], "会计2601")
        # 新密码可直接登录
        login = self.client.post("/api/training/auth/login", json={
            "student_no": "2026101", "password": "Abcd1234!", "college": COLLEGE})
        self.assertEqual(login.status_code, 200, login.text)

    def test_create_student_default_password(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2026102", "name": "李二"})
        self.assertEqual(res.status_code, 200, res.text)
        login = self.client.post("/api/training/auth/login", json={
            "student_no": "2026102", "password": "Tp#20261022026", "college": COLLEGE})
        self.assertEqual(login.status_code, 200, login.text)

    def test_create_duplicate_rejected(self):
        self._staff_login()
        body = {"college": COLLEGE, "student_no": "2026103", "name": "王三"}
        first = self.client.post("/api/training/staff/students", json=body)
        self.assertEqual(first.status_code, 200)
        dup = self.client.post("/api/training/staff/students", json=body)
        self.assertEqual(dup.status_code, 422, dup.text)
        self.assertIn("已存在", dup.json()["detail"])

    def test_create_validates_password_length(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2026104", "name": "赵四",
            "initial_password": "short"})
        self.assertEqual(res.status_code, 422, res.text)

    # ---- 批量导入 -------------------------------------------------------

    def test_import_batch_with_skips(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/students/import", json={
            "college": COLLEGE, "default_password": "Class2026!",
            "default_class_name": "会计2601",
            "rows": [
                {"student_no": "2026201", "name": "陈一"},
                {"student_no": "2026202", "name": "陈二"},
                {"student_no": "2026201", "name": "重复学号"},
            ]})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["import"]
        self.assertEqual(len(data["created"]), 2)
        self.assertEqual(len(data["skipped"]), 1)
        # 默认密码 + 默认班级生效
        login = self.client.post("/api/training/auth/login", json={
            "student_no": "2026201", "password": "Class2026!", "college": COLLEGE})
        self.assertEqual(login.status_code, 200, login.text)
        listing = self.client.get("/api/training/staff/students").json()["students"]
        row = [s for s in listing if s["student_no"] == "2026201"][0]
        self.assertEqual(row["class_name"], "会计2601")

    # ---- 重置密码 -------------------------------------------------------

    def test_reset_password_invalidates_old_session(self):
        self._staff_login()
        self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2026301", "name": "周五",
            "initial_password": "OldPass123"})
        login = self.client.post("/api/training/auth/login", json={
            "student_no": "2026301", "password": "OldPass123", "college": COLLEGE})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.get("/api/training/my/goals").status_code, 200)
        reset = self.client.post("/api/training/staff/students/2026301/reset-password",
                                 json={"new_password": "NewPass456"})
        self.assertEqual(reset.status_code, 422)  # 学号不是 id，应报学生不存在
        listing = self.client.get("/api/training/staff/students").json()["students"]
        sid = [s for s in listing if s["student_no"] == "2026301"][0]["id"]
        reset = self.client.post(f"/api/training/staff/students/{sid}/reset-password",
                                 json={"new_password": "NewPass456"})
        self.assertEqual(reset.status_code, 200, reset.text)
        # 旧会话失效
        self.assertEqual(self.client.get("/api/training/my/goals").status_code, 401)
        # 新密码可登录、旧密码不可
        old = self.client.post("/api/training/auth/login", json={
            "student_no": "2026301", "password": "OldPass123", "college": COLLEGE})
        self.assertEqual(old.status_code, 401)
        new = self.client.post("/api/training/auth/login", json={
            "student_no": "2026301", "password": "NewPass456", "college": COLLEGE})
        self.assertEqual(new.status_code, 200, new.text)

    # ---- 停用/启用 ------------------------------------------------------

    def test_toggle_deactivates_login(self):
        self._staff_login()
        self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2026401", "name": "吴六",
            "initial_password": "Pass12345"})
        listing = self.client.get("/api/training/staff/students").json()["students"]
        sid = [s for s in listing if s["student_no"] == "2026401"][0]["id"]
        off = self.client.post(f"/api/training/staff/students/{sid}/toggle",
                               json={"active": False})
        self.assertEqual(off.status_code, 200, off.text)
        self.assertEqual(off.json()["student"]["active"], 0)
        denied = self.client.post("/api/training/auth/login", json={
            "student_no": "2026401", "password": "Pass12345", "college": COLLEGE})
        self.assertEqual(denied.status_code, 401)
        on = self.client.post(f"/api/training/staff/students/{sid}/toggle",
                              json={"active": True})
        self.assertEqual(on.json()["student"]["active"], 1)
        ok = self.client.post("/api/training/auth/login", json={
            "student_no": "2026401", "password": "Pass12345", "college": COLLEGE})
        self.assertEqual(ok.status_code, 200, ok.text)


    # ---- 教师改密码 ------------------------------------------------------

    def test_staff_change_password_flow(self):
        self._staff_login()
        # 旧密码错 → 401
        bad = self.client.post("/api/training/staff/auth/change-password", json={
            "old_password": "wrong-pass", "new_password": "BrandNew88"})
        self.assertEqual(bad.status_code, 401, bad.text)
        # 新密码太短 → 422
        short = self.client.post("/api/training/staff/auth/change-password", json={
            "old_password": "staff-pass", "new_password": "short"})
        self.assertEqual(short.status_code, 422, short.text)
        # 成功 → 会话失效（me 401），新密码可登录，旧密码不可
        ok = self.client.post("/api/training/staff/auth/change-password", json={
            "old_password": "staff-pass", "new_password": "BrandNew88"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self.client.get("/api/training/staff/auth/me").status_code, 401)
        old = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(old.status_code, 401)
        new = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "BrandNew88"})
        self.assertEqual(new.status_code, 200, new.text)


if __name__ == "__main__":
    unittest.main()
