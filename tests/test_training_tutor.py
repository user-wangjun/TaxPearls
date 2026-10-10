"""上下文 AI 答疑（考证线）接口测试。

模型传输入口整体替换为桩：覆盖正常回答（引用校验）、调用失败降级、
未配置降级、作答归属与判分前置校验、提问校验与本人隔离。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp import training_tutor
from webapp.storage import Store
from webapp.training_portal import register
from webapp.training_tutor import register as register_tutor

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_training_cookie"
NOW = "2026-10-09T23:30:00"


class _StubSettings:
    def __init__(self, configured: bool):
        self._configured = configured
        self.effective_model = "stub-model"
        self.timeout = 5
        self.max_tokens = 512

    def problem(self):
        return not self._configured


class TrainingTutorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        register_tutor(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()
        self._orig_chat, self._orig_settings = training_tutor.chat_fn, training_tutor.AISettings
        self.addCleanup(lambda: (setattr(training_tutor, "chat_fn", self._orig_chat),
                                 setattr(training_tutor, "AISettings", self._orig_settings)))

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        student_a, student_b, cert = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        kp, attempt = uuid.uuid4().hex, uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_a, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("pass-a"), None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_b, "示例大学", "2026002", "王同学", None,
                        _passwords.hash("pass-b"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", "[]", "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,1,?,?)", (kp, cert, "KP-01", "收入确认", "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point_mark (id,knowledge_point_id,mark_type,level,basis_ref,basis_version,created_by,created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, kp, "high_freq", "high", "考纲第三章", "2026大纲", None, NOW))
            # 学生 A 的一道已判分作答（结果含标准答案）、一道作答中作答；学生 B 无记录
            result = json.dumps({"score": 60.0, "perfect": False, "standard_answer": ["rule-1"],
                                 "missed": [{"name": "收入确认时点", "explanation": "期末应确认"}]})
            db.execute("""INSERT INTO training_self_practice_attempts
                       (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level, year,
                       digest, status, result_json, mode, created_at, scored_at)
                       VALUES (?,?,?,?,?,?, 'normal', 2026, 'd', 'scored', ?, 'new', ?, ?)""",
                       (attempt, student_a, cert, kp, "rule-1", 42,
                        db.field_codec.seal(result, "training_self_practice_attempts",
                                            "result_json", attempt), NOW, NOW))
            db.execute("""INSERT INTO training_self_practice_attempts
                       (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level, year,
                       digest, status, mode, created_at)
                       VALUES (?,?,?,?,?,?, 'normal', 2026, 'd2', 'open', 'new', ?)""",
                       (uuid.uuid4().hex, student_a, cert, kp, "rule-1", 7, NOW))
        self.student_a, self.student_b, self.cert = student_a, student_b, cert
        self.kp, self.attempt = kp, attempt

    def _login(self, student_no, password):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": student_no, "password": password, "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _ask(self, **kwargs):
        body = {"certificate_id": self.cert, "question": "这个知识点常考什么？"}
        body.update(kwargs)
        return self.client.post("/api/training/my/tutor", json=body)

    def _stub(self, configured: bool):
        training_tutor.AISettings = SimpleNamespace(from_env=lambda: _StubSettings(configured))

    def test_normal_answer_with_citation_validation(self):
        self._stub(True)
        training_tutor.chat_fn = lambda cfg, messages: json.dumps({
            "answer": "该知识点常考收入确认时点，注意期末截止。",
            "citations": [f"kp:{self.kp}", "fake:node"]})
        self._login("2026001", "pass-a")
        res = self._ask(knowledge_point_id=self.kp)
        self.assertEqual(res.status_code, 200, res.text)
        msg = res.json()["message"]
        self.assertEqual(msg["model"], "stub-model")
        self.assertEqual(msg["citations"], [f"kp:{self.kp}"])  # 越界引用被过滤
        self.assertEqual(msg["degraded"], "")
        history = self.client.get("/api/training/my/tutor/history").json()["messages"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["question"], "这个知识点常考什么？")

    def test_degrades_on_model_failure_without_blocking_evidence(self):
        self._stub(True)

        def boom(cfg, messages):
            from src.ai_transport import TransportError
            raise TransportError("http", 500)

        training_tutor.chat_fn = boom
        self._login("2026001", "pass-a")
        res = self._ask(attempt_id=self.attempt)
        self.assertEqual(res.status_code, 200, res.text)
        msg = res.json()["message"]
        self.assertIn("模型调用失败", msg["degraded"])
        self.assertIn("依据要点", msg["answer"])
        self.assertTrue(msg["citations"])
        # 标准答案原样返回，模型未改写
        self.assertEqual(msg["standard_answer"], ["rule-1"])

    def test_degrades_when_model_unconfigured(self):
        self._stub(False)
        self._login("2026001", "pass-a")
        res = self._ask(knowledge_point_id=self.kp)
        self.assertEqual(res.status_code, 200, res.text)
        msg = res.json()["message"]
        self.assertIn("模型未配置", msg["degraded"])
        self.assertEqual(msg["model"], "")

    def test_attempt_must_be_owned_and_scored(self):
        self._stub(True)
        training_tutor.chat_fn = lambda cfg, messages: json.dumps(
            {"answer": "ok", "citations": [f"attempt:{self.attempt}"]})
        self._login("2026001", "pass-a")
        # 他人/不存在的作答不可见
        other = self.client.post("/api/training/my/tutor", json={
            "certificate_id": self.cert, "question": "q", "attempt_id": "no-such"})
        self.assertEqual(other.status_code, 404)
        # 未判分作答不可答疑（夹具中已播种一道 open 作答）
        with self.store.connect() as db:
            open_id = db.execute(
                "SELECT id FROM training_self_practice_attempts WHERE status='open'").fetchone()["id"]
        open_res = self.client.post("/api/training/my/tutor", json={
            "certificate_id": self.cert, "question": "q", "attempt_id": open_id})
        self.assertEqual(open_res.status_code, 409)
        self.assertIn("判分", open_res.json()["detail"])

    def test_question_validation_and_history_isolation(self):
        self._stub(True)
        training_tutor.chat_fn = lambda cfg, messages: json.dumps(
            {"answer": "ok", "citations": [f"kp:{self.kp}"]})
        self._login("2026001", "pass-a")
        self.assertEqual(self._ask(question="   ").status_code, 422)
        self.assertEqual(self._ask(question="长" * 501).status_code, 422)
        self._ask(knowledge_point_id=self.kp)
        # 本人隔离：学生 B 看不到学生 A 的答疑历史
        self._login("2026002", "pass-b")
        other_history = self.client.get("/api/training/my/tutor/history").json()["messages"]
        self.assertEqual(other_history, [])


if __name__ == "__main__":
    unittest.main()
