"""课程挂学期实体（FR-U04 v2）：学期不再是自由文本，而是指向 college_term 的外键。

覆盖四件事：
1. 显式 term_id 绑定后，展示文本 semester 跟学期名同步（不再各写各的）；
2. 旧调用只给学期文本时，按「同院校同学期名」自动落到实体；
3. 院校已有学期、文本却对不上时拒绝（否则又回到自由文本对不上账的老问题）；
4. additive 迁移把历史课程的学期文本回填成 term_id，且重复执行幂等。
"""
from __future__ import annotations

import hashlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp import schema, training_reports
from webapp.storage import Store

COLLEGE = "课程学期测试学院"
OTHER_COLLEGE = "另一所学院"
TERM_A = "2026-2027学年第一学期"
TERM_B = "2026-2027学年第二学期"
STAFF_ID = "cu-course-terms"
STAFF_TOKEN = "course-terms-token"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CourseTermApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "course-terms.db"
        self.env = patch.dict(os.environ, {"TAXPEARLS_DB": str(self.path)})
        self.env.start()
        self.store = Store(self.path, keepalive=True)
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user (id,username,password_hash,display_name,college,"
                       "role,active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (STAFF_ID, "ct-admin", "x", "学期管理员", COLLEGE, "admin", "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO college_user (id,username,password_hash,display_name,college,"
                       "role,active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                       ("cu-other", "ct-other", "x", "外校管理员", OTHER_COLLEGE, "admin",
                        "2026-10-23T00:00:00+00:00"))
            db.execute("""INSERT INTO college_term (id,college,name,start_date,end_date,is_current,archived,created_at)
                          VALUES (?,?,?,?,?,1,0,?)""",
                       ("t-a", COLLEGE, TERM_A, None, None, "2026-10-23T00:00:00+00:00"))
            db.execute("""INSERT INTO college_term (id,college,name,start_date,end_date,is_current,archived,created_at)
                          VALUES (?,?,?,?,?,0,0,?)""",
                       ("t-b", COLLEGE, TERM_B, None, None, "2026-10-23T00:00:00+00:00"))
            db.execute("""INSERT INTO college_term (id,college,name,start_date,end_date,is_current,archived,created_at)
                          VALUES (?,?,?,?,?,0,1,?)""",
                       ("t-old", COLLEGE, "2025-2026学年第一学期", None, None,
                        "2026-10-23T00:00:00+00:00"))
            db.execute("""INSERT INTO college_term (id,college,name,start_date,end_date,is_current,archived,created_at)
                          VALUES (?,?,?,?,?,0,0,?)""",
                       ("t-foreign", OTHER_COLLEGE, TERM_A, None, None, "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO training_staff_sessions (token_hash,staff_id,expires_at,created_at)"
                       " VALUES (?,?,?,?)",
                       (digest(STAFF_TOKEN), STAFF_ID, "2099-01-01T00:00:00+00:00",
                        "2026-10-23T00:00:00+00:00"))
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.client.cookies.set("taxpearls_staff_session", STAFF_TOKEN)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        # lifespan 建的应用级 Store 绑定的是本用例的临时库；不关就串到下个用例、也删不掉临时目录
        # 应用级 Store 的 keepalive 连接建在 TestClient 的线程里，这里不能直接 close
        # （跨线程关闭会抛 ProgrammingError）；置空即可让下个用例按新环境变量重建库。
        module.store = None
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 工具
    def create(self, **body):
        payload = {"name": "涉税风险识别", "college": COLLEGE, "class_names": ["会计2601"]}
        payload.update(body)
        return self.client.post("/api/training/staff/courses", json=payload)

    def patch_course(self, course_id, **body):
        return self.client.patch(f"/api/training/staff/courses/{course_id}", json=body)

    def courses(self, **params):
        response = self.client.get("/api/training/staff/courses", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["courses"]

    # ---------------------------------------------------------------- 用例
    def test_create_with_term_id_syncs_display_text(self):
        response = self.create(term_id="t-a")
        self.assertEqual(response.status_code, 200, response.text)
        course = response.json()["course"]
        self.assertEqual(course["term_id"], "t-a")
        self.assertEqual(course["semester"], TERM_A, "展示文本应取学期实体名")
        self.assertEqual(course["term_name"], TERM_A)
        self.assertTrue(course["term_is_current"])
        listed = self.courses()[0]
        self.assertEqual(listed["term_id"], "t-a")
        self.assertEqual(listed["semester"], TERM_A)

    def test_legacy_text_only_binds_matching_term(self):
        response = self.create(semester=TERM_B)
        self.assertEqual(response.status_code, 200, response.text)
        course = response.json()["course"]
        self.assertEqual(course["term_id"], "t-b", "只给文本时按同名学期自动落地")
        self.assertEqual(course["semester"], TERM_B)

    def test_unmatched_text_rejected_when_terms_exist(self):
        response = self.create(semester="2026-2027-1")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("不是本院校已建立的学期", response.text)

    def test_cross_college_and_archived_term_rejected(self):
        self.assertEqual(self.create(name="跨校学期", term_id="t-foreign").status_code, 404)
        response = self.create(name="归档学期", term_id="t-old")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("已归档", response.text)

    def test_same_name_different_term_allowed_same_term_rejected(self):
        self.assertEqual(self.create(term_id="t-a").status_code, 200)
        self.assertEqual(self.create(term_id="t-a").status_code, 422, "同学期同名课程应拦")
        self.assertEqual(self.create(term_id="t-b").status_code, 200, "不同学期可以同名")
        self.assertEqual(len(self.courses()), 2)

    def test_patch_moves_term_and_can_unbind(self):
        course = self.create(term_id="t-a").json()["course"]
        moved = self.patch_course(course["id"], term_id="t-b")
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["course"]["term_id"], "t-b")
        self.assertEqual(moved.json()["course"]["semester"], TERM_B)
        unbound = self.patch_course(course["id"], term_id=None)
        self.assertEqual(unbound.status_code, 200, unbound.text)
        self.assertIsNone(unbound.json()["course"]["term_id"])
        self.assertIsNone(unbound.json()["course"]["semester"], "解绑学期时展示文本一并清空")
        # 正常改名字不应该把学期弄丢
        renamed = self.patch_course(course["id"], name="改名后的课程")
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertIsNone(renamed.json()["course"]["term_id"])

    def test_list_filters_by_term(self):
        self.create(name="第一学期课", term_id="t-a")
        self.create(name="第二学期课", term_id="t-b")
        self.assertEqual(len(self.courses()), 2)
        self.assertEqual([c["name"] for c in self.courses(term_id="t-a")], ["第一学期课"])

    def test_teacher_cannot_touch_other_college_term(self):
        # 外校管理员用别人的 term_id：staff(body.college) 先按院校校验
        self.client.cookies.clear()
        with self.store.connect() as db:
            db.execute("INSERT INTO training_staff_sessions (token_hash,staff_id,expires_at,created_at)"
                       " VALUES (?,?,?,?)",
                       (digest("other-token"), "cu-other", "2099-01-01T00:00:00+00:00",
                        "2026-10-23T00:00:00+00:00"))
        self.client.cookies.set("taxpearls_staff_session", "other-token")
        response = self.create(college=COLLEGE, name="越权课程", term_id="t-a")
        self.assertEqual(response.status_code, 403, response.text)


class CourseTermMigrationTests(unittest.TestCase):
    """历史课程的学期文本要能被 additive 迁移回填成 term_id。"""

    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "backfill.db", keepalive=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_backfill_binds_legacy_text_and_is_idempotent(self):
        with self.store.connect() as db:
            db.execute("""INSERT INTO college_term (id,college,name,start_date,end_date,is_current,archived,created_at)
                          VALUES (?,?,?,?,?,1,0,?)""",
                       ("t-a", COLLEGE, TERM_A, None, None, "2026-10-23T00:00:00+00:00"))
            db.execute("""INSERT INTO course (id,college,name,semester,teacher_id,description,active,
                                              created_at,created_by)
                          VALUES (?,?,?,?,?,?,1,?,?)""",
                       ("c-legacy", COLLEGE, "历史课程", TERM_A, None, None,
                        "2026-10-01T00:00:00+00:00", None))
            db.execute("""INSERT INTO course (id,college,name,semester,teacher_id,description,active,
                                              created_at,created_by)
                          VALUES (?,?,?,?,?,?,1,?,?)""",
                       ("c-odd", COLLEGE, "对不上的课程", "2026-2027-1", None, None,
                        "2026-10-01T00:00:00+00:00", None))
        with self.store.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(course)")}
        self.assertIn("term_id", columns, "additive 迁移应补出 term_id 列")
        schema.initialize(self.store)  # 模拟一次升级启动
        with self.store.connect() as db:
            rows = {row["id"]: row["term_id"] for row in db.execute(
                "SELECT id, term_id FROM course")}
        self.assertEqual(rows["c-legacy"], "t-a", "学期文本能对上就回填实体")
        self.assertIsNone(rows["c-odd"], "对不上的历史文本保持不挂，不硬凑")
        schema.initialize(self.store)  # 幂等
        with self.store.connect() as db:
            again = {row["id"]: row["term_id"] for row in db.execute(
                "SELECT id, term_id FROM course")}
        self.assertEqual(again, rows)


class CourseTermReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "report-terms.db", keepalive=True)
        with self.store.connect() as db:
            db.execute("""INSERT INTO college_term (id,college,name,start_date,end_date,is_current,archived,created_at)
                          VALUES (?,?,?,?,?,1,0,?)""",
                       ("t-a", COLLEGE, TERM_A, None, None, "2026-10-23T00:00:00+00:00"))
            db.execute("""INSERT INTO college_term (id,college,name,start_date,end_date,is_current,archived,created_at)
                          VALUES (?,?,?,?,?,0,0,?)""",
                       ("t-b", COLLEGE, TERM_B, None, None, "2026-10-23T00:00:00+00:00"))
            for cid, name, term in (("c-a", "第一学期课", "t-a"), ("c-b", "第二学期课", "t-b"),
                                    ("c-none", "没挂学期的课", None)):
                db.execute("""INSERT INTO course (id,college,name,semester,term_id,teacher_id,
                                                  description,active,created_at,created_by)
                              VALUES (?,?,?,?,?,?,?,1,?,?)""",
                           (cid, COLLEGE, name, TERM_A if term == "t-a" else None, term, None, None,
                            "2026-10-01T00:00:00+00:00", None))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_report_courses_follow_term_filter(self):
        with self.store.connect() as db:
            report = training_reports.build_report(db, COLLEGE)
            self.assertEqual(len(report["courses"]), 3)
            by_term = training_reports.build_report(db, COLLEGE, term_id="t-a")
            self.assertEqual([c["name"] for c in by_term["courses"]], ["第一学期课"],
                             "按学期筛选时未挂学期的课程无从归属，不能混进来")
            self.assertEqual(by_term["courses"][0]["term_name"], TERM_A)
            self.assertTrue(any("未挂学期实体" in note for note in report["notes"]))


if __name__ == "__main__":
    unittest.main()
