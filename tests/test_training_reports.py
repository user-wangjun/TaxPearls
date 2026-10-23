"""高校管理报表（FR-U05）：院系/专业/班级/课程维度聚合的口径与鉴权。

重点守四件事：
1. 教学班未绑定班级实体时**不进**院系/专业汇总（宁可显示"未归属"，也不按名字猜）；
2. 绑定后各级数字与「班级学情统计」「成绩单」一致（同一 gradebook，只做求和）；
3. 跨层级可对账：院系合计 = 其下专业合计 = 其下班组合计；
4. 只有管理员能看报表、能改绑定。
"""
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp.storage import Store, _hash_token

COLLEGE = "报表测试学院"
OTHER = "邻校"


class LearningReportTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "0",
                                           "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / "report.db", keepalive=True)
        module.store = self.store
        self.password = "Report-pw-7731!"
        self.teacher = self.store.create_user("rep-teacher", self.password, "任课教师", "teacher", "school-a")
        self.student = self.store.create_user("2026001", self.password, "陈雨欣", "student", "school-a")
        self.quiet = self.store.create_user("2026002", self.password, "李子航", "student", "school-a")
        self.now = datetime.now(UTC).isoformat(timespec="seconds")
        self.admin_staff = self.add_staff("rep-admin", "管理员", "admin")
        self.teacher_staff = self.add_staff("rep-teacher-staff", "任课教师", "teacher")
        with self.store.connect() as db:
            db.execute("INSERT INTO university_identity_links (user_id,staff_id,student_id) VALUES (?,?,NULL)",
                       (self.teacher["id"], self.teacher_staff))
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.client.post("/api/login", json={"username": "rep-teacher", "password": self.password})
        case = self.client.post("/api/exercises",
                                json={"rule_id": "R-001", "seed": 13, "expected_version": "2.0"}).json()
        self.class_id = self.client.post("/api/classes", json={
            "name": "会计2601 演示班", "student_ids": [self.student["id"], self.quiet["id"]]}).json()["id"]
        self.assignment = self.client.post("/api/assignments", json={
            "title": "期末实训", "audit_id": case["audit_id"], "published": True,
            "class_id": self.class_id}).json()["id"]
        with self.store.connect() as db:
            db.execute("""INSERT INTO training_task_settings
                            (subject_kind,subject_id,org_id,answer_release,updated_at)
                          VALUES ('assignment',?,?,'after_submit','2026-01-01T00:00:00+00:00')
                          ON CONFLICT(subject_kind,subject_id) DO UPDATE SET answer_release='after_submit'""",
                       (self.assignment, self.teacher["org_id"]))
        self.client.post("/api/login", json={"username": "2026001", "password": self.password})
        submitted = self.client.post(f"/api/assignments/{self.assignment}/submit",
                                     json={"selected_rule_ids": case["metadata"]["standard_answer"]})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.class_entity = self.add_org()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    # ------------------------------------------------------------------ 造数
    def add_staff(self, username, display, role, college=COLLEGE):
        staff_id = username + "-id"
        with self.store.connect() as db:
            db.execute("""INSERT INTO college_user
                            (id,username,password_hash,display_name,college,department,email,phone,role,active,created_at)
                          VALUES (?,?,?,?,?,NULL,NULL,NULL,?,1,?)""",
                       (staff_id, username, "not-used-in-tests", display, college, role, self.now))
        return staff_id

    def add_org(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO college_department (id,college,name,archived,created_at) VALUES ('d1',?,?,0,?)",
                       (COLLEGE, "会计学院", self.now))
            db.execute("""INSERT INTO college_major (id,college,name,department_id,archived,created_at)
                          VALUES ('m1',?,?, 'd1',0,?)""", (COLLEGE, "大数据与会计", self.now))
            db.execute("""INSERT INTO college_term (id,college,name,start_date,end_date,is_current,archived,created_at)
                          VALUES ('t1',?,'2026-2027-1','2026-09-01','2027-01-20',1,0,?)""",
                       (COLLEGE, self.now))
            db.execute("""INSERT INTO college_class (id,college,name,department_id,term_id,archived,created_at,major_id)
                          VALUES ('c1',?, '会计2601','d1','t1',0,?,'m1')""", (COLLEGE, self.now))
            db.execute("""INSERT INTO course (id,college,name,semester,teacher_id,description,active,created_at,created_by)
                          VALUES ('course1',?, '涉税实务','2026-2027-1',NULL,NULL,1,?,NULL)""",
                       (COLLEGE, self.now))
            db.execute("INSERT INTO course_class (course_id,class_name) VALUES ('course1','会计2601')")
        return "c1"

    def staff_cookie(self, staff_id):
        token = "token-" + staff_id
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO training_staff_sessions (token_hash,staff_id,expires_at,created_at) "
                       "VALUES (?,?,'2099-01-01T00:00:00+00:00',?)",
                       (_hash_token(token), staff_id, self.now))
        return token

    def as_staff(self, staff_id):
        self.client.cookies.clear()
        self.client.cookies.set("taxpearls_staff_session", self.staff_cookie(staff_id))

    def report(self, staff_id, **params):
        self.as_staff(staff_id)
        return self.client.get("/api/training/staff/reports/learning",
                               params={"college": COLLEGE, **params})

    def bind(self, staff_id, college_class_id):
        self.as_staff(staff_id)
        return self.client.post("/api/training/staff/class-bindings",
                                json={"college": COLLEGE, "teaching_class_id": self.class_id,
                                      "college_class_id": college_class_id})

    # ------------------------------------------------------------------ 用例
    def test_unbound_class_is_listed_but_not_counted(self):
        response = self.report(self.admin_staff)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["college"], COLLEGE)
        self.assertEqual(data["totals"]["classes"], 0, "未绑定就不该计入院系汇总")
        self.assertEqual(len(data["unbound"]), 1)
        row = data["unbound"][0]
        self.assertEqual(row["name"], "会计2601 演示班")
        self.assertEqual(row["students"], 2)
        self.assertEqual(row["tasks"], 1)
        self.assertEqual(row["submitted"], 1)
        self.assertEqual(row["submit_rate"], 50.0)
        self.assertTrue(any(s["name"] == "会计2601" for s in row["suggestions"]),
                        "应给出候选班级实体，但只是建议")
        dept = data["departments"][0]
        self.assertEqual(dept["totals"]["classes"], 0)
        self.assertEqual(data["courses"][0]["classes"], 0)
        self.assertIn("未关联", data["courses"][0]["note"])

    def test_binding_moves_numbers_into_department_and_levels_reconcile(self):
        self.assertEqual(self.bind(self.admin_staff, self.class_entity).status_code, 200)
        data = self.report(self.admin_staff).json()
        self.assertEqual(data["unbound"], [])
        dept = data["departments"][0]
        major = dept["majors"][0]
        cls = major["classes"][0]
        for level in (dept["totals"], major["totals"], cls["totals"]):
            self.assertEqual(level["classes"], 1)
            self.assertEqual(level["students"], 2)
            self.assertEqual(level["tasks"], 1)
            self.assertEqual(level["expected"], 2)
            self.assertEqual(level["submitted"], 1)
            self.assertEqual(level["submit_rate"], 50.0)
            self.assertEqual(level["mean"], 100.0)
        self.assertEqual(dept["totals"], major["totals"], "院系合计必须等于其下专业合计")
        course = data["courses"][0]
        self.assertEqual(course["classes"], 1, "课程绑定的是班级名，经班级实体找到已绑定的教学班")
        self.assertEqual(course["submitted"], 1)
        self.assertIsNone(course["note"])

    def test_unbind_and_rebind_are_reversible(self):
        self.bind(self.admin_staff, self.class_entity)
        self.assertEqual(self.report(self.admin_staff).json()["totals"]["classes"], 1)
        self.assertEqual(self.bind(self.admin_staff, None).status_code, 200)
        data = self.report(self.admin_staff).json()
        self.assertEqual(data["totals"]["classes"], 0)
        self.assertEqual(len(data["unbound"]), 1)

    def test_filters_by_term_and_department(self):
        self.bind(self.admin_staff, self.class_entity)
        self.assertEqual(self.report(self.admin_staff, term_id="t1").json()["totals"]["classes"], 1)
        self.assertEqual(self.report(self.admin_staff, term_id="nope").json()["totals"]["classes"], 0)
        self.assertEqual(self.report(self.admin_staff, department_id="d1").json()["totals"]["classes"], 1)
        self.assertEqual(self.report(self.admin_staff, department_id="d2").json()["totals"]["classes"], 0)

    def test_teacher_cannot_read_report_or_bind(self):
        self.assertEqual(self.report(self.teacher_staff).status_code, 403)
        self.assertEqual(self.bind(self.teacher_staff, self.class_entity).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/training/staff/reports/learning",
                                         params={"college": COLLEGE}).status_code, 401)

    def test_cannot_bind_class_of_another_college(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO college_department (id,college,name,archived,created_at) "
                       "VALUES ('dx',?, '外校院系',0,?)", (OTHER, self.now))
            db.execute("""INSERT INTO college_class (id,college,name,department_id,term_id,archived,created_at,major_id)
                          VALUES ('cx',?, '外校班','dx',NULL,0,?,NULL)""", (OTHER, self.now))
        response = self.bind(self.admin_staff, "cx")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
