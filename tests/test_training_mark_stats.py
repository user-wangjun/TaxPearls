"""教师侧内容运营统计（三类标注口径报表）测试。

覆盖：按类型计数与覆盖知识点数、未标注数、high_freq 按依据版本细分、
题目关联覆盖与缺口、停用知识点不计入、认证与证书校验。
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
STUDENT_COOKIE = "test_ms_cookie"
NOW = "2026-10-10T23:30:00"


class MarkStatsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "ms.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        self.staff_id, self.cert = uuid.uuid4().hex, uuid.uuid4().hex
        self.kps = {}
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (self.staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (self.cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            for i in range(1, 4):
                kp_id = uuid.uuid4().hex
                db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                           " VALUES (?,?,?,?,?,1,?,?)",
                           (kp_id, self.cert, f"KP-{i:02d}", f"知识点{i}",
                            "初级会计实务" if i <= 2 else "经济法基础", NOW, NOW))
                self.kps[f"KP-{i:02d}"] = kp_id
            # KP-01 高频 1 条；KP-02 高频 1 条 + 风险情境 1 条；KP-03 无标注、无题目
            db.execute("INSERT INTO knowledge_point_mark VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.kps["KP-01"], "high_freq", "high",
                        "考纲第三章", "2026大纲", self.staff_id, NOW))
            db.execute("INSERT INTO knowledge_point_mark VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.kps["KP-02"], "high_freq", "high",
                        "考纲第四章", "2026大纲", self.staff_id, NOW))
            db.execute("INSERT INTO knowledge_point_mark VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.kps["KP-02"], "risk_context", "medium",
                        "存货减值情境", "", self.staff_id, NOW))
            db.execute("INSERT INTO knowledge_point_link (id,knowledge_point_id,target_type,target_id,created_by,created_at)"
                       " VALUES (?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.kps["KP-01"], "rule", "rule-1", None, NOW))

    def _login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _stats(self):
        res = self.client.get(f"/api/training/staff/certificates/{self.cert}/mark-stats")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["mark_stats"]

    def test_counts_points_and_unmarked(self):
        self._login()
        s = self._stats()
        self.assertEqual(s["knowledge_points"]["total"], 3)
        self.assertEqual(s["knowledge_points"]["with_questions"], 1)
        self.assertEqual(s["knowledge_points"]["without_questions"], 2)
        self.assertEqual(s["knowledge_points"]["by_subject"],
                         {"初级会计实务": 2, "经济法基础": 1})
        self.assertEqual(s["marks"]["high_freq"]["count"], 2)
        self.assertEqual(s["marks"]["high_freq"]["points"], 2)
        self.assertEqual(s["marks"]["high_freq"]["unmarked_points"], 1)
        self.assertEqual(s["marks"]["risk_context"],
                         {"count": 1, "points": 1, "unmarked_points": 2})
        self.assertEqual(s["marks"]["error_prone"],
                         {"count": 0, "points": 0, "unmarked_points": 3})

    def test_high_freq_grouped_by_basis_version(self):
        self._login()
        with self.store.connect() as db:
            db.execute("INSERT INTO knowledge_point_mark VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.kps["KP-03"], "high_freq", "high",
                        "考纲第五章", "2027大纲", self.staff_id, NOW))
        s = self._stats()
        self.assertEqual(s["marks"]["high_freq"]["count"], 3)
        self.assertEqual(s["marks"]["high_freq"]["by_basis_version"], {"2026大纲": 2, "2027大纲": 1})

    def test_inactive_knowledge_points_excluded(self):
        self._login()
        with self.store.connect() as db:
            db.execute("UPDATE knowledge_point SET active=0 WHERE id=?", (self.kps["KP-02"],))
        s = self._stats()
        self.assertEqual(s["knowledge_points"]["total"], 2)
        self.assertEqual(s["marks"]["high_freq"]["count"], 1)  # KP-02 的两条标注不计入
        self.assertEqual(s["marks"]["risk_context"]["count"], 0)

    def test_empty_certificate_all_zero(self):
        self._login()
        empty = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (empty, "empty", "空证书", "", "[]", "", NOW, NOW))
        s = self.client.get(f"/api/training/staff/certificates/{empty}/mark-stats").json()["mark_stats"]
        self.assertEqual(s["knowledge_points"]["total"], 0)
        self.assertEqual(s["marks"]["high_freq"]["count"], 0)

    def test_auth_and_cert_validation(self):
        self.assertEqual(self.client.get(
            f"/api/training/staff/certificates/{self.cert}/mark-stats").status_code, 401)
        self._login()
        self.assertEqual(self.client.get(
            f"/api/training/staff/certificates/{uuid.uuid4().hex}/mark-stats").status_code, 404)


if __name__ == "__main__":
    unittest.main()
