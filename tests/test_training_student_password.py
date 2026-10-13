"""学生自助改密（考证线学生端补齐）：旧密码验证 + 全部会话失效。

覆盖：改密成功后旧会话与并行会话一并失效、新密码可登录、旧密码被拒；
旧密码错误 401；新密码过短 422；新旧相同 422；未登录 401。
学生初始密码为默认口令（Tp#学号2026），本功能消除首登不改密的默认口令风险。
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
import webapp.training_portal as training_portal_module
from webapp.login_guard import LoginGuard
from webapp.storage import Store
from webapp.training_portal import register

_passwords = PasswordHasher()
COOKIE = "test_student_pass_cookie"
NOW = "2026-10-13T10:00:00"
COLLEGE = "示例大学"
OLD_PASS = "secret123"
NEW_PASS = "NewStudent#2026"


class StudentPasswordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "sp.db")
        # LoginGuard 是模块级全局实例：CI 全量 discover 共享同一进程与
        # TestClient IP，失败计数跨文件累积会以 429 打爆后续登录。
        # 本文件换用全新实例——既隔离外部累积，也不向后续文件泄漏记录。
        self._orig_guard = training_portal_module.login_guard
        training_portal_module.login_guard = LoginGuard()
        self.app = FastAPI()
        register(self.app, lambda: self.store, COOKIE)
        self._seed()

    def tearDown(self):
        training_portal_module.login_guard = self._orig_guard
        self._tmp.cleanup()

    def _seed(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (uuid.uuid4().hex, COLLEGE, "2026001", "李同学", None,
                        _passwords.hash(OLD_PASS), None, NOW))

    def _client(self) -> TestClient:
        return TestClient(self.app)

    def _login(self, client: TestClient, password: str = OLD_PASS):
        res = client.post("/api/training/auth/login",
                          json={"student_no": "2026001", "password": password})
        self.assertEqual(res.status_code, 200, res.text)
        return res

    def _change(self, client: TestClient, old: str, new: str):
        return client.post("/api/training/auth/change-password",
                           json={"old_password": old, "new_password": new})

    def test_change_success_revokes_all_sessions(self):
        """改密成功：并行建立的两个会话一并失效，新密码可登录、旧密码被拒。"""
        c1, c2 = self._client(), self._client()
        self._login(c1)
        self._login(c2)
        res = self._change(c1, OLD_PASS, NEW_PASS)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["password_changed"])
        # 两个旧会话都失效（含发起改密的那个）
        self.assertEqual(c1.get("/api/training/auth/me").status_code, 401)
        self.assertEqual(c2.get("/api/training/auth/me").status_code, 401)
        # 新密码可登录；旧密码被拒
        fresh = self._client()
        self._login(fresh, NEW_PASS)
        stale = self._client()
        res = stale.post("/api/training/auth/login",
                         json={"student_no": "2026001", "password": OLD_PASS})
        self.assertEqual(res.status_code, 401)

    def test_change_wrong_old_password(self):
        c = self._client()
        self._login(c)
        res = self._change(c, "wrong-old-pass", NEW_PASS)
        self.assertEqual(res.status_code, 401, res.text)
        self.assertIn("当前密码不正确", res.json()["detail"])
        # 旧会话不受失败尝试影响
        self.assertEqual(c.get("/api/training/auth/me").status_code, 200)

    def test_change_short_new_password(self):
        c = self._client()
        self._login(c)
        res = self._change(c, OLD_PASS, "short1!")
        self.assertEqual(res.status_code, 422, res.text)
        self.assertIn("至少 8 位", res.json()["detail"])

    def test_change_same_as_old(self):
        c = self._client()
        self._login(c)
        res = self._change(c, OLD_PASS, OLD_PASS)
        self.assertEqual(res.status_code, 422, res.text)
        self.assertIn("不能与当前密码相同", res.json()["detail"])

    def test_change_requires_login(self):
        res = self._change(self._client(), OLD_PASS, NEW_PASS)
        self.assertEqual(res.status_code, 401)


if __name__ == "__main__":
    unittest.main()
