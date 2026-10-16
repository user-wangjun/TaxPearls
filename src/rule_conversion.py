"""Compile maintained YAML screens into versioned graph conditions.

Conversion proves the computation, not legal validity or policy applicability.
Only the existing, validated arithmetic registry is executable.  No text eval.
"""
from dataclasses import asdict
from decimal import Decimal, localcontext
import hashlib
import json

from src import engine
from src.models import Company, Dataset, Metric

VERSION = 'yaml-screen-v2'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def metric_unit(name):
    if name.endswith('人数'):
        return '人'
    if name.startswith('参考.') and '率' in name or name.endswith('税率'):
        return '比例'
    return '元'


def compile_rule(rule):
    """Every YAML leaf becomes one registered calculation and one Condition."""
    engine.validate_rule_update(rule)
    snapshot = asdict(rule)
    identity = digest({'compiler':VERSION, 'rule':snapshot,'metric_units':{name:metric_unit(name) for name in rule.inputs}})
    rid = f'{rule.id}@{rule.version}:converted:{identity[:16]}'
    bases = {}
    for name, requirement in rule.inputs.items():
        mid = 'business:' + digest({'name':name, 'requirement':requirement, 'compiler':VERSION,'unit':metric_unit(name)})[:24]
        bases[name] = {'metric_id':mid, 'metric_code':mid, 'logical_code':name, 'name':name,
                       'value_type':'decimal', 'unit':metric_unit(name), 'source_schema':'BusinessMetric',
                       'source_field':'value', 'source_filter_json':canonical({'name':name}),
                       'aggregation':'direct', 'period_basis':'rule_period', 'definition':requirement}
    calculations, conditions = [], []

    def visit(logic):
        if logic['type']=='all':
            return {'and':[visit(child) for child in logic['conditions']]}
        number = len(conditions)+1
        cid, mid = f'{rid}:C{number}', f'{rid}:CALC{number}'
        needed = engine._validate_logic(logic)
        spec = {'registry':VERSION, 'logic':logic, 'dependencies':{name:bases[name]['metric_id'] for name in sorted(needed)},
                'requirements':{name:rule.inputs[name] for name in sorted(needed)}}
        calculations.append({'metric_id':mid,'metric_code':mid,'name':f'{rule.name} · 子条件{number}计算',
                             'value_type':'decimal','unit':'布尔数值','source_schema':'RuleCalculation',
                             'source_field':'value','source_filter_json':canonical(spec),'aggregation':'derived',
                             'calculation_code':VERSION,'period_basis':'rule_period',
                             'definition':'计算值1表示命中原有内部筛查条件，0表示未命中；不是法定标准。'})
        conditions.append({'condition_id':cid,'rule_id':rid,'left_metric_id':mid,'operator':'>','right_kind':'literal',
                           'right_literal':'0','unit':'布尔数值','condition_source':'internal_screening',
                           'notes':canonical({'logic':logic,'threshold_basis':rule.threshold_basis})})
        return {'ref':cid}

    logic = visit(rule.logic)
    return {'rule_id':rid, 'fingerprint':identity, 'compiler':VERSION, 'snapshot':snapshot,
            'bases':bases, 'calculations':calculations, 'conditions':conditions, 'logic':logic}


def validate_calculation(definition):
    versions = {VERSION, 'yaml-screen-v1'}
    if (definition.get('source_schema')!='RuleCalculation' or definition.get('calculation_code') not in versions
            or definition.get('aggregation')!='derived' or definition.get('unit')!='布尔数值'):
        raise ValueError('未注册的规则计算函数。')
    spec = json.loads(definition['source_filter_json'])
    if (set(spec)!={'registry','logic','dependencies','requirements'}
            or spec['registry']!=definition['calculation_code']):
        raise ValueError('规则计算定义无效。')
    needed = engine._validate_logic(spec['logic'])
    if needed!=set(spec['dependencies']) or needed!=set(spec['requirements']):
        raise ValueError('规则计算依赖与原规则不一致。')
    if any(not isinstance(mid,str) or not mid for mid in spec['dependencies'].values()):
        raise ValueError('规则计算依赖编号无效。')
    return spec


def calculate(definition, facts, *, task=None):
    spec = validate_calculation(definition)
    unknown = {'state':'unknown','value':None,'unit':'布尔数值','source_record_ids':[]}
    if spec['registry'] != VERSION:
        return {**unknown, 'reason':'指标期间与参考范围校验已升级，请使用当前规则重新核查。'}
    metrics, sources = {}, []
    scopes = []
    for name, mid in spec['dependencies'].items():
        fact = facts.get(mid,{})
        ids = fact.get('source_record_ids',fact.get('source_ids',[]))
        if fact.get('state')!='known' or not ids or fact.get('unit')!=metric_unit(name):
            return {**unknown,'reason':f'缺少已核实指标：{name}；{spec["requirements"][name]}'}
        try:
            value = Decimal(fact['value'])
            if not value.is_finite():
                raise ValueError('非有限值')
        except (ValueError, TypeError, ArithmeticError):
            return {**unknown,'reason':f'指标格式无效：{name}'}
        metrics[name] = Metric(name,value,'；'.join(ids), period=fact.get('metric_period', ''),
                               reference=fact.get('reference'))
        if fact.get('company_scope'):
            scopes.append(fact['company_scope'])
        sources.extend(ids)
    if scopes and any(scope != scopes[0] for scope in scopes):
        return {**unknown, 'reason':'关联指标的企业参考适用范围不一致，请重新核对来源。'}
    scope = scopes[0] if scopes else {}
    task = task or {}
    period = (f"{task['start_date']}至{task['end_date']}"
              if task.get('start_date') and task.get('end_date') else '')
    company = Company('', '', scope.get('industry', ''), period, scope.get('region', ''),
                      scope.get('taxpayer_type', ''), scope.get('business_scope', ''))
    # Only explicit synthetic tasks with frozen synthetic sources permit demo
    # benchmarks. Enterprise tasks cannot opt in through a numeric binding.
    simulation = task.get('data_scope') == 'synthetic' and all(
        facts[mid].get('simulation') is True for mid in spec['dependencies'].values())
    dataset = Dataset(company,[],{},metrics,simulation=simulation)
    class Screen:
        inputs = spec['requirements']
        logic = spec['logic']
    try:
        with localcontext() as context:
            context.prec=64
            hit, measured, calculation, threshold = engine._logic(spec['logic'],dataset,Screen())
        return {'state':'known','value':'1' if hit else '0','unit':'布尔数值',
                'source_record_ids':list(dict.fromkeys(sources)),'reason':'',
                'calculation':calculation,'measured':str(measured) if measured is not None else None,
                'threshold':threshold}
    except (engine.MissingMetric, ValueError, ArithmeticError) as exc:
        return {**unknown,'reason':str(exc)}


def install(db, rule, legacy_id):
    """Immutable technical conversion; policy approval deliberately stays separate."""
    compiled = compile_rule(rule)
    rid = compiled['rule_id']
    if db.execute('SELECT 1 FROM TaxRule WHERE rule_id=?',(rid,)).fetchone():
        return rid
    db.execute('INSERT INTO TaxRule (rule_id,rule_code,version,fingerprint,name,tax_type,rule_type,applicable_region,'
               'valid_from,valid_to,condition_logic_json,outcome_text,notes,supersedes_id,conversion_json) '
               "VALUES (?,?,?,?,?,?,'risk_screen','CN',?,?,?,?,?,?,?)",
               (rid,rule.id,rule.version,compiled['fingerprint'],rule.name,rule.tax_type,rule.effective_from,
                rule.effective_to,canonical(compiled['logic']),rule.suggestion,
                '已完成确定性计算转换；阈值为原有内部筛查参数，政策审核另行保留。',legacy_id,
                canonical({'compiler':VERSION,'source_rule':compiled['snapshot'],'source_rule_hash':digest(compiled['snapshot'])})))
    for metric in [*compiled['bases'].values(),*compiled['calculations']]:
        columns=list(metric)
        db.execute(f'INSERT OR IGNORE INTO Metric ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',list(metric.values()))
        eid=digest([rid,metric['metric_id'],'USES_METRIC'])
        db.execute("INSERT INTO Relationship (relationship_id,from_type,from_id,relation_type,to_type,to_id) "
                   "VALUES (?,'TaxRule',?,'USES_METRIC','Metric',?)",(eid,rid,metric['metric_id']))
    for condition in compiled['conditions']:
        columns=list(condition)
        db.execute(f'INSERT INTO Condition ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',list(condition.values()))
        eid=digest([rid,condition['condition_id'],'HAS_CONDITION'])
        db.execute("INSERT INTO Relationship (relationship_id,from_type,from_id,relation_type,to_type,to_id) "
                   "VALUES (?,'TaxRule',?,'HAS_CONDITION','Condition',?)",(eid,rid,condition['condition_id']))
    return rid
