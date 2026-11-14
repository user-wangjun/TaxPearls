"""教师/管理员内容维护接口测试（FR-K01～K04 管理侧）。

覆盖：staff 认证、证书上架/下架、官方日期录入与引用保护、考纲知识点
层级与循环防护、三类标注（考证高频强制依据）。
"""
from __future__ import annotations

import auth_support

import tempfile
import subprocess
import unittest
import uuid
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path
from html.parser import HTMLParser

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import STATIC_DIR, register, today_cst

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

    def _content_login(self):
        self._staff_login()
        auth_support.content_session(self.store, self.client)

    def _student_login(self):
        self.client.cookies.delete("taxpearls_session")
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": "study-pass", "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)
        return res

    # ---- 教师认证 -------------------------------------------------------

    def test_existing_university_registry_backfill_preserves_account_and_quota(self):
        from webapp import university_members as service
        user = self.store.create_user("existing-campus", "Strong@2026Pass", "高校管理员", "org_admin", "existing-school")
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS org_profiles(org_id TEXT PRIMARY KEY,org_type TEXT)")
            db.execute("INSERT INTO org_profiles VALUES ('existing-school','university')")
            db.execute("INSERT INTO org_settings(org_id,display_name,updated_at) VALUES ('existing-school','原高校',?)", (NOW,))
            db.execute("INSERT INTO org_quota(org_id,seats,revision,updated_at) VALUES ('existing-school',5,1,?)", (NOW,))
            service.backfill_existing_organizations(db)
            service.backfill_existing_organizations(db)
            link = db.execute("SELECT * FROM university_identity_links WHERE user_id=?", (user['id'],)).fetchone()
            self.assertIsNotNone(link)
            self.assertEqual(db.execute("SELECT count(*) FROM users WHERE org_id='existing-school'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM college_user WHERE college='existing-school'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT teacher_quota,student_quota FROM college_quota WHERE college='existing-school'").fetchone()[:], (5, 0))
        context = service.user_context(self.store, user)
        self.assertEqual((context['id'], context['workspace'], context['education_role']), (user['id'], '/training-staff', 'admin'))
        from webapp import app as main
        from webapp.login_guard import LoginGuard
        with patch.object(main, "store", self.store), patch.object(main, "login_guard", LoginGuard()):
            client = TestClient(main.app)
            signed = client.post('/api/login', json={'username':user['username'], 'password':'Strong@2026Pass'})
            self.assertEqual(signed.status_code, 200, signed.text)
            self.assertEqual(signed.json()['user']['id'], user['id'])
            created = client.post('/api/training/staff/courses', json={'college':'existing-school', 'name':'数据联通验证课程', 'teacher_id':link['staff_id']})
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(len(client.get('/api/training/staff/courses').json()['courses']), 1)
            denied = client.post('/api/training/staff/courses', json={'college':'示例大学', 'name':'跨院校课程'})
            self.assertEqual(denied.status_code, 403)

    def test_existing_registry_backfill_does_not_match_legacy_profiles_by_name(self):
        from webapp import university_members as service
        user = self.store.create_user("unlinked-campus", "Strong@2026Pass", "管理员", "org_admin", "示例大学")
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS org_profiles(org_id TEXT PRIMARY KEY,org_type TEXT)")
            db.execute("INSERT INTO org_profiles VALUES ('示例大学','university')")
            db.execute("INSERT INTO org_settings(org_id,display_name,updated_at) VALUES ('示例大学','示例大学',?)", (NOW,))
            service.backfill_existing_organizations(db)
            self.assertIsNone(db.execute("SELECT * FROM university_identity_links WHERE user_id=?", (user['id'],)).fetchone())

    def test_university_workspace_assets_and_main_entry(self):
        # One login entry; account ownership is resolved on the server.
        from webapp import app as main_app
        page = (Path(main_app.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="loginAccountType"', page)
        self.assertIn('id="universityPanel"', page)
        startup = (Path(main_app.STATIC_DIR) / "auth.js").read_text(encoding="utf-8")
        for asset in ("university-workspace.js", "university.css"):
            response = self.client.get("/" + asset)
            self.assertEqual(response.status_code, 200, asset)
            self.assertIn(asset, page + startup)

    def test_main_login_routes_by_stored_identity_and_replaces_browser_session(self):
        from webapp import app as main
        from webapp.login_guard import LoginGuard
        with patch.object(main, "store", self.store), patch.object(main, "login_guard", LoginGuard()):
            client = TestClient(main.app)
            staff = client.post("/api/login", json={"username": "admin1", "password": "staff-pass"})
            self.assertEqual(staff.status_code, 200, staff.text)
            self.assertEqual(staff.json()["user"]["workspace"], "/training-staff")
            self.assertEqual(client.get("/api/status").json()["user"]["id"], "staff-" + self.staff_id)
            self.assertEqual(client.get("/api/training/staff/auth/me").status_code, 200)
            student = client.post("/api/login", json={"username": "2026001", "password": "study-pass"})
            self.assertEqual(student.status_code, 200, student.text)
            self.assertEqual(student.json()["user"]["role"], "student")
            self.assertEqual(client.get("/api/training/staff/auth/me").status_code, 401)
            self.assertEqual(client.get("/api/status").json()["user"]["id"], "student-" + self.student_id)
            platform = self.store.create_user("business", "Strong@2026Pass", "企业成员", "accountant", "org-business")
            result = client.post("/api/login", json={"username": "business", "password": "Strong@2026Pass"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["user"]["id"], platform["id"])
            self.assertEqual(client.get("/api/training/auth/me").status_code, 401)
            self.assertEqual(client.get("/api/me").status_code, 200)

    def test_unified_login_never_guesses_duplicate_ownership_or_mints_failed_sessions(self):
        from webapp import app as main
        from webapp.login_guard import LoginGuard
        other = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (other, "其他大学", "2026001", "同学", None,
                        _passwords.hash("study-pass"), None, NOW))
        with patch.object(main, "store", self.store), patch.object(main, "login_guard", LoginGuard()):
            client = TestClient(main.app)
            wrong = client.post("/api/login", json={"username": "2026001", "password": "wrong"})
            self.assertEqual(wrong.status_code, 401)
            ambiguous = client.post("/api/login", json={"username": "2026001", "password": "study-pass"})
            self.assertEqual(ambiguous.status_code, 409, ambiguous.text)
            self.assertTrue(ambiguous.json()["requires_college"])
            self.assertNotIn("set-cookie", ambiguous.headers)
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM training_student_sessions").fetchone()[0], 0)
            chosen = client.post("/api/login", json={"username": "2026001", "password": "study-pass", "college": "其他大学"})
            self.assertEqual(chosen.status_code, 200, chosen.text)
            self.assertEqual(chosen.json()["user"]["id"], "student-" + other)

    def test_unified_login_email_precedence_roles_and_inactive_records(self):
        from webapp import app as main
        from webapp.login_guard import LoginGuard
        self.store.create_user("mail-owner", "Strong@2026Pass", "邮箱归属", "accountant", "business", "bound@example.com")
        with self.store.connect() as db:
            db.execute("UPDATE college_user SET username='bound@example.com', email='staff@example.com' WHERE id=?", (self.staff_id,))
            db.execute("UPDATE users SET active=0 WHERE username='mail-owner'")
        with patch.object(main, "store", self.store), patch.object(main, "login_guard", LoginGuard()):
            client = TestClient(main.app)
            disabled = client.post("/api/login", json={"username": "bound@example.com", "password": "staff-pass"})
            self.assertEqual(disabled.status_code, 401, "disabled email owner must not fall back to another account's username")
            staff = client.post("/api/login", json={"username": "STAFF@example.com", "password": "staff-pass", "role": "platform_admin", "remember": True})
            self.assertEqual(staff.status_code, 200, staff.text)
            self.assertEqual(staff.json()["user"]["education_role"], "admin")
            self.assertIn("Max-Age=2592000", staff.headers["set-cookie"])
            with self.store.connect() as db:
                db.execute("UPDATE college_user SET active=0 WHERE id=?", (self.staff_id,))
            self.assertIsNone(client.get("/api/status").json()["user"])

    def test_legacy_mixed_sessions_require_login_instead_of_picking_a_role(self):
        from webapp import app as main
        from webapp.login_guard import LoginGuard
        with patch.object(main, "store", self.store), patch.object(main, "login_guard", LoginGuard()):
            client = TestClient(main.app)
            signed = client.post("/api/training/staff/auth/login", json={"username": "admin1", "password": "staff-pass"})
            self.assertEqual(signed.status_code, 200)
            self.assertEqual(client.post("/api/training/auth/login", json={"student_no": "2026001", "password": "study-pass"}).status_code, 200)
            # Current entry points clear other cookies. Reproduce an older browser
            # that still presents both valid identities explicitly.
            from webapp.training_portal import login_staff
            previous_staff_token = login_staff(self.store, "admin1", "staff-pass")['token']
            client.cookies.set("taxpearls_staff_session", previous_staff_token, domain="testserver.local", path="/")
            result = client.get("/api/status").json()
            self.assertTrue(result["session_conflict"])
            self.assertIsNone(result["user"])
            self.assertEqual(client.post("/api/login", json={"username": "2026001", "password": "study-pass"}).status_code, 200)
            self.assertFalse(client.get("/api/status").json()["session_conflict"])
            self.assertEqual(client.get("/api/status").json()["user"]["id"], "student-" + self.student_id)

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
        anonymous = self.client.get('/training-staff',follow_redirects=False)
        self.assertEqual(anonymous.status_code,307)
        self.assertEqual(anonymous.headers['location'],'/')
        self._staff_login()
        page = self.client.get("/training-staff",follow_redirects=False)
        self.assertEqual(page.status_code,307)
        self.assertEqual(page.headers["location"],"/#panel=examPanel")
        script = self.client.get("/training-staff.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("text/javascript", script.headers["content-type"])

    def test_training_entry_controls_and_tax_practice_container(self):
        class Containers(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.nodes = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get("id"):
                    self.nodes[attrs["id"]] = (attrs, tuple(self.stack))
                if tag in ("main", "section", "div"):
                    self.stack.append((tag, attrs.get("id")))

            def handle_endtag(self, tag):
                if tag in ("main", "section", "div"):
                    self.stack.pop()

        staff_page = Containers()
        self._staff_login()
        staff_page.feed((STATIC_DIR/"training-staff.html").read_text(encoding="utf-8"))
        self.assertIn("cert-select", staff_page.nodes)
        self.assertIn("cert-summary", staff_page.nodes)
        for removed in ("new-cert-btn", "cert-form", "auth-card", "bank-card", "outline-card",
                        "kf-code", "unit-form", "case-form", "mark-stats"):
            self.assertNotIn(removed, staff_page.nodes)
        for field in ("sf-college", "si-college", "cf2-college", "cm-college", "ti-college"):
            attrs = staff_page.nodes[field][0]
            self.assertIn("readonly", attrs)
            self.assertNotIn("value", attrs)  # 登录身份填充，不预设演示院校
        student_page = Containers()
        self._student_login()
        student_page.feed((STATIC_DIR/"training-portal.html").read_text(encoding="utf-8"))
        parents = student_page.nodes["pr-card"][1]
        self.assertIn(("section", "app-view"), parents)
        self.assertNotIn(("div", "pass-modal"), parents)
        self.assertNotIn("code-card", student_page.nodes)
        self.assertNotIn("code-problems", staff_page.nodes)
        self.assertIn("税务案例训练", (STATIC_DIR/"training-portal.html").read_text(encoding="utf-8"))

    def test_tax_practice_retake_and_resume_buttons_reach_handlers(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const nodes = new Map(), starts = [], requests = [];
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {id, listeners: {}, value: '', innerHTML: '', textContent: '',
    focus() { this.focused = true; },
    addEventListener(kind, fn) { this.listeners[kind] = fn; }});
  return nodes.get(id);
}
const ctx = {disposed: false, embedded: false,
  document: {getElementById: node, querySelectorAll() { return []; }}, setTimeout() {}};
vm.createContext(ctx);
const source = fs.readFileSync('webapp/static/training-portal.js', 'utf8');
const controller=source.split('/* controller body */')[1].split('/* controller end */')[0];
vm.runInContext(controller.slice(0,controller.lastIndexOf('const ready=boot();')), ctx);
ctx.startPractice = async body => starts.push(body);
ctx.api = async path => { requests.push(path); return {attempt: {id: 'unfinished'}}; };
ctx.renderAttempt = () => {};
vm.runInContext("state.prCert='cert';", ctx);
async function click(container, dataset) {
  const btn = {dataset, closest() { return this; }};
  const card = container === 'pr-attempt' ? 'answer-card' : 'pr-card';
  for (const id of [container, card, 'app-view']) {
    const handler = node(id).listeners.click;
    if (handler) await handler({target: btn});
  }
}
(async () => {
  for (const container of ['pr-wrong', 'pr-attempt']) {
    await click(container, {act: 'redo-same', rule: 'R-001', seed: '42', kp: 'kp'});
    await click(container, {act: 'redo-new', rule: 'R-001', kp: 'kp'});
  }
  assert.equal(starts.length, 4, 'Wrong-list and result buttons must each start one practice');
  assert.equal(starts.filter(x => x.mode === 'redo_same' && x.seed === 42).length, 2);
  assert.equal(starts.filter(x => x.mode === 'new').length, 2);
  await click('pr-open', {act: 'practice-resume', id: 'unfinished'});
  assert.equal(requests.length, 1, 'Continue button must load the unfinished practice');
  assert.equal(requests[0], '/api/training/my/practice/unfinished');
  assert.equal(node('pr-card').hidden, true, 'Continue must leave the practice list');
  assert.equal(node('answer-card').hidden, false, 'Continue must open the answer page');
  assert.equal(node('answer-title').focused, true, 'Continue must focus the answer heading');
  assert.equal(node('app-error').textContent, '', 'Continue must not fall into the error handler');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_staff_actions_report_request_failures(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const nodes = new Map(), requests = [], notices = [];
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {id, listeners: {}, value: '测试名称', dataset: {v: '1'},
    hidden: true, innerHTML: '', textContent: '', checked: false,
    addEventListener(kind, fn) { this.listeners[kind] = fn; }});
  return nodes.get(id);
}
const ctx = {disposed: false, embedded: true,
  window: {universityWorkspace: {notify: (...args) => notices.push(args)}},
  document: {getElementById: node, querySelector: node, querySelectorAll() { return []; }},
  setTimeout() {}};
vm.createContext(ctx);
const source = fs.readFileSync('webapp/static/training-staff.js', 'utf8');
const controller = source.split('/* controller body */')[1].split('/* controller end */')[0];
vm.runInContext(controller.slice(0, controller.lastIndexOf('const ready=boot();')), ctx);
vm.runInContext("state.staff={college:'测试大学',role:'admin'};", ctx);
let failure = '仅实训管理员可维护组织架构', reloads = 0;
ctx.api = async path => { requests.push(path); if (failure) throw new Error(failure); return {}; };
ctx.loadOrg = async () => { reloads++; };
async function click(act) {
  const btn = {dataset: {act, id: 'entity'}, closest() { return this; }};
  await node('app-view').listeners.click({target: btn});
}
(async () => {
  for (const act of ['org-dep-create', 'org-dep-toggle', 'org-term-create',
                     'org-term-current', 'org-term-toggle', 'org-class-toggle']) {
    const before = notices.length;
    await click(act);
    assert.equal(notices.length, before + 1, act + ' must report a rejected request');
    assert.deepEqual(notices.at(-1), [failure, true]);
  }
  assert.equal(requests.length, 6);
  assert.equal(reloads, 0, 'Rejected writes must not continue with a success refresh');
  failure = '';
  await click('org-dep-toggle');
  assert.equal(reloads, 1, 'Successful actions must still refresh');
  await click('cancel-course-form');
  assert.equal(node('#course-form').hidden, true, 'Synchronous form controls still work');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_public_content_requires_platform_maintenance(self):
        from webapp import training_content, training_source_auth, training_graph, training_outline, teaching_units
        for module in (training_content, training_source_auth, training_graph, training_outline, teaching_units):
            module.register(self.client.app, lambda: self.store)
        self._staff_login()
        cert = f"/api/training/staff/certificates/{self.cert}"
        writes = [
            ("POST", "/api/training/staff/certificates", {"code": "new", "name": "公共证书"}),
            ("PATCH", cert, {"name": "不应被修改"}),
            ("POST", cert + "/dates", {"date_type": "official", "exam_date": _future()}),
            ("DELETE", "/api/training/staff/dates/missing", None),
            ("POST", cert + "/knowledge-points", {"code": "KP", "name": "公共知识点"}),
            ("PATCH", "/api/training/staff/knowledge-points/missing", {"name": "修改"}),
            ("POST", cert + "/knowledge-points/import", {"text": "code,name\nKP,知识点", "format": "csv"}),
            ("POST", "/api/training/staff/knowledge-points/missing/marks", {"mark_type": "risk_context"}),
            ("DELETE", "/api/training/staff/marks/missing", None),
            ("POST", "/api/training/staff/knowledge-points/missing/links", {"target_type": "rule", "target_id": "R-002"}),
            ("DELETE", "/api/training/staff/links/missing", None),
            ("POST", "/api/training/staff/knowledge-points/missing/relations", {"to_kp_id": "target", "relation_type": "concept"}),
            ("DELETE", "/api/training/staff/relations/missing", None),
            ("POST", cert + "/content-versions", {"label": "新版", "source_ref": "来源"}),
            ("POST", "/api/training/staff/content-versions/missing/retire", None),
            ("POST", cert + "/source-authorizations", {"licensor": "发布方", "auth_no": "TEST", "question_types": ["real"], "valid_from": "2026-01-01"}),
            ("POST", cert + "/bank-questions", {"questions": [{}]}),
            ("POST", "/api/training/staff/bank-questions/missing/review", {"decision": "approve"}),
            ("POST", cert + "/outline-upgrade/start", {"to_version": "2027"}),
            ("POST", cert + "/outline-upgrade/activate", None),
            ("POST", "/api/training/teaching/units", {"certificate_id": self.cert, "name": "公共单元", "stage": "data", "rule_codes": ["R-002"]}),
            ("POST", "/api/training/teaching/cases", {"certificate_id": self.cert, "name": "公共案例", "scenario": "情境", "rule_codes": ["R-002"]}),
        ]
        # A legacy school administrator session alone cannot maintain shared content.
        for method, path, body in writes:
            result = self.client.request(method, path, json=body) if body is not None else self.client.request(method, path)
            self.assertEqual(result.status_code, 401, (path, result.text))
        user = self.store.create_user("school-owner", "CampusOwner-2026!", "高校管理员", "org_admin", "campus")
        self.client.cookies.set("taxpearls_session", self.store.authenticate(user["username"], "CampusOwner-2026!")[1])
        for method, path, body in writes:
            result = self.client.request(method, path, json=body) if body is not None else self.client.request(method, path)
            self.assertEqual(result.status_code, 403, (path, result.text))
        self.assertEqual(self.client.get(cert + "/knowledge-points").status_code, 200)
        self.assertEqual(self.client.get("/api/training/staff/certificates").json()["certificates"][0]["name"], "初级会计职称")
        # Platform maintenance works without creating a fictitious teacher identity.
        auth_support.content_session(self.store, self.client)
        self.client.cookies.delete("taxpearls_staff_session")
        result = self.client.post(cert + "/dates", json={"date_type": "official", "exam_date": _future(90)})
        self.assertEqual(result.status_code, 200, result.text)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT created_by FROM exam_date WHERE id=?", (result.json()["exam_date"]["id"],)).fetchone()[0])
            self.assertGreater(db.execute("SELECT count(*) FROM audit_log WHERE action='access_public_teaching_maintenance'").fetchone()[0], 0)

    def test_teacher_resource_rendering_omits_maintenance_metadata(self):
        script = r"""
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const nodes=new Map();
const node=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',value:'',addEventListener(){}});return nodes.get(id);};
const ctx={document:{getElementById:node},URL}; vm.createContext(ctx);
const source=fs.readFileSync('webapp/static/training-staff.js','utf8');
const body=source.split('/* controller body */')[1].split('/* controller end */')[0];
vm.runInContext(body.slice(0,body.lastIndexOf('const ready=boot();')),ctx);
vm.runInContext(`state.certificates=[{id:'cert',name:'税务师职业资格',code:'tax-adviser',subjects:['财务与会计'],description:'按科目组织实训。',source_ref:'https://example.com/notice',exam_dates:[]}];
state.currentId='cert';state.kps=[{id:'point',name:'财务报表与会计勾稽',code:'KB-ACCOUNTING',subject:'财务与会计',source_ref:'internal/path',outline_version:'2026科目分类 / 规则仿真 f9b2bd5f0784',description:'核对报表项目。'}];
renderAll();`,ctx);
const html=[...nodes.values()].map(n=>n.innerHTML).join('');
assert.match(html,/财务报表与会计勾稽/);assert.match(html,/财务与会计/);assert.match(html,/查看发布来源/);
assert.doesNotMatch(html,/f9b2bd5f0784|KB-ACCOUNTING|tax-adviser|internal\/path|<input|<textarea|data-act=/);
vm.runInContext(`state.certificates[0].source_ref='javascript:alert(1)';renderCertForm();`,ctx);
assert.doesNotMatch(node('cert-summary').innerHTML,/javascript:|<a /);
vm.runInContext(`state.certificates=[];state.currentId=null;state.kps=[];renderAll();`,ctx);
assert.match(node('cert-summary').innerHTML,/暂无可用教学资源/);
"""
        result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # ---- 证书上架（FR-K01）---------------------------------------------

    def test_certificate_create_duplicate_and_toggle(self):
        self._content_login()
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
        self._content_login()
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
        self._content_login()
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
        self._content_login()
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
        self._content_login()
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
