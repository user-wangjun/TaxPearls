"""考证刷题线学生端（FR-K01/K02）：证书目录、学习目标与考试倒计时。

学生身份独立于平台 users 账号：以 student_info（学号 + 密码）登录，
会话存 training_student_sessions 表，使用独立 cookie，与主站会话互不影响。
倒计时按中国日历日计算且永不为负；考试日期过期仅作标记，绝不自动修改
目标状态（验收：不自动判定目标达成）；目标修改保留 created_at 历史，
归档不删行（验收：修改范围不丢失历史记录）。

需求基线：docs/07-高校考证刷题线需求.md（TP-CERT-001，university-extension 分支）。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Cookie, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp.login_guard import LoginGuard
from webapp.storage import _hash_token, _now, session_max_age

STATIC_DIR_KEYS = ("training-portal.html", "training-portal.js")

_passwords = PasswordHasher()
CST = timezone(timedelta(hours=8))  # 考试与倒计时按中国日历日（无夏令时）
STATIC_DIR = Path(__file__).resolve().parent / "static"
login_guard = LoginGuard()


class TrainingPortalError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def today_cst() -> date:
    return datetime.now(CST).date()


def countdown(target: str | None) -> dict[str, Any] | None:
    """倒计时只到日；剩余天数最小为 0，过期以 expired 标记（不出现负数）。"""
    if not target:
        return None
    try:
        day = date.fromisoformat(str(target)[:10])
    except (TypeError, ValueError):
        return None
    delta = (day - today_cst()).days
    return {"target_date": day.isoformat(), "days_left": max(delta, 0), "expired": delta < 0}


def _parse_date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise TrainingPortalError("日期格式必须是 YYYY-MM-DD。") from None


def _student_public(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "student_no": row["student_no"], "name": row["name"],
            "college": row["college"], "class_name": row["class_name"]}


# ---------------------------------------------------------------------------
# 学生会话
# ---------------------------------------------------------------------------

def login_student(store, student_no: str, password: str, college: str | None = None,
                  remember: bool = False) -> dict[str, Any] | None:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT * FROM student_info WHERE student_no=? AND active=1", (student_no.strip(),)
        ).fetchall()
        if college:
            rows = [r for r in rows if r["college"] == college.strip()]
        if not rows:
            return None
        # 先验密码再谈歧义：全部失败一律 401，不泄露学号存在性
        verified = []
        for row in rows:
            try:
                _passwords.verify(row["password_hash"], password)
                verified.append(row)
            except (VerifyMismatchError, InvalidHashError):
                continue
        if not verified:
            return None
        if len(verified) > 1:
            raise TrainingPortalError("该学号在多个院校存在，请在登录时填写院校名称。")
        row = verified[0]
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(seconds=session_max_age(remember))).isoformat(timespec="seconds")
        db.execute("DELETE FROM training_student_sessions WHERE expires_at < ?", (_now(),))
        db.execute("INSERT INTO training_student_sessions VALUES (?,?,?,?)",
                   (_hash_token(token), row["id"], expires, _now()))
        return {"student": _student_public(row), "token": token}


def student_for_token(store, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with store.connect() as db:
        row = db.execute(
            """SELECT s.id, s.student_no, s.name, s.college, s.class_name, s.active
               FROM training_student_sessions t JOIN student_info s ON s.id = t.student_id
               WHERE t.token_hash=? AND t.expires_at>=?""",
            (_hash_token(token), _now()),
        ).fetchone()
    if not row or not row["active"]:
        return None
    return _student_public(row)


def logout_student(store, token: str | None) -> None:
    if token:
        with store.connect() as db:
            db.execute("DELETE FROM training_student_sessions WHERE token_hash=?", (_hash_token(token),))


# ---------------------------------------------------------------------------
# 证书目录（FR-K01）
# ---------------------------------------------------------------------------

def list_certificates(store, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    with store.connect() as db:
        where = "" if include_inactive else "WHERE active=1"
        certs = [dict(r) for r in db.execute(
            f"SELECT * FROM certificate {where} ORDER BY created_at").fetchall()]
        for cert in certs:
            cert["subjects"] = json.loads(cert.pop("subjects_json") or "[]")
            dates = db.execute(
                """SELECT id, round_label, date_type, exam_date FROM exam_date
                   WHERE certificate_id=? AND date_type IN ('official','expected')
                   ORDER BY exam_date""", (cert["id"],)).fetchall()
            cert["exam_dates"] = [dict(d) | {"countdown": countdown(d["exam_date"])} for d in dates]
        return certs


# ---------------------------------------------------------------------------
# 学习目标与倒计时（FR-K02）
# ---------------------------------------------------------------------------

def _goal_payload(db, row: sqlite3.Row) -> dict[str, Any]:
    cert = db.execute("SELECT id, code, name, subjects_json FROM certificate WHERE id=?",
                      (row["certificate_id"],)).fetchone()
    official = None
    if row["official_date_id"]:
        official = db.execute(
            "SELECT id, round_label, date_type, exam_date FROM exam_date WHERE id=?",
            (row["official_date_id"],)).fetchone()
    source, target = None, None
    if official is not None:
        source, target = official["date_type"], official["exam_date"]
    elif row["planned_date"]:
        source, target = "planned", row["planned_date"]
    return {
        "id": row["id"], "status": row["status"],
        "certificate": {"id": cert["id"], "code": cert["code"], "name": cert["name"],
                        "subjects": json.loads(cert["subjects_json"] or "[]")} if cert else None,
        "official_date": (dict(official) | {"countdown": countdown(official["exam_date"])}) if official else None,
        "planned_date": row["planned_date"],
        "countdown": countdown(target), "countdown_source": source,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_goals(store, student_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    with store.connect() as db:
        where = "" if include_archived else "AND status != 'archived'"
        rows = db.execute(
            f"SELECT * FROM student_goal WHERE student_id=? {where} ORDER BY created_at DESC",
            (student_id,)).fetchall()
        return [_goal_payload(db, r) for r in rows]


def _validate_target(db, certificate_id: str, official_date_id: str | None, planned_date: str | None) -> None:
    if official_date_id:
        row = db.execute("SELECT date_type, exam_date FROM exam_date WHERE id=? AND certificate_id=?",
                         (official_date_id, certificate_id)).fetchone()
        if not row:
            raise TrainingPortalError("所选考试日期不存在或不属于该证书。")
        if row["date_type"] not in ("official", "expected"):
            raise TrainingPortalError("只能选择官方或预计考试日作为目标。")
        if countdown(row["exam_date"])["expired"]:
            raise TrainingPortalError(f"考试日期 {row['exam_date']} 已经过期，请选择未来的考试日。")
    if planned_date:
        day = date.fromisoformat(_parse_date(planned_date))
        if day < today_cst():
            raise TrainingPortalError(f"个人计划日期 {planned_date} 已过期，请选择今天或未来的日期。")


def create_goal(store, student_id: str, certificate_id: str, official_date_id: str | None = None,
                planned_date: str | None = None) -> dict[str, Any]:
    if planned_date is not None:
        planned_date = _parse_date(planned_date)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cert = db.execute("SELECT active FROM certificate WHERE id=?", (certificate_id,)).fetchone()
        if not cert or not cert["active"]:
            raise TrainingPortalError("证书不存在或已下架。", 404)
        _validate_target(db, certificate_id, official_date_id, planned_date)
        goal_id = str(uuid.uuid4())
        now = _now()
        try:
            db.execute(
                """INSERT INTO student_goal (id, student_id, certificate_id, planned_date,
                   official_date_id, status, created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)""",
                (goal_id, student_id, certificate_id, planned_date, official_date_id, now, now))
        except sqlite3.IntegrityError:
            raise TrainingPortalError("该证书已有进行中的学习目标，请先归档或暂停它。", 409) from None
        row = db.execute("SELECT * FROM student_goal WHERE id=?", (goal_id,)).fetchone()
        return _goal_payload(db, row)


def update_goal(store, student_id: str, goal_id: str, *, official_date_id: str | None = None,
                planned_date: str | None = None) -> dict[str, Any]:
    if planned_date is not None:
        planned_date = _parse_date(planned_date)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM student_goal WHERE id=? AND student_id=?",
                         (goal_id, student_id)).fetchone()
        if not row:
            raise TrainingPortalError("学习目标不存在。", 404)
        if row["status"] != "active":
            raise TrainingPortalError("目标当前不是进行中状态，恢复后再修改。")
        _validate_target(db, row["certificate_id"], official_date_id, planned_date)
        db.execute(
            "UPDATE student_goal SET planned_date=?, official_date_id=?, updated_at=? WHERE id=?",
            (planned_date, official_date_id, _now(), goal_id))
        row = db.execute("SELECT * FROM student_goal WHERE id=?", (goal_id,)).fetchone()
        return _goal_payload(db, row)


def set_goal_status(store, student_id: str, goal_id: str, action: str) -> dict[str, Any]:
    allowed = {"pause": ("active", "paused"), "resume": ("paused", "active"), "archive": (None, "archived")}
    if action not in allowed:
        raise TrainingPortalError("不支持的目标操作。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM student_goal WHERE id=? AND student_id=?",
                         (goal_id, student_id)).fetchone()
        if not row:
            raise TrainingPortalError("学习目标不存在。", 404)
        current, target = allowed[action]
        if current is not None and row["status"] != current:
            raise TrainingPortalError(f"目标当前状态为 {row['status']}，无法执行该操作。")
        try:
            db.execute("UPDATE student_goal SET status=?, updated_at=? WHERE id=?",
                       (target, _now(), goal_id))
        except sqlite3.IntegrityError:
            raise TrainingPortalError("该证书已有另一个进行中的目标，请先归档它。", 409) from None
        row = db.execute("SELECT * FROM student_goal WHERE id=?", (goal_id,)).fetchone()
        return _goal_payload(db, row)


# ---------------------------------------------------------------------------
# 教师/管理员认证（college_user）与内容维护（FR-K01/K02/K03/K04 管理侧）
# ---------------------------------------------------------------------------

STAFF_COOKIE = "taxpearls_staff_session"
staff_login_guard = LoginGuard()


def _staff_public(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "college": row["college"], "role": row["role"]}


def login_staff(store, username: str, password: str, remember: bool = False) -> dict[str, Any] | None:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_user WHERE username=? AND active=1",
                         (username.strip(),)).fetchone()
        if not row:
            return None
        try:
            _passwords.verify(row["password_hash"], password)
        except (VerifyMismatchError, InvalidHashError):
            return None
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(seconds=session_max_age(remember))).isoformat(timespec="seconds")
        db.execute("DELETE FROM training_staff_sessions WHERE expires_at < ?", (_now(),))
        db.execute("INSERT INTO training_staff_sessions VALUES (?,?,?,?)",
                   (_hash_token(token), row["id"], expires, _now()))
        return {"staff": _staff_public(row), "token": token}


def staff_for_token(store, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with store.connect() as db:
        row = db.execute(
            """SELECT c.id, c.username, c.display_name, c.college, c.role, c.active
               FROM training_staff_sessions t JOIN college_user c ON c.id = t.staff_id
               WHERE t.token_hash=? AND t.expires_at>=?""",
            (_hash_token(token), _now()),
        ).fetchone()
    if not row or not row["active"] or row["role"] not in ("teacher", "admin"):
        return None
    return _staff_public(row)


def logout_staff(store, token: str | None) -> None:
    if token:
        with store.connect() as db:
            db.execute("DELETE FROM training_staff_sessions WHERE token_hash=?", (_hash_token(token),))


def _clean_subjects(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(s, str) for s in value):
        raise TrainingPortalError("科目清单必须是字符串数组。")
    out = list(dict.fromkeys(s.strip() for s in value if s.strip()))
    return out


def create_certificate(store, staff: dict, code: str, name: str, *, description: str = "",
                       subjects: list[str] | None = None, source_ref: str = "") -> dict[str, Any]:
    code, name = code.strip(), name.strip()
    if not code or not name:
        raise TrainingPortalError("证书编码与名称不能为空。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM certificate WHERE code=?", (code,)).fetchone():
            raise TrainingPortalError(f"证书编码 {code} 已存在。", 409)
        cert_id = str(uuid.uuid4())
        now = _now()
        db.execute(
            """INSERT INTO certificate (id, code, name, description, subjects_json, source_ref,
               active, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)""",
            (cert_id, code, name, description.strip(), _json_subjects(subjects or []),
             source_ref.strip(), now, now))
        row = db.execute("SELECT * FROM certificate WHERE id=?", (cert_id,)).fetchone()
        return _certificate_payload(db, row)


def _json_subjects(subjects: list[str]) -> str:
    return json.dumps(_clean_subjects(subjects), ensure_ascii=False)


def _certificate_payload(db, row: sqlite3.Row) -> dict[str, Any]:
    cert = dict(row)
    cert["subjects"] = json.loads(cert.pop("subjects_json") or "[]")
    dates = db.execute(
        """SELECT id, round_label, date_type, exam_date, created_at FROM exam_date
           WHERE certificate_id=? AND date_type IN ('official','expected') ORDER BY exam_date""",
        (cert["id"],)).fetchall()
    cert["exam_dates"] = [dict(d) | {"countdown": countdown(d["exam_date"])} for d in dates]
    cert["knowledge_point_count"] = db.execute(
        "SELECT count(*) FROM knowledge_point WHERE certificate_id=?", (cert["id"],)).fetchone()[0]
    return cert


def get_certificate(store, cert_id: str, *, include_inactive: bool = True) -> dict[str, Any]:
    with store.connect() as db:
        row = db.execute("SELECT * FROM certificate WHERE id=?", (cert_id,)).fetchone()
        if not row:
            raise TrainingPortalError("证书不存在。", 404)
        return _certificate_payload(db, row)


def update_certificate(store, staff: dict, cert_id: str, *, fields: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name": str, "description": str, "source_ref": str, "subjects": list, "active": bool}
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            raise TrainingPortalError(f"不支持的字段：{key}。")
        if key == "subjects":
            updates["subjects_json"] = _json_subjects(value)
        elif key == "name":
            if not value.strip():
                raise TrainingPortalError("证书名称不能为空。")
            updates["name"] = value.strip()
        else:
            updates[key] = value
    if not updates:
        raise TrainingPortalError("没有提供任何要更新的字段。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在。", 404)
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE certificate SET {sets} WHERE id=?", (*updates.values(), cert_id))
        row = db.execute("SELECT * FROM certificate WHERE id=?", (cert_id,)).fetchone()
        return _certificate_payload(db, row)


def create_exam_date(store, staff: dict, cert_id: str, date_type: str, exam_date: str,
                     round_label: str = "") -> dict[str, Any]:
    if date_type not in ("official", "expected"):
        raise TrainingPortalError("考试日期类型只能是 official 或 expected；个人计划日由学生自行设置。")
    day = date.fromisoformat(_parse_date(exam_date))
    if day < today_cst():
        raise TrainingPortalError(f"考试日期 {exam_date} 已经过期，不能录入。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在。", 404)
        date_id = str(uuid.uuid4())
        now = _now()
        db.execute(
            """INSERT INTO exam_date (id, certificate_id, round_label, date_type, exam_date,
               created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
            (date_id, cert_id, round_label.strip(), date_type, day.isoformat(), staff["id"], now, now))
        row = db.execute("SELECT * FROM exam_date WHERE id=?", (date_id,)).fetchone()
        return dict(row) | {"countdown": countdown(row["exam_date"])}


def update_exam_date(store, staff: dict, date_id: str, *, fields: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "round_label" in fields:
        updates["round_label"] = str(fields["round_label"]).strip()
    if "exam_date" in fields:
        day = date.fromisoformat(_parse_date(fields["exam_date"]))
        if day < today_cst():
            raise TrainingPortalError(f"考试日期 {fields['exam_date']} 已经过期，不能修改为过去。")
        updates["exam_date"] = day.isoformat()
    if not updates:
        raise TrainingPortalError("没有提供任何要更新的字段。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM exam_date WHERE id=?", (date_id,)).fetchone():
            raise TrainingPortalError("考试日期不存在。", 404)
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE exam_date SET {sets} WHERE id=?", (*updates.values(), date_id))
        row = db.execute("SELECT * FROM exam_date WHERE id=?", (date_id,)).fetchone()
        return dict(row) | {"countdown": countdown(row["exam_date"])}


def delete_exam_date(store, staff: dict, date_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM exam_date WHERE id=?", (date_id,)).fetchone()
        if not row:
            raise TrainingPortalError("考试日期不存在。", 404)
        used = db.execute("SELECT count(*) FROM student_goal WHERE official_date_id=?",
                          (date_id,)).fetchone()[0]
        if used:
            raise TrainingPortalError(f"已有 {used} 个学生目标引用该考试日期，不能删除；可修改日期或让目标改挂其他日期。", 409)
        db.execute("DELETE FROM exam_date WHERE id=?", (date_id,))
    return {"ok": True}


def _assert_same_certificate(db, kp_id: str, cert_id: str, label: str) -> None:
    row = db.execute("SELECT certificate_id FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()
    if not row:
        raise TrainingPortalError(f"{label}不存在。", 404)
    if row["certificate_id"] != cert_id:
        raise TrainingPortalError(f"{label}不属于该证书。")


def _would_cycle(db, kp_id: str, parent_id: str) -> bool:
    """沿 parent 链向上走，若回到 kp_id 自身则成环。"""
    seen, current = set(), parent_id
    while current:
        if current == kp_id:
            return True
        if current in seen:
            return True
        seen.add(current)
        row = db.execute("SELECT parent_id FROM knowledge_point WHERE id=?", (current,)).fetchone()
        current = row["parent_id"] if row else None
    return False


def create_knowledge_point(store, staff: dict, cert_id: str, code: str, name: str, *,
                           subject: str = "", parent_id: str | None = None,
                           description: str = "", source_ref: str = "",
                           outline_version: str = "") -> dict[str, Any]:
    code, name = code.strip(), name.strip()
    if not code or not name:
        raise TrainingPortalError("知识点编码与名称不能为空。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在。", 404)
        if db.execute("SELECT 1 FROM knowledge_point WHERE certificate_id=? AND code=?",
                      (cert_id, code)).fetchone():
            raise TrainingPortalError(f"知识点编码 {code} 在该证书下已存在。", 409)
        if parent_id:
            _assert_same_certificate(db, parent_id, cert_id, "上级知识点")
        kp_id = str(uuid.uuid4())
        now = _now()
        db.execute(
            """INSERT INTO knowledge_point (id, certificate_id, code, name, subject, parent_id,
               description, source_ref, outline_version, active, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
            (kp_id, cert_id, code, name, subject.strip(), parent_id, description.strip(),
             source_ref.strip(), outline_version.strip(), now, now))
        row = db.execute("SELECT * FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()
        return _kp_payload(db, row)


def _kp_payload(db, row: sqlite3.Row) -> dict[str, Any]:
    marks = db.execute(
        "SELECT id, mark_type, level, basis_ref, basis_version, created_at FROM knowledge_point_mark"
        " WHERE knowledge_point_id=? ORDER BY mark_type, created_at", (row["id"],)).fetchall()
    return dict(row) | {"marks": [dict(m) for m in marks]}


def list_knowledge_points(store, staff: dict, cert_id: str, *,
                          include_inactive: bool = True) -> list[dict[str, Any]]:
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在。", 404)
        where = "" if include_inactive else "AND active=1"
        rows = db.execute(
            f"SELECT * FROM knowledge_point WHERE certificate_id=? {where} ORDER BY code",
            (cert_id,)).fetchall()
        return [_kp_payload(db, r) for r in rows]


def update_knowledge_point(store, staff: dict, kp_id: str, *, fields: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name": None, "subject": None, "description": None, "source_ref": None,
               "outline_version": None, "active": None, "parent_id": None}
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            raise TrainingPortalError(f"不支持的字段：{key}。")
        if key == "name":
            if not str(value).strip():
                raise TrainingPortalError("知识点名称不能为空。")
            updates["name"] = str(value).strip()
        elif key in ("subject", "description", "source_ref", "outline_version"):
            updates[key] = str(value).strip()
        elif key == "active":
            updates["active"] = 1 if value else 0
        else:
            updates["parent_id"] = value
    if not updates:
        raise TrainingPortalError("没有提供任何要更新的字段。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()
        if not row:
            raise TrainingPortalError("知识点不存在。", 404)
        if "parent_id" in updates:
            parent = updates["parent_id"]
            if parent == kp_id:
                raise TrainingPortalError("知识点不能以自己为上级。")
            if parent and _would_cycle(db, kp_id, parent):
                raise TrainingPortalError("上级知识点选择会形成循环层级。")
            if parent:
                _assert_same_certificate(db, parent, row["certificate_id"], "上级知识点")
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE knowledge_point SET {sets} WHERE id=?", (*updates.values(), kp_id))
        row = db.execute("SELECT * FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()
        return _kp_payload(db, row)


def create_mark(store, staff: dict, kp_id: str, mark_type: str, *, level: str = "high",
                basis_ref: str = "", basis_version: str = "") -> dict[str, Any]:
    if mark_type not in ("high_freq", "risk_context", "error_prone"):
        raise TrainingPortalError("标注类型必须是 high_freq / risk_context / error_prone。")
    if level not in ("high", "medium", "low"):
        raise TrainingPortalError("标注程度必须是 high / medium / low。")
    basis_ref = basis_ref.strip()
    if mark_type == "high_freq" and not basis_ref:
        raise TrainingPortalError("考证高频标注必须提供依据（如官方考纲章节），不允许无依据标注。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM knowledge_point WHERE id=?", (kp_id,)).fetchone():
            raise TrainingPortalError("知识点不存在。", 404)
        mark_id = str(uuid.uuid4())
        try:
            db.execute(
                """INSERT INTO knowledge_point_mark (id, knowledge_point_id, mark_type, level,
                   basis_ref, basis_version, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (mark_id, kp_id, mark_type, level, basis_ref, basis_version.strip(), staff["id"], _now()))
        except sqlite3.IntegrityError:
            raise TrainingPortalError("该知识点已存在相同依据的同类标注。", 409) from None
        row = db.execute("SELECT * FROM knowledge_point_mark WHERE id=?", (mark_id,)).fetchone()
        return dict(row)


def update_mark(store, staff: dict, mark_id: str, *, fields: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "level" in fields:
        if fields["level"] not in ("high", "medium", "low"):
            raise TrainingPortalError("标注程度必须是 high / medium / low。")
        updates["level"] = fields["level"]
    if "basis_ref" in fields:
        updates["basis_ref"] = str(fields["basis_ref"]).strip()
    if "basis_version" in fields:
        updates["basis_version"] = str(fields["basis_version"]).strip()
    if not updates:
        raise TrainingPortalError("没有提供任何要更新的字段。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM knowledge_point_mark WHERE id=?", (mark_id,)).fetchone()
        if not row:
            raise TrainingPortalError("标注不存在。", 404)
        final_ref = updates.get("basis_ref", row["basis_ref"])
        if row["mark_type"] == "high_freq" and not final_ref:
            raise TrainingPortalError("考证高频标注必须保留依据，不允许清空。")
        updates.setdefault("basis_version", row["basis_version"])
        updates["basis_ref"] = final_ref
        sets = ", ".join(f"{k}=?" for k in set(updates))
        db.execute(f"UPDATE knowledge_point_mark SET {sets} WHERE id=?", (*updates.values(), mark_id))
        row = db.execute("SELECT * FROM knowledge_point_mark WHERE id=?", (mark_id,)).fetchone()
        return dict(row)


def delete_mark(store, staff: dict, mark_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM knowledge_point_mark WHERE id=?", (mark_id,)).fetchone():
            raise TrainingPortalError("标注不存在。", 404)
        db.execute("DELETE FROM knowledge_point_mark WHERE id=?", (mark_id,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# 路由注册（接线模式与 classroom/mistake_book 一致）
# ---------------------------------------------------------------------------

class LoginBody(BaseModel):
    student_no: str = Field(min_length=1)
    password: str = Field(min_length=1)
    college: str | None = None
    remember: bool = False


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    certificate_id: str = Field(min_length=1)
    official_date_id: str | None = None
    planned_date: str | None = None


class GoalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    official_date_id: str | None = None
    planned_date: str | None = None


class StaffLoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    remember: bool = False


class CertificateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    subjects: list[str] = Field(default_factory=list)
    source_ref: str = ""


class CertificatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    description: str | None = None
    subjects: list[str] | None = None
    source_ref: str | None = None
    active: bool | None = None


class ExamDateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_type: str
    exam_date: str
    round_label: str = ""


class ExamDatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_date: str | None = None
    round_label: str | None = None


class KnowledgePointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    subject: str = ""
    parent_id: str | None = None
    description: str = ""
    source_ref: str = ""
    outline_version: str = ""


class KnowledgePointPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    subject: str | None = None
    parent_id: str | None = None
    description: str | None = None
    source_ref: str | None = None
    outline_version: str | None = None
    active: bool | None = None


class MarkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mark_type: str
    level: str = "high"
    basis_ref: str = ""
    basis_version: str = ""


class MarkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: str | None = None
    basis_ref: str | None = None
    basis_version: str | None = None


def _patch_fields(body) -> dict[str, Any]:
    return {k: getattr(body, k) for k in body.model_fields_set}


def register(app, store_provider: Callable[[], Any], cookie_name: str) -> None:
    def response(body: Any) -> JSONResponse:
        return JSONResponse(body, headers={"Cache-Control": "private, no-store"})

    def student(session: str | None) -> dict[str, Any]:
        who = student_for_token(store_provider(), session)
        if not who:
            raise HTTPException(status_code=401, detail="请先使用学号登录。")
        return who

    def guarded(handler):
        @wraps(handler)  # 保留原签名：FastAPI 依赖参数内省解析 Cookie/Body
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except TrainingPortalError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from None
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="操作与现有数据冲突。") from None
        return wrapper

    @app.post("/api/training/auth/login")
    @guarded
    def training_login(body: LoginBody, request: Request) -> Response:
        ip = request.client.host if request.client else "unknown"
        with login_guard.reserve(body.student_no, ip) as wait:
            if wait:
                return JSONResponse(status_code=429, content={"detail": "登录尝试过于频繁，请稍后重试。"},
                                    headers={"Retry-After": str(wait)})
            result = login_student(store_provider(), body.student_no, body.password,
                                   college=body.college, remember=body.remember)
            if not result:
                login_guard.record_failure(body.student_no, ip)
                raise HTTPException(status_code=401, detail="学号或密码不正确。")
            login_guard.record_success(body.student_no)
        resp = response({"student": result["student"]})
        resp.set_cookie(
            cookie_name, result["token"], max_age=session_max_age(body.remember), httponly=True,
            samesite="strict", secure=os.environ.get("TAXPEARLS_COOKIE_SECURE") == "1", path="/",
        )
        return resp

    @app.post("/api/training/auth/logout")
    def training_logout(session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        logout_student(store_provider(), session)
        resp = response({"ok": True})
        resp.delete_cookie(cookie_name, path="/")
        return resp

    @app.get("/api/training/auth/me")
    @guarded
    def training_me(session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        return response({"student": student(session)})

    @app.get("/api/training/certificates")
    @guarded
    def training_certificates(session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        student(session)
        return response({"certificates": list_certificates(store_provider())})

    @app.get("/api/training/my/goals")
    @guarded
    def training_goals(include_archived: bool = False,
                       session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"goals": list_goals(store_provider(), who["id"], include_archived=include_archived)})

    @app.post("/api/training/my/goals")
    @guarded
    def training_goal_create(body: GoalCreate,
                             session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"goal": create_goal(store_provider(), who["id"], body.certificate_id,
                                             official_date_id=body.official_date_id,
                                             planned_date=body.planned_date)})

    @app.patch("/api/training/my/goals/{goal_id}")
    @guarded
    def training_goal_update(goal_id: str, body: GoalPatch,
                             session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        kwargs: dict[str, Any] = {}
        if "official_date_id" in body.model_fields_set:
            kwargs["official_date_id"] = body.official_date_id
        if "planned_date" in body.model_fields_set:
            kwargs["planned_date"] = body.planned_date
        return response({"goal": update_goal(store_provider(), who["id"], goal_id, **kwargs)})

    for action in ("pause", "resume", "archive"):
        def _make(action: str):
            @guarded
            def endpoint(goal_id: str,
                         session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
                who = student(session)
                return response({"goal": set_goal_status(store_provider(), who["id"], goal_id, action)})
            return endpoint
        app.post(f"/api/training/my/goals/{{goal_id}}/{action}")(_make(action))

    # ---- 教师/管理员：认证与内容维护（证书上架、官方日期、考纲知识点、三类标注）----

    def staff(session: str | None) -> dict[str, Any]:
        who = staff_for_token(store_provider(), session)
        if not who:
            raise HTTPException(status_code=401, detail="请先使用教师或管理员账号登录。")
        return who

    @app.post("/api/training/staff/auth/login")
    @guarded
    def staff_login(body: StaffLoginBody, request: Request) -> Response:
        ip = request.client.host if request.client else "unknown"
        with staff_login_guard.reserve(body.username, ip) as wait:
            if wait:
                return JSONResponse(status_code=429, content={"detail": "登录尝试过于频繁，请稍后重试。"},
                                    headers={"Retry-After": str(wait)})
            result = login_staff(store_provider(), body.username, body.password, remember=body.remember)
            if not result:
                staff_login_guard.record_failure(body.username, ip)
                raise HTTPException(status_code=401, detail="用户名或密码不正确。")
            staff_login_guard.record_success(body.username)
        resp = response({"staff": result["staff"]})
        resp.set_cookie(
            STAFF_COOKIE, result["token"], max_age=session_max_age(body.remember), httponly=True,
            samesite="strict", secure=os.environ.get("TAXPEARLS_COOKIE_SECURE") == "1", path="/",
        )
        return resp

    @app.post("/api/training/staff/auth/logout")
    def staff_logout(session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        logout_staff(store_provider(), session)
        resp = response({"ok": True})
        resp.delete_cookie(STAFF_COOKIE, path="/")
        return resp

    @app.get("/api/training/staff/auth/me")
    @guarded
    def staff_me(session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        return response({"staff": staff(session)})

    @app.get("/api/training/staff/certificates")
    @guarded
    def staff_certificates(include_inactive: bool = True,
                           session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        with store_provider().connect() as db:
            where = "" if include_inactive else "WHERE active=1"
            rows = db.execute(f"SELECT id FROM certificate {where} ORDER BY created_at").fetchall()
        return response({"certificates": [get_certificate(store_provider(), r["id"]) for r in rows]})

    @app.post("/api/training/staff/certificates")
    @guarded
    def staff_certificate_create(body: CertificateCreate,
                                 session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"certificate": create_certificate(
            store_provider(), who, body.code, body.name, description=body.description,
            subjects=body.subjects, source_ref=body.source_ref)})

    @app.patch("/api/training/staff/certificates/{cert_id}")
    @guarded
    def staff_certificate_update(cert_id: str, body: CertificatePatch,
                                 session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"certificate": update_certificate(
            store_provider(), who, cert_id, fields=_patch_fields(body))})

    @app.post("/api/training/staff/certificates/{cert_id}/dates")
    @guarded
    def staff_date_create(cert_id: str, body: ExamDateCreate,
                          session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"exam_date": create_exam_date(
            store_provider(), who, cert_id, body.date_type, body.exam_date, body.round_label)})

    @app.patch("/api/training/staff/dates/{date_id}")
    @guarded
    def staff_date_update(date_id: str, body: ExamDatePatch,
                          session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"exam_date": update_exam_date(
            store_provider(), who, date_id, fields=_patch_fields(body))})

    @app.delete("/api/training/staff/dates/{date_id}")
    @guarded
    def staff_date_delete(date_id: str,
                          session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response(delete_exam_date(store_provider(), who, date_id))

    @app.get("/api/training/staff/certificates/{cert_id}/knowledge-points")
    @guarded
    def staff_kp_list(cert_id: str, include_inactive: bool = True,
                      session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"knowledge_points": list_knowledge_points(
            store_provider(), who, cert_id, include_inactive=include_inactive)})

    @app.post("/api/training/staff/certificates/{cert_id}/knowledge-points")
    @guarded
    def staff_kp_create(cert_id: str, body: KnowledgePointCreate,
                        session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"knowledge_point": create_knowledge_point(
            store_provider(), who, cert_id, body.code, body.name, subject=body.subject,
            parent_id=body.parent_id, description=body.description, source_ref=body.source_ref,
            outline_version=body.outline_version)})

    @app.patch("/api/training/staff/knowledge-points/{kp_id}")
    @guarded
    def staff_kp_update(kp_id: str, body: KnowledgePointPatch,
                        session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"knowledge_point": update_knowledge_point(
            store_provider(), who, kp_id, fields=_patch_fields(body))})

    @app.post("/api/training/staff/knowledge-points/{kp_id}/marks")
    @guarded
    def staff_mark_create(kp_id: str, body: MarkCreate,
                          session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"mark": create_mark(
            store_provider(), who, kp_id, body.mark_type, level=body.level,
            basis_ref=body.basis_ref, basis_version=body.basis_version)})

    @app.patch("/api/training/staff/marks/{mark_id}")
    @guarded
    def staff_mark_update(mark_id: str, body: MarkPatch,
                          session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"mark": update_mark(store_provider(), who, mark_id, fields=_patch_fields(body))})

    @app.delete("/api/training/staff/marks/{mark_id}")
    @guarded
    def staff_mark_delete(mark_id: str,
                          session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response(delete_mark(store_provider(), who, mark_id))

    @app.get("/training-certificates")
    def training_portal_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "training-portal.html", media_type="text/html")

    @app.get("/training-portal.js")
    def training_portal_script() -> FileResponse:
        return FileResponse(STATIC_DIR / "training-portal.js", media_type="text/javascript")
