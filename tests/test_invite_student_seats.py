"""FR-U06 尾巴：院校初始学生额度既不能静默落 0，也不能吃掉旧库升级路径。

覆盖：
1. 高校创始码未指定学生额度 → 落默认档（与核销默认档同源），不是 0；
2. 显式 0 仍按 0 执行（「先建校、后开学生」的刻意用法）；
3. 企业创始码未指定 → 恒为 0（学生额度不适用于企业）；
4. 旧库院校回填补默认档而不是 0（写成 0 会让升级后的院校一个学生都加不进来）。
"""

import tempfile
import unittest
from pathlib import Path

import auth_support

from webapp import university_members as service
from webapp.storage import Store
from webapp.training_portal import _DEFAULT_STUDENT_QUOTA, college_quota_state


class InviteStudentSeatsTests(unittest.TestCase):
    password = "SecureForest!2026"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "seats.db", keepalive=True)
        self.addCleanup(self.store.close)
        self.root = self.store.create_initial_admin("rootadmin", self.password, "平台", "root")

    def founder(self, invite, email):
        user, _ = self.store.register_with_code(
            email, auth_support.register_code(self.store, email), invite["code"], self.password,
            browser_session=auth_support.BROWSER,
        )
        with self.store.connect() as db:
            link = db.execute("SELECT staff_id FROM university_identity_links WHERE user_id=?", (user["id"],)).fetchone()
            staff = db.execute("SELECT * FROM college_user WHERE id=?", (link["staff_id"],)).fetchone()
        return user, dict(staff)

    def test_university_default_is_not_zero(self):
        invite = self.store.create_invite_code(self.root, "示例财经大学", 4, org_type="university")
        self.assertEqual(invite["student_seats"], _DEFAULT_STUDENT_QUOTA)
        _user, staff = self.founder(invite, "founder@example.com")
        with self.store.connect() as db:
            row = db.execute("SELECT teacher_quota,student_quota FROM college_quota WHERE college=?",
                             (staff["college"],)).fetchone()
            state = college_quota_state(db, staff["college"])
        self.assertEqual((row["teacher_quota"], row["student_quota"]), (4, _DEFAULT_STUDENT_QUOTA))
        self.assertEqual(state["student_remaining"], _DEFAULT_STUDENT_QUOTA)
        self.assertTrue(state["configured"])

    def test_explicit_zero_is_honoured(self):
        invite = self.store.create_invite_code(self.root, "零学生席大学", 3, org_type="university", student_seats=0)
        self.assertEqual(invite["student_seats"], 0)
        _user, staff = self.founder(invite, "none@example.com")
        with self.store.connect() as db:
            state = college_quota_state(db, staff["college"])
        self.assertEqual(state["student_quota"], 0)
        self.assertEqual(state["student_remaining"], 0)

    def test_enterprise_has_no_student_seats(self):
        invite = self.store.create_invite_code(self.root, "示例税务师事务所", 5, org_type="enterprise")
        self.assertEqual(invite["student_seats"], 0)
        with self.assertRaises(ValueError):
            self.store.create_invite_code(self.root, "示例税务师事务所", 5, org_type="enterprise", student_seats=10)

    def test_legacy_org_backfill_gets_default_students(self):
        """旧库没有学生额度概念：回填必须是默认档，0 会直接锁死学生入口。"""
        org_id = "org-legacy-university"
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS org_profiles (org_id TEXT PRIMARY KEY, org_type TEXT)")
            db.execute("INSERT INTO org_profiles VALUES (?, 'university')", (org_id,))
            db.execute("INSERT INTO org_settings(org_id,org_type,display_name,updated_at) VALUES (?,'university',?,?)",
                       (org_id, "旧库财经大学", service.now()))
            db.execute("INSERT INTO org_quota(org_id,seats,updated_at) VALUES (?,?,?)", (org_id, 6, service.now()))
            db.execute("""INSERT INTO users(id,username,password_hash,display_name,role,org_id,active,email,created_at)
                          VALUES ('u-legacy','legacyadmin','x','旧库管理员','org_admin',?,1,'legacy@example.com',?)""",
                       (org_id, service.now()))
            db.execute("DELETE FROM university_organizations WHERE org_id=? OR college=?", (org_id, org_id))
            db.execute("DELETE FROM college_user WHERE college=?", (org_id,))
            db.execute("DELETE FROM college_quota WHERE college=?", (org_id,))
            db.execute("COMMIT")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            service.backfill_existing_organizations(db)
            db.execute("COMMIT")
        with self.store.connect() as db:
            row = db.execute("SELECT teacher_quota,student_quota FROM college_quota WHERE college=?",
                             (org_id,)).fetchone()
            link = db.execute("SELECT staff_id FROM university_identity_links WHERE user_id='u-legacy'").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(link)
        self.assertEqual(row["teacher_quota"], 6)
        self.assertEqual(row["student_quota"], _DEFAULT_STUDENT_QUOTA)


if __name__ == "__main__":
    unittest.main()
