"""独立在线考试 · E3（判分复核 / 成绩发布 / 成绩单导出）单测。

守的五条边界：
1. 复核只改**这一题**的人工分并重算整卷，`score`（自动分）永不覆写；
2. 一条人工分都没有时 `adjusted_score` 必须是 NULL——不能凭空造出"复核过"的错觉；
3. 成绩发布前必须先结束考试，否则考生交卷即见分；
4. 撤回发布后学生立刻看不到成绩，不需要重开考试；
5. 成绩单 CSV 的最终成绩与监考台、学生端同源（复核分优先），且带 BOM 能被 Excel 直读。
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
from webapp.storage import Store

COLLEGE = "机考E3学院"
OTHER_COLLEGE = "别的学院"
ORG = "org-exam-e3"
STAFF_ID = "cu-e3-admin"
TOKEN = "e3-staff-token"
INFO_A, USER_A, NO_A, TOKEN_A = "info-e3-a", "u-e3-a", "20260201", "e3-token-a"
INFO_B, USER_B, NO_B, TOKEN_B = "info-e3-b", "u-e3-b", "20260202", "e3-token-b"

Q1 = {"kind": "single", "stem": "销售货物适用税率？",
      "options": [{"key": "A", "text": "13%"}, {"key": "B", "text": "6%"}],
      "answer_keys": ["A"], "points": 2, "explanation": "销售货物 13%"}
Q2 = {"kind": "multiple", "stem": "视同销售的有？",
      "options": [{"key": "A", "text": "自产货物用于集体福利"},
                  {"key": "B", "text": "购进货物用于免税项目"},
                  {"key": "C", "text": "自产货物无偿赠送"}],
      "answer_keys": ["A", "C"], "points": 4}


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ExamE3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "exams-e3.db"
        self.env = patch.dict(os.environ, {"TAXPEARLS_DB": str(self.path)})
        self.env.start()
        self.store = Store(self.path, keepalive=True)
        stamp = "2026-10-26T00:00:00+00:00"
        with self.store.connect() as db:
            db.execute("INSERT INTO org_settings (org_id, org_type, display_name, updated_at)"
                       " VALUES (?, 'university', ?, ?)", (ORG, COLLEGE, stamp))
            db.execute("INSERT INTO university_organizations VALUES (?,?)", (COLLEGE, ORG))
            db.execute("INSERT INTO college_user (id,username,password_hash,display_name,college,role,"
                       "active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (STAFF_ID, "e3-admin", "x", "复核教师", COLLEGE, "admin", stamp))
            for info, user, no, name in ((INFO_A, USER_A, NO_A, "甲同学"), (INFO_B, USER_B, NO_B, "乙同学")):
                db.execute("INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,"
                           "created_at) VALUES (?,?,?,?,?,?,1,?)",
                           (user, no, "x", name, "student", ORG, stamp))
                db.execute("""INSERT INTO student_info (id,college,student_no,name,class_name,
                              password_hash,active,created_at) VALUES (?,?,?,?,?,?,1,?)""",
                           (info, COLLEGE, no, name, "会计2601", "x", stamp))
                db.execute("INSERT INTO university_identity_links (user_id,student_id) VALUES (?,?)",
                           (user, info))
            db.execute("INSERT INTO training_staff_sessions (token_hash,staff_id,expires_at,created_at)"
                       " VALUES (?,?,?,?)", (digest(TOKEN), STAFF_ID, "2099-01-01T00:00:00+00:00", stamp))
            for token, info in ((TOKEN_A, INFO_A), (TOKEN_B, INFO_B)):
                db.execute("""INSERT INTO training_student_sessions (token_hash,student_id,expires_at,
                              created_at) VALUES (?,?,?,?)""",
                           (digest(token), info, "2099-01-01T00:00:00+00:00", stamp))
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.client.cookies.set("taxpearls_staff_session", TOKEN)
        paper = self.client.post("/api/training/exams/papers", json={
            "title": "期中测验", "college": COLLEGE, "duration_minutes": 30,
            "pass_score": 4, "answer_release": "manual"}).json()["paper"]
        self.paper_id = paper["id"]
        self.client.put(f"/api/training/exams/papers/{self.paper_id}/questions",
                        json={"college": COLLEGE, "questions": [Q1, Q2]})
        self.client.put(f"/api/training/exams/papers/{self.paper_id}/roster",
                        json={"college": COLLEGE, "student_nos": [NO_A, NO_B]})
        self.qids = [q["id"] for q in self.client.get(
            f"/api/training/exams/papers/{self.paper_id}",
            params={"college": COLLEGE}).json()["paper"]["questions"]]
        self.assertEqual(self.client.post(f"/api/training/exams/papers/{self.paper_id}/publish",
                                          params={"college": COLLEGE}).status_code, 200)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = None  # 应用级 Store 的 keepalive 在 TestClient 线程里，不能跨线程 close
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 工具
    def as_student(self, token: str):
        self.client.cookies.set("taxpearls_training_session", token)
        return self

    def sit(self, token: str, picks: list[list[str]]):
        """让某个学生把这场考试答完并交卷。"""
        self.as_student(token)
        self.assertEqual(self.client.post(f"/api/training/exams/{self.paper_id}/start").status_code, 200)
        self.client.post(f"/api/training/exams/{self.paper_id}/answers",
                         json={"answers": [{"question_id": qid, "picked": picked}
                                           for qid, picked in zip(self.qids, picks)]})
        submitted = self.client.post(f"/api/training/exams/{self.paper_id}/submit")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        return submitted.json()

    def close(self):
        return self.client.post(f"/api/training/exams/papers/{self.paper_id}/close",
                                params={"college": COLLEGE})

    def release(self, release=True):
        return self.client.post(f"/api/training/exams/papers/{self.paper_id}/release",
                                json={"college": COLLEGE, "release": release})

    def review(self, student_id: str, question_id: str, score, note=None):
        return self.client.post(
            f"/api/training/exams/papers/{self.paper_id}/sessions/{student_id}"
            f"/answers/{question_id}/review",
            json={"college": COLLEGE, "score": score, "note": note})

    def result(self, token: str):
        self.as_student(token)
        return self.client.get(f"/api/training/exams/{self.paper_id}/result")

    def session_row(self, student_id: str):
        with self.store.connect() as db:
            return dict(db.execute(
                "SELECT score, adjusted_score FROM online_exam_session WHERE paper_id=? AND student_id=?",
                (self.paper_id, student_id)).fetchone())

    # ---------------------------------------------------------------- 用例
    def test_auto_score_without_review_leaves_adjusted_null(self):
        self.sit(TOKEN_A, [["A"], ["A"]])          # Q1 对(2) / Q2 错(0) → 自动 2
        self.assertIsNone(self.session_row(USER_A)["adjusted_score"],
                          "没复核过就不该有「复核后分数」")
        self.assertEqual(self.session_row(USER_A)["score"], 2)

    def test_review_changes_only_that_question_and_recalculates(self):
        self.sit(TOKEN_A, [["A"], ["A"]])
        # Q2 漏选了 C，教师按半分给 2 分
        reviewed = self.review(USER_A, self.qids[1], 2, "漏选 C，给一半分")
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        body = reviewed.json()
        self.assertEqual((body["auto_score"], body["score"], body["reviewed"]), (2.0, 4.0, 1))
        row = self.session_row(USER_A)
        self.assertEqual((row["score"], row["adjusted_score"]), (2, 4), "自动分不可被覆写")
        # 撤销人工分 → 回到自动判分，adjusted 回到 NULL
        undone = self.review(USER_A, self.qids[1], None)
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(undone.json()["score"], 2.0)
        self.assertIsNone(self.session_row(USER_A)["adjusted_score"])
        # 审计留痕：每次复核一条 event
        with self.store.connect() as db:
            events = db.execute(
                "SELECT count(*) n FROM online_exam_event WHERE kind='review'").fetchone()["n"]
        self.assertEqual(events, 2)

    def test_review_rejects_out_of_range_and_unfinished(self):
        self.as_student(TOKEN_A)
        self.client.post(f"/api/training/exams/{self.paper_id}/start")
        self.client.post(f"/api/training/exams/{self.paper_id}/answers",
                         json={"answers": [{"question_id": self.qids[0], "picked": ["A"]}]})
        still_open = self.review(USER_A, self.qids[0], 1)
        self.assertEqual(still_open.status_code, 409, still_open.text)
        self.assertIn("还在作答", still_open.text)
        self.client.post(f"/api/training/exams/{self.paper_id}/submit")
        self.assertEqual(self.review(USER_A, self.qids[0], 3).status_code, 422, "超过本题满分要拒")
        self.assertEqual(self.review(USER_A, self.qids[0], -1).status_code, 422)
        self.assertEqual(self.review(USER_A, "no-such-question", 1).status_code, 404)
        self.assertEqual(self.review("no-such-student", self.qids[0], 1).status_code, 404)

    def test_release_requires_closed_exam_then_opens_and_revokes(self):
        self.sit(TOKEN_A, [["A"], ["A", "C"]])
        early = self.release()
        self.assertEqual(early.status_code, 409, early.text)
        self.assertIn("先结束考试", early.text)
        self.assertFalse(self.result(TOKEN_A).json()["released"])
        self.assertEqual(self.close().status_code, 200)
        self.assertEqual(self.release().status_code, 200)
        opened = self.result(TOKEN_A).json()
        self.assertTrue(opened["released"])
        self.assertEqual(opened["score"], 6.0)
        self.assertTrue(opened["passed"])
        self.assertEqual(self.release(False).status_code, 200)
        self.assertFalse(self.result(TOKEN_A).json()["released"], "撤回后立刻看不到")

    def test_student_sees_reviewed_score_not_auto_score(self):
        self.sit(TOKEN_A, [["A"], ["A"]])
        self.review(USER_A, self.qids[1], 4, "补全给满分")
        self.close()
        self.release()
        body = self.result(TOKEN_A).json()
        self.assertEqual(body["score"], 6.0, "学生看到的必须是复核后的最终分")
        self.assertEqual(body["auto_score"], 2.0)
        self.assertTrue(body["reviewed"])
        detail = {q["id"]: q for q in body["questions"]}
        self.assertEqual(detail[self.qids[1]]["points_gained"], 0, "自动判分原样保留")
        self.assertEqual(detail[self.qids[1]]["manual_score"], 4)
        self.assertEqual(detail[self.qids[1]]["gain"], 4, "学生看到的得分是复核分")

    def test_scores_csv_matches_same_source_and_has_bom(self):
        self.sit(TOKEN_A, [["A"], ["A"]])
        self.sit(TOKEN_B, [["A"], ["A", "C"]])
        self.review(USER_A, self.qids[1], 3, "部分给分")
        self.close()
        self.release()
        response = self.client.get(f"/api/training/exams/papers/{self.paper_id}/scores.csv",
                                   params={"college": COLLEGE})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.text.startswith("\ufeff"), "Excel 直读需要 BOM")
        self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])
        lines = [line for line in response.text.lstrip("\ufeff").splitlines() if not line.startswith("#")]
        self.assertEqual(lines[0], "学号,姓名,状态,自动判分,复核后分数,最终成绩,是否及格,交卷时间,违规次数")
        rows = {line.split(",")[0]: line.split(",") for line in lines[1:]}
        self.assertEqual(rows[NO_A][2], "已交卷")
        self.assertEqual(rows[NO_A][3:7], ["2.0", "5.0", "5.0", "是"],
                         "自动分 2 / 复核后整卷 2+3=5 / 最终取复核")
        self.assertEqual(rows[NO_B][3:7], ["6.0", "", "6.0", "是"], "未复核时复核列留空")
        self.assertEqual(rows[NO_A][8], "0")

    def test_csv_lists_absent_students_and_rejects_other_college(self):
        self.sit(TOKEN_A, [["A"], ["A", "C"]])
        self.close()
        response = self.client.get(f"/api/training/exams/papers/{self.paper_id}/scores.csv",
                                   params={"college": COLLEGE})
        rows = {line.split(",")[0]: line.split(",")
                for line in response.text.lstrip("\ufeff").splitlines() if not line.startswith("#")}
        self.assertEqual(rows[NO_B][2], "未参加", "没来考的人也要出现在成绩单上")
        self.assertEqual(rows[NO_B][5], "", "未参加不给分数")
        denied = self.client.get(f"/api/training/exams/papers/{self.paper_id}/scores.csv",
                                 params={"college": OTHER_COLLEGE})
        self.assertEqual(denied.status_code, 403)
        self.client.cookies.delete("taxpearls_staff_session")
        self.assertEqual(self.client.get(
            f"/api/training/exams/papers/{self.paper_id}/scores.csv",
            params={"college": COLLEGE}).status_code, 401)

    def test_session_answers_exposes_keys_and_manual_fields(self):
        self.sit(TOKEN_A, [["A"], ["A"]])
        detail = self.client.get(
            f"/api/training/exams/papers/{self.paper_id}/sessions/{USER_A}/answers",
            params={"college": COLLEGE})
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual(body["student"]["student_no"], NO_A)
        self.assertEqual(body["session"]["score"], 2.0)
        self.assertEqual([q["id"] for q in body["questions"]], self.qids, "按卷面顺序，不随机")
        self.assertEqual(body["questions"][1]["answer_keys"], ["A", "C"])
        self.assertEqual(body["questions"][1]["picked"], ["A"])
        self.assertIsNone(body["questions"][1]["manual_score"])
        self.review(USER_A, self.qids[1], 2, "漏选")
        again = self.client.get(
            f"/api/training/exams/papers/{self.paper_id}/sessions/{USER_A}/answers",
            params={"college": COLLEGE}).json()
        self.assertEqual(again["questions"][1]["manual_score"], 2)
        self.assertEqual(again["questions"][1]["manual_note"], "漏选")
        self.assertEqual(again["session"]["score"], 4.0)


if __name__ == "__main__":
    unittest.main()
