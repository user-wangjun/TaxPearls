"""真实学生试用支撑（第四条就绪）接口测试。

覆盖：学生反馈提交（类别/内容/关联对象校验、作答只能关联本人）、
学生只读本人反馈且不含他人信息、教师按院校查看与处理（跨院校拒绝）、
处理留痕（状态流转/备注/处理人时间）、试用统计（登录/近7日活跃/作答与
满分/题库题作答/知识点覆盖/反馈进度，只读聚合）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store, _now
from webapp.training_portal import CST, register as register_portal
from webapp.training_trial import register as register_trial

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_item4_student"
STAFF_COOKIE = "taxpearls_staff_session"
NOW = "2026-10-18T10:00:00"


class TrainingTrialTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register_portal(app, lambda: self.store, STUDENT_COOKIE)
        register_trial(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        (staff_id, staff2_id, stu1, stu2, stu_other, cert, kp,
         auth_id, qid) = (uuid.uuid4().hex for _ in range(9))
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff2_id, "admin2", _passwords.hash("staff2-pass"), "他校管理员",
                        "他省职院", None, None, None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (stu1, "示例大学", "2026001", "李同学", "会计2601",
                        _passwords.hash("pass1"), None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (stu2, "示例大学", "2026002", "王同学", "会计2601",
                        _passwords.hash("pass2"), None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (stu_other, "他省职院", "2026003", "赵同学", "会计2602",
                        _passwords.hash("pass3"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            db.execute(
                "INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                " VALUES (?,?,?,?,?,1,?,?)",
                (kp, cert, "KP-01", "库存现金", "初级会计实务", NOW, NOW))
            # 题库题与授权（反馈关联对象用）
            db.execute(
                """INSERT INTO training_source_authorization (id, certificate_id, licensor, auth_no,
                   question_types, valid_from, valid_until, scope_note, status, created_by,
                   created_at, updated_at) VALUES (?,?,?,?,?,?,NULL,'','active',NULL,?,?)""",
                (auth_id, cert, "授权出版方", "AUTH-T-001", "real", "2026-01-01", NOW, NOW))
            db.execute(
                """INSERT INTO training_bank_question (id, certificate_id, source_type, year,
                   question_no, knowledge_point_id, stem, options_json, answer_keys_json,
                   explanation, source_ref, auth_id, status, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'approved', NULL, ?, ?)""",
                (qid, cert, "real", 2026, "一1", kp, "题干", '[{"key":"A","text":"1"}]',
                 '["A"]', "解析", "2026 年真题 · 授权文号 AUTH-T-001", auth_id, NOW, NOW))
            # 作答数据（统计用）：stu1 近 7 日仿真满分 + 真题满分（整证作答不挂知识点）；
            # stu2 仅有一周前的旧作答（活跃口径按近 7 日计）；stu_other 不作答。
            def attempt(aid, student, kp_id, source, perfect, created):
                result = {"perfect": 1 if perfect else 0, "score": 100 if perfect else 0}
                sealed = db.field_codec.seal(json.dumps(result), "training_self_practice_attempts",
                                             "result_json", aid)
                db.execute(
                    """INSERT INTO training_self_practice_attempts (id, student_id, certificate_id,
                       knowledge_point_id, rule_id, seed, level, year, source_type, digest, status,
                       result_json, created_at) VALUES (?,?,?,?,?,1,'normal',2026,?,'d','scored',?,?)""",
                    (aid, student, cert, kp_id, "r-1" if source == "simulated" else "", source,
                     sealed, created))
            old = (datetime_now_cst() - timedelta(days=10)).isoformat(timespec="seconds")
            attempt(uuid.uuid4().hex, stu1, kp, "simulated", True, _now())
            attempt(uuid.uuid4().hex, stu1, None, "real", True, _now())
            attempt(uuid.uuid4().hex, stu2, kp, "simulated", True, old)
            # 登录会话：stu1、stu2 登录过
            future = (datetime_now_cst() + timedelta(days=1)).isoformat(timespec="seconds")
            db.execute("INSERT INTO training_student_sessions VALUES ('h1',?,?,?)",
                       (stu1, future, _now()))
            db.execute("INSERT INTO training_student_sessions VALUES ('h2',?,?,?)",
                       (stu2, future, _now()))
        self.staff_id, self.staff2_id = staff_id, staff2_id
        self.stu1, self.stu2, self.stu_other = stu1, stu2, stu_other
        self.cert, self.kp, self.qid = cert, kp, qid
        self.attempt_for_ref = self._first_attempt(stu1)

    def _first_attempt(self, student_id):
        with self.store.connect() as db:
            return db.execute(
                "SELECT id FROM training_self_practice_attempts WHERE student_id=? LIMIT 1",
                (student_id,)).fetchone()["id"]

    def _student_login(self, student_no, password):
        res = self.client.post("/api/training/auth/login",
                               json={"student_no": student_no, "password": password,
                                     "college": "示例大学" if student_no != "2026003" else "他省职院"})
        self.assertEqual(res.status_code, 200, res.text)

    def _staff_login(self, username="admin1", password="staff-pass"):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": username, "password": password})
        self.assertEqual(res.status_code, 200, res.text)

    def _create(self, **overrides):
        body = {"category": "bug", "content": "练习页提交后解析显示不全",
                "certificate_id": self.cert} | overrides
        return self.client.post("/api/training/my/feedback", json=body)

    # ---- 学生反馈 ---------------------------------------------------------

    def test_student_feedback_validation_and_refs(self):
        self._student_login("2026001", "pass1")
        created = self._create(ref_type="kp", ref_id=self.kp)
        self.assertEqual(created.status_code, 200, created.text)
        view = created.json()["feedback"]
        self.assertEqual(view["status"], "new")
        self.assertNotIn("student_id", view)          # 学生视图不含身份标识
        # 类别 / 内容 / 关联对象校验
        self.assertEqual(self._create(category="wrong").status_code, 422)
        self.assertEqual(self._create(content="  ").status_code, 422)
        self.assertEqual(self._create(content="x" * 1001).status_code, 422)
        self.assertEqual(self._create(ref_type="unknown").status_code, 422)
        self.assertEqual(self._create(ref_type="kp", ref_id="").status_code, 422)
        self.assertEqual(self._create(ref_type="kp", ref_id=uuid.uuid4().hex).status_code, 404)
        self.assertEqual(self._create(ref_type="attempt",
                                      ref_id=uuid.uuid4().hex).status_code, 404)
        # 作答只能关联本人；题库题关联须存在
        others = self._first_attempt(self.stu2)
        self.assertEqual(self._create(ref_type="attempt", ref_id=others).status_code, 404)
        self.assertEqual(self._create(ref_type="attempt",
                                      ref_id=self.attempt_for_ref).status_code, 200)
        self.assertEqual(self._create(ref_type="question", ref_id=self.qid).status_code, 200)
        self.assertEqual(self._create(ref_type="question",
                                      ref_id=uuid.uuid4().hex).status_code, 404)
        # ref_type=none 时忽略传入的 ref_id
        none_ref = self._create(ref_type="none", ref_id=self.kp)
        self.assertEqual(none_ref.json()["feedback"]["ref_id"], "")

    def test_student_sees_only_own_feedback(self):
        self._student_login("2026001", "pass1")
        first = self._create().json()["feedback"]["id"]
        self.client.post("/api/training/auth/logout")
        self._student_login("2026002", "pass2")
        second = self._create(content="希望增加回忆题数量").json()["feedback"]["id"]
        mine_list = self.client.get("/api/training/my/feedback").json()["feedback"]
        # 只读到本人那一条，另一位学生的反馈不可见
        self.assertEqual(len(mine_list), 1)
        self.assertEqual(mine_list[0]["id"], second)
        self.assertNotIn(first, [r["id"] for r in mine_list])

    # ---- 教师处理 ---------------------------------------------------------

    def test_staff_feedback_college_scope_and_handle(self):
        self._student_login("2026001", "pass1")
        fid = self._create().json()["feedback"]["id"]
        self.client.post("/api/training/auth/logout")
        self._student_login("2026003", "pass3")
        other_fid = self._create(content="他校学生反馈").json()["feedback"]["id"]
        # 本校教师只能看到本校学生反馈
        self._staff_login()
        rows = self.client.get("/api/training/staff/trial-feedback").json()["feedback"]
        self.assertEqual([r["id"] for r in rows], [fid])
        self.assertEqual(rows[0]["student"]["student_no"], "2026001")
        self.assertEqual(rows[0]["student"]["name"], "李同学")
        # 过滤器
        rows = self.client.get(
            "/api/training/staff/trial-feedback?category=suggestion").json()["feedback"]
        self.assertEqual(rows, [])
        # 跨院校处理被拒
        self._staff_login("admin2", "staff2-pass")
        res = self.client.patch(f"/api/training/staff/trial-feedback/{fid}",
                                json={"status": "processing"})
        self.assertEqual(res.status_code, 403)
        rows = self.client.get("/api/training/staff/trial-feedback").json()["feedback"]
        self.assertEqual([r["id"] for r in rows], [other_fid])
        # 本校教师处理：new → processing → resolved，处理留痕
        self._staff_login()
        empty = self.client.patch(f"/api/training/staff/trial-feedback/{fid}", json={})
        self.assertEqual(empty.status_code, 422)
        bad = self.client.patch(f"/api/training/staff/trial-feedback/{fid}",
                                json={"status": "closed"})
        self.assertEqual(bad.status_code, 422)
        step1 = self.client.patch(f"/api/training/staff/trial-feedback/{fid}",
                                  json={"status": "processing", "staff_note": "已定位，修复中"})
        self.assertEqual(step1.status_code, 200, step1.text)
        self.assertEqual(step1.json()["feedback"]["status"], "processing")
        self.assertEqual(step1.json()["feedback"]["handled_by"], self.staff_id)
        self.assertIsNotNone(step1.json()["feedback"]["handled_at"])
        step2 = self.client.patch(f"/api/training/staff/trial-feedback/{fid}",
                                  json={"status": "resolved", "staff_note": "已修复并上线"})
        self.assertEqual(step2.json()["feedback"]["status"], "resolved")
        # 仅更新备注不改状态：处理人与时间保留
        note_only = self.client.patch(f"/api/training/staff/trial-feedback/{fid}",
                                      json={"staff_note": "最终备注"})
        self.assertEqual(note_only.json()["feedback"]["status"], "resolved")
        self.assertEqual(note_only.json()["feedback"]["staff_note"], "最终备注")
        missing = self.client.patch(f"/api/training/staff/trial-feedback/{uuid.uuid4().hex}",
                                    json={"status": "resolved"})
        self.assertEqual(missing.status_code, 404)

    # ---- 试用统计 ---------------------------------------------------------

    def test_trial_stats_aggregation(self):
        self._student_login("2026001", "pass1")
        self._create(ref_type="kp", ref_id=self.kp)          # new
        self._create(ref_type="attempt", ref_id=self.attempt_for_ref,
                     category="content")                     # new
        self._staff_login()
        stats = self.client.get("/api/training/staff/trial-stats").json()["stats"]
        self.assertEqual(stats["college"], "示例大学")
        self.assertEqual(stats["students_total"], 2)          # 仅本校学生
        self.assertEqual(stats["students_logged_in"], 2)
        self.assertEqual(stats["students_active_7d"], 1)      # 仅 stu1 近 7 日有作答
        self.assertEqual(stats["attempts_total"], 3)          # 累计作答（含 10 天前）
        self.assertEqual(stats["attempts_perfect"], 3)
        self.assertEqual(stats["attempts_bank"], 1)           # 真题作答占比数据源
        self.assertEqual(stats["kps_covered"], 1)             # 真题整证作答不计知识点
        self.assertEqual(stats["feedback"], {"total": 2, "new": 2,
                                             "processing": 0, "resolved": 0})
        # 处理一条后进度变化；他校统计不受影响
        rows = self.client.get(
            "/api/training/staff/trial-feedback?category=content").json()["feedback"]
        self.assertEqual(len(rows), 1)
        self.client.patch(f"/api/training/staff/trial-feedback/{rows[0]['id']}",
                          json={"status": "resolved"})
        stats = self.client.get("/api/training/staff/trial-stats").json()["stats"]
        self.assertEqual(stats["feedback"], {"total": 2, "new": 1,
                                             "processing": 0, "resolved": 1})
        self._staff_login("admin2", "staff2-pass")
        other = self.client.get("/api/training/staff/trial-stats").json()["stats"]
        self.assertEqual(other["college"], "他省职院")
        self.assertEqual(other["students_total"], 1)
        self.assertEqual(other["attempts_total"], 0)
        self.assertEqual(other["feedback"]["total"], 0)


def datetime_now_cst():
    from datetime import datetime
    return datetime.now(CST)


if __name__ == "__main__":
    unittest.main()
