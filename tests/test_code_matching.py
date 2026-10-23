"""判题比对模式测试（FR-J04 v3.1：exact / float 浮点容差 / any_of 多解判定）。

覆盖：compare_output 纯函数（容差边界、非数值逐字符、多解候选、fail-closed）、
题库 API 用例配置校验（非法模式/容差越界/候选缺失）、端到端判题
（浮点输出 6 位小数通过 1e-4 容差、超容差判错、多解命中）。
LoginGuard 惯例：setUp 换全新实例、tearDown 还原。
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
from webapp import code_sandbox
from webapp.login_guard import LoginGuard
from webapp.storage import Store
from webapp.training_portal import register

_passwords = PasswordHasher()
NOW = "2026-10-18T12:00:00"
COLLEGE = "示例大学"
STUDENT_COOKIE = "test_matching_student"
STAFF_COOKIE = "test_matching_staff"


class CompareOutputTests(unittest.TestCase):
    def test_exact_unchanged(self):
        self.assertTrue(code_sandbox.compare_output("a\nb\n", "a\nb"))
        self.assertFalse(code_sandbox.compare_output("a\nb\n", "a\nc"))
        # 尾随空白与末尾空行仍归一
        self.assertTrue(code_sandbox.compare_output("a  \nb\n\n", "a\nb"))

    def test_float_tolerance(self):
        self.assertTrue(code_sandbox.compare_output(
            "3.14159265\n", "3.14159267", mode="float", tolerance=1e-6))
        self.assertFalse(code_sandbox.compare_output(
            "3.14159265\n", "3.2", mode="float", tolerance=1e-6))
        # 相对容差锚定期望值：大数值放宽、小数值收紧（1e-5 误差 > 1e-6 容差）
        self.assertTrue(code_sandbox.compare_output(
            "1000000.1\n", "1000000.0", mode="float", tolerance=1e-6))
        self.assertFalse(code_sandbox.compare_output(
            "0.00001\n", "0.0", mode="float", tolerance=1e-6))
        # 科学计数法与非数值 token
        self.assertTrue(code_sandbox.compare_output(
            "1.0000001e-3 x\n", "0.001 x", mode="float", tolerance=1e-4))
        self.assertFalse(code_sandbox.compare_output(
            "3.14 y\n", "3.14 x", mode="float", tolerance=1e-6))  # 非数值须逐字符
        # token 数不一致不通过
        self.assertFalse(code_sandbox.compare_output(
            "1.0 2.0\n", "1.0", mode="float", tolerance=1e-6))

    def test_any_of_alternatives(self):
        self.assertTrue(code_sandbox.compare_output(
            "yes\n", "no", mode="any_of", alternatives=["yes", "maybe"]))
        self.assertTrue(code_sandbox.compare_output(
            "3.14\n", "3.1415", mode="any_of", tolerance=1e-3,
            alternatives=["2.71"]))  # 期望本身也参与比对
        self.assertFalse(code_sandbox.compare_output(
            "other\n", "no", mode="any_of", alternatives=["yes", "maybe"]))
        # exact 模式忽略候选（fail-closed 由题库配置层拒绝，引擎层兜底不误判）
        self.assertFalse(code_sandbox.compare_output("yes\n", "no", mode="exact",
                                                     alternatives=["yes"]))

    def test_fail_closed_on_bad_config(self):
        self.assertFalse(code_sandbox.compare_output("x\n", "x", mode="regex"))
        self.assertFalse(code_sandbox.compare_output("x\n", "x", mode="float",
                                                     tolerance=2.0))
        self.assertFalse(code_sandbox.compare_output("x\n", "x", mode="float",
                                                     tolerance=0.0))


class MatchingAPITests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "match.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        import webapp.training_portal as tp
        self._orig = [(tp, "login_guard", tp.login_guard),
                      (tp, "staff_login_guard", tp.staff_login_guard),
                      (tp, "_CODE_SUBMISSION_RATE_SECONDS", tp._CODE_SUBMISSION_RATE_SECONDS)]
        tp.login_guard = LoginGuard()
        tp.staff_login_guard = LoginGuard()
        tp._CODE_SUBMISSION_RATE_SECONDS = 0  # 连续提交场景不受 30s 限频干扰
        self._seed()

    def tearDown(self):
        for module, name, obj in self._orig:
            setattr(module, name, obj)
        self._tmp.cleanup()

    def _seed(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, "admin1", _passwords.hash("staff-pass"), "管理员",
                 COLLEGE, None, None, None, "admin", NOW))
            db.execute(
                "INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, COLLEGE, "2026001", "学生甲", "会计2601",
                 _passwords.hash("Stu2026x!"), None, NOW))

    def _student_login(self):
        res = self.client.post("/api/training/auth/login",
                               json={"student_no": "2026001",
                                     "password": "Stu2026x!", "college": COLLEGE})
        self.assertEqual(res.status_code, 200, res.text)

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _create_problem(self, cases):
        self._staff_login()
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "匹配模式题", "time_limit_ms": 5000,
            "cases": cases})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["problem"]["id"]

    def _submit(self, pid, code):
        self._student_login()
        res = self.client.post(f"/api/training/my/code/{pid}/submit",
                               json={"code_text": code})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["result"]

    def test_case_config_validation(self):
        self._staff_login()
        # 非法 match_mode
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "t", "cases": [
                {"input_text": "", "expected_output": "1", "match_mode": "regex"}]})
        self.assertEqual(res.status_code, 422, res.text)
        # exact 模式带候选 → 配置错误（TrainingPortalError → 422）
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "t", "cases": [
                {"expected_output": "1", "alternatives": ["2"]}]}
        )
        self.assertEqual(res.status_code, 422, res.text)
        # any_of 无候选 → 配置错误
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "t", "cases": [
                {"expected_output": "1", "match_mode": "any_of"}]})
        self.assertEqual(res.status_code, 422, res.text)
        # 容差越界 → pydantic 422
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "t", "cases": [
                {"expected_output": "1", "match_mode": "float",
                 "float_tolerance": 2.0}]})
        self.assertEqual(res.status_code, 422, res.text)

    def test_float_mode_end_to_end(self):
        pid = self._create_problem([
            {"input_text": "1 2\n", "expected_output": "3.0\n",
             "match_mode": "float", "float_tolerance": 1e-4},
            {"input_text": "2 3\n", "expected_output": "5\n"},  # exact 混排
        ])
        ok = self._submit(pid, "a, b = map(float, input().split())\nprint(a + b)\n")
        # 用例 1（float 容差）：3.0 vs 3.0 通过；用例 2（exact）：5.0 vs "5" 不通过
        self.assertEqual(ok["status"], "wrong_answer", ok["detail"])
        self.assertEqual(ok["total_score"], 50.0)
        self.assertTrue(ok["detail"][0]["passed"])
        self.assertFalse(ok["detail"][1]["passed"])

    def test_any_of_mode_end_to_end(self):
        # 限频按（同题+同学生）30 秒计：每个候选各建一题，逐一验证多解命中
        for answer in ("odd\n", "奇数\n", "jishu\n"):
            pid = self._create_problem([
                {"input_text": "1\n", "expected_output": "odd\n", "match_mode": "any_of",
                 "alternatives": ["奇数", "jishu"]},
            ])
            result = self._submit(pid, f"print('{answer.strip()}')\n")
            self.assertEqual(result["status"], "accepted", result["detail"])

    def test_float_beyond_tolerance_wrong(self):
        pid = self._create_problem([
            {"input_text": "1 2\n", "expected_output": "3.0\n",
             "match_mode": "float", "float_tolerance": 1e-6}])
        result = self._submit(pid, "print(3.1)\n")
        self.assertEqual(result["status"], "wrong_answer")


if __name__ == "__main__":
    unittest.main()
