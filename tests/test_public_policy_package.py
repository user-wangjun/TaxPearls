"""Public snapshot delivery preserves local school data and explicit source reviews."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from src.public_policy_package import load_package, import_package
from webapp import policy_graph as pg

ROOT = Path(__file__).resolve().parents[1]


class PublicPolicyPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load_package(ROOT/'data/public-policy/snapshot.json.gz', ROOT)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.baseline = Path(cls.tmp.name)/'baseline'
        cls.baseline.mkdir()
        db = sqlite3.connect(cls.baseline/'school.sqlite')
        db.row_factory = sqlite3.Row
        try:
            pg.migrate(db)
            db.execute('CREATE TABLE local_accounts(id TEXT PRIMARY KEY,secret TEXT)')
            db.execute("INSERT INTO local_accounts VALUES ('teacher','private-sentinel')")
            db.execute('CREATE TABLE knowledge_point(id INTEGER PRIMARY KEY,description TEXT)')
            db.executemany('INSERT INTO knowledge_point VALUES (?,?)', [(i, 'local teaching content') for i in range(113)])
            db.commit()
            with db:
                cls.first = import_package(db, cls.payload, ROOT)
        finally:
            db.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/'relocated-school'
        shutil.copytree(self.baseline, self.home)
        self.db = sqlite3.connect(self.home/'school.sqlite')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)

    def test_snapshot_is_complete_and_contains_only_public_graph(self):
        self.assertEqual(set(self.payload['tables']), set(pg.IDS))
        self.assertEqual(len(self.payload['originals']), 932)
        self.assertEqual(len(self.payload['tables']['Policy']), 155)
        self.assertEqual(len(self.payload['tables']['Clause']), 2350)
        self.assertEqual(self.db.execute('SELECT count(*) FROM knowledge_point').fetchone()[0], 113)
        self.assertEqual(self.db.execute('SELECT secret FROM local_accounts').fetchone()[0], 'private-sentinel')
        self.assertFalse(self.first['business_tables_changed'])

    def test_guangdong_vat_candidates_have_scope_sources_and_no_approval(self):
        policies = [p for p in self.payload['tables']['Policy'] if p['policy_id'].startswith('GD-')]
        self.assertEqual(len(policies), 87)
        self.assertTrue(all(p['validity_status'] == 'unverified' and 'VAT' in json.loads(p['tax_types_json']) for p in policies))
        audits = [json.loads(p['source_metadata_json'])['source_audit'] for p in policies]
        self.assertTrue(all(a['candidate_import'] == 'passed' and a['legal_review'] == 'pending_human_review' for a in audits))
        self.assertEqual(sum(a['historical_only'] for a in audits), 35)
        pilot = next(p for p in policies if '开展增值税及附加税费申报试点' in p['title'])
        scope = json.loads(pilot['scope_json'])
        self.assertEqual(scope['excluded_regions'], ['CN-44-4403'])
        self.assertTrue(any('具体范围由' in c['quote'] for c in scope['conditions']))
        clauses = [c for c in self.payload['tables']['Clause'] if c['policy_id'] == pilot['policy_id']]
        self.assertTrue(any('填写说明' in c['content'] and c['clause_label'].startswith('附件') for c in clauses))
        self.assertFalse(any(c['extraction_status'] == 'verified' for c in clauses))
        from src.policy_paths import check_clause_source
        from src.kg_retrieval.applicability import check
        clause = clauses[0]
        decision = check(clause, pilot, {'tax_type': 'VAT', 'region_code': 'CN-44-4403',
            'period_start': '2026-10-01', 'period_end': '2026-10-21', 'data_scope': 'synthetic'}, [], source_root=self.home)
        self.assertEqual(decision['status'], 'excluded')
        for clause in self.payload['tables']['Clause']:
            check_clause_source(clause, self.home)

    def test_guangdong_research_and_recovered_assets_remain_evidence_not_approval(self):
        policies = [p for p in self.payload['tables']['Policy'] if p['policy_id'].startswith('GD-')]
        self.assertEqual(sum(p['effective_from'] is None for p in policies), 47)
        researched = [json.loads(p['source_metadata_json']).get('legal_research', {}) for p in policies]
        self.assertEqual(sum(len(r.get('events', [])) for r in researched), 18)
        self.assertEqual(sum(bool(r.get('clause_date_candidates')) for r in researched), 14)
        self.assertTrue(all(e['review_status'] == 'candidate_evidence' for r in researched for e in r.get('events', [])))
        quality = {a['url']: a['quality_review'] for p in policies
                   for a in json.loads(p['source_metadata_json'])['attachment_quality']}
        recovered = [a for a in quality.values() if a.get('parser_compatibility_recovered')]
        self.assertEqual(len(recovered), 2)
        self.assertTrue(all(not a['password_supplied'] and a['content_verified'] for a in recovered))
        self.assertEqual(sum(a.get('method') == 'independent_word_main_story_and_table_cells' for a in quality.values()), 32)
        forms = [a for a in quality.values() if a.get('blank_form')]
        self.assertEqual(len(forms), 1)
        self.assertFalse(forms[0]['business_values_present'])
        self.assertTrue(forms[0]['uncertainties'])
        document = next(p for p in policies if '重新修订' in p['title'] and '纳税申报办法' in p['title'])
        self.assertEqual(document['effective_from'], '2009-07-01')
        self.assertEqual(document['validity_status'], 'unverified')
        dates = json.loads(document['date_evidence_json'])
        self.assertTrue(any(e['effective_on'] == '2016-06-01' for e in dates['research_events']))

    def test_supplementary_vat_sources_do_not_approve_future_or_historical_policy(self):
        from src.kg_retrieval.applicability import check
        policies = [p for p in self.payload['tables']['Policy'] if p['policy_id'].startswith('VAT-COVERAGE-')]
        self.assertEqual(len(policies), 19)
        self.assertEqual(len({p['source_url'] for p in policies}), 19)
        self.assertTrue(all(p['validity_status'] == 'unverified' for p in policies))
        for p in policies:
            metadata = json.loads(p['source_metadata_json'])
            self.assertFalse(metadata['content_gaps'])
            self.assertEqual(metadata['legal_review'], 'pending_human_review')
        future = next(p for p in policies if p['document_no'] == '国家税务总局公告2026年第19号')
        historical = next(p for p in policies if '2015年第15号' in p['document_no'])
        self.assertEqual(future['effective_from'], '2026-11-01')
        self.assertEqual(historical['effective_to'], '2017-06-29')
        task = {'tax_type': 'VAT', 'region_code': 'CN-44', 'period_start': '2026-10-01',
                'period_end': '2026-10-21', 'data_scope': 'synthetic'}
        for p in [future, historical]:
            clause = next(c for c in self.payload['tables']['Clause'] if c['policy_id'] == p['policy_id'])
            decision = check(clause, p, task, [], source_root=self.home)
            self.assertEqual(decision['status'], 'excluded')
        ledger = json.loads((ROOT / 'docs/广东增值税政策覆盖审核.json').read_text(encoding='utf-8'))
        self.assertEqual(ledger['imported'], 19)
        self.assertEqual(ledger['failures'], [])
        self.assertTrue(any(r['status'] == 'unresolved' for r in ledger['reference_inventory']))
        for row in ledger['reference_inventory']:
            if row['status'] == 'covered':
                self.assertTrue(row['matched_policy_ids'])
        # Additional knowledge never silently enables more executable checks.
        self.assertEqual(len(self.payload['tables']['TaxRule']), 1257)

    def test_reference_review_preserves_quoted_errors_and_checks_evidence_offsets(self):
        ledger = json.loads((ROOT / 'docs/广东增值税引用效力日期复核.json').read_text(encoding='utf-8'))
        self.assertEqual(len(ledger['documents']), 139)
        self.assertEqual(len(ledger['policy_source_reviews']), 151)
        conflicts = [r for r in ledger['documents'] if r.get('identity_review')]
        self.assertEqual(len(conflicts), 6)
        self.assertFalse(any(r['status'] == 'citation_conflict_pending_verification' for r in conflicts))
        self.assertTrue(any('2013〕38号' in r['document_no_quoted'] for r in conflicts))
        self.assertTrue(any(r['status'] == 'pending_verification' for r in ledger['documents']))
        self.assertEqual(ledger['formal_approvals'], 0)
        for source in ledger['policy_source_reviews']:
            for proof in source['effect_notes'] + source.get('own_date_quotes', []):
                raw = (self.home / proof['retained_raw_path']).read_bytes()
                text_bytes = (self.home / proof['retained_text_path']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), proof['raw_sha256'])
                self.assertEqual(hashlib.sha256(text_bytes).hexdigest(), proof['text_sha256'])
                text = text_bytes.decode('utf-8')
                self.assertEqual(text[proof['start_char']:proof['end_char_exclusive']], proof['quote'])
        policies = self.payload['tables']['Policy']
        old = next(p for p in policies if p['document_no'] == '粤国税函〔2008〕652号')
        self.assertEqual((old['effective_from'], old['effective_to']), ('2009-01-01', '2009-06-30'))
        self.assertEqual(old['validity_status'], 'unverified')
        small = next(p for p in policies if p['document_no'] == '财政部 税务总局公告2023年第19号')
        self.assertEqual(small['effective_to'], '2027-12-31')
        self.assertIsNone(small['effective_from'])

    def test_reviewed_citations_ship_unique_targets_without_rewriting_source(self):
        resolutions = self.payload['citation_resolutions']
        self.assertEqual(len(resolutions), 6)
        policies = {p['policy_id']: p for p in self.payload['tables']['Policy']}
        for row in resolutions:
            if row['document_no_quoted'] == '国税发明电〔2003〕55号':
                self.assertEqual(row['matched_policy_ids'], [])
                self.assertEqual(row['status'], 'source_resolved_content_pending')
                self.assertEqual(row['canonical_title'], '国家税务总局关于加强货物运输业税收征收管理有关问题的通知')
                continue
            self.assertEqual(len(row['matched_policy_ids']), 1)
            target = policies[row['matched_policy_ids'][0]]
            self.assertEqual(target['validity_status'], 'unverified')
            self.assertIn(row['document_no_quoted'], row['quote'])
            for proof in row['identity_evidence']:
                raw = (self.home / proof['retained_text_path']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), proof['text_sha256'])
                self.assertEqual(raw.decode()[proof['start_char']:proof['end_char_exclusive']], proof['quote'])
        distinct = next(r for r in resolutions if r['document_no_quoted'] == '广东省国家税务局公告2013年第12号')
        self.assertEqual(distinct['identity_conclusion'], 'distinct_documents_correct_citation')
        self.assertIn('/201309/', policies[distinct['matched_policy_ids'][0]]['source_url'])
        self.assertNotEqual(distinct['source_url'], policies[distinct['matched_policy_ids'][0]]['source_url'])

    def test_citation_output_uses_correct_numbers_and_keeps_original_only_as_evidence(self):
        from src.public_policy_package import citation_view
        from src.kg_retrieval.batching import POLICY_FIELDS
        policies = {p['policy_id']: p for p in self.payload['tables']['Policy']}
        corrections = []
        for clause in self.payload['tables']['Clause']:
            view = citation_view(clause, policies[clause['policy_id']])
            if 'citation_corrections' not in view:
                continue
            corrections.extend(view['citation_corrections'])
            self.assertEqual(view['original_text'], clause['content'])
            for change in view['citation_corrections']:
                self.assertIn(change['corrected_quote'], view['text'])
                self.assertNotIn(change['original_quote'], view['text'])
            unrelated = {**policies[clause['policy_id']], 'source_url': 'https://example.gov.cn/other'}
            self.assertEqual(citation_view(clause, unrelated), {'text': clause['content']})
        self.assertEqual(len(corrections), 5)
        self.assertIn('text', POLICY_FIELDS)
        self.assertNotIn('original_text', POLICY_FIELDS)
        self.assertNotIn('citation_corrections', POLICY_FIELDS)
        legacy = {'content': '原有条款', 'source_location': '附件一第3页'}
        self.assertEqual(citation_view(legacy, {}), {'text': '原有条款'})
        graph = pg.graph(self.db)
        corrected_nodes = [n for n in graph['nodes'] if n.get('citation_corrected')]
        self.assertEqual(len(corrected_nodes), 5)
        for node in corrected_nodes:
            raw = dict(self.db.execute('SELECT * FROM Clause WHERE clause_id=?', (node['clause_id'],)).fetchone())
            self.assertNotEqual(node['content'], raw['content'])
            self.assertEqual(node['content'], node['requirement'])
            self.assertNotIn('original_text', node)

    def test_supplement_retains_exceptions_and_evidenced_version_changes(self):
        policies = self.payload['tables']['Policy']
        def policy(prefix):
            return next(p for p in policies if p['policy_id'].startswith(prefix+':organized:'))
        new = policy('VAT-REGISTER-2026-2')
        old = policy('VAT-REGISTER-2018-43')
        hainan = policy('PIT-HAINAN-2020-41')
        self.assertEqual(old['effective_to'], '2025-12-31')
        self.assertEqual(hainan['effective_to'], '2022-12-31')
        self.assertEqual(hainan['region_code'], 'CN-HI')
        clauses = {c['clause_id']:c for c in self.payload['tables']['Clause']}
        text = ''.join(c['content'] for c in clauses.values() if c['policy_id']==new['policy_id'])
        for limit in ['自然人', '非企业单位', '偶然发生', '当期1日', '2025年四季度', '2018年第6号']:
            self.assertIn(limit, text)
        edge = next(e for e in self.payload['tables']['Relationship'] if e['to_id']==old['policy_id'] and e['relation_type']=='REPEALS')
        self.assertIn('第43号', clauses[edge['evidence_clause_id']]['content'])
        self.assertEqual(edge['review_status'], 'candidate')
        self.assertEqual(new['validity_status'], 'unverified')
        reform=policy('VAT-REFORM-2019-39')
        rate_edge=next(e for e in self.payload['tables']['Relationship'] if e['from_id']==reform['policy_id'] and e['relation_type']=='AMENDS')
        self.assertIn('9%',clauses[rate_edge['evidence_clause_id']]['content'])

    def test_repeat_after_relocation_preserves_reviews_and_local_school_data(self):
        key = self.payload['tables']['Policy'][0]['policy_id']
        with self.db:
            self.db.execute("UPDATE Policy SET validity_status='in_force',issuer='人工复核机关' WHERE policy_id=?", (key,))
        before = [tuple(r) for r in self.db.execute('SELECT * FROM knowledge_point')]
        with self.db:
            result = import_package(self.db, self.payload, ROOT)
        self.assertEqual(sum(result['inserted'].values()), 0)
        self.assertEqual(self.db.execute('SELECT validity_status FROM Policy WHERE policy_id=?', (key,)).fetchone()[0], 'in_force')
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM knowledge_point')], before)
        self.assertEqual(self.db.execute('SELECT secret FROM local_accounts').fetchone()[0], 'private-sentinel')

    def test_content_conflict_stops_without_inserting_other_nodes(self):
        modified = deepcopy(self.payload)
        modified['tables']['Clause'][0]['content'] += ' conflicting edit'
        before = self.db.total_changes
        with self.assertRaises(ValueError), self.db:
            import_package(self.db, modified, ROOT)
        self.assertEqual(self.db.total_changes, before)

    def test_retained_file_tamper_stops_without_database_changes(self):
        file = self.home/self.payload['originals'][0]['path']
        file.write_bytes(b'tampered-engineering-fixture')
        before = self.db.total_changes
        with self.assertRaisesRegex(ValueError, '原文内容冲突'), self.db:
            import_package(self.db, self.payload, ROOT)
        self.assertEqual(self.db.total_changes, before)

    def test_late_relationship_failure_rolls_back_rows_and_new_files(self):
        modified = deepcopy(self.payload)
        edge = modified['tables']['Relationship'][0]
        edge['relationship_id'] = 'synthetic-invalid-edge'
        edge['to_id'] = 'synthetic-missing-endpoint'
        fresh = self.home/'empty'
        fresh.mkdir()
        db = sqlite3.connect(fresh/'empty.sqlite')
        db.row_factory = sqlite3.Row
        try:
            pg.migrate(db)
            with self.assertRaises(ValueError), db:
                import_package(db, modified, ROOT)
            self.assertEqual(db.execute('SELECT count(*) FROM Policy').fetchone()[0], 0)
            self.assertEqual(list((fresh/'policy-originals').iterdir()), [])
        finally:
            db.close()

    def test_checked_in_package_hash_and_relative_paths_are_stable(self):
        manifest = json.loads((ROOT/'data/public-policy/manifest.json').read_text(encoding='utf-8'))
        raw = (ROOT/'data/public-policy/snapshot.json.gz').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['compressed_sha256'])
        self.assertTrue(all(not Path(r['raw_file_path']).is_absolute() for r in self.payload['tables']['Policy']))
