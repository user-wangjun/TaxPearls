"""Bounded, in-memory material import. PDF candidates always require review."""
from __future__ import annotations
from src import periods

from dataclasses import asdict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import re
from xml.etree import ElementTree

import pdfplumber

from . import config, loader, related_graph, material_review, material_security, material_formats, material_format_guard, financial_import
from . import material_provenance as provenance
from .models import (
    Account, Company, Dataset, Metric, RelatedGraph, RelatedRelation,
    RelatedSubject, RelatedTrade,
)
from .ai_extraction import ExtractionError
from .workbooks import MAX_FILE as MAX_FILE, MAX_EXPANDED, open_workbook, formula_values

InputError = loader.InputError
MAX_TOTAL = MAX_EXPANDED
MAX_FILES = 20
COMPANY_KEYS = ("name", "taxpayer_id", "industry", "period")
SCOPE_FIELDS = {'地区': 'region', '纳税人身份': 'taxpayer_type', '业务构成': 'business_scope'}


def _text(value):
    return "" if value is None else str(value).strip()


def _serial(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serial(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serial(v) for v in value]
    return value


def expand_uploads(files):
    """Validate the complete batch in a bounded worker before exposing its contents."""
    return [(name, raw) for name, raw, _ in material_format_guard.check(files, expand=True, wait=True)]


def _table_workbook(name, data, extension):
    from openpyxl import Workbook
    rows = material_formats.table_rows(data, extension)
    stem = name.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    known = {**{k: v[0] for k, v in material_review.TABLES.items()},
             config.SHEET_INVOICES: config.COL_INVOICES,
             config.SHEET_BANK: config.COL_BANK,
             config.SHEET_BANK_ADJUSTMENTS: config.COL_BANK_ADJUSTMENTS,
             config.SHEET_HUMAN: config.COL_HUMAN,
             config.SHEET_CONTRACTS: config.COL_CONTRACTS,
             config.SHEET_FULFILLMENTS: config.COL_FULFILLMENTS,
             config.SHEET_CONTRACT_LINKS: config.COL_CONTRACT_LINKS}
    headers = [loader._header_token(c) for c in rows[0]]
    matching = [sheet for sheet, cols in known.items()
                if headers[:len(cols)] == [loader._header_token(c) for c in cols]]
    source_kind = financial_import.table_kind(rows)
    sheet = source_kind or (stem if stem in matching else matching[0] if len(matching) == 1 else '上传表格')
    wb = Workbook()
    try:
        wb.active.title = sheet
        for row in rows:
            wb.active.append(row)
        out = BytesIO()
        wb.save(out)
        return out.getvalue()
    finally:
        wb.close()


def _image(data, doc):
    info = doc.get('format_check') or material_format_guard.metadata(doc['name'], data)
    frames = info['pages'] if info else material_formats.image_pages(data, doc['kind'])
    doc.update(page_count=len(frames), review_required=True,
               pages=[{**page, 'text': '', 'label': f"扫描图片第 {page['page']} 页"} for page in frames],
               summary=f'{len(frames)} 页扫描图片；须核对图片中的企业、期间、单位和金额。')
    doc['warnings'].append('图片不含可验证文本，需 AI 视觉提取或人工补录，并对照原图逐项核对。')


def _excel(data, doc, *, allow_incomplete_company=False, capture_standard=False, keys=None, import_options=None):
    try:
        wb = open_workbook(data)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    try:
        material_security.scan_workbook(wb, doc)
        company = Company("", "", "", "")
        if config.SHEET_COMPANY in wb.sheetnames:
            company = loader._read_company(wb, allow_incomplete=allow_incomplete_company)
        elif config.SHEET_SUPPLEMENT in wb.sheetnames:
            ws = loader._sheet(wb, config.SHEET_SUPPLEMENT, config.COL_SUPPLEMENT)
            periods = {_text(row[3]) for row in ws.iter_rows(min_row=2, values_only=True) if row[0] is not None}
            periods.discard("")
            if len(periods) != 1:
                raise InputError("补充指标必须具有唯一的核对所属期。")
            company.period = periods.pop()
        saved_formulas = formula_values(data) if any(
            cell.data_type == 'f' for sheet in wb for cell in sheet._cells.values()) else {}
        imported = financial_import.read(wb, asdict(company), keys, import_options, saved_formulas)
        exported = {s['sheet'] for s in imported['sheets']} if imported else set()
        if imported:
            company = Company(**imported['company'])
        accounts = loader._read_accounts(wb) if config.SHEET_ACCOUNTS in wb.sheetnames and config.SHEET_ACCOUNTS not in exported else []
        declarations = loader._read_declarations(wb) if config.SHEET_DECLARATION in wb.sheetnames and config.SHEET_DECLARATION not in exported else {}
        metrics = {}
        for sheet in (config.SHEET_INCOME, config.SHEET_BALANCE, config.SHEET_CASHFLOW):
            if sheet not in exported:
                loader._read_statement(wb, sheet, sheet, metrics)
        period_series = loader._read_history(wb, company, metrics)
        invoices = loader._read_invoices(wb, company)
        bank_transactions = loader._read_bank_transactions(wb)
        bank_adjustments = loader._read_bank_adjustments(wb)
        human_records = loader._read_human_records(wb)
        contracts = loader._read_contracts(wb)
        fulfillments = loader._read_fulfillments(wb)
        contract_links = loader._read_contract_links(wb)
        graph = related_graph.read_workbook(wb, company)
        loader._read_supplement(wb, company, metrics)
        tables = material_review.capture(wb, excluded=exported) if capture_standard else {}
        reviewable = material_review.fields({'standard_tables': tables})
        if (not accounts and not declarations and not metrics and not period_series and not invoices
                and not bank_transactions and not bank_adjustments and not human_records
                and not contracts and not fulfillments and not contract_links and graph is None and not reviewable and not imported):
            raise InputError("当前未识别该来源的账表结构，尚未采用金额。原件不修改；"
                             "请提供支持格式的来源导出，或将此材料保留待适配。"
                             "不要仅重命名工作表或列名来通过检查。")
        serialized_series = [
            {
                "name": name,
                "value": str(item.metric.value),
                "source": item.metric.source,
                "period": item.period.label,
                "detail": item.metric.detail,
            }
            for name, values in period_series.items()
            for item in values.values()
        ]
        doc.update(company=asdict(company), accounts=_serial([asdict(a) for a in accounts]),
                   declarations=_serial(declarations), rows=[_serial(asdict(m)) for m in metrics.values()],
                   period_series=serialized_series, invoices=_serial([asdict(invoice) for invoice in invoices]),
                   bank_transactions=_serial([asdict(item) for item in bank_transactions]),
                   bank_adjustments=_serial([asdict(item) for item in bank_adjustments]),
                   human_records=_serial([asdict(item) for item in human_records]),
                   contracts=_serial([asdict(item) for item in contracts]),
                   fulfillments=_serial([asdict(item) for item in fulfillments]),
                   contract_links=_serial([asdict(item) for item in contract_links]),
                   related_graph=_serial(asdict(graph)) if graph else None)
        if config.SHEET_COMPANY in wb.sheetnames:
            for row in wb[config.SHEET_COMPANY].iter_rows(min_row=2, values_only=True):
                if len(row) >= 2 and _text(row[0]) in SCOPE_FIELDS and _text(row[1]):
                    doc['company'][SCOPE_FIELDS[_text(row[0])]] = _text(row[1])
        if capture_standard:
            doc['standard_tables'] = tables
            material_review.annotate(doc, tables, {})
        if imported:
            imported['base'] = {key: deepcopy(doc.get(key, default)) for key, default in (
                ('accounts', []), ('declarations', {}), ('rows', []),
                ('account_cell_sources', {}), ('declaration_cell_sources', {}))}
            doc['import_model'] = imported
            doc['import_mapping'] = {'version': imported['version'], 'sheets': imported['sheets'],
                'unmapped': imported['unmapped'], 'ignored_sheets': imported['ignored_sheets'],
                'pending': imported['pending'],
                'formula_cells': [deepcopy(cell) for cell in imported['cells'].values() if cell.get('formula')],
                'source_digest': provenance.digest(imported)}
            doc['warnings'].extend(imported['warnings'])
            doc['warnings'].append('财务导出映射须复核栏次、单位和实际期间；未映射项目不参与检测，空白不作零。')
            # Only derived financial values change here. Replacing the entire
            # document with the evaluator's deepcopy would detach the live local
            # extraction record and leave a successful parse marked "running".
            evaluated = financial_import.evaluate(doc)
            for key in ('accounts', 'declarations', 'rows', 'account_cell_sources', 'declaration_cell_sources'):
                doc[key] = evaluated[key]
        doc["summary"] = (
            f"{len(doc['accounts'])} 行科目、{len(doc['declarations'])} 项申报、{len(doc['rows'])} 项补充/报表指标、"
            f"{len(serialized_series)} 条期间序列、{len(invoices)} 张发票、"
            f"{len(bank_transactions)} 笔银行流水、{len(bank_adjustments)} 条银行调节、"
            f"{len(human_records)} 条人力记录、{len(contracts)} 份合同、"
            f"{len(fulfillments)} 条履约记录、{len(contract_links)} 条四流勾稽、"
            f"{len(graph.trades) if graph else 0} 条关联交易"
        )
    finally:
        wb.close()


def _xml_values(root) -> dict[str, list[str]]:
    values = {}
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > 5000 or depth > 40:
            raise InputError("XML 元素数量或嵌套深度超过限制。")
        local = element.tag.rsplit("}", 1)[-1]
        text = (element.text or "").strip()
        if text:
            values.setdefault(loader._header_token(local), []).append(text)
        stack.extend((child, depth + 1) for child in element)
    return values


def _xml_value(values, *aliases, required=False, label="字段"):
    found = []
    for alias in aliases:
        found.extend(values.get(loader._header_token(alias), []))
    found = list(dict.fromkeys(found))
    if len(found) > 1:
        raise InputError(f"XML 中{label}存在冲突值：{'、'.join(found)}")
    if required and not found:
        raise InputError(f"XML 缺少{label}。")
    return found[0] if found else ""


def _xml(data, doc):
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.IGNORECASE):
        raise InputError("XML 不允许 DTD 或实体声明。")
    try:
        root = material_formats.xml_root(data)
        material_security.add_findings(doc, material_security.detect('\n'.join(root.itertext()), location='XML'))
    except ElementTree.ParseError as exc:
        raise InputError(f"XML 无法解析：{exc}") from exc
    root_name = root.tag.rsplit("}", 1)[-1].lower()
    mof_namespace = "http://xbrl.mof.gov.cn/taxonomy/"
    if root_name != "xbrl" or not any(
        isinstance(element.tag, str) and element.tag.startswith("{" + mof_namespace)
        for element in root.iter()
    ):
        raise InputError("XML 不是财政部电子凭证会计数据标准的数电发票 XBRL 实例。")
    values = _xml_values(root)
    number = loader._invoice_number(
        _xml_value(values, "InvoiceNumber", "Fphm", "InvoiceNo", "发票号码", required=True, label="发票号码"),
        "XML 发票号码",
    )
    issued_on = loader._invoice_date(
        _xml_value(values, "RequestTime", "IssueDate", "Kprq", "开票日期", required=True, label="开票日期"),
        f"XML 发票「{number}」",
    )
    amount = loader._number(
        _xml_value(values, "TotalAmWithoutTax", "AmountWithoutTax", "Hjje", "不含税金额合计", required=True, label="不含税金额合计"),
        f"XML 发票「{number}」不含税金额",
    )
    tax = loader._number(
        _xml_value(values, "TotalTaxAm", "TotalTaxAmount", "Hjse", "合计税额", required=True, label="合计税额"),
        f"XML 发票「{number}」税额",
    )
    total = loader._number(
        _xml_value(values, "TotalTax-includedAmount", "TotalTaxIncludedAmount", "Jshj", "价税合计小写", label="价税合计"),
        f"XML 发票「{number}」价税合计",
    )
    if amount is None or tax is None:
        raise InputError(f"XML 发票「{number}」不含税金额和税额不能为空。")
    if total is not None and abs(abs(total) - abs(amount) - abs(tax)) > Decimal("0.01"):
        raise InputError(f"XML 发票「{number}」价税合计与不含税金额加税额不一致。")
    red = loader._invoice_bool(
        _xml_value(values, "WhetherEinvoiceIsRedEinvoice", "IsRedInvoice", "红字发票标志", label="红字标志"),
        f"XML 发票「{number}」红字标志",
    )
    void = loader._invoice_bool(
        _xml_value(values, "WhetherEinvoiceIsVoided", "IsVoid", "作废标志", label="作废标志"),
        f"XML 发票「{number}」作废标志",
    )
    raw_status = _xml_value(values, "InvoiceStatus", "Status", "发票状态", label="发票状态")
    if void:
        raw_status = "作废"
    elif red:
        raw_status = "红字"
    status = loader._invoice_status(raw_status, amount, tax, f"XML 发票「{number}」")
    seller_id = _xml_value(values, "SellerIdNum", "SellerTaxpayerId", "XsfNsrsbh", "销售方纳税人识别号", label="销售方税号")
    buyer_id = _xml_value(values, "BuyerIdNum", "PurchaserIdNum", "GmfNsrsbh", "购买方纳税人识别号", label="购买方税号")
    accounting_id = _xml_value(
        values, "UnifiedSocialCreditCodeOfAccountingEntity", "AccountingEntityId", label="会计主体统一社会信用代码"
    )
    accounting_name = _xml_value(values, "NameOfAccountingEntity", "AccountingEntityName", label="会计主体名称")
    if not buyer_id and accounting_id and accounting_id != seller_id:
        buyer_id = accounting_id
    if accounting_id:
        doc["company"]["taxpayer_id"] = accounting_id
    if accounting_name:
        doc["company"]["name"] = accounting_name
    explicit_kind = _xml_value(values, "InvoiceDirection", "BusinessDirection", "发票方向", label="发票方向")
    company = Company(doc["company"]["name"], doc["company"]["taxpayer_id"], "", "")
    kind = loader._invoice_kind(explicit_kind, "XML 发票", seller_id, buyer_id, company)
    confirmed = loader._invoice_bool(
        _xml_value(values, "WhetherEinvoiceUsageHasBeenConfirmed", "UsageConfirmed", "用途确认状态", label="用途确认状态"),
        f"XML 发票「{number}」用途确认状态",
    )
    usage = _xml_value(values, "UsageConfirmation", "DeductionUsage", "用途确认", label="用途确认")
    transferred = loader._invoice_bool(
        _xml_value(values, "WhetherInputVatHasBeenTransferredOut", "InputVatTransferredOut", "进项转出标志", label="进项转出标志"),
        f"XML 发票「{number}」进项转出标志",
    )
    transferred_tax = loader._number(
        _xml_value(values, "AmountOfTransferredOutInputVat", "TransferredOutInputVat", "进项转出税额", label="进项转出税额"),
        f"XML 发票「{number}」进项转出税额",
    )
    deductible_tax = None
    if kind in {"", "采购"} and status != "作废" and confirmed is True and (not usage or "抵扣" in usage):
        deductible_tax = abs(tax)
        if transferred:
            transferred_tax = abs(transferred_tax or Decimal(0))
            if transferred_tax > deductible_tax:
                raise InputError(f"XML 发票「{number}」进项转出税额不能大于发票税额。")
            deductible_tax -= transferred_tax
    detail = f"XML 数电发票；{kind or '待按企业税号判定方向'}{status}；不含税 {abs(amount):,.2f}；税额 {abs(tax):,.2f}"
    if total is not None:
        detail += f"；价税合计 {abs(total):,.2f}"
    invoice = loader._Invoice(
        number, issued_on, kind, abs(amount), abs(tax), status, deductible_tax,
        _xml_value(values, "PeriodOfUsageConfirmation", "DeductionPeriod", "抵扣所属期", label="抵扣所属期"),
        seller_id, buyer_id, "XML 数电发票结构化元素", detail,
    )
    doc["invoices"] = [_serial(asdict(invoice))]
    doc["warnings"].append("XML 已按结构化字段解析；当前不代替电子税务局验真或数字签名验证。")
    doc["summary"] = f"1 张 XML 数电发票：{number}，{kind or '购销方向提交时按企业税号判定'}，{status}"


def pdf_range(value, total):
    """A server-validated source-page interval, never renumbered derived pages."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'first', 'last'} or
            any(type(value[k]) is not int for k in ('first', 'last')) or
            not 1 <= value['first'] <= value['last'] <= total or
            value['last'] - value['first'] + 1 > material_formats.PDF_SEGMENT_PAGES):
        raise InputError(f'PDF 片段须为原件有效起止页码，每次最多 {material_formats.PDF_SEGMENT_PAGES} 页。')
    return deepcopy(value)


def _pdf_statement_columns(page, text, doc, keys, page_no):
    """Read explicitly bounded native statements, not flattened numeric order.

    Annual flows need an explicit year; a balance date is an endpoint only.
    Side-by-side balance sections retain their own labels and current column.
    """
    lines = text.splitlines()
    title = re.sub(r'\s+', '', lines[0]) if lines else ''
    kind = re.sub(r'^(?:母公司|合并)', '', title)
    if kind not in {'利润表', '资产负债表', '现金流量表'}:
        return False
    header = '\n'.join(lines[:8])
    years = re.findall(r'(?m)^\s*(\d{4})\s*年度\s*$', header)
    as_of = ''
    if kind == '资产负债表':
        dates = re.findall(r'(?m)^\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*$', header)
        if len(dates) != 1 or years:
            return False
        try:
            as_of = date(*map(int, dates[0])).isoformat()
        except ValueError:
            raise InputError('PDF 资产负债表时点无效，请核对原件。') from None
    elif len(years) != 1:
        return False
    if not re.search(r'单位[:：]\s*人民币元(?:\s|$)', header):
        return False
    words = page.extract_words()
    current_labels = {'期末数', '期末金额'} if as_of else {'本期数', '本期金额'}
    previous_labels = {'上年年末数', '期初数', '期初金额'} if as_of else {'上年同期数', '上期金额'}
    current = sorted((w for w in words if w['text'] in current_labels), key=lambda w: w['x0'])
    previous = sorted((w for w in words if w['text'] in previous_labels), key=lambda w: w['x0'])
    if len(current) not in ({1, 2} if as_of else {1}) or len(current) != len(previous):
        return False
    if any(abs(c['top'] - p['top']) > 2 or c['x1'] >= p['x0'] or
           (i and previous[i - 1]['x1'] >= c['x0']) for i, (c, p) in enumerate(zip(current, previous))):
        return False
    money = financial_import._money_metadata(header, title)
    if not money['currency_supported'] or not money['unit_supported'] or money['factor'] != '1':
        raise InputError('PDF 财务报表币种或金额单位冲突，不能按人民币元读取。')
    edges = [e for e in page.edges if e['orientation'] == 'v' and e['bottom'] - e['top'] > 25]
    columns = []
    for i, (c, p) in enumerate(zip(current, previous)):
        left = max((e['x0'] for e in edges if e['x0'] < c['x0']), default=None)
        right = min((e['x0'] for e in edges if e['x0'] > c['x1']), default=None)
        label_start = min((e['x0'] for e in edges if e['x0'] > previous[i - 1]['x1']), default=None) if i else float('-inf')
        if left is None or right is None or label_start is None or not left < c['x0'] < c['x1'] < right < p['x0']:
            return False
        label_end = min((e['x0'] for e in edges if label_start + 1 < e['x0'] <= left), default=None)
        if label_end is None:
            return False
        columns.append((c, left, right, label_start, label_end))
    bottom = max(e['bottom'] for e in edges)
    rows = []
    for word in sorted(words, key=lambda w: (w['top'], w['x0'])):
        # Font bounding boxes can extend fractionally beyond a ruling even
        # when glyphs sit inside its final row; allow at most one PDF point.
        if word['top'] <= max(w['bottom'] for w in current + previous) + 2 or word['bottom'] > bottom + 1:
            continue
        if not rows or abs(rows[-1][0]['top'] - word['top']) > 2:
            rows.append([])
        rows[-1].append(word)
    candidates = []
    for row in rows:
        row.sort(key=lambda w: w['x0'])
        for column, left, right, label_start, label_end in columns:
            label = ''.join(w['text'] for w in row if w['x0'] >= label_start and w['x1'] < label_end)
            label = re.sub(r'^[一二三四五六七八九十]+[、．.]|^(?:减|加)[:：]', '', label)
            # Only these explicit operating components, never wider totals.
            if kind == '利润表' and label in {'其中：营业收入', '其中:营业收入', '其中：营业成本', '其中:营业成本'}:
                label = label[3:]
            label = financial_import.ITEM_ALIASES.get(kind, {}).get(label, label)
            label = re.sub(r'[（(].*$', '', label)
            name = kind + '.' + label
            if kind == '现金流量表':
                name = {'期初现金及现金等价物余额': '现金.期初现金及等价物',
                        '期末现金及现金等价物余额': '现金.期末现金及等价物'}.get(label, name)
            if name not in keys:
                continue
            if any(not any(abs(edge['x0'] - boundary) < 1 and edge['top'] <= row[0]['top'] and
                           edge['bottom'] + 1 >= max(w['bottom'] for w in row) for edge in edges)
                   for boundary in (left, right)):
                continue
            amount = ''.join(w['text'] for w in row if w['x0'] >= left and w['x1'] <= right)
            if not re.fullmatch(r'[-+−]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', amount):
                continue  # Blank, crossing-cell and ambiguous amounts are not zero.
            number = loader._number(amount.replace(',', '').replace('−', '-'), f'PDF 第 {page_no} 页')
            if number is not None:
                timing = f'报表时点 {as_of}（不代表完整期间）' if as_of else f'实际期间 {years[0]}年度'
                candidates.append({'name': name, 'value': str(number), 'page': page_no,
                    'detail': f'{title}；本期栏次 {column["text"]}；{timing}；单位 人民币元；'
                              f'原行 {" | ".join(w["text"] for w in row)}；'
                              f'原页金额列 x={left:.2f}–{right:.2f}，行 y={row[0]["top"]:.2f}'})
    if len({r['name'] for r in candidates}) != len(candidates):
        raise InputError('PDF 本期财务报表项目重复，请核对原页，不静默选择或汇总。')
    basis = 'consolidated' if title.startswith('合并') else 'parent' if title.startswith('母公司') else 'unspecified'
    if doc.get('pdf_statement_basis') not in {None, basis}:
        raise InputError('PDF 中母公司/合并财务报表口径混杂，请分别选择原页，不混用金额。')
    names = re.findall(r'编制单位[:：]\s*(.+?)(?=\s+单位[:：]|\n|$)', header)
    for key, value in [('period', '' if as_of else years[0]), ('name', names[0] if len(names) == 1 else '')]:
        if value and doc['company'].get(key) not in {'', value}:
            raise InputError('PDF 财务报表企业或年度冲突，请分别选择原页。')
        if value:
            doc['company'][key] = value
    doc['pdf_statement_basis'] = basis
    doc.setdefault('pdf_statements', []).append({'page': page_no, 'title': title, 'basis': basis,
                                                'period': '' if as_of else years[0], 'as_of': as_of})
    doc['rows'].extend(candidates)
    return True  # Recognized structure with blank values must not use a flattened fallback.


def _pdf(data, doc, keys, selected_range=None):
    doc['pdf_parser_version'] = provenance.PDF_VERSION
    local = doc.setdefault('extraction', {}).setdefault('local', {})
    local.update(program=provenance.program('local'), status='running', started_at=provenance.now())
    doc.pop('pdf_income_basis', None)  # A changed page range must not retain the previous basis.
    doc.pop('pdf_statement_basis', None)
    doc.pop('pdf_statements', None)
    aliases = dict(zip(config.COMPANY_FIELDS, COMPANY_KEYS))
    aliases.update(SCOPE_FIELDS)
    aliases.update({"纳税人名称": "name", "编制单位": "name", "统一社会信用代码": "taxpayer_id", "税号": "taxpayer_id"})
    seen = set()
    with pdfplumber.open(BytesIO(data)) as pdf:
        if not 1 <= len(pdf.pages) <= material_formats.MAX_PDF_PAGES:
            raise InputError(f'PDF 须为 1–{material_formats.MAX_PDF_PAGES} 页。')
        doc["page_count"] = len(pdf.pages)
        selected_range = pdf_range(selected_range, doc['page_count'])
        if doc['page_count'] > material_formats.PDF_SEGMENT_PAGES or selected_range is not None:
            first, last = (selected_range['first'], selected_range['last']) if selected_range else (None, None)
            doc['pdf_selection'] = {'version': 'pdf-source-range-v1', 'source_sha256': sha256(data).hexdigest(),
                'total_pages': doc['page_count'], 'first': first, 'last': last, 'pending': selected_range is None,
                'max_segment_pages': material_formats.PDF_SEGMENT_PAGES,
                'segments': [{'first': i, 'last': min(i + material_formats.PDF_SEGMENT_PAGES - 1, doc['page_count'])}
                             for i in range(1, doc['page_count'] + 1, material_formats.PDF_SEGMENT_PAGES)],
                'unprocessed_ranges': ([{'first': 1, 'last': first - 1}] if first and first > 1 else []) +
                    ([{'first': last + 1, 'last': doc['page_count']}] if last and last < doc['page_count'] else [])}
            if selected_range is None:
                doc['pdf_selection']['unprocessed_ranges'] = [{'first': 1, 'last': doc['page_count']}]
                doc['summary'] = f"{doc['page_count']} 页 PDF 原件已保留；请选择同一企业、同一期间的原始页码片段。"
                local.update(status='awaiting_selection', finished_at=provenance.now())
                return
            doc['warnings'].append(f"本次只读取原件第 {first}–{last} 页，共 {doc['page_count']} 页；未选页不参与检测，不表示已完成整份材料解析。")
        first, last = (selected_range['first'], selected_range['last']) if selected_range else (1, doc['page_count'])
        for page_no in range(first, last + 1):
            page = pdf.pages[page_no - 1]
            text = (page.extract_text() or "")
            if len(text) > 30000:
                raise InputError(f"PDF 第 {page_no} 页文字过多。")
            doc["pages"].append({"page": page_no, "text": text})
            if not text.strip():
                doc["warnings"].append(f"第 {page_no} 页无可提取文字，可能为扫描页；可使用 AI 视觉提取候选值，仍须对照原件人工核对。")
                page.close()
                continue
            if _pdf_statement_columns(page, text, doc, keys, page_no):
                page.close()
                continue
            cumulative_titles = [re.sub(r'\s+', '', line) for line in text.splitlines()]
            if any(re.fullmatch(r'(?:\d+[、.．])?(?:合并|母公司)?年初(?:到|至)报告期末(?:利润表|现金流量表)',
                                title) for title in cumulative_titles):
                # A single displayed amount still belongs to this cumulative
                # statement, not a quarter inferred from the report cover.
                doc['warnings'].append(f'第 {page_no} 页含年初到报告期末累计财务表，本地暂未适配其期间和跨页金额栏；'
                    '本期发生额不能直接当作本季度金额，不使用通用单金额回退。请提供明确实际起止期间和金额栏的受支持原表，'
                    '或单独授权提取候选后对照原件核对；未读取的金额保持缺失。')
                page.close()
                continue
            for line in text.splitlines():
                for label, key in aliases.items():
                    match = re.fullmatch(r"\s*" + re.escape(label) + r"\s*[:：]\s*(.+?)\s*", line)
                    if match:
                        value = match[1]
                        if doc["company"].get(key) and doc["company"][key] != value:
                            raise InputError(f"PDF 中出现不一致的{label}，请拆分为同一企业、同一期间的材料。")
                        doc["company"][key] = value
            # Only extract an explicit single value, never choose between amount columns.
            prefix = next((p for p in ("利润表", "资产负债表", "现金流量表") if p in text[:300]), "")
            is_vat = "增值税" in text[:300]
            candidates = []
            for line in text.splitlines():
                match = re.fullmatch(r"\s*([^:：]+?)\s*[:：]\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*", line)
                if match:
                    candidates.append((match[1].strip(), match[2], line))
            for table in page.extract_tables():
                if not table or len(table[0]) != 2:
                    continue
                header = [_text(v).replace("\n", "") for v in table[0]]
                if header[0] not in {"项目", "指标"} or header[1] not in {"本期金额", "金额", "数值"}:
                    continue
                candidates.extend((_text(row[0]).replace("\n", ""), _text(row[1]), " | ".join(_text(v) for v in row)) for row in table[1:] if len(row) == 2)
            # Units must be reviewed; do not infer or scale amounts automatically.
            if "万元" in text or "千元" in text:
                doc["warnings"].append(f"第 {page_no} 页含非元单位；候选数值保留原数，提交前必须换算为元。")
            for label, value, excerpt in candidates:
                key = f"{prefix}.{label}" if prefix and f"{prefix}.{label}" in keys else f"增值税.{label}" if is_vat and f"增值税.{label}" in keys else label if label in keys else ""
                if key not in keys or (page_no, key, value) in seen:
                    continue
                try:
                    number = loader._number(value.replace(",", ""), f"PDF 第 {page_no} 页")
                except InputError:
                    continue
                if number is None:
                    continue
                seen.add((page_no, key, value))
                doc["rows"].append({"name": key, "value": str(number), "page": page_no, "detail": excerpt})
            page.close()
        if len(doc["rows"]) > 500:
            raise InputError("PDF 候选指标超过 500 项，请拆分材料。")
    doc["warnings"].insert(0, "PDF 结果是待核对候选值：请核实企业、期间、单位、金额列及指标口径；未识别项目可手工添加。金额统一为元，空白保持缺失。")
    doc["summary"] = f"{doc['page_count']} 页 PDF，识别到 {len(doc['rows'])} 项候选指标"
    local.update(status='succeeded', finished_at=provenance.now())
    if selected_range is not None:
        doc['summary'] = f"原件共 {doc['page_count']} 页，本次解析第 {first}–{last} 页，识别到 {len(doc['rows'])} 项候选指标"


def _unmapped_excel(data, doc):
    """Expose cell addresses for AI without bypassing standard-table validation."""
    known = {config.SHEET_ACCOUNTS, config.SHEET_COMPANY, config.SHEET_DECLARATION,
             config.SHEET_INCOME, config.SHEET_BALANCE, config.SHEET_CASHFLOW,
             config.SHEET_HISTORY, config.SHEET_INVOICES, config.SHEET_BANK,
             config.SHEET_BANK_ADJUSTMENTS, config.SHEET_HUMAN, config.SHEET_CONTRACTS,
             config.SHEET_FULFILLMENTS, config.SHEET_CONTRACT_LINKS, config.SHEET_SUPPLEMENT,
             related_graph.SHEET_SUBJECTS, related_graph.SHEET_RELATIONS, related_graph.SHEET_TRADES}
    wb = open_workbook(data, read_only=True)
    try:
        if known.intersection(wb.sheetnames):
            raise InputError("标准工作表校验失败，请先修正格式/重复项；AI 不覆盖原始输入错误。")
        for page_no, ws in enumerate(wb, 1):
            if ws.max_row > 2000 or ws.max_column > 50:
                raise InputError("AI Excel 单表最多 2000 行、50 列，请拆分。")
            lines = []
            for row in ws:
                cells = []
                for cell in row:
                    if cell.data_type == "f":
                        raise InputError("AI Excel 含未计算公式，请先导出固定数值快照。")
                    if cell.value is not None:
                        cells.append(f"{cell.coordinate}={cell.value}")
                if cells:
                    lines.append(" | ".join(cells))
            text = "\n".join(lines)
            if len(text) > 60000:
                raise InputError("AI Excel 单表内容过长，请拆分。")
            doc["pages"].append({"page": page_no, "text": text, "label": f"工作表：{ws.title}"})
        doc["page_count"] = len(doc["pages"])
    finally:
        wb.close()


def preview(files, keys, extractor=None, *, allow_incomplete_company=False, capture_standard=False):
    docs = []
    parser = provenance.program('local')
    for index, (name, data, format_check) in enumerate(material_format_guard.check(files, expand=True, wait=True)):
        suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        doc = {"id": str(index), "name": name, "fingerprint": sha256(data).hexdigest()[:16],
               "sha256": sha256(data).hexdigest(),
               "format_check": format_check,
               "kind": suffix, "company": dict.fromkeys(COMPANY_KEYS, ""),
               "accounts": [], "declarations": {}, "rows": [], "period_series": [], "invoices": [],
                "bank_transactions": [], "bank_adjustments": [],
                "human_records": [], "contracts": [], "fulfillments": [], "contract_links": [],
                "related_graph": None,
               "pages": [], "warnings": [], "error": "",
               'security': {'version': material_security.VERSION, 'findings': []},
               "extraction": {"method": "local", 'local': {
                   'program': parser, 'status': 'running', 'started_at': provenance.now()}}}
        local = doc['extraction']['local']
        try:
            if suffix in {"xlsx", "xls", "csv", "tsv"}:
                parsed = _table_workbook(name, data, suffix) if suffix in {"csv", "tsv"} else data
                try:
                    _excel(parsed, doc, allow_incomplete_company=allow_incomplete_company, capture_standard=capture_standard, keys=keys)
                except InputError:
                    local.update(status='failed', finished_at=provenance.now())
                    if extractor is None:
                        raise
                    _unmapped_excel(parsed, doc)
                    local['fallback'] = 'unmapped_workbook_text'
                    extractor.enrich(doc, data)
            elif name.lower().endswith(".xml"):
                _xml(data, doc)
            elif suffix == "pdf" or suffix in material_formats.IMAGE_FORMATS:
                if suffix == "pdf":
                    _pdf(data, doc, keys)
                else:
                    _image(data, doc)
                local.update(status='succeeded', finished_at=provenance.now())
                if doc.get('pdf_selection', {}).get('pending'):
                    local['status'] = 'awaiting_selection'
                    if extractor is not None:
                        doc['warnings'].append('长 PDF 未发送至模型；选择片段后保存只做本地解析，须人工核对。')
                elif extractor is not None:
                    try:
                        extractor.enrich(doc, data)
                    except ExtractionError as exc:
                        doc['extraction']['method'] = 'ai_failed'
                        doc["warnings"].insert(0, f"AI 未完成：{exc} 当前仅显示本地解析候选，需人工核对或重新上传。")
            else:
                raise InputError("不支持此文件类型；请选择支持的账表、票据或扫描图片（也可放入 ZIP）。")
        except InputError as exc:
            doc["error"] = str(exc)
            if local['status'] == 'running':
                local['status'] = 'failed'
        except ExtractionError as exc:
            doc["error"] = f"AI 提取失败：{exc}"
            doc['extraction']['method'] = 'ai_failed'
        except Exception:
            doc["error"] = "文件无法解析；请检查是否损坏、加密或格式不符。"
            if local['status'] == 'running':
                local['status'] = 'failed'
        finally:
            if local['status'] == 'running':
                local['status'] = 'succeeded'
            local.setdefault('finished_at', provenance.now())
        material_security.scan_pages(doc)
        docs.append(doc)
    return docs


def _merge_value(target, key, value, location):
    if key in target and target[key] != value:
        raise InputError(f"{location}：{key} 存在冲突值；请核对后选择一份正确材料，不能自动相加或覆盖。")
    target[key] = value


def build_dataset(documents, selections, company_override, keys):
    """Validate edits server-side and merge same-scope evidence without summation."""
    company_data, accounts, declarations, metrics, period_series, invoices = {}, {}, {}, {}, {}, {}
    bank_transactions, bank_adjustments, human_records, unkeyed_bank_docs = {}, {}, {}, set()
    contracts, fulfillments, contract_links = {}, {}, {}
    graph = None
    statement_basis = None
    account_sources, declaration_sources = {}, {}
    for doc in documents:
        selection = selections[doc["id"]]
        if doc["error"]:
            raise InputError(f"{doc['name']}：{doc['error']}")
        blocked = material_security.blockers(doc, selection, selection.get('evidence_scope'),
                                            require_review='evidence_scope' in selection)
        if blocked:
            raise InputError(f"{doc['name']}：{blocked[0]['message']} 请对照原件核对或排除材料。")
        basis = financial_import.statement_basis(doc)
        if basis:
            if statement_basis not in {None, basis}:
                raise InputError('母公司/合并或未明确口径的财务报表不能跨材料混用，请排除不同口径材料。')
            statement_basis = basis
        company = dict(doc["company"])
        edits = selection.get("company", {})
        if not isinstance(edits, dict):
            raise InputError("企业信息格式错误。")
        for key in COMPANY_KEYS:
            val = _text(edits.get(key, company[key]))
            if len(val) > 200:
                raise InputError("企业信息过长。")
            editable = doc["kind"] == "pdf" or doc.get("review_required", False)
            if not editable and company[key] and val != company[key]:
                raise InputError("Excel 中已有企业信息不能在核对页改写。")
            company[key] = val or _text(company_override.get(key, ""))
            if company[key]:
                _merge_value(company_data, key, company[key], "企业或期间不一致")
        source = f"{doc['name']} [SHA256:{doc['fingerprint']}]"
        for sheet in doc.get('import_mapping', {}).get('sheets', []):
            if (not sheet.get('excluded') and sheet.get('as_of') and
                    sheet['as_of'] != periods.parse_period(company['period'], '检测期间').end.isoformat()):
                raise InputError('原文财务报表时点与检测期间终点不同，补填不能消除原文冲突。')
        for statement in doc.get('pdf_statements', []):
            if (doc.get('kind') != 'history' and statement.get('as_of') and
                    statement['as_of'] != periods.parse_period(company['period'], '检测期间').end.isoformat()):
                raise InputError('PDF 资产负债表时点与检测期间终点不同，补填不能消除原文冲突。')
        raw_graph = doc.get("related_graph")
        if raw_graph is not None:
            if graph is not None:
                raise InputError("关联方图材料一次只能选择一份完整工作簿；请先合并并复核主体、关系和交易")
            graph = RelatedGraph(
                [RelatedSubject(**{**item, "source": f"{source} / {item['source']}"})
                 for item in raw_graph["subjects"]],
                [RelatedRelation(**{**item, "source": f"{source} / {item['source']}"})
                 for item in raw_graph["relations"]],
                [RelatedTrade(**{**item, "amount": Decimal(item["amount"]),
                                 "source": f"{source} / {item['source']}"})
                 for item in raw_graph["trades"]],
            )
        for raw in doc.get("invoices", []):
            if not isinstance(raw, dict):
                raise InputError(f"{source}：发票记录格式错误")
            number = loader._invoice_number(raw.get("number"), f"{source} 发票号码")
            issued_on = loader._invoice_date(raw.get("issued_on"), f"{source} 发票「{number}」")
            kind = _text(raw.get("kind"))
            status = _text(raw.get("status"))
            if kind not in {"", "销项", "采购"} or status not in {"正常", "红字", "作废"}:
                raise InputError(f"{source}：发票「{number}」方向或状态无效")
            amount = loader._number(raw.get("amount"), f"{source} 发票「{number}」不含税金额")
            tax = loader._number(raw.get("tax"), f"{source} 发票「{number}」税额")
            deductible_tax = loader._number(raw.get("deductible_tax"), f"{source} 发票「{number}」抵扣税额")
            if amount is None or tax is None or amount < 0 or tax < 0:
                raise InputError(f"{source}：发票「{number}」标准化金额须为非负数")
            if deductible_tax is not None and (deductible_tax < 0 or deductible_tax > tax):
                raise InputError(f"{source}：发票「{number}」抵扣税额须在 0 与发票税额之间")
            if number in invoices:
                raise InputError(f"发票号码重复「{number}」；请先去重，禁止跨文件重复计入")
            invoices[number] = loader._Invoice(
                number, issued_on, kind, amount, tax, status, deductible_tax,
                _text(raw.get("deductible_period")), _text(raw.get("seller_id")), _text(raw.get("buyer_id")),
                f"{source} / {_text(raw.get('source'))}", _text(raw.get("detail")),
            )
        for raw in doc.get("bank_transactions", []):
            if not isinstance(raw, dict):
                raise InputError(f"{source}：银行流水记录格式错误")
            explicit_id = raw.get("explicit_id") is True
            transaction_id = loader._bank_identifier(raw.get("transaction_id"), f"{source} 银行流水号")
            transacted_on = loader._bank_date(raw.get("transacted_on"), f"{source} 银行流水「{transaction_id}」")
            income = loader._number(raw.get("income"), f"{source} 银行流水「{transaction_id}」收入金额")
            expense = loader._number(raw.get("expense"), f"{source} 银行流水「{transaction_id}」支出金额")
            if income is None or expense is None or income < 0 or expense < 0 or (income > 0) == (expense > 0):
                raise InputError(f"{source}：银行流水「{transaction_id}」收支金额无效")
            currency = loader._bank_currency(raw.get("currency"), f"{source} 银行流水「{transaction_id}」币种")
            if explicit_id:
                key = "ID:" + transaction_id
            else:
                unkeyed_bank_docs.add(doc["id"])
                key = f"LEGACY:{doc['id']}:{_text(raw.get('key')) or transaction_id}"
            if key in bank_transactions:
                raise InputError(f"银行流水号重复「{transaction_id}」；请先去重，禁止跨文件重复计入")
            bank_transactions[key] = loader._BankTransaction(
                key, transaction_id, transacted_on, _text(raw.get("summary")), income, expense,
                _text(raw.get("category")), currency, _text(raw.get("counterparty")),
                _text(raw.get("account_hint")), explicit_id,
                f"{source} / {_text(raw.get('source'))}", _text(raw.get("detail")),
            )
        for raw in doc.get("bank_adjustments", []):
            if not isinstance(raw, dict):
                raise InputError(f"{source}：银行调节记录格式错误")
            number = loader._bank_identifier(raw.get("number"), f"{source} 银行调节编号")
            if number in bank_adjustments:
                raise InputError(f"银行调节编号重复「{number}」；请先去重")
            transaction_id = (
                loader._bank_identifier(raw.get("transaction_id"), f"{source} 银行调节「{number}」流水号")
                if _text(raw.get("transaction_id")) else ""
            )
            category = _text(raw.get("category"))
            if category not in loader._BANK_LINKED_CATEGORIES | loader._BANK_STANDALONE_CATEGORIES:
                raise InputError(f"{source}：银行调节「{number}」分类无效")
            period = _text(raw.get("period"))
            periods.parse_period(period, f"{source} 银行调节「{number}」权责所属期")
            recognized = loader._number(raw.get("recognized_amount"), f"{source} 银行调节「{number}」本期不含税收入")
            amount = loader._number(raw.get("adjustment_amount"), f"{source} 银行调节「{number}」调节金额")
            reviewed = raw.get("reviewed")
            if type(reviewed) is not bool:
                raise InputError(f"{source}：银行调节「{number}」复核状态无效")
            bank_adjustments[number] = loader._BankAdjustment(
                number, transaction_id, category, period, recognized, amount, _text(raw.get("direction")),
                _text(raw.get("workpaper")), reviewed, _text(raw.get("detail")),
                f"{source} / {_text(raw.get('source'))}",
            )
        for raw in doc.get("human_records", []):
            if not isinstance(raw, dict):
                raise InputError(f"{source}：人力记录格式错误")
            kind = _text(raw.get("kind"))
            person_key = _text(raw.get("person_key"))
            month = loader._human_month(raw.get("month"), f"{source} 人力记录所属月")
            active = raw.get("active")
            if kind not in {"个税", "社保", "公积金"} or not re.fullmatch(r"[0-9a-f]{64}", person_key):
                raise InputError(f"{source}：人力记录类型或人员标识无效")
            if type(active) is not bool:
                raise InputError(f"{source}：人力记录状态无效")
            amount = loader._number(raw.get("amount"), f"{source} 人力记录金额")
            if amount is not None and amount < 0:
                raise InputError(f"{source}：人力记录金额不能为负数")
            key = (kind, person_key, month)
            if key in human_records:
                raise InputError("同一人员、记录类型和所属月跨文件重复；请先去重")
            human_records[key] = loader._HumanRecord(
                kind, person_key, month, active, amount,
                f"{source} / {_text(raw.get('source'))}",
            )
        for raw in doc.get("contracts", []):
            if not isinstance(raw, dict):
                raise InputError(f"{source}：合同记录格式错误")
            number = loader._bank_identifier(raw.get("number"), f"{source} 合同编号")
            if number in contracts:
                raise InputError(f"合同编号重复「{number}」；请先去重")
            kind = _text(raw.get("kind"))
            counterparty = _text(raw.get("counterparty"))
            if kind not in {"销售", "采购"} or not counterparty or len(counterparty) > 200:
                raise InputError(f"{source}：合同「{number}」类型或对方名称无效")
            signed_on = loader._contract_date(raw.get("signed_on"), f"{source} 合同「{number}」签订日期")
            amount = loader._number(raw.get("amount"), f"{source} 合同「{number}」含税金额")
            if amount is None or amount <= 0:
                raise InputError(f"{source}：合同「{number}」含税金额必须为正数")
            performance_start = loader._contract_date(
                raw.get("performance_start"), f"{source} 合同「{number}」履约起始日"
            )
            performance_end = loader._contract_date(
                raw.get("performance_end"), f"{source} 合同「{number}」履约结束日"
            )
            if performance_start > performance_end:
                raise InputError(f"{source}：合同「{number}」履约起始日不能晚于结束日")
            reviewed = raw.get("reviewed")
            if type(reviewed) is not bool:
                raise InputError(f"{source}：合同「{number}」复核状态无效")
            workpaper, detail = loader._contract_note(
                raw.get("workpaper"), raw.get("detail"), reviewed, f"{source} 合同「{number}」"
            )
            contracts[number] = loader._Contract(
                number, kind, counterparty, signed_on, amount, performance_start, performance_end,
                reviewed, workpaper, detail, f"{source} / {_text(raw.get('source'))}",
            )
        for raw in doc.get("fulfillments", []):
            if not isinstance(raw, dict):
                raise InputError(f"{source}：履约记录格式错误")
            number = loader._bank_identifier(raw.get("number"), f"{source} 履约单据号")
            if number in fulfillments:
                raise InputError(f"履约单据号重复「{number}」；请先去重")
            contract_number = loader._bank_identifier(
                raw.get("contract_number"), f"{source} 履约单据「{number}」合同编号"
            )
            fulfilled_on = loader._contract_date(
                raw.get("fulfilled_on"), f"{source} 履约单据「{number}」履约日期"
            )
            kind = _text(raw.get("kind"))
            amount = loader._number(raw.get("amount"), f"{source} 履约单据「{number}」含税金额")
            if kind not in loader._FULFILLMENT_KINDS or amount is None or amount <= 0:
                raise InputError(f"{source}：履约单据「{number}」类型或金额无效")
            reviewed = raw.get("reviewed")
            if type(reviewed) is not bool:
                raise InputError(f"{source}：履约单据「{number}」复核状态无效")
            workpaper, detail = loader._contract_note(
                raw.get("workpaper"), raw.get("detail"), reviewed, f"{source} 履约单据「{number}」"
            )
            fulfillments[number] = loader._Fulfillment(
                number, contract_number, fulfilled_on, kind, amount, reviewed, workpaper, detail,
                f"{source} / {_text(raw.get('source'))}",
            )
        for raw in doc.get("contract_links", []):
            if not isinstance(raw, dict):
                raise InputError(f"{source}：四流勾稽记录格式错误")
            number = loader._bank_identifier(raw.get("number"), f"{source} 四流勾稽编号")
            if number in contract_links:
                raise InputError(f"四流勾稽编号重复「{number}」；请先去重")
            contract_number = loader._bank_identifier(
                raw.get("contract_number"), f"{source} 四流勾稽「{number}」合同编号"
            )
            invoice_number = (
                loader._invoice_number(raw.get("invoice_number"), f"{source} 四流勾稽「{number}」发票号码")
                if _text(raw.get("invoice_number")) else ""
            )
            transaction_id = (
                loader._bank_identifier(raw.get("transaction_id"), f"{source} 四流勾稽「{number}」银行流水号")
                if _text(raw.get("transaction_id")) else ""
            )
            fulfillment_number = (
                loader._bank_identifier(raw.get("fulfillment_number"), f"{source} 四流勾稽「{number}」履约单据号")
                if _text(raw.get("fulfillment_number")) else ""
            )
            if not any((invoice_number, transaction_id, fulfillment_number)):
                raise InputError(f"{source}：四流勾稽「{number}」至少关联一项外部流转证据")
            amount = loader._number(raw.get("amount"), f"{source} 四流勾稽「{number}」含税金额")
            if amount is None or amount <= 0:
                raise InputError(f"{source}：四流勾稽「{number}」含税金额必须为正数")
            reviewed = raw.get("reviewed")
            if type(reviewed) is not bool:
                raise InputError(f"{source}：四流勾稽「{number}」复核状态无效")
            workpaper, detail = loader._contract_note(
                raw.get("workpaper"), raw.get("detail"), reviewed, f"{source} 四流勾稽「{number}」"
            )
            contract_links[number] = loader._ContractLink(
                number, contract_number, invoice_number, transaction_id, fulfillment_number,
                amount, reviewed, workpaper, detail, f"{source} / {_text(raw.get('source'))}",
            )
        for raw in doc.get("period_series", []):
            key = _text(raw.get("name"))
            if key not in config.PERIOD_SERIES:
                raise InputError(f"{source}：不支持的期间序列指标「{key}」")
            period = periods.parse_period(raw.get("period"), f"{source} 期间序列")
            value = loader._number(raw.get("value"), f"{source} 期间序列")
            if value is None:
                continue
            metric = Metric(
                key,
                value,
                f"{source} / {_text(raw.get('source'))}",
                _text(raw.get("detail")),
            )
            values = period_series.setdefault(key, {})
            existing = values.get(period.key)
            if existing and existing.metric.value != value:
                raise InputError(f"期间序列「{key}」在「{period.label}」存在冲突值，请核对后重选材料。")
            if existing:
                metric.source = existing.metric.source + "；" + metric.source
                metric.detail = existing.metric.detail or metric.detail
                period = existing.period
            values[period.key] = loader._PeriodValue(period, metric, existing.row if existing else 0)
        for row in doc["accounts"]:
            account = Account(row["code"], row["name"], *(loader._number(row[k], source) for k in ("opening", "debit", "credit", "closing")))
            _merge_value(accounts, account.code, account, source)
            for side in ('opening', 'debit', 'credit', 'closing'):
                location = doc.get('account_cell_sources', {}).get(account.code, {}).get(side, '')
                account_sources.setdefault((account.code, side), []).append(source + (' / ' + location if location else ''))
        for key, val in doc["declarations"].items():
            _merge_value(declarations, key, loader._number(val, source), source)
            location = doc.get('declaration_cell_sources', {}).get(key, '')
            declaration_sources.setdefault(key, []).append(source + (' / ' + location if location else ''))
        rows = doc["rows"]
        if editable:
            if selection.get("reviewed") is not True:
                raise InputError(f"{doc['name']}：请先核对提取结果的期间、单位和金额口径。")
            rows = selection.get("rows", rows)
            if not isinstance(rows, list) or len(rows) > 500:
                raise InputError("PDF 指标列表格式错误或超过 500 项。")
        for row in rows:
            if not isinstance(row, dict):
                raise InputError("指标行格式错误。")
            key = _text(row.get("name"))
            value = loader._number(row.get("value"), source)
            if value is None:
                continue
            if editable and key not in keys:
                raise InputError(f"{key}：请选择规则支持的标准指标。")
            if key.startswith("人力.") and key.endswith("人数") and (value < 0 or value != value.to_integral_value()):
                raise InputError("人数必须为非负整数。")
            detail = _text(row.get("detail"))
            if len(detail) > 2000:
                raise InputError("口径说明超过 2000 字。")
            if editable:
                page = row.get("page")
                selected_pdf = doc.get('pdf_selection')
                if (type(page) is not int or not 1 <= page <= doc["page_count"] or not detail or
                        (selected_pdf and (selected_pdf['pending'] or not selected_pdf['first'] <= page <= selected_pdf['last']))):
                    raise InputError("PDF 指标须填写有效页码和口径说明。")
                origin = f"PDF 第 {page} 页" if doc["kind"] == "pdf" else doc["pages"][page - 1]["label"]
                row_source = f"{source} / {origin}（人工核对/录入）"
                if selected_pdf:
                    row_source += f"；本次仅解析原件第 {selected_pdf['first']}–{selected_pdf['last']} 页/共 {selected_pdf['total_pages']} 页"
                original = [r for r in doc["rows"] if r["name"] == key and r["page"] == page]
                ai_source = doc.get("extraction", {}).get("method") == "ai"
                original_values = [_text(r.get('ai_raw_value') if ai_source else r.get('value')) for r in original]
                original_values = [value for value in original_values if value]
                missing = "缺失，人工补录" if original else "无，人工补录"
                detail += "；识别候选原值：" + ("、".join(original_values) or missing)
                if ai_source:
                    row_source += f"；AI 提取模型 {doc['extraction']['model']}"
                    detail += "；AI 原始证据：" + "；".join(f"{r.get('ai_raw_value')} {r.get('ai_unit')}；{r.get('ai_quote')}" for r in original)
            else:
                row_source = f"{source} / {row['source']}"
            if key in metrics:
                if doc["kind"] != "pdf" and row.get("source", "").startswith(("发票明细", "银行流水")):
                    raise InputError(f"指标「{key}」来自多份明细汇总；请先合并原始明细并去重，再上传一份完整清单。")
                if metrics[key].value != value:
                    raise InputError(f"指标「{key}」在材料中存在冲突值，请核对后重选材料。")
                metrics[key].source += "；" + row_source
            else:
                metrics[key] = Metric(key, value, row_source, detail)
    missing = [label for key, label in zip(COMPANY_KEYS, config.COMPANY_FIELDS) if not company_data.get(key)]
    if missing:
        raise InputError("请补充企业信息：" + "、".join(missing))
    derived = loader._build_metrics(list(accounts.values()), declarations)
    for key, metric in derived.items():
        if key in config.ACCOUNT_MAP:
            sources = [s for code in config.ACCOUNT_MAP[key]["accounts"]
                       for s in account_sources[(code, config.ACCOUNT_MAP[key]['side'])]]
        else:
            sources = declaration_sources[config.DECLARATION_ITEMS[key]]
        metric.source = "；".join(dict.fromkeys(sources)) + " / " + metric.source
        if key in metrics:
            if metric.value != metrics[key].value:
                raise InputError(f"指标「{key}」与原始账表计算值冲突，请核对。")
            metric.source += "；" + metrics[key].source
        metrics[key] = metric
    company = Company(*(company_data[k] for k in COMPANY_KEYS))
    for key, metric in loader._invoice_metrics(company, list(invoices.values())).items():
        if key in metrics:
            if metrics[key].value != metric.value:
                raise InputError(f"指标「{key}」与发票明细计算值冲突，请核对。")
            metric.source += "；" + metrics[key].source
        metrics[key] = metric
    if len(unkeyed_bank_docs) > 1:
        raise InputError("多份银行流水合并时，每笔流水必须提供银行唯一编号或交易流水号，不能仅按行号去重")
    for key, metric in loader._bank_metrics(
        company, list(bank_transactions.values()), list(bank_adjustments.values())
    ).items():
        if key in metrics:
            if metrics[key].value != metric.value:
                raise InputError(f"指标「{key}」与银行流水/调节底稿计算值冲突，请核对。")
            metric.source += "；" + metrics[key].source
        metrics[key] = metric
    for key, metric in loader._human_metrics(company, list(human_records.values())).items():
        if key in metrics:
            if metrics[key].value != metric.value:
                raise InputError(f"指标「{key}」与人力记录计算值冲突，请核对。")
            metric.source += "；" + metrics[key].source
        metrics[key] = metric
    for key, metric in loader._contract_metrics(
        company,
        list(contracts.values()),
        list(fulfillments.values()),
        list(contract_links.values()),
        list(invoices.values()),
        list(bank_transactions.values()),
    ).items():
        if key in metrics:
            if metrics[key].value != metric.value:
                raise InputError(f"指标「{key}」与合同四流勾稽计算值冲突，请核对。")
            metric.source += "；" + metrics[key].source
        metrics[key] = metric
    periods.derive_period_metrics(company, metrics, period_series)
    if not metrics and graph is None:
        raise InputError("没有可执行核对的指标；请补充至少一个有效指标，缺失值不会当成零。")
    return Dataset(company, list(accounts.values()), declarations, metrics, graph,
                   sources=list(dict.fromkeys(doc["name"] for doc in documents)))
