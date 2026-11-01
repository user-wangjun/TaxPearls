"""实操案例的四维评价量规（重设计方案 §2.3 / B4 后半）。

口径（用户 2026-10-22 确认，不得改）
------------------------------------
1. **只用于实操案例**。客观题沿用题目评分，量规不参与、也不改写 ``adjusted_score``。
2. **旁挂维度明细表，总分单独展示**。量规总分与客观题成绩在页面上分开展示，
   不混成一个"课程总分"；确需折算必须由教师在发布前写清权重与公式。
3. **量规带版本，发布后冻结**。改权重时若已有评分，不原位覆盖，升一个新版本；
   旧分留在旧版本上，回看时读得到当时的口径。

评分细则（设计方案 §2.3）
------------------------
- 每维 0/1/2 分，默认满分 = 2 × 维度数（全四维即 8 分）。
- 短练习没训练到的维度是**不适用**，存 ``NULL``。**不按 0 分算，也不按满分算**，
  分母按实际参与评价的维度数算，页面上要显示真实分母。
- 权重可按任务配置（默认每维 1.0），加权后给出百分制便于对照。
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
from webapp.teaching_case_tasks import DIMENSIONS, DIMENSION_LABELS, CaseTaskError, task_by_id

MAX_PER_DIMENSION = 2
_COMMENT_MAX = 5_000


class RubricError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def migrate(db) -> None:
    """量规定义（带版本）与维度明细分。全部 additive。"""
    tables = {row["name"] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "teaching_case_task" not in tables:
        return   # 案例任务表还没建（顺序由 schema.initialize 保证），下次自会补上
    db.executescript("""
        CREATE TABLE IF NOT EXISTS training_rubric (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES teaching_case_task(id),
            version INTEGER NOT NULL,                 -- 同一任务内递增，旧版本保留
            dimensions_json TEXT NOT NULL,            -- [{key,label,weight}] 冻结的维度与权重
            note TEXT NOT NULL DEFAULT '',            -- 权重口径说明（展示给学生）
            created_by TEXT REFERENCES users(id),
            created_at TEXT NOT NULL,
            UNIQUE(task_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_training_rubric_task
            ON training_rubric(task_id, version DESC);
        CREATE TABLE IF NOT EXISTS training_rubric_score (
            id TEXT PRIMARY KEY,
            rubric_id TEXT NOT NULL REFERENCES training_rubric(id),
            student_id TEXT NOT NULL REFERENCES users(id),
            dimension TEXT NOT NULL,
            score INTEGER CHECK (score IS NULL OR score BETWEEN 0 AND 2),  -- NULL=不适用
            comment TEXT,                             -- 加密列：教师给该维度的意见
            scored_by TEXT REFERENCES users(id),
            scored_at TEXT NOT NULL,
            UNIQUE(rubric_id, student_id, dimension)
        );
        CREATE INDEX IF NOT EXISTS idx_training_rubric_score_student
            ON training_rubric_score(rubric_id, student_id);
    """)


# --------------------------------------------------------------------------- 量规本体

def _default_dimensions(picked: list[str]) -> list[dict[str, Any]]:
    return [{"key": d, "label": DIMENSION_LABELS[d], "weight": 1.0} for d in picked]


def _clean_dimensions(raw: Any, picked: list[str]) -> list[dict[str, Any]]:
    """权重只对本次评价的维度生效；给了未评价的维度直接拒，避免"配了不生效"。"""
    if raw is None:
        return _default_dimensions(picked)
    if not isinstance(raw, list) or not raw:
        raise RubricError("dimensions 须为非空数组。")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RubricError("维度项须为对象。")
        key = str(item.get("key", "")).strip()
        if key not in DIMENSIONS:
            raise RubricError(f"未知维度：{key or '(空)'}。")
        if key not in picked:
            raise RubricError(f"本次任务不评价「{DIMENSION_LABELS[key]}」，不能配权重。", 409)
        if key in seen:
            raise RubricError(f"维度重复：{DIMENSION_LABELS[key]}。")
        weight = item.get("weight", 1.0)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) \
                or weight <= 0 or weight > 10:
            raise RubricError(f"「{DIMENSION_LABELS[key]}」的权重须为 0 到 10 之间的数。")
        seen.add(key)
        result.append({"key": key, "label": DIMENSION_LABELS[key], "weight": round(float(weight), 2)})
    missing = [d for d in picked if d not in seen]
    if missing:
        raise RubricError("本次评价的维度都要配权重，缺：" + "、".join(DIMENSION_LABELS[d] for d in missing))
    return result


def current_rubric(db, task_id: str):
    return db.execute("SELECT * FROM training_rubric WHERE task_id=? ORDER BY version DESC, rowid DESC LIMIT 1",
                      (task_id,)).fetchone()


def ensure_rubric(db, task_id: str, who: dict[str, Any]) -> dict[str, Any]:
    """取当前量规；没有就按任务发布时选的维度建 v1（默认每维权重 1）。"""
    row = current_rubric(db, task_id)
    if row:
        return _rubric_payload(row)
    task = task_by_id(db, task_id)
    if task is None:
        raise RubricError("案例任务不存在。", 404)
    return save_rubric(db, task_id, who, None, "")


def save_rubric(db, task_id: str, who: dict[str, Any], dimensions: Any, note: str) -> dict[str, Any]:
    """配（改）权重。已有评分时不覆盖，升一个新版本。"""
    task = task_by_id(db, task_id)
    if task is None:
        raise RubricError("案例任务不存在。", 404)
    picked = json.loads(task["dimensions_json"])
    cleaned = _clean_dimensions(dimensions, picked)
    previous = current_rubric(db, task_id)
    scored = 0
    if previous:
        scored = db.execute("SELECT COUNT(*) c FROM training_rubric_score WHERE rubric_id=?",
                            (previous["id"],)).fetchone()["c"]
    if previous and not scored and previous["dimensions_json"] == json.dumps(cleaned) \
            and str(note or "") == previous["note"]:
        return _rubric_payload(previous)     # 幂等：没变就不升版本
    if previous and scored:
        version = int(previous["version"]) + 1
    elif previous:
        # 没评分：原位更新这一版，避免"试改一下"就攒出一串版本
        db.execute("UPDATE training_rubric SET dimensions_json=?, note=?, created_by=?, created_at=?"
                   " WHERE id=?", (json.dumps(cleaned), str(note or "")[:_COMMENT_MAX],
                                   who["id"], now(), previous["id"]))
        return _rubric_payload(db.execute("SELECT * FROM training_rubric WHERE id=?",
                                          (previous["id"],)).fetchone())
    else:
        version = 1
    rubric_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO training_rubric (id,task_id,version,dimensions_json,note,created_by,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (rubric_id, task_id, version, json.dumps(cleaned), str(note or "")[:_COMMENT_MAX],
         who["id"], now()))
    row = db.execute("SELECT * FROM training_rubric WHERE id=?", (rubric_id,)).fetchone()
    if previous and scored:
        payload = _rubric_payload(row)
        payload["rotated_from"] = previous["id"]
        payload["note"] = (payload["note"] +
                           " 已有评分，权重改动另存为新版本，旧分按旧口径保留。").strip()
        return payload
    return _rubric_payload(row)


def _rubric_payload(row) -> dict[str, Any]:
    dims = json.loads(row["dimensions_json"])
    return {"id": row["id"], "task_id": row["task_id"], "version": int(row["version"]),
            "dimensions": dims, "max_score": round(MAX_PER_DIMENSION * sum(d["weight"] for d in dims), 2),
            "note": row["note"], "created_at": row["created_at"],
            "rotated_from": row["rotated_from"] if "rotated_from" in row.keys() else None}


# --------------------------------------------------------------------------- 评分

def _scores(db, rubric_id: str, student_id: str) -> list[dict[str, Any]]:
    # 身份列必须一起 SELECT：行工厂按 rubric_id/student_id/dimension 校验解密上下文
    rows = db.execute(
        """SELECT rubric_id,student_id,dimension,score,comment,scored_by,scored_at
           FROM training_rubric_score WHERE rubric_id=? AND student_id=?""",
        (rubric_id, student_id)).fetchall()
    return [{"dimension": r["dimension"], "score": r["score"], "comment": r["comment"],
             "scored_by": r["scored_by"], "scored_at": r["scored_at"]} for r in rows]
    # comment 已由行工厂解密


def total(dimensions: list[dict[str, Any]], scores: list[dict[str, Any]]) -> dict[str, Any]:
    """按实际分母算总分：不适用（score 为 None）的维度既不计分也不进分母。"""
    by_key = {s["dimension"]: s["score"] for s in scores}
    counted, earned, denominator, missing = [], 0.0, 0.0, []
    for dim in dimensions:
        value = by_key.get(dim["key"])
        if value is None:
            missing.append(dim["key"])
            continue                      # 不适用：不按 0 分算，也不按满分算
        counted.append(dim["key"])
        earned += value * dim["weight"]
        denominator += MAX_PER_DIMENSION * dim["weight"]
    return {"raw": round(earned, 2), "max": round(denominator, 2),
            "percent": round(earned / denominator * 100, 1) if denominator else None,
            "counted": counted, "not_applicable": missing,
            "scale": f"0~{MAX_PER_DIMENSION} 分/维，按实际分母折算"}


def save_scores(store, who: dict[str, Any], task_id: str, student_id: str,
                entries: list[dict[str, Any]]) -> dict[str, Any]:
    if who["role"] != "teacher":
        raise RubricError("只有任课教师可以按量规评分。", 403)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current_actor(db, who, {"teacher"})
        task = task_by_id(db, task_id)
        if task is None:
            raise RubricError("案例任务不存在。", 404)
        assignment = db.execute("SELECT created_by,org_id FROM assignments WHERE id=?",
                                (task["assignment_id"],)).fetchone()
        if not assignment or assignment["created_by"] != who["id"] \
                or assignment["org_id"] != who["org_id"]:
            raise RubricError("案例任务不存在。", 404)
        member = db.execute(
            """SELECT 1 FROM training_class_members m
               JOIN training_assignment_settings s ON s.class_id=m.class_id
               WHERE s.assignment_id=? AND m.student_id=?""",
            (task["assignment_id"], student_id)).fetchone()
        if not member:
            raise RubricError("该学生不在本任务班级名册里。", 404)
        rubric = ensure_rubric(db, task_id, who)
        allowed = {d["key"] for d in rubric["dimensions"]}
        for entry in entries:
            key = str(entry.get("dimension", "")).strip()
            if key not in allowed:
                raise RubricError(f"本次任务不评价「{DIMENSION_LABELS.get(key, key or '(空)')}」。", 409)
            value = entry.get("score")
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                      or not 0 <= value <= MAX_PER_DIMENSION):
                raise RubricError(f"「{DIMENSION_LABELS[key]}」只能是 0/1/2 分或留空（不适用）。")
        codec = store._field_codec
        stamp = now()
        for entry in entries:
            key = str(entry["dimension"]).strip()
            value = entry.get("score")
            text = (entry.get("comment") or "").strip()
            if len(text) > _COMMENT_MAX:
                raise RubricError(f"维度意见最多 {_COMMENT_MAX} 字。")
            db.execute(
                """INSERT INTO training_rubric_score
                     (id,rubric_id,student_id,dimension,score,comment,scored_by,scored_at)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(rubric_id,student_id,dimension) DO UPDATE SET
                     score=excluded.score, comment=excluded.comment,
                     scored_by=excluded.scored_by, scored_at=excluded.scored_at""",
                (secrets.token_hex(12), rubric["id"], student_id, key, value,
                 codec.seal(text, "training_rubric_score", "comment", rubric["id"], student_id, key)
                 if text else None, who["id"], stamp))
        store._log(db, who, "score_rubric", "rubric", rubric["id"], f"student={student_id}")
        return result_for(db, task_id, student_id, who)


def result_for(db, task_id: str, student_id: str, who: dict[str, Any]) -> dict[str, Any]:
    rubric = ensure_rubric(db, task_id, who)
    scores = _scores(db, rubric["id"], student_id)
    summary = total(rubric["dimensions"], scores)
    by_key = {s["dimension"]: s for s in scores}
    return {"rubric": rubric,
            "scores": [{"dimension": d["key"], "label": d["label"], "weight": d["weight"],
                        "score": (by_key.get(d["key"]) or {}).get("score"),
                        "comment": (by_key.get(d["key"]) or {}).get("comment") or "",
                        "scored_at": (by_key.get(d["key"]) or {}).get("scored_at")}
                       for d in rubric["dimensions"]],
            "total": summary}


def student_result(store, who: dict[str, Any], assignment_id: str) -> dict[str, Any]:
    """学生看自己的量规结果：按任务答案开放策略，未开放时只说"教师尚未反馈"。"""
    from webapp import training_submissions
    from webapp.teaching_case_tasks import _visible_assignment, task_row
    with store.connect() as db:
        db.execute("BEGIN")
        current_actor(db, who, {"student"})
        _visible_assignment(db, who, assignment_id)
        row = task_row(db, assignment_id)
        if row is None or row["archived_at"]:
            raise RubricError("这道作业不是实操案例任务。", 404)
        visible, note = training_submissions.result_visible(db, assignment_id, who["id"])
        if not visible:
            return {"released": False, "note": note, "rubric": None, "scores": [], "total": None}
        value = result_for(db, row["id"], who["id"], who)
    value["released"] = True
    value["note"] = "量规分与客观题成绩分开展示，不合并计算。"
    return value


# --------------------------------------------------------------------------- 接口

class RubricBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimensions: list[dict[str, Any]] | None = Field(default=None, max_length=4)
    note: str | None = Field(default=None, max_length=_COMMENT_MAX)


class ScoreItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: str = Field(min_length=1, max_length=32)
    score: int | None = Field(default=None, ge=0, le=MAX_PER_DIMENSION)
    comment: str | None = Field(default=None, max_length=_COMMENT_MAX)


class ScoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_id: str = Field(min_length=1, max_length=64)
    scores: list[ScoreItem] = Field(min_length=1, max_length=4)


def register(app, store_provider: Callable[[], Any], user_fn: Callable[..., dict],
             cookie_name: str) -> None:
    def response(value: Any) -> JSONResponse:
        return JSONResponse(value, headers={"Cache-Control": "private, no-store"})

    def guarded(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except (RubricError, CaseTaskError) as exc:
                raise HTTPException(exc.status, str(exc)) from None
        return wrapper

    def actor(session: str | None) -> dict[str, Any]:
        user = user_fn(session)
        if user["role"] not in {"teacher", "student"}:
            raise HTTPException(403, "当前账号没有课程任务权限。")
        return user

    def owned_task(db, who: dict[str, Any], task_id: str):
        task = task_by_id(db, task_id)
        if task is None:
            raise RubricError("案例任务不存在。", 404)
        assignment = db.execute("SELECT created_by,org_id FROM assignments WHERE id=?",
                                (task["assignment_id"],)).fetchone()
        if not assignment or assignment["created_by"] != who["id"] \
                or assignment["org_id"] != who["org_id"]:
            raise RubricError("案例任务不存在。", 404)
        return task

    @app.get("/api/training/teaching/case-tasks/{task_id}/rubric")
    @guarded
    def read_rubric(task_id: str, session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有任课教师可以查看量规配置。")
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {"teacher"})
            owned_task(db, user, task_id)
            return response(ensure_rubric(db, task_id, user))

    @app.put("/api/training/teaching/case-tasks/{task_id}/rubric")
    @guarded
    def write_rubric(task_id: str, body: RubricBody,
                     session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有任课教师可以配置量规权重。")
        repo = store_provider()
        with repo.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {"teacher"})
            owned_task(db, user, task_id)
            value = save_rubric(db, task_id, user, body.dimensions, body.note or "")
            repo._log(db, user, "update_rubric", "rubric", value["id"], f"version={value['version']}")
        return response(value)

    @app.put("/api/training/teaching/case-tasks/{task_id}/rubric-scores")
    @guarded
    def write_scores(task_id: str, body: ScoreBody,
                     session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        entries = [{"dimension": item.dimension, "score": item.score, "comment": item.comment}
                   for item in body.scores]
        return response(save_scores(store_provider(), user, task_id, body.student_id, entries))

    @app.get("/api/training/teaching/case-tasks/{task_id}/rubric-scores")
    @guarded
    def read_scores(task_id: str, student_id: str | None = None,
                    session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "teacher":
            raise HTTPException(403, "只有任课教师可以查看量规评分。")
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {"teacher"})
            owned_task(db, user, task_id)
            if student_id:
                return response(result_for(db, task_id, student_id, user))
            members = [r["student_id"] for r in db.execute(
                """SELECT m.student_id FROM training_class_members m
                   JOIN training_assignment_settings s ON s.class_id=m.class_id
                   JOIN teaching_case_task t ON t.assignment_id=s.assignment_id
                   WHERE t.id=? ORDER BY m.student_id""", (task_id,)).fetchall()]
            return response({"items": [{"student_id": s, **{k: v for k, v in
                                                            result_for(db, task_id, s, user).items()
                                                            if k != "rubric"}}
                                       for s in members]})

    @app.get("/api/assignments/{assignment_id}/rubric")
    @guarded
    def my_rubric(assignment_id: str,
                  session: str | None = Cookie(default=None, alias=cookie_name)) -> Any:
        user = actor(session)
        if user["role"] != "student":
            raise HTTPException(403, "只有学生可以看本人的量规结果。")
        return response(student_result(store_provider(), user, assignment_id))
