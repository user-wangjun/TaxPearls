"""编程题库数据模型测试（FR-J01 v1：仅题库与管理，判题引擎/沙箱待专项）。

覆盖：staff 鉴权、题目创建（默认限制/越界拒绝/仅 python/知识点引用校验）、
用例全量替换与序号重排、编辑与停用、列表计数。
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
STUDENT_COOKIE = "test_code_problem_cookie"
NOW = "2026-10-14T15:30:00"
COLLEGE = "示例大学"


class CodeProblemTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "cp.db")
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
            cert_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                (cert_id, "cjkj", "初级会计职称", "", '["实训"]', "", NOW, NOW))
            db.execute(
                """INSERT INTO knowledge_point (id, certificate_id, code, name, subject,
                   parent_id, description, source_ref, outline_version, active, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
                (uuid.uuid4().hex, cert_id, "KP-01", "纳税申报",
                 "实训", None, "", "2026大纲", "2026", NOW, NOW))

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _kp_id(self):
        with self.store.connect() as db:
            return db.execute("SELECT id FROM knowledge_point LIMIT 1").fetchone()["id"]

    def test_requires_staff_auth(self):
        self.assertEqual(
            self.client.get("/api/training/staff/code-problems").status_code, 401)
        self.assertEqual(self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "T"}).status_code, 401)

    def test_create_with_cases(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "两数之和", "statement": "读入两个整数输出和",
            "knowledge_point_id": self._kp_id(),
            "cases": [
                {"input_text": "1 2\n", "expected_output": "3\n", "sample": True},
                {"input_text": "5 7\n", "expected_output": "12\n", "weight": 2.0},
            ]})
        self.assertEqual(res.status_code, 200, res.text)
        p = res.json()["problem"]
        self.assertEqual(p["case_count"], 2)
        self.assertEqual([c["seq"] for c in p["cases"]], [1, 2])
        self.assertEqual(p["cases"][1]["weight"], 2.0)

    def test_create_rejects_non_python_and_bad_limits(self):
        self._staff_login()
        base = {"college": COLLEGE, "title": "T"}
        self.assertEqual(self.client.post("/api/training/staff/code-problems", json={
            **base, "language": "java"}).status_code, 422)
        self.assertEqual(self.client.post("/api/training/staff/code-problems", json={
            **base, "time_limit_ms": 100}).status_code, 422)
        self.assertEqual(self.client.post("/api/training/staff/code-problems", json={
            **base, "memory_limit_kb": 8 * 1024 * 1024}).status_code, 422)

    def test_create_unknown_kp_404(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "T", "knowledge_point_id": "no-such"})
        self.assertEqual(res.status_code, 404, res.text)

    def test_update_replaces_cases_and_deactivates(self):
        self._staff_login()
        pid = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "旧题",
            "cases": [{"input_text": "a", "expected_output": "b"}]}).json()["problem"]["id"]
        res = self.client.patch(f"/api/training/staff/code-problems/{pid}", json={
            "title": "新题", "active": False,
            "cases": [{"input_text": "x", "expected_output": "y"},
                      {"input_text": "p", "expected_output": "q"}]})
        self.assertEqual(res.status_code, 200, res.text)
        p = res.json()["problem"]
        self.assertEqual(p["title"], "新题")
        self.assertEqual(p["active"], 0)
        self.assertEqual(p["case_count"], 2)
        self.assertEqual([c["seq"] for c in p["cases"]], [1, 2])  # 重排从 1 起
        listing = self.client.get(
            "/api/training/staff/code-problems?include_inactive=false").json()["problems"]
        self.assertFalse(any(x["id"] == pid for x in listing))

    def test_update_unknown_404_and_zero_weight_rejected(self):
        self._staff_login()
        self.assertEqual(
            self.client.patch("/api/training/staff/code-problems/no-such",
                              json={"title": "x"}).status_code, 404)
        pid = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "权重题"}).json()["problem"]["id"]
        res = self.client.patch(f"/api/training/staff/code-problems/{pid}", json={
            "cases": [{"input_text": "i", "expected_output": "o", "weight": 0}]})
        self.assertEqual(res.status_code, 422, res.text)


if __name__ == "__main__":
    unittest.main()
