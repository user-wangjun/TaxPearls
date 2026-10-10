"""考证刷题内容版本治理（FR-K08）。

首发仅接入规则仿真题（source_type='simulated'）；数据模型预留 real（真题）、
recall（回忆题）、mock（模拟题）类型区分，但服务层一律拒绝创建——真题等外部
题源须先获得授权并建立审核流程（验收：生成题不冒充真题）。

治理规则：
- 同一证书同一时间仅一个 active 内容版本；发布新版本时旧版本自动转 retired，
  行保留不删除，供历史作答回溯（验收：旧作答读取当时内容版本）。
- 学生开始练习时绑定当前 active 版本；证书尚无版本时自动建立"仿真基线"版本
  （题源=规则仿真题，年度=当前年份），不改变出题与判分逻辑。
- 判分始终以出题时的材料指纹（digest）为准；内容版本变化不影响历史判分复现，
  仅在作答视图上给出适用性提示（版本轮换、年度变化）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from fastapi import Cookie, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp.storage import _now

# 注意：本模块被 webapp.training_portal 顶层导入，故此处不得在模块级导入
# training_portal（会循环）；需要其符号时一律在函数内延迟导入。

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"

SOURCE_TYPES = ("simulated", "real", "recall", "mock")
SOURCE_LABELS = {"simulated": "仿真题", "real": "真题", "recall": "回忆题", "mock": "模拟题"}


def _portal_error(message: str, status: int = 422) -> Exception:
    from webapp.training_portal import TrainingPortalError
    return TrainingPortalError(message, status)


def _today_cst():
    from webapp.training_portal import today_cst
    return today_cst()


def load_rules() -> list[Any]:
    from src import engine
    return engine.load_rules(RULES_DIR)


def _pack_digest(rule_ids: list[str], rules: list[Any]) -> str:
    """对版本覆盖的规则内容计算指纹：规则内容或覆盖范围变化都会改变指纹。"""
    by_id = {r.id: r for r in rules}
    content = [asdict(by_id[rid]) if rid in by_id else {"id": rid, "missing": True}
               for rid in sorted(set(rule_ids))]
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def linked_rule_ids(db, cert_id: str) -> list[str]:
    """证书下启用知识点关联的全部规则（与整证随机出题的候选范围一致）。"""
    rows = db.execute(
        """SELECT DISTINCT l.target_id FROM knowledge_point_link l
           JOIN knowledge_point k ON k.id = l.knowledge_point_id
           WHERE l.target_type='rule' AND k.certificate_id=? AND k.active=1""",
        (cert_id,)).fetchall()
    return sorted({r["target_id"] for r in rows})


def resolve_active_version(db, cert_id: str) -> Any | None:
    return db.execute(
        """SELECT * FROM training_content_version WHERE certificate_id=? AND status='active'
           ORDER BY created_at DESC, id LIMIT 1""", (cert_id,)).fetchone()


def _brief_row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row["id"], "label": row["label"], "year": row["year"],
            "source_type": row["source_type"], "status": row["status"]}


def version_brief(db, version_id: str | None) -> dict[str, Any] | None:
    """作答所绑定的内容版本摘要；NULL（含未版本化的历史作答）返回 None。"""
    if not version_id:
        return None
    row = db.execute(
        "SELECT id, label, year, source_type, status FROM training_content_version WHERE id=?",
        (version_id,)).fetchone()
    return _brief_row(row)


def applicability(db, cert_id: str, version_id: str | None) -> dict[str, Any]:
    """作答与当前内容的关系提示；只陈述事实与口径，不改写判分与解析。"""
    attempt_version = version_brief(db, version_id)
    current = resolve_active_version(db, cert_id)
    current_brief = _brief_row(current)
    notice = None
    if attempt_version is None:
        notice = ("本题作答早于内容版本管理上线：题目与判分以作答时的材料指纹为准，"
                  "不受后续内容变更影响。")
    elif attempt_version["status"] == "retired":
        if current_brief and current_brief["year"] != attempt_version["year"]:
            notice = (f"本题出自内容版本「{attempt_version['label']}」"
                      f"（{attempt_version['year']} 年度），该版本已轮换；"
                      f"当前为「{current_brief['label']}」（{current_brief['year']} 年度）。"
                      "本题仍按作答当时版本的题目与解析展示，解析适用性以作答当时考纲为准。")
        else:
            notice = (f"本题出自内容版本「{attempt_version['label']}」，该版本已轮换退役；"
                      "本题仍按作答当时版本的题目与解析展示。")
    elif current_brief is None:
        notice = "当前证书暂无启用的内容版本。"
    return {"attempt_version": attempt_version, "current_version": current_brief, "notice": notice}


def _baseline_label(db, cert_id: str, year: int) -> str:
    base = f"仿真基线 {year}"
    n = db.execute(
        "SELECT count(*) FROM training_content_version WHERE certificate_id=? AND label LIKE ?",
        (cert_id, base + "%")).fetchone()[0]
    return base if n == 0 else f"{base} #{n + 1}"


def ensure_active_version(db, cert_id: str, rules: list[Any]) -> Any:
    """返回当前 active 版本；没有则建立仿真基线版本（须在 BEGIN IMMEDIATE 事务内调用）。"""
    active = resolve_active_version(db, cert_id)
    if active:
        return active
    rule_ids = linked_rule_ids(db, cert_id)
    year = _today_cst().year
    now = _now()
    version_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO training_content_version (id, certificate_id, label, source_type, year,
           rules_digest, rule_ids_json, status, source_ref, note, created_by, created_at, updated_at)
           VALUES (?,?,?,'simulated',?,?,?,'active','','',NULL,?,?)""",
        (version_id, cert_id, _baseline_label(db, cert_id, year), year,
         _pack_digest(rule_ids, rules), json.dumps(rule_ids, ensure_ascii=False), now, now))
    return db.execute("SELECT * FROM training_content_version WHERE id=?", (version_id,)).fetchone()


def _payload(db, row) -> dict[str, Any]:
    return dict(row) | {
        "rule_ids": json.loads(row["rule_ids_json"] or "[]"),
        "source_type_label": SOURCE_LABELS.get(row["source_type"], row["source_type"]),
    }


def list_versions(store, cert_id: str) -> list[dict[str, Any]]:
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise _portal_error("证书不存在。", 404)
        rows = db.execute(
            "SELECT * FROM training_content_version WHERE certificate_id=?"
            " ORDER BY created_at DESC", (cert_id,)).fetchall()
        return [_payload(db, r) for r in rows]


def create_version(store, staff: dict, cert_id: str, *, label: str, source_type: str = "simulated",
                   year: int | None = None, source_ref: str = "", note: str = "",
                   rule_ids: list[str] | None = None) -> dict[str, Any]:
    label = str(label).strip()
    if not label:
        raise _portal_error("版本标签不能为空。")
    if source_type not in SOURCE_TYPES:
        raise _portal_error("题源类型必须是 simulated / real / recall / mock。")
    if source_type != "simulated":
        raise _portal_error(
            f"{SOURCE_LABELS[source_type]}接入暂缓：须先获得授权并建立审核流程，"
            "当前仅开放规则仿真题，生成题不冒充真题。")
    if year is None:
        year = _today_cst().year
    try:
        year = int(year)
    except (TypeError, ValueError):
        raise _portal_error("适用年度必须是整数。") from None
    source_ref, note = str(source_ref).strip(), str(note).strip()
    if not source_ref:
        raise _portal_error("发布内容版本必须提供依据说明（如考纲版本或整理口径）。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise _portal_error("证书不存在。", 404)
        if db.execute("SELECT 1 FROM training_content_version WHERE certificate_id=? AND label=?",
                      (cert_id, label)).fetchone():
            raise _portal_error(f"版本标签 {label} 在该证书下已存在。", 409)
        if rule_ids is None:
            final_ids = linked_rule_ids(db, cert_id)
        else:
            final_ids = sorted({str(r).strip() for r in rule_ids if str(r).strip()})
        known = {r.id for r in load_rules()}
        unknown = [rid for rid in final_ids if rid not in known]
        if unknown:
            raise _portal_error(f"规则不存在或未启用：{', '.join(unknown)}。")
        now = _now()
        # 版本轮换：新版本启用即退役同证书其它 active 版本（行保留，供旧作答回溯）。
        db.execute(
            "UPDATE training_content_version SET status='retired', updated_at=?"
            " WHERE certificate_id=? AND status='active'", (now, cert_id))
        version_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO training_content_version (id, certificate_id, label, source_type, year,
               rules_digest, rule_ids_json, status, source_ref, note, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,'active',?,?,?,?,?)""",
            (version_id, cert_id, label, source_type, year, _pack_digest(final_ids, load_rules()),
             json.dumps(final_ids, ensure_ascii=False), source_ref, note, staff["id"], now, now))
        row = db.execute("SELECT * FROM training_content_version WHERE id=?", (version_id,)).fetchone()
        return _payload(db, row)


def retire_version(store, staff: dict, version_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM training_content_version WHERE id=?", (version_id,)).fetchone()
        if not row:
            raise _portal_error("内容版本不存在。", 404)
        if row["status"] == "active":
            db.execute("UPDATE training_content_version SET status='retired', updated_at=? WHERE id=?",
                       (_now(), version_id))
        row = db.execute("SELECT * FROM training_content_version WHERE id=?", (version_id,)).fetchone()
        return _payload(db, row)


# ---------------------------------------------------------------------------
# 教师端路由（题源治理）
# ---------------------------------------------------------------------------

class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1)
    source_type: str = "simulated"
    year: int | None = None
    source_ref: str = ""
    note: str = ""
    rule_ids: list[str] | None = None


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

    @app.get("/api/training/staff/certificates/{cert_id}/content-versions")
    @guarded
    def staff_version_list(cert_id: str,
                           session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"versions": list_versions(store_provider(), cert_id)})

    @app.post("/api/training/staff/certificates/{cert_id}/content-versions")
    @guarded
    def staff_version_create(cert_id: str, body: VersionCreate,
                             session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"content_version": create_version(
            store_provider(), who, cert_id, label=body.label, source_type=body.source_type,
            year=body.year, source_ref=body.source_ref, note=body.note, rule_ids=body.rule_ids)})

    @app.post("/api/training/staff/content-versions/{version_id}/retire")
    @guarded
    def staff_version_retire(version_id: str,
                             session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"content_version": retire_version(store_provider(), who, version_id)})
