"""独立在线考试 · E1（出卷与发布冻结）单测。

守的是四条边界：
1. 题目进来就复制成快照，发布后改题库也不漂移；
2. 发布就是冻结：题目、时长、策略动不了，只能改说明；
3. 发布有硬门槛：至少一道题、名单非空、时长合法；
4. 名单不静默丢人：查不到平台账号的学号、查无此学号都要列出来。
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp import exams
from webapp.storage import Store

COLLEGE = "考试测试学院"
OTHER = "另一所学院"
ORG = "org-exam-test"
STAFF_ID = "cu-exam-admin"
OTHER_STAFF = "cu-exam-other"
TOKEN = "exam-token"
OTHER_TOKEN = "exam-token-other"

Q1 = {"kind": "single", "stem": "增值税一般纳税人销售货物适用税率是？",
      "options": [{"key": "A", "text": "13%"}, {"key": "B", "text": "6%"}],
      "answer_keys": ["A"], "points": 2}
Q2 = {"kind": "multiple", "stem": "下列属于增值税视同销售的有？",
      "options": [{"key": "A", "text": "自产货物用于集体福利"}, {"key": "B", "text": "购进货物用于免税项目"},
                  {"key": "C", "text": "自产货物无偿赠送"}],
      "answer_keys": ["A", "C"], "points": 4}


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ExamE1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "exams.db"
        self.env = patch.dict(os.environ, {"TAXPEARLS_DB": str(self.path)})
        self.env.start()
        self.store = Store(self.path, keepalive=True)
        with self.store.connect() as db:
            db.execute("INSERT INTO org_settings (org_id, org_type, display_name, updated_at)"
                       " VALUES (?, 'university', ?, ?)", (ORG, COLLEGE, "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO org_settings (org_id, org_type, display_name, updated_at)"
                       " VALUES (?, 'university', ?, ?)", (ORG + "-2", OTHER, "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO university_organizations VALUES (?,?)", (COLLEGE, ORG))
            db.execute("INSERT INTO university_organizations VALUES (?,?)", (OTHER, ORG + "-2"))
            for uid, username, name, role in ((STAFF_ID, "exam-admin", "考试管理员", "admin"),
                                              ("u-teacher", "exam-teacher", "任课老师", "student")):
                db.execute("INSERT INTO users (id, username, password_hash, display_name, role, org_id,"
                           " active, created_at) VALUES (?,?,?,?,?,?,1,?)",
                           (uid, username, "x", name, role, ORG, "2026-10-23T00:00:00+00:00"))
            for uid, username, name, role in ((OTHER_STAFF, "other-admin", "外校管理员", "admin"),):
                db.execute("INSERT INTO users (id, username, password_hash, display_name, role, org_id,"
                           " active, created_at) VALUES (?,?,?,?,?,?,1,?)",
                           (uid, username, "x", name, role, ORG + "-2", "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO college_user (id,username,password_hash,display_name,college,role,"
                       "active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (STAFF_ID, "exam-admin", "x", "考试管理员", COLLEGE, "admin",
                        "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO college_user (id,username,password_hash,display_name,college,role,"
                       "active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (OTHER_STAFF, "other-admin", "x", "外校管理员", OTHER, "admin",
                        "2026-10-23T00:00:00+00:00"))
            # 学生：两条正常关联 + 一条未关联平台账号 + 一条同班重复导入
            for sid, name, no, linked in (("u-stu-1", "甲同学", "20260101", True),
                                          ("u-stu-2", "乙同学", "20260102", True),
                                          ("u-stu-3", "丙同学", "20260103", False)):
                info_id = "info-" + sid
                db.execute("""INSERT INTO student_info (id,college,student_no,name,class_name,
                              password_hash,active,created_at) VALUES (?,?,?,?,?,?,1,?)""",
                           (info_id, COLLEGE, no, name, "会计2601", "x", "2026-10-23T00:00:00+00:00"))
                if linked:
                    db.execute("""INSERT INTO users (id,username,password_hash,display_name,role,org_id,
                                  active,created_at) VALUES (?,?,?,?,?,?,1,?)""",
                               (sid, no, "x", name, "student", ORG, "2026-10-23T00:00:00+00:00"))
                    db.execute("INSERT INTO university_identity_links (user_id,student_id) VALUES (?,?)",
                               (sid, info_id))
            db.execute("INSERT INTO training_classes (id,org_id,owner_id,name,revision,created_at)"
                       " VALUES (?,?,?,?,1,?)",
                       ("c-1", ORG, STAFF_ID, "会计2601 演示班", "2026-10-23T00:00:00+00:00"))
            for sid in ("u-stu-1", "u-stu-2"):
                db.execute("INSERT INTO training_class_members (class_id,student_id) VALUES (?,?)",
                           ("c-1", sid))
            db.execute("""INSERT INTO course (id,college,name,semester,teacher_id,description,active,
                          created_at,created_by) VALUES (?,?,?,?,?,?,1,?,?)""",
                       ("crs-1", COLLEGE, "涉税风险识别", "2026-2027学年第一学期", None, None,
                        "2026-10-01T00:00:00+00:00", STAFF_ID))
            for token, staff in ((TOKEN, STAFF_ID), (OTHER_TOKEN, OTHER_STAFF)):
                db.execute("INSERT INTO training_staff_sessions (token_hash,staff_id,expires_at,created_at)"
                           " VALUES (?,?,?,?)",
                           (digest(token), staff, "2099-01-01T00:00:00+00:00", "2026-10-23T00:00:00+00:00"))
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.client.cookies.set("taxpearls_staff_session", TOKEN)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        # 应用级 Store 的 keepalive 连接建在 TestClient 线程里，跨线程 close 会抛错，只能置空
        module.store = None
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 工具
    def create(self, **body):
        payload = {"title": "税务基础期末测验", "college": COLLEGE, "duration_minutes": 90}
        payload.update(body)
        return self.client.post("/api/training/exams/papers", json=payload)

    def detail(self, paper_id, college=COLLEGE):
        return self.client.get(f"/api/training/exams/papers/{paper_id}", params={"college": college})

    def questions(self, paper_id, questions, college=COLLEGE):
        return self.client.put(f"/api/training/exams/papers/{paper_id}/questions",
                               json={"college": college, "questions": questions})

    def roster(self, paper_id, *, class_ids=None, student_ids=None, student_nos=None, college=COLLEGE):
        return self.client.put(f"/api/training/exams/papers/{paper_id}/roster",
                               json={"college": college, "class_ids": class_ids or [],
                                     "student_ids": student_ids or [], "student_nos": student_nos or []})

    def publish(self, paper_id, college=COLLEGE):
        return self.client.post(f"/api/training/exams/papers/{paper_id}/publish",
                                params={"college": college})

    # ---------------------------------------------------------------- 用例
    def test_create_paper_starts_as_editable_draft(self):
        response = self.create(description="期末测验说明", course_id="crs-1", pass_score=60)
        self.assertEqual(response.status_code, 200, response.text)
        paper = response.json()["paper"]
        self.assertEqual(paper["status"], "draft")
        self.assertEqual(paper["phase"], "draft")
        self.assertTrue(paper["can_edit"])
        self.assertEqual(paper["duration_minutes"], 90)
        self.assertEqual(paper["total_points"], 0)
        self.assertEqual(paper["course"]["name"], "涉税风险识别")
        self.assertEqual(paper["org_id"], ORG)
        self.assertEqual(paper["owner_name"], "考试管理员")

    def test_question_validation_rejects_bad_payloads(self):
        paper = self.create().json()["paper"]
        cases = [
            ({**Q1, "kind": "essay"}, "不支持的题型"),
            ({**Q1, "answer_keys": ["Z"]}, "答案不在选项里"),
            ({**Q1, "answer_keys": ["A", "B"]}, "单选只能一个答案"),
            ({**Q2, "answer_keys": ["A"]}, "多选至少两个答案"),
            ({**Q1, "points": 0}, "分值必须大于 0"),
            ({**Q1, "options": [{"key": "A", "text": "唯一选项"}]}, "至少两个选项"),
        ]
        for index, (question, hint) in enumerate(cases):
            response = self.questions(paper["id"], [question])
            self.assertEqual(response.status_code, 422, f"第{index}组应被拦：{hint} · {response.text}")

    def test_publish_requires_questions_and_roster(self):
        paper = self.create().json()["paper"]
        no_question = self.publish(paper["id"])
        self.assertEqual(no_question.status_code, 422, response_text(no_question))
        self.assertIn("还没有题目", no_question.text)
        self.assertEqual(self.questions(paper["id"], [Q1, Q2]).status_code, 200)
        no_roster = self.publish(paper["id"])
        self.assertEqual(no_roster.status_code, 422)
        self.assertIn("考生名单是空的", no_roster.text)
        self.assertEqual(self.roster(paper["id"], class_ids=["c-1"]).status_code, 200)
        published = self.publish(paper["id"])
        self.assertEqual(published.status_code, 200, published.text)
        body = published.json()["paper"]
        self.assertEqual(body["status"], "published")
        self.assertEqual(body["phase"], "open")
        self.assertEqual(body["total_points"], 6)
        self.assertEqual(body["roster_count"], 2)
        self.assertTrue(body["fingerprint"])
        self.assertEqual(body["frozen"]["question_count"], 2)

    def _seed_bank(self, question_id: str = "bq-approved", stem: str = "真题题干", status: str = "approved"):
        with self.store.connect() as db:
            db.execute("""INSERT OR IGNORE INTO certificate (id,code,name,description,subjects_json,
                          source_ref,active,created_at,updated_at)
                          VALUES (?,?,?,?,?,?,1,?,?)""",
                       ("cert-1", "cjkj", "初级会计职称", "测试用证书", "[]", "test",
                        "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
            db.execute("""INSERT OR IGNORE INTO training_source_authorization
                          (id,certificate_id,licensor,auth_no,question_types,valid_from,valid_until,
                           scope_note,status,created_by,created_at,updated_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       ("auth-1", "cert-1", "财政部考办", "SW-2026-001", "real", "2026-01-01",
                        None, "教学使用", "active", STAFF_ID,
                        "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
            db.execute("""INSERT OR REPLACE INTO training_bank_question
                          (id,certificate_id,source_type,year,question_no,stem,options_json,
                           answer_keys_json,explanation,source_ref,auth_id,status,created_at,updated_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (question_id, "cert-1", "real", 2026, "1", stem,
                        json.dumps([{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}],
                                   ensure_ascii=False),
                        json.dumps(["A"], ensure_ascii=False), "解析", "2026 真题", "auth-1", status,
                        "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))

    def test_published_paper_is_frozen_even_if_bank_changes(self):
        """题目进卷即快照：发布后题库再改，已发布卷面一个字都不许变。"""
        self._seed_bank(stem="真题题干（原始）")
        paper = self.create().json()["paper"]
        picked = self.client.post(f"/api/training/exams/papers/{paper['id']}/questions/from-bank",
                                  json={"college": COLLEGE, "question_ids": ["bq-approved"]})
        self.assertEqual(picked.status_code, 200, picked.text)
        self.assertEqual(self.roster(paper["id"], class_ids=["c-1"]).status_code, 200)
        published = self.publish(paper["id"]).json()["paper"]
        fingerprint, snapshot = published["fingerprint"], published["questions"]
        self.assertEqual(snapshot[0]["stem"], "真题题干（原始）")
        self.assertEqual(snapshot[0]["source"], "bank")
        # 题库后来被改写、被驳回，甚至整行删掉都不影响卷面
        with self.store.connect() as db:
            db.execute("UPDATE training_bank_question SET stem='直接改库', status='rejected'"
                       " WHERE id='bq-approved'")
        after = self.detail(paper["id"]).json()["paper"]
        self.assertEqual(after["fingerprint"], fingerprint)
        self.assertEqual(after["questions"], snapshot)
        self.assertEqual(after["questions"][0]["stem"], "真题题干（原始）")
        self.assertEqual(after["total_points"], 2)
        self.assertFalse(after["can_edit"])

    def test_published_paper_rejects_content_changes(self):
        paper = self.create().json()["paper"]
        self.assertEqual(self.questions(paper["id"], [Q1]).status_code, 200)
        self.assertEqual(self.roster(paper["id"], student_nos=["20260101"]).status_code, 200)
        self.assertEqual(self.publish(paper["id"]).status_code, 200)
        self.assertEqual(self.questions(paper["id"], [Q2]).status_code, 409, "已发布不能改题目")
        patch_title = self.client.patch(f"/api/training/exams/papers/{paper['id']}",
                                        json={"college": COLLEGE, "title": "改个名字"})
        self.assertEqual(patch_title.status_code, 409, patch_title.text)
        patch_desc = self.client.patch(f"/api/training/exams/papers/{paper['id']}",
                                       json={"college": COLLEGE, "description": "补充说明"})
        self.assertEqual(patch_desc.status_code, 200, patch_desc.text)
        self.assertEqual(patch_desc.json()["paper"]["description"], "补充说明")
        self.assertEqual(self.publish(paper["id"]).status_code, 409, "不能重复发布")

    def test_roster_resolution_and_unresolved_report(self):
        paper = self.create().json()["paper"]
        body = self.roster(paper["id"], class_ids=["c-1"], student_nos=["20260102", "20260103", "20990101"])
        self.assertEqual(body.status_code, 200, body.text)
        result = body.json()["roster"]
        self.assertEqual(result["enrolled"], 2, "整班两人，学号重复不重复计数")
        reasons = {item["student_no"]: item["reason"] for item in result["unresolved"]}
        self.assertIn("20260103", reasons)
        self.assertIn("未关联平台账号", reasons["20260103"])
        self.assertIn("20990101", reasons)
        self.assertIn("查无此学号", reasons["20990101"])
        # 再改一次名单，旧的被整份替换而不是叠加
        again = self.roster(paper["id"], student_ids=["u-stu-1"]).json()["roster"]
        self.assertEqual(again["enrolled"], 1)

    def test_cross_college_and_other_school_staff_are_blocked(self):
        paper = self.create().json()["paper"]
        self.assertEqual(self.detail(paper["id"], college=OTHER).status_code, 403,
                         "别校管理员看本院校卷：403")
        self.client.cookies.clear()
        self.client.cookies.set("taxpearls_staff_session", OTHER_TOKEN)
        self.assertEqual(self.detail(paper["id"], college=OTHER).status_code, 404,
                         "卷不属于他院校：404（不泄露存在性）")
        self.assertEqual(self.create(college=COLLEGE).status_code, 403, "跨校建卷 403")
        self.client.cookies.clear()
        self.assertEqual(self.create().status_code, 401, "未登录 401")

    def test_close_only_after_publish_and_lifecycle_listing(self):
        paper = self.create().json()["paper"]
        self.assertEqual(self.client.post(
            f"/api/training/exams/papers/{paper['id']}/close",
            params={"college": COLLEGE}).status_code, 409, "草稿不能结束")
        self.assertEqual(self.questions(paper["id"], [Q1]).status_code, 200)
        self.assertEqual(self.roster(paper["id"], student_nos=["20260101"]).status_code, 200)
        self.assertEqual(self.publish(paper["id"]).status_code, 200)
        closed = self.client.post(f"/api/training/exams/papers/{paper['id']}/close",
                                  params={"college": COLLEGE})
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["paper"]["phase"], "closed")
        self.assertEqual(self.roster(paper["id"], student_nos=["20260101"]).status_code, 409,
                         "已结束不能再改名单")
        listing = self.client.get("/api/training/exams/papers", params={"college": COLLEGE})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()["papers"]), 1)
        self.assertEqual(listing.json()["papers"][0]["phase"], "closed")

    def test_bank_pick_only_takes_approved_questions(self):
        self._seed_bank(question_id="bq-approved", stem="真题题干", status="approved")
        self._seed_bank(question_id="bq-draft", stem="草稿题", status="draft")
        pick = self.client.get("/api/training/exams/papers/bank-questions", params={"college": COLLEGE})
        self.assertEqual(pick.status_code, 200, pick.text)
        self.assertEqual([q["id"] for q in pick.json()["questions"]], ["bq-approved"])
        paper = self.create().json()["paper"]
        response = self.client.post(
            f"/api/training/exams/papers/{paper['id']}/questions/from-bank",
            json={"college": COLLEGE, "question_ids": ["bq-approved", "bq-draft"]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()["paper"]
        self.assertEqual(body["added"], 1)
        self.assertEqual(body["missing"], ["bq-draft"])
        self.assertEqual(body["questions"][0]["source"], "bank")


class ExamSourceSyncTests(unittest.TestCase):
    """不用 HTTP：直接验 LIMIT 与工具函数（发布把题目复制到卷里，就不该再受题库影响）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "exams-unit.db", keepalive=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_tables_exist_after_migration(self):
        expected = {"online_exam_paper", "online_exam_question", "online_exam_roster",
                    "online_exam_session", "online_exam_answer", "online_exam_event"}

        def tables():
            with self.store.connect() as db:
                rows = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {r["name"] for r in rows if r["name"].startswith("online_exam_")}

        self.assertEqual(tables(), expected)
        # 重复执行幂等（延续既有 additive 迁移的口径）
        with self.store.connect() as db:
            exams.migrate(db)
        self.assertEqual(tables(), expected)


def response_text(response) -> str:
    return response.text[:300]


if __name__ == "__main__":
    unittest.main()
