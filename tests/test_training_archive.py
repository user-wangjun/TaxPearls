"""个人学习档案与知识库测试（FR-K 剩余项 v1）+ 掌握程度口径（FR-K07）。

覆盖：掌握程度四级判定（含"一次重练答对不代表长期掌握"与"最近未满分回落"）、
按知识点聚合作答/错题/笔记/提问、跳过不计入、整证随机只进总览、笔记
upsert 与本人隔离、参数校验与认证。
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
from webapp.training_portal import mastery_level, register

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_archive_cookie"
NOW = "2026-10-10T22:00:00"


class KnowledgePointArchiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "ar.db")
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
            for code, name in (("KP-01", "收入确认"), ("KP-02", "会计要素"), ("KP-03", "政府会计")):
                kp_id = uuid.uuid4().hex
                db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                           " VALUES (?,?,?,?,?,1,?,?)", (kp_id, self.cert, code, name, "", NOW, NOW))
                self.kps[code] = kp_id

    def _attempt(self, kp_id: str | None, *, perfect: bool, mode: str = "new",
                 skipped: bool = False, scored_at: str | None = NOW) -> None:
        """种一道作答；perfect=False 时 result 含 false_positive 使其非满分。"""
        attempt_id = uuid.uuid4().hex
        result = json.dumps({"score": 100.0 if perfect else 60.0, "perfect": perfect,
                             "missed": [], "false_positives": [] if perfect else [{"name": "误选"}]})
        with self.store.connect() as db:
            if skipped:
                db.execute("""INSERT INTO training_self_practice_attempts
                           (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level,
                           year, digest, status, mode, created_at, skipped_at)
                           VALUES (?,?,?,?,?,?, 'normal', 2026, ?, 'open', ?, ?, ?)""",
                           (attempt_id, self.student_a, self.cert, kp_id, "rule-1", 1, "d", mode, NOW, NOW))
                return
            db.execute("""INSERT INTO training_self_practice_attempts
                       (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level,
                       year, digest, status, result_json, mode, created_at, scored_at)
                       VALUES (?,?,?,?,?,?, 'normal', 2026, ?, 'scored', ?, ?, ?, ?)""",
                       (attempt_id, self.student_a, self.cert, kp_id, "rule-1", 1, "d",
                        db.field_codec.seal(result, "training_self_practice_attempts",
                                            "result_json", attempt_id),
                        mode, NOW, scored_at))

    def _login(self, student_no, password):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": student_no, "password": password, "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _profile(self, cert_id=None):
        return self.client.get(
            f"/api/training/my/profile?certificate_id={cert_id or self.cert}")

    # ---- 掌握程度口径（纯函数） -------------------------------------------

    def test_mastery_ladder_rules(self):
        self.assertEqual(mastery_level(0, None, None), "unstarted")
        # 一次重练答对不代表长期掌握：1-2 次作答最多"初识"
        self.assertEqual(mastery_level(1, 1.0, True), "acquainted")
        self.assertEqual(mastery_level(2, 1.0, True), "acquainted")
        self.assertEqual(mastery_level(3, 0.5, False), "consolidating")
        self.assertEqual(mastery_level(3, 0.8, True), "proficient")
        # 正确率够但最近一次未满分 → 巩固中（需复核）
        self.assertEqual(mastery_level(5, 0.8, False), "consolidating")

    # ---- 档案聚合 ---------------------------------------------------------

    def test_profile_aggregates_mastery_notes_and_tutor_counts(self):
        # KP-01：3 次满分 → 较熟练；KP-02：1 次满分 → 初识（一次重练不代表掌握）；
        # KP-02 另有一次跳过（不计入）；KP-03：未练；整证随机 1 次满分只进总览。
        for _ in range(3):
            self._attempt(self.kps["KP-01"], perfect=True)
        self._attempt(self.kps["KP-02"], perfect=True)
        self._attempt(self.kps["KP-02"], perfect=True, skipped=True)
        self._attempt(None, perfect=True)
        self._login("2026001", "pass-a")
        body = self._profile().json()["profile"]
        s = body["summary"]
        self.assertEqual(s["total_kps"], 3)
        self.assertEqual(s["covered_kps"], 2)
        self.assertEqual(s["attempts"], 5)  # KP-01 3 次 + KP-02 1 次 + 整证随机 1 次；跳过不含
        self.assertEqual(s["skipped"], 1)
        self.assertEqual(s["accuracy"], 1.0)
        items = {i["code"]: i for i in body["knowledge_points"]}
        self.assertEqual(items["KP-01"]["mastery"]["level"], "proficient")
        self.assertEqual(items["KP-01"]["mastery"]["attempts"], 3)
        self.assertEqual(items["KP-02"]["mastery"]["level"], "acquainted")
        self.assertEqual(items["KP-02"]["mastery"]["attempts"], 1)  # 跳过不计入
        self.assertEqual(items["KP-03"]["mastery"]["level"], "unstarted")
        self.assertEqual(items["KP-01"]["wrong"], 0)
        self.assertEqual(body["goal"], None)  # 未建目标也能看档案

    def test_recent_failure_blocks_proficient(self):
        # 4 次作答：前 3 次满分、最近一次未满分 → 正确率 75% → 巩固中；
        # 即使正确率达 80%（5 次 4 满分）但最近一次未满分，也不给较熟练。
        for i in range(3):
            self._attempt(self.kps["KP-01"], perfect=True, scored_at=f"2026-10-0{i + 1}T10:00:00")
        self._attempt(self.kps["KP-01"], perfect=False, scored_at="2026-10-05T10:00:00")
        self._login("2026001", "pass-a")
        items = {i["code"]: i for i in self._profile().json()["profile"]["knowledge_points"]}
        m = items["KP-01"]["mastery"]
        self.assertEqual(m["level"], "consolidating")
        self.assertEqual(m["accuracy"], 0.75)
        self.assertIs(m["last_perfect"], False)

    def test_summary_and_goal_with_goal_attached(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO student_goal (id,student_id,certificate_id,status,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.student_a, self.cert, "active", NOW, NOW))
        self._login("2026001", "pass-a")
        body = self._profile().json()["profile"]
        self.assertIsNotNone(body["goal"])
        self.assertEqual(body["goal"]["certificate"]["id"], self.cert)

    # ---- 笔记（个人知识库 v1） ---------------------------------------------

    def test_note_upsert_get_and_isolation(self):
        self._login("2026001", "pass-a")
        kp = self.kps["KP-01"]
        res = self.client.put(f"/api/training/my/knowledge-points/{kp}/note",
                              json={"content": "收入确认五步法，注意时点。"})
        self.assertEqual(res.status_code, 200, res.text)
        first = res.json()["note"]
        res = self.client.put(f"/api/training/my/knowledge-points/{kp}/note",
                              json={"content": "补充：合同履约成本结转。"})
        second = res.json()["note"]
        self.assertEqual(first["created_at"], second["created_at"])  # 同一条记录更新
        self.assertEqual(second["content"], "补充：合同履约成本结转。")
        got = self.client.get(f"/api/training/my/knowledge-points/{kp}/note").json()["note"]
        self.assertEqual(got["content"], second["content"])
        # 本人隔离：学生 B 看不到 A 的笔记
        self._login("2026002", "pass-b")
        got_b = self.client.get(f"/api/training/my/knowledge-points/{kp}/note").json()["note"]
        self.assertIsNone(got_b)

    def test_note_validation(self):
        self._login("2026001", "pass-a")
        kp = self.kps["KP-01"]
        self.assertEqual(self.client.put(f"/api/training/my/knowledge-points/{kp}/note",
                                         json={"content": "   "}).status_code, 422)
        self.assertEqual(self.client.put(f"/api/training/my/knowledge-points/{kp}/note",
                                         json={"content": "字" * 5001}).status_code, 422)
        self.assertEqual(self.client.put(
            f"/api/training/my/knowledge-points/{uuid.uuid4().hex}/note",
            json={"content": "x"}).status_code, 404)

    def test_profile_reflects_note_count(self):
        self._login("2026001", "pass-a")
        self.client.put(f"/api/training/my/knowledge-points/{self.kps['KP-03']}/note",
                        json={"content": "政府会计双分录。"})
        s = self._profile().json()["profile"]["summary"]
        self.assertEqual(s["notes"], 1)

    # ---- 边界与认证 ---------------------------------------------------------

    def test_profile_unknown_certificate_404(self):
        self._login("2026001", "pass-a")
        self.assertEqual(self._profile(uuid.uuid4().hex).status_code, 404)

    def test_auth_required(self):
        self.assertEqual(self._profile().status_code, 401)
        self.assertEqual(self.client.get(
            f"/api/training/my/knowledge-points/{self.kps['KP-01']}/note").status_code, 401)
        self.assertEqual(self.client.put(
            f"/api/training/my/knowledge-points/{self.kps['KP-01']}/note",
            json={"content": "x"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
