"""Evidence gates for untrusted materials. Findings supplement, never replace, review."""
import base64
from copy import deepcopy
import re
import unicodedata

from . import material_provenance as provenance
from .input_errors import InputError

VERSION = 'material-security-v1'
MAX_TEXT = 2_000_000
PATTERNS = (
    ('instruction_override', r'(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|system|above)\s+(?:instructions?|prompts?|rules?)|(?:忽略|无视|忘记|覆盖).{0,12}(?:之前|以上|系统|原有).{0,8}(?:指令|提示|规则)'),
    ('forged_role', r'<\|(?:im_start|start_header_id)\|>\s*(?:system|developer)|</?(?:system|developer)(?:\s[^>]{0,100})?>|(?:^|\n)\s*(?:system|developer)\s*:\s*|(?:你现在是|系统指令|开发者指令|最高优先级指令)'),
    ('result_tampering', r'(?:将|把|修改|改写|设置).{0,45}(?:金额|收入|税额|提取结果|检测结果).{0,25}(?:改成|改为|设为|替换为|设置为)|(?:skip|bypass).{0,15}(?:validation|review|verification)|(?:跳过|绕过|关闭).{0,15}(?:核对|复核|校验|门禁)'),
    ('data_exfiltration', r'(?:发送|上传|泄露|导出).{0,40}(?:API.?KEY|密钥|密码|其他用户|其他任务|系统提示)|(?:send|upload|reveal|exfiltrate).{0,40}(?:api.?key|secret|password|system prompt|other user)'),
)


def detect(text, *, location='', origin='source'):
    """Normalize only the detection view; keep the original evidence immutable."""
    view = unicodedata.normalize('NFKC', str(text))
    view = ''.join(c for c in view if unicodedata.category(c) != 'Cf')
    findings = []
    if len(view) > MAX_TEXT:
        findings.append({'code': 'scan_limit', 'location': location, 'origin': origin,
                         'quote': '', 'message': '安全检查文本超限，未完成完整检查。'})
        view = view[:MAX_TEXT]
    variants = [('plain', view)]
    # One bounded decoding layer catches common obfuscation without recursive expansion.
    for token in re.findall(r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,2048}={0,2}(?![A-Za-z0-9+/])', view)[:20]:
        try:
            decoded = base64.b64decode(token, validate=True).decode('utf-8')
        except (ValueError, UnicodeError):
            continue
        variants.append(('base64', decoded))
    for encoding, candidate in variants:
        for code, pattern in PATTERNS:
            match = re.search(pattern, candidate, re.I)
            if match:
                findings.append({'code': code, 'location': location, 'origin': origin,
                    'encoding': encoding, 'quote': candidate[max(0, match.start()-30):match.end()+80][:240],
                    'message': '发现可能影响提取或校验的指令内容，须排除或替换此材料。'})
    return findings


def add_findings(doc, findings):
    state = doc.setdefault('security', {'version': VERSION, 'findings': []})
    state['version'] = VERSION
    for finding in findings:
        if finding not in state['findings']:
            state['findings'].append(finding)
    state['findings'] = state['findings'][:100]


def scan_pages(doc):
    for page in doc.get('pages', []):
        add_findings(doc, detect(page.get('text', ''), location=str(page.get('page', ''))))
    return doc.get('security', {}).get('findings', [])


def scan_workbook(book, doc):
    total = 0
    for sheet in book:
        # Normal workbooks use populated cells; read-only fallback is bounded by open_workbook.
        cells = sheet._cells.values() if hasattr(sheet, '_cells') else (
            cell for row in sheet.iter_rows() for cell in row)
        for cell in cells:
            parts = [str(cell.value)] if cell.value is not None else []
            if getattr(cell, 'comment', None):
                parts.append(cell.comment.text)
            text = '\n'.join(parts)
            total += len(text)
            if total > MAX_TEXT:
                add_findings(doc, [{'code': 'scan_limit', 'location': sheet.title,
                    'origin': 'source', 'quote': '', 'message': '安全检查文本超限。'}])
                return
            add_findings(doc, detect(text, location=f'{sheet.title}!{cell.coordinate}'))


def _rows(doc, selection):
    if not isinstance(selection.get('rows', doc.get('rows', [])), list):
        raise InputError('指标编辑须为列表。')
    return [{k: row.get(k, '') for k in ('name', 'value', 'page', 'detail')}
            for row in selection.get('rows', doc.get('rows', [])) if isinstance(row, dict)]


def review_requirements(doc, selection, scope=None):
    """IDs bind each acknowledgement to original, candidate values and batch scope."""
    mapped = doc.get('import_mapping')
    selected_pdf = doc.get('pdf_selection')
    if doc.get('extraction', {}).get('method') != 'ai' and not mapped and not selected_pdf and not doc.get('pdf_statements'):
        return []
    reviews = selection.get('evidence_reviews', [])
    formulas = (mapped or {}).get('formula_cells', [])
    if not isinstance(reviews, list) or len(reviews) > 503 + len(formulas) or any(
            not isinstance(x, str) or not re.fullmatch(r'[0-9a-f]{64}', x) for x in reviews):
        raise InputError('逐项证据复核编号格式无效。')
    scope = {k: str(v or '').strip() for k, v in (scope or doc.get('company', {})).items()}
    if 'taxpayer_id' in scope:
        scope['taxpayer_id'] = re.sub(r'\s+', '', scope['taxpayer_id']).upper()
    context = {'version': VERSION, 'sha256': doc.get('sha256', doc.get('fingerprint')),
               'file_id': doc['id'], 'rows': _rows(doc, selection),
               'scope': scope, 'purpose': selection.get('purpose', 'current')}
    if doc.get('kind') == 'pdf':
        context['pdf_parser'] = {'version': doc.get('pdf_parser_version'),
                                'program': doc.get('extraction', {}).get('local', {}).get('program')}
    observations = doc.get('extraction', {}).get('period_observations', [])
    if observations:
        context['period_observations'] = observations
    statements = doc.get('pdf_statements', [])
    if statements:
        context['pdf_statements'] = statements
    if mapped:
        context.pop('rows', None)
        context['mapping'] = mapped
        context['standard_edits'] = selection.get('standard_edits', {})
        context['import_options'] = selection.get('import_options', {})
    if selected_pdf:
        context['pdf_selection'] = selected_pdf
    items = [{'key': 'scope', 'label': '企业归属与实际期间',
              'reason': '对照原件核实企业名称、税号及本期/历史期间；用户补填不是原件证明。'}]
    if observations:
        items.append({'key': 'report_dates', 'label': '模型期间候选、报表时点与完整业务期间',
            'reason': '未采用的模型期间/时点候选：' + '、'.join(str(item['value']) for item in observations) +
                '；未作为完整期间采用。须对照原页与期间说明，不能从单个日期猜测月、季或年度。'})
    if selected_pdf and not selected_pdf.get('pending'):
        items.append({'key': 'pdf_range', 'label': 'PDF 原始页码与未读取范围',
            'reason': f"原件共 {selected_pdf['total_pages']} 页；本次选择 {selected_pdf['first']}–{selected_pdf['last']} 页。未选页不参与检测；须对照片段核实材料归属、单位、期间和金额。"})
    if statements:
        items.append({'key': 'pdf_statements', 'label': 'PDF 报表主体口径、时点与期间',
            'reason': '；'.join(f"原页 {s['page']} · {s['title']} · " +
                (f"报表时点 {s['as_of']}（不是完整期间）" if s.get('as_of') else f"原文年度 {s['period']}")
                for s in statements) + '；对照原页核实母公司/合并范围，不与其他口径混用。'})
    if mapped:
        items.extend({'key': 'mapping:' + str(index), 'label': '财务导出映射 · ' + sheet['sheet'],
            'reason': ('本工作表已排除，不参与本次检测；核实排除范围与原件归属。' if sheet.get('excluded') else
                '核对项目/科目与金额列、原单位 ' + (sheet['unit'] or '未指定') + '、实际期间 ' +
                (sheet['period'] or '未提供') + '；主体口径 ' + sheet.get('statement_basis', '未明确') +
                '；原科目不重编号，未映射项目不作已检查。')}
            for index, sheet in enumerate(mapped['sheets']))
        items.extend({'key': 'formula:' + cell['id'], 'label': '公式金额 · ' + cell['id'],
            'reason': '原公式 ' + cell['formula'] + '；文件缓存 ' + (cell.get('cached_raw') or '缺失') +
                ' 仅为候选，未验证新鲜度。核对原件及计算口径；本次使用 ' +
                str(selection.get('standard_edits', {}).get(cell['id']) if
                    selection.get('standard_edits', {}).get(cell['id']) is not None else '留空（缺失，不是零）') +
                '，不执行公式、不改原件。'} for cell in formulas)
    for index, row in enumerate(doc.get('rows', []) if doc.get('extraction', {}).get('method') == 'ai' else []):
        items.append({'key': f'row:{index}', 'label': f"第 {row.get('page', '?')} 页 · {row.get('name', '')}",
                      'reason': '；'.join(row.get('ai_issues', [])) or '核对表名、栏次、原始金额、单位及实际期间。'})
    # Large formula exports bind every amount to one immutable context digest,
    # avoiding repeated serialization of the entire source mapping per cell.
    context_digest = provenance.digest(context) if formulas else None
    reviewed = set(reviews)
    output = []
    for item in items:
        identity = provenance.digest({'context_digest': context_digest, 'item': item} if formulas else {**context, 'item': item})
        output.append({**item, 'id': identity, 'reviewed': identity in reviewed})
    return output


def blockers(doc, selection, scope=None, *, require_review=True):
    if selection.get('purpose') == 'excluded':
        return []
    if provenance.pdf_reanalysis_required(doc):
        return [{'code': 'pdf_reanalysis_required', 'file_id': doc['id'],
                 'message': 'PDF 适配已更新或旧解析状态不明，须重新核对留存原件。',
                 'impact': '旧候选金额、编辑、复核和可确认状态不能批准新版解析。',
                 'required': '保存并重新分析，从完整原字节重读已选片段；或明确排除此文件。不会自动发送模型。'}]
    from . import financial_import
    if financial_import.reanalysis_required(doc):
        return [{'code': 'financial_reanalysis_required', 'file_id': doc['id'],
                 'message': '财务导出适配已更新，旧解析结果须重新核对原件。',
                 'impact': '旧的可确认状态、数值修正及复核不能批准新版解析。',
                 'required': '保存并重新分析，从留存原件重新解析后复核；或明确排除此文件。'}]
    if doc.get('kind') in {'xlsx', 'xls', 'xml'} and doc.get('security', {}).get('version') != VERSION:
        return [{'code': 'security_rescan_required', 'file_id': doc['id'],
                 'message': '旧版材料尚未完成安全检查。', 'impact': '旧的可确认状态不能直接放行。',
                 'required': '保存并重新分析以检查原件，或明确排除后补传。'}]
    # A readiness check must not change the frozen payload used by confirmation hashes.
    scanned = dict(doc)
    if 'security' in scanned:
        scanned['security'] = deepcopy(scanned['security'])
    scan_pages(scanned)
    findings = list(scanned.get('security', {}).get('findings', []))
    # Client edits are still data and cannot become another instruction channel.
    findings.extend(detect(str(selection.get('rows', '')), origin='user_edit'))
    findings.extend(detect(str(scope or {}), origin='user_edit'))
    if findings:
        return [{'code': 'suspicious_material', 'file_id': doc['id'],
                 'message': '材料或修改内容含可疑指令，已隔离，不能参与检测。',
                 'impact': '修改金额或勾选复核不能解除此隔离。',
                 'required': '查看可疑内容，明确排除此文件或补传可信原件。'}]
    if doc.get('pdf_selection', {}).get('pending'):
        return [{'code': 'pdf_range_required', 'file_id': doc['id'],
            'message': '长 PDF 尚未明确原始页码片段。', 'impact': '完整原件已保留，但不能把未读取页当作已检查。',
            'required': '选择同一企业、同一期间的原件起止页码，保存重新分析后复核；也可明确移除此材料。'}]
    pending_mapping = doc.get('import_mapping', {}).get('pending', [])
    if pending_mapping:
        return [{'code': 'financial_mapping_required', 'file_id': doc['id'],
            'message': '；'.join(p['sheet'] + '：' + p['message'] for p in pending_mapping),
            'impact': '未明确的单位/金额栏次及混合主体、期间不能成为正式输入。',
            'required': '在财务映射中明确原单位、实际期间和金额列；逐工作表排除其他主体或期间，保存重新分析后再复核。'}]
    if not require_review and not doc.get('import_mapping', {}).get('formula_cells'):
        return []
    reviews = selection.get('evidence_reviews', [])
    if (not isinstance(reviews, list) or len(reviews) > 503 + len(doc.get('import_mapping', {}).get('formula_cells', [])) or
            any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-f]{64}', x) for x in reviews)):
        raise InputError('逐项证据复核编号格式无效。')
    pending = [item for item in review_requirements(doc, selection, scope) if not item['reviewed']]
    return [{'code': 'evidence_review_required', 'file_id': doc['id'],
             'message': f'尚有 {len(pending)} 项证据未明确复核。',
             'impact': 'AI 候选或财务导出映射未经复核不能直接成为正式检测输入。',
             'required': '逐项对照原件复核，保存并重新分析。修改候选值或企业期间后须重新复核。'}] if pending else []
