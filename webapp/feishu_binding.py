"""Account linking through a signed bot adapter or server-verified Feishu OAuth.

No browser route accepts an open_id. A URL ticket grants neither a session nor a
binding; bot tickets need a web claim AND the original private-chat action.
"""
from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPException as HTTPProtocolError
import hashlib
import base64
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request as URLRequest, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from fastapi import Cookie, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from webapp import channel_bindings as bindings, members
from webapp.access import AccessDenied

TTL = 600
LIMIT = 4096
INTERNAL_PATH = '/api/internal/feishu-binding'
STATIC = Path(__file__).parent / 'static'
INVALID = '绑定请求已失效或不可访问，请回到飞书重新发起。'
logger = logging.getLogger(__name__)


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS feishu_binding_requests (
        id TEXT PRIMARY KEY, ticket_hash TEXT NOT NULL, scope_key TEXT NOT NULL,
        subject_hash TEXT NOT NULL, identity_json TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK(state IN ('pending','web_confirmed','completed','cancelled')),
        created_at REAL NOT NULL, expires_at REAL NOT NULL,
        user_id TEXT REFERENCES users(id), org_id TEXT, role TEXT, session_hash TEXT,
        binding_id TEXT, last_prompt_at REAL NOT NULL DEFAULT 0
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_feishu_binding_subject ON feishu_binding_requests(scope_key,subject_hash)')
    db.execute('CREATE TABLE IF NOT EXISTS feishu_bridge_nonces (nonce TEXT PRIMARY KEY, expires_at REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS feishu_task_creations (operation_key TEXT PRIMARY KEY, task_id TEXT NOT NULL, created_at TEXT NOT NULL)')


@dataclass(frozen=True)
class Settings:
    app_id: str
    tenant_key: str
    bridge_key: bytes
    public_base: str

    @property
    def scope(self):
        return bindings.Scope('feishu', self.app_id, self.tenant_key)


def settings():
    if os.getenv('TAXPEARLS_FEISHU_BINDING_ENABLED') != '1':
        raise HTTPException(503, '飞书账户绑定尚未启用。')
    app_id = os.getenv('TAXPEARLS_FEISHU_APP_ID', '').strip()
    tenant = os.getenv('TAXPEARLS_FEISHU_APP_TENANT_KEY', '').strip()
    key = os.getenv('TAXPEARLS_FEISHU_BRIDGE_KEY', '').strip()
    base = os.getenv('TAXPEARLS_PUBLIC_BASE_URL', '').rstrip('/')
    try:
        parsed = urlsplit(base)
        local = parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}
        valid_url = ((parsed.scheme == 'https' or local) and parsed.hostname and not parsed.username
                     and not parsed.password and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment)
        if not re.fullmatch(r'cli_[A-Za-z0-9]+', app_id) or not tenant or not re.fullmatch(r'[0-9a-fA-F]{64}', key) or not valid_url:
            raise ValueError()
        bindings._identifier(tenant)
    except ValueError:
        raise HTTPException(503, '飞书绑定服务配置不完整，请检查应用、租户、独立桥接密钥和公网地址。') from None
    return Settings(app_id, tenant, bytes.fromhex(key), base)


def bot_url():
    # Opening the bot is public and does not grant a binding or a login session.
    app_id = os.getenv('TAXPEARLS_FEISHU_APP_ID', '').strip()
    if not re.fullmatch(r'cli_[A-Za-z0-9]+', app_id):
        raise HTTPException(503, '尚未配置飞书助手入口，暂时无法提供二维码或打开链接，请联系管理员。')
    return 'https://applink.feishu.cn/client/bot/open?appId=' + app_id


def _secret(config, purpose, identity):
    return hmac.new(config.bridge_key, (purpose + ':' + identity).encode(), hashlib.sha256).hexdigest()


def _oauth_secret():
    secret = os.getenv('TAXPEARLS_FEISHU_APP_SECRET', '').strip()
    if not secret:
        raise HTTPException(503, '网页绑定尚未配置，请联系管理员配置飞书应用凭据和授权回调地址。')
    return secret


def _oauth_account_proof(config, actor, session):
    return _secret(config, 'oauth-account', json.dumps(
        [config.scope.key, actor['id'], actor['org_id'], actor['role'], bindings._digest(session)], separators=(',', ':')))


def _oauth_verifier(config, state):
    return _secret(config, 'oauth-pkce', state)


def oauth_begin(store, config, session, account_proof):
    _oauth_secret()
    stamp, identity = time.time(), secrets.token_hex(16)
    state = identity + '.' + secrets.token_hex(32)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        actor = bindings._session(db, session)
        if actor['role'] not in {'org_admin', 'accountant'}:
            raise AccessDenied('本次飞书绑定先支持企业管理员和会计账号。', 403)
        if not hmac.compare_digest(_oauth_account_proof(config, actor, session), account_proof):
            raise AccessDenied('登录账号或会话已变化，请刷新页面核对账号后再确认。', 409)
        if db.execute("SELECT 1 FROM channel_bindings WHERE user_id=? AND channel='feishu'", (actor['id'],)).fetchone():
            raise AccessDenied('当前账号已绑定飞书，请先解绑。', 409)
        db.execute('DELETE FROM channel_binding_flows WHERE expires_at<=?', (stamp,))
        latest = db.execute("SELECT MAX(created_at) FROM channel_binding_flows WHERE user_id=? AND channel='feishu'", (actor['id'],)).fetchone()[0]
        if latest is not None and stamp - latest < 5:
            raise AccessDenied('请稍后再发起飞书授权。', 429)
        if db.execute('SELECT COUNT(*) FROM channel_binding_flows').fetchone()[0] >= LIMIT:
            raise AccessDenied('绑定服务繁忙，请稍后重试。', 503)
        db.execute("UPDATE channel_binding_flows SET status='cancelled',recipient_key=NULL WHERE user_id=? AND channel='feishu' AND status IN ('pending','candidate')", (actor['id'],))
        db.execute('''INSERT INTO channel_binding_flows
            (id,code_hash,user_id,org_id,role,session_hash,channel,scope_key,created_at,expires_at,status)
            VALUES (?,?,?,?,?,?,'feishu',?,?,?,'pending')''',
            (identity, bindings._digest(state), actor['id'], actor['org_id'], actor['role'], bindings._digest(session), config.scope.key, stamp, stamp + TTL))
        members.log(db, actor, 'feishu_oauth_begin', 'channel_binding', identity, 'channel=feishu')
    challenge = base64.urlsafe_b64encode(hashlib.sha256(_oauth_verifier(config, state).encode()).digest()).decode().rstrip('=')
    return {'url': 'https://accounts.feishu.cn/open-apis/authen/v1/authorize?' + urlencode({
        'client_id': config.app_id, 'response_type': 'code',
        'redirect_uri': config.public_base + '/connect/feishu/callback', 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'consent',
        'scope': 'contact:user.base:readonly'}), 'expires_in': TTL}


class _NoOAuthRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _oauth_failure(stage, kind, *, status=None, value=None):
    # Never echo provider descriptions, bodies, URLs, tokens or exception strings.
    value = value if isinstance(value, dict) else {}
    code = str(value.get('code', ''))
    code = code if re.fullmatch(r'[0-9]{1,10}', code) else ''
    oauth_error = value.get('error')
    oauth_error = oauth_error if isinstance(oauth_error, str) and oauth_error in {
        'invalid_request', 'invalid_client', 'invalid_grant', 'unauthorized_client',
        'unsupported_grant_type', 'invalid_scope', 'access_denied', 'server_error', 'temporarily_unavailable',
    } else ''
    reason = {
        'timeout': '服务器连接飞书超时，请稍后重新发起授权。',
        'tls': '服务器与飞书的 HTTPS 证书校验失败，请管理员检查系统证书和代理配置。',
        'network': '服务器无法连接飞书，请管理员检查出站网络、DNS 和代理配置。',
        'format': '飞书返回了无法解析的响应，请管理员检查网络代理及上游服务。',
        'oversized': '飞书响应超过大小限制，请管理员检查上游服务。',
    }.get(kind, '飞书拒绝了本次请求，请管理员根据错误码核对应用配置。')
    if oauth_error in {'invalid_client', 'unauthorized_client'} or code in {'20130', '20140'}:
        reason = '应用身份校验未通过，请核对网站后端的 App ID、App Secret 及飞书后台允许的认证方式。'
    elif oauth_error == 'invalid_grant':
        reason = '授权码校验未通过，可能已过期、已使用或与回调地址及 PKCE 校验信息不一致，请重新发起授权。'
    elif oauth_error == 'invalid_scope':
        reason = '授权权限未通过校验，请核对用户身份权限是否开通并发布。'
    details = [f'HTTP {status}'] if status else []
    if code:
        details.append('飞书错误码 ' + code)
    if oauth_error:
        details.append(oauth_error)
    label = '换取授权凭据' if stage == 'token' else '读取用户身份'
    logger.warning('feishu_oauth_failure stage=%s kind=%s http_status=%s provider_code=%s oauth_error=%s',
                   stage, kind, status or '-', code or '-', oauth_error or '-')
    return HTTPException(502, '飞书' + label + '失败' + ('（' + '，'.join(details) + '）' if details else '') + '。' + reason)


def _oauth_json(url, *, body=None, token=None):
    # Fixed provider URLs, no redirects/retries, credentials never reach browser or logs.
    stage = 'token' if body is not None else 'user_info'
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if body is not None:
        headers['Content-Type'] = 'application/json'
    request = URLRequest(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    status = None
    try:
        try:
            response = build_opener(_NoOAuthRedirect()).open(request, timeout=15)
        except HTTPError as error:
            # OAuth failures normally use HTTP 400; their JSON carries the useful code.
            response = error
        with response as stream:
            status = stream.status
            raw = stream.read(65537)
    except (socket.timeout, TimeoutError):
        raise _oauth_failure(stage, 'timeout') from None
    except ssl.SSLError:
        raise _oauth_failure(stage, 'tls') from None
    except URLError as error:
        kind = 'tls' if isinstance(error.reason, ssl.SSLError) else 'timeout' if isinstance(error.reason, (socket.timeout, TimeoutError)) else 'network'
        raise _oauth_failure(stage, kind) from None
    except (OSError, ValueError, HTTPProtocolError):
        raise _oauth_failure(stage, 'network') from None
    if len(raw) > 65536:
        raise _oauth_failure(stage, 'oversized', status=status)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise _oauth_failure(stage, 'format', status=status) from None
    if not isinstance(value, dict):
        raise _oauth_failure(stage, 'format', status=status)
    if not 200 <= status < 300 or value.get('code', 0) not in (0, '0') or value.get('error'):
        raise _oauth_failure(stage, 'provider', status=status, value=value)
    return value


def _oauth_identity(config, code, state):
    value = _oauth_json('https://accounts.feishu.cn/oauth/v3/token', body={
        'grant_type': 'authorization_code', 'client_id': config.app_id, 'client_secret': _oauth_secret(),
        'code': code, 'redirect_uri': config.public_base + '/connect/feishu/callback',
        'code_verifier': _oauth_verifier(config, state)})
    token = value.get('access_token')
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise HTTPException(502, '飞书未返回有效授权凭据，请重新发起绑定。')
    value = _oauth_json('https://open.feishu.cn/open-apis/authen/v1/user_info', token=token)
    data = value.get('data')
    subject = data.get('open_id') if isinstance(data, dict) else None
    if not isinstance(subject, str) or not re.fullmatch(r'ou_[A-Za-z0-9]+', subject):
        raise HTTPException(502, '飞书未返回有效用户身份，请重新发起绑定。')
    # The binding scope is the bot app's owner tenant, including shared external users.
    # Provider email, nickname, user tenant and browser parameters never choose an account.
    return subject


def _oauth_row(db, config, session, state):
    if not re.fullmatch(r'[0-9a-f]{32}\.[0-9a-f]{64}', state):
        raise AccessDenied('飞书授权已失效，请重新发起绑定。', 409)
    row = db.execute('SELECT * FROM channel_binding_flows WHERE id=?', (state[:32],)).fetchone()
    actor = bindings._flow_actor(db, row, session)
    if row['channel'] != 'feishu' or row['scope_key'] != config.scope.key or not hmac.compare_digest(row['code_hash'], bindings._digest(state)):
        raise AccessDenied('飞书授权已失效，请重新发起绑定。', 409)
    if actor['role'] not in {'org_admin', 'accountant'}:
        raise AccessDenied('当前账号不支持飞书绑定。', 403)
    return row, actor


def oauth_complete(store, config, session, state, code, error):
    _oauth_secret()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row, actor = _oauth_row(db, config, session, state)
        if row['status'] != 'pending':
            raise AccessDenied('飞书授权已使用，请重新发起绑定。', 409)
        # Consume before the network request. Simultaneous/replayed callbacks cannot exchange twice.
        db.execute("UPDATE channel_binding_flows SET status=? WHERE id=?", ('cancelled' if error or not code else 'candidate', row['id']))
    if error or not code:
        return {'state': 'cancelled'}
    try:
        subject = _oauth_identity(config, code, state)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row, actor = _oauth_row(db, config, session, state)
            result = bindings.insert_verified(db, actor, config.scope, subject)
            db.execute("UPDATE channel_binding_flows SET status='completed' WHERE id=?", (row['id'],))
            db.execute("UPDATE feishu_binding_requests SET state='cancelled' WHERE user_id=? AND state='web_confirmed'", (actor['id'],))
        return {'state': 'completed', 'binding': result, 'url': bot_url()}
    except Exception:
        with store.connect() as db:
            db.execute("UPDATE channel_binding_flows SET status='cancelled' WHERE id=? AND status='candidate'", (state[:32],))
        raise


def _ticket(config, identity):
    return identity + '.' + _secret(config, 'link', identity)


def _check_actor(db, row):
    user = db.execute('''SELECT u.id,u.org_id,u.role FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''', (row['session_hash'], members.now())).fetchone()
    if (not user or user['id'] != row['user_id'] or user['org_id'] != row['org_id'] or user['role'] != row['role']):
        raise AccessDenied('确认账号的登录或权限已变化，请重新发起绑定。', 409)
    return dict(user)


def _request_row(db, config, ticket):
    if not re.fullmatch(r'[0-9a-f]{32}\.[0-9a-f]{64}', ticket):
        raise AccessDenied(INVALID)
    row = db.execute('SELECT * FROM feishu_binding_requests WHERE id=?', (ticket[:32],)).fetchone()
    if (not row or row['scope_key'] != config.scope.key or row['expires_at'] <= time.time()
            or not hmac.compare_digest(row['ticket_hash'], bindings._digest(ticket))):
        raise AccessDenied(INVALID)
    return row


def _account(db, actor):
    row = db.execute('SELECT username,display_name,email FROM users WHERE id=?', (actor['id'],)).fetchone()
    org = db.execute('SELECT display_name FROM org_settings WHERE org_id=?', (actor['org_id'],)).fetchone()
    return {'username': row['username'], 'display_name': row['display_name'],
            'organization': org['display_name'] if org else actor['org_id']}


def _web_proof(config, row, actor, session):
    identity = json.dumps([row['id'], actor['id'], actor['org_id'], actor['role'], bindings._digest(session)], separators=(',', ':'))
    return _secret(config, 'web-account', identity)


def _masked_account(db, actor):
    # The private-chat final confirmation gets only a short account hint.
    value = db.execute('SELECT username FROM users WHERE id=?', (actor['id'],)).fetchone()['username']
    return value[:2] + '***' + (value[-1:] if len(value) > 2 else '')


def issue(store, config, subject, chat, event_id):
    if not re.fullmatch(r'ou_[A-Za-z0-9]+', subject) or not re.fullmatch(r'oc_[A-Za-z0-9]+', chat):
        raise AccessDenied('飞书会话身份无效。', 422)
    bindings._identifier(event_id)
    stamp = time.time()
    event_hash = bindings._digest(config.scope.key + ':' + subject + ':' + event_id)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM feishu_binding_requests WHERE expires_at<=?', (stamp,))
        existing = db.execute('SELECT * FROM channel_bindings WHERE channel=? AND scope_key=? AND subject_key=?',
                              ('feishu', config.scope.key, bindings._digest(subject))).fetchone()
        if existing:
            return {'state': 'bound'}
        old = db.execute('SELECT * FROM feishu_binding_requests WHERE event_hash=?', (event_hash,)).fetchone()
        if old:
            return {'state': old['state'], 'url': config.public_base + '/connect/feishu#ticket=' + _ticket(config, old['id'])}
        recent = db.execute('SELECT * FROM feishu_binding_requests WHERE scope_key=? AND subject_hash=? ORDER BY created_at DESC LIMIT 1',
                            (config.scope.key, bindings._digest(subject))).fetchone()
        if recent and recent['state'] in {'pending', 'web_confirmed'} and json.loads(recent['identity_json'])['chat'] == chat:
            return {'state': recent['state'], 'url': config.public_base + '/connect/feishu#ticket=' + _ticket(config, recent['id'])}
        if recent and stamp - recent['created_at'] < 15:
            raise AccessDenied('绑定链接刚刚发送，请使用原卡片或稍后重试。', 429)
        if db.execute('SELECT COUNT(*) FROM feishu_binding_requests').fetchone()[0] >= LIMIT:
            raise AccessDenied('绑定服务繁忙，请稍后重试。', 503)
        db.execute("UPDATE feishu_binding_requests SET state='cancelled' WHERE scope_key=? AND subject_hash=? AND state IN ('pending','web_confirmed')",
                   (config.scope.key, bindings._digest(subject)))
        identity = secrets.token_hex(16)
        ticket = _ticket(config, identity)
        payload = db.field_codec.seal(json.dumps({'subject': subject, 'chat': chat}),
                                     'feishu_binding_requests', 'identity_json', identity)
        db.execute('''INSERT INTO feishu_binding_requests
            (id,ticket_hash,scope_key,subject_hash,identity_json,event_hash,state,created_at,expires_at)
            VALUES (?,?,?,?,?,?,'pending',?,?)''',
            (identity, bindings._digest(ticket), config.scope.key, bindings._digest(subject), payload, event_hash, stamp, stamp + TTL))
        return {'state': 'pending', 'url': config.public_base + '/connect/feishu#ticket=' + ticket, 'expires_in': TTL}


def inspect_request(store, config, session, ticket):
    with store.connect() as db:
        db.execute('BEGIN')
        actor = bindings._session(db, session)
        row = _request_row(db, config, ticket)
        if row['user_id'] and (row['user_id'] != actor['id'] or row['session_hash'] != bindings._digest(session)):
            raise AccessDenied(INVALID)
        result = {'state': row['state'], 'account': _account(db, actor), 'expires_in': max(0, int(row['expires_at'] - time.time())),
                  'web_proof': _web_proof(config, row, actor, session)}
        if row['state'] == 'web_confirmed':
            _check_actor(db, row)
        if row['state'] == 'completed':
            result['state'] = 'completed' if db.execute('SELECT 1 FROM channel_bindings WHERE id=?', (row['binding_id'],)).fetchone() else 'cancelled'
        return result


def claim(store, config, session, ticket, web_proof):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        actor = bindings._session(db, session)
        if actor['role'] not in {'org_admin', 'accountant'}:
            raise AccessDenied('本次飞书绑定先支持企业管理员和会计账号。', 403)
        row = _request_row(db, config, ticket)
        if not hmac.compare_digest(_web_proof(config, row, actor, session), web_proof):
            raise AccessDenied('登录账号或会话已变化，请刷新页面核对账号后再确认。', 409)
        if row['state'] == 'web_confirmed' and row['session_hash'] == bindings._digest(session):
            _check_actor(db, row)
            return {'state': 'web_confirmed'}
        if row['state'] != 'pending':
            raise AccessDenied('该绑定请求已被确认或取消，请重新发起。', 409)
        if db.execute('SELECT 1 FROM channel_bindings WHERE user_id=? AND channel=?', (actor['id'], 'feishu')).fetchone():
            raise AccessDenied('当前账号已绑定飞书，请先解绑。', 409)
        db.execute("UPDATE feishu_binding_requests SET state='cancelled' WHERE user_id=? AND state='web_confirmed'",
                   (actor['id'],))
        db.execute("UPDATE feishu_binding_requests SET state='web_confirmed',user_id=?,org_id=?,role=?,session_hash=? WHERE id=?",
                   (actor['id'], actor['org_id'], actor['role'], bindings._digest(session), row['id']))
        members.log(db, actor, 'feishu_binding_web_confirm', 'feishu_binding', row['id'], 'channel=feishu')
        return {'state': 'web_confirmed'}


def pending_confirmations(store, config):
    result = []
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT * FROM feishu_binding_requests WHERE scope_key=? AND state='web_confirmed' AND expires_at>? AND last_prompt_at<? LIMIT 20",
                          (config.scope.key, time.time(), time.time() - 30)).fetchall()
        for row in rows:
            try:
                actor = _check_actor(db, row)
            except AccessDenied:
                db.execute("UPDATE feishu_binding_requests SET state='cancelled' WHERE id=?", (row['id'],))
                continue
            identity = json.loads(row['identity_json'])
            result.append({**identity, 'id': row['id'], 'account_hint': _masked_account(db, actor),
                           'confirmation': _secret(config, 'confirm', row['id'] + ':' + row['session_hash'])})
            db.execute('UPDATE feishu_binding_requests SET last_prompt_at=? WHERE id=?', (time.time(), row['id']))
    return result


def finish(store, config, identity, subject, chat, confirmation, *, cancel=False):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM feishu_binding_requests WHERE id=?', (identity,)).fetchone()
        if (not row or row['scope_key'] != config.scope.key or row['expires_at'] <= time.time()
                or row['state'] not in {'web_confirmed', 'completed'}):
            raise AccessDenied(INVALID, 409)
        source = json.loads(row['identity_json'])
        expected = _secret(config, 'confirm', row['id'] + ':' + row['session_hash'])
        if source != {'subject': subject, 'chat': chat} or not hmac.compare_digest(expected, confirmation):
            raise AccessDenied(INVALID)
        actor = _check_actor(db, row)
        if row['state'] == 'completed':
            if not cancel and db.execute('SELECT 1 FROM channel_bindings WHERE id=?', (row['binding_id'],)).fetchone():
                return {'state': 'completed'}
            raise AccessDenied(INVALID, 409)
        if cancel:
            db.execute("UPDATE feishu_binding_requests SET state='cancelled' WHERE id=?", (identity,))
            members.log(db, actor, 'feishu_binding_cancel', 'feishu_binding', identity, 'channel=feishu')
            return {'state': 'cancelled'}
        result = bindings.insert_verified(db, actor, config.scope, subject)
        db.execute("UPDATE feishu_binding_requests SET state='completed',binding_id=? WHERE id=?", (result['id'], identity))
        return {'state': 'completed'}


class TicketBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ticket: str = Field(min_length=1, max_length=120, repr=False)


class ClaimBody(TicketBody):
    web_proof: str = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$', repr=False)


class OAuthBeginBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_proof: str = Field(pattern=r'^[0-9a-f]{64}$', repr=False)


class OAuthCompleteBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: str = Field(min_length=1, max_length=120, repr=False)
    code: str = Field(default='', max_length=2048, repr=False)
    error: str = Field(default='', max_length=256)


class BridgeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    app_id: str = Field(max_length=256)
    operation: str = Field(max_length=20)
    binding_tag: str = Field(default='', max_length=64)
    operation_id: str = Field(default='', max_length=32)
    view: str = Field(default='', max_length=20)
    page: int = Field(default=0, ge=0, le=9, strict=True)
    subject: str = Field(default='', max_length=256)
    chat: str = Field(default='', max_length=256)
    event_id: str = Field(default='', max_length=256)
    id: str = Field(default='', max_length=64)
    confirmation: str = Field(default='', max_length=128, repr=False)


def _same_origin(request):
    origin = request.headers.get('origin')
    if request.headers.get('sec-fetch-site') == 'cross-site' or origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '请从本站绑定页面操作。')
    if request.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise HTTPException(415, '请使用 JSON 请求。')


def register(app, store_provider, cookie_name):
    @app.get('/connect/feishu')
    @app.get('/connect/feishu/callback')
    def page():
        return FileResponse(STATIC / 'feishu-connect.html', headers={
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Robots-Tag': 'noindex, nofollow',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})

    @app.get('/feishu-connect.js')
    @app.get('/feishu-connect.css')
    def asset(request: Request):
        filename = request.url.path[1:]
        return FileResponse(STATIC / filename, headers={'Cache-Control': 'no-store'})

    @app.get('/api/channels/feishu/entry')
    def entry():
        url = bot_url()
        try:
            settings()
        except HTTPException:
            return {'url': url, 'binding_ready': False, 'oauth_ready': False,
                    'message': '账号绑定暂不可用，可先扫码或点击链接打开飞书助手。'}
        return {'url': url, 'binding_ready': True, 'oauth_ready': bool(os.getenv('TAXPEARLS_FEISHU_APP_SECRET', '').strip()),
                'binding_url': '/connect/feishu', 'message': '可从网页授权绑定，或打开助手使用私聊绑定入口。'}

    @app.get('/api/channels/feishu/oauth/account')
    def oauth_account(session: str | None = Cookie(default=None, alias=cookie_name)):
        config = settings()
        _oauth_secret()
        with store_provider().connect() as db:
            actor = bindings._session(db, session)
            return JSONResponse({'account': _account(db, actor), 'account_proof': _oauth_account_proof(config, actor, session)}, headers={'Cache-Control': 'no-store'})

    @app.post('/api/channels/feishu/oauth/begin')
    def begin_oauth(body: OAuthBeginBody, request: Request, session: str | None = Cookie(default=None, alias=cookie_name)):
        _same_origin(request)
        return JSONResponse(oauth_begin(store_provider(), settings(), session, body.account_proof), headers={'Cache-Control': 'no-store'})

    @app.post('/api/channels/feishu/oauth/complete')
    def complete_oauth(body: OAuthCompleteBody, request: Request, session: str | None = Cookie(default=None, alias=cookie_name)):
        _same_origin(request)
        return JSONResponse(oauth_complete(store_provider(), settings(), session, body.state, body.code, body.error), headers={'Cache-Control': 'no-store'})

    @app.get('/api/channels/feishu/qr.svg')
    def qr():
        import qrcode
        import qrcode.image.svg
        image = qrcode.make(bot_url(), image_factory=qrcode.image.svg.SvgPathFillImage)
        return Response(image.to_string(), media_type='image/svg+xml')

    @app.get('/api/channels/feishu/bindings')
    def listing(session: str | None = Cookie(default=None, alias=cookie_name)):
        # Reading one's existing links remains possible while new binding is disabled.
        return [row for row in bindings.list_bindings(store_provider(), session) if row['channel'] == 'feishu']

    @app.delete('/api/channels/feishu/bindings/{binding_id}')
    def unlink(binding_id: str, request: Request, session: str | None = Cookie(default=None, alias=cookie_name)):
        _same_origin(request)
        settings()
        store = store_provider()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            actor = bindings._session(db, session)
            if not db.execute('SELECT 1 FROM channel_bindings WHERE id=? AND user_id=? AND channel=?',
                              (binding_id, actor['id'], 'feishu')).fetchone():
                raise AccessDenied()
        bindings.unlink(store, session, binding_id)
        return {'ok': True}

    @app.post('/api/channels/feishu/status')
    def status(body: TicketBody, request: Request, session: str | None = Cookie(default=None, alias=cookie_name)):
        _same_origin(request)
        return inspect_request(store_provider(), settings(), session, body.ticket)

    @app.post('/api/channels/feishu/claim')
    def web_claim(body: ClaimBody, request: Request, session: str | None = Cookie(default=None, alias=cookie_name)):
        _same_origin(request)
        return claim(store_provider(), settings(), session, body.ticket, body.web_proof)

    @app.post('/api/channels/feishu/cancel')
    def web_cancel(body: TicketBody, request: Request, session: str | None = Cookie(default=None, alias=cookie_name)):
        _same_origin(request)
        config = settings()
        with store_provider().connect() as db:
            db.execute('BEGIN IMMEDIATE')
            actor = bindings._session(db, session)
            row = _request_row(db, config, body.ticket)
            if row['state'] != 'web_confirmed' or row['session_hash'] != bindings._digest(session) or row['user_id'] != actor['id']:
                raise AccessDenied(INVALID, 409)
            db.execute("UPDATE feishu_binding_requests SET state='cancelled' WHERE id=?", (row['id'],))
            members.log(db, actor, 'feishu_binding_cancel', 'feishu_binding', row['id'], 'channel=feishu')
        return {'state': 'cancelled'}

    @app.post(INTERNAL_PATH)
    async def bridge(request: Request):
        config = settings()
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, '请求过大。')
        stamp = request.headers.get('x-tp-time', '')
        nonce = request.headers.get('x-tp-nonce', '')
        signature = request.headers.get('x-tp-signature', '')
        if (not re.fullmatch(r'[0-9]{1,12}', stamp) or abs(time.time() - int(stamp)) > 60
                or not re.fullmatch(r'[0-9a-f]{32}', nonce) or not re.fullmatch(r'[0-9a-f]{64}', signature)):
            raise HTTPException(401, '桥接鉴权失败。')
        message = '\n'.join(['POST', INTERNAL_PATH, stamp, nonce, hashlib.sha256(raw).hexdigest()])
        expected = hmac.new(config.bridge_key, message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, '桥接鉴权失败。')
        store = store_provider()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM feishu_bridge_nonces WHERE expires_at<=?', (time.time(),))
            if db.execute('SELECT 1 FROM feishu_bridge_nonces WHERE nonce=?', (nonce,)).fetchone():
                raise HTTPException(409, '桥接请求已使用。')
            if db.execute('SELECT COUNT(*) FROM feishu_bridge_nonces').fetchone()[0] >= LIMIT:
                raise HTTPException(429, '桥接请求过于频繁。')
            db.execute('INSERT INTO feishu_bridge_nonces VALUES (?,?)', (nonce, time.time() + 120))
        try:
            body = BridgeBody.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422, '桥接参数无效。') from None
        if body.app_id != config.app_id:
            raise HTTPException(403, '应用不匹配。')
        if body.operation == 'issue':
            result = issue(store, config, body.subject, body.chat, body.event_id)
        elif body.operation == 'lookup':
            if not re.fullmatch(r'ou_[A-Za-z0-9]+', body.subject):
                raise HTTPException(422, '飞书会话身份无效。')
            actor = bindings.actor_for_subject(store, config.scope, body.subject)
            result = {'state': 'bound' if actor else 'unbound'}
        elif body.operation == 'pending':
            result = {'items': pending_confirmations(store, config)}
        elif body.operation == 'binding_unlink':
            if not re.fullmatch(r'ou_[A-Za-z0-9]+', body.subject) or not re.fullmatch(r'[0-9a-f]{64}', body.binding_tag):
                raise HTTPException(422, '解除绑定请求无效，请重新读取账户状态。')
            from webapp import feishu_tasks
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT id FROM channel_bindings WHERE channel='feishu' AND scope_key=? AND subject_key=?",
                                 (config.scope.key, bindings._digest(body.subject))).fetchone()
                if row:
                    user, _ = feishu_tasks.actor(db, config, body.subject, body.binding_tag)
                    bindings.unlink_for_actor(db, user, row['id'])
            result = {'state': 'unbound'}
        elif body.operation in {'task_menu', 'task_list', 'task_create'}:
            from webapp import feishu_tasks
            result = feishu_tasks.execute(store, config, body)
        elif body.operation in {'confirm', 'cancel'}:
            result = finish(store, config, body.id, body.subject, body.chat, body.confirmation, cancel=body.operation == 'cancel')
        else:
            raise HTTPException(422, '不支持的桥接操作。')
        return JSONResponse(result)
