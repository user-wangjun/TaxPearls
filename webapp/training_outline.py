"""考纲年度升版（第四条就绪：2027 考纲预计 2026-12 下旬发布，发布后逐条核对升版）。

依据内容三项决策之二：唯一权威依据为全国会计考办年度考试大纲原文，升版 =
逐条人工核对，不由系统自动改写。核对在现行知识点树上进行（知识点 id 不变，
学生作答与覆盖统计天然连续）：

- 发起：现行树全部启用知识点标记 review_status='pending'，登记升版单
  （from_version → to_version，同一证书同时只允许一张「核对中」升版单）。
- 逐条核对（复用既有知识点编辑接口）：
    confirmed=与新版一致沿用 / updated=已按新纲修改 / removed=新版删除 /
    pending=待核对；核对期新增的知识点记 review_status='added' 且对学生
    隐藏（active=0），激活时才上线。
- 激活：全部条目核对完成（无 pending）后切换——confirmed/updated/added
  行 outline_version 统一改为目标版本并对学生可见，removed 行停用留痕；
  升版单转为 activated，历史作答按原知识点 id 与当时内容版本回溯。

边界：本模块只管理核对流程与版本切换，不生成、不猜测大纲内容；新旧版本
差异须由教师对照官方大纲原文逐条录入。
"""
from __future__ import annotations

import sqlite3
import uuid
from functools import wraps
from typing import Any, Callable

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from webapp.storage import _now

REVIEW_STATUSES = ("pending", "confirmed", "updated", "removed", "added")
ACTIVATED_STATUSES = ("confirmed", "updated", "added")


def _portal_error(message: str, status: int = 422) -> Exception:
    from webapp.training_portal import TrainingPortalError
    return TrainingPortalError(message, status)


def working_upgrade(db, cert_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM training_outline_upgrade WHERE certificate_id=? AND status='working'"
        " ORDER BY started_at DESC LIMIT 1", (cert_id,)).fetchone()


def review_counts(db, cert_id: str) -> dict[str, int]:
    """核对进度计数：pending/confirmed/updated/added/removed 各状态行数。"""
    rows = db.execute(
        """SELECT review_status, count(*) n FROM knowledge_point
           WHERE certificate_id=? AND review_status!='' GROUP BY review_status""",
        (cert_id,)).fetchall()
    counts = {k: 0 for k in REVIEW_STATUSES}
    for r in rows:
        counts[r["review_status"]] = r["n"]
    return counts


def dominant_version(db, cert_id: str) -> str:
    """证书下启用知识点中出现最多的考纲版本（升版发起时的 from_version）。"""
    row = db.execute(
        """SELECT outline_version, count(*) n FROM knowledge_point
           WHERE certificate_id=? AND active=1 AND outline_version!=''
           GROUP BY outline_version ORDER BY n DESC, outline_version LIMIT 1""",
        (cert_id,)).fetchone()
    return str(row["outline_version"]) if row else ""


def start_upgrade(store, staff: dict, cert_id: str, *, to_version: str, note: str = "") -> dict[str, Any]:
    to_version = str(to_version).strip()
    if not to_version:
        raise _portal_error("目标考纲版本不能为空（如 2027）。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise _portal_error("证书不存在。", 404)
        if working_upgrade(db, cert_id):
            raise _portal_error("该证书已有一张「核对中」的升版单，请先完成或再核对后再发起新升版。", 409)
        total = db.execute(
            "SELECT count(*) n FROM knowledge_point WHERE certificate_id=? AND active=1",
            (cert_id,)).fetchone()["n"]
        if total == 0:
            raise _portal_error("该证书还没有启用的知识点，无需升版。")
        from_version = dominant_version(db, cert_id)
        if not from_version:
            raise _portal_error("现行知识点未登记考纲版本，请先补录 outline_version 再发起升版。")
        if from_version == to_version:
            raise _portal_error(f"目标版本与现行版本相同（{to_version}），请核对后填写新年度。")
        now = _now()
        upgrade_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO training_outline_upgrade (id, certificate_id, from_version, to_version,
               status, started_by, started_at, activated_by, activated_at, note)
               VALUES (?,?,?,?, 'working',?,?,NULL,NULL,?)""",
            (upgrade_id, cert_id, from_version, to_version, staff["id"], now, str(note).strip()[:500]))
        db.execute(
            "UPDATE knowledge_point SET review_status='pending', updated_at=?"
            " WHERE certificate_id=? AND active=1", (now, cert_id))
        row = db.execute("SELECT * FROM training_outline_upgrade WHERE id=?", (upgrade_id,)).fetchone()
        return _upgrade_payload(db, row)


def _upgrade_payload(db, row: sqlite3.Row) -> dict[str, Any]:
    return dict(row) | {"review_counts": review_counts(db, row["certificate_id"])}


def upgrade_status(store, cert_id: str) -> dict[str, Any]:
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise _portal_error("证书不存在。", 404)
        current = working_upgrade(db, cert_id)
        history = db.execute(
            "SELECT * FROM training_outline_upgrade WHERE certificate_id=? AND status='activated'"
            " ORDER BY activated_at DESC LIMIT 5", (cert_id,)).fetchall()
        return {
            "current": _upgrade_payload(db, current) if current else None,
            "history": [dict(h) for h in history],
            "review_counts": review_counts(db, cert_id),
            "dominant_version": dominant_version(db, cert_id),
        }


def activate_upgrade(store, staff: dict, cert_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        upgrade = working_upgrade(db, cert_id)
        if not upgrade:
            raise _portal_error("没有进行中的考纲升版，请先发起升版核对。", 404)
        counts = review_counts(db, cert_id)
        if counts["pending"]:
            raise _portal_error(
                f"还有 {counts['pending']} 条知识点待核对；逐条核对完成（确认/修改/删除）后才能激活切换。", 409)
        if not any(counts[k] for k in ACTIVATED_STATUSES):
            raise _portal_error("没有可激活的知识点（全部被删除）——请核对升版范围。")
        now = _now()
        # removed 行停用留痕（active=0，行与升版单都保留），同时清空核对状态：
        # review_status 是本轮核对的工作字段，残留会让下一轮 review_counts 混入上轮历史。
        db.execute(
            "UPDATE knowledge_point SET active=0, review_status='', updated_at=?"
            " WHERE certificate_id=? AND review_status='removed'", (now, cert_id))
        db.execute(
            """UPDATE knowledge_point SET active=1, outline_version=?, review_status='', updated_at=?
               WHERE certificate_id=? AND review_status IN ('confirmed','updated','added')""",
            (upgrade["to_version"], now, cert_id))
        db.execute(
            """UPDATE training_outline_upgrade SET status='activated', activated_by=?, activated_at=?
               WHERE id=?""", (staff["id"], now, upgrade["id"]))
        row = db.execute("SELECT * FROM training_outline_upgrade WHERE id=?", (upgrade["id"],)).fetchone()
        after = review_counts(db, cert_id)
        activated_n = db.execute(
            """SELECT count(*) n FROM knowledge_point
               WHERE certificate_id=? AND outline_version=? AND active=1""",
            (cert_id, upgrade["to_version"])).fetchone()["n"]
        return dict(row) | {"activated": activated_n, "removed": counts["removed"],
                            "review_counts": after}


def assert_consistent_review_status(value: str) -> str:
    value = str(value).strip()
    if value not in REVIEW_STATUSES:
        raise _portal_error(
            f"核对状态必须是 {' / '.join(REVIEW_STATUSES)} 之一。")
    return value


# ---------------------------------------------------------------------------
# 教师端路由
# ---------------------------------------------------------------------------

class OutlineUpgradeStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_version: str = Field(min_length=1)
    note: str = ""


def register(app, store_provider: Callable[[], Any]) -> None:
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

    @app.get("/api/training/staff/certificates/{cert_id}/outline-upgrade")
    @guarded
    def outline_status(cert_id: str,
                       session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response(upgrade_status(store_provider(), cert_id))

    @app.post("/api/training/staff/certificates/{cert_id}/outline-upgrade/start")
    @guarded
    def outline_start(cert_id: str, body: OutlineUpgradeStart,
                      session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"upgrade": start_upgrade(store_provider(), who, cert_id,
                                                  to_version=body.to_version, note=body.note)})

    @app.post("/api/training/staff/certificates/{cert_id}/outline-upgrade/activate")
    @guarded
    def outline_activate(cert_id: str,
                         session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"upgrade": activate_upgrade(store_provider(), who, cert_id)})
