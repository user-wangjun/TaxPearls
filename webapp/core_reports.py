"""Frozen core-task check reports, encrypted with the existing material storage key."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from pathlib import Path
import tempfile
from urllib.parse import quote

from fastapi import Cookie, HTTPException
from fastapi.responses import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, Field

from src import render
from src.kg_retrieval import metrics
from src.kg_retrieval.common import digest
from webapp import core_inputs, material_batches as batches, members
from webapp.access import AccessDenied

ROOT = Path(__file__).resolve().parents[1]


def report_evidence(slot):
    package=slot['bundle']
    cited={cid for claim in (slot.get('ai') or {}).get('assessments',[]) for cid in claim.get('clause_ids',[])}
    items=package['policy_evidence']+package['policy_leads']
    return [item for item in items if item.get('required') or item['clause_id'] in cited]


def clause_reference(clause_id, slot):
    """Resolve display citations from the frozen evidence, retaining IDs in JSON."""
    package = slot['bundle']
    item = next((p for p in package['policy_evidence'] + package['policy_leads']
                 if p['clause_id'] == clause_id), None)
    if item is None:
        return '条款来源待核实（定位见同版本 JSON）'
    label = item.get('clause_label')
    if not label:
        # Organized corpus IDs end with the source's complete clause label.
        organized = re.fullmatch(r'[^:]+:organized:[0-9a-f]{20}:(.+)', clause_id)
        if organized:
            label = organized[1]
        else:
            article = re.match(r'\s*(第[零〇一二三四五六七八九十百千万0-9]+条(?:之[零〇一二三四五六七八九十百千万0-9]+)?)', item.get('text') or '')
            label = article[1] if article else None
    return ' · '.join((item['title'], label or '条款位置待核实'))


def check_result(slot):
    """Present existing checks without promoting an evidence gap to a pass."""
    package = slot['bundle']
    ai = slot.get('ai') or {}
    fact = package.get('fact_assessment') or {}
    risk = ai.get('risk_assessment') or package.get('risk_assessment') or {}
    pending = bool(slot.get('stale') or package['bundle_status'] != 'ready' or package.get('gaps')
                   or fact.get('status') != 'FACT_CONFIRMED'
                   or ai and (not ai.get('validated') or ai.get('status') == 'needs_review'
                              or any(c.get('type') == 'insufficient_evidence' for c in ai.get('assessments', [])))
                   or risk.get('status') in {'NEEDS_REVIEW', 'NEEDS_MORE_DATA'})
    try:
        amount = Decimal(fact['amount']) if fact.get('amount') is not None else None
        difference = (fact.get('status') == 'FACT_CONFIRMED' and amount is not None
                      and amount.is_finite() and amount != 0)
    except (InvalidOperation, TypeError, ValueError):
        difference = False
    if slot.get('stale'):
        status, label = 'needs_review', '资料已变化，须重新检查'
    elif difference:
        status, label = 'issue', '已确认数据差异，问题性质待核实'
    elif not pending and (risk.get('status') == 'RISK_CANDIDATE' or package['check']['disposition'] == 'condition_met'):
        status, label = 'issue', '发现待核实风险线索'
    elif pending:
        status, label = 'needs_review', '暂无法完成本项检查'
    elif package['check']['disposition'] == 'excluded':
        status, label = 'excluded', '本项不适用'
    elif package['check']['disposition'] == 'condition_not_met' and risk.get('status') == 'NO_RISK_SIGNAL':
        status, label = 'passed', '本项检查通过'
    else:
        status, label, pending = 'needs_review', '暂无法完成本项检查', True
    if status == 'issue':
        advice = '核对本项涉及的原始凭证、账簿及申报资料，查明差异或异常原因，记录核对结果；确认需要更正后再按实际情况处理。'
    elif status == 'needs_review':
        advice = '补充下列缺失材料，核对数据口径和法规适用条件，完成复核后重新检查。'
    else:
        advice = '留存本项核对材料；资料或业务事实变化后重新检查。'
    return {'status': status, 'label': label, 'pending': pending, 'advice': advice}


def report_result(report):
    items = [check_result(slot) for slot in report['checks'].values()]
    issues = sum(item['status'] == 'issue' for item in items)
    pending = sum(item['pending'] or item['status'] == 'needs_review' for item in items)
    passed = sum(item['status'] == 'passed' for item in items)
    policy = report.get('policy_assessment') or {}
    blockers = []
    if report['processing'].get('status') != 'ready_for_analysis' or report['processing'].get('issues'):
        blockers.append('整体资料仍有缺口或待核对事项。')
    if policy and (policy.get('consistency', {}).get('status') != 'checked'
                   or any(p.get('status') == 'needs_review' for p in policy.get('results', []))):
        blockers.append('政策适用或跨政策核对尚未完成。')
    if issues:
        status, label = 'issues_found', '发现需处理的问题'
        message = f'发现 {issues} 项数据差异或待核实风险线索。'
        issue_pending = sum(item['status'] == 'issue' and item['pending'] for item in items)
        if issue_pending:
            message += f'其中 {issue_pending} 项的证据或适用性仍待复核。'
        if pending > issue_pending:
            message += f'另有 {pending - issue_pending} 项暂无法完成检查。'
    elif pending or blockers or not passed:
        status, label = 'incomplete', '暂无法完成检查'
        message = '本次尚不能返回检查通过；请完成待检查或待复核事项。'
        if not pending and not blockers and not passed:
            message = '本次项目均不适用，尚无可据以返回检查通过的适用项目。'
    else:
        status, label = 'passed', '本次检查通过'
        message = '本次检查范围内未发现异常。未运行项目不作结论。'
    return {'status': status, 'label': label, 'message': message, 'blockers': blockers,
            'issues': issues, 'pending': pending, 'passed': passed,
            'excluded': sum(item['status'] == 'excluded' for item in items)}


def material_reference(source_ids, report):
    sources = {row['record_id']: row for row in report.get('sources', [])}
    files = {row['file_id']: row for row in report.get('source_files', [])}
    labels = []
    for sid in source_ids:
        source = sources.get(sid)
        if source:
            label = ' · '.join(v for v in (source.get('file_name') or '来源文件待核对',
                                           source.get('source_position') or '原表位置待核对') if v)
        elif sid.startswith('file:') and sid[5:] in files:
            label = files[sid[5:]]['name']
        else:
            label = '材料来源待核对'
        if label not in labels:
            labels.append(label)
    return '；'.join(labels) or '缺少材料来源'


def metric_label(metric_id, slot):
    return next((fact['name'] for fact in slot['bundle']['facts'] if fact['metric_id'] == metric_id), '对比指标')


def report_gaps(gaps):
    grouped = {}
    labels = {'change_pending': '政策修订关系尚待审核。'}
    for gap in gaps:
        key = (gap['code'], gap.get('message') or labels.get(gap['code'], gap['code']))
        grouped.setdefault(key, {'message': key[1], 'count': 0})['count'] += 1
    return list(grouped.values())


def source_location(value):
    try:
        location=json.loads(value)
        if isinstance(location,dict) and location.get('text_path') and location.get('start_line'):
            return f"{location['text_path']} · 第{location['start_line']}至{location.get('end_line',location['start_line'])}行"
    except (ValueError,TypeError):
        pass
    return value


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS core_tax_reports (
        batch_id TEXT NOT NULL REFERENCES material_batches(id),
        version INTEGER NOT NULL, input_revision INTEGER NOT NULL,
        content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        created_by TEXT NOT NULL, snapshot_cipher BLOB NOT NULL, pdf_cipher BLOB,
        PRIMARY KEY(batch_id,version)
    )''')


def context(org_id, task_id, version, field="snapshot"):
    return batches._context(org_id, task_id, "tax-report:" + field, version)


def summary(checks):
    result = {"total": len(checks), "clues": 0, "not_met": 0, "excluded": 0, "needs_review": 0}
    for slot in checks.values():
        package = slot["bundle"]
        if package["check"]["rule_id"] == "VAT-R01" and not slot.get("stale"):
            risk = (slot.get("ai") or {}).get("risk_assessment", package.get("risk_assessment", {}))
            key = {"NO_RISK_SIGNAL": "not_met", "RISK_CANDIDATE": "clues", "NOT_APPLICABLE": "excluded"}.get(risk.get("status"), "needs_review")
            result[key] += 1
            continue
        pending = (slot.get("stale") or package["bundle_status"] != "ready"
                   or slot.get("ai") and (not slot["ai"].get("validated") or slot["ai"].get("status") == "needs_review"))
        key = "needs_review" if pending else {"condition_met": "clues", "condition_not_met": "not_met", "excluded": "excluded"}.get(package["check"]["disposition"], "needs_review")
        result[key] += 1
    return result


def archive(store, actor, task_id, expected_revision):
    # Frozen task read and final CAS both use the existing tenant/client authorization.
    view = batches.read(store, actor, task_id)
    if view["revision"] != expected_revision:
        raise AccessDenied("任务版本已变化，请重新读取后生成报告。", 409)
    payload = core_inputs.latest(view)
    checks = deepcopy(payload.get("tax_checks", {}))
    from webapp import core_policies
    if not checks:
        raise AccessDenied("请先完成至少一项核查，再生成检查报告。", 409)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = batches._authorize(db, actor, task_id)
        if batch["revision"] != expected_revision:
            raise AccessDenied("资料已更新，请重新读取。", 409)
        policy_assessment = core_policies.current_for_report(payload)
        for rid, slot in checks.items():
            old = slot["bundle"]
            fresh = metrics.build_task_bundle(db, payload, old["task"], rid, topic=old["check"]["topic"],
                                              token_budget=old["batching"]["soft_token_budget"])
            if slot.get("stale") or fresh["content_hash"] != old["content_hash"]:
                raise AccessDenied("资料或政策依据已变化，请重新核查后生成报告。", 409)
            if (slot.get("ai") or {}).get("validated"):
                from webapp.knowledge_evidence import validate_answer
                try:
                    validate_answer({"assessments": slot["ai"]["assessments"]}, old)
                except (ValueError, KeyError, TypeError):
                    slot["ai"] = {"status": "needs_review", "validated": False, "assessments": [],
                                  "reason": "历史 AI 说明未通过当前校验，请重新分析。"}
        file_names = {doc['id']: doc['name'] for doc in payload.get('documents', [])}
        sources = [{"record_id": row["id"], "file_id": row.get("source_file_id"),
                    "file_name": file_names.get(row.get('source_file_id')),
                    "source_position": row.get("source_position"), "field_sources": row.get("field_sources", {})}
                   for row in payload["processing"].get("records", []) if row["id"] in
                   {sid for slot in checks.values() for fact in slot["bundle"]["facts"] for sid in fact["source_ids"]}]
        template = (ROOT / "templates/core_check_report.html").read_text(encoding="utf-8")
        snapshot = {"schema": 1, "task": deepcopy(payload["task"]), "checks": checks,
                    'policy_assessment': policy_assessment,
                    "processing": {key: deepcopy(payload["processing"].get(key)) for key in ("status", "issues", "notices")},
                    "sources": sources, "summary": summary(checks), "formal_finding": False,
                    "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
                    "scope": "仅包含本任务已保存的核查项目，未运行项目不作结论。"}
        file_evidence = {sid[5:] for slot in checks.values() for f in slot["bundle"]["facts"] for sid in f["source_ids"] if sid.startswith("file:")}
        snapshot["source_files"] = [{"file_id":d["id"],"name":d["name"],"sha256":d["sha256"],
                                     "purpose":"零发生额原件，由本任务核对人确认"}
                                    for d in payload.get("documents", []) if d["id"] in file_evidence]
        snapshot['result'] = report_result(snapshot)
        content_hash = digest(snapshot)
        previous = db.execute("SELECT * FROM core_tax_reports WHERE batch_id=? ORDER BY version DESC LIMIT 1", (task_id,)).fetchone()
        if previous and previous["content_hash"] == content_hash:
            return {**descriptor(previous), "cached": True}
        version, stamp = (previous["version"] + 1 if previous else 1), members.now()
        snapshot.update(version=version, created_at=stamp, input_revision=expected_revision,
                        report_no=f"TP-KG-{task_id[:8]}-V{version}", content_hash=content_hash)
        env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
        env.filters.update(report_evidence=report_evidence,report_gaps=report_gaps,source_location=source_location,
                           clause_reference=clause_reference, check_result=check_result, report_result=report_result,
                           material_reference=material_reference, metric_label=metric_label)
        html = env.from_string(template).render(report=snapshot)
        if core_policies.current_for_report(payload) != policy_assessment:
            raise AccessDenied("报告生成期间政策版本变化，请重新核对。", 409)
        frozen = {"report": snapshot, "html": html, "html_sha256": hashlib.sha256(html.encode()).hexdigest(),
                  "template_sha256": hashlib.sha256(template.encode()).hexdigest()}
        cipher = batches._seal(batches._json(frozen), context(actor["org_id"], task_id, version))
        db.execute("INSERT INTO core_tax_reports VALUES (?,?,?,?,?,?,?,NULL)",
                   (task_id, version, expected_revision, content_hash, stamp, actor["id"], cipher))
        batches._event(db, batch, actor, "archive_tax_report", {"report_version": version, "input_revision": expected_revision})
        return {"version": version, "input_revision": expected_revision, "created_at": stamp, "content_hash": content_hash, "cached": False}


def descriptor(row):
    return {key: row[key] for key in ("version", "input_revision", "content_hash", "created_at")}


def load(store, actor, task_id, version):
    with store.connect() as db:
        batches._authorize(db, actor, task_id)
        row = db.execute("SELECT * FROM core_tax_reports WHERE batch_id=? AND version=?", (task_id, version)).fetchone()
        if not row:
            raise AccessDenied("检查报告版本不存在或无权访问。", 404)
        frozen = json.loads(batches._open(row["snapshot_cipher"], context(actor["org_id"], task_id, version)))
        if hashlib.sha256(frozen["html"].encode()).hexdigest() != frozen["html_sha256"]:
            raise batches.MaterialStorageError("检查报告完整性校验失败。")
        return frozen, dict(row)


class ArchiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


def register(app, get_store, get_user, allow):
    def actor(session):
        user = get_user(session)
        allow(user, "org_admin", "accountant")
        return user

    @app.get("/api/enterprise/input-tasks/{task_id}/reports")
    def listing(task_id: str, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user, store = actor(session), get_store()
        with store.connect() as db:
            batches._authorize(db, user, task_id)
            return [descriptor(row) for row in db.execute("SELECT * FROM core_tax_reports WHERE batch_id=? ORDER BY version DESC", (task_id,))]

    @app.post("/api/enterprise/input-tasks/{task_id}/reports")
    def create(task_id: str, body: ArchiveBody, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        try:
            return archive(get_store(), actor(session), task_id, body.expected_revision)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/enterprise/input-tasks/{task_id}/reports/{version}")
    def report(task_id: str, version: int, format: str = "html", session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user, store = actor(session), get_store()
        frozen, row = load(store, user, task_id, version)
        headers = {"Cache-Control": "private, no-store", "X-TaxPearls-Report-Version": str(version)}
        if format == "html":
            return Response(frozen["html"], media_type="text/html", headers={**headers, "X-TaxPearls-SHA256": frozen["html_sha256"]})
        name = quote(f"企业税务核查-{task_id[:8]}-v{version}")
        if format == "json":
            return Response(batches._json(frozen["report"]), media_type="application/json",
                            headers={**headers, "Content-Disposition": f"attachment; filename*=UTF-8''{name}.json"})
        if format != "pdf":
            raise HTTPException(422, "报告格式须为 html、pdf 或 json。")
        if not row["pdf_cipher"]:
            with tempfile.TemporaryDirectory(prefix="taxpearls-core-report-") as tmp:
                try:
                    target = Path(tmp) / "report.pdf"
                    render.export_pdf(frozen["html"], target, report_no=frozen["report"]["report_no"], report_title="企业税务核查报告")
                    data = target.read_bytes()
                except RuntimeError:
                    raise HTTPException(503, "PDF 导出暂不可用，请先查看网页报告或下载 JSON。") from None
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                batch = batches._authorize(db, user, task_id)
                db.execute("UPDATE core_tax_reports SET pdf_cipher=COALESCE(pdf_cipher,?) WHERE batch_id=? AND version=?",
                           (batches._seal(data, context(user["org_id"], task_id, version, "pdf")), task_id, version))
                batches._event(db, batch, user, "export_tax_report_pdf", {"report_version": version})
        # Reauthorize after browser rendering and return the immutable cached bytes.
        _, row = load(store, user, task_id, version)
        data = batches._open(row["pdf_cipher"], context(user["org_id"], task_id, version, "pdf"))
        return Response(data, media_type="application/pdf", headers={**headers, "Content-Disposition": f"attachment; filename*=UTF-8''{name}.pdf"})
