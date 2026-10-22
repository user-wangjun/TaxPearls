"""高校端演示数据初始化（幂等，可反复重跑）。

解决什么问题
------------
主库（TaxPearls/instance/taxpearls.db）此前只有「证书 / 知识点 / 学生名册」，
没有任何教学业务数据，也没有 ``university_identity_links``。后果：

1. ``attach_platform_session`` 在链接表里查不到映射就静默 return，教师登录只拿到
   ``taxpearls_staff_session``，拿不到 ``taxpearls_session``；
2. 于是 ``/api/assignments``、``/api/papers``、``/api/training/tasks`` 对教师
   全部 401 —— 课程与作业主链在真实身份下根本走不通；
3. 组织架构、课程、班级、作业、试卷五张核心表全 0 行，9 个视图有 5 个是空态。

两个阶段
--------
``identities``：直写库。给 college_user 里的师生各补一行平台 ``users`` + 一条
``university_identity_links``。密码哈希从 college_user **原样搬运**，所以登录
密码不变（建链后 ``identity_hash`` 会改用 users 那一份校验）。

``demo``：走真实 HTTP API。院系→专业→学期→班级实体→课程→班级→仿真案例→
单案例作业→组卷试卷→任务设置。走 API 才能同时验证接口链路本身。

用法
----
    python -B -X utf8 scripts/seed_university_demo.py --dry-run
    python -B -X utf8 scripts/seed_university_demo.py --stage identities
    python -B -X utf8 scripts/seed_university_demo.py --stage demo --base http://127.0.0.1:8000

全部数据均为**仿真演示数据**，不含任何真实企业信息。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_COLLEGE = "演示财经职业学院"
DEMO_TEACHER = "staff01"
DEMO_DEPT = "会计学院"
DEMO_MAJOR = "大数据与会计"
DEMO_TERM = "2026-2027学年第一学期"
DEMO_ORG_CLASS = "会计2601"
DEMO_COURSE = "涉税风险识别与合规实务"
DEMO_STUDENT_CLASS = "会计2601 演示班"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(step: str, detail: str = "") -> None:
    print(f"[{step}] {detail}".rstrip())


def load_env() -> None:
    """把仓库根的 .env 灌进环境变量（不覆盖已有值）。

    Store 初始化需要 TAXPEARLS_FIELD_KEY 才能解开加密列；启动器（bat）跑
    ``python -m webapp`` 时由应用自身加载，独立脚本没有这一步。
    """
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- identities


def ensure_identities(store, dry: bool) -> dict[str, int]:
    """给师生补平台身份与映射。返回 {"teacher": n, "student": n}。

    教职工在 ``college_user``，学生在 ``student_info``，两张表分开存。
    """
    made = {"teacher": 0, "student": 0}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        people = [
            {"kind": "teacher", "id": r["id"], "username": r["username"],
             "password_hash": r["password_hash"], "display_name": r["display_name"],
             "email": r["email"]}
            for r in db.execute(
                "SELECT id,username,password_hash,display_name,email FROM college_user "
                "WHERE active=1 AND role IN ('admin','teacher') ORDER BY username")
        ] + [
            {"kind": "student", "id": r["id"], "username": r["student_no"],
             "password_hash": r["password_hash"], "display_name": r["name"],
             "email": r["email"]}
            for r in db.execute(
                "SELECT id,student_no,name,password_hash,email FROM student_info "
                "WHERE active=1 ORDER BY student_no")
        ]
        for person in people:
            kind = person["kind"]
            column = "student_id" if kind == "student" else "staff_id"
            linked = db.execute(
                f"SELECT user_id FROM university_identity_links WHERE {column}=?",
                (person["id"],)).fetchone()
            if linked:
                continue
            if dry:
                made[kind] += 1
                continue
            user_id = secrets.token_hex(12)
            try:
                db.execute(
                    """INSERT INTO users(id,username,password_hash,display_name,role,org_id,
                                         active,created_at,email)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (user_id, person["username"], person["password_hash"],
                     person["display_name"], "student" if kind == "student" else "teacher",
                     "default", 1, now(), person["email"] or None))
            except Exception:
                # 用户名已被占用：复用同名平台账号，只补链接
                existing = db.execute(
                    "SELECT id FROM users WHERE username=?", (person["username"],)).fetchone()
                if not existing:
                    raise
                user_id = existing["id"]
            db.execute(
                f"INSERT INTO university_identity_links(user_id,{column}) VALUES (?,?)",
                (user_id, person["id"]))
            made[kind] += 1
    log("身份", f"新建/复用 教师 {made['teacher']} 条，学生 {made['student']} 条")
    return made


# ---------------------------------------------------------------------- demo


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=60) as response:
                body = response.read()
                return response.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            return exc.code, detail[:300]

    def get(self, path, **params):
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return self.call("GET", path)

    def post(self, path, payload):
        return self.call("POST", path, payload)

    def put(self, path, payload):
        return self.call("PUT", path, payload)


def must(status: int, body, what: str):
    if status >= 300:
        raise SystemExit(f"{what} 失败：HTTP {status} {body}")
    return body


def ensure_org(api: Api) -> dict[str, str]:
    ids: dict[str, str] = {}
    status, body = api.get("/api/training/staff/org/departments", college=DEMO_COLLEGE)
    depts = must(status, body, "读取院系")["departments"]
    found = next((d for d in depts if d["name"] == DEMO_DEPT), None)
    if not found:
        found = must(*api.post("/api/training/staff/org/departments",
                               {"college": DEMO_COLLEGE, "name": DEMO_DEPT}), "创建院系")["department"]
        log("组织", f"院系 {DEMO_DEPT}")
    ids["department"] = found["id"]

    status, body = api.get("/api/training/staff/org/majors", college=DEMO_COLLEGE)
    majors = must(status, body, "读取专业")["majors"]
    found = next((m for m in majors if m["name"] == DEMO_MAJOR), None)
    if not found:
        found = must(*api.post("/api/training/staff/org/majors",
                              {"college": DEMO_COLLEGE, "name": DEMO_MAJOR,
                               "department_id": ids["department"]}), "创建专业")["major"]
        log("组织", f"专业 {DEMO_MAJOR}")
    ids["major"] = found["id"]

    status, body = api.get("/api/training/staff/org/terms", college=DEMO_COLLEGE)
    terms = must(status, body, "读取学期")["terms"]
    found = next((t for t in terms if t["name"] == DEMO_TERM), None)
    if not found:
        found = must(*api.post("/api/training/staff/org/terms",
                              {"college": DEMO_COLLEGE, "name": DEMO_TERM,
                               "is_current": True}), "创建学期")["term"]
        log("组织", f"学期 {DEMO_TERM}")
    ids["term"] = found["id"]

    status, body = api.get("/api/training/staff/org/classes", college=DEMO_COLLEGE)
    classes = must(status, body, "读取班级实体")["classes"]
    found = next((c for c in classes if c["name"] == DEMO_ORG_CLASS), None)
    if not found:
        found = must(*api.post("/api/training/staff/org/classes",
                              {"college": DEMO_COLLEGE, "name": DEMO_ORG_CLASS,
                               "department_id": ids["department"], "major_id": ids["major"],
                               "term_id": ids["term"]}), "创建班级实体")["class"]
        log("组织", f"班级实体 {DEMO_ORG_CLASS}")
    ids["org_class"] = found["id"]
    return ids


def ensure_course(api: Api) -> str:
    status, body = api.get("/api/training/staff/courses", college=DEMO_COLLEGE)
    courses = must(status, body, "读取课程")["courses"]
    found = next((c for c in courses if c["name"] == DEMO_COURSE), None)
    if found:
        return found["id"]
    body = must(*api.post("/api/training/staff/courses",
                         {"college": DEMO_COLLEGE, "name": DEMO_COURSE,
                          "semester": DEMO_TERM, "class_names": [DEMO_ORG_CLASS],
                          "description": "仿真案例驱动的涉税风险识别实训（演示课程）"}),
                "创建课程")["course"]
    log("课程", f"{DEMO_COURSE} -> {body['id']}")
    return body["id"]


def student_user_ids(store) -> list[str]:
    """返回已绑定平台身份的学生 users.id。学生档案在 student_info，不在 college_user。"""
    with store.connect() as db:
        rows = db.execute(
            """SELECT l.user_id FROM university_identity_links l
               JOIN student_info s ON s.id=l.student_id
               WHERE l.student_id IS NOT NULL AND s.active=1 ORDER BY s.student_no""").fetchall()
    return [r["user_id"] for r in rows]


def ensure_organization(store, dry: bool) -> None:
    """补租户设置 + 一条「院校 ↔ org_id」映射。

    两层依赖缺一不可：
    - ``org_settings``：``session_identity`` 读它的 ``org_type``，没有行或不是
      'university'，高校身份校验直接 403；
    - ``university_organizations``：``course_for_actor`` 要求有 college→org 的行，
      否则任何课程都判成「不存在或无权管理」。
    真库此前这两张表都是 0 行。
    """
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        settings = db.execute("SELECT org_id,org_type FROM org_settings WHERE org_id='default'").fetchone()
        if not settings:
            if dry:
                log("院校", "将新建 org_settings(default, university)")
            else:
                db.execute(
                    """INSERT INTO org_settings(org_id,display_name,report_title,footer_text,
                                               updated_at,org_type)
                       VALUES ('default',?,?,'',?,'university')""",
                    (DEMO_COLLEGE, "涉税风险实训报告", now()))
                log("院校", "已建 org_settings(default, university)")
        elif settings["org_type"] != "university":
            log("院校", f"注意：org_type={settings['org_type']}，高校身份接口可能 403")

        row = db.execute("SELECT org_id FROM university_organizations WHERE college=?",
                         (DEMO_COLLEGE,)).fetchone()
        if row:
            log("院校", f"{DEMO_COLLEGE} -> org {row['org_id']}")
            return
        if dry:
            log("院校", f"将新建映射 {DEMO_COLLEGE} -> default")
            return
        db.execute("INSERT INTO university_organizations(college,org_id) VALUES (?,?)",
                   (DEMO_COLLEGE, "default"))
        log("院校", f"已建映射 {DEMO_COLLEGE} -> default")


def bind_course_classroom(api: Api, course_id: str, class_id: str) -> None:
    status, body = api.put(f"/api/teaching/courses/{course_id}/classrooms",
                           {"class_ids": [class_id]})
    if status >= 300:
        log("课程", f"班级关联失败（不阻塞演示）：HTTP {status} {str(body)[:120]}")
    else:
        log("课程", "已关联教学班级")


def ensure_classroom(api: Api, student_ids: list[str]) -> str:
    status, body = api.get("/api/classes")
    classes = must(status, body, "读取班级")["classes"] if isinstance(body, dict) else body
    found = next((c for c in classes if c.get("name") == DEMO_STUDENT_CLASS), None)
    if found:
        must(*api.put(f"/api/classes/{found['id']}",
                     {"name": DEMO_STUDENT_CLASS, "student_ids": student_ids,
                      "revision": found["revision"]}), "更新班级成员")
        log("班级", f"{DEMO_STUDENT_CLASS} 已存在，成员已同步 {len(student_ids)} 人")
        return found["id"]
    body = must(*api.post("/api/classes",
                         {"name": DEMO_STUDENT_CLASS, "student_ids": student_ids}),
                "创建班级")
    log("班级", f"{DEMO_STUDENT_CLASS} 建好，{len(student_ids)} 人")
    return body["id"]


def ensure_cases(api: Api) -> list[str]:
    status, body = api.get("/api/exercises/rules", year=2026)
    rules = must(status, body, "读取出题规则")["rules"]
    wanted = ["R-001", "R-005", "R-006"]
    ids: list[str] = []
    for index, rule_id in enumerate(wanted):
        rule = next((r for r in rules if r["id"] == rule_id), None)
        if not rule:
            log("案例", f"规则 {rule_id} 未启用，跳过")
            continue
        status, body = api.post("/api/exercises", {
            "rule_id": rule_id, "seed": 23 + index, "year": 2026,
            "expected_version": rule["version"]})
        if status >= 300:
            log("案例", f"{rule_id} 生成失败：HTTP {status} {str(body)[:120]}")
            continue
        ids.append(body["audit_id"])
        log("案例", f"{rule_id} -> audit {body['audit_id']}")
    if not ids:
        raise SystemExit("没有生成任何仿真案例，后续任务无法建立。")
    return ids


def ensure_tasks(api: Api, class_id: str, course_id: str, cases: list[str]) -> None:
    due = (datetime.now(UTC) + timedelta(days=14)).isoformat(timespec="seconds")
    status, body = api.get("/api/training/tasks")
    items = must(status, body, "读取任务清单")["items"]
    titles = {i["title"] for i in items}

    if "演示任务：单案例涉税风险识别" not in titles:
        must(*api.post("/api/assignments", {
            "title": "演示任务：单案例涉税风险识别",
            "audit_id": cases[0], "class_id": class_id, "published": True,
            "deadline_at": due}), "创建单案例作业")
        log("任务", "单案例作业已发布")

    if "演示任务：多案例综合卷" not in titles:
        items_payload = [{"audit_id": cid, "points": round(100 / len(cases))}
                         for cid in cases]
        items_payload[0]["points"] = 100 - sum(i["points"] for i in items_payload[1:])
        must(*api.post("/api/papers", {
            "title": "演示任务：多案例综合卷", "class_id": class_id,
            "course_id": course_id, "deadline_at": due, "published": True,
            "items": items_payload}), "创建组卷试卷")
        log("任务", "组卷试卷已发布")

    status, body = api.get("/api/training/tasks")
    for item in must(status, body, "复查任务清单")["items"]:
        must(*api.put(f"/api/training/tasks/{item['kind']}/{item['id']}/settings", {
            "open_at": None, "submit_limit": 1, "correction_limit": 1,
            "answer_release": "after_submit"}), f"设置任务 {item['title']}")
        log("任务", f"{item['kind']} · {item['title']} · 答案提交后开放")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stage", choices=["identities", "demo", "all"], default="all")
    parser.add_argument("--db", default=os.environ.get("TAXPEARLS_DB"))
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--password", default=os.environ.get("TAXPEARLS_DEMO_PASSWORD", ""))
    args = parser.parse_args()

    path = args.db or str(ROOT.parent / "TaxPearls" / "instance" / "taxpearls.db")
    if not Path(path).exists():
        print(f"数据库不存在：{path}")
        return 2
    os.environ["TAXPEARLS_DB"] = path
    load_env()
    log("库", path)

    from webapp.storage import Store

    store = Store(path, keepalive=True)
    try:
        if args.stage in ("identities", "all"):
            ensure_identities(store, args.dry_run)
        if args.stage in ("demo", "all"):
            if args.dry_run:
                log("演示", "干跑：跳过 HTTP 阶段（需要真实写入后才可验证）")
                return 0
            if not args.password:
                raise SystemExit("需要 --password 或 TAXPEARLS_DEMO_PASSWORD 以教师身份登录。")
            api = Api(args.base)
            must(*api.post("/api/training/staff/auth/login",
                          {"username": DEMO_TEACHER, "password": args.password}),
                 "教师登录")
            if not any(c.name == "taxpearls_session" for c in api.jar):
                raise SystemExit("登录后没有拿到平台会话：身份链接仍未生效。")
            log("登录", "已拿到 staff + 平台双会话")
            ensure_organization(store, False)
            ensure_org(api)
            course_id = ensure_course(api)
            case_ids = ensure_cases(api)
            class_id = ensure_classroom(api, student_user_ids(store))
            bind_course_classroom(api, course_id, class_id)
            ensure_tasks(api, class_id, course_id, case_ids)
    finally:
        store.close()
    log("完成", "演示数据就绪")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
