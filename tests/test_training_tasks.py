"""课程任务主链：统一清单、策略旁挂、答案开放与越权边界。"""
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp.storage import Store


class TrainingTaskTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "0",
                                          "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / "tasks.db", keepalive=True)
        module.store = self.store
        self.password = "Tasks-qmzb28vk-4419!"
        self.teacher = self.store.create_user("task-teacher", self.password, "任课教师", "teacher", "school-a")
        self.colleague = self.store.create_user("task-colleague", self.password, "同事", "teacher", "school-a")
        self.student = self.store.create_user("task-student", self.password, "学生甲", "student", "school-a")
        self.other = self.store.create_user("task-other", self.password, "同校他班", "student", "school-a")
        self.foreign = self.store.create_user("task-foreign", self.password, "外校学生", "student", "school-b")
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        self.case = self.client.post("/api/exercises", json={
            "rule_id": "R-001", "seed": 41, "expected_version": "2.0"}).json()
        self.class_id = self.client.post("/api/classes", json={
            "name": "会计一班", "student_ids": [self.student["id"]]}).json()["id"]
        self.future = datetime(2032, 1, 1, tzinfo=UTC).isoformat()

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

    def single(self, published=True, **fields):
        body = {"title": "单案例作业", "audit_id": self.case["audit_id"], "published": published,
                "class_id": self.class_id, "deadline_at": self.future}
        body.update(fields)
        response = self.client.post("/api/assignments", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def paper(self, published=True):
        response = self.client.post("/api/papers", json={
            "title": "综合风险卷", "class_id": self.class_id, "deadline_at": self.future,
            "published": published,
            "items": [{"audit_id": self.case["audit_id"], "points": 100}]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def paper_items(self, paper_id):
        return self.client.get("/api/papers/" + paper_id).json()["items"]

    def tasks(self):
        response = self.client.get("/api/training/tasks")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def detail(self, kind, subject_id):
        return self.client.get(f"/api/training/tasks/{kind}/{subject_id}")

    def test_teacher_list_holds_single_and_paper_without_duplicating_items(self):
        single = self.single()
        paper_id = self.paper()
        self.assertEqual(self.detail("assignment", self.paper_items(paper_id)[0]["id"]).status_code, 409)
        items = self.tasks()
        self.assertEqual({(i["kind"], i["id"]) for i in items}, {("assignment", single), ("paper", paper_id)})
        by_kind = {i["kind"]: i for i in items}
        self.assertEqual(by_kind["paper"]["question_count"], 1)
        self.assertEqual(by_kind["paper"]["class_id"], self.class_id)
        self.assertEqual(by_kind["paper"]["expected"], 1)
        self.assertEqual(by_kind["assignment"]["state"], "published")
        self.assertEqual(by_kind["assignment"]["answer_release"], "after_due")

    def test_student_sees_only_published_tasks_in_own_class(self):
        single = self.single(published=False)
        self.login(self.student)
        self.assertEqual(self.detail("assignment", single).status_code, 404)
        self.assertEqual(self.tasks(), [])
        self.login(self.teacher)
        self.client.put(f"/api/assignments/{single}/settings", json={
            "revision": 1, "published": True, "deadline_at": self.future})
        self.login(self.student)
        self.assertEqual([i["id"] for i in self.tasks()], [single])
        self.login(self.other)
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.detail("assignment", single).status_code, 404)
        self.login(self.foreign)
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.detail("assignment", single).status_code, 404)

    def test_other_teacher_cannot_read_or_change_task(self):
        single = self.single()
        self.login(self.colleague)
        self.assertEqual(self.detail("assignment", single).status_code, 404)
        self.assertEqual(self.tasks(), [])
        response = self.client.put(f"/api/training/tasks/assignment/{single}/settings",
                                   json={"answer_release": "after_submit"})
        self.assertEqual(response.status_code, 404)

    def test_answer_release_after_submit_gates_the_result(self):
        single = self.single()
        response = self.client.put(f"/api/training/tasks/assignment/{single}/settings",
                                   json={"answer_release": "after_submit", "submit_limit": 1,
                                         "open_at": None})
        self.assertEqual(response.status_code, 200, response.text)
        self.login(self.student)
        task = self.detail("assignment", single).json()
        self.assertFalse(task["answers_visible"])
        self.assertTrue(task["can_submit"])
        self.assertEqual(self.client.post(f"/api/assignments/{single}/submit",
                                          json={"selected_rule_ids": []}).status_code, 200)
        task = self.detail("assignment", single).json()
        self.assertTrue(task["answers_visible"])
        self.assertFalse(task["can_submit"])

    def test_manual_release_keeps_answers_closed_until_teacher_opens(self):
        single = self.single()
        # 默认 after_due 属自动开放，手工开放应被拒绝
        self.assertEqual(self.client.post(f"/api/training/tasks/assignment/{single}/answers/release").status_code, 409)
        self.client.put(f"/api/training/tasks/assignment/{single}/settings", json={"answer_release": "manual"})
        self.login(self.student)
        self.client.post(f"/api/assignments/{single}/submit", json={"selected_rule_ids": []})
        self.assertFalse(self.detail("assignment", single).json()["answers_visible"])
        self.login(self.teacher)
        released = self.client.post(f"/api/training/tasks/assignment/{single}/answers/release")
        self.assertEqual(released.status_code, 200, released.text)
        self.login(self.student)
        self.assertTrue(self.detail("assignment", single).json()["answers_visible"])

    def test_repeat_submission_and_schedule_change_are_refused_after_work_starts(self):
        single = self.single()
        response = self.client.put(f"/api/training/tasks/assignment/{single}/settings", json={"submit_limit": 3})
        self.assertEqual(response.status_code, 409)
        self.assertIn("提交版本", response.json()["detail"])
        self.login(self.student)
        self.client.post(f"/api/assignments/{single}/submit", json={"selected_rule_ids": []})
        self.login(self.teacher)
        response = self.client.put(f"/api/training/tasks/assignment/{single}/settings",
                                   json={"answer_release": "after_submit", "submit_limit": 1,
                                         "open_at": "2031-12-01T00:00:00+00:00"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("归档", response.json()["detail"])

    def test_archive_hides_task_from_students_and_keeps_records(self):
        single = self.single()
        self.login(self.student)
        self.client.post(f"/api/assignments/{single}/submit", json={"selected_rule_ids": []})
        self.login(self.teacher)
        self.assertEqual(self.client.post(f"/api/training/tasks/assignment/{single}/archive",
                                          json={"archived": True}).status_code, 200)
        self.assertEqual(self.detail("assignment", single).json()["state"], "archived")
        self.login(self.student)
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.detail("assignment", single).status_code, 404)
        self.login(self.teacher)
        response = self.client.put(f"/api/training/tasks/assignment/{single}/settings",
                                   json={"answer_release": "after_submit"})
        self.assertEqual(response.status_code, 409)

    def test_preview_returns_teacher_only_student_view(self):
        paper_id = self.paper()
        response = self.client.get(f"/api/training/tasks/paper/{paper_id}/preview")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["answers_included"])
        self.assertTrue(body["task"]["preview"])
        self.assertEqual(len(body["task"]["questions"]), 1)
        self.login(self.student)
        self.assertEqual(self.client.get(f"/api/training/tasks/paper/{paper_id}/preview").status_code, 403)

    def test_students_cannot_mutate_settings(self):
        single = self.single()
        self.login(self.student)
        self.assertEqual(self.client.put(f"/api/training/tasks/assignment/{single}/settings",
                                         json={"answer_release": "after_submit"}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/training/tasks/assignment/{single}/archive",
                                          json={"archived": True}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
