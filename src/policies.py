"""Evidence-bound policy applicability, separate from the 24 risk rules.

No tax is calculated and no financial metric is overwritten. The reviewed
catalog is local/versioned; request processing never scrapes or calls a model.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from .input_errors import InputError

CATALOG_PATH = Path(__file__).resolve().parents[1] / 'docs' / '企业政策适用目录.yaml'
_PROGRAM_SHA = hashlib.sha256(Path(__file__).read_bytes()).digest()
STATUS_LABELS = {'applicable': '所采条件符合（已复核）', 'not_applicable': '所采条款不适用',
                 'needs_review': '待核对'}
YES_NO = {'yes': '是（有依据）', 'no': '否（有依据）'}


def _field(label, kind='choice', choices=None, help_text=''):
    return {'label': label, 'type': kind, 'choices': (choices or YES_NO) if kind == 'choice' else {},
            'help': help_text}


# Public definitions also drive the upload/review UI. Blank always means unknown.
FIELDS = {
    'business_start': _field('政策事项开始日期（空项沿用主期间）', 'date'),
    'business_end': _field('政策事项结束日期（空项沿用主期间）', 'date'),
    'vat_small': _field('本期增值税小规模身份'),
    'vat_subject': _field('纳税主体', choices={'enterprise': '企业', 'individual_business': '个体工商户', 'natural': '自然人'}),
    'vat_cycle': _field('主管税务机关核定的计税期间', choices={'month': '月', 'quarter': '季度', 'day': '按次（日）'}),
    'vat_natural_month': _field('自然人属于2026年第4号第二条月纳税情形'),
    'vat_sales': _field('所选计税期间全部不含税销售额（人民币元）', 'number'),
    'vat_sales_checked': _field('全部交易/同日合计/差额扣除口径已核对'),
    'vat_waiver': _field('起征点免税放弃范围', choices={'none': '未放弃', 'partial': '部分放弃', 'all': '全部放弃'}),
    'vat_special': _field('本次判断的交易是否开具专用发票'),
    'vat_business': _field('本次交易类型', choices={'ordinary': '普通货物/服务等', 'real_estate': '销售或出租不动产', 'land': '转让土地使用权', 'used': '使用过的固定资产/旧货', 'other': '其他专项或不明确'}),
    'vat_other_exemption': _field('本次交易已享受其他免税'),
    'vat_reduction_waiver': _field('本次交易是否放弃减税'),
    'vat_separate': _field('交易适用口径、分别核算及专项政策已核对'),
    'cit_nonrestricted': _field('属于国家非限制和禁止行业'),
    'cit_income': _field('年度应纳税所得额（人民币元，非收入/会计利润）', 'signed_number'),
    'cit_people': _field('含劳务派遣的季度平均人数', 'number'),
    'cit_assets': _field('季度平均资产总额（人民币元）', 'number'),
    'cit_stats': _field('人数及资产统计口径', choices={'quarter_average': '全年季度平均', 'operating_average': '开业/终止实际经营期季度平均', 'year_end': '期末数（不足以认定）'}),
    'cit_operating_period': _field('年中开业/终止的实际经营期间已核对'),
    'cit_income_checked': _field('年度所得及人员资产统计依据已核对'),
    'six_tax': _field('本次税费', choices={'resource': '资源税（非水资源）', 'city': '城市维护建设税', 'property': '房产税', 'land': '城镇土地使用税', 'stamp': '印花税（非证券交易）', 'cultivated': '耕地占用税', 'education': '教育费附加', 'local_education': '地方教育附加', 'water': '水资源税', 'securities': '证券交易印花税', 'vat': '增值税', 'cit': '企业所得税', 'deed': '契税', 'other': '其他'}),
    'six_eligibility': _field('六税两费资格依据', choices={'small_vat': '增值税小规模', 'small_cit': '汇算确认的小型微利', 'individual': '个体工商户', 'new_general': '首次汇算前新设一般纳税人', 'none': '均不符合'}),
    'six_settlement': _field('相关年度汇算结果确认小型微利资格'),
    'six_individual': _field('个体工商户主体登记已核对'),
    'six_new': _field('确为新设立且登记为一般纳税人'),
    'six_before_settlement': _field('处于首次汇算清缴之前'),
    'six_people': _field('申报期上月末人数（含派遣）', 'number'),
    'six_assets': _field('申报期上月末资产总额（人民币元）', 'number'),
    'six_other_reliefs': _field('其他优惠已逐项核对，且不含未确认减免'),
    'rd_resident': _field('居民企业'),
    'rd_audit': _field('查账征收且会计核算健全'),
    'rd_industry': _field('研发政策行业类别', choices={'eligible': '已核对不属于排除行业', 'tobacco': '烟草制造', 'hospitality': '住宿餐饮', 'trade': '批发零售', 'real_estate': '房地产', 'leasing': '租赁商务服务', 'entertainment': '娱乐', 'other_excluded': '其他规定排除', 'special': '特殊行业更高比例需另核'}),
    'rd_activity': _field('研发活动属于规定范围，非日常维护/常规升级'),
    'rd_accounts': _field('按项目辅助账、研发与经营费用分开核算'),
    'rd_costs': _field('合格费用、费用上限与留存资料已逐项核对'),
    'rd_mode': _field('本次研发费用类型（混合项目须分开）', choices={'expense': '费用化', 'capital': '资本化无形资产', 'overseas_org': '委托境外机构', 'overseas_individual': '委托境外个人', 'domestic_delegate': '境内委托（需配套条款）', 'mixed': '混合或不明确'}),
    'rd_contract': _field('境外技术合同登记、独立交易及关联方明细已核对'),
    'rd_overseas_cost': _field('境外机构委托实际费用（人民币元）', 'number'),
    'rd_domestic_cost': _field('境内符合条件研发费用（人民币元）', 'number'),
    'rd_amortization': _field('资本化摊销期间与口径已核对'),
    'property_location': _field('所涉土地/房屋所在地', choices={'guangdong': '广东省', 'other': '广东以外'}),
    'deed_kind': _field('契税事项', choices={'ordinary': '普通承受', 'cash': '征收征用现金补偿后重新承受', 'exchange': '产权调换后重新承受', 'disaster': '不可抗力住房灭失后重新承受', 'housing': '个人住房优惠', 'exempt': '法定免税或其他专项'}),
    'deed_national': _field('国家计税依据和减免已核对，本次无其他国家专项'),
    'deed_proof': _field('县级以上征收征用决定及补偿资料已核对'),
    'deed_price': _field('重新承受成交价格（人民币元）', 'number'),
    'deed_compensation': _field('现金补偿（人民币元）', 'number'),
    'deed_difference': _field('产权调换需缴纳的差价（人民币元）', 'number'),
    'property_area': _field('在城市/县城/建制镇/工矿区征税范围内'),
    'property_payer': _field('第三条规定的纳税义务人身份已核对'),
    'property_use': _field('房产本次用途', choices={'own': '自用从价', 'rent': '出租从租', 'mixed': '混合用途需拆分', 'special': '免税或特殊出租'}),
    'property_basis': _field('原值或租金税法口径已核对，非固定资产净值'),
    'property_exemptions': _field('法定/专项减免已核对，本次无其他专项'),
}

VAT_FIELDS = ['vat_small', 'vat_subject', 'vat_cycle', 'vat_natural_month', 'vat_sales',
              'vat_sales_checked', 'vat_waiver', 'vat_special']
POLICY_FIELDS = {
    'VAT-THRESHOLD-2026': VAT_FIELDS,
    'VAT-REDUCED-2026': [*VAT_FIELDS, 'vat_business', 'vat_other_exemption', 'vat_reduction_waiver', 'vat_separate'],
    'CIT-SMALL': ['cit_nonrestricted', 'cit_income', 'cit_people', 'cit_assets', 'cit_stats', 'cit_operating_period', 'cit_income_checked'],
    'SMALL-SIX-TWO': ['six_tax', 'six_eligibility', 'vat_small', 'six_settlement', 'six_individual', 'six_new', 'six_before_settlement', 'cit_nonrestricted', 'six_people', 'six_assets', 'six_other_reliefs'],
    'CIT-RD': ['rd_resident', 'rd_audit', 'rd_industry', 'rd_activity', 'rd_accounts', 'rd_costs', 'rd_mode', 'rd_contract', 'rd_overseas_cost', 'rd_domestic_cost', 'rd_amortization'],
    'GD-DEED-BASE': ['property_location', 'deed_kind', 'deed_national', 'deed_proof', 'deed_price', 'deed_compensation', 'deed_difference'],
    'GD-PROPERTY-BASE': ['property_location', 'property_area', 'property_payer', 'property_use', 'property_basis', 'property_exemptions'],
}


def catalog():
    # JSON is a deliberately restricted subset of YAML: no duplicate keys,
    # executable tags or aliases in the reviewed policy catalog.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate policy catalog key')
            result[key] = value
        return result
    result = json.loads(CATALOG_PATH.read_text(encoding='utf-8'), object_pairs_hook=unique)
    if {p['id'] for p in result['policies']} != set(POLICY_FIELDS):
        raise ValueError('policy catalog and evaluator disagree')
    return result


def fingerprint():
    # Bind to code actually loaded by this process, not newly changed source
    # bytes that an old worker has not imported/executed.
    return hashlib.sha256(CATALOG_PATH.read_bytes() + _PROGRAM_SHA).hexdigest()


def program_current():
    return hashlib.sha256(Path(__file__).read_bytes()).digest() == _PROGRAM_SHA


def public_catalog():
    result = catalog()
    return {**result, 'fingerprint': fingerprint(), 'program_current': program_current(), 'fields': deepcopy(FIELDS),
            'policy_fields': deepcopy(POLICY_FIELDS), 'status_labels': STATUS_LABELS}


def normalize(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'selected', 'facts', 'reviews'}:
        raise InputError('政策输入只允许选择、业务事实与复核标识。')
    selected, facts, reviews = value.get('selected', []), value.get('facts', {}), value.get('reviews', {})
    if (not isinstance(selected, list) or len(selected) > len(POLICY_FIELDS)
            or any(not isinstance(v, str) or v not in POLICY_FIELDS for v in selected)
            or len(set(selected)) != len(selected)):
        raise InputError('政策选择无效或重复。')
    allowed = {'business_start', 'business_end'} | {f for p in selected for f in POLICY_FIELDS[p]}
    if not isinstance(facts, dict) or set(facts) - allowed:
        raise InputError('业务事实字段不属于所选政策。')
    normalized = {}
    for key, fact in facts.items():
        if not isinstance(fact, dict) or set(fact) != {'value', 'source'}:
            raise InputError('每项政策事实须分别提供值和依据位置。')
        if any(not isinstance(fact[k], str) for k in ('value', 'source')):
            raise InputError('政策事实及依据须为文本。')
        val, source = fact['value'].strip(), fact['source'].strip()
        if len(val) > 100 or len(source) > 500 or any(ord(c) < 32 for c in val + source):
            raise InputError('政策事实过长或含控制字符。')
        if not val and not source:
            continue
        spec = FIELDS[key]
        if val and spec['type'] == 'choice' and val not in spec['choices']:
            raise InputError(spec['label'] + '：选项无效。')
        if val and spec['type'] in {'number', 'signed_number'}:
            if not re.fullmatch(r'-?\d{1,16}(?:\.\d{1,6})?', val):
                raise InputError(spec['label'] + '：请输入有界十进制数，不使用百分号或科学计数。')
            try:
                number = Decimal(val)
            except InvalidOperation:
                raise InputError(spec['label'] + '：数值无效。') from None
            if spec['type'] == 'number' and number < 0:
                raise InputError(spec['label'] + '：不能为负数。')
        if val and spec['type'] == 'date':
            try:
                if date.fromisoformat(val).isoformat() != val:
                    raise ValueError
            except ValueError:
                raise InputError(spec['label'] + '：使用YYYY-MM-DD日期。') from None
        normalized[key] = {'value': val, 'source': source}
    if (not isinstance(reviews, dict) or set(reviews) - set(selected)
            or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in reviews.values())):
        raise InputError('政策复核标识无效。')
    return {'selected': sorted(selected), 'facts': dict(sorted(normalized.items())), 'reviews': dict(reviews)}


class _Check:
    def __init__(self, facts):
        self.facts = facts
        self.missing = []
        self.failures = []
        self.reasons = []
        self.notes = []
        self.used = []

    def get(self, key):
        if key not in self.used:
            self.used.append(key)
        fact = self.facts.get(key, {})
        if not fact.get('value') or not fact.get('source'):
            if key not in self.missing:
                self.missing.append(key)
            return None
        return fact['value']

    def yes(self, key, message, disqualifies=True):
        val = self.get(key)
        if val == 'no':
            (self.failures if disqualifies else self.reasons).append(message)
        return val == 'yes'

    def number(self, key):
        val = self.get(key)
        return Decimal(val) if val is not None else None

    def at_most(self, key, limit, message):
        val = self.number(key)
        if val is not None and val > Decimal(limit):
            self.failures.append(message)


def _vat(c, start, end):
    c.yes('vat_small', '非增值税小规模纳税人，不适用所采小规模政策。')
    subject, cycle = c.get('vat_subject'), c.get('vat_cycle')
    if subject == 'natural':
        special = c.get('vat_natural_month')
        if special == 'yes' and cycle != 'month' or special == 'no' and cycle != 'day':
            c.reasons.append('自然人计税期间须按2026年第4号第二、三条核对。')
    if start and end and cycle:
        aligned = (cycle == 'day' and start == end or
                   cycle == 'month' and start.day == 1 and start.year == end.year and start.month == end.month and end.day == monthrange(end.year, end.month)[1] or
                   cycle == 'quarter' and start.day == 1 and start.month in {1, 4, 7, 10} and start.year == end.year and end.month == start.month + 2 and end.day == monthrange(end.year, end.month)[1])
        if not aligned:
            c.reasons.append('销售额期间与核定计税期间不一致；年度金额不能当月/季销售额。')
    c.yes('vat_sales_checked', '全部交易、日合计或差额计税口径尚未核对。', False)
    sales, waiver, special = c.number('vat_sales'), c.get('vat_waiver'), c.get('vat_special')
    if waiver == 'partial':
        c.reasons.append('部分放弃免税须拆分已放弃与未放弃交易，不能对全部金额统一判定。')
    if special == 'yes' and waiver == 'none':
        c.reasons.append('专用发票与未放弃免税信息矛盾，须核对交易范围。')
    threshold = {'month': Decimal('100000'), 'quarter': Decimal('300000'), 'day': Decimal('1000')}.get(cycle)
    return sales, threshold, waiver, special


def _evaluate(policy_id, c, start, end):
    if policy_id.startswith('VAT-'):
        sales, threshold, waiver, special = _vat(c, start, end)
        below = sales is not None and threshold is not None and sales < threshold
        if policy_id == 'VAT-THRESHOLD-2026':
            if sales is not None and threshold is not None and not below:
                c.failures.append('销售额达到起征点（含相等），不享受起征点免税；不是仅对超额计税。')
            if waiver == 'all' or special == 'yes':
                c.failures.append('本次交易已放弃起征点免税或开具专用发票。')
            c.notes.append('2026年口径为严格小于起征点；月10万、季30万、日1000元。')
        else:
            business = c.get('vat_business')
            if business in {'real_estate', 'land'}:
                c.failures.append('销售/出租不动产及转让土地使用权不属于所采1%条款。')
            if business in {'used', 'other'}:
                c.reasons.append('固定资产、旧货或其他专项须使用相应条款，不能统一认定1%。')
            if c.get('vat_other_exemption') == 'yes' or below and waiver == 'none' and special == 'no':
                c.failures.append('本次交易已符合免税，不对同一免税金额再采用1%减税。')
            if c.get('vat_reduction_waiver') == 'yes':
                c.failures.append('本次交易已放弃减税。')
            c.yes('vat_separate', '专项口径或分别核算尚未核对。', False)
            c.notes.append('仅判断所采3%减按1%条款，预缴与最终申报不能混用。')
    elif policy_id == 'CIT-SMALL':
        c.yes('cit_nonrestricted', '国家限制或禁止行业不符合小型微利条件。')
        c.at_most('cit_income', '3000000', '年度应纳税所得额超过300万元，不按其中300万元单独享受。')
        c.at_most('cit_people', '300', '季度平均从业人数超过300人。')
        c.at_most('cit_assets', '50000000', '季度平均资产超过5000万元。')
        stats = c.get('cit_stats')
        if stats == 'year_end':
            c.reasons.append('年末数不能代替全年季度平均人数与资产。')
        if stats == 'operating_average':
            c.yes('cit_operating_period', '实际经营期统计口径未核对。', False)
        c.yes('cit_income_checked', '所得、人员及资产统计资料尚未核对。', False)
        if start and end and (start.year != end.year or stats == 'quarter_average' and
                              (start.month, start.day, end.month, end.day) != (1, 1, 12, 31)):
            c.reasons.append('年度资格须对应同一纳税年度；季度预缴不能冒充已确定的全年结果。')
        c.notes.append('25%所得计入比例×20%税率；不是直接把法定税率改为5%。')
    elif policy_id == 'SMALL-SIX-TWO':
        tax, category = c.get('six_tax'), c.get('six_eligibility')
        if tax in {'water', 'securities', 'vat', 'cit', 'deed', 'other'}:
            c.failures.append('本次税费不属于公告列明的六税两费范围。')
        if category == 'none':
            c.failures.append('不属于所列三类优惠主体。')
        elif category == 'small_vat':
            c.yes('vat_small', '小规模资格不成立。')
        elif category == 'small_cit':
            c.yes('six_settlement', '相关年度汇算未确认小型微利资格。', False)
        elif category == 'individual':
            c.yes('six_individual', '个体工商户主体登记不成立。')
        elif category == 'new_general':
            c.yes('six_new', '非新设立的一般纳税人，不适用首次汇算前例外。')
            c.yes('six_before_settlement', '已办理首次汇算，不能继续按新设例外判定。')
            c.yes('cit_nonrestricted', '国家限制或禁止行业不适用新设例外。')
            c.at_most('six_people', '300', '申报期上月末人数超过300人。')
            c.at_most('six_assets', '50000000', '申报期上月末资产超过5000万元。')
        c.yes('six_other_reliefs', '其他减免依据尚未核对，叠加范围需复核。', False)
        c.notes.append('公告第四条允许与合法其他优惠叠加；先其他减免，再对剩余税额减半，比例不直接相加。')
    elif policy_id == 'CIT-RD':
        for key, message in [('rd_resident', '非居民企业。'), ('rd_audit', '不符合查账征收/核算健全条件。'),
                             ('rd_activity', '活动不符合研发范围。'), ('rd_accounts', '未分项目辅助账或研发与经营费用划分不清。')]:
            c.yes(key, message)
        c.yes('rd_costs', '费用范围、上限和备查材料尚未核对。', False)
        industry, mode = c.get('rd_industry'), c.get('rd_mode')
        if industry not in {None, 'eligible', 'special'}:
            c.failures.append('属于所列不适用研发加计扣除的行业。')
        if industry == 'special':
            c.reasons.append('特殊行业更高比例未采齐配套条款，需另核对，不能默认100%。')
        if mode == 'overseas_individual':
            c.failures.append('2018年64号不包括委托境外个人。')
        elif mode in {'mixed', 'domestic_delegate'}:
            c.reasons.append('混合分类须拆分；境内委托需另核配套条件与80%归集，不认定全部金额。')
        elif mode == 'capital':
            c.yes('rd_amortization', '无形资产摊销期间及口径尚未核对。', False)
            c.notes.append('资本化按成本200%税前摊销，不是当期一次扣除200%。')
        elif mode == 'overseas_org':
            c.yes('rd_contract', '境外合同登记、独立交易或关联方资料条件不成立。')
            foreign, domestic = c.number('rd_overseas_cost'), c.number('rd_domestic_cost')
            if foreign is not None and domestic is not None:
                # Compare without rounding 2/3. Exceeding does not disqualify
                # the entire project, but the unsegmented total is not approved.
                if foreign * Decimal('0.8') * 3 > domestic * 2:
                    c.reasons.append('按80%归集的境外委托费用超过境内合格费用的2/3，须拆出可加计范围。')
                if domestic == 0:
                    c.failures.append('境内合格研发费用为零，无境外委托可加计限额。')
            c.notes.append('境外机构按实际费用80%归集，并受境内合格费用2/3上限约束。')
        else:
            c.notes.append('费用化在据实扣除基础上再加计100%，不是退还支出的100%。')
    else:
        if c.get('property_location') == 'other':
            c.failures.append('财产不在广东省，不能套广东地方条款；企业注册地不能代替财产所在地。')
        if policy_id == 'GD-DEED-BASE':
            kind = c.get('deed_kind')
            c.yes('deed_national', '国家计税依据/专项减免仍需核对，不能直接套3%。', False)
            if kind in {'housing', 'exempt', 'disaster'}:
                c.reasons.append('个人住房、法定免税或灾害面积规则需专项核对，不直接批准基本3%。')
            if kind in {'cash', 'exchange'}:
                c.yes('deed_proof', '缺少县级以上征收征用及补偿资料。', False)
            if kind == 'cash':
                price, compensation = c.number('deed_price'), c.number('deed_compensation')
                if price is not None and compensation is not None:
                    c.notes.append('重新承受价格不超过补偿部分免税；本次全部价格在免税范围。' if price <= compensation else
                                   '重新承受价格超过补偿，仅超过部分按规定征税；不能对全价征税。')
            elif kind == 'exchange':
                difference = c.number('deed_difference')
                if difference is not None:
                    c.notes.append('产权调换无缴纳差价免税。' if difference == 0 else '产权调换仅缴纳差价部分按规定征税。')
            else:
                c.notes.append('国家专项减免优先核对；普通事项地方基本税率3%，不生成应纳税款。')
        else:
            for key, message in [('property_area', '不在规定征税区域。'), ('property_payer', '所选主体不是规定纳税义务人。')]:
                c.yes(key, message)
            use = c.get('property_use')
            if use in {'mixed', 'special'}:
                c.reasons.append('混合用途须分事项/期间；免税或特殊出租不能统一套基本口径。')
            c.yes('property_basis', '房产原值/租金税法口径尚未核对。', False)
            c.yes('property_exemptions', '法定/专项减免尚未核对。', False)
            c.notes.append('从价：原值一次减除30%后年税率1.2%。' if use == 'own' else
                           '从租：基本税率12%，不替代特殊出租优惠。')


def _consistency_issues(scope, inputs):
    """Compare explicit claims for the same subject/period, never infer identity.

    Qualification-specific hidden facts belong only to their active route.
    Missing sources cannot turn an explicit contradiction into a valid claim.
    These issues are unresolved facts, not legal policy incompatibility rulings.
    """
    ids = set(inputs['selected'])
    vat_ids = ids & {'VAT-THRESHOLD-2026', 'VAT-REDUCED-2026'}
    six = 'SMALL-SIX-TWO' in ids
    value = lambda key: inputs['facts'].get(key, {}).get('value')
    category = value('six_eligibility') if six else None
    declared = scope['taxpayer_type']
    declared_small = declared in {'小规模', '小规模纳税人', '增值税小规模纳税人'}
    declared_general = declared in {'一般纳税人', '增值税一般纳税人'}
    issues = []
    affected = sorted(vat_ids | ({'SMALL-SIX-TWO'} if six else set()))
    if (value('vat_small') == 'yes' and declared_general
            or value('vat_small') == 'no' and declared_small
            or category == 'small_vat' and declared_general):
        issues.append({'code': 'declared_vat_identity_conflict', 'policy_ids': affected,
            'field_ids': ['taxpayer_type', 'vat_small', *(['six_eligibility'] if six else [])],
            'message': '本期企业纳税人身份与所填小规模事实/资格依据矛盾；须核对身份及生效时间，不能用勾选复核消除。'})
    if category == 'new_general' and value('six_new') == 'yes' and (
            value('vat_small') == 'yes' or declared_small):
        issues.append({'code': 'new_general_small_vat_conflict', 'policy_ids': affected,
            'field_ids': ['taxpayer_type', 'vat_small', 'six_eligibility', 'six_new'],
            'message': '同一主体同一事项期间同时声明小规模身份与新设一般纳税人资格，事实冲突；须核对或拆分身份变更期间，相关政策不能直接复核放行。'})
    if vat_ids and category == 'individual' and value('six_individual') == 'yes' and (
            value('vat_subject') in {'enterprise', 'natural'}):
        issues.append({'code': 'subject_identity_conflict', 'policy_ids': affected,
            'field_ids': ['vat_subject', 'six_eligibility', 'six_individual'],
            'message': '同一主体同时填为企业/自然人和已登记个体工商户，主体类型矛盾；须对照登记资料核对，不能自动选择有利身份。'})
    return issues


def assess(company, value):
    """Return a server-generated, frozen-able assessment, or None for legacy use."""
    inputs = normalize(value)
    if not inputs['selected']:
        return None
    if not program_current():
        raise InputError('政策判断程序源码已变化，请重启服务后重新分析；旧报告保留。')
    before = fingerprint()
    data = catalog()
    digest = fingerprint()
    if before != digest:
        raise InputError('读取期间政策目录发生变化，请重新分析。')
    scope = {k: str(company.get(k, '') or '') for k in (
        'name', 'taxpayer_id', 'industry', 'region', 'taxpayer_type', 'business_scope', 'period_start', 'period_end')}
    issues = _consistency_issues(scope, inputs)
    results = []
    for policy in data['policies']:
        if policy['id'] not in inputs['selected']:
            continue
        c = _Check(inputs['facts'])
        start = end = None
        for key in ('business_start', 'business_end'):
            if key in inputs['facts']:
                c.get(key)
        try:
            start = date.fromisoformat(inputs['facts'].get('business_start', {}).get('value') or scope['period_start'])
            end = date.fromisoformat(inputs['facts'].get('business_end', {}).get('value') or scope['period_end'])
            if start > end:
                raise ValueError
            parent_start, parent_end = date.fromisoformat(scope['period_start']), date.fromisoformat(scope['period_end'])
            if start < parent_start or end > parent_end:
                c.reasons.append('政策事项期间不在本次企业主期间内。')
            lower = date.fromisoformat(policy['effective_from'])
            upper = date.fromisoformat(policy['effective_to']) if policy['effective_to'] else date.max
            if end < lower or start > upper:
                c.failures.append('事项期间在所采政策版本生效区间之外，不能沿用该版本。')
            elif start < lower or end > upper:
                c.reasons.append('事项跨越政策生效边界，须拆分期间或另核政策版本。')
        except (ValueError, TypeError):
            c.reasons.append('企业主期间或政策事项日期不完整/无效，不能判断生效版本。')
        if not scope['name'] or not scope['taxpayer_id']:
            c.reasons.append('企业名称或税号缺失，业务事实尚未绑定明确主体。')
        _evaluate(policy['id'], c, start, end)
        for issue in issues:
            if policy['id'] in issue['policy_ids']:
                c.reasons.append(issue['message'])
                for key in issue['field_ids']:
                    if key in FIELDS and key in inputs['facts']:
                        c.get(key)
        candidate = 'needs_review' if c.missing or c.reasons else 'not_applicable' if c.failures else 'applicable'
        token_payload = {'id': policy['id'], 'scope': scope, 'selected': inputs['selected'],
                         'facts': inputs['facts'], 'fingerprint': digest}
        token = hashlib.sha256(json.dumps(token_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        reviewed = candidate != 'needs_review' and inputs['reviews'].get(policy['id']) == token
        status = candidate if candidate != 'applicable' or reviewed else 'needs_review'
        results.append({**deepcopy(policy), 'candidate_status': candidate, 'status': status,
            'status_label': STATUS_LABELS[status], 'reviewed': reviewed,
            'review_token': token if candidate != 'needs_review' else None,
            'missing_fields': [{'id': key, 'label': FIELDS[key]['label']} for key in c.missing],
            'reasons': c.reasons + c.failures + (['所采条件已具备；请核对业务事实及来源后明确复核。'] if candidate == 'applicable' and not reviewed else []),
            'notes': c.notes, 'facts': [{'id': key, 'label': FIELDS[key]['label'], **inputs['facts'][key],
                'value_label': FIELDS[key]['choices'].get(inputs['facts'][key]['value'], inputs['facts'][key]['value'])}
                for key in c.used if key in inputs['facts']],
            'business_start': start.isoformat() if start else None, 'business_end': end.isoformat() if end else None})
    ids = set(inputs['selected'])
    relationships = []
    if ids & {'VAT-THRESHOLD-2026', 'VAT-REDUCED-2026'}:
        relationships.append({'kind': 'precedence', 'policy_ids': sorted(ids & {'VAT-THRESHOLD-2026', 'VAT-REDUCED-2026'}),
            'source_id': 'vat-law', 'clause': '增值税法第二十三条；2026年第4号第一、四条',
            'message': '先核对免税，再核对应税交易减税；同一免税金额不再乘1%，部分放弃优惠须拆分。'})
    if 'SMALL-SIX-TWO' in ids:
        relationships.append({'kind': 'stack', 'policy_ids': ['SMALL-SIX-TWO'], 'source_id': 'small-2023', 'clause': '第四条',
            'message': '已依法享受所列税费其他优惠的，可叠加减半；先其他减免再对剩余税额减半，不直接相加百分比。'})
    if 'GD-PROPERTY-BASE' in ids:
        relationships.append({'kind': 'exclusive', 'policy_ids': ['GD-PROPERTY-BASE'], 'source_id': 'gd-property', 'clause': '第四、五条',
            'message': '同一房产同一用途期间按从价或从租口径判断；混合用途需拆分，不能对全额同时套两套基本口径。'})
    if 'GD-DEED-BASE' in ids:
        relationships.append({'kind': 'precedence', 'policy_ids': ['GD-DEED-BASE'], 'source_id': 'gd-deed', 'clause': '决定第一项',
            'message': '地方基本税率须服从国家规定的免征/减征；不按文号新旧、地区或用户选择直接排序。'})
    if 'CIT-RD' in ids:
        relationships.append({'kind': 'exclusive', 'policy_ids': ['CIT-RD'], 'source_id': 'rd-2023', 'clause': '第一条',
            'message': '同一费用不得同时归为费用化加计与资本化摊销；须按实际分类分别核对。'})
    if 'CIT-RD' in ids and 'CIT-SMALL' in ids:
        relationships.append({'kind': 'needs_review', 'policy_ids': ['CIT-RD', 'CIT-SMALL'], 'source_id': None, 'clause': None,
            'message': '本目录未采齐两项组合的所得口径及办理依据，不自动形成合并计算或最优优惠结论；须独立核对。'})
    used_sources = {s for result in results for s in result['source_ids']}
    pending = any(r['status'] == 'needs_review' for r in results) or any(
        r['kind'] == 'needs_review' for r in relationships)
    consistency_status = 'conflict' if issues else 'needs_review' if pending else 'checked'
    consistency = {'status': consistency_status, 'issues': issues,
        'status_label': {'conflict': '存在业务事实冲突，相关政策待核对',
            'needs_review': '仍有待核对事项，不能形成组合适用结论',
            'checked': '所采一致性检查通过（不代表组合适用或自动算税）'}[consistency_status],
        'combination_conclusion_enabled': False}
    return {'version': data['version'], 'fingerprint': digest, 'checked_as_of': data['checked_as_of'],
            'scope': scope, 'results': results, 'relationships': relationships, 'consistency': consistency,
            'sources': [deepcopy(s) for s in data['sources'] if s['id'] in used_sources],
            'limitations': data['limitations'], 'automatic_calculation_enabled': False}


def assert_current(assessment):
    if assessment is not None and (not program_current() or assessment.get('fingerprint') != fingerprint()):
        raise InputError('政策目录或判断程序已更新，请重新保存分析；既有冻结报告不改写。')
