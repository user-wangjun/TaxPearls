"""Explicit adapters for user-supplied synthetic data, never a production importer."""
import csv
from io import BytesIO
import json
from pathlib import Path
import sqlite3

from openpyxl import Workbook
from src.kg_retrieval.common import digest
from webapp import policy_graph as pg
from tests.test_core_inputs import TAX, OTHER

ROOT = Path(__file__).parent / 'fixtures/vat_v12'


def data(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8-sig'))


def rows(name):
    with (ROOT / name).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def workbook(scenario, kind, *, empty=False, period=None, revision=1):
    """Convert normalized CSV into actually uploaded Excel with original-cell evidence."""
    start,end = period or (scenario['period_start'],scenario['period_end'])
    book=Workbook();sheet=book.active
    sheet.title='DEMO申报主表' if kind=='vat_declaration' else '销项发票明细'
    sheet.append([sheet.title]);sheet.append(['纳税人识别号：'+TAX])
    sheet.append([('税款所属期' if kind=='vat_declaration' else '统计期间')+'：'+start+' 至 '+end])
    sheet.append(['金额单位：元'])
    if kind=='vat_declaration':
        sheet.append(['项目','栏次',*(['备注'] if revision==2 else []),'本期数' if revision==1 else '本月数'])
        entries=[r for r in rows('demo_vat_returns.csv') if r['scenario_id']==scenario['scenario_id']]
        for r in entries:
            sheet.append(['DEMO销售额','DEMO-SALES',*(['新版本移列'] if revision==2 else []),r['declared_value']])
    else:
        sheet.append(['票号','发票明细行号','开具日期','销方税号','购方税号','金额','税额','红字状态','作废状态','进销项','整票抬头金额（不用于逐行累计）'])
        entries=[] if empty else [r for r in rows('demo_invoices.csv') if r['scenario_id']==scenario['scenario_id']]
        for r in entries:
            seller=TAX if r['company_id']=='C-A' else OTHER
            sheet.append([r['invoice_id'],r['line_id'],r['issue_date'],seller,OTHER if seller==TAX else '91310000000000000Z',
                          r['amount_excluding_tax'],r['tax_amount'],'红字' if r['red_flag']=='true' else '蓝字',
                          {'valid':'正常','void':'作废'}.get(r['status'],'unknown'),'销项',
                          '1000' if scenario['scenario_id']=='S11' else None])
    content=BytesIO();book.save(content);book.close();return content.getvalue()


def graph(directory):
    """Enrich missing executable fields explicitly, guarded by an empty test DB."""
    directory=Path(directory);db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON');pg.migrate(db)
    source=data('demo_kg.json')
    assert source['is_fictional_fixture'] is True
    group={name:[] for name in pg.IDS}
    for p in source['Policy']:
        clauses=[c for c in source['Clause'] if c['policy_id']==p['policy_id']]
        original=directory/(p['policy_id']+'.txt');original.write_text('\n'.join(c['text'] for c in clauses),encoding='utf-8')
        import hashlib
        group['Policy'].append({'policy_id':p['policy_id'],'title':p['title'],'document_no':p['doc_no'],'issuer':'合成素材作者',
            'tax_type':'CIT' if p['policy_id']=='DEMO-P06' else 'VAT','region_code':p['region'],
            # Explicit synthetic oracle coverage, NOT an inferred legal end date.
            # Raw fixture nulls are preserved in the source files. This finite
            # horizon lets time/region cases test their intended dimension while
            # the dedicated unknown-effect case remains unverified.
            'effective_from':p['effective_from'],'effective_to':p['effective_to'] or ('2026-12-31' if p['effective_from'] else None),
            'validity_status':'unverified' if p['effective_from'] is None else 'in_force',
            'source_url':'local-fixture://v12/'+p['policy_id'],'raw_file_path':str(original),
            'file_sha256':hashlib.sha256(original.read_bytes()).hexdigest()})
    for index,c in enumerate(source['Clause'],1):
        group['Clause'].append({'clause_id':c['clause_id'],'policy_id':c['policy_id'],'clause_label':c['label'],
            'sort_index':index,'content':c['text'],'effective_from':c['effective_from'],'effective_to':c['effective_to'],
            'source_location':c['policy_id']+'.txt:'+c['label'],'extraction_status':'verified'})
    for r in source['TaxRule']:
        if r['rule_id']!='DEMO-VAT-R01':
            continue  # Five candidate cards have no executable definitions yet.
        group['TaxRule'].append({'rule_id':r['rule_id'],'rule_code':r['rule_id'],'name':r['title'],'tax_type':'VAT',
            'rule_type':r['rule_type'],'valid_from':'2024-01-01','condition_logic_json':{'ref':'DEMO-CD01'} if r['rule_id']=='DEMO-VAT-R01' else {},
            'outcome_text':'纯合成预期，不用于真实判断。'})
    for m in source['Metric']:
        invoice=m['source_schema']=='Invoice'
        group['Metric'].append({**m,'metric_code':m['metric_id'],'value_type':'decimal','unit':'元',
            'source_field':'amount_excluding_tax' if invoice else 'declared_value','aggregation':'sum' if invoice else 'direct',
            'period_basis':'issue_date' if invoice else 'tax_period',
            'source_filter_json':{'direction':'output','red_letter_status':'蓝字','void_status':'正常'} if invoice else
                {'form_type':'main','form_name':'DEMO申报主表','item_key':'DEMO-SALES','column_scope':'本期数'}})
    group['Condition'].append({'condition_id':'DEMO-CD01','rule_id':'DEMO-VAT-R01','left_metric_id':'DEMO-M01',
        'operator':'!=','right_kind':'metric','right_metric_id':'DEMO-M02','unit':'元','condition_source':'internal_screening','source_clause_id':'DEMO-C01'})
    # An independent unknown exception flag avoids inventing an applicable exception.
    group['Metric'].append({**group['Metric'][1],'metric_id':'DEMO-MX','metric_code':'DEMO-MX','name':'合成例外是否存在'})
    group['Condition'].append({'condition_id':'DEMO-CDX','rule_id':'DEMO-VAT-R01','left_metric_id':'DEMO-MX',
        'operator':'>','right_kind':'literal','right_literal':'0','unit':'元','condition_source':'internal_screening','source_clause_id':'DEMO-C02'})
    for x in source['Exception']:
        group['Exception'].append({**x,'name':x['description'],'trigger_logic_json':{'ref':'DEMO-CDX'},'effect':'needs_review'})
    for i,e in enumerate(source['Relationship']):
        kind=e['type'];start,end=e['from'],e['to']
        if kind=='DERIVES_FROM':start,end=end,start;kind='DERIVES_RULE'
        from_type='Clause' if start.startswith('DEMO-C') else 'TaxRule'
        to_type='Clause' if end.startswith('DEMO-C') else 'Exception' if end.startswith('DEMO-X') else 'TaxRule'
        row={'relationship_id':'V12-E'+str(i),'from_type':from_type,'from_id':start,'relation_type':kind,'to_type':to_type,'to_id':end}
        if kind in {'AMENDS','SUPPLEMENTS'}:
            row.update(effective_from=next(c['effective_from'] for c in source['Clause'] if c['clause_id']==start),evidence_clause_id=start)
        group['Relationship'].append(row)
    pg.import_bundle(db,group)
    # Only this in-memory, account-free fixture database is approved by the
    # engineering oracle. Incoming approved_for_fixture_only is never imported.
    db.execute("UPDATE TaxRule SET review_status='approved',reviewed_by='v12-fixture-oracle',reviewed_at='2026-10-16T00:00:00Z'")
    db.execute("UPDATE Relationship SET review_status='approved'");db.commit()
    return db


def task(day='2026-03-01',end=None):
    return {'audit_task_id':'V12-TEST','tax_type':'VAT','region_code':'CN-44','period_start':day,'period_end':end or day,
            'data_scope':'synthetic','comparability':{'DEMO-CD01':{'reviewed':True,'basis':'合成口径预期'}}}


def facts():
    return {mid:{'state':'known','value':value,'unit':'元','source_ids':['V12-SOURCE-'+mid]}
            for mid,value in [('DEMO-M01','100000'),('DEMO-M02','90000')]}


def source_ids():
    return [s for f in facts().values() for s in f['source_ids']]


def identity():
    return digest(data('gold_cases.json'))
