"""Public-only text RAG contracts, scoped retrieval, provenance and stream harness."""
import asyncio
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from src.kg_retrieval import semantic_search
from src.settings import AISettings
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

    def test_no_relationship_or_enterprise_tables_are_queried(self):
        seen = []
        self.db.set_trace_callback(seen.append)
        rag.retrieve(self.db, '进项税额抵扣')
        text = '\n'.join(seen).lower()
        for table in ['relationship', 'audits', 'material_originals', 'core_input_tasks', 'clients', 'taxrule']:
            self.assertNotIn(table, text)


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    def package(self):
        nodes = [{'id': 'S1', 'label': '合成测试原文', 'content': '合成测试：存在例外。'}]
        return {'nodes': nodes, 'question': '解释'}, {'content_hash': 'test', 'node_ids': ['S1']}

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


class EndpointTests(unittest.TestCase):
    setUp = test_node_qa.EndpointTests.setUp

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
