"""Frozen core-task check reports, encrypted with the existing material storage key."""
from copy import deepcopy
from calendar import monthrange
from datetime import date
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
from src import periods
from src.input_errors import InputError
from src.kg_retrieval import bundle, metrics
from src.kg_retrieval.common import digest
from webapp import core_inputs, material_batches as batches, members
from webapp.access import AccessDenied

ROOT = Path(__file__).resolve().parents[1]


def report_evidence(slot):
    package=slot['bundle']
    cited={cid for claim in (slot.get('ai') or {}).get('assessments',[]) for cid in claim.get('clause_ids',[])}
    items=package['policy_evidence']+package['policy_leads']
    return [item for item in items if item.get('required') or item['clause_id'] in cited]


def report_analysis(slot):
    """Retain business explanations while omitting model and execution diagnostics."""
    ai = slot.get('ai') or {}
    if not ai.get('validated'):
        return []
    diagnostic = re.compile(
        r'(?<![a-z0-9_])(?:UNKNOWN|UNKOWN|TRUE|FALSE|conditions|fact_evaluation|TaxRule|RuleCalculation|JSON|tokens?)(?![a-z0-9_])'
        r'|(?<![a-z0-9_])[a-z]+_[a-z_]+(?![a-z0-9_])|R-\d+@|:(?:converted|organized):'
        r'|程序|子条件|布尔|内部筛查|规则版本|规则关系|整体核查状态|不得据此改判|只输出|条件成立或不成立',
        re.IGNORECASE)

    def business_text(value):
        sentences = re.findall(r'[^。！？\n]+[。！？]?', value or '')
        return ''.join(sentence.strip() for sentence in sentences if not diagnostic.search(sentence))

    result = []
    for claim in (ai.get('batch_summary') or {}).get('assessments', ai.get('assessments', [])):
        text = business_text(claim.get('text'))
        materials = list(dict.fromkeys(value for material in claim.get('required_materials', [])
                                      if (value := business_text(material))))
        if text or materials:
            result.append({'text': text, 'required_materials': materials})
    return result


def report_period(subject, report):
    """Describe required dates without replacing historical periods with the task period."""
    fact = subject if isinstance(subject, dict) else {}
    name = fact.get('name', '') if fact else str(subject)
    task = report.get('task', {})
    try:
        start, end = (date.fromisoformat(task[key]) for key in ('start_date', 'end_date'))
        if start > end:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return '时间范围待确认，请明确起止日期或余额时点。'
    current = f'{start.isoformat()} 至 {end.isoformat()}'
    explicit = re.findall(r'\d{4}-\d{2}-\d{2}', name)
    if explicit:
        try:
            dates = [date.fromisoformat(value) for value in explicit]
            if len(dates) == 2 and dates[0] <= dates[1]:
                return f'材料说明要求期间：{dates[0].isoformat()} 至 {dates[1].isoformat()}；请核对本项所需口径。'
            if len(dates) == 1 and re.search(r'截至|时点|期初|期末', name):
                return f'材料说明要求时点：{dates[0].isoformat()}；请核对余额口径。'
        except ValueError:
            pass
        return '材料说明中的时间范围或日期口径待确认，请明确起止日期或余额时点。'
    calendar_labels = re.findall(r'\d{4}年\d{1,2}月(?:\d{1,2}日)?|\d{4}年度|\d{4}-(?:Q[1-4]|H[12]|\d{2})(?![\d-])', name)
    if calendar_labels:
        try:
            if len(calendar_labels) == 1:
                label = calendar_labels[0]
                if '日' in label:
                    point = periods.statement_date(label)
                    if point and re.search(r'截至|时点|期初|期末', name):
                        return f'材料说明要求时点：{point}；请核对余额口径。'
                else:
                    interval = periods.parse_period(label, '材料期间')
                    return f'要求期间：{interval.start.isoformat()} 至 {interval.end.isoformat()}。'
        except (InputError, ValueError):
            pass
        return '材料说明中的时间范围或日期口径待确认，请明确各期起止日期或余额时点。'
    reference = fact.get('reference') or {}
    if '参考' in name:
        if reference.get('start') and reference.get('end'):
            try:
                a, b = date.fromisoformat(reference['start']), date.fromisoformat(reference['end'])
                if a > b:
                    raise ValueError
                return f'参考依据有效期间：{a.isoformat()} 至 {b.isoformat()}；须核对其是否覆盖本次检查期间 {current}。'
            except (TypeError, ValueError):
                pass
        return f'参考依据有效期间待确认；须覆盖本次检查期间 {current}。'
    try:
        expected = periods.metric_period(name, current)
    except (InputError, ValueError):
        return f'历史或年度范围待确认；须提供完整的相应期间，不能用本次检查期间 {current} 代替。'
    if expected:
        if '末' in name:
            return f'余额时点：{expected.end.isoformat()} 期末。'
        return f'要求期间：{expected.start.isoformat()} 至 {expected.end.isoformat()}。'
    if '上年同期' in name and not re.search(r'本期|可比|以及|及本', name):
        try:
            expected = periods.metric_period('历史.上年同期收入', current)
            return f'要求期间：{expected.start.isoformat()} 至 {expected.end.isoformat()}。'
        except (InputError, ValueError):
            return f'上年同期日期待确认；不能直接使用本次检查期间 {current}。'
    if re.search(r'上期|前期|上年同期|去年|前年|上年|可比|历史|年度|同比|滚动|连续', name):
        # Only explicit calendar periods can determine the immediately preceding period.
        if '上期' in name and not re.search(r'可比|历史|年度|上年', name):
            try:
                interval = periods.parse_period(current, '检查期间')
                index = start.year * 12 + start.month - 1 - interval.months
                year, month = divmod(index, 12)
                a = date(year, month + 1, 1)
                index += interval.months - 1
                year, month = divmod(index, 12)
                b = date(year, month + 1, monthrange(year, month + 1)[1])
                if '期末' in name:
                    return f'上期期末余额时点：{b.isoformat()}。'
                if '期初' in name:
                    return f'上期期初余额时点：{a.isoformat()}。'
                return f'上期：{a.isoformat()} 至 {b.isoformat()}。'
            except (InputError, ValueError):
                pass
        return f'历史或可比期间待确认；请明确各期起止日期，不与本次检查期间 {current} 混用。'
    points = []
    if '期初' in name:
        points.append(f'期初余额：{start.isoformat()} 期初')
    if re.search(r'期末|年末', name):
        points.append(f'期末余额：{end.isoformat()} 期末')
    if not points or re.search(r'本期|入库|出库|调整|收发存|发生额', name):
        points.insert(0, f'要求期间：{current}')
    basis = {'tax_period': '按税款所属期核对', 'issue_date': '按开票日期核对',
             'posting_date': '按记账日期核对'}.get(fact.get('period_basis'))
    if basis:
        points.append(basis)
    return '；'.join(points) + '。'


def report_materials(report):
    """One upload checklist with exact date boundaries and reversible project links."""
    uploads, reviews, by_check = {}, {}, {}
    review_words = re.compile(r'(?:政策|法规|条款|规则).*(?:复核|核实|效力|生效|有效|原文|证据|文本|覆盖|审核)'
                              r'|适用性.*(?:核对|核实|复核)|效力复核|原文核对')
    families = (
        (r'增值税.*申报表|增值税申报资料', '增值税申报表及附列资料'),
        (r'个税.*申报|个人所得税.*申报', '个人所得税申报资料'),
        (r'社保.*(?:申报|参保)|社会保险.*(?:申报|参保)', '社保参保及申报资料'),
        (r'存货.*盘点|盘盈|盘亏', '存货盘点与差异处理资料'),
        (r'存货.*(?:跌价|减值)', '存货跌价准备资料'),
        (r'存货.*(?:计价|计算方法)', '存货计价与成本计算依据'),
        (r'存货.*(?:余额|入库|出库|调整|收发存|取数)', '存货账簿及收发存资料'),
        (r'工资表|人员花名册|入离职', '工资表及人员名册'),
        (r'参考.*(?:毛利率|税负率)|(?:毛利率|税负率).*参考', '参考指标及出处说明'),
        (r'利润表|财务报表|营业收入.*报表|历史\.上年同期(?:收入|成本)|年度\..*净利润', '利润表及财务报表'),
        (r'资产负债表|年度\..*资产', '资产负债表及资产余额资料'),
        (r'科目余额表|总账|明细账|账簿|账面\.|营业收入的原始', '会计账簿及科目余额表'),
        (r'增值税\.|申报销售额', '增值税申报表及附列资料'),
        (r'销项发票|进项发票|发票明细|发票.*清单|发票开具汇总|发票\.', '发票明细及对应凭证'),
        (r'差异|调节|勾稽|复核记录|取数逻辑|计算底稿|分析底稿|完整性说明', '核对及差异调节底稿'),
        (r'合同|结算单|收款|付款|凭证|单据', '业务凭证及交易资料'),
    )

    def add_upload(text, period, project):
        text = re.sub(r'^(?:需补充|请补充|补充)[：:\s]*', '', text).strip('。；; ')
        if not text:
            return
        name = next((label for pattern, label in families if re.search(pattern, text)), text)
        key = (re.sub(r'[\s，,、；;。]', '', name), period)
        row = uploads.setdefault(key, {'number': len(uploads) + 1, 'name': name, 'period': period,
                                       'requirements': [], 'projects': []})
        if text not in row['requirements']:
            row['requirements'].append(text)
        if project not in row['projects']:
            row['projects'].append(project)
        if row['number'] not in by_check[project['key']]['uploads']:
            by_check[project['key']]['uploads'].append(row['number'])

    for number, (rid, slot) in enumerate(report['checks'].items(), 1):
        by_check[rid] = {'uploads': [], 'review': False}
        if check_result(slot)['status'] not in {'issue', 'needs_review'}:
            continue
        package = slot['bundle']
        project = {'key': rid, 'number': number, 'topic': package['check']['topic']}
        facts = [fact for fact in package.get('facts', []) if not fact.get('is_calculation')]
        requests = [text for claim in report_analysis(slot) for text in claim['required_materials']]
        for text in requests:
            if review_words.search(text):
                by_check[rid]['review'] = True
                continue
            matched = [fact for fact in facts if fact['name'] in text or
                       len(fact['name'].split('.')[-1]) >= 4 and fact['name'].split('.')[-1] in text]
            # Explicit temporal wording must not be overridden by a matching current-period metric.
            temporal = re.search(r'\d{4}|上期|前期|上年|去年|前年|可比|历史|年度|同比|滚动|连续|期初|期末', text)
            required = list(dict.fromkeys(report_period(fact, report) for fact in matched))
            if temporal:
                period = report_period(text, report)
                # Exact annual metric names can resolve their independently required dates.
                if matched and all(fact['name'] in text for fact in matched) and not re.search(r'\d{4}|可比|同比|滚动|连续', text):
                    period = ' '.join(required)
            else:
                period = ' '.join(required) if required else report_period(text, report)
            add_upload(text, period, project)
        # Missing data still appears when AI was unavailable or did not provide a list.
        for fact in facts:
            if fact.get('state') != 'known':
                add_upload(fact['name'] + '的原始报表或取数凭证', report_period(fact, report), project)
        basis_codes = {'rule_unapproved', 'rule_logic_pending', 'relationship_pending', 'rule_period', 'rule_region',
                       'required_clause_inapplicable', 'policy_review', 'policy_evidence_missing',
                       'rule_source_changed', 'change_pending', 'change_source_missing', 'entity_pending', 'exception_review'}
        if by_check[rid]['review'] or any(gap.get('code') in basis_codes for gap in package.get('gaps', [])):
            by_check[rid]['review'] = True
            text = '核实本项法规依据、适用条件及其是否覆盖本次检查期间；由检查或复核人员处理。'
            row = reviews.setdefault(text, {'text': text, 'projects': []})
            row['projects'].append(project)
    return {'uploads': list(uploads.values()), 'reviews': list(reviews.values()), 'by_check': by_check}


def clause_reference(clause_id, slot):
    """Resolve display citations from the frozen evidence, retaining IDs in JSON."""
    package = slot['bundle']
    item = next((p for p in package['policy_evidence'] + package['policy_leads']
                 if p['clause_id'] == clause_id), None)
    if item is None:
        return '条款来源待核实'
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
                              or any(c.get('type') == 'insufficient_evidence' for c in
                                     (ai.get('batch_summary') or {}).get('assessments', ai.get('assessments', []))))
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
    descriptions = {
        'issue': '核对发现数据差异或异常线索，请结合原始材料查明原因。',
        'needs_review': '现有资料或适用依据尚不足以完成本项判断。',
        'passed': '本项核对未发现异常。',
        'excluded': '本项不适用于当前业务情况。',
    }
    return {'status': status, 'label': label, 'pending': pending, 'advice': advice,
            'description': descriptions[status]}


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


def report_gaps(gaps, slot=None):
    """Summarize review blockers for readers without exposing diagnostic fields."""
    grouped = {}
    labels = {
        'budget_unsplittable': '本项自动分析未完成，需人工复核。',
        'metric_missing': '请补充或核对本项所需数据。',
        'comparability_pending': '不同材料的统计口径尚未核对一致。',
        'exception_review': '本项业务的特殊适用情况尚待核实。',
        'rule_unapproved': '本项检查依据尚待复核。',
        'rule_logic_pending': '本项检查依据尚待复核。',
        'relationship_pending': '本项检查依据尚待复核。',
        'rule_period': '检查依据是否覆盖本次检查期间尚待核实。',
        'rule_region': '检查依据对所在地区的适用性尚待核实。',
        'entity_pending': '企业或业务是否符合适用条件尚待核实。',
        'required_clause_inapplicable': '法规条款对本次业务及期间的适用性尚待复核。',
        'policy_review': '法规条款对本次业务及期间的适用性尚待复核。',
        'policy_evidence_missing': '本项法规依据尚未核实。',
        'rule_source_changed': '相关法规存在变更，当前适用版本尚待核实。',
        'change_pending': '相关法规存在变更，当前适用版本尚待核实。',
        'change_source_missing': '相关法规存在变更，当前适用版本尚待核实。',
    }
    names = {fact['metric_id']: fact['name'] for fact in (slot or {}).get('bundle', {}).get('facts', [])
             if not fact.get('is_calculation')}
    basis_review = {'rule_unapproved', 'rule_logic_pending', 'relationship_pending', 'rule_period', 'rule_region',
                    'required_clause_inapplicable', 'policy_review', 'policy_evidence_missing',
                    'rule_source_changed', 'change_pending', 'change_source_missing'}
    for gap in gaps:
        message = labels.get(gap.get('code'), '本项仍有资料或依据待复核。')
        if slot is not None and gap.get('code') in basis_review:
            message = '本项判断依据尚待确认。'
        if gap.get('code') == 'metric_missing':
            reason = gap.get('message') or ''
            if '单位' in reason:
                message = '金额或数量单位尚未核实。'
            elif '期间' in reason:
                message = '数据所属期间或统计口径尚未核实。'
            elif '来源' in reason:
                message = '缺少可核对的原始材料。'
            if name := names.get(gap.get('metric_id')):
                message = f'{name}：{message}'
        grouped.setdefault(message, {'message': message, 'count': 0})['count'] += 1
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


def archive(store, actor, task_id, expected_revision, *, complete_report_version=None):
    # Frozen task read and final CAS both use the existing tenant/client authorization.
    view = batches.read_current(store, actor, task_id)
    if view["revision"] != expected_revision:
        raise AccessDenied("任务版本已变化，请重新读取后生成报告。", 409)
    payload = core_inputs.latest(view)
    if payload.get("completion"):
        raise AccessDenied("任务已完成并归档，请先重新打开任务。", 409)
    checks = deepcopy(payload.get("tax_checks", {}))
    from webapp import core_policies
    if not checks:
        raise AccessDenied("请先完成至少一项核查，再生成检查报告。", 409)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = batches._authorize(db, actor, task_id)
        if batch["revision"] != expected_revision:
            raise AccessDenied("资料已更新，请重新读取。", 409)
        from webapp import core_record_index
        core_record_index.compare(db, batch, actor, payload)
        policy_assessment = core_policies.current_for_report(payload)
        for rid, slot in checks.items():
            old = slot["bundle"]
            fresh = metrics.build_task_bundle(db, payload, old["task"], rid, topic=old["check"]["topic"],
                                              token_budget=old["batching"]["soft_token_budget"],
                                              output_tokens=old["batching"].get("output_tokens"))
            if slot.get("stale") or bundle.content_hash(fresh) != bundle.content_hash(old):
                raise AccessDenied("资料或政策依据已变化，请重新核查后生成报告。", 409)
            if (slot.get("ai") or {}).get("validated"):
                from webapp.knowledge_evidence import validate_saved_analysis
                try:
                    validate_saved_analysis(slot["ai"], old)
                except (ValueError, KeyError, TypeError):
                    slot["ai"] = {"status": "needs_review", "validated": False, "assessments": [],
                                  "reason": "历史 AI 说明未通过当前校验，请重新分析。"}
        file_names = {doc['id']: doc['name'] for doc in payload.get('documents', [])}
        sources = [{"record_id": row["id"], "file_id": row.get("source_file_id"),
                    "file_name": file_names.get(row.get('source_file_id')),
                    "source_position": row.get("source_position"), "field_sources": row.get("field_sources", {}),
                    **({"business_record": {key: row["dedup"][key] for key in ("record_key", "version_key")}}
                       if row.get("dedup") else {})}
                   for row in payload["processing"].get("records", []) if row["id"] in
                   {sid for slot in checks.values() for fact in slot["bundle"]["facts"] for sid in fact["source_ids"]}]
        template = (ROOT / "templates/core_check_report.html").read_text(encoding="utf-8")
        source_ids = {source["record_id"] for source in sources}
        for slot in checks.values():
            for row in slot["bundle"].get("supporting_materials", []):
                if row["source_id"] not in source_ids:
                    source_ids.add(row["source_id"])
                    sources.append({"record_id": row["source_id"], "file_id": row["file_id"],
                                    "file_name": file_names.get(row["file_id"]), "source_position": row["source_position"],
                                    "field_sources": {}, "source_reviewed": row["source_reviewed"]})
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
        snapshot['supplements'] = report_materials(snapshot)
        content_hash = digest(snapshot)
        previous = db.execute("SELECT * FROM core_tax_reports WHERE batch_id=? ORDER BY version DESC LIMIT 1", (task_id,)).fetchone()
        if complete_report_version is not None:
            if not previous or previous['version'] != complete_report_version or previous['content_hash'] != content_hash:
                raise AccessDenied("报告与当前资料或检查结果不一致，请先更新检查报告后再归档。", 409)
            if db.execute("SELECT 1 FROM material_jobs WHERE batch_id=? AND state IN ('queued','running')", (task_id,)).fetchone():
                raise AccessDenied("任务仍有待处理工作，请处理结束后再归档。", 409)
            if core_policies.current_for_report(payload) != policy_assessment:
                raise AccessDenied("归档期间政策版本变化，请重新核对。", 409)
            payload['completion'] = {'report_version': previous['version'], 'content_hash': content_hash,
                                     'input_revision': expected_revision, 'completed_at': members.now(),
                                     'completed_by': actor['id']}
            batches._append(db, batch, actor, 'confirmation', payload, lifecycle='complete')
            return payload['completion']
        if previous and previous["content_hash"] == content_hash:
            return {**descriptor(previous), "cached": True}
        version, stamp = (previous["version"] + 1 if previous else 1), members.now()
        snapshot.update(version=version, created_at=stamp, input_revision=expected_revision,
                        report_no=f"TP-KG-{task_id[:8]}-V{version}", content_hash=content_hash)
        env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
        env.filters.update(report_evidence=report_evidence,report_analysis=report_analysis,report_materials=report_materials,report_period=report_period,report_gaps=report_gaps,source_location=source_location,
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


class CompleteBody(ArchiveBody):
    report_version: int = Field(ge=1)


def register(app, get_store, get_user, allow):
    def actor(session):
        user = get_user(session)
        allow(user, "org_admin", "accountant")
        return user

    @app.post("/api/enterprise/input-tasks/{task_id}/complete")
    def complete(task_id: str, body: CompleteBody, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        store, user = get_store(), actor(session)
        archive(store, user, task_id, body.expected_revision, complete_report_version=body.report_version)
        return core_inputs.project(batches.read_current(store, user, task_id))

    @app.post("/api/enterprise/input-tasks/{task_id}/reopen")
    def reopen(task_id: str, body: ArchiveBody, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        store, user = get_store(), actor(session)
        view = batches.read_current(store, user, task_id)
        payload = deepcopy(core_inputs.latest(view))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = batches._authorize(db, user, task_id)
            if batch['revision'] != body.expected_revision or view['revision'] != body.expected_revision:
                raise AccessDenied('任务版本已变化，请重新读取。', 409)
            if not payload.get('completion'):
                raise AccessDenied('任务尚未归档，无需重新打开。', 409)
            payload.pop('completion')
            batches._append(db, batch, user, 'edit', payload, lifecycle='reopen')
        return core_inputs.project(batches.read_current(store, user, task_id))

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
