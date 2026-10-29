"""Lossless request compaction, output planning and auditable batch synthesis."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from tests import test_kg_retrieval as retrieval_tests
from src.kg_retrieval import batching, outbound
from webapp import knowledge_evidence as service


class AnalysisContextTests(unittest.TestCase):
    setUp = retrieval_tests.RetrievalTests.setUp
    package = retrieval_tests.RetrievalTests.package
    context_package = retrieval_tests.RetrievalTests.context_package

    def test_duplicate_context_compacts_losslessly_without_mutating_evidence(self):
        text = '同一份来源和适用期间核对说明。' * 50
        original = {'facts': [{'metric_id': 'm1', 'basis': text}, {'metric_id': 'm2', 'basis': text}],
                    'gaps': [{'message': text}], 'check': {'rule_id': 'R01'}}
        before = deepcopy(original)
        packed = outbound.compact(original)
        self.assertLess(batching.encoded_size(packed), batching.encoded_size(original) * .6)
        self.assertEqual(outbound.expand(packed), original)
        self.assertEqual(original, before)
        self.assertEqual(packed['facts'][0]['metric_id'], 'm1')

    def test_output_capacity_splits_even_when_input_fits_and_keeps_every_record(self):
        package = self.context_package()
        package['gaps'] = [{'code': 'missing', 'message': f'待核对事项{i}'} for i in range(60)]
        small = batching.split(package, 128000, output_tokens=2048)
        large = batching.split(package, 128000, output_tokens=32768)
        self.assertGreater(small['part_count'], large['part_count'])
        self.assertFalse(small['unsplittable'])
        self.assertTrue(all(p['output_token_upper_bound'] <= small['output_budget'] for p in small['parts']))
        self.assertEqual([g for p in small['parts'] for g in p['gaps']], package['gaps'])
        text = ''.join(p['text'] for part in small['parts'] for p in part['policy_evidence'])
        self.assertEqual(text, package['policy_evidence'][0]['text'])

    def test_output_length_recovers_and_saved_summary_is_revalidated_after_restart(self):
        from webapp.knowledge import OutputLengthExceeded
        package = self.context_package()
        attempts = []
        def provider(system, wire, failure):
            part = outbound.expand(wire)
            attempts.append(part)
            if len(attempts) == 1:
                raise OutputLengthExceeded()
            return retrieval_tests.RetrievalTests.context_answer(system, part, failure)
        gate = {'passed': True, 'model_fingerprints': [None]}
        with patch.object(service, 'acceptance_gate', return_value=gate), patch('webapp.knowledge._chat_json', side_effect=provider):
            result = service.analyze(package)
        self.assertTrue(result['validated'])
        self.assertTrue(result['context_splits'])
        self.assertEqual(result['batch_summary']['status'], 'completed')
        self.assertEqual(result['batch_summary']['completed_batches'], len(result['batch_results']))
        service.validate_saved_analysis(result, package)
        for change in ('missing', 'foreign', 'summary'):
            bad = deepcopy(result)
            if change == 'missing': bad['batch_results'].pop()
            elif change == 'foreign': bad['batch_results'][0]['assessments'][0]['clause_ids'] = ['foreign']
            else: bad['batch_summary']['assessments'][0]['text'] = '篡改的联合分析'
            with self.subTest(change=change), self.assertRaises(ValueError):
                service.validate_saved_analysis(bad, package)

    def test_non_length_incomplete_never_replays_a_paid_request(self):
        package = self.context_package()
        with patch.object(service, 'acceptance_gate', return_value={'passed': True, 'model_fingerprints': [None]}), \
             patch('webapp.knowledge._chat_json', side_effect=service.HTTPException(502, '上游未正常结束')) as provider:
            result = service.analyze(package)
        self.assertFalse(result['validated'])
        self.assertEqual(provider.call_count, 1)

    def test_small_policy_groups_share_capacity_without_repeating_common_facts(self):
        package = self.package()
        example = deepcopy(package['policy_evidence'][0])
        package['policy_evidence'] = [{**deepcopy(example), 'clause_id': f'clause-{i}',
            'policy_id': f'policy-{i}', 'text': '必须保留的独立来源。' * 20} for i in range(15)]
        package['policy_leads'] = []
        plan = batching.split(package, 12000, output_tokens=16384)
        self.assertFalse(plan['unsplittable'])
        self.assertLess(plan['part_count'], 15)
        self.assertEqual([p['clause_id'] for part in plan['parts'] for p in part['policy_evidence']],
                         [p['clause_id'] for p in package['policy_evidence']])

    def test_literal_dictionary_reference_in_source_is_never_reinterpreted(self):
        text = '必须保留的来源原文。' * 40
        original = {'facts': [], 'first': text, 'second': text, 'source_metadata': {'context_ref': 'ctx1'}}
        self.assertEqual(outbound.expand(outbound.compact(original)), original)
        self.assertEqual(outbound.expand({'shared_context': {'original': '普通来源字段'}}),
                         {'shared_context': {'original': '普通来源字段'}})

    def test_wire_date_allowlist_matches_strict_validation_scope(self):
        package = self.package()
        seen = []
        def provider(system, wire, failure):
            part = outbound.expand(wire)
            expected = {part['task']['period_start'], part['task']['period_end']}
            expected |= {str(p[k]) for p in part['policy_evidence'] + part['policy_leads']
                         for k in ('effective_from', 'effective_to') if p.get(k)}
            self.assertEqual(set(part['response_constraints']['allowed_dates']), expected)
            seen.append(True)
            return retrieval_tests.RetrievalTests.context_answer(system, part, failure)
        with patch.object(service, 'acceptance_gate', return_value={'passed': True, 'model_fingerprints': [None]}), \
             patch('webapp.knowledge._chat_json', side_effect=provider):
            result = service.analyze(package)
        self.assertTrue(result['validated'])
        self.assertTrue(seen)

    def test_unbound_search_leads_are_indexed_but_required_sources_are_kept_in_full(self):
        package = self.package()
        lead = {**deepcopy(package['policy_evidence'][0]), 'clause_id': 'unbound-search-result',
                'text': '未关联为本项依据的长篇检索线索。' * 1000, 'retrieval_only': True, 'required': False}
        required = {**deepcopy(lead), 'clause_id': 'required-change', 'required': True}
        package['policy_leads'] = [lead, required]
        before = deepcopy(package)
        plan = batching.split(package, 128000, output_tokens=16384)
        delivered = [p for part in plan['parts'] for p in part['policy_leads']]
        self.assertNotIn(lead['clause_id'], {p['clause_id'] for p in delivered})
        self.assertEqual(''.join(p['text'] for p in delivered if p['clause_id'] == required['clause_id']), required['text'])
        index = [p for part in plan['parts'] for p in part.get('retrieval_lead_index', [])]
        self.assertEqual([p['clause_id'] for p in index], [lead['clause_id']])
        self.assertTrue(index[0]['original_text_sha256'])
        self.assertEqual(package, before)

    def test_capacity_refinement_keeps_the_local_lead_index_trace(self):
        from webapp.knowledge_batches import children
        package = self.context_package()
        part = outbound.prepare(package, authorized=True, destination='https://provider.example')['parts'][0]
        index = [{'clause_id': 'unbound', 'original_text_sha256': 'a' * 64, 'citable': False}]
        part['retrieval_lead_index'] = index
        parts = children(part, package, batching.encoded_size(part) // 2, 'https://provider.example')
        self.assertEqual([item for p in parts for item in p.get('retrieval_lead_index', [])], index)

    def test_negated_disclaimers_are_allowed_but_affirmative_conclusions_are_blocked(self):
        actual = '本条件不形成风险线索；该数值核对结果不代表政策适用、申报准确性或全面合规认定。'
        self.assertFalse(service.legal_claim(actual))
        for text in ('不能据此直接确认违法。', '不代表企业不存在任何税务风险。'):
            with self.subTest(text=text): self.assertFalse(service.legal_claim(text))
        for text in ('已经确定违法。', '企业全面合规。', '不存在任何税务风险。',
                     '不代表政策适用，但企业全面合规。', '不能确认违法，但确定享受优惠。',
                     '不代表政策适用且已经确定违法。', '不能不确认违法。'):
            with self.subTest(text=text): self.assertTrue(service.legal_claim(text))
        package = self.package()
        package['check'].update(evaluation='FALSE', disposition='condition_not_met')
        claim = {'type': 'condition_not_met', 'text': actual, 'rule_id': package['check']['rule_id'],
                 'clause_ids': [package['policy_evidence'][0]['clause_id']], 'source_ids': [],
                 'amounts': [], 'dates': [], 'required_materials': []}
        service.validate_answer({'assessments': [claim]}, package)
        claim['text'] = '本条件未成立，不能据此形成风险线索。'
        service.validate_answer({'assessments': [claim]}, package)
        claim['text'] = '本条件不形成风险线索，但实际形成风险线索。'
        with self.assertRaises(ValueError): service.validate_answer({'assessments': [claim]}, package)
