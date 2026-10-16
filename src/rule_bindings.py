"""Server-derived business metric bindings; clients never supply metric amounts."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json

from src import config, tax_conditions
from src.kg_retrieval.common import digest
from src.rule_conversion import metric_unit

FIELDS = {
    'account_balance':{'opening_balance','current_debit','current_credit','closing_balance'},
    'vat_declaration':{'declared_value'},
    'invoice_detail':{'amount_excluding_tax','tax_amount'},
}


def source_kind(name):
    if name in config.ACCOUNT_MAP:
        return 'account_balance'
    if name in config.DECLARATION_ITEMS:
        return 'vat_declaration'
    if name in {'发票.销项净额','发票.销项税额净额','发票.采购不含税净额','发票.采购税额净额'}:
        return 'invoice_detail'
    return None


def binding(name, ids, field, aggregation, basis, processing, task, reviewer):
    kind = source_kind(name)
    if kind is None:
        raise ValueError('此指标需要完整报表、历史资料或调节底稿，请关联有权限的同企业同期完整审计材料。')
    if (not isinstance(ids,list) or not 1<=len(ids)<=5000 or any(not isinstance(x,str) for x in ids)
            or len(ids)!=len(set(ids)) or field not in FIELDS[kind] or aggregation not in {'direct','sum'}
            or not isinstance(basis,str) or not 3<=len(basis.strip())<=1500):
        raise ValueError('请选择明确来源记录、取数字段、汇总方式并填写已核实的统计调整口径。')
    if aggregation=='direct' and len(ids)!=1:
        raise ValueError('直接取数须且只能选择一条来源。')
    catalog={r['id']:r for r in processing.get('records',[])}
    if any(i not in catalog or catalog[i].get('material_type')!=kind for i in ids):
        raise ValueError('指标来源不存在或材料类型与指标不匹配。')
    allowed_field = config.ACCOUNT_MAP.get(name,{}).get('side')
    if allowed_field and field!={'debit':'current_debit','credit':'current_credit'}[allowed_field]:
        raise ValueError('科目取数借贷方向与原规则定义不符。')
    if name in config.ACCOUNT_MAP:
        codes=[catalog[i].get('account_code') for i in ids]
        if set(codes)!=set(config.ACCOUNT_MAP[name]['accounts']) or len(codes)!=len(set(codes)):
            raise ValueError('原规则要求的科目须完整且各取一次，不能遗漏科目或混选父子级重复汇总。')
    if kind=='invoice_detail':
        direction = 'output' if '销项' in name else 'input'
        expected_field = 'tax_amount' if '税额' in name else 'amount_excluding_tax'
        if field!=expected_field or any(catalog[i].get('direction')!=direction for i in ids):
            raise ValueError('发票取数方向或金额税额字段与指标定义不符。')
    return {'name':name,'record_ids':ids,'field':field,'aggregation':aggregation,'basis':basis.strip(),
            'reviewed_by':reviewer,'period_start':task['start_date'],'period_end':task['end_date'],
            'source_signature':digest([{k:catalog[i].get(k) for k in ['id',field,'source_file_id','source_position','unit','duplicate_of','in_scope','direction','red_letter_status','void_status']} for i in ids])}


def restore(payload):
    """Retain frozen, authorized audit-source records when core inputs are reparsed."""
    records=payload.get('processing',{}).get('records')
    if records is None:
        return
    existing={r['id'] for r in records}
    for row in payload.get('task',{}).get('audit_metric_records',[]):
        if row['id'] not in existing:
            records.append(row)


def material_signature(processing):
    return digest([r for r in processing.get('records',[]) if r.get('material_type')!='business_metric'])


def resolve(definition, processing, task):
    name=json.loads(definition['source_filter_json'])['name']
    unknown={'state':'unknown','value':None,'unit':definition.get('unit'),'source_record_ids':[]}
    rows=[r for r in processing.get('records',[]) if r.get('material_type')=='business_metric' and r.get('metric_name')==name]
    if rows and task.get('audit_metric_records') and task.get('audit_metric_source_signature')!=material_signature(processing):
        return {**unknown,'reason':'任务原始资料已变化，请重新核对并关联完整材料指标。'}
    chosen=task.get('metric_bindings',{}).get(name)
    if not chosen:
        if len(rows)==1:
            row=rows[0]
            if (row.get('tenant_id')!=task['tenant_id'] or row.get('company_id')!=task['company_id']
                    or row.get('task_id')!=task['id'] or row.get('period_start')!=task['start_date']
                    or row.get('period_end')!=task['end_date'] or row.get('unit')!=definition.get('unit')):
                return {**unknown,'reason':'关联底稿的企业、机构、期间或单位不匹配。'}
            value=tax_conditions.typed(row['value'],'decimal')
            return {'state':'known','value':str(value),'unit':row['unit'],'source_record_ids':[row['id']],
                    'reason':'','calculation':row.get('source_position',''),
                    **{key:deepcopy(row.get(key)) for key in ('metric_period','reference','company_scope','simulation')}}
        return {**unknown,'reason':f'尚未绑定已核实来源：{name}；{definition.get("definition","")}'}
    if not chosen.get('reviewed_by') or not chosen.get('basis'):
        return {**unknown,'reason':'指标统计调整口径尚未核对。'}
    if (chosen.get('period_start'),chosen.get('period_end')) != (task['start_date'],task['end_date']):
        return {**unknown,'reason':'检查期间已改变，须重新核对绑定。'}
    try:
        checked=binding(name,chosen['record_ids'],chosen['field'],chosen['aggregation'],chosen['basis'],processing,task,chosen['reviewed_by'])
        if checked['source_signature']!=chosen['source_signature']:
            return {**unknown,'reason':'指标依赖资料已经改变，须重新核对绑定。'}
        catalog={r['id']:r for r in processing['records']}
        selected=[catalog[i] for i in chosen['record_ids']]
        kind=source_kind(name)
        file_ids={r.get('source_file_id') for r in processing.get('records',[]) if r.get('material_type')==kind}
        for issue in [*processing.get('issues',[]),*[n for n in processing.get('notices',[]) if n['code']=='identity_unverified']]:
            global_pending=(kind=='vat_declaration' and issue['code'] in {'forms_unconfirmed','form_coverage_gap','main_form_gap'}
                or kind=='invoice_detail' and issue['code']=='invoice_coverage_gap'
                or issue['code']=='coverage_gap' and {'account_balance':'科目余额表','vat_declaration':'增值税申报表','invoice_detail':'发票明细'}[kind] in issue['message'])
            if issue.get('source_file_id') in file_ids or global_pending:
                return {**unknown,'reason':'绑定资料仍有待核实事项：'+issue['message']}
        for row in selected:
            if (row.get('tenant_id')!=task['tenant_id'] or row.get('company_id')!=task['company_id']
                    or row.get('task_id')!=task['id'] or row.get('duplicate_of') or not row.get('in_scope')
                    or row.get('unit')!=definition.get('unit')):
                return {**unknown,'reason':'指标来源归属、重复、检查区间或单位未通过核对。'}
            if row['material_type']!='invoice_detail' and (row.get('period_start')!=task['start_date'] or row.get('period_end')!=task['end_date']):
                return {**unknown,'reason':'不自动拼接或拆分不同期间的申报与余额。'}
            if row['material_type']=='invoice_detail' and (row.get('void_status') not in {'正常','未作废','normal','valid'}
                    or row.get('red_letter_status') not in {'蓝字','红字','blue','red'}):
                return {**unknown,'reason':'发票红字或作废状态未核实；作废不计入绑定。'}
        values=[]
        for row in selected:
            if row.get('value_states',{}).get(chosen['field'])!='present':
                return {**unknown,'reason':'绑定字段缺失，未将空白视作零。'}
            value=tax_conditions.typed(row.get(chosen['field']),'decimal')
            if row.get('material_type')=='invoice_detail' and row.get('red_letter_status') in {'红字','red'}:
                value=-abs(value)
            values.append(value)
        with localcontext() as context:
            context.prec=64
            value=sum(values,Decimal(0))
        if any(Decimal(row['value'])!=value for row in rows):
            return {**unknown,'reason':'当前绑定与已关联完整材料的同名指标存在冲突，须复核原件。'}
        return {'state':'known','value':str(value),'unit':definition['unit'],'source_record_ids':chosen['record_ids'],
                'reason':'','calculation':chosen['basis'],
                'metric_period':f"{task['start_date']}至{task['end_date']}"}
    except (ValueError,KeyError,TypeError,ArithmeticError):
        return {**unknown,'reason':'指标绑定来源已失效或尚未核实。'}


def audit_records(entry, task):
    """Caller has already authorized the frozen audit against the current user."""
    from src.loader import _parse_period
    dataset=entry['dataset']
    period=_parse_period(dataset.company.period,'关联审计期间')
    if (entry.get('client_id')!=task['company_id'] or entry.get('org_id')!=task['tenant_id']
            or dataset.company.taxpayer_id.strip().upper()!=task['tax_no']
            or period.start.isoformat()!=task['start_date'] or period.end.isoformat()!=task['end_date']):
        raise ValueError('只允许关联同机构、同企业、同完整检查期间的已归档材料。')
    rows=[]
    for name,metric in dataset.metrics.items():
        if metric.value is None or not metric.source:
            continue
        value=Decimal(str(metric.value))
        if not value.is_finite():
            continue
        scope={key:getattr(dataset.company,key) for key in ('industry','region','taxpayer_type','business_scope')}
        rid='audit-metric:'+digest([entry['id'],name,str(value),metric.source,metric.detail,
                                  metric.period,metric.reference,scope,dataset.simulation])[:32]
        rows.append({'id':rid,'material_type':'business_metric','metric_name':name,'value':str(value),
                     'unit':metric_unit(name),'tenant_id':task['tenant_id'],'company_id':task['company_id'],
                     'task_id':task['id'],'period_start':task['start_date'],'period_end':task['end_date'],
                     'source_file_id':'audit:'+entry['id'],'source_position':metric.source,
                     'field_sources':{'value':{'source_label':name,'position':metric.source,'raw_value':str(value)}},
                     'audit_id':entry['id'],'detail':metric.detail,'metric_period':metric.period,
                     'reference':deepcopy(metric.reference),'company_scope':scope,'simulation':dataset.simulation})
    return rows
