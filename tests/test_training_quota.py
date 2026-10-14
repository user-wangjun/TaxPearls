"""FR-U06 测试：院校席位与配额（师生分席、事务内核销、调整留痕）。

覆盖：默认档放行与展示；学生配额满员拒绝、停用腾席后再建；
教师批量导入超限逐行 skipped（不中断整批）；多线程并发建学生恰好
用满空位、绝不超限（并发核销不绕过约束——验收点）；调整 revision
乐观锁、低于当前用量拒绝、逐次留痕；权限（teacher 写 403 / 未登录 401）。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import webapp.training_portal as training_portal_module
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from webapp.login_guard import LoginGuard

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import (
    TrainingPortalError, create_student, register,
)

_passwords = PasswordHasher()
COOKIE_NAME = "test_quota_cookie"
NOW = "2026-10-13T23:00:00"
COLLEGE = "演示财经职业学院"


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "quota.db"
        self.store = Store(self.path)
        # LoginGuard 模块级全局 + CI discover 同进程：换全新实例隔离。
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
            # 在读学生 3 名（王五停用不计席位）
            for no, name, active in (("2026001", "张三", 1), ("2026002", "李四", 1),
                                     ("2026003", "王五", 0), ("2026004", "赵六", 1)):
                db.execute(
                    "INSERT INTO student_info (id, college, student_no, name, class_name,"
                    " password_hash, email, active, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, COLLEGE, no, name, "会计2601",
                     _passwords.hash("StudentPass1!"), None, active, NOW))

    def _login(self, username: str, password: str):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": username, "password": password})
        self.assertEqual(res.status_code, 200, res.text)

    def _set_quota(self, teacher: int, student: int, revision: int | None = None):
        state = self.client.get(
            f"/api/training/staff/org/quota?college={COLLEGE}").json()["quota"]
        return self.client.patch("/api/training/staff/org/quota", json={
            "college": COLLEGE, "teacher_quota": teacher, "student_quota": student,
            "revision": state["revision"] if revision is None else revision})

    def test_default_quota_allows_and_reports(self):
        self._login("teacher01", "TeacherPass1!")
        state = self.client.get(
            f"/api/training/staff/org/quota?college={COLLEGE}").json()["quota"]
        self.assertFalse(state["configured"])
        self.assertEqual(state["teacher_quota"], 10)
        self.assertEqual(state["student_quota"], 200)
        self.assertEqual(state["students_used"], 3)   # 停用不计
        self.assertEqual(state["teachers_used"], 1)   # 只数 teacher 角色，admin 不占席
        # 默认档下创建学生照常可用
        res = self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2099001", "name": "默认档"})
        self.assertEqual(res.status_code, 200, res.text)

    def test_student_quota_enforced_and_freed_by_deactivation(self):
        self._login("admin01", "AdminPass1!")
        self.assertEqual(self._set_quota(10, 3).status_code, 200)  # 在读 3 → 满
        full = self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2099001", "name": "超限生"})
        self.assertEqual(full.status_code, 409, full.text)
        self.assertIn("学生席位已满", full.json()["detail"])
        # 停用一名腾出席位后再建成功
        students = self.client.get("/api/training/staff/students").json()["students"]
        zhao = next(s for s in students if s["student_no"] == "2026004")
        self.assertEqual(self.client.post(f"/api/training/staff/students/{zhao['id']}/toggle",
                                          json={"active": False}).status_code, 200)
        freed = self.client.post("/api/training/staff/students", json={
            "college": COLLEGE, "student_no": "2099001", "name": "腾席生"})
        self.assertEqual(freed.status_code, 200, freed.text)

    def test_teacher_import_quota_skips_overflow(self):
        self._login("admin01", "AdminPass1!")
        self.assertEqual(self._set_quota(1, 200).status_code, 200)  # 在读教师 1 → 满
        res = self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "NewStaff2026!",
            "rows": [{"username": "t02", "display_name": "甲"}, {"username": "t03", "display_name": "乙"}]})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["import"]
        self.assertEqual(data["created"], [])       # 教师席满：两行全部带原因跳过
        self.assertEqual(len(data["skipped"]), 2)
        self.assertTrue(all("教师席位已满" in s["reason"] for s in data["skipped"]))

    def test_concurrent_students_do_not_exceed_quota(self):
        """并发核销不绕过：8 线程抢 2 个空位，恰好 2 成功、库内不超限。"""
        self._login("admin01", "AdminPass1!")
        self.assertEqual(self._set_quota(10, 5).status_code, 200)  # 在读 3 + 空位 2
        errors: list[str] = []

        def worker(i: int) -> str:
            try:
                each = Store(self.path)
                create_student(each, COLLEGE, f"2099{i:03d}", f"并发{i}",
                               initial_password="StudentPass1!")
                return "created"
            except TrainingPortalError as exc:
                errors.append(str(exc))
                return "rejected"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(worker, range(8)))
        self.assertEqual(outcomes.count("created"), 2, errors)
        self.assertEqual(outcomes.count("rejected"), 6)
        self.assertTrue(all("学生席位已满" in e for e in errors))
        with self.store.connect() as db:
            used = db.execute("SELECT count(*) FROM student_info WHERE college=? AND active=1",
                              (COLLEGE,)).fetchone()[0]
        self.assertEqual(used, 5)

    def test_quota_update_history_revision_and_bounds(self):
        self._login("admin01", "AdminPass1!")
        first = self._set_quota(20, 300)
        self.assertEqual(first.status_code, 200, first.text)
        state = first.json()["quota"]
        self.assertTrue(state["configured"])
        self.assertEqual(state["revision"], 1)
        # 旧 revision 409
        self.assertEqual(self.client.patch("/api/training/staff/org/quota", json={
            "college": COLLEGE, "teacher_quota": 20, "student_quota": 300,
            "revision": 0}).status_code, 409)
        # 低于当前用量 422
        self.assertEqual(self.client.patch("/api/training/staff/org/quota", json={
            "college": COLLEGE, "teacher_quota": 20, "student_quota": 2,
            "revision": state["revision"]}).status_code, 422)
        # 越界 422
        self.assertEqual(self.client.patch("/api/training/staff/org/quota", json={
            "college": COLLEGE, "teacher_quota": 0, "student_quota": 300,
            "revision": state["revision"]}).status_code, 422)
        # 第二次调整后留痕两条，记录新旧值与操作人
        self._set_quota(30, 400)
        final = self.client.get(
            f"/api/training/staff/org/quota?college={COLLEGE}").json()["quota"]
        self.assertEqual(final["revision"], 2)
        self.assertEqual(len(final["history"]), 2)
        self.assertIsNone(final["history"][-1]["old_student_quota"])   # 首次：无旧值
        self.assertEqual(final["history"][0]["old_student_quota"], 300)
        self.assertEqual(final["history"][0]["new_student_quota"], 400)
        self.assertEqual(final["history"][0]["changed_by_name"], "管理员")

    def test_quota_permissions(self):
        self._login("teacher01", "TeacherPass1!")
        self.assertEqual(self.client.get(
            f"/api/training/staff/org/quota?college={COLLEGE}").status_code, 200)
        self.assertEqual(self.client.patch("/api/training/staff/org/quota", json={
            "college": COLLEGE, "teacher_quota": 20, "student_quota": 300,
            "revision": 0}).status_code, 403)

    def test_quota_requires_login(self):
        self.assertEqual(self.client.get(
            f"/api/training/staff/org/quota?college={COLLEGE}").status_code, 401)
        self.assertEqual(self.client.patch("/api/training/staff/org/quota", json={
            "college": COLLEGE, "teacher_quota": 20, "student_quota": 300,
            "revision": 0}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
