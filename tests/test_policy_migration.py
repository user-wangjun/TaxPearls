"""Migration acceptance against the complete retained sources, not node counts alone."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import yaml

from src import policy_corpus as corpus, policy_migration as migration, policies
from webapp import policy_graph as pg

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT/'docs/法规原文/来源清单.json'


class PolicyMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = migration.prepare(MANIFEST)
        cls.old = corpus.prepare(MANIFEST)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        pg.migrate(self.db)
        self.addCleanup(self.db.close)

    def import_all(self):
        return corpus.import_plan(self.db, self.plan, self.root/'originals')

    def test_every_existing_policy_and_clause_is_preserved_byte_for_byte(self):
        for table in ['Policy','Clause']:
            by_id = {r[pg.IDS[table]]:r for r in self.plan['bundle'][table]}
            for old in self.old['bundle'][table]:
                self.assertEqual(by_id[old[pg.IDS[table]]], old)
        self.assertTrue(self.old['bundle']['Clause'], 'source corpus must not be empty')

    def test_every_source_clause_has_auditable_coverage_and_lossless_predicates(self):
        plan = self.plan
        coverage = {r['clause_id']:r for r in plan['knowledge_migration']['coverage']}
        rules = {r['rule_id']:r for r in plan['bundle']['TaxRule']}
        for clause in plan['bundle']['Clause']:
            record = coverage[clause['clause_id']]
            self.assertEqual(record['chars'],len(clause['content']))
            if record['role'] == 'source_context':
                self.assertIsNone(record['rule_id'])
                continue
            note = json.loads(rules[record['rule_id']]['notes'])
            parts = note['fragments']
            self.assertEqual(''.join(p['text'] for p in parts),clause['content'])
            for p in parts:
                self.assertEqual(clause['content'][p['start']:p['end']],p['text'])

    def test_all_seven_catalogs_keep_conditions_exclusions_parameters_inputs_and_history(self):
        rules = {r['rule_code']:r for r in self.plan['bundle']['TaxRule'] if r['rule_code']}
        for filename in ['国家级税费政策.yaml','广东省地方政策.yaml']:
            catalog = yaml.safe_load((ROOT/'docs'/filename).read_text(encoding='utf-8'))
            for topic in catalog['policies']:
                rule = rules[topic['id']]
                note = json.loads(rule['notes'])
                self.assertEqual(note['catalog_annotation'],topic)
                self.assertEqual(note['required_fact_fields'],policies.POLICY_FIELDS[topic['id']])
                self.assertEqual(rule['applicable_region'],'CN-GD' if topic['id'].startswith('GD-') else 'CN')
                self.assertEqual(rule['valid_from'],topic['effective_from'])
                for kind in ['conditions','exclusions']:
                    migrated = [json.loads(c['notes'])['expression'] for c in self.plan['bundle']['Condition']
                                if c['rule_id']==rule['rule_id'] and json.loads(c['notes'])['source_span'][0]==kind]
                    self.assertEqual(migrated,topic[kind])
        self.assertEqual(set(rules),set(policies.POLICY_FIELDS))

    def test_official_snapshots_have_honest_distinct_provenance_and_matching_hashes(self):
        snapshots = [c for c in self.plan['bundle']['Clause'] if c['clause_label']=='已采官方原文快照']
        self.assertEqual(len(snapshots),12)
        for clause in snapshots:
            loc=json.loads(clause['source_location'])
            self.assertEqual(corpus.sha(''.join(clause['content'].split()).encode()),loc['snapshot_sha256'])
            self.assertEqual(loc['provenance_kind'],'retained_catalog_snapshot')
            self.assertNotEqual(loc['upstream_response_sha256'],loc['raw_sha256'])

    def test_staggered_subject_dates_survive_without_an_unconditional_repeal(self):
        phases=[r for r in self.plan['bundle']['TaxRule'] if json.loads(r['notes']).get('phase_in')]
        self.assertEqual({r['valid_from'] for r in phases},{'2027-01-01','2029-01-01','2030-01-01'})
        for rule in phases:
            self.assertIn(json.loads(rule['notes'])['phase_in']['subject'],rule['name'])
        changes=[e for e in self.plan['bundle']['Relationship'] if e['relation_type'] in {'REPEALS','AMENDS'}]
        expected = {r['relationship_id']: r for r in self.old['bundle']['Relationship']
                    if r['relation_type'] in {'REPEALS', 'AMENDS'}}
        self.assertEqual({r['relationship_id']: r for r in changes}, expected)
        # Other source relationships (e.g. INTERPRETS) must survive too.
        all_edges = {r['relationship_id']: r for r in self.plan['bundle']['Relationship']}
        for old in self.old['bundle']['Relationship']:
            self.assertEqual(all_edges[old['relationship_id']], old)

    def test_chinese_choices_and_exceptions_do_not_turn_into_an_executable_conjunction(self):
        text='第一条 居民企业或者非居民企业符合条件的，依本法办理；但是另有规定的，从其规定。\n'
        clause=dict(clause_id='fiction:article',policy_id='fiction',clause_label='第一条',content=text)
        plan=dict(bundle={'Policy':[dict(policy_id='fiction',title='工程样本',tax_type='EIT',region_code='CN')],
                          'Clause':[clause],'Relationship':[]})
        migration.add_knowledge(plan)
        rule=plan['bundle']['TaxRule'][0]
        self.assertEqual(set(json.loads(rule['condition_logic_json'])),{'ref'})
        self.assertIn('或者',rule['outcome_text'])
        self.assertTrue(plan['bundle']['Exception'])
        self.assertTrue(all(x['effect']=='needs_review' for x in plan['bundle']['Exception']))

    def test_upgrade_from_original_corpus_is_idempotent_and_preserves_explicit_reviews(self):
        corpus.import_plan(self.db,self.old,self.root/'old-originals')
        cid=self.old['bundle']['Clause'][5]['clause_id']
        self.db.execute("UPDATE Clause SET extraction_status='verified' WHERE clause_id=?",(cid,))
        self.import_all()
        before={t:self.db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in pg.IDS}
        self.import_all()
        after={t:self.db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in pg.IDS}
        self.assertEqual(before,after)
        self.assertEqual(self.db.execute('SELECT extraction_status FROM Clause WHERE clause_id=?',(cid,)).fetchone()[0],'verified')
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(self.db.execute('SELECT count(*) FROM kg_clause_fts').fetchone()[0],after['Clause'])

    def test_all_rules_have_real_source_edges_and_all_conditions_and_exceptions_have_sources(self):
        self.import_all()
        self.assertEqual(self.db.execute("SELECT count(*) FROM TaxRule t WHERE NOT EXISTS (SELECT 1 FROM Relationship r WHERE r.to_type='TaxRule' AND r.to_id=t.rule_id AND r.relation_type='DERIVES_RULE')").fetchone()[0],0)
        for table in ['Condition','Exception']:
            self.assertEqual(self.db.execute(f'SELECT count(*) FROM "{table}" WHERE source_clause_id IS NULL').fetchone()[0],0)
        for edge in self.db.execute('SELECT * FROM Relationship'):
            pg._check_relationship(self.db,dict(edge))

    def test_candidate_migration_cannot_grant_legal_approval_or_produce_true(self):
        self.import_all()
        for code in policies.POLICY_FIELDS:
            rule=self.db.execute('SELECT * FROM TaxRule WHERE rule_code=?',(code,)).fetchone()
            result=pg.evaluate_rule(self.db,rule['rule_id'],{},start='2026-01-01',end='2026-12-31',region='CN-GD')
            self.assertEqual(result['truth'],'UNKNOWN')
            self.assertEqual(result['disposition'],'needs_review')
            with self.assertRaises(ValueError):
                pg.approve_rule(self.db,rule['rule_id'],'test-reviewer')

    def test_source_tamper_is_rejected_before_any_graph_mutation(self):
        plan=deepcopy(self.plan)
        first=next(iter(plan['files']))
        plan['files'][first]['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'哈希'):
            corpus.import_plan(self.db,plan,self.root/'originals')
        self.assertEqual(self.db.execute('SELECT count(*) FROM Policy').fetchone()[0],0)

    def test_failure_during_exception_import_rolls_back_the_entire_upgrade(self):
        original=pg.import_bundle
        def fail(db,rows,**options):
            if 'Exception' in rows:
                raise ValueError('exception-stage-failure')
            return original(db,rows,**options)
        with patch.object(pg,'import_bundle',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'exception-stage'):
                self.import_all()
        for table in pg.IDS:
            self.assertEqual(self.db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0],0)
        self.assertEqual(list((self.root/'originals').iterdir()),[])

    def test_admin_import_and_public_graph_expose_guangdong_and_restart_idempotently(self):
        import base64
        import os
        from fastapi.testclient import TestClient
        from webapp import app as module
        from webapp.storage import Store
        with patch.dict(os.environ, {'TAXPEARLS_FIELD_KEY':base64.b64encode(b'm'*32).decode(),
                                     'TAXPEARLS_AI_ENABLED':'0'}):
            store=Store(self.root/'business.sqlite')
            store.create_user('migration-admin','Migration-test-2026!','工程测试','platform_admin','test-org')
            store.create_user('migration-other','Migration-test-2026!','工程测试','org_admin','other-org')
            with patch.object(module,'store',store), TestClient(module.app) as client:
                manifest_hash=corpus.sha(MANIFEST.read_bytes())
                body={'manifest_sha256':manifest_hash}
                self.assertEqual(client.post('/api/knowledge/corpus/import',json=body).status_code,401)
                token=store.authenticate('migration-other','Migration-test-2026!')[1]
                client.cookies.set(module.COOKIE_NAME,token)
                self.assertEqual(client.post('/api/knowledge/corpus/import',json=body).status_code,403)
                client.cookies.set(module.COOKIE_NAME,store.authenticate('migration-admin','Migration-test-2026!')[1])
                response=client.post('/api/knowledge/corpus/import',json=body)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['formal_approvals'],0)
                status=client.get('/api/knowledge/corpus/status').json()
                self.assertEqual(status['knowledge_migration']['guangdong_topics'],2)
                self.assertGreater(status['counts']['Exception'],0)
                graph=client.get('/api/knowledge/graph')
                self.assertEqual(graph.status_code,200,graph.text[:500])
                by_code={n.get('rule_code'):n for n in graph.json()['nodes'] if n['kind']=='rule'}
                for code in ['GD-DEED-BASE','GD-PROPERTY-BASE']:
                    self.assertEqual(by_code[code]['rule_level'],'provincial')
                    self.assertEqual(by_code[code]['region'],'广东省')
                again=client.post('/api/knowledge/corpus/import',json=body)
                self.assertEqual(again.status_code,200,again.text[:500])
                self.assertEqual(status['counts'],client.get('/api/knowledge/corpus/status').json()['counts'])


if __name__=='__main__':
    unittest.main()
