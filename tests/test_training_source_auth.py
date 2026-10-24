"""真题授权登记与题库审核（第四条就绪）接口测试。

覆盖：授权登记/撤销/有效期口径、无授权拒绝发布与导入、题库批量导入
（逐行校验、授权文号自动留痕）、审核流转（草稿→待审→上线/驳回）、
授权到期或撤销后草稿不得再进审核、上线题下架后学生抽不到但历史作答可回看、
题库题作答判分（完全匹配得满分，答案/解析判分前不下发）。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import date
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import register as register_portal
from webapp.training_source_auth import register as register_source_auth

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_item4_student"
STAFF_COOKIE = "taxpearls_staff_session"
NOW = "2026-10-18T10:00:00"
TODAY = date(2026, 10, 18)


def _question(**overrides):
    item = {"source_type": "real", "year": 2026, "question_no": "一、单项选择题第1题",
            "stem": "下列各项中，属于流动资产的是（　）。",
            "options": [{"key": "A", "text": "库存现金"}, {"key": "B", "text": "应付账款"},
                        {"key": "C", "text": "实收资本"}, {"key": "D", "text": "累计折旧"}],
            "answer_keys": ["A"], "explanation": "库存现金属于流动资产，其余为负债或权益类。"}
    item.update(overrides)
    return item


class TrainingSourceAuthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register_portal(app, lambda: self.store, STUDENT_COOKIE)
        register_source_auth(app, lambda: self.store)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        staff_id, staff2_id, student_id, cert, kp = (uuid.uuid4().hex for _ in range(5))
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff2_id, "admin2", _passwords.hash("staff2-pass"), "他校管理员",
                        "他省职院", None, None, None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_id, "示例大学", "2026001", "李同学", "会计2601",
                        _passwords.hash("study-pass"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            db.execute(
                "INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                " VALUES (?,?,?,?,?,1,?,?)",
                (kp, cert, "KP-01", "库存现金", "初级会计实务", NOW, NOW))
        self.staff_id, self.staff2_id = staff_id, staff2_id
        self.student_id, self.cert, self.kp = student_id, cert, kp

    def _staff_login(self, username="admin1", password="staff-pass"):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": username, "password": password})
        self.assertEqual(res.status_code, 200, res.text)

    def _student_login(self):
        res = self.client.post("/api/training/auth/login",
                               json={"student_no": "2026001", "password": "study-pass",
                                     "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _create_auth(self, **overrides):
        body = {"licensor": "全国会计考办授权出版方", "auth_no": "AUTH-2026-001",
                "question_types": ["real"], "valid_from": "2026-01-01",
                "valid_until": "2027-12-31", "scope_note": "仅限校内教学"}
        body.update(overrides)
        return self.client.post(
            f"/api/training/staff/certificates/{self.cert}/source-authorizations", json=body)

    def _import(self, items, **overrides):
        body = {"questions": items} | overrides
        return self.client.post(
            f"/api/training/staff/certificates/{self.cert}/bank-questions", json=body)

    # ---- 授权登记 ---------------------------------------------------------

    def test_authorization_registry_and_validity(self):
        self._staff_login()
        res = self._create_auth()
        self.assertEqual(res.status_code, 200, res.text)
        record = res.json()["authorization"]
        self.assertEqual(record["status"], "active")
        # 文号证书内唯一
        dup = self._create_auth(auth_no="AUTH-2026-001", question_types=["recall"])
        self.assertEqual(dup.status_code, 409)
        # 缺授权方/文号、未知题源、日期倒挂均拒绝
        self.assertEqual(self._create_auth(licensor=" ").status_code, 422)
        self.assertEqual(self._create_auth(auth_no="X", question_types=["simulated"]).status_code, 422)
        self.assertEqual(self._create_auth(auth_no="Y", valid_from="2027-01-01",
                                           valid_until="2026-01-01").status_code, 422)
        # 未生效的授权不覆盖当日（未来生效须同时把失效日放开，否则是日期倒挂）
        future = self._create_auth(auth_no="AUTH-FUTURE", question_types=["recall"],
                                   valid_from="2030-01-01", valid_until="")
        self.assertEqual(future.status_code, 200)
        listing = self.client.get(
            f"/api/training/staff/certificates/{self.cert}/source-authorizations").json()["authorizations"]
        by_no = {a["auth_no"]: a for a in listing}
        self.assertFalse(by_no["AUTH-FUTURE"]["covering_today"])
        self.assertTrue(by_no["AUTH-2026-001"]["covering_today"])
        # 撤销后状态变更且可再撤销为幂等、行保留可审计
        revoked = self.client.post(
            f"/api/training/staff/source-authorizations/{record['id']}/revoke")
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json()["authorization"]["status"], "revoked")
        self.assertEqual(len(listing), 2)

    def test_expired_authorization_blocks_new_publish(self):
        self._staff_login()
        expired = self._create_auth(auth_no="AUTH-OLD", valid_from="2025-01-01",
                                    valid_until="2026-09-30")
        self.assertEqual(expired.status_code, 200)
        res = self._import([_question()])
        self.assertEqual(res.json()["imported"], 0)
        self.assertIn("书面授权", res.json()["results"][0]["error"])

    # ---- 题库导入与审核 ---------------------------------------------------

    def _prepare_bank_question(self, source_type="real", answer_keys=None):
        """登记覆盖全部题源的授权并导入一题，返回题目视图。"""
        self._staff_login()
        auth = self._create_auth(question_types=["real", "recall", "mock"]).json()["authorization"]
        res = self._import([_question(source_type=source_type,
                                      knowledge_point_id=self.kp,
                                      answer_keys=answer_keys or ["A"])])
        self.assertEqual(res.json()["imported"], 1, res.text)
        qid = res.json()["results"][0]["id"]
        return auth, qid

    def test_batch_import_validates_line_by_line(self):
        self._staff_login()
        self._create_auth(question_types=["real", "recall", "mock"])
        res = self._import([
            _question(knowledge_point_id=self.kp),
            _question(source_type="simulated"),           # 仿真题不入题库
            _question(answer_keys=["E"]),                  # 答案含未定义选项键
            _question(explanation=""),                     # 缺解析
            "not-a-dict",                                  # 非 JSON 对象
            _question(knowledge_point_id=self.kp, stem="多选题",
                      answer_keys=["A", "D"]),             # 合法多选
        ])
        payload = res.json()
        self.assertEqual(payload["imported"], 2)
        self.assertEqual(payload["failed"], 4)
        self.assertEqual([r["ok"] for r in payload["results"]],
                         [True, False, False, False, False, True])
        ok_ids = {r["id"] for r in payload["results"] if r["ok"]}
        # 每题自动附带授权文号留痕
        listing = self.client.get(
            f"/api/training/staff/certificates/{self.cert}/bank-questions").json()["questions"]
        self.assertEqual(len(listing), 2)
        for q in listing:
            self.assertIn("AUTH-2026-001", q["source_ref"])
            self.assertIn(q["id"], ok_ids)
            self.assertEqual(q["status"], "draft")
            self.assertIn("answer_keys", q)  # 教师侧可见答案
        # 未知授权 id / 授权不覆盖该题源 / 未知知识点 拒绝
        self.assertEqual(self._import([_question(auth_id=uuid.uuid4().hex)]).json()["failed"], 1)
        limited = self._create_auth(auth_no="AUTH-RECALL", question_types=["recall"])
        self.assertEqual(limited.status_code, 200)
        self.assertEqual(self._import([_question(auth_id=limited.json()["authorization"]["id"])]).json()["failed"], 1)
        self.assertEqual(self._import([_question(knowledge_point_id=uuid.uuid4().hex)]).json()["failed"], 1)

    def test_review_flow_and_authorization_recheck_on_submit(self):
        auth, qid = self._prepare_bank_question()
        # 草稿不能直接审核；待审才能通过
        early = self.client.post(f"/api/training/staff/bank-questions/{qid}/review",
                                 json={"decision": "approve"})
        self.assertEqual(early.status_code, 409)
        submitted = self.client.post(f"/api/training/staff/bank-questions/{qid}/submit")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["question"]["status"], "pending")
        bad = self.client.post(f"/api/training/staff/bank-questions/{qid}/review",
                               json={"decision": "maybe"})
        self.assertEqual(bad.status_code, 422)
        approved = self.client.post(f"/api/training/staff/bank-questions/{qid}/review",
                                    json={"decision": "approve", "note": "核对 2026 真题卷"})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["question"]["status"], "approved")
        # 授权撤销后：存量上线题学生仍可见，但下架回草稿后不得再提交审核
        self.client.post(f"/api/training/staff/source-authorizations/{auth['id']}/revoke")
        res = self._import([_question()])
        self.assertEqual(res.json()["imported"], 0)  # 撤销后不得再导入
        offline = self.client.post(f"/api/training/staff/bank-questions/{qid}/offline")
        self.assertEqual(offline.status_code, 200, offline.text)
        resubmit = self.client.post(f"/api/training/staff/bank-questions/{qid}/submit")
        self.assertEqual(resubmit.status_code, 422)
        self.assertIn("书面授权", resubmit.json()["detail"])

    def test_reject_then_edit_resubmit(self):
        _, qid = self._prepare_bank_question()
        self.client.post(f"/api/training/staff/bank-questions/{qid}/submit")
        rejected = self.client.post(f"/api/training/staff/bank-questions/{qid}/review",
                                    json={"decision": "reject", "note": "选项 B 文字有误"})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["question"]["status"], "rejected")
        # 驳回题可再次提交审核
        resub = self.client.post(f"/api/training/staff/bank-questions/{qid}/submit")
        self.assertEqual(resub.status_code, 200, resub.text)
        self.assertEqual(resub.json()["question"]["status"], "pending")
        self.assertEqual(resub.json()["question"]["review_note"], "选项 B 文字有误")

    # ---- 学生作答闭环 -----------------------------------------------------

    def _approve_one(self, answer_keys=None):
        auth, qid = self._prepare_bank_question(answer_keys=answer_keys)
        self.client.post(f"/api/training/staff/bank-questions/{qid}/submit")
        self.client.post(f"/api/training/staff/bank-questions/{qid}/review",
                         json={"decision": "approve"})
        return qid

    def _start_bank(self, **overrides):
        body = {"certificate_id": self.cert, "knowledge_point_id": self.kp,
                "source_type": "real"} | overrides
        return self.client.post("/api/training/my/practice/start", json=body)

    def test_bank_attempt_scoring_and_answer_hiding(self):
        qid = self._approve_one()
        self._student_login()
        started = self._start_bank()
        self.assertEqual(started.status_code, 200, started.text)
        attempt = started.json()["attempt"]
        self.assertTrue(attempt["bank"])
        self.assertEqual(attempt["bank_question_id"], qid)
        self.assertNotIn("answer_keys", attempt)     # 作中不下发答案
        self.assertNotIn("explanation", attempt)     # 判分前不下发解析
        # 无授权校验：题库练习不接受规则/种子参数
        bad = self._start_bank(rule_id="R1", seed=1)
        self.assertEqual(bad.status_code, 422)
        # 答错（选 B）：0 分，解析开放
        scored = self.client.post(
            f"/api/training/my/practice/{attempt['id']}/submit", json={"selected_keys": ["B"]})
        self.assertEqual(scored.status_code, 200, scored.text)
        result = scored.json()["attempt"]["result"]
        self.assertFalse(result["perfect"])
        self.assertEqual(result["score"], 0)
        # 判分响应本身也要能直接渲染题目（题干 + 结构化选项），且仍标记为题库题
        self.assertTrue(scored.json()["attempt"]["bank"])
        self.assertIn("流动资产", scored.json()["attempt"]["stem"])
        self.assertEqual(len(scored.json()["attempt"]["options"]), 4)
        self.assertEqual(result["missed"], ["A"])
        self.assertEqual(result["false_positives"], ["B"])
        self.assertEqual(result["answer_keys"], ["A"])
        self.assertIn("流动资产", result["explanation"])
        # 重复提交不重复计数
        again = self.client.post(
            f"/api/training/my/practice/{attempt['id']}/submit", json={"selected_keys": ["A"]})
        self.assertEqual(again.status_code, 409)
        # 历史作答回看：题目下架后仍可读取当时题干与解析
        self._staff_login()
        self.client.post(f"/api/training/staff/bank-questions/{qid}/offline")
        self._student_login()
        detail = self.client.get(f"/api/training/my/practice/{attempt['id']}").json()["attempt"]
        self.assertEqual(detail["status"], "scored")
        # 当时题目按判分快照回溯：题干与选项都在，选项仍为结构化数据（不拼进题干）
        self.assertIn("流动资产", detail["stem"])
        self.assertEqual([o["text"] for o in detail["options"]],
                         ["库存现金", "应付账款", "实收资本", "累计折旧"])

    def test_multi_select_scoring_rules(self):
        self._approve_one(answer_keys=["A", "D"])
        self._student_login()
        attempt = self._start_bank().json()["attempt"]
        partial = self.client.post(
            f"/api/training/my/practice/{attempt['id']}/submit", json={"selected_keys": ["A"]})
        self.assertFalse(partial.json()["attempt"]["result"]["perfect"])  # 漏选不得分
        attempt2 = self._start_bank().json()["attempt"]
        wrong = self.client.post(
            f"/api/training/my/practice/{attempt2['id']}/submit", json={"selected_keys": ["A", "B", "D"]})
        self.assertFalse(wrong.json()["attempt"]["result"]["perfect"])   # 错选不得分
        attempt3 = self._start_bank().json()["attempt"]
        right = self.client.post(
            f"/api/training/my/practice/{attempt3['id']}/submit", json={"selected_keys": ["D", "A"]})
        self.assertTrue(right.json()["attempt"]["result"]["perfect"])    # 顺序无关完全匹配
        self.assertEqual(right.json()["attempt"]["score"], 100)

    def test_offline_questions_not_drawn_but_history_readable(self):
        qid = self._approve_one()
        self._student_login()
        first = self._start_bank().json()["attempt"]
        self.client.post(
            f"/api/training/my/practice/{first['id']}/submit", json={"selected_keys": ["A"]})
        # 下架后新练习抽不到，历史作答可回看，原题重做被拒
        self._staff_login()
        self.client.post(f"/api/training/staff/bank-questions/{qid}/offline")
        self._student_login()
        empty = self._start_bank()
        self.assertEqual(empty.status_code, 404)
        self.assertIn("已审核上线", empty.json()["detail"])
        redo = self._start_bank(mode="redo_same", bank_question_id=qid)
        self.assertEqual(redo.status_code, 409)
        # 历史作答列表仍可读取（含题库题标识）
        history = self.client.get(f"/api/training/my/practice?certificate_id={self.cert}")
        self.assertEqual(history.status_code, 200, history.text)
        rows = history.json()["attempts"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bank_question_id"], qid)
        self.assertEqual(rows[0]["source_type"], "real")

    def test_student_bank_routes_require_login(self):
        res = self.client.post("/api/training/my/practice/start",
                               json={"certificate_id": self.cert, "source_type": "real"})
        self.assertEqual(res.status_code, 401)
        res = self.client.get(f"/api/training/staff/certificates/"
                              f"{self.cert}/source-authorizations")
        self.assertEqual(res.status_code, 401)

    # ---- 作答口径与抽题公平性 -----------------------------------------------

    def test_empty_selection_is_not_scored_as_wrong(self):
        """空提交不是答错：拒绝判分并提示走「跳过」，避免悄悄记 0 分拉低正确率。"""
        self._approve_one()
        self._student_login()
        attempt = self._start_bank().json()["attempt"]
        empty = self.client.post(
            f"/api/training/my/practice/{attempt['id']}/submit", json={"selected_keys": []})
        self.assertEqual(empty.status_code, 422, empty.text)
        # 空提交不消耗本次作答：状态仍为 open，改选后可正常判分
        detail = self.client.get(f"/api/training/my/practice/{attempt['id']}").json()["attempt"]
        self.assertEqual(detail["status"], "open")
        ok = self.client.post(
            f"/api/training/my/practice/{attempt['id']}/submit", json={"selected_keys": ["A"]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertTrue(ok.json()["attempt"]["result"]["perfect"])

    def test_draw_prefers_unseen_questions(self):
        """抽题优先未做过的题：同题不重复抽，整库做完后才允许重复练。"""
        self._staff_login()
        self._create_auth(question_types=["real", "recall", "mock"])
        res = self._import([_question(knowledge_point_id=self.kp, question_no="一、1"),
                            _question(knowledge_point_id=self.kp, question_no="一、2")])
        self.assertEqual(res.json()["imported"], 2, res.text)
        ids = [r["id"] for r in res.json()["results"]]
        for qid in ids:
            self.client.post(f"/api/training/staff/bank-questions/{qid}/submit")
            self.client.post(f"/api/training/staff/bank-questions/{qid}/review",
                             json={"decision": "approve"})
        self._student_login()
        first = self._start_bank().json()["attempt"]
        self.client.post(f"/api/training/my/practice/{first['id']}/submit",
                         json={"selected_keys": ["A"]})
        second = self._start_bank().json()["attempt"]
        self.assertNotEqual(first["bank_question_id"], second["bank_question_id"],
                            "两题都没做过时不应重复抽同一题")
        self.client.post(f"/api/training/my/practice/{second['id']}/submit",
                         json={"selected_keys": ["A"]})
        # 两题都做过：仍可继续练（不因"没有新题"直接拒绝）
        third = self._start_bank()
        self.assertEqual(third.status_code, 200, third.text)
        self.assertIn(third.json()["attempt"]["bank_question_id"], ids)

    def test_import_rejects_unreasonable_year(self):
        """考试年度须落在 1992~明年：脏年份会污染题库与年度统计。"""
        self._staff_login()
        self._create_auth(question_types=["real", "recall", "mock"])
        res = self._import([_question(year=0), _question(year=9999),
                            _question(year=2026, knowledge_point_id=self.kp)])
        payload = res.json()
        self.assertEqual(payload["imported"], 1, res.text)
        self.assertEqual([r["line"] for r in payload["results"] if not r["ok"]], [1, 2])
        self.assertIn("考试年度不合法", payload["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
