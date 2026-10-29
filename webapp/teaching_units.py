"""教学单元与实操案例（重设计方案 B1：数据模型 + 服务层 + 接口）。

口径（设计方案 §0.1/§5.3，不得改）：
- 单元与案例一律绑定 `rule_code`（R-001…R-024）与图谱 TaxRule 快照 id，
  **不绑定 YAML 文件路径**；执行源仍是 rules/*.yaml。
- 依据链实时查图谱（TaxRule→USES_METRIC→Metric、→Clause→Policy）。
  本库 Clause/Policy 为 0：凡引用条款一律返回「依据待专业复核」，
  不得表述为已审核的法定标准（legal_basis 文本来自规则快照，属内部筛查口径）。
- R-024 等未挂靠知识点的规则通过 unlinked 接口暴露，教师端另行提示。
- 停用走 archived 状态，不删行；案例标准结论由任务链路在下发/判分时使用，
  本模块的列表接口对教师全量、对学生由上层按任务策略过滤。

接口命名空间：/api/training/teaching/*（避免与现有 /api/training/* 路由打架）。
"""
from __future__ import annotations

from webapp.training_content import public_content_operator

import json
import sqlite3
import uuid
from functools import wraps
from typing import Any, Callable

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from webapp.storage import _now

STAGES = ("voucher", "data", "conclusion")
STAGE_LABELS = {"voucher": "凭证层", "data": "数据层", "conclusion": "结论层"}
UNIT_STATUSES = ("draft", "published", "archived")
_NAME_MAX = 120
_STANDARD_MAX = 500
_TEXT_MAX = 20_000
_REVIEW_PENDING = "依据待专业复核"


def _portal_error(message: str, status: int = 422) -> Exception:
    from webapp.training_portal import TrainingPortalError
    return TrainingPortalError(message, status)


def load_rules(store):
    from webapp.training_content import load_rules as _load
    return _load(store)


# ---------------------------------------------------------------------------
# 依据链（图谱实时查询）
# ---------------------------------------------------------------------------

def rule_snapshot_id(db, rule_code: str) -> str | None:
    """该 rule_code 最新一条 TaxRule 快照 id（读取路径触发 sync_rules 后查询）。

    sync_rules 先插入带 execution_json 的版本快照，rule_conversion.install 随后
    可能插入更新的「已转换」行（无 execution_json）；依据链优先取带执行快照的
    版本（含 legal_basis/references 口径），它才携带法条要点。"""
    row = db.execute(
        "SELECT rule_id FROM TaxRule WHERE rule_code=? AND execution_json IS NOT NULL"
        " ORDER BY rowid DESC LIMIT 1", (rule_code,)).fetchone()
    return row["rule_id"] if row else None


def rule_evidence(store, rule_code: str) -> dict[str, Any]:
    """规则的依据链：TaxRule 快照 → USES_METRIC → Metric；条款经图谱关系。

    Clause/Policy 缺失时 legal_status='pending_review'，法条文本仅作内部筛查
    口径展示（来自规则快照的 legal_basis），不得当作已审核法定依据。"""
    rules = load_rules(store)
    if not any(r.id == rule_code for r in rules):
        raise _portal_error(f"规则不存在：{rule_code}。", 404)
    with store.connect() as db:
        from webapp import policy_graph
        policy_graph.sync_rules(db, rules)
        snapshot_id = rule_snapshot_id(db, rule_code)
        if not snapshot_id:
            raise _portal_error("规则快照尚未生成，请稍后重试。", 409)
        rule_row = db.execute("SELECT * FROM TaxRule WHERE rule_id=?",
                              (snapshot_id,)).fetchone()
        metrics = []
        for edge in db.execute(
                "SELECT to_id FROM Relationship WHERE from_type='TaxRule' AND from_id=?"
                " AND relation_type='USES_METRIC' ORDER BY to_id", (snapshot_id,)).fetchall():
            metric = dict(db.execute("SELECT * FROM Metric WHERE metric_id=?",
                                     (edge["to_id"],)).fetchone())
            if metric:
                metrics.append({"metric_id": metric["metric_id"], "name": metric["name"],
                                "definition": metric.get("definition") or "",
                                "value_type": metric["value_type"]})
        clause_ids = [row["from_id"] for row in db.execute(
            "SELECT from_id FROM Relationship WHERE from_type='Clause' AND to_type='TaxRule'"
            " AND to_id=?", (snapshot_id,)).fetchall()]
        for row in db.execute(
                "SELECT source_clause_id FROM \"Condition\" WHERE rule_id=?"
                " AND source_clause_id IS NOT NULL", (snapshot_id,)).fetchall():
            clause_ids.append(row["source_clause_id"])
        clauses = []
        for cid in dict.fromkeys(clause_ids):
            row = dict(db.execute("SELECT * FROM Clause WHERE clause_id=?", (cid,)).fetchone())
            if row:
                policy = db.execute("SELECT * FROM Policy WHERE policy_id=?",
                                    (row["policy_id"],)).fetchone()
                clauses.append({"clause_id": cid, "clause_label": row["clause_label"],
                                "policy_title": policy["title"] if policy else "",
                                "source_location": row.get("source_location") or ""})
        snapshot = json.loads(rule_row["execution_json"]) if rule_row["execution_json"] else {}
        legal_basis = snapshot.get("legal_basis") or []
        references = snapshot.get("references") or []
        return {
            "rule_code": rule_code,
            "snapshot_id": snapshot_id,
            "version": rule_row["version"],
            "name": rule_row["name"],
            "metrics": metrics,
            "clauses": clauses,
            "legal_basis": legal_basis,       # 规则快照携带的法条要点（内部筛查口径）
            "references": references,
            # 本库图谱条款层为 0：没有已审核条款即不得声称法定依据成立
            "legal_status": "verified" if clauses else "pending_review",
            "legal_status_label": "" if clauses else _REVIEW_PENDING,
        }


def unlinked_rules(store) -> list[dict[str, Any]]:
    """没有任何启用知识点关联的规则（如 R-024）——教师端需提示「本班学不到」。"""
    rules = load_rules(store)
    with store.connect() as db:
        linked = {row["target_id"] for row in db.execute(
            "SELECT DISTINCT target_id FROM knowledge_point_link"
            " WHERE target_type='rule' AND active=1")}
    return [{"rule_code": r.id, "name": r.name} for r in rules if r.id not in linked]


# ---------------------------------------------------------------------------
# 教学单元 CRUD
# ---------------------------------------------------------------------------

def _unit_payload(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row) | {
        "knowledge_point_ids": json.loads(row["knowledge_point_ids_json"]),
        "rule_codes": json.loads(row["rule_codes_json"]),
        "stage_label": STAGE_LABELS.get(row["stage"], row["stage"]),
    }


def _case_payload(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row) | {
        "rule_codes": json.loads(row["rule_codes_json"]),
        "materials": json.loads(row["materials_json"]),
        "standard_conclusion": json.loads(row["standard_conclusion_json"]),
    }


def _clean_codes(raw: Any, field: str, *, valid: set[str] | None = None,
                 required: bool = True) -> list[str]:
    if raw is None:
        raw = []
    if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
        raise _portal_error(f"{field} 须为字符串数组。")
    codes = list(dict.fromkeys(v.strip() for v in raw if v.strip()))
    if required and not codes:
        raise _portal_error(f"{field} 不能为空。")
    if valid is not None:
        unknown = [c for c in codes if c not in valid]
        if unknown:
            raise _portal_error(f"{field} 含未知规则：{', '.join(unknown)}。")
    return codes


def _clean_kp_ids(db, cert_id: str, raw: Any) -> list[str]:
    ids = _clean_codes(raw, "knowledge_point_ids", required=False)
    unknown = [i for i in ids if not db.execute(
        "SELECT 1 FROM knowledge_point WHERE id=? AND certificate_id=?",
        (i, cert_id)).fetchone()]
    if unknown:
        raise _portal_error(f"知识点不属于该证书：{', '.join(unknown)}。")
    return ids


def _clean_materials(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise _portal_error("materials 须为数组。")
    items = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                or not item["name"].strip():
            raise _portal_error("材料项须含非空 name 字段。")
        items.append({"name": item["name"].strip()[:_NAME_MAX],
                      "note": str(item.get("note", "") or "").strip()[:_TEXT_MAX]})
    return items


def _clean_conclusion(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise _portal_error("standard_conclusion 须为数组。")
    items = []
    for item in raw:
        if not isinstance(item, dict):
            raise _portal_error("标准结论项须为对象。")
        expected = str(item.get("expected", "")).strip()
        if expected not in ("hit", "pass", "not_executed"):
            raise _portal_error("结论 expected 须为 hit/pass/not_executed（命中/通过/未执行）。")
        items.append({"rule_code": str(item.get("rule_code", "")).strip(),
                      "expected": expected,
                      "note": str(item.get("note", "") or "").strip()[:_TEXT_MAX]})
    return items


def _require_cert(db, cert_id: str) -> None:
    if not db.execute("SELECT 1 FROM certificate WHERE id=?", (cert_id,)).fetchone():
        raise _portal_error("证书不存在。", 404)


def _require_row(db, table: str, row_id: str) -> sqlite3.Row:
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise _portal_error("记录不存在。", 404)
    return row


def list_units(store, cert_id: str) -> list[dict[str, Any]]:
    with store.connect() as db:
        _require_cert(db, cert_id)
        rows = db.execute(
            "SELECT * FROM teaching_unit WHERE certificate_id=?"
            " ORDER BY sort_order, created_at", (cert_id,)).fetchall()
        return [_unit_payload(r) for r in rows]


def create_unit(store, staff: dict, cert_id: str, *, name: str, stage: str,
                standard: str = "", knowledge_point_ids: list[str] | None = None,
                rule_codes: list[str] | None = None, deliverables: str = "",
                sort_order: int = 0) -> dict[str, Any]:
    name = str(name).strip()
    if not name or len(name) > _NAME_MAX:
        raise _portal_error(f"单元名称不能为空且不超过 {_NAME_MAX} 字。")
    if stage not in STAGES:
        raise _portal_error(f"stage 须为 {'/'.join(STAGES)}（凭证层/数据层/结论层）。")
    rules = load_rules(store)
    codes = _clean_codes(rule_codes, "rule_codes", valid={r.id for r in rules})
    with store.connect() as db:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        _require_cert(db, cert_id)
        kps = _clean_kp_ids(db, cert_id, knowledge_point_ids)
        unit_id = uuid.uuid4().hex
        now = _now()
        db.execute(
            "INSERT INTO teaching_unit (id, certificate_id, name, stage, standard,"
            " knowledge_point_ids_json, rule_codes_json, deliverables, status,"
            " sort_order, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?, 'draft', ?,?,?,?)",
            (unit_id, cert_id, name, stage, str(standard).strip()[:_STANDARD_MAX],
             json.dumps(kps, ensure_ascii=False), json.dumps(codes, ensure_ascii=False),
             str(deliverables).strip()[:_TEXT_MAX], int(sort_order),
             staff.get("id"), now, now))
        return _unit_payload(_require_row(db, "teaching_unit", unit_id))


def update_unit(store, staff: dict, unit_id: str, *, name: str | None = None,
                stage: str | None = None, standard: str | None = None,
                knowledge_point_ids: list[str] | None = None,
                rule_codes: list[str] | None = None, deliverables: str | None = None,
                sort_order: int | None = None, status: str | None = None) -> dict[str, Any]:
    rules = load_rules(store)
    with store.connect() as db:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        row = _require_row(db, "teaching_unit", unit_id)
        if row["status"] == "archived":
            raise _portal_error("已归档单元不可修改；如需调整请新建单元。")
        if status is not None:
            if status not in UNIT_STATUSES:
                raise _portal_error(f"status 须为 {'/'.join(UNIT_STATUSES)}。")
            if row["status"] == "draft" and status != "published":
                raise _portal_error("草稿只能发布或保持草稿。")
            if row["status"] == "published" and status == "draft":
                raise _portal_error("已发布单元不能退回草稿；可归档后新建。")
        if stage is not None and stage not in STAGES:
            raise _portal_error(f"stage 须为 {'/'.join(STAGES)}。")
        updates: dict[str, Any] = {"updated_at": _now()}
        if name is not None:
            name = str(name).strip()
            if not name or len(name) > _NAME_MAX:
                raise _portal_error(f"单元名称不能为空且不超过 {_NAME_MAX} 字。")
            updates["name"] = name
        if stage is not None:
            updates["stage"] = stage
        if standard is not None:
            updates["standard"] = str(standard).strip()[:_STANDARD_MAX]
        if knowledge_point_ids is not None:
            updates["knowledge_point_ids_json"] = json.dumps(
                _clean_kp_ids(db, row["certificate_id"], knowledge_point_ids),
                ensure_ascii=False)
        if rule_codes is not None:
            updates["rule_codes_json"] = json.dumps(
                _clean_codes(rule_codes, "rule_codes", valid={r.id for r in rules}),
                ensure_ascii=False)
        if deliverables is not None:
            updates["deliverables"] = str(deliverables).strip()[:_TEXT_MAX]
        if sort_order is not None:
            updates["sort_order"] = int(sort_order)
        if status is not None:
            updates["status"] = status
        db.execute(f"UPDATE teaching_unit SET {','.join(k + '=?' for k in updates)}"
                   " WHERE id=?", (*updates.values(), unit_id))
        return _unit_payload(_require_row(db, "teaching_unit", unit_id))


# ---------------------------------------------------------------------------
# 实操案例 CRUD
# ---------------------------------------------------------------------------

def list_cases(store, cert_id: str, *, unit_id: str | None = None) -> list[dict[str, Any]]:
    with store.connect() as db:
        _require_cert(db, cert_id)
        if unit_id:
            rows = db.execute(
                "SELECT * FROM teaching_case WHERE certificate_id=? AND unit_id=?"
                " ORDER BY created_at", (cert_id, unit_id)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM teaching_case WHERE certificate_id=?"
                " ORDER BY created_at", (cert_id,)).fetchall()
        return [_case_payload(r) for r in rows]


def create_case(store, staff: dict, cert_id: str, *, name: str, unit_id: str | None = None,
                scenario: str = "", rule_codes: list[str] | None = None,
                materials: list[Any] | None = None, standard_conclusion: list[Any] | None = None,
                desensitization_note: str = "") -> dict[str, Any]:
    name = str(name).strip()
    if not name or len(name) > _NAME_MAX:
        raise _portal_error(f"案例名称不能为空且不超过 {_NAME_MAX} 字。")
    rules = load_rules(store)
    codes = _clean_codes(rule_codes, "rule_codes", valid={r.id for r in rules})
    mats = _clean_materials(materials)
    conclusion = _clean_conclusion(standard_conclusion)
    for item in conclusion:
        if item["rule_code"] and item["rule_code"] not in codes:
            raise _portal_error(
                f"标准结论引用了未绑定规则：{item['rule_code']}；请先加入 rule_codes。")
    with store.connect() as db:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        _require_cert(db, cert_id)
        if unit_id:
            unit = _require_row(db, "teaching_unit", unit_id)
            if unit["certificate_id"] != cert_id:
                raise _portal_error("教学单元不属于该证书。")
        case_id = uuid.uuid4().hex
        now = _now()
        db.execute(
            "INSERT INTO teaching_case (id, certificate_id, unit_id, name, scenario,"
            " rule_codes_json, materials_json, standard_conclusion_json,"
            " desensitization_note, status, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?, 'draft', ?,?,?)",
            (case_id, cert_id, unit_id, name, str(scenario).strip()[:_TEXT_MAX],
             json.dumps(codes, ensure_ascii=False), json.dumps(mats, ensure_ascii=False),
             json.dumps(conclusion, ensure_ascii=False),
             str(desensitization_note).strip()[:_TEXT_MAX],
             staff.get("id"), now, now))
        return _case_payload(_require_row(db, "teaching_case", case_id))


def update_case(store, staff: dict, case_id: str, *, name: str | None = None,
                unit_id: str | None = None, scenario: str | None = None,
                rule_codes: list[str] | None = None, materials: list[Any] | None = None,
                standard_conclusion: list[Any] | None = None,
                desensitization_note: str | None = None,
                status: str | None = None) -> dict[str, Any]:
    rules = load_rules(store)
    with store.connect() as db:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        row = _require_row(db, "teaching_case", case_id)
        if row["status"] == "archived":
            raise _portal_error("已归档案例不可修改；如需调整请新建案例。")
        if status is not None:
            if status not in UNIT_STATUSES:
                raise _portal_error(f"status 须为 {'/'.join(UNIT_STATUSES)}。")
            if row["status"] == "published" and status == "draft":
                raise _portal_error("已发布案例不能退回草稿；可归档后新建。")
        updates: dict[str, Any] = {"updated_at": _now()}
        if name is not None:
            name = str(name).strip()
            if not name or len(name) > _NAME_MAX:
                raise _portal_error(f"案例名称不能为空且不超过 {_NAME_MAX} 字。")
            updates["name"] = name
        if unit_id is not None:
            if unit_id:
                unit = _require_row(db, "teaching_unit", unit_id)
                if unit["certificate_id"] != row["certificate_id"]:
                    raise _portal_error("教学单元不属于该证书。")
            updates["unit_id"] = unit_id or None
        if scenario is not None:
            updates["scenario"] = str(scenario).strip()[:_TEXT_MAX]
        if rule_codes is not None:
            updates["rule_codes_json"] = json.dumps(
                _clean_codes(rule_codes, "rule_codes", valid={r.id for r in rules}),
                ensure_ascii=False)
        if materials is not None:
            updates["materials_json"] = json.dumps(_clean_materials(materials),
                                                    ensure_ascii=False)
        if standard_conclusion is not None:
            conclusion = _clean_conclusion(standard_conclusion)
            bound = set(json.loads(updates.get(
                "rule_codes_json", row["rule_codes_json"])))
            for item in conclusion:
                if item["rule_code"] and item["rule_code"] not in bound:
                    raise _portal_error(
                        f"标准结论引用了未绑定规则：{item['rule_code']}；请先加入 rule_codes。")
            updates["standard_conclusion_json"] = json.dumps(conclusion, ensure_ascii=False)
        if desensitization_note is not None:
            updates["desensitization_note"] = str(desensitization_note).strip()[:_TEXT_MAX]
        if status is not None:
            updates["status"] = status
        db.execute(f"UPDATE teaching_case SET {','.join(k + '=?' for k in updates)}"
                   " WHERE id=?", (*updates.values(), case_id))
        return _case_payload(_require_row(db, "teaching_case", case_id))


# ---------------------------------------------------------------------------
# 路由（/api/training/teaching/*）
# ---------------------------------------------------------------------------

class UnitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    certificate_id: str
    name: str
    stage: str
    standard: str = ""
    knowledge_point_ids: list[str] = []
    rule_codes: list[str]
    deliverables: str = ""
    sort_order: int = 0


class UnitUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    stage: str | None = None
    standard: str | None = None
    knowledge_point_ids: list[str] | None = None
    rule_codes: list[str] | None = None
    deliverables: str | None = None
    sort_order: int | None = None
    status: str | None = None


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    certificate_id: str
    name: str
    unit_id: str | None = None
    scenario: str = ""
    rule_codes: list[str]
    materials: list[dict[str, Any]] = []
    standard_conclusion: list[dict[str, Any]] = []
    desensitization_note: str = ""


class CaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    unit_id: str | None = None
    scenario: str | None = None
    rule_codes: list[str] | None = None
    materials: list[dict[str, Any]] | None = None
    standard_conclusion: list[dict[str, Any]] | None = None
    desensitization_note: str | None = None
    status: str | None = None


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

    @app.get("/api/training/teaching/units")
    @guarded
    def units_list(certificate_id: str,
                   session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"units": list_units(store_provider(), certificate_id)})

    @app.post("/api/training/teaching/units")
    @guarded
    def units_create(body: UnitCreate,
                     session: str | None = Cookie(default=None, alias="taxpearls_session")) -> Response:
        who = public_content_operator(store_provider(), session)
        unit = create_unit(store_provider(), who, body.certificate_id, name=body.name,
                           stage=body.stage, standard=body.standard,
                           knowledge_point_ids=body.knowledge_point_ids,
                           rule_codes=body.rule_codes, deliverables=body.deliverables,
                           sort_order=body.sort_order)
        return response({"unit": unit})

    @app.put("/api/training/teaching/units/{unit_id}")
    @guarded
    def units_update(unit_id: str, body: UnitUpdate,
                     session: str | None = Cookie(default=None, alias="taxpearls_session")) -> Response:
        who = public_content_operator(store_provider(), session)
        unit = update_unit(store_provider(), who, unit_id, name=body.name, stage=body.stage,
                           standard=body.standard, knowledge_point_ids=body.knowledge_point_ids,
                           rule_codes=body.rule_codes, deliverables=body.deliverables,
                           sort_order=body.sort_order, status=body.status)
        return response({"unit": unit})

    @app.get("/api/training/teaching/cases")
    @guarded
    def cases_list(certificate_id: str, unit_id: str | None = None,
                   session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"cases": list_cases(store_provider(), certificate_id,
                                             unit_id=unit_id)})

    @app.post("/api/training/teaching/cases")
    @guarded
    def cases_create(body: CaseCreate,
                     session: str | None = Cookie(default=None, alias="taxpearls_session")) -> Response:
        who = public_content_operator(store_provider(), session)
        case = create_case(store_provider(), who, body.certificate_id, name=body.name,
                           unit_id=body.unit_id, scenario=body.scenario,
                           rule_codes=body.rule_codes, materials=body.materials,
                           standard_conclusion=body.standard_conclusion,
                           desensitization_note=body.desensitization_note)
        return response({"case": case})

    @app.put("/api/training/teaching/cases/{case_id}")
    @guarded
    def cases_update(case_id: str, body: CaseUpdate,
                     session: str | None = Cookie(default=None, alias="taxpearls_session")) -> Response:
        who = public_content_operator(store_provider(), session)
        case = update_case(store_provider(), who, case_id, name=body.name,
                           unit_id=body.unit_id, scenario=body.scenario,
                           rule_codes=body.rule_codes, materials=body.materials,
                           standard_conclusion=body.standard_conclusion,
                           desensitization_note=body.desensitization_note,
                           status=body.status)
        return response({"case": case})

    @app.get("/api/training/teaching/rule-evidence/{rule_code}")
    @guarded
    def evidence(rule_code: str,
                 session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"evidence": rule_evidence(store_provider(), rule_code)})

    @app.get("/api/training/teaching/unlinked-rules")
    @guarded
    def unlinked(session: str | None = Cookie(default=None, alias=STAFF_COOKIE)) -> Response:
        staff(session)
        return response({"rules": unlinked_rules(store_provider())})
