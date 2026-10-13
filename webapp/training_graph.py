"""学习图谱关系（FR-K03/K04 待完善项）：知识点间的前置、概念关联与易混淆关系。

- 三类关系均要求依据（basis_ref，考纲章节/教材说明），服务层强制校验——
  没有依据不建关系（验收：不凭印象标注）；
- prerequisite 构成依赖图（from 依赖 to：学 from 前先学 to），建立时做环检测；
- concept/confusable 语义对称，反向重复视为重复（409）；
- 学生侧仅展示关系与对侧知识点，不虚构掌握结论；教师侧可维护与删除。
"""
from __future__ import annotations

import sqlite3
import uuid
from functools import wraps
from typing import Any, Callable

from fastapi import Cookie, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp.storage import _now

# 本模块被 webapp.training_portal 顶层导入，不得在模块级导入 training_portal。
RULE_TYPES = ("prerequisite", "concept", "confusable")
TYPE_LABELS = {"prerequisite": "前置知识", "concept": "概念关联", "confusable": "易混淆"}
SYMMETRIC_TYPES = ("concept", "confusable")


def _portal_error(message: str, status: int = 422) -> Exception:
    from webapp.training_portal import TrainingPortalError
    return TrainingPortalError(message, status)


def _load_kp(db, kp_id: str):
    return db.execute(
        "SELECT id, certificate_id, code, name, active FROM knowledge_point WHERE id=?",
        (kp_id,)).fetchone()


def _reachable(db, start: str, target: str) -> bool:
    """沿 prerequisite 依赖边（from→to 表示 to 是 from 的前置）判断 target 是否可达。"""
    seen, stack = {start}, [start]
    while stack:
        current = stack.pop()
        if current == target:
            return True
        for row in db.execute(
                "SELECT to_kp_id FROM knowledge_point_relation"
                " WHERE from_kp_id=? AND relation_type='prerequisite'", (current,)):
            if row["to_kp_id"] not in seen:
                seen.add(row["to_kp_id"])
                stack.append(row["to_kp_id"])
    return False


def create_relation(store, staff: dict, from_kp_id: str, to_kp_id: str, relation_type: str,
                    basis_ref: str, basis_version: str = "") -> dict[str, Any]:
    relation_type = str(relation_type).strip()
    if relation_type not in RULE_TYPES:
        raise _portal_error("关系类型必须是 prerequisite / concept / confusable。")
    if from_kp_id == to_kp_id:
        raise _portal_error("知识点不能与自身建立关系。")
    basis_ref, basis_version = str(basis_ref).strip(), str(basis_version).strip()
    if not basis_ref:
        raise _portal_error("建立关系必须提供依据（考纲章节或教材说明）。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        from_kp, to_kp = _load_kp(db, from_kp_id), _load_kp(db, to_kp_id)
        if not from_kp or not to_kp:
            raise _portal_error("知识点不存在。", 404)
        if from_kp["certificate_id"] != to_kp["certificate_id"]:
            raise _portal_error("暂不支持跨证书建立知识点关系。")
        exists = db.execute(
            "SELECT 1 FROM knowledge_point_relation WHERE from_kp_id=? AND to_kp_id=? AND relation_type=?",
            (from_kp_id, to_kp_id, relation_type)).fetchone()
        reverse = db.execute(
            "SELECT 1 FROM knowledge_point_relation WHERE from_kp_id=? AND to_kp_id=? AND relation_type=?",
            (to_kp_id, from_kp_id, relation_type)).fetchone()
        if relation_type in SYMMETRIC_TYPES and (exists or reverse):
            raise _portal_error(f"{TYPE_LABELS[relation_type]}关系已存在（对称方向视为重复）。", 409)
        if exists:
            raise _portal_error("该关系已存在。", 409)
        if relation_type == "prerequisite" and _reachable(db, to_kp_id, from_kp_id):
            raise _portal_error("建立该前置关系会形成依赖环（A 依赖 B 依赖 A），已拒绝。", 409)
        relation_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO knowledge_point_relation (id, from_kp_id, to_kp_id, relation_type,
               basis_ref, basis_version, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (relation_id, from_kp_id, to_kp_id, relation_type, basis_ref, basis_version,
             staff["id"], _now()))
        row = db.execute("SELECT * FROM knowledge_point_relation WHERE id=?", (relation_id,)).fetchone()
        return _payload(db, row)


def delete_relation(store, staff: dict, relation_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM knowledge_point_relation WHERE id=?", (relation_id,)).fetchone()
        if not row:
            raise _portal_error("关系不存在。", 404)
        db.execute("DELETE FROM knowledge_point_relation WHERE id=?", (relation_id,))
        return _payload(db, row)


def _payload(db, row) -> dict[str, Any]:
    def kp_brief(kp_id: str):
        kp = _load_kp(db, kp_id)
        return {"id": kp_id, "code": kp["code"], "name": kp["name"]} if kp else None
    return dict(row) | {
        "relation_type_label": TYPE_LABELS.get(row["relation_type"], row["relation_type"]),
        "from_kp": kp_brief(row["from_kp_id"]),
        "to_kp": kp_brief(row["to_kp_id"]),
    }


def list_relations(store, kp_id: str) -> dict[str, Any]:
    with store.connect() as db:
        if not _load_kp(db, kp_id):
            raise _portal_error("知识点不存在。", 404)
        outgoing = db.execute(
            "SELECT * FROM knowledge_point_relation WHERE from_kp_id=?"
            " ORDER BY relation_type, created_at", (kp_id,)).fetchall()
        incoming = db.execute(
            "SELECT * FROM knowledge_point_relation WHERE to_kp_id=?"
            " ORDER BY relation_type, created_at", (kp_id,)).fetchall()
        return {"relations": [_payload(db, r) for r in outgoing],
                "incoming": [_payload(db, r) for r in incoming]}


def relations_payload(db, kp_id: str) -> dict[str, list[dict[str, Any]]]:
    """学生视图：该知识点的前置/概念/易混淆对侧知识点（对称类型双向聚合）。"""
    def brief(kp_id: str):
        kp = _load_kp(db, kp_id)
        return {"id": kp_id, "code": kp["code"], "name": kp["name"]} if kp else None

    out: dict[str, list[dict[str, Any]]] = {t: [] for t in RULE_TYPES}
    seen: set[tuple[str, str]] = set()
    rows = db.execute(
        "SELECT * FROM knowledge_point_relation WHERE from_kp_id=? OR to_kp_id=?"
        " ORDER BY created_at", (kp_id, kp_id)).fetchall()
    for r in rows:
        other_id = r["to_kp_id"] if r["from_kp_id"] == kp_id else r["from_kp_id"]
        key = (r["relation_type"], other_id)
        if other_id == kp_id or key in seen:
            continue
        seen.add(key)
        target = brief(other_id)
        if target:
            out[r["relation_type"]].append(target | {"relation_id": r["id"]})
    return out


# ---------------------------------------------------------------------------
# 教师端路由（图谱关系维护）
# ---------------------------------------------------------------------------

class RelationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_kp_id: str = Field(min_length=1)
    relation_type: str
    basis_ref: str = ""
    basis_version: str = ""


def register(app, store_provider: Callable[[], Any]) -> None:
    # 延迟导入：本模块被 training_portal 顶层引用，不能反向在模块级导入它。
    from webapp.training_portal import STAFF_COOKIE, TrainingPortalError, staff_for_token

    def response(body: Any) -> JSONResponse:
        return JSONResponse(body, headers={"Cache-Control": "private, no-store"})

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

    @app.get("/api/training/staff/knowledge-points/{kp_id}/relations")
    @guarded
    def staff_relation_list(kp_id: str,
                            session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response(list_relations(store_provider(), kp_id))

    @app.post("/api/training/staff/knowledge-points/{kp_id}/relations")
    @guarded
    def staff_relation_create(kp_id: str, body: RelationCreate,
                              session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"relation": create_relation(
            store_provider(), who, kp_id, body.to_kp_id, body.relation_type,
            body.basis_ref, body.basis_version)})

    @app.delete("/api/training/staff/relations/{relation_id}")
    @guarded
    def staff_relation_delete(relation_id: str,
                              session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"relation": delete_relation(store_provider(), who, relation_id)})
