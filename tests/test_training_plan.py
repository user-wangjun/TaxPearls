"""工作台完整版学习安排（FR-K 剩余项）与掌握程度直显测试。

覆盖：掌握程度分布汇总、plan 优先级（续作 > 错题复核 > 巩固中补练 >
常考未练 > 补覆盖 > 超期复习）、跳过不计入、节奏提示与题量缺口、
计划长度上限、不展示未经验证的通过概率。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import register, today_cst

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_plan_cookie"
NOW = "2026-10-10T23:00:00"
STALE = "2026-09-28T10:00:00"  # 距 NOW 超过 7 天


class StudyPlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "pl.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        self.student_id = uuid.uuid4().hex
        self.cert = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (self.student_id, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("pass-a"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (self.cert, "cjkj", "初级会计职称", "", "[]", "", NOW, NOW))
            self.kps = {}
            for i in range(1, 6):
                kp_id = uuid.uuid4().hex
                db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                           " VALUES (?,?,?,?,?,1,?,?)",
                           (kp_id, self.cert, f"KP-{i:02d}", f"知识点{i}", "", NOW, NOW))
                self.kps[f"KP-{i:02d}"] = kp_id
            # KP-01~04 各关联一条规则题目；KP-05 无题目（题量缺口）
            for code, rule in (("KP-01", "rule-1"), ("KP-02", "rule-2"),
                               ("KP-03", "rule-3"), ("KP-04", "rule-4")):
                db.execute("INSERT INTO knowledge_point_link (id,knowledge_point_id,target_type,target_id,created_by,created_at)"
                           " VALUES (?,?,?,?,?,?)",
                           (uuid.uuid4().hex, self.kps[code], "rule", rule, None, NOW))
            db.execute("INSERT INTO knowledge_point_mark (id,knowledge_point_id,mark_type,level,basis_ref,basis_version,created_by,created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.kps["KP-03"], "high_freq", "high", "考纲第三章", "2026大纲", None, NOW))
            self.exam_date = (today_cst() + timedelta(days=100)).isoformat()
            self.date_id = uuid.uuid4().hex
            db.execute("INSERT INTO exam_date (id,certificate_id,round_label,date_type,exam_date,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?,?)",
                       (self.date_id, self.cert, "2027年第一批", "official", self.exam_date, NOW, NOW))
            db.execute("INSERT INTO student_goal (id,student_id,certificate_id,planned_date,official_date_id,status,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.student_id, self.cert, None, self.date_id, "active", NOW, NOW))

    def _attempt(self, kp_id: str | None, *, perfect: bool, mode: str = "new",
                 skipped: bool = False, scored_at: str = NOW, status: str | None = None) -> None:
        attempt_id = uuid.uuid4().hex
        with self.store.connect() as db:
            if skipped:
                db.execute("""INSERT INTO training_self_practice_attempts
                           (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level,
                           year, digest, status, mode, created_at, skipped_at)
                           VALUES (?,?,?,?,?,?, 'normal', 2026, ?, 'open', ?, ?, ?)""",
                           (attempt_id, self.student_id, self.cert, kp_id, "rule-1", 1, "d", mode, NOW, NOW))
                return
            if status == "open":
                db.execute("""INSERT INTO training_self_practice_attempts
                           (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level,
                           year, digest, status, mode, created_at)
                           VALUES (?,?,?,?,?,?, 'normal', 2026, ?, 'open', ?, ?)""",
                           (attempt_id, self.student_id, self.cert, kp_id, "rule-1", 1, "d", mode, NOW))
                return
            result = json.dumps({"score": 100.0 if perfect else 60.0, "perfect": perfect,
                                 "missed": [], "false_positives": [] if perfect else [{"name": "误选"}]})
            db.execute("""INSERT INTO training_self_practice_attempts
                       (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level,
                       year, digest, status, result_json, mode, created_at, scored_at)
                       VALUES (?,?,?,?,?,?, 'normal', 2026, ?, 'scored', ?, ?, ?, ?)""",
                       (attempt_id, self.student_id, self.cert, kp_id, "rule-1", 1, "d",
                        db.field_codec.seal(result, "training_self_practice_attempts",
                                            "result_json", attempt_id),
                        mode, NOW, scored_at))

    def _dashboard(self):
        self._login()
        res = self.client.get("/api/training/my/dashboard")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["goals"][0]

    def _login(self):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "pass-a", "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    # ---- 掌握程度直显与覆盖口径 ---------------------------------------------

    def test_mastery_summary_and_skip_excluded(self):
        for _ in range(3):
            self._attempt(self.kps["KP-01"], perfect=True)
        self._attempt(self.kps["KP-02"], perfect=True)
        self._attempt(self.kps["KP-01"], perfect=True, skipped=True)
        goal = self._dashboard()
        self.assertEqual(goal["mastery"], {"unstarted": 3, "acquainted": 1,
                                           "consolidating": 0, "proficient": 1})
        self.assertEqual(goal["coverage"]["attempts"], 4)  # 跳过不计入
        self.assertEqual(goal["coverage"]["skipped"], 1)
        self.assertEqual(goal["coverage"]["covered"], 2)

    # ---- 计划优先级 ---------------------------------------------------------

    def test_plan_priority_resume_then_redo_then_consolidating(self):
        self._attempt(self.kps["KP-01"], perfect=False, status="open")  # 续作
        for _ in range(2):
            self._attempt(self.kps["KP-01"], perfect=False)  # 未满分 → 错题复核
        self._attempt(self.kps["KP-02"], perfect=True)
        self._attempt(self.kps["KP-02"], perfect=True)
        self._attempt(self.kps["KP-02"], perfect=False)  # 3 次 2 满分 → 巩固中
        plan = self._dashboard()["plan"]
        kinds = [t["kind"] for t in plan]
        self.assertEqual(kinds[0], "resume")
        self.assertEqual(kinds[1], "redo")
        self.assertEqual(plan[1]["knowledge_point_id"], self.kps["KP-01"])
        self.assertEqual(plan[1]["rule_id"], "rule-1")
        self.assertEqual(kinds[2], "practice")  # 巩固中补练
        self.assertEqual(plan[2]["knowledge_point_id"], self.kps["KP-02"])
        self.assertIn("巩固", plan[2]["title"])
        kinds_rest = kinds[3:]
        self.assertEqual(kinds_rest.count("practice"), 2)  # 常考 KP-03 + 未练 KP-04
        self.assertIn("常考", plan[3]["title"])

    def test_hot_untried_before_plain_untried(self):
        plan = self._dashboard()["plan"]
        titles = [t["title"] for t in plan]
        self.assertIn("新学常考「知识点3」", titles)
        self.assertIn("新学「知识点4」", titles)
        self.assertLess(titles.index("新学常考「知识点3」"), titles.index("新学「知识点4」"))
        # KP-05 无关联题目：不进计划，只进缺口说明
        self.assertTrue(all("知识点5" not in t for t in titles))
        self.assertEqual(self._dashboard()["gaps"]["kps_without_questions"], 1)

    def test_stale_proficient_gets_review_task(self):
        for i in range(3):
            self._attempt(self.kps["KP-01"], perfect=True,
                          scored_at=f"2026-09-2{i + 1}T10:00:00")  # 最近一次距今 > 7 天
        plan = self._dashboard()["plan"]
        review = [t for t in plan if t["kind"] == "review"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["knowledge_point_id"], self.kps["KP-01"])
        self.assertIn("7 天", review[0]["reason"])

    def test_fresh_proficient_not_recommended_for_review(self):
        for _ in range(3):
            self._attempt(self.kps["KP-01"], perfect=True)  # scored_at=NOW
        plan = self._dashboard()["plan"]
        self.assertEqual([t for t in plan if t["kind"] == "review"], [])

    # ---- 节奏提示、缺口与边界 -------------------------------------------------

    def test_pacing_hint_deterministic_and_no_probability(self):
        goal = self._dashboard()
        pacing = goal["pacing"]
        self.assertEqual(pacing["remaining_kps"], 5)
        self.assertEqual(pacing["days_left"], goal["countdown"]["days_left"])
        self.assertEqual(pacing["per_day"], -(-5 // pacing["days_left"]))
        self.assertIn("每天", pacing["note"])
        raw = json.dumps(self.client.get("/api/training/my/dashboard").json(),
                         ensure_ascii=False)
        self.assertNotIn("通过率", raw)
        self.assertNotIn("probability", raw.lower())

    def test_plan_capped_at_eight_tasks(self):
        # 全部 5 个知识点都制造任务：KP-01 续作+错题、KP-02 巩固、KP-03/04 新学、KP-01 复习
        self._attempt(self.kps["KP-01"], perfect=False, status="open")
        self._attempt(self.kps["KP-01"], perfect=False)
        for i in range(3):
            self._attempt(self.kps["KP-02"], perfect=(i < 2))
        plan = self._dashboard()["plan"]
        self.assertLessEqual(len(plan), 8)

    def test_no_goal_no_dashboard_entry(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM student_goal")
        self._login()
        res = self.client.get("/api/training/my/dashboard")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["goals"], [])


if __name__ == "__main__":
    unittest.main()
