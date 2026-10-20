"""FR-U03 测试：教师批量导入（admin-only）+ 班级统计/整建制迁移/归属追溯。

第二期新增 Excel 名册直读：.xlsx 上传导入学生（表头识别、数字学号、
公式无缓存不补造、错误行带原因、坏文件/超限拒绝、未登录 401）。

第一期覆盖：教师账号批量建号（重复/错误行带原因、不静默）、只建 teacher 角色、
非管理员 403；班级聚合统计；整建制迁移只动在读学生且逐生写追溯、
原班级为空 404、同班级拒绝；未登录 401。
"""
from __future__ import annotations

import io
import tempfile
import unittest
import uuid
from pathlib import Path

import openpyxl
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import parse_student_xlsx, register

_passwords = PasswordHasher()
COOKIE_NAME = "test_roster_cookie"
NOW = "2026-10-11T21:00:00"
COLLEGE = "演示财经职业学院"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_bytes(rows: list[list], header: list | None = ("学号", "姓名", "班级")) -> bytes:
    """构造 .xlsx 字节流；rows 为每行单元格列表，header=None 表示无表头。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    if header is not None:
        ws.append(list(header))
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


class RosterAdminTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "roster.db")
        app = FastAPI()
        register(app, lambda: self.store, COOKIE_NAME)
        self.client = TestClient(app)
        self._seed()
        with self.store.connect() as db:
            staff=db.execute("SELECT id FROM college_user WHERE username=?",('admin01',)).fetchone()[0]
            db.execute("INSERT INTO teacher_student_quotas VALUES (?,?,?,1,?,?)",(staff,COLLEGE,50,staff,NOW))
            teacher=db.execute("SELECT id FROM college_user WHERE username='teacher01'").fetchone()[0]
            db.execute("INSERT INTO teacher_student_quotas VALUES (?,?,50,1,?,?)",(teacher,COLLEGE,staff,NOW))

    def tearDown(self):
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
            self.students = {}
            for no, name, cls, active in (
                    ("2026001", "张三", "会计2601", 1), ("2026002", "李四", "会计2601", 1),
                    ("2026003", "王五", "会计2601", 0),   # 停用：不随迁移走
                    ("2026004", "赵六", "会计2602", 1)):
                sid = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO student_info (id, college, student_no, name, class_name,"
                    " password_hash, email, active, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, COLLEGE, no, name, cls, _passwords.hash("StudentPass1!"), None, active, NOW))
                self.students[no] = sid

    def _login(self, username, password):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": username, "password": password})
        self.assertEqual(res.status_code, 200, res.text)

    # ---- 教师批量导入 -------------------------------------------------------

    def test_admin_import_teachers(self):
        self._login("admin01", "AdminPass1!")
        res = self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "NewStaff2026!",
            "rows": [
                {"username": "teacher02", "display_name": "王老师",
                 "email": "lecturer@demo.edu"},
                {"username": "teacher01", "display_name": "重复登录名"},
                {"username": "", "display_name": "缺登录名"},
                {"username": "teacher03", "display_name": ""},
            ]})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["import"]
        self.assertEqual([c["username"] for c in data["created"]], ["teacher02"])
        self.assertEqual(len(data["skipped"]), 3)
        reasons = {s["username"]: s["reason"] for s in data["skipped"]}
        self.assertIn("登录名已存在", reasons["teacher01"])
        self.assertIn("登录名必填", reasons[""])
        self.assertIn("姓名必填", reasons["teacher03"])
        # 建号可登录且角色为 teacher，密码哈希非明文
        self._login("teacher02", "NewStaff2026!")
        me = self.client.get("/api/training/staff/auth/me").json()["staff"]
        self.assertEqual(me["role"], "teacher")
        with self.store.connect() as db:
            row = db.execute("SELECT password_hash FROM college_user WHERE username=?",
                             ("teacher02",)).fetchone()
        self.assertNotIn("NewStaff2026!", row["password_hash"])

    def test_teacher_cannot_import_teachers(self):
        self._login("teacher01", "TeacherPass1!")
        res = self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "NewStaff2026!",
            "rows": [{"username": "teacher09", "display_name": "越权"}]})
        self.assertEqual(res.status_code, 403)
        with self.store.connect() as db:
            n = db.execute("SELECT count(*) n FROM college_user WHERE username=?",
                           ("teacher09",)).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_teacher_import_short_password_422(self):
        self._login("admin01", "AdminPass1!")
        res = self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "123",
            "rows": [{"username": "teacher04", "display_name": "短密码"}]})
        self.assertEqual(res.status_code, 422)

    # ---- 班级统计与整建制迁移 -------------------------------------------------

    def test_class_listing_counts(self):
        self._login("teacher01", "TeacherPass1!")
        data = self.client.get(
            f"/api/training/staff/classes?college={COLLEGE}").json()["classes"]
        by = {c["class_name"]: c for c in data}
        self.assertEqual(by["会计2601"]["students"], 3)
        self.assertEqual(by["会计2601"]["active_students"], 2)
        self.assertEqual(by["会计2602"]["active_students"], 1)

    def test_class_migration_with_history(self):
        self._login("teacher01", "TeacherPass1!")
        res = self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "会计2601", "to_class": "会计2701"})
        self.assertEqual(res.status_code, 200, res.text)
        mig = res.json()["migration"]
        self.assertEqual(mig["migrated"], 2)  # 只动在读学生
        with self.store.connect() as db:
            moved = {r["student_no"]: r["class_name"] for r in db.execute(
                "SELECT student_no, class_name FROM student_info WHERE college=?", (COLLEGE,))}
            self.assertEqual(moved["2026001"], "会计2701")
            self.assertEqual(moved["2026002"], "会计2701")
            self.assertEqual(moved["2026003"], "会计2601")  # 停用学生保持原班级
            hist = db.execute(
                """SELECT h.student_id, h.from_class, h.to_class, h.changed_by, u.username
                   FROM roster_class_history h LEFT JOIN college_user u ON u.id=h.changed_by""").fetchall()
            self.assertEqual(len(hist), 2)
            self.assertEqual({h["to_class"] for h in hist}, {"会计2701"})
            self.assertEqual({h["username"] for h in hist}, {"teacher01"})
            self.assertEqual({h["student_id"] for h in hist},
                             {self.students["2026001"], self.students["2026002"]})
        # 追溯端点可见，带学号姓名与操作人
        his = self.client.get(
            f"/api/training/staff/class-history?college={COLLEGE}").json()["history"]
        self.assertEqual(len(his), 2)
        self.assertEqual({r["student_no"] for r in his}, {"2026001", "2026002"})
        self.assertEqual({r["changed_by_name"] for r in his}, {"现教师"})

    def test_class_migration_rejections(self):
        self._login("teacher01", "TeacherPass1!")
        # 同班级
        self.assertEqual(self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "会计2601", "to_class": "会计2601"}).status_code, 422)
        # 空班级（pydantic 422）
        self.assertEqual(self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "会计2601", "to_class": ""}).status_code, 422)
        # 不存在的班级 → 404
        self.assertEqual(self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "不存在的班", "to_class": "会计2701"}).status_code, 404)

    def test_roster_auth_required(self):
        self.assertEqual(self.client.get(
            f"/api/training/staff/classes?college={COLLEGE}").status_code, 401)
        self.assertEqual(self.client.post("/api/training/staff/classes/migrate", json={
            "college": COLLEGE, "from_class": "a", "to_class": "b"}).status_code, 401)
        self.assertEqual(self.client.post("/api/training/staff/teachers/import", json={
            "college": COLLEGE, "default_password": "x1Pass2026!!",
            "rows": [{"username": "someone", "display_name": "某人"}]}).status_code, 401)

    # ---- Excel 名册直读（FR-U03 第二期） -------------------------------------

    def _upload_xlsx(self, data: bytes, *, college: str = COLLEGE,
                     default_password: str = "NewStu2026!",
                     default_class_name: str = ""):
        return self.client.post(
            "/api/training/staff/students/import-xlsx",
            files={"file": ("roster.xlsx", data, XLSX_MIME)},
            data={"college": college, "default_password": default_password,
                  "default_class_name": default_class_name})

    def test_student_import_xlsx_basic(self):
        """教师上传 .xlsx：数字学号转纯文本、班级逐行生效、库内重复行带原因跳过。"""
        self._login("teacher01", "TeacherPass1!")
        data = xlsx_bytes([
            [2026005, "钱七", "会计2601"],      # int 学号
            ["2026006", "孙八", None],          # 班级留空 → 默认班级兜底
            ["2026001", "学号重复", "会计2603"],  # 与库内 seed 冲突
        ])
        res = self._upload_xlsx(data, default_class_name="会计2602")
        self.assertEqual(res.status_code, 200, res.text)
        result = res.json()["import"]
        self.assertEqual([c["student_no"] for c in result["created"]], ["2026005", "2026006"])
        by_no = {c["student_no"]: c for c in result["created"]}
        self.assertEqual(by_no["2026005"]["class_name"], "会计2601")
        self.assertEqual(by_no["2026006"]["class_name"], "会计2602")
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("已存在学号 2026001", result["skipped"][0]["reason"])
        with self.store.connect() as db:
            n = db.execute("SELECT count(*) n FROM student_info WHERE college=?",
                           (COLLEGE,)).fetchone()["n"]
        self.assertEqual(n, 6)  # seed 4 + 新增 2

    def test_student_import_xlsx_header_variants(self):
        """表头变体（学生学号/学生姓名/行政班级）可识别；整值浮点学号不带小数点。"""
        self._login("teacher01", "TeacherPass1!")
        data = xlsx_bytes([[2026007.0, "周九", "会计2601"]],
                          header=["学生学号", "学生姓名", "行政班级"])
        res = self._upload_xlsx(data)
        self.assertEqual(res.status_code, 200, res.text)
        created = res.json()["import"]["created"]
        self.assertEqual([c["student_no"] for c in created], ["2026007"])

    def test_student_import_xlsx_rejects_bad_structure(self):
        """无表头、缺姓名列、空表、无数据行：整体拒绝（不猜列序）。"""
        self._login("teacher01", "TeacherPass1!")
        cases = [
            (xlsx_bytes([["2026005", "钱七", "会计2601"]], header=None), 422),   # 无表头
            (xlsx_bytes([["2026005", "钱七"]], header=["学号", "班级"]), 422),   # 缺姓名列
            (xlsx_bytes([], header=None), 422),                                  # 空工作表
            (xlsx_bytes([[None, None, None]]), 422),                             # 表头外无数据行
        ]
        for data, expected in cases:
            with self.subTest(data_len=len(data)):
                res = self._upload_xlsx(data)
                self.assertEqual(res.status_code, expected, res.text)

    def test_student_import_xlsx_blank_rows_and_error_rows(self):
        """全空行忽略；缺学号/缺姓名的行带原因跳过，不错误行中断整批。"""
        self._login("teacher01", "TeacherPass1!")
        data = xlsx_bytes([
            [None, None, None],               # 空行：忽略
            ["2026008", "吴十", "会计2601"],  # 有效
            [None, "缺学号", "会计2601"],     # 行级错误
            ["2026009", None, "会计2601"],    # 行级错误
        ])
        res = self._upload_xlsx(data)
        self.assertEqual(res.status_code, 200, res.text)
        result = res.json()["import"]
        self.assertEqual([c["student_no"] for c in result["created"]], ["2026008"])
        self.assertEqual(len(result["skipped"]), 2)
        reasons = " | ".join(s["reason"] for s in result["skipped"])
        self.assertIn("学号", reasons)
        self.assertIn("姓名", reasons)

    def test_student_import_xlsx_formula_without_cache(self):
        """公式单元格只读缓存值：openpyxl 无计算引擎缓存为空 → 该行按缺学号跳过，不补造。"""
        self._login("teacher01", "TeacherPass1!")
        data = xlsx_bytes([["=B2", "公式生", "会计2601"]])
        res = self._upload_xlsx(data)
        self.assertEqual(res.status_code, 200, res.text)
        result = res.json()["import"]
        self.assertEqual(result["created"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("学号", result["skipped"][0]["reason"])

    def test_student_import_xlsx_bad_bytes(self):
        """非 .xlsx 内容：400 拒绝并提示格式要求。"""
        self._login("teacher01", "TeacherPass1!")
        res = self._upload_xlsx(b"this is not an excel file")
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIn(".xlsx", res.json()["detail"])

    def test_student_import_xlsx_requires_login(self):
        res = self._upload_xlsx(xlsx_bytes([["2026005", "钱七", "会计2601"]]))
        self.assertEqual(res.status_code, 401)
        with self.store.connect() as db:
            n = db.execute("SELECT count(*) n FROM student_info WHERE college=?",
                           (COLLEGE,)).fetchone()["n"]
        self.assertEqual(n, 4)  # 未入库

    def test_parse_student_xlsx_limits(self):
        """文件大小与数据行数上限整体拒绝（单元级验证，不静默截断）。"""
        with self.assertRaises(Exception) as ctx:
            parse_student_xlsx(b"x" * 11, max_bytes=10)
        self.assertIn("上限", str(ctx.exception))
        rows = [[f"9{i:04d}", f"生{i}", "会计2601"] for i in range(6)]
        with self.assertRaises(Exception) as ctx:
            parse_student_xlsx(xlsx_bytes(rows), max_rows=5)
        self.assertIn("上限", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
