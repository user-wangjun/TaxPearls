"""Lossless, category-aware evidence batches with explicit cross-batch dependencies."""
from copy import deepcopy
import json
from .passages import fragment

MAX_BUDGET = 16 * 1024 * 1024
POLICY_FIELDS = {'issuer', 'clause_version', 'original_char_count', 'policy_version', 'applicability', 'evidence_spans', 'effective_from', 'text', 'fragment_count', 'source_char_start', 'source_char_end', 'partial_clause', 'file_sha256', 'source_url', 'source_metadata', 'date_evidence', 'policy_id', 'scope', 'document_no', 'original_text_sha256', 'required', 'reasons', 'omitted_char_count', 'segments', 'region_code', 'source_location', 'context_complete', 'clause_id', 'title', 'review_status', 'selection_kind', 'tax_types', 'effective_to', 'coverage_review', 'fragment_index'}
LABELS = {'complete_check': '本项完整核对', 'facts': '企业事实与计算', 'supporting_materials': '额外佐证材料', 'rule_context': '规则条件与例外',
          'policy_basis': '政策依据', 'policy_exception': '例外与排除依据', 'policy_change': '政策变更依据',
          'policy_lead': '待核实政策线索', 'policy_relations': '政策关联', 'review_gaps': '待核实事项'}
MATERIALS = {'Invoice': '发票资料', 'VATDeclaration': '申报资料', 'AccountBalance': '科目余额资料',
             'BusinessMetric': '已绑定业务指标', 'RuleCalculation': '程序计算指标'}


def encoded_size(value):
    # Match the actual user-message serializer, including JSON whitespace.
    return len(json.dumps(value, ensure_ascii=False).encode())


def split(bundle, token_budget):
    if not 256 <= token_budget <= MAX_BUDGET:
        raise ValueError('材料包预算须在 256–16777216 之间。')
    base = {key: deepcopy(bundle[key]) for key in ('task', 'check', 'facts', 'conditions', 'exceptions', 'relationships')}
    for key in ('fact_assessment', 'risk_assessment'):
        if key in bundle:
            base[key] = {k: deepcopy(v) for k, v in bundle[key].items() if k != 'mapping_review'}
    policies = {kind: [{k: deepcopy(v) for k, v in item.items() if k in POLICY_FIELDS} for item in bundle[kind]]
                for kind in ('policy_evidence', 'policy_leads')}
    for records in policies.values():
        for record in records:
            if 'coverage_review' in record:
                record['coverage_review'] = {k: v for k, v in record['coverage_review'].items() if k in
                    {'review_id', 'end_kind', 'coverage_start', 'coverage_end', 'evidence_json'}}
    full = {**base, **policies, 'gaps': deepcopy(bundle['gaps'])}
    if bundle.get('supporting_materials'):
        full['supporting_materials'] = deepcopy(bundle['supporting_materials'])
    group_names = {p.get('policy_id'): ' · '.join(str(p[k]) for k in ('title', 'document_no') if p.get(k))
                   for rows in policies.values() for p in rows if p.get('title')}

    def stamp(data, category, group):
        return {**data, 'bundle_status': bundle.get('bundle_status', 'needs_review'),
                'analysis_mode': 'review_only' if bundle.get('bundle_status') != 'ready' else 'evidence_assessment',
                'batch_category': category, 'batch_category_name': LABELS[category], 'batch_group': group,
                'batch_group_name': group_names.get(group, group),
                'part_number': 999999, 'part_count': 999999, 'requires_joint_review': True,
                'bundle_id': bundle['bundle_id'], 'token_upper_bound': MAX_BUDGET, 'over_budget': False}

    def fits(data, category, group):
        # Analysis adds response constraints after outbound minimization.
        return encoded_size(stamp(data, category, group)) + 384 <= token_budget

    def finish(records):
        parts = []
        for number, (data, category, group) in enumerate(records, 1):
            part = stamp(data, category, group)
            part.update(part_number=number, part_count=len(records), requires_joint_review=len(records) > 1)
            for _ in range(2):
                part['token_upper_bound'] = encoded_size(part)
            part['over_budget'] = part['token_upper_bound'] > token_budget
            parts.append(part)
        return {'parts': parts, 'part_count': len(parts), 'soft_token_budget': token_budget,
                'estimator': 'utf8_bytes_upper_bound', 'requires_joint_review': len(parts) > 1,
                'unsplittable': any(p['over_budget'] for p in parts), 'strategy': 'category_and_source_v1'}

    if fits(full, 'complete_check', ''):
        return finish([(full, 'complete_check', '')])

    # Keep compact decision context common. Large fact/graph collections become
    # their own categories; incomplete batches must never decide overall risk.
    shared = deepcopy(base)
    shared.update(policy_evidence=[], policy_leads=[], gaps=[])
    if bundle.get('supporting_materials'):
        shared['supporting_materials'] = []
    shared['required_clause_ids'] = sorted({p['clause_id'] for rows in policies.values() for p in rows if p.get('required')})
    if 'risk_assessment' in shared:
        shared['risk_assessment'].pop('missing_evidence', None)  # losslessly retained in the gaps category
    groups = {}

    def add(category, group, field, item):
        groups.setdefault((category, group), []).append((field, item))

    for item in bundle.get('supporting_materials', []):
        add('supporting_materials', item['file_id'], 'supporting_materials', deepcopy(item))

    for field in ('relationships', 'facts', 'conditions', 'exceptions'):
        if encoded_size(shared) < token_budget // 2:
            break
        for item in shared[field]:
            if field == 'facts':
                definition = bundle.get('metric_definitions', {}).get(item.get('metric_id'), {})
                group = MATERIALS.get(definition.get('source_schema'), '其他业务指标')
                add('facts', group, field, item)
            else:
                category = 'policy_relations' if field == 'relationships' else 'rule_context'
                add(category, LABELS[category], field, item)
        shared[field] = []
    if not fits(shared, 'rule_context', ''):
        return finish([(full, 'complete_check', '')])

    exception_sources = {x.get('source_clause_id') for x in bundle['exceptions']}
    change_sources = {e.get(key) for e in bundle['relationships']
                      if e.get('relation_type') in {'AMENDS', 'REPEALS', 'INTERPRETS', 'SUPPLEMENTS'}
                      for key in ('from_id', 'to_id', 'evidence_clause_id')}
    for field, rows in policies.items():
        for item in rows:
            cid = item['clause_id']
            category = ('policy_exception' if cid in exception_sources else 'policy_change' if cid in change_sources
                        else 'policy_lead' if field == 'policy_leads' else 'policy_basis')
            add(category, item.get('policy_id', ''), field, item)
    for gap in bundle['gaps']:
        add('review_gaps', '待核实事项', 'gaps', deepcopy(gap))

    records = []
    for (category, group), items in groups.items():
        current = deepcopy(shared)
        has_items = False
        for field, item in items:
            proposed = deepcopy(current)
            proposed[field].append(item)
            if fits(proposed, category, group):
                current, has_items = proposed, True
                continue
            if has_items:
                records.append((current, category, group))
                current, has_items = deepcopy(shared), False
            proposed = deepcopy(current)
            proposed[field].append(item)
            if fits(proposed, category, group):
                current, has_items = proposed, True
                continue
            # Split only text, at character offsets; retain ID, location, hash
            # and explicit fragment boundaries. No truncation or AI summary.
            text = item.get('text')
            if not isinstance(text, str) or not text:
                return finish([(full, 'complete_check', '')])
            def make_fragment(start, end):
                piece = fragment(item, start, end)
                if field == 'supporting_materials':
                    piece['partial_source'] = piece.pop('partial_clause')
                return piece

            start, fragments = 0, []
            while start < len(text):
                low, high, best = 1, len(text) - start, 0
                while low <= high:
                    size = (low + high) // 2
                    piece = {**make_fragment(start, start + size), 'fragment_index': 999999, 'fragment_count': 999999}
                    proposal = deepcopy(shared)
                    proposal[field].append(piece)
                    if fits(proposal, category, group):
                        best, low = size, size + 1
                    else:
                        high = size - 1
                if not best:
                    return finish([(full, 'complete_check', '')])
                fragments.append(make_fragment(start, start + best))
                start += best
            for index, piece in enumerate(fragments, 1):
                piece.update(fragment_index=index, fragment_count=len(fragments))
                proposal = deepcopy(shared)
                proposal[field].append(piece)
                records.append((proposal, category, group))
        if has_items:
            records.append((current, category, group))
    return finish(records or [(full, 'complete_check', '')])
