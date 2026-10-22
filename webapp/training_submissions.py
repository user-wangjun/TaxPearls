"""提交版本、草稿与订正：在既有 ``submissions`` 旁边挂明细，不改它一行结构。

为什么不动 ``submissions``：
- 它的 ``UNIQUE(assignment_id, student_id)`` 是「当前一次正式提交」的唯一键，
  15 处以上既有查询（统计、画像、错题、试卷进度）都依赖这个语义；
- ``details_json`` / ``feedback`` 是加密列，AAD 绑定表名 + 身份键，换表必须重新
  封包，不能原样拷贝；
- SQLite 改唯一键要重建表，收益远小于风险。

所以本模块只做四件事：
1. ``training_submission_draft``：草稿，一人一任务一行，可反复覆盖；
2. ``training_submission_version``：append-only 历史版本，含**那一次**的复核分；
3. ``training_submission_state``：学生这一层的进度状态与退回信息；
4. 提交前闸门：没有提交过 → 放行；提交过 → 必须教师退回、且订正次数未用尽。

口径（用户 2026-10-22 确认）：
- 重交必须教师退回，学生不能自己重交；
- 历次提交与当时的复核分全部保留，当前成绩以最新一次已复核为准；
- 截止即封锁提交与订正，补交只能教师改期限；
- 订正默认 1 次，按任务可调，与限交次数分开算。
"""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Callable

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp import classroom
from webapp.access import current_actor

STATUS = ("not_started", "in_progress", "submitted", "pending_review", "returned", "done")


class SubmissionError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS training_submission_draft (
            assignment_id TEXT NOT NULL REFERENCES assignments(id),
            student_id TEXT NOT NULL REFERENCES users(id),
            answers_json TEXT NOT NULL,       -- 学生选择（与 submissions.answers_json 同口径，明文）
            saved_at TEXT NOT NULL,
            PRIMARY KEY (assignment_id, student_id)
        );
        CREATE TABLE IF NOT EXISTS training_submission_version (
            id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL REFERENCES assignments(id),
            student_id TEXT NOT NULL REFERENCES users(id),
            version INTEGER NOT NULL,         -- 1,2,3…按提交先后
            kind TEXT NOT NULL DEFAULT 'submit' CHECK (kind IN ('submit','correct')),
            answers_json TEXT NOT NULL,
            score REAL NOT NULL,              -- 自动判分
            details_json TEXT NOT NULL,       -- 加密列
            adjusted_score REAL,              -- 该次人工复核分（历史保留，不随重交清空）
            feedback TEXT,                    -- 加密列
            reviewed_by TEXT REFERENCES users(id),
            reviewed_at TEXT,
            submitted_at TEXT NOT NULL,
            UNIQUE (assignment_id, student_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_submission_version_task
            ON training_submission_version(assignment_id, student_id, version DESC);
        CREATE TABLE IF NOT EXISTS training_submission_state (
            assignment_id TEXT NOT NULL REFERENCES assignments(id),
            student_id TEXT NOT NULL REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'not_started'
                CHECK (status IN ('not_started','in_progress','submitted','pending_review','returned','done')),
            attempts INTEGER NOT NULL DEFAULT 0,      -- 正式提交次数
            corrections INTEGER NOT NULL DEFAULT 0,   -- 已用订正次数
            return_reason TEXT,                       -- 退回原因（加密列）
            returned_by TEXT REFERENCES users(id),
            returned_at TEXT,
            correct_due_at TEXT,                      -- 订正期限
            updated_at TEXT NOT NULL,
            PRIMARY KEY (assignment_id, student_id)
        );
    """)
    # 订正次数与限交次数分开：默认 1 次，按任务可调
    columns = {r["name"] for r in db.execute("PRAGMA table_info(training_task_settings)")}
    if columns and "correction_limit" not in columns:
        db.execute("ALTER TABLE training_task_settings ADD COLUMN correction_limit INTEGER NOT NULL DEFAULT 1")
    # 重交策略：teacher_return=必须教师退回（默认，2026-10-22 口径）；free=沿用历史覆盖式重交
    if columns and "retake_policy" not in columns:
        db.execute("ALTER TABLE training_task_settings ADD COLUMN retake_policy TEXT NOT NULL DEFAULT 'teacher_return'")


# --------------------------------------------------------------------------- 读取

def settings_for(db, assignment_id: str) -> dict[str, int]:
    """任务级限制：题目归属试卷时以试卷为准，否则取作业自身。"""
    row = db.execute(
        """SELECT s.paper_id,t.submit_limit,t.correction_limit,t.retake_policy FROM training_assignment_settings s
           LEFT JOIN training_task_settings t
             ON t.subject_kind=CASE WHEN s.paper_id IS NULL THEN 'assignment' ELSE 'paper' END
            AND t.subject_id=COALESCE(s.paper_id,s.assignment_id)
           WHERE s.assignment_id=?""",
        (assignment_id,),
    ).fetchone()
    limit = int(row["submit_limit"] or 1) if row else 1
    correction = int(row["correction_limit"] if row and row["correction_limit"] is not None else 1)
    policy = (row["retake_policy"] if row and row["retake_policy"] else "teacher_return")
    return {"submit_limit": max(limit, 1), "correction_limit": max(correction, 0),
            "retake_policy": policy if policy in {"free", "teacher_return"} else "teacher_return"}


def state_of(db, assignment_id: str, student_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM training_submission_state WHERE assignment_id=? AND student_id=?",
        (assignment_id, student_id),
    ).fetchone()
    base = {"status": "not_started", "attempts": 0, "corrections": 0, "return_reason": None,
            "returned_by": None, "returned_at": None, "correct_due_at": None, "updated_at": None}
    if row:
        base.update({k: row[k] for k in base if k in row.keys()})
    return base


def attempts(db, assignment_id: str, student_id: str) -> int:
    return int(state_of(db, assignment_id, student_id)["attempts"])


def draft(db, assignment_id: str, student_id: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT answers_json,saved_at FROM training_submission_draft WHERE assignment_id=? AND student_id=?",
        (assignment_id, student_id),
    ).fetchone()
    if not row:
        return None
    return {"answers": json.loads(row["answers_json"]), "saved_at": row["saved_at"]}


def versions(store, db, assignment_id: str, student_id: str) -> list[dict[str, Any]]:
    # 身份列必须一起 SELECT：行工厂按 assignment_id/student_id 校验解密上下文
    rows = db.execute(
        """SELECT assignment_id,student_id,id,version,kind,answers_json,score,adjusted_score,
                  feedback,reviewed_by,reviewed_at,submitted_at
           FROM training_submission_version WHERE assignment_id=? AND student_id=? ORDER BY version""",
        (assignment_id, student_id),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item.pop("assignment_id", None)
        item.pop("student_id", None)
        item["answers"] = json.loads(item.pop("answers_json"))
        result.append(item)   # feedback 已由行工厂解密为明文
    return result


# --------------------------------------------------------------------------- 写入

def _codec(store):
    return store._field_codec


def guard_submit(db, assignment_id: str, student_id: str) -> None:
    """提交前闸门，必须在写入事务内调用。

    - 从未提交：放行（由限交次数以外的逻辑判断截止时间，截止在 classroom 里已校验）；
    - 已提交：必须处于「已退回」，且订正次数未用尽。
    """
    limits = settings_for(db, assignment_id)
    if limits["retake_policy"] == "free":
        # 历史口径：学生可自行重交，覆盖当前提交；旧版本仍会进版本表，不丢记录
        return
    state = state_of(db, assignment_id, student_id)
    current = db.execute(
        "SELECT 1 FROM submissions WHERE assignment_id=? AND student_id=?", (assignment_id, student_id)
    ).fetchone()
    if not current:
        if state["attempts"] >= limits["submit_limit"]:
            raise classroom.ClassroomError(
                f"已达提交上限（限交 {limits['submit_limit']} 次），如需再交请联系任课教师。", 409)
        return
    if state["status"] != "returned":
        raise classroom.ClassroomError(
            "这份作业已经提交过。需要重新提交请等任课教师退回并说明原因。", 409)
    if state["corrections"] >= limits["correction_limit"]:
        raise classroom.ClassroomError(
            f"订正次数已用尽（限 {limits['correction_limit']} 次），请联系任课教师。", 409)


def snapshot_current(store, db, assignment_id: str, student_id: str) -> int | None:
    """把即将被覆盖的那一次提交存成历史版本，返回版本号。

    加密列必须**解包后重新封包**：AAD 绑定了表名和身份键（submissions 用
    (assignment_id, student_id)，版本表用 (assignment_id, student_id, version)），
    直接拷贝密文会在写入触发器校验时失败。
    """
    row = db.execute(
        "SELECT * FROM submissions WHERE assignment_id=? AND student_id=?", (assignment_id, student_id)
    ).fetchone()
    if not row:
        return None
    codec = _codec(store)
    last = db.execute(
        "SELECT COALESCE(MAX(version),0) v FROM training_submission_version WHERE assignment_id=? AND student_id=?",
        (assignment_id, student_id),
    ).fetchone()
    version = int(last["v"]) + 1
    # 行工厂已按 submissions 的身份键解密，这里拿到的是明文；换表必须重新封包
    details = row["details_json"]
    feedback = row["feedback"]
    db.execute(
        """INSERT INTO training_submission_version
             (id,assignment_id,student_id,version,kind,answers_json,score,details_json,
              adjusted_score,feedback,reviewed_by,reviewed_at,submitted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (secrets.token_hex(12), assignment_id, student_id, version,
         # 版本 1 是原始提交，之后每次因退回产生的都是订正
         "submit" if version == 1 else "correct",
         row["answers_json"], row["score"],
         codec.seal(details, "training_submission_version", "details_json", assignment_id, student_id, version),
         row["adjusted_score"],
         codec.seal(feedback, "training_submission_version", "feedback", assignment_id, student_id, version)
         if feedback is not None else None,
         row["reviewed_by"], None, row["submitted_at"]),
    )
    return version


def record_submitted(db, assignment_id: str, student_id: str) -> None:
    """正式提交成功后更新学生进度，并清掉草稿。"""
    state = state_of(db, assignment_id, student_id)
    corrections = int(state["corrections"]) + (1 if state["status"] == "returned" else 0)
    db.execute(
        """INSERT INTO training_submission_state
             (assignment_id,student_id,status,attempts,corrections,updated_at)
           VALUES (?,?,'submitted',1,?,?)
           ON CONFLICT(assignment_id,student_id) DO UPDATE SET
             status='submitted', attempts=training_submission_state.attempts+1,
             corrections=excluded.corrections,
             return_reason=NULL, returned_by=NULL, returned_at=NULL, correct_due_at=NULL,
             updated_at=excluded.updated_at""",
        (assignment_id, student_id, corrections, now()),
    )
    db.execute("DELETE FROM training_submission_draft WHERE assignment_id=? AND student_id=?",
               (assignment_id, student_id))


def set_state(db, assignment_id: str, student_id: str, status: str, **fields) -> None:
    if status not in STATUS:
        raise SubmissionError("进度状态不正确。")
    db.execute(
        """INSERT INTO training_submission_state (assignment_id,student_id,status,updated_at)
           VALUES (?,?,?,?) ON CONFLICT(assignment_id,student_id) DO UPDATE SET
             status=excluded.status, updated_at=excluded.updated_at""",
        (assignment_id, student_id, status, now()),
    )
    if fields:
        allowed = {"return_reason", "returned_by", "returned_at", "correct_due_at"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if changes:
            db.execute(
                "UPDATE training_submission_state SET " + ",".join(f"{k}=?" for k in changes)
                + " WHERE assignment_id=? AND student_id=?",
                (*changes.values(), assignment_id, student_id),
            )


def save_draft(db, assignment_id: str, student_id: str, answers: list[str]) -> dict[str, Any]:
    stamp = now()
    db.execute(
        """INSERT INTO training_submission_draft (assignment_id,student_id,answers_json,saved_at)
           VALUES (?,?,?,?) ON CONFLICT(assignment_id,student_id) DO UPDATE SET
             answers_json=excluded.answers_json, saved_at=excluded.saved_at""",
        (assignment_id, student_id, json.dumps(answers), stamp),
    )
    if state_of(db, assignment_id, student_id)["status"] == "not_started":
        db.execute(
            """INSERT INTO training_submission_state (assignment_id,student_id,status,updated_at)
               VALUES (?,?,'in_progress',?) ON CONFLICT(assignment_id,student_id) DO NOTHING""",
            (assignment_id, student_id, stamp),
        )
    return {"saved_at": stamp}


def progress(db, assignment_id: str, student_id: str, store=None) -> dict[str, Any]:
    limits = settings_for(db, assignment_id)
    state = state_of(db, assignment_id, student_id)
    draft_row = draft(db, assignment_id, student_id)
    status = state["status"]
    if status in {"submitted", "pending_review", "done"}:
        review = db.execute(
            "SELECT adjusted_score FROM submissions WHERE assignment_id=? AND student_id=?",
            (assignment_id, student_id),
        ).fetchone()
        if review is None:
            status = "not_started"
        else:
            status = "done" if review["adjusted_score"] is not None else "pending_review"
    return {
        "status": status,
        "return_reason": state["return_reason"],   # 行工厂已解密
        "attempts": int(state["attempts"]),
        "corrections": int(state["corrections"]),
        "submit_limit": limits["submit_limit"],
        "correction_limit": limits["correction_limit"],
        "retake_policy": limits["retake_policy"],
        "draft_saved_at": draft_row["saved_at"] if draft_row else None,
        "correct_due_at": state["correct_due_at"],
    }


def return_for_revision(store, db, assignment_id: str, student_id: str, teacher: dict,
                        reason: str, correct_due_at: str | None) -> dict[str, Any]:
    if not reason.strip():
        raise SubmissionError("退回必须写明原因，学生要按原因订正。")
    codec = _codec(store)
    set_state(db, assignment_id, student_id, "returned",
              return_reason=codec.seal(reason.strip(), "training_submission_state", "return_reason",
                                       assignment_id, student_id),
              returned_by=teacher["id"], returned_at=now(), correct_due_at=correct_due_at)
    return progress(db, assignment_id, student_id, store)


# --------------------------------------------------------------------------- 接口

class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[str] = Field(max_length=200)


class ReturnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    correct_due_at: str | None = None


def register(app, store_provider: Callable[[], Any], user_fn: Callable[..., dict], cookie_name: str) -> None:
    def response(value: Any) -> JSONResponse:
        return JSONResponse(value, headers={"Cache-Control": "private, no-store"})

    def actor(session: str | None) -> dict[str, Any]:
        user = user_fn(session)
        if user["role"] not in {"teacher", "student"}:
            raise HTTPException(403, "当前账号没有课程任务权限。")
        return user

    def guarded(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except (SubmissionError, classroom.ClassroomError) as exc:
                raise HTTPException(exc.status, str(exc)) from None
        return wrapper

    def accessible(db, assignment_id: str, user: dict):
        row = db.execute("SELECT * FROM assignments WHERE id=? AND org_id=?", (assignment_id, user["org_id"])).fetchone()
        item = classroom.decorate(db, dict(row)) if row else None
        if not item or not classroom.can_access(db, item, user):
            raise SubmissionError("作业不存在。", 404)
        return item

    @app.get("/api/assignments/{assignment_id}/progress")
    @guarded
    def assignment_progress(assignment_id: str, session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {"teacher", "student"})
            item = accessible(db, assignment_id, user)
            if user["role"] == "teacher":
                return response({"assignment": {"id": item["id"], "title": item["title"]}})
            body = progress(db, assignment_id, user["id"], store_provider())
            body["can_submit"] = bool(item["can_submit"])
            return response(body)

    @app.get("/api/assignments/{assignment_id}/draft")
    @guarded
    def get_draft(assignment_id: str, session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "student":
            raise HTTPException(403, "只有学生有草稿。")
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {"student"})
            accessible(db, assignment_id, user)
            return response({"draft": draft(db, assignment_id, user["id"])})

    @app.put("/api/assignments/{assignment_id}/draft")
    @guarded
    def put_draft(assignment_id: str, body: DraftBody,
                  session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "student":
            raise HTTPException(403, "只有学生能存草稿。")
        repo = store_provider()
        with repo.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"student"})
            item = accessible(db, assignment_id, user)
            if not item["can_submit"]:
                raise SubmissionError("已到截止时间，不能再保存草稿。", 409)
            if item["paper_id"]:
                raise SubmissionError("组卷题目请在试卷内保存，避免拆散整份进度。", 409)
            value = save_draft(db, assignment_id, user["id"], body.answers)
            repo._log(db, user, "save_draft", "assignment", assignment_id, f"answers={len(body.answers)}")
        return response(value)

    @app.delete("/api/assignments/{assignment_id}/draft")
    @guarded
    def drop_draft(assignment_id: str, session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        with store_provider().connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"student"})
            accessible(db, assignment_id, user)
            db.execute("DELETE FROM training_submission_draft WHERE assignment_id=? AND student_id=?",
                       (assignment_id, user["id"]))
        return response({"cleared": True})

    @app.get("/api/assignments/{assignment_id}/versions")
    @guarded
    def list_versions(assignment_id: str, student_id: str | None = None,
                      session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {"teacher", "student"})
            accessible(db, assignment_id, user)
            if user["role"] == "teacher":
                if not student_id:
                    rows = db.execute(
                        """SELECT DISTINCT student_id FROM training_submission_version WHERE assignment_id=?
                           ORDER BY student_id""", (assignment_id,)).fetchall()
                    return response({"students": [r["student_id"] for r in rows]})
                target = student_id
            else:
                target = user["id"]
            return response({"items": versions(store_provider(), db, assignment_id, target)})

    @app.post("/api/assignments/{assignment_id}/return")
    @guarded
    def return_revision(assignment_id: str, body: ReturnBody,
                        session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有任课教师可以退回。")
        repo = store_provider()
        with repo.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"teacher"})
            accessible(db, assignment_id, user)
            submitted = db.execute(
                "SELECT 1 FROM submissions WHERE assignment_id=? AND student_id=?",
                (assignment_id, body.student_id)).fetchone()
            if not submitted:
                raise SubmissionError("该学生还没有提交，不能退回。", 409)
            value = return_for_revision(repo, db, assignment_id, body.student_id, user,
                                        body.reason, body.correct_due_at)
            repo._log(db, user, "return_submission", "assignment", assignment_id,
                      f"student={body.student_id}")
        return response(value)
