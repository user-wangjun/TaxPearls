"""The policy collection is evidence, not an activated tax calculator."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts.verify_guangdong_policies import (
    CATALOGS, extract_html, extract_pdf, load_collection, official_url,
    parse_catalog, snapshot, validate, validate_catalog,
)

ROOT = Path(__file__).resolve().parents[1]


class GuangdongPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / 'docs/广东省官方政策数据.json').read_text(encoding='utf-8'))
        cls.catalogs = [parse_catalog(path.read_text(encoding='utf-8')) for path in CATALOGS]

    def test_yaml_split_is_lossless_against_original_collection(self):
        merged = load_collection(CATALOGS)
        policies = deepcopy(merged['policies'])
        sources = deepcopy(merged['sources'])
        for policy in policies:
            policy.pop('policy_level')
            policy.pop('applicable_regions')
            self.assertTrue(all(isinstance(p['value'], str) for p in policy['parameters']))
            self.assertIsInstance(policy['effective_from'], str)
            if policy['effective_to'] is not None:
                self.assertIsInstance(policy['effective_to'], str)
        for source in sources:
            source.pop('issuing_level')
        self.assertEqual(policies, self.data['policies'])
        self.assertEqual(sources, self.data['sources'])
        for key in ('checked_as_of', 'limitations', 'review_status', 'runtime_integration', 'automatic_calculation_enabled'):
            self.assertEqual(merged[key], self.data[key])

    def test_national_not_provincial_and_sources_do_not_overlap(self):
        local, national = self.catalogs
        self.assertEqual((local['policy_level'], local['applicable_regions']), ('provincial', ['广东省']))
        self.assertEqual((national['policy_level'], national['applicable_regions']), ('national', ['全国']))
        self.assertEqual((len(local['policies']), len(local['sources'])), (2, 3))
        self.assertEqual((len(national['policies']), len(national['sources'])), (5, 9))
        self.assertEqual({s['id'] for s in local['sources']} & {s['id'] for s in national['sources']}, set())

    def test_file_entry_and_source_level_mismatch_rejected(self):
        for which in ('file', 'policy', 'source', 'region'):
            data = deepcopy(self.catalogs[0])
            if which == 'file':
                data['policy_level'] = 'national'
                data['applicable_regions'] = ['全国']
            elif which == 'policy':
                data['policies'][0]['policy_level'] = 'national'
            elif which == 'source':
                data['sources'][0]['issuing_level'] = 'national'
            else:
                data['policies'][0]['applicable_regions'] = ['全国']
            with self.subTest(which=which), self.assertRaises(ValueError):
                validate_catalog(data)

    def test_cross_catalog_source_or_reference_rejected(self):
        for which in ('source', 'reference'):
            data = deepcopy(self.catalogs[0])
            if which == 'source':
                data['sources'].append(deepcopy(self.catalogs[1]['sources'][0]))
            else:
                data['policies'][0]['source_ids'].append('vat-law')
            with self.subTest(which=which), self.assertRaises(ValueError):
                validate_catalog(data)

    def test_duplicate_yaml_fields_anchors_and_merge_keys_rejected(self):
        for text in ('policy_level: provincial\npolicy_level: national\n',
                     'a: &ref {policy_level: national}\nb: *ref\n',
                     'a: &ref {policy_level: national}\n<<: *ref\n',
                     'policy_level: provincial\npolicies:\n- id: A\n  id: B\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_catalog(text)

    def test_unsafe_yaml_tag_cannot_execute(self):
        from yaml.constructor import ConstructorError
        with self.assertRaises(ConstructorError):
            parse_catalog('!!python/object/apply:builtins.print [not-executed]')

    def test_yaml_snapshot_required_and_parameters_not_float(self):
        for which in ('snapshot', 'float'):
            data = deepcopy(self.catalogs[0])
            if which == 'snapshot':
                data['sources'][0].pop('snapshot')
            else:
                data['policies'][0]['parameters'][0]['value'] = 0.03
            with self.subTest(which=which), self.assertRaises(ValueError):
                validate_catalog(data)

    def test_original_json_remains_readable_and_duplicate_catalog_rejected(self):
        self.assertEqual(load_collection([ROOT / 'docs/广东省官方政策数据.json']), self.data)
        with self.assertRaisesRegex(ValueError, '来源编号重复'):
            load_collection([CATALOGS[0], CATALOGS[0]])

    def test_policy_data_cannot_load_as_original_risk_rules(self):
        from src.engine import RuleError, load_rules
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(prefix='taxpearls-policy-separation-') as directory:
            for path in CATALOGS:
                target = Path(directory) / path.name
                target.write_bytes(path.read_bytes())
            with self.assertRaisesRegex(RuleError, '缺少字段'):
                load_rules(directory)

    def test_saved_sources_and_parameters_are_traceable(self):
        validate(self.data)
        self.assertEqual(len(self.data['sources']), 12)
        self.assertEqual(len(self.data['policies']), 7)
        self.assertTrue(all(s.get('snapshot') for s in self.data['sources']))
        self.assertEqual(sum(p['region_scope'] == '广东省地方规定' for p in self.data['policies']), 2)

    def test_official_hosts_only(self):
        for url in ('http://www.gd.gov.cn/a', 'https://www.gd.gov.cn.evil.invalid/a',
                    'https://evil.invalid/?next=https://www.gd.gov.cn',
                    'https://user:password@www.gd.gov.cn/a', 'https://www.gd.gov.cn:443/a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                official_url(url)

    def test_article_only_ignores_script_and_empty_trailing_wrapper(self):
        article = '正式条款' * 30
        raw = (f'<nav>不是条款</nav><div class="article-content"><p>{article}</p>'
               '<script>不能作为证据</script><style>不能作为样式证据</style></div>'
               '<div class="article-content"> \n </div>').encode()
        self.assertEqual(extract_html(raw, 'article-content'), article)

    def test_missing_duplicate_incomplete_or_short_article_rejected(self):
        body = '<div class="article-content">' + '条款' * 30 + '</div>'
        for raw in ('<html>登录页面</html>', body + body, body + '<div class="article-content">另一个正文</div>',
                    body[:-6], '<div class="article-content">短文</div>'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                extract_html(raw.encode(), 'article-content')

    def test_false_pdf_rejected(self):
        with self.assertRaises(ValueError):
            extract_pdf(b'<html>not a PDF</html>', [1])

    def test_required_clause_cannot_come_from_navigation(self):
        source = {'id': 'test', 'format': 'html', 'container': 'article', 'required_text': ['税率3%']}
        raw = ('<nav>税率3%</nav><div class="article">' + '无关正文' * 30 + '</div>').encode()
        with self.assertRaisesRegex(ValueError, '必要条款未匹配'):
            snapshot(source, raw, '2026-10-05')

    def test_source_and_pdf_page_tampering_rejected(self):
        for change in ('text', 'page', 'date', 'hash'):
            data = deepcopy(self.data)
            saved = data['sources'][0]['snapshot']
            if change == 'text':
                saved['text'] += '伪造'
            elif change == 'page':
                saved['pages'][0]['page'] = 50
            elif change == 'date':
                saved['retrieved_on'] = '2026-10-04'
            else:
                saved['response_sha256'] = 'invalid'
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(data)

    def test_parameter_missing_or_false_evidence_rejected(self):
        for field, value in (('source_id', 'not-collected'), ('clause', ''),
                             ('evidence_text', ''), ('evidence_text', '不存在的政策税率')):
            data = deepcopy(self.data)
            data['policies'][0]['parameters'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate(data)

    def test_duplicate_sources_and_policies_rejected(self):
        for field in ('sources', 'policies'):
            data = deepcopy(self.data)
            data[field].append(deepcopy(data[field][0]))
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(data)

    def test_not_activated_or_region_guessed(self):
        for flag in ('runtime_integration', 'automatic_calculation_enabled'):
            data = deepcopy(self.data)
            data[flag] = True
            with self.assertRaises(ValueError):
                validate(data)
        data = deepcopy(self.data)
        data['policies'][0]['region_scope'] = '全国通用'
        with self.assertRaises(ValueError):
            validate(data)

    def test_2026_vat_boundary_and_exceptions_preserved(self):
        policies = {p['id']: p for p in self.data['policies']}
        threshold = policies['VAT-THRESHOLD-2026']
        self.assertEqual([p['value'] for p in threshold['parameters']], ['100000', '300000', '1000'])
        self.assertEqual({p['operator'] for p in threshold['parameters']}, {'lt'})
        self.assertEqual((threshold['effective_from'], threshold['effective_to']), ('2026-01-01', '2027-12-31'))
        self.assertIn('全额计税', ''.join(threshold['exclusions']))
        self.assertIn('销售/出租不动产', ''.join(policies['VAT-REDUCED-2026']['exclusions']))

    def test_cit_averages_and_qualification_are_not_year_end_or_revenue(self):
        policy = next(p for p in self.data['policies'] if p['id'] == 'CIT-SMALL')
        self.assertEqual([p['value'] for p in policy['parameters']], ['0.25', '0.20', '3000000', '300', '50000000'])
        self.assertEqual({p['operator'] for p in policy['parameters'] if 'operator' in p}, {'lte'})
        self.assertIn('全年季度平均', ''.join(policy['conditions']))
        self.assertIn('实际经营期', ''.join(policy['calculation_notes']))
        self.assertIn('不等于年度应纳税所得额', ''.join(policy['exclusions']))

    def test_rd_old_ratios_and_six_taxes_exclusions_preserved(self):
        policies = {p['id']: p for p in self.data['policies']}
        rd = policies['CIT-RD']
        self.assertEqual([p['value'] for p in rd['parameters']], ['1.00', '2.00', '0.80', '2/3'])
        self.assertIn('旧50%/150%', ''.join(rd['exclusions']))
        self.assertIsNone(rd['effective_to'])
        exclusions = ''.join(policies['SMALL-SIX-TWO']['exclusions'])
        for term in ('水资源税', '证券交易印花税', '契税', '不能把多项减免比例直接相加'):
            self.assertIn(term, exclusions)


if __name__ == '__main__':
    unittest.main()
