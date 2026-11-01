"""Synchronize maintained rule knowledge and its teaching projection.

This is an explicit maintenance operation. It imports retained sources as
candidates, never grants a legal approval or writes enterprise facts/grades.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from src import policy_corpus, rule_conversion
from webapp import policy_graph, training_content
from webapp.storage import _now

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'docs/法规原文/来源清单.json'
OUTLINE_SOURCE = 'https://zhejiang.chinatax.gov.cn/art/2026/5/8/art_22104_654126.html'
CERT_CODE = 'tax-adviser'
PREFIX = 'knowledge:tax-adviser:'
GROUPS = {
    'vat': ('增值税申报与抵扣', '税法（一）', {'R-001', 'R-002', 'R-005', 'R-006', 'R-014', 'R-016', 'R-017'}),
    'pit': ('个人所得税与工资薪金', '税法（二）', {'R-004', 'R-018'}),
    'accounting': ('财务报表与会计勾稽', '财务与会计', {'R-007', 'R-008', 'R-009', 'R-010', 'R-011', 'R-012', 'R-013', 'R-015'}),
    'review': ('涉税风险分析与材料复核', '涉税服务实务', {'R-003', 'R-019', 'R-020', 'R-021', 'R-022', 'R-023', 'R-024'}),
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


def teaching(db, rules):
    """Add only namespaced teaching content; preserve teachers' own content."""
    now = _now()
    cert = db.execute('SELECT id FROM certificate WHERE code=?', (CERT_CODE,)).fetchone()
    cert_id = cert['id'] if cert else PREFIX + 'certificate'
    if not cert:
        db.execute('''INSERT INTO certificate
            (id,code,name,description,subjects_json,source_ref,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)''',
                   (cert_id, CERT_CODE, '税务师职业资格',
                    '按2026年度公布的考试科目组织税务规则仿真练习；当前覆盖以知识点为准，非完整考纲、真题或高频题库。',
                    json.dumps(['税法（一）', '税法（二）', '涉税服务相关法律', '财务与会计', '涉税服务实务'], ensure_ascii=False),
                    OUTLINE_SOURCE, now, now))
    known = {r.id: r for r in rules}
    version = _hash([asdict(r) for r in sorted(rules, key=lambda r: r.id)])
    source = ('2026年度税务师职业资格考试报名公告第五部分：科目分类；' + OUTLINE_SOURCE
              + '；下列知识点为本项目按规则整理的仿真实训主题，不声称为官方考纲逐项映射或常考标注。')
    expected = set()

    def point(key, code, name, subject, parent, description):
        key = PREFIX + key
        expected.add(key)
        db.execute('''INSERT INTO knowledge_point
            (id,certificate_id,code,name,subject,parent_id,description,source_ref,outline_version,active,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,1,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,subject=excluded.subject,
            description=excluded.description,source_ref=excluded.source_ref,
            outline_version=excluded.outline_version,active=1,updated_at=excluded.updated_at''',
                   (key, cert_id, code, name, subject, parent, description, source,
                    '2026科目分类 / 规则仿真 ' + version[:12], now, now))
        return key

    for group, (name, subject, codes) in GROUPS.items():
        classified = set().union(*(g[2] for g in GROUPS.values()))
        additional = {r.id for r in rules if r.id not in classified
                      and group == ('vat' if r.tax_type == '增值税' else 'pit' if r.tax_type == '个人所得税' else 'review')}
        selected = [known[c] for c in sorted((codes | additional) & known.keys())]
        parent = point(group, 'KB-' + group.upper(), name, subject, None, '本项目仿真实训主题；不代表已覆盖整门考试科目。')
        for rule in selected:
            key = point(rule.id, 'KB-' + rule.id, rule.name, subject, parent,
                        rule.scope + '\n指标口径：' + '；'.join(rule.inputs.values())
                        + '\n参数依据：' + rule.threshold_basis + '\n风险命中仅提示复核，不是违法或补税结论。')
            db.execute('''INSERT OR IGNORE INTO knowledge_point_link
                (id,knowledge_point_id,target_type,target_id,created_at) VALUES (?,?,'rule',?,?)''',
                       (PREFIX + 'link:' + rule.id, key, rule.id, now))
            db.execute('''INSERT OR IGNORE INTO knowledge_point_link
                (id,knowledge_point_id,target_type,target_id,created_at) VALUES (?,?,'rule',?,?)''',
                       (PREFIX + 'group-link:' + group + ':' + rule.id, parent, rule.id, now))
    # Removed maintained rules leave historical knowledge points intact, but
    # cannot be selected for a new exercise.
    for row in db.execute('SELECT id FROM knowledge_point WHERE certificate_id=?', (cert_id,)):
        if row['id'].startswith(PREFIX) and row['id'] not in expected:
            db.execute('UPDATE knowledge_point SET active=0,updated_at=? WHERE id=?', (now, row['id']))
    # The relation is derived from an exact shared input, not an invented
    # prerequisite or unsupported claim about examination frequency.
    db.execute('DELETE FROM knowledge_point_relation WHERE id LIKE ?', (PREFIX + 'relation:%',))
    relation_count = 0
    mapped = [r for r in rules if PREFIX + r.id in expected]
    for index, left in enumerate(mapped):
        for right in mapped[index + 1:]:
            shared = sorted(set(left.inputs) & set(right.inputs))
            if not shared:
                continue
            key = PREFIX + 'relation:' + _hash(sorted([left.id, right.id]))[:24]
            db.execute('''INSERT INTO knowledge_point_relation
                (id,from_kp_id,to_kp_id,relation_type,basis_ref,basis_version,created_at)
                VALUES (?,?,?,'concept',?,?,?)''',
                       (key, PREFIX + left.id, PREFIX + right.id, '共同取数指标：' + '、'.join(shared), version, now))
            relation_count += 1
    content = training_content.resolve_active_version(db, cert_id)
    if content is None:
        content = db.execute('SELECT * FROM training_content_version WHERE certificate_id=? ORDER BY created_at DESC,id LIMIT 1',
                             (cert_id,)).fetchone()
    if content is None:
        content = training_content.ensure_active_version(db, cert_id, rules)
    return {'certificate_id': cert_id, 'knowledge_points': len(expected), 'rule_links': len(mapped),
            'relations': relation_count, 'content_version_id': content['id'], 'rules_fingerprint': version}


def synchronize(db, rules, plan, originals):
    """Call in the caller's write transaction after authorization/backup."""
    policy_graph.sync_rules(db, rules)
    ids = {r.id: rule_conversion.compile_rule(r)['rule_id'] for r in rules}
    corpus = policy_corpus.import_plan(db, plan, originals, rule_ids=ids)
    education = teaching(db, rules)
    # Initialize missing baselines; existing teaching versions remain pinned
    # until a teacher explicitly publishes a replacement.
    for row in db.execute("SELECT id FROM certificate WHERE active=1 AND id<>?", (education['certificate_id'],)):
        if (training_content.linked_rule_ids(db, row['id']) and not db.execute(
                'SELECT 1 FROM training_content_version WHERE certificate_id=?', (row['id'],)).fetchone()):
            training_content.ensure_active_version(db, row['id'], rules)
    return {'corpus': corpus, 'teaching': education, 'current_rule_count': len(ids),
            'manifest_sha256': plan['manifest_sha256'], 'formal_approvals': 0, 'external_ai_calls': 0}


def rule_references(db, rules, linked):
    """Read shared public knowledge by exact rule version; never enterprise facts."""
    result = []
    for rule in sorted(rules, key=lambda item: item.id):
        if rule.id not in linked:
            continue
        compiled = rule_conversion.compile_rule(rule)
        current = db.execute('SELECT review_status FROM TaxRule WHERE rule_id=?', (compiled['rule_id'],)).fetchone()
        sources = db.execute('''SELECT DISTINCT p.policy_id,p.title,p.source_url,p.validity_status,
            p.issuer,p.region_code,p.publish_date,p.effective_from,p.effective_to,
            c.clause_id,c.clause_label,c.extraction_status,c.content,c.source_location,
            c.effective_from AS clause_effective_from,c.effective_to AS clause_effective_to
            FROM Relationship r JOIN Clause c ON r.from_type='Clause' AND r.from_id=c.clause_id
            JOIN Policy p ON c.policy_id=p.policy_id
            WHERE r.to_type='TaxRule' AND r.to_id=? AND r.relation_type='PROVIDES_CONTEXT'
            ORDER BY p.title,c.sort_index''', (compiled['rule_id'],)).fetchall()
        result.append({'rule_code': rule.id, 'rule_version': rule.version, 'rule_id': compiled['rule_id'],
                       'fingerprint': compiled['fingerprint'], 'sources': [dict(s) for s in sources],
                       'name': rule.name, 'inputs': rule.inputs, 'logic': rule.logic,
                       'scope': rule.scope, 'effective_from': rule.effective_from, 'effective_to': rule.effective_to,
                       'review_status': current['review_status'] if current else 'not_synced',
                       'threshold_basis': rule.threshold_basis})
    return result


def rule_knowledge(db, kp_id, rules, version=None):
    """Teaching references use the published snapshot when one is available."""
    linked = {r['target_id'] for r in db.execute(
        "SELECT target_id FROM knowledge_point_link WHERE knowledge_point_id=? AND target_type='rule' AND active=1", (kp_id,))}
    if version is not None:
        linked &= set(json.loads(version['rule_ids_json'] or '[]'))
    return version_references(db, version, rules, linked)


def version_references(db, version, rules, linked):
    if version is not None:
        if version['knowledge_snapshot_json']:
            result = [r for r in json.loads(version['knowledge_snapshot_json']) if r['rule_code'] in linked]
            for reference in result:
                for source in reference['sources']:
                    latest = db.execute('''SELECT p.validity_status,c.extraction_status FROM Clause c
                        JOIN Policy p ON p.policy_id=c.policy_id WHERE c.clause_id=?''',
                                        (source['clause_id'],)).fetchone()
                    source['current_validity_status'] = latest['validity_status'] if latest else 'missing'
                    source['current_extraction_status'] = latest['extraction_status'] if latest else 'missing'
                    source['snapshot_status'] = 'published'
            return result
    return rule_references(db, rules, linked)
