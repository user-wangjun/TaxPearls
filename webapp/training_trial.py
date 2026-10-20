"""真实学生试用支撑（第四条就绪：试用验证学习闭环，软件侧先行）。

试用期的两条软件通道：
- 学生反馈：试用学生在学生页对缺陷/建议/内容纠错提交反馈，可关联知识点、
  作答或题库题；本人只见自己的反馈。教师按院校查看与处理（new→processing
  →resolved），处理留痕（处理人/时间/备注）。
- 试用统计：教师按自己院校口径查看试用盘面——建档/登录过/近 7 日活跃学生数、
  作答与满分次数、覆盖知识点数、真题作答占比、反馈处理进度。只读聚合，
  不暴露单个学生的作答内容（个人数据本人隔离，沿用既有口径）。

边界：本模块不做问卷、不推送、不导出个人身份信息；试用招募与评估结论属
线下运营动作，软件只保证数据可观测。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from webapp.storage import _now
from webapp.training_portal import CST

FEEDBACK_CATEGORIES = ("bug", "suggestion", "content", "other")
FEEDBACK_STATUSES = ("new", "processing", "resolved")
REF_TYPES = ("none", "kp", "attempt", "question")
_CONTENT_MAX = 1000


def _portal_error(message: str, status: int = 422) -> Exception:
    from webapp.training_portal import TrainingPortalError
    return TrainingPortalError(message, status)


def _now_cst():
    from datetime import datetime
    return datetime.now(CST)


# ---------------------------------------------------------------------------
# 服务层：学生反馈
# ---------------------------------------------------------------------------

def create_feedback(store, student: dict, *, category: str, content: str,
                    certificate_id: str | None = None, ref_type: str = "none",
                    ref_id: str = "") -> dict[str, Any]:
    category = str(category).strip()
    if category not in FEEDBACK_CATEGORIES:
        raise _portal_error("反馈类别必须是 bug / suggestion / content / other。")
    content = str(content).strip()
    if not content:
        raise _portal_error("反馈内容不能为空。")
    if len(content) > _CONTENT_MAX:
        raise _portal_error(f"反馈内容不超过 {_CONTENT_MAX} 字。")
    ref_type = str(ref_type).strip() or "none"
    if ref_type not in REF_TYPES:
        raise _portal_error("关联对象类型必须是 none / kp / attempt / question。")
    ref_id = str(ref_id).strip()
    if ref_type == "none":
        ref_id = ""
    elif not ref_id:
        raise _portal_error("关联对象类型不为空时必须提供对象 id。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if certificate_id and not db.execute(
                "SELECT 1 FROM certificate WHERE id=?", (certificate_id,)).fetchone():
            raise _portal_error("关联证书不存在。", 404)
        if ref_type == "kp" and not db.execute(
                "SELECT 1 FROM knowledge_point WHERE id=?", (ref_id,)).fetchone():
            raise _portal_error("关联的知识点不存在。", 404)
        if ref_type == "attempt" and not db.execute(
                "SELECT 1 FROM training_self_practice_attempts WHERE id=? AND student_id=?",
                (ref_id, student["id"])).fetchone():
            raise _portal_error("关联的作答记录不存在或不属于本人。", 404)
        if ref_type == "question" and not db.execute(
                "SELECT 1 FROM training_bank_question WHERE id=?", (ref_id,)).fetchone():
            raise _portal_error("关联的题库题不存在。", 404)
        feedback_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO training_trial_feedback (id, student_id, certificate_id, ref_type, ref_id,
               category, content, status, staff_note, handled_by, handled_at, created_at)
               VALUES (?,?,?,?,?,?,?,'new','',NULL,NULL,?)""",
            (feedback_id, student["id"], certificate_id, ref_type, ref_id, category, content, _now()))
        row = db.execute("SELECT * FROM training_trial_feedback WHERE id=?", (feedback_id,)).fetchone()
        return _feedback_view(db, row, redact_student=True)


def _feedback_view(db, row: sqlite3.Row, *, redact_student: bool) -> dict[str, Any]:
    view = dict(row)
    if redact_student:
        view.pop("student_id", None)
    else:
        info = db.execute(
            "SELECT student_no, name, class_name FROM student_info WHERE id=?",
            (row["student_id"],)).fetchone()
        view["student"] = dict(info) if info else None
    return view


def list_my_feedback(store, student_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM training_trial_feedback WHERE student_id=? ORDER BY created_at DESC LIMIT ?",
            (student_id, max(1, min(int(limit), 200)))).fetchall()
        return [_feedback_view(db, r, redact_student=True) for r in rows]


def list_staff_feedback(store, college: str, *, status: str | None = None,
                        category: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    clauses, params = ["WHERE s.college=?"], [college]
    if status in FEEDBACK_STATUSES:
        clauses.append("AND f.status=?"); params.append(status)
    if category in FEEDBACK_CATEGORIES:
        clauses.append("AND f.category=?"); params.append(category)
    params.append(max(1, min(int(limit), 500)))
    with store.connect() as db:
        rows = db.execute(
            f"""SELECT f.* FROM training_trial_feedback f
                JOIN student_info s ON s.id=f.student_id
                {' '.join(clauses)} ORDER BY f.created_at DESC LIMIT ?""", params).fetchall()
        return [_feedback_view(db, r, redact_student=False) for r in rows]


def handle_feedback(store, staff: dict, feedback_id: str, *, status: str | None = None,
                    staff_note: str | None = None) -> dict[str, Any]:
    if status is not None and status not in FEEDBACK_STATUSES:
        raise _portal_error("处理状态必须是 new / processing / resolved。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM training_trial_feedback WHERE id=?", (feedback_id,)).fetchone()
        if not row:
            raise _portal_error("反馈记录不存在。", 404)
        college = db.execute("SELECT college FROM student_info WHERE id=?",
                             (row["student_id"],)).fetchone()
        if not college or college["college"] != staff["college"]:
            raise _portal_error("只能处理本人院校学生的反馈。", 403)
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        if staff_note is not None:
            updates["staff_note"] = str(staff_note).strip()[:2000]
        if not updates:
            raise _portal_error("没有提供任何要更新的字段。")
        if "status" in updates and updates["status"] != "new":
            updates["handled_by"] = staff["id"]
            updates["handled_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE training_trial_feedback SET {sets} WHERE id=?",
                   (*updates.values(), feedback_id))
        row = db.execute("SELECT * FROM training_trial_feedback WHERE id=?", (feedback_id,)).fetchone()
        return _feedback_view(db, row, redact_student=False)


# ---------------------------------------------------------------------------
# 服务层：试用统计（教师院校口径，只读聚合）
# ---------------------------------------------------------------------------

def trial_stats(store, college: str) -> dict[str, Any]:
    cutoff = (_now_cst() - timedelta(days=7)).isoformat()
    with store.connect() as db:
        students = db.execute(
            "SELECT id FROM student_info WHERE college=?", (college,)).fetchall()
        ids = [s["id"] for s in students]
        stats = {
            "college": college,
            "students_total": len(ids),
            "students_logged_in": 0,
            "students_active_7d": 0,
            "attempts_total": 0,
            "attempts_perfect": 0,
            "attempts_bank": 0,
            "kps_covered": 0,
            "feedback": {"total": 0, "new": 0, "processing": 0, "resolved": 0},
        }
        if ids:
            marks = ",".join("?" for _ in ids)
            stats["students_logged_in"] = db.execute(
                f"SELECT count(DISTINCT student_id) n FROM training_student_sessions"
                f" WHERE student_id IN ({marks})", ids).fetchone()["n"]
            stats["students_active_7d"] = db.execute(
                f"SELECT count(DISTINCT student_id) n FROM training_self_practice_attempts"
                f" WHERE student_id IN ({marks}) AND created_at>=?", (*ids, cutoff)).fetchone()["n"]
            row = db.execute(
                f"""SELECT count(*) total,
                           sum(CASE WHEN source_type!='simulated' THEN 1 ELSE 0 END) bank
                    FROM training_self_practice_attempts
                    WHERE student_id IN ({marks}) AND status='scored'""", ids).fetchone()
            stats["attempts_total"] = row["total"] or 0
            stats["attempts_bank"] = row["bank"] or 0
            # result_json 是字段加密列（密文入库、行工厂解密），SQL 侧 json_extract
            # 会报「malformed JSON」直接打断整张盘面；必须带上主键列 id（解密时校验
            # 对象关联）逐行读取后再统计满分。口径与既有作答统计一致。
            perfect = 0
            for attempt in db.execute(
                    f"""SELECT id, result_json FROM training_self_practice_attempts
                        WHERE student_id IN ({marks}) AND status='scored'""", ids).fetchall():
                try:
                    payload = json.loads(attempt["result_json"] or "{}")
                except (TypeError, ValueError):
                    continue  # 历史脏数据不计入满分，但不拖垮整张盘面
                if isinstance(payload, dict) and payload.get("perfect"):
                    perfect += 1
            stats["attempts_perfect"] = perfect
            stats["kps_covered"] = db.execute(
                f"""SELECT count(DISTINCT knowledge_point_id) n FROM training_self_practice_attempts
                    WHERE student_id IN ({marks}) AND status='scored' AND knowledge_point_id IS NOT NULL""",
                ids).fetchone()["n"]
        fb = db.execute(
            """SELECT f.status, count(*) n FROM training_trial_feedback f
               JOIN student_info s ON s.id=f.student_id WHERE s.college=?
               GROUP BY f.status""", (college,)).fetchall()
        for r in fb:
            stats["feedback"][r["status"]] = r["n"]
            stats["feedback"]["total"] += r["n"]
        return stats


# ---------------------------------------------------------------------------
# 路由（学生端 + 教师端）
# ---------------------------------------------------------------------------

class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    content: str = Field(min_length=1, max_length=_CONTENT_MAX)
    certificate_id: str | None = None
    ref_type: str = "none"
    ref_id: str = ""


class FeedbackPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    staff_note: str | None = None


def register(app, store_provider: Callable[[], Any], cookie_name: str) -> None:
    from webapp.training_portal import (STAFF_COOKIE, TrainingPortalError, staff_for_token,
                                        student_for_token)

    def response(body: Any) -> JSONResponse:
        return JSONResponse(body, headers={"Cache-Control": "private, no-store"})

    def student(session: str | None) -> dict[str, Any]:
        who = student_for_token(store_provider(), session)
        if not who:
            raise HTTPException(status_code=401, detail="请先使用学号登录。")
        return who

    def staff(session: str | None) -> dict[str, Any]:
        who = staff_for_token(store_provider(), session)
        if not who:
            raise HTTPException(status_code=401, detail="请先使用教师或管理员账号登录。")
        return who

    def guarded(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except TrainingPortalError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from None
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="操作与现有数据冲突。") from None
        return wrapper

    @app.post("/api/training/my/feedback")
    @guarded
    def feedback_create(body: FeedbackCreate,
                        session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"feedback": create_feedback(
            store_provider(), who, category=body.category, content=body.content,
            certificate_id=body.certificate_id, ref_type=body.ref_type, ref_id=body.ref_id)})

    @app.get("/api/training/my/feedback")
    @guarded
    def feedback_mine(session: str | None = Cookie(default=None, alias=cookie_name)) -> Response:
        who = student(session)
        return response({"feedback": list_my_feedback(store_provider(), who["id"])})

    @app.get("/api/training/staff/trial-feedback")
    @guarded
    def staff_feedback(status: str | None = None, category: str | None = None,
                       session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"feedback": list_staff_feedback(store_provider(), who["college"],
                                                         status=status, category=category)})

    @app.patch("/api/training/staff/trial-feedback/{feedback_id}")
    @guarded
    def staff_feedback_handle(feedback_id: str, body: FeedbackPatch,
                              session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"feedback": handle_feedback(store_provider(), who, feedback_id,
                                                     status=body.status, staff_note=body.staff_note)})

    @app.get("/api/training/staff/trial-stats")
    @guarded
    def staff_trial_stats(session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"stats": trial_stats(store_provider(), who["college"])})
