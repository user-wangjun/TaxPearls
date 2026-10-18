"""Lossless, source-bound migration of policy knowledge into the eight graph types.

Textual predicates are explicitly non-executable. Migrating an interpretation is
not approval of that interpretation, and a conjunction is never guessed from prose.
"""
import hashlib
import json
from pathlib import Path
import re

import yaml

from src import policy_corpus as corpus

VERSION = 'policy-knowledge-v1'
SUBJECTS = re.compile(r'小型微利企业|增值税小规模纳税人|小规模纳税人|一般纳税人|'
                      r'非居民企业|居民企业|个体工商户|扣缴义务人|纳税义务人|纳税人|'
                      r'用人单位|事业单位|社会团体|国家机关|社会组织|产权所有人|'
                      r'经营管理人|代管人|承典人|使用人|职工|个人|企业|单位')
EXCEPTION = re.compile(r'但是|但[，,]|除外|不包括|不适用|不得|免征|减征|免税|减免|另有规定|从其规定|不征收|不予|不含')
CONDITION = re.compile(r'符合|满足|如果|[若如].*则|超过|不超过|达到|未达到|未超过|以下|以上|以内|应当|必须|需要|按照|属于|适用|自.*起|在.*[时的]')
SENTENCE = re.compile(r'[^。；\n]+[。；\n]*|[。；\n]+')


def canonical(value):
    return corpus.canonical(value)


def key(kind, value):
    return 'migration:' + kind + ':' + hashlib.sha256(canonical(value).encode()).hexdigest()[:32]


def fragments(text):
    """Partition, rather than summarize, including punctuation and whitespace."""
    return [{'start': m.start(), 'end': m.end(), 'text': m.group(),
             'kind': 'exception' if EXCEPTION.search(m.group()) else
                     'condition' if CONDITION.search(m.group()) else 'statement'}
            for m in SENTENCE.finditer(text)]


def _edge(bundle, source_type, source, relation, target_type, target, clause):
    row = dict(from_type=source_type, from_id=source, relation_type=relation,
               to_type=target_type, to_id=target, evidence_clause_id=clause, review_status='candidate')
    row['relationship_id'] = key('edge', row)
    bundle['Relationship'].append(row)


def _predicate(bundle, rid, clause, text, position, kind, *, origin='original'):
    identity = [VERSION, rid, position, kind, text]
    mid, cid = key('metric', identity), key('condition', identity)
    provenance = dict(expression=text, kind=kind, source_clause_id=clause,
                      source_span=position, origin=origin, evaluation_mode='review_required')
    bundle['Metric'].append(dict(metric_id=mid, metric_code=mid, name=text.strip()[:120] or '原文空白',
                                 value_type='boolean', source_schema='PolicyInterpretation',
                                 source_field='reviewed_truth', source_filter_json=canonical(provenance),
                                 aggregation='direct', period_basis='policy_period', definition=text))
    bundle['Condition'].append(dict(condition_id=cid, rule_id=rid, left_metric_id=mid, operator='=',
                                    right_kind='literal', right_literal='true', condition_source='pending',
                                    source_clause_id=clause, notes=canonical(provenance)))
    _edge(bundle, 'TaxRule', rid, 'HAS_CONDITION', 'Condition', cid, clause)
    _edge(bundle, 'Condition', cid, 'USES_METRIC', 'Metric', mid, clause)
    if kind == 'exception':
        eid = key('exception', identity)
        bundle['Exception'].append(dict(exception_id=eid, rule_id=rid, name=text.strip()[:120],
                                        trigger_logic_json=canonical({'ref': cid}), effect='needs_review',
                                        source_clause_id=clause, description=canonical(provenance)))
        _edge(bundle, 'TaxRule', rid, 'HAS_EXCEPTION', 'Exception', eid, clause)
    return cid


def _rule(bundle, policy, clause, *, code=None, annotation=None):
    text = clause['content']
    rid = key('rule', [VERSION, clause['clause_id'], annotation])
    parts = fragments(text)
    # The entire provision is retained as an unresolved predicate. Independently
    # split sentences must not silently turn statutory ORs into executable ANDs.
    root = _predicate(bundle, rid, clause['clause_id'], text, [0, len(text)], 'whole_provision')
    for part in ([] if annotation else parts):
        if part['kind'] != 'statement':
            _predicate(bundle, rid, clause['clause_id'], part['text'], [part['start'], part['end']], part['kind'])
    subjects = []
    for match in SUBJECTS.finditer(text if not annotation else '\n'.join(annotation.get('conditions', []))):
        subjects.append(dict(name=match.group(), start=match.start(), end=match.end()))
    note = dict(migration_version=VERSION, evaluation_mode='review_required',
                source_clause_id=clause['clause_id'], fragments=parts, subject_mentions=subjects,
                subject_binding='candidate_mentions_not_confirmed_eligibility',
                logic_resolution='原文的并列、选择、例外及跨条引用保留，未经解释不执行',
                catalog_annotation=annotation)
    bundle['TaxRule'].append(dict(rule_id=rid, rule_code=code, name=policy['title'] + ' · ' + clause['clause_label'],
                                  tax_type=policy['tax_type'], rule_type='policy_rule',
                                  applicable_region=policy['region_code'], valid_from=policy.get('effective_from'),
                                  valid_to=policy.get('effective_to'), condition_logic_json=canonical({'ref': root}),
                                  outcome_text=text, notes=canonical(note), review_status='candidate', version=VERSION))
    _edge(bundle, 'Clause', clause['clause_id'], 'PROVIDES_CONTEXT' if annotation else 'DERIVES_RULE',
          'TaxRule', rid, clause['clause_id'])
    return rid, subjects


def add_knowledge(plan):
    """Migrate every clause, recording non-operative context instead of hiding it."""
    bundle = plan['bundle']
    for table in ['TaxRule', 'Entity', 'Metric', 'Condition', 'Exception']:
        bundle.setdefault(table, [])
    policies = {p['policy_id']: p for p in bundle['Policy']}
    concepts = {r['entity_id'] for r in bundle['Entity']}
    coverage = []
    for clause in bundle['Clause']:
        cid, label = clause['clause_id'], clause['clause_label']
        _edge(bundle, 'Policy', clause['policy_id'], 'HAS_CLAUSE', 'Clause', cid, cid)
        if clause.get('parent_clause_id'):
            _edge(bundle, 'Clause', clause['parent_clause_id'], 'HAS_SUBCLAUSE', 'Clause', cid, cid)
        context = bool(re.fullmatch(r'.*第[一二三四五六七八九十百零〇0-9]+[章节][^\n]*', label))
        if context:
            coverage.append(dict(clause_id=cid, role='source_context', rule_id=None, chars=len(clause['content'])))
            continue
        annotation = next((i['topic'] for i in plan.get('catalog_migration', [])
                           if i['clause']['clause_id'] == cid), None)
        rid, subjects = _rule(bundle, policies[clause['policy_id']], clause, annotation=annotation)
        for subject in {s['name'] for s in subjects}:
            eid = key('entity', subject)
            if eid not in concepts:
                bundle['Entity'].append(dict(entity_id=eid, entity_type='subject_concept', name=subject,
                                             description='原文出现的主体概念；与规则的关联为候选，不能据此认定实际企业资格。'))
                concepts.add(eid)
            _edge(bundle, 'TaxRule', rid, 'APPLIES_TO', 'Entity', eid, cid)
        coverage.append(dict(clause_id=cid, role='policy_knowledge', rule_id=rid, chars=len(clause['content']),
                             fragment_count=len(fragments(clause['content']))))
    plan['knowledge_migration'] = dict(version=VERSION, coverage=coverage, source_clause_count=len(coverage),
                                       complete_source_coverage=len(coverage) == len(bundle['Clause']),
                                       counts={t: len(v) for t, v in bundle.items()},
                                       automatic_policy_approval=False, automatic_execution=False)
    # Insert in dependency order even though Policy/Clause/Relationship existed first.
    order = ['Policy', 'Clause', 'TaxRule', 'Entity', 'Metric', 'Condition', 'Exception', 'Relationship']
    plan['bundle'] = {t: bundle[t] for t in order}
    return plan


def _manifest_knowledge(plan, manifest):
    """Keep edition metadata and staggered subject dates as graph knowledge.

    These annotations cannot be represented by one unconditional repeal edge.
    Preserve the unresolved change relation along with the per-subject dates.
    """
    path = Path(manifest).resolve()
    data = path.read_bytes()
    file_key = 'catalog/' + path.name
    plan['files'][file_key] = dict(path=str(path),sha256=corpus.sha(data),suffix=path.suffix,bytes=len(data))
    for index, document in enumerate(plan['documents']):
        metadata = {k:v for k,v in document.items() if k not in {'sources','deferred_attachments'}}
        metadata['unresolved_relations'] = [r for r in plan.get('unresolved_relations', [])
                                             if document['document_id'] in {r.get('from_document_id'),r.get('to_document_id')}]
        text = canonical(metadata)
        location = dict(raw_path=file_key,text_path=file_key,raw_sha256=corpus.sha(data),text_sha256=corpus.sha(data),
                        provenance_kind='manifest_annotation',manifest_document_id=document['document_id'],
                        start_char=0,end_char_exclusive=len(text),annotation_sha256=corpus.sha(text.encode()))
        plan['bundle']['Clause'].append(dict(clause_id=key('metadata',[VERSION,metadata]),policy_id=document['policy_id'],
                                             clause_label='版本与适用范围整理',sort_index=200000+index,
                                             content=text,source_location=canonical(location),
                                             extraction_status='needs_review',version=corpus.sha(text.encode())))
    return plan


def _catalogs(plan, paths):
    """Keep original catalog snapshots and annotations, with distinct provenance.

    A catalog text snapshot is not claimed to be the original HTTP/PDF bytes.
    Both the retained catalog hash and the historical response hash are kept.
    """
    from src import policies as evaluator
    bundle = plan['bundle']
    known_urls = {p['source_url']: p for p in bundle['Policy']}
    sources, topics = {}, {}
    for path in paths:
        path = Path(path).resolve()
        data = path.read_bytes()
        catalog = yaml.safe_load(data)
        file_key = 'catalog/' + path.name
        plan['files'][file_key] = dict(path=str(path), sha256=corpus.sha(data), suffix=path.suffix, bytes=len(data))
        for source in catalog['sources']:
            sources[source['id']] = (source, catalog, file_key)
        for topic in catalog['policies']:
            if topic['id'] in topics:
                raise ValueError('政策主题重复，不能覆盖。')
            topics[topic['id']] = topic
    by_source = {}
    for sid, (source, catalog, file_key) in sources.items():
        snapshot = source['snapshot']
        text = snapshot['text']
        compact = re.sub(r'\s+', '', text)
        if corpus.sha(compact.encode()) != snapshot['article_text_sha256']:
            raise ValueError('目录原文快照哈希不一致：' + sid)
        if any(re.sub(r'\s+', '', anchor) not in compact for anchor in source['required_text']):
            raise ValueError('目录原文缺少已采条款：' + sid)
        prior = known_urls.get(source['url'])
        pid = key('policy', [VERSION, sid, snapshot['article_text_sha256'], plan['files'][file_key]['sha256']])
        source_taxes = {t['tax'] for t in topics.values() if sid in t['source_ids']}
        policy = dict(policy_id=pid, title=source['title'], document_no=source.get('document_number'),
                      issuer=source.get('issuer') or '发布机关待核实',
                      tax_type=next(iter(source_taxes)) if len(source_taxes)==1 else 'MIXED',
                      region_code='CN-GD' if sid.startswith('gd-') else 'CN', source_url=source['url'],
                      raw_file_path=plan['files'][file_key]['path'], file_sha256=plan['files'][file_key]['sha256'],
                      fetched_at=snapshot['retrieved_on'], validity_status='unverified', version=snapshot['article_text_sha256'])
        bundle['Policy'].append(policy)
        # Exact snapshot retained as a clause with an honest YAML location.
        cid = key('snapshot', [pid, sid])
        location = dict(raw_path=file_key, text_path=file_key,
                        raw_sha256=plan['files'][file_key]['sha256'], text_sha256=plan['files'][file_key]['sha256'],
                        snapshot_field=f'sources[id={sid}].snapshot.text', snapshot_sha256=snapshot['article_text_sha256'],
                        upstream_response_sha256=snapshot['response_sha256'], upstream_url=source['url'],
                        start_char=0, end_char_exclusive=len(text), provenance_kind='retained_catalog_snapshot')
        bundle['Clause'].append(dict(clause_id=cid, policy_id=policy['policy_id'], clause_label='已采官方原文快照',
                                     sort_index=1, content=text, source_location=canonical(location),
                                     version=snapshot['article_text_sha256'], extraction_status='needs_review'))
        if prior:
            _edge(bundle, 'Policy', prior['policy_id'], 'SAME_SOURCE_URL', 'Policy', pid, cid)
        # Preserve the exact older snapshot even if a newer source version exists.
        by_source[sid] = policy
    annotations = []
    for code, topic in topics.items():
        primary = by_source[topic['source_ids'][0]]
        # Catalog judgments are visibly distinct from official verbatim clauses.
        text = canonical(topic)
        cid = key('catalog-topic', [VERSION, topic])
        source, cat, file_key = sources[topic['source_ids'][0]]
        location = dict(raw_path=file_key, text_path=file_key, raw_sha256=plan['files'][file_key]['sha256'],
                        text_sha256=plan['files'][file_key]['sha256'], catalog_field=f'policies[id={code}]',
                        provenance_kind='catalog_annotation', annotation_sha256=corpus.sha(text.encode()),
                        start_char=0, end_char_exclusive=len(text))
        policy = {**primary, 'region_code':'CN-GD' if topic['policy_level'] == 'provincial' else 'CN',
                  'tax_type':topic['tax'], 'effective_from':topic.get('effective_from'), 'effective_to':topic.get('effective_to')}
        clause = dict(clause_id=cid, policy_id=primary['policy_id'], clause_label='政策目录解释/' + code,
                      sort_index=100000 + len(annotations), content=text, source_location=canonical(location),
                      version=corpus.sha(text.encode()), extraction_status='needs_review')
        # add_knowledge handles these later; retain all old fields, formulas,
        # required inputs, version notes, dates and exclusions in the clause.
        bundle['Clause'].append(clause)
        annotations.append(dict(code=code, topic=topic, clause=clause, policy=policy,
                                sources=[p['policy_id'] for p in (by_source[s] for s in topic['source_ids'])],
                                fields=evaluator.POLICY_FIELDS[code]))
    plan['catalog_migration'] = annotations
    return plan


def prepare(manifest, *, catalog_paths=None):
    plan = corpus.prepare(manifest)
    _manifest_knowledge(plan, manifest)
    root = Path(manifest).resolve().parent.parent
    paths = catalog_paths if catalog_paths is not None else [root/'国家级税费政策.yaml', root/'广东省地方政策.yaml']
    _catalogs(plan, paths)
    add_knowledge(plan)
    bundle = plan['bundle']
    by_clause = {json.loads(r['notes'])['source_clause_id']: r for r in bundle['TaxRule']}
    for item in plan['catalog_migration']:
        rid = by_clause[item['clause']['clause_id']]['rule_id']
        row = by_clause[item['clause']['clause_id']]
        row.update(rule_code=item['code'], name=item['topic']['name'], tax_type=item['topic']['tax'],
                   applicable_region=item['policy']['region_code'],
                   valid_from=item['topic'].get('effective_from'), valid_to=item['topic'].get('effective_to'))
        for source_id in item['sources']:
            for clause in bundle['Clause']:
                if (clause['policy_id'] == source_id
                        and json.loads(clause['source_location']).get('provenance_kind') == 'retained_catalog_snapshot'):
                    _edge(bundle, 'Clause', clause['clause_id'], 'DERIVES_RULE', 'TaxRule', rid, clause['clause_id'])
        for category, kind in [('conditions', 'condition'), ('exclusions', 'exception')]:
            for index, text in enumerate(item['topic'][category]):
                _predicate(bundle, rid, item['clause']['clause_id'], text, [category, index], kind, origin='catalog_annotation')
        # Parameters are dictionary values with their original unit, operator,
        # exact evidence text and upstream source. They are never executed as
        # tax formulas or confused with an enterprise measurement.
        for index, parameter in enumerate(item['topic']['parameters']):
            mid = key('parameter', [VERSION, rid, index, parameter])
            bundle['Metric'].append(dict(metric_id=mid, metric_code=mid, name=parameter['name'],
                                         value_type='text' if '/' in parameter['value'] else 'decimal',
                                         unit=parameter['unit'], source_schema='PolicyParameter',
                                         source_field='value', source_filter_json=canonical(parameter),
                                         aggregation='direct', period_basis='policy_period', definition=canonical(parameter)))
            _edge(bundle, 'TaxRule', rid, 'USES_METRIC', 'Metric', mid, item['clause']['clause_id'])
        from src.policies import FIELDS
        for field in item['fields']:
            mid = key('input', [VERSION, rid, field, FIELDS[field]])
            spec = FIELDS[field]
            bundle['Metric'].append(dict(metric_id=mid, metric_code=mid, name=spec['label'],
                                         value_type='decimal' if spec['type'] in {'number','signed_number'} else 'text',
                                         source_schema='PolicyInput', source_field=field,
                                         source_filter_json=canonical({'policy_code':item['code'], 'field':field, 'spec':spec}),
                                         aggregation='direct', period_basis='policy_period',
                                         definition=canonical(spec)))
            _edge(bundle, 'TaxRule', rid, 'USES_METRIC', 'Metric', mid, item['clause']['clause_id'])
        note = json.loads(row['notes'])
        note.update(catalog_annotation=item['topic'], required_fact_fields=item['fields'],
                    evaluator='src.policies:' + item['code'])
        row['notes'] = canonical(note)
    # Subject-specific commencement dates stay separate. In particular, the
    # staggered 2027/2029/2030 presentation standard must not be flattened into
    # one CN-wide effective date or one unconditional repeal relationship.
    for document in plan['documents']:
        for phase in document.get('phase_in_conditions', []):
            clause = next(c for c in bundle['Clause'] if c['policy_id']==document['policy_id']
                          and c['clause_label']=='版本与适用范围整理')
            rid = key('phase-rule', [VERSION, document['policy_id'], phase])
            mid, cid = key('phase-date', [rid]), key('phase-condition', [rid])
            eid = key('phase-subject', phase['subject'])
            bundle['Entity'].append(dict(entity_id=eid,entity_type='phase_in_subject',name=phase['subject'],
                                         description='清单整理的分阶段适用主体；须核对提前执行与实际事项。'))
            bundle['Metric'].append(dict(metric_id=mid,metric_code=mid,name='适用事项日期',value_type='date',
                                         source_schema='PolicyContext',source_field='business_date',aggregation='direct',
                                         period_basis='policy_period',definition='须核对主体与提前执行条件，不能只比较日期。'))
            bundle['Condition'].append(dict(condition_id=cid,rule_id=rid,left_metric_id=mid,operator='>=',
                                            right_kind='literal',right_literal=phase['effective_from'],
                                            condition_source='pending',source_clause_id=clause['clause_id'],notes=canonical(phase)))
            bundle['TaxRule'].append(dict(rule_id=rid,rule_code=None,name=phase['subject']+' · 分阶段施行',rule_type='policy_rule',
                                          tax_type=document['tax_type'],applicable_region='CN',valid_from=phase['effective_from'],
                                          condition_logic_json=canonical({'ref':cid}),outcome_text=canonical(phase),
                                          review_status='candidate',version=VERSION,
                                          notes=canonical({'migration_version':VERSION,'source_clause_id':clause['clause_id'],
                                                           'phase_in':phase,'evaluation_mode':'review_required',
                                                           'temporal_note':document.get('temporal_note')})))
            for a,b,kind,target in [('Clause',clause['clause_id'],'DERIVES_RULE',('TaxRule',rid)),
                                    ('TaxRule',rid,'APPLIES_TO',('Entity',eid)),
                                    ('TaxRule',rid,'HAS_CONDITION',('Condition',cid)),
                                    ('Condition',cid,'USES_METRIC',('Metric',mid))]:
                _edge(bundle,a,b,kind,*target,clause['clause_id'])
    plan['knowledge_migration']['counts'] = {t:len(v) for t,v in bundle.items()}
    plan['knowledge_migration']['catalog_topics'] = [i['code'] for i in plan['catalog_migration']]
    return plan
