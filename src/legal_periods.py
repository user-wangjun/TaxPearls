"""Period-bound official background citations; never legal liability decisions.

Only the reviewed clauses below are catalogued. Unknown earlier versions stay
unavailable; publication dates are not used as dates of commencement. Resolved
citations and their original definition are frozen with each audit rule.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta

from .input_errors import InputError
from .periods import parse_period

VAT = 'https://fgk.chinatax.gov.cn/zcfgk/c100009/c5237365/content.html'
VAT_OLD = 'https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/201712/t20171204_2764533.htm'
ACCOUNT = 'https://kjs.mof.gov.cn/zt/kjfxcgc/kjfqw/202408/t20240814_3941788.htm'
ACCOUNT_OLD = 'https://kjs.mof.gov.cn/zhengcefabu/201711/t20171129_2762381.htm'
TAX = 'https://12366.chinatax.gov.cn/bzds/120/120-1-4.html'
PIT = 'https://www.chinatax.gov.cn/n810219/n810744/n3752930/n3752974/c3970366/content.html'
SOCIAL = 'https://www.npc.gov.cn/npc/c2/c10134/201905/t20190522_175815.html'
SOCIAL_CURRENT = 'https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/bgt/art/2023/art_e81d115419b4463ebb59ec46467fb136.html'

# Inclusive intervals of the reviewed text editions, not configurable thresholds.
EDITIONS = {
    VAT: [('2017-11-19', '2025-12-31', VAT_OLD,
           '《中华人民共和国增值税暂行条例》（2017年修订）第四条、第五条、第八条、第十九条：一般计税、进项和纳税义务时间；仅为核对背景，不等于违法认定。'),
          ('2026-01-01', None, VAT,
           '《中华人民共和国增值税法》第十四条、第十六条、第二十一条、第二十八条：一般计税、进项抵扣和纳税义务时间。')],
    ACCOUNT: [('2017-11-05', '2024-06-30', ACCOUNT_OLD,
               '《中华人民共和国会计法》（2017年）第九条、第二十条：核算及报表编制义务，不规定本规则阈值。'),
              ('2024-07-01', None, ACCOUNT,
               '《中华人民共和国会计法》（2024年）第九条、第二十条：核算及报表编制义务，不规定本规则阈值。')],
    TAX: [('2015-04-24', None, TAX,
           '《中华人民共和国税收征收管理法》（2015年修正）第二十五条：如实申报义务；差异本身不能认定少缴税。')],
    PIT: [('2019-01-01', None, PIT,
           '《中华人民共和国个人所得税法》（2018年修正）第九条：扣缴义务；人数或工资差异仅供核对。')],
    SOCIAL: [('2011-07-01', '2018-12-28', SOCIAL,
              '《中华人民共和国社会保险法》（2010年）第五十八条：参保登记义务，不要求个税与社保人数天然相等。'),
             ('2018-12-29', None, SOCIAL_CURRENT,
              '《中华人民共和国社会保险法》（2018年修正）第五十八条：参保登记义务，不要求个税与社保人数天然相等。')],
}


def resolve(rule, period, *, strict=True):
    """Retain every applicable segment; never select a new edition by end date."""
    if rule.legal_context and rule.legal_context.get('period') == period:
        if strict and rule.legal_context.get('error'):
            raise InputError(rule.legal_context['error'])
        return rule
    origin = deepcopy(rule.legal_origin) if rule.legal_origin else {
        'legal_basis': list(rule.legal_basis), 'references': list(rule.references)}
    if not any(url in EDITIONS for url in origin['references']):
        return rule
    try:
        interval = parse_period(period, '法规适用期间')
    except InputError as exc:
        if strict:
            raise
        return replace(rule, legal_basis=[], references=[], legal_origin=origin,
                       legal_context={'period': period, 'error': str(exc)})
    bases, references = [], []
    for url in origin['references']:
        if url not in EDITIONS:
            references.append(url)
            continue
        cursor = interval.start
        for first, last, source, text in EDITIONS[url]:
            start = max(interval.start, date.fromisoformat(first))
            end = min(interval.end, date.fromisoformat(last) if last else date.max)
            if start > end:
                continue
            if start > cursor:
                break
            bases.append(f'{text}【本次适用段：{start.isoformat()} 至 {end.isoformat()}】')
            references.append(source)
            cursor = end + timedelta(days=1) if end < date.max else date.max
        if cursor <= interval.end:
            error = (f'规则 {rule.id} 尚无覆盖 {interval.start} 至 {interval.end} 的已核对法规版本；'
                     '本项不能用新法回溯旧期，请补齐依据后重新分析。')
            if strict:
                raise InputError(error)
            return replace(rule, legal_basis=[], references=[], legal_origin=origin,
                           legal_context={'period': period, 'error': error})
    # Preserve bespoke notes that are not one of the replaced background acts.
    names = ('《中华人民共和国增值税法》', '《中华人民共和国会计法》',
             '《中华人民共和国税收征收管理法》', '《中华人民共和国个人所得税法》',
             '《中华人民共和国社会保险法》')
    bases.extend(text for text in origin['legal_basis'] if not text.startswith(names))
    return replace(rule, legal_basis=bases, references=list(dict.fromkeys(references)), legal_origin=origin,
                   legal_context={'period': period, 'error': ''})
