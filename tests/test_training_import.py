"""知识点批量导入测试（FR-K03 录入效率工具）。

覆盖：checklist/CSV 两种格式解析、预览不落库、幂等 upsert（重复导入为更新
不重复建行、空字段不清空既有值）、全或无（校验失败整批拒绝）、循环与未知
上级拦截、staff 认证、导入不触碰标注/关联/停用状态。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import parse_kp_import, register

_passwords = PasswordHasher()
COOKIE = "test_import_cookie"
NOW = "2026-10-10T21:00:00"

CHECKLIST = """# 2026 年度初级会计大纲 · 知识点录入清单

## 科目：初级会计实务（科目字段填：初级会计实务）

### 章节点：`SW-01` 总论
- source_ref：`2026大纲《初级会计实务》一、总论`
- 描述建议：官方能力要求——掌握 5 项、熟悉 2 项、了解 0 项

#### 节点：`SW-01-01` 会计基本理论
- source_ref：`2026大纲《初级会计实务》一、总论（一）会计基本理论`
- 描述（考点清单，可直接粘贴）：

1\\.会计的概念、职能和目标（掌握）\\
2\\.会计基本假设（掌握）\\

#### 节点：`SW-01-02` 会计人员职业道德规范
- source_ref：`2026大纲《初级会计实务》一、总论（二）会计人员职业道德规范`
- 描述（考点清单，可直接粘贴）：

1\\.会计人员职业道德规范（掌握）\\
"""

CSV_TEXT = """code,parent_code,name,subject,description,source_ref,outline_version
JF-01,,经济法基础总论,经济法基础,总论概述,2026大纲《经济法基础》一、总论,2026
JF-01-01,JF-01,法律基础,经济法基础,法律基础知识,2026大纲《经济法基础》一、总论（一）,2026
"""


class KnowledgePointImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "ki.db")
        app = FastAPI()
        register(app, lambda: self.store, COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        self.staff_id = uuid.uuid4().hex
        self.cert = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (self.staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (self.cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))

    def _login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _import_url(self, suffix=""):
        return f"/api/training/staff/certificates/{self.cert}/knowledge-points/import{suffix}"

    def _db_kps(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM knowledge_point ORDER BY code").fetchall()]

    # ---- 解析 -----------------------------------------------------------

    def test_checklist_parse_captures_tree_and_subject(self):
        rows, errors = parse_kp_import(CHECKLIST, "checklist", default_outline_version="2026")
        self.assertEqual(errors, [])
        self.assertEqual([r["code"] for r in rows], ["SW-01", "SW-01-01", "SW-01-02"])
        self.assertEqual(rows[0]["parent_code"], "")
        self.assertEqual(rows[1]["parent_code"], "SW-01")
        self.assertEqual(rows[2]["parent_code"], "SW-01")
        self.assertTrue(all(r["subject"] == "初级会计实务" for r in rows))
        self.assertTrue(all(r["source_ref"].startswith("2026大纲") for r in rows))
        # 描述保留考点清单并还原被转义的句点
        self.assertIn("1.会计的概念、职能和目标（掌握）", rows[1]["description"])
        self.assertIn("2.会计基本假设（掌握）", rows[1]["description"])
        self.assertTrue(rows[0]["description"].startswith("官方能力要求"))

    def test_csv_parse_requires_header(self):
        rows, errors = parse_kp_import(CSV_TEXT, "csv")
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["code"], "JF-01")
        self.assertEqual(rows[1]["parent_code"], "JF-01")
        bad, errs = parse_kp_import("a,b,c\n1,2,3\n", "csv")
        self.assertEqual(bad, [])
        self.assertTrue(any("表头" in e for e in errs))

    def test_parse_rejects_duplicates_self_parent_and_missing_version(self):
        text = "### 章节点：`X-01` 甲\n### 章节点：`X-01` 重复\n#### 节点：`X-02` 乙\n"
        rows, errors = parse_kp_import(text, "checklist", default_outline_version="2026")
        self.assertTrue(any("重复" in e for e in errors))
        self.assertEqual(len([r for r in rows if r["code"] == "X-01"]), 1)  # 重复行被剔除
        rows2, errors2 = parse_kp_import(text, "checklist")  # 未提供默认版本
        self.assertEqual(rows2, [])
        self.assertTrue(any("outline_version" in e for e in errors2))
        head = "code,parent_code,name,subject,description,source_ref,outline_version"
        csv_dup, errs = parse_kp_import(f"{head}\nA,,甲,s,d,r,2026\nA,,乙,s,d,r,2026\n", "csv")
        self.assertEqual(len(csv_dup), 1)  # 首行保留、重复行剔除
        self.assertEqual(csv_dup[0]["name"], "甲")
        self.assertTrue(any("重复" in e for e in errs))
        csv_self, errs = parse_kp_import(f"{head}\nA,A,甲,s,d,r,2026\n", "csv")
        self.assertEqual(csv_self, [])
        self.assertTrue(any("自己" in e for e in errs))

    def test_parse_rejects_payload_cycle(self):
        head = "code,parent_code,name,subject,description,source_ref,outline_version"
        _, errors = parse_kp_import(f"{head}\nA,B,甲,s,d,r,2026\nB,A,乙,s,d,r,2026\n", "csv")
        self.assertTrue(any("循环" in e for e in errors))

    def test_unknown_format_rejected(self):
        with self.assertRaises(Exception):
            parse_kp_import("x", "yaml")

    # ---- 预览与导入 ------------------------------------------------------

    def test_preview_reports_actions_without_writing(self):
        self._login()
        res = self.client.post(self._import_url("/preview"), json={
            "text": CHECKLIST, "format": "checklist", "default_outline_version": "2026"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["summary"], {"total": 3, "insert": 3, "update": 0, "warning": 0})
        self.assertEqual(self._db_kps(), [])  # 预览不落库

    def test_import_creates_tree_with_parent_links(self):
        self._login()
        res = self.client.post(self._import_url(), json={
            "text": CHECKLIST, "format": "checklist", "default_outline_version": "2026"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json(), {"ok": True, "created": 3, "updated": 0, "total": 3})
        rows = {r["code"]: r for r in self._db_kps()}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["SW-01-01"]["parent_id"], rows["SW-01"]["id"])
        self.assertEqual(rows["SW-01-02"]["parent_id"], rows["SW-01"]["id"])
        self.assertEqual(rows["SW-01"]["parent_id"], None)
        self.assertEqual(rows["SW-01-01"]["outline_version"], "2026")
        self.assertEqual(rows["SW-01-01"]["source_ref"],
                         "2026大纲《初级会计实务》一、总论（一）会计基本理论")
        self.assertIn("2.会计基本假设（掌握）", rows["SW-01-01"]["description"])
        self.assertEqual(rows["SW-01"]["subject"], "初级会计实务")

    def test_reimport_updates_without_duplicates_and_preserves_blanks(self):
        self._login()
        payload = {"text": CHECKLIST, "format": "checklist", "default_outline_version": "2026"}
        self.client.post(self._import_url(), json=payload)
        before = {r["code"]: r for r in self._db_kps()}
        renamed = CHECKLIST.replace("会计基本理论", "会计基本理论（改）")
        res = self.client.post(self._import_url(), json={
            "text": renamed, "format": "checklist", "default_outline_version": "2026"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["created"], 0)
        self.assertEqual(res.json()["updated"], 3)
        after = {r["code"]: r for r in self._db_kps()}
        self.assertEqual(len(after), 3)  # 不重复建行
        self.assertEqual(after["SW-01-01"]["name"], "会计基本理论（改）")
        self.assertEqual(after["SW-01-01"]["created_at"], before["SW-01-01"]["created_at"])
        # 子节点重命名后编码与父级关系不变
        self.assertEqual(after["SW-01-01"]["parent_id"], after["SW-01"]["id"])

    def test_marks_links_and_inactive_untouched_by_reimport(self):
        self._login()
        self.client.post(self._import_url(), json={
            "text": CHECKLIST, "format": "checklist", "default_outline_version": "2026"})
        rows = {r["code"]: r for r in self._db_kps()}
        kp_id = rows["SW-01-01"]["id"]
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO knowledge_point_mark VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, kp_id, "high_freq", "high", "历年试题2026年第一批", "2026",
                 None, NOW))
            db.execute("UPDATE knowledge_point SET active=0 WHERE id=?", (kp_id,))
        res = self.client.post(self._import_url(), json={
            "text": CHECKLIST, "format": "checklist", "default_outline_version": "2026"})
        self.assertEqual(res.status_code, 200, res.text)
        after = {r["code"]: r for r in self._db_kps()}
        self.assertEqual(after["SW-01-01"]["active"], 0)  # 停用状态不被导入改写
        with self.store.connect() as db:
            marks = db.execute(
                "SELECT mark_type, basis_ref FROM knowledge_point_mark WHERE knowledge_point_id=?",
                (kp_id,)).fetchall()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["basis_ref"], "历年试题2026年第一批")

    def test_csv_import_resolves_parents(self):
        self._login()
        res = self.client.post(self._import_url(), json={"text": CSV_TEXT, "format": "csv"})
        self.assertEqual(res.status_code, 200, res.text)
        rows = {r["code"]: r for r in self._db_kps()}
        self.assertEqual(rows["JF-01-01"]["parent_id"], rows["JF-01"]["id"])
        self.assertEqual(rows["JF-01"]["subject"], "经济法基础")

    # ---- 校验与全或无 -----------------------------------------------------

    def test_unknown_parent_rejects_whole_batch(self):
        self._login()
        head = "code,parent_code,name,subject,description,source_ref,outline_version"
        res = self.client.post(self._import_url(), json={
            "text": f"{head}\nA,NOPE,甲,s,d,r,2026\n", "format": "csv",
            "default_outline_version": "2026"})
        self.assertEqual(res.status_code, 422, res.text)
        self.assertIn("NOPE", res.json()["detail"])
        self.assertEqual(self._db_kps(), [])

    def test_cross_batch_parent_cycle_rejected_on_update(self):
        self._login()
        head = "code,parent_code,name,subject,description,source_ref,outline_version"
        # A、B 已按 A<-B 建立父子；再次导入把 A 的上级改成 B（B 又是 A 的子级）→ 成环拒绝。
        payload = {"text": f"{head}\nA,,甲,s,d,r,2026\nB,A,乙,s,d,r,2026\n", "format": "csv",
                   "default_outline_version": "2026"}
        self.client.post(self._import_url(), json=payload)
        res = self.client.post(self._import_url(), json={
            "text": f"{head}\nA,B,甲,s,d,r,2026\n", "format": "csv",
            "default_outline_version": "2026"})
        self.assertEqual(res.status_code, 422, res.text)
        self.assertIn("循环", res.json()["detail"])
        rows = {r["code"]: r for r in self._db_kps()}
        self.assertIsNone(rows["A"]["parent_id"])  # 原关系未被破坏

    def test_auth_required_for_preview_and_import(self):
        for url in (self._import_url("/preview"), self._import_url()):
            res = self.client.post(url, json={"text": CHECKLIST, "format": "checklist"})
            self.assertEqual(res.status_code, 401, url)

    def test_import_to_unknown_certificate_404(self):
        self._login()
        res = self.client.post(
            f"/api/training/staff/certificates/{uuid.uuid4().hex}/knowledge-points/import",
            json={"text": CHECKLIST, "format": "checklist", "default_outline_version": "2026"})
        self.assertEqual(res.status_code, 404, res.text)

    def test_child_before_parent_in_payload_still_imports(self):
        self._login()
        # 子行排在父行之前（CSV）：拓扑排序保证父级先落库。
        head = "code,parent_code,name,subject,description,source_ref,outline_version"
        text = (f"{head}\n"
                "JF-02-01,JF-02,会计要素与会计等式,经济法基础,,2026大纲《经济法基础》二、会计基础（一）,2026\n"
                "JF-02,,会计基础,经济法基础,,2026大纲《经济法基础》二、会计基础,2026\n")
        res = self.client.post(self._import_url(), json={"text": text, "format": "csv"})
        self.assertEqual(res.status_code, 200, res.text)
        rows = {r["code"]: r for r in self._db_kps()}
        self.assertEqual(rows["JF-02-01"]["parent_id"], rows["JF-02"]["id"])


if __name__ == "__main__":
    unittest.main()
