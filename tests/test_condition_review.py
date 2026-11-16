import copy
import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from src.settings import AISettings
from src.ai_transport import TransportError
from webapp import condition_review as review, node_qa, public_rag as rag


class ConditionReviewTests(unittest.IsolatedAsyncioTestCase):
    def upstream(self, result):
        async def stream(*a, **kw):
            yield json.dumps(result)
        return stream

    def package(self):
        node = {'id':'S1','label':'学校范围','content':'学校，是指依法设立的学历教育机构，以及技工学校。'}
        payload = {'question':'普通学历学校不是技工学校，属于学校吗？','previous_questions':[], 'nodes':[node]}
        draft = {'status':'answerable','citations':['S1'],'summary':'依据学校范围','answer':'必须同时属于技工学校。[S1]'}
        result = {'decision':'revise','checks':[{'relation':'any_of','source_id':'S1',
                  'quote':node['content'],'draft_quote':draft['answer'],'verdict':'unsupported',
                  'known_facts':['普通学历学校不是技工学校'],
                  'missing_facts':[],'note':'两类机构为并列范围。'}],
                  'issues':['把并列范围误当累积条件。'],
                  'response':{**draft,'answer':'依法设立的学历教育机构属于学校范围，无需同时属于技工学校。[S1]'}}
        return payload, draft, result

    def test_quote_facts_citations_and_schema_are_checked(self):
        payload,draft,result=self.package()
        self.assertEqual(review.validate(result,payload,payload['nodes'],draft,rag.validate)['answer'],result['response']['answer'])
        spaced=copy.deepcopy(result)
        spaced['checks'][0]['draft_quote']='\n'+draft['answer']+'\n'
        spaced['checks'][0]['known_facts']=['普通学历学校\n不是技工学校']
        self.assertEqual(review.validate(spaced,payload,payload['nodes'],draft,rag.validate)['answer'],result['response']['answer'])
        for mutation in ('quote','fact','citation','extra'):
            bad=copy.deepcopy(result)
            if mutation=='quote':bad['checks'][0]['quote']='不存在的原文'
            elif mutation=='fact':bad['checks'][0]['known_facts']=['已经取得资质']
            elif mutation=='citation':bad['response']['citations']=['S99']
            else:bad['command']='执行'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                review.validate(bad,payload,payload['nodes'],draft,rag.validate)

    async def test_reviewed_draft_never_leaks_and_revised_stream_matches_done(self):
        payload,draft,result=self.package()
        async def fake(*a,**kw):
            text=json.dumps(draft,ensure_ascii=False)
            for offset in range(0,len(text),5):yield text[offset:offset+5]
        with patch.object(node_qa,'chat_stream',fake),patch.object(review,'chat_stream',self.upstream(result)):
            events=[e async for e in rag.answer_events(AISettings(),payload,{'content_hash':'test'},
                                                       max_tokens=1024,review_settings=AISettings())]
        final=events[-1]
        self.assertEqual(final['condition_review']['status'],'revised')
        self.assertEqual(final['answer'],result['response']['answer'])
        self.assertEqual(''.join(e['text'] for e in events if e['type']=='delta'),final['answer'])
        self.assertNotIn(draft['answer'],json.dumps(events,ensure_ascii=False))

    async def test_failed_review_withholds_draft_and_revoked_identity_skips_paid_review(self):
        payload,draft,_=self.package()
        async def fake(*a,**kw):yield json.dumps(draft)
        with patch.object(node_qa,'chat_stream',fake),patch.object(review,'chat_stream',self.upstream({})):
            events=[e async for e in rag.answer_events(AISettings(),payload,{'content_hash':'test'},
                                                       max_tokens=1024,review_settings=AISettings())]
        self.assertEqual(events[-1]['status'],'insufficient')
        self.assertEqual([n['id'] for n in events[-1]['citations']],['S1'])
        self.assertEqual(events[-1]['condition_review']['reason'],'invalid_review')
        self.assertFalse(any(e['type']=='delta' for e in events))
        async def revoked():raise PermissionError('revoked')
        with patch.object(node_qa,'chat_stream',fake),patch.object(review,'chat_stream') as paid:
            with self.assertRaises(PermissionError):
                _=[e async for e in rag.answer_events(AISettings(),payload,{'content_hash':'test'},
                   max_tokens=1024,review_settings=AISettings(),review_guard=revoked)]
            paid.assert_not_called()

    async def test_review_timeout_retains_original_sources_without_draft(self):
        payload,draft,_=self.package()
        async def answer(*a,**kw):yield json.dumps(draft)
        async def timed_out(*a,**kw):
            raise TransportError('timeout')
            yield ''
        with patch.object(node_qa,'chat_stream',answer),patch.object(review,'chat_stream',timed_out):
            events=[e async for e in rag.answer_events(AISettings(),payload,{'content_hash':'test'},
                                                       max_tokens=1024,review_settings=AISettings())]
        self.assertEqual(events[-1]['condition_review']['reason'],'upstream_timeout')
        self.assertEqual(events[-1]['citations'],payload['nodes'])
        self.assertFalse(any(e['type']=='delta' for e in events))

    async def test_concept_answer_also_checks_facts_before_releasing_text(self):
        payload,_,_=self.package()
        payload.update(question='收钱授权别人用商标，属于无形资产吗？',nodes=[
            {'id':'S1','label':'无形资产定义','content':'销售无形资产包括有偿转让所有权或者使用权。'}])
        draft={'status':'answerable','citations':['S1'],'summary':'按题设，您是境内一般纳税人。',
               'answer':'题设您是境内一般纳税人，商标使用权属于无形资产。[S1]'}
        result={'decision':'revise','checks':[{'relation':'scope','source_id':'S1',
            'quote':payload['nodes'][0]['content'],'draft_quote':draft['summary'],
            'verdict':'unsupported','known_facts':['收钱授权别人用商标'],
            'missing_facts':['未说明主体身份'],'note':'定义不证明用户的身份，先解释一般规则。'}],
            'issues':['用户没有给出境内一般纳税人身份。'],
            'response':{**draft,'summary':'解释有偿转让使用权的定义。',
                        'answer':'有偿授权商标使用权属于销售无形资产的定义范围；具体税务处理需另核身份和交易范围。[S1]'}}
        async def fake(*a,**kw):yield json.dumps(draft)
        with patch.object(node_qa,'chat_stream',fake),patch.object(review,'chat_stream',self.upstream(result)):
            events=[e async for e in rag.answer_events(AISettings(),payload,{'content_hash':'test'},
                                                       max_tokens=1024,review_settings=AISettings())]
        self.assertEqual(events[-1]['condition_review']['status'],'revised')
        self.assertEqual(events[-1]['answer'],result['response']['answer'])
        self.assertNotIn('境内一般纳税人', ''.join(e['text'] for e in events if e['type']=='delta'))
        self.assertNotIn(draft['summary'],json.dumps(events,ensure_ascii=False))
        self.assertEqual(''.join(e['text'] for e in events if e['type']=='delta'),events[-1]['answer'])

    async def test_review_unavailable_configuration_preserves_existing_protocol(self):
        payload,draft,_=self.package()
        async def fake(*a,**kw):yield json.dumps(draft)
        with patch.object(node_qa,'chat_stream',fake),patch.object(review,'chat_stream') as paid:
            events=[e async for e in rag.answer_events(AISettings(),payload,{'content_hash':'test'},max_tokens=1024)]
        paid.assert_not_called()
        self.assertEqual(events[-1]['answer'],draft['answer'])

    async def test_explicit_page_filters_are_sent_and_accepted_as_query_scope(self):
        payload,draft,result=self.package()
        payload.update(as_of='2026-02-15',region='CN-SH',
                       scope={'date_source':'user_filter','region_source':'user_filter'})
        result['checks'][0]['known_facts']=['2026-02-15','CN-SH']
        seen=[]
        async def checking(settings,messages,**kw):
            seen.append(json.loads(messages[-1]['content']))
            yield json.dumps(result)
        with patch.object(review,'chat_stream',checking):
            response,metadata=await review.review(AISettings(),payload,payload['nodes'],draft,rag.validate)
        self.assertEqual(metadata['status'],'revised')
        self.assertEqual(response,result['response'])
        self.assertEqual(seen[0]['as_of'],'2026-02-15')
        self.assertEqual(seen[0]['region'],'CN-SH')
        self.assertEqual(seen[0]['scope'],payload['scope'])

    def test_derived_reference_date_does_not_become_a_user_fact(self):
        payload,draft,result=self.package()
        payload.update(as_of='2030-02-15',scope={'date_source':'question_year'})
        result['checks'][0]['known_facts']=['2030-02-15']
        with self.assertRaisesRegex(ValueError,'invented facts'):
            review.validate(result,payload,payload['nodes'],draft,rag.validate)

    async def test_keep_keeps_validated_answer_and_omits_draft_quote(self):
        payload,_,result=self.package();draft=result['response']
        result.update(decision='keep',response=None,issues=[])
        result['checks'][0].update(verdict='supported',draft_quote=draft['answer'])
        async def fake(*a,**kw):yield json.dumps(draft)
        with patch.object(node_qa,'chat_stream',fake),patch.object(review,'chat_stream',self.upstream(result)):
            events=[e async for e in rag.answer_events(AISettings(),payload,{'content_hash':'test'},
                                                       max_tokens=1024,review_settings=AISettings())]
        self.assertEqual(events[-1]['answer'],draft['answer'])
        self.assertEqual(events[-1]['condition_review']['status'],'passed')
        self.assertNotIn('draft_quote',events[-1]['condition_review']['checks'][0])

    async def test_cancelling_review_closes_upstream(self):
        payload,draft,_=self.package();started=asyncio.Event();closed=asyncio.Event()
        async def answer(*a,**kw):yield json.dumps(draft)
        async def checking(*a,**kw):
            started.set()
            try:
                await asyncio.Event().wait()
                yield ''
            finally:closed.set()
        async def consume():
            return [e async for e in rag.answer_events(AISettings(),payload,{'content_hash':'test'},
                                                       max_tokens=1024,review_settings=AISettings())]
        with patch.object(node_qa,'chat_stream',answer),patch.object(review,'chat_stream',checking):
            task=asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(),1);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(closed.is_set())

    async def test_one_review_call_is_reserved_from_retry_budget(self):
        payload,_,_=self.package();calls=[]
        async def incomplete(*a,**kw):
            calls.append(1)
            raise TransportError('incomplete')
            yield ''
        with patch.object(node_qa,'chat_stream',incomplete):
            events=[e async for e in rag.answer_events(replace(AISettings(),max_calls=2),payload,{'content_hash':'test'},
                                                       max_tokens=1024,review_settings=AISettings())]
        self.assertEqual(len(calls),1)
        self.assertEqual(events[-1]['error_code'],'upstream_incomplete')
