"""Passage retrieval must preserve exact provenance and decision-limiting context."""
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.kg_retrieval import batching, bundle, evaluate, fixtures, outbound, semantic_search


def long_policy():
    pieces = ['【纯合成工程测试】仅适用于测试主体，须先核对所属期间。\n']
    for i in range(1, 31):
        text = '仓储台账的历史记录与档案排列说明。' * 35
        if i == 15:
            text = '客户退回货物后，应核对原交易和红字发票确认单。必须留存退货凭据。'
        if i == 29:
            text = '特殊交易不适用一般处理，资料缺失时不得确认。'
        if i == 30:
            text = '本说明自2026年1月1日起施行，原说明同时废止。'
        pieces.append(f'第{i}条　{text}\n')
    return ''.join(pieces)


class PassageTests(unittest.TestCase):
    def selection(self, text=None, **kwargs):
        from src.kg_retrieval.passages import select
        text = text or long_policy()
        start = text.index('客户退回货物')
        return select(text, [{'route': 'semantic', 'reason': {'spans': [
            {'source_char_start': start + 3, 'source_char_end': start + 12,
             'original_text_sha256': hashlib.sha256(text.encode()).hexdigest()}]}}], **kwargs)

    def test_matching_passage_keeps_whole_article_scope_exception_and_commencement(self):
        result = self.selection()
        self.assertLess(len(result['text']), len(long_policy()) // 3)
        for text in ('第15条', '必须留存退货凭据', '仅适用于测试主体', '特殊交易不适用', '原说明同时废止'):
            self.assertIn(text, result['text'])
        self.assertTrue(result['partial_clause'])
        self.assertFalse(result['context_complete'])
        self.assertEqual(result['original_text_sha256'], hashlib.sha256(long_policy().encode()).hexdigest())
        for span in result['evidence_spans']:
            self.assertEqual(result['text'][span['text_char_start']:span['text_char_end']],
                             long_policy()[span['source_char_start']:span['source_char_end']])

    def test_required_or_changed_or_unstructured_sources_are_never_cut(self):
        for kwargs in ({'required': True}, {'dependency': True}):
            self.assertEqual(self.selection(**kwargs)['text'], long_policy())
        unstructured = '普通段落没有可靠条号。' * 300 + '客户退回货物。'
        self.assertEqual(self.selection(unstructured)['text'], unstructured)

    def test_unresolved_reference_duplicate_labels_and_bad_offsets_fall_back_to_full(self):
        for text in (long_policy().replace('必须留存退货凭据。', '依照前款及附件办理。'),
                     long_policy().replace('第16条', '第15条')):
            self.assertEqual(self.selection(text)['text'], text)
        from src.kg_retrieval.passages import select
        stale = [{'route': 'semantic', 'reason': {'spans': [
            {'source_char_start': 0, 'source_char_end': 15, 'original_text_sha256': 'stale'}]}}]
        self.assertEqual(select(long_policy(), stale)['text'], long_policy())

    def test_multiple_hits_are_merged_without_repeating_or_fabricating_source_text(self):
        from src.kg_retrieval.passages import select
        text = long_policy()
        match = {'route': 'keyword', 'reason': {'matched_terms': ['客户退回', '红字发票', '退货凭据']}}
        result = select(text, [match, match])
        self.assertEqual(result['text'].count('第15条'), 1)
        spans = result['evidence_spans']
        self.assertTrue(all(a['source_char_end'] < b['source_char_start'] for a, b in zip(spans, spans[1:])))

    def test_capacity_fragments_keep_original_offsets_across_selected_passages(self):
        from src.kg_retrieval.passages import fragment
        item = self.selection()
        split = item['evidence_spans'][1]['text_char_start'] + 5
        fragments = [fragment(item, 0, split), fragment(item, split, len(item['text']))]
        self.assertEqual(''.join(f['text'] for f in fragments), item['text'])
        for piece in fragments:
            for span in piece['evidence_spans']:
                self.assertEqual(piece['text'][span['text_char_start']:span['text_char_end']],
                                 long_policy()[span['source_char_start']:span['source_char_end']])

    def test_small_omission_does_not_increase_payload_with_provenance_metadata(self):
        from src.kg_retrieval.passages import select
        text = '前言。\n第1条 ' + '普通说明正文。' * 170 + '\n第2条 无关归档。\n'
        result = select(text, [{'reason': {'matched_terms': ['普通说明正文']}}])
        self.assertEqual(result['text'], text)


class PassageBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        fixtures.seed(self.db, Path(self.tmp.name))
        self.text = long_policy()
        self.db.execute("UPDATE Clause SET content=? WHERE clause_id='CL13'", (self.text,))
        policy = self.db.execute("SELECT raw_file_path FROM Policy WHERE policy_id='P13'").fetchone()[0]
        Path(policy).write_text(self.text, encoding='utf-8')
        self.db.execute("UPDATE Policy SET file_sha256=? WHERE policy_id='P13'", (hashlib.sha256(Path(policy).read_bytes()).hexdigest(),))

    def package(self, rule='R01', **kwargs):
        case = {'id': 'PASSAGES', 'rule_id': rule}
        if rule == 'R05':
            case.update(period_start='2026-08-01', period_end='2026-08-31')
        task, facts, catalog = evaluate.case_input(self.db, case)
        return bundle.build(self.db, task, rule, facts, topic='客户退回货物红字发票', source_catalog=catalog,
                            model_dir=Path(self.tmp.name) / 'missing', token_budget=128000, **kwargs)

    def test_reviewed_citation_text_survives_passage_selection(self):
        corrected = self.text.replace('客户退回货物', '客户退回货物（已核对引用）')
        def view(clause, policy):
            if clause['clause_id'] == 'CL13':
                return {'text': corrected, 'original_text': self.text,
                        'citation_corrections': [{'original_quote': '客户退回货物',
                                                  'corrected_quote': '客户退回货物（已核对引用）'}]}
            return {'text': clause['content']}
        with patch.object(bundle, 'citation_view', side_effect=view):
            package = self.package()
        row = next(r for r in package['policy_evidence'] if r['clause_id'] == 'CL13')
        self.assertEqual(row['text'], corrected)
        self.assertEqual(row['original_text'], self.text)
        self.assertFalse(row.get('partial_clause', False))
        self.assertTrue(row['citation_corrections'])

    def test_actual_bundle_and_outbound_keep_partial_scope_and_required_source(self):
        package = self.package()
        records = {p['clause_id']: p for p in package['policy_evidence']}
        self.assertLess(len(records['CL13']['text']), len(self.text) // 3)
        self.assertEqual(records['CL01']['text'], self.db.execute("SELECT content FROM Clause WHERE clause_id='CL01'").fetchone()[0])
        self.assertIn('retrieval_context_partial', {g['code'] for g in package['gaps']})
        self.assertEqual(package['check']['evaluation'], 'UNKNOWN')
        delivery = outbound.prepare(package, authorized=True, destination='https://provider.example')
        delivered = next(p for part in delivery['parts'] for p in part['policy_evidence'] if p['clause_id'] == 'CL13')
        self.assertEqual(delivered['evidence_spans'], records['CL13']['evidence_spans'])
        self.assertFalse(delivered['context_complete'])
        package['batching'] = bundle.split_bundle(package, 4200)
        delivery = outbound.prepare(package, authorized=True, destination='https://provider.example')
        pieces = [p for part in delivery['parts'] for p in part['policy_evidence'] if p['clause_id'] == 'CL13']
        self.assertEqual(''.join(p['text'] for p in pieces), records['CL13']['text'])
        for piece in pieces:
            for span in piece['evidence_spans']:
                self.assertEqual(piece['text'][span['text_char_start']:span['text_char_end']],
                                 self.text[span['source_char_start']:span['source_char_end']])
        self.assertTrue(all(batching.encoded_size(p) <= 4200 for p in delivery['parts']))

    def test_condition_exception_and_amendment_dependencies_survive_top_one(self):
        self.db.execute("UPDATE Condition SET source_clause_id='CL05A' WHERE condition_id='C05'")
        for rule, expected in (('R04', {'CL04', 'CL04E'}), ('R05', {'CL05A', 'CL20'})):
            package = self.package(rule, candidate_limit=1, review_limit=1)
            records = {p['clause_id']: p for p in package['policy_evidence']}
            self.assertTrue(expected <= records.keys())
            self.assertTrue(all(not records[c].get('partial_clause') for c in expected))

    def test_excerpted_optional_policy_cannot_be_used_as_complete_applicability(self):
        from webapp.knowledge_evidence import validate_answer
        package = self.package()
        self.assertTrue(any(p.get('partial_clause') for p in package['policy_evidence']))
        answer = {'assessments': [{'type': 'risk_clue', 'text': '本次出现待核查线索。',
            'rule_id': 'R01', 'clause_ids': ['CL01'], 'source_ids': [], 'amounts': [],
            'dates': [], 'required_materials': []}]}
        with self.assertRaises(ValueError):
            validate_answer(answer, package)
        answer['assessments'][0]['type'] = 'insufficient_evidence'
        validate_answer(answer, package)

    def test_evaluation_checks_real_excerpt_provenance_and_detects_changed_offsets(self):
        case = {'id': 'PASSAGE-ORACLE', 'rule_id': 'R01', 'topic': '客户退回货物红字发票',
                'expected_clauses': ['CL01', 'CL13'], 'allowed_clauses': ['CL01', 'CL02', 'CL13'],
                'expected_status': 'needs_review', 'expected_evaluation': 'UNKNOWN'}
        report, packages = evaluate.evaluate(self.db, [case], model_dir=Path(self.tmp.name) / 'missing', token_budget=128000)
        self.assertTrue(report['cases'][0]['passed'], report['cases'][0]['failures'])
        self.assertEqual(report['metrics']['source_traceability'], 1)
        selected = next(p for p in packages[case['id']]['policy_evidence'] if p['clause_id'] == 'CL13')
        selected['evidence_spans'][0]['source_char_start'] += 1
        with patch.object(evaluate, 'build', return_value=packages[case['id']]):
            corrupted, _ = evaluate.evaluate(self.db, [case])
        self.assertFalse(corrupted['cases'][0]['passed'])
        self.assertLess(corrupted['metrics']['source_traceability'], 1)


class LocalChunkTests(unittest.TestCase):
    def model(self):
        model, reason = semantic_search.available()
        if model is None:
            self.skipTest(reason)
        return model

    def test_real_encoder_chunks_have_exact_locations_and_paragraph_boundaries(self):
        model = self.model()
        text = '  开头与空白。\n\n' + '完整自然段甲。' * 15 + '\r\n' + '完整自然段乙。' * 15 + '\n' + '很长的连续正文，' * 150
        spans = model.chunk_spans(text)
        covered = set()
        for span in spans:
            start, end = span['source_char_start'], span['source_char_end']
            covered.update(range(start, end))
            self.assertEqual(span['text'], text[start:end])
            self.assertLessEqual(len(model.tokenizer.encode(span['text'], add_special_tokens=False).ids), 480)
        self.assertEqual(covered, set(range(len(text))))
        paragraph = '完整自然段甲。' * 15 + '\r\n'
        self.assertTrue(any(paragraph in s['text'] for s in spans))

    def test_wrapped_pdf_lines_are_packed_instead_of_multiplying_vector_count(self):
        model = self.model()
        text = ('运输服务的原始资料应按完整来源核对，不得遗漏规定条件。\n' * 80)
        spans = model.chunk_spans(text)
        self.assertLess(len(spans), 12)
        self.assertTrue(all(len(model.tokenizer.encode(s['text'], add_special_tokens=False).ids) <= 480 for s in spans))

    def test_legacy_index_migration_and_search_propagate_all_matching_spans(self):
        model = self.model()
        with tempfile.TemporaryDirectory() as temp:
            db = sqlite3.connect(':memory:')
            db.row_factory = sqlite3.Row
            self.addCleanup(db.close)
            fixtures.seed(db, Path(temp))
            # Exercise the on-disk legacy schema, not just a fresh v2 table.
            db.execute('DROP TABLE kg_clause_vectors')
            db.execute('''CREATE TABLE kg_clause_vectors (
                vector_id INTEGER PRIMARY KEY AUTOINCREMENT, clause_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL, content_hash TEXT NOT NULL,
                model_fingerprint TEXT NOT NULL, vector BLOB NOT NULL,
                UNIQUE(clause_id,chunk_index,model_fingerprint))''')
            from src.kg_retrieval.common import digest
            clause = dict(db.execute("SELECT * FROM Clause WHERE clause_id='CL02'").fetchone())
            db.execute('INSERT INTO kg_clause_vectors(clause_id,chunk_index,content_hash,model_fingerprint,vector) VALUES(?,?,?,?,?)',
                       ('CL02', 0, digest([clause['content'], clause['version']]), model.fingerprint, b'legacy-must-reindex'))
            result = semantic_search.search(db, '客户把货退回，之前已经开过的票怎么办')
            self.assertIn('CL02', result['updated_clause_ids'])
            hit = next(h for h in result['hits'] if h['clause_id'] == 'CL02')
            self.assertTrue(hit['reason']['spans'])
            semantic_search.initialize(db)
            self.assertIn('source_char_start', {r['name'] for r in db.execute('PRAGMA table_info(kg_clause_vectors)')})
            with patch.object(type(semantic_search.available()[0]), 'encode', wraps=semantic_search.available()[0].encode) as encode:
                semantic_search.search(db, '客户退货')
                self.assertEqual(encode.call_count, 1)


if __name__ == '__main__':
    unittest.main()
