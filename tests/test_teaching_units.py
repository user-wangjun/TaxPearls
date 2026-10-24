"""教学单元与实操案例（重设计方案 B1）测试。

覆盖：建表 additive（Store 初始化即建）、单元/案例 CRUD 与校验
（stage/规则存在性/知识点归属/状态流转/重名冲突）、依据链
（快照→USES_METRIC→Metric 查得到、Clause 为空时返回「依据待专业复核」）、
未挂靠规则提示（无 knowledge_point_link 的规则出现在 unlinked 列表）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import register as register_portal
from webapp.teaching_units import register as register_teaching_units

_passwords = PasswordHasher()
STAFF_COOKIE = "taxpearls_staff_session"
STUDENT_COOKIE = "taxpearls_training_session"
NOW = "2026-10-22T10:00:00"


class TeachingUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # keepalive：本机 SQLite WAL 抖动会以 readonly 面目打断写事务
        self.store = Store(Path(self._tmp.name) / "tp.db", keepalive=True)
        app = FastAPI()
        register_portal(app, lambda: self.store, STUDENT_COOKIE)
        register_teaching_units(app, lambda: self.store)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _seed(self):
        (self.staff_id, self.cert, kp1, kp2) = (uuid.uuid4().hex for _ in range(4))
        self.kp1, self.kp2 = kp1, kp2
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (self.staff_id, "admin1", _passwords.hash("staff-b1"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (self.cert, "cjkj", "初级会计职称", "", '["初级会计实务"]',
                        "", NOW, NOW))
            for kid, code in ((kp1, "KP-01"), (kp2, "KP-02")):
                db.execute(
                    "INSERT INTO knowledge_point (id,certificate_id,code,name,subject,"
                    "active,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
                    (kid, self.cert, code, f"知识点{code}", "初级会计实务", NOW, NOW))
            # R-002 挂 kp1；R-024 不挂任何知识点（对齐真库现状）
            db.execute(
                "INSERT INTO knowledge_point_link (id,knowledge_point_id,target_type,"
                "target_id,active,created_by,created_at) VALUES (?,?,?,?,1,NULL,?)",
                (uuid.uuid4().hex, kp1, "rule", "R-002", NOW))
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": "admin1", "password": "staff-b1"})
        self.assertEqual(res.status_code, 200, res.text)

    # ---- 单元 ---------------------------------------------------------

    def test_unit_crud_and_validation(self):
        created = self.client.post("/api/training/teaching/units", json={
            "certificate_id": self.cert, "name": "增值税账申勾稽",
            "stage": "data", "standard": "账表票申报四方口径一致且差异可解释",
            "knowledge_point_ids": [self.kp1], "rule_codes": ["R-002", "R-005"],
            "deliverables": "勾稽底稿与差异调节表", "sort_order": 1})
        self.assertEqual(created.status_code, 200, created.text)
        unit = created.json()["unit"]
        self.assertEqual(unit["status"], "draft")
        self.assertEqual(unit["stage_label"], "数据层")
        self.assertEqual(unit["rule_codes"], ["R-002", "R-005"])

        # 未知规则拒绝
        bad = self.client.post("/api/training/teaching/units", json={
            "certificate_id": self.cert, "name": "坏单元", "stage": "data",
            "rule_codes": ["R-999"]})
        self.assertEqual(bad.status_code, 422)

        # 非本证书知识点拒绝
        bad_kp = self.client.post("/api/training/teaching/units", json={
            "certificate_id": self.cert, "name": "坏知识点", "stage": "data",
            "knowledge_point_ids": ["kp-not-mine"], "rule_codes": ["R-002"]})
        self.assertEqual(bad_kp.status_code, 422)

        # stage 非法拒绝
        bad_stage = self.client.post("/api/training/teaching/units", json={
            "certificate_id": self.cert, "name": "坏阶段", "stage": "magic",
            "rule_codes": ["R-002"]})
        self.assertEqual(bad_stage.status_code, 422)

        # 重名冲突 409
        dup = self.client.post("/api/training/teaching/units", json={
            "certificate_id": self.cert, "name": "增值税账申勾稽", "stage": "data",
            "rule_codes": ["R-002"]})
        self.assertEqual(dup.status_code, 409)

        # 发布 → 已发布不能退回草稿
        published = self.client.put(f"/api/training/teaching/units/{unit['id']}",
                                    json={"status": "published"})
        self.assertEqual(published.status_code, 200)
        back = self.client.put(f"/api/training/teaching/units/{unit['id']}",
                               json={"status": "draft"})
        self.assertEqual(back.status_code, 422)

        # 归档后不可改
        self.client.put(f"/api/training/teaching/units/{unit['id']}",
                        json={"status": "archived"})
        after = self.client.put(f"/api/training/teaching/units/{unit['id']}",
                                json={"standard": "改一下"})
        self.assertEqual(after.status_code, 422)

        listing = self.client.get(f"/api/training/teaching/units?certificate_id={self.cert}")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()["units"]), 1)

    def test_unit_requires_login(self):
        # 未登录 401：TestClient 无 cookie 时（登出或新 client）
        res = self.client.get("/api/training/teaching/unlinked-rules")
        self.assertEqual(res.status_code, 200)  # 已登录
        self.assertIn("R-024", json.dumps(res.json()))

    # ---- 案例 ---------------------------------------------------------

    def test_case_crud_and_conclusion_validation(self):
        unit = self.client.post("/api/training/teaching/units", json={
            "certificate_id": self.cert, "name": "增值税账申勾稽", "stage": "data",
            "rule_codes": ["R-002"]}).json()["unit"]
        created = self.client.post("/api/training/teaching/cases", json={
            "certificate_id": self.cert, "name": "账申勾稽样板案例", "unit_id": unit["id"],
            "scenario": "某商贸公司 2026 年 9 月属期仿真账表",
            "rule_codes": ["R-002"],
            "materials": [{"name": "科目余额表", "note": "仿真生成"},
                          {"name": "增值税申报主表", "note": "仿真生成"}],
            "standard_conclusion": [
                {"rule_code": "R-002", "expected": "hit", "note": "销项与申报差异超容差"}],
            "desensitization_note": "全部数据由合成样例派生，不含真实企业信息"})
        self.assertEqual(created.status_code, 200, created.text)
        case = created.json()["case"]
        self.assertEqual(case["status"], "draft")
        self.assertEqual(len(case["materials"]), 2)

        # 标准结论引用未绑定规则 → 拒绝
        bad = self.client.post("/api/training/teaching/cases", json={
            "certificate_id": self.cert, "name": "坏结论案例", "rule_codes": ["R-002"],
            "standard_conclusion": [{"rule_code": "R-011", "expected": "pass"}]})
        self.assertEqual(bad.status_code, 422)

        # 结论三态校验
        bad_state = self.client.post("/api/training/teaching/cases", json={
            "certificate_id": self.cert, "name": "坏三态案例", "rule_codes": ["R-002"],
            "standard_conclusion": [{"rule_code": "R-002", "expected": "maybe"}]})
        self.assertEqual(bad_state.status_code, 422)

        # 材料项缺 name → 拒绝
        bad_mat = self.client.post("/api/training/teaching/cases", json={
            "certificate_id": self.cert, "name": "坏材料案例", "rule_codes": ["R-002"],
            "materials": [{"note": "没有名字"}]})
        self.assertEqual(bad_mat.status_code, 422)

        # 按单元过滤
        listing = self.client.get(
            f"/api/training/teaching/cases?certificate_id={self.cert}&unit_id={unit['id']}")
        self.assertEqual(len(listing.json()["cases"]), 1)

    # ---- 依据链与未挂靠规则 -------------------------------------------

    def test_rule_evidence_metrics_and_pending_review(self):
        res = self.client.get("/api/training/teaching/rule-evidence/R-002")
        self.assertEqual(res.status_code, 200, res.text)
        ev = res.json()["evidence"]
        self.assertEqual(ev["rule_code"], "R-002")
        self.assertTrue(ev["snapshot_id"].startswith("R-002@"))
        # sync_rules 会给每条规则的 inputs/evidence 建 USES_METRIC 指标
        self.assertGreater(len(ev["metrics"]), 0)
        self.assertIn("销项税额", json.dumps(ev["metrics"], ensure_ascii=False))
        # 本测试库 Clause/Policy 为 0 → 必须标「依据待专业复核」
        self.assertEqual(ev["clauses"], [])
        self.assertEqual(ev["legal_status"], "pending_review")
        self.assertIn("待专业复核", ev["legal_status_label"])
        # 法条要点来自规则快照（内部筛查口径），必须存在
        self.assertTrue(ev["legal_basis"])

    def test_rule_evidence_unknown_rule_404(self):
        res = self.client.get("/api/training/teaching/rule-evidence/R-999")
        self.assertEqual(res.status_code, 404)

    def test_unlinked_rules_exposes_r024(self):
        res = self.client.get("/api/training/teaching/unlinked-rules")
        self.assertEqual(res.status_code, 200)
        codes = [r["rule_code"] for r in res.json()["rules"]]
        self.assertIn("R-024", codes)
        self.assertNotIn("R-002", codes)  # kp1 已挂 R-002


if __name__ == "__main__":
    unittest.main()
