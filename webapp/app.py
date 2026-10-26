"""TaxPearls P1 web application.

Persistent audits, Argon2 authentication, role-based access control, client
ownership, audit logs, rule switches and deterministic training/marking.
Enterprise originals and sensitive evidence use authenticated encryption at rest.
"""
from __future__ import annotations
from src import periods
from src.ai_transport import TransportError

import base64
import json
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
from typing import Any, Annotated, Literal
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, Cookie, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ConfigDict, AwareDatetime, model_validator
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool
from webapp.uploads import multipart, single_file

from src import engine, loader, render, training, sandbox_feedback, legal_periods
from src import settings as _environment_settings  # noqa: F401 - Load .env before Store and route initialization.
from src.mailer import MailError, send_password_reset_email, send_registration_code_email, send_login_code_email
from src.models import Dataset, Rule
from webapp import captcha, classroom, members, email_auth, ai_probes, ai_keys, policy_graph
from webapp.access import AccessDenied, current_actor
from webapp.enterprise_scope import identity
from webapp.sensitive_storage import SensitiveStorageError
from webapp.notifications import NotificationWorker, email_delivery_enabled
from webapp.storage import SetupAlreadyInitialized, Store, session_max_age
from webapp.login_guard import LoginGuard, RateLimiter
from webapp.knowledge import (
    ai_config, ask_graph, audit_narrative_hash, build_graph,
    finding_evidence_hash, generate_audit_narrative, interpret_finding, extract_policy_candidates,
)

ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "rules"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGO_SVG = ROOT / "logo" / "logo-shui-hai-shi-zhu.svg"
MAX_ORG_LOGO_BYTES = 512 * 1024
MAX_NORMALIZED_LOGO_BYTES = 1024 * 1024
MAX_ORG_LOGO_EDGE = 1200
COOKIE_NAME = "taxpearls_session"
EMAIL_COOKIE_NAME = "taxpearls_email_browser"

@asynccontextmanager
async def lifespan(application):
    global store
    if store is None:
        store = Store(keepalive=True)
    from src.public_policy_package import bootstrap
    # 公共图谱导入只在"新库第一次启动"时发生（装好即打标记短路）。本地 Windows 全量单测
    # 每个用例都是新库，每次要完整导入 2.4 万个节点（实测约 12s），因此留一个只给本地用的开关。
    # 生产与 CI 都不许关：关掉后启动不保证公共政策图谱已安装，知识问答/引用校验会缺数据。
    if _environment_settings.flag('TAXPEARLS_PUBLIC_PACKAGE_INSTALL', True):
        application.state.public_knowledge = await run_in_threadpool(bootstrap, store, RULES_DIR)
    else:
        application.state.public_knowledge = {'status': 'skipped',
                                              'reason': 'TAXPEARLS_PUBLIC_PACKAGE_INSTALL=0'}
    worker = NotificationWorker(lambda: store)
    worker.start()
    from webapp.retrieval_checks import Worker as RetrievalCheckWorker
    retrieval_worker = RetrievalCheckWorker(lambda: store)
    retrieval_worker.start()
    try:
        application.state.material_worker.start()
        yield
    finally:
        application.state.material_worker.stop()
        retrieval_worker.stop()
        worker.stop()


app = FastAPI(title="税海拾珠 · 税务风险审计", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.exception_handler(AccessDenied)
async def denied_access(_request: Request, exc: AccessDenied):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.exception_handler(SensitiveStorageError)
async def sensitive_storage_failure(_request: Request, exc: SensitiveStorageError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.middleware("http")
async def private_api_responses(request: Request, call_next):
    if request.url.path in {'/api/knowledge/node-ask', '/api/knowledge/rag-ask', '/api/knowledge/ask',
                            '/api/training/knowledge/ask'} and request.method == 'POST':
        parts, size = [], 0
        async for part in request.stream():
            size += len(part)
            if size > 24576:
                return JSONResponse(status_code=413, content={'detail': '问答输入过大，请缩短问题。'})
            parts.append(part)
        request._body = b''.join(parts)
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers.append('Vary', 'Cookie')
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError):
    # Do not echo credentials/raw request data, including JSON NaN/Infinity
    # which the default validation response cannot safely JSON-encode.
    return JSONResponse(status_code=422,content={"detail":[
        {"loc":error["loc"],"type":error["type"],"msg":error["msg"]} for error in exc.errors()
    ]},headers={"Cache-Control":"no-store"})
store: Store | None = None
ai_keys.STORE_PROVIDER = lambda: store
from webapp.feishu_binding import register as register_feishu_binding
register_feishu_binding(app, lambda: store, COOKIE_NAME)
login_guard = LoginGuard()
reset_limiter = RateLimiter({"email": (1, 15 * 60), "ip": (10, 60 * 60)})
register_code_limiter = RateLimiter({"email": (1, 15 * 60), "ip": (10, 60 * 60)})
register_complete_limiter = RateLimiter({"email": (10, 15 * 60), "ip": (60, 60 * 60)})
reset_confirm_limiter = RateLimiter({"token": (5, 15 * 60), "ip": (40, 15 * 60)})
captcha_limiter = RateLimiter({"ip": (30, 60)})
email_verify_limiter = RateLimiter({"token": (10, 15 * 60), "ip": (60, 15 * 60)})
email_login_limiter = RateLimiter({"email": (10, 15 * 60), "ip": (60, 15 * 60)})
ai_probe_limiter = RateLimiter({'actor': (30, 60), 'target': (4, 60)})


class AIProbeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['health', 'balance']
    key_id: str = Field(default='', max_length=64)
    api_key: str = Field(default='', max_length=4096, repr=False)
    provider: Literal['configured', 'deepseek', 'openrouter', 'custom'] = 'configured'
    base_url: str = Field(default='', max_length=500)
    model: str = Field(default='', max_length=200)
    balance_protocol: Literal['auto', 'deepseek', 'openrouter', 'credit_grants'] = 'auto'


class AIKeyUsageBody(BaseModel):
    usage: Literal['all', 'extraction', 'knowledge', 'report', 'tutor', 'none'] | None = None
    usages: list[Literal['extraction', 'knowledge', 'report', 'tutor']] | None = Field(default=None, max_length=4)

    @model_validator(mode='after')
    def check_usages(self):
        if 'usages' in self.model_fields_set and (self.usages is None or 'usage' in self.model_fields_set):
            raise ValueError('请使用用途多选或旧版单用途格式，不能混用。')
        if self.usages is not None and len(self.usages) != len(set(self.usages)):
            raise ValueError('用途不能重复。')
        return self


class AIKeyCreateBody(AIKeyUsageBody):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    api_key: str = Field(min_length=1, max_length=4096, repr=False)
    provider: Literal['configured', 'deepseek', 'openrouter', 'custom'] = 'configured'
    model: str = Field(default='', max_length=200)
    base_url: str = Field(default='', max_length=500)
    usage: Literal['all', 'extraction', 'knowledge', 'report', 'tutor', 'none'] | None = None
    balance_protocol: Literal['auto', 'deepseek', 'openrouter', 'credit_grants'] = 'auto'
    json_mode: bool | None = Field(default=None, strict=True)
    vision: bool | None = Field(default=None, strict=True)
    disable_thinking: bool | None = Field(default=None, strict=True)
    reasoning_effort: Literal['default', 'none', 'enabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] | None = None


class AIKeyEditBody(AIKeyUsageBody):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    api_key: str = Field(default='', max_length=4096, repr=False)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    usage: Literal['all', 'extraction', 'knowledge', 'report', 'tutor', 'none'] | None = None
    balance_protocol: Literal['auto', 'deepseek', 'openrouter', 'credit_grants'] | None = None
    json_mode: bool | None = Field(default=None, strict=True)
    vision: bool | None = Field(default=None, strict=True)
    disable_thinking: bool | None = Field(default=None, strict=True)
    reasoning_effort: Literal['default', 'none', 'enabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] | None = None


class AIKeyDeleteBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(min_length=1, max_length=64)


class AIModelsBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default='', max_length=4096, repr=False)
    key_id: str = Field(default='', max_length=64)
    revision: str = Field(default='', max_length=64)


def _ai_key_origin(request):
    origin = request.headers.get('origin')
    if (request.headers.get('sec-fetch-site') == 'cross-site' or
            (origin and origin.rstrip('/') != str(request.base_url).rstrip('/'))):
        raise HTTPException(403, '请从本站管理员页面发起操作。')


@app.post('/api/admin/ai-keys')
def ai_key_create(body: AIKeyCreateBody, request: Request,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, 'platform_admin')
    _ai_key_origin(request)
    try:
        settings = _environment_settings.AISettings.from_env()
    except ValueError:
        raise HTTPException(503, 'AI 环境配置无效，请检查服务端配置。') from None
    return ai_keys.add(store, user, settings, name=body.name, key=body.api_key,
                       provider=body.provider, model=body.model, base_url=body.base_url,
                       usage=body.usage, balance_protocol=body.balance_protocol,
                       json_mode=body.json_mode, vision=body.vision, disable_thinking=body.disable_thinking,
                       reasoning_effort=body.reasoning_effort, usages=body.usages)


@app.put('/api/admin/ai-keys/{key_id}')
def ai_key_edit(key_id: str, body: AIKeyEditBody, request: Request,
                session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, 'platform_admin')
    _ai_key_origin(request)
    try:
        settings = _environment_settings.AISettings.from_env()
    except ValueError:
        raise HTTPException(503, 'AI 环境配置无效，请检查服务端配置。') from None
    return ai_keys.update(store, user, settings, key_id, body.revision, name=body.name, key=body.api_key,
                          base_url=body.base_url, model=body.model, usage=body.usage,
                          balance_protocol=body.balance_protocol, json_mode=body.json_mode,
                          vision=body.vision, disable_thinking=body.disable_thinking, reasoning_effort=body.reasoning_effort,
                          usages=body.usages)


@app.post('/api/admin/ai-keys/{key_id}/reveal')
def ai_key_reveal(key_id: str, body: AIKeyDeleteBody, request: Request,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, 'platform_admin')
    _ai_key_origin(request)
    if not ai_probe_limiter.allow(actor=user['id'], target=key_id + ':reveal'):
        raise HTTPException(429, '查看过于频繁，请一分钟后重试。')
    try:
        settings = _environment_settings.AISettings.from_env()
    except ValueError:
        raise HTTPException(503, 'AI 环境配置无效，请检查服务端配置。') from None
    return ai_keys.reveal(store, user, settings, key_id, body.revision)


@app.delete('/api/admin/ai-keys/{key_id}')
def ai_key_delete(key_id: str, body: AIKeyDeleteBody, request: Request,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, 'platform_admin')
    _ai_key_origin(request)
    try:
        settings = _environment_settings.AISettings.from_env()
    except ValueError:
        raise HTTPException(503, 'AI 环境配置无效，请检查服务端配置。') from None
    return ai_keys.remove(store, user, settings, key_id, body.revision)


@app.get('/api/admin/ai-keys')
def ai_key_catalog(session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, 'platform_admin')
    try:
        settings = _environment_settings.AISettings.from_env()
        return ai_keys.catalog(settings, store, user)
    except ValueError:
        raise HTTPException(503, 'AI 环境配置无效，请检查服务端配置。') from None


@app.post('/api/admin/ai-keys/models')
def ai_key_models(body: AIModelsBody, request: Request,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, 'platform_admin')
    _ai_key_origin(request)
    try:
        settings = _environment_settings.AISettings.from_env()
    except ValueError:
        raise HTTPException(503, 'AI 环境配置无效，请检查服务端配置。') from None
    if body.key_id:
        with store.connect() as db:
            entry = ai_keys._find(db, user, settings, body.key_id, body.revision)
        key = body.api_key.strip() or entry['settings'].api_key
    else:
        if body.revision:
            raise HTTPException(422, '请核对密钥选择。')
        key = body.api_key.strip()
    target = replace(settings, enabled=True, base_url=body.base_url.strip().rstrip('/'),
                     api_key=key, backup_api_keys=(), model='model-list')
    if target.problem():
        raise HTTPException(422, '请填写有效的接口 URL 和 key 后获取模型。')
    if not ai_probe_limiter.allow(actor=user['id'], target=ai_probes.identity(target) + ':models'):
        raise HTTPException(429, '获取过于频繁，请一分钟后重试。')
    try:
        data = ai_probes.models(target, with_capabilities=True)
    except TransportError as exc:
        message = ('上游未提供兼容的模型列表接口，请手动填写模型名称。' if exc.status == 404 else
                   ai_probes.MESSAGES.get({401:'invalid_key',402:'insufficient_balance',403:'forbidden',
                       429:'rate_limited'}.get(exc.status,exc.kind), '获取模型列表失败，请核对上游服务。'))
        raise HTTPException(502, message) from None
    user = _user(session)
    _allow(user, 'platform_admin')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        from webapp.access import current_actor
        current_actor(db, user, {'platform_admin'})
        if body.key_id:
            ai_keys._find(db, user, _environment_settings.AISettings.from_env(), body.key_id, body.revision)
        ai_keys.save_capabilities(db, target, data['capabilities'])
        store._log(db, user, 'ai_models_fetched', 'ai_key', body.key_id or 'temporary',
                   json.dumps({'count': len(data['models'])}))
    return data


@app.post('/api/admin/ai-keys/probe')
def ai_key_probe(body: AIProbeBody, request: Request,
                 session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, 'platform_admin')
    _ai_key_origin(request)
    try:
        settings = _environment_settings.AISettings.from_env()
        if body.key_id:
            if body.api_key or body.provider != 'configured' or body.model or body.base_url:
                raise HTTPException(422, '已配置密钥检测不能混用临时密钥或覆盖模型。')
            selected_entry = next((entry for entry in ai_keys.entries(settings, store)
                                   if ai_probes.identity(entry['settings']) == body.key_id), None)
            target = selected_entry['settings'] if selected_entry else None
            if target is None:
                raise HTTPException(404, '密钥配置已变更，请刷新列表后重试。')
            with store.connect() as db:
                if body.operation == 'health' and not ai_keys._reasoning_valid(db, target):
                    raise HTTPException(422, '已保存的思考程度不被上游支持，请编辑配置后测活。')
        else:
            if not body.api_key.strip():
                raise HTTPException(422, '请选择已配置密钥或填写临时密钥。')
            if body.base_url and body.provider != 'custom':
                raise HTTPException(422, '自定义接口地址须选择自定义服务。')
            target = ai_probes.temporary(settings, body.provider, body.api_key.strip(), body.model.strip(), body.base_url)
        validation = replace(target, model=target.model or ('balance-only' if body.operation == 'balance' else ''))
        if validation.problem():
            raise HTTPException(422, '检测配置无效，请核对密钥格式、接口地址与模型名称。')
    except ValueError:
        raise HTTPException(503, 'AI 环境配置无效，请检查服务端配置。') from None
    target_id = ai_probes.identity(target)
    if body.key_id and body.balance_protocol == 'auto':
        protocol = selected_entry['balance_protocol']
    else:
        protocol = body.balance_protocol
    if not ai_probe_limiter.allow(actor=user['id'], target=target_id + ':' + body.operation):
        raise HTTPException(429, '检测过于频繁，请一分钟后重试。')
    result = ai_probes.run(target, body.operation, protocol)
    user = _user(session)
    _allow(user, 'platform_admin')
    # Audit sanitized outcomes only; temporary credentials/results are never persisted.
    if body.key_id and not any(ai_probes.identity(entry['settings']) == body.key_id
                               for entry in ai_keys.entries(settings, store)):
        raise HTTPException(409, '检测期间密钥已删除，请刷新列表。')
    store.log(user, 'ai_key_probe', 'ai_key_' + body.operation, target_id if body.key_id else 'temporary',
              json.dumps(result, ensure_ascii=False) if body.key_id else
              json.dumps({'operation': body.operation, 'status': result['status']}, ensure_ascii=False))
    return result


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
    remember: bool = False
    college: str | None = Field(default=None, max_length=200)


class UserBody(BaseModel):
    username: str
    password: str
    display_name: str
    role: str
    email: str = ""
    org_id: str | None = None


class PasswordResetBody(BaseModel):
    email: str = Field(max_length=254)


class PasswordResetConfirmBody(BaseModel):
    token: str = Field(max_length=512)
    password: str = Field(max_length=128)


class EmailStartBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=254)
    captcha_id: str = Field(max_length=64)
    captcha_answer: str = Field(max_length=16)
    purpose: Literal['register','login'] = 'register'
    invite_code: str = Field(default='',max_length=128)


class EmailMagicBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(max_length=128)


class EmailLoginBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)


class RegisterCompleteBody(BaseModel):
    model_config = {"extra": "forbid"}
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)
    invite_code: str = Field(max_length=128)
    password: str = Field(max_length=128)
    email_proof: str = Field(default='',max_length=128)


class InviteBody(BaseModel):
    """创始码捆绑机构名称和类别：一码一位、私聊交付，注册时验证邮箱。"""
    org_name: str = Field(min_length=1, max_length=120)
    org_type: Literal['enterprise', 'university']
    seats: int = Field(default=5, ge=1, le=200)
    # None = 未指定：高校落默认学生档，企业恒为 0（见 Store.create_invite_code）。
    student_seats: int | None = Field(default=None, ge=0, le=100000)
    bound_email: str = ""
    expires_days: int = Field(default=7, ge=1, le=30)


class ClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    taxpayer_id: str = Field(min_length=1, max_length=64)
    accountant_id: str | None = None


class AssignmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1,max_length=200)
    audit_id: str = Field(min_length=1,max_length=64)
    target_student_id: str | None = None
    weights: dict[str, classroom.Weight] = Field(default_factory=dict)
    false_positive_penalty: float = Field(default=5.0, ge=0, le=100,allow_inf_nan=False)
    published: bool = True
    class_id: str | None = Field(default=None,min_length=1,max_length=64)
    course_id: str | None = Field(default=None,min_length=1,max_length=200)
    deadline_at: AwareDatetime | None = None


class SubmissionBody(BaseModel):
    selected_rule_ids: list[str]


class SandboxFeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["mark_risk", "calculate"]
    rule_id: str | None = Field(default=None, max_length=32)
    evidence_metrics: list[Annotated[str, Field(max_length=160, strict=True)]] = Field(default_factory=list, max_length=100)
    left: str | None = Field(default=None, max_length=160)
    right: str | None = Field(default=None, max_length=160)
    operation: Literal["difference", "ratio"] = "difference"
    period_mode: Literal["same_period", "year_on_year"] = "same_period"


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


def _teaching_user(session: str | None) -> dict[str, Any]:
    from webapp.access import teaching_actor
    user = _user(session)
    with store.connect() as db:
        return teaching_actor(db, user)


def _allow(user: dict[str, Any], *roles: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(status_code=403, detail="当前角色无权执行此操作。")


def _audit_or_404(audit_id: str, user: dict[str, Any]) -> dict[str, Any]:
    entry = store.get_audit_for_user(audit_id, user)
    if not entry:
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
    from src.rule_conversion import effective_rules
    return effective_rules(store, RULES_DIR)


def _audit_rules(dataset: Dataset, enabled: set[str] | None = None) -> list[Any]:
    """Select one rule version covering the complete audited period.

    An interval crossing a version boundary needs a split-period audit; choosing
    a version by the end date would silently apply it to earlier transactions.
    """
    period = periods.parse_period(dataset.company.period, "审计所属期")
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
    # Missing reviewed editions limit the affected rule, not the entire batch.
    return [legal_periods.resolve(rule, dataset.company.period, strict=False) for rule in selected]


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
    from webapp.access import is_teaching_dataset
    return is_teaching_dataset(dataset)


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
        "material_reference": entry.get("material_reference"),
        "policy_assessment": dataset.policy_assessment,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control":"no-store"})


@app.get("/p/{code}")
def member_invitation_landing(code: str) -> FileResponse:
    # This route never authenticates or reveals an institution. Full-hash
    # validation happens only after email proof in the registration transaction.
    return FileResponse(STATIC_DIR / "join.html", media_type="text/html",
                        headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer",
                                 "X-Robots-Tag":"noindex, nofollow"})


@app.get("/logo")
def logo() -> Response:
    return FileResponse(LOGO_SVG, media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return FileResponse(LOGO_SVG, media_type="image/svg+xml")


@app.get("/auth-signup-ticket-v1.png")
def auth_signup_ticket() -> FileResponse:
    return FileResponse(STATIC_DIR / "auth-signup-ticket-v1.png", media_type="image/png")


# Explicit public asset allowlist; no arbitrary static path or directory access.
@app.get("/auth-glass.css")
@app.get("/auth-glass.js")
@app.get("/auth-ocean.mjs")
@app.get("/auth-wordmark.png")
@app.get("/auth-ocean-poster-desktop.webp")
@app.get("/auth-ocean-poster-mobile.webp")
@app.get("/vendor/three.module.mjs")
def auth_glass_asset(request: Request) -> FileResponse:
    assets = {
        "/auth-glass.css": ("auth-glass.css", "text/css"),
        "/auth-glass.js": ("auth-glass.js", "text/javascript"),
        "/auth-ocean.mjs": ("auth-ocean.mjs", "text/javascript"),
        "/auth-wordmark.png": ("auth-wordmark.png", "image/png"),
        "/auth-ocean-poster-desktop.webp": ("auth-ocean-poster-desktop.webp", "image/webp"),
        "/auth-ocean-poster-mobile.webp": ("auth-ocean-poster-mobile.webp", "image/webp"),
        "/vendor/three.module.mjs": ("vendor/three.module.mjs", "text/javascript"),
    }
    name, media_type = assets[request.url.path]
    return FileResponse(STATIC_DIR / name, media_type=media_type)


@app.get("/workspace.js")
def workspace_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "workspace.js", media_type="text/javascript")


@app.get("/mistake-book.js")
def mistake_book_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "mistake-book.js", media_type="text/javascript")


@app.get("/training-certificates")
def training_portal_page(request: Request) -> FileResponse:
    from webapp.university_members import teaching_page
    return teaching_page(store,request)


@app.get("/training-portal.js")
def training_portal_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "training-portal.js", media_type="text/javascript")


@app.get("/training-dark.css")
def training_dark_theme() -> FileResponse:
    return FileResponse(STATIC_DIR / "training-dark.css", media_type="text/css")


@app.get("/classroom.js")
def classroom_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "classroom.js", media_type="text/javascript")


@app.get("/training-ai-coach.js")
def ai_coach_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "training-ai-coach.js", media_type="text/javascript")


@app.get("/training-tasks.js")
def training_tasks_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "training-tasks.js", media_type="text/javascript")


@app.get("/training-submission-ui.js")
def training_submission_ui_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "training-submission-ui.js", media_type="text/javascript")


@app.get("/teaching-case-ui.js")
def teaching_case_ui_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "teaching-case-ui.js", media_type="text/javascript")


@app.get("/exam-ui.js")
def exam_ui_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "exam-ui.js", media_type="text/javascript")


@app.get("/training-reports.js")
def training_reports_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "training-reports.js", media_type="text/javascript")


@app.get("/console.js")
def console_script():
    return FileResponse(STATIC_DIR / "console.js", media_type="text/javascript")


@app.get('/ai-keys.js')
def ai_keys_script():
    return FileResponse(STATIC_DIR / 'ai-keys.js', media_type='text/javascript')


@app.get("/enterprise.js")
def enterprise_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "enterprise.js", media_type="text/javascript")


@app.get("/workspace.css")
def workspace_styles() -> FileResponse:
    return FileResponse(STATIC_DIR / "workspace.css", media_type="text/css")


@app.get("/api/dashboard")
def dashboard(session: str | None = Cookie(default=None, alias=COOKIE_NAME),
              company: str | None = Query(default=None, max_length=200),
              page: int = Query(default=1, ge=1), page_size: int = Query(default=24, ge=1, le=100)):
    from webapp.dashboard import collect
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    return collect(store, user, company, page, page_size)


from webapp.org_reports import register as register_org_reports
register_org_reports(app, lambda: store, _user, _allow, COOKIE_NAME)

from webapp.report_verification import register as register_report_verification
register_report_verification(app, lambda: store, _user, _allow, _audit_or_404, COOKIE_NAME)


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
def status(request: Request) -> dict[str, Any]:
    from webapp.university_members import resolve_browser_identity
    user, conflict = resolve_browser_identity(store, request)
    return {"needs_setup": not store.has_users(), "user": user, "session_conflict": conflict}


class GraphQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    node_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    audit_id: str | None = Field(default=None, min_length=1, max_length=200)
    taxpayer_id: str | None = Field(default=None, min_length=1, max_length=200)


class PublicQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=2000)
    previous_questions: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def valid_text(self):
        for value in [getattr(self, 'node_id', ''), self.question, *self.previous_questions]:
            if any((ord(c) < 32 and c not in '\n\r\t') or 0xD800 <= ord(c) <= 0xDFFF for c in value):
                raise ValueError('输入包含无效控制字符。')
        return self


class NodeQuestion(PublicQuestion):
    node_id: str = Field(min_length=1, max_length=200)


class RAGQuestion(PublicQuestion):
    as_of: date | None = None
    region: str = Field(default='', max_length=30, pattern=r'^(?:CN(?:-[A-Za-z0-9]+)*)?$')

    @model_validator(mode='after')
    def valid_region(self):
        from webapp.public_rag import normalize_region
        if normalize_region(self.region) is None:
            raise ValueError('检索地区无效。')
        return self


node_question_limiter = RateLimiter({'actor': (12, 60)})
_node_question_active = set()


async def public_node_answer(body, session, *, stream=True, identity_reader=None, rag=False):
    """One shared harness for public and university node questions, including old JSON callers."""
    import asyncio
    from contextlib import aclosing
    from webapp import node_qa, public_rag
    from src.settings import AISettings
    reader = identity_reader or (lambda: _user(session))
    user = await run_in_threadpool(reader)
    # This application runs one process. No enterprise scope can enter this endpoint.
    if user['id'] in _node_question_active or len(_node_question_active) >= 8:
        raise HTTPException(429, '已有问答正在生成，请停止或等待完成后再试。')
    if not node_question_limiter.allow(actor=user['id']):
        raise HTTPException(429, '提问过于频繁，请稍后再试。')
    _node_question_active.add(user['id'])
    try:
        def prepare():
            from webapp.knowledge import public_graph
            settings = ai_keys.apply(AISettings.from_env(), usage='knowledge')
            if settings.problem() and not rag:
                raise HTTPException(503, 'AI 尚未连接，请检查模型配置。')
            with store.connect() as db:
                graph = public_graph(db, _effective_rules(), teaching=user.get('org_type') == 'university') if not rag else None
                info = ai_keys.capability(db, settings)
            limits = ai_probes.context_capability(info)
            capacity = limits.get('context_window', 32768)
            output = min(settings.max_tokens, limits.get('max_output_tokens', settings.max_tokens))
            if rag:
                settings, output = public_rag.generation_plan(settings, limits)
            budget = min(48000, capacity * 3 // 5, capacity - output - 4096 - max(4096, capacity // 20))
            if budget < 1024:
                raise HTTPException(413, '模型可用上下文不足，请调整模型或输出额度。')
            if rag:
                with store.connect() as db:
                    payload, manifest = public_rag.retrieve(db, body.question, body.previous_questions,
                                                           region=body.region, as_of=body.as_of, budget=budget)
                if payload['nodes'] and settings.problem():
                    raise HTTPException(503, 'AI 尚未连接，请检查模型配置；检索已找到原文。')
            else:
                payload, manifest = node_qa.build_context(graph, body.node_id, body.question,
                                                        body.previous_questions, budget=budget)
            manifest['capacity_source'] = 'provider_models' if limits.get('context_window') else 'conservative_fallback'
            manifest['output_reserve'] = output
            return settings, payload, manifest, output
        settings, payload, manifest, output = await run_in_threadpool(prepare)
    except BaseException:
        _node_question_active.discard(user['id'])
        raise

    async def events():
        try:
            # Re-check authentication after context assembly and before the paid request.
            await run_in_threadpool(reader)
            handler = public_rag.answer_events if rag else node_qa.answer_events
            async with aclosing(handler(settings, payload, manifest, max_tokens=output)) as source:
                async for event in source:
                    # Never release deltas after a session has been revoked.
                    current = await run_in_threadpool(reader)
                    if current['id'] != user['id']:
                        raise HTTPException(401, '登录身份已变化。')
                    yield event
        except HTTPException:
            yield {'type': 'error', 'message': '访问权限已变化，请重新登录后提问。', 'complete': False}
        finally:
            _node_question_active.discard(user['id'])

    if not stream:
        async with aclosing(events()) as source:
            async for event in source:
                if event['type'] == 'done':
                    return event
                if event['type'] == 'error':
                    raise HTTPException(502, event['message'])
        raise HTTPException(502, '回答未完成。')

    async def encoded():
        iterator = events()
        pending = None
        try:
            # Heartbeats keep a quiet upstream cancellable and avoid buffering proxies.
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                ready, _ = await asyncio.wait({pending}, timeout=5)
                if not ready:
                    yield ': keepalive\n\n'
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    break
                pending = None
                yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await iterator.aclose()
            _node_question_active.discard(user['id'])
    return StreamingResponse(encoded(), media_type='text/event-stream',
                             headers={'Cache-Control': 'private, no-store', 'X-Accel-Buffering': 'no'})


@app.post('/api/knowledge/node-ask')
async def knowledge_node_ask(body: NodeQuestion, request: Request,
                             session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    def reader():
        from webapp.university_members import session_identity
        user = session_identity(store, request)
        if not user:
            raise HTTPException(401, '请先登录。')
        return user
    return await public_node_answer(body, session, identity_reader=reader)


@app.post('/api/knowledge/rag-ask')
async def knowledge_rag_ask(body: RAGQuestion, request: Request):
    def reader():
        from webapp.university_members import session_identity
        user = session_identity(store, request)
        if not user:
            raise HTTPException(401, '请先登录。')
        return user
    return await public_node_answer(body, None, identity_reader=reader, rag=True)


def _graph_for_user(user, audit_id, taxpayer_id=None):
    if taxpayer_id is not None:
        _allow(user, "org_admin", "accountant", "teacher")
        from webapp.enterprise_graph import read_enterprise_history, build_enterprise_graph
        company, entries = read_enterprise_history(store, user, taxpayer_id, audit_id)
        return build_enterprise_graph(company, entries, user['org_id'], audit_id)
    entry = None
    if audit_id:
        _allow(user, "org_admin", "accountant", "teacher")
        entry = _audit_or_404(audit_id, user)
    if entry is not None:
        return {**build_graph([], entry), "schema_version": 1, "view": "frozen_audit_evidence"}
    with store.connect() as db:
        from webapp.knowledge import public_graph
        return public_graph(db, _effective_rules())


class PolicyBundleBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bundle: dict[str, list[dict[str, Any]]]


class PolicyPreviewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    facts: dict[str, dict[str, Any]]
    start: date
    end: date
    region: str = Field(min_length=1, max_length=100)
    entities: list[str] = Field(default_factory=list, max_length=100)


class PolicySourceReviewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    issuer: str = Field(min_length=1, max_length=300)
    validity_status: Literal['in_force', 'partially_modified', 'repealed', 'expired']
    effective_from: date
    effective_to: date | None = None
    clause_ids: list[str] = Field(min_length=1, max_length=200)
    end_kind: Literal['unknown', 'fixed', 'unfixed'] = 'unknown'
    coverage_start: date | None = None
    coverage_end: date | None = None
    evidence: dict[str, str] = Field(default_factory=dict, max_length=10)


@app.get("/api/knowledge/schema")
def knowledge_schema(session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    _user(session)
    with store.connect() as db:
        return {"version": 1, "types": {table: [dict(r) for r in db.execute(f'PRAGMA table_info("{table}")')]
                                        for table in policy_graph.IDS},
                "relations": {name: sorted(pairs) for name, pairs in policy_graph.RELATIONS.items()},
                "truth_values": ["TRUE", "FALSE", "UNKNOWN"]}


@app.post("/api/knowledge/candidates")
def knowledge_candidates(body: PolicyBundleBody,
                         session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "platform_admin")
    try:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"platform_admin"})
            result = policy_graph.import_bundle(db, body.bundle)
            store._log(db, user, "import_policy_candidates", "knowledge", "v1", json.dumps(result))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"imported": result, "review_status": "candidate"}


@app.post("/api/knowledge/rules/{rule_id}/approve")
def knowledge_approve(rule_id: str,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "platform_admin")
    try:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"platform_admin"})
            result = policy_graph.approve_rule(db, rule_id, user["id"])
            store._log(db, user, "approve_policy_rule", "knowledge", rule_id, result["version"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return result


@app.post("/api/knowledge/policies/{policy_id}/verify")
def knowledge_verify_source(policy_id: str, body: PolicySourceReviewBody,
                            session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "platform_admin")
    try:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"platform_admin"})
            result = policy_graph.verify_source(db, policy_id, issuer=body.issuer, validity_status=body.validity_status,
                                                effective_from=body.effective_from.isoformat(),
                                                effective_to=body.effective_to.isoformat() if body.effective_to else None,
                                                clause_ids=body.clause_ids, reviewer=user['id'], end_kind=body.end_kind,
                                                coverage_start=body.coverage_start.isoformat() if body.coverage_start else None,
                                                coverage_end=body.coverage_end.isoformat() if body.coverage_end else None,
                                                evidence=body.evidence)
            store._log(db, user, "verify_policy_source", "knowledge", policy_id, json.dumps(result, ensure_ascii=False))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return result


@app.post("/api/knowledge/clauses/{clause_id}/extract")
def knowledge_extract(clause_id: str,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "platform_admin")
    try:
        with store.connect() as db:
            current_actor(db, user, {"platform_admin"})
            clause = policy_graph.get(db, "Clause", clause_id)
            policy = policy_graph.get(db, "Policy", clause["policy_id"])
            fields = {table: [r["name"] for r in db.execute(f'PRAGMA table_info("{table}")')]
                      for table in policy_graph.IDS}
        bundle, model = extract_policy_candidates(policy, clause, fields)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"platform_admin"})
            result = policy_graph.import_bundle(db, bundle) if any(bundle.values()) else {}
            store._log(db, user, "extract_policy_candidates", "knowledge", clause_id, model)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"imported": result, "review_status": "candidate", "model": model}


@app.post("/api/knowledge/rules/{rule_id}/preview")
def knowledge_preview(rule_id: str, body: PolicyPreviewBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "platform_admin")
    try:
        with store.connect() as db:
            result = policy_graph.evaluate_rule(db, rule_id, body.facts, start=body.start.isoformat(),
                                               end=body.end.isoformat(), region=body.region, entities=body.entities)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {**result, "mode": "manual_preview", "formal_finding": False}


@app.get("/api/knowledge/enterprises")
def knowledge_enterprises(session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    from webapp.enterprise_graph import read_enterprise_history
    return read_enterprise_history(store, user)


@app.get("/api/knowledge/graph")
def knowledge_graph(audit_id: str | None = None, taxpayer_id: str | None = None,
                    session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    graph = _graph_for_user(user, audit_id, taxpayer_id)
    base, model, _ = ai_config()
    return {**graph, "ai": {"configured": bool(base and model), "model": model}}


@app.post("/api/knowledge/ask")
async def knowledge_ask(body: GraphQuestion,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    if body.audit_id is None and body.taxpayer_id is None:
        try:
            question = NodeQuestion(node_id=body.node_id, question=body.question)
        except ValueError:
            raise HTTPException(422, '问题包含无效字符。') from None
        return await public_node_answer(question, session, stream=False)
    return await run_in_threadpool(_enterprise_knowledge_ask, body, session)


def _enterprise_knowledge_ask(body, session):
    user = _user(session)
    graph = _graph_for_user(user, body.audit_id, body.taxpayer_id)
    result = ask_graph(graph, body.node_id, body.question)
    if body.taxpayer_id is not None:
        if _graph_for_user(user, body.audit_id, body.taxpayer_id) != graph:
            raise HTTPException(status_code=409, detail="企业历史或访问权限已变化，请重新载入图谱后提问。")
    elif body.audit_id:
        _audit_or_404(body.audit_id, user)
    return result


@app.post("/api/audits/{audit_id}/findings/{rule_id}/interpretation")
def finding_interpretation(audit_id: str, rule_id: str,
                           session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(audit_id, user)
    finding = next((item for item in entry["findings"] if item.rule.id == rule_id), None)
    if not finding:
        raise HTTPException(status_code=404, detail="该审计中不存在指定 Finding。")
    if finding.status != "hit":
        raise HTTPException(status_code=422, detail="仅可对规则引擎已命中的 Finding 生成白话解读。")
    evidence_hash = finding_evidence_hash(finding)
    cached = store.get_finding_interpretation(audit_id, rule_id, evidence_hash)
    if cached:
        _audit_or_404(audit_id, user)
        store.log(user, "interpret_finding", "audit", audit_id,
                  f"rule={rule_id};cache=hit;model={cached['model']}")
        return cached
    result = interpret_finding(finding)
    _audit_or_404(audit_id, user)
    if result["evidence_hash"] != evidence_hash:
        raise HTTPException(status_code=502, detail="模型解读与当前审计证据版本不一致。")
    saved = store.save_finding_interpretation(
        audit_id, rule_id, evidence_hash, result, user,
    )
    store.log(user, "interpret_finding", "audit", audit_id,
              f"rule={rule_id};cache=miss;model={result['model']}")
    return {**saved, "cached": False}


@app.post("/api/audits/{audit_id}/narrative")
def audit_narrative(audit_id: str,
                    session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(audit_id, user)
    findings = entry["findings"]
    evidence_hash = audit_narrative_hash(findings)
    cached = store.get_audit_narrative(audit_id, evidence_hash)
    if cached:
        _audit_or_404(audit_id, user)
        store.log(user, "generate_audit_narrative", "audit", audit_id,
                  f"cache=hit;model={cached['model']}")
        return cached
    result = generate_audit_narrative(findings)
    _audit_or_404(audit_id, user)
    if result["evidence_hash"] != evidence_hash:
        raise HTTPException(status_code=502, detail="模型总体结论与当前审计证据版本不一致。")
    saved = store.save_audit_narrative(audit_id, evidence_hash, result, user)
    store.log(user, "generate_audit_narrative", "audit", audit_id,
              f"cache=miss;model={result['model']}")
    return {**saved, "cached": False}


@app.get("/graph.js")
def graph_script():
    return FileResponse(STATIC_DIR / "graph.js", media_type="text/javascript")


@app.get("/policy-source.js")
def policy_source_script():
    return FileResponse(STATIC_DIR / "policy-source.js", media_type="text/javascript")


@app.post("/api/setup")
def setup(body: SetupBody) -> dict[str, Any]:
    try:
        store.create_initial_admin(body.username, body.password, body.display_name.strip() or body.username,
                                   body.org_id, body.email)
    except SetupAlreadyInitialized:
        raise HTTPException(status_code=409, detail="系统已初始化。") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc):
            raise HTTPException(status_code=409, detail="该邮箱已被占用，请换用其他邮箱。") from None
        raise HTTPException(status_code=409, detail="账号与现有数据冲突。") from None
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request) -> Response:
    from webapp.university_members import authenticate_legacy,attach_platform_session,attach_session,user_context
    ip = request.client.host if request.client else "unknown"
    with login_guard.reserve(body.username, ip) as wait:
        if wait:
            return JSONResponse(status_code=429, content={"detail": "登录尝试过于频繁，请稍后重试。"},
                                headers={"Retry-After": str(wait)})
        result = store.authenticate(body.username, body.password, remember=body.remember)
        context=user_context(store,result[0]) if result else None
        if result and not context:
            store.logout(result[1])
            result=None
        try:
            legacy=authenticate_legacy(store,body.username,body.password,body.remember,college=body.college) if not result else None
        except ValueError as exc:
            login_guard.record_failure(body.username,ip)
            return JSONResponse(status_code=409, content={"detail":str(exc), "requires_college":True})
        if not result and not legacy:
            count = login_guard.record_failure(body.username, ip)
            store.log(None, "login_failed", "account_hash", login_guard.fingerprint(body.username.strip().lower()),
                      f"ip_hash={login_guard.fingerprint(ip)};failure_count={count}")
            return _err(401, "邮箱、用户名或密码错误。")
        user, token = (context,result[1]) if result else (legacy['user'],legacy['token'])
        login_guard.record_success(body.username)
    store.log(user if result else None, "login", "session", user["id"])
    response = JSONResponse({"user": user})
    cookie=COOKIE_NAME
    if result:
        attach_session(store,response,user,remember=body.remember)
    else:
        attach_platform_session(store,response,legacy['profile']['id'],staff=legacy['staff'],remember=body.remember)
        cookie='taxpearls_staff_session' if legacy['staff'] else 'taxpearls_training_session'
    response.set_cookie(
        cookie, token, max_age=session_max_age(body.remember), httponly=True,
        samesite="strict", secure=os.environ.get("TAXPEARLS_COOKIE_SECURE") == "1",
    )
    return response


@app.post("/api/logout")
def logout(request: Request, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = store.user_for_token(session)
    if user:
        store.log(user, "logout", "session", user["id"])
    response = JSONResponse({"ok": True})
    from webapp.university_members import logout_browser
    logout_browser(store,request,response)
    return response


def _email_public_base(request: Request) -> str:
    """Do not send secrets to an attacker-controlled Host header."""
    configured = os.getenv('TAXPEARLS_PUBLIC_BASE_URL','').strip().rstrip('/')
    value = configured or str(request.base_url).rstrip('/')
    try:
        parsed = urlsplit(value)
        local = parsed.hostname in {'localhost','127.0.0.1','::1'}
        valid = (parsed.scheme in {'http','https'} and bool(parsed.hostname)
                 and not parsed.username and not parsed.password and parsed.path in {'','/'}
                 and not parsed.query and not parsed.fragment and '\\' not in value
                 and not any(ord(ch)<=32 for ch in value) and (parsed.port is None or 1<=parsed.port<=65535)
                 and (parsed.scheme=='https' or local) and (bool(configured) or local))
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(503,'请配置可信的 TAXPEARLS_PUBLIC_BASE_URL（生产环境须 HTTPS）。')
    return value


def _email_browser_response(content, browser):
    response = JSONResponse(content)
    response.set_cookie(EMAIL_COOKIE_NAME,browser,max_age=30*60,httponly=True,samesite='strict',
                        secure=os.environ.get('TAXPEARLS_COOKIE_SECURE')=='1')
    return response


def _email_login_response(user, token):
    from webapp.university_members import attach_session,user_context
    user=user_context(store,user)
    if not user:
        store.logout(token)
        return _err(401,'账号未关联有效身份，请联系管理员核对。')
    response=JSONResponse({'user':user,'purpose':'login'})
    attach_session(store,response,user)
    response.set_cookie(COOKIE_NAME,token,max_age=12*3600,httponly=True,samesite='strict',
                        secure=os.environ.get('TAXPEARLS_COOKIE_SECURE')=='1')
    return response


@app.post("/api/auth/password/reset")
def password_reset(body: PasswordResetBody, request: Request) -> Response:
    """申请重置邮件。防枚举：无论邮箱是否存在，成功响应完全一致。"""
    ip = request.client.host if request.client else "unknown"
    if not reset_limiter.allow(email=body.email, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    base = _email_public_base(request)
    browser = email_auth.browser_secret(request.cookies.get(EMAIL_COOKIE_NAME))
    try:
        delivery = email_auth.issue(store,body.email,'reset',browser)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    if delivery:
        try:
            send_password_reset_email(to=delivery['email'], reset_url=f"{base}/#email={delivery['token']}")
        except MailError:
            email_auth.revoke_delivery(store,delivery['token'])
            store.log(None, "password_reset_failed", "email_hash",
                      login_guard.fingerprint(delivery['email']), 'delivery_error')
        else:
            store.log(None, "password_reset_sent", "email_hash",
                      login_guard.fingerprint(delivery['email']),f"ip_hash={login_guard.fingerprint(ip)}")
    return _email_browser_response({"message": "若该邮箱可用，重置邮件将发送至该邮箱。请在发起请求的浏览器打开，10 分钟内有效；未收到可稍后重新申请。"},browser)


@app.post("/api/auth/password/reset/confirm")
def password_reset_confirm(body: PasswordResetConfirmBody, request: Request) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    if not reset_confirm_limiter.allow(token=body.token, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    try:
        email_auth.reset_password(store,body.token,body.password,request.cookies.get(EMAIL_COOKIE_NAME,''))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"message": "密码已重置，请使用新密码登录。"}


@app.get("/api/auth/captcha")
def auth_captcha(request: Request) -> dict[str, str]:
    """签发图片人机验证码（PNG data URL）。"""
    ip = request.client.host if request.client else "unknown"
    if not captcha_limiter.allow(ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    try:
        return captcha.issue()
    except captcha.CaptchaCapacityError:
        return JSONResponse(status_code=503, content={"detail": "验证码容量暂满，请稍后重试。"},
                            headers={"Retry-After": "60"})


@app.post("/api/auth/email/start")
def email_start(body: EmailStartBody, request: Request) -> dict[str, Any]:
    """验证码/魔术链接共用一份绑定请求，注册或邮箱登录均需图片挑战。"""
    ip = request.client.host if request.client else "unknown"
    if not captcha.verify(body.captcha_id, body.captcha_answer):
        return JSONResponse(status_code=422, content={"detail": "人机验证不正确，请重试。"})
    email = body.email.strip().lower()
    if not register_code_limiter.allow(email=email, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "发送过于频繁，请 15 分钟后再试。"})
    base = _email_public_base(request)
    browser = email_auth.browser_secret(request.cookies.get(EMAIL_COOKIE_NAME))
    try:
        delivery=email_auth.issue(store,email,body.purpose,browser,invite_code=body.invite_code)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    if not delivery and body.purpose=='register':
        return _email_browser_response({'message':'该邮箱已注册，请直接登录或申请找回密码。','exists':True},browser)
    if delivery:
        try:
            sender=send_registration_code_email if body.purpose=='register' else send_login_code_email
            sender(to=email,code=delivery['code'],signup_url=f"{base}/#email={delivery['token']}")
        except MailError:
            email_auth.revoke_delivery(store,delivery['token'])
            store.log(None,'email_delivery_failed','email_hash',login_guard.fingerprint(email),'delivery_error')
            if body.purpose=='register':
                raise HTTPException(502,'验证邮件发送失败，请稍后重试。') from None
        else:
            store.log(None,'email_verification_sent','email_hash',login_guard.fingerprint(email),
                      f'purpose={body.purpose};ip_hash={login_guard.fingerprint(ip)}')
    message=('若该邮箱可用，验证邮件将发送至该邮箱。' if body.purpose=='login' else '验证邮件已发送。')
    return _email_browser_response({'message':message+'请在发起请求的浏览器输入验证码或打开邮件链接；10 分钟内有效，输错 5 次作废。','exists':False},browser)


@app.post('/api/auth/email/verify')
def email_verify(body: EmailMagicBody, request: Request) -> Response:
    ip=request.client.host if request.client else 'unknown'
    if not email_verify_limiter.allow(token=body.token,ip=ip):
        return JSONResponse(status_code=429,content={'detail':'请求过于频繁，请稍后再试。'})
    try:
        result=email_auth.redeem_magic(store,body.token,request.cookies.get(EMAIL_COOKIE_NAME,''))
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    if result['purpose']=='login':
        return _email_login_response(result['user'],result['session'])
    return JSONResponse(result)


@app.post('/api/auth/email/login')
def email_login(body: EmailLoginBody, request: Request) -> Response:
    ip=request.client.host if request.client else 'unknown'
    if not email_login_limiter.allow(email=body.email,ip=ip):
        return JSONResponse(status_code=429,content={'detail':'请求过于频繁，请稍后再试。'})
    try:
        user,token=email_auth.login_with_code(store,body.email,body.code,request.cookies.get(EMAIL_COOKIE_NAME,''))
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    return _email_login_response(user,token)


@app.post("/api/register/complete")
def register_complete(body: RegisterCompleteBody, request: Request) -> Response:
    """邮箱验证与完整邀请凭证同事务；角色/机构来自凭证，不接受客户端指定。"""
    ip = request.client.host if request.client else "unknown"
    if not register_complete_limiter.allow(email=body.email, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    try:
        user, token = store.register_with_code(body.email, body.code, body.invite_code, body.password,
                                             browser_session=request.cookies.get(EMAIL_COOKIE_NAME,''),email_proof=body.email_proof)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc):
            raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录。") from None
        raise HTTPException(status_code=409, detail="账号与现有数据冲突。") from None
    response = JSONResponse({"user": user, "org_id": user["org_id"]})
    from webapp.university_members import attach_session
    attach_session(store,response,user)
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
                                          body.bound_email, body.expires_days, org_type=body.org_type,student_seats=body.student_seats)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return invite


@app.get("/api/invites")
def list_invites(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    actor = _user(session)
    _allow(actor, "platform_admin")
    return store.list_invite_codes(actor=actor)


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    from webapp.university_members import session_identity
    user=session_identity(store,request)
    if not user:
        raise HTTPException(401,'请先登录。')
    return user


@app.get("/api/notifications/preferences")
def notification_preferences(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
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
    return saved


@app.put("/api/notifications/preferences")
def update_notification_preferences(body: NotificationPreferencesBody,
        session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    return _save_notification_preferences(user, user["id"], body)


@app.get("/api/notifications/recipients")
def notification_recipients(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    return store.list_notification_recipients(user)


@app.put("/api/notifications/recipients/{user_id}")
def update_notification_recipient(user_id: str, body: NotificationPreferencesBody,
        session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    return _save_notification_preferences(user, user_id, body)


@app.get("/api/notifications")
def notifications(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    return store.list_notifications(user)


@app.put("/api/notifications/{notification_id}/read")
def read_notification(notification_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    if not store.mark_notification_read(user, notification_id):
        raise HTTPException(404, "通知不存在或无权查看。")
    return {"id": notification_id, "read": True}


@app.post("/api/notifications/{notification_id}/retry")
def retry_notification(notification_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME),
                       channel: str = Query(default='email',max_length=32)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    try:
        authorized = store.retry_notification_delivery(user, notification_id,channel=channel)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not authorized:
        raise HTTPException(404, "通知不存在或无权访问。")
    return {"id": notification_id,"channel":channel,"status":"pending",**({'email_status':'pending'} if channel=='email' else {})}


@app.get("/api/users")
def users(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    return store.list_users(None if user["role"] == "platform_admin" else user["org_id"], actor=user)


@app.post("/api/users")
def create_user(body: UserBody, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    actor = _user(session)
    _allow(actor, "org_admin")
    org_id = body.org_id or actor["org_id"]
    if actor["role"] == "org_admin" and (body.role != "accountant" or org_id != actor["org_id"]):
        raise HTTPException(status_code=403, detail="机构管理员只能创建本机构会计账号。")
    try:
        created = store.create_user(body.username, body.password, body.display_name, body.role, org_id, body.email, actor=actor)
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
    return created


@app.get("/api/clients")
def clients(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "org_admin", "accountant")
    return store.list_clients(user)


@app.post("/api/clients")
def create_client(body: ClientBody, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin")
    name = body.name.strip()
    taxpayer_id = identity(body.taxpayer_id)
    if not name or not taxpayer_id:
        raise HTTPException(status_code=422, detail="企业名称和纳税人识别号不能为空。")
    if re.search(r"[^0-9A-Z]", taxpayer_id):
        raise HTTPException(status_code=422, detail="纳税人识别号只能包含数字和大写字母，请核对后重新填写。")
    if body.accountant_id:
        accountant = store.get_user(body.accountant_id)
        if not accountant or accountant["role"] != "accountant" or accountant["org_id"] != user["org_id"]:
            raise HTTPException(status_code=422, detail="负责人必须是本机构会计。")
    client = store.upsert_client(user, name, taxpayer_id, body.accountant_id)
    return client


@app.post("/api/audit")
async def audit(request: Request, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = _user(session)
    # Legacy direct import is teaching-only. Business uploads must retain
    # originals and explicitly confirm a server-side analysis revision.
    _allow(user, "teacher")
    async with multipart(request, material_scope=user['org_id']) as form:
        file = single_file(form)
        if not file.filename or not file.filename.lower().endswith(".xlsx"):
            return _err(422, "仅支持 .xlsx 格式的审计材料。")
        client_id = form.get("client_id") or None
        if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 64):
            return _err(422, "客户档案编号格式错误。")
        data = await file.read()
    try:
        from src import material_format_guard
        await run_in_threadpool(material_format_guard.check, [(file.filename, data)])
        dataset = await run_in_threadpool(loader.load_bytes, data)
        dataset.simulation = True  # This authenticated endpoint is teaching-only.
    except loader.InputError as exc:
        return _err(422, f"审计材料不符合模板要求：{exc}")
    return JSONResponse(await run_in_threadpool(_save_audit, dataset, user, client_id))


def _save_audit(dataset: Dataset, user: dict[str, Any], client_id: str | None = None,
                *, frozen_rules: list[Rule] | None = None, exercise_metadata: dict | None = None,
                material_context: dict | None = None, frozen_graph_rule: Rule | None = None) -> dict[str, Any]:
    if user["role"] == "teacher" and not _is_synthetic_dataset(dataset):
        raise HTTPException(403, "教师只能导入明确标记为仿真样例的教学数据，严禁使用真实企业账套。")
    if user['role'] == 'teacher' and client_id is not None:
        raise HTTPException(403, "教师备课案例不能关联客户档案。")
    if client_id:
        client = store.get_client(client_id)
        if not client or client["org_id"] != user["org_id"]:
            raise HTTPException(422, "客户档案不存在或不属于当前机构。")
        if user["role"] == "accountant" and client.get("accountant_id") != user["id"]:
            raise HTTPException(403, "会计只能审计自己负责的客户。")
        if client["taxpayer_id"] != dataset.company.taxpayer_id:
            raise HTTPException(422, "审计材料中的纳税人识别号与所选客户档案不一致。")
    try:
        from src import related_graph
        if material_context is not None and frozen_graph_rule != related_graph.definition():
            raise AccessDenied('关联方检查范围或版本已变化，请重新分析并确认材料。', 409)
        enabled = store.enabled_rule_ids()
        rules = _audit_rules(dataset, enabled) if frozen_rules is None else frozen_rules
        findings = engine.run(rules, dataset)
        # FR-B09 is a separate relationship traversal, not a YAML condition.
        findings.extend(related_graph.run(dataset, include_unavailable=material_context is not None))
        findings.sort(key=lambda f: ({"hit": 0, "pass": 1, "skipped": 2}[f.status], -f.severity_rank, f.rule.id))
    except engine.RuleError as exc:
        raise HTTPException(500, f"规则执行失败：{exc}")
    audit_id = uuid.uuid4().hex
    when = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    vm = render.build_view_model(dataset, findings)
    from webapp.report_archive import build_snapshot
    material_reference = ({'batch_id': material_context['batch_id'], 'analysis_revision': material_context['revision'],
                           'confirmation_revision': material_context['revision'] + 1} if material_context else None)
    snapshot = build_snapshot({"id": audit_id, "org_id": user["org_id"], "audited_at": when,
                               "dataset": dataset, "findings": findings, 'material_reference': material_reference},
                              _org_branding(user["org_id"]))
    store.save_audit(audit_id, user, client_id, dataset, findings, vm["summary"], when,
                     report_snapshot=snapshot, exercise_metadata=exercise_metadata, create_client=True,
                     material_context=material_context)
    entry = store.get_audit(audit_id)
    assert entry is not None
    return _result(entry)


from webapp.material_upload import register as register_material_upload
register_material_upload(app, _user, _allow, _save_audit, RULES_DIR, _audit_or_404)

from webapp.enterprise_materials import register as register_enterprise_materials
from webapp.core_inputs import register as register_core_inputs
register_core_inputs(app, lambda: store, _user, _allow, STATIC_DIR)
from webapp.knowledge_evidence import register as register_knowledge_evidence
register_knowledge_evidence(app, lambda: store, _user, _allow)
from webapp.retrieval_checks import register as register_retrieval_checks
register_retrieval_checks(app, lambda: store, _user, _allow)
from webapp.core_reports import register as register_core_reports
register_core_reports(app, lambda: store, _user, _allow)
register_enterprise_materials(app, lambda: store, _user, _allow, RULES_DIR,
                             lambda dataset: _audit_rules(dataset, store.enabled_rule_ids()),
                             _save_audit, lambda audit_id, user: _result(_audit_or_404(audit_id, user)))

from webapp.exercises import register as register_exercises
register_exercises(app, lambda: store, _teaching_user, _allow, _audit_or_404, lambda data, enabled: _audit_rules(data,enabled),
                   _save_audit, _trial_payload, COOKIE_NAME)
classroom.register(app,lambda: store,_teaching_user,_allow,_is_synthetic_dataset,COOKIE_NAME)
from webapp import ai_coach as ai_coach_routes
ai_coach_routes.register(app, lambda: store, _teaching_user, COOKIE_NAME)
from webapp import training_tasks as training_task_routes
training_task_routes.register(app, lambda: store, _teaching_user, COOKIE_NAME)
from webapp import training_submissions as training_submission_routes
training_submission_routes.register(app, lambda: store, _teaching_user, COOKIE_NAME)
from webapp import teaching_case_tasks as case_task_routes
case_task_routes.register(app, lambda: store, _teaching_user, COOKIE_NAME)
from webapp import teaching_rubric as rubric_routes
rubric_routes.register(app, lambda: store, _teaching_user, COOKIE_NAME)
from webapp.mistake_book import register as register_mistake_book
register_mistake_book(app,lambda: store,_user,_allow,COOKIE_NAME)
members.register(app,lambda: store,_user,COOKIE_NAME)

from webapp import rule_bindings as rule_binding_routes
rule_binding_routes.register(app,lambda:store,_user,_allow,_effective_rules)
from webapp.training_portal import register as register_training_portal
register_training_portal(app, lambda: store, "taxpearls_training_session")
from webapp import university_members
university_members.register(app,lambda: store,_user)
from webapp import university_workspace
university_workspace.register(app, lambda: store, _effective_rules, ai_config)
from webapp.training_content import register as register_training_content
register_training_content(app, lambda: store)
from webapp.training_graph import register as register_training_graph
register_training_graph(app, lambda: store)
from webapp.training_tutor import register as register_training_tutor
register_training_tutor(app, lambda: store, "taxpearls_training_session")
from webapp.training_source_auth import register as register_training_source_auth
register_training_source_auth(app, lambda: store)
from webapp.training_outline import register as register_training_outline
register_training_outline(app, lambda: store)
from webapp.training_trial import register as register_training_trial
register_training_trial(app, lambda: store, "taxpearls_training_session")
from webapp import exams as exam_routes
exam_routes.register(app, lambda: store)
from webapp.teaching_units import register as register_teaching_units
register_teaching_units(app, lambda: store)


@app.get("/api/audits")
def audits(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    return store.list_audits(user)


@app.get("/api/audits/{audit_id}")
def audit_detail(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(audit_id, user)
    store.log(user, "view_audit", "audit", audit_id)
    return _result(entry)


@app.get("/api/audits/{audit_id}/changes")
def audit_changes(audit_id: str, baseline_id: str | None = None,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    from src import risk_changes

    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    current = _audit_or_404(audit_id, user)
    candidates = risk_changes.baseline_candidates(store.audit_history_for_comparison(current, user), current, latest_only=False)
    choices = [{key: row[key] for key in ("id", "period", "audited_at")} for row in candidates]
    if baseline_id:
        # Check access before identity/period validation; do not reveal foreign IDs.
        baseline = _audit_or_404(baseline_id, user)
    elif candidates:
        baseline = _audit_or_404(candidates[0]["id"], user)
    else:
        return {"status": "no_baseline", "current": {key: current[key] for key in ("id", "period", "audited_at")},
                "baselines": [], "items": [], "counts": {},
                "message": "无可比基期：须同机构/客户/税号且有更早、同粒度的明确期间。首期不推断新增风险。"}
    try:
        result = risk_changes.compare(baseline, current)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    store.log(user, "view_risk_changes", "audit", audit_id, f"baseline={baseline['id']}")
    return {**result, "baselines": choices}


def _archived_report(entry, user, version=None):
    from webapp.report_archive import build_snapshot

    try:
        if version is None:
            narrative = store.get_audit_narrative(entry["id"], audit_narrative_hash(entry["findings"]))
            snapshot = build_snapshot(entry, _org_branding(entry["org_id"]), narrative)
            version, _ = store.archive_report(entry["id"], user, snapshot)
        result = store.get_report_version(entry["id"], version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not result:
        raise HTTPException(404, "归档报告版本不存在。")
    return result


@app.get("/api/archive")
def archive_search(q: str = Query(default="", max_length=120), period: str = Query(default="", max_length=80),
                   date_from: date | None = None, date_to: date | None = None,
                   risk: str = Query(default="all", pattern="^(all|hit|high)$"),
                   page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                   session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "审计日期起始日不能晚于截止日。")
    return store.search_audits(user, q.strip(), period.strip(), date_from.isoformat() if date_from else None,
                             date_to.isoformat() if date_to else None, risk, page, page_size)


@app.get("/api/audits/{audit_id}/report-versions")
def report_versions(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    _audit_or_404(audit_id, user)
    try:
        return store.report_versions(audit_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/audits/{audit_id}/report-versions")
def archive_current_report(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    result = _archived_report(_audit_or_404(audit_id, user), user)
    return {key: result[key] for key in ("version", "html_sha256", "content_sha256", "created_at", "manifest")}


@app.get("/api/report/{audit_id}")
def report(
    audit_id: str, background: BackgroundTasks, confirm: bool = False,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    version: int | None = Query(default=None, ge=1),
) -> Any:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    if user["role"] == "accountant" and not confirm:
        raise HTTPException(status_code=409, detail="会计导出需二次确认，请确认报告用途后重试。")
    entry = _audit_or_404(audit_id, user)
    archived = _archived_report(entry, user, version)
    manifest = archived["manifest"]
    try:
        if archived["pdf_bytes"] is None:
            fd, tmp = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            try:
                render.export_pdf(archived["html"], tmp, report_no=manifest["report_no"], footer_text=manifest["footer_text"])
                archived = store.attach_report_pdf(audit_id, archived["version"], Path(tmp).read_bytes(), actor=user)
            finally:
                Path(tmp).unlink(missing_ok=True)
    except RuntimeError as exc:
        return _err(500, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    short = _safe_filename_component(entry["company_name"].replace("（仿真样例）", "")[:12])
    title = _safe_filename_component(manifest["report_title"])
    pdf_name = f"{title}-{short}-{entry['audited_at'][:10].replace('-', '')}-v{archived['version']}.pdf"
    _audit_or_404(audit_id, user)
    store.log(user, "export_report", "audit", audit_id, f"version={archived['version']}")
    return Response(content=archived["pdf_bytes"], media_type="application/pdf", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(pdf_name),
        "X-TaxPearls-Report-Version": str(archived["version"]), "X-TaxPearls-SHA256": archived["pdf_sha256"],
        "Cache-Control": "private, no-store"})


@app.get("/api/report/{audit_id}/html")
def report_html(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME),
                version: int | None = Query(default=None, ge=1)) -> Response:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(audit_id, user)
    archived = _archived_report(entry, user, version)
    _audit_or_404(audit_id, user)
    store.log(user, "view_report", "audit", audit_id, f"version={archived['version']}")
    return Response(content=archived["html"], media_type="text/html", headers={
        "X-TaxPearls-Report-Version": str(archived["version"]), "X-TaxPearls-SHA256": archived["html_sha256"],
        "Cache-Control": "private, no-store"})


@app.get("/api/org/settings")
def get_org_settings(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "student")
    return store.get_org_settings(user["org_id"], actor=user)


@app.put("/api/org/settings")
def put_org_settings(
    body: OrgSettingsBody,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin")
    try:
        saved = store.update_org_settings(
            user["org_id"], body.display_name, body.report_title, body.footer_text, actor=user
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return saved


@app.get("/api/org/logo")
def get_org_logo(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "student")
    logo = store.get_org_logo(user["org_id"], actor=user)
    if not logo:
        raise HTTPException(status_code=404, detail="当前机构尚未配置 Logo。")
    return Response(
        content=logo[1], media_type=logo[0],
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/org/logo")
async def put_org_logo(
    request: Request,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin")
    async with multipart(request, file_limit=MAX_ORG_LOGO_BYTES, max_fields=0) as form:
        content = await single_file(form).read()
    try:
        mime, normalized = _normalize_org_logo(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    finally:
        content = b""
    saved = store.update_org_logo(user["org_id"], mime, normalized, actor=user)
    return saved


@app.delete("/api/org/logo")
def delete_org_logo(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin")
    saved = store.clear_org_logo(user["org_id"], actor=user)
    return saved


@app.get("/api/knowledge/rule-catalog")
def knowledge_rule_catalog(session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    """Current graph editions, with legacy controls explicitly scoped to compatibility."""
    _user(session)
    from src.rule_conversion import compile_rule
    loaded = _effective_rules()
    overrides, enabled = store.rule_overrides(), store.enabled_rule_ids()
    definitions = {compile_rule(r)['rule_id']: r for r in loaded}
    with store.connect() as db:
        policy_graph.sync_rules(db, loaded)
        native = [dict(row) for row in db.execute(
            'SELECT * FROM TaxRule WHERE execution_json IS NULL AND conversion_json IS NULL')]
        rows = native + [policy_graph.get(db, 'TaxRule', rid) for rid in definitions]
        result = []
        for row in rows:
            rid = row['rule_id']
            rule = definitions.get(rid)
            result.append({
                'rule_id': rid, 'rule_code': row['rule_code'], 'name': row['name'],
                'version': row['version'], 'review_status': row['review_status'],
                'reviewed_by': row['reviewed_by'], 'reviewed_at': row['reviewed_at'],
                'source': 'converted' if rule else 'graph',
                'legacy': _rule_payload(rule, enabled is None or rule.id in enabled,
                                        overrides.get(rule.id)) if rule else None,
            })
    return sorted(result, key=lambda row: (row['rule_code'] or '', row['version'], row['rule_id']))


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
    store.change_rule_state(rule_id, body.enabled, user, valid)
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
            user, previous_override["version"] if previous_override else current.version,
            candidate.effective_from, candidate.effective_to,
            asdict(candidate),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    enabled = store.enabled_rule_ids()
    return _rule_payload(candidate, enabled is None or rule_id in enabled, saved)


@app.post("/api/rules/{rule_id}/trial")
def rule_trial(
    rule_id: str, body: RuleTrialBody,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(body.audit_id, user)
    current = _rule_by_id(rule_id)
    candidate = _candidate_rule(current, body)
    finding = engine.evaluate(candidate, entry["dataset"])
    store.log(
        user, "trial_rule_parameters", "audit", body.audit_id,
        f"rule={rule_id}; version={candidate.version}; result={finding.status}",
    )
    return _trial_payload(finding, body.audit_id)


@app.post("/api/assignments")
def create_assignment(body: AssignmentBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _teaching_user(session)
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
        if not student or not student["active"] or student["role"] != "student" or student["org_id"] != user["org_id"]:
            raise HTTPException(status_code=422, detail="指定学生不存在或不属于当前机构。")
    try:
        assignment_id = store.create_assignment(
            user, body.title.strip(), body.audit_id, body.target_student_id,
            body.weights, body.false_positive_penalty, body.published,body.class_id,body.deadline_at,body.course_id,
        )
    except classroom.ClassroomError as exc:
        raise HTTPException(exc.status,str(exc)) from None
    return {"id": assignment_id}


@app.get("/api/assignments")
def assignments(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _teaching_user(session)
    _allow(user, "teacher", "student")
    return store.list_assignments(user)


@app.get("/api/assignments/{assignment_id}")
def assignment_detail(assignment_id: str,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _teaching_user(session)
    _allow(user, "teacher", "student")
    assignment = store.get_assignment_for_user(assignment_id,user)
    if not assignment:
        raise HTTPException(status_code=404, detail="作业不存在。")
    entry = store.get_audit(assignment["audit_id"])
    if not entry or entry["org_id"] != user["org_id"]:
        raise HTTPException(404,"作业材料不存在。")
    dataset: Dataset = entry["dataset"]
    public_assignment=dict(assignment)
    if user["role"] == "student":
        public_assignment.pop("weights",None)
    return {
        **public_assignment, "company": _company_dict(dataset),
        "generated_material_available": store.has_generated_exercise(assignment["audit_id"], user["org_id"]),
        "accounts": [
            {"code": a.code, "name": a.name, "opening": str(a.opening), "debit": str(a.debit),
             "credit": str(a.credit), "closing": str(a.closing)} for a in dataset.accounts
        ],
        "declarations": {k: str(v) for k, v in dataset.declarations.items()},
        "metrics": [{"name":m.name,"value":str(m.value),"source":m.source,"detail":m.detail}
                    for m in dataset.metrics.values()],
        # Answer options must not inherit the audit's hit-first ordering.
        "rules": [{"id": f.rule.id, "name": f.rule.name, "category": f.rule.category}
                  for f in sorted(entry["findings"], key=lambda finding: finding.rule.id)],
        # 学生端结果按任务策略下发：未开放时只回执已提交，不给分数与逐项解析
        "submission": (training_submission_routes.result_for_student(store, assignment_id, user["id"])
                       if user["role"] == "student" else None),
    }


@app.post("/api/assignments/{assignment_id}/submit")
def submit_assignment(assignment_id: str, body: SubmissionBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _teaching_user(session)
    _allow(user, "student")
    assignment = store.get_assignment_for_user(assignment_id,user)
    if not assignment:
        raise HTTPException(status_code=404, detail="作业不存在。")
    if not assignment["can_submit"]:
        raise HTTPException(409,"任务尚未开放或已截止，不能提交或覆盖已有成绩。")
    entry = store.get_audit(assignment["audit_id"])
    if not entry or entry["org_id"] != user["org_id"]:
        raise HTTPException(404,"作业材料不存在。")
    try:
        result = training.score_submission(
            entry["findings"], body.selected_rule_ids,
            assignment["weights"], assignment["false_positive_penalty"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        store.save_submission(assignment_id, user["id"], body.selected_rule_ids, result["score"], result,user=user)
    except classroom.ClassroomError as exc:
        raise HTTPException(exc.status,str(exc)) from None
    # 答案与解析按任务策略下发：after_due / manual 未到时点，回执里只确认已提交
    visible, note = training_submission_routes.result_gate(store, assignment_id, user["id"])
    if visible:
        return {**result, "result_released": True, "result_note": ""}
    return {"result_released": False, "result_note": note, "score": None,
            "correct": [], "missed": [], "false_positives": [],
            "false_positive_deduction": None}


@app.post("/api/assignments/{assignment_id}/feedback")
def teaching_feedback(assignment_id: str, body: SandboxFeedbackBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "student", "teacher")
    assignment = store.get_assignment_for_user(assignment_id,user)
    if not assignment:
        raise HTTPException(404, "作业不存在。")
    entry = store.get_audit(assignment["audit_id"])
    if not entry or entry["org_id"] != user["org_id"]:
        raise HTTPException(404, "作业材料不存在。")
    try:
        result = sandbox_feedback.feedback(entry["dataset"], [f.rule for f in entry["findings"]], **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return JSONResponse(result, headers={"Cache-Control":"private, no-store"})


@app.put("/api/submissions/{submission_id}/review")
def review_submission(submission_id: str, body: ReviewBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _teaching_user(session)
    _allow(user, "teacher")
    if store.submission_org(submission_id,user=user) != user["org_id"]:
        raise HTTPException(status_code=404, detail="提交记录不存在。")
    store.review_submission(submission_id, user["id"], body.adjusted_score, body.feedback.strip(), actor=user)
    return {"ok": True}


@app.get("/api/submissions")
def submissions(assignment_id: str | None = None,
                session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _teaching_user(session)
    _allow(user, "teacher")
    return store.list_submissions(user, assignment_id)


@app.get("/api/audit-log")
def audit_log(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "teacher", "org_admin", "platform_admin")
    return store.list_logs(user)
