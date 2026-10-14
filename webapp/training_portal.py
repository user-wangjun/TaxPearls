"""考证刷题线学生端（FR-K01/K02）：证书目录、学习目标与考试倒计时。

学生身份独立于平台 users 账号：以 student_info（学号 + 密码）登录，
会话存 training_student_sessions 表，使用独立 cookie，与主站会话互不影响。
倒计时按中国日历日计算且永不为负；考试日期过期仅作标记，绝不自动修改
目标状态（验收：不自动判定目标达成）；目标修改保留 created_at 历史，
归档不删行（验收：修改范围不丢失历史记录）。

需求基线：docs/07-高校考证刷题线需求.md（TP-CERT-001，university-extension 分支）。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import openpyxl

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Cookie, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp.login_guard import LoginGuard
from webapp.storage import _hash_token, _now, session_max_age
from webapp.training_content import (applicability, ensure_active_version,
                                     resolve_active_version, version_brief)
from webapp.training_graph import relations_payload

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
    except (TypeError, ValueError):
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


def change_staff_password(store, staff: dict[str, Any], old_password: str, new_password: str) -> dict[str, Any]:
    """教师本人修改密码：验证当前密码，更新后清除自己全部会话（需重新登录）。"""
    new_password = (new_password or "").strip()
    if len(new_password) < _MIN_PASSWORD_LEN:
        raise TrainingPortalError(f"新密码至少 {_MIN_PASSWORD_LEN} 位。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_user WHERE id=? AND active=1", (staff["id"],)).fetchone()
        if not row:
            raise TrainingPortalError("账号不存在或已停用。", 404)
        try:
            _passwords.verify(row["password_hash"], old_password or "")
        except (VerifyMismatchError, InvalidHashError):
            raise TrainingPortalError("当前密码不正确。", 401) from None
        if old_password == new_password:
            raise TrainingPortalError("新密码不能与当前密码相同。")
        db.execute("UPDATE college_user SET password_hash=? WHERE id=?",
                   (_passwords.hash(new_password), row["id"]))
        db.execute("DELETE FROM training_staff_sessions WHERE staff_id=?", (row["id"],))
    return {"password_changed": True}


def change_student_password(store, student: dict[str, Any], old_password: str,
                            new_password: str) -> dict[str, Any]:
    """学生本人修改密码：验证当前密码，更新后清除本人全部会话（需重新登录）。

    与教师改密同口径；学生初始密码为默认口令（Tp#学号2026），首登改密即
    消除默认口令风险。教师重置入口保留（不验证旧密码，属管理员动作）。"""
    new_password = _validate_student_password(new_password)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM student_info WHERE id=? AND active=1",
                         (student["id"],)).fetchone()
        if not row:
            raise TrainingPortalError("学生账号不存在或已停用。", 404)
        try:
            _passwords.verify(row["password_hash"], old_password or "")
        except (VerifyMismatchError, InvalidHashError):
            raise TrainingPortalError("当前密码不正确。", 401) from None
        if old_password == new_password:
            raise TrainingPortalError("新密码不能与当前密码相同。")
        db.execute("UPDATE student_info SET password_hash=? WHERE id=?",
                   (_passwords.hash(new_password), row["id"]))
        db.execute("DELETE FROM training_student_sessions WHERE student_id=?", (row["id"],))
    return {"password_changed": True}


# ---------------------------------------------------------------------------
# 教师端学生名册管理（小范围真实使用：单个添加 + 批量导入）
# ---------------------------------------------------------------------------

_STUDENT_COLS = "id, college, student_no, name, class_name, active, created_at"
_MIN_PASSWORD_LEN = 8


def _validate_student_password(password: str) -> str:
    password = (password or "").strip()
    if len(password) < _MIN_PASSWORD_LEN:
        raise TrainingPortalError(f"密码至少 {_MIN_PASSWORD_LEN} 位。")
    return password


def list_students(store, *, include_inactive: bool = True) -> list[dict[str, Any]]:
    with store.connect() as db:
        where = "" if include_inactive else "WHERE active=1"
        rows = db.execute(
            f"SELECT {_STUDENT_COLS} FROM student_info {where} ORDER BY college, student_no"
        ).fetchall()
    return [dict(r) for r in rows]


def create_student(store, college: str, student_no: str, name: str, *,
                   class_name: str | None = None, initial_password: str | None = None,
                   email: str | None = None) -> dict[str, Any]:
    college = (college or "").strip()
    student_no = (student_no or "").strip()
    name = (name or "").strip()
    if not college or not student_no or not name:
        raise TrainingPortalError("院校、学号、姓名均不能为空。")
    password = _validate_student_password(initial_password or f"Tp#{student_no}2026")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        require_student_slot(db, college)  # FR-U06：事务内核销，并发不绕过
        dup = db.execute(
            "SELECT id FROM student_info WHERE college=? AND student_no=?", (college, student_no)
        ).fetchone()
        if dup:
            raise TrainingPortalError(f"该院校下已存在学号 {student_no}。")
        sid = str(uuid.uuid4())
        db.execute(
            """INSERT INTO student_info
               (id, college, student_no, name, class_name, password_hash, email, active, created_at)
               VALUES (?,?,?,?,?,?,?,1,?)""",
            (sid, college, student_no, name, (class_name or "").strip() or None,
             _passwords.hash(password), (email or "").strip() or None, _now()),
        )
        row = db.execute(f"SELECT {_STUDENT_COLS} FROM student_info WHERE id=?", (sid,)).fetchone()
    return dict(row)


def import_students(store, college: str, rows: list[dict[str, Any]], *,
                    default_password: str, default_class_name: str | None = None) -> dict[str, Any]:
    """逐行导入学生；单行失败不中断整批，返回 created/skipped 明细。"""
    _validate_student_password(default_password)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        student_no = str(row.get("student_no") or "").strip()
        try:
            created.append(create_student(
                store, college, student_no, str(row.get("name") or ""),
                class_name=row.get("class_name") or default_class_name,
                initial_password=default_password))
        except TrainingPortalError as exc:
            skipped.append({"line": index, "student_no": student_no, "reason": str(exc)})
    return {"created": created, "skipped": skipped}


_XLSX_MAX_BYTES = 2_000_000   # 名册文件大小上限（约 2MB，远超正常班级名册）
_XLSX_MAX_ROWS = 1000         # 单次导入数据行上限；超出整体拒绝，请分批


def _xlsx_cell_text(cells: tuple, index: int | None) -> str:
    """读取单元格为文本：数字学号（int/整值 float）转为无小数点字符串。"""
    if index is None or index >= len(cells) or cells[index] is None:
        return ""
    value = cells[index]
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def parse_student_xlsx(data: bytes, *,
                       max_bytes: int = _XLSX_MAX_BYTES,
                       max_rows: int = _XLSX_MAX_ROWS) -> list[dict[str, Any]]:
    """Excel 名册直读（FR-U03 第二期）：把 .xlsx 名册解析为 import_students 的行。

    边界沿用项目惯例：只读单元格缓存值（data_only=True），不执行公式——公式
    单元格无缓存按空值处理，不补造；第一行必须是表头且含「学号」「姓名」列
    （「班级」列可选），不按列序猜测；整行全空的行忽略（与文本导入一致）；
    文件大小与数据行数超上限整体拒绝，不静默截断。"""
    if not data:
        raise TrainingPortalError("文件为空。", 400)
    if len(data) > max_bytes:
        raise TrainingPortalError(f"文件超过大小上限（{max_bytes // 1024} KB），请分批导入。", 413)
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        # 外部上传的字节流无法保证可解析：zip 损坏（BadZipFile）、旧版 .xls
        # （InvalidFileException）、缺关键部件（KeyError）等一律按"无法读取"拒绝。
        raise TrainingPortalError(
            "无法读取的 Excel 文件，仅支持 .xlsx（旧版 .xls 请先另存为 .xlsx）。", 400) from None
    try:
        sheet = workbook.worksheets[0]  # 只读第一个工作表，不按表名猜测
        data_rows = sheet.iter_rows(values_only=True)
        header = next(data_rows, None)
        if header is None:
            raise TrainingPortalError("工作表为空。", 422)
        col_no = col_name = col_class = None
        for index, raw in enumerate(header):
            text = str(raw or "").strip()
            if col_no is None and "学号" in text:
                col_no = index
            elif col_name is None and "姓名" in text:
                col_name = index
            elif col_class is None and "班级" in text:
                col_class = index
        if col_no is None or col_name is None:
            raise TrainingPortalError(
                "第一行需为表头且包含「学号」和「姓名」列（「班级」列可选）。", 422)
        rows: list[dict[str, Any]] = []
        for cells in data_rows:
            if len(rows) >= max_rows:
                raise TrainingPortalError(
                    f"数据行超过单次上限（{max_rows} 行），请分批导入。", 422)
            student_no = _xlsx_cell_text(cells, col_no)
            name = _xlsx_cell_text(cells, col_name)
            class_name = _xlsx_cell_text(cells, col_class)
            if not student_no and not name and not class_name:
                continue
            rows.append({"student_no": student_no, "name": name,
                         "class_name": class_name or None})
        if not rows:
            raise TrainingPortalError("未读取到数据行（表头之外没有有效内容）。", 422)
        return rows
    finally:
        workbook.close()


def reset_student_password(store, student_id: str, new_password: str) -> dict[str, Any]:
    password = _validate_student_password(new_password)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id FROM student_info WHERE id=?", (student_id,)).fetchone()
        if not row:
            raise TrainingPortalError("学生不存在。")
        db.execute("UPDATE student_info SET password_hash=? WHERE id=?",
                   (_passwords.hash(password), student_id))
        db.execute("DELETE FROM training_student_sessions WHERE student_id=?", (student_id,))
    return {"id": student_id, "password_reset": True}


def set_student_active(store, student_id: str, active: bool) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id FROM student_info WHERE id=?", (student_id,)).fetchone()
        if not row:
            raise TrainingPortalError("学生不存在。")
        db.execute("UPDATE student_info SET active=? WHERE id=?", (1 if active else 0, student_id))
        if not active:
            db.execute("DELETE FROM training_student_sessions WHERE student_id=?", (student_id,))
        row = db.execute(f"SELECT {_STUDENT_COLS} FROM student_info WHERE id=?", (student_id,)).fetchone()
    return dict(row)


# ---- 班级与教师名册（FR-U03 第一期：班级统计 / 整建制迁移 / 教师批量导入） ----

def list_classes(store, college: str) -> list[dict[str, Any]]:
    """按院校聚合班级与人数（自由文本 class_name 去重；空班级名不列示）。"""
    with store.connect() as db:
        rows = db.execute(
            """SELECT class_name,
                      count(*) AS students,
                      sum(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active_students
               FROM student_info
               WHERE college=? AND class_name IS NOT NULL AND TRIM(class_name) != ''
               GROUP BY class_name ORDER BY class_name""", (college,)).fetchall()
    return [dict(r) for r in rows]


def migrate_class(store, changed_by: str, college: str, from_class: str, to_class: str) -> dict[str, Any]:
    """学期初整建制迁移：把一个班级的全部在读学生改入新班级，逐生写入归属追溯。

    只迁移在读（active=1）学生，停用学生名册保持原班级便于复查；不开新学期
    归档（学期实体属 FR-U01，另行排期）。"""
    from_class = (from_class or "").strip()
    to_class = (to_class or "").strip()
    if not from_class or not to_class:
        raise TrainingPortalError("原班级与新班级均必填。")
    if from_class == to_class:
        raise TrainingPortalError("新班级与原班级相同，无需迁移。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        students = db.execute(
            """SELECT id, student_no, name FROM student_info
               WHERE college=? AND class_name=? AND active=1
               ORDER BY student_no""", (college, from_class)).fetchall()
        if not students:
            raise TrainingPortalError("该院校下没有此班级的在读学生。", 404)
        now = _now()
        for s in students:
            db.execute("UPDATE student_info SET class_name=? WHERE id=?", (to_class, s["id"]))
            db.execute(
                """INSERT INTO roster_class_history (id, student_id, from_class, to_class, changed_by, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (str(uuid.uuid4()), s["id"], from_class, to_class, changed_by, now))
        moved = [dict(s) | {"from_class": from_class, "to_class": to_class} for s in students]
    return {"migrated": len(moved), "students": moved}


def list_class_history(store, college: str, *, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with store.connect() as db:
        rows = db.execute(
            """SELECT h.id, h.student_id, s.student_no, s.name, h.from_class, h.to_class,
                      u.display_name AS changed_by_name, h.created_at
               FROM roster_class_history h
               JOIN student_info s ON s.id = h.student_id
               LEFT JOIN college_user u ON u.id = h.changed_by
               WHERE s.college=?
               ORDER BY h.created_at DESC, h.id LIMIT ?""", (college, limit)).fetchall()
    return [dict(r) for r in rows]


# ---- 高校组织架构（FR-U01 第一期：院系 / 学年学期 / 班级实体） ----
#
# 与现有两层模型并存：student_info.class_name 文本名册与既有作业数据归属
# 一律不改写；班级实体作为注册表经 (college, name) 对齐在读人数。写操作
# 仅限实训管理员（与教师批量导入同权限语义），教师只读。

class OrgDepartmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str
    name: str


class OrgNamePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    archived: bool | None = None


class OrgTermCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str
    name: str
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False


class OrgTermPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool | None = None
    archived: bool | None = None


class OrgClassCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str
    name: str
    department_id: str | None = None
    term_id: str | None = None


class OrgClassPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    department_id: str | None = None
    term_id: str | None = None
    archived: bool | None = None


class OrgClassClone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term_id: str
    name: str | None = None


def _iso_date_or_none(value: str | None, field: str) -> str | None:
    value = (value or "").strip() or None
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise TrainingPortalError(f"{field}须为 YYYY-MM-DD 格式。") from None


def _org_name(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise TrainingPortalError(f"{what}名称必填。")
    return value


def list_departments(store, college: str, *, include_archived: bool = True) -> list[dict[str, Any]]:
    """院系列表，附引用班级数（删除保护提示用）。"""
    where = "" if include_archived else "AND d.archived=0"
    with store.connect() as db:
        rows = db.execute(
            f"""SELECT d.id, d.college, d.name, d.archived, d.created_at,
                      count(c.id) AS classes
               FROM college_department d
               LEFT JOIN college_class c ON c.department_id = d.id
               WHERE d.college=? {where}
               GROUP BY d.id
               ORDER BY d.archived, d.name""", (college,)).fetchall()
    return [dict(r) for r in rows]


def create_department(store, college: str, name: str) -> dict[str, Any]:
    college = (college or "").strip()
    name = _org_name(name, "院系")
    if not college:
        raise TrainingPortalError("院校必填。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        dup = db.execute("SELECT id FROM college_department WHERE college=? AND name=?",
                         (college, name)).fetchone()
        if dup:
            raise TrainingPortalError(f"该院系下已存在「{name}」。", 409)
        did = str(uuid.uuid4())
        db.execute("INSERT INTO college_department (id, college, name, archived, created_at)"
                   " VALUES (?,?,?,0,?)", (did, college, name, _now()))
        row = db.execute("SELECT * FROM college_department WHERE id=?", (did,)).fetchone()
    return dict(row)


def _update_archived(db, table: str, row_id: str, archived: bool | None) -> None:
    if archived is not None:
        db.execute(f"UPDATE {table} SET archived=? WHERE id=?", (1 if archived else 0, row_id))


def update_department(store, department_id: str, *, name: str | None = None,
                      archived: bool | None = None) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_department WHERE id=?", (department_id,)).fetchone()
        if not row:
            raise TrainingPortalError("院系不存在。", 404)
        if name is not None:
            name = _org_name(name, "院系")
            dup = db.execute(
                "SELECT id FROM college_department WHERE college=? AND name=? AND id!=?",
                (row["college"], name, department_id)).fetchone()
            if dup:
                raise TrainingPortalError(f"该院系下已存在「{name}」。", 409)
            db.execute("UPDATE college_department SET name=? WHERE id=?", (name, department_id))
        _update_archived(db, "college_department", department_id, archived)
        row = db.execute("SELECT * FROM college_department WHERE id=?", (department_id,)).fetchone()
    return dict(row)


def delete_department(store, department_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_department WHERE id=?", (department_id,)).fetchone()
        if not row:
            raise TrainingPortalError("院系不存在。", 404)
        used = db.execute("SELECT count(*) n FROM college_class WHERE department_id=?",
                          (department_id,)).fetchone()["n"]
        if used:
            raise TrainingPortalError(f"该院系仍被 {used} 个班级引用，请先调整班级归属或归档院系。", 409)
        db.execute("DELETE FROM college_department WHERE id=?", (department_id,))
    return {"deleted": True, "id": department_id}


def list_terms(store, college: str, *, include_archived: bool = True) -> list[dict[str, Any]]:
    where = "" if include_archived else "AND t.archived=0"
    with store.connect() as db:
        rows = db.execute(
            f"""SELECT t.id, t.college, t.name, t.start_date, t.end_date, t.is_current,
                       t.archived, t.created_at, count(c.id) AS classes
                FROM college_term t
                LEFT JOIN college_class c ON c.term_id = t.id
                WHERE t.college=? {where}
                GROUP BY t.id
                ORDER BY t.archived, t.name""", (college,)).fetchall()
    return [dict(r) for r in rows]


def create_term(store, college: str, name: str, *, start_date: str | None = None,
                end_date: str | None = None, is_current: bool = False) -> dict[str, Any]:
    college = (college or "").strip()
    name = _org_name(name, "学期")
    if not college:
        raise TrainingPortalError("院校必填。")
    start_date = _iso_date_or_none(start_date, "开始日期")
    end_date = _iso_date_or_none(end_date, "结束日期")
    if start_date and end_date and end_date < start_date:
        raise TrainingPortalError("结束日期不能早于开始日期。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        dup = db.execute("SELECT id FROM college_term WHERE college=? AND name=?",
                         (college, name)).fetchone()
        if dup:
            raise TrainingPortalError(f"已存在学期「{name}」。", 409)
        if is_current:
            db.execute("UPDATE college_term SET is_current=0 WHERE college=?", (college,))
        tid = str(uuid.uuid4())
        db.execute("INSERT INTO college_term (id, college, name, start_date, end_date,"
                   " is_current, archived, created_at) VALUES (?,?,?,?,?,?,0,?)",
                   (tid, college, name, start_date, end_date, 1 if is_current else 0, _now()))
        row = db.execute("SELECT * FROM college_term WHERE id=?", (tid,)).fetchone()
    return dict(row)


def update_term(store, term_id: str, *, name: str | None = None, start_date: str | None = None,
                end_date: str | None = None, is_current: bool | None = None,
                archived: bool | None = None) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_term WHERE id=?", (term_id,)).fetchone()
        if not row:
            raise TrainingPortalError("学期不存在。", 404)
        if name is not None:
            name = _org_name(name, "学期")
            dup = db.execute("SELECT id FROM college_term WHERE college=? AND name=? AND id!=?",
                             (row["college"], name, term_id)).fetchone()
            if dup:
                raise TrainingPortalError(f"已存在学期「{name}」。", 409)
            db.execute("UPDATE college_term SET name=? WHERE id=?", (name, term_id))
        if start_date is not None or end_date is not None:
            new_start = _iso_date_or_none(start_date, "开始日期") if start_date is not None else row["start_date"]
            new_end = _iso_date_or_none(end_date, "结束日期") if end_date is not None else row["end_date"]
            if new_start and new_end and new_end < new_start:
                raise TrainingPortalError("结束日期不能早于开始日期。")
            db.execute("UPDATE college_term SET start_date=?, end_date=? WHERE id=?",
                       (new_start, new_end, term_id))
        if is_current:
            db.execute("UPDATE college_term SET is_current=0 WHERE college=?", (row["college"],))
        if is_current is not None:
            db.execute("UPDATE college_term SET is_current=? WHERE id=?",
                       (1 if is_current else 0, term_id))
        _update_archived(db, "college_term", term_id, archived)
        row = db.execute("SELECT * FROM college_term WHERE id=?", (term_id,)).fetchone()
    return dict(row)


def delete_term(store, term_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_term WHERE id=?", (term_id,)).fetchone()
        if not row:
            raise TrainingPortalError("学期不存在。", 404)
        used = db.execute("SELECT count(*) n FROM college_class WHERE term_id=?",
                          (term_id,)).fetchone()["n"]
        if used:
            raise TrainingPortalError(f"该学期仍被 {used} 个班级引用，请先调整班级归属或归档学期。", 409)
        db.execute("DELETE FROM college_term WHERE id=?", (term_id,))
    return {"deleted": True, "id": term_id}


def _class_unique_check(db, college: str, name: str, term_id: str | None,
                        exclude_id: str | None = None) -> None:
    """同院校 + 同班级名 + 同学期唯一（term_id 为空时同样查重，补 SQLite NULL 语义）。"""
    params: list[Any] = [college, name]
    where = "college=? AND name=?"
    if term_id:
        where += " AND term_id=?"
        params.append(term_id)
    else:
        where += " AND term_id IS NULL"
    if exclude_id:
        where += " AND id!=?"
        params.append(exclude_id)
    dup = db.execute(f"SELECT id FROM college_class WHERE {where}", params).fetchone()
    if dup:
        raise TrainingPortalError(f"同学期下已存在班级「{name}」。", 409)


def _org_ref_check(db, college: str, department_id: str | None,
                   term_id: str | None) -> tuple[str | None, str | None]:
    """引用的院系/学期必须存在且属于同院校（跨院校引用拒绝）。"""
    if department_id:
        row = db.execute("SELECT id FROM college_department WHERE id=?", (department_id,)).fetchone()
        if not row:
            raise TrainingPortalError("院系不存在。", 404)
        own = db.execute("SELECT college FROM college_department WHERE id=?", (department_id,)).fetchone()
        if own["college"] != college:
            raise TrainingPortalError("院系属于其他院校。", 422)
    if term_id:
        own = db.execute("SELECT college FROM college_term WHERE id=?", (term_id,)).fetchone()
        if not own:
            raise TrainingPortalError("学期不存在。", 404)
        if own["college"] != college:
            raise TrainingPortalError("学期属于其他院校。", 422)
    return department_id, term_id


def list_org_classes(store, college: str, *, include_archived: bool = True) -> list[dict[str, Any]]:
    """班级实体列表：对齐文本名册统计在读人数，不改写学生归属。"""
    where = "" if include_archived else "AND c.archived=0"
    with store.connect() as db:
        rows = db.execute(
            f"""SELECT c.id, c.college, c.name, c.department_id, c.term_id, c.archived,
                       c.created_at, d.name AS department_name, t.name AS term_name,
                       (SELECT count(*) FROM student_info s
                        WHERE s.college=c.college AND s.class_name=c.name AND s.active=1)
                       AS active_students
                FROM college_class c
                LEFT JOIN college_department d ON d.id=c.department_id
                LEFT JOIN college_term t ON t.id=c.term_id
                WHERE c.college=? {where}
                ORDER BY c.archived, c.name""", (college,)).fetchall()
    return [dict(r) for r in rows]


def create_org_class(store, college: str, name: str, *, department_id: str | None = None,
                 term_id: str | None = None) -> dict[str, Any]:
    college = (college or "").strip()
    name = _org_name(name, "班级")
    if not college:
        raise TrainingPortalError("院校必填。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        department_id, term_id = _org_ref_check(db, college, (department_id or "").strip() or None,
                                                (term_id or "").strip() or None)
        _class_unique_check(db, college, name, term_id)
        cid = str(uuid.uuid4())
        db.execute("INSERT INTO college_class (id, college, name, department_id, term_id,"
                   " archived, created_at) VALUES (?,?,?,?,?,0,?)",
                   (cid, college, name, department_id, term_id, _now()))
        row = db.execute("SELECT * FROM college_class WHERE id=?", (cid,)).fetchone()
    return dict(row)


def update_org_class(store, class_id: str, *, name: str | None = None,
                 department_id: str | None = None, term_id: str | None = None,
                 archived: bool | None = None) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_class WHERE id=?", (class_id,)).fetchone()
        if not row:
            raise TrainingPortalError("班级不存在。", 404)
        new_name = _org_name(name, "班级") if name is not None else row["name"]
        new_department = row["department_id"]
        new_term = row["term_id"]
        if department_id is not None or term_id is not None:
            if department_id is not None:
                new_department = department_id.strip() or None
            if term_id is not None:
                new_term = term_id.strip() or None
            new_department, new_term = _org_ref_check(db, row["college"], new_department, new_term)
        _class_unique_check(db, row["college"], new_name, new_term, exclude_id=class_id)
        db.execute("UPDATE college_class SET name=?, department_id=?, term_id=? WHERE id=?",
                   (new_name, new_department, new_term, class_id))
        _update_archived(db, "college_class", class_id, archived)
        row = db.execute("SELECT * FROM college_class WHERE id=?", (class_id,)).fetchone()
    return dict(row)


def delete_org_class(store, class_id: str) -> dict[str, Any]:
    """班级实体只是注册表，学生名册不受影响；直接删除并返回。"""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_class WHERE id=?", (class_id,)).fetchone()
        if not row:
            raise TrainingPortalError("班级不存在。", 404)
        db.execute("DELETE FROM college_class WHERE id=?", (class_id,))
    return {"deleted": True, "id": class_id}


def clone_org_class(store, class_id: str, *, term_id: str, name: str | None = None) -> dict[str, Any]:
    """班级按学期重建：复制实体元数据到新学期（学生名册不动，归属调整走 U03 迁移）。"""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM college_class WHERE id=?", (class_id,)).fetchone()
        if not row:
            raise TrainingPortalError("班级不存在。", 404)
        term_id = (term_id or "").strip()
        _org_ref_check(db, row["college"], None, term_id)
        term = db.execute("SELECT * FROM college_term WHERE id=?", (term_id,)).fetchone()
        if term_id == row["term_id"]:
            raise TrainingPortalError("目标学期与班级当前学期相同。", 422)
        new_name = _org_name(name, "班级") if name is not None else row["name"]
        _class_unique_check(db, row["college"], new_name, term_id)
        cid = str(uuid.uuid4())
        db.execute("INSERT INTO college_class (id, college, name, department_id, term_id,"
                   " archived, created_at) VALUES (?,?,?,?,?,0,?)",
                   (cid, row["college"], new_name, row["department_id"], term_id, _now()))
        new_row = db.execute("SELECT * FROM college_class WHERE id=?", (cid,)).fetchone()
    return {"class": dict(new_row), "cloned_from": class_id, "term_name": term["name"]}


# ---- 院校席位与配额（FR-U06）：师生分席、事务内核销、调整留痕 ----
#
# 无配额记录的院校按默认档放行（不阻塞既有部署）；一旦配置即按记录执行。
# 核销必须在 BEGIN IMMEDIATE 写事务内 count-check-insert（SQLite 单写者
# 串行化，并发不绕过——与 platform members.require_slot 同模式）。

_DEFAULT_TEACHER_QUOTA = 10
_DEFAULT_STUDENT_QUOTA = 200
_QUOTA_UPPER = 10000


class OrgQuotaPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str
    teacher_quota: int
    student_quota: int
    revision: int


def college_quota_state(db, college: str) -> dict[str, Any]:
    """用量与配额快照（可在已持有的写事务内调用，供核销判定）。"""
    row = db.execute("SELECT teacher_quota, student_quota, revision FROM college_quota"
                     " WHERE college=?", (college,)).fetchone()
    teachers = db.execute("SELECT count(*) FROM college_user"
                          " WHERE college=? AND active=1 AND role='teacher'",
                          (college,)).fetchone()[0]
    students = db.execute("SELECT count(*) FROM student_info WHERE college=? AND active=1",
                          (college,)).fetchone()[0]
    tq = row["teacher_quota"] if row else _DEFAULT_TEACHER_QUOTA
    sq = row["student_quota"] if row else _DEFAULT_STUDENT_QUOTA
    return {"college": college, "configured": bool(row),
            "teacher_quota": tq, "student_quota": sq,
            "teachers_used": teachers, "students_used": students,
            "teacher_remaining": max(0, tq - teachers), "student_remaining": max(0, sq - students),
            "revision": row["revision"] if row else 0}


def require_teacher_slot(db, college: str) -> None:
    state = college_quota_state(db, college)
    if state["teacher_remaining"] < 1:
        raise TrainingPortalError(
            f"教师席位已满（{state['teachers_used']}/{state['teacher_quota']}），"
            "请先停用不用的教师账号或调整院校配额。", 409)


def require_student_slot(db, college: str) -> None:
    state = college_quota_state(db, college)
    if state["student_remaining"] < 1:
        raise TrainingPortalError(
            f"学生席位已满（{state['students_used']}/{state['student_quota']}），"
            "请先停用不用的学生或调整院校配额。", 409)


def college_quota_overview(store, college: str, *, history_limit: int = 10) -> dict[str, Any]:
    college = (college or "").strip()
    if not college:
        raise TrainingPortalError("院校必填。")
    with store.connect() as db:
        state = college_quota_state(db, college)
        hist = [dict(r) for r in db.execute(
            """SELECT h.old_teacher_quota, h.old_student_quota, h.new_teacher_quota,
                      h.new_student_quota, h.created_at, u.display_name AS changed_by_name
               FROM college_quota_history h LEFT JOIN college_user u ON u.id=h.changed_by
               WHERE h.college=? ORDER BY h.created_at DESC, h.id LIMIT ?""",
            (college, max(1, min(int(history_limit), 50)))).fetchall()]
    state["history"] = hist
    return state


def set_college_quota(store, actor_id: str, college: str, teacher_quota: int,
                      student_quota: int, revision: int) -> dict[str, Any]:
    """调整院校配额：revision 乐观锁、不得低于当前用量、逐次留痕。"""
    college = (college or "").strip()
    if not college:
        raise TrainingPortalError("院校必填。")
    for value, label in ((teacher_quota, "教师"), (student_quota, "学生")):
        if not isinstance(value, int) or not 1 <= value <= _QUOTA_UPPER:
            raise TrainingPortalError(f"{label}配额须为 1–{_QUOTA_UPPER} 的整数。", 422)
    if not isinstance(revision, int) or revision < 0:
        raise TrainingPortalError("版本信息无效，请刷新后重试。", 422)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT teacher_quota, student_quota, revision FROM college_quota"
                         " WHERE college=?", (college,)).fetchone()
        current_rev = row["revision"] if row else 0
        if current_rev != revision:
            raise TrainingPortalError("配额配置已变化，请刷新后重试。", 409)
        state = college_quota_state(db, college)
        if teacher_quota < state["teachers_used"]:
            raise TrainingPortalError(
                f"教师配额不能低于当前在读教师数（{state['teachers_used']}）。", 422)
        if student_quota < state["students_used"]:
            raise TrainingPortalError(
                f"学生配额不能低于当前在读学生数（{state['students_used']}）。", 422)
        now = _now()
        db.execute("""INSERT INTO college_quota
                      (college, teacher_quota, student_quota, revision, updated_by, updated_at)
                      VALUES (?,?,?,?,?,?)
                      ON CONFLICT(college) DO UPDATE SET
                      teacher_quota=excluded.teacher_quota, student_quota=excluded.student_quota,
                      revision=college_quota.revision+1, updated_by=excluded.updated_by,
                      updated_at=excluded.updated_at""",
                   (college, teacher_quota, student_quota, current_rev + 1, actor_id, now))
        db.execute("""INSERT INTO college_quota_history
                      (id, college, old_teacher_quota, old_student_quota,
                       new_teacher_quota, new_student_quota, changed_by, created_at)
                      VALUES (?,?,?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), college,
                    row["teacher_quota"] if row else None,
                    row["student_quota"] if row else None,
                    teacher_quota, student_quota, actor_id, now))
        state = college_quota_state(db, college)
    return state


def import_staff_users(store, college: str, rows: list[dict[str, Any]], *,
                       default_password: str, created_by: str) -> dict[str, Any]:
    """批量建教师账号（FR-U03）；仅管理员可调用（路由层强制）。

    逐行导入，单行失败不中断整批；重复/错误行带原因返回，不部分静默成功。
    批量导入只建 teacher 角色；admin 账号仍走人工建立，避免批量提权。"""
    _validate_student_password(default_password)
    college = (college or "").strip()
    if not college:
        raise TrainingPortalError("院校必填。")
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        username = str(row.get("username") or "").strip()
        display_name = str(row.get("display_name") or "").strip()
        line_no = f"{username or f'第{index}行'}"
        if not username or re.search(r"\s", username) or len(username) < 2:
            skipped.append({"line": index, "username": username,
                            "reason": "登录名必填、不含空格且至少 2 位。"})
            continue
        if not display_name:
            skipped.append({"line": index, "username": line_no, "reason": "姓名必填。"})
            continue
        try:
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                require_teacher_slot(db, college)  # FR-U06：事务内核销，并发不绕过
                dup = db.execute("SELECT id FROM college_user WHERE username=?", (username,)).fetchone()
                if dup:
                    skipped.append({"line": index, "username": line_no,
                                    "reason": "登录名已存在（全局唯一）。"})
                    continue
                uid = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO college_user
                       (id, username, password_hash, display_name, college, department, email, phone,
                        role, active, created_at)
                       VALUES (?,?,?,?,?,NULL,?,?, 'teacher', 1, ?)""",
                    (uid, username, _passwords.hash(default_password), display_name, college,
                     (str(row.get("email") or "").strip() or None),
                     (str(row.get("phone") or "").strip() or None), _now()))
        except TrainingPortalError as exc:
            # 席位已满等可预见原因按行跳过，不中断整批（不部分静默成功）。
            skipped.append({"line": index, "username": line_no, "reason": str(exc)})
            continue
        created.append({"id": uid, "username": username, "display_name": display_name})
    return {"created": created, "skipped": skipped}


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
            active_version = resolve_active_version(db, cert["id"])
            cert["content_version"] = (
                {"id": active_version["id"], "label": active_version["label"],
                 "year": active_version["year"], "source_type": active_version["source_type"]}
                if active_version else None)
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
    active_version = resolve_active_version(db, cert["id"])
    cert["content_version"] = (
        {"id": active_version["id"], "label": active_version["label"],
         "year": active_version["year"], "source_type": active_version["source_type"]}
        if active_version else None)
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
    links = db.execute(
        "SELECT id, target_type, target_id, created_at FROM knowledge_point_link"
        " WHERE knowledge_point_id=? ORDER BY target_type, target_id", (row["id"],)).fetchall()
    return dict(row) | {"marks": [dict(m) for m in marks], "links": [dict(l) for l in links],
                        "relations": relations_payload(db, row["id"]),
                        "incoming_prerequisite": _incoming_prerequisite(db, row["id"])}


def _incoming_prerequisite(db, kp_id: str) -> list[dict[str, Any]]:
    """哪些知识点把当前知识点当前置（教师视图展示图谱入边）。"""
    rows = db.execute(
        """SELECT r.id, r.basis_ref, r.basis_version, k.id AS kp_id, k.code, k.name
           FROM knowledge_point_relation r JOIN knowledge_point k ON k.id = r.from_kp_id
           WHERE r.to_kp_id=? AND r.relation_type='prerequisite' ORDER BY k.code""", (kp_id,)).fetchall()
    return [dict(r) for r in rows]


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


# ---- 知识点批量导入（FR-K03 录入效率工具）----
# 边界（内容三项决策之二）：工具只导入人工整理并核对过的考纲数据，不生成内容。
# 支持两种格式：
#   checklist —— 官方大纲 PDF 预整理清单（`### 章节点：`CODE` 名称` / `#### 节点：…`）；
#   csv       —— 表头 code,parent_code,name,subject,description,source_ref,outline_version。
# 语义：按 (certificate_id, code) 幂等 upsert；已有行只覆盖解析值非空的字段；
# parent_code 为空表示"不指定上级"（更新时保留原上级，不因部分导入误脱挂）；
# 不触碰 active、三类标注、规则关联与图谱关系。

_KP_IMPORT_MAX_TEXT = 300_000
_KP_IMPORT_MAX_ROWS = 2000


def _checklist_rows(text: str) -> list[dict[str, str]]:
    lines = text.replace("\r\n", "\n").split("\n")
    rows: list[dict[str, str]] = []
    subject = ""
    chapter: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r"^##\s*科目[:：]\s*(.+?)\s*$", line)
        if m:
            # 清单里科目行可能带给录入者的括注（如“（科目字段填：初级会计实务）”），去掉。
            subject = re.sub(r"（[^（）]*）\s*$", "", m.group(1)).strip()
            i += 1
            continue
        m = re.match(r"^###\s*章节点[:：]\s*`([^`]+)`\s*(.*)$", line)
        if m:
            chapter = m.group(1).strip()
            rows.append({"code": chapter, "name": m.group(2).strip(), "subject": subject,
                         "parent_code": "", "description": "", "source_ref": "", "outline_version": ""})
            i += 1
            continue
        m = re.match(r"^####\s*节点[:：]\s*`([^`]+)`\s*(.*)$", line)
        if m:
            rows.append({"code": m.group(1).strip(), "name": m.group(2).strip(), "subject": subject,
                         "parent_code": chapter or "", "description": "", "source_ref": "", "outline_version": ""})
            i += 1
            continue
        m = re.match(r"^-\s*source_ref[:：]\s*`(.+?)`\s*$", line)
        if m and rows:
            rows[-1]["source_ref"] = m.group(1).strip()
            i += 1
            continue
        m = re.match(r"^-\s*描述建议[:：]\s*(.+?)\s*$", line)
        if m and rows:
            rows[-1]["description"] = m.group(1).strip()
            i += 1
            continue
        if re.match(r"^-\s*描述（考点清单[^）]*）[:：]\s*$", line) and rows:
            i += 1
            while i < len(lines) and not lines[i].strip():  # 清单在标记行后有空行
                i += 1
            buf: list[str] = []
            while i < len(lines):
                nxt = lines[i].rstrip()
                if not nxt.strip() or nxt.lstrip().startswith(("#", "- ", "|", ">")):
                    break
                buf.append(nxt.strip().replace("\\.", ".").rstrip())
                i += 1
            rows[-1]["description"] = "\n".join(buf)
            continue
        i += 1
    return rows


def _csv_rows(text: str) -> tuple[list[dict[str, str]], list[str]]:
    expected = ["code", "parent_code", "name", "subject", "description", "source_ref", "outline_version"]
    errors: list[str] = []
    rows: list[dict[str, str]] = []
    header_seen = False
    for lineno, raw in enumerate(csv.reader(io.StringIO(text.lstrip("\ufeff"))), start=1):
        cells = [c.strip() for c in raw]
        if not any(cells):
            continue
        if not header_seen:
            if [c.lower() for c in cells[:7]] == expected:
                header_seen = True
                continue
            errors.append(f"第 {lineno} 行：CSV 首行须为表头 {','.join(expected)}（format=csv）。")
            return [], errors
        if len(cells) < len(expected):
            errors.append(f"第 {lineno} 行：列数不足，应为 {len(expected)} 列。")
            continue
        rows.append(dict(zip(expected, cells[:len(expected)])))
    if not header_seen and not errors:
        errors.append("CSV 缺少表头行。")
    return rows, errors


def parse_kp_import(text: str, fmt: str, *, default_outline_version: str = "") -> tuple[list[dict[str, str]], list[str]]:
    """解析导入文本为知识点行；返回 (rows, errors)。行字段统一为 7 个导入列。"""
    errors: list[str] = []
    if len(text) > _KP_IMPORT_MAX_TEXT:
        raise TrainingPortalError(f"导入文本过长（上限 {_KP_IMPORT_MAX_TEXT} 字符）。")
    if fmt == "checklist":
        rows = _checklist_rows(text)
    elif fmt == "csv":
        rows, errors = _csv_rows(text)
    else:
        raise TrainingPortalError("format 仅支持 checklist 或 csv。")
    if not rows and not errors:
        errors.append("未解析到任何知识点行，请检查格式。")
    if len(rows) > _KP_IMPORT_MAX_ROWS:
        errors.append(f"知识点行数超过上限 {_KP_IMPORT_MAX_ROWS}。")

    by_code: dict[str, dict[str, str]] = {}
    clean: list[dict[str, str]] = []
    for idx, row in enumerate(rows, start=1):
        if not row["code"]:
            errors.append(f"第 {idx} 行：code 不能为空。")
            continue
        if row["code"] in by_code:
            errors.append(f"编码 {row['code']} 在导入内容中重复出现。")
            continue
        if not row["name"]:
            errors.append(f"编码 {row['code']}：name 不能为空。")
            continue
        if row["parent_code"] == row["code"]:
            errors.append(f"编码 {row['code']}：不能以自己为上级。")
            continue
        if not (row["outline_version"] or default_outline_version):
            errors.append(f"编码 {row['code']}：outline_version 为空且未提供默认版本。")
            continue
        by_code[row["code"]] = row
        clean.append(row)

    # 载荷内父级链成环检测（含通过已存在库行的复合环在落库前由 _would_cycle 拦截）。
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(code: str) -> None:
        if code in done or code not in by_code:
            return
        if code in visiting:
            errors.append(f"编码 {code}：导入内容的上级链形成循环。")
            return
        visiting.add(code)
        parent = by_code[code]["parent_code"]
        if parent and parent in by_code and parent not in done:
            walk(parent)
        visiting.discard(code)
        done.add(code)

    for code in list(by_code):
        walk(code)
    return clean, errors


def _plan_kp_import(store, staff: dict, cert_id: str, text: str, fmt: str,
                    default_outline_version: str) -> list[dict[str, Any]]:
    """解析并对照库内现状生成逐行动作计划；不做任何写入。"""
    rows, errors = parse_kp_import(text, fmt, default_outline_version=default_outline_version)
    if errors:
        raise TrainingPortalError("导入内容存在问题，未做任何修改：\n" + "\n".join(errors[:20]))
    plan: list[dict[str, Any]] = []
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在。", 404)
        existing = {r["code"]: dict(r) for r in db.execute(
            "SELECT id, code, name, parent_id FROM knowledge_point WHERE certificate_id=?",
            (cert_id,)).fetchall()}
        for row in rows:
            effective = {k: (row[k].strip() if row[k] else "") for k in row}
            if not effective["outline_version"]:
                effective["outline_version"] = default_outline_version.strip()
            parent_code = effective["parent_code"]
            if parent_code and parent_code not in {r["code"] for r in rows} and parent_code not in existing:
                raise TrainingPortalError(
                    f"编码 {row['code']}：上级编码 {parent_code} 不在导入内容中，也不在该证书下已存在。")
            action = "update" if row["code"] in existing else "insert"
            warnings: list[str] = []
            if not effective["source_ref"]:
                warnings.append("未填考纲依据 source_ref")
            plan.append({
                "code": row["code"], "name": effective["name"], "subject": effective["subject"],
                "description": effective["description"], "source_ref": effective["source_ref"],
                "outline_version": effective["outline_version"], "parent_code": parent_code,
                "action": action, "existing_id": existing[row["code"]]["id"] if action == "update" else None,
                "existing_name": existing[row["code"]]["name"] if action == "update" else None,
                "warnings": warnings})
    return plan


def preview_kp_import(store, staff: dict, cert_id: str, text: str, fmt: str,
                      default_outline_version: str = "") -> dict[str, Any]:
    plan = _plan_kp_import(store, staff, cert_id, text, fmt, default_outline_version)
    summary = {"total": len(plan),
               "insert": sum(1 for p in plan if p["action"] == "insert"),
               "update": sum(1 for p in plan if p["action"] == "update"),
               "warning": sum(1 for p in plan if p["warnings"])}
    return {"plan": plan, "summary": summary}


def apply_kp_import(store, staff: dict, cert_id: str, text: str, fmt: str,
                    default_outline_version: str = "") -> dict[str, Any]:
    plan = _plan_kp_import(store, staff, cert_id, text, fmt, default_outline_version)
    created = updated = 0
    # 拓扑排序插入段：载荷内新插入的父级先于子级落库（子行可出现在父行之前）。
    by_code = {p["code"]: p for p in plan}
    insert_order: list[dict[str, Any]] = []
    state: dict[str, int] = {}

    def visit(item: dict[str, Any]) -> None:
        if state.get(item["code"]) == 1:
            return
        if state.get(item["code"]) == 0:  # parse 阶段已拦截，防御兜底
            raise TrainingPortalError(f"编码 {item['code']}：导入内容的上级链形成循环。")
        state[item["code"]] = 0
        parent = by_code.get(item["parent_code"]) if item["parent_code"] else None
        if parent is not None and parent["action"] == "insert":
            visit(parent)
        state[item["code"]] = 1
        insert_order.append(item)

    for item in (p for p in plan if p["action"] == "insert"):
        visit(item)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ids: dict[str, str] = {r["code"]: r["id"] for r in db.execute(
            "SELECT id, code FROM knowledge_point WHERE certificate_id=?", (cert_id,)).fetchall()}
        # 先插入再更新：同一事务内两遍扫描，保证载荷内父级（新插入行）先于子级存在。
        for phase, ordered in (("insert", insert_order), ("update", [p for p in plan if p["action"] == "update"])):
            for item in ordered:
                now = _now()
                parent_id: str | None = None
                if item["parent_code"]:
                    parent_id = ids.get(item["parent_code"])
                    if not parent_id:
                        raise TrainingPortalError(
                            f"编码 {item['code']}：上级 {item['parent_code']} 未能解析。")
                    if phase == "update":
                        # 库内既有链 + 新上级可能构成环（如互换父子），落库前逐一校验。
                        if parent_id == ids.get(item["code"]) or _would_cycle(db, ids[item["code"]], parent_id):
                            raise TrainingPortalError(
                                f"编码 {item['code']}：修改上级会形成循环层级。")
                if phase == "insert":
                    kp_id = str(uuid.uuid4())
                    db.execute(
                        """INSERT INTO knowledge_point (id, certificate_id, code, name, subject, parent_id,
                           description, source_ref, outline_version, active, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
                        (kp_id, cert_id, item["code"], item["name"], item["subject"], parent_id,
                         item["description"], item["source_ref"], item["outline_version"], now, now))
                    ids[item["code"]] = kp_id
                    created += 1
                else:
                    kp_id = ids[item["code"]]
                    # 幂等覆盖：只覆盖解析值非空的字段；parent_code 为空保留原上级。
                    sets: dict[str, Any] = {"name": item["name"], "updated_at": now}
                    for col in ("subject", "description", "source_ref", "outline_version"):
                        if item[col]:
                            sets[col] = item[col]
                    if parent_id:
                        sets["parent_id"] = parent_id
                    db.execute(f"UPDATE knowledge_point SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                               (*sets.values(), kp_id))
                    updated += 1
    return {"ok": True, "created": created, "updated": updated, "total": len(plan)}


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


def mark_stats(store, staff: dict, cert_id: str) -> dict[str, Any]:
    """教师侧内容运营统计（三类标注口径定版后的报表 v1）。

    口径：标注是内容层事实，统计范围 = 证书 × 标注依据版本（high_freq 的
    basis_version 即考纲版本）；只统计教师维护的标注与题目关联，不含学生
    个人作答数据——咨询频率不等同于风险等级，个人错误不写成公共知识事实。"""
    mark_types = ("high_freq", "risk_context", "error_prone")
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在。", 404)
        kps = db.execute(
            "SELECT id, subject FROM knowledge_point WHERE certificate_id=? AND active=1",
            (cert_id,)).fetchall()
        kp_ids = [r["id"] for r in kps]
        by_subject: dict[str, int] = {}
        for r in kps:
            key = r["subject"] or "（未填科目）"
            by_subject[key] = by_subject.get(key, 0) + 1
        linked = {r["knowledge_point_id"] for r in db.execute(
            f"""SELECT DISTINCT knowledge_point_id FROM knowledge_point_link
                WHERE target_type='rule'
                AND knowledge_point_id IN ({','.join('?' * len(kp_ids)) or "''"})""",
            kp_ids).fetchall()} if kp_ids else set()
        marks = db.execute(
            f"""SELECT m.mark_type, m.basis_version, m.knowledge_point_id FROM knowledge_point_mark m
                WHERE m.knowledge_point_id IN ({','.join('?' * len(kp_ids)) or "''"})""",
            kp_ids).fetchall() if kp_ids else []
        by_type: dict[str, dict[str, Any]] = {}
        for mt in mark_types:
            rows = [m for m in marks if m["mark_type"] == mt]
            points = {m["knowledge_point_id"] for m in rows}
            entry = {"count": len(rows), "points": len(points),
                     "unmarked_points": len(kp_ids) - len(points)}
            if mt == "high_freq":
                versions: dict[str, int] = {}
                for m in rows:
                    v = m["basis_version"] or "（未填版本）"
                    versions[v] = versions.get(v, 0) + 1
                entry["by_basis_version"] = versions
            by_type[mt] = entry
        return {
            "knowledge_points": {"total": len(kp_ids), "with_questions": len(linked),
                                 "without_questions": len(kp_ids) - len(linked),
                                 "by_subject": by_subject},
            "marks": by_type,
            "generated_at": _now(),
        }


# ---------------------------------------------------------------------------
# 知识点-题目关联（FR-K05）
# ---------------------------------------------------------------------------

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"


def _load_rules() -> list[Any]:
    from src import engine
    return engine.load_rules(RULES_DIR)


def create_link(store, staff: dict, kp_id: str, target_type: str, target_id: str) -> dict[str, Any]:
    if target_type not in ("rule", "task", "question"):
        raise TrainingPortalError("关联类型必须是 rule / task / question。")
    target_id = str(target_id).strip()
    if not target_id:
        raise TrainingPortalError("关联目标不能为空。")
    if target_type == "question":
        raise TrainingPortalError("题库尚未接入：当前练习以规则仿真题承载，请先关联规则或实训任务。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM knowledge_point WHERE id=?", (kp_id,)).fetchone():
            raise TrainingPortalError("知识点不存在。", 404)
        if target_type == "rule" and target_id not in {r.id for r in _load_rules()}:
            raise TrainingPortalError("规则不存在或未启用。")
        if target_type == "task" and not db.execute(
                "SELECT 1 FROM training_task WHERE id=?", (target_id,)).fetchone():
            raise TrainingPortalError("实训任务不存在。")
        link_id = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT INTO knowledge_point_link (id, knowledge_point_id, target_type, target_id, created_by, created_at)"
                " VALUES (?,?,?,?,?,?)", (link_id, kp_id, target_type, target_id, staff["id"], _now()))
        except sqlite3.IntegrityError:
            raise TrainingPortalError("该知识点已存在相同关联。", 409) from None
        return {"id": link_id, "knowledge_point_id": kp_id, "target_type": target_type,
                "target_id": target_id}


def delete_link(store, staff: dict, link_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM knowledge_point_link WHERE id=?", (link_id,)).fetchone():
            raise TrainingPortalError("关联不存在。", 404)
        db.execute("DELETE FROM knowledge_point_link WHERE id=?", (link_id,))
    return {"ok": True}


def student_knowledge_points(store, student_id: str, cert_id: str) -> list[dict[str, Any]]:
    """学生视角的知识点清单：启用知识点 + 高频标注（带依据）+ 规则关联数 + 本人覆盖统计。"""
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=? AND active=1", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在或已下架。", 404)
        rows = db.execute(
            "SELECT * FROM knowledge_point WHERE certificate_id=? AND active=1 ORDER BY code",
            (cert_id,)).fetchall()
        rule_counts = {r["knowledge_point_id"]: r["n"] for r in db.execute(
            "SELECT knowledge_point_id, count(*) n FROM knowledge_point_link"
            " WHERE target_type='rule' AND knowledge_point_id IN (SELECT id FROM knowledge_point"
            " WHERE certificate_id=?) GROUP BY knowledge_point_id", (cert_id,)).fetchall()}
        coverage: dict[str, dict[str, int]] = {}
        for r in db.execute(
                "SELECT id, knowledge_point_id, result_json FROM training_self_practice_attempts"
                " WHERE student_id=? AND certificate_id=? AND status='scored' AND knowledge_point_id IS NOT NULL",
                (student_id, cert_id)).fetchall():
            entry = coverage.setdefault(r["knowledge_point_id"], {"attempts": 0, "perfect": 0})
            entry["attempts"] += 1
            try:
                if json.loads(r["result_json"] or "{}").get("perfect"):
                    entry["perfect"] += 1
            except ValueError:
                continue
        out = []
        for row in rows:
            marks = db.execute(
                "SELECT mark_type, level, basis_ref, basis_version FROM knowledge_point_mark"
                " WHERE knowledge_point_id=? AND mark_type='high_freq'", (row["id"],)).fetchall()
            item = dict(row)
            item["high_freq"] = [dict(m) for m in marks]
            item["rule_links"] = rule_counts.get(row["id"], 0)
            item["relations"] = relations_payload(db, row["id"])
            stat = coverage.get(row["id"], {"attempts": 0, "perfect": 0})
            item["attempts"] = stat["attempts"]
            item["perfect"] = stat["perfect"]
            out.append(item)
        return out


# ---------------------------------------------------------------------------
# 自主刷题闭环（FR-K06）：确定性出题 → 作答 → 判分 → 解析与错题
# ---------------------------------------------------------------------------

def _generate(rules: list[Any], rule_id: str, seed: int):
    from src import exercise_generator
    try:
        return exercise_generator.generate(rules, rule_id, seed=seed)
    except ValueError as exc:
        raise TrainingPortalError(f"出题失败：{exc}") from None


def _materials(exercise) -> dict[str, Any]:
    from dataclasses import asdict
    return json.loads(json.dumps(asdict(exercise.dataset), ensure_ascii=False, default=str))


def _candidate_rules(db, cert_id: str, kp_id: str | None) -> list[str]:
    if kp_id:
        rows = db.execute(
            "SELECT target_id FROM knowledge_point_link WHERE knowledge_point_id=? AND target_type='rule'",
            (kp_id,)).fetchall()
    else:
        rows = db.execute(
            "SELECT DISTINCT l.target_id FROM knowledge_point_link l JOIN knowledge_point k ON k.id=l.knowledge_point_id"
            " WHERE l.target_type='rule' AND k.certificate_id=? AND k.active=1", (cert_id,)).fetchall()
    return sorted({r["target_id"] for r in rows})


def start_practice(store, student_id: str, cert_id: str, kp_id: str | None = None,
                   rule_id: str | None = None, seed: int | None = None,
                   mode: str = "new") -> dict[str, Any]:
    if mode not in ("new", "redo_same"):
        raise TrainingPortalError("作答模式必须是 new 或 redo_same。")
    if mode == "redo_same" and (rule_id is None or seed is None):
        raise TrainingPortalError("原题重做必须显式提供规则与种子。")
    rules = _load_rules()
    by_id = {r.id: r for r in rules}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM certificate WHERE id=? AND active=1", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在或已下架。", 404)
        if kp_id:
            kp = db.execute("SELECT certificate_id, active FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()
            if not kp or kp["certificate_id"] != cert_id or not kp["active"]:
                raise TrainingPortalError("知识点不存在或未启用。", 404)
        if rule_id:
            if rule_id not in by_id:
                raise TrainingPortalError("所选规则不存在或未启用。")
            if kp_id:
                linked = db.execute(
                    "SELECT 1 FROM knowledge_point_link WHERE knowledge_point_id=? AND target_type='rule' AND target_id=?",
                    (kp_id, rule_id)).fetchone()
                if not linked:
                    prior = db.execute(
                        "SELECT 1 FROM training_self_practice_attempts WHERE student_id=? AND knowledge_point_id=? AND rule_id=?",
                        (student_id, kp_id, rule_id)).fetchone()
                    if not prior:
                        raise TrainingPortalError("该规则未关联到此知识点，请从关联规则中练习。")
        else:
            candidates = [c for c in _candidate_rules(db, cert_id, kp_id) if c in by_id]
            if not candidates:
                raise TrainingPortalError("该范围还没有可练题目：知识点尚未关联规则，请等待教师配置。")
            rule_id = secrets.choice(candidates)
        # 绑定当前内容版本（FR-K08）：证书尚无版本时自动建立仿真基线版本。
        version = ensure_active_version(db, cert_id, rules)
    if seed is None:
        seed = secrets.randbelow(2147483648)
    exercise = _generate(rules, rule_id, seed)
    from src.exercise_generator import case_digest
    digest = case_digest(exercise.dataset, rules)
    attempt_id = str(uuid.uuid4())
    with store.connect() as db:
        db.execute(
            """INSERT INTO training_self_practice_attempts (id, student_id, certificate_id, knowledge_point_id,
               rule_id, seed, level, year, digest, status, content_version_id, mode, created_at)
               VALUES (?,?,?,?,?,?, 'normal', ?, ?, 'open', ?, ?, ?)""",
            (attempt_id, student_id, cert_id, kp_id, rule_id, seed, version["year"], digest,
             version["id"], mode, _now()))
        row = db.execute("SELECT * FROM training_self_practice_attempts WHERE id=?", (attempt_id,)).fetchone()
        return _open_view(exercise, row, rules, db=db)


def _open_view(exercise, row: Any, rules: list[Any], db=None) -> dict[str, Any]:
    """作中视图：材料与规则目录下发，答案（rule_id）绝不下发。"""
    view = {
        "id": row["id"], "status": "open",
        "certificate_id": row["certificate_id"], "knowledge_point_id": row["knowledge_point_id"],
        "level": row["level"], "year": row["year"], "created_at": row["created_at"],
        "mode": row["mode"], "skipped": bool(row["skipped_at"]),
        "materials": _materials(exercise),
        "rule_catalog": sorted(({"id": r.id, "name": r.name} for r in rules), key=lambda x: x["name"]),
    }
    if db is not None:
        view["content_version"] = version_brief(db, row["content_version_id"])
        view["applicability"] = applicability(db, row["certificate_id"], row["content_version_id"])
    return view


def _attempt_row(store, student_id: str, attempt_id: str) -> sqlite3.Row:
    with store.connect() as db:
        return db.execute("SELECT * FROM training_self_practice_attempts WHERE id=? AND student_id=?",
                          (attempt_id, student_id)).fetchone() or (_ for _ in ()).throw(
        TrainingPortalError("练习记录不存在。", 404))


def get_attempt(store, student_id: str, attempt_id: str) -> dict[str, Any]:
    row = _attempt_row(store, student_id, attempt_id)
    if row["status"] == "scored":
        with store.connect() as db:
            return _scored_view(row, db)
    rules = _load_rules()
    exercise = _generate(rules, row["rule_id"], row["seed"])
    with store.connect() as db:
        return _open_view(exercise, row, rules, db=db)


def submit_practice(store, student_id: str, attempt_id: str, selected: list[str]) -> dict[str, Any]:
    chosen = sorted({s.strip() for s in selected if isinstance(s, str) and s.strip()})
    row = _attempt_row(store, student_id, attempt_id)
    if row["status"] != "open":
        raise TrainingPortalError("本题已判分，重复提交不重复计数。", 409)
    rules = _load_rules()
    by_id = {r.id: r for r in rules}
    if row["rule_id"] not in by_id:
        raise TrainingPortalError("本题规则已被停用，无法判分；请放弃本题重新开始练习。")
    exercise = _generate(rules, row["rule_id"], row["seed"])
    from src.exercise_generator import case_digest
    if case_digest(exercise.dataset, rules) != row["digest"]:
        raise TrainingPortalError("规则已更新，本题材料发生变化；本次作答未计入，请放弃并重新开始本题。")
    from src.training import score_submission
    try:
        result = score_submission(exercise.findings, chosen)
    except ValueError as exc:
        raise TrainingPortalError(str(exc)) from None
    result["perfect"] = not result["missed"] and not result["false_positives"]
    payload = json.dumps(result, ensure_ascii=False)
    with store.connect() as db:
        # result_json 为受保护列：写入库前按字段加密惯例封装（读取时行工厂自动解密）
        sealed = db.field_codec.seal(payload, "training_self_practice_attempts", "result_json", attempt_id)
        db.execute(
            "UPDATE training_self_practice_attempts SET status='scored', answers_json=?, result_json=?, scored_at=? WHERE id=?",
            (json.dumps(chosen, ensure_ascii=False), sealed, _now(), attempt_id))
        row = db.execute("SELECT * FROM training_self_practice_attempts WHERE id=?", (attempt_id,)).fetchone()
        return _scored_view(row, db)


def _scored_view(row: sqlite3.Row, db=None) -> dict[str, Any]:
    result = json.loads(row["result_json"] or "{}")
    view = {
        "id": row["id"], "status": "scored", "certificate_id": row["certificate_id"],
        "knowledge_point_id": row["knowledge_point_id"], "rule_id": row["rule_id"],
        "seed": row["seed"], "level": row["level"], "year": row["year"], "mode": row["mode"],
        "answers": json.loads(row["answers_json"] or "[]"),
        "result": result, "score": result.get("score"), "perfect": result.get("perfect"),
        "created_at": row["created_at"], "scored_at": row["scored_at"],
    }
    if db is not None:
        view["content_version"] = version_brief(db, row["content_version_id"])
        view["applicability"] = applicability(db, row["certificate_id"], row["content_version_id"])
    return view


def list_attempts(store, student_id: str, *, cert_id: str | None = None,
                  status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses, params = ["WHERE student_id=?"], [student_id]
    if cert_id:
        clauses.append("AND certificate_id=?"); params.append(cert_id)
    if status in ("open", "scored"):
        clauses.append("AND status=?"); params.append(status)
    if status == "open":
        clauses.append("AND skipped_at IS NULL")  # 跳过的作答不再出现在"未完成"里
    params.append(max(1, min(int(limit), 200)))
    out = []
    with store.connect() as db:
        rows = db.execute(
            f"SELECT * FROM training_self_practice_attempts {' '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
            params).fetchall()
        for row in rows:
            if row["status"] == "scored":
                view = _scored_view(row, db)
            else:
                view = {"id": row["id"], "status": "open", "certificate_id": row["certificate_id"],
                        "knowledge_point_id": row["knowledge_point_id"], "created_at": row["created_at"],
                        "mode": row["mode"], "skipped": bool(row["skipped_at"])}
            out.append(view)
    return out


def skip_practice(store, student_id: str, attempt_id: str) -> dict[str, Any]:
    """跳过：只标记不判分，留痕供统计；已判分的作答不可跳过（跳过不计入正确率）。"""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM training_self_practice_attempts WHERE id=? AND student_id=?",
            (attempt_id, student_id)).fetchone()
        if not row:
            raise TrainingPortalError("作答记录不存在。", 404)
        if row["status"] == "scored":
            raise TrainingPortalError("该题已判分，不能跳过。", 409)
        if not row["skipped_at"]:
            db.execute("UPDATE training_self_practice_attempts SET skipped_at=? WHERE id=?",
                       (_now(), attempt_id))
        return {"attempt_id": attempt_id, "skipped": True}


def practice_stats(store, student_id: str, cert_id: str) -> dict[str, Any]:
    """作答细分统计（FR-K05/K06 待完善项）：新题/原题重做/未作答即交/跳过分开列示。

    口径：accuracy=满分/已判分；未作答即交（空选择）单独计数，不与新题正确率混算；
    跳过不计入任何正确率。"""
    def bucket(mode: str) -> dict[str, Any]:
        return {"attempts": 0, "perfect": 0, "accuracy": None}

    stats = {"new": bucket("new"), "redo_same": bucket("redo_same"),
             "unanswered": {"attempts": 0, "perfect": 0, "accuracy": None}, "skipped": 0}
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在。", 404)
        rows = db.execute(
            """SELECT id, mode, answers_json, skipped_at, result_json FROM training_self_practice_attempts
               WHERE student_id=? AND certificate_id=?""", (student_id, cert_id)).fetchall()
    for r in rows:
        if r["skipped_at"]:
            stats["skipped"] += 1
            continue
        if r["result_json"] is None:
            continue  # 作答中，不计入统计
        try:
            result = json.loads(r["result_json"] or "{}")
        except ValueError:
            result = {}
        perfect = bool(result.get("perfect"))
        empty = not json.loads(r["answers_json"] or "[]")
        key = "unanswered" if empty else ("redo_same" if r["mode"] == "redo_same" else "new")
        stats[key]["attempts"] += 1
        if perfect:
            stats[key]["perfect"] += 1
    for b in stats.values():
        if isinstance(b, dict) and b["attempts"]:
            b["accuracy"] = round(b["perfect"] / b["attempts"], 4)
    return stats


def wrong_attempts(store, student_id: str, cert_id: str | None = None) -> list[dict[str, Any]]:
    """错题重练清单：已判分且未满分的尝试，供按规则再练。"""
    rows = [v for v in list_attempts(store, student_id, cert_id=cert_id, status="scored", limit=200)
            if not v.get("perfect")]
    return rows


def coverage(store, student_id: str, cert_id: str) -> list[dict[str, Any]]:
    with store.connect() as db:
        kps = db.execute(
            "SELECT id, code, name, subject, parent_id FROM knowledge_point"
            " WHERE certificate_id=? AND active=1 ORDER BY code", (cert_id,)).fetchall()
        stats: dict[str, dict[str, int]] = {}
        for r in db.execute(
                "SELECT id, knowledge_point_id, result_json FROM training_self_practice_attempts"
                " WHERE student_id=? AND certificate_id=? AND status='scored' AND knowledge_point_id IS NOT NULL",
                (student_id, cert_id)).fetchall():
            entry = stats.setdefault(r["knowledge_point_id"], {"attempts": 0, "perfect": 0})
            entry["attempts"] += 1
            try:
                if json.loads(r["result_json"] or "{}").get("perfect"):
                    entry["perfect"] += 1
            except ValueError:
                continue
        rule_counts = {r["knowledge_point_id"]: r["n"] for r in db.execute(
            "SELECT knowledge_point_id, count(*) n FROM knowledge_point_link WHERE target_type='rule'"
            " GROUP BY knowledge_point_id").fetchall()}
        return [dict(k) | {"rule_links": rule_counts.get(k["id"], 0),
                           "attempts": stats.get(k["id"], {}).get("attempts", 0),
                           "perfect": stats.get(k["id"], {}).get("perfect", 0)} for k in kps]


# ---------------------------------------------------------------------------
# 学生目标工作台（FR-K07）：聚合目标/倒计时、覆盖、常考、薄弱点与可解释推荐
# ---------------------------------------------------------------------------

# 复习提醒口径：较熟练知识点超过该天数未作答即安排一次巩固（确定性规则，可解释）。
REVIEW_STALE_DAYS = 7


def _attempt_evidence(db, student_id: str, cert_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    """作答证据聚合（口径与作答细分一致）：返回 (按知识点聚合, 总计, 明细)。

    跳过留痕计数但不计入正确率与掌握程度；作答中（result_json 为空）不计入；
    整证随机作答（knowledge_point_id 为空）计入总计、不映射到知识点。"""
    agg: dict[str, dict[str, Any]] = {}
    total = {"attempts": 0, "perfect": 0, "skipped": 0}
    detail: list[dict[str, Any]] = []
    for r in db.execute(
            """SELECT id, knowledge_point_id, rule_id, skipped_at, result_json, created_at, scored_at
               FROM training_self_practice_attempts
               WHERE student_id=? AND certificate_id=?
               ORDER BY scored_at, created_at""", (student_id, cert_id)).fetchall():
        if r["skipped_at"]:
            total["skipped"] += 1
            continue
        if r["result_json"] is None:
            continue
        try:
            result = json.loads(r["result_json"] or "{}")
        except ValueError:
            result = {}
        perfect = bool(result.get("perfect"))
        total["attempts"] += 1
        total["perfect"] += int(perfect)
        detail.append({"id": r["id"], "knowledge_point_id": r["knowledge_point_id"],
                       "rule_id": r["rule_id"], "created_at": r["created_at"],
                       "scored_at": r["scored_at"], "perfect": perfect})
        if not r["knowledge_point_id"]:
            continue
        e = agg.setdefault(r["knowledge_point_id"],
                           {"attempts": 0, "perfect": 0, "last_perfect": None, "last_scored_at": None})
        e["attempts"] += 1
        e["perfect"] += int(perfect)
        e["last_perfect"] = perfect
        e["last_scored_at"] = r["scored_at"]
    return agg, total, detail


def dashboard(store, student_id: str) -> dict[str, Any]:
    """工作台聚合视图。推荐为确定性规则（非算法画像）：续作 > 错题重练 >
    巩固中补练 > 常考未练 > 未练补覆盖 > 较熟练超期复习；无历史不虚构，
    题量不足说明缺口，不展示未经验证的通过概率；每条建议带原因并可直达内容。
    plan 为完整版学习安排（FR-K 剩余项），recommendation 保留为首条建议。"""
    with store.connect() as db:
        goal_rows = db.execute(
            "SELECT * FROM student_goal WHERE student_id=? AND status='active' ORDER BY created_at",
            (student_id,)).fetchall()
        goals = []
        for row in goal_rows:
            payload = _goal_payload(db, row)
            cert_id = row["certificate_id"]
            cert = db.execute("SELECT name FROM certificate WHERE id=?", (cert_id,)).fetchone()
            kps = db.execute(
                "SELECT id, code, name FROM knowledge_point WHERE certificate_id=? AND active=1 ORDER BY code",
                (cert_id,)).fetchall()
            stats, totals, attempts = _attempt_evidence(db, student_id, cert_id)
            rule_links = {r["knowledge_point_id"]: r["n"] for r in db.execute(
                "SELECT knowledge_point_id, count(*) n FROM knowledge_point_link WHERE target_type='rule'"
                " AND knowledge_point_id IN (SELECT id FROM knowledge_point WHERE certificate_id=?)"
                " GROUP BY knowledge_point_id", (cert_id,)).fetchall()}
            high_freq = [dict(h) for h in db.execute(
                """SELECT k.id, k.code, k.name, m.basis_ref, m.basis_version FROM knowledge_point k
                   JOIN knowledge_point_mark m ON m.knowledge_point_id = k.id
                   WHERE k.certificate_id=? AND k.active=1 AND m.mark_type='high_freq'
                   ORDER BY k.code""", (cert_id,)).fetchall()]
            open_rows = [dict(o) for o in db.execute(
                "SELECT id, created_at FROM training_self_practice_attempts WHERE student_id=? AND certificate_id=?"
                " AND status='open' ORDER BY created_at DESC LIMIT 3", (student_id, cert_id)).fetchall()]
            # 错题重练只针对从未满分过的知识点：已满分过的不再推荐重练
            unmastered = {k for k in stats if stats[k]["perfect"] == 0}
            wrong = [a for a in attempts if not a["perfect"] and a["knowledge_point_id"] in unmastered]
            wrong_count: dict[tuple[str, str], int] = {}
            for a in wrong:
                key = (a["knowledge_point_id"], a["rule_id"])
                wrong_count[key] = wrong_count.get(key, 0) + 1
            covered = len(stats)
            total = len(kps)
            weak_ids = sorted((k for k in stats if stats[k]["perfect"] == 0),
                              key=lambda k: -stats[k]["attempts"])[:5]
            weak = [{"id": k["id"], "code": k["code"], "name": k["name"],
                     "attempts": stats[k["id"]]["attempts"]} for k in kps if k["id"] in weak_ids]
            kp_names = {k["id"]: dict(k) for k in kps}
            # 可解释推荐（按优先级取第一条）
            recommendation = None
            if open_rows:
                recommendation = {"kind": "resume", "attempt_id": open_rows[0]["id"],
                                  "reason": "有一道未完成的练习，继续作答即可判分并开放解析。"}
            elif wrong:
                (wk_id, wr_id) = next(iter(wrong_count))
                name = kp_names.get(wk_id, {}).get("name", "知识点")
                recommendation = {"kind": "practice", "certificate_id": cert_id, "knowledge_point_id": wk_id,
                                  "rule_id": wr_id,
                                  "reason": f"「{name}」已练 {wrong_count[(wk_id, wr_id)]} 次未满分，建议重练错题。"}
            else:
                untried = [k for k in kps if k["id"] not in stats and rule_links.get(k["id"], 0) > 0]
                hot = [k for k in untried if any(h["id"] == k["id"] for h in high_freq)]
                pick = hot[0] if hot else (untried[0] if untried else None)
                if pick is not None:
                    reason = "常考知识点还未练习，建议从它开始。" if hot else "还有未练过的知识点。"
                    recommendation = {"kind": "practice", "certificate_id": cert_id,
                                      "knowledge_point_id": pick["id"], "reason": reason}
                elif stats:
                    weakest = min(stats.items(), key=lambda kv: kv[1]["perfect"] / max(kv[1]["attempts"], 1))[0]
                    recommendation = {"kind": "practice", "certificate_id": cert_id, "knowledge_point_id": weakest,
                                      "reason": "已练知识点全部满分，可巩固正确率最低的知识点。"}
                else:
                    recommendation = {"kind": "none",
                                      "reason": "该证书下还没有关联题目的知识点，等待教师配置后即可练习。"}
            # 掌握程度汇总（口径见 mastery_level）：展示练习量/正确率之外的掌握分布。
            def _level(kp_id: str) -> tuple[str, str | None]:
                e = stats.get(kp_id)
                n = e["attempts"] if e else 0
                acc = (e["perfect"] / n) if e and n else None
                return mastery_level(n, acc, e["last_perfect"] if e else None), (
                    e["last_scored_at"] if e else None)

            mastery = {"unstarted": 0, "acquainted": 0, "consolidating": 0, "proficient": 0}
            for k in kps:
                level, _ = _level(k["id"])
                mastery[level] += 1
            # 完整版学习安排：续作 > 错题复核 > 巩固中补练 > 常考未练 > 补覆盖 > 超期复习。
            plan: list[dict[str, Any]] = []
            if open_rows:
                plan.append({"kind": "resume", "attempt_id": open_rows[0]["id"],
                             "title": "续作未完成的练习",
                             "reason": "有一道未完成的练习，继续作答即可判分并开放解析。"})
            for (wk_id, wr_id), cnt in sorted(wrong_count.items(), key=lambda kv: -kv[1])[:3]:
                wname = kp_names.get(wk_id, {}).get("name", "知识点")
                plan.append({"kind": "redo", "certificate_id": cert_id, "knowledge_point_id": wk_id,
                             "rule_id": wr_id, "title": f"重练「{wname}」错题",
                             "reason": f"已练 {cnt} 次未满分，从错题入手见效最快。"})
            consolidating = [k for k in kps if _level(k["id"])[0] == "consolidating"
                             and rule_links.get(k["id"], 0) > 0]
            for k in consolidating[:3]:
                plan.append({"kind": "practice", "certificate_id": cert_id, "knowledge_point_id": k["id"],
                             "title": f"巩固「{k['name']}」",
                             "reason": "掌握程度为巩固中（正确率不足或最近一次未满分），再练新题巩固。"})
            untried_with_q = [k for k in kps if k["id"] not in stats and rule_links.get(k["id"], 0) > 0]
            hot_ids = {h["id"] for h in high_freq}
            for k in [k for k in untried_with_q if k["id"] in hot_ids][:3]:
                plan.append({"kind": "practice", "certificate_id": cert_id, "knowledge_point_id": k["id"],
                             "title": f"新学常考「{k['name']}」",
                             "reason": "常考知识点还未练习，建议优先覆盖。"})
            for k in [k for k in untried_with_q if k["id"] not in hot_ids][:3]:
                plan.append({"kind": "practice", "certificate_id": cert_id, "knowledge_point_id": k["id"],
                             "title": f"新学「{k['name']}」", "reason": "还未练习的知识点，补全覆盖面。"})
            now_cst = datetime.now(CST)
            for k in kps:
                level, last_at = _level(k["id"])
                if level != "proficient" or not last_at:
                    continue
                try:
                    last_day = datetime.fromisoformat(last_at)
                    if last_day.tzinfo is None:
                        last_day = last_day.replace(tzinfo=CST)
                except ValueError:
                    continue
                if (now_cst - last_day).days >= REVIEW_STALE_DAYS:
                    plan.append({"kind": "review", "certificate_id": cert_id, "knowledge_point_id": k["id"],
                                 "title": f"复习「{k['name']}」",
                                 "reason": f"已较熟练但超过 {REVIEW_STALE_DAYS} 天未练，安排一次巩固防遗忘。"})
            plan = plan[:8]
            # 剩余时间节奏提示：确定性换算，不作任何通过率承诺。
            countdown = payload["countdown"]
            days_left = countdown["days_left"] if countdown and not countdown.get("expired") else None
            remaining = total - covered
            pacing: dict[str, Any] | None = None
            if remaining <= 0:
                pacing = {"remaining_kps": 0, "days_left": days_left, "per_day": 0,
                          "note": "知识点已全部练过，可按掌握程度安排巩固与复习。"}
            elif days_left is not None:
                per_day = -(-remaining // days_left) if days_left > 0 else remaining
                pacing = {"remaining_kps": remaining, "days_left": days_left, "per_day": min(per_day, remaining),
                          "note": (f"距考试 {days_left} 天，剩余 {remaining} 个知识点未练，约每天 {per_day} 个即可全部覆盖。"
                                   if days_left > 0 else
                                   f"考试就在今天，剩余 {remaining} 个知识点未练，按状态量力安排。")}
            gap_kps = sum(1 for k in kps if rule_links.get(k["id"], 0) == 0)
            goals.append({
                "goal_id": row["id"], "certificate_id": cert_id,
                "certificate_name": cert["name"] if cert else "",
                "countdown": payload["countdown"], "countdown_source": payload["countdown_source"],
                "official_date": payload["official_date"], "planned_date": payload["planned_date"],
                "coverage": {"total": total, "covered": covered,
                             "attempts": totals["attempts"], "perfect": totals["perfect"],
                             "skipped": totals["skipped"]},
                "mastery": mastery, "plan": plan, "pacing": pacing,
                "gaps": {"kps_without_questions": gap_kps},
                "high_freq": high_freq, "weak": weak,
                "recommendation": recommendation,
                "open_attempts": open_rows,
            })
        return {"goals": goals,
                "hint": "" if goals else "还没有考证目标：在下方证书目录中选择「设为我的目标」即可开始，目标不依赖教师建班。"}


# ---------------------------------------------------------------------------
# 个人学习档案与知识库（FR-K 剩余项 v1）+ 掌握程度口径（FR-K07 待定义项定版）
# ---------------------------------------------------------------------------

MASTERY_LABELS = {"unstarted": "未开始", "acquainted": "初识",
                  "consolidating": "巩固中", "proficient": "较熟练"}


def mastery_level(attempts_n: int, accuracy: float | None, last_perfect: bool | None) -> str:
    """掌握程度口径（v1）：确定性聚合规则，可解释、不声称"已掌握"。

    - unstarted 未开始：无已判分非跳过作答；
    - acquainted 初识：1–2 次作答（一次重练答对不代表长期掌握，次数不足不升级）；
    - consolidating 巩固中：≥3 次作答但正确率 < 80%，或最近一次未满分（需复核）；
    - proficient 较熟练：≥3 次作答、正确率 ≥ 80% 且最近一次满分。
    纯聚合计算、无隐藏状态：最近表现下降自然回落；跳过作答不计入次数与正确率
    （与作答细分口径一致）。展示层附 attempts/accuracy/last_perfect 供解释。"""
    if attempts_n <= 0:
        return "unstarted"
    if attempts_n < 3:
        return "acquainted"
    if accuracy is not None and accuracy >= 0.8 and last_perfect:
        return "proficient"
    return "consolidating"


def student_profile(store, student_id: str, cert_id: str) -> dict[str, Any]:
    """个人学习档案：按知识点聚合历次作答、错题、笔记与答疑提问（FR-K v1）。

    全部按本人隔离；聚合只读，不改作答历史（验收：修改目标不清空历史）。
    三类标注统计口径：标注是内容层事实（证书 × outline_version 范围），学生端
    仅展示考证高频；个人易错来自本人作答统计（错题/掌握程度），与教师维护的
    error_prone 标注分开呈现，不把个人错误写成公共知识事实。"""
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise TrainingPortalError("证书不存在。", 404)
        cert = db.execute("SELECT name FROM certificate WHERE id=?", (cert_id,)).fetchone()
        kps = db.execute(
            "SELECT id, code, name, subject FROM knowledge_point"
            " WHERE certificate_id=? AND active=1 ORDER BY code", (cert_id,)).fetchall()
        kp_ids = {r["id"] for r in kps}

        agg, total, _detail = _attempt_evidence(db, student_id, cert_id)

        notes: dict[str, dict[str, Any]] = {}
        for r in db.execute(
                """SELECT knowledge_point_id, content, updated_at FROM student_kp_notes
                   WHERE student_id=?""", (student_id,)).fetchall():
            if r["knowledge_point_id"] in kp_ids:
                notes[r["knowledge_point_id"]] = dict(r)
        tutor_counts = {r["knowledge_point_id"]: r["n"] for r in db.execute(
            """SELECT knowledge_point_id, count(*) n FROM training_tutor_messages
               WHERE student_id=? AND certificate_id=? AND knowledge_point_id IS NOT NULL
               GROUP BY knowledge_point_id""", (student_id, cert_id)).fetchall()}

        items = []
        for k in kps:
            stat = agg.get(k["id"], {"attempts": 0, "perfect": 0,
                                     "last_perfect": None, "last_scored_at": None})
            n = stat["attempts"]
            accuracy = round(stat["perfect"] / n, 4) if n else None
            level = mastery_level(n, accuracy, stat["last_perfect"])
            note = notes.get(k["id"])
            items.append({
                "id": k["id"], "code": k["code"], "name": k["name"], "subject": k["subject"],
                "mastery": {"level": level, "label": MASTERY_LABELS[level],
                            "attempts": n, "accuracy": accuracy,
                            "perfect": stat["perfect"], "last_perfect": stat["last_perfect"],
                            "last_scored_at": stat["last_scored_at"]},
                "wrong": n - stat["perfect"],
                "note": {"content": note["content"], "updated_at": note["updated_at"]} if note else None,
                "tutor_questions": tutor_counts.get(k["id"], 0),
            })
        goal_row = db.execute(
            "SELECT * FROM student_goal WHERE student_id=? AND certificate_id=? AND status='active'",
            (student_id, cert_id)).fetchone()
        goal_payload = _goal_payload(db, goal_row) if goal_row else None
        covered = sum(1 for i in items if i["mastery"]["attempts"] > 0)
        return {
            "certificate": {"id": cert_id, "name": cert["name"] if cert else ""},
            "goal": goal_payload,
            "summary": {
                "total_kps": len(items), "covered_kps": covered,
                "coverage_rate": round(covered / len(items), 4) if items else None,
                "proficient_kps": sum(1 for i in items if i["mastery"]["level"] == "proficient"),
                "attempts": total["attempts"], "skipped": total["skipped"],
                "accuracy": round(total["perfect"] / total["attempts"], 4) if total["attempts"] else None,
                "notes": len(notes),
                "tutor_questions": sum(tutor_counts.values()),
            },
            "knowledge_points": items,
        }


def _md_cell(text: Any) -> str:
    """Markdown 表格单元格转义：竖线与换行不破坏表格结构。"""
    return str(text if text is not None else "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def student_profile_export(store, student_id: str, cert_id: str) -> tuple[str, str]:
    """学生本人自助导出学习档案为 Markdown（档案导出细则 v1，只读）。

    边界：导出是快照副本，不改动在线作答/笔记/答疑数据；按本人隔离，
    教师不设导出入口；导出内容与 /api/training/my/profile 同一口径
    （掌握程度四级、跳过不计入正确率），不额外补造数据。"""
    profile = student_profile(store, student_id, cert_id)
    s = profile["summary"]
    generated = _now()[:19].replace("T", " ")
    pct = lambda v: "—" if v is None else f"{round(v * 100, 1)}%"
    lines: list[str] = [
        f"# 学习档案导出 · {profile['certificate']['name']}",
        "",
        f"- 导出时间：{generated}（本文件为导出时刻的快照副本，不代表之后的学习记录）",
        "- 掌握程度口径：未开始 / 初识（1-2 次）/ 巩固中 / 较熟练（≥3 次且正确率≥80% 且最近一次满分）；跳过不计入正确率。",
        "",
    ]
    if profile["goal"]:
        g = profile["goal"]
        target = (g["official_date"]["exam_date"] if g.get("official_date")
                  else g.get("planned_date")) or "未定"
        lines.append(f"- 学习目标：{g['certificate']['name'] if g['certificate'] else ''}"
                     f"（状态：{g['status']}；目标日期：{target}）")
    lines += [
        "",
        "## 总览",
        "",
        f"- 知识点覆盖：{s['covered_kps']}/{s['total_kps']}（{pct(s['coverage_rate'])}）",
        f"- 较熟练知识点：{s['proficient_kps']} 个",
        f"- 累计作答：{s['attempts']} 次，总正确率 {pct(s['accuracy'])}（跳过 {s['skipped']} 次，不计入）",
        f"- 笔记 {s['notes']} 条 · 答疑提问 {s['tutor_questions']} 次",
        "",
        "## 知识点明细",
        "",
        "| 编码 | 知识点 | 科目 | 掌握程度 | 作答 | 正确率 | 错题 | 最近作答 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for k in profile["knowledge_points"]:
        m = k["mastery"]
        lines.append(
            f"| {_md_cell(k['code'])} | {_md_cell(k['name'])} | {_md_cell(k['subject'])} "
            f"| {_md_cell(m['label'])} | {m['attempts']} | {pct(m['accuracy'])} "
            f"| {k['wrong']} | {_md_cell((m['last_scored_at'] or '—')[:10])} |")
    noted = [k for k in profile["knowledge_points"] if k["note"]]
    if noted:
        lines += ["", "## 我的笔记", ""]
        for k in noted:
            lines += [f"### {k['code']} {k['name']}", "",
                      k["note"]["content"], ""]
    filename = f"profile-{profile['certificate']['id']}-{_now()[:10]}.md"
    return "\n".join(lines) + "\n", filename


def get_kp_note(store, student_id: str, kp_id: str) -> dict[str, Any] | None:
    with store.connect() as db:
        row = db.execute("SELECT id FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()
        if not row:
            raise TrainingPortalError("知识点不存在。", 404)
        note = db.execute(
            "SELECT content, created_at, updated_at FROM student_kp_notes"
            " WHERE student_id=? AND knowledge_point_id=?", (student_id, kp_id)).fetchone()
        return dict(note) if note else None


def upsert_kp_note(store, student_id: str, kp_id: str, content: str) -> dict[str, Any]:
    content = content.strip()
    if not content:
        raise TrainingPortalError("笔记内容不能为空；要清空请用删除。")
    if len(content) > 5000:
        raise TrainingPortalError("笔记内容过长（上限 5000 字符）。")
    now = _now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM knowledge_point WHERE id=?", (kp_id,)).fetchone():
            raise TrainingPortalError("知识点不存在。", 404)
        db.execute(
            """INSERT INTO student_kp_notes (id, student_id, knowledge_point_id, content, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(student_id, knowledge_point_id)
               DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (str(uuid.uuid4()), student_id, kp_id, content, now, now))
        row = db.execute(
            "SELECT content, created_at, updated_at FROM student_kp_notes"
            " WHERE student_id=? AND knowledge_point_id=?", (student_id, kp_id)).fetchone()
        return dict(row)


def delete_kp_note(store, student_id: str, kp_id: str) -> dict[str, Any]:
    """删除本人某知识点笔记（待补项：笔记删除入口）；本人隔离，他人笔记不可见不可删。"""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM knowledge_point WHERE id=?", (kp_id,)).fetchone():
            raise TrainingPortalError("知识点不存在。", 404)
        cur = db.execute("DELETE FROM student_kp_notes WHERE student_id=? AND knowledge_point_id=?",
                         (student_id, kp_id))
        if not cur.rowcount:
            raise TrainingPortalError("笔记不存在或已删除。", 404)
        return {"ok": True}


# ---------------------------------------------------------------------------
# 路由注册（接线模式与 classroom/mistake_book 一致）
# ---------------------------------------------------------------------------

class StaffPasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class StudentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    student_no: str = Field(min_length=1)
    name: str = Field(min_length=1)
    class_name: str | None = None
    initial_password: str | None = None
    email: str | None = None


class StudentImportRow(BaseModel):
    student_no: str = Field(min_length=1)
    name: str = Field(min_length=1)
    class_name: str | None = None


class StudentImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    default_password: str = Field(min_length=1)
    default_class_name: str | None = None
    rows: list[StudentImportRow] = Field(min_length=1)


class StaffImportRow(BaseModel):
    # 不设 min_length：空登录名/姓名是可预期的错误行，由服务层逐行给原因（不整批拒绝）。
    username: str = ""
    display_name: str = ""
    email: str | None = None
    phone: str | None = None


class StaffImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    default_password: str = Field(min_length=1)
    rows: list[StaffImportRow] = Field(min_length=1)


class ClassMigrateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    from_class: str = Field(min_length=1)
    to_class: str = Field(min_length=1)


class StudentPasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_password: str = Field(min_length=1)


class StudentToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


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


class KnowledgePointImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=300_000)
    format: str = "checklist"
    default_outline_version: str = ""


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=5000)


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


class LinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: str
    target_id: str = Field(min_length=1)


class PracticeStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    certificate_id: str = Field(min_length=1)
    knowledge_point_id: str | None = None
    rule_id: str | None = None
    seed: int | None = None
    mode: str = "new"  # new=新题 / redo_same=原题重做（错题订正，须显式给 rule_id+seed）


class PracticeSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_rule_ids: list[str] = Field(default_factory=list)


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

    @app.post("/api/training/auth/change-password")
    @guarded
    def training_change_password(body: StaffPasswordChange,
                                 session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        """学生本人自助修改密码（旧密码验证 + 全部会话失效，需重新登录）。"""
        who = student(session)
        result = change_student_password(store_provider(), who, body.old_password, body.new_password)
        resp = response(result)
        resp.delete_cookie(cookie_name, path="/")
        return resp

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

    # ---- 教师：知识点-题目关联（FR-K05）----

    @app.post("/api/training/staff/knowledge-points/{kp_id}/links")
    @guarded
    def staff_link_create(kp_id: str, body: LinkCreate,
                          session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"link": create_link(store_provider(), who, kp_id,
                                             body.target_type, body.target_id)})

    @app.delete("/api/training/staff/links/{link_id}")
    @guarded
    def staff_link_delete(link_id: str,
                          session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response(delete_link(store_provider(), who, link_id))

    # ---- 学生：知识点清单（含覆盖统计）----

    @app.get("/api/training/my/knowledge-points")
    @guarded
    def my_knowledge_points(certificate_id: str,
                            session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"knowledge_points": student_knowledge_points(
            store_provider(), who["id"], certificate_id)})

    # ---- 学生：目标工作台（FR-K07）----

    @app.get("/api/training/my/dashboard")
    @guarded
    def my_dashboard(session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response(dashboard(store_provider(), who["id"]))

    # ---- 学生：自主刷题闭环（FR-K06）----

    @app.post("/api/training/my/practice/start")
    @guarded
    def practice_start(body: PracticeStart,
                       session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"attempt": start_practice(
            store_provider(), who["id"], body.certificate_id,
            kp_id=body.knowledge_point_id, rule_id=body.rule_id, seed=body.seed,
            mode=body.mode)})

    @app.post("/api/training/my/practice/{attempt_id}/skip")
    @guarded
    def practice_skip(attempt_id: str,
                      session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response(skip_practice(store_provider(), who["id"], attempt_id))

    @app.get("/api/training/my/practice/stats")
    @guarded
    def practice_stats_route(certificate_id: str,
                             session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"stats": practice_stats(store_provider(), who["id"], certificate_id)})

    @app.get("/api/training/my/practice/wrong")
    @guarded
    def practice_wrong(certificate_id: str | None = None,
                       session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"attempts": wrong_attempts(store_provider(), who["id"], cert_id=certificate_id)})

    @app.get("/api/training/my/practice/{attempt_id}")
    @guarded
    def practice_detail(attempt_id: str,
                        session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"attempt": get_attempt(store_provider(), who["id"], attempt_id)})

    @app.get("/api/training/my/practice")
    @guarded
    def practice_list(certificate_id: str | None = None, status: str | None = None,
                      session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"attempts": list_attempts(
            store_provider(), who["id"], cert_id=certificate_id, status=status)})

    @app.post("/api/training/my/practice/{attempt_id}/submit")
    @guarded
    def practice_submit(attempt_id: str, body: PracticeSubmit,
                        session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"attempt": submit_practice(
            store_provider(), who["id"], attempt_id, body.selected_rule_ids)})

    @app.get("/api/training/my/coverage")
    @guarded
    def my_coverage(certificate_id: str,
                    session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"coverage": coverage(store_provider(), who["id"], certificate_id)})

    @app.get("/api/training/my/profile")
    @guarded
    def my_profile(certificate_id: str,
                   session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"profile": student_profile(store_provider(), who["id"], certificate_id)})

    @app.get("/api/training/my/profile/export")
    @guarded
    def my_profile_export(certificate_id: str,
                          session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        markdown, filename = student_profile_export(store_provider(), who["id"], certificate_id)
        return Response(
            content=markdown, media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"',
                     "Cache-Control": "private, no-store"})

    @app.get("/api/training/my/knowledge-points/{kp_id}/note")
    @guarded
    def my_note_get(kp_id: str,
                    session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"note": get_kp_note(store_provider(), who["id"], kp_id)})

    @app.put("/api/training/my/knowledge-points/{kp_id}/note")
    @guarded
    def my_note_put(kp_id: str, body: NoteBody,
                    session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"note": upsert_kp_note(store_provider(), who["id"], kp_id, body.content)})

    @app.delete("/api/training/my/knowledge-points/{kp_id}/note")
    @guarded
    def my_note_delete(kp_id: str,
                       session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response(delete_kp_note(store_provider(), who["id"], kp_id))

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

    def admin(session: str | None) -> dict[str, Any]:
        who = staff(session)
        if who.get("role") != "admin":
            raise HTTPException(status_code=403, detail="该操作仅限实训管理员。")
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

    @app.post("/api/training/staff/auth/change-password")
    @guarded
    def staff_change_password(body: StaffPasswordChange,
                              session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        result = change_staff_password(store_provider(), who, body.old_password, body.new_password)
        resp = response(result)
        resp.delete_cookie(STAFF_COOKIE, path="/")
        return resp

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

    @app.post("/api/training/staff/certificates/{cert_id}/knowledge-points/import/preview")
    @guarded
    def staff_kp_import_preview(cert_id: str, body: KnowledgePointImportBody,
                                session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response(preview_kp_import(store_provider(), who, cert_id, body.text,
                                          body.format, body.default_outline_version))

    @app.post("/api/training/staff/certificates/{cert_id}/knowledge-points/import")
    @guarded
    def staff_kp_import(cert_id: str, body: KnowledgePointImportBody,
                        session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response(apply_kp_import(store_provider(), who, cert_id, body.text,
                                        body.format, body.default_outline_version))

    @app.patch("/api/training/staff/knowledge-points/{kp_id}")
    @guarded
    def staff_kp_update(kp_id: str, body: KnowledgePointPatch,
                        session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"knowledge_point": update_knowledge_point(
            store_provider(), who, kp_id, fields=_patch_fields(body))})

    @app.get("/api/training/staff/rules")
    @guarded
    def staff_rules(session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        # 轻量规则目录：供教师把知识点关联到规则仿真题（练习题源）。
        return response({"rules": sorted(
            ({"id": r.id, "name": r.name} for r in _load_rules()), key=lambda x: x["id"])})

    @app.get("/api/training/staff/certificates/{cert_id}/mark-stats")
    @guarded
    def staff_mark_stats(cert_id: str,
                         session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"mark_stats": mark_stats(store_provider(), who, cert_id)})

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

    @app.get("/api/training/staff/students")
    @guarded
    def staff_student_list(include_inactive: bool = True,
                           session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"students": list_students(store_provider(), include_inactive=include_inactive)})

    @app.post("/api/training/staff/students")
    @guarded
    def staff_student_create(body: StudentCreate,
                             session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"student": create_student(
            store_provider(), body.college, body.student_no, body.name,
            class_name=body.class_name, initial_password=body.initial_password,
            email=body.email)})

    @app.post("/api/training/staff/students/import")
    @guarded
    def staff_student_import(body: StudentImportBody,
                             session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"import": import_students(
            store_provider(), body.college,
            [r.model_dump() for r in body.rows],
            default_password=body.default_password,
            default_class_name=body.default_class_name)})

    @app.post("/api/training/staff/students/import-xlsx")
    @guarded
    def staff_student_import_xlsx(file: UploadFile = File(...),
                                  college: str = Form(...),
                                  default_password: str = Form(...),
                                  default_class_name: str = Form(""),
                                  session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        """Excel 名册直读（FR-U03 第二期）：教师上传 .xlsx 名册批量建学生号。

        与文本导入共用 import_students 下游（逐行导入、重复/错误行带原因返回、
        不部分静默成功）；解析边界见 parse_student_xlsx（只读缓存值、不执行
        公式、表头识别、不按列序猜测）。"""
        staff(session)
        rows = parse_student_xlsx(file.file.read())
        return response({"import": import_students(
            store_provider(), college, rows,
            default_password=default_password,
            default_class_name=(default_class_name or "").strip() or None)})

    @app.post("/api/training/staff/students/{student_id}/reset-password")
    @guarded
    def staff_student_reset_password(student_id: str, body: StudentPasswordReset,
                                     session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response(reset_student_password(store_provider(), student_id, body.new_password))

    @app.post("/api/training/staff/students/{student_id}/toggle")
    @guarded
    def staff_student_toggle(student_id: str, body: StudentToggle,
                             session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"student": set_student_active(store_provider(), student_id, body.active)})

    # ---- 班级与教师名册（FR-U03 第一期） ----

    @app.get("/api/training/staff/classes")
    @guarded
    def staff_classes(college: str,
                      session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"classes": list_classes(store_provider(), college)})

    @app.post("/api/training/staff/classes/migrate")
    @guarded
    def staff_class_migrate(body: ClassMigrateBody,
                            session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"migration": migrate_class(
            store_provider(), who["id"], body.college, body.from_class, body.to_class)})

    @app.get("/api/training/staff/class-history")
    @guarded
    def staff_class_history(college: str, limit: int = 50,
                            session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"history": list_class_history(store_provider(), college, limit=limit)})

    # ---- 高校组织架构（FR-U01 第一期：写仅限管理员，教师只读） ----

    @app.get("/api/training/staff/org/departments")
    @guarded
    def org_department_list(college: str, include_archived: bool = True,
                            session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"departments": list_departments(
            store_provider(), college, include_archived=include_archived)})

    @app.post("/api/training/staff/org/departments")
    @guarded
    def org_department_create(body: OrgDepartmentCreate,
                              session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response({"department": create_department(store_provider(), body.college, body.name)})

    @app.patch("/api/training/staff/org/departments/{department_id}")
    @guarded
    def org_department_update(department_id: str, body: OrgNamePatch,
                              session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response({"department": update_department(
            store_provider(), department_id, name=body.name, archived=body.archived)})

    @app.delete("/api/training/staff/org/departments/{department_id}")
    @guarded
    def org_department_delete(department_id: str,
                              session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response(delete_department(store_provider(), department_id))

    @app.get("/api/training/staff/org/terms")
    @guarded
    def org_term_list(college: str, include_archived: bool = True,
                      session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"terms": list_terms(
            store_provider(), college, include_archived=include_archived)})

    @app.post("/api/training/staff/org/terms")
    @guarded
    def org_term_create(body: OrgTermCreate,
                        session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response({"term": create_term(
            store_provider(), body.college, body.name, start_date=body.start_date,
            end_date=body.end_date, is_current=body.is_current)})

    @app.patch("/api/training/staff/org/terms/{term_id}")
    @guarded
    def org_term_update(term_id: str, body: OrgTermPatch,
                        session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response({"term": update_term(
            store_provider(), term_id, name=body.name, start_date=body.start_date,
            end_date=body.end_date, is_current=body.is_current, archived=body.archived)})

    @app.delete("/api/training/staff/org/terms/{term_id}")
    @guarded
    def org_term_delete(term_id: str,
                        session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response(delete_term(store_provider(), term_id))

    @app.get("/api/training/staff/org/classes")
    @guarded
    def org_class_list(college: str, include_archived: bool = True,
                       session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"classes": list_org_classes(
            store_provider(), college, include_archived=include_archived)})

    @app.post("/api/training/staff/org/classes")
    @guarded
    def org_class_create(body: OrgClassCreate,
                         session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response({"class": create_org_class(
            store_provider(), body.college, body.name,
            department_id=body.department_id, term_id=body.term_id)})

    @app.patch("/api/training/staff/org/classes/{class_id}")
    @guarded
    def org_class_update(class_id: str, body: OrgClassPatch,
                         session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response({"class": update_org_class(
            store_provider(), class_id, name=body.name, department_id=body.department_id,
            term_id=body.term_id, archived=body.archived)})

    @app.delete("/api/training/staff/org/classes/{class_id}")
    @guarded
    def org_class_delete(class_id: str,
                         session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response(delete_org_class(store_provider(), class_id))

    @app.post("/api/training/staff/org/classes/{class_id}/clone")
    @guarded
    def org_class_clone(class_id: str, body: OrgClassClone,
                        session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        admin(session)
        return response({"clone": clone_org_class(
            store_provider(), class_id, term_id=body.term_id, name=body.name)})

    @app.get("/api/training/staff/org/quota")
    @guarded
    def org_quota_overview_route(college: str,
                                 session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"quota": college_quota_overview(store_provider(), college)})

    @app.patch("/api/training/staff/org/quota")
    @guarded
    def org_quota_update(body: OrgQuotaPatch,
                         session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = admin(session)
        return response({"quota": set_college_quota(
            store_provider(), who["id"], body.college,
            body.teacher_quota, body.student_quota, body.revision)})

    @app.post("/api/training/staff/teachers/import")
    @guarded
    def staff_teacher_import(body: StaffImportBody,
                             session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = admin(session)
        return response({"import": import_staff_users(
            store_provider(), body.college,
            [r.model_dump() for r in body.rows],
            default_password=body.default_password, created_by=who["id"])})

    @app.get("/training-certificates")
    def training_portal_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "training-portal.html", media_type="text/html")

    @app.get("/training-portal.js")
    def training_portal_script() -> FileResponse:
        return FileResponse(STATIC_DIR / "training-portal.js", media_type="text/javascript")

    @app.get("/training-staff")
    def training_staff_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "training-staff.html", media_type="text/html")

    @app.get("/training-staff.js")
    def training_staff_script() -> FileResponse:
        return FileResponse(STATIC_DIR / "training-staff.js", media_type="text/javascript")
