"""AI 备课助手：生成校验、审核发布、学生作答判分与越权边界。"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp import ai_coach
from webapp.storage import Store
from src.settings import AISettings

GENERATED = {
    "knowledge_points": [
        {"name": "进项税额抵扣", "summary": "用于集体福利的进项税不得抵扣", "difficulty": "基础",
         "source_quote": "集体福利部分不能抵"},
    ],
    "questions": [
        {"kp": "进项税额抵扣", "kind": "single", "stem": "下列哪项进项税额不得抵扣？",
         "options": [{"key": "A", "text": "生产用原料"}, {"key": "B", "text": "职工食堂"}, {"key": "C", "text": "办公用品"}],
         "answers": ["B"], "explanation": "用于集体福利不得抵扣", "source_quote": ""},
        {"kp": "进项税额抵扣", "kind": "multiple", "stem": "下列哪些属于不得抵扣情形？",
         "options": [{"key": "A", "text": "集体福利"}, {"key": "B", "text": "个人消费"}, {"key": "C", "text": "生产用料"}],
         "answers": ["A", "B"], "explanation": "两类都不得抵扣", "source_quote": ""},
    ],
}

BRIEF = "这节课讲增值税一般计税方法，重点是销项税额与进项税额的确定，学生常把不得抵扣的进项税记错。"


class AiCoachGenerationTests(unittest.TestCase):
    def test_generation_uses_full_configured_output_budget_for_complete_papers(self):
        settings = AISettings(enabled=True, api_key="test-key", max_tokens=8192, timeout=90)
        for count in (5, 20):
            with self.subTest(count=count):
                generated = {**GENERATED, "questions": [GENERATED["questions"][i % 2] for i in range(count)]}
                with patch("webapp.ai_keys.apply", return_value=settings), patch(
                        "src.ai_transport.chat_content", return_value=json.dumps(generated, ensure_ascii=False)) as chat:
                    draft = ai_coach.generate_draft(None, {}, ai_coach.DraftBody(
                        brief=BRIEF, question_count=count, kinds=["single", "multiple"]))
                self.assertEqual(len(draft["questions"]), count)
                self.assertEqual(chat.call_args.kwargs["max_tokens"], settings.max_tokens)
                self.assertEqual(chat.call_args.args[2], settings.timeout)

    def test_generation_rejects_incomplete_question_count(self):
        settings = AISettings(enabled=True, api_key="test-key")
        with patch("webapp.ai_keys.apply", return_value=settings), patch(
                "src.ai_transport.chat_content", return_value=json.dumps(GENERATED, ensure_ascii=False)):
            with self.assertRaisesRegex(ai_coach.CoachError, "题"):
                ai_coach.generate_draft(None, {}, ai_coach.DraftBody(brief=BRIEF, question_count=5))


class AiCoachTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "0",
                                          "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / "ai-coach.db", keepalive=True)
        module.store = self.store
        self.password = "Coach-xkdh27fh-4193!"
        self.teacher = self.store.create_user("coach-teacher", self.password, "任课教师", "teacher", "school-a")
        self.colleague = self.store.create_user("coach-other", self.password, "同事", "teacher", "school-a")
        self.student = self.store.create_user("coach-student", self.password, "学生甲", "student", "school-a")
        self.outsider = self.store.create_user("coach-outsider", self.password, "外校学生", "student", "school-b")
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        self.class_id = self.client.post("/api/classes", json={
            "name": "会计一班", "student_ids": [self.student["id"]]}).json()["id"]

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

    def task_body(self, publish=False, **fields):
        body = {"title": "增值税进项税专项练习", "brief": BRIEF, "class_id": self.class_id,
                "knowledge_points": [{"name": "进项税额抵扣", "summary": "集体福利不得抵扣"}],
                "questions": [
                    {"kp": "进项税额抵扣", "kind": "single", "stem": "下列哪项进项税额不得抵扣？",
                     "options": [{"key": "A", "text": "生产用原料"}, {"key": "B", "text": "职工食堂"}],
                     "answers": ["B"], "explanation": "集体福利不得抵扣"},
                    {"kp": "进项税额抵扣", "kind": "multiple", "stem": "下列哪些不得抵扣？",
                     "options": [{"key": "A", "text": "集体福利"}, {"key": "B", "text": "个人消费"},
                                 {"key": "C", "text": "生产用料"}],
                     "answers": ["A", "B"], "explanation": "都属于不得抵扣情形"}],
                "publish": publish}
        body.update(fields)
        return body

    def test_unavailable_generation_directs_teacher_to_platform_admin(self):
        with patch("src.ai_transport.chat_content") as chat:
            response = self.client.post("/api/teaching/ai-tasks/draft", json={
                "brief": BRIEF, "question_count": 5, "kinds": ["single"]})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("联系平台管理员", response.json()["detail"])
        self.assertNotIn("AI 密钥", response.json()["detail"])
        self.assertNotIn("本地解析", response.json()["detail"])
        chat.assert_not_called()

    def create(self, publish=False):
        response = self.client.post("/api/teaching/ai-tasks", json=self.task_body(publish))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_generation_requires_configured_ai(self):
        response = self.client.post("/api/teaching/ai-tasks/draft",
                                    json={"brief": BRIEF, "question_count": 5, "kinds": ["single"]})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("AI", response.json()["detail"])

    def test_generation_normalizes_model_output(self):
        generated = {**GENERATED, "questions": [*GENERATED["questions"], GENERATED["questions"][0]]}
        with patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "1", "TAXPEARLS_AI_API_KEY": "test-key"}), \
                patch("src.ai_transport.chat_content", return_value=json.dumps(generated, ensure_ascii=False)):
            response = self.client.post("/api/teaching/ai-tasks/draft",
                                        json={"brief": BRIEF, "question_count": 3,
                                              "kinds": ["single", "multiple"]})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(len(data["knowledge_points"]), 1)
        self.assertEqual([q["kind"] for q in data["questions"]], ["single", "multiple", "single"])
        self.assertEqual(data["questions"][1]["answers"], ["A", "B"])
        self.assertTrue(data["title"])

    def test_generation_rejects_unsafe_output(self):
        broken = dict(GENERATED)
        broken["questions"] = [{**GENERATED["questions"][0], "answers": ["Z"]}]
        with patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "1", "TAXPEARLS_AI_API_KEY": "test-key"}), \
                patch("src.ai_transport.chat_content", return_value=json.dumps(broken, ensure_ascii=False)):
            response = self.client.post("/api/teaching/ai-tasks/draft",
                                        json={"brief": BRIEF, "question_count": 3, "kinds": ["single"]})
        self.assertEqual(response.status_code, 422, response.text)
        with patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "1", "TAXPEARLS_AI_API_KEY": "test-key"}), \
                patch("src.ai_transport.chat_content", return_value="这不是 JSON"):
            response = self.client.post("/api/teaching/ai-tasks/draft",
                                        json={"brief": BRIEF, "question_count": 3, "kinds": ["single"]})
        self.assertEqual(response.status_code, 502, response.text)

    def test_draft_is_invisible_until_published(self):
        task = self.create()
        self.assertEqual(task["status"], "draft")
        self.assertEqual(task["stats"]["submitted"], 0)
        self.login(self.student)
        self.assertEqual(self.client.get("/api/teaching/ai-tasks").json()["items"], [])
        self.assertEqual(self.client.get(f"/api/teaching/ai-tasks/{task['id']}").status_code, 404)

    def test_published_task_hides_answers_until_submission(self):
        task = self.create(publish=True)
        self.login(self.student)
        items = self.client.get("/api/teaching/ai-tasks").json()["items"]
        self.assertEqual([item["id"] for item in items], [task["id"]])
        self.assertIsNone(items[0]["score"])
        detail = self.client.get(f"/api/teaching/ai-tasks/{task['id']}").json()
        self.assertIsNone(detail["submission"])
        self.assertNotIn("brief", detail)
        for question in detail["questions"]:
            self.assertNotIn("answers", question)
            self.assertNotIn("explanation", question)

    def test_submission_is_scored_once(self):
        task = self.create(publish=True)
        self.login(self.student)
        detail = self.client.get(f"/api/teaching/ai-tasks/{task['id']}").json()
        right, wrong = detail["questions"][0], detail["questions"][1]
        response = self.client.post(f"/api/teaching/ai-tasks/{task['id']}/submit",
                                    json={"answers": {right["id"]: ["B"], wrong["id"]: ["A"]}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["score"], 50.0)
        second = self.client.post(f"/api/teaching/ai-tasks/{task['id']}/submit",
                                  json={"answers": {right["id"]: ["B"], wrong["id"]: ["A", "B"]}})
        self.assertEqual(second.status_code, 409, second.text)
        after = self.client.get(f"/api/teaching/ai-tasks/{task['id']}").json()
        self.assertEqual(after["submission"]["score"], 50.0)
        self.assertEqual(after["questions"][0]["answers"], ["B"])
        self.assertTrue(after["questions"][0]["explanation"])

    def test_single_choice_accepts_only_one_option(self):
        task = self.create(publish=True)
        self.login(self.student)
        detail = self.client.get(f"/api/teaching/ai-tasks/{task['id']}").json()
        q = detail["questions"][0]
        response = self.client.post(f"/api/teaching/ai-tasks/{task['id']}/submit",
                                    json={"answers": {q["id"]: ["A", "B"]}})
        self.assertEqual(response.status_code, 422, response.text)

    def test_scores_round_only_after_counting_correct_answers(self):
        for count, correct_count, expected in ((3, 3, 100), (6, 6, 100), (7, 7, 100),
                                               (3, 1, 33.3), (7, 5, 71.4), (3, 0, 0)):
            with self.subTest(count=count, correct_count=correct_count):
                self.login(self.teacher)
                questions = [self.task_body()["questions"][0] for _ in range(count)]
                task = self.client.post("/api/teaching/ai-tasks", json=self.task_body(
                    publish=True, questions=questions)).json()
                self.login(self.student)
                answers = {q["id"]: q["answers"] for q in task["questions"][:correct_count]}
                response = self.client.post(f"/api/teaching/ai-tasks/{task['id']}/submit", json={"answers": answers})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["score"], expected)
                saved = self.client.get(f"/api/teaching/ai-tasks/{task['id']}").json()
                self.assertEqual(saved["submission"]["score"], expected)
                self.login(self.teacher)
                stats = self.client.get(f"/api/teaching/ai-tasks/{task['id']}").json()["stats"]
                self.assertEqual(stats["mean_score"], expected)

    def test_cross_teacher_and_foreign_student_are_rejected(self):
        task = self.create()
        self.login(self.colleague)
        self.assertEqual(self.client.get(f"/api/teaching/ai-tasks/{task['id']}").status_code, 403)
        self.assertEqual(self.client.put(f"/api/teaching/ai-tasks/{task['id']}",
                                         json=self.task_body()).status_code, 403)
        self.login(self.teacher)
        self.client.post(f"/api/teaching/ai-tasks/{task['id']}/publish", json={"published": True})
        self.login(self.outsider)
        self.assertEqual(self.client.get(f"/api/teaching/ai-tasks/{task['id']}").status_code, 404)
        self.assertEqual(self.client.post(f"/api/teaching/ai-tasks/{task['id']}/submit",
                                          json={"answers": {}}).status_code, 404)

    def test_published_task_cannot_be_rewritten(self):
        task = self.create(publish=True)
        response = self.client.put(f"/api/teaching/ai-tasks/{task['id']}", json=self.task_body())
        self.assertEqual(response.status_code, 409, response.text)
        archived = self.client.post(f"/api/teaching/ai-tasks/{task['id']}/publish", json={"published": False})
        self.assertEqual(archived.json()["status"], "archived")

    def test_teacher_statistics_reflect_submissions(self):
        task = self.create(publish=True)
        self.login(self.student)
        detail = self.client.get(f"/api/teaching/ai-tasks/{task['id']}").json()
        answers = {question["id"]: [question["options"][0]["key"]] for question in detail["questions"]}
        submitted = self.client.post(f"/api/teaching/ai-tasks/{task['id']}/submit", json={"answers": answers})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.login(self.teacher)
        items = self.client.get("/api/teaching/ai-tasks").json()["items"]
        self.assertEqual(items[0]["submitted"], 1)
        self.assertEqual(items[0]["expected"], 1)

    def test_teacher_reads_question_details_before_and_after_archive(self):
        task = self.create(publish=True)
        url = f"/api/teaching/ai-tasks/{task['id']}/submissions"
        empty = self.client.get(url)
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["items"], [])
        self.login(self.student)
        questions = self.client.get(f"/api/teaching/ai-tasks/{task['id']}").json()["questions"]
        submitted = self.client.post(f"/api/teaching/ai-tasks/{task['id']}/submit", json={
            "answers": {questions[0]["id"]: ["B"], questions[1]["id"]: []}})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.login(self.teacher)
        for archived in (False, True):
            if archived:
                self.client.post(f"/api/teaching/ai-tasks/{task['id']}/publish", json={"published": False})
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            data = response.json()
            self.assertEqual(data["task"]["title"], task["title"])
            self.assertNotIn("model", data["task"])
            self.assertEqual(data["task"]["questions"][0]["stem"], questions[0]["stem"])
            record = data["items"][0]
            self.assertEqual(record["student_name"], "学生甲")
            self.assertEqual(record["username"], self.student["username"])
            self.assertEqual(record["score"], 50.0)
            self.assertTrue(record["submitted_at"])
            right, blank = record["detail"]
            self.assertEqual(right["picked"], ["B"])
            self.assertTrue(right["correct"])
            self.assertEqual(right["points"], 50.0)
            self.assertEqual(blank["picked"], [])
            self.assertFalse(blank["correct"])
            self.assertEqual(blank["answers"], ["A", "B"])
            self.assertTrue(blank["explanation"])

    def test_submission_details_are_only_visible_to_task_owner(self):
        task = self.create(publish=True)
        url = f"/api/teaching/ai-tasks/{task['id']}/submissions"
        foreign_teacher = self.store.create_user("foreign-teacher", self.password, "外校教师", "teacher", "school-b")
        for who, status in ((self.student, 403), (self.colleague, 403),
                            (self.outsider, 403), (foreign_teacher, 404)):
            self.login(who)
            response = self.client.get(url)
            self.assertEqual(response.status_code, status, response.text)
        self.login(self.teacher)
        response = self.client.get("/api/teaching/ai-tasks/missing/submissions")
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
