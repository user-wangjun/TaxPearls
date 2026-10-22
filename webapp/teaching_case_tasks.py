"""实操案例 → 学生任务链路（重设计方案 B4 前半：先建链路，量规随后挂）。

为什么不是"第二条任务系统"
--------------------------
案例任务**不新建任务对象**：发布即建一道既有 ``assignments``，把班级、截止时间、
课程绑定、发布状态、提交与复核、草稿/退回/版本、答案开放策略全部交给既有主链，
本模块只旁挂两样东西：

1. ``teaching_case_task``：这道作业用的是哪个案例、本次训练哪几步、评价哪几维；
2. ``teaching_case_delivery``：学生按五步填写的过程性交付物（加密列）。

案例本身（情境、材料包、标准结论、绑定规则）在 B1 的 ``teaching_case`` 表里，
本模块不复制一份——避免"改了案例，已发布任务跟着变"。

口径（设计方案 §2.1 / §2.3，不得改）
------------------------------------
- 五步是完整模式，**短练习只训练其中一部分**：发布时挑 ``steps``，没挑的步骤
  学生不用填，也不参与评价。
- 四维量规同理：发布时挑 ``dimensions``，没挑的维度是"不适用"，**不按 0 分算**。
- 案例的 ``standard_conclusion``（标准结论）属于答案，**按任务的答案开放策略下发**，
  与客观题解析共用 ``training_submissions.result_visible`` 的判定。
- 学生可见性由 ``training_tasks.detail`` 判定（未发布/已归档/跨机构/非本班 404），
  本模块不再自己判一遍，避免两处口径不同步。
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Callable

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp.access import current_actor

STEPS = ("receive", "identify", "reconcile", "judge", "evidence")
STEP_LABELS = {"receive": "① 接收材料", "identify": "② 识别要素", "reconcile": "③ 勾稽分析",
               "judge": "④ 作出判断", "evidence": "⑤ 提交证据与建议"}
STEP_HINTS = {
    "receive": "核对主体、期间与材料清单，写清缺了什么（缺材料不当作零业务）。",
    "identify": "提取金额、税额、日期、主体、状态等要素，并注明每项在原件的什么位置。",
    "reconcile": "写明指标口径、计算过程与差异解释；数据不足时标「未执行」，不补零凑结论。",
    "judge": "逐条区分命中 / 通过 / 未执行，写清依据版本与不确定项。",
    "evidence": "形成「结论—指标—原件位置—规则版本—政策依据—建议」的证据链。",
}
DIMENSIONS = ("material", "reconcile", "basis", "trace")
DIMENSION_LABELS = {"material": "材料与要素", "reconcile": "勾稽与口径",
                    "basis": "判定与依据", "trace": "留痕与建议"}
_CONTENT_MAX = 20_000


class CaseTaskError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def migrate(db) -> None:
    """案例任务与五步交付物。全部 additive：只建新表，不动既有结构。

    ``teaching_case`` 由 B1 在 schema 里建；本函数在建表前先确认它存在，
    避免在旧库上先跑本模块时留下指向不存在表的外键。
    """
    tables = {row["name"] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "teaching_case" not in tables or "assignments" not in tables:
        return
    db.executescript("""
        CREATE TABLE IF NOT EXISTS teaching_case_task (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL REFERENCES teaching_case(id),
            assignment_id TEXT NOT NULL REFERENCES assignments(id),
            steps_json TEXT NOT NULL DEFAULT '[]',       -- 本次训练的步骤（STEPS 子集）
            dimensions_json TEXT NOT NULL DEFAULT '[]',  -- 本次评价的维度（DIMENSIONS 子集）
            created_by TEXT REFERENCES users(id),
            created_at TEXT NOT NULL,
            archived_at TEXT,                            -- 归档不删行，学生记录保留
            UNIQUE(assignment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_teaching_case_task_case
            ON teaching_case_task(case_id, archived_at);
        CREATE TABLE IF NOT EXISTS teaching_case_delivery (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES teaching_case_task(id),
            student_id TEXT NOT NULL REFERENCES users(id),
            step TEXT NOT NULL CHECK (step IN ('receive','identify','reconcile','judge','evidence')),
            content TEXT,                                -- 加密列：学生过程性作答
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, student_id, step)
        );
        CREATE INDEX IF NOT EXISTS idx_teaching_case_delivery_task
            ON teaching_case_delivery(task_id, student_id);
    """)


# --------------------------------------------------------------------------- 取数

def task_row(db, assignment_id: str):
    return db.execute("SELECT * FROM teaching_case_task WHERE assignment_id=?",
                      (assignment_id,)).fetchone()


def task_by_id(db, task_id: str):
    return db.execute("SELECT * FROM teaching_case_task WHERE id=?", (task_id,)).fetchone()


def _payload(row, db) -> dict[str, Any]:
    case = db.execute("SELECT * FROM teaching_case WHERE id=?", (row["case_id"],)).fetchone()
    assignment = db.execute("SELECT id,title,published FROM assignments WHERE id=?",
                            (row["assignment_id"],)).fetchone()
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "assignment_id": row["assignment_id"],
        "assignment_title": assignment["title"] if assignment else None,
        "steps": json.loads(row["steps_json"]),
        "step_labels": [STEP_LABELS[s] for s in json.loads(row["steps_json"])],
        "dimensions": json.loads(row["dimensions_json"]),
        "dimension_labels": [DIMENSION_LABELS[d] for d in json.loads(row["dimensions_json"])],
        "archived": bool(row["archived_at"]),
        "created_at": row["created_at"],
        "case_name": case["name"] if case else None,
    }


def _case_payload(row) -> dict[str, Any]:
    """案例本体：情境、材料包、规则、脱敏说明。标准结论单独按答案策略下发。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "scenario": row["scenario"],
        "materials": json.loads(row["materials_json"]),
        "rule_codes": json.loads(row["rule_codes_json"]),
        "desensitization_note": row["desensitization_note"],
        "status": row["status"],
    }


def _deliveries(db, task_id: str, student_id: str) -> list[dict[str, Any]]:
    # 身份列必须一起 SELECT：行工厂按 task_id/student_id/step 校验解密上下文
    rows = db.execute(
        """SELECT task_id,student_id,step,content,updated_at FROM teaching_case_delivery
           WHERE task_id=? AND student_id=?""", (task_id, student_id)).fetchall()
    return [{"step": r["step"], "content": r["content"], "updated_at": r["updated_at"]}
            for r in rows]   # content 已由行工厂解密


# --------------------------------------------------------------------------- 发布

def _clean_subset(raw: Any, field: str, valid: tuple[str, ...], *, required: bool) -> list[str]:
    if raw is None:
        raw = list(valid)
    if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
        raise CaseTaskError(f"{field} 须为字符串数组。")
    picked = [v for v in dict.fromkeys(raw) if v in valid]
    unknown = [v for v in raw if v not in valid]
    if unknown:
        raise CaseTaskError(f"{field} 含未知项：{', '.join(unknown)}。")
    if required and not picked:
        raise CaseTaskError(f"{field} 不能为空。")
    return picked


def publish(store, who: dict[str, Any], case_id: str, *, class_id: str, audit_id: str,
            title: str | None = None, course_id: str | None = None,
            deadline_at: str | None = None, steps: Any = None, dimensions: Any = None,
            published: bool = True) -> dict[str, Any]:
    """把案例发布成一道班级作业。

    ``audit_id`` 是必需的数据底座：学生要勾稽、判断的对象是那个仿真案例的数据集；
    案例本体只提供情境、材料清单、标准结论与评价维度。
    """
    if who["role"] != "teacher":
        raise CaseTaskError("只有任课教师可以发布案例任务。", 403)
    with store.connect() as db:
        db.execute("BEGIN")
        current_actor(db, who, {"teacher"})
        case = db.execute("SELECT * FROM teaching_case WHERE id=?", (case_id,)).fetchone()
        if case is None:
            raise CaseTaskError("案例不存在。", 404)
        if case["status"] != "published":
            raise CaseTaskError("案例还是草稿，请先把案例发布，再发布成学生任务。", 409)
        picked_steps = _clean_subset(steps, "steps", STEPS, required=True)
        picked_dims = _clean_subset(dimensions, "dimensions", DIMENSIONS, required=True)
        if not class_id:
            raise CaseTaskError("发布成任务必须指定班级。")
    assignment_id = store.create_assignment(
        who, str(title or case["name"]).strip()[:200], audit_id, None, {}, 0.0,
        published, class_id=class_id, deadline_at=deadline_at, course_id=course_id)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current_actor(db, who, {"teacher"})
        task_id = uuid.uuid4().hex
        db.execute(
            """INSERT INTO teaching_case_task
                 (id,case_id,assignment_id,steps_json,dimensions_json,created_by,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (task_id, case_id, assignment_id, json.dumps(picked_steps),
             json.dumps(picked_dims), who["id"], now()))
        row = task_by_id(db, task_id)
        value = _payload(row, db)
    value["assignment_id"] = assignment_id
    value["note"] = "案例已发布成作业；班级、截止时间、提交与复核沿用既有任务主链。"
    return value


def list_tasks(store, who: dict[str, Any], case_id: str | None = None) -> list[dict[str, Any]]:
    with store.connect() as db:
        db.execute("BEGIN")
        current_actor(db, who, {"teacher"})
        if case_id:
            rows = db.execute(
                """SELECT t.* FROM teaching_case_task t JOIN assignments a ON a.id=t.assignment_id
                   WHERE t.case_id=? AND a.created_by=? AND a.org_id=? ORDER BY t.created_at DESC""",
                (case_id, who["id"], who["org_id"])).fetchall()
        else:
            rows = db.execute(
                """SELECT t.* FROM teaching_case_task t JOIN assignments a ON a.id=t.assignment_id
                   WHERE a.created_by=? AND a.org_id=? ORDER BY t.created_at DESC""",
                (who["id"], who["org_id"])).fetchall()
        return [_payload(r, db) for r in rows]


def set_archived(store, who: dict[str, Any], task_id: str, archived: bool) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current_actor(db, who, {"teacher"})
        row = task_by_id(db, task_id)
        if row is None:
            raise CaseTaskError("案例任务不存在。", 404)
        assignment = db.execute("SELECT created_by,org_id FROM assignments WHERE id=?",
                                (row["assignment_id"],)).fetchone()
        if not assignment or assignment["created_by"] != who["id"] \
                or assignment["org_id"] != who["org_id"]:
            raise CaseTaskError("案例任务不存在。", 404)
        db.execute("UPDATE teaching_case_task SET archived_at=? WHERE id=?",
                   (now() if archived else None, task_id))
        value = _payload(task_by_id(db, task_id), db)
        store._log(db, who, "archive_case_task", "case_task", task_id, f"archived={int(archived)}")
    return value


# --------------------------------------------------------------------------- 学生视角

def _editable(db, assignment_id: str, student_id: str) -> tuple[bool, str]:
    """截止前、且还没正式提交（或已被退回）才能改交付物。"""
    from webapp import training_submissions
    _, due = training_submissions.task_row(db, assignment_id)
    if due and now() >= due:
        return False, "已到截止时间，不能再修改交付物。"
    status = training_submissions.state_of(db, assignment_id, student_id)["status"]
    if status in {"submitted", "pending_review", "done"}:
        return False, "已正式提交，需要修改请等任课教师退回。"
    return True, ""


def student_view(store, who: dict[str, Any], assignment_id: str) -> dict[str, Any]:
    """学生在作业页看到的案例任务：情境、材料、本次要交哪些步骤、自己填了什么。"""
    from webapp import training_submissions
    with store.connect() as db:
        db.execute("BEGIN")
        # 可见性交给主链：未发布/已归档/跨机构/非本班一律 404，这里不再判一遍
        task = _visible_assignment(db, who, assignment_id)
        row = task_row(db, assignment_id)
        if row is None or row["archived_at"]:
            raise CaseTaskError("这道作业不是实操案例任务。", 404)
        case = db.execute("SELECT * FROM teaching_case WHERE id=?", (row["case_id"],)).fetchone()
        if case is None:
            raise CaseTaskError("关联案例不存在。", 404)
        picked = json.loads(row["steps_json"])
        visible, note = training_submissions.result_visible(db, assignment_id, who["id"])
        editable, edit_note = _editable(db, assignment_id, who["id"])
        filled = {d["step"]: d for d in _deliveries(db, row["id"], who["id"])}
    return {
        "task": {"id": row["id"], "steps": picked,
                 "step_labels": [STEP_LABELS[s] for s in picked],
                 "dimensions": json.loads(row["dimensions_json"]),
                 "dimension_labels": [DIMENSION_LABELS[d] for d in json.loads(row["dimensions_json"])],
                 "case_id": row["case_id"], "assignment_id": assignment_id},
        "case": _case_payload(case),
        "standard_conclusion": (json.loads(case["standard_conclusion_json"]) if visible else []),
        "conclusion_note": "" if visible else note,
        "steps_detail": [{"step": s, "label": STEP_LABELS[s], "hint": STEP_HINTS[s],
                          "content": (filled.get(s) or {}).get("content") or "",
                          "updated_at": (filled.get(s) or {}).get("updated_at")}
                         for s in picked],
        "editable": editable,
        "edit_note": edit_note,
        "deadline_at": task["due_at"],
        "my_status": task.get("my_status"),
    }


def _visible_assignment(db, who: dict[str, Any], assignment_id: str) -> dict[str, Any]:
    """可见性交给主链：未发布/已归档/跨机构/非本班一律 404。

    主链抛的是 ``TaskError``，这里统一转成 ``CaseTaskError``，免得走成 500。
    """
    from webapp import training_tasks
    try:
        return training_tasks.detail(db, who, "assignment", assignment_id)
    except training_tasks.TaskError as exc:
        raise CaseTaskError(str(exc), exc.status) from None


def save_delivery(store, who: dict[str, Any], assignment_id: str, step: str,
                  content: str | None) -> dict[str, Any]:
    if step not in STEPS:
        raise CaseTaskError("步骤不正确。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current_actor(db, who, {"student"})
        _visible_assignment(db, who, assignment_id)   # 可见性与班级校验
        row = task_row(db, assignment_id)
        if row is None or row["archived_at"]:
            raise CaseTaskError("这道作业不是实操案例任务。", 404)
        if step not in json.loads(row["steps_json"]):
            raise CaseTaskError("本次任务不训练这一步。", 409)
        editable, note = _editable(db, assignment_id, who["id"])
        if not editable:
            raise CaseTaskError(note, 409)
        text = (content or "").strip()
        if len(text) > _CONTENT_MAX:
            raise CaseTaskError(f"这一步最多 {_CONTENT_MAX} 字。")
        if not text:
            db.execute("DELETE FROM teaching_case_delivery WHERE task_id=? AND student_id=? AND step=?",
                       (row["id"], who["id"], step))
            return {"step": step, "content": "", "updated_at": None}
        codec = store._field_codec
        value = codec.seal(text, "teaching_case_delivery", "content", row["id"], who["id"], step)
        db.execute(
            """INSERT INTO teaching_case_delivery (id,task_id,student_id,step,content,updated_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(task_id,student_id,step) DO UPDATE SET
                 content=excluded.content, updated_at=excluded.updated_at""",
            (secrets.token_hex(12), row["id"], who["id"], step, value, now()))
        fresh = db.execute(
            "SELECT task_id,student_id,step,content,updated_at FROM teaching_case_delivery"
            " WHERE task_id=? AND student_id=? AND step=?", (row["id"], who["id"], step)).fetchone()
    return {"step": fresh["step"], "content": fresh["content"], "updated_at": fresh["updated_at"]}


def teacher_deliveries(store, who: dict[str, Any], task_id: str,
                       student_id: str | None = None) -> dict[str, Any]:
    """教师看本班学生的五步交付物。"""
    with store.connect() as db:
        db.execute("BEGIN")
        current_actor(db, who, {"teacher"})
        row = task_by_id(db, task_id)
        if row is None:
            raise CaseTaskError("案例任务不存在。", 404)
        assignment = db.execute("SELECT created_by,org_id FROM assignments WHERE id=?",
                                (row["assignment_id"],)).fetchone()
        if not assignment or assignment["created_by"] != who["id"] \
                or assignment["org_id"] != who["org_id"]:
            raise CaseTaskError("案例任务不存在。", 404)
        members = [r["student_id"] for r in db.execute(
            """SELECT m.student_id FROM training_class_members m
               JOIN training_assignment_settings s ON s.class_id=m.class_id
               WHERE s.assignment_id=? ORDER BY m.student_id""", (row["assignment_id"],)).fetchall()]
        targets = [student_id] if student_id else members
        targets = [s for s in targets if s in set(members)]
        steps = json.loads(row["steps_json"])
        items = []
        for sid in targets:
            filled = {d["step"]: d for d in _deliveries(db, task_id, sid)}
            items.append({"student_id": sid,
                          "steps": [{"step": s, "label": STEP_LABELS[s],
                                     "content": (filled.get(s) or {}).get("content") or "",
                                     "updated_at": (filled.get(s) or {}).get("updated_at")}
                                    for s in steps]})
    return {"task_id": task_id, "steps": steps, "items": items}


# --------------------------------------------------------------------------- 接口

class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    class_id: str = Field(min_length=1, max_length=64)
    audit_id: str = Field(min_length=1, max_length=64)
    course_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    deadline_at: str | None = None
    published: bool = True
    steps: list[str] = Field(default_factory=list, max_length=5)
    dimensions: list[str] = Field(default_factory=list, max_length=4)


class DeliveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str = Field(min_length=1, max_length=32)
    content: str | None = Field(default=None, max_length=_CONTENT_MAX)


class ArchiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool = True


def register(app, store_provider: Callable[[], Any], user_fn: Callable[..., dict],
             cookie_name: str) -> None:
    def response(value: Any) -> JSONResponse:
        return JSONResponse(value, headers={"Cache-Control": "private, no-store"})

    def guarded(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except CaseTaskError as exc:
                raise HTTPException(exc.status, str(exc)) from None
        return wrapper

    def actor(session: str | None) -> dict[str, Any]:
        user = user_fn(session)
        if user["role"] not in {"teacher", "student"}:
            raise HTTPException(403, "当前账号没有课程任务权限。")
        return user

    @app.post("/api/training/teaching/cases/{case_id}/publish")
    @guarded
    def publish_case(case_id: str, body: PublishBody,
                     session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有任课教师可以发布案例任务。")
        value = publish(store_provider(), user, case_id, class_id=body.class_id,
                        audit_id=body.audit_id, title=body.title, course_id=body.course_id,
                        deadline_at=body.deadline_at, steps=body.steps,
                        dimensions=body.dimensions, published=body.published)
        return response(value)

    @app.get("/api/training/teaching/case-tasks")
    @guarded
    def tasks(case_id: str | None = None,
              session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有任课教师可以查看案例任务。")
        return response({"items": list_tasks(store_provider(), user, case_id)})

    @app.post("/api/training/teaching/case-tasks/{task_id}/archive")
    @guarded
    def archive(task_id: str, body: ArchiveBody,
                session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有任课教师可以归档案例任务。")
        return response(set_archived(store_provider(), user, task_id, body.archived))

    @app.get("/api/training/teaching/case-tasks/{task_id}/deliveries")
    @guarded
    def deliveries(task_id: str, student_id: str | None = None,
                   session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有任课教师可以查看学生交付物。")
        return response(teacher_deliveries(store_provider(), user, task_id, student_id))

    @app.get("/api/assignments/{assignment_id}/case")
    @guarded
    def student_case(assignment_id: str,
                     session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "student":
            raise HTTPException(403, "只有学生可以看本人要完成的实操步骤。")
        return response(student_view(store_provider(), user, assignment_id))

    @app.put("/api/assignments/{assignment_id}/case-delivery")
    @guarded
    def save(assignment_id: str, body: DeliveryBody,
             session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "student":
            raise HTTPException(403, "只有学生可以填写本人交付物。")
        return response(save_delivery(store_provider(), user, assignment_id,
                                      body.step, body.content))
