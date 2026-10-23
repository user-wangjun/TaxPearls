"""教师端成绩单导出（/api/classes/{id}/statistics.csv）的口径与鉴权。

导出的数字必须和页面上「班级学情统计」一致，所以这里重点守四件事：
1. 只导本班有效名册，未发布/撤回作业按页面同一套规则排除或单列；
2. 自动分与复核分分开，当前分=复核分优先（人工调分不改客观记录）；
3. 未交留空，不写 0 分；材料异常的提交既不按 0 分也不按未交；
4. 跨教师、跨机构、未登录都不能拿到文件。
"""
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import io
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp.storage import Store


class GradebookExportTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "0",
                                           "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / "gradebook.db", keepalive=True)
        module.store = self.store
        self.password = "Gradebook-pw-4d29!"
        self.teacher = self.store.create_user("gb-teacher", self.password, "任课教师", "teacher", "school-a")
        self.colleague = self.store.create_user("gb-colleague", self.password, "同校教师", "teacher", "school-a")
        self.student = self.store.create_user("2026001", self.password, "陈雨欣", "student", "school-a")
        self.quiet = self.store.create_user("2026002", self.password, "李子航", "student", "school-a")
        self.foreign = self.store.create_user("gb-foreign", self.password, "外校学生", "student", "school-b")
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        self.case = self.client.post("/api/exercises",
                                     json={"rule_id": "R-020", "seed": 41, "expected_version": "2.0"}).json()
        self.class_id = self.client.post("/api/classes", json={
            "name": "会计2601", "student_ids": [self.student["id"], self.quiet["id"]]}).json()["id"]
        body = {"title": "单案例涉税风险识别", "audit_id": self.case["audit_id"],
                "published": True, "class_id": self.class_id}
        self.assignment = self.client.post("/api/assignments", json=body).json()["id"]
        self.open_answers_after_submit(self.assignment)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    # ---------------------------------------------------------------- 构造辅助
    def login(self, who):
        self.client.cookies.clear()
        response = self.client.post("/api/login", json={"username": who["username"], "password": self.password})
        self.assertEqual(response.status_code, 200, response.text)

    def attach_class(self, assignment_id, class_id):
        with self.store.connect() as db:
            db.execute("UPDATE training_assignment_settings SET class_id=? WHERE assignment_id=?",
                       (class_id, assignment_id))

    def open_answers_after_submit(self, subject_id):
        with self.store.connect() as db:
            db.execute("""INSERT INTO training_task_settings
                            (subject_kind,subject_id,org_id,answer_release,updated_at)
                          VALUES ('assignment',?,?,'after_submit','2026-01-01T00:00:00+00:00')
                          ON CONFLICT(subject_kind,subject_id)
                          DO UPDATE SET answer_release='after_submit'""",
                       (subject_id, self.teacher["org_id"]))

    def submit(self, assignment_id, answers):
        return self.client.post(f"/api/assignments/{assignment_id}/submit",
                                json={"selected_rule_ids": answers})

    def export(self, layout="matrix", include_withdrawn="false", class_id=None):
        return self.client.get(f"/api/classes/{class_id or self.class_id}/statistics.csv",
                               params={"layout": layout, "include_withdrawn": include_withdrawn})

    def rows(self, response):
        text = response.content.decode("utf-8-sig")
        return [row for row in csv.reader(io.StringIO(text))]

    def data_rows(self, response):
        return [row for row in self.rows(response) if row and not row[0].startswith("#")][1:]

    # -------------------------------------------------------------------- 用例
    def test_matrix_export_keeps_unsubmitted_blank_and_counts_submitted(self):
        self.login(self.student)
        self.assertEqual(self.submit(self.assignment, self.case["metadata"]["standard_answer"]).status_code, 200)
        self.login(self.teacher)
        response = self.export()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"), "CSV 须带 UTF-8 BOM，否则 Excel 打开中文乱码")

        header = self.rows(response)[3]
        self.assertEqual(header[:3], ["学号", "姓名", "班级"])
        self.assertEqual(header[-2:], ["已交/应交", "均分"])
        table = {row[0]: row for row in self.data_rows(response)}
        self.assertEqual(table["2026001"][-2], "1/1")
        self.assertEqual(table["2026002"][-2], "0/1")
        # 未交的学生成绩列必须留空，不能写 0 —— 0 分和没交是两件事
        self.assertEqual(table["2026002"][3], "")
        self.assertEqual(table["2026002"][4], "未交")
        self.assertEqual(table["2026001"][4], "已交")

    def test_adjusted_score_wins_but_automatic_score_is_kept(self):
        self.login(self.student)
        self.submit(self.assignment, [])
        self.login(self.teacher)
        submission = self.store.get_submission(self.assignment, self.student["id"])["id"]
        adjusted = self.client.put(f"/api/submissions/{submission}/review",
                                   json={"adjusted_score": 88, "feedback": "人工复核：部分对"})
        self.assertEqual(adjusted.status_code, 200, adjusted.text)
        detail = {(row[0], row[3]): row for row in self.data_rows(self.export("detail"))}
        line = detail[("2026001", "单案例涉税风险识别")]
        # 列：学号,姓名,班级,任务,类型,状态,自动分,复核分,当前分,提交时间
        self.assertEqual(float(line[6]), 0.0)
        self.assertEqual(float(line[7]), 88.0)
        self.assertEqual(float(line[8]), 88.0)
        self.assertEqual(line[5], "已交")

    def test_unpublished_task_is_skipped_and_withdrawn_needs_opt_in(self):
        with self.store.connect() as db:
            db.execute("UPDATE assignments SET published=0 WHERE id=?", (self.assignment,))
        response = self.export()
        self.assertEqual(response.status_code, 200, response.text)
        comments = " ".join(row[0] for row in self.rows(response) if row and row[0].startswith("#"))
        self.assertIn("未纳入", comments)
        self.assertIn("未发布且无人提交", comments)
        self.assertEqual(self.data_rows(response), [], "没有任何任务时数据区应为空，而不是伪造一行 0 分")

        # 撤回作业的提交只能在发布期间产生：先提交，再撤回。
        with self.store.connect() as db:
            db.execute("UPDATE assignments SET published=1 WHERE id=?", (self.assignment,))
        self.login(self.student)
        self.assertEqual(self.submit(self.assignment, []).status_code, 200)
        self.login(self.teacher)
        with self.store.connect() as db:
            db.execute("UPDATE assignments SET published=0 WHERE id=?", (self.assignment,))
        self.assertIn("已撤回（勾选「含撤回」才计入）",
                      " ".join(row[0] for row in self.rows(self.export()) if row and row[0].startswith("#")))
        included = self.export(include_withdrawn="true")
        self.assertEqual({row[0] for row in self.data_rows(included)}, {"2026001", "2026002"})

    def test_duplicate_task_titles_are_disambiguated(self):
        body = {"title": "单案例涉税风险识别", "audit_id": self.case["audit_id"],
                "published": True, "class_id": self.class_id}
        self.client.post("/api/assignments", json=body)
        header = self.rows(self.export())[3]
        self.assertIn("单案例涉税风险识别（第1份）(分)", header)
        self.assertIn("单案例涉税风险识别（第2份）(分)", header)

    def test_roster_and_permissions(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO training_class_members VALUES (?,?)",
                           [(self.class_id, self.foreign["id"])])
        table = {row[0] for row in self.data_rows(self.export())}
        self.assertEqual(table, {"2026001", "2026002"}, "外校学生不能被并进本班成绩单")

        self.login(self.colleague)
        self.assertEqual(self.export().status_code, 404)
        self.login(self.student)
        self.assertEqual(self.export().status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.export().status_code, 401)

    def test_bad_layout_is_rejected(self):
        self.assertEqual(self.export("pivot").status_code, 422)


class GradebookDeadlineTests(unittest.TestCase):
    """截止不影响导出：过了截止仍要能导出（成绩单是事后用的）。"""

    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "0",
                                           "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / "gradebook-deadline.db", keepalive=True)
        module.store = self.store
        self.password = "Gradebook-dl-8823!"
        self.teacher = self.store.create_user("gb-dl-teacher", self.password, "教师", "teacher", "school-a")
        self.student = self.store.create_user("2026100", self.password, "学生", "student", "school-a")
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.client.post("/api/login", json={"username": "gb-dl-teacher", "password": self.password})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    def test_expired_deadline_still_exports(self):
        case = self.client.post("/api/exercises",
                                json={"rule_id": "R-001", "seed": 7, "expected_version": "2.0"}).json()
        class_id = self.client.post("/api/classes", json={
            "name": "已截止班", "student_ids": [self.student["id"]]}).json()["id"]
        assignment = self.client.post("/api/assignments", json={
            "title": "过期任务", "audit_id": case["audit_id"], "published": True,
            "class_id": class_id, "deadline_at": "2032-01-01T00:00:00+00:00"}).json()["id"]
        with self.store.connect() as db:
            db.execute("""UPDATE training_assignment_settings
                             SET deadline_at=? WHERE assignment_id=?""",
                       (datetime(2020, 1, 1, tzinfo=UTC).isoformat(), assignment))
        response = self.client.get(f"/api/classes/{class_id}/statistics.csv")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row[0] for row in csv.reader(io.StringIO(response.content.decode("utf-8-sig")))
                          if row and not row[0].startswith("#")][1:], ["2026100"])


if __name__ == "__main__":
    unittest.main()
