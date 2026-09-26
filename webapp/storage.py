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
import secrets
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any
from contextlib import contextmanager

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from src.models import Account, Company, Dataset, EvidenceItem, Finding, Metric, Rule

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "instance" / "taxpearls.db"
SESSION_HOURS = 12
ROLES = {"teacher", "student", "org_admin", "accountant", "platform_admin"}

_passwords = PasswordHasher()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
        self.path = Path(path or configured or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._init_schema()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self.connect() as db:
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
                CREATE TABLE IF NOT EXISTS org_settings (
                    org_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '税海拾珠',
                    report_title TEXT NOT NULL DEFAULT '税务风险审计报告',
                    footer_text TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
                    org_id TEXT NOT NULL, action TEXT NOT NULL,
                    target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def has_users(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    ORG_DEFAULTS = {"org_id": "", "display_name": "税海拾珠", "report_title": "税务风险审计报告", "footer_text": ""}

    def get_org_settings(self, org_id: str) -> dict[str, str]:
        with self.connect() as db:
            row = db.execute(
                "SELECT org_id, display_name, report_title, footer_text FROM org_settings WHERE org_id=?",
                (org_id,),
            ).fetchone()
        if row:
            return dict(row)
        return {**self.ORG_DEFAULTS, "org_id": org_id}

    def update_org_settings(
        self, org_id: str, display_name: str, report_title: str, footer_text: str
    ) -> dict[str, str]:
        display_name = display_name.strip()
        report_title = report_title.strip()
        if not display_name or not report_title:
            raise ValueError("机构名称与报告标题不能为空")
        with self.connect() as db:
            db.execute(
                "INSERT INTO org_settings (org_id, display_name, report_title, footer_text, updated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(org_id) DO UPDATE SET display_name=excluded.display_name,"
                " report_title=excluded.report_title, footer_text=excluded.footer_text,"
                " updated_at=excluded.updated_at",
                (org_id, display_name, report_title, footer_text.strip(), _now()),
            )
        return self.get_org_settings(org_id)

    def create_user(self, username: str, password: str, display_name: str, role: str, org_id: str) -> dict[str, Any]:
        username = username.strip().lower()
        if role not in ROLES:
            raise ValueError("无效角色")
        if len(username) < 3 or not password.strip() or not display_name.strip() or not org_id.strip():
            raise ValueError("用户名至少3位，密码、姓名和机构不能为空")
        user_id = secrets.token_hex(12)
        with self.connect() as db:
            db.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,?,1,?)",
                (user_id, username, _passwords.hash(password), display_name.strip(), role, org_id.strip(), _now()),
            )
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id,username,display_name,role,org_id,active,created_at FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self, org_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id,username,display_name,role,org_id,active,created_at FROM users"
        args: tuple[Any, ...] = ()
        if org_id:
            sql += " WHERE org_id=?"
            args = (org_id,)
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY created_at", args)]

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
                rows = db.execute("SELECT * FROM clients WHERE org_id=? AND accountant_id=? ORDER BY name", (user["org_id"], user["id"]))
            else:
                rows = db.execute("SELECT * FROM clients WHERE org_id=? ORDER BY name", (user["org_id"],))
            return [dict(row) for row in rows]

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
