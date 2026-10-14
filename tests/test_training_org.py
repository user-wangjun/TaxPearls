"""FR-U01 第一期测试：高校组织架构（院系 / 学年学期 / 班级实体）。

覆盖：三实体 CRUD（写 admin-only、教师写 403、读 staff）；重复名 409；
学期 is_current 同院校唯一；删除引用保护 409；班级归档过滤；
在读人数按 (college, class_name) 对齐文本名册（不改写学生归属）；
班级按学期重建（同学期拒绝、新学期成功、学生名册不动）；
跨院校引用 422；未登录 401。

与现有两层模型并存：测试同时验证 student_info.class_name 全程不被改写。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

import webapp.training_portal as training_portal_module
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from webapp.login_guard import LoginGuard

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import register

_passwords = PasswordHasher()
COOKIE_NAME = "test_org_cookie"
NOW = "2026-10-13T22:00:00"
COLLEGE = "演示财经职业学院"


class OrgStructureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "org.db")
        # LoginGuard 模块级全局 + CI discover 同进程 + TestClient IP 恒定：
        # 失败登录跨文件累计会以 429 打爆后续登录，本文件换全新实例隔离。
        self._orig_guard = training_portal_module.login_guard
        training_portal_module.login_guard = LoginGuard()
        app = FastAPI()
        register(app, lambda: self.store, COOKIE_NAME)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        training_portal_module.login_guard = self._orig_guard
        self._tmp.cleanup()

    def _seed(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO college_user (id, username, password_hash, display_name, college,"
                " department, email, phone, role, active, created_at)"
                " VALUES (?,?,?,?,?,NULL,NULL,NULL,'admin',1,?)",
                (uuid.uuid4().hex, "admin01", _passwords.hash("AdminPass1!"), "管理员", COLLEGE, NOW))
            db.execute(
                "INSERT INTO college_user (id, username, password_hash, display_name, college,"
                " department, email, phone, role, active, created_at)"
                " VALUES (?,?,?,?,?,NULL,NULL,NULL,'teacher',1,?)",
                (uuid.uuid4().hex, "teacher01", _passwords.hash("TeacherPass1!"), "现教师", COLLEGE, NOW))
            for no, name, cls, active in (
                    ("2026001", "张三", "会计2601", 1), ("2026002", "李四", "会计2601", 1),
                    ("2026003", "王五", "会计2601", 0),   # 停用：不计入在读
                    ("2026004", "赵六", "会计2602", 1)):
                db.execute(
                    "INSERT INTO student_info (id, college, student_no, name, class_name,"
                    " password_hash, email, active, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, COLLEGE, no, name, cls,
                     _passwords.hash("StudentPass1!"), None, active, NOW))

    def _login(self, username: str, password: str):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": username, "password": password})
        self.assertEqual(res.status_code, 200, res.text)

    def _login_admin(self):
        self._login("admin01", "AdminPass1!")

    def _login_teacher(self):
        self._login("teacher01", "TeacherPass1!")

    # ---- 院系 --------------------------------------------------------------

    def test_department_crud_and_protection(self):
        self._login_admin()
        res = self.client.post("/api/training/staff/org/departments",
                               json={"college": COLLEGE, "name": "会计学院"})
        self.assertEqual(res.status_code, 200, res.text)
        dep_id = res.json()["department"]["id"]
        # 重复名 409
        dup = self.client.post("/api/training/staff/org/departments",
                               json={"college": COLLEGE, "name": "会计学院"})
        self.assertEqual(dup.status_code, 409)
        # 建学期与班级并引用院系
        term_id = self.client.post("/api/training/staff/org/terms", json={
            "college": COLLEGE, "name": "2026-2027-1", "is_current": True}).json()["term"]["id"]
        self.client.post("/api/training/staff/org/classes", json={
            "college": COLLEGE, "name": "会计2601",
            "department_id": dep_id, "term_id": term_id})
        # 被引用院系删除保护
        blocked = self.client.delete(f"/api/training/staff/org/departments/{dep_id}")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        # 归档 + 恢复 + 改名
        self.assertEqual(self.client.patch(f"/api/training/staff/org/departments/{dep_id}",
                                           json={"archived": True}).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/training/staff/org/departments/{dep_id}",
                                           json={"name": "财会学院"}).status_code, 200)
        listed = self.client.get(
            f"/api/training/staff/org/departments?college={COLLEGE}&include_archived=false"
        ).json()["departments"]
        self.assertEqual(listed, [])  # 归档后默认列表不显示
        # 空引用院系可删
        empty = self.client.post("/api/training/staff/org/departments",
                                 json={"college": COLLEGE, "name": "信息学院"}).json()["department"]["id"]
        self.assertEqual(self.client.delete(f"/api/training/staff/org/departments/{empty}").status_code, 200)

    # ---- 学期 ---------------------------------------------------------------

    def test_term_current_uniqueness_and_protection(self):
        self._login_admin()
        t1 = self.client.post("/api/training/staff/org/terms", json={
            "college": COLLEGE, "name": "2026-2027-1", "is_current": True}).json()["term"]["id"]
        t2 = self.client.post("/api/training/staff/org/terms", json={
            "college": COLLEGE, "name": "2026-2027-2", "is_current": True}).json()["term"]["id"]
        terms = {t["id"]: t for t in self.client.get(
            f"/api/training/staff/org/terms?college={COLLEGE}").json()["terms"]}
        self.assertEqual(terms[t1]["is_current"], 0)   # 设为当前会清除同院校其他 current
        self.assertEqual(terms[t2]["is_current"], 1)
        # 引用后删除保护
        self.client.post("/api/training/staff/org/classes", json={
            "college": COLLEGE, "name": "会计2601", "term_id": t2})
        self.assertEqual(self.client.delete(f"/api/training/staff/org/terms/{t2}").status_code, 409)
        # 日期校验
        bad = self.client.post("/api/training/staff/org/terms", json={
            "college": COLLEGE, "name": "2027-2028-1",
            "start_date": "2027-09-01", "end_date": "2027-01-15"})
        self.assertEqual(bad.status_code, 422)

    # ---- 班级 ---------------------------------------------------------------

    def test_class_alignment_archive_and_permissions(self):
        """在读人数按 (college, class_name) 对齐文本名册；归档过滤；教师写 403。"""
        self._login_teacher()
        # 教师只读：写 403、读 200
        self.assertEqual(self.client.post("/api/training/staff/org/departments", json={
            "college": COLLEGE, "name": "越权院系"}).status_code, 403)
        listed = self.client.get(f"/api/training/staff/org/classes?college={COLLEGE}").json()
        self.assertEqual(listed["classes"], [])
        self._login_admin()
        dep_id = self.client.post("/api/training/staff/org/departments", json={
            "college": COLLEGE, "name": "会计学院"}).json()["department"]["id"]
        term_id = self.client.post("/api/training/staff/org/terms", json={
            "college": COLLEGE, "name": "2026-2027-1", "is_current": True}).json()["term"]["id"]
        cls_id = self.client.post("/api/training/staff/org/classes", json={
            "college": COLLEGE, "name": "会计2601",
            "department_id": dep_id, "term_id": term_id}).json()["class"]["id"]
        # 同学期同名 409；不同学期同名允许（学期重建的前提）
        self.assertEqual(self.client.post("/api/training/staff/org/classes", json={
            "college": COLLEGE, "name": "会计2601", "term_id": term_id}).status_code, 409)
        # 在读人数对齐（2 在读，停用不计）
        row = {c["name"]: c for c in self.client.get(
            f"/api/training/staff/org/classes?college={COLLEGE}").json()["classes"]}["会计2601"]
        self.assertEqual(row["active_students"], 2)
        self.assertEqual(row["department_name"], "会计学院")
        self.assertEqual(row["term_name"], "2026-2027-1")
        # 归档过滤
        self.client.patch(f"/api/training/staff/org/classes/{cls_id}", json={"archived": True})
        visible = self.client.get(
            f"/api/training/staff/org/classes?college={COLLEGE}&include_archived=false"
        ).json()["classes"]
        self.assertEqual([c["name"] for c in visible], [])

    def test_class_clone_to_new_term(self):
        """按学期重建：新学期同名成功、同学期拒绝、学生名册全程不改写。"""
        self._login_admin()
        term_old = self.client.post("/api/training/staff/org/terms", json={
            "college": COLLEGE, "name": "2026-2027-1", "is_current": True}).json()["term"]["id"]
        term_new = self.client.post("/api/training/staff/org/terms", json={
            "college": COLLEGE, "name": "2026-2027-2"}).json()["term"]["id"]
        cls_id = self.client.post("/api/training/staff/org/classes", json={
            "college": COLLEGE, "name": "会计2601", "term_id": term_old}).json()["class"]["id"]
        # 同学期重建拒绝
        self.assertEqual(self.client.post(f"/api/training/staff/org/classes/{cls_id}/clone",
                                          json={"term_id": term_old}).status_code, 422)
        # 新学期重建成功；原班与学生在读口径不变
        res = self.client.post(f"/api/training/staff/org/classes/{cls_id}/clone",
                               json={"term_id": term_new})
        self.assertEqual(res.status_code, 200, res.text)
        clone = res.json()["clone"]
        self.assertEqual(clone["class"]["name"], "会计2601")
        self.assertEqual(clone["class"]["term_id"], term_new)
        with self.store.connect() as db:
            names = {r["student_no"]: r["class_name"] for r in db.execute(
                "SELECT student_no, class_name FROM student_info WHERE college=?", (COLLEGE,))}
        self.assertEqual(names["2026001"], "会计2601")  # 学生名册未被改写
        # 新学期已有同名班（刚克隆的），再次克隆同名 409；改名可绕开
        dup = self.client.post(f"/api/training/staff/org/classes/{cls_id}/clone",
                               json={"term_id": term_new})
        self.assertEqual(dup.status_code, 409)
        renamed = self.client.post(f"/api/training/staff/org/classes/{cls_id}/clone",
                                   json={"term_id": term_new, "name": "会计2701"})
        self.assertEqual(renamed.status_code, 200, renamed.text)

    def test_cross_college_reference_rejected(self):
        self._login_admin()
        other_term = self.client.post("/api/training/staff/org/terms", json={
            "college": "其他职业学院", "name": "X-2026-1"}).json()["term"]["id"]
        res = self.client.post("/api/training/staff/org/classes", json={
            "college": COLLEGE, "name": "会计2601", "term_id": other_term})
        self.assertEqual(res.status_code, 422, res.text)

    def test_org_requires_login(self):
        self.assertEqual(self.client.get(
            f"/api/training/staff/org/departments?college={COLLEGE}").status_code, 401)
        self.assertEqual(self.client.post("/api/training/staff/org/terms", json={
            "college": COLLEGE, "name": "T"}).status_code, 401)
        self.assertEqual(self.client.post("/api/training/staff/org/classes", json={
            "college": COLLEGE, "name": "C"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
