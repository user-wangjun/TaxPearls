"""P1 persistence, authentication and audit trail.

Only normalized audit results are persisted.  Uploaded workbooks are parsed in
memory and are never written to disk.  Passwords use Argon2id and session
tokens are stored as SHA-256 digests so a database copy cannot be used as a
logged-in browser session.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any
from contextlib import contextmanager

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from src.models import (
    Account, Company, Dataset, EvidenceItem, Finding, Metric, RelatedGraph,
    RelatedRelation, RelatedSubject, RelatedTrade, Rule,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "instance" / "taxpearls.db"
SESSION_HOURS = 12
ROLES = {"teacher", "student", "org_admin", "accountant", "platform_admin"}
COMMON_PASSWORDS = {"password123!", "admin123456!", "1234567890a!", "qwerty12345!"}


class SetupAlreadyInitialized(Exception):
    pass

_passwords = PasswordHasher()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or not 10 <= len(password) <= 128:
        raise ValueError("密码长度须为 10–128 位")
    classes = sum(bool(re.search(pattern, password)) for pattern in
                  (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if classes < 3 or password.lower() in COMMON_PASSWORDS:
        raise ValueError("密码须包含至少三类字符，且不能使用常见弱口令")


def _normalize_email(email: str) -> str:
    """邮箱归一化：去空格 + 转小写。同一邮箱只允许一个账号。"""
    return (email or "").strip().lower()


def _validate_email(email: str) -> str:
    """校验并归一化邮箱；允许为空（邮箱是可选绑定项），非法格式直接拒绝。"""
    address = _normalize_email(email)
    if not address:
        return ""
    if " " in address or address.count("@") != 1 or len(address) > 254:
        raise ValueError("邮箱格式不正确")
    local, _, domain = address.partition("@")
    if not local or not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("邮箱格式不正确")
    return address


PASSWORD_RESET_MINUTES = 10
REGISTER_CODE_MINUTES = 10
REGISTER_CODE_MAX_ATTEMPTS = 5
INVITE_CODE_DAYS = 7
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_invite_code(length: int = 12) -> str:
    """生成邀请码：Crockford Base32（排除 I/L/O/U，5.7.2）。"""
    return "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(length))


def _normalize_invite_code(code: str) -> str:
    """邀请码规范化：去分隔符与空白 → 大写 → 形近归一。"""
    cleaned = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return cleaned.translate(str.maketrans({"I": "1", "L": "1", "O": "0"}))


def _username_from_email(email: str, db) -> str:
    """由邮箱本地部分生成用户名，撞名自动追加随机后缀。"""
    local = _normalize_email(email).partition("@")[0]
    base = "".join(ch for ch in local if ch.isalnum())[:16] or "user"
    if not db.execute("SELECT 1 FROM users WHERE username=?", (base,)).fetchone():
        return base
    for _ in range(5):
        candidate = f"{base}{secrets.token_hex(2)}"
        if not db.execute("SELECT 1 FROM users WHERE username=?", (candidate,)).fetchone():
            return candidate
    return f"{base}{secrets.token_hex(4)}"


def serialize_dataset(dataset: Dataset) -> dict[str, Any]:
    return {
        "company": asdict(dataset.company),
        "accounts": [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(a).items()}
            for a in dataset.accounts
        ],
        "declarations": {k: str(v) for k, v in dataset.declarations.items()},
        "metrics": {
            k: {"name": m.name, "value": str(m.value), "source": m.source, "detail": m.detail}
            for k, m in dataset.metrics.items()
        },
        "related_graph": None if dataset.related_graph is None else {
            "subjects": [asdict(item) for item in dataset.related_graph.subjects],
            "relations": [asdict(item) for item in dataset.related_graph.relations],
            "trades": [{**asdict(item), "amount": str(item.amount)} for item in dataset.related_graph.trades],
        },
    }


def deserialize_dataset(data: dict[str, Any]) -> Dataset:
    def dec(value: Any) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    return Dataset(
        company=Company(**data["company"]),
        accounts=[
            Account(
                code=a["code"], name=a["name"], opening=dec(a["opening"]),
                debit=dec(a["debit"]), credit=dec(a["credit"]), closing=dec(a["closing"]),
            )
            for a in data["accounts"]
        ],
        declarations={k: Decimal(v) for k, v in data["declarations"].items()},
        metrics={
            k: Metric(name=m["name"], value=Decimal(m["value"]), source=m["source"], detail=m["detail"])
            for k, m in data["metrics"].items()
        },
        related_graph=(RelatedGraph(
            subjects=[RelatedSubject(**item) for item in data["related_graph"]["subjects"]],
            relations=[RelatedRelation(**item) for item in data["related_graph"]["relations"]],
            trades=[RelatedTrade(**{**item, "amount": Decimal(item["amount"])})
                    for item in data["related_graph"]["trades"]],
        ) if data.get("related_graph") is not None else None),
    )


def serialize_findings(findings: list[Finding]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for finding in findings:
        item = asdict(finding)
        item["measured"] = finding.measured
        out.append(item)
    return out


def deserialize_findings(data: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    for item in data:
        rule = Rule(**item["rule"])
        evidence = [EvidenceItem(**row) for row in item.get("evidence", [])]
        findings.append(Finding(
            rule=rule, status=item["status"], measured=item.get("measured"),
            threshold_desc=item.get("threshold_desc", ""), conclusion=item.get("conclusion", ""),
            evidence=evidence, calculation=item.get("calculation", ""),
            skip_reason=item.get("skip_reason", ""),
        ))
    return findings


class Store:
    """Small SQLite repository with explicit organization and ownership checks."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured = os.environ.get("TAXPEARLS_DB")
        p = Path(path or configured or DEFAULT_DB)
        # 相对路径一律锚定项目根，与启动时的工作目录无关（防止在子目录启动时误建空库）。
        self.path = p if p.is_absolute() else (ROOT / p).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._init_schema()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            # journal_mode=WAL 在 _init_schema 时设置一次并持久化于库文件；
            # 不在每次连接时执行——并发连接同时切 WAL 在 Windows 上会以
            # SQLITE_READONLY 的面目报错（tests/test_auth_hardening 的偶发抖动根因）。
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self.connect() as db:
            # WAL 一次设置、持久化于库文件
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
                    role TEXT NOT NULL, org_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clients (
                    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL,
                    taxpayer_id TEXT NOT NULL, accountant_id TEXT REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(org_id, taxpayer_id)
                );
                CREATE TABLE IF NOT EXISTS audits (
                    id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
                    client_id TEXT REFERENCES clients(id), created_by TEXT NOT NULL REFERENCES users(id),
                    company_name TEXT NOT NULL, taxpayer_id TEXT NOT NULL,
                    industry TEXT NOT NULL, period TEXT NOT NULL,
                    dataset_json TEXT NOT NULL, findings_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL, audited_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audits_org ON audits(org_id, audited_at DESC);
                CREATE TABLE IF NOT EXISTS assignments (
                    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, title TEXT NOT NULL,
                    audit_id TEXT NOT NULL REFERENCES audits(id), created_by TEXT NOT NULL REFERENCES users(id),
                    target_student_id TEXT REFERENCES users(id), weights_json TEXT NOT NULL,
                    false_positive_penalty REAL NOT NULL, published INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignments(id),
                    student_id TEXT NOT NULL REFERENCES users(id), answers_json TEXT NOT NULL,
                    score REAL NOT NULL, details_json TEXT NOT NULL, submitted_at TEXT NOT NULL,
                    adjusted_score REAL, feedback TEXT, reviewed_by TEXT REFERENCES users(id),
                    UNIQUE(assignment_id, student_id)
                );
                CREATE TABLE IF NOT EXISTS rule_state (
                    rule_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_overrides (
                    rule_id TEXT PRIMARY KEY, version TEXT NOT NULL,
                    logic_json TEXT NOT NULL, threshold_basis TEXT NOT NULL,
                    updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_version_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id TEXT NOT NULL, version TEXT NOT NULL,
                    effective_from TEXT, effective_to TEXT,
                    logic_json TEXT NOT NULL, threshold_basis TEXT NOT NULL,
                    rule_json TEXT,
                    updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL,
                    UNIQUE(rule_id, version),
                    CHECK(effective_from IS NOT NULL OR effective_to IS NULL)
                );
                CREATE INDEX IF NOT EXISTS idx_rule_version_period
                    ON rule_version_history(rule_id, effective_from, effective_to);
                CREATE TABLE IF NOT EXISTS org_settings (
                    org_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '税海拾珠',
                    report_title TEXT NOT NULL DEFAULT '税务风险审计报告',
                    footer_text TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    logo_mime TEXT, logo_bytes BLOB, logo_updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
                    org_id TEXT NOT NULL, action TEXT NOT NULL,
                    target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_preferences (
                    user_id TEXT PRIMARY KEY REFERENCES users(id),
                    audit_completed INTEGER NOT NULL DEFAULT 0,
                    high_risk INTEGER NOT NULL DEFAULT 0,
                    email_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    org_id TEXT NOT NULL, audit_id TEXT NOT NULL REFERENCES audits(id),
                    event TEXT NOT NULL, summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, read_at TEXT,
                    UNIQUE(user_id,audit_id,event)
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_user
                    ON notifications(user_id,org_id,created_at);
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    notification_id TEXT PRIMARY KEY REFERENCES notifications(id),
                    recipient_email TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT,
                    provider_id TEXT, error_code TEXT, claimed_at TEXT, payload_json TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
                    ON notification_deliveries(status,created_at,notification_id);
                CREATE TABLE IF NOT EXISTS finding_interpretations (
                    audit_id TEXT NOT NULL REFERENCES audits(id),
                    rule_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
                    model TEXT NOT NULL, result_json TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    PRIMARY KEY(audit_id, rule_id, evidence_hash)
                );
                CREATE TABLE IF NOT EXISTS audit_narratives (
                    audit_id TEXT NOT NULL REFERENCES audits(id),
                    evidence_hash TEXT NOT NULL, model TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    PRIMARY KEY(audit_id, evidence_hash)
                );
            """)
            version_columns = {row["name"] for row in db.execute("PRAGMA table_info(rule_version_history)")}
            if "rule_json" not in version_columns:
                db.execute("ALTER TABLE rule_version_history ADD COLUMN rule_json TEXT")
            delivery_columns = {row["name"] for row in db.execute("PRAGMA table_info(notification_deliveries)")}
            if "payload_json" not in delivery_columns:
                db.execute("ALTER TABLE notification_deliveries ADD COLUMN payload_json TEXT")
            # Preserve pre-B12 overrides as undated legacy versions. Existing
            # audits already contain immutable Finding snapshots.
            db.execute("""INSERT OR IGNORE INTO rule_version_history
                       (rule_id,version,effective_from,effective_to,logic_json,
                        threshold_basis,updated_by,updated_at)
                       SELECT rule_id,version,NULL,NULL,logic_json,
                              threshold_basis,updated_by,updated_at FROM rule_overrides""")
            # Existing P1 databases predate configurable organization logos.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(org_settings)")}
            for name, sql_type in (
                ("logo_mime", "TEXT"), ("logo_bytes", "BLOB"), ("logo_updated_at", "TEXT")
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE org_settings ADD COLUMN {name} {sql_type}")
            # 注册与开户（FR-G10/G11）：用户绑定邮箱。email 可空但唯一（部分唯一索引）。
            # 因「用户自设密码」，password_hash 保持 NOT NULL——仅需加列，无需重建表。
            user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if "email" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN email TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS email_tokens (
                    token_hash TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    session_key TEXT,
                    code_hash TEXT,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL
                )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_email_tokens_email ON email_tokens(email, purpose)")
            db.execute("""CREATE TABLE IF NOT EXISTS invite_codes (
                    token_hash TEXT PRIMARY KEY,
                    org_name TEXT NOT NULL,
                    seats INTEGER NOT NULL,
                    bound_email TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    redeemed_by TEXT REFERENCES users(id),
                    redeemed_at TEXT,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    CHECK (redeemed_by IS NULL OR redeemed_at IS NOT NULL)
                )""")
            db.execute("""CREATE TABLE IF NOT EXISTS org_quota (
                    org_id TEXT PRIMARY KEY,
                    seats INTEGER NOT NULL,
                    updated_by TEXT REFERENCES users(id),
                    updated_at TEXT NOT NULL
                )""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
                       "ON users(email) WHERE email IS NOT NULL AND email <> ''")
            count = db.execute("SELECT COUNT(*) FROM users WHERE role='platform_admin'").fetchone()[0]
            if count > 1:
                raise RuntimeError("现有数据库含多个平台管理员，需人工确认归并后才能安装唯一约束；未自动删除账号。")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_platform_admin "
                       "ON users(role) WHERE role='platform_admin'")

    def has_users(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    ORG_DEFAULTS = {
        "org_id": "", "display_name": "税海拾珠", "report_title": "税务风险审计报告",
        "footer_text": "", "logo_mime": None, "logo_updated_at": None, "has_logo": False,
    }

    def get_org_settings(self, org_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """SELECT org_id,display_name,report_title,footer_text,logo_mime,logo_updated_at
                   FROM org_settings WHERE org_id=?""",
                (org_id,),
            ).fetchone()
        if row:
            result = dict(row)
            result["has_logo"] = bool(result["logo_mime"])
            return result
        return {**self.ORG_DEFAULTS, "org_id": org_id}

    def update_org_settings(
        self, org_id: str, display_name: str, report_title: str, footer_text: str
    ) -> dict[str, Any]:
        display_name = display_name.strip()
        report_title = report_title.strip()
        footer_text = footer_text.strip()
        if not display_name or not report_title:
            raise ValueError("机构名称与报告标题不能为空")
        if any(ord(ch) < 32 for ch in display_name + report_title):
            raise ValueError("机构名称与报告标题不能包含控制字符")
        with self.connect() as db:
            db.execute(
                "INSERT INTO org_settings (org_id, display_name, report_title, footer_text, updated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(org_id) DO UPDATE SET display_name=excluded.display_name,"
                " report_title=excluded.report_title, footer_text=excluded.footer_text,"
                " updated_at=excluded.updated_at",
                (org_id, display_name, report_title, footer_text, _now()),
            )
        return self.get_org_settings(org_id)

    def get_org_logo(self, org_id: str) -> tuple[str, bytes] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT logo_mime,logo_bytes FROM org_settings WHERE org_id=?", (org_id,)
            ).fetchone()
        if not row or not row["logo_mime"] or not row["logo_bytes"]:
            return None
        return str(row["logo_mime"]), bytes(row["logo_bytes"])

    def update_org_logo(self, org_id: str, mime: str, content: bytes) -> dict[str, Any]:
        now = _now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO org_settings
                       (org_id,display_name,report_title,footer_text,updated_at,logo_mime,logo_bytes,logo_updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(org_id) DO UPDATE SET logo_mime=excluded.logo_mime,
                       logo_bytes=excluded.logo_bytes,logo_updated_at=excluded.logo_updated_at,
                       updated_at=excluded.updated_at""",
                (org_id, self.ORG_DEFAULTS["display_name"], self.ORG_DEFAULTS["report_title"], "",
                 now, mime, content, now),
            )
        return self.get_org_settings(org_id)

    def clear_org_logo(self, org_id: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                "UPDATE org_settings SET logo_mime=NULL,logo_bytes=NULL,logo_updated_at=NULL,updated_at=? WHERE org_id=?",
                (_now(), org_id),
            )
        return self.get_org_settings(org_id)

    def create_user(self, username: str, password: str, display_name: str, role: str,
                    org_id: str, email: str = "") -> dict[str, Any]:
        username = username.strip().lower()
        if role not in ROLES:
            raise ValueError("无效角色")
        if len(username) < 3 or not display_name.strip() or not org_id.strip():
            raise ValueError("用户名至少3位，姓名和机构不能为空")
        _validate_password(password)
        address = _validate_email(email)  # 可选；非法格式拒绝，空则不绑定
        user_id = secrets.token_hex(12)
        with self.connect() as db:
            db.execute(
                """INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,email,created_at)
                   VALUES (?,?,?,?,?,?,1,?,?)""",
                (user_id, username, _passwords.hash(password), display_name.strip(), role,
                 org_id.strip(), address or None, _now()),
            )
        return self.get_user(user_id)

    def create_initial_admin(self, username: str, password: str, display_name: str,
                             org_id: str, email: str = "") -> dict[str, Any]:
        username = username.strip().lower()
        if len(username) < 3 or not display_name.strip() or not org_id.strip():
            raise ValueError("用户名至少3位，姓名和机构不能为空")
        _validate_password(password)
        address = _validate_email(email)
        user_id = secrets.token_hex(12)
        password_hash = _passwords.hash(password)
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    raise SetupAlreadyInitialized()
                db.execute(
                    """INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,email,created_at)
                       VALUES (?,?,?,?,?,?,1,?,?)""",
                    (user_id, username, password_hash, display_name.strip(), "platform_admin",
                     org_id.strip(), address or None, _now()),
                )
        user = self.get_user(user_id)
        assert user is not None
        return user

    @staticmethod
    def _with_email(row: dict[str, Any]) -> dict[str, Any]:
        # 对外统一为空字符串，避免前端把未绑定邮箱渲染成 null。
        row["email"] = row.get("email") or ""
        return row

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id,username,display_name,role,org_id,active,created_at,email FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return self._with_email(dict(row)) if row else None

    def list_users(self, org_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id,username,display_name,role,org_id,active,created_at,email FROM users"
        args: tuple[Any, ...] = ()
        if org_id:
            sql += " WHERE org_id=?"
            args = (org_id,)
        with self.connect() as db:
            return [self._with_email(dict(row)) for row in db.execute(sql + " ORDER BY created_at", args)]

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        address = _normalize_email(email)
        if not address:
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT id,username,display_name,role,org_id,active,created_at,email
                   FROM users WHERE email=? AND active=1""",
                (address,),
            ).fetchone()
        return self._with_email(dict(row)) if row else None

    def create_invite_code(self, creator: dict[str, Any], org_name: str, seats: int,
                           bound_email: str, expires_days: int = INVITE_CODE_DAYS) -> dict[str, Any]:
        """平台管理员签发一次性创始码。明文只在此刻返回一次。"""
        if creator.get("role") != "platform_admin":
            raise ValueError("只有平台管理员可以签发创始码")
        org_name = (org_name or "").strip()
        address = _validate_email(bound_email)
        if not org_name:
            raise ValueError("机构名称不能为空")
        if not isinstance(seats, int) or not 1 <= seats <= 200:
            raise ValueError("席位须为 1–200 的整数")
        if not address:
            raise ValueError("必须绑定机构负责人的邮箱")
        code = _generate_invite_code(12)
        expires = (datetime.now(UTC) + timedelta(days=expires_days)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                """INSERT INTO invite_codes
                       (token_hash,org_name,seats,bound_email,expires_at,revoked,created_by,created_at)
                   VALUES (?,?,?,?,?,0,?,?)""",
                (_hash_token(_normalize_invite_code(code)), org_name, seats, address, expires,
                 creator["id"], _now()),
            )
        return {"code": code, "org_name": org_name, "seats": seats,
                "bound_email": address, "expires_at": expires}

    def list_invite_codes(self) -> list[dict[str, Any]]:
        """平台管理员查看创始码（不含明文）。"""
        with self.connect() as db:
            rows = db.execute(
                """SELECT token_hash,org_name,seats,bound_email,expires_at,redeemed_by,
                          redeemed_at,revoked,created_at
                   FROM invite_codes ORDER BY created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def register_with_code(self, email: str, code: str, invite_code: str,
                           password: str) -> tuple[dict[str, Any], str]:
        """邮箱验证码 + 创始码注册：单事务完成核验与建号。

        顺序：邮箱验证码（一次性 / 尝试上限）→ 创始码 CAS 核销 → 建机构与
        org_admin（org_id 由系统生成，5.8.2）→ 建席位配额 → 种会话。
        任何一步失败整体回滚。
        """
        _validate_password(password)
        address = _normalize_email(email)
        if not address or "@" not in address:
            raise ValueError("邮箱格式不正确")
        now = _now()
        code_hash = _hash_token((code or "").strip())
        invite_hash = _hash_token(_normalize_invite_code(invite_code))
        # ⚠️ 验证码输错时的尝试计数必须**提交**而非回滚——在 with 块内 raise 会触发
        # 整体回滚（含 attempts 自增），计数永远停在 0。因此错误以标志位记录、
        # 事务提交后再抛出。
        error: str | None = None
        user_id = org_id = session_token = None
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                # ① 邮箱验证码：一次性、10 分钟、5 次尝试上限
                token_row = db.execute(
                    """SELECT token_hash,attempts,expires_at,used_at FROM email_tokens
                       WHERE email=? AND purpose='register' AND used_at IS NULL""",
                    (address,),
                ).fetchone()
                if not token_row or token_row["expires_at"] < now:
                    error = "验证码无效或已过期，请重新获取。"
                elif token_row["token_hash"] != code_hash:
                    attempts = token_row["attempts"] + 1
                    if attempts >= REGISTER_CODE_MAX_ATTEMPTS:
                        db.execute("UPDATE email_tokens SET used_at=?,attempts=? WHERE token_hash=?",
                                   (now, attempts, token_row["token_hash"]))
                        error = "验证码错误次数过多，请重新获取。"
                    else:
                        db.execute("UPDATE email_tokens SET attempts=? WHERE token_hash=?",
                                   (attempts, token_row["token_hash"]))
                        error = f"验证码不正确（还可尝试 {REGISTER_CODE_MAX_ATTEMPTS - attempts} 次）。"
                if error is None:
                    # ② 创始码校验：CAS 核销由数据库层保证只能兑换一次
                    invite = db.execute(
                        """SELECT org_name,seats,bound_email,expires_at,revoked,redeemed_by
                           FROM invite_codes WHERE token_hash=?""",
                        (invite_hash,),
                    ).fetchone()
                    if not invite or invite["revoked"]:
                        error = "邀请码无效。"
                    elif invite["redeemed_by"]:
                        error = "邀请码已被使用。"
                    elif invite["expires_at"] < now:
                        error = "邀请码已过期，请联系平台管理员重新签发。"
                    elif _normalize_email(invite["bound_email"]) != address:
                        error = "该创始码绑定的是其他邮箱，请使用绑定的邮箱注册。"
                    elif db.execute("SELECT 1 FROM users WHERE email=?", (address,)).fetchone():
                        error = "该邮箱已注册，请直接登录。"  # 一个邮箱 = 一个账号 = 一个机构（5.8.3）
                if error is None:
                    # ③ 建机构与 org_admin：org_id 由系统生成（5.8.2），角色由层级硬编码（5.8.1）
                    org_id = "org-" + secrets.token_hex(6)
                    user_id = secrets.token_hex(12)
                    username = _username_from_email(address, db)
                    db.execute(
                        """INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,email,created_at)
                           VALUES (?,?,?,?,?,?,1,?,?)""",
                        (user_id, username, _passwords.hash(password), invite["org_name"], "org_admin",
                         org_id, address, now),
                    )
                    db.execute(
                        "INSERT INTO org_quota (org_id,seats,updated_at) VALUES (?,?,?)",
                        (org_id, invite["seats"], now),
                    )
                    db.execute("UPDATE invite_codes SET redeemed_by=?,redeemed_at=? WHERE token_hash=?",
                               (user_id, now, invite_hash))
                    db.execute("UPDATE email_tokens SET used_at=? WHERE token_hash=?",
                               (now, token_row["token_hash"]))
                    session_token = secrets.token_urlsafe(32)
                    expires = (datetime.now(UTC) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
                    db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                               (_hash_token(session_token), user_id, expires, now))
        if error:
            raise ValueError(error)
        user = self.get_user(user_id)
        assert user is not None
        self.log(user, "register", "user", user["id"], f"invite={invite_hash[:12]}…;org={org_id}")
        return user, session_token

    def create_password_reset(self, email: str) -> str | None:
        """为已激活用户生成一次性重置令牌；邮箱不存在时返回 None。

        调用方必须保证：无论返回令牌还是 None，对外的响应完全一致（防账号枚举，3.5）。
        同一邮箱同时只保留一张未使用的重置令牌，新申请会作废旧令牌。
        """
        address = _normalize_email(email)
        if not address or not self.get_user_by_email(address):
            return None
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(minutes=PASSWORD_RESET_MINUTES)).isoformat(timespec="seconds")
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM email_tokens WHERE email=? AND purpose='reset' AND used_at IS NULL",
                           (address,))
                db.execute(
                    """INSERT INTO email_tokens (token_hash,email,purpose,attempts,expires_at,used_at,created_at)
                       VALUES (?,?,?,0,?,NULL,?)""",
                    (_hash_token(token), address, "reset", expires, _now()),
                )
        return token

    def create_register_code(self, email: str) -> str | None:
        """生成 6 位注册验证码；邮箱格式非法返回 None。旧验证码随即作废。"""
        address = _validate_email(_normalize_email(email))
        if not address:
            return None
        code = f"{secrets.randbelow(1000000):06d}"
        expires = (datetime.now(UTC) + timedelta(minutes=REGISTER_CODE_MINUTES)).isoformat(timespec="seconds")
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM email_tokens WHERE email=? AND purpose='register' AND used_at IS NULL",
                           (address,))
                db.execute(
                    """INSERT INTO email_tokens (token_hash,email,purpose,attempts,expires_at,used_at,created_at)
                       VALUES (?,?,?,0,?,NULL,?)""",
                    (_hash_token(code), address, "register", expires, _now()),
                )
        return code

    def redeem_password_reset(self, token: str, new_password: str) -> dict[str, Any]:
        """用一次性重置令牌设置新密码；成功后该账号全部历史会话失效。"""
        _validate_password(new_password)
        token_hash = _hash_token((token or "").strip())
        now = _now()
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT email,expires_at,used_at FROM email_tokens WHERE token_hash=? AND purpose='reset'",
                    (token_hash,),
                ).fetchone()
                if not row or row["used_at"] or row["expires_at"] < now:
                    raise ValueError("重置链接无效或已过期，请重新申请。")
                user = db.execute(
                    """SELECT id,username,display_name,role,org_id,active,created_at,email
                       FROM users WHERE email=? AND active=1""",
                    (row["email"],),
                ).fetchone()
                if not user:
                    raise ValueError("重置链接无效或已过期，请重新申请。")
                db.execute("UPDATE users SET password_hash=? WHERE id=?",
                           (_passwords.hash(new_password), user["id"]))
                db.execute("UPDATE email_tokens SET used_at=? WHERE token_hash=?", (now, token_hash))
                db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        record = {"id": user["id"], "org_id": user["org_id"]}
        self.log(record, "password_reset", "user", user["id"])
        result = self.get_user(user["id"])
        assert result is not None
        return result

    def authenticate(self, username: str, password: str) -> tuple[dict[str, Any], str] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=? AND active=1", (username.strip().lower(),)).fetchone()
            if not row:
                return None
            try:
                _passwords.verify(row["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                return None
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(UTC) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (_hash_token(token), row["id"], expires, _now()))
        user = self.get_user(row["id"])
        assert user is not None
        return user, token

    def user_for_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT u.id,u.username,u.display_name,u.role,u.org_id,u.active,u.created_at
                   FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>=? AND u.active=1""",
                (_hash_token(token), _now()),
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))

    def log(self, user: dict[str, Any] | None, action: str, target_type: str, target_id: str, detail: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_log(user_id,org_id,action,target_type,target_id,detail,created_at) VALUES (?,?,?,?,?,?,?)",
                (user["id"] if user else None, user["org_id"] if user else "system", action, target_type, target_id, detail, _now()),
            )

    def list_logs(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as db:
            if user["role"] == "platform_admin":
                rows = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300")
            else:
                rows = db.execute("SELECT * FROM audit_log WHERE org_id=? ORDER BY id DESC LIMIT 300", (user["org_id"],))
            return [dict(row) for row in rows]

    def upsert_client(self, user: dict[str, Any], name: str, taxpayer_id: str, accountant_id: str | None = None) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM clients WHERE org_id=? AND taxpayer_id=?", (user["org_id"], taxpayer_id)).fetchone()
            if row:
                db.execute("UPDATE clients SET name=?, accountant_id=COALESCE(?,accountant_id) WHERE id=?", (name, accountant_id, row["id"]))
                client_id = row["id"]
            else:
                client_id = secrets.token_hex(12)
                db.execute("INSERT INTO clients VALUES (?,?,?,?,?,?)", (client_id, user["org_id"], name, taxpayer_id, accountant_id, _now()))
        return self.get_client(client_id)

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        return dict(row) if row else None

    def list_clients(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as db:
            if user["role"] == "accountant":
                rows = db.execute(
                    """SELECT c.*,u.display_name AS accountant_name,u.username AS accountant_username
                       FROM clients c LEFT JOIN users u ON u.id=c.accountant_id
                       WHERE c.org_id=? AND c.accountant_id=? ORDER BY c.name""",
                    (user["org_id"], user["id"]),
                )
            else:
                rows = db.execute(
                    """SELECT c.*,u.display_name AS accountant_name,u.username AS accountant_username
                       FROM clients c LEFT JOIN users u ON u.id=c.accountant_id
                       WHERE c.org_id=? ORDER BY c.name""",
                    (user["org_id"],),
                )
            return [dict(row) for row in rows]

    def notification_preferences(self, user_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
        return {"user_id": user_id, "audit_completed": bool(row["audit_completed"]) if row else False,
                "high_risk": bool(row["high_risk"]) if row else False,
                "email_enabled": bool(row["email_enabled"]) if row else False}

    def set_notification_preferences(self, actor: dict[str, Any], user_id: str,
                                     audit_completed: bool, high_risk: bool, email_enabled: bool) -> dict[str, Any]:
        from webapp.notifications import RECIPIENT_ROLES

        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                target = db.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
                if not target or target["role"] not in RECIPIENT_ROLES:
                    raise ValueError("接收人不存在、已停用或角色不支持审计通知。")
                if actor["id"] != user_id and not (actor["role"] == "platform_admin" or
                        actor["role"] == "org_admin" and actor["org_id"] == target["org_id"]):
                    raise PermissionError("无权配置该接收人的通知。")
                current = db.execute("SELECT email_enabled FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
                if actor["id"] != user_id and email_enabled != bool(current["email_enabled"] if current else False):
                    raise PermissionError("邮件通知必须由接收人本人开启或关闭。")
                if email_enabled and not target["email"]:
                    raise ValueError("请先绑定本人邮箱，再开启邮件通知。")
                db.execute("""INSERT INTO notification_preferences VALUES (?,?,?,?,?,?)
                           ON CONFLICT(user_id) DO UPDATE SET audit_completed=excluded.audit_completed,
                               high_risk=excluded.high_risk,email_enabled=excluded.email_enabled,
                               updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                           (user_id, int(audit_completed), int(high_risk), int(email_enabled), actor["id"], _now()))
                # A queued message is cancelled immediately on unsubscribe.
                # Already claimed/accepted messages cannot be retracted.
                db.execute("""UPDATE notification_deliveries SET status='suppressed',error_code='unsubscribed',updated_at=?
                           WHERE status IN ('pending','failed') AND notification_id IN
                             (SELECT id FROM notifications WHERE user_id=? AND
                              (?=0 OR event='audit_completed' AND ?=0 OR event='high_risk' AND ?=0))""",
                           (_now(), user_id, int(email_enabled), int(audit_completed), int(high_risk)))
        return self.notification_preferences(user_id)

    def list_notifications(self, user: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        from webapp.notifications import RECIPIENT_ROLES

        if user["role"] not in RECIPIENT_ROLES:
            return []
        where = "n.user_id=? AND n.org_id=? AND a.org_id=?"
        args: list[Any] = [user["id"], user["org_id"], user["org_id"]]
        if user["role"] == "accountant":
            where += " AND c.org_id=? AND c.accountant_id=?"
            args.extend([user["org_id"], user["id"]])
        args.append(max(1, min(limit, 100)))
        with self.connect() as db:
            rows = db.execute(f"""SELECT n.*,d.status AS email_status FROM notifications n
                              JOIN audits a ON a.id=n.audit_id LEFT JOIN clients c ON c.id=a.client_id
                              LEFT JOIN notification_deliveries d ON d.notification_id=n.id
                              WHERE {where} ORDER BY n.created_at DESC,n.id LIMIT ?""", args).fetchall()
        return [{**{key: row[key] for key in ("id", "audit_id", "event", "created_at", "read_at", "email_status")},
                 "summary": json.loads(row["summary_json"])} for row in rows]

    def mark_notification_read(self, user: dict[str, Any], notification_id: str) -> bool:
        from webapp.notifications import RECIPIENT_ROLES

        if user["role"] not in RECIPIENT_ROLES:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT n.id,c.org_id AS client_org,c.accountant_id FROM notifications n
                               JOIN audits a ON a.id=n.audit_id LEFT JOIN clients c ON c.id=a.client_id
                               WHERE n.id=? AND n.user_id=? AND n.org_id=? AND a.org_id=?""",
                             (notification_id, user["id"], user["org_id"], user["org_id"])).fetchone()
            if not row or user["role"] == "accountant" and (row["client_org"] != user["org_id"] or row["accountant_id"] != user["id"]):
                return False
            result = db.execute("""UPDATE notifications SET read_at=COALESCE(read_at,?)
                                  WHERE id=? AND user_id=? AND org_id=?""",
                                (_now(), notification_id, user["id"], user["org_id"]))
            return result.rowcount == 1

    def claim_notification_delivery(self) -> dict[str, Any] | None:
        """Atomically claim one pending email, rechecking consent and current access."""
        from webapp.notifications import RECIPIENT_ROLES, email_content
        from src.mailer import _from_header

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT d.*,n.user_id,n.org_id,n.audit_id,n.event,n.summary_json,
                          u.active,u.role,u.org_id AS user_org,u.email,
                          p.audit_completed,p.high_risk,p.email_enabled,a.org_id AS audit_org,
                          c.org_id AS client_org,c.accountant_id
                          FROM notification_deliveries d JOIN notifications n ON n.id=d.notification_id
                          JOIN users u ON u.id=n.user_id JOIN audits a ON a.id=n.audit_id
                          LEFT JOIN notification_preferences p ON p.user_id=u.id
                          LEFT JOIN clients c ON c.id=a.client_id
                          WHERE d.status='pending' ORDER BY d.created_at,d.notification_id LIMIT 100""").fetchall()
            for row in rows:
                authorized = (row["active"] and row["role"] in RECIPIENT_ROLES and
                              row["user_org"] == row["org_id"] == row["audit_org"])
                if row["role"] == "accountant":
                    authorized = authorized and row["client_org"] == row["org_id"] and row["accountant_id"] == row["user_id"]
                consent = row["email_enabled"] and row[row["event"]] and row["email"] == row["recipient_email"]
                if not authorized or not consent:
                    db.execute("""UPDATE notification_deliveries SET status='suppressed',
                                  error_code='recipient_unavailable',updated_at=? WHERE notification_id=? AND status='pending'""",
                               (_now(), row["notification_id"]))
                    continue
                token = secrets.token_hex(16)
                payload = json.loads(row["payload_json"]) if row["payload_json"] else None
                if payload is None:
                    subject, html, text = email_content(json.loads(row["summary_json"]))
                    payload = {"subject": subject, "html": html, "text": text, "from_header": _from_header()}
                db.execute("""UPDATE notification_deliveries SET status='claimed',claim_token=?,
                              attempts=attempts+1,claimed_at=?,updated_at=?,payload_json=? WHERE notification_id=? AND status='pending'""",
                           (token, _now(), _now(), _json(payload), row["notification_id"]))
                return {"notification_id": row["notification_id"], "claim_token": token,
                        "recipient_email": row["recipient_email"], "summary": json.loads(row["summary_json"]), "payload": payload}
        return None

    def finish_notification_delivery(self, notification_id: str, claim_token: str, status: str,
                                     provider_id: str = "", error_code: str = "") -> bool:
        if (status not in {"accepted", "failed", "uncertain"} or status == "accepted" and not provider_id
                or len(provider_id) > 128 or not re.fullmatch(r"[a-z0-9_]{0,40}", error_code)):
            raise ValueError("通知发送回执无效。")
        with self.connect() as db:
            result = db.execute("""UPDATE notification_deliveries SET status=?,provider_id=?,error_code=?,
                                  updated_at=? WHERE notification_id=? AND status='claimed' AND claim_token=?""",
                                (status, provider_id or None, error_code or None, _now(), notification_id, claim_token))
            return result.rowcount == 1

    def recover_notification_claims(self) -> int:
        """A crashed send may have reached the provider: never automatically resend it."""
        threshold = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with self.connect() as db:
            result = db.execute("""UPDATE notification_deliveries SET status='uncertain',error_code='interrupted',updated_at=?
                                  WHERE status='claimed' AND claimed_at<?""", (_now(), threshold))
            return result.rowcount

    def retry_notification_delivery(self, user: dict[str, Any], notification_id: str) -> bool:
        from webapp.notifications import RECIPIENT_ROLES

        if user["role"] not in RECIPIENT_ROLES:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT d.*,c.org_id AS client_org,c.accountant_id
                               FROM notification_deliveries d JOIN notifications n ON n.id=d.notification_id
                               JOIN audits a ON a.id=n.audit_id LEFT JOIN clients c ON c.id=a.client_id
                               WHERE n.id=? AND n.user_id=? AND n.org_id=? AND a.org_id=?""",
                             (notification_id, user["id"], user["org_id"], user["org_id"])).fetchone()
            if not row or user["role"] == "accountant" and (row["client_org"] != user["org_id"] or row["accountant_id"] != user["id"]):
                return False
            if row["status"] not in {"failed", "uncertain"}:
                raise ValueError("只有失败或结果未知的邮件可申请重试。")
            cutoff = (datetime.now(UTC) - timedelta(hours=23)).isoformat(timespec="seconds")
            if row["attempts"] >= 3 or row["created_at"] < cutoff:
                raise ValueError("已超出安全重试次数或 23 小时窗口；请人工核查，不再重发。")
            db.execute("""UPDATE notification_deliveries SET status='pending',claim_token=NULL,
                          provider_id=NULL,error_code=NULL,updated_at=? WHERE notification_id=?""", (_now(), notification_id))
            return True

    def _enqueue_audit_notifications(self, db, org_id: str, client_id: str | None,
                                     audit_id: str, findings: list[Finding], when: str) -> None:
        from webapp.notifications import RECIPIENT_ROLES, safe_summary, email_content
        from src.mailer import _from_header

        members = db.execute("""SELECT u.id,u.role,u.email,p.audit_completed,p.high_risk,p.email_enabled
                              FROM notification_preferences p JOIN users u ON u.id=p.user_id
                              WHERE u.active=1 AND u.org_id=?""", (org_id,)).fetchall()
        client = db.execute("SELECT org_id,accountant_id FROM clients WHERE id=?", (client_id,)).fetchone() if client_id else None
        high = any(item.status == "hit" and item.rule.severity == "high" for item in findings)
        for member in members:
            if member["role"] not in RECIPIENT_ROLES:
                continue
            if member["role"] == "accountant" and (not client or client["org_id"] != org_id or client["accountant_id"] != member["id"]):
                continue
            for event in ("audit_completed", "high_risk"):
                if not member[event] or event == "high_risk" and not high:
                    continue
                key = hashlib.sha256(f"{member['id']}:{audit_id}:{event}".encode()).hexdigest()
                summary = safe_summary(audit_id, findings, event, when)
                created = _now()
                db.execute("""INSERT OR IGNORE INTO notifications VALUES (?,?,?,?,?,?,?,NULL)""",
                           (key, member["id"], org_id, audit_id, event, _json(summary), created))
                if member["email_enabled"] and member["email"]:
                    subject, html, text = email_content(summary)
                    payload = {"subject": subject, "html": html, "text": text, "from_header": _from_header()}
                    db.execute("""INSERT OR IGNORE INTO notification_deliveries
                               (notification_id,recipient_email,created_at,updated_at,payload_json) VALUES (?,?,?,?,?)""",
                               (key, member["email"], created, created, _json(payload)))

    def save_audit(self, audit_id: str, user: dict[str, Any], client_id: str | None,
                   dataset: Dataset, findings: list[Finding], summary: dict[str, Any], audited_at: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (audit_id, user["org_id"], client_id, user["id"], dataset.company.name,
                 dataset.company.taxpayer_id, dataset.company.industry, dataset.company.period,
                 _json(serialize_dataset(dataset)), _json(serialize_findings(findings)),
                 _json(summary), audited_at),
            )
            self._enqueue_audit_notifications(db, user["org_id"], client_id, audit_id, findings, audited_at)

    def get_audit(self, audit_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM audits WHERE id=?", (audit_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["dataset"] = deserialize_dataset(json.loads(item.pop("dataset_json")))
        item["findings"] = deserialize_findings(json.loads(item.pop("findings_json")))
        item["summary"] = json.loads(item.pop("summary_json"))
        return item

    def list_audits(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        args: list[Any] = [user["org_id"]]
        where = "a.org_id=?"
        if user["role"] == "accountant":
            where += " AND c.accountant_id=?"
            args.append(user["id"])
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT a.id,a.company_name,a.taxpayer_id,a.industry,a.period,a.audited_at,
                           a.summary_json,a.client_id
                    FROM audits a LEFT JOIN clients c ON c.id=a.client_id
                    WHERE {where} ORDER BY a.audited_at DESC, a.rowid DESC""", args,
            )
            result = []
            for row in rows:
                item = dict(row)
                item["summary"] = json.loads(item.pop("summary_json"))
                result.append(item)
            return result

    def get_finding_interpretation(self, audit_id: str, rule_id: str,
                                   evidence_hash: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT model,result_json,created_at FROM finding_interpretations
                   WHERE audit_id=? AND rule_id=? AND evidence_hash=?""",
                (audit_id, rule_id, evidence_hash),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["result_json"])
        return {**result, "model": row["model"], "created_at": row["created_at"], "cached": True}

    def save_finding_interpretation(self, audit_id: str, rule_id: str,
                                    evidence_hash: str, result: dict[str, Any],
                                    user_id: str) -> dict[str, Any]:
        stored = {key: value for key, value in result.items() if key not in {"model", "cached", "created_at"}}
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO finding_interpretations
                   (audit_id,rule_id,evidence_hash,model,result_json,created_by,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (audit_id, rule_id, evidence_hash, result["model"], _json(stored), user_id, _now()),
            )
        return self.get_finding_interpretation(audit_id, rule_id, evidence_hash)

    def get_audit_narrative(self, audit_id: str, evidence_hash: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT model,result_json,created_at FROM audit_narratives
                   WHERE audit_id=? AND evidence_hash=?""",
                (audit_id, evidence_hash),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["result_json"])
        return {**result, "model": row["model"], "created_at": row["created_at"], "cached": True}

    def save_audit_narrative(self, audit_id: str, evidence_hash: str,
                             result: dict[str, Any], user_id: str) -> dict[str, Any]:
        stored = {key: value for key, value in result.items() if key not in {"model", "cached", "created_at"}}
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO audit_narratives
                   (audit_id,evidence_hash,model,result_json,created_by,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (audit_id, evidence_hash, result["model"], _json(stored), user_id, _now()),
            )
        return self.get_audit_narrative(audit_id, evidence_hash)

    def create_assignment(self, user: dict[str, Any], title: str, audit_id: str,
                          target_student_id: str | None, weights: dict[str, float],
                          false_positive_penalty: float, published: bool) -> str:
        assignment_id = secrets.token_hex(12)
        with self.connect() as db:
            db.execute(
                "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?,?,?)",
                (assignment_id, user["org_id"], title, audit_id, user["id"], target_student_id,
                 _json(weights), false_positive_penalty, int(published), _now()),
            )
        return assignment_id

    def list_assignments(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as db:
            if user["role"] == "student":
                rows = db.execute(
                    """SELECT * FROM assignments WHERE org_id=? AND published=1
                       AND (target_student_id IS NULL OR target_student_id=?) ORDER BY created_at DESC""",
                    (user["org_id"], user["id"]),
                )
            else:
                rows = db.execute("SELECT * FROM assignments WHERE org_id=? ORDER BY created_at DESC", (user["org_id"],))
            return [self._assignment_dict(dict(row)) for row in rows]

    def get_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        return self._assignment_dict(dict(row)) if row else None

    @staticmethod
    def _assignment_dict(item: dict[str, Any]) -> dict[str, Any]:
        item["weights"] = json.loads(item.pop("weights_json"))
        item["published"] = bool(item["published"])
        return item

    def save_submission(self, assignment_id: str, student_id: str, answers: list[str],
                        score: float, details: dict[str, Any]) -> str:
        submission_id = secrets.token_hex(12)
        with self.connect() as db:
            db.execute(
                """INSERT INTO submissions(id,assignment_id,student_id,answers_json,score,details_json,submitted_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(assignment_id,student_id) DO UPDATE SET
                     answers_json=excluded.answers_json, score=excluded.score,
                     details_json=excluded.details_json, submitted_at=excluded.submitted_at,
                     adjusted_score=NULL, feedback=NULL, reviewed_by=NULL""",
                (submission_id, assignment_id, student_id, _json(answers), score, _json(details), _now()),
            )
        return submission_id

    def get_submission(self, assignment_id: str, student_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM submissions WHERE assignment_id=? AND student_id=?", (assignment_id, student_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["answers"] = json.loads(item.pop("answers_json"))
        item["details"] = json.loads(item.pop("details_json"))
        return item

    def list_submissions(self, org_id: str, assignment_id: str | None = None) -> list[dict[str, Any]]:
        where, args = "a.org_id=?", [org_id]
        if assignment_id:
            where += " AND s.assignment_id=?"
            args.append(assignment_id)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT s.*,u.display_name,a.title FROM submissions s
                    JOIN assignments a ON a.id=s.assignment_id
                    JOIN users u ON u.id=s.student_id
                    WHERE {where} ORDER BY s.submitted_at DESC""", args,
            )
            result = []
            for row in rows:
                item = dict(row)
                item["answers"] = json.loads(item.pop("answers_json"))
                item["details"] = json.loads(item.pop("details_json"))
                result.append(item)
            return result

    def submission_org(self, submission_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT a.org_id FROM submissions s JOIN assignments a ON a.id=s.assignment_id
                   WHERE s.id=?""", (submission_id,),
            ).fetchone()
        return row["org_id"] if row else None

    def review_submission(self, submission_id: str, teacher_id: str, adjusted_score: float, feedback: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE submissions SET adjusted_score=?,feedback=?,reviewed_by=? WHERE id=?",
                       (adjusted_score, feedback, teacher_id, submission_id))

    def enabled_rule_ids(self) -> set[str] | None:
        with self.connect() as db:
            rows = list(db.execute("SELECT rule_id,enabled FROM rule_state"))
        if not rows:
            return None
        return {row["rule_id"] for row in rows if row["enabled"]}

    def set_rule_enabled(self, rule_id: str, enabled: bool, user_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO rule_state VALUES (?,?,?,?)
                   ON CONFLICT(rule_id) DO UPDATE SET enabled=excluded.enabled,
                     updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (rule_id, int(enabled), user_id, _now()),
            )

    def rule_overrides(self) -> dict[str, dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT o.rule_id,o.version,o.logic_json,o.threshold_basis,o.updated_by,o.updated_at,
                          h.effective_from,h.effective_to
                   FROM rule_overrides o LEFT JOIN rule_version_history h
                     ON h.rule_id=o.rule_id AND h.version=o.version"""
            )
            return {
                row["rule_id"]: {
                    "version": row["version"],
                    "logic": json.loads(row["logic_json"]),
                    "threshold_basis": row["threshold_basis"],
                    "updated_by": row["updated_by"],
                    "updated_at": row["updated_at"],
                    "effective_from": row["effective_from"],
                    "effective_to": row["effective_to"],
                }
                for row in rows
            }

    def rule_versions(self, rule_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as db:
            if rule_id is None:
                rows = db.execute("""SELECT * FROM rule_version_history
                                     ORDER BY rule_id,id""").fetchall()
            else:
                rows = db.execute("""SELECT * FROM rule_version_history
                                     WHERE rule_id=? ORDER BY id""", (rule_id,)).fetchall()
        versions: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            versions.setdefault(row["rule_id"], []).append({
                "version": row["version"], "effective_from": row["effective_from"],
                "effective_to": row["effective_to"], "logic": json.loads(row["logic_json"]),
                "threshold_basis": row["threshold_basis"], "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
                "rule": json.loads(row["rule_json"]) if row["rule_json"] else None,
            })
        return versions

    def set_rule_override(
        self, rule_id: str, version: str, logic: dict[str, Any],
        threshold_basis: str, user_id: str, expected_version: str,
        effective_from: str | None = None, effective_to: str | None = None,
        rule_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if rule_snapshot is not None and (rule_snapshot.get("id") != rule_id or rule_snapshot.get("version") != version
                or rule_snapshot.get("logic") != logic or rule_snapshot.get("threshold_basis") != threshold_basis
                or rule_snapshot.get("effective_from") != effective_from or rule_snapshot.get("effective_to") != effective_to):
            raise ValueError("规则快照与发布参数不一致。")
        if effective_to and not effective_from:
            raise ValueError("规则终止日期必须同时提供起始日期。")
        for value in (effective_from, effective_to):
            if value:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError("规则生效日期须为 YYYY-MM-DD。")
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise ValueError("规则生效日期须为有效的 YYYY-MM-DD。") from None
        if effective_from and effective_to and effective_from > effective_to:
            raise ValueError("规则生效终止日不能早于起始日。")
        updated_at = _now()
        closed_version = closed_on = None
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT version FROM rule_overrides WHERE rule_id=?", (rule_id,)).fetchone()
                if current and current["version"] != expected_version:
                    raise ValueError("规则版本已变化，请刷新后重试。")
                existing = db.execute("""SELECT version,effective_from,effective_to
                                         FROM rule_version_history WHERE rule_id=?""", (rule_id,)).fetchall()
                if any(row["version"] == version for row in existing):
                    raise ValueError("该规则版本号已使用，请填写更高版本。")
                if not effective_from and any(row["effective_from"] for row in existing):
                    raise ValueError("已有定期生效版本，后续版本必须填写生效起始日。")
                if effective_from:
                    latest_start = max((row["effective_from"] for row in existing if row["effective_from"]), default=None)
                    upper = effective_to or "9999-12-31"
                    closing = []
                    for row in existing:
                        if row["effective_from"] and effective_from <= (row["effective_to"] or "9999-12-31") and row["effective_from"] <= upper:
                            if row["effective_to"] is None and row["effective_from"] < effective_from:
                                closing.append(row["version"])
                            else:
                                raise ValueError(f"生效区间与 v{row['version']} 重叠。")
                    if latest_start and effective_from <= latest_start:
                        raise ValueError("新版本生效起始日须晚于既有定期版本。")
                    if len(closing) > 1:
                        raise ValueError("既有规则生效区间重叠，请人工修复后再保存。")
                    if closing:
                        previous_end = (date.fromisoformat(effective_from) - timedelta(days=1)).isoformat()
                        closed_version, closed_on = closing[0], previous_end
                        db.execute("""UPDATE rule_version_history SET effective_to=?
                                      WHERE rule_id=? AND version=? AND effective_to IS NULL""",
                                   (previous_end, rule_id, closing[0]))
                db.execute("""INSERT INTO rule_version_history
                           (rule_id,version,effective_from,effective_to,logic_json,
                            threshold_basis,updated_by,updated_at,rule_json)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                           (rule_id, version, effective_from, effective_to, _json(logic),
                            threshold_basis, user_id, updated_at,
                            _json(rule_snapshot) if rule_snapshot is not None else None))
                db.execute(
                    """INSERT INTO rule_overrides
                           (rule_id,version,logic_json,threshold_basis,updated_by,updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(rule_id) DO UPDATE SET version=excluded.version,
                           logic_json=excluded.logic_json,threshold_basis=excluded.threshold_basis,
                           updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                    (rule_id, version, _json(logic), threshold_basis, user_id, updated_at),
                )
        return {
            "rule_id": rule_id, "version": version, "logic": logic,
            "threshold_basis": threshold_basis, "updated_by": user_id,
            "updated_at": updated_at, "effective_from": effective_from,
            "effective_to": effective_to,
            "closed_version": closed_version, "closed_on": closed_on,
        }
