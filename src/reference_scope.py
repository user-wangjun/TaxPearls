"""Explicit benchmark scope, not a claim that a supplied source is authoritative."""
from datetime import date
import re

from .input_errors import InputError
from .periods import parse_period

SPECS = {
    '税负率': ('参考.税负率下限', '参考.税负率上限',
              '应纳增值税/不含税销售额', ('region', 'industry', 'taxpayer_type')),
    '毛利率': ('参考.毛利率下限', '参考.毛利率上限',
              '（营业收入-营业成本）/营业收入', ('region', 'industry', 'business_scope')),
}
FIELDS = ('region', 'industry', 'taxpayer_type', 'business_scope', 'start', 'end',
          'definition', 'publisher', 'version', 'source', 'nature')
LABELS = {'region': '地区', 'industry': '行业', 'taxpayer_type': '纳税人身份', 'business_scope': '业务构成'}


def normalize(raw):
    if not isinstance(raw, dict) or set(raw) not in (set(FIELDS), set(FIELDS) | {'metric_source'}):
        raise InputError('参考值适用范围缺失或字段不完整；请上传含“参考值适用范围”的来源工作簿。')
    if any(not isinstance(v, str) or len(v) > 2000 for v in raw.values()):
        raise InputError('参考值适用范围须为有界文本。')
    data = {k: v.strip() for k, v in raw.items()}
    if any(not data[k] for k in ('publisher', 'version', 'source', 'definition')):
        raise InputError('参考值必须注明发布者、版本、来源与指标口径。')
    if data['nature'] not in {'来源参考', '仿真示意'}:
        raise InputError('参考值性质须为“来源参考”或“仿真示意”；用户来源不等于已获权威核定。')
    try:
        if any(not re.fullmatch(r'\d{4}-\d{2}-\d{2}', data[k]) for k in ('start', 'end')):
            raise ValueError
        if date.fromisoformat(data['start']) > date.fromisoformat(data['end']):
            raise ValueError
    except ValueError:
        raise InputError('参考值须有明确且顺序正确的适用起止日（YYYY-MM-DD）。') from None
    return data


def validate(dataset, name, rule):
    spec = next((s for s in SPECS.values() if name in s[:2]), None)
    if not spec:
        return
    lower, upper, definition, dimensions = spec
    expected = ({'numerator': '增值税.应纳税额', 'denominator': '增值税.销售额'}
                if lower == '参考.税负率下限' else {
                    'numerator': {'sub': ['利润表.营业收入', '利润表.营业成本']}, 'denominator': '利润表.营业收入'})
    expected.update(type='ratio_range', min=lower, max=upper)
    if rule.logic != expected:
        raise InputError('规则实际计算口径与参考指标定义不一致；不能改变公式后继续套用原参考区间。')
    metric = dataset.metrics[name]
    data = normalize(metric.reference)
    if not data.get('metric_source') or data['metric_source'] != data['source']:
        raise InputError('参考范围声明的来源与上下限数值的实际来源不一致或未绑定；不能拼接不同基准。')
    other = dataset.metrics.get(upper if name == lower else lower)
    if other and normalize(other.reference) != data:
        raise InputError('参考区间上下限必须来自同一来源、版本及适用范围。')
    if data['nature'] == '仿真示意' and not dataset.simulation:
        raise InputError('仿真示意参考值不能用于企业正式检测；请补充匹配的来源参考。')
    if data['definition'] != definition:
        raise InputError(f'参考指标口径不匹配，应为“{definition}”；不能将另一种比率套入本规则。')
    period = parse_period(dataset.company.period, '参考值核对期间')
    if data['start'] > period.start.isoformat() or data['end'] < period.end.isoformat():
        raise InputError('参考值适用期未覆盖整个主检测期间，不能仅按年度名称匹配。')
    for key in dimensions:
        actual = getattr(dataset.company, key)
        if not actual or not data[key] or actual.strip() != data[key]:
            raise InputError(f'参考值{LABELS[key]}未明确或与企业不匹配；不从名称或说明猜测。')


def description(raw):
    data = normalize(raw)
    return (f"性质：{data['nature']}；发布者：{data['publisher']}；版本：{data['version']}；"
            f"来源：{data['source']}；适用期：{data['start']}至{data['end']}；"
            + '；'.join(f'{LABELS[k]}：{data[k] or "未提供"}' for k in LABELS)
            + f"；指标口径：{data['definition']}；来源由输入提供，未自动核定权威性。")


def synthetic(company):
    """Only generators/tests call this; never synthesize metadata for user inputs."""
    period = parse_period(company.period, '仿真期间')
    result = {kind: dict(zip(FIELDS, (company.region, company.industry, company.taxpayer_type,
            company.business_scope, period.start.isoformat(), period.end.isoformat(), spec[2],
            'TaxPearls 仿真生成器', '仿真示意-v1', '纯仿真底稿，不是真实行业标准', '仿真示意')))
            for kind, spec in SPECS.items()}
    for data in result.values():
        data['metric_source'] = data['source']
    return result
