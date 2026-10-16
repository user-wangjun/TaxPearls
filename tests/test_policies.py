"""Synthetic business facts; boundary values/dates come from official sources."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src import engine, policies
from src.input_errors import InputError
from src.models import Company, Dataset
from src.snapshots import serialize_dataset, deserialize_dataset

ROOT = Path(__file__).resolve().parents[1]
SCOPE = {'name': '政策测试企业（合成）', 'taxpayer_id': 'SYNTHETIC-POLICY', 'region': '广东省',
         'period_start': '2026-01-01', 'period_end': '2026-12-31'}


def facts(**values):
    return {key: {'value': str(value), 'source': '合成业务依据：测试材料第1页/' + key} for key, value in values.items()}


def vat(**changes):
    return facts(**{'vat_small': 'yes', 'vat_subject': 'enterprise', 'vat_cycle': 'month',
        'vat_sales': '99999.99', 'vat_sales_checked': 'yes', 'vat_waiver': 'none', 'vat_special': 'no', **changes})


def cit(**changes):
    return facts(**{'cit_nonrestricted': 'yes', 'cit_income': '3000000', 'cit_people': '300',
        'cit_assets': '50000000', 'cit_stats': 'quarter_average', 'cit_income_checked': 'yes', **changes})


def rd(**changes):
    return facts(**{'rd_resident': 'yes', 'rd_audit': 'yes', 'rd_industry': 'eligible', 'rd_activity': 'yes',
        'rd_accounts': 'yes', 'rd_costs': 'yes', 'rd_mode': 'expense', **changes})


def review(scope, selected, values):
    value = {'selected': selected, 'facts': values}
    first = policies.assess(scope, value)
    value['reviews'] = {r['id']: r['review_token'] for r in first['results'] if r['review_token']}
    return policies.assess(scope, value), value


class PolicyTests(unittest.TestCase):
    def result(self, id, values, scope=None):
        return review(scope or SCOPE, [id], values)[0]['results'][0]

    def test_container_copy_layout_can_load_policy_catalog_without_checkout(self):
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('COPY ["docs/企业政策适用目录.yaml", "./docs/企业政策适用目录.yaml"]',
                      [line.strip() for line in dockerfile.splitlines()])
        with TemporaryDirectory(prefix='taxpearls-policy-layout-') as directory:
            target = Path(directory)
            shutil.copytree(ROOT / 'src', target / 'src', ignore=shutil.ignore_patterns('__pycache__'))
            (target / 'docs').mkdir()
            shutil.copyfile(policies.CATALOG_PATH, target / 'docs' / policies.CATALOG_PATH.name)
            result = subprocess.run([sys.executable, '-B', '-c',
                'from src import policies; '
                'assert policies.program_current(); '
                'assert len(policies.public_catalog()["policies"]) == 7; '
                'assert len(policies.public_catalog()["sources"]) == 12; '
                'assert policies.assess({}, {}) is None'], cwd=target,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_catalog_preserves_24_rule_definitions_and_verified_official_evidence(self):
        baseline = [(p.name, p.read_bytes()) for p in (ROOT / 'rules').glob('*.yaml')]
        self.assertEqual(len(engine.load_rules(ROOT / 'rules')), 24)
        cat = policies.public_catalog()
        self.assertEqual(len(cat['policies']), 7)
        self.assertEqual(len(cat['sources']), 12)
        for source in cat['sources']:
            self.assertTrue(source['verification']['anchors_verified'])
            self.assertEqual(source['verification']['checked_on'], '2026-10-06')
            self.assertEqual(len(source['verification']['text_sha256']), 64)
        self.result('CIT-SMALL', cit())
        self.assertEqual(baseline, [(p.name, p.read_bytes()) for p in (ROOT / 'rules').glob('*.yaml')])

    def test_vat_threshold_strict_official_boundary_and_period_basis(self):
        for cycle, limit, start, end in [('month', 100000, '2026-01-01', '2026-01-31'),
                ('quarter', 300000, '2026-01-01', '2026-03-31'), ('day', 1000, '2026-01-02', '2026-01-02')]:
            scope = {**SCOPE, 'period_start': start, 'period_end': end}
            for sales, expected in [(str(limit-1), 'applicable'), (str(limit), 'not_applicable'), (str(limit+1), 'not_applicable')]:
                with self.subTest(cycle=cycle, sales=sales):
                    self.assertEqual(self.result('VAT-THRESHOLD-2026', vat(vat_cycle=cycle, vat_sales=sales), scope)['status'], expected)
        self.assertEqual(self.result('VAT-THRESHOLD-2026', vat())['status'], 'needs_review')

    def test_real_vat_effective_dates_not_publication_date_or_old_inclusive_rule(self):
        for start, end, expected in [('2025-12-01', '2025-12-31', 'not_applicable'),
                ('2026-01-01', '2026-01-31', 'applicable'), ('2027-12-01', '2027-12-31', 'applicable'),
                ('2028-01-01', '2028-01-31', 'not_applicable'), ('2025-12-01', '2026-01-31', 'needs_review')]:
            with self.subTest(start=start):
                r=self.result('VAT-THRESHOLD-2026', vat(), {**SCOPE, 'period_start':start,'period_end':end})
                self.assertEqual(r['status'], expected)

    def test_vat_reduction_exclusions_waivers_and_exemption_first(self):
        scope={**SCOPE,'period_end':'2026-01-31'}
        common={**vat(vat_sales='100000'), **facts(vat_business='ordinary',vat_other_exemption='no',vat_reduction_waiver='no',vat_separate='yes')}
        self.assertEqual(self.result('VAT-REDUCED-2026', common,scope)['status'],'applicable')
        for key,val,expected in [('vat_business','real_estate','not_applicable'), ('vat_business','land','not_applicable'),
                ('vat_business','used','needs_review'),('vat_reduction_waiver','yes','not_applicable'),
                ('vat_other_exemption','yes','not_applicable'),('vat_sales','99999.99','not_applicable'),
                ('vat_waiver','partial','needs_review')]:
            with self.subTest(key=key,val=val):
                self.assertEqual(self.result('VAT-REDUCED-2026',{**common,**facts(**{key:val})},scope)['status'],expected)
        both,_=review(scope,['VAT-THRESHOLD-2026','VAT-REDUCED-2026'],{**common,**facts(vat_sales='99999')})
        self.assertEqual([r['status'] for r in both['results']],['applicable','not_applicable'])
        self.assertEqual(both['relationships'][0]['kind'],'precedence')

    def test_natural_person_month_exception_and_conflicting_taxpayer_identity(self):
        scope={**SCOPE,'period_end':'2026-01-31'}
        self.assertEqual(self.result('VAT-THRESHOLD-2026',vat(vat_subject='natural',vat_natural_month='no'),scope)['status'],'needs_review')
        self.assertEqual(self.result('VAT-THRESHOLD-2026',vat(vat_subject='natural',vat_natural_month='yes'),scope)['status'],'applicable')
        self.assertEqual(self.result('VAT-THRESHOLD-2026',vat(),{**scope,'taxpayer_type':'一般纳税人'})['status'],'needs_review')

    def test_cit_all_three_inclusive_limits_statistics_and_actual_period(self):
        self.assertEqual(self.result('CIT-SMALL',cit())['status'],'applicable')
        for key,val in [('cit_income','3000000.01'),('cit_people','300.01'),('cit_assets','50000000.01'),('cit_nonrestricted','no')]:
            with self.subTest(key=key):
                self.assertEqual(self.result('CIT-SMALL',cit(**{key:val}))['status'],'not_applicable')
        self.assertEqual(self.result('CIT-SMALL',cit(cit_income='-1'))['status'],'applicable')
        self.assertEqual(self.result('CIT-SMALL',cit(cit_stats='year_end'))['status'],'needs_review')
        self.assertEqual(self.result('CIT-SMALL',cit(),{**SCOPE,'period_end':'2026-03-31'})['status'],'needs_review')
        partial={**SCOPE,'period_start':'2026-07-01'}
        self.assertEqual(self.result('CIT-SMALL',cit(cit_stats='operating_average',cit_operating_period='yes'),partial)['status'],'applicable')
        for year,expected in [('2022','not_applicable'),('2023','applicable'),('2027','applicable'),('2028','not_applicable')]:
            self.assertEqual(self.result('CIT-SMALL',cit(),{**SCOPE,'period_start':year+'-01-01','period_end':year+'-12-31'})['status'],expected)

    def test_six_two_or_eligibility_excluded_taxes_new_enterprise_and_stacking(self):
        common=facts(six_tax='property',six_eligibility='small_vat',vat_small='yes',six_other_reliefs='yes')
        self.assertEqual(self.result('SMALL-SIX-TWO',common)['status'],'applicable')
        for tax in ['water','securities','vat','cit','deed','other']:
            self.assertEqual(self.result('SMALL-SIX-TWO',{**common,**facts(six_tax=tax)})['status'],'not_applicable')
        self.assertEqual(self.result('SMALL-SIX-TWO',facts(six_tax='property',six_eligibility='small_cit',six_settlement='no',six_other_reliefs='yes'))['status'],'needs_review')
        new=facts(six_tax='property',six_eligibility='new_general',six_new='yes',six_before_settlement='yes',cit_nonrestricted='yes',six_people='300',six_assets='50000000',six_other_reliefs='yes')
        self.assertEqual(self.result('SMALL-SIX-TWO',new)['status'],'applicable')
        self.assertEqual(self.result('SMALL-SIX-TWO',{**new,**facts(six_assets='50000001')})['status'],'not_applicable')
        assessed,_=review(SCOPE,['SMALL-SIX-TWO'],common)
        self.assertEqual(assessed['relationships'][0]['kind'],'stack')
        self.assertIn('剩余税额',assessed['relationships'][0]['message'])

    def test_rd_qualification_modes_limit_and_replaced_old_ratio(self):
        self.assertEqual(self.result('CIT-RD',rd())['status'],'applicable')
        for key,val in [('rd_resident','no'),('rd_audit','no'),('rd_activity','no'),('rd_accounts','no'),('rd_industry','trade'),('rd_mode','overseas_individual')]:
            self.assertEqual(self.result('CIT-RD',rd(**{key:val}))['status'],'not_applicable')
        self.assertEqual(self.result('CIT-RD',rd(rd_mode='capital',rd_amortization='yes'))['status'],'applicable')
        self.assertEqual(self.result('CIT-RD',rd(rd_mode='mixed'))['status'],'needs_review')
        overseas=rd(rd_mode='overseas_org',rd_contract='yes',rd_domestic_cost='120',rd_overseas_cost='100')
        self.assertEqual(self.result('CIT-RD',overseas)['status'],'applicable')
        self.assertEqual(self.result('CIT-RD',{**overseas,**facts(rd_overseas_cost='100.01')})['status'],'needs_review')
        r=self.result('CIT-RD',rd())
        self.assertEqual(r['parameters'][0]['value'],'1.00')
        self.assertEqual(self.result('CIT-RD',rd(),{**SCOPE,'period_start':'2022-01-01','period_end':'2022-12-31'})['status'],'not_applicable')

    def test_guangdong_location_basis_use_and_real_effective_dates(self):
        common=facts(property_location='guangdong',property_area='yes',property_payer='yes',property_use='own',property_basis='yes',property_exemptions='yes')
        self.assertEqual(self.result('GD-PROPERTY-BASE',common)['status'],'applicable')
        self.assertEqual(self.result('GD-PROPERTY-BASE',{**common,**facts(property_location='other')})['status'],'not_applicable')
        self.assertEqual(self.result('GD-PROPERTY-BASE',{**common,**facts(property_use='mixed')})['status'],'needs_review')
        for day,expected in [('2021-09-28','not_applicable'),('2021-09-29','applicable'),('2021-10-01','applicable')]:
            self.assertEqual(self.result('GD-PROPERTY-BASE',common,{**SCOPE,'period_start':day,'period_end':day})['status'],expected)
        deed=facts(property_location='guangdong',deed_kind='ordinary',deed_national='yes')
        for day,expected in [('2021-08-31','not_applicable'),('2021-09-01','applicable')]:
            self.assertEqual(self.result('GD-DEED-BASE',deed,{**SCOPE,'period_start':day,'period_end':day})['status'],expected)
        cash={**deed,**facts(deed_kind='cash',deed_proof='yes',deed_price='100',deed_compensation='100')}
        self.assertIn('全部价格在免税',self.result('GD-DEED-BASE',cash)['notes'][0])
        self.assertEqual(self.result('GD-DEED-BASE',{**deed,**facts(deed_kind='housing')})['status'],'needs_review')

    def test_missing_sources_unknown_values_and_review_binding(self):
        first=policies.assess(SCOPE,{'selected':['CIT-SMALL'],'facts':cit()})
        self.assertEqual(first['results'][0]['status'],'needs_review')
        assessed,inputs=review(SCOPE,['CIT-SMALL'],cit())
        self.assertEqual(assessed['results'][0]['status'],'applicable')
        saved = {f['id']: f for f in assessed['results'][0]['facts']}
        self.assertEqual(saved['cit_nonrestricted']['value'], 'yes')
        self.assertEqual(saved['cit_nonrestricted']['value_label'], '是（有依据）')
        self.assertEqual(saved['cit_income']['value_label'], '3000000')
        for key,value in [('cit_people','299'),('cit_assets','1')]:
            altered=deepcopy(inputs);altered['facts'][key]['value']=value
            self.assertEqual(policies.assess(SCOPE,altered)['results'][0]['status'],'needs_review')
        altered=deepcopy(inputs);altered['facts']['cit_income']['source']='另一原件位置'
        self.assertEqual(policies.assess(SCOPE,altered)['results'][0]['status'],'needs_review')
        self.assertEqual(policies.assess({**SCOPE,'taxpayer_id':'OTHER'},inputs)['results'][0]['status'],'needs_review')
        altered=deepcopy(inputs);altered['facts']['cit_income']['source']=''
        r=policies.assess(SCOPE,altered)['results'][0]
        self.assertIn('cit_income',[f['id'] for f in r['missing_fields']])
        with patch.object(policies,'fingerprint',return_value='x'*64):
            self.assertEqual(policies.assess(SCOPE,inputs)['results'][0]['status'],'needs_review')
            with self.assertRaises(InputError):policies.assert_current(assessed)

    def test_cross_policy_vat_identity_conflict_cannot_be_reviewed_away(self):
        values = {**vat(), **facts(six_tax='property', six_eligibility='new_general',
            six_new='yes', six_before_settlement='yes', cit_nonrestricted='yes',
            six_people='100', six_assets='1000000', six_other_reliefs='yes')}
        for vat_id in ['VAT-THRESHOLD-2026', 'VAT-REDUCED-2026']:
            selected = [vat_id, 'SMALL-SIX-TWO']
            current = values if vat_id == 'VAT-THRESHOLD-2026' else {**values,
                **facts(vat_business='ordinary', vat_other_exemption='no', vat_reduction_waiver='no', vat_separate='yes')}
            for identity in ['', '一般纳税人', '小规模纳税人', '增值税小规模纳税人']:
                with self.subTest(identity=identity, vat_id=vat_id):
                    scope = {**SCOPE, 'period_end': '2026-01-31', 'taxpayer_type': identity}
                    assessment, inputs = review(scope, selected, current)
                    self.assertEqual(assessment['consistency']['status'], 'conflict')
                    for result in assessment['results']:
                        self.assertEqual(result['status'], 'needs_review')
                        self.assertIsNone(result['review_token'])
                        self.assertFalse(result['reviewed'])
                    inputs['reviews'] = {key: 'a' * 64 for key in selected}
                    self.assertTrue(all(r['status'] == 'needs_review'
                        for r in policies.assess(scope, inputs)['results']))
        # The company declaration also constrains a single selected policy.
        scope = {**SCOPE, 'taxpayer_type': '小规模纳税人'}
        six_values = {k: v for k, v in values.items() if k in policies.POLICY_FIELDS['SMALL-SIX-TWO']}
        self.assertEqual(self.result('SMALL-SIX-TWO', six_values, scope)['status'], 'needs_review')
        # An assertion without its source is not silently ignored in another policy.
        no_source = deepcopy(values)
        no_source['vat_small']['source'] = ''
        assessment, _ = review({**SCOPE, 'period_end': '2026-01-31'},
            ['VAT-THRESHOLD-2026', 'SMALL-SIX-TWO'], no_source)
        self.assertEqual(assessment['consistency']['status'], 'conflict')

    def test_consistent_tax_identities_and_inactive_qualification_facts(self):
        scope = {**SCOPE, 'period_end': '2026-01-31', 'taxpayer_type': '一般纳税人'}
        values = {**vat(vat_small='no'), **facts(six_tax='property', six_eligibility='new_general',
            six_new='yes', six_before_settlement='yes', cit_nonrestricted='yes',
            six_people='100', six_assets='1000000', six_other_reliefs='yes')}
        assessment, _ = review(scope, ['VAT-THRESHOLD-2026', 'SMALL-SIX-TWO'], values)
        self.assertEqual(assessment['consistency']['status'], 'checked')
        self.assertEqual({r['id']: r['status'] for r in assessment['results']},
            {'VAT-THRESHOLD-2026': 'not_applicable', 'SMALL-SIX-TWO': 'applicable'})
        values.update(facts(vat_small='yes', six_eligibility='small_vat'))
        # Old new-general details are hidden after changing the qualification route.
        assessment, _ = review({**scope, 'taxpayer_type': '小规模纳税人'},
            ['VAT-THRESHOLD-2026', 'SMALL-SIX-TWO'], values)
        self.assertEqual(assessment['consistency']['status'], 'checked')
        self.assertTrue(all(r['status'] == 'applicable' for r in assessment['results']))
        r = self.result('VAT-THRESHOLD-2026', vat(vat_small='no'),
            {**scope, 'taxpayer_type': '小规模纳税人'})
        self.assertEqual(r['status'], 'needs_review')

    def test_cross_policy_subject_conflict_only_invalidates_affected_policies(self):
        scope = {**SCOPE, 'period_end': '2026-01-31'}
        property_values = facts(property_location='guangdong', property_area='yes', property_payer='yes',
            property_use='own', property_basis='yes', property_exemptions='yes')
        selected = ['VAT-THRESHOLD-2026', 'SMALL-SIX-TWO', 'GD-PROPERTY-BASE']
        values = {**vat(), **property_values, **facts(six_tax='property',
            six_eligibility='individual', six_individual='yes', six_other_reliefs='yes')}
        for subject in ['enterprise', 'natural']:
            with self.subTest(subject=subject):
                current = {**values, **facts(vat_subject=subject, vat_natural_month='yes')}
                assessment, _ = review(scope, selected, current)
                statuses = {r['id']: r['status'] for r in assessment['results']}
                self.assertEqual(statuses['VAT-THRESHOLD-2026'], 'needs_review')
                self.assertEqual(statuses['SMALL-SIX-TWO'], 'needs_review')
                self.assertEqual(statuses['GD-PROPERTY-BASE'], 'applicable')
                self.assertIn('vat_subject', assessment['consistency']['issues'][0]['field_ids'])
        assessment, _ = review(scope, selected, {**values, **facts(vat_subject='individual_business')})
        self.assertEqual(assessment['consistency']['status'], 'checked')
        self.assertTrue(all(r['status'] == 'applicable' for r in assessment['results']))

    def test_unresolved_combination_is_explicit_not_a_combined_approval(self):
        assessment, inputs = review(SCOPE, ['CIT-SMALL', 'CIT-RD'], {**cit(), **rd()})
        self.assertTrue(all(r['status'] == 'applicable' for r in assessment['results']))
        self.assertEqual(assessment['consistency']['status'], 'needs_review')
        self.assertFalse(assessment['consistency']['combination_conclusion_enabled'])
        self.assertIn('组合', assessment['consistency']['status_label'])
        # No overall status can be supplied by clients or acknowledged into approval.
        inputs['consistency'] = {'status': 'checked'}
        with self.assertRaises(InputError):
            policies.assess(SCOPE, inputs)

    def test_input_cannot_inject_dataset_policy_result_or_nan_or_booleans(self):
        for value in [{'selected':['BAD']},{'selected':['CIT-SMALL']*2},{'selected':['CIT-SMALL'],'status':'applicable'},
                {'selected':['CIT-SMALL'],'facts':facts(cit_income='NaN')},
                {'selected':['CIT-SMALL'],'facts':facts(cit_income='1e2')},
                {'selected':['CIT-SMALL'],'facts':{'cit_income':{'value':True,'source':'a'}}},
                {'selected':['CIT-SMALL'],'facts':facts(cit_income='1',vat_small='yes')},
                {'selected':['CIT-SMALL'],'reviews':{'CIT-SMALL':True}}]:
            with self.subTest(value=value),self.assertRaises(InputError):policies.normalize(value)

    def test_loaded_program_and_catalog_read_race_cannot_approve_old_code(self):
        assessment,_=review(SCOPE,['CIT-SMALL'],cit())
        with patch.object(policies,'program_current',return_value=False):
            self.assertFalse(policies.public_catalog()['program_current'])
            with self.assertRaises(InputError):policies.assess(SCOPE,{'selected':['CIT-SMALL'],'facts':cit()})
            with self.assertRaises(InputError):policies.assert_current(assessment)
            self.assertIsNone(policies.assess(SCOPE,{}))
        with patch.object(policies,'fingerprint',side_effect=['a'*64,'b'*64]):
            with self.assertRaises(InputError):policies.assess(SCOPE,{'selected':['CIT-SMALL'],'facts':cit()})

    def test_backward_compatible_freezing_without_mutating_risk_metric_space(self):
        ds=Dataset(Company('A','ID','industry','2026'),[],{}, {})
        old=serialize_dataset(ds)
        self.assertNotIn('policy_assessment',old)
        assessment,_=review(SCOPE,['CIT-SMALL'],cit())
        new=replace(ds,policy_assessment=assessment)
        payload=serialize_dataset(new)
        restored=deserialize_dataset(payload)
        self.assertEqual(restored.policy_assessment,assessment)
        payload['policy_assessment']['results'][0]['status']='forged'
        self.assertEqual(restored.policy_assessment['results'][0]['status'],'applicable')
        self.assertEqual(ds.values(),new.values())
        self.assertEqual(old,serialize_dataset(ds))


if __name__ == '__main__':
    unittest.main()
