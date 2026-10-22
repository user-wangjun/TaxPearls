"""实操案例 → 学生任务链路（重设计方案 B4 前半）。

要验的不是"能不能建一条记录"，而是这四件事：
1. 发布复用既有任务主链（班级、截止、发布、提交与复核都不另起炉灶）；
2. 学生只看得到本次训练的步骤，标准结论按答案开放策略下发；
3. 交付物是学生过程性作答，落库加密；
4. 边界：非本班 404、未训练步骤拒收、提交后锁死、归档后学生不可见。
"""
from __future__ import annotations

import json
import os
import sqlite3
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module
from webapp.storage import Store

NOW = "2026-10-22T10:00:00"


class TeachingCaseTaskTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_AI_ENABLED": "0",
                                          "TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / "case_tasks.db", keepalive=True)
        module.store = self.store
        self.password = "Case-Task#2026"
        self.teacher = self.store.create_user("ct-teacher", self.password, "任课教师", "teacher", "school-a")
        self.student = self.store.create_user("ct-student", self.password, "学生甲", "student", "school-a")
        self.outsider = self.store.create_user("ct-outsider", self.password, "同校他班", "student", "school-a")
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        self.audit = self.client.post("/api/exercises", json={
            "rule_id": "R-001", "seed": 77, "expected_version": "2.0"}).json()["audit_id"]
        self.class_id = self.client.post("/api/classes", json={
            "name": "会计一班", "student_ids": [self.student["id"]]}).json()["id"]
        self.future = datetime(2032, 1, 1, tzinfo=UTC).isoformat()
        self.cert, self.case_id = self._seed_case("published")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    # ------------------------------------------------------------------ 夹具

    def login(self, who):
        self.client.cookies.clear()
        response = self.client.post("/api/login", json={"username": who["username"], "password": self.password})
        self.assertEqual(response.status_code, 200, response.text)

    def _seed_case(self, status: str, suffix: str = "") -> tuple[str, str]:
        cert, case_id = (f"ct-cert{suffix}", f"ct-case{suffix}")
        with self.store.connect() as db:
            db.execute("INSERT INTO certificate (id,code,name,description,subjects_json,"
                       "source_ref,active,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, f"cjkj{suffix}", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            db.execute(
                """INSERT INTO teaching_case
                     (id,certificate_id,unit_id,name,scenario,rule_codes_json,materials_json,
                      standard_conclusion_json,desensitization_note,status,created_by,created_at,updated_at)
                   VALUES (?,?,NULL,?,?,?,?,?,?,?,NULL,?,?)""",
                (case_id, cert, "留抵退税真实性核查", "某贸易公司 2026 年一季度申请留抵退税。",
                 json.dumps(["R-001"]),
                 json.dumps([{"name": "增值税申报表", "note": "2026Q1"},
                             {"name": "进项发票清单", "note": "导出件"}]),
                 json.dumps([{"rule_code": "R-001", "expected": "hit", "note": "进项与申报表不符"}]),
                 "仿真数据，非真实企业凭证。", status, NOW, NOW))
        return cert, case_id

    def publish(self, case_id=None, steps=None, dimensions=None, **fields):
        body = {"class_id": self.class_id, "audit_id": self.audit, "deadline_at": self.future,
                "steps": ["receive", "identify"] if steps is None else steps,
                "dimensions": ["material"] if dimensions is None else dimensions}
        body.update(fields)
        return self.client.post(f"/api/training/teaching/cases/{case_id or self.case_id}/publish", json=body)

    def assignment_of(self, published) -> str:
        return published.json()["assignment_id"]

    # ------------------------------------------------------------------ 发布

    def test_publish_reuses_the_existing_task_chain(self):
        response = self.publish(steps=["receive", "identify", "judge"],
                                dimensions=["material", "basis"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        assignment_id = body["assignment_id"]
        self.assertEqual(body["steps"], ["receive", "identify", "judge"])
        self.assertEqual(body["step_labels"][0], "① 接收材料")
        self.assertEqual(body["dimensions"], ["material", "basis"])
        # 班级、截止、发布状态都留在既有表里，旁挂表不复制一份
        with self.store.connect() as db:
            settings = db.execute("SELECT class_id,deadline_at FROM training_assignment_settings"
                                  " WHERE assignment_id=?", (assignment_id,)).fetchone()
            assignment = db.execute("SELECT published,audit_id FROM assignments WHERE id=?",
                                    (assignment_id,)).fetchone()
        self.assertEqual(settings["class_id"], self.class_id)
        self.assertEqual(assignment["published"], 1)
        self.assertEqual(assignment["audit_id"], self.audit)
        # 主链清单里看得见
        listed = self.client.get("/api/training/tasks").json()["items"]
        self.assertIn(("assignment", assignment_id), {(i["kind"], i["id"]) for i in listed})

    def test_draft_case_cannot_be_published(self):
        _, draft = self._seed_case("draft", "-draft")
        response = self.publish(case_id=draft)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("先把案例发布", response.json()["detail"])

    def test_unknown_step_or_dimension_is_refused(self):
        self.assertEqual(self.publish(steps=["receive", "乱填"]).status_code, 422)
        self.assertEqual(self.publish(dimensions=[]).status_code, 422)

    def test_student_cannot_publish_and_teacher_sees_only_own_tasks(self):
        self.login(self.student)
        self.assertEqual(self.publish().status_code, 403)
        self.login(self.teacher)
        self.publish()
        self.assertEqual(len(self.client.get("/api/training/teaching/case-tasks").json()["items"]), 1)

    # ------------------------------------------------------------------ 学生

    def test_student_sees_only_picked_steps_and_conclusion_is_gated(self):
        assignment_id = self.assignment_of(self.publish())
        self.login(self.student)
        view = self.client.get(f"/api/assignments/{assignment_id}/case").json()
        self.assertEqual([s["step"] for s in view["steps_detail"]], ["receive", "identify"])
        self.assertEqual(len(view["case"]["materials"]), 2)
        self.assertEqual(view["case"]["rule_codes"], ["R-001"])
        # 没提交，标准结论不下发，且给出原因
        self.assertEqual(view["standard_conclusion"], [])
        self.assertEqual(view["conclusion_note"], "尚未提交，暂不提供结果。")
        self.assertTrue(view["editable"])

    def test_delivery_round_trips_and_is_encrypted_at_rest(self):
        assignment_id = self.assignment_of(self.publish())
        self.login(self.student)
        saved = self.client.put(f"/api/assignments/{assignment_id}/case-delivery",
                                json={"step": "receive", "content": "收到申报表与发票清单，缺银行流水。"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["content"], "收到申报表与发票清单，缺银行流水。")
        again = self.client.get(f"/api/assignments/{assignment_id}/case").json()
        first = next(s for s in again["steps_detail"] if s["step"] == "receive")
        self.assertEqual(first["content"], "收到申报表与发票清单，缺银行流水。")
        self.assertTrue(first["updated_at"])
        # 磁盘上是密文，不是明文
        raw = sqlite3.connect(str(Path(self.tmp.name) / "case_tasks.db"))
        try:
            rows = raw.execute("SELECT content FROM teaching_case_delivery").fetchall()
        finally:
            raw.close()
        self.assertTrue(rows)
        for (value,) in rows:
            self.assertTrue(value.startswith("tp-field-v1:"), value[:24])
            self.assertNotIn("银行流水", value)

    def test_step_outside_this_task_is_refused(self):
        assignment_id = self.assignment_of(self.publish())
        self.login(self.student)
        response = self.client.put(f"/api/assignments/{assignment_id}/case-delivery",
                                   json={"step": "evidence", "content": "证据链"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("不训练这一步", response.json()["detail"])

    def test_student_outside_the_class_gets_404(self):
        assignment_id = self.assignment_of(self.publish())
        self.login(self.outsider)
        self.assertEqual(self.client.get(f"/api/assignments/{assignment_id}/case").status_code, 404)

    def test_delivery_is_locked_after_formal_submission(self):
        assignment_id = self.assignment_of(self.publish())
        self.login(self.student)
        self.assertEqual(self.client.put(f"/api/assignments/{assignment_id}/case-delivery",
                                         json={"step": "receive", "content": "初稿"}).status_code, 200)
        self.client.post(f"/api/assignments/{assignment_id}/submit", json={"selected_rule_ids": []})
        locked = self.client.put(f"/api/assignments/{assignment_id}/case-delivery",
                                 json={"step": "receive", "content": "改一下"})
        self.assertEqual(locked.status_code, 409, locked.text)
        self.assertIn("已正式提交", locked.json()["detail"])

    def test_teacher_reads_deliveries_of_own_class_only(self):
        assignment_id = self.assignment_of(self.publish())
        self.login(self.student)
        self.client.put(f"/api/assignments/{assignment_id}/case-delivery",
                        json={"step": "identify", "content": "进项合计 120 万，见发票清单第 3 页。"})
        self.login(self.teacher)
        items = self.client.get("/api/training/teaching/case-tasks").json()["items"]
        found = next(i for i in items if i["assignment_id"] == assignment_id)
        body = self.client.get(f"/api/training/teaching/case-tasks/{found['id']}/deliveries").json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["student_id"], self.student["id"])
        content = {s["step"]: s["content"] for s in body["items"][0]["steps"]}
        self.assertIn("进项合计 120 万", content["identify"])
        # 他班学生不在名册里，查不到
        self.assertEqual(self.client.get(
            f"/api/training/teaching/case-tasks/{found['id']}/deliveries"
            f"?student_id={self.outsider['id']}").json()["items"], [])

    # ------------------------------------------------------------------ 归档

    def test_archive_hides_it_from_students_but_keeps_the_record(self):
        assignment_id = self.assignment_of(self.publish())
        self.login(self.student)
        self.client.put(f"/api/assignments/{assignment_id}/case-delivery",
                        json={"step": "receive", "content": "已收到材料"})
        self.login(self.teacher)
        task_id = self.client.get("/api/training/teaching/case-tasks").json()["items"][0]["id"]
        self.assertEqual(self.client.post(f"/api/training/teaching/case-tasks/{task_id}/archive",
                                          json={"archived": True}).json()["archived"], True)
        self.login(self.student)
        self.assertEqual(self.client.get(f"/api/assignments/{assignment_id}/case").status_code, 404)
        self.login(self.teacher)
        with self.store.connect() as db:
            kept = db.execute("SELECT COUNT(*) c FROM teaching_case_delivery").fetchone()["c"]
        self.assertEqual(kept, 1, "归档不删学生记录")


if __name__ == "__main__":
    unittest.main()
