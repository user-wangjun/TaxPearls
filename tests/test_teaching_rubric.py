"""实操案例的四维评价量规（重设计方案 §2.3 / B4 后半）。

要验的四条口径：
1. 量规只挂在实操案例上，客观题成绩（adjusted_score）不受它影响；
2. 不适用（NULL）不按 0 分算，也不按满分算，分母按实际维度数算；
3. 权重改了且已有评分时不覆盖，升版本，旧分按旧口径留在旧版本上；
4. 学生侧按任务答案开放策略下发，未开放只说"教师尚未反馈"。
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp.storage import Store

NOW = "2026-10-22T10:00:00"


class TeachingRubricTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "0",
                                          "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / "rubric.db", keepalive=True)
        module.store = self.store
        self.password = "Rubric#2026test"
        self.teacher = self.store.create_user("rb-teacher", self.password, "任课教师", "teacher", "school-a")
        self.student = self.store.create_user("rb-student", self.password, "学生甲", "student", "school-a")
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        self.audit = self.client.post("/api/exercises", json={
            "rule_id": "R-001", "seed": 91, "expected_version": "2.0"}).json()["audit_id"]
        self.class_id = self.client.post("/api/classes", json={
            "name": "会计一班", "student_ids": [self.student["id"]]}).json()["id"]
        self.future = datetime(2032, 1, 1, tzinfo=UTC).isoformat()
        self.case_id = self._seed_case()
        self.task_id, self.assignment_id = self._publish()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    # ------------------------------------------------------------------ 夹具

    def login(self, who):
        self.client.cookies.clear()
        response = self.client.post("/api/login", json={"username": who["username"], "password": self.password})
        self.assertEqual(response.status_code, 200, response.text)

    def _seed_case(self) -> str:
        cert, case_id = ("rb-cert", "rb-case")
        with self.store.connect() as db:
            db.execute("INSERT INTO certificate (id,code,name,description,subjects_json,"
                       "source_ref,active,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "rbkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            db.execute(
                """INSERT INTO teaching_case
                     (id,certificate_id,unit_id,name,scenario,rule_codes_json,materials_json,
                      standard_conclusion_json,desensitization_note,status,created_by,created_at,updated_at)
                   VALUES (?,?,NULL,?,?,?,?,?,?, 'published',NULL,?,?)""",
                (case_id, cert, "进项税额核对", "某商贸企业 2026Q1 进项核对。",
                 json.dumps(["R-001"]), json.dumps([{"name": "申报表", "note": ""}]),
                 json.dumps([{"rule_code": "R-001", "expected": "hit", "note": ""}]),
                 "仿真数据。", NOW, NOW))
        return case_id

    def _publish(self, steps=None, dimensions=None) -> tuple[str, str]:
        response = self.client.post(f"/api/training/teaching/cases/{self.case_id}/publish", json={
            "class_id": self.class_id, "audit_id": self.audit, "deadline_at": self.future,
            "steps": steps or ["receive", "identify"],
            "dimensions": dimensions or ["material", "reconcile"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"], response.json()["assignment_id"]

    def score(self, entries, student_id=None, task_id=None):
        return self.client.put(
            f"/api/training/teaching/case-tasks/{task_id or self.task_id}/rubric-scores",
            json={"student_id": student_id or self.student["id"], "scores": entries})

    # ------------------------------------------------------------------ 量规定义

    def test_rubric_defaults_to_equal_weights_and_full_denominator(self):
        body = self.client.get(f"/api/training/teaching/case-tasks/{self.task_id}/rubric").json()
        self.assertEqual(body["version"], 1)
        self.assertEqual([d["key"] for d in body["dimensions"]], ["material", "reconcile"])
        self.assertEqual([d["weight"] for d in body["dimensions"]], [1.0, 1.0])
        self.assertEqual(body["max_score"], 4.0)

    def test_weight_cannot_be_set_for_a_dimension_this_task_does_not_score(self):
        response = self.client.put(f"/api/training/teaching/case-tasks/{self.task_id}/rubric", json={
            "dimensions": [{"key": "material", "weight": 1}, {"key": "trace", "weight": 2}]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("不评价", response.json()["detail"])

    def test_changing_weights_before_any_score_stays_on_version_one(self):
        first = self.client.put(f"/api/training/teaching/case-tasks/{self.task_id}/rubric", json={
            "dimensions": [{"key": "material", "weight": 1}, {"key": "reconcile", "weight": 3}],
            "note": "勾稽口径是本次重点"}).json()
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["max_score"], 8.0)
        again = self.client.get(f"/api/training/teaching/case-tasks/{self.task_id}/rubric").json()
        self.assertEqual(again["id"], first["id"], "没评分时原位更新，不攒版本")

    # ------------------------------------------------------------------ 评分

    def test_not_applicable_is_excluded_from_the_denominator(self):
        scored = self.score([{"dimension": "material", "score": 2, "comment": "要素齐全"},
                             {"dimension": "reconcile", "score": None, "comment": "本次未训练"}])
        self.assertEqual(scored.status_code, 200, scored.text)
        total = scored.json()["total"]
        # 不适用不按 0 分算，也不按满分算：分母只算"材料与要素"这一维（满分 2）
        self.assertEqual(total["raw"], 2.0)
        self.assertEqual(total["max"], 2.0)
        self.assertEqual(total["percent"], 100.0)
        self.assertEqual(total["counted"], ["material"])
        self.assertEqual(total["not_applicable"], ["reconcile"])

    def test_weighted_scores_and_comments_round_trip(self):
        self.client.put(f"/api/training/teaching/case-tasks/{self.task_id}/rubric", json={
            "dimensions": [{"key": "material", "weight": 1}, {"key": "reconcile", "weight": 3}]})
        scored = self.score([{"dimension": "material", "score": 2, "comment": "缺银行流水"},
                             {"dimension": "reconcile", "score": 1, "comment": "差异解释不足"}])
        total = scored.json()["total"]
        self.assertEqual(total["raw"], 5.0)          # 2×1 + 1×3
        self.assertEqual(total["max"], 8.0)          # 2×1 + 2×3
        self.assertEqual(total["percent"], 62.5)
        stored = self.client.get(f"/api/training/teaching/case-tasks/{self.task_id}/rubric-scores"
                                 f"?student_id={self.student['id']}").json()
        comments = {s["dimension"]: s["comment"] for s in stored["scores"]}
        self.assertEqual(comments["material"], "缺银行流水")

    def test_dimension_outside_the_task_is_refused_and_score_range_is_checked(self):
        self.assertEqual(self.score([{"dimension": "trace", "score": 1}]).status_code, 409)
        self.assertEqual(self.score([{"dimension": "material", "score": 3}]).status_code, 422)

    def test_rubric_does_not_touch_the_objective_score(self):
        self.login(self.student)
        self.client.post(f"/api/assignments/{self.assignment_id}/submit", json={"selected_rule_ids": []})
        self.login(self.teacher)
        submissions = self.client.get(f"/api/submissions?assignment_id={self.assignment_id}").json()
        self.client.put(f"/api/submissions/{submissions[0]['id']}/review",
                        json={"adjusted_score": 77, "feedback": "客观题复核"})
        self.score([{"dimension": "material", "score": 2}, {"dimension": "reconcile", "score": 0}])
        with self.store.connect() as db:
            row = db.execute("SELECT adjusted_score FROM submissions WHERE assignment_id=?",
                             (self.assignment_id,)).fetchone()
        self.assertEqual(row["adjusted_score"], 77.0, "量规分绝不写回客观题复核分")

    def test_weight_change_after_scoring_rotates_to_a_new_version(self):
        self.score([{"dimension": "material", "score": 2}, {"dimension": "reconcile", "score": 2}])
        rotated = self.client.put(f"/api/training/teaching/case-tasks/{self.task_id}/rubric", json={
            "dimensions": [{"key": "material", "weight": 3}, {"key": "reconcile", "weight": 1}]}).json()
        self.assertEqual(rotated["version"], 2)
        self.assertEqual(rotated["rotated_from"] is not None, True)
        # 新版本下这一名学生还没有分；旧分留在 v1 上，按旧口径读得到
        new_view = self.client.get(f"/api/training/teaching/case-tasks/{self.task_id}/rubric-scores"
                                   f"?student_id={self.student['id']}").json()
        self.assertIsNone(new_view["total"]["percent"])
        self.assertEqual(new_view["total"]["not_applicable"], ["material", "reconcile"])
        with self.store.connect() as db:
            old = db.execute("SELECT id FROM training_rubric WHERE task_id=? AND version=1",
                             (self.task_id,)).fetchone()
            kept = db.execute("SELECT COUNT(*) c FROM training_rubric_score WHERE rubric_id=?",
                              (old["id"],)).fetchone()["c"]
        self.assertEqual(kept, 2)

    # ------------------------------------------------------------------ 学生侧

    def test_student_result_follows_the_answer_release_policy(self):
        self.score([{"dimension": "material", "score": 2, "comment": "很好"},
                    {"dimension": "reconcile", "score": 1}])
        self.login(self.student)
        before = self.client.get(f"/api/assignments/{self.assignment_id}/rubric").json()
        self.assertFalse(before["released"])
        self.assertEqual(before["scores"], [])
        self.assertIn("尚未提交", before["note"])
        self.client.post(f"/api/assignments/{self.assignment_id}/submit", json={"selected_rule_ids": []})
        after = self.client.get(f"/api/assignments/{self.assignment_id}/rubric").json()
        # 默认 after_due 且截止在 2032 年：提交后仍未开放
        self.assertFalse(after["released"])
        self.assertEqual(after["note"], "答案与解析在截止后开放；如需提前查看，请联系任课教师调整任务设置。")
        self.login(self.teacher)
        self.client.put(f"/api/training/tasks/assignment/{self.assignment_id}/settings",
                        json={"answer_release": "after_submit", "submit_limit": 1, "open_at": None})
        self.login(self.student)
        released = self.client.get(f"/api/assignments/{self.assignment_id}/rubric").json()
        self.assertTrue(released["released"])
        self.assertEqual(released["total"]["raw"], 3.0)
        self.assertIn("不合并计算", released["note"])

    def test_student_cannot_score_and_teacher_only_sees_own_task(self):
        self.login(self.student)
        self.assertEqual(self.score([{"dimension": "material", "score": 2}]).status_code, 403)
        self.assertEqual(self.client.get(
            f"/api/training/teaching/case-tasks/{self.task_id}/rubric").status_code, 403)


if __name__ == "__main__":
    unittest.main()
