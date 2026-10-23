"""Public-only text RAG contracts, scoped retrieval, provenance and stream harness."""
import asyncio
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from src.kg_retrieval import semantic_search
from src.settings import AISettings
from src.ai_transport import TransportError
from webapp import public_rag as rag, node_qa, policy_graph, app as module
from tests import test_node_qa


def fixtures(db):
    policy_graph.migrate(db)
    for pid, region, title in [('national', 'CN', '增值税测试资料（合成）'), ('gd', 'CN-GD', '广东契税测试资料（合成）'),
                               ('sh', 'CN-SH', '上海契税测试资料（合成）')]:
        db.execute('INSERT INTO Policy(policy_id,title,issuer,region_code,source_url,validity_status,effective_from) '
                   'VALUES (?,?,?,?,?,?,?)', (pid, title, '测试机构', region, 'https://example.invalid/mock/'+pid, 'unverified', '2024-01-01'))
    for cid, pid, parent, text, start, end in [
        ('credit', 'national', None, '合成测试：增值税进项税额抵扣应核对凭证，存在例外，不是真实政策。', '2024-01-01', None),
        ('refund', 'national', None, '合成测试：退税申请需要核对申请条件和材料。', '2024-01-01', None),
        ('gd-rate', 'gd', None, '合成测试：契税地区条件应以广东省相关规定为依据。', '2024-01-01', None),
        ('sh-rate', 'sh', None, '合成测试：契税地区条件应以上海市相关规定为依据。', '2024-01-01', None),
        ('old', 'national', None, '合成测试：旧版研发费用加计扣除口径。', '2020-01-01', '2023-12-31'),
        ('new', 'national', None, '合成测试：新版研发费用加计扣除口径。', '2024-01-01', None),
        ('parent', 'national', None, '合成测试：本章中所说的费用应符合统一定义。', None, None),
        ('child', 'national', 'parent', '合成测试：职工教育经费条件。', None, None),
    ]:
        db.execute('INSERT INTO Clause(clause_id,policy_id,parent_clause_id,clause_label,sort_index,content,source_location,'
                   'effective_from,effective_to,extraction_status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (cid, pid, parent, cid, 1, text, 'C:/private/original.txt:L5', start, end, 'needs_review'))
    db.commit()


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        fixtures(self.db)
        self.local = patch.object(rag, 'semantic', return_value={'status': 'not_ready', 'hits': [], 'message': '仅关键词'})
        self.local.start()
        self.addCleanup(self.local.stop)

    def test_direct_question_retrieves_complete_original_and_provenance(self):
        payload, manifest = rag.retrieve(self.db, '进项税额抵扣有哪些规定？')
        self.assertIn('credit', [row['clause_id'] for row in payload['nodes']])
        source = next(row for row in payload['nodes'] if row['clause_id'] == 'credit')
        self.assertIn('存在例外', source['content'])
        self.assertEqual(source['validity_status'], 'unverified')
        self.assertNotIn('private', json.dumps(payload))
        self.assertEqual(source['source_location'], 'original.txt:L5')
        self.assertEqual(manifest['retrieval_mode'], 'keyword_only')
        self.assertEqual(manifest['utf8_bytes'], len(node_qa.canonical(payload).encode()))

    def test_source_check_cost_is_budgeted_without_silently_dropping_checks(self):
        body='\n'.join('不可省略核对：第'+str(i)+'类主体不得享受该项优惠。' for i in range(30))
        self.db.execute("UPDATE Clause SET content=? WHERE clause_id='credit'",(body,))
        payload,manifest=rag.retrieve(self.db,'不可省略核对',budget=48000)
        self.assertEqual(len(payload['source_checks']),30)
        self.assertEqual(manifest['source_check_count'],30)
        self.assertLessEqual(len(node_qa.canonical(payload).encode()),48000)
        without={k:v for k,v in payload.items() if k!='source_checks'}
        with self.assertRaises(HTTPException) as exc:
            rag.retrieve(self.db,'不可省略核对',budget=len(node_qa.canonical(without).encode())+600)
        self.assertEqual(exc.exception.status_code,413)

    def test_metadata_document_title_is_searchable(self):
        self.db.execute("UPDATE Policy SET document_no='演示字〔2025〕137号' WHERE policy_id='gd'")
        payload, _ = rag.retrieve(self.db, '演示字〔2025〕137号讲什么？')
        self.assertIn('gd-rate', [row['clause_id'] for row in payload['nodes']])

    def test_region_aliases_filter_before_top_k(self):
        for region in ['CN-GD', 'CN-44']:
            payload, _ = rag.retrieve(self.db, '契税地区规定', region=region)
            self.assertIn('gd-rate', [row['clause_id'] for row in payload['nodes']])
            self.assertNotIn('sh-rate', [row['clause_id'] for row in payload['nodes']])
        payload, _ = rag.retrieve(self.db, '契税地区规定', region='CN')
        self.assertFalse(payload['nodes'])

    def test_date_keeps_requested_version_and_unknown_end_is_not_invented(self):
        # Explicit clause dates override the containing document's date.
        old, _ = rag.retrieve(self.db, '研发费用加计扣除', as_of='2023-06-01')
        self.assertEqual([n['clause_id'] for n in old['nodes']], ['old'])
        new, _ = rag.retrieve(self.db, '研发费用加计扣除', as_of='2025-06-01')
        self.assertEqual([n['clause_id'] for n in new['nodes']], ['new'])
        self.assertIsNone(new['nodes'][0]['effective_to'])

    def test_general_question_has_no_implied_date_and_keeps_version_metadata(self):
        payload, manifest = rag.retrieve(self.db, '研发费用加计扣除')
        self.assertEqual({n['clause_id'] for n in payload['nodes']}, {'old', 'new'})
        self.assertIsNone(manifest['reference_date'])
        self.assertEqual(manifest['scope']['date_source'], 'unspecified')
        self.assertNotIn('requested_on', payload)
        self.assertTrue(all(n['effective_from'] for n in payload['nodes']))

    def test_explicit_current_question_and_date_select_matching_version(self):
        current, manifest = rag.retrieve(self.db, '目前研发费用加计扣除')
        self.assertEqual([n['clause_id'] for n in current['nodes']], ['new'])
        self.assertEqual(manifest['scope']['date_source'], 'question_current')
        old, manifest = rag.retrieve(self.db, '2023年6月1日研发费用加计扣除')
        self.assertEqual([n['clause_id'] for n in old['nodes']], ['old'])
        self.assertEqual(manifest['reference_date'], '2023-06-01')

    def test_explicit_year_selects_overlapping_versions_without_inventing_a_day(self):
        historical, manifest = rag.retrieve(self.db, '2023年研发费用加计扣除')
        self.assertEqual([n['clause_id'] for n in historical['nodes']], ['old'])
        self.assertIsNone(manifest['reference_date'])
        self.assertEqual(manifest['scope']['date_source'], 'question_year')
        self.assertEqual(manifest['scope']['period_start'], '2023-01-01')
        self.assertEqual(manifest['scope']['period_end'], '2023-12-31')
        self.db.execute("UPDATE Clause SET effective_from='2023-07-01' WHERE clause_id='new'")
        historical, _ = rag.retrieve(self.db, '2023年研发费用加计扣除')
        self.assertEqual({n['clause_id'] for n in historical['nodes']}, {'old', 'new'})

    def test_province_in_question_narrows_public_text_scope(self):
        payload, manifest = rag.retrieve(self.db, '广东省契税相关规定')
        self.assertEqual(manifest['region'], 'CN-GD')
        self.assertNotIn('sh-rate', [n['clause_id'] for n in payload['nodes']])

    def test_single_short_tax_term_remains_searchable(self):
        payload, _ = rag.retrieve(self.db, '退税')
        self.assertIn('refund', [n['clause_id'] for n in payload['nodes']])

    def test_no_hit_does_not_invent_semantic_success(self):
        payload, manifest = rag.retrieve(self.db, '火星飞船驾驶证')
        self.assertFalse(payload['nodes'])
        self.assertEqual(manifest['semantic_status'], 'not_ready')

    def test_parent_context_included_and_missing_parent_fails_closed(self):
        payload, _ = rag.retrieve(self.db, '职工教育经费')
        self.assertEqual({n['clause_id'] for n in payload['nodes']}, {'child', 'parent'})
        self.db.execute("UPDATE Clause SET extraction_status='pending' WHERE clause_id='parent'")
        with self.assertRaises(HTTPException):
            rag.retrieve(self.db, '职工教育经费')

    def test_capacity_preserves_complete_clause_or_refuses(self):
        self.db.execute("UPDATE Clause SET content=content||? WHERE clause_id='credit'", ('完整原文'*2000,))
        with self.assertRaises(HTTPException) as error:
            rag.retrieve(self.db, '进项税额抵扣', budget=3000)
        self.assertEqual(error.exception.status_code, 413)

    def test_oversized_first_hit_does_not_hide_smaller_complete_evidence(self):
        self.db.execute("UPDATE Clause SET content=content||? WHERE clause_id='credit'", ('完整原文'*2000,))
        with patch.object(rag, 'semantic', return_value={'status': 'ready', 'hits': [('credit', .9), ('refund', .8)]}):
            payload, manifest = rag.retrieve(self.db, '进项税额抵扣与退税', budget=3500)
        self.assertIn('refund', [n['clause_id'] for n in payload['nodes']])
        self.assertNotIn('credit', [n['clause_id'] for n in payload['nodes']])
        self.assertGreater(manifest['coverage']['omitted_candidates'], 0)

    def test_short_policy_keeps_conditions_exceptions_and_transition_together(self):
        self.db.execute("UPDATE Clause SET content='合成登记办法：超过标准当期生效。' WHERE clause_id='credit'")
        self.db.execute("UPDATE Clause SET content='例外：自然人不办理登记。旧办法同时废止。' WHERE clause_id='refund'")
        import hashlib
        texts=[self.db.execute('SELECT content FROM Clause WHERE clause_id=?',(c,)).fetchone()[0] for c in ('credit','refund')]
        checksum=hashlib.sha256(''.join(texts).encode()).hexdigest()
        for cid,start,end in [('credit',0,len(texts[0])),('refund',len(texts[0]),sum(map(len,texts)))]:
            self.db.execute('UPDATE Clause SET source_location=? WHERE clause_id=?',(json.dumps({'text_sha256':checksum,'start_char':start,'end_char_exclusive':end}),cid))
        payload, _ = rag.retrieve(self.db, '超过标准当期登记生效')
        self.assertIn('refund', {c for n in payload['nodes'] for c in n.get('source_clause_ids',[n['clause_id']])})
        self.assertIn('例外：自然人', ''.join(n['content'] for n in payload['nodes']))

    def test_long_fact_pattern_keeps_the_actual_question_at_the_end_searchable(self):
        question = ''.join(chr(0x4e00+i) for i in range(200))+'需要核对职业资格继续教育。'
        self.assertIn('职业资格', rag.terms(question))

    def test_business_wording_can_retrieve_invoice_authenticity_without_legal_term(self):
        self.db.execute("UPDATE Policy SET tax_type='TAX_ADMIN' WHERE policy_id='national'")
        self.db.execute("UPDATE Clause SET content='任何单位和个人不得开具与实际经营业务情况不符的发票。' WHERE clause_id='credit'")
        payload, _ = rag.retrieve(self.db, '没有实际咨询服务，却通过关联个体户开票，能作为费用列支吗？')
        self.assertIn('credit', [n['clause_id'] for n in payload['nodes']])

    def test_historical_scope_excludes_undated_snapshot_and_catalog_annotations(self):
        self.db.execute("INSERT INTO Policy(policy_id,title,issuer,source_url) VALUES ('undated','增值税','测试','https://example.invalid/unknown')")
        self.db.execute("INSERT INTO Clause(clause_id,policy_id,clause_label,sort_index,content,extraction_status) VALUES ('undated','undated','快照',1,'进项税额抵扣：未来政策','parsed')")
        self.db.execute("INSERT INTO Clause(clause_id,policy_id,clause_label,sort_index,content,source_location,extraction_status) VALUES ('catalog','national','整理',1,'进项税额抵扣目录说明','{\"catalog_annotation\":true}','parsed')")
        payload, _ = rag.retrieve(self.db, '进项税额抵扣', as_of='2024-06-01')
        self.assertFalse({'undated','catalog'} & {n['clause_id'] for n in payload['nodes']})

    def test_retired_clause_is_only_a_historical_reference_when_comparing_versions(self):
        payload, _ = rag.retrieve(self.db, '研发费用加计扣除新旧规定衔接', as_of='2025-06-01')
        old = next(n for n in payload['nodes'] if n['clause_id']=='old')
        self.assertEqual(old['temporal_role'], 'historical_reference')
        self.assertEqual(next(n for n in payload['nodes'] if n['clause_id']=='new')['temporal_role'], 'in_scope')

    def test_fixture_urls_credentials_and_malformed_urls_not_sent(self):
        for url in ['local-fixture://demo', 'https://secret:key@example.invalid', 'http://[invalid']:
            self.db.execute('UPDATE Policy SET source_url=?', (url,))
            payload, _ = rag.retrieve(self.db, '进项税额抵扣')
            self.assertFalse(payload['nodes'])

    def test_semantic_and_keyword_fusion_deduplicates_evidence(self):
        with patch.object(rag, 'semantic', return_value={'status': 'ready', 'hits': [('credit', .83), ('refund', .72)]}):
            payload, manifest = rag.retrieve(self.db, '进项税额抵扣')
        self.assertEqual(payload['nodes'][0]['clause_id'], 'credit')
        self.assertEqual(len([n for n in payload['nodes'] if n['clause_id'] == 'credit']), 1)
        self.assertEqual(manifest['retrieval_mode'], 'hybrid')

    def test_short_followup_borrows_question_topic_but_new_topic_does_not(self):
        payload, _ = rag.retrieve(self.db, '能举例吗', ['进项税额抵扣有哪些规定？'])
        self.assertIn('credit', [n['clause_id'] for n in payload['nodes']])
        payload, _ = rag.retrieve(self.db, '火星飞船驾驶证', ['进项税额抵扣有哪些规定？'])
        self.assertFalse(payload['nodes'])

    def test_clarifying_province_and_year_can_reuse_previous_topic(self):
        payload, manifest = rag.retrieve(self.db, '广东省，2025年', ['契税地区规定'])
        self.assertIn('gd-rate', [n['clause_id'] for n in payload['nodes']])
        self.assertNotIn('sh-rate', [n['clause_id'] for n in payload['nodes']])
        self.assertEqual(manifest['region'], 'CN-GD')
        self.assertEqual(manifest['scope']['date_source'], 'question_year')

    def test_clarification_replaces_previous_reference_date(self):
        payload, manifest = rag.retrieve(self.db, '2023年6月1日', ['2025年1月1日研发费用加计扣除'])
        self.assertEqual([n['clause_id'] for n in payload['nodes']], ['old'])
        self.assertEqual(manifest['reference_date'], '2023-06-01')

    def test_followup_date_replaces_previous_year_and_topic_change_drops_scope(self):
        payload, manifest = rag.retrieve(self.db, '2025年1月1日', ['2023年研发费用加计扣除'])
        self.assertEqual([n['clause_id'] for n in payload['nodes']], ['new'])
        self.assertNotIn('period_start', manifest['scope'])
        payload, manifest = rag.retrieve(self.db, '研发费用加计扣除', ['广东省2023年契税规定'])
        self.assertEqual({n['clause_id'] for n in payload['nodes']}, {'old', 'new'})
        self.assertEqual(manifest['region'], '')
        self.assertEqual(manifest['scope']['date_source'], 'unspecified')

    def test_no_enterprise_or_rule_tables_are_queried(self):
        seen = []
        self.db.set_trace_callback(seen.append)
        rag.retrieve(self.db, '进项税额抵扣')
        text = '\n'.join(seen).lower()
        for table in ['audits', 'material_originals', 'core_input_tasks', 'clients', 'taxrule']:
            self.assertNotIn(table, text)

    def test_public_interpretation_evidence_is_mandatory_even_without_matching_words(self):
        self.db.execute("UPDATE Clause SET content='合成解释：本公告当期按月拆分，不是原季度。' WHERE clause_id='refund'")
        self.db.execute("INSERT INTO Relationship(relationship_id,from_type,from_id,relation_type,to_type,to_id,evidence_clause_id,review_status) VALUES('interpret','Policy','national','INTERPRETS','Policy','national','refund','candidate')")
        payload, _=rag.retrieve(self.db,'进项税额抵扣',as_of='2024-06-01')
        self.assertIn('refund',[n['clause_id'] for n in payload['nodes']])

    def test_later_repeal_explains_recorded_end_date_without_applying_future_conditions(self):
        self.db.execute("UPDATE Policy SET effective_to='2025-12-31' WHERE policy_id='national'")
        self.db.execute("INSERT INTO Policy(policy_id,title,issuer,region_code,source_url,validity_status,effective_from) VALUES('later','合成后续替代办法','测试','CN','https://example.invalid/later','unverified','2026-01-01')")
        original='合成后续变更：自2026年1月1日起施行，旧办法同时废止。\n附件：后续人才目录\n'+'后续目录内容。'*2000
        for cid,text in [('repeal',original),('future-condition','合成新条件：居住满183天。')]:
            self.db.execute("INSERT INTO Clause(clause_id,policy_id,clause_label,sort_index,content,effective_from,extraction_status) VALUES(?,?,?,0,?,?,?)",(cid,'later',cid,text,'2026-01-01','parsed'))
        self.db.execute("INSERT INTO Relationship(relationship_id,from_type,from_id,relation_type,to_type,to_id,evidence_clause_id,effective_from,review_status) VALUES('later-repeal','Policy','later','REPEALS','Policy','national','repeal','2026-01-01','candidate')")
        payload,_=rag.retrieve(self.db,'进项税额抵扣',as_of='2025-06-01')
        chronology=next((n for n in payload['nodes'] if n['clause_id']=='repeal'),None)
        self.assertIsNotNone(chronology)
        self.assertEqual(chronology['temporal_role'],'later_change_reference')
        self.assertNotIn('future-condition',[n['clause_id'] for n in payload['nodes']])
        self.assertNotIn('后续目录内容',chronology['content'])
        self.assertEqual(chronology['content'],original[:chronology['evidence_spans'][0]['source_char_end']])

    def test_explicit_tax_topic_does_not_fill_budget_with_another_tax(self):
        self.db.execute("UPDATE Policy SET tax_type='EIT' WHERE policy_id='gd'")
        self.db.execute("UPDATE Clause SET content='增值税纳税登记和纳税申报（另一税种的合成干扰文本）' WHERE clause_id='gd-rate'")
        payload, _=rag.retrieve(self.db,'增值税纳税登记和纳税申报')
        self.assertNotIn('gd-rate',[n['clause_id'] for n in payload['nodes']])

    def test_multi_topic_retrieval_keeps_secondary_subject_evidence(self):
        self.db.execute("UPDATE Policy SET tax_type='TAX_ADMIN' WHERE policy_id='national'")
        with patch.object(rag,'semantic',return_value={'status':'ready','hits':[('credit',.9)],'facet_hits':[('refund',.85)]}):
            payload,_=rag.retrieve(self.db,'工资改为咨询费后开票')
        self.assertIn('refund',[n['clause_id'] for n in payload['nodes']])

    def test_subject_vocabulary_also_retrieves_exact_body_evidence_without_vectors(self):
        self.db.execute("UPDATE Clause SET content='合成规则：农业生产者销售的自产农产品免征增值税。' WHERE clause_id='refund'")
        self.db.execute("UPDATE Clause SET content='合成规则：一般纳税人登记管理。' WHERE clause_id='credit'")
        with patch.object(rag,'semantic',return_value={'status':'not_ready','hits':[]}):
            payload,_=rag.retrieve(self.db,'养殖企业的一般纳税人登记依据',as_of='2025-01-01')
        self.assertIn('refund',[n['clause_id'] for n in payload['nodes']])


class SourceCheckTests(unittest.TestCase):
    def test_inability_and_waiver_exceptions_remain_visible(self):
        text='第一条 无法取得某材料的，须提交替代证明。\n第二条 符合特殊条件的，无需办理该手续。'
        self.assertEqual(len(rag.source_checks([{'id':'S1','content':text}])),2)

    def test_exclusion_intro_keeps_all_nested_items_of_its_article(self):
        text='第一条 除以下情形外，应办理登记：\n（一）符合条件的非企业单位。\n（二）自然人。\n第二条 其他事项另行核实。'
        notes=rag.source_checks([{'id':'S1','content':text,'temporal_role':'in_scope'}])
        self.assertEqual(len(notes),1)
        self.assertIn('自然人',notes[0]['text'])
        self.assertIn('非企业单位',notes[0]['text'])
        self.assertNotIn('第二条',notes[0]['text'])

    def test_unheaded_exclusion_list_keeps_the_list_with_its_intro(self):
        text='下列主体不得享受优惠：\n（一）甲类主体。\n（二）乙类主体。'
        notes=rag.source_checks([{'id':'S1','content':text,'temporal_role':'in_scope'}])
        self.assertEqual(notes[0]['text'],text)

    def test_exact_exclusion_spans_are_deduplicated_and_keep_review_state(self):
        text='适用条件。\n第八条 被列为限制对象的主体，不得享受该项优惠。\n第三条 偶然转让收入不计入累计额。'
        node={'id':'S1','content':text,'source_url':'https://example.invalid/p','version':'v1',
              'validity_status':'unverified','temporal_role':'in_scope'}
        notes=rag.source_checks([node,{**node,'id':'S2'}])
        self.assertEqual(len(notes),2)
        for note in notes:
            self.assertEqual(note['text'],text[note['start_char']:note['end_char']])
            self.assertEqual(note['source_content_hash'],hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(note['validity_status'],'unverified')
            self.assertEqual(note['source_id'],'S1')

    def test_historical_and_future_rules_are_not_current_exclusion_checks(self):
        nodes=[{'id':'S'+str(i),'content':'某类主体不得享受该优惠。','temporal_role':role}
               for i,role in enumerate(['historical_reference','later_change_reference','in_scope'],1)]
        self.assertEqual([n['source_id'] for n in rag.source_checks(nodes)],['S3'])


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    def package(self):
        nodes = [{'id': 'S1', 'label': '合成测试原文', 'content': '合成测试：存在例外。'}]
        return {'nodes': nodes, 'question': '解释'}, {'content_hash': 'test', 'node_ids': ['S1']}

    async def test_omitted_source_exclusion_is_carried_into_final_answer_with_exact_source(self):
        payload,manifest=self.package()
        exclusion='第八条 被列为限制对象的主体，不得享受该项优惠。'
        payload['nodes'].append({'id':'S2','content':exclusion,'label':'合成排除条款','temporal_role':'in_scope'})
        calls=[]
        async def fake(*a,**kw):
            calls.append(1)
            raw=json.dumps({'status':'answerable','citations':['S1'],'summary':'一般条件','answer':'仅有任职记录不足。[S1]'})
            for char in raw:yield char
        with patch.object(node_qa,'chat_stream',fake):
            events=[e async for e in rag.answer_events(AISettings(),payload,manifest,max_tokens=1024)]
        final=events[-1]
        self.assertEqual(final['type'],'done')
        self.assertNotIn(exclusion,final['model_answer'])
        self.assertIn(exclusion,final['answer'])
        self.assertIn('[S2]',final['answer'])
        self.assertEqual([c['id'] for c in final['citations']],['S1','S2'])
        self.assertTrue(final['source_check_coverage']['all_extracted_items_shown'])
        self.assertEqual(''.join(e['text'] for e in events if e['type']=='delta'),final['answer'])
        self.assertEqual(len(calls),1)

    async def test_checklist_cannot_rescue_invalid_model_output_or_add_fabricated_sources(self):
        payload,manifest=self.package()
        payload['nodes'][0]['content']='限制对象不得享受优惠；原文注记[S999]。'
        for citation in ['S1','S999']:
            async def fake(*a,**kw):
                yield json.dumps({'status':'answerable','citations':[citation],'summary':'依据','answer':'说明['+citation+']'})
            with patch.object(node_qa,'chat_stream',fake):
                events=[e async for e in rag.answer_events(AISettings(),payload,manifest,max_tokens=1024)]
            if citation=='S999':self.assertEqual(events[-1]['type'],'error')
            else:
                self.assertEqual(events[-1]['type'],'done')
                self.assertNotIn('[S999]',events[-1]['answer'])
                self.assertEqual([n['id'] for n in events[-1]['citations']],['S1'])

    async def test_tampered_source_checklist_never_reaches_provider(self):
        payload,manifest=self.package()
        payload['source_checks']=[{'text':'伪造的排除条件','source_id':'S1'}]
        with patch.object(node_qa,'chat_stream') as upstream:
            events=[e async for e in rag.answer_events(AISettings(),payload,manifest,max_tokens=1024)]
        upstream.assert_not_called()
        self.assertEqual(events[-1]['type'],'error')

    async def test_incomplete_before_any_answer_can_recover_once(self):
        calls=[]
        async def fake(*a,**kw):
            calls.append(1)
            if len(calls)==1:raise TransportError('incomplete')
            yield json.dumps({'status':'answerable','citations':['S1'],'summary':'依据','answer':'有例外。[S1]'})
        with patch.object(node_qa,'chat_stream',fake):
            events=[e async for e in rag.answer_events(AISettings(),*self.package(),max_tokens=1024)]
        self.assertEqual(len(calls),2)
        self.assertEqual(events[-1]['type'],'done')
        self.assertEqual(events[-1]['generation_attempts'],2)

    async def test_started_answer_or_repeated_incomplete_is_never_replayed(self):
        for visible in [False,True]:
            calls=[]
            async def fake(*a,**kw):
                calls.append(1)
                if visible:yield '{"status":"answerable","citations":["S1"],"summary":"","answer":"部分'
                raise TransportError('incomplete')
            with patch.object(node_qa,'chat_stream',fake):
                events=[e async for e in rag.answer_events(AISettings(),*self.package(),max_tokens=1024)]
            self.assertEqual(len(calls),1 if visible else 2)
            self.assertEqual(events[-1]['type'],'error')

    async def test_answer_has_inline_sources_and_uses_rag_prompt(self):
        seen = []
        async def fake(settings, messages, **kw):
            seen.extend(messages)
            text = json.dumps({'status': 'answerable', 'citations': ['S1'], 'summary': '原文说明', 'answer': '存在例外。[S1]'}, ensure_ascii=False)
            for pos in range(0, len(text), 4):
                yield text[pos:pos+4]
        with patch.object(node_qa, 'chat_stream', fake):
            events = [e async for e in rag.answer_events(AISettings(), *self.package(), max_tokens=1024)]
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('不要求用户先选择', seen[0]['content'])
        self.assertEqual(events[-1]['citations'][0]['id'], 'S1')

    async def test_missing_inline_or_fabricated_citation_not_completed(self):
        for answer in ['没有引用', '错误引用[S999]', '格式错[节点ID：S1]']:
            async def fake(*a, **kw):
                yield json.dumps({'status': 'answerable', 'citations': ['S1'], 'summary': '', 'answer': answer})
            with patch.object(node_qa, 'chat_stream', fake):
                events = [e async for e in rag.answer_events(AISettings(), *self.package(), max_tokens=1024)]
            self.assertEqual(events[-1]['type'], 'error')

    async def test_known_bare_source_ids_are_linked_without_replaying_or_changing_claims(self):
        payload,manifest=self.package()
        payload['nodes'].append({**payload['nodes'][0],'id':'S10'})
        raw=json.dumps({'status':'answerable','citations':['S1','S10'],'summary':'依据',
                        'answer':'见S1第八条，另见S10。'},ensure_ascii=False)
        async def fake(*a,**kw):
            for char in raw:yield char
        with patch.object(node_qa,'chat_stream',fake):
            events=[e async for e in rag.answer_events(AISettings(),payload,manifest,max_tokens=1024)]
        self.assertEqual(events[-1]['type'],'done')
        self.assertEqual(events[-1]['answer'],'见[S1]第八条，另见[S10]。')
        self.assertEqual(''.join(e['text'] for e in events if e['type']=='delta'),events[-1]['answer'])

    async def test_unknown_bare_source_id_is_never_repaired_into_a_valid_citation(self):
        async def fake(*a,**kw):
            yield json.dumps({'status':'answerable','citations':['S1'],'summary':'依据','answer':'见S999或[S1]。'})
        with patch.object(node_qa,'chat_stream',fake):
            events=[e async for e in rag.answer_events(AISettings(),*self.package(),max_tokens=1024)]
        self.assertEqual(events[-1]['type'],'error')

    async def test_no_evidence_never_calls_provider(self):
        payload, manifest = self.package()
        payload['nodes'] = []
        with patch.object(node_qa, 'chat_stream') as upstream:
            events = [e async for e in rag.answer_events(AISettings(), payload, manifest, max_tokens=1024)]
        upstream.assert_not_called()
        self.assertEqual(events[-1]['status'], 'insufficient')
        self.assertIsNone(events[-1]['model'])

    async def test_unrelated_answer_is_replaced_and_never_streamed(self):
        async def fake(*a, **kw):
            yield json.dumps({'status': 'out_of_scope', 'citations': [], 'summary': 'private', 'answer': 'unrelated'})
        with patch.object(node_qa, 'chat_stream', fake):
            events = [e async for e in rag.answer_events(AISettings(), *self.package(), max_tokens=1024)]
        self.assertEqual(events[-1]['answer'], rag.REFUSAL)
        self.assertNotIn('delta', [e['type'] for e in events])

    async def test_cancel_closes_nested_generator(self):
        closed = []
        async def fake(*a, **kw):
            try:
                yield '{"status":"answerable","citations":["S1"],"summary":"","answer":"部分'
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        with patch.object(node_qa, 'chat_stream', fake):
            source = rag.answer_events(AISettings(), *self.package(), max_tokens=1024)
            while (await anext(source))['type'] != 'delta':
                pass
            await source.aclose()
        self.assertEqual(closed, [True])


class GenerationPlanTests(unittest.TestCase):
    def test_max_reasoning_has_bounded_combined_budget_without_changing_effort(self):
        original=AISettings(reasoning_effort='max',reasoning_protocol='deepseek')
        planned,output=rag.generation_plan(original,{'max_output_tokens':393216})
        self.assertEqual(output,32768)
        self.assertEqual(planned.reasoning_effort,'max')
        self.assertEqual(planned.timeout,180)
        self.assertEqual(original.timeout,90)
        self.assertEqual(original.max_tokens,8192)

    def test_provider_ceiling_and_unknown_capacity_are_respected(self):
        original=AISettings(reasoning_effort='max',reasoning_protocol='deepseek')
        self.assertEqual(rag.generation_plan(original,{'max_output_tokens':16000})[1],16000)
        self.assertEqual(rag.generation_plan(original,{})[1],8192)
        ordinary=AISettings(reasoning_effort='high',reasoning_protocol='deepseek')
        self.assertEqual(rag.generation_plan(ordinary,{'max_output_tokens':393216}),(ordinary,8192))


class EndpointTests(unittest.TestCase):
    setUp = test_node_qa.EndpointTests.setUp

    def test_endpoint_reserves_actual_combined_generation_budget(self):
        captured=[]
        async def fake(settings,messages,**kw):
            captured.append((settings,kw))
            yield json.dumps({'status':'answerable','citations':['S1'],'summary':'依据','answer':'示例[S1]'})
        payload={'nodes':[{'id':'S1','label':'合成原文','content':'合成：限制主体不得享受该优惠。'}]}
        manifest={'content_hash':'synthetic','node_ids':['S1']}
        settings=AISettings(enabled=True,api_key='synthetic',reasoning_effort='max',reasoning_protocol='deepseek')
        with patch('webapp.ai_keys.apply',return_value=settings), \
             patch('webapp.ai_keys.capability',return_value={'context_window':1048576,'max_output_tokens':393216}), \
             patch.object(rag,'retrieve',return_value=(payload,manifest)), \
             patch.object(node_qa,'chat_stream',fake):
            response=self.client.post('/api/knowledge/rag-ask',json={'question':'解释规定'})
        self.assertEqual(response.status_code,200)
        self.assertIn('"type": "done"',response.text)
        self.assertEqual(captured[0][0].reasoning_effort,'max')
        self.assertEqual(captured[0][1]['max_tokens'],32768)
        self.assertEqual(manifest['output_reserve'],32768)
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertNotIn('不得享受',events[-1]['model_answer'])
        self.assertIn('不得享受',events[-1]['answer'])
        self.assertTrue(events[-1]['source_check_coverage']['all_extracted_items_shown'])

    def test_rag_rejects_enterprise_and_client_evidence_and_validates_filters(self):
        for extra in [{'node_id': 'rule'}, {'audit_id': 'secret'}, {'task_id': 'secret'}, {'nodes': []},
                      {'region': 'CN-NOTREAL'}, {'as_of': 'bad'}, {'previous_questions': ['x']*4}]:
            self.assertEqual(self.client.post('/api/knowledge/rag-ask', json={'question': '解释', **extra}).status_code, 422)
        self.assertEqual(self.client.post('/api/knowledge/rag-ask', content=b'x'*24577).status_code, 413)
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/knowledge/rag-ask', json={'question': '解释'}).status_code, 401)

    def test_rag_empty_library_streams_insufficient_even_without_model(self):
        with patch('webapp.ai_keys.apply', return_value=AISettings()), patch.object(node_qa, 'chat_stream') as upstream:
            response = self.client.post('/api/knowledge/rag-ask', json={'question': '进项税额抵扣'})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(s[6:]) for s in response.text.splitlines() if s.startswith('data: ')]
        self.assertEqual(events[-1]['status'], 'insufficient')
        self.assertFalse(module._node_question_active)
        upstream.assert_not_called()

    def test_real_retrieval_stream_citations_and_public_only_payload(self):
        with module.store.connect() as db:
            fixtures(db)
        seen = []
        async def fake(settings, messages, **kw):
            payload = json.loads(messages[-1]['content'])
            seen.append(payload)
            sid = payload['nodes'][0]['id']
            yield json.dumps({'status': 'answerable', 'citations': [sid], 'summary': '', 'answer': '合成测试解释。['+sid+']'})
        with patch('webapp.ai_keys.apply', return_value=AISettings(enabled=True, api_key='synthetic')), \
             patch.object(rag, 'semantic', return_value={'status': 'not_ready', 'hits': [], 'message': '仅关键词'}), \
             patch.object(node_qa, 'chat_stream', fake):
            response = self.client.post('/api/knowledge/rag-ask', json={'question': '进项税额抵扣', 'region': 'CN', 'as_of': '2025-01-01'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "done"', response.text)
        self.assertIn('原文', response.text)
        self.assertNotIn('selected', seen[0])
        self.assertNotIn('edges', seen[0])
        self.assertTrue(all('source_url' in n and 'content' in n for n in seen[0]['nodes']))
        self.assertNotIn('private', json.dumps(seen))
        self.assertFalse(module._node_question_active)

    def test_rag_session_revocation_stops_content_and_closes_generator(self):
        with module.store.connect() as db:
            fixtures(db)
        closed = []
        async def fake(*args, **kw):
            try:
                with module.store.connect() as db:
                    db.execute("UPDATE users SET active=0 WHERE username='node-admin'")
                yield json.dumps({'status': 'answerable', 'citations': ['S1'], 'summary': '', 'answer': 'must not escape[S1]'})
            finally:
                closed.append(True)
        with patch('webapp.ai_keys.apply', return_value=AISettings(enabled=True, api_key='synthetic')), \
             patch.object(rag, 'semantic', return_value={'status': 'not_ready', 'hits': []}), \
             patch.object(node_qa, 'chat_stream', fake):
            response = self.client.post('/api/knowledge/rag-ask', json={'question': '进项税额抵扣'})
        self.assertNotIn('must not escape', response.text)
        self.assertIn('访问权限已变化', response.text)
        self.assertEqual(closed, [True])
        self.assertFalse(module._node_question_active)

    def test_concurrent_node_and_rag_requests_share_one_slot(self):
        with module.store.connect() as db:
            user = dict(db.execute("SELECT id FROM users WHERE username='node-admin'").fetchone())
        module._node_question_active.add(user['id'])
        response = self.client.post('/api/knowledge/rag-ask', json={'question': '进项税额抵扣'})
        self.assertEqual(response.status_code, 429)


class LocalSemanticTests(unittest.TestCase):
    def test_actual_encoder_and_durable_vectors_are_used(self):
        # Optional local runtime, no downloads or external service.
        model, reason = semantic_search.available()
        if model is None:
            self.skipTest(reason)
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        fixtures(db)
        result = rag.semantic(db, '进项税额抵扣需要核对凭证', {'credit': (dict(db.execute(
            "SELECT * FROM Clause WHERE clause_id='credit'").fetchone()), {})})
        self.assertEqual(result['status'], 'ready')
        self.assertIn('credit', [hit[0] for hit in result['hits']])
        self.assertGreater(db.execute('SELECT count(*) FROM kg_clause_vectors').fetchone()[0], 0)
