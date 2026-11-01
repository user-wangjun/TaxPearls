"""Deterministic batch scopes and synthesis, shared by live and saved validation."""
from copy import deepcopy

from src.kg_retrieval import batching, outbound, bundle
from src.kg_retrieval.common import digest


def payload_for(part, package, depth=0):
    payload = deepcopy(part)
    partial = package['batching']['part_count'] > 1 or depth > 0
    expected = 'insufficient_evidence' if package['bundle_status'] != 'ready' or partial else {
        'condition_met': 'risk_clue', 'condition_not_met': 'condition_not_met', 'excluded': 'excluded'
    }.get(package['check'].get('disposition'), 'insufficient_evidence')
    payload['response_constraints'] = {
        'required_type': expected, 'fact_assessment_is_readonly': True,
        'policy_readiness_is_independent_of_numeric_difference': True,
        'allowed_rule_id': payload['check']['rule_id'],
        'allowed_clause_ids': sorted({p['clause_id'] for p in payload['policy_evidence'] + payload['policy_leads']}),
        'allowed_source_ids': sorted({s for f in payload['facts'] for s in f.get('source_ids', [])} |
                                    {r['source_id'] for r in payload.get('supporting_materials', [])})}
    if partial:
        payload.update(bundle_status='needs_review', analysis_mode='review_only')
        payload['response_constraints']['partial_batch_is_not_a_complete_check'] = True
    return payload


def children(part, package, target, destination):
    child = {**deepcopy(part), 'bundle_id': package['bundle_id'],
             'metric_definitions': package.get('metric_definitions', {})}
    plan = batching.split(child, target, output_tokens=package['batching'].get('output_tokens'))
    if plan['unsplittable'] or not plan['parts'] or any(
            batching.encoded_size(p) >= batching.encoded_size(part) for p in plan['parts']):
        raise ValueError('必要核查信息仍超过容量，原始证据已保留，需核对模型容量。')
    child['batching'] = plan
    delivery = outbound.prepare(child, authorized=True, destination=destination)
    if delivery['outbound_gate'] == 'blocked':
        raise ValueError('细分批次未通过外送安全检查。')
    for p in delivery['parts']:
        p['required_clause_ids'] = sorted(set(p.get('required_clause_ids', [])) | set(part.get('required_clause_ids', [])))
    return delivery['parts']


def saved_scopes(package, splits):
    """Replay only local deterministic splitting; never contact a provider."""
    scopes = []
    def visit(part, depth=0):
        if depth > 4:
            raise ValueError('历史分批层级无效。')
        payload = payload_for(part, package, depth)
        batch_id = digest(payload)
        if batch_id in splits:
            target = splits[batch_id]
            if type(target) is not int or not 256 <= target <= batching.MAX_BUDGET:
                raise ValueError('历史分批容量无效。')
            for child in children(part, package, target, 'https://validation.local'):
                visit(child, depth + 1)
        else:
            scopes.append(payload)
    for part in outbound.prepare(package, authorized=True, destination='https://validation.local')['parts']:
        visit(part)
    return scopes


def summarize(package, results):
    """Join validated receipts with the original program decision, without an LLM.

    Raw part-local statements remain in batch_results and assessments. The short
    overview cannot add an amount, legal conclusion or citation of its own.
    """
    claims = [a for r in results for a in r['assessments']]
    materials = list(dict.fromkeys(m for a in claims for m in a['required_materials']))
    kind = {'condition_met': 'risk_clue', 'condition_not_met': 'condition_not_met', 'excluded': 'excluded'}.get(
        package['check'].get('disposition'), 'insufficient_evidence')
    if package['bundle_status'] != 'ready' or materials:
        kind = 'insufficient_evidence'
    texts = {
        'condition_not_met': '已完成全部材料批次的联合核对，本项比对条件不形成风险线索。',
        'risk_clue': '已完成全部材料批次的联合核对，本项形成待核查线索，请结合原始资料复核。',
        'excluded': '已完成全部材料批次的联合核对，本项符合程序已核对的排除条件。',
        'insufficient_evidence': '已汇总全部材料批次，本项仍有资料或适用依据需要核实，请查看对应缺口及各批证据。'}
    summary_claim = {'type': kind, 'text': texts[kind], 'rule_id': package['check']['rule_id'],
        'clause_ids': list(dict.fromkeys(c for a in claims for c in a['clause_ids']))[:64],
        'source_ids': list(dict.fromkeys(s for a in claims for s in a['source_ids']))[:64],
        'amounts': [], 'dates': [], 'required_materials': materials[:64]}
    gaps = {}
    for gap in package['gaps']:
        code = gap.get('code', 'other')
        gaps[code] = gaps.get(code, 0) + 1
    return {'status': 'completed', 'method': 'validated_program_join_v1',
            'evidence_hash': bundle.content_hash(package), 'completed_batches': len(results),
            'receipt_hash': digest(results), 'batch_ids': [r['batch_id'] for r in results],
            'program_evaluation': package['check']['evaluation'], 'remaining_gaps': gaps,
            'required_material_count': len(materials), 'assessments': [summary_claim]}
