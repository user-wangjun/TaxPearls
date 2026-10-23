"""Public node boundaries, complete dependencies, streaming and cancellation contracts."""
import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2 as httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.ai_stream import chat_stream
from src.ai_transport import TransportError, _key_failures
from src.settings import AISettings
from webapp import node_qa, app as module
from webapp.login_guard import RateLimiter
from webapp.storage import Store


def graph_fixture():
    def node(key, kind, **fields):
        return {'id': key, 'schema_type': kind, 'record_id': key, 'label': key, **fields}
    def edge(a, b, relation, **fields):
        return {'source': a, 'target': b, 'relation_type': relation, **fields}
    return {'nodes': [node('rule', 'TaxRule', condition_logic_json='{"ref":"cond"}', review_status='candidate'),
                      node('cond', 'Condition'), node('metric', 'Metric'), node('exception', 'Exception'),
                      node('clause', 'Clause', content='完整条款；但符合例外的除外。',
                           source_location='C:/private/original.txt:L8', raw_file_path='C:/private/secret'),
                      node('policy', 'Policy', validity_status='unverified'),
                      node('amend', 'Clause', content='部分修改说明。'),
                      node('evidence', 'Clause', content='关系依据。'),
                      node('sibling', 'TaxRule'), node('other', 'Clause', content='无关条款'),
                      node('client', 'Company', value='enterprise secret')],
            'edges': [edge('rule', 'cond', 'HAS_CONDITION'), edge('cond', 'metric', 'USES_METRIC'),
                      edge('rule', 'exception', 'HAS_EXCEPTION'), edge('clause', 'rule', 'DERIVES_RULE'),
                      edge('policy', 'clause', 'HAS_CLAUSE'), edge('policy', 'other', 'HAS_CLAUSE'),
                      edge('other', 'sibling', 'DERIVES_RULE'),
                      edge('amend', 'clause', 'AMENDS', review_status='candidate', evidence_clause_id='evidence')]}


class ContextTests(unittest.TestCase):
    def test_dependencies_sources_exceptions_changes_without_policy_hub_explosion(self):
        payload, manifest = node_qa.build_context(graph_fixture(), 'rule', '解释这个条件和例外')
        ids = set(manifest['node_ids'])
        self.assertEqual(ids, {'rule', 'cond', 'metric', 'exception', 'clause', 'policy', 'amend', 'evidence'})
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('enterprise secret', text)
        self.assertNotIn('C:/private', text)
        self.assertIn('original.txt:L8', text)
        self.assertIn('完整条款；但符合例外的除外。', text)
        self.assertIn('candidate', text)
        self.assertEqual(manifest['utf8_bytes'], len(node_qa.canonical(payload).encode()))

    def test_policy_contains_all_retained_clauses_and_oversize_is_not_silent(self):
        graph = graph_fixture()
        payload, manifest = node_qa.build_context(graph, 'policy', '介绍这个文件')
        self.assertIn('other', manifest['node_ids'])
        graph['nodes'][4]['content'] = '完整原文' * 10000
        with self.assertRaises(HTTPException) as raised:
            node_qa.build_context(graph, 'rule', '解释', budget=3000)
        self.assertEqual(raised.exception.status_code, 413)

    def test_enterprise_scope_and_invalid_node_are_rejected(self):
        for graph, selected in [(dict(graph_fixture(), taxpayer_id='TEST'), 'rule'),
                                (graph_fixture(), 'client'), (graph_fixture(), 'missing')]:
            with self.assertRaises(HTTPException):
                node_qa.build_context(graph, selected, '解释')

    def test_source_json_projects_locations_without_paths(self):
        value = json.dumps({'text_path': 'C:/server/private/file.txt', 'start_line': 8,
                            'end_line': 12, 'pdf_pages': [2], 'internal': 'not sent'})
        projected = node_qa.public_location(value)
        self.assertNotIn('server', projected)
        self.assertNotIn('internal', projected)
        self.assertIn('file.txt', projected)

    def test_policy_modification_keeps_amendment_text_and_cycle_terminates(self):
        graph = graph_fixture()
        graph['nodes'].append({'id': 'new-policy', 'record_id': 'new-policy', 'schema_type': 'Policy', 'label': '修改文件'})
        graph['nodes'].append({'id': 'new-clause', 'record_id': 'new-clause', 'schema_type': 'Clause', 'label': '修改条款', 'content': '完整修订依据'})
        graph['edges'] += [{'source': 'new-policy', 'target': 'policy', 'relation_type': 'AMENDS'},
                           {'source': 'policy', 'target': 'new-policy', 'relation_type': 'INTERPRETS'},
                           {'source': 'new-policy', 'target': 'new-clause', 'relation_type': 'HAS_CLAUSE'}]
        _, manifest = node_qa.build_context(graph, 'rule', '依据是什么')
        self.assertIn('new-clause', manifest['node_ids'])

    def test_partial_json_handles_escapes_and_unicode_boundaries(self):
        raw = json.dumps({'status': 'answerable', 'citations': ['rule'], 'summary': '说明',
                          'answer': '他说："你好"\n例外😀'}, ensure_ascii=True)
        previous = ''
        for end in range(1, len(raw) + 1):
            fields = node_qa.partial_fields(raw[:end])
            if 'answer' in fields:
                self.assertTrue(fields['answer'].startswith(previous))
                fields['answer'].encode('utf-8')
                previous = fields['answer']
        self.assertEqual(previous, json.loads(raw)['answer'])


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, fields, *, failure=False):
        async def fake(*args, **kwargs):
            raw = json.dumps(fields, ensure_ascii=False)
            for start in range(0, len(raw), 7):
                yield raw[start:start+7]
            if failure:
                raise TransportError('incomplete')
        payload, manifest = node_qa.build_context(graph_fixture(), 'rule', '解释')
        with patch.object(node_qa, 'chat_stream', fake):
            return [e async for e in node_qa.answer_events(AISettings(), payload, manifest, max_tokens=1024)]

    async def test_gate_precedes_text_and_final_citations_are_projected(self):
        fields = {'status': 'answerable', 'citations': ['rule'], 'summary': '参考规则定义。', 'answer': '当前规则待核实。'}
        events = await self.collect(fields)
        types = [e['type'] for e in events]
        self.assertLess(types.index('gate'), types.index('delta'))
        self.assertEqual(''.join(e['text'] for e in events if e['type'] == 'delta'), fields['answer'])
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[-1]['citations'][0]['id'], 'rule')

    async def test_out_of_scope_never_streams_unrelated_content(self):
        events = await self.collect({'status': 'out_of_scope', 'citations': [],
                                     'summary': 'malicious summary', 'answer': 'unrelated generated text'})
        self.assertNotIn('delta', [e['type'] for e in events])
        self.assertNotIn('summary', [e['type'] for e in events])
        self.assertNotIn('unrelated generated text', json.dumps(events))

    async def test_missing_evidence_and_clarification_are_distinct(self):
        for status in ['insufficient', 'clarify']:
            events = await self.collect({'status': status, 'citations': [], 'summary': '缺少上下文', 'answer': '请明确具体条件。'})
            self.assertEqual(events[-1]['status'], status)
            self.assertFalse(any(e['type'] == 'delta' for e in events))

    async def test_bad_citations_and_truncation_cannot_complete(self):
        for cited, failure in [(['invented'], False), (['rule'], True)]:
            events = await self.collect({'status': 'answerable', 'citations': cited, 'summary': '', 'answer': '说明'}, failure=failure)
            self.assertEqual(events[-1]['type'], 'error')
            self.assertNotIn('done', [e['type'] for e in events])

    async def test_inline_fabricated_citation_is_not_a_completed_answer(self):
        events = await self.collect({'status': 'answerable', 'citations': ['rule'], 'summary': '', 'answer': '见[invented]。'})
        self.assertEqual(events[-1]['type'], 'error')

    async def test_prompt_placeholder_prefix_is_tolerated_but_fabricated_ids_rejected(self):
        # Models echo the prompt placeholder literally; a stripped prefix that
        # maps to a real ID must complete, an invented ID must still fail.
        for answer, completes in [('见[节点ID：rule]。', True), ('见[节点ID: rule]。', True),
                                  ('见[节点ID略]。', True), ('见[节点ID：invented]。', False)]:
            events = await self.collect({'status': 'answerable', 'citations': ['rule'], 'summary': '', 'answer': answer})
            self.assertEqual(events[-1]['type'] == 'done', completes, answer)

    async def test_closing_harness_closes_nested_transport(self):
        closed = []
        async def fake(*args, **kwargs):
            try:
                yield '{"status":"answerable","citations":["rule"],"summary":"","answer":"部分回答'
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        payload, manifest = node_qa.build_context(graph_fixture(), 'rule', '解释')
        with patch.object(node_qa, 'chat_stream', fake):
            source = node_qa.answer_events(AISettings(), payload, manifest, max_tokens=1024)
            while (await anext(source))['type'] != 'delta':
                pass
            await source.aclose()
        self.assertEqual(closed, [True])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sse_parser_handles_split_bytes_and_ignores_reasoning(self):
        lines = [json.dumps({'choices': [{'index': 0, 'delta': {'reasoning_content': 'PRIVATE THOUGHT'}}]}),
                 json.dumps({'choices': [{'index': 0, 'delta': {'content': '中文回答'}}]}, ensure_ascii=False),
                 json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}), '[DONE]']
        data = ''.join('data: '+line+'\n\n' for line in lines).encode()
        class Bytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                for start in range(0, len(data), 3):
                    yield data[start:start+3]
        async def handler(request):
            self.assertTrue(json.loads(request.content)['stream'])
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Bytes())
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('src.ai_stream.httpx.AsyncClient', return_value=client):
            result = [x async for x in chat_stream(AISettings(api_key='synthetic'), [], max_tokens=1024)]
        self.assertEqual(result, ['中文回答'])

    async def test_retry_only_auth_rejection_and_reject_incomplete(self):
        for status, expected in [(401, 2), (500, 1), (200, 1)]:
            _key_failures.clear()
            calls = []
            async def handler(request):
                calls.append(request)
                if len(calls) == 1 and status != 200:
                    return httpx.Response(status)
                return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                      content=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: [DONE]\n\n')
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch('src.ai_stream.httpx.AsyncClient', return_value=client):
                with self.assertRaises(TransportError):
                    _ = [x async for x in chat_stream(AISettings(api_key='a', backup_api_keys=('b',)), [], max_tokens=1024)]
            self.assertEqual(len(calls), expected)
        _key_failures.clear()

    async def test_cancel_closes_upstream(self):
        closed = asyncio.Event()
        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"start"}}]}\n\n'
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=Waiting())))
        with patch('src.ai_stream.httpx.AsyncClient', return_value=client):
            source = chat_stream(AISettings(api_key='synthetic'), [], max_tokens=1024)
            self.assertEqual(await anext(source), 'start')
            pending = asyncio.create_task(anext(source))
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await source.aclose()
        self.assertTrue(closed.is_set())


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {'TAXPEARLS_FIELD_KEY': base64.b64encode(b'n'*32).decode(),
                             'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '0', 'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        old = module.store
        module.store = Store(Path(self.directory.name)/'node.db')
        self.addCleanup(setattr, module, 'store', old)
        module.store.create_user('node-admin', 'node-qa-test-2026', '测试', 'org_admin', 'org-node')
        self.client = TestClient(module.app)
        login = self.client.post('/api/login', json={'username': 'node-admin', 'password': 'node-qa-test-2026'})
        self.assertEqual(login.status_code, 200)
        self.addCleanup(self.client.close)
        self.limiter = patch.object(module, 'node_question_limiter', RateLimiter({'actor': (12,60)}))
        self.limiter.start()
        self.addCleanup(self.limiter.stop)
        module._node_question_active.clear()
        self.addCleanup(module._node_question_active.clear)

    def test_auth_body_scope_whitespace_and_size_before_model(self):
        with patch.object(node_qa, 'chat_stream') as upstream:
            for extra in [{'taxpayer_id': 'TEST'}, {'audit_id': 'private'}, {'nodes': []}]:
                self.assertEqual(self.client.post('/api/knowledge/node-ask', json={
                    'node_id': 'rule', 'question': '解释', **extra}).status_code, 422)
            for question in ['  ', 'x'*2001, '\x00', '\ud800']:
                self.assertEqual(self.client.post('/api/knowledge/node-ask',
                    headers={'Content-Type': 'application/json'},
                    content=json.dumps({'node_id': 'rule', 'question': question})).status_code, 422)
            self.assertEqual(self.client.post('/api/knowledge/node-ask', content=b'x'*24577).status_code, 413)
            self.assertEqual(self.client.post('/api/knowledge/ask', json={
                'node_id': 'rule', 'question': '解释', 'audit_id': ''}).status_code, 422)
            self.client.cookies.clear()
            self.assertEqual(self.client.post('/api/knowledge/node-ask', json={
                'node_id': 'rule', 'question': '解释'}).status_code, 401)
            upstream.assert_not_called()

    def test_stream_and_legacy_json_share_harness_and_release_slot(self):
        async def fake(*args, **kwargs):
            yield json.dumps({'status': 'answerable', 'citations': ['rule'], 'summary': '规则定义', 'answer': '解释结果'})
        with patch('webapp.knowledge.public_graph', return_value=graph_fixture()), \
             patch('webapp.ai_keys.apply', return_value=AISettings(enabled=True, api_key='synthetic')), \
             patch.object(node_qa, 'chat_stream', fake):
            response = self.client.post('/api/knowledge/node-ask', json={'node_id': 'rule', 'question': '解释'})
            self.assertEqual(response.status_code, 200)
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
            self.assertEqual(events[-1]['type'], 'done')
            self.assertEqual(response.headers['x-accel-buffering'], 'no')
            self.assertFalse(module._node_question_active)
            legacy = self.client.post('/api/knowledge/ask', json={'node_id': 'rule', 'question': '解释'})
            self.assertEqual(legacy.json()['answer'], '解释结果')
            self.assertFalse(module._node_question_active)

    def test_real_public_graph_selected_rule_is_projected_and_enterprise_free(self):
        from webapp.knowledge import public_graph
        with module.store.connect() as db:
            graph = public_graph(db, module._effective_rules())
        selected = next(n['id'] for n in graph['nodes'] if n.get('schema_type') == 'TaxRule')
        captured = []
        async def fake(settings, messages, **kwargs):
            captured.append(json.loads(messages[1]['content']))
            yield json.dumps({'status': 'answerable', 'citations': [selected], 'summary': '', 'answer': '仅解释规则定义。'})
        with patch('webapp.ai_keys.apply', return_value=AISettings(enabled=True, api_key='synthetic')), \
             patch.object(node_qa, 'chat_stream', fake):
            result = self.client.post('/api/knowledge/node-ask', json={'node_id': selected, 'question': '解释规则'})
        self.assertEqual(result.status_code, 200)
        self.assertIn('"type": "done"', result.text)
        self.assertTrue(all(n['schema_type'] in node_qa.TYPES for n in captured[0]['nodes']))
        self.assertFalse(any('raw_file_path' in n or 'execution_json' in n or 'value' in n for n in captured[0]['nodes']))

    def test_revoked_session_stops_stream_before_answer(self):
        closed = []
        async def fake(*args, **kwargs):
            try:
                with module.store.connect() as db:
                    db.execute("UPDATE users SET active=0 WHERE username='node-admin'")
                yield json.dumps({'status': 'answerable', 'citations': ['rule'], 'summary': '', 'answer': 'must not escape'})
            finally:
                closed.append(True)
        with patch('webapp.knowledge.public_graph', return_value=graph_fixture()), \
             patch('webapp.ai_keys.apply', return_value=AISettings(enabled=True, api_key='synthetic')), \
             patch.object(node_qa, 'chat_stream', fake):
            response = self.client.post('/api/knowledge/node-ask', json={'node_id': 'rule', 'question': '解释'})
        self.assertNotIn('must not escape', response.text)
        self.assertIn('访问权限已变化', response.text)
        self.assertEqual(closed, [True])
        self.assertFalse(module._node_question_active)

    def test_frequency_limit_applies_to_invalid_nodes_too(self):
        with patch('webapp.knowledge.public_graph', return_value=graph_fixture()), \
             patch('webapp.ai_keys.apply', return_value=AISettings(enabled=True, api_key='synthetic')):
            for _ in range(12):
                self.assertEqual(self.client.post('/api/knowledge/node-ask', json={
                    'node_id': 'missing', 'question': '解释'}).status_code, 422)
            self.assertEqual(self.client.post('/api/knowledge/node-ask', json={
                'node_id': 'rule', 'question': '解释'}).status_code, 429)
            self.assertFalse(module._node_question_active)
