"""V12-K01..K40 invoke product modules; source fixtures remain unchanged.

Run directly with --output for per-case JSON. This is synthetic engineering
acceptance, not official form/policy approval or real-model semantic acceptance.
"""
import base64
from copy import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import vat_v12 as fixture
from tests import test_kg_retrieval as kg
from tests import test_vat_reconciliation as vr
from src import vat_reconciliation as vat
from src.kg_retrieval import applicability, bundle, keyword_search, outbound, relation_search, semantic_search
from webapp import knowledge_evidence as service

CASES = fixture.data('gold_cases.json')
RESULTS = []


class VATV12Acceptance(unittest.TestCase):
    login = kg.RetrievalHTTPTests.login
    upload = vr.VATReconciliationTests.upload
    run_check = vr.VATReconciliationTests.run_check

    def setUp(self):
        self.environment=patch.dict(os.environ,{'TAXPEARLS_FIELD_KEY':base64.b64encode(os.urandom(32)).decode(),
            'TAXPEARLS_MATERIAL_KEY':base64.b64encode(os.urandom(32)).decode(),'TAXPEARLS_AI_ENABLED':'0'})
        self.environment.start();self.addCleanup(self.environment.stop)
        kg.RetrievalHTTPTests.setUp(self)

    def graph(self):
        db=fixture.graph(Path(self.tmp.name));self.addCleanup(db.close);return db

    def build(self,db,task=None,**kwargs):
        return bundle.build(db,task or fixture.task(),'DEMO-VAT-R01',fixture.facts(),source_catalog=fixture.source_ids(),**kwargs)

    def review(self,task,*,comparable=True,proof='',revision='DEMO-2026-A'):
        row=next(r for r in task['processing']['records'] if r['material_type']=='vat_declaration')
        response=self.client.post('/api/enterprise/input-tasks/'+task['id']+'/vat-review',json={
            'expected_revision':task['revision'],'mapping_record_id':row['id'],'form_revision':revision,
            'taxpayer_category':'合成一般纳税人','comparable':comparable,'basis':'本案例独立确认同主体同期不含税口径；非真实税收规定。','zero_proof_file_id':proof})
        self.assertEqual(response.status_code,200,response.text)
        return self.client.get('/api/enterprise/input-tasks/'+task['id']).json()

    def start(self,sid,*,review=True,version=1):
        scene=next(s for s in fixture.data('demo_business_scenarios.json') if s['scenario_id']==sid)
        response=self.client.post('/api/enterprise/input-tasks',json={'company_name':'纯合成测试V12-'+sid,
            'tax_no':fixture.TAX,'start_date':scene['period_start'],'end_date':scene['period_end'],
            'applicable_forms':['DEMO必需附表'] if sid=='S07' else []})
        self.assertEqual(response.status_code,200,response.text);task=response.json()
        if any(r['scenario_id']==sid for r in fixture.rows('demo_vat_returns.csv')):
            task=self.upload(task,fixture.workbook(scene,'vat_declaration',revision=version),'DEMO申报.xlsx','vat_declaration')
        invoice_rows=[r for r in fixture.rows('demo_invoices.csv') if r['scenario_id']==sid]
        proof=''
        if invoice_rows or scene['zero_proof']:
            period=(scene['period_start'],'2026-04-30') if sid=='S04' else None
            task=self.upload(task,fixture.workbook(scene,'invoice_detail',period=period),'DEMO发票.xlsx')
            if scene['zero_proof']:proof=task['documents'][-1]['id']
        if review and any(r['material_type']=='vat_declaration' for r in task['processing']['records']):
            task=self.review(task,comparable=scene['can_run_amount_compare'],proof=proof,
                             revision='DEMO-2026-B' if version==2 else 'DEMO-2026-A')
        return task

    def archive(self,task_id,revision):
        url='/api/enterprise/input-tasks/'+task_id+'/reports'
        response=self.client.post(url,json={'expected_revision':revision})
        self.assertEqual(response.status_code,200,response.text)
        row=response.json();html=self.client.get(url+'/'+str(row['version'])+'?format=html')
        self.assertEqual(html.status_code,200);return row,html.content

    def assert_applicable(self,db,cid,day,expected=True):
        row=dict(db.execute('SELECT * FROM Clause WHERE clause_id=?',(cid,)).fetchone())
        policy=dict(db.execute('SELECT * FROM Policy WHERE policy_id=?',(row['policy_id'],)).fetchone())
        decision=applicability.check(row,policy,fixture.task(day),relation_search.dependency_closure(db,{cid})['relationships'])
        self.assertEqual(decision['status']=='applicable',expected,decision)
        return decision

    def execute(self,number):
        case=CASES[number-1]
        if number in {1,2,3,4,5,6,7,8,9,10,12,14,15,19,21,22,23,24,38,39,40}:
            db=self.graph()
            if number==1:
                ids={r['clause_id'] for r in relation_search.search(db,'DEMO-VAT-R01')['hits']}
                self.assertIn('DEMO-C01',ids);return {'relation_hits':sorted(ids)}
            if number==2:
                result=keyword_search.search(db,'红字、作废与跨期',limit=20)
                self.assertIn('DEMO-C10',{r['clause_id'] for r in result['hits']});return result
            if number==3:
                if semantic_search.available()[0] is None:
                    self.skipTest('Optional local semantic model unavailable; semantic acceptance is not proven.')
                result=semantic_search.search(db,'开票的那个月不能直接当作应当纳税的月份',limit=3)
                self.assertEqual(result['status'],'ready',result)
                self.assertIn('DEMO-C12',{r['clause_id'] for r in result['hits']});return result
            if number==4:
                context=relation_search.rule_context(db,'DEMO-VAT-R01')
                self.assertEqual({x['exception_id'] for x in context['exceptions']},{'DEMO-X01','DEMO-X02'})
                self.assertTrue({'DEMO-C02','DEMO-C11'}<=set(context['required']));return {'required':context['required']}
            if number in {5,6,7,14}:
                # Source file puts audit_date under expected. Promote it explicitly
                # into the actual query; never silently use the conflicting March default.
                day=case['expected']['audit_date'];wanted=case['expected']['required_clause_ids'][0]
                self.assert_applicable(db,wanted,day)
                for cid in case['expected']['forbidden_as_applicable_clause_ids']:self.assert_applicable(db,cid,day,False)
                return {'effective_query_date':day,'applicable':wanted}
            if number==8:
                self.assert_applicable(db,'DEMO-C06','2026-03-01');decision=self.assert_applicable(db,'DEMO-C07','2026-03-01',False)
                self.assertIn('地区',''.join(decision['reasons']));return decision
            if number==9:
                decision=self.assert_applicable(db,'DEMO-C09','2026-03-01',False)
                self.assertEqual(decision['status'],'needs_review');return decision
            if number in {10,40}:
                routes={'relation':relation_search.search(db,'DEMO-VAT-R01'),
                    'keyword':keyword_search.search(db,'申报销售数据'),
                    'semantic':semantic_search.search(db,'申报销售数据')}
                fused=bundle.fuse(routes);first=next(x for x in fused if x['clause_id']=='DEMO-C01')
                self.assertGreaterEqual(len(first['matches']),2)
                self.assertEqual(sum(x['clause_id']=='DEMO-C01' for x in fused),1)
                package=self.build(db);self.assertEqual(sum(x['clause_id']=='DEMO-C01' for x in package['policy_evidence']),1)
                return {'matches':len(first['matches']),'unique_clause_count':1}
            if number==12:
                package=self.build(db,token_budget=800)
                text='\n'.join(x['text'] for x in package['policy_evidence']+package['policy_leads'])
                for cid in ['DEMO-C02','DEMO-C11']:
                    self.assertIn(db.execute('SELECT content FROM Clause WHERE clause_id=?',(cid,)).fetchone()[0],text)
                self.assertTrue(package['batching']['unsplittable'] or package['batching']['part_count']>1)
                return {'batching':{k:v for k,v in package['batching'].items() if k!='parts'},'full_exceptions_retained':True}
            if number==15:
                db.execute("UPDATE Condition SET source_clause_id='DEMO-C03' WHERE condition_id='DEMO-CD01'")
                package=self.build(db,fixture.task('2026-10-01'))
                self.assertEqual(package['risk_assessment']['status'],'NEEDS_REVIEW')
                self.assertTrue(any(g['code'] in {'rule_source_changed','required_clause_inapplicable'} for g in package['gaps']))
                self.assertIn('DEMO-C03',{r['clause_id'] for r in package['retrieval_trace']['excluded']} | {r['clause_id'] for r in package['policy_evidence']})
                return {'risk_status':package['risk_assessment']['status'],'gaps':package['gaps']}
            if number==19:
                package=self.build(db)
                self.assertTrue(all(f['state']=='known' for f in package['facts'] if f['metric_id']!='DEMO-MX'))
                self.assertTrue(any(x['trigger_state']=='UNKNOWN' for x in package['exceptions']))
                self.assertEqual(package['risk_assessment']['status'],'NEEDS_REVIEW')
                return {'exception_states':[x['trigger_state'] for x in package['exceptions']]}
            if number in {21,23,24}:
                package=self.build(db)
                if number==21:
                    malicious=case['expected']['input'];package['batching']['parts'][0]['policy_leads'].append({'text':malicious})
                    delivery=outbound.prepare(package,authorized=True,destination='https://api.deepseek.com')
                    self.assertEqual(delivery['outbound_gate'],'allowed')
                    self.assertIn(malicious,json.dumps(delivery['parts'],ensure_ascii=False))
                    self.assertNotIn('tools',delivery['parts'][0])
                elif number==23:
                    package['batching']['parts'][0]['policy_leads'].append({'text':'未授权税号 '+fixture.TAX})
                    delivery=outbound.prepare(package,authorized=True,destination='https://api.deepseek.com')
                    self.assertEqual(delivery['outbound_gate'],'blocked')
                else:
                    delivery=outbound.prepare(package,authorized=True,destination='https://api.deepseek.com')
                    self.assertEqual(delivery['outbound_gate'],'allowed');self.assertEqual(delivery['analysis_mode'],'review_only')
                return {k:v for k,v in delivery.items() if k!='parts'}
            if number==22:
                package=self.build(db)
                answer={'assessments':[{'type':'insufficient_evidence','text':'待核查','rule_id':'DEMO-VAT-R01',
                    'clause_ids':['FAKE-CLAUSE-XYZ'],'source_ids':[],'amounts':[],'dates':[],'required_materials':[]}]}
                with self.assertRaises(ValueError):service.validate_answer(answer,package)
                return {'fabricated_citation_rejected':True}
            if number==38:
                model,reason=semantic_search.available()
                if model is None:
                    self.skipTest(reason)
                first=semantic_search.synchronize(db,model)
                changed=copy(model);changed.fingerprint=model.fingerprint+'-V12-MODEL-CHANGE'
                second=semantic_search.synchronize(db,changed)
                self.assertEqual(len(second['updated_clause_ids']),12)
                self.assertNotEqual(first['model_fingerprint'],second['model_fingerprint']);return second
            if number==39:
                with patch('socket.create_connection',side_effect=AssertionError('offline network attempt')) as network:
                    package=self.build(db,offline=True)
                network.assert_not_called();self.assertEqual(package['execution']['external_api_calls'],0)
                return package['execution']
        sid=case['input']['business_scenario_id']
        if number==16:
            one=self.run_check(self.start('S01',version=1))
            two=self.run_check(self.start('S01',version=2))
            a=one['bundle']['fact_assessment'];b=two['bundle']['fact_assessment']
            self.assertEqual(a['amount'],b['amount']);self.assertEqual(b['mapping_review']['form_revision'],'DEMO-2026-B')
            self.assertNotEqual(one['bundle']['content_hash'],two['bundle']['content_hash'])
            return {'old_version':a['mapping_review']['form_revision'],'new_version':b['mapping_review']['form_revision'],'amount':b['amount']}
        if number==20:
            task=self.start('S01');result=self.run_check(task);row,_=self.archive(task['id'],result['revision'])
            url='/api/enterprise/input-tasks/'+task['id'];self.login('kg-other')
            statuses=[self.client.get(url).status_code]+[self.client.get(url+'/reports/'+str(row['version'])+'?format='+f).status_code for f in ['html','pdf','json']]
            self.assertEqual(statuses,[404]*4);return {'foreign_http':statuses}
        if number==31:
            task=self.start('S02');result=self.run_check(task);old,old_html=self.archive(task['id'],result['revision'])
            task=self.client.get('/api/enterprise/input-tasks/'+task['id']).json()
            task=self.upload(task,vr.invoices([('V12-SUPPLEMENT','L1','5000','蓝字','正常')]),'补传.xlsx')
            self.assertTrue(task['tax_checks'][vat.RULE]['stale'])
            # Supplement's January metadata cannot enter a March task. Rebuild the
            # source with the current period before adopting it (no date guessing).
            from openpyxl import load_workbook
            from io import BytesIO
            wb=load_workbook(BytesIO(vr.invoices([('V12-SUPPLEMENT','L1','5000','蓝字','正常')])))
            wb.active['A3']='统计期间：2026-03-01 至 2026-03-31';wb.active['C6']='2026-03-15'
            raw=BytesIO();wb.save(raw);wb.close()
            wrong=task['documents'][-1]['id']
            task=self.client.post('/api/enterprise/input-tasks/'+task['id']+'/prepare',json={'expected_revision':task['revision'],'source_options':{wrong:{'excluded':True}}}).json()
            task=self.upload(task,raw.getvalue(),'同期间补传.xlsx');task=self.review(task)
            with patch('webapp.knowledge._chat_json') as ai:
                response=self.client.post('/api/enterprise/input-tasks/'+task['id']+'/recheck',json={'expected_revision':task['revision']})
            ai.assert_not_called();self.assertEqual(response.status_code,200,response.text)
            latest=response.json();slot=latest['tax_checks'][vat.RULE]
            self.assertEqual(Decimal(slot['bundle']['fact_assessment']['amount']),Decimal('15000'))
            new,_=self.archive(task['id'],latest['revision']);self.assertGreater(new['version'],old['version'])
            self.assertEqual(self.client.get('/api/enterprise/input-tasks/'+task['id']+'/reports/'+str(old['version'])+'?format=html').content,old_html)
            return {'old_difference':'10000','new_difference':slot['bundle']['fact_assessment']['amount'],'versions':[old['version'],new['version']],'ai_calls':0}
        if number==37:
            task=self.start('S04');result=self.run_check(task)
            value=next(f['value'] for f in result['bundle']['facts'] if f['metric_id']==vat.INVOICE)
            self.assertEqual(Decimal(value),Decimal('3000'));self.assertTrue(any(not r['in_scope'] for r in task['processing']['records'] if r['material_type']=='invoice_detail'))
            return {'march_invoice_metric':value,'april_source_retained':True}
        if number==29:
            task=self.start('S03');raw=fixture.workbook(next(s for s in fixture.data('demo_business_scenarios.json') if s['scenario_id']=='S03'),'invoice_detail')
            task=self.upload(task,raw,'重复文件.xlsx');task=self.review(task);result=self.run_check(task)
            value=next(f['value'] for f in result['bundle']['facts'] if f['metric_id']==vat.INVOICE)
            self.assertEqual(Decimal(value),Decimal('1300'));return {'invoice_metric_after_duplicate':value}
        if number==18:
            task=self.start('S04',review=False)
            scene=next(s for s in fixture.data('demo_business_scenarios.json') if s['scenario_id']=='S04')
            task=self.upload(task,fixture.workbook(scene,'vat_declaration',period=('2026-04-01','2026-04-30')),'不同所属期申报.xlsx','vat_declaration')
            task=self.review(task,comparable=False);result=self.run_check(task)
            self.assertNotEqual(result['bundle']['fact_assessment']['status'],'FACT_CONFIRMED');return result['bundle']['fact_assessment']
        task=self.start(sid);result=self.run_check(task);package=result['bundle'];fact=package['fact_assessment'];risk=package['risk_assessment']
        if number in {11,28,36}:
            self.assertEqual(fact['status'],'FACT_INCOMPLETE');self.assertIsNone(fact['amount']);self.assertEqual(risk['status'],'NEEDS_MORE_DATA')
        elif number==13:
            self.assertEqual(fact['status'],'NOT_COMPARABLE');self.assertEqual(risk['status'],'NEEDS_REVIEW')
        elif number in {17,26,27,30,33}:
            self.assertEqual(fact['status'],'FACT_CONFIRMED',package['facts']);self.assertEqual(Decimal(fact['amount']),Decimal(0))
            self.assertEqual(risk['status'],'NO_RISK_SIGNAL')
            expected=next(s['expected_output_invoice_amount'] for s in fixture.data('demo_business_scenarios.json') if s['scenario_id']==sid)
            self.assertEqual(Decimal(next(f['value'] for f in package['facts'] if f['metric_id']==vat.INVOICE)),Decimal(expected))
            if number==27:self.assertTrue(any(s.startswith('file:') for s in fact['source_ids']))
        elif number==25:
            self.assertEqual(fact['status'],'FACT_CONFIRMED');self.assertEqual(Decimal(fact['amount']),Decimal('10000'))
            self.assertEqual(risk['status'],'NEEDS_REVIEW')
        elif number in {32,35}:
            self.assertNotEqual(fact['status'],'FACT_CONFIRMED');self.assertIsNone(fact['amount'])
            if number==35:
                records=[r for r in task['processing']['records'] if r['material_type']=='invoice_detail']
                self.assertEqual(len(records),2);self.assertFalse(any(r.get('duplicate_of') for r in records))
        elif number==34:
            self.assertEqual(fact['status'],'FACT_UNVERIFIED');self.assertIsNone(fact['amount'])
        else:
            self.fail('No product assertion for V12-K'+str(number))
        return {'fact_assessment':fact,'risk_assessment':risk,'metric_results':package['facts']}


def make_test(number):
    def test(self):
        case=CASES[number-1]
        try:
            actual=self.execute(number)
        except unittest.SkipTest as exc:
            RESULTS.append({'case_id':'V12-'+case['case_id'],'passed':False,'status':'not_verified','reason':str(exc),'expected':case['expected']})
            raise
        except Exception as exc:
            RESULTS.append({'case_id':'V12-'+case['case_id'],'passed':False,'failure':str(exc),'expected':case['expected']})
            raise
        RESULTS.append({'case_id':'V12-'+case['case_id'],'passed':True,'expected':case['expected'],'actual':actual})
    test.__doc__=CASES[number-1]['title']
    return test


for index in range(1,41):
    setattr(VATV12Acceptance,'test_V12_K'+str(index).zfill(2),make_test(index))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(VATV12Acceptance))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'data_scope':'synthetic','formal_policy_acceptance':False,
        'fixture_fingerprint':fixture.identity(),'case_count':40,'passed_count':sum(r['passed'] for r in RESULTS),
        'all_cases_executed':len(RESULTS)==40,'cases':RESULTS},ensure_ascii=False,indent=2),encoding='utf-8')
    complete = len(RESULTS) == 40 and all(row['passed'] for row in RESULTS)
    raise SystemExit(0 if result.wasSuccessful() and complete else 1)
