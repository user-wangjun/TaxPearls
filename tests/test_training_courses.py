"""课程管理测试（FR-U04 v1：课程绑定教师与班级，支持同课多班）。

覆盖：staff 鉴权门禁、课程 CRUD（创建/编辑/停用启用）、同院校同学期重名
拒绝、教师引用校验、班级绑定全量替换、花名册按绑定班级聚合在读学生
（院校隔离+同课多班去重）、教师列表端点。
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
STUDENT_COOKIE = "test_course_cookie"
NOW = "2026-10-13T22:30:00"
COLLEGE = "示例大学"
OTHER_COLLEGE = "另一所大学"


class CourseManagementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "course.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, "admin1", _passwords.hash("staff-pass"), "管理员",
                 COLLEGE, None, None, None, "admin", NOW))
            db.execute(
                "INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, "teacher1", _passwords.hash("tea-pass"), "王老师",
                 COLLEGE, None, None, None, "teacher", NOW))
            for i, (no, cls, college) in enumerate([
                    ("2026001", "会计2601", COLLEGE), ("2026002", "会计2601", COLLEGE),
                    ("2026003", "会计2602", COLLEGE), ("2026900", "会计2601", OTHER_COLLEGE)]):
                db.execute(
                    "INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                    (uuid.uuid4().hex, college, no, f"学生{i}", cls,
                     _passwords.hash("pass"), None, NOW))

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _teacher_id(self):
        rows = self.client.get("/api/training/staff/teachers").json()["teachers"]
        return [t for t in rows if t["username"] == "teacher1"][0]["id"]

    # ---- 鉴权门禁 ----

    def test_courses_require_staff_auth(self):
        self.assertEqual(self.client.get("/api/training/staff/courses").status_code, 401)
        self.assertEqual(self.client.post("/api/training/staff/courses", json={
            "name": "课", "college": COLLEGE}).status_code, 401)

    def test_teacher_list_endpoint(self):
        self._staff_login()
        rows = self.client.get("/api/training/staff/teachers").json()["teachers"]
        names = {r["username"] for r in rows}
        self.assertIn("admin1", names)
        self.assertIn("teacher1", names)

    # ---- 创建 ----

    def test_create_course_with_teacher_and_classes(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/courses", json={
            "name": "税务风险审计实训", "college": COLLEGE, "semester": "2026-2027-1",
            "teacher_id": self._teacher_id(),
            "class_names": ["会计2601", "会计2602"], "description": "考证班"})
        self.assertEqual(res.status_code, 200, res.text)
        course = res.json()["course"]
        self.assertEqual(course["classes"], ["会计2601", "会计2602"])
        self.assertEqual(course["student_count"], 3)  # 同校两班在读 3 人（跨校同班名不计）
        self.assertEqual(course["teacher_name"], "王老师")

    def test_create_duplicate_same_semester_rejected(self):
        self._staff_login()
        body = {"name": "审计基础", "college": COLLEGE, "semester": "2026-2027-1",
                "class_names": ["会计2601"]}
        self.assertEqual(self.client.post("/api/training/staff/courses", json=body).status_code, 200)
        dup = self.client.post("/api/training/staff/courses", json=body)
        self.assertEqual(dup.status_code, 422, dup.text)
        self.assertIn("已存在", dup.json()["detail"])

    def test_create_same_name_different_semester_allowed(self):
        self._staff_login()
        base = {"name": "审计基础", "college": COLLEGE, "class_names": ["会计2601"]}
        self.assertEqual(self.client.post("/api/training/staff/courses", json={
            **base, "semester": "2026-2027-1"}).status_code, 200)
        self.assertEqual(self.client.post("/api/training/staff/courses", json={
            **base, "semester": "2026-2027-2"}).status_code, 200)

    def test_create_with_unknown_teacher_rejected(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/courses", json={
            "name": "税法", "college": COLLEGE, "teacher_id": "no-such-id"})
        self.assertEqual(res.status_code, 404, res.text)

    def test_create_requires_name(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/courses", json={
            "name": "  ", "college": COLLEGE})
        self.assertEqual(res.status_code, 422, res.text)

    # ---- 编辑与停用 ----

    def test_update_course_fields_and_class_rebind(self):
        self._staff_login()
        cid = self.client.post("/api/training/staff/courses", json={
            "name": "会计信息化", "college": COLLEGE,
            "class_names": ["会计2601"]}).json()["course"]["id"]
        res = self.client.patch(f"/api/training/staff/courses/{cid}", json={
            "semester": "2026-2027-2", "class_names": ["会计2602"], "description": "改说明"})
        self.assertEqual(res.status_code, 200, res.text)
        course = res.json()["course"]
        self.assertEqual(course["semester"], "2026-2027-2")
        self.assertEqual(course["classes"], ["会计2602"])
        self.assertEqual(course["student_count"], 1)
        self.assertEqual(course["description"], "改说明")

    def test_toggle_course_active(self):
        self._staff_login()
        cid = self.client.post("/api/training/staff/courses", json={
            "name": "经济法", "college": COLLEGE}).json()["course"]["id"]
        off = self.client.patch(f"/api/training/staff/courses/{cid}", json={"active": False})
        self.assertEqual(off.json()["course"]["active"], 0)
        listing = self.client.get("/api/training/staff/courses").json()["courses"]
        self.assertTrue(any(c["id"] == cid for c in listing))  # include_inactive 默认 True
        filtered = self.client.get(
            "/api/training/staff/courses?include_inactive=false").json()["courses"]
        self.assertFalse(any(c["id"] == cid for c in filtered))

    def test_update_unknown_course_404(self):
        self._staff_login()
        res = self.client.patch("/api/training/staff/courses/no-such", json={"name": "x"})
        self.assertEqual(res.status_code, 404, res.text)

    # ---- 花名册 ----

    def test_course_roster_aggregates_by_class_with_college_isolation(self):
        self._staff_login()
        cid = self.client.post("/api/training/staff/courses", json={
            "name": "成本会计", "college": COLLEGE,
            "class_names": ["会计2601", "会计2602"]}).json()["course"]["id"]
        data = self.client.get(f"/api/training/staff/courses/{cid}/roster").json()
        nos = [s["student_no"] for s in data["students"]]
        self.assertEqual(data["count"], 3)
        self.assertIn("2026001", nos)
        self.assertNotIn("2026900", nos)  # 其他院校同班名不进花名册

    def test_course_roster_unknown_404(self):
        self._staff_login()
        self.assertEqual(
            self.client.get("/api/training/staff/courses/no-such/roster").status_code, 404)


if __name__ == "__main__":
    unittest.main()
