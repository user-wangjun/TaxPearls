"""真题授权登记与题库审核（第四条就绪：真题授权 2026-12 后接洽，软件侧先行）。

内容三项决策之三（授权先行、不自行采集）的软件侧落地：
- 授权登记：与命题管理方/授权出版方取得书面授权后，由管理员登记授权方、
  授权文号、覆盖题源、有效期与范围说明；撤销只改状态不删行，可审计。
- 服务层校验：发布真题/回忆/模拟题内容版本时，必须存在覆盖该题源且当日
  在有效期内的生效授权；授权文号自动写入版本 source_ref（决策三要求留痕）。
- 题库审核：导入的题库题（真题/回忆题/模拟题）经「草稿→待审→审核上线」
  流程后才对学生可见；答案与解析判分后才开放（沿用自主练习既有口径）。
  审核通过题目可下架回草稿；已上线题目的历史作答不受下架影响。

边界：本模块只做登记、导入与审核流转，不采集、不爬取、不代理任何授权谈判；
批量导入仅接受人工核对过的结构化文本（JSON），不解析 PDF/Word。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from functools import wraps
from typing import Any, Callable

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from webapp.storage import _now
from webapp.training_content import SOURCE_LABELS

BANK_STATUSES = ("draft", "pending", "approved", "rejected")
_BANK_OPTION_KEY_MAX = 8          # 选项键上限（A-H），与初级会计客观题形态一致
_BANK_TEXT_MAX = 20_000
_BANK_BATCH_MAX = 500
_FEEDBACK_REF_TYPES = ("none", "kp", "attempt", "question")


def _portal_error(message: str, status: int = 422) -> Exception:
    from webapp.training_portal import TrainingPortalError
    return TrainingPortalError(message, status)


def _today_cst():
    from webapp.training_portal import today_cst
    return today_cst()


def _parse_iso_date(value: str) -> str:
    from webapp.training_portal import _parse_date
    return _parse_date(value)


def valid_authorization(db, cert_id: str, source_type: str, *, on: str | None = None) -> sqlite3.Row | None:
    """覆盖 source_type 且当日生效的授权记录；没有则返回 None。

    有效期按自然日口径：valid_from <= 当日 <= valid_until（valid_until 为空 = 长期）。"""
    today = (on or _today_cst().isoformat())
    for row in db.execute(
            "SELECT * FROM training_source_authorization WHERE certificate_id=? AND status='active'"
            " ORDER BY created_at DESC", (cert_id,)).fetchall():
        types = {t.strip() for t in str(row["question_types"]).split(",") if t.strip()}
        if source_type not in types:
            continue
        if str(row["valid_from"]) > today:
            continue
        if row["valid_until"] and str(row["valid_until"]) < today:
            continue
        return row
    return None


def require_authorization(db, cert_id: str, source_type: str) -> sqlite3.Row:
    auth = valid_authorization(db, cert_id, source_type)
    if auth is None:
        raise _portal_error(
            f"{SOURCE_LABELS.get(source_type, source_type)}须先登记生效的书面授权"
            "（授权方、文号、有效期）才能发布或导入；当前未找到覆盖该题源的有效授权。")
    return auth


# ---------------------------------------------------------------------------
# 服务层：授权登记
# ---------------------------------------------------------------------------

def _covers_today(row: sqlite3.Row, today: str) -> bool:
    """该条授权当日是否生效（状态生效 + 生效日已到 + 未过失效日）。"""
    if row["status"] != "active":
        return False
    if str(row["valid_from"]) > today:
        return False
    return not (row["valid_until"] and str(row["valid_until"]) < today)


def list_authorizations(store, cert_id: str) -> list[dict[str, Any]]:
    today = _today_cst().isoformat()
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise _portal_error("证书不存在。", 404)
        rows = db.execute(
            "SELECT * FROM training_source_authorization WHERE certificate_id=?"
            " ORDER BY created_at DESC", (cert_id,)).fetchall()
        return [dict(r) | {"question_type_list": [t.strip() for t in str(r["question_types"]).split(",") if t.strip()],
                           "covering_today": _covers_today(r, today)}
                for r in rows]


def create_authorization(store, staff: dict, cert_id: str, *, licensor: str, auth_no: str,
                         question_types: list[str], valid_from: str, valid_until: str | None,
                         scope_note: str = "") -> dict[str, Any]:
    licensor, auth_no = licensor.strip(), auth_no.strip()
    scope_note = str(scope_note).strip()
    if not licensor or not auth_no:
        raise _portal_error("授权方与授权文号不能为空。")
    allowed = {"real", "recall", "mock"}
    types = sorted({str(t).strip() for t in question_types if str(t).strip()})
    if not types:
        raise _portal_error("覆盖题源不能为空（real / recall / mock）。")
    unknown = [t for t in types if t not in allowed]
    if unknown:
        raise _portal_error(f"题源类型不支持：{', '.join(unknown)}。")
    if valid_until == "":
        valid_until = None
    from_date = _parse_iso_date(valid_from)
    until_date = _parse_iso_date(valid_until) if valid_until else None
    if until_date and until_date < from_date:
        raise _portal_error("失效日不能早于生效日。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise _portal_error("证书不存在。", 404)
        if db.execute("SELECT 1 FROM training_source_authorization WHERE certificate_id=? AND auth_no=?",
                      (cert_id, auth_no)).fetchone():
            raise _portal_error(f"授权文号 {auth_no} 在该证书下已登记。", 409)
        auth_id = str(uuid.uuid4())
        now = _now()
        db.execute(
            """INSERT INTO training_source_authorization (id, certificate_id, licensor, auth_no,
               question_types, valid_from, valid_until, scope_note, status, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,'active',?,?,?)""",
            (auth_id, cert_id, licensor, auth_no, ",".join(types), from_date, until_date,
             scope_note, staff["id"], now, now))
        row = db.execute("SELECT * FROM training_source_authorization WHERE id=?", (auth_id,)).fetchone()
        return dict(row)


def revoke_authorization(store, staff: dict, auth_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM training_source_authorization WHERE id=?", (auth_id,)).fetchone()
        if not row:
            raise _portal_error("授权记录不存在。", 404)
        if row["status"] == "active":
            db.execute("UPDATE training_source_authorization SET status='revoked', updated_at=? WHERE id=?",
                       (_now(), auth_id))
        row = db.execute("SELECT * FROM training_source_authorization WHERE id=?", (auth_id,)).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# 服务层：题库题导入与审核
# ---------------------------------------------------------------------------

def _clean_options(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not (2 <= len(raw) <= _BANK_OPTION_KEY_MAX):
        raise _portal_error(f"选项必须是 2~{_BANK_OPTION_KEY_MAX} 个。")
    out: list[dict[str, str]] = []
    keys: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise _portal_error("选项格式必须是 {key, text}。")
        key = str(item.get("key", "")).strip().upper()
        text = str(item.get("text", "")).strip()
        if len(key) != 1 or not key.isalnum():
            raise _portal_error(f"选项键必须是单个字母或数字：{key!r}。")
        if key in keys:
            raise _portal_error(f"选项键重复：{key}。")
        if not text:
            raise _portal_error(f"选项 {key} 内容不能为空。")
        if len(text) > _BANK_TEXT_MAX:
            raise _portal_error(f"选项 {key} 内容过长。")
        keys.add(key)
        out.append({"key": key, "text": text})
    return out


def _normalize_question(store, cert_id: str, item: dict[str, Any], *, default_source_type: str | None = None) -> dict[str, Any]:
    source_type = str(item.get("source_type") or default_source_type or "").strip()
    if source_type not in ("real", "recall", "mock"):
        raise _portal_error("题源必须是 real / recall / mock（仿真题为规则生成，不入题库）。")
    try:
        year = int(item.get("year"))
    except (TypeError, ValueError):
        raise _portal_error("考试年度必须是整数。") from None
    stem = str(item.get("stem", "")).strip()
    if not stem:
        raise _portal_error("题干不能为空。")
    if len(stem) > _BANK_TEXT_MAX:
        raise _portal_error("题干过长。")
    options = _clean_options(item.get("options"))
    keys = {o["key"] for o in options}
    answer = sorted({str(k).strip().upper() for k in (item.get("answer_keys") or []) if str(k).strip()})
    if not answer:
        raise _portal_error("标准答案不能为空。")
    unknown = [k for k in answer if k not in keys]
    if unknown:
        raise _portal_error(f"答案包含未定义的选项键：{', '.join(unknown)}。")
    explanation = str(item.get("explanation", "")).strip()
    if not explanation:
        raise _portal_error("解析不能为空（解析判分后开放，缺失会导致学生无可看依据）。")
    if len(explanation) > _BANK_TEXT_MAX:
        raise _portal_error("解析过长。")
    return {"source_type": source_type, "year": year,
            "question_no": str(item.get("question_no", "")).strip()[:200],
            "stem": stem, "options": options, "answer_keys": answer,
            "explanation": explanation,
            "knowledge_point_id": str(item.get("knowledge_point_id") or "").strip() or None,
            "auth_id": str(item.get("auth_id") or "").strip() or None}


def _insert_question(db, staff_id: str, cert_id: str, q: dict[str, Any], auth_row: sqlite3.Row) -> str:
    source_ref = (f"{q['year']} 年{SOURCE_LABELS[q['source_type']]} · 授权文号 {auth_row['auth_no']}")
    qid = str(uuid.uuid4())
    now = _now()
    db.execute(
        """INSERT INTO training_bank_question (id, certificate_id, source_type, year, question_no,
           knowledge_point_id, stem, options_json, answer_keys_json, explanation, source_ref, auth_id,
           status, created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)""",
        (qid, cert_id, q["source_type"], q["year"], q["question_no"], q["knowledge_point_id"],
         q["stem"], json.dumps(q["options"], ensure_ascii=False), json.dumps(q["answer_keys"]),
         q["explanation"], source_ref, auth_row["id"], staff_id, now, now))
    return qid


def _question_payload(row: sqlite3.Row, *, with_answer: bool = False) -> dict[str, Any]:
    view = dict(row)
    view["options"] = json.loads(row["options_json"] or "[]")
    view["source_type_label"] = SOURCE_LABELS.get(row["source_type"], row["source_type"])
    if with_answer:
        view["answer_keys"] = json.loads(row["answer_keys_json"] or "[]")
    else:
        view.pop("answer_keys_json", None)
    return view


def import_questions(store, staff: dict, cert_id: str, items: list[Any]) -> dict[str, Any]:
    """批量导入题库题：逐行校验带原因，合法行入库为草稿，非法行原样报错不中断整批。"""
    if not isinstance(items, list) or not items:
        raise _portal_error("导入清单不能为空。")
    if len(items) > _BANK_BATCH_MAX:
        raise _portal_error(f"单批最多 {_BANK_BATCH_MAX} 题。")
    results: list[dict[str, Any]] = []
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise _portal_error("证书不存在。", 404)
        # 事务内缓存授权校验结果：同批同题源只查一次，行间复用。
        auth_cache: dict[str, sqlite3.Row] = {}
        for index, raw in enumerate(items):
            line = index + 1
            try:
                if not isinstance(raw, dict):
                    raise _portal_error("每题必须是 JSON 对象。")
                q = _normalize_question(store, cert_id, raw)
                key = q["source_type"]
                if key not in auth_cache:
                    auth_cache[key] = require_authorization(db, cert_id, key)
                auth_row = auth_cache[key]
                if q["auth_id"]:
                    explicit = db.execute(
                        "SELECT * FROM training_source_authorization WHERE id=? AND certificate_id=?",
                        (q["auth_id"], cert_id)).fetchone()
                    if not explicit:
                        raise _portal_error("指定的授权记录不存在或不属于该证书。")
                    if explicit["status"] != "active":
                        raise _portal_error("指定的授权记录已撤销。")
                    types = {t.strip() for t in str(explicit["question_types"]).split(",") if t.strip()}
                    if q["source_type"] not in types:
                        raise _portal_error("指定的授权记录不覆盖该题源。")
                    auth_row = explicit
                if q["knowledge_point_id"]:
                    kp = db.execute("SELECT certificate_id FROM knowledge_point WHERE id=?",
                                    (q["knowledge_point_id"],)).fetchone()
                    if not kp or kp["certificate_id"] != cert_id:
                        raise _portal_error("关联知识点不存在或不属于该证书。")
                qid = _insert_question(db, staff["id"], cert_id, q, auth_row)
                results.append({"line": line, "ok": True, "id": qid})
            except Exception as exc:  # 行级失败记录原因，继续下一行
                results.append({"line": line, "ok": False, "error": str(exc)})
        ok_count = sum(1 for r in results if r["ok"])
        return {"imported": ok_count, "failed": len(results) - ok_count, "results": results}


def list_bank_questions(store, cert_id: str, *, status: str | None = None,
                        source_type: str | None = None) -> list[dict[str, Any]]:
    clauses, params = ["WHERE certificate_id=?"], [cert_id]
    if status in BANK_STATUSES:
        clauses.append("AND status=?"); params.append(status)
    if source_type in ("real", "recall", "mock"):
        clauses.append("AND source_type=?"); params.append(source_type)
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
            raise _portal_error("证书不存在。", 404)
        rows = db.execute(
            f"SELECT * FROM training_bank_question {' '.join(clauses)}"
            " ORDER BY year DESC, question_no, created_at DESC", params).fetchall()
        return [_question_payload(r, with_answer=True) for r in rows]


def _bank_row(db, question_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM training_bank_question WHERE id=?", (question_id,)).fetchone()
    if not row:
        raise _portal_error("题库题目不存在。", 404)
    return row


def submit_question(store, staff: dict, question_id: str) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _bank_row(db, question_id)
        if row["status"] not in ("draft", "rejected"):
            raise _portal_error("仅草稿或被驳回的题目可提交审核。", 409)
        # 提交审核时复查授权仍然生效（授权到期/撤销后存量草稿不得再进入审核）。
        require_authorization(db, row["certificate_id"], row["source_type"])
        db.execute("UPDATE training_bank_question SET status='pending', updated_at=? WHERE id=?",
                   (_now(), question_id))
        row = _bank_row(db, question_id)
        return _question_payload(row, with_answer=True)


def review_question(store, staff: dict, question_id: str, *, decision: str, note: str = "") -> dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise _portal_error("审核决定必须是 approve 或 reject。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _bank_row(db, question_id)
        if row["status"] != "pending":
            raise _portal_error("仅待审状态的题目可审核。", 409)
        status = "approved" if decision == "approve" else "rejected"
        db.execute(
            "UPDATE training_bank_question SET status=?, review_note=?, reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?",
            (status, str(note).strip()[:2000], staff["id"], _now(), _now(), question_id))
        row = _bank_row(db, question_id)
        return _question_payload(row, with_answer=True)


def offline_question(store, staff: dict, question_id: str) -> dict[str, Any]:
    """已上线题目下架回草稿：学生不再抽到；历史作答保留（按当时题目与解析回溯）。"""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _bank_row(db, question_id)
        if row["status"] != "approved":
            raise _portal_error("仅已审核上线的题目可下架。", 409)
        db.execute("UPDATE training_bank_question SET status='draft', updated_at=? WHERE id=?",
                   (_now(), question_id))
        row = _bank_row(db, question_id)
        return _question_payload(row, with_answer=True)


def bank_question_for_attempt(db, question_id: str) -> sqlite3.Row:
    """作答取题：已上线题目直接返回；下架后历史作答仍可回看（get/submit 用）。"""
    row = db.execute("SELECT * FROM training_bank_question WHERE id=?", (question_id,)).fetchone()
    if not row:
        raise _portal_error("题库题目不存在。", 404)
    return row


# ---------------------------------------------------------------------------
# 教师端路由
# ---------------------------------------------------------------------------

class AuthorizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    licensor: str = Field(min_length=1)
    auth_no: str = Field(min_length=1)
    question_types: list[str] = Field(min_length=1)
    valid_from: str
    valid_until: str | None = None
    scope_note: str = ""


class QuestionImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 逐行校验：这里不能收窄成 list[dict]，否则一行写坏（字符串/数组）会让整个
    # 请求在模型校验阶段 422，其余合法行也一起进不了库，与「非法行原样报错、
    # 不中断整批」的口径冲突。结构性校验放在 import_questions 的行级循环里。
    questions: list[Any] = Field(min_length=1)


class QuestionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
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

    @app.get("/api/training/staff/certificates/{cert_id}/source-authorizations")
    @guarded
    def auth_list(cert_id: str,
                  session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"authorizations": list_authorizations(store_provider(), cert_id)})

    @app.post("/api/training/staff/certificates/{cert_id}/source-authorizations")
    @guarded
    def auth_create(cert_id: str, body: AuthorizationCreate,
                    session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        record = create_authorization(store_provider(), who, cert_id, licensor=body.licensor,
                                      auth_no=body.auth_no, question_types=body.question_types,
                                      valid_from=body.valid_from, valid_until=body.valid_until,
                                      scope_note=body.scope_note)
        return response({"authorization": record})

    @app.post("/api/training/staff/source-authorizations/{auth_id}/revoke")
    @guarded
    def auth_revoke(auth_id: str,
                    session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"authorization": revoke_authorization(store_provider(), who, auth_id)})

    @app.get("/api/training/staff/certificates/{cert_id}/bank-questions")
    @guarded
    def bank_list(cert_id: str, status: str | None = None, source_type: str | None = None,
                  session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"questions": list_bank_questions(store_provider(), cert_id, status=status,
                                                          source_type=source_type)})

    @app.post("/api/training/staff/certificates/{cert_id}/bank-questions")
    @guarded
    def bank_import(cert_id: str, body: QuestionImport,
                    session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response(import_questions(store_provider(), who, cert_id, body.questions))

    @app.post("/api/training/staff/bank-questions/{question_id}/submit")
    @guarded
    def bank_submit(question_id: str,
                    session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"question": submit_question(store_provider(), who, question_id)})

    @app.post("/api/training/staff/bank-questions/{question_id}/review")
    @guarded
    def bank_review(question_id: str, body: QuestionReview,
                    session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"question": review_question(store_provider(), who, question_id,
                                                     decision=body.decision, note=body.note)})

    @app.post("/api/training/staff/bank-questions/{question_id}/offline")
    @guarded
    def bank_offline(question_id: str,
                     session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        who = staff(session)
        return response({"question": offline_question(store_provider(), who, question_id)})
