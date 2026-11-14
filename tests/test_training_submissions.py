"""提交版本与订正：闸门、快照、草稿、退回与状态推导。"""
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp.storage import Store


class SubmissionVersionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "0",
                                          "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / "versions.db", keepalive=True)
        module.store = self.store
        self.password = "Version-kd73mzqp-5521!"
        self.teacher = self.store.create_user("ver-teacher", self.password, "任课教师", "teacher", "school-v")
        self.colleague = self.store.create_user("ver-colleague", self.password, "同事", "teacher", "school-v")
        self.student = self.store.create_user("ver-student", self.password, "学生甲", "student", "school-v")
        self.other = self.store.create_user("ver-other", self.password, "同校他班", "student", "school-v")
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        self.case = self.client.post("/api/exercises", json={
            "rule_id": "R-001", "seed": 63, "expected_version": "2.0"}).json()
        self.class_id = self.client.post("/api/classes", json={
            "name": "会计一班", "student_ids": [self.student["id"]]}).json()["id"]
        self.future = datetime(2032, 1, 1, tzinfo=UTC).isoformat()
        self.aid = self.client.post("/api/assignments", json={
            "title": "订正作业", "audit_id": self.case["audit_id"], "published": True,
            "class_id": self.class_id, "deadline_at": self.future}).json()["id"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    def login(self, who):
        self.client.cookies.clear()
        response = self.client.post("/api/login", json={"username": who["username"], "password": self.password})
        self.assertEqual(response.status_code, 200, response.text)

    def submit(self):
        return self.client.post(f"/api/assignments/{self.aid}/submit", json={"selected_rule_ids": []})

    def progress(self):
        response = self.client.get(f"/api/assignments/{self.aid}/progress")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def versions(self, student_id=None):
        path = f"/api/assignments/{self.aid}/versions"
        if student_id:
            path += f"?student_id={student_id}"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def review(self, score=88, feedback="复核意见"):
        subs = self.store.list_submissions(self.teacher, self.aid)
        self.store.review_submission(subs[0]["id"], self.teacher["id"], score, feedback, actor=self.teacher)

    def give_back(self, reason="口径写错了，请按任务要求重算", student_id=None):
        return self.client.post(f"/api/assignments/{self.aid}/return", json={
            "student_id": student_id or self.student["id"], "reason": reason})

    def test_repeat_submit_is_refused_until_teacher_returns_it(self):
        self.login(self.student)
        self.assertEqual(self.submit().status_code, 200)
        second = self.submit()
        self.assertEqual(second.status_code, 409, second.text)
        self.assertIn("退回", second.json()["detail"])
        self.assertEqual(self.progress()["attempts"], 1)
        self.assertEqual(len(self.store.list_submissions(self.teacher, self.aid)), 1)

    def test_return_then_correct_keeps_previous_score_in_versions(self):
        self.client.put(f"/api/training/tasks/assignment/{self.aid}/settings",
                        json={"answer_release": "after_submit"})
        self.login(self.student)
        self.assertEqual(self.submit().status_code, 200)
        self.login(self.teacher)
        self.review(88, "第一次的口径写错了")
        self.assertEqual(self.give_back().status_code, 200)

        self.login(self.student)
        state = self.progress()
        self.assertEqual(state["status"], "returned")
        self.assertIn("口径写错", state["return_reason"])
        self.assertEqual(self.submit().status_code, 200)

        history = self.versions()["items"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["version"], 1)
        self.assertEqual(history[0]["adjusted_score"], 88.0)
        self.assertEqual(history[0]["feedback"], "第一次的口径写错了")
        self.assertEqual(history[0]["kind"], "submit")
        # 当前这次是新的提交，复核结果为待复核
        current = self.store.get_submission(self.aid, self.student["id"])
        self.assertIsNone(current["adjusted_score"])
        self.assertEqual(self.progress()["status"], "pending_review")
        self.assertEqual(self.progress()["corrections"], 1)

    def test_correction_limit_is_enforced(self):
        self.client.put(f"/api/training/tasks/assignment/{self.aid}/settings",
                        json={"submit_limit": 1, "correction_limit": 1, "answer_release": "after_due"})
        self.login(self.student)
        self.submit()
        self.login(self.teacher)
        self.give_back()
        self.login(self.student)
        self.assertEqual(self.submit().status_code, 200)
        self.login(self.teacher)
        self.give_back()
        self.login(self.student)
        exhausted = self.submit()
        self.assertEqual(exhausted.status_code, 409, exhausted.text)
        self.assertIn("订正次数已用尽", exhausted.json()["detail"])

    def test_zero_correction_limit_blocks_first_correction(self):
        self.client.put(f"/api/training/tasks/assignment/{self.aid}/settings",
                        json={"submit_limit": 1, "correction_limit": 0, "answer_release": "after_due"})
        self.login(self.student)
        self.submit()
        self.login(self.teacher)
        self.give_back()
        self.login(self.student)
        self.assertEqual(self.submit().status_code, 409)

    def test_draft_round_trip_and_cleared_after_submit(self):
        self.login(self.student)
        saved = self.client.put(f"/api/assignments/{self.aid}/draft", json={"answers": ["R-001"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["saved_at"])
        loaded = self.client.get(f"/api/assignments/{self.aid}/draft").json()["draft"]
        self.assertEqual(loaded["answers"], ["R-001"])
        self.assertEqual(self.progress()["status"], "in_progress")
        self.submit()
        self.assertIsNone(self.client.get(f"/api/assignments/{self.aid}/draft").json()["draft"])

    def test_draft_is_refused_after_deadline(self):
        self.client.put(f"/api/assignments/{self.aid}/settings",
                        json={"published": True, "revision": 1,
                              "deadline_at": datetime(2020, 1, 1, tzinfo=UTC).isoformat()})
        # 截止时间已过，发布时被拒；改用直接写库模拟截止后场景
        with self.store.connect() as db:
            db.execute("UPDATE training_assignment_settings SET deadline_at=? WHERE assignment_id=?",
                       (datetime(2020, 1, 1, tzinfo=UTC).isoformat(), self.aid))
        self.login(self.student)
        response = self.client.put(f"/api/assignments/{self.aid}/draft", json={"answers": []})
        self.assertEqual(response.status_code, 409, response.text)

    def test_paper_question_draft_is_kept_without_counting_as_submitted(self):
        """组卷每题都是一道独立作业，草稿按题存；不写 submissions 就不算已交。"""
        self.login(self.teacher)
        paper = self.client.post("/api/papers", json={
            "title": "综合风险卷", "class_id": self.class_id, "deadline_at": self.future,
            "published": True,
            "items": [{"audit_id": self.case["audit_id"], "points": 100}]}).json()
        question = self.client.get("/api/papers/" + paper["id"]).json()["items"][0]["id"]
        self.login(self.student)
        saved = self.client.put(f"/api/assignments/{question}/draft", json={"answers": ["R-001"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        loaded = self.client.get(f"/api/assignments/{question}/draft").json()["draft"]
        self.assertEqual(loaded["answers"], ["R-001"])
        progress = self.client.get("/api/papers/" + paper["id"]).json()["progress"]
        self.assertEqual((progress["completed"], progress["score"]), (0, None))

    def test_return_requires_reason_and_an_existing_submission(self):
        self.login(self.teacher)
        # 没有提交记录：先判「不能退回」，与原因无关
        not_submitted = self.give_back()
        self.assertEqual(not_submitted.status_code, 409)
        self.assertIn("还没有提交", not_submitted.json()["detail"])
        self.login(self.student)
        self.submit()
        self.login(self.teacher)
        blank = self.client.post(f"/api/assignments/{self.aid}/return", json={
            "student_id": self.student["id"], "reason": "   "})
        self.assertEqual(blank.status_code, 422, blank.text)
        self.assertIn("原因", blank.json()["detail"])
        self.assertEqual(self.give_back("口径写错了").status_code, 200)

    def test_versions_are_scoped_to_owner_class_and_school(self):
        self.login(self.student)
        self.submit()
        self.login(self.teacher)
        self.give_back()
        self.login(self.student)
        self.submit()
        self.login(self.teacher)
        self.assertEqual(len(self.versions(self.student["id"])["items"]), 1)
        self.assertEqual(self.versions()["students"], [self.student["id"]])
        self.login(self.colleague)
        self.assertEqual(self.client.get(f"/api/assignments/{self.aid}/versions").status_code, 404)
        self.login(self.other)
        self.assertEqual(self.client.get(f"/api/assignments/{self.aid}/versions").status_code, 404)
        self.assertEqual(self.client.get(f"/api/assignments/{self.aid}/progress").status_code, 404)

    def test_progress_is_not_shared_between_students(self):
        self.login(self.student)
        self.submit()
        task = next(t for t in self.client.get("/api/training/tasks").json()["items"] if t["id"] == self.aid)
        self.assertEqual(task["my_status"], "pending_review")
        self.login(self.teacher)
        self.review(0, "已复核为零分")
        self.login(self.student)
        self.assertEqual(self.progress()["status"], "done")
        task = next(t for t in self.client.get("/api/training/tasks").json()["items"] if t["id"] == self.aid)
        self.assertEqual(task["my_status"], "done")
        self.assertEqual(task["mine_reviewed"], 1)
        self.login(self.teacher)
        self.give_back()
        self.login(self.student)
        task = next(t for t in self.client.get("/api/training/tasks").json()["items"] if t["id"] == self.aid)
        self.assertEqual(task["my_status"], "returned", "A review score must not hide a subsequent return")
        self.login(self.other)
        self.assertEqual(self.client.get(f"/api/assignments/{self.aid}/progress").status_code, 404)


if __name__ == "__main__":
    unittest.main()
