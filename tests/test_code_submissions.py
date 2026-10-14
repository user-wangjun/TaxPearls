"""编程提交与教学级判题测试（FR-J02 提交加密存储 + J04 v1 教学级引擎）。

覆盖：学生鉴权、提交加密落库（密文形态+解密一致性）、判题（满分/部分/超时/
运行错误）、限频、教师查看提交（含代码明文）、学生历史本人隔离。
沙箱边界如实标注：教学演示级受限子进程，非容器级安全边界。
"""
from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.sensitive_storage import PREFIX
from webapp.storage import Store
from webapp.training_portal import register

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_code_submit_cookie"
NOW = "2026-10-14T21:00:00"
COLLEGE = "示例大学"


class CodeSubmitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "cs.db")
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
                (uuid.uuid4().hex, cert_id, "KP-01", "编程基础", "实训", None, "",
                 "2026大纲", "2026", NOW, NOW))
            db.execute(
                "INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, COLLEGE, "2026001", "学生甲", "会计2601",
                 _passwords.hash("Stu2026x!"), None, NOW))

    def _student_login(self):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "Stu2026x!"})
        self.assertEqual(res.status_code, 200, res.text)

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _create_problem(self, cases):
        self._staff_login()
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "两数之和", "statement": "读入 a b，输出 a+b",
            "time_limit_ms": 5000,
            "cases": cases})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["problem"]["id"]

    # ---- 提交与加密 ----

    def test_submit_stores_code_encrypted(self):
        pid = self._create_problem([
            {"input_text": "1 2\n", "expected_output": "3\n", "sample": True}])
        self._student_login()
        code = "a, b = map(int, input().split())\nprint(a + b)\n"
        res = self.client.post(f"/api/training/my/code/{pid}/submit",
                               json={"code_text": code})
        self.assertEqual(res.status_code, 200, res.text)
        result = res.json()["result"]
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["total_score"], 100.0)
        self.assertEqual(result["passed_count"], 1)
        self.assertIn("sandbox_notice", res.json())
        # 落库为密文形态（敏感存储前缀）：绕开 Store 的自动解密行工厂，
        # 用原生 sqlite3 直读磁盘文件验证持久化层不存明文
        import sqlite3 as _sq
        raw = _sq.connect(str(Path(self._tmp.name) / "cs.db"))
        row = raw.execute(
            "SELECT code_text FROM code_submission WHERE problem_id=?", (pid,)).fetchone()
        raw.close()
        self.assertTrue(row[0].startswith(PREFIX))
        self.assertNotIn("map(int", row[0])

    def test_submit_partial_and_wrong(self):
        pid = self._create_problem([
            {"input_text": "1 2\n", "expected_output": "3\n"},
            {"input_text": "5 7\n", "expected_output": "12\n"},
            {"input_text": "0 0\n", "expected_output": "0\n"},
        ])
        self._student_login()
        code = "a, b = map(int, input().split())\nprint(a + b)\n"
        res = self.client.post(f"/api/training/my/code/{pid}/submit",
                               json={"code_text": code})
        self.assertEqual(res.status_code, 200, res.text)
        result = res.json()["result"]
        self.assertEqual(result["status"], "accepted")  # 三组全对
        time.sleep(31)  # 等限频窗口
        # 只对第一组的错误代码
        res2 = self.client.post(f"/api/training/my/code/{pid}/submit",
                                json={"code_text": "print(3)\n"})
        self.assertEqual(res2.status_code, 200, res2.text)
        r2 = res2.json()["result"]
        self.assertIn(r2["status"], ("partial", "wrong_answer"))
        self.assertLess(r2["total_score"], 100.0)

    def test_submit_timeout_case(self):
        pid = self._create_problem([
            {"input_text": "", "expected_output": "done\n"}])
        self._student_login()
        res = self.client.post(f"/api/training/my/code/{pid}/submit", json={
            "code_text": "while True:\n    pass\n"})
        self.assertEqual(res.status_code, 200, res.text)
        result = res.json()["result"]
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["total_score"], 0.0)

    def test_submit_runtime_error_case(self):
        pid = self._create_problem([
            {"input_text": "", "expected_output": "x\n"}])
        self._student_login()
        res = self.client.post(f"/api/training/my/code/{pid}/submit", json={
            "code_text": "raise ValueError('boom')\n"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["result"]["status"], "runtime_error")

    # ---- 限频与鉴权 ----

    def test_submit_rate_limited(self):
        pid = self._create_problem([
            {"input_text": "", "expected_output": "ok\n"}])
        self._student_login()
        first = self.client.post(f"/api/training/my/code/{pid}/submit",
                                 json={"code_text": "print('ok')\n"})
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(f"/api/training/my/code/{pid}/submit",
                                  json={"code_text": "print('no')\n"})
        self.assertEqual(second.status_code, 422, second.text)
        self.assertIn("频繁", second.json()["detail"])

    def test_submit_requires_student_auth(self):
        pid = self._create_problem([{"input_text": "", "expected_output": "x\n"}])
        res = self.client.post(f"/api/training/my/code/{pid}/submit",
                               json={"code_text": "print(1)\n"})
        self.assertEqual(res.status_code, 401, res.text)

    def test_submit_unknown_problem_404(self):
        self._student_login()
        res = self.client.post("/api/training/my/code/no-such/submit",
                               json={"code_text": "print(1)\n"})
        self.assertEqual(res.status_code, 404, res.text)

    # ---- 教师查看与本人历史 ----

    def test_staff_view_submissions_with_code(self):
        pid = self._create_problem([{"input_text": "", "expected_output": "ok\n"}])
        self._student_login()
        self.client.post(f"/api/training/my/code/{pid}/submit",
                         json={"code_text": "print('ok')\n"})
        self._staff_login()
        res = self.client.get(f"/api/training/staff/code-problems/{pid}/submissions"
                              "?include_code=true")
        self.assertEqual(res.status_code, 200, res.text)
        rows = res.json()["submissions"]
        self.assertEqual(len(rows), 1)
        self.assertIn("print('ok')", rows[0]["code_text"])
        self.assertEqual(rows[0]["judge"]["status"], "accepted")

    def test_student_history_scoped_to_self(self):
        pid = self._create_problem([{"input_text": "", "expected_output": "ok\n"}])
        self._student_login()
        self.client.post(f"/api/training/my/code/{pid}/submit",
                         json={"code_text": "print('ok')\n"})
        res = self.client.get(f"/api/training/my/code/submissions?problem_id={pid}")
        self.assertEqual(res.status_code, 200, res.text)
        rows = res.json()["submissions"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("code_text", rows[0])  # 历史列表不带代码正文
        self.assertEqual(rows[0]["judge"]["total_score"], 100.0)


if __name__ == "__main__":
    unittest.main()
