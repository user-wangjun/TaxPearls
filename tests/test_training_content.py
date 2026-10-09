"""内容版本治理（FR-K08）接口测试。

覆盖：版本创建与轮换（同证书仅一个启用版本）、真题等外部题源暂缓拒绝、
学生练习自动绑定仿真基线版本、旧作答读取当时内容版本并在轮换/退役/
年度变化时给出适用性提示、未版本化历史作答的诚实降级、重复标签冲突。
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
from src import engine
from webapp.storage import Store
from webapp.training_content import register as register_content
from webapp.training_portal import RULES_DIR, register

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_training_cookie"
STAFF_COOKIE = "taxpearls_staff_session"
NOW = "2026-10-09T22:00:00"


class TrainingContentVersionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        register_content(app, lambda: self.store)
        self.client = TestClient(app)
        self.rules = engine.load_rules(RULES_DIR)
        self.rule_id = self.rules[0].id
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        staff_id, student_id, cert, cert2 = (uuid.uuid4().hex for _ in range(4))
        kp, kp2 = uuid.uuid4().hex, uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_id, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("study-pass"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert2, "empty", "无关联证书", "", "[]", "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
                       (kp, cert, "KP-01", "收入确认", "初级会计实务", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
                       (kp2, cert2, "KP-X", "占位知识点", "", NOW, NOW))
        self.staff_id, self.student_id, self.cert, self.cert2 = staff_id, student_id, cert, cert2
        self.kp, self.kp2 = kp, kp2

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _student_login(self):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "study-pass", "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _link(self):
        return self.client.post(f"/api/training/staff/knowledge-points/{self.kp}/links", json={
            "target_type": "rule", "target_id": self.rule_id})

    def _start(self, **kwargs):
        body = {"certificate_id": self.cert, "knowledge_point_id": self.kp}
        body.update(kwargs)
        return self.client.post("/api/training/my/practice/start", json=body)

    def _create_version(self, label, year=None, source_type="simulated", source_ref="依据 2026 版考纲"):
        body = {"label": label, "source_type": source_type, "source_ref": source_ref}
        if year is not None:
            body["year"] = year
        return self.client.post(f"/api/training/staff/certificates/{self.cert}/content-versions",
                                json=body)

    # ---- 版本管理与治理规则 ----------------------------------------------

    def test_create_version_rotation_and_conflict(self):
        self._staff_login()
        res = self._create_version("2026 考纲仿真题 v1", year=2026)
        self.assertEqual(res.status_code, 200, res.text)
        v1 = res.json()["content_version"]
        self.assertEqual(v1["status"], "active")
        self.assertEqual(v1["source_type"], "simulated")
        res = self._create_version("2027 考纲仿真题 v1", year=2027)
        self.assertEqual(res.status_code, 200, res.text)
        v2 = res.json()["content_version"]
        versions = self.client.get(
            f"/api/training/staff/certificates/{self.cert}/content-versions").json()["versions"]
        by_label = {v["label"]: v for v in versions}
        self.assertEqual(by_label["2026 考纲仿真题 v1"]["status"], "retired")  # 旧版本自动退役
        self.assertEqual(by_label["2027 考纲仿真题 v1"]["status"], "active")
        dup = self._create_version("2027 考纲仿真题 v1")
        self.assertEqual(dup.status_code, 409)
        # 缺依据说明拒绝发布
        no_ref = self.client.post(
            f"/api/training/staff/certificates/{self.cert}/content-versions",
            json={"label": "无依据版本"})
        self.assertEqual(no_ref.status_code, 422)

    def test_external_question_sources_refused(self):
        self._staff_login()
        for source_type in ("real", "recall", "mock"):
            res = self._create_version(f"试_{source_type}", source_type=source_type)
            self.assertEqual(res.status_code, 422, res.text)
            self.assertIn("暂缓", res.json()["detail"])
            self.assertIn("授权", res.json()["detail"])
        versions = self.client.get(
            f"/api/training/staff/certificates/{self.cert}/content-versions").json()["versions"]
        self.assertEqual(versions, [])  # 数据模型预留但服务层不放行

    def test_practice_binds_auto_baseline_version(self):
        self._staff_login()
        self._link()
        self._student_login()
        attempt = self._start().json()["attempt"]
        self.assertEqual(attempt["status"], "open")
        version = attempt["content_version"]
        self.assertIsNotNone(version)
        self.assertEqual(version["source_type"], "simulated")
        self.assertTrue(version["label"].startswith("仿真基线"))
        # 证书目录展示当前题源版本（仿真，不冒充真题）
        certs = self.client.get("/api/training/certificates").json()["certificates"]
        mine = next(c for c in certs if c["id"] == self.cert)
        self.assertEqual(mine["content_version"]["id"], version["id"])
        # 教师侧可见该基线版本
        self._staff_login()
        versions = self.client.get(
            f"/api/training/staff/certificates/{self.cert}/content-versions").json()["versions"]
        self.assertEqual([v["id"] for v in versions], [version["id"]])

    def test_old_attempt_reads_its_version_with_year_notice(self):
        self._staff_login()
        self._link()
        self._student_login()
        scored = self.client.post(
            f"/api/training/my/practice/{self._start(seed=11).json()['attempt']['id']}/submit",
            json={"selected_rule_ids": [self.rule_id]})
        self.assertEqual(scored.status_code, 200, scored.text)
        old_version = scored.json()["attempt"]["content_version"]
        self._staff_login()
        self._create_version("2027 考纲仿真题 v1", year=2027)
        detail = self.client.get(
            f"/api/training/my/practice/{scored.json()['attempt']['id']}").json()["attempt"]
        # 旧作答读取当时内容版本，并给出年度变化适用性提示
        self.assertEqual(detail["content_version"]["id"], old_version["id"])
        self.assertEqual(detail["applicability"]["current_version"]["year"], 2027)
        self.assertIn("轮换", detail["applicability"]["notice"])
        self.assertIn("2027", detail["applicability"]["notice"])
        # 新练习绑定新版本
        new_attempt = self._start(seed=12).json()["attempt"]
        self.assertEqual(new_attempt["content_version"]["year"], 2027)
        self.assertNotEqual(new_attempt["content_version"]["id"], old_version["id"])

    def test_retired_version_notice_without_replacement(self):
        self._staff_login()
        self._link()
        self._student_login()
        scored = self.client.post(
            f"/api/training/my/practice/{self._start(seed=13).json()['attempt']['id']}/submit",
            json={"selected_rule_ids": [self.rule_id]})
        attempt_id = scored.json()["attempt"]["id"]
        version_id = scored.json()["attempt"]["content_version"]["id"]
        self._staff_login()
        res = self.client.post(f"/api/training/staff/content-versions/{version_id}/retire")
        self.assertEqual(res.status_code, 200, res.text)
        detail = self.client.get(f"/api/training/my/practice/{attempt_id}").json()["attempt"]
        self.assertEqual(detail["content_version"]["status"], "retired")
        self.assertIsNone(detail["applicability"]["current_version"])
        self.assertIn("退役", detail["applicability"]["notice"])

    def test_legacy_attempt_without_version_degrades_honestly(self):
        self._staff_login()
        self._link()
        attempt_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO training_self_practice_attempts
                   (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level,
                   year, digest, status, created_at)
                   VALUES (?,?,?,?,?,?, 'normal', 2026, 'legacy-digest', 'open', ?)""",
                (attempt_id, self.student_id, self.cert, self.kp, self.rule_id, 1, NOW))
        self._student_login()
        detail = self.client.get(f"/api/training/my/practice/{attempt_id}").json()["attempt"]
        self.assertIsNone(detail["content_version"])
        self.assertIsNone(detail["applicability"]["attempt_version"])
        self.assertIn("早于内容版本管理", detail["applicability"]["notice"])


if __name__ == "__main__":
    unittest.main()
