"""内容版本治理（FR-K08）接口测试。

覆盖：版本创建与轮换（同证书仅一个启用版本）、真题等外部题源暂缓拒绝、
学生练习自动绑定仿真基线版本、旧作答读取当时内容版本并在轮换/退役/
年度变化时给出适用性提示、未版本化历史作答的诚实降级、重复标签冲突。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from src import engine
from webapp.storage import Store
from webapp.training_content import register as register_content
from webapp import training_content as content
from webapp.training_portal import RULES_DIR, register
from webapp.training_source_auth import register as register_source_auth

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
        register_source_auth(app, lambda: self.store)
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

    def test_external_question_sources_require_authorization(self):
        self._staff_login()
        # 无授权时发布真题/回忆/模拟题版本一律拒绝（决策三：授权先行）
        for source_type in ("real", "recall", "mock"):
            res = self._create_version(f"试_{source_type}", source_type=source_type)
            self.assertEqual(res.status_code, 422, res.text)
            self.assertIn("授权", res.json()["detail"])
            self.assertIn("书面授权", res.json()["detail"])
        versions = self.client.get(
            f"/api/training/staff/certificates/{self.cert}/content-versions").json()["versions"]
        self.assertEqual(versions, [])  # 数据模型预留但服务层不放行
        # 登记生效授权后允许发布，且授权文号自动写入依据留痕
        auth = self.client.post(
            f"/api/training/staff/certificates/{self.cert}/source-authorizations",
            json={"licensor": "全国会计考办授权出版方", "auth_no": "AUTH-2026-001",
                  "question_types": ["real", "recall", "mock"], "valid_from": "2026-01-01",
                  "scope_note": "仅限校内教学使用"}).json()["authorization"]
        res = self._create_version("2026 真题精选 v1", year=2026, source_type="real",
                                   source_ref="2026 年真题整理")
        self.assertEqual(res.status_code, 200, res.text)
        version = res.json()["content_version"]
        self.assertEqual(version["source_type"], "real")
        self.assertIn("AUTH-2026-001", version["source_ref"])
        # 授权撤销后不得再发布（存量版本与作答不受影响）
        self.client.post(f"/api/training/staff/source-authorizations/{auth['id']}/revoke")
        res = self._create_version("2026 回忆题 v1", source_type="recall")
        self.assertEqual(res.status_code, 422, res.text)

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
        self.assertEqual(self._start().status_code, 409)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM training_content_version WHERE certificate_id=?',
                                       (self.cert,)).fetchone()[0], 1)

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

    def test_unrelated_rule_change_does_not_rotate_or_raise_update_notice(self):
        self._staff_login(); self._link(); self._student_login()
        attempt = self._start(seed=42).json()['attempt']
        newer = [replace(r, version='99.0') if r.id != self.rule_id else r for r in self.rules]
        with patch('webapp.training_portal._load_rules', return_value=newer):
            repeated = self._start(seed=42).json()['attempt']
            points = self.client.get(f'/api/training/my/knowledge-points?certificate_id={self.cert}').json()['knowledge_points']
        self.assertEqual(attempt['content_version']['id'], repeated['content_version']['id'])
        self.assertEqual(attempt['materials'], repeated['materials'])
        self.assertFalse(points[0]['knowledge_update']['pending'])

    def test_related_change_is_adopted_only_by_publishing(self):
        self._staff_login(); self._link(); self._student_login()
        first = self._start(seed=42).json()['attempt']
        newer = [replace(r, version='99.0', threshold_basis='测试更新口径')
                 if r.id == self.rule_id else r for r in self.rules]
        with patch('webapp.training_portal._load_rules', return_value=newer), \
                patch('webapp.training_content.load_rules', return_value=newer):
            self.assertEqual(self._start(seed=42).json()['attempt']['content_version']['id'], first['content_version']['id'])
            self._staff_login()
            versions = self.client.get(f'/api/training/staff/certificates/{self.cert}/content-versions').json()['versions']
            self.assertEqual(versions[0]['knowledge_update']['changed_rule_ids'], [self.rule_id])
            published = self._create_version('采用公共知识更新').json()['content_version']
            fresh = self._start(seed=42).json()['attempt']
            self.assertEqual(fresh['content_version']['id'], published['id'])
            self.assertNotEqual(first['content_version']['id'], fresh['content_version']['id'])
            old = self.client.post(f"/api/training/my/practice/{first['id']}/submit", json={'selected_rule_ids': [self.rule_id]})
            self.assertEqual(old.status_code, 200, old.text)

    def test_new_link_waits_for_publication_and_cannot_be_selected_explicitly(self):
        self._staff_login(); self._link(); self._student_login()
        self._start()
        added = self.rules[1].id
        self._staff_login()
        response = self.client.post(f'/api/training/staff/knowledge-points/{self.kp}/links',
                                    json={'target_type': 'rule', 'target_id': added})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._start(rule_id=added).status_code, 409)
        self._create_version('纳入新关联')
        self.assertEqual(self._start(rule_id=added).status_code, 200)

    def test_additive_knowledge_migration_does_not_rewrite_existing_version(self):
        self._staff_login(); self._link(); self._student_login()
        attempt = self._start().json()['attempt']
        with self.store.connect() as db:
            before = db.execute('SELECT rules_snapshot_json FROM training_content_version WHERE id=?',
                                (attempt['content_version']['id'],)).fetchone()[0]
            db.execute('ALTER TABLE training_content_version DROP COLUMN knowledge_snapshot_json')
        migrated = Store(self.store.path)
        with migrated.connect() as db:
            version = content.resolve_active_version(db, self.cert)
            self.assertEqual(version['rules_snapshot_json'], before)
            self.assertIsNone(version['knowledge_snapshot_json'])
            self.assertTrue(content.update_status(db, version, self.rules)['pending'])
            self.assertEqual(db.execute('SELECT count(*) FROM training_self_practice_attempts').fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
