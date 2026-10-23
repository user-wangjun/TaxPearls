"""Real invitation, delegated quota, identity and platform-only capacity contracts."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import auth_support
from fastapi.testclient import TestClient

from webapp import app as application, email_auth, invitations, members, university_members as service
from webapp.access import AccessDenied
from webapp.login_guard import LoginGuard
from webapp.storage import Store, _passwords
from webapp.training_portal import login_staff, login_student, staff_for_token, student_for_token


class UniversityMembershipTests(unittest.TestCase):
    password = "SecureForest!2026"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "members.db", keepalive=True)
        self.addCleanup(self.store.close)
        self.root = self.store.create_initial_admin("rootadmin", self.password, "平台", "root")
        invite = self.store.create_invite_code(self.root, "示例财经大学", 4, org_type="university", student_seats=10)
        self.owner, _ = self.store.register_with_code(
            "founder@example.com",
            auth_support.register_code(self.store, "founder@example.com"),
            invite["code"],
            self.password,
            browser_session=auth_support.BROWSER,
        )
        with self.store.connect() as db:
            linked = db.execute(
                "SELECT staff_id FROM university_identity_links WHERE user_id=?", (self.owner["id"],)
            ).fetchone()
            self.actor = dict(db.execute("SELECT * FROM college_user WHERE id=?", (linked[0],)).fetchone())
        self.college = self.actor["college"]
        self.patches = patch.multiple(application, store=self.store, login_guard=LoginGuard())
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.client = TestClient(application.app)

    def link(self, role="student", actor=None):
        actor = actor or self.actor
        result = service.mutate_link(self.store, actor, role=role)
        return next(l for l in result["links"] if l["role"] == role)

    def join(self, link, email="learner@example.com", student_no="2026001", **changes):
        code = link["path"].rsplit("/", 1)[1]
        proof = email_auth.issue(self.store, email, "register", auth_support.BROWSER, invite_code=code)
        payload = {
            "invite_code": code,
            "email": email,
            "password": self.password,
            "code": proof["code"] if proof else "",
            "display_name": "测试成员",
            "student_no": student_no,
            "accepted": True,
            **changes,
        }
        return service.redeem(self.store, service.JoinBody(**payload), auth_support.BROWSER)

    def allocate(self, seats, actor=None, target=None):
        actor = actor or self.actor
        target = target or actor["id"]
        with self.store.connect() as db:
            revision = service.teacher_state(db, target)["revision"]
        return service.allocate(self.store, actor, target, seats, revision)

    def teacher(self, email="teacher@example.com"):
        link = self.link("teacher")
        user, token = self.join(link, email)
        with self.store.connect() as db:
            row = db.execute("SELECT staff_id FROM university_identity_links WHERE user_id=?", (user["id"],)).fetchone()
            return dict(db.execute("SELECT * FROM college_user WHERE id=?", (row[0],)).fetchone()), user, token

    def staff_cookie(self, actor=None):
        actor = actor or self.actor
        result = login_staff(self.store, actor["username"], self.password)
        self.client.cookies.set("taxpearls_staff_session", result["token"])

    def root_cookie(self):
        self.client.cookies.set("taxpearls_session", self.store.authenticate(self.root["username"], self.password)[1])

    def test_founder_has_one_staff_identity_and_zero_own_student_allocation(self):
        snapshot = service.overview(self.store, self.actor)
        self.assertTrue(snapshot["administrator"])
        self.assertEqual(
            (
                snapshot["quota"]["teachers_used"],
                snapshot["quota"]["teacher_quota"],
                snapshot["quota"]["student_quota"],
            ),
            (1, 4, 10),
        )
        self.assertEqual(snapshot["mine"]["seats"], 0)
        response = self.client.post("/api/login", json={"username": "founder@example.com", "password": self.password})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("taxpearls_staff_session", response.cookies)
        self.assertEqual(self.client.get("/api/me").json()["workspace"], "/training-staff")

    def test_self_allocation_and_student_join_use_teacher_quota(self):
        self.allocate(2)
        link = self.link()
        self.assertEqual(service.overview(self.store, self.actor)["mine"]["used"], 0)
        student, token = self.join(link)
        snapshot = service.overview(self.store, self.actor)
        self.assertEqual(
            (snapshot["mine"]["used"], snapshot["mine"]["remaining"], snapshot["quota"]["unallocated"]), (1, 1, 8)
        )
        with self.store.connect() as db:
            profile = db.execute(
                "SELECT student_id FROM university_identity_links WHERE user_id=?", (student["id"],)
            ).fetchone()[0]
            self.assertEqual(
                db.execute(
                    "SELECT teacher_id FROM student_teacher_memberships WHERE student_id=?", (profile,)
                ).fetchone()[0],
                self.actor["id"],
            )
        response = service.attach_session(self.store, __import__("fastapi").Response(), student)
        self.assertEqual(response, "/training-certificates")
        self.assertEqual(self.store.user_for_token(token)["id"], student["id"])

    def test_teacher_invitation_does_not_consume_until_join_and_teacher_cannot_allocate(self):
        teacher, user, _ = self.teacher()
        snapshot = service.overview(self.store, teacher)
        self.assertFalse(snapshot["administrator"])
        self.assertIsNone(snapshot["quota"])
        self.assertEqual(snapshot["mine"]["seats"], 0)
        with self.assertRaises(AccessDenied):
            service.allocate(self.store, teacher, teacher["id"], 3, 0)
        with self.assertRaises(AccessDenied):
            service.mutate_link(self.store, teacher, role="teacher")
        self.allocate(3, target=teacher["id"])
        student_link = self.link(actor=teacher)
        self.join(student_link, "student2@example.com", "2026002")
        self.staff_cookie(teacher)
        listing = self.client.get("/api/training/membership/students")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(len(listing.json()["students"]), 1)
        self.assertEqual(len(service.links(self.store, teacher)["links"]), 1)
        self.assertEqual(user["role"], "teacher")

    def test_overallocation_stale_revision_and_lower_than_used_are_rejected(self):
        self.allocate(10)
        teacher, _, _ = self.teacher()
        with self.assertRaises(AccessDenied):
            service.allocate(self.store, self.actor, teacher["id"], 1, 0)
        with self.assertRaises(AccessDenied):
            service.allocate(self.store, self.actor, self.actor["id"], 10, 0)
        self.join(self.link())
        with self.assertRaises(AccessDenied):
            self.allocate(0)

    def test_school_admin_cannot_change_total_capacity_through_any_endpoint(self):
        self.staff_cookie()
        payload = {"college": self.college, "teacher_quota": 100, "student_quota": 1000, "revision": 1}
        response = self.client.patch("/api/training/staff/org/quota", json=payload)
        self.assertEqual(response.status_code, 403, response.text)
        self.client.cookies.set("taxpearls_session", self.store.authenticate(self.owner["username"], self.password)[1])
        response = self.client.put(
            f"/api/universities/{self.college}/capacity", json={k: v for k, v in payload.items() if k != "college"}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.root_cookie()
        response = self.client.put(
            f"/api/universities/{self.college}/capacity", json={"teacher_quota": 8, "student_quota": 20, "revision": 1}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["student_quota"], 20)
        self.assertEqual(
            self.client.put(
                f"/api/universities/{self.college}/capacity",
                json={"teacher_quota": 8, "student_quota": 20, "revision": 1},
            ).status_code,
            409,
        )

    def test_capacity_cannot_shrink_below_reserved_quota(self):
        self.allocate(10)
        self.root_cookie()
        response = self.client.put(
            f"/api/universities/{self.college}/capacity", json={"teacher_quota": 4, "student_quota": 9, "revision": 1}
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_capacity_audit_retains_actor_ip_versions_and_atomic_failure(self):
        self.root_cookie()
        client = TestClient(application.app, client=('2001:db8::21', 12345))
        client.cookies.update(self.client.cookies)
        path = f'/api/universities/{self.college}/capacity'
        response = client.put(path, json={'teacher_quota': 6, 'student_quota': 20, 'revision': 1},
                              headers={'X-Forwarded-For': '203.0.113.99'})
        self.assertEqual(response.status_code, 200, response.text)
        record = next(r for r in self.store.list_logs(self.root) if r['action'] == 'set_university_capacity')
        detail = json.loads(record['detail'])
        self.assertEqual((detail['before'], detail['after']),
                         ({'teacher_quota': 4, 'student_quota': 10}, {'teacher_quota': 6, 'student_quota': 20}))
        self.assertEqual((detail['source_ip'], detail['old_revision'], detail['new_revision']), ('2001:db8::21', 1, 2))
        self.assertEqual((detail['actor']['id'], detail['organization_id'], detail['college']),
                         (self.root['id'], self.owner['org_id'], self.college))
        with self.store.connect() as db:
            before = service.school_state(db, self.college)
            count = db.execute('SELECT COUNT(*) FROM university_capacity_events').fetchone()[0]
        logs = self.store.list_logs(self.root)
        with patch.object(members, 'log', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                client.put(path, json={'teacher_quota': 7, 'student_quota': 30, 'revision': 2})
        with self.store.connect() as db:
            self.assertEqual(service.school_state(db, self.college), before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM university_capacity_events').fetchone()[0], count)
        self.assertEqual(self.store.list_logs(self.root), logs)
        response = client.put(path, json={'teacher_quota': 8, 'student_quota': 30, 'revision': 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.list_logs(self.root), logs)

    def test_member_overview_uses_school_capacity_after_save_and_activation_changes(self):
        self.allocate(2)
        student, _ = self.join(self.link())
        self.root_cookie()
        path = f"/api/members/{self.owner['org_id']}"
        result = self.client.get(path).json()
        self.assertEqual(result['org_type'], 'university')
        school = result['university_quota']
        self.assertEqual((school['teachers_used'], school['teacher_quota'],
                          school['students_used'], school['student_quota'], school['allocated']), (1, 4, 1, 10, 2))
        response = self.client.put(f"/api/universities/{self.college}/capacity", json={
            'teacher_quota': 6, 'student_quota': 20, 'revision': school['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        school = self.client.get(path).json()['university_quota']
        self.assertEqual((school['teacher_quota'], school['student_quota'], school['unallocated']), (6, 20, 18))
        for active, expected in ((False, 0), (True, 1)):
            response = self.client.put(f"{path}/{student['id']}/active", json={
                'active': active, 'expected_active': not active})
            self.assertEqual(response.status_code, 200, response.text)
            school = self.client.get(path).json()['university_quota']
            self.assertEqual((school['students_used'], school['student_remaining']), (expected, 20 - expected))
            self.assertEqual((school['teachers_used'], school['teacher_quota']), (1, 6))

    def test_university_rejects_legacy_quota_edits_and_missing_link_has_no_fallback(self):
        self.root_cookie()
        path = f"/api/members/{self.owner['org_id']}"
        before = self.client.get(path).json()
        response = self.client.put(path + '/quota', json={'seats': 9, 'revision': before['quota']['revision']})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get(path).json(), before)
        with self.store.connect() as db:
            db.execute('DELETE FROM university_organizations WHERE org_id=?', (self.owner['org_id'],))
        self.assertIsNone(self.client.get(path).json()['university_quota'])

    def test_member_school_capacity_keeps_organization_access_boundary(self):
        outsider = self.store.create_user('outsider', self.password, '其他机构', 'org_admin', org_id='other')
        self.client.cookies.set('taxpearls_session', self.store.authenticate(outsider['username'], self.password)[1])
        self.assertEqual(self.client.get(f"/api/members/{self.owner['org_id']}").status_code, 404)

    def test_link_copy_secret_is_encrypted_and_refresh_disable_are_revision_bound(self):
        link = self.link()
        code = link["path"].split("/")[-1]
        with closing(sqlite3.connect(self.store.path)) as db:
            stored = db.execute(
                "SELECT code_secret FROM training_invitation_links WHERE id=?", (link["id"],)
            ).fetchone()[0]
        self.assertNotIn(code, stored)
        self.assertTrue(stored.startswith("tp-field-v1:"))
        disabled = service.mutate_link(self.store, self.actor, link_id=link["id"], revision=1, active=False)["links"][0]
        with self.assertRaises(ValueError):
            service.preview(self.store, code)
        rotated = service.mutate_link(self.store, self.actor, link_id=link["id"], revision=disabled["revision"])[
            "links"
        ][0]
        self.assertFalse(rotated["active"])
        with self.assertRaises(AccessDenied):
            service.mutate_link(self.store, self.actor, link_id=link["id"], revision=1, active=True)
        service.mutate_link(self.store, self.actor, link_id=link["id"], revision=rotated["revision"], active=True)
        with self.assertRaises(ValueError):
            service.preview(self.store, code)
        self.assertEqual(
            service.preview(self.store, rotated["path"].split("/")[-1])["teacher"], self.actor["display_name"]
        )

    def test_failed_capacity_redemption_preserves_proof_and_creates_nothing(self):
        link = self.link()
        code = link["path"].split("/")[-1]
        proof = email_auth.issue(self.store, "learner@example.com", "register", auth_support.BROWSER, invite_code=code)
        body = service.JoinBody(
            invite_code=code,
            email="learner@example.com",
            password=self.password,
            code=proof["code"],
            display_name="测试",
            student_no="2026001",
            accepted=True,
        )
        with self.assertRaises(AccessDenied):
            service.redeem(self.store, body, auth_support.BROWSER)
        self.allocate(1)
        user, _ = service.redeem(self.store, body, auth_support.BROWSER)
        self.assertEqual(user["role"], "student")

    def test_parallel_redemption_only_uses_one_last_teacher_seat(self):
        self.allocate(1)
        link = self.link()
        code = link["path"].split("/")[-1]
        payloads = []
        for n in range(2):
            email = f"person{n}@example.com"
            proof = email_auth.issue(self.store, email, "register", auth_support.BROWSER, invite_code=code)
            payloads.append(
                service.JoinBody(
                    invite_code=code,
                    email=email,
                    password=self.password,
                    code=proof["code"],
                    display_name="测试",
                    student_no=str(100 + n),
                    accepted=True,
                )
            )

        def attempt(body):
            try:
                service.redeem(self.store, body, auth_support.BROWSER)
                return True
            except AccessDenied:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, payloads))
        self.assertEqual(sum(results), 1)
        self.assertEqual(service.overview(self.store, self.actor)["mine"]["used"], 1)

    def test_other_teacher_cannot_see_or_change_student_or_invitation(self):
        self.allocate(1)
        link = self.link()
        student, _ = self.join(link)
        teacher, _, _ = self.teacher()
        with self.store.connect() as db:
            student_id = db.execute(
                "SELECT student_id FROM university_identity_links WHERE user_id=?", (student["id"],)
            ).fetchone()[0]
        self.staff_cookie(teacher)
        self.assertEqual(self.client.get("/api/training/staff/students").json()["students"], [])
        self.assertEqual(
            self.client.post(f"/api/training/staff/students/{student_id}/toggle", json={"active": False}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/training/staff/students/{student_id}/reset-password", json={"new_password": "ChangedForest!2026"}
            ).status_code,
            404,
        )
        with self.assertRaises(AccessDenied):
            service.mutate_link(self.store, teacher, link_id=link["id"], revision=1, active=False)

    def test_password_change_sync_and_member_disable_revoke_both_sessions(self):
        teacher, user, main_token = self.teacher()
        self.staff_cookie(teacher)
        old_staff = self.client.cookies.get("taxpearls_staff_session")
        result = self.client.post(
            "/api/training/staff/auth/change-password",
            json={"old_password": self.password, "new_password": "ChangedForest!2026"},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIsNone(self.store.user_for_token(main_token))
        self.assertIsNone(staff_for_token(self.store, old_staff))
        self.assertIsNotNone(self.store.authenticate(user["username"], "ChangedForest!2026"))
        logged = login_staff(self.store, teacher["username"], "ChangedForest!2026")
        members.set_active(self.store, self.root, user["org_id"], user["id"], False, True)
        self.assertIsNone(staff_for_token(self.store, logged["token"]))
        members.set_active(self.store, self.root, user["org_id"], user["id"], True, False)
        self.assertIsNone(staff_for_token(self.store, logged["token"]))

    def test_student_disable_release_reenable_does_not_bypass_teacher_capacity(self):
        from webapp.training_portal import set_student_active

        self.allocate(1)
        link = self.link()
        user, _ = self.join(link)
        session = login_student(self.store, "2026001", self.password, college=self.college)
        sid = session["student"]["id"]
        set_student_active(self.store, sid, False, college=self.college, actor=self.actor)
        self.assertIsNone(student_for_token(self.store, session["token"]))
        self.join(link, "person2@example.com", "2026002")
        with self.assertRaises(AccessDenied):
            set_student_active(self.store, sid, True, college=self.college, actor=self.actor)
        self.assertIsNone(self.store.authenticate(user["username"], self.password))

    def test_legacy_unassigned_students_remain_reserved_and_can_be_claimed_once(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                (
                    "legacy",
                    self.college,
                    "old01",
                    "历史学生",
                    None,
                    _passwords.hash(self.password),
                    None,
                    service.now(),
                ),
            )
        with self.assertRaises(AccessDenied):
            self.allocate(10)
        result = service.assign_legacy(self.store, self.actor, "legacy", self.actor["id"])
        self.assertEqual(
            (result["mine"]["seats"], result["mine"]["used"], result["quota"]["unassigned_students"]), (1, 1, 0)
        )
        with self.assertRaises(AccessDenied):
            service.assign_legacy(self.store, self.actor, "legacy", self.actor["id"])

    def test_join_api_requires_explicit_consent_and_denies_client_role_override(self):
        self.allocate(1)
        link = self.link()
        code = link["path"].split("/")[-1]
        response = self.client.get("/p/" + code)
        self.assertEqual(response.status_code, 200)
        self.assertIn("join-form", response.text)
        preview = self.client.get("/api/join/" + code)
        self.assertEqual(preview.json()["organization"], "示例财经大学")
        body = {"invite_code": code, "email": "learner@example.com", "password": self.password, "accepted": False}
        self.assertEqual(self.client.post("/api/join", json=body).status_code, 422)
        self.assertEqual(
            self.client.post("/api/join", json={**body, "accepted": True, "role": "admin"}).status_code, 422
        )
        self.assertEqual(service.overview(self.store, self.actor)["mine"]["used"], 0)

    def test_employee_links_use_the_same_landing_and_reject_school_roles(self):
        owner = self.store.create_user("enterprise", self.password, "企业管理员", "org_admin", "enterprise")
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO org_settings(org_id,org_type,display_name,updated_at) VALUES ('enterprise','enterprise','企业甲',?)",
                (service.now(),),
            )
        members.set_quota(self.store, self.root, "enterprise", 3, 0)
        link = invitations.issue(self.store, owner, "enterprise", "accountant")
        ledger = invitations.overview(self.store, owner, "enterprise")
        self.assertEqual(ledger["links"][0]["path"], link["path"])
        self.assertNotIn("token_hash", json.dumps(ledger))
        with self.assertRaises(AccessDenied):
            invitations.issue(self.store, owner, "enterprise", "teacher")
        with self.assertRaises(AccessDenied):
            invitations.issue(self.store, self.owner, self.owner["org_id"], "teacher")
        self.assertFalse(service.preview(self.store, link["code"])["university"])
        user, _ = self.join(link, "employee@example.com")
        self.assertEqual((user["role"], user["org_id"]), ("accountant", "enterprise"))
        self.assertEqual(user["display_name"], "测试成员")

    def test_anonymous_teaching_entries_return_to_the_one_login_url(self):
        main = self.client.get("/").content
        for path in ("/training-staff", "/training-certificates"):
            with self.subTest(path=path):
                page = self.client.get(path, follow_redirects=False)
                self.assertEqual(page.status_code, 307)
                self.assertEqual(page.headers["location"], "/")
                self.assertEqual(page.headers["cache-control"], "no-store")
                self.assertEqual(self.client.get(path).content, main)
        signed = self.client.post(
            "/api/training/staff/auth/login", json={"username": self.owner["email"].upper(), "password": self.password}
        )
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertIn('id="universityMembers"', self.client.get("/training-staff").text)
        self.assertEqual(self.client.get("/training-certificates").content, main)
        self.client.post("/api/training/staff/auth/logout")
        self.assertEqual(self.client.get("/training-staff").content, main)

    def test_student_can_use_verified_email_from_the_shared_login_page(self):
        self.allocate(1)
        student, _ = self.join(self.link())
        signed = self.client.post(
            "/api/training/auth/login", json={"student_no": student["email"].upper(), "password": self.password}
        )
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertIn('id="examWorkspace"', self.client.get("/training-certificates").text)
        self.assertIn('id="authScreen"', self.client.get("/training-staff").text)

    def test_one_login_api_selects_workspaces_from_account_identity(self):
        teacher, user, _ = self.teacher()
        self.allocate(1)
        student, _ = self.join(self.link())
        company = self.store.create_user("company", self.password, "企业管理员", "org_admin", "company")
        cases = (
            (self.owner["email"], self.owner["id"], "/training-staff"),
            (user["email"], user["id"], "/training-staff"),
            (student["email"], student["id"], "/training-certificates"),
            ("2026001", student["id"], "/training-certificates"),
            (company["username"], company["id"], None),
            (self.root["username"], self.root["id"], None),
        )
        for identifier, user_id, workspace in cases:
            with self.subTest(identifier=identifier):
                self.client.post("/api/logout")
                signed = self.client.post("/api/login", json={"username": identifier, "password": self.password})
                self.assertEqual(signed.status_code, 200, signed.text)
                self.assertEqual(signed.json()["user"]["workspace"], workspace)
                me = self.client.get("/api/me").json()
                self.assertEqual((me["id"], me["workspace"]), (user_id, workspace))
                self.assertEqual(self.client.get("/api/status").json()["user"]["workspace"], workspace)

    def test_email_code_login_returns_the_same_identity_workspace(self):
        proof = email_auth.issue(self.store, self.owner["email"], "login", auth_support.BROWSER)
        self.client.cookies.set("taxpearls_email_browser", auth_support.BROWSER)
        signed = self.client.post("/api/auth/email/login", json={"email": self.owner["email"], "code": proof["code"]})
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertEqual(signed.json()["user"]["workspace"], "/training-staff")
        self.assertEqual(self.client.get("/api/me").json()["workspace"], "/training-staff")

    def test_legacy_accounts_use_the_shared_entry_without_creating_main_identities(self):
        from webapp.training_portal import create_student

        with self.store.connect() as db:
            db.execute(
                """INSERT INTO college_user(id,username,password_hash,display_name,college,email,role,created_at)
                VALUES ('legacy-teacher','legacyteacher',?,'旧教师','旧学校','legacyteacher@example.com','teacher',?)""",
                (_passwords.hash(self.password), service.now()),
            )
            original_users = db.execute("SELECT count(*) FROM users").fetchone()[0]
            original_links = db.execute("SELECT count(*) FROM university_identity_links").fetchone()[0]
        create_student(
            self.store, "旧学校", "LEGACY100", "旧学生", initial_password=self.password, email="oldlearner@example.com"
        )
        for identifier, workspace in (("legacyteacher", "/training-staff"), ("LEGACY100", "/training-certificates")):
            signed = self.client.post("/api/login", json={"username": identifier, "password": self.password})
            self.assertEqual(signed.status_code, 200, signed.text)
            self.assertEqual(self.client.get("/api/me").json()["workspace"], workspace)
            self.assertEqual(self.client.get("/api/users").status_code, 401)
            self.assertEqual(self.client.get(workspace).status_code, 200)
            self.client.post("/api/logout")
            self.assertEqual(self.client.get("/api/me").status_code, 401)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], original_users)
            self.assertEqual(db.execute("SELECT count(*) FROM university_identity_links").fetchone()[0], original_links)

    def test_ambiguous_legacy_credentials_cannot_select_an_identity(self):
        from webapp.training_portal import create_student

        with self.store.connect() as db:
            db.execute(
                """INSERT INTO college_user(id,username,password_hash,display_name,college,role,created_at)
                VALUES ('ambiguous-teacher','samelegacy',?,'教师','旧学校','teacher',?)""",
                (_passwords.hash(self.password), service.now()),
            )
        create_student(self.store, "旧学校", "samelegacy", "学生", initial_password=self.password)
        signed = self.client.post("/api/login", json={"username": "samelegacy", "password": self.password})
        self.assertEqual(signed.status_code, 409, signed.text)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_disabled_primary_email_never_falls_through_to_a_legacy_alias(self):
        primary = self.store.create_user(
            "disabledmain", self.password, "停用账号", "accountant", "business", "disabled@example.com"
        )
        with self.store.connect() as db:
            db.execute("UPDATE users SET active=0 WHERE id=?", (primary["id"],))
            db.execute(
                """INSERT INTO college_user(id,username,password_hash,display_name,college,role,created_at)
                VALUES ('legacy-alias','disabled@example.com',?,'旧教师','旧学校','teacher',?)""",
                (_passwords.hash(self.password), service.now()),
            )
        signed = self.client.post("/api/login", json={"username": "disabled@example.com", "password": self.password})
        self.assertEqual(signed.status_code, 401, signed.text)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_account_switch_clears_previous_teaching_permissions(self):
        owner = self.client.post("/api/login", json={"username": self.owner["email"], "password": self.password})
        self.assertEqual(owner.status_code, 200, owner.text)
        self.assertEqual(self.client.get("/api/training/membership").status_code, 200)
        self.allocate(1)
        student, _ = self.join(self.link())
        signed = self.client.post("/api/login", json={"username": student["email"], "password": self.password})
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertEqual(self.client.get("/api/training/membership").status_code, 401)
        self.assertEqual(self.client.get("/api/training/auth/me").status_code, 200)
        employee = self.store.create_user("switcher", self.password, "员工", "accountant", "business")
        switched = self.client.post("/api/login", json={"username": employee["username"], "password": self.password})
        self.assertEqual(switched.status_code, 200, switched.text)
        self.assertEqual(self.client.get("/api/training/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/api/training/membership/session").status_code, 403)

    def test_each_logout_revokes_current_browser_across_all_entry_points(self):
        for endpoint in ("/api/logout", "/api/training/staff/auth/logout"):
            with self.subTest(endpoint=endpoint):
                signed = self.client.post(
                    "/api/login", json={"username": self.owner["email"], "password": self.password}
                )
                main = signed.cookies["taxpearls_session"]
                staff = signed.cookies["taxpearls_staff_session"]
                other = self.store.authenticate(self.owner["email"], self.password)[1]
                self.assertEqual(self.client.post(endpoint).status_code, 200)
                self.assertIsNone(self.store.user_for_token(main))
                self.assertIsNone(staff_for_token(self.store, staff))
                self.assertEqual(self.store.user_for_token(other)["id"], self.owner["id"])
                self.assertEqual(self.client.get("/api/me").status_code, 401)
                self.assertEqual(self.client.post("/api/training/membership/session").status_code, 401)
        self.allocate(1)
        student, _ = self.join(self.link())
        signed = self.client.post("/api/login", json={"username": student["email"], "password": self.password})
        main = signed.cookies["taxpearls_session"]
        training = signed.cookies["taxpearls_training_session"]
        self.assertEqual(self.client.post("/api/training/auth/logout").status_code, 200)
        self.assertIsNone(self.store.user_for_token(main))
        self.assertIsNone(student_for_token(self.store, training))
        self.assertEqual(self.client.post("/api/training/membership/session").status_code, 401)

    def test_direct_teaching_login_aligns_main_identity_and_remember_duration(self):
        self.client.post("/api/login", json={"username": self.root["username"], "password": self.password})
        staff = self.client.post(
            "/api/training/staff/auth/login",
            json={"username": self.actor["username"], "password": self.password, "remember": True},
        )
        self.assertEqual(staff.status_code, 200, staff.text)
        self.assertEqual(self.store.user_for_token(staff.cookies["taxpearls_session"])["id"], self.owner["id"])
        self.allocate(1)
        student, _ = self.join(self.link())
        signed = self.client.post(
            "/api/training/auth/login",
            json={"student_no": "2026001", "college": self.college, "password": self.password},
        )
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertEqual(self.store.user_for_token(signed.cookies["taxpearls_session"])["id"], student["id"])
        self.assertEqual(self.client.get("/api/training/membership").status_code, 401)

    def test_platform_can_reenable_the_single_linked_school_administrator(self):
        with self.store.connect() as db:
            db.execute("UPDATE users SET active=0 WHERE id=?", (self.owner["id"],))
            db.execute("UPDATE college_user SET active=0 WHERE id=?", (self.actor["id"],))
        result = members.set_active(self.store, self.root, self.owner["org_id"], self.owner["id"], True, False)
        self.assertTrue(result["active"])
        self.assertEqual(service.overview(self.store, self.actor)["quota"]["teachers_used"], 1)

    def test_existing_member_confirmation_is_idempotent_at_full_capacity(self):
        self.allocate(1)
        link = self.link()
        student, _ = self.join(link)
        body = service.JoinBody(
            invite_code=link["path"].split("/")[-1],
            email=student["email"],
            password=self.password,
            existing=True,
            accepted=True,
        )
        repeated, _ = service.redeem(self.store, body, auth_support.BROWSER)
        self.assertEqual(repeated["id"], student["id"])
        self.assertEqual(service.overview(self.store, self.actor)["mine"]["used"], 1)
        self.assertEqual(service.links(self.store, self.actor)["links"][0]["used_count"], 1)

    def test_wrong_email_code_attempts_commit_without_consuming_link(self):
        self.allocate(1)
        link = self.link()
        code = link["path"].split("/")[-1]
        proof = email_auth.issue(self.store, "learner@example.com", "register", auth_support.BROWSER, invite_code=code)
        wrong = "000000" if proof["code"] != "000000" else "111111"
        body = service.JoinBody(
            invite_code=code,
            email="learner@example.com",
            password=self.password,
            code=wrong,
            display_name="测试",
            student_no="2026001",
            accepted=True,
        )
        with self.assertRaisesRegex(ValueError, "验证码不正确"):
            service.redeem(self.store, body, auth_support.BROWSER)
        with self.store.connect() as db:
            record = db.execute(
                "SELECT attempts,used_at FROM email_tokens WHERE email='learner@example.com'"
            ).fetchone()
            self.assertEqual(record["attempts"], 1)
            self.assertIsNone(record["used_at"])
        self.assertEqual(service.links(self.store, self.actor)["links"][0]["used_count"], 0)


if __name__ == "__main__":
    unittest.main()
