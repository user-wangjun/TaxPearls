"""TaxPearls P1 web application.

Persistent audits, Argon2 authentication, role-based access control, client
ownership, audit logs, rule switches and deterministic training/marking.
Uploaded workbook bytes stay in memory; only normalized evidence is stored.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Cookie, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, UnidentifiedImageError

from src import engine, loader, render, training
from src import settings  # Load .env before Store and route initialization.
from src.mailer import MailError, send_password_reset_email, send_registration_code_email
from src.models import Dataset, Rule
from webapp import captcha
from webapp.notifications import NotificationWorker, email_delivery_enabled
from webapp.storage import SetupAlreadyInitialized, Store
from webapp.login_guard import LoginGuard, RateLimiter
from webapp.knowledge import (
    ai_config, ask_graph, audit_narrative_hash, build_graph,
    finding_evidence_hash, generate_audit_narrative, interpret_finding,
)

ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "rules"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGO_SVG = ROOT / "logo" / "logo-shui-hai-shi-zhu.svg"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ORG_LOGO_BYTES = 512 * 1024
MAX_NORMALIZED_LOGO_BYTES = 1024 * 1024
MAX_ORG_LOGO_EDGE = 1200
COOKIE_NAME = "taxpearls_session"

@asynccontextmanager
async def lifespan(application):
    worker = NotificationWorker(lambda: store)
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="税海拾珠 · 税务风险审计", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)
store = Store()
login_guard = LoginGuard()
reset_limiter = RateLimiter({"email": (1, 15 * 60), "ip": (10, 60 * 60)})
register_code_limiter = RateLimiter({"email": (1, 15 * 60), "ip": (10, 60 * 60)})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


class SetupBody(BaseModel):
    username: str
    password: str
    display_name: str = ""
    email: str = ""
    org_id: str = "default"


class LoginBody(BaseModel):
    username: str
    password: str


class UserBody(BaseModel):
    username: str
    password: str
    display_name: str
    role: str
    email: str = ""
    org_id: str | None = None


class PasswordResetBody(BaseModel):
    email: str


class PasswordResetConfirmBody(BaseModel):
    token: str
    password: str


class EmailStartBody(BaseModel):
    email: str
    captcha_id: str
    captcha_answer: str


class RegisterCompleteBody(BaseModel):
    email: str
    code: str
    invite_code: str
    password: str


class InviteBody(BaseModel):
    org_name: str = Field(min_length=1, max_length=120)
    seats: int = Field(ge=1, le=200)
    bound_email: str
    expires_days: int = Field(default=7, ge=1, le=30)


class ClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    taxpayer_id: str = Field(min_length=1, max_length=64)
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


class NotificationPreferencesBody(BaseModel):
    audit_completed: bool = False
    high_risk: bool = False
    email_enabled: bool = False


class RuleParametersBody(BaseModel):
    expected_version: str = Field(min_length=1, max_length=32)
    new_version: str = Field(min_length=1, max_length=32)
    logic: dict[str, Any]
    threshold_basis: str = Field(min_length=1, max_length=500)
    effective_from: str | None = Field(default=None, max_length=10)
    effective_to: str | None = Field(default=None, max_length=10)


class RuleTrialBody(RuleParametersBody):
    audit_id: str = Field(min_length=1, max_length=64)


class OrgSettingsBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    report_title: str = Field(min_length=1, max_length=120)
    footer_text: str = Field(default="", max_length=300)


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


def _normalize_org_logo(content: bytes) -> tuple[str, bytes]:
    """Decode, constrain and re-encode logos so stored bytes cannot contain active content."""
    if not content:
        raise ValueError("Logo 文件为空。")
    if len(content) > MAX_ORG_LOGO_BYTES:
        raise ValueError("Logo 文件不得超过 512KB。")
    try:
        with Image.open(BytesIO(content)) as image:
            source_format = image.format
            if source_format not in {"PNG", "JPEG"}:
                raise ValueError("Logo 仅支持 PNG 或 JPEG 图片。")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("Logo 必须是静态图片。")
            width, height = image.size
            if width < 1 or height < 1 or width > MAX_ORG_LOGO_EDGE or height > MAX_ORG_LOGO_EDGE:
                raise ValueError("Logo 宽高须在 1–1200 像素之间。")
            image.load()
            normalized = ImageOps.exif_transpose(image)
            output = BytesIO()
            if source_format == "JPEG":
                normalized.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
                mime = "image/jpeg"
            else:
                mode = "RGBA" if "A" in normalized.getbands() or "transparency" in normalized.info else "RGB"
                normalized.convert(mode).save(output, format="PNG", optimize=True, compress_level=9)
                mime = "image/png"
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Logo 不是有效的 PNG 或 JPEG 图片。") from exc
    data = output.getvalue()
    if len(data) > MAX_NORMALIZED_LOGO_BYTES:
        raise ValueError("Logo 规范化后过大，请降低图片复杂度或尺寸。")
    return mime, data


def _org_branding(org_id: str) -> dict[str, Any]:
    settings = store.get_org_settings(org_id)
    logo = store.get_org_logo(org_id)
    settings["logo_data_uri"] = (
        f"data:{logo[0]};base64,{base64.b64encode(logo[1]).decode('ascii')}" if logo else ""
    )
    return settings


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(". ")
    return cleaned[:80] or "审计报告"


def _version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,2}", value.strip()):
        raise ValueError("规则版本须为 2.1 或 2.1.0 形式。")
    parts = [int(part) for part in value.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def _effective_rules() -> list[Any]:
    overrides = store.rule_overrides()
    histories = store.rule_versions()
    effective = []
    for base in engine.load_rules(RULES_DIR):
        override = overrides.get(base.id)
        if override and _version_tuple(override["version"]) > _version_tuple(base.version):
            record = next((item for item in histories.get(base.id, []) if item["version"] == override["version"]), None)
            frozen = Rule(**record["rule"]) if record and record.get("rule") else base
            candidate = replace(
                frozen, version=override["version"], logic=deepcopy(override["logic"]),
                threshold_basis=override["threshold_basis"],
                effective_from=override["effective_from"], effective_to=override["effective_to"],
            )
            engine.validate_rule_update(candidate)
            effective.append(candidate)
        else:
            effective.append(base)
    return effective


def _audit_rules(dataset: Dataset, enabled: set[str] | None = None) -> list[Any]:
    """Select one rule version covering the complete audited period.

    An interval crossing a version boundary needs a split-period audit; choosing
    a version by the end date would silently apply it to earlier transactions.
    """
    period = loader._parse_period(dataset.company.period, "审计所属期")
    histories = store.rule_versions()
    selected = []
    for base in engine.load_rules(RULES_DIR):
        if enabled is not None and base.id not in enabled:
            continue
        versions = histories.get(base.id, [])
        overlapping = [item for item in versions if item["effective_from"]
                       and item["effective_from"] <= period.end.isoformat()
                       and (item["effective_to"] or "9999-12-31") >= period.start.isoformat()]
        if overlapping:
            item = overlapping[0]
            if len(overlapping) != 1 or item["effective_from"] > period.start.isoformat() or (item["effective_to"] or "9999-12-31") < period.end.isoformat():
                raise HTTPException(422, f"规则 {base.id} 生效期跨越审计所属期；请拆分期间审计，不能混用版本。")
        else:
            undated = [item for item in versions if not item["effective_from"]]
            item = undated[-1] if undated else None
        if item:
            frozen = Rule(**item["rule"]) if item.get("rule") else base
            candidate = replace(frozen, version=item["version"], logic=deepcopy(item["logic"]),
                                threshold_basis=item["threshold_basis"],
                                effective_from=item["effective_from"], effective_to=item["effective_to"])
            selected.append(engine.validate_rule_update(candidate))
        else:
            selected.append(base)
    return selected


def _rule_by_id(rule_id: str) -> Any:
    rule = next((item for item in _effective_rules() if item.id == rule_id), None)
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在。")
    return rule


def _candidate_rule(rule: Any, body: RuleParametersBody) -> Any:
    if body.expected_version.strip() != rule.version:
        raise HTTPException(status_code=409, detail=f"规则已更新为 v{rule.version}，请刷新后重试。")
    try:
        current_version = _version_tuple(rule.version)
        new_version = _version_tuple(body.new_version.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if new_version <= current_version:
        raise HTTPException(status_code=422, detail=f"新版本必须高于当前 v{rule.version}。")
    effective_from = body.effective_from or None
    effective_to = body.effective_to or None
    if effective_to and not effective_from:
        raise HTTPException(422, "规则终止日期必须同时提供起始日期。")
    for value in (effective_from, effective_to):
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise HTTPException(422, "规则生效日期须为 YYYY-MM-DD。")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(422, "规则生效日期须为有效的 YYYY-MM-DD。") from None
    if effective_from and effective_to and effective_from > effective_to:
        raise HTTPException(422, "规则生效终止日不能早于起始日。")
    candidate = replace(
        rule, version=body.new_version.strip(), logic=deepcopy(body.logic),
        threshold_basis=body.threshold_basis.strip(),
        effective_from=effective_from, effective_to=effective_to,
    )
    try:
        return engine.validate_rule_update(candidate)
    except engine.RuleError as exc:
        raise HTTPException(status_code=422, detail=f"规则参数无效：{exc}") from None


def _rule_payload(rule: Any, enabled: bool, override: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": rule.id, "name": rule.name, "category": rule.category,
        "severity": rule.severity, "version": rule.version, "enabled": enabled,
        "logic": rule.logic, "inputs": rule.inputs,
        "threshold_basis": rule.threshold_basis,
        "effective_from": rule.effective_from, "effective_to": rule.effective_to,
        "customized": bool(override and override.get("version") == rule.version),
        "updated_at": override.get("updated_at") if override else None,
    }


def _trial_payload(finding: Any, audit_id: str) -> dict[str, Any]:
    return {
        "audit_id": audit_id, "rule_id": finding.rule.id,
        "version": finding.rule.version, "name": finding.rule.name,
        "effective_from": finding.rule.effective_from,
        "effective_to": finding.rule.effective_to,
        "status": finding.status, "severity": finding.rule.severity,
        "conclusion": finding.conclusion, "calculation": finding.calculation,
        "threshold_desc": finding.threshold_desc, "skip_reason": finding.skip_reason,
        "evidence": [
            {"label": item.label, "value": item.value, "source": item.source}
            for item in finding.evidence
        ],
    }


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
    narrative = store.get_audit_narrative(entry["id"], audit_narrative_hash(findings))
    return {
        "audit_id": entry["id"], "audited_at": entry["audited_at"],
        "company": _company_dict(dataset), "summary": vm["summary"],
        "findings": vm["findings"], "metrics": _metrics_list(dataset),
        "narrative": narrative,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/logo")
def logo() -> Response:
    return FileResponse(LOGO_SVG, media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
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
    clients = [] if user["role"] == "teacher" else store.list_clients(user)
    return {"records": records, "history": history, "clients": clients}


@app.get("/api/knowledge")
def knowledge(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    _user(session)
    enabled = store.enabled_rule_ids()
    return [{"id": r.id, "name": r.name, "category": r.category,
             "tax_type": r.tax_type, "severity": r.severity, "scope": r.scope,
             "inputs": r.inputs, "evidence": r.evidence, "legal_basis": r.legal_basis,
             "references": r.references, "suggestion": r.suggestion,
             "enabled": enabled is None or r.id in enabled}
            for r in _effective_rules()]


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
    return build_graph(_effective_rules(), entry)


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


@app.post("/api/audits/{audit_id}/findings/{rule_id}/interpretation")
def finding_interpretation(audit_id: str, rule_id: str,
                           session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "platform_admin")
    entry = _audit_or_404(audit_id, user)
    finding = next((item for item in entry["findings"] if item.rule.id == rule_id), None)
    if not finding:
        raise HTTPException(status_code=404, detail="该审计中不存在指定 Finding。")
    if finding.status != "hit":
        raise HTTPException(status_code=422, detail="仅可对规则引擎已命中的 Finding 生成白话解读。")
    evidence_hash = finding_evidence_hash(finding)
    cached = store.get_finding_interpretation(audit_id, rule_id, evidence_hash)
    if cached:
        store.log(user, "interpret_finding", "audit", audit_id,
                  f"rule={rule_id};cache=hit;model={cached['model']}")
        return cached
    result = interpret_finding(finding)
    if result["evidence_hash"] != evidence_hash:
        raise HTTPException(status_code=502, detail="模型解读与当前审计证据版本不一致。")
    saved = store.save_finding_interpretation(
        audit_id, rule_id, evidence_hash, result, user["id"],
    )
    store.log(user, "interpret_finding", "audit", audit_id,
              f"rule={rule_id};cache=miss;model={result['model']}")
    return {**saved, "cached": False}


@app.post("/api/audits/{audit_id}/narrative")
def audit_narrative(audit_id: str,
                    session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "platform_admin")
    entry = _audit_or_404(audit_id, user)
    findings = entry["findings"]
    evidence_hash = audit_narrative_hash(findings)
    cached = store.get_audit_narrative(audit_id, evidence_hash)
    if cached:
        store.log(user, "generate_audit_narrative", "audit", audit_id,
                  f"cache=hit;model={cached['model']}")
        return cached
    result = generate_audit_narrative(findings)
    if result["evidence_hash"] != evidence_hash:
        raise HTTPException(status_code=502, detail="模型总体结论与当前审计证据版本不一致。")
    saved = store.save_audit_narrative(audit_id, evidence_hash, result, user["id"])
    store.log(user, "generate_audit_narrative", "audit", audit_id,
              f"cache=miss;model={result['model']}")
    return {**saved, "cached": False}


@app.get("/graph.js")
def graph_script():
    return FileResponse(STATIC_DIR / "graph.js", media_type="text/javascript")


@app.post("/api/setup")
def setup(body: SetupBody) -> dict[str, Any]:
    try:
        user = store.create_initial_admin(body.username, body.password, body.display_name.strip() or body.username,
                                          body.org_id, body.email)
    except SetupAlreadyInitialized:
        raise HTTPException(status_code=409, detail="系统已初始化。") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc):
            raise HTTPException(status_code=409, detail="该邮箱已被占用，请换用其他邮箱。") from None
        raise HTTPException(status_code=409, detail="账号与现有数据冲突。") from None
    store.log(user, "setup", "system", "initial")
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request) -> Response:
    ip = request.client.host if request.client else "unknown"
    with login_guard.reserve(body.username, ip) as wait:
        if wait:
            return JSONResponse(status_code=429, content={"detail": "登录尝试过于频繁，请稍后重试。"},
                                headers={"Retry-After": str(wait)})
        result = store.authenticate(body.username, body.password)
        if not result:
            count = login_guard.record_failure(body.username, ip)
            store.log(None, "login_failed", "account_hash", login_guard.fingerprint(body.username.strip().lower()),
                      f"ip_hash={login_guard.fingerprint(ip)};failure_count={count}")
            return _err(401, "用户名或密码错误。")
        user, token = result
        login_guard.record_success(body.username)
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


@app.post("/api/auth/password/reset")
def password_reset(body: PasswordResetBody, request: Request) -> dict[str, Any]:
    """申请重置邮件。防枚举：无论邮箱是否存在，成功响应完全一致。"""
    ip = request.client.host if request.client else "unknown"
    if not reset_limiter.allow(email=body.email, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    token = store.create_password_reset(body.email)
    if token:
        base = (os.getenv("TAXPEARLS_PUBLIC_BASE_URL", "").strip().rstrip("/")
                or str(request.base_url).rstrip("/"))
        try:
            send_password_reset_email(to=body.email.strip().lower(), reset_url=f"{base}/?reset={token}")
        except MailError as exc:
            store.log(None, "password_reset_failed", "email_hash",
                      login_guard.fingerprint(body.email.strip().lower()), str(exc)[:200])
            raise HTTPException(status_code=502, detail="重置邮件发送失败，请稍后重试。") from None
        # 留痕口径：发送事件入审计日志，邮箱只以哈希出现，不落明文。
        store.log(None, "password_reset_sent", "email_hash",
                  login_guard.fingerprint(body.email.strip().lower()),
                  f"ip_hash={login_guard.fingerprint(ip)}")
    return {"message": "若该邮箱已注册，重置邮件已发送，请查收（含垃圾箱）。"}


@app.post("/api/auth/password/reset/confirm")
def password_reset_confirm(body: PasswordResetConfirmBody) -> dict[str, Any]:
    try:
        user = store.redeem_password_reset(body.token, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    store.log(user, "password_reset", "user", user["id"])
    return {"message": "密码已重置，请使用新密码登录。"}


@app.get("/api/auth/captcha")
def auth_captcha() -> dict[str, str]:
    """签发算术人机验证题。"""
    return captcha.issue()


@app.post("/api/auth/email/start")
def email_start(body: EmailStartBody, request: Request) -> dict[str, Any]:
    """注册第一步：人机验证 + 发送邮箱验证码。

    - 人机验证挡的是「发送验证码」这个可被滥用的动作
    - 邮箱已注册时不发信，提示直接登录
    - 限频：单邮箱 15 分钟 1 次 / 单 IP 每小时 10 次
    """
    ip = request.client.host if request.client else "unknown"
    if not captcha.verify(body.captcha_id, body.captcha_answer):
        return JSONResponse(status_code=422, content={"detail": "人机验证不正确，请重试。"})
    email = body.email.strip().lower()
    if not register_code_limiter.allow(email=email, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "发送过于频繁，请 15 分钟后再试。"})
    if store.get_user_by_email(email):
        return {"message": "该邮箱已注册，请直接登录；忘记密码可用登录页的「忘记密码？」找回。",
                "exists": True}
    code = store.create_register_code(email)
    if not code:
        return JSONResponse(status_code=422, content={"detail": "邮箱格式不正确。"})
    base = (os.getenv("TAXPEARLS_PUBLIC_BASE_URL", "").strip().rstrip("/")
            or str(request.base_url).rstrip("/"))
    signup_url = f"{base}/?email={email}&code={code}"
    try:
        send_registration_code_email(to=email, code=code, signup_url=signup_url)
    except MailError as exc:
        store.log(None, "register_code_failed", "email_hash",
                  login_guard.fingerprint(email), str(exc)[:200])
        raise HTTPException(status_code=502, detail="验证邮件发送失败，请稍后重试。") from None
    store.log(None, "register_code_sent", "email_hash",
              login_guard.fingerprint(email), f"ip_hash={login_guard.fingerprint(ip)}")
    return {"message": "验证码已发送，10 分钟内有效；输错 5 次将作废。", "exists": False}


@app.post("/api/register/complete")
def register_complete(body: RegisterCompleteBody) -> Response:
    """开户最后一步：邮箱验证码 + 创始码在单事务内核验并建号。

    第一层（创始码）：建机构，注册者成为 org_admin。第二层（机构链接）为第二轮实现。
    """
    try:
        user, token = store.register_with_code(body.email, body.code, body.invite_code, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc):
            raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录。") from None
        raise HTTPException(status_code=409, detail="账号与现有数据冲突。") from None
    response = JSONResponse({"user": user, "org_id": user["org_id"]})
    response.set_cookie(
        COOKIE_NAME, token, max_age=12 * 3600, httponly=True,
        samesite="strict", secure=os.environ.get("TAXPEARLS_COOKIE_SECURE") == "1",
    )
    return response


@app.post("/api/invites")
def create_invite(body: InviteBody, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    """平台管理员签发一次性创始码。"""
    actor = _user(session)
    _allow(actor, "platform_admin")
    try:
        invite = store.create_invite_code(actor, body.org_name, body.seats,
                                          body.bound_email, body.expires_days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    store.log(actor, "invite_created", "invite", invite["code"][:4] + "…",
              f"org={body.org_name};seats={body.seats}")
    return invite


@app.get("/api/invites")
def list_invites(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    actor = _user(session)
    _allow(actor, "platform_admin")
    return store.list_invite_codes()


@app.get("/api/me")
def me(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    return _user(session)


@app.get("/api/notifications/preferences")
def notification_preferences(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin", "accountant", "teacher")
    account = store.get_user(user["id"])
    return {**store.notification_preferences(user["id"]), "has_email": bool(account and account.get("email")),
            "delivery_enabled": email_delivery_enabled()}


def _save_notification_preferences(user: dict[str, Any], target_id: str, body: NotificationPreferencesBody) -> dict[str, Any]:
    try:
        saved = store.set_notification_preferences(user, target_id, body.audit_completed, body.high_risk, body.email_enabled)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    store.log(user, "notification_preferences", "user", target_id,
              f"audit_completed={body.audit_completed};high_risk={body.high_risk};email={body.email_enabled}")
    return saved


@app.put("/api/notifications/preferences")
def update_notification_preferences(body: NotificationPreferencesBody,
        session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin", "accountant", "teacher")
    return _save_notification_preferences(user, user["id"], body)


@app.get("/api/notifications/recipients")
def notification_recipients(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    members = store.list_users(None if user["role"] == "platform_admin" else user["org_id"])
    return [{"id": member["id"], "display_name": member["display_name"], "org_id": member["org_id"],
             "role": member["role"], "has_email": bool(member.get("email")),
             **store.notification_preferences(member["id"])}
            for member in members if member["active"] and member["role"] != "student"]


@app.put("/api/notifications/recipients/{user_id}")
def update_notification_recipient(user_id: str, body: NotificationPreferencesBody,
        session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    return _save_notification_preferences(user, user_id, body)


@app.get("/api/notifications")
def notifications(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin", "accountant", "teacher")
    return store.list_notifications(user)


@app.put("/api/notifications/{notification_id}/read")
def read_notification(notification_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin", "accountant", "teacher")
    if not store.mark_notification_read(user, notification_id):
        raise HTTPException(404, "通知不存在或无权查看。")
    return {"id": notification_id, "read": True}


@app.post("/api/notifications/{notification_id}/retry")
def retry_notification(notification_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin", "accountant", "teacher")
    try:
        authorized = store.retry_notification_delivery(user, notification_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not authorized:
        raise HTTPException(404, "通知不存在或无权访问。")
    store.log(user, "notification_retry", "notification", notification_id, "manual retry")
    return {"id": notification_id, "email_status": "pending"}


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
        created = store.create_user(body.username, body.password, body.display_name, body.role, org_id, body.email)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except sqlite3.IntegrityError as exc:
        if "users.username" in str(exc):
            raise HTTPException(status_code=409, detail="用户名已存在。") from None
        if "users.email" in str(exc):
            raise HTTPException(status_code=409, detail="该邮箱已被占用，请换用其他邮箱。") from None
        if "users.role" in str(exc):
            raise HTTPException(status_code=409, detail="平台管理员已存在。") from None
        raise HTTPException(status_code=409, detail="账号与现有数据冲突。") from None
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
    name, taxpayer_id = body.name.strip(), body.taxpayer_id.strip()
    if not name or not taxpayer_id:
        raise HTTPException(status_code=422, detail="企业名称和纳税人识别号不能为空。")
    if body.accountant_id:
        accountant = store.get_user(body.accountant_id)
        if not accountant or accountant["role"] != "accountant" or accountant["org_id"] != user["org_id"]:
            raise HTTPException(status_code=422, detail="负责人必须是本机构会计。")
    client = store.upsert_client(user, name, taxpayer_id, body.accountant_id)
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
        if client["taxpayer_id"] != dataset.company.taxpayer_id:
            raise HTTPException(422, "审计材料中的纳税人识别号与所选客户档案不一致。")
    elif user["role"] in {"org_admin", "accountant"}:
        assigned = user["id"] if user["role"] == "accountant" else None
        client_id = store.upsert_client(user, dataset.company.name, dataset.company.taxpayer_id, assigned)["id"]
    try:
        enabled = store.enabled_rule_ids()
        rules = _audit_rules(dataset, enabled)
        findings = engine.run(rules, dataset)
        # FR-B09 is a separate relationship traversal, not a YAML condition.
        from src import related_graph
        findings.extend(related_graph.run(dataset))
        findings.sort(key=lambda f: ({"hit": 0, "pass": 1, "skipped": 2}[f.status], -f.severity_rank, f.rule.id))
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
    narrative = store.get_audit_narrative(audit_id, audit_narrative_hash(findings))
    org = _org_branding(entry["org_id"])
    try:
        when = datetime.fromisoformat(entry["audited_at"])
        html, _ = render.render_html(
            dataset, findings, when=when, write=False,
            org_name=org["display_name"], report_title=org["report_title"],
            footer_text=org["footer_text"], logo_data_uri=org["logo_data_uri"],
            ai_narrative=narrative,
        )
        report_no = render.make_report_no(dataset.company.name, when)
        short = _safe_filename_component(dataset.company.name.replace("（仿真样例）", "")[:12])
        title = _safe_filename_component(org["report_title"])
        pdf_name = f"{title}-{short}-{entry['audited_at'][:10].replace('-', '')}.pdf"
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
    org = _org_branding(entry["org_id"])
    narrative = store.get_audit_narrative(
        audit_id, audit_narrative_hash(entry["findings"]),
    )
    html, _ = render.render_html(
        entry["dataset"], entry["findings"],
        when=datetime.fromisoformat(entry["audited_at"]), write=False,
        org_name=org["display_name"], report_title=org["report_title"],
        footer_text=org["footer_text"], logo_data_uri=org["logo_data_uri"],
        ai_narrative=narrative,
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


@app.get("/api/org/logo")
def get_org_logo(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = _user(session)
    logo = store.get_org_logo(user["org_id"])
    if not logo:
        raise HTTPException(status_code=404, detail="当前机构尚未配置 Logo。")
    return Response(
        content=logo[1], media_type=logo[0],
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/org/logo")
async def put_org_logo(
    file: UploadFile = File(...),
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "platform_admin")
    content = await file.read(MAX_ORG_LOGO_BYTES + 1)
    try:
        mime, normalized = _normalize_org_logo(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    finally:
        content = b""
    saved = store.update_org_logo(user["org_id"], mime, normalized)
    store.log(user, "update_org_logo", "org", user["org_id"], f"mime={mime}; bytes={len(normalized)}")
    return saved


@app.delete("/api/org/logo")
def delete_org_logo(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "platform_admin")
    saved = store.clear_org_logo(user["org_id"])
    store.log(user, "delete_org_logo", "org", user["org_id"])
    return saved


@app.get("/api/rules")
def rules(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    _user(session)
    loaded = _effective_rules()
    overrides = store.rule_overrides()
    enabled = store.enabled_rule_ids()
    return [
        _rule_payload(r, enabled is None or r.id in enabled, overrides.get(r.id))
        for r in loaded
    ]


@app.get("/api/rules/{rule_id}/versions")
def rule_versions(rule_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin")
    _rule_by_id(rule_id)
    return store.rule_versions(rule_id).get(rule_id, [])


@app.put("/api/rules/{rule_id}/state")
def rule_state(rule_id: str, body: RuleStateBody,
               session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin")
    valid = {rule.id for rule in _effective_rules()}
    if rule_id not in valid:
        raise HTTPException(status_code=404, detail="规则不存在。")
    if store.enabled_rule_ids() is None:
        for existing in valid:
            store.set_rule_enabled(existing, True, user["id"])
    store.set_rule_enabled(rule_id, body.enabled, user["id"])
    store.log(user, "set_rule_state", "rule", rule_id, f"enabled={body.enabled}")
    return {"id": rule_id, "enabled": body.enabled}


@app.put("/api/rules/{rule_id}/parameters")
def rule_parameters(
    rule_id: str, body: RuleParametersBody,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin")
    # Capture the database revision before loading the current YAML/library
    # version. A deployment may have a newer base than the stored override.
    previous_override = store.rule_overrides().get(rule_id)
    current = _rule_by_id(rule_id)
    candidate = _candidate_rule(current, body)
    try:
        saved = store.set_rule_override(
            rule_id, candidate.version, candidate.logic, candidate.threshold_basis,
            user["id"], previous_override["version"] if previous_override else current.version,
            candidate.effective_from, candidate.effective_to,
            asdict(candidate),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    store.log(
        user, "update_rule_parameters", "rule", rule_id,
        f"version={current.version}->{candidate.version};effective={candidate.effective_from or 'legacy'}"
        f"..{candidate.effective_to or 'open'};closed={saved['closed_version'] or '-'}@{saved['closed_on'] or '-'}",
    )
    enabled = store.enabled_rule_ids()
    return _rule_payload(candidate, enabled is None or rule_id in enabled, saved)


@app.post("/api/rules/{rule_id}/trial")
def rule_trial(
    rule_id: str, body: RuleTrialBody,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin", "accountant", "teacher")
    entry = _audit_or_404(body.audit_id, user)
    current = _rule_by_id(rule_id)
    candidate = _candidate_rule(current, body)
    finding = engine.evaluate(candidate, entry["dataset"])
    store.log(
        user, "trial_rule_parameters", "rule", rule_id,
        f"audit={body.audit_id}; version={candidate.version}; result={finding.status}",
    )
    return _trial_payload(finding, body.audit_id)


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
