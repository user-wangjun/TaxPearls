"""课程任务主链：统一清单、策略旁挂、答案开放、冻结快照与越权边界。"""
import json
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

    def test_published_task_freezes_a_version_and_stays_idempotent(self):
        single = self.single()
        first = self.detail("assignment", single).json()["frozen"]
        self.assertIsNotNone(first)
        self.assertTrue(first["material_sha256"])
        self.assertEqual(first["answer_release"], "after_due")
        # 重复读取不新建版本：内容没变，指纹相同
        again = self.detail("assignment", single).json()["frozen"]
        self.assertEqual(first["id"], again["id"])
        with self.store.connect() as db:
            count = db.execute("SELECT COUNT(*) c FROM training_task_version WHERE subject_id=?",
                               (single,)).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_draft_task_is_not_frozen_until_published(self):
        single = self.single(published=False)
        task = self.detail("assignment", single).json()
        self.assertIsNone(task["frozen"])
        self.assertFalse(task["version_drift"]["frozen"])
        with self.store.connect() as db:
            count = db.execute("SELECT COUNT(*) c FROM training_task_version WHERE subject_id=?",
                               (single,)).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_answer_policy_change_creates_a_new_frozen_version(self):
        single = self.single()
        before = self.detail("assignment", single).json()["frozen"]
        self.client.put(f"/api/training/tasks/assignment/{single}/settings",
                        json={"answer_release": "after_submit", "submit_limit": 1, "open_at": None})
        after = self.detail("assignment", single).json()["frozen"]
        self.assertNotEqual(before["id"], after["id"])
        self.assertEqual(after["answer_release"], "after_submit")

    def test_submission_records_the_frozen_version_it_was_graded_under(self):
        single = self.single()
        frozen = self.detail("assignment", single).json()["frozen"]
        self.login(self.student)
        self.client.post(f"/api/assignments/{single}/submit", json={"selected_rule_ids": []})
        with self.store.connect() as db:
            state = db.execute("SELECT task_version_id FROM training_submission_state"
                               " WHERE assignment_id=?", (single,)).fetchone()
        self.assertEqual(state["task_version_id"], frozen["id"])

        # 之后改策略产生新版本：既有记录不跟着漂移，仍指向判分时那一版
        self.login(self.teacher)
        self.client.put(f"/api/training/tasks/assignment/{single}/settings",
                        json={"answer_release": "after_submit", "submit_limit": 1, "open_at": None})
        later = self.detail("assignment", single).json()["frozen"]
        with self.store.connect() as db:
            unchanged = db.execute("SELECT task_version_id FROM training_submission_state"
                                   " WHERE assignment_id=?", (single,)).fetchone()
        self.assertEqual(unchanged["task_version_id"], frozen["id"])

        # 退回后订正：被覆盖的那一次归档成版本 1，标的是它当初判分用的快照；
        # 新的这一次才记当下的新快照。
        self.assertEqual(self.client.post(f"/api/assignments/{single}/return", json={
            "student_id": self.student["id"], "reason": "按新口径重算"}).status_code, 200)
        self.login(self.student)
        self.client.post(f"/api/assignments/{single}/submit", json={"selected_rule_ids": []})
        with self.store.connect() as db:
            archived = db.execute("SELECT task_version_id FROM training_submission_version"
                                  " WHERE assignment_id=? AND version=1", (single,)).fetchone()
            current = db.execute("SELECT task_version_id FROM training_submission_state"
                                 " WHERE assignment_id=?", (single,)).fetchone()
        self.assertEqual(archived["task_version_id"], frozen["id"])
        self.assertEqual(current["task_version_id"], later["id"])

    def test_paper_freezes_one_version_covering_every_question(self):
        paper_id = self.paper()
        frozen = self.detail("paper", paper_id).json()["frozen"]
        self.assertIsNotNone(frozen)
        self.assertEqual(len(frozen["rules"]), 1)
        self.assertEqual(frozen["rules"][0]["assignment_id"], self.paper_items(paper_id)[0]["id"])

    def test_drift_reports_public_knowledge_update_without_rotating_task(self):
        single = self.single()
        frozen = self.detail("assignment", single).json()["frozen"]
        self.assertEqual(self.detail("assignment", single).json()["version_drift"]["changed_rules"], [])
        # 模拟公共知识更新：把快照 id 改成别的值，等价于规则版本被刷新
        with self.store.connect() as db:
            rules = json.loads(db.execute("SELECT rules_json FROM training_task_version WHERE id=?",
                                          (frozen["id"],)).fetchone()["rules_json"])
            for entry in rules:
                entry["rule_snapshot_ids"] = ["stale-snapshot"]
            db.execute("UPDATE training_task_version SET rules_json=? WHERE id=?",
                       (json.dumps(rules), frozen["id"]))
        drift = self.detail("assignment", single).json()["version_drift"]
        self.assertTrue(drift["changed_rules"])
        self.assertIn("新建任务", drift["note"])
        # 任务本身没有被自动轮换
        self.assertEqual(self.detail("assignment", single).json()["frozen"]["id"], frozen["id"])


if __name__ == "__main__":
    unittest.main()
