"""Candidate metadata shared by corpus preparation and applicability checks.

Tax coverage of knowledge does not enable a business check or approve a source.
Missing metadata keeps the legacy single-tax/single-region behavior.
"""
import json
import re
import hashlib

TAX_TYPES = {'增值税': 'VAT', '土地增值税': 'LAT', '消费税': 'CONSUMPTION',
             '企业所得税': 'EIT', '个人所得税': 'PIT', '房产税': 'PROPERTY',
             '城镇土地使用税': 'URBAN_LAND_USE', '耕地占用税': 'FARMLAND_OCCUPATION',
             '契税': 'DEED', '车船税': 'VEHICLE_VESSEL', '水资源税': 'WATER_RESOURCE',
             '资源税': 'RESOURCE', '环境保护税': 'ENVIRONMENT', '印花税': 'STAMP',
             '城市维护建设税': 'URBAN_MAINTENANCE', '车辆购置税': 'VEHICLE_PURCHASE',
             '烟叶税': 'TOBACCO', '船舶吨税': 'VESSEL_TONNAGE', '关税': 'CUSTOMS',
             '征管通用': 'TAX_ADMIN', '会计规范': 'ACCOUNTING', '社保依据': 'SOCIAL_INSURANCE',
             '跨税种': 'MIXED'}


def tax_code(value):
    return TAX_TYPES.get(value, {'IIT': 'PIT', 'CIT': 'EIT', '综合': 'ACCOUNTING'}.get(value, value))


def region(value):
    if not isinstance(value, str) or not re.fullmatch(r'CN(?:-[A-Za-z0-9]+)*', value):
        raise ValueError('政策地域须使用明确的 CN 地域代码。')
    return value


def canonical_region(value):
    # The retained Guangdong catalog uses the older alpha code; the enterprise
    # selector uses CN-44. Normalize for matching, never rewrite source records.
    if isinstance(value, str) and (value == 'CN-GD' or value.startswith('CN-GD-')):
        return 'CN-44' + value[len('CN-GD'):]
    return value


def metadata(row, field, default):
    value = row.get(field)
    return json.loads(value) if isinstance(value, str) else default if value is None else value


def validate(row):
    """Validate optional transport fields without promoting their review state."""
    region(row.get('region_code', 'CN'))
    types = metadata(row, 'tax_types_json', [])
    if not isinstance(types, list) or len(types) != len(set(types)) or any(t not in TAX_TYPES.values() for t in types):
        raise ValueError('政策涉及税种须使用不重复的已注册代码。')
    scope = metadata(row, 'scope_json', {})
    if not isinstance(scope, dict) or set(scope) - {'included_regions', 'excluded_regions', 'conditions', 'evidence'}:
        raise ValueError('政策适用范围结构无效。')
    for key in ('included_regions', 'excluded_regions'):
        values = scope.get(key, [])
        if not isinstance(values, list):
            raise ValueError('政策地域范围须为列表。')
        for value in values:
            region(value)
    for field in ('date_evidence_json', 'source_metadata_json'):
        if not isinstance(metadata(row, field, {}), dict):
            raise ValueError('政策日期或来源证据结构无效。')


def prepare(item):
    """Only add supplied metadata; old source identities and IDs stay unchanged."""
    fields = {}
    for key, field in [('tax_types', 'tax_types_json'), ('scope', 'scope_json'),
                       ('date_evidence', 'date_evidence_json'), ('source_metadata', 'source_metadata_json')]:
        if key in item:
            value = item[key]
            if key == 'tax_types':
                value = [tax_code(t) for t in value]
            fields[field] = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    validate({'region_code': item.get('region_code', 'CN'), **fields})
    return fields


def clause_identity(clause):
    from src.policy_paths import source_identity
    value = {'content': clause['content'], 'source': source_identity(clause['source_location']),
             'version': clause.get('version')}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def coverage_review(policy, clause, task, reviews):
    for row in sorted(reviews, key=lambda r: r['reviewed_at'], reverse=True):
        if (row['policy_id'] == policy['policy_id'] and row['end_kind'] == 'unfixed'
                and policy['validity_status'] == 'in_force' and row['file_sha256'] == policy['file_sha256']
                and row['coverage_start'] and row['coverage_start'] <= task['period_start']
                and row['coverage_end'] and row['coverage_end'] >= task['period_end']
                and json.loads(row['clause_snapshots_json']).get(clause['clause_id']) == clause_identity(clause)):
            return dict(row)
    return None
