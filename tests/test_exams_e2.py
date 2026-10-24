"""独立在线考试 · E2（学生机考：会话/限时/保存/交卷/事件/监考台）单测。

守的是六条边界：
1. 剩余时间一律服务端算，到点写接口拒绝并**惰性收卷**（没有定时任务）；
2. 一场考试一人只能开一次会话，重复进入 409，但刷新取卷可以继续答；
3. 取卷**不含**标准答案与解析，成绩按发布策略才下发；
4. 客观题判分是"选择集合完全一致才得分"；
5. 防作弊只记录不判定：切页计数、心跳不计数；
6. 监考台看得到每个考生的状态，教师可强制收卷。
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp import exams
from webapp.storage import Store

COLLEGE = "机考测试学院"
ORG = "org-exam-e2"
STAFF_ID = "cu-e2-admin"
TOKEN = "e2-staff-token"
STUDENT_INFO = "info-e2-1"
STUDENT_USER = "u-e2-stu-1"
STUDENT_NO = "20260101"
STUDENT_TOKEN = "e2-student-token"
# 名单里的第二名考生：已关联平台账号，用来验证监考台的"未进入"状态
SECOND_INFO = "info-e2-2"
SECOND_USER = "u-e2-stu-2"
SECOND_NO = "20260103"
OTHER_INFO = "info-e2-x"  # 学籍没关联平台账号
OUT_INFO = "info-e2-out"
OUT_USER = "u-e2-out"

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


class ExamE2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "exams-e2.db"
        self.env = patch.dict(os.environ, {"TAXPEARLS_DB": str(self.path)})
        self.env.start()
        self.store = Store(self.path, keepalive=True)
        with self.store.connect() as db:
            db.execute("INSERT INTO org_settings (org_id, org_type, display_name, updated_at)"
                       " VALUES (?, 'university', ?, ?)", (ORG, COLLEGE, "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO university_organizations VALUES (?,?)", (COLLEGE, ORG))
            db.execute("INSERT INTO college_user (id,username,password_hash,display_name,college,role,"
                       "active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (STAFF_ID, "e2-admin", "x", "考试管理员", COLLEGE, "admin",
                        "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,"
                       "created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (STUDENT_USER, STUDENT_NO, "x", "甲同学", "student", ORG,
                        "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,"
                       "created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (SECOND_USER, SECOND_NO, "x", "丙同学", "student", ORG,
                        "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,"
                       "created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (OUT_USER, "20260999", "x", "旁听生", "student", ORG,
                        "2026-10-23T00:00:00+00:00"))
            for info_id, name, no, user in ((STUDENT_INFO, "甲同学", STUDENT_NO, STUDENT_USER),
                                            (SECOND_INFO, "丙同学", SECOND_NO, SECOND_USER),
                                            (OTHER_INFO, "乙同学", "20260102", None),
                                            (OUT_INFO, "旁听生", "20260999", OUT_USER)):
                db.execute("""INSERT INTO student_info (id,college,student_no,name,class_name,
                              password_hash,active,created_at) VALUES (?,?,?,?,?,?,1,?)""",
                           (info_id, COLLEGE, no, name, "会计2601", "x", "2026-10-23T00:00:00+00:00"))
                if user:
                    db.execute("INSERT INTO university_identity_links (user_id,student_id) VALUES (?,?)",
                               (user, info_id))
            db.execute("INSERT INTO training_staff_sessions (token_hash,staff_id,expires_at,created_at)"
                       " VALUES (?,?,?,?)",
                       (digest(TOKEN), STAFF_ID, "2099-01-01T00:00:00+00:00", "2026-10-23T00:00:00+00:00"))
            for token, info in ((STUDENT_TOKEN, STUDENT_INFO), ("e2-second-token", SECOND_INFO),
                                ("e2-other-token", OTHER_INFO), ("e2-out-token", OUT_INFO)):
                db.execute("""INSERT INTO training_student_sessions (token_hash,student_id,expires_at,
                              created_at) VALUES (?,?,?,?)""",
                           (digest(token), info, "2099-01-01T00:00:00+00:00", "2026-10-23T00:00:00+00:00"))
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.client.cookies.set("taxpearls_staff_session", TOKEN)
        self.client.cookies.set("taxpearls_training_session", STUDENT_TOKEN)
        # 一门已发布、时长 30 分钟、两道题、名单含甲同学的考试
        paper = self.client.post("/api/training/exams/papers", json={
            "title": "期末测验", "college": COLLEGE, "duration_minutes": 30}).json()["paper"]
        self.paper_id = paper["id"]
        self.client.put(f"/api/training/exams/papers/{self.paper_id}/questions",
                        json={"college": COLLEGE, "questions": [Q1, Q2]})
        self.client.put(f"/api/training/exams/papers/{self.paper_id}/roster",
                        json={"college": COLLEGE, "student_nos": [STUDENT_NO, SECOND_NO]})
        # 乙同学（20260102）学籍没关联平台账号，故意不进名单——test_window_gate 用它测 403
        self.qids = self._question_ids()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = None  # 应用级 Store 的 keepalive 在 TestClient 线程里，不能跨线程 close
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 工具
    def _question_ids(self):
        detail = self.client.get(
            f"/api/training/exams/papers/{self.paper_id}", params={"college": COLLEGE}).json()["paper"]
        return [q["id"] for q in detail["questions"]]

    def publish(self):
        return self.client.post(f"/api/training/exams/papers/{self.paper_id}/publish",
                                params={"college": COLLEGE})

    def start(self):
        return self.client.post(f"/api/training/exams/{self.paper_id}/start")

    def as_student(self, token: str):
        self.client.cookies.set("taxpearls_training_session", token)
        return self

    def force_timeout(self):
        """把会话结束时间拨到过去，模拟"到点了"（不用真等 30 分钟）。"""
        with self.store.connect() as db:
            row = db.execute(
                "SELECT id FROM online_exam_session WHERE paper_id=?", (self.paper_id,)).fetchone()
            db.execute("UPDATE online_exam_session SET ends_at=? WHERE id=?",
                       ((datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds"), row["id"]))
        return row["id"]

    # ---------------------------------------------------------------- 用例
    def test_start_opens_session_with_server_side_clock(self):
        self.assertEqual(self.publish().status_code, 200)
        started = self.start()
        self.assertEqual(started.status_code, 200, started.text)
        session = started.json()["session"]
        self.assertEqual(session["status"], "in_progress")
        self.assertAlmostEqual(session["remaining_seconds"], 30 * 60, delta=5)
        self.assertEqual(self.start().status_code, 409, "重复进入要拦住")

    def test_window_gate_and_roster_gate(self):
        # 未开始：open_at 在未来
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
        paper = self.client.post("/api/training/exams/papers", json={
            "title": "未开始的考试", "college": COLLEGE, "duration_minutes": 20,
            "open_at": future}).json()["paper"]
        self.client.put(f"/api/training/exams/papers/{paper['id']}/questions",
                        json={"college": COLLEGE, "questions": [Q1]})
        self.client.put(f"/api/training/exams/papers/{paper['id']}/roster",
                        json={"college": COLLEGE, "student_nos": [STUDENT_NO]})
        self.assertEqual(self.client.post(
            f"/api/training/exams/papers/{paper['id']}/publish",
            params={"college": COLLEGE}).status_code, 200)
        denied = self.client.post(f"/api/training/exams/{paper['id']}/start")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertIn("尚未开始", denied.text)
        # 不在名单里的学生
        self.as_student("e2-out-token")
        self.assertEqual(self.client.post(f"/api/training/exams/{self.paper_id}/start").status_code, 404)
        # 学籍没关联平台账号
        self.as_student("e2-other-token")
        no_link = self.client.post(f"/api/training/exams/{self.paper_id}/start")
        self.assertEqual(no_link.status_code, 403, no_link.text)
        self.assertIn("没有关联平台账号", no_link.text)
        # 未登录
        self.client.cookies.clear()
        self.assertEqual(self.client.post(f"/api/training/exams/{self.paper_id}/start").status_code, 401)

    def test_take_paper_hides_answers_and_restores_picks(self):
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.start().status_code, 200)
        taken = self.client.get(f"/api/training/exams/{self.paper_id}/paper")
        self.assertEqual(taken.status_code, 200, taken.text)
        body = taken.json()
        for question in body["questions"]:
            self.assertNotIn("answer_keys", question, "取卷绝不能带标准答案")
            self.assertNotIn("explanation", question)
        saved = self.client.post(f"/api/training/exams/{self.paper_id}/answers",
                                 json={"answers": [{"question_id": self.qids[0], "picked": ["A"]},
                                                   {"question_id": self.qids[1], "picked": ["A", "C"]}]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["saved"], 2)
        again = self.client.get(f"/api/training/exams/{self.paper_id}/paper").json()
        picks = {q["id"]: q["picked"] for q in again["questions"]}
        self.assertEqual(picks[self.qids[0]], ["A"])
        self.assertEqual(picks[self.qids[1]], ["A", "C"])
        self.assertEqual(again["session"]["answered"], 2)

    def test_timeout_blocks_writes_and_force_collects(self):
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.start().status_code, 200)
        session_id = self.force_timeout()
        denied = self.client.post(f"/api/training/exams/{self.paper_id}/answers",
                                  json={"answers": [{"question_id": self.qids[0], "picked": ["A"]}]})
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertIn("自动收卷", denied.text)
        with self.store.connect() as db:
            row = db.execute("SELECT status, submitted_at FROM online_exam_session WHERE id=?",
                             (session_id,)).fetchone()
        self.assertEqual(row["status"], "forced", "惰性收卷：到点自动收")
        self.assertIsNotNone(row["submitted_at"])
        self.assertEqual(self.client.post(f"/api/training/exams/{self.paper_id}/submit").status_code, 409)

    def test_submit_scores_and_release_gate(self):
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.start().status_code, 200)
        self.client.post(f"/api/training/exams/{self.paper_id}/answers",
                         json={"answers": [{"question_id": self.qids[0], "picked": ["A"]},
                                           {"question_id": self.qids[1], "picked": ["A"]}]})
        submitted = self.client.post(f"/api/training/exams/{self.paper_id}/submit")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertFalse(submitted.json()["score_visible"], "默认 manual：交卷不立刻给分")
        self.assertIsNone(submitted.json()["score"])
        result = self.client.get(f"/api/training/exams/{self.paper_id}/result").json()
        self.assertFalse(result["released"])
        self.assertIn("尚未发布", result["reason"])
        self.assertEqual(result["questions"], [], "未发布连题目解析都不给")
        with self.store.connect() as db:
            score = db.execute("SELECT score FROM online_exam_session WHERE paper_id=?",
                               (self.paper_id,)).fetchone()["score"]
        self.assertEqual(score, 2.0, "单选对 2 分；多选只选 A 不算对")

    def test_after_submit_policy_shows_score_immediately(self):
        paper = self.client.post("/api/training/exams/papers", json={
            "title": "交卷即看分", "college": COLLEGE, "duration_minutes": 10,
            "answer_release": "after_submit"}).json()["paper"]
        self.client.put(f"/api/training/exams/papers/{paper['id']}/questions",
                        json={"college": COLLEGE, "questions": [Q1, Q2]})
        self.client.put(f"/api/training/exams/papers/{paper['id']}/roster",
                        json={"college": COLLEGE, "student_nos": [STUDENT_NO]})
        self.assertEqual(self.client.post(f"/api/training/exams/papers/{paper['id']}/publish",
                                          params={"college": COLLEGE}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/training/exams/{paper['id']}/start").status_code, 200)
        qids = [q["id"] for q in self.client.get(
            f"/api/training/exams/papers/{paper['id']}", params={"college": COLLEGE}).json()["paper"]["questions"]]
        self.client.post(f"/api/training/exams/{paper['id']}/answers",
                         json={"answers": [{"question_id": qids[0], "picked": ["A"]},
                                           {"question_id": qids[1], "picked": ["A", "C"]}]})
        submitted = self.client.post(f"/api/training/exams/{paper['id']}/submit").json()
        self.assertTrue(submitted["score_visible"])
        self.assertEqual(submitted["score"], 6.0)
        result = self.client.get(f"/api/training/exams/{paper['id']}/result").json()
        self.assertTrue(result["released"])
        self.assertEqual(len(result["questions"]), 2)
        self.assertTrue(all(q["correct"] for q in result["questions"]))

    def test_events_count_violations_but_heartbeat_does_not(self):
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.start().status_code, 200)
        posted = self.client.post(f"/api/training/exams/{self.paper_id}/events", json={
            "events": [{"kind": "blur"}, {"kind": "visibility"}, {"kind": "copy", "detail": {"key": "A"}},
                       {"kind": "heartbeat"}]})
        self.assertEqual(posted.status_code, 200, posted.text)
        self.assertEqual(posted.json()["recorded"], 4)
        self.assertEqual(posted.json()["violations"], 3, "心跳不算异常")
        beat = self.client.post(f"/api/training/exams/{self.paper_id}/heartbeat")
        self.assertEqual(beat.status_code, 200, beat.text)
        self.assertGreater(beat.json()["remaining_seconds"], 0)
        with self.store.connect() as db:
            row = db.execute("SELECT violations, last_heartbeat_at FROM online_exam_session"
                             " WHERE paper_id=?", (self.paper_id,)).fetchone()
            events = db.execute("SELECT count(*) n FROM online_exam_event").fetchone()["n"]
        self.assertEqual(row["violations"], 3)
        self.assertIsNotNone(row["last_heartbeat_at"])
        self.assertEqual(events, 4)

    def test_proctor_desk_and_force_submit(self):
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.start().status_code, 200)
        self.client.post(f"/api/training/exams/{self.paper_id}/answers",
                         json={"answers": [{"question_id": self.qids[0], "picked": ["A"]}]})
        self.client.post(f"/api/training/exams/{self.paper_id}/events",
                         json={"events": [{"kind": "blur"}]})
        desk = self.client.get(f"/api/training/exams/papers/{self.paper_id}/sessions",
                               params={"college": COLLEGE})
        self.assertEqual(desk.status_code, 200, desk.text)
        body = desk.json()
        self.assertEqual(body["summary"]["roster"], 2)
        self.assertEqual(body["summary"]["in_progress"], 1)
        self.assertEqual(body["summary"]["not_started"], 1)
        self.assertEqual(body["summary"]["violations"], 1)
        mine = next(p for p in body["people"] if p["student_no"] == STUDENT_NO)
        self.assertEqual(mine["status"], "in_progress")
        self.assertEqual(mine["answered"], 1)
        self.assertEqual(mine["question_count"], 2)
        self.assertIsNotNone(mine["remaining_seconds"])
        forced = self.client.post(
            f"/api/training/exams/papers/{self.paper_id}/sessions/{mine['student_id']}/force-submit",
            params={"college": COLLEGE})
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertEqual(forced.json()["status"], "forced")
        self.assertEqual(forced.json()["score"], 2.0)
        again = self.client.post(
            f"/api/training/exams/papers/{self.paper_id}/sessions/{mine['student_id']}/force-submit",
            params={"college": COLLEGE})
        self.assertEqual(again.status_code, 409, "已经交过就不能再收")
        # 学生视角：被强制收卷后不能继续作答
        self.assertEqual(self.client.post(f"/api/training/exams/{self.paper_id}/answers",
                                          json={"answers": []}).status_code, 409)

    def test_my_exams_lists_only_own(self):
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.start().status_code, 200)
        mine = self.client.get("/api/training/exams/my").json()["exams"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["id"], self.paper_id)
        self.assertEqual(mine[0]["session_status"], "in_progress")
        self.assertIsNotNone(mine[0]["remaining_seconds"])
        # 旁听生不在名单里，列表为空
        self.as_student("e2-out-token")
        self.assertEqual(self.client.get("/api/training/exams/my").json()["exams"], [])

    def test_closed_exam_rejects_late_writes(self):
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.start().status_code, 200)
        self.assertEqual(self.client.post(f"/api/training/exams/papers/{self.paper_id}/close",
                                          params={"college": COLLEGE}).status_code, 200)
        denied = self.client.post(f"/api/training/exams/{self.paper_id}/answers",
                                  json={"answers": [{"question_id": self.qids[0], "picked": ["A"]}]})
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertIn("已结束", denied.text)


class ExamE2ShuffleTests(unittest.TestCase):
    """乱序必须稳定：同一场考试多次取卷顺序不能变（否则重连后题号对不上答案）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "exams-e2-shuffle.db", keepalive=True)
        with self.store.connect() as db:
            db.execute("INSERT INTO org_settings (org_id, org_type, display_name, updated_at)"
                       " VALUES (?, 'university', ?, ?)", (ORG, COLLEGE, "2026-10-23T00:00:00+00:00"))
            db.execute("INSERT INTO university_organizations VALUES (?,?)", (COLLEGE, ORG))
            db.execute("INSERT INTO college_user (id,username,password_hash,display_name,college,role,"
                       "active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                       (STAFF_ID, "e2-admin", "x", "管理员", COLLEGE, "admin", "2026-10-23T00:00:00+00:00"))
            # 先建母卷再插题目：online_exam_question.paper_id 有外键
            db.execute("""INSERT INTO online_exam_paper (id,org_id,college,owner_id,title,
                          duration_minutes,shuffle_questions,shuffle_options,total_points,answer_release,
                          status,created_at,updated_at,created_by)
                          VALUES (?,?,?,?,?,?,1,1,3,'manual','published',?,?,?)""",
                       ("p-1", ORG, COLLEGE, STAFF_ID, "乱序卷", 30,
                        "2026-10-23T00:00:00+00:00", "2026-10-23T00:00:00+00:00", STAFF_ID))
            for position, stem in ((1, "第一题"), (2, "第二题"), (3, "第三题")):
                db.execute("""INSERT INTO online_exam_question
                              (id,paper_id,position,kind,stem,options_json,answer_keys_json,points,
                               explanation,source,fingerprint,created_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (f"q{position}", "p-1", position, "single", stem,
                            json.dumps([{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}],
                                       ensure_ascii=False),
                            json.dumps(["A"], ensure_ascii=False), 1, "", "manual", "fp",
                            "2026-10-23T00:00:00+00:00"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_order_is_stable_within_one_session(self):
        with self.store.connect() as db:
            paper = exams._paper_row(db, "p-1")  # connect() 给的是上下文管理器，必须进 with 拿连接
            first = [q["id"] for q in exams._ordered_questions(db, paper, "session-1")]
            second = [q["id"] for q in exams._ordered_questions(db, paper, "session-1")]
            other = [q["id"] for q in exams._ordered_questions(db, paper, "session-2")]
            row = db.execute("SELECT * FROM online_exam_question WHERE id='q1'").fetchone()
            opts = [o["key"] for o in exams._view_options(row, "session-1", shuffle=True)]
            opts_again = [o["key"] for o in exams._view_options(row, "session-1", shuffle=True)]
        self.assertEqual(first, second, "同一会话取卷顺序必须一致")
        self.assertEqual(opts, opts_again, "同一会话选项顺序必须一致")
        self.assertEqual(sorted(first), ["q1", "q2", "q3"])
        self.assertNotEqual(first, other, "不同考生的顺序可以不同")


if __name__ == "__main__":
    unittest.main()
