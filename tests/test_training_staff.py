"""教师/管理员内容维护接口测试（FR-K01～K04 管理侧）。

覆盖：staff 认证、证书上架/下架、官方日期录入与引用保护、考纲知识点
层级与循环防护、三类标注（考证高频强制依据）。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import register, today_cst

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_training_cookie"
NOW = "2026-10-09T21:00:00"


def _future(days=60):
    return (today_cst() + timedelta(days=days)).isoformat()


class TrainingStaffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "ts.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        staff_id, student_id, cert, cert2 = (uuid.uuid4().hex for _ in range(4))
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_id, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("study-pass"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert2, "other", "其他证书", "", "[]", "", NOW, NOW))
            db.execute("INSERT INTO exam_date (id,certificate_id,round_label,date_type,exam_date,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, cert, "2027年第一批", "official", _future(), NOW, NOW))
        self.staff_id, self.student_id, self.cert, self.cert2 = staff_id, student_id, cert, cert2

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)
        return res

    def _student_login(self):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "study-pass", "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)
        return res

    # ---- 教师认证 -------------------------------------------------------

    def test_staff_login_me_logout(self):
        self._staff_login()
        me = self.client.get("/api/training/staff/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["staff"]["role"], "admin")
        self.client.post("/api/training/staff/auth/logout")
        self.assertEqual(self.client.get("/api/training/staff/auth/me").status_code, 401)

    def test_staff_wrong_password_and_student_cookie_rejected(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "wrong"})
        self.assertEqual(res.status_code, 401)
        # 学生会话不能访问教师接口
        self._student_login()
        res = self.client.post("/api/training/staff/certificates", json={
            "code": "hack", "name": "越权"})
        self.assertEqual(res.status_code, 401)

    def test_staff_page_and_script_served(self):
        page = self.client.get("/training-staff")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        script = self.client.get("/training-staff.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("text/javascript", script.headers["content-type"])

    # ---- 证书上架（FR-K01）---------------------------------------------

    def test_certificate_create_duplicate_and_toggle(self):
        self._staff_login()
        res = self.client.post("/api/training/staff/certificates", json={
            "code": "cns", "name": "纳税实务证书", "subjects": ["纳税实务", "税法", "税法 "],
            "description": "测试证书", "source_ref": "依据 X"})
        self.assertEqual(res.status_code, 200, res.text)
        cert = res.json()["certificate"]
        # 科目清洗：去空格 + 去重
        self.assertEqual(cert["subjects"], ["纳税实务", "税法"])
        dup = self.client.post("/api/training/staff/certificates", json={
            "code": "cns", "name": "重复编码"})
        self.assertEqual(dup.status_code, 409)
        # 下架后学生目录不可见，教师列表可见
        off = self.client.patch(f"/api/training/staff/certificates/{cert['id']}", json={"active": False})
        self.assertEqual(off.status_code, 200)
        self.assertEqual(off.json()["certificate"]["active"], False)
        staff_list = self.client.get("/api/training/staff/certificates").json()["certificates"]
        self.assertIn("cns", [c["code"] for c in staff_list])
        self._student_login()
        student_view = self.client.get("/api/training/certificates").json()["certificates"]
        self.assertNotIn("cns", [c["code"] for c in student_view])

    # ---- 官方日期录入（FR-K02）-----------------------------------------

    def test_exam_date_crud_and_reference_protection(self):
        self._staff_login()
        ok = self.client.post(f"/api/training/staff/certificates/{self.cert}/dates", json={
            "date_type": "official", "exam_date": _future(30), "round_label": "2026年加考"})
        self.assertEqual(ok.status_code, 200, ok.text)
        date_id = ok.json()["exam_date"]["id"]
        self.assertEqual(ok.json()["exam_date"]["countdown"]["days_left"], 30)
        # 过去日期、个人类型被拒
        self.assertEqual(self.client.post(f"/api/training/staff/certificates/{self.cert}/dates", json={
            "date_type": "official", "exam_date": "2020-01-01"}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/training/staff/certificates/{self.cert}/dates", json={
            "date_type": "personal", "exam_date": _future()}).status_code, 422)
        # 学生目标引用后删除被保护
        self._student_login()
        goal = self.client.post("/api/training/my/goals", json={
            "certificate_id": self.cert, "official_date_id": date_id})
        self.assertEqual(goal.status_code, 200)
        self._staff_login()
        blocked = self.client.delete(f"/api/training/staff/dates/{date_id}")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("引用", blocked.json()["detail"])
        # 修改日期仍可（改期而非删除）
        moved = self.client.patch(f"/api/training/staff/dates/{date_id}", json={
            "exam_date": _future(45), "round_label": "2026年加考（延期）"})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json()["exam_date"]["exam_date"], _future(45))

    # ---- 考纲知识点（FR-K03）-------------------------------------------

    def _make_kp(self, cert, code, name, parent=None):
        res = self.client.post(f"/api/training/staff/certificates/{cert}/knowledge-points", json={
            "code": code, "name": name, "subject": "初级会计实务",
            "parent_id": parent, "source_ref": "考纲第三章", "outline_version": "2027版"})
        return res

    def test_knowledge_point_crud_and_guards(self):
        self._staff_login()
        root = self._make_kp(self.cert, "KP-01", "会计概述")
        self.assertEqual(root.status_code, 200, root.text)
        root_id = root.json()["knowledge_point"]["id"]
        child = self._make_kp(self.cert, "KP-01-01", "会计职能", parent=root_id)
        self.assertEqual(child.status_code, 200)
        # 证书内重复编码
        self.assertEqual(self._make_kp(self.cert, "KP-01", "重名").status_code, 409)
        # 跨证书父级、自引用、循环
        other_root = self._make_kp(self.cert2, "KP-X", "其他证书知识点")
        self.assertEqual(self.client.post(
            f"/api/training/staff/certificates/{self.cert}/knowledge-points", json={
                "code": "KP-BAD", "name": "跨证书父级",
                "parent_id": other_root.json()["knowledge_point"]["id"]}).status_code, 422)
        self.assertEqual(self.client.patch(
            f"/api/training/staff/knowledge-points/{root_id}", json={"parent_id": root_id}).status_code, 422)
        child_id = child.json()["knowledge_point"]["id"]
        self.assertEqual(self.client.patch(
            f"/api/training/staff/knowledge-points/{root_id}",
            json={"parent_id": child_id}).status_code, 422)
        # 更新与停用
        updated = self.client.patch(f"/api/training/staff/knowledge-points/{child_id}", json={
            "name": "会计职能（修订）", "active": False})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["knowledge_point"]["active"], False)

    # ---- 三类标注（FR-K04）---------------------------------------------

    def test_marks_require_basis_for_high_freq(self):
        self._staff_login()
        root_id = self._make_kp(self.cert, "KP-02", "收入确认").json()["knowledge_point"]["id"]
        no_basis = self.client.post(f"/api/training/staff/knowledge-points/{root_id}/marks", json={
            "mark_type": "high_freq"})
        self.assertEqual(no_basis.status_code, 422)
        self.assertIn("依据", no_basis.json()["detail"])
        ok = self.client.post(f"/api/training/staff/knowledge-points/{root_id}/marks", json={
            "mark_type": "high_freq", "basis_ref": "考纲第三章第2节", "basis_version": "2027版"})
        self.assertEqual(ok.status_code, 200)
        mark_id = ok.json()["mark"]["id"]
        dup = self.client.post(f"/api/training/staff/knowledge-points/{root_id}/marks", json={
            "mark_type": "high_freq", "basis_ref": "考纲第三章第2节"})
        self.assertEqual(dup.status_code, 409)
        # 高频标注不允许清空依据
        cleared = self.client.patch(f"/api/training/staff/marks/{mark_id}", json={
            "basis_ref": "", "level": "medium"})
        self.assertEqual(cleared.status_code, 422)
        # 其他类型无依据可标（口径由统计/规则来源说明，不强制文本依据）
        risk = self.client.post(f"/api/training/staff/knowledge-points/{root_id}/marks", json={
            "mark_type": "risk_context"})
        self.assertEqual(risk.status_code, 200)
        self.assertEqual(self.client.delete(f"/api/training/staff/marks/{mark_id}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
