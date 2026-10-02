"""TaxPearls P1 web application.

Persistent audits, Argon2 authentication, role-based access control, client
ownership, audit logs, rule switches and deterministic training/marking.
Uploaded workbook bytes stay in memory; only normalized evidence is stored.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Cookie, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from src import engine, loader, render, training
from src import settings  # Load .env before Store and route initialization.
from src.models import Dataset
from webapp.storage import Store
from webapp.knowledge import build_graph, ask_graph, ai_config

ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "rules"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGO_SVG = ROOT / "logo" / "logo-shui-hai-shi-zhu.svg"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
COOKIE_NAME = "taxpearls_session"

app = FastAPI(title="税海拾珠 · 税务风险审计", version="1.0.0", docs_url=None, redoc_url=None)
store = Store()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


class SetupBody(BaseModel):
    username: str
    password: str
    display_name: str = ""
    org_id: str = "default"


class LoginBody(BaseModel):
    username: str
    password: str


class UserBody(BaseModel):
    username: str
    password: str
    display_name: str
    role: str
    org_id: str | None = None


class ClientBody(BaseModel):
    name: str
    taxpayer_id: str
    accountant_id: str | None = None


class AssignmentBody(BaseModel):
    title: str
    audit_id: str
    target_student_id: str | None = None
    weights: dict[str, float] = Field(default_factory=dict)
    false_positive_penalty: float = Field(default=5.0, ge=0, le=100)
    published: bool = True


class SubmissionBody(BaseModel):
    selected_rule_ids: list[str]


class ReviewBody(BaseModel):
    adjusted_score: float = Field(ge=0, le=100)
    feedback: str = ""


class RuleStateBody(BaseModel):
    enabled: bool


class OrgSettingsBody(BaseModel):
    display_name: str
    report_title: str
    footer_text: str = ""


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message})


def _user(session: str | None) -> dict[str, Any]:
    user = store.user_for_token(session)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录。")
    return user


def _allow(user: dict[str, Any], *roles: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(status_code=403, detail="当前角色无权执行此操作。")


def _can_access_audit(user: dict[str, Any], entry: dict[str, Any]) -> bool:
    if user["role"] == "platform_admin":
        return True
    if user["org_id"] != entry["org_id"]:
        return False
    if user["role"] != "accountant":
        return True
    client = store.get_client(entry["client_id"]) if entry.get("client_id") else None
    return bool(client and client.get("accountant_id") == user["id"])


def _audit_or_404(audit_id: str, user: dict[str, Any]) -> dict[str, Any]:
    entry = store.get_audit(audit_id)
    if not entry or not _can_access_audit(user, entry):
        raise HTTPException(status_code=404, detail="审计结果不存在或无权访问。")
    return entry


def _company_dict(dataset: Dataset) -> dict[str, str]:
    c = dataset.company
    return {"name": c.name, "taxpayer_id": c.taxpayer_id, "industry": c.industry, "period": c.period}


def _is_synthetic_dataset(dataset: Dataset) -> bool:
    """Use one definition for teaching-data checks across audit and training."""
    name = dataset.company.name
    taxpayer_id = dataset.company.taxpayer_id.upper()
    return "仿真" in name or "纯合成测试" in name or "TEST" in taxpayer_id


def _metrics_list(dataset: Dataset) -> list[dict[str, str]]:
    return [
        {"name": m.name, "value": f"{m.value:,.2f}", "source": m.source, "detail": m.detail}
        for m in dataset.metrics.values()
    ]


def _result(entry: dict[str, Any]) -> dict[str, Any]:
    dataset, findings = entry["dataset"], entry["findings"]
    vm = render.build_view_model(dataset, findings)
    return {
        "audit_id": entry["id"], "audited_at": entry["audited_at"],
        "company": _company_dict(dataset), "summary": vm["summary"],
        "findings": vm["findings"], "metrics": _metrics_list(dataset),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/logo")
def logo() -> Response:
    return FileResponse(LOGO_SVG, media_type="image/svg+xml")


@app.get("/auth-ocean-data-v1.webp")
def auth_ocean_visual() -> FileResponse:
    return FileResponse(STATIC_DIR / "auth-ocean-data-v1.webp", media_type="image/webp")


@app.get("/auth-pearl-real-v1.webp")
def auth_pearl_visual() -> FileResponse:
    return FileResponse(STATIC_DIR / "auth-pearl-real-v1.webp", media_type="image/webp")


@app.get("/workspace.js")
def workspace_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "workspace.js", media_type="text/javascript")


@app.get("/workspace.css")
def workspace_styles() -> FileResponse:
    return FileResponse(STATIC_DIR / "workspace.css", media_type="text/css")


@app.get("/api/dashboard")
def dashboard(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "platform_admin")
    records = []
    seen = set()
    history = store.list_audits(user)
    for row in history:
        key = (row["taxpayer_id"] or row["company_name"], row["period"])
        if key in seen:
            continue
        seen.add(key)
        entry = store.get_audit(row["id"])
        if not entry:
            continue
        records.append({**row, "metrics": {
            name: {"value": str(metric.value), "source": metric.source}
            for name, metric in entry["dataset"].metrics.items()
        }, "risks": [{"id": f.rule.id, "name": f.rule.name,
                        "severity": f.rule.severity, "category": f.rule.category,
                        "status": f.status, "reason": f.skip_reason}
                       for f in entry["findings"] if f.status != "pass"]})
    return {"records": records, "history": history}


@app.get("/api/knowledge")
def knowledge(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    _user(session)
    enabled = store.enabled_rule_ids()
    return [{"id": r.id, "name": r.name, "category": r.category,
             "tax_type": r.tax_type, "severity": r.severity, "scope": r.scope,
             "inputs": r.inputs, "evidence": r.evidence, "legal_basis": r.legal_basis,
             "references": r.references, "suggestion": r.suggestion,
             "enabled": enabled is None or r.id in enabled}
            for r in engine.load_rules(RULES_DIR)]


@app.get("/api/status")
def status(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    return {"needs_setup": not store.has_users(), "user": store.user_for_token(session)}


class GraphQuestion(BaseModel):
    node_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    audit_id: str | None = None


def _graph_for_user(user, audit_id):
    entry = None
    if audit_id:
        _allow(user, "org_admin", "accountant", "teacher", "platform_admin")
        entry = _audit_or_404(audit_id, user)
    return build_graph(engine.load_rules(RULES_DIR), entry)


@app.get("/api/knowledge/graph")
def knowledge_graph(audit_id: str | None = None,
                    session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    graph = _graph_for_user(user, audit_id)
    base, model, _ = ai_config()
    return {**graph, "ai": {"configured": bool(base and model), "model": model}}


@app.post("/api/knowledge/ask")
def knowledge_ask(body: GraphQuestion,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    graph = _graph_for_user(user, body.audit_id)
    return ask_graph(graph, body.node_id, body.question)


@app.get("/graph.js")
def graph_script():
    return FileResponse(STATIC_DIR / "graph.js", media_type="text/javascript")


@app.post("/api/setup")
def setup(body: SetupBody) -> dict[str, Any]:
    if store.has_users():
        raise HTTPException(status_code=409, detail="系统已初始化。")
    try:
        user = store.create_user(body.username, body.password, body.display_name.strip() or body.username, "platform_admin", body.org_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:
        if exc.__class__.__name__ == "IntegrityError":
            raise HTTPException(status_code=409, detail="用户名已存在。") from None
        raise
    store.log(user, "setup", "system", "initial")
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody) -> Response:
    result = store.authenticate(body.username, body.password)
    if not result:
        return _err(401, "用户名或密码错误。")
    user, token = result
    store.log(user, "login", "session", user["id"])
    response = JSONResponse({"user": user})
    response.set_cookie(
        COOKIE_NAME, token, max_age=12 * 3600, httponly=True,
        samesite="strict", secure=os.environ.get("TAXPEARLS_COOKIE_SECURE") == "1",
    )
    return response


@app.post("/api/logout")
def logout(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = store.user_for_token(session)
    if user:
        store.log(user, "logout", "session", user["id"])
    store.logout(session)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/api/me")
def me(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    return _user(session)


@app.get("/api/users")
def users(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    return store.list_users(None if user["role"] == "platform_admin" else user["org_id"])


@app.post("/api/users")
def create_user(body: UserBody, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    actor = _user(session)
    _allow(actor, "platform_admin", "org_admin")
    org_id = body.org_id or actor["org_id"]
    if actor["role"] == "org_admin" and (body.role != "accountant" or org_id != actor["org_id"]):
        raise HTTPException(status_code=403, detail="机构管理员只能创建本机构会计账号。")
    try:
        created = store.create_user(body.username, body.password, body.display_name, body.role, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:
        if exc.__class__.__name__ == "IntegrityError":
            raise HTTPException(status_code=409, detail="用户名已存在。") from None
        raise
    store.log(actor, "create_user", "user", created["id"], created["role"])
    return created


@app.get("/api/clients")
def clients(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "platform_admin")
    return store.list_clients(user)


@app.post("/api/clients")
def create_client(body: ClientBody, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "platform_admin")
    if body.accountant_id:
        accountant = store.get_user(body.accountant_id)
        if not accountant or accountant["role"] != "accountant" or accountant["org_id"] != user["org_id"]:
            raise HTTPException(status_code=422, detail="负责人必须是本机构会计。")
    client = store.upsert_client(user, body.name.strip(), body.taxpayer_id.strip(), body.accountant_id)
    store.log(user, "upsert_client", "client", client["id"])
    return client


@app.post("/api/audit")
async def audit(
    file: UploadFile = File(...), client_id: str | None = Form(default=None),
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> Response:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "platform_admin")
    if file.filename is None or not file.filename.lower().endswith(".xlsx"):
        return _err(422, "仅支持 .xlsx 格式的审计材料（Excel 工作簿）。请使用标准模板导出。")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return _err(422, "文件过大：审计材料不得超过 10MB。")
    if not data:
        return _err(422, "上传文件为空。")
    try:
        dataset = loader.load_bytes(data)
    except loader.InputError as exc:
        return _err(422, f"审计材料不符合模板要求：{exc}")
    finally:
        data = b""
    return JSONResponse(_save_audit(dataset, user, client_id))


def _save_audit(dataset: Dataset, user: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    if user["role"] == "teacher" and not _is_synthetic_dataset(dataset):
        raise HTTPException(403, "教师只能导入明确标记为仿真样例的教学数据，严禁使用真实企业账套。")
    if client_id:
        client = store.get_client(client_id)
        if not client or client["org_id"] != user["org_id"]:
            raise HTTPException(422, "客户档案不存在或不属于当前机构。")
        if user["role"] == "accountant" and client.get("accountant_id") != user["id"]:
            raise HTTPException(403, "会计只能审计自己负责的客户。")
    elif user["role"] in {"org_admin", "accountant"}:
        assigned = user["id"] if user["role"] == "accountant" else None
        client_id = store.upsert_client(user, dataset.company.name, dataset.company.taxpayer_id, assigned)["id"]
    try:
        rules = engine.load_rules(RULES_DIR)
        enabled = store.enabled_rule_ids()
        if enabled is not None:
            rules = [rule for rule in rules if rule.id in enabled]
        findings = engine.run(rules, dataset)
    except engine.RuleError as exc:
        raise HTTPException(500, f"规则执行失败：{exc}")
    audit_id = uuid.uuid4().hex
    when = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    vm = render.build_view_model(dataset, findings)
    store.save_audit(audit_id, user, client_id, dataset, findings, vm["summary"], when)
    store.log(user, "create_audit", "audit", audit_id, f"{dataset.company.name}; rules={len(findings)}")
    entry = store.get_audit(audit_id)
    assert entry is not None
    return _result(entry)


from webapp.material_upload import register as register_material_upload
register_material_upload(app, _user, _allow, _save_audit, RULES_DIR)


@app.get("/api/audits")
def audits(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    if user["role"] == "student":
        raise HTTPException(status_code=403, detail="学生不能浏览审计档案。")
    return store.list_audits(user)


@app.get("/api/audits/{audit_id}")
def audit_detail(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    if user["role"] == "student":
        raise HTTPException(status_code=403, detail="学生不能浏览审计档案。")
    entry = _audit_or_404(audit_id, user)
    store.log(user, "view_audit", "audit", audit_id)
    return _result(entry)


@app.get("/api/report/{audit_id}")
def report(
    audit_id: str, background: BackgroundTasks, confirm: bool = False,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> Any:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "platform_admin")
    if user["role"] == "accountant" and not confirm:
        raise HTTPException(status_code=409, detail="会计导出需二次确认，请确认报告用途后重试。")
    entry = _audit_or_404(audit_id, user)
    dataset, findings = entry["dataset"], entry["findings"]
    org = store.get_org_settings(user["org_id"])
    try:
        when = datetime.fromisoformat(entry["audited_at"])
        html, _ = render.render_html(
            dataset, findings, when=when, write=False,
            org_name=org["display_name"], report_title=org["report_title"],
            footer_text=org["footer_text"],
        )
        report_no = render.make_report_no(dataset.company.name, when)
        short = dataset.company.name.replace("（仿真样例）", "")[:12]
        pdf_name = f"{org['report_title']}-{short}-{entry['audited_at'][:10].replace('-', '')}.pdf"
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        render.export_pdf(html, tmp, report_no=report_no, footer_text=org["footer_text"])
    except RuntimeError as exc:
        return _err(500, str(exc))
    store.log(user, "export_report", "audit", audit_id)
    background.add_task(os.remove, tmp)
    return FileResponse(tmp, media_type="application/pdf", filename=pdf_name, background=background)


@app.get("/api/report/{audit_id}/html")
def report_html(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "platform_admin")
    entry = _audit_or_404(audit_id, user)
    org = store.get_org_settings(user["org_id"])
    html, _ = render.render_html(
        entry["dataset"], entry["findings"], write=False,
        org_name=org["display_name"], report_title=org["report_title"],
        footer_text=org["footer_text"],
    )
    store.log(user, "view_report", "audit", audit_id)
    return Response(content=html, media_type="text/html")


@app.get("/api/org/settings")
def get_org_settings(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    return store.get_org_settings(user["org_id"])


@app.put("/api/org/settings")
def put_org_settings(
    body: OrgSettingsBody,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "platform_admin")
    try:
        saved = store.update_org_settings(
            user["org_id"], body.display_name, body.report_title, body.footer_text
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    store.log(user, "update_org_settings", "org", user["org_id"],
              f"title={saved['report_title']}")
    return saved


@app.get("/api/rules")
def rules(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    _user(session)
    loaded = engine.load_rules(RULES_DIR)
    enabled = store.enabled_rule_ids()
    return [
        {"id": r.id, "name": r.name, "category": r.category, "severity": r.severity,
         "version": r.version, "enabled": enabled is None or r.id in enabled}
        for r in loaded
    ]


@app.put("/api/rules/{rule_id}/state")
def rule_state(rule_id: str, body: RuleStateBody,
               session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin")
    valid = {rule.id for rule in engine.load_rules(RULES_DIR)}
    if rule_id not in valid:
        raise HTTPException(status_code=404, detail="规则不存在。")
    if store.enabled_rule_ids() is None:
        for existing in valid:
            store.set_rule_enabled(existing, True, user["id"])
    store.set_rule_enabled(rule_id, body.enabled, user["id"])
    store.log(user, "set_rule_state", "rule", rule_id, f"enabled={body.enabled}")
    return {"id": rule_id, "enabled": body.enabled}


@app.post("/api/assignments")
def create_assignment(body: AssignmentBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "teacher")
    entry = _audit_or_404(body.audit_id, user)
    dataset: Dataset = entry["dataset"]
    if not _is_synthetic_dataset(dataset):
        raise HTTPException(status_code=422, detail="实训作业只能使用明确标记的仿真数据。")
    hit_ids = {f.rule.id for f in entry["findings"] if f.status == "hit"}
    if not hit_ids:
        raise HTTPException(status_code=422, detail="该案例没有命中风险，无法生成可评分作业。")
    if set(body.weights) - hit_ids:
        raise HTTPException(status_code=422, detail="权重只能配置该案例实际命中的规则。")
    if body.target_student_id:
        student = store.get_user(body.target_student_id)
        if not student or student["role"] != "student" or student["org_id"] != user["org_id"]:
            raise HTTPException(status_code=422, detail="指定学生不存在或不属于当前机构。")
    assignment_id = store.create_assignment(
        user, body.title.strip(), body.audit_id, body.target_student_id,
        body.weights, body.false_positive_penalty, body.published,
    )
    store.log(user, "create_assignment", "assignment", assignment_id, body.audit_id)
    return {"id": assignment_id}


@app.get("/api/assignments")
def assignments(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "teacher", "student")
    return store.list_assignments(user)


@app.get("/api/assignments/{assignment_id}")
def assignment_detail(assignment_id: str,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "teacher", "student")
    assignment = store.get_assignment(assignment_id)
    if not assignment or assignment["org_id"] != user["org_id"]:
        raise HTTPException(status_code=404, detail="作业不存在。")
    if user["role"] == "student" and (not assignment["published"] or assignment["target_student_id"] not in (None, user["id"])):
        raise HTTPException(status_code=404, detail="作业不存在。")
    entry = store.get_audit(assignment["audit_id"])
    assert entry is not None
    dataset: Dataset = entry["dataset"]
    return {
        **assignment, "company": _company_dict(dataset),
        "accounts": [
            {"code": a.code, "name": a.name, "opening": str(a.opening), "debit": str(a.debit),
             "credit": str(a.credit), "closing": str(a.closing)} for a in dataset.accounts
        ],
        "declarations": {k: str(v) for k, v in dataset.declarations.items()},
        "rules": [{"id": f.rule.id, "name": f.rule.name, "category": f.rule.category} for f in entry["findings"]],
        "submission": store.get_submission(assignment_id, user["id"]) if user["role"] == "student" else None,
    }


@app.post("/api/assignments/{assignment_id}/submit")
def submit_assignment(assignment_id: str, body: SubmissionBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "student")
    assignment = store.get_assignment(assignment_id)
    if (not assignment or assignment["org_id"] != user["org_id"] or not assignment["published"]
            or assignment["target_student_id"] not in (None, user["id"])):
        raise HTTPException(status_code=404, detail="作业不存在。")
    entry = store.get_audit(assignment["audit_id"])
    assert entry is not None
    try:
        result = training.score_submission(
            entry["findings"], body.selected_rule_ids,
            assignment["weights"], assignment["false_positive_penalty"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    store.save_submission(assignment_id, user["id"], body.selected_rule_ids, result["score"], result)
    store.log(user, "submit_assignment", "assignment", assignment_id, f"score={result['score']}")
    return result


@app.put("/api/submissions/{submission_id}/review")
def review_submission(submission_id: str, body: ReviewBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "teacher")
    if store.submission_org(submission_id) != user["org_id"]:
        raise HTTPException(status_code=404, detail="提交记录不存在。")
    store.review_submission(submission_id, user["id"], body.adjusted_score, body.feedback.strip())
    store.log(user, "review_submission", "submission", submission_id, f"score={body.adjusted_score}")
    return {"ok": True}


@app.get("/api/submissions")
def submissions(assignment_id: str | None = None,
                session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "teacher")
    return store.list_submissions(user["org_id"], assignment_id)


@app.get("/api/audit-log")
def audit_log(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "teacher", "org_admin", "platform_admin")
    return store.list_logs(user)
