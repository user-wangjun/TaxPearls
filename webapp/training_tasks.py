"""课程任务主链：两类任务统一成一份清单，并旁挂任务级策略。

设计约束（与《高校端教学端重设计方案》一致）：
- **不新建并行任务系统**。任务对象仍是既有 ``assignments``（单案例作业）与
  ``training_papers``（组卷）。本模块只在旁边挂一层策略，不复制截止时间、
  班级、课程——它们分别由 ``training_assignment_settings`` /
  ``training_papers`` / ``university_assignment_courses`` 持有，避免双份真相。
- 旁挂表 ``training_task_settings`` 只存既有表上没有的东西：开放时间、提交次数、
  答案开放策略、归档时间。
- 学生可见性由服务端判定：未发布、已归档、跨机构、非本班一律 404，不做
  "前端隐藏"式的保护。答案同理，按 ``answer_release`` 判定后才下发。
"""
from __future__ import annotations

from datetime import UTC, datetime
from functools import wraps
from typing import Any, Callable, Literal

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp.access import current_actor

KINDS = ("assignment", "paper")
ANSWER_RELEASE = ("after_submit", "after_due", "manual")


class TaskError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS training_task_settings (
            subject_kind TEXT NOT NULL CHECK (subject_kind IN ('assignment','paper')),
            subject_id TEXT NOT NULL,
            org_id TEXT NOT NULL,
            open_at TEXT,                              -- 开放时间（空=发布即可用）
            submit_limit INTEGER NOT NULL DEFAULT 1 CHECK (submit_limit BETWEEN 1 AND 9),
            answer_release TEXT NOT NULL DEFAULT 'after_due'
                CHECK (answer_release IN ('after_submit','after_due','manual')),
            answer_released_at TEXT,                   -- 教师手工开放答案的时间
            archived_at TEXT,                          -- 归档时间（不删除学生记录）
            updated_by TEXT REFERENCES users(id),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (subject_kind, subject_id)
        );
    """)


# --------------------------------------------------------------------------- 读取工具

def _settings(db, kind: str, subject_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM training_task_settings WHERE subject_kind=? AND subject_id=?",
        (kind, subject_id),
    ).fetchone()
    base = {"open_at": None, "submit_limit": 1, "correction_limit": 1,
            "retake_policy": "teacher_return", "answer_release": "after_due",
            "answer_released_at": None, "archived_at": None, "updated_at": None}
    if row:
        base.update({k: row[k] for k in base if k in row.keys()})
    return base


def _class_scope(db, org_id: str, class_id: str | None) -> int:
    """应提交人数：有班级按班级名册，无班级按本机构在职学生。"""
    if class_id:
        row = db.execute(
            """SELECT COUNT(*) c FROM training_class_members m JOIN users u ON u.id=m.student_id
               WHERE m.class_id=? AND u.org_id=? AND u.active=1""",
            (class_id, org_id),
        ).fetchone()
    else:
        row = db.execute("SELECT COUNT(*) c FROM users WHERE role='student' AND active=1 AND org_id=?", (org_id,)).fetchone()
    return int(row["c"] if row else 0)


def _course(db, subject_kind: str, subject_ids: list[str]) -> tuple[str | None, str | None]:
    if not subject_ids:
        return None, None
    marks = ",".join("?" for _ in subject_ids)
    row = db.execute(
        f"""SELECT c.id,c.name FROM university_assignment_courses ac
            JOIN course c ON c.id=ac.course_id
            WHERE ac.assignment_id IN ({marks}) ORDER BY c.name LIMIT 1""",
        tuple(subject_ids),
    ).fetchone()
    return (row["id"], row["name"]) if row else (None, None)


def _task_state(published: bool, archived: bool, open_at: str | None, due_at: str | None) -> str:
    if archived:
        return "archived"
    if not published:
        return "draft"
    if open_at and now() < open_at:
        return "scheduled"
    if due_at and now() >= due_at:
        return "closed"
    return "published"


def _progress(db, assignment_ids: list[str], student_id: str | None) -> dict[str, int]:
    if not assignment_ids:
        return {"submitted": 0, "reviewed": 0}
    marks = ",".join("?" for _ in assignment_ids)
    if student_id:
        row = db.execute(
            f"""SELECT COUNT(*) c,
                       SUM(CASE WHEN adjusted_score IS NOT NULL THEN 1 ELSE 0 END) r
                FROM submissions WHERE student_id=? AND assignment_id IN ({marks})""",
            (student_id, *assignment_ids),
        ).fetchone()
    else:
        row = db.execute(
            f"""SELECT COUNT(DISTINCT student_id) c,
                       COUNT(DISTINCT CASE WHEN adjusted_score IS NOT NULL THEN student_id END) r
                FROM submissions WHERE assignment_id IN ({marks})""",
            tuple(assignment_ids),
        ).fetchone()
    return {"submitted": int(row["c"] or 0), "reviewed": int(row["r"] or 0)}


def _answers_visible(task: dict[str, Any], who: dict[str, Any]) -> bool:
    if who["role"] == "teacher":
        return True
    if task["answer_release"] == "manual":
        return bool(task["answer_released_at"])
    if task["answer_release"] == "after_submit":
        return bool(task["fully_submitted"])
    # after_due：截止后才开放；已交但未到截止同样不给
    return bool(task["due_at"] and now() >= task["due_at"])


def _can_submit(task: dict[str, Any], remaining: int) -> bool:
    if task["state"] != "published":
        return False
    return remaining > 0


# --------------------------------------------------------------------------- 视图

def _assignment_task(db, row, who) -> dict[str, Any]:
    sett = _settings(db, "assignment", row["id"])
    due_at = row["deadline_at"]
    submitted_mine = 0
    if who["role"] == "student":
        sub = db.execute(
            "SELECT adjusted_score FROM submissions WHERE assignment_id=? AND student_id=?",
            (row["id"], who["id"]),
        ).fetchone()
        submitted_mine = 1 if sub else 0
        reviewed = 1 if sub and sub["adjusted_score"] is not None else 0
    else:
        counts = _progress(db, [row["id"]], None)
        submitted_mine, reviewed = counts["submitted"], counts["reviewed"]
    course_id, course_name = _course(db, "assignment", [row["id"]])
    class_name = None
    if row["class_id"]:
        found = db.execute("SELECT name FROM training_classes WHERE id=?", (row["class_id"],)).fetchone()
        class_name = found["name"] if found else None
    task = {
        "kind": "assignment", "id": row["id"], "title": row["title"], "task_type": "single",
        "course_id": course_id, "course_name": course_name,
        "class_id": row["class_id"], "class_name": class_name,
        "published": bool(row["published"]), "open_at": sett["open_at"], "due_at": due_at,
        "state": _task_state(bool(row["published"]), bool(sett["archived_at"]), sett["open_at"], due_at),
        "submit_limit": int(sett["submit_limit"]),
        "correction_limit": int(sett["correction_limit"]),
        "retake_policy": sett["retake_policy"],
        "answer_release": sett["answer_release"], "answer_released_at": sett["answer_released_at"],
        "expected": _class_scope(db, who["org_id"], row["class_id"]),
        "submitted_total": submitted_mine if who["role"] == "teacher" else None,
        "submitted_mine": submitted_mine if who["role"] == "student" else 0,
        "reviewed_total": reviewed if who["role"] == "teacher" else None,
        "mine_reviewed": reviewed if who["role"] == "student" else 0,
        "question_count": 1, "total_points": 100.0,
    }
    task["fully_submitted"] = task["submitted_mine"] >= 1
    task["answers_visible"] = _answers_visible(task, who)
    task["can_submit"] = bool(who["role"] == "student" and _can_submit(task, task["submit_limit"] - task["submitted_mine"]))
    return task


def _paper_task(db, row, who) -> dict[str, Any]:
    sett = _settings(db, "paper", row["id"])
    items = db.execute(
        "SELECT assignment_id,position,points FROM training_assignment_settings WHERE paper_id=? ORDER BY position",
        (row["id"],),
    ).fetchall()
    ids = [i["assignment_id"] for i in items]
    counts = _progress(db, ids, who["id"] if who["role"] == "student" else None)
    if who["role"] == "student":
        submitted_mine = min(counts["submitted"], len(ids)) if ids else 0
        reviewed = counts["reviewed"]
    else:
        submitted_mine, reviewed = 0, counts["reviewed"]
    course_id, course_name = _course(db, "paper", ids)
    class_name = None
    if row["class_id"]:
        found = db.execute("SELECT name FROM training_classes WHERE id=?", (row["class_id"],)).fetchone()
        class_name = found["name"] if found else None
    due_at = row["deadline_at"]
    task = {
        "kind": "paper", "id": row["id"], "title": row["title"], "task_type": "paper",
        "course_id": course_id, "course_name": course_name,
        "class_id": row["class_id"], "class_name": class_name,
        "published": bool(row["published"]), "open_at": sett["open_at"], "due_at": due_at,
        "state": _task_state(bool(row["published"]), bool(sett["archived_at"]), sett["open_at"], due_at),
        "submit_limit": int(sett["submit_limit"]),
        "correction_limit": int(sett["correction_limit"]),
        "retake_policy": sett["retake_policy"],
        "answer_release": sett["answer_release"], "answer_released_at": sett["answer_released_at"],
        "question_count": len(ids), "total_points": float(sum(float(i["points"]) for i in items)),
        "expected": _class_scope(db, who["org_id"], row["class_id"]),
        "submitted_total": counts["submitted"] if who["role"] == "teacher" else None,
        "submitted_mine": submitted_mine,
        "reviewed_total": reviewed if who["role"] == "teacher" else None,
        "mine_reviewed": reviewed if who["role"] == "student" else 0,
    }
    # 组卷按题提交，整份"已提交"以全部题目都已作答为准
    task["fully_submitted"] = bool(ids) and submitted_mine >= len(ids)
    task["answers_visible"] = _answers_visible(task, who)
    remaining = len(ids) - submitted_mine
    task["can_submit"] = bool(who["role"] == "student" and _can_submit(task, remaining))
    return task


def _owned_assignment(db, who, subject_id: str):
    row = db.execute("SELECT * FROM assignments WHERE id=? AND org_id=?", (subject_id, who["org_id"])).fetchone()
    if not row:
        raise TaskError("任务不存在。", 404)
    if who["role"] == "teacher" and row["created_by"] != who["id"]:
        raise TaskError("任务不存在。", 404)
    return row


def _owned_paper(db, who, subject_id: str):
    row = db.execute("SELECT * FROM training_papers WHERE id=? AND org_id=?", (subject_id, who["org_id"])).fetchone()
    if not row:
        raise TaskError("任务不存在。", 404)
    if who["role"] == "teacher" and row["owner_id"] != who["id"]:
        raise TaskError("任务不存在。", 404)
    return row


def _subject_row(db, who, kind: str, subject_id: str):
    if kind == "assignment":
        row = _owned_assignment(db, who, subject_id)
        item = db.execute("SELECT * FROM training_assignment_settings WHERE assignment_id=?", (subject_id,)).fetchone()
        data = dict(row)
        data["class_id"] = item["class_id"] if item else None
        data["deadline_at"] = item["deadline_at"] if item else None
        data["paper_id"] = item["paper_id"] if item else None
        return data
    return dict(_owned_paper(db, who, subject_id))


def list_tasks(db, who: dict[str, Any]) -> list[dict[str, Any]]:
    """教师看自己创建的任务；学生看已发布、未归档、且在范围内的任务。"""
    tasks: list[dict[str, Any]] = []
    if who["role"] == "teacher":
        for row in db.execute(
            "SELECT a.*,s.class_id,s.paper_id,s.deadline_at FROM assignments a "
            "LEFT JOIN training_assignment_settings s ON s.assignment_id=a.id "
            "WHERE a.org_id=? AND a.created_by=? AND (s.paper_id IS NULL) ORDER BY a.created_at DESC",
            (who["org_id"], who["id"]),
        ).fetchall():
            tasks.append(_assignment_task(db, dict(row), who))
        for row in db.execute(
            "SELECT * FROM training_papers WHERE org_id=? AND owner_id=? ORDER BY created_at DESC",
            (who["org_id"], who["id"]),
        ).fetchall():
            tasks.append(_paper_task(db, dict(row), who))
    else:
        rows = db.execute(
            """SELECT a.*,s.class_id,s.paper_id,s.deadline_at FROM assignments a
               LEFT JOIN training_assignment_settings s ON s.assignment_id=a.id
               WHERE a.org_id=? AND a.published=1 AND (a.target_student_id IS NULL OR a.target_student_id=?)
                 AND (s.paper_id IS NULL)
                 AND (s.class_id IS NULL OR EXISTS(SELECT 1 FROM training_class_members m
                      WHERE m.class_id=s.class_id AND m.student_id=?))
               ORDER BY a.created_at DESC""",
            (who["org_id"], who["id"], who["id"]),
        ).fetchall()
        for row in rows:
            tasks.append(_assignment_task(db, dict(row), who))
        papers = db.execute(
            """SELECT p.* FROM training_papers p
               WHERE p.org_id=? AND p.published=1
                 AND (p.class_id IS NULL OR EXISTS(SELECT 1 FROM training_class_members m
                      WHERE m.class_id=p.class_id AND m.student_id=?))
               ORDER BY p.created_at DESC""",
            (who["org_id"], who["id"]),
        ).fetchall()
        for row in papers:
            tasks.append(_paper_task(db, dict(row), who))
    live = [t for t in tasks if not (who["role"] == "student" and t["state"] == "archived")]
    live.sort(key=lambda t: (t["state"] == "archived", t["due_at"] or "9999"))
    return live


def detail(db, who: dict[str, Any], kind: str, subject_id: str) -> dict[str, Any]:
    if kind not in KINDS:
        raise TaskError("任务类型不正确。", 404)
    row = _subject_row(db, who, kind, subject_id)
    if kind == "assignment":
        if row.get("paper_id"):
            raise TaskError("这是组卷题目，请打开所属试卷。", 409)
        task = _assignment_task(db, row, who)
    else:
        task = _paper_task(db, row, who)
    if who["role"] == "student":
        if not task["published"] or task["state"] in {"draft", "archived"}:
            raise TaskError("任务不存在。", 404)
        if task["class_id"]:
            member = db.execute(
                "SELECT 1 FROM training_class_members WHERE class_id=? AND student_id=?",
                (task["class_id"], who["id"]),
            ).fetchone()
            if not member and row.get("target_student_id") != who["id"]:
                raise TaskError("任务不存在。", 404)
    return task


def preview(db, who: dict[str, Any], kind: str, subject_id: str) -> dict[str, Any]:
    """教师预览学生视角：结构与学生看到的一致，答案仅教师可见。"""
    task = detail(db, who, kind, subject_id)
    student_view = dict(task)
    student_view["submitted_mine"] = 0
    student_view["can_submit"] = task["state"] == "published"
    student_view["answers_visible"] = bool(task["answer_release"] == "after_submit")
    student_view["preview"] = True
    if kind == "paper":
        items = db.execute(
            """SELECT a.id,a.title,s.position,s.points FROM training_assignment_settings s
               JOIN assignments a ON a.id=s.assignment_id WHERE s.paper_id=? ORDER BY s.position""",
            (subject_id,),
        ).fetchall()
        student_view["questions"] = [dict(r) for r in items]
    else:
        student_view["questions"] = [{"id": task["id"], "title": task["title"], "position": 1,
                                      "points": 100.0}]
    return {"task": student_view, "answers_included": True,
            "note": "预览内容仅教师可见；学生端到开放时间且满足答案策略后才显示结果。"}


def update_settings(db, who: dict[str, Any], kind: str, subject_id: str, body) -> dict[str, Any]:
    task = detail(db, who, kind, subject_id)
    if who["role"] != "teacher":
        raise TaskError("只有任课教师可以调整任务设置。", 403)
    if task["state"] == "archived":
        raise TaskError("已归档任务不能改设置，请新建任务。", 409)
    if body.submit_limit != 1:
        raise TaskError("多次提交需要提交版本记录，随订正流程一起开放；当前仅支持限交 1 次。", 409)
    if body.answer_release not in ANSWER_RELEASE:
        raise TaskError("答案开放方式不正确。")
    if body.open_at and task["due_at"] and body.open_at >= task["due_at"]:
        raise TaskError("开放时间必须早于截止时间。")
    previous = _settings(db, kind, subject_id)
    has_submissions = bool(task["submitted_total"] or task["submitted_mine"])
    if has_submissions and (body.open_at != previous["open_at"]
                            or body.submit_limit != previous["submit_limit"]):
        raise TaskError("已有学生作答，排期和提交次数不能再改；需要调整请归档后新建任务，保留原记录。", 409)
    db.execute(
        """INSERT INTO training_task_settings
             (subject_kind,subject_id,org_id,open_at,submit_limit,correction_limit,retake_policy,answer_release,updated_by,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(subject_kind,subject_id) DO UPDATE SET
             open_at=excluded.open_at, submit_limit=excluded.submit_limit,
             correction_limit=excluded.correction_limit, retake_policy=excluded.retake_policy,
             answer_release=excluded.answer_release, updated_by=excluded.updated_by,
             updated_at=excluded.updated_at""",
        (kind, subject_id, who["org_id"], body.open_at, body.submit_limit,
         body.correction_limit, body.retake_policy, body.answer_release, who["id"], now()),
    )
    return detail(db, who, kind, subject_id)


def release_answers(db, who: dict[str, Any], kind: str, subject_id: str) -> dict[str, Any]:
    task = detail(db, who, kind, subject_id)
    if who["role"] != "teacher":
        raise TaskError("只有任课教师可以开放答案。", 403)
    if task["answer_release"] != "manual":
        raise TaskError("该任务的答案按策略自动开放，不需要手工开放。", 409)
    db.execute(
        """INSERT INTO training_task_settings (subject_kind,subject_id,org_id,answer_release,answer_released_at,updated_by,updated_at)
           VALUES (?,?,?,'manual',?,?,?)
           ON CONFLICT(subject_kind,subject_id) DO UPDATE SET
             answer_released_at=excluded.answer_released_at,
             updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
        (kind, subject_id, who["org_id"], now(), who["id"], now()),
    )
    return detail(db, who, kind, subject_id)


def set_archived(db, who: dict[str, Any], kind: str, subject_id: str, archived: bool) -> dict[str, Any]:
    task = detail(db, who, kind, subject_id)
    if who["role"] != "teacher":
        raise TaskError("只有任课教师可以归档任务。", 403)
    if not archived and task["state"] != "archived":
        raise TaskError("该任务未归档。", 409)
    db.execute(
        """INSERT INTO training_task_settings (subject_kind,subject_id,org_id,archived_at,updated_by,updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(subject_kind,subject_id) DO UPDATE SET
             archived_at=excluded.archived_at, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
        (kind, subject_id, who["org_id"], now() if archived else None, who["id"], now()),
    )
    return detail(db, who, kind, subject_id)


# --------------------------------------------------------------------------- 接口

class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open_at: str | None = None
    submit_limit: int = Field(default=1, ge=1, le=9)
    correction_limit: int = Field(default=1, ge=0, le=5)
    retake_policy: Literal["teacher_return", "free"] = "teacher_return"
    answer_release: Literal["after_submit", "after_due", "manual"] = "after_due"


class ArchiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool = True


def register(app, store_provider: Callable[[], Any], user_fn: Callable[..., dict], cookie_name: str) -> None:
    def response(value: Any) -> JSONResponse:
        return JSONResponse(value, headers={"Cache-Control": "private, no-store"})

    def guarded(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except TaskError as exc:
                raise HTTPException(exc.status, str(exc)) from None
        return wrapper

    def actor(session: str | None) -> dict[str, Any]:
        user = user_fn(session)
        if user["role"] not in {"teacher", "student"}:
            raise HTTPException(403, "当前账号没有课程任务权限。")
        return user

    def write(session: str | None, handler):
        user = actor(session)
        repo = store_provider()
        with repo.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"teacher"})
            value = handler(db, user)
            repo._log(db, user, "update_task_settings", "task", f"{value['kind']}:{value['id']}", value["state"])
        return response(value)

    @app.get("/api/training/tasks")
    @guarded
    def tasks(session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {"teacher", "student"})
            return response({"items": list_tasks(db, user),
                             "scope": "教师只看到本人创建的任务；学生只看到已发布、未归档且在自己班级内的任务。"})

    @app.get("/api/training/tasks/{kind}/{subject_id}")
    @guarded
    def one(kind: str, subject_id: str, session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {"teacher", "student"})
            return response(detail(db, user, kind, subject_id))

    @app.get("/api/training/tasks/{kind}/{subject_id}/preview")
    @guarded
    def preview_one(kind: str, subject_id: str, session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有教师可以预览学生视角。")
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {"teacher"})
            return response(preview(db, user, kind, subject_id))

    @app.put("/api/training/tasks/{kind}/{subject_id}/settings")
    @guarded
    def settings(kind: str, subject_id: str, body: SettingsBody,
                 session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        return write(session, lambda db, user: update_settings(db, user, kind, subject_id, body))

    @app.post("/api/training/tasks/{kind}/{subject_id}/answers/release")
    @guarded
    def release(kind: str, subject_id: str, session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        return write(session, lambda db, user: release_answers(db, user, kind, subject_id))

    @app.post("/api/training/tasks/{kind}/{subject_id}/archive")
    @guarded
    def archive(kind: str, subject_id: str, body: ArchiveBody,
                session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        return write(session, lambda db, user: set_archived(db, user, kind, subject_id, body.archived))
