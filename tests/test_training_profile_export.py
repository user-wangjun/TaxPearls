"""学生学习档案导出测试（档案导出细则 v1）。

覆盖：导出为 Markdown 快照副本（含总览数字、知识点明细表、笔记全文）、
Content-Disposition 附件下载、口径与 /profile 一致（跳过不计入）、
未知证书 404、未登录 401、本人隔离（B 导不出 A 的笔记）。
"""
from __future__ import annotations

import json
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
STUDENT_COOKIE = "test_export_cookie"
NOW = "2026-10-11T20:00:00"


class ProfileExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "ex.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        self.student_a, self.student_b = uuid.uuid4().hex, uuid.uuid4().hex
        self.cert = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (self.student_a, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("pass-a"), None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (self.student_b, "示例大学", "2026002", "王同学", None,
                        _passwords.hash("pass-b"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (self.cert, "cjkj", "初级会计职称", "", "[]", "", NOW, NOW))
            self.kps = {}
            for code, name in (("KP-01", "收入确认"), ("KP-02", "会计要素")):
                kp_id = uuid.uuid4().hex
                db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                           " VALUES (?,?,?,?,?,1,?,?)", (kp_id, self.cert, code, name, "初级会计实务", NOW, NOW))
                self.kps[code] = kp_id

    def _attempt(self, kp_id: str | None, *, perfect: bool, skipped: bool = False) -> None:
        attempt_id = uuid.uuid4().hex
        result = json.dumps({"score": 100.0 if perfect else 60.0, "perfect": perfect,
                             "missed": [], "false_positives": [] if perfect else [{"name": "误选"}]})
        with self.store.connect() as db:
            if skipped:
                db.execute("""INSERT INTO training_self_practice_attempts
                           (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level,
                           year, digest, status, mode, created_at, skipped_at)
                           VALUES (?,?,?,?,?,?, 'normal', 2026, ?, 'open', 'new', ?, ?)""",
                           (attempt_id, self.student_a, self.cert, kp_id, "rule-1", 1, "d", NOW, NOW))
                return
            db.execute("""INSERT INTO training_self_practice_attempts
                       (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level,
                       year, digest, status, result_json, mode, created_at, scored_at)
                       VALUES (?,?,?,?,?,?, 'normal', 2026, ?, 'scored', ?, 'new', ?, ?)""",
                       (attempt_id, self.student_a, self.cert, kp_id, "rule-1", 1, "d",
                        db.field_codec.seal(result, "training_self_practice_attempts",
                                            "result_json", attempt_id),
                        NOW, NOW))

    def _login(self, student_no, password):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": student_no, "password": password, "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _export(self, cert_id=None):
        return self.client.get(
            f"/api/training/my/profile/export?certificate_id={cert_id or self.cert}")

    def test_export_markdown_snapshot(self):
        for _ in range(3):
            self._attempt(self.kps["KP-01"], perfect=True)
        self._attempt(self.kps["KP-02"], perfect=True)
        self._attempt(self.kps["KP-02"], perfect=True, skipped=True)  # 不计入作答
        self._login("2026001", "pass-a")
        self.client.put(f"/api/training/my/knowledge-points/{self.kps['KP-01']}/note",
                        json={"content": "收入确认五步法，注意时点。"})
        res = self._export()
        self.assertEqual(res.status_code, 200, res.text)
        # 附件下载语义与快照口径
        self.assertIn("text/markdown", res.headers["content-type"])
        self.assertIn("attachment", res.headers.get("content-disposition", ""))
        self.assertIn("profile-", res.headers.get("content-disposition", ""))
        md = res.text
        self.assertIn("# 学习档案导出 · 初级会计职称", md)
        self.assertIn("快照副本", md)
        self.assertIn("知识点覆盖：2/2", md)
        self.assertIn("累计作答：4 次", md)  # 3 + 1；跳过不含
        self.assertIn("较熟练知识点：1 个", md)
        self.assertIn("收入确认五步法，注意时点。", md)  # 笔记全文
        self.assertIn("| KP-01 | 收入确认 |", md)
        self.assertIn("未开始", md)  # 未练知识点也如实列出
        # 快照只读：导出后再查在线档案与笔记，数据不变
        self.assertEqual(
            self.client.get(f"/api/training/my/knowledge-points/{self.kps['KP-01']}/note")
            .json()["note"]["content"], "收入确认五步法，注意时点。")

    def test_export_empty_profile(self):
        self._login("2026002", "pass-b")
        res = self._export()
        self.assertEqual(res.status_code, 200)
        md = res.text
        self.assertIn("知识点覆盖：0/2（0.0%）", md)
        self.assertIn("累计作答：0 次", md)
        self.assertNotIn("## 我的笔记", md)

    def test_export_isolation_between_students(self):
        # A 的笔记不出现在 B 的导出中（本人隔离）
        self._login("2026001", "pass-a")
        self.client.put(f"/api/training/my/knowledge-points/{self.kps['KP-01']}/note",
                        json={"content": "A 的私密笔记。"})
        self._login("2026002", "pass-b")
        self.assertNotIn("A 的私密笔记。", self._export().text)

    def test_export_unknown_certificate_404(self):
        self._login("2026001", "pass-a")
        self.assertEqual(self._export(uuid.uuid4().hex).status_code, 404)

    def test_export_auth_required(self):
        self.assertEqual(self._export().status_code, 401)

    def test_export_cell_escaping(self):
        # 名称含竖线不破坏 Markdown 表格结构
        with self.store.connect() as db:
            db.execute("UPDATE knowledge_point SET name=? WHERE id=?",
                       ("收入|确认", self.kps["KP-01"]))
        self._login("2026001", "pass-a")
        md = self._export().text
        row = [l for l in md.splitlines() if l.startswith("| KP-01 ")][0]
        self.assertIn("收入\\|确认", row)
        self.assertEqual(row.count("\\|"), 1)
        self.assertEqual(row.replace("\\|", "").count("|"), 9)  # 表格列数不因转义前竖线而变


if __name__ == "__main__":
    unittest.main()
