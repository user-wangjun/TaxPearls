"""One-off/manual collection of public Guangdong industrial statistics.

Prints JSON to stdout. Never populates audit thresholds, edits a database,
executes website scripts, or starts a recurring collection job.
"""
from __future__ import annotations

import argparse
from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.request import urlopen

SOURCES = (
    ('2024年1-12月', 'https://stats.gd.gov.cn/zyjjzb/content/post_4664160.html'),
    ('2025年1-12月', 'https://stats.gd.gov.cn/zyjjzb/content/post_4860577.html'),
    ('2026年1-6月', 'https://stats.gd.gov.cn/zyjjzb/content/post_4937566.html'),
)
AGGREGATES = {'全省总计', '采矿业', '制造业', '电力、热力、燃气及水生产和供应业'}
TAX_URL = ('https://guangdong.chinatax.gov.cn/siteapps/webpage/gdtax/tjjcx/'
           'tjjcx_result.jsp?cid=3f7636f792034d57a649420804d1fcb9')


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.tables = {}, []
        self.table = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') in {'ArticleTitle', 'PubDate'}:
            if attrs['name'] in self.meta:
                raise ValueError('发布元数据重复，不能猜测版本。')
            self.meta[attrs['name']] = attrs.get('content', '')
        if tag == 'table':
            if self.table is not None:
                raise ValueError('嵌套表格尚未支持。')
            self.table = []
        elif tag == 'tr' and self.table is not None:
            self.row = []
        elif tag in {'td', 'th'} and self.row is not None:
            self.cell = []
            self.span = any(attrs.get(key, '1') != '1' for key in ('colspan', 'rowspan'))

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {'td', 'th'} and self.cell is not None:
            self.row.append((re.sub(r'\s+', '', ''.join(self.cell)), self.span))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def number(value):
    if not value:
        return None
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
        raise ValueError(f'未知数值格式「{value}」，不改成零。')
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError('数值无法解析。') from None


def parse_statistics(raw, period, url, retrieved_on):
    parsed = Tables()
    parsed.feed(raw.decode('utf-8'))
    expected = period + '分行业规模以上工业企业主要经济指标'
    if parsed.meta.get('ArticleTitle') != expected:
        raise ValueError('原文标题与所选期间不一致。')
    published = date.fromisoformat(parsed.meta['PubDate'][:10])
    if published > date.fromisoformat(retrieved_on):
        raise ValueError('发布日期晚于采集日。')
    match = re.fullmatch(r'(\d{4})年1-(6|12)月', period)
    if not match:
        raise ValueError('本次仅采集明确的全年或上半年版本。')
    year, month = map(int, match.groups())
    tables = [table for table in parsed.tables if table and [x[0] for x in table[0]]
              == ['主要行业', '营业收入', '营业成本']]
    if len(tables) != 1:
        raise ValueError('营业收入/营业成本表缺失或重复。')
    table = tables[0]
    header = ['本月止累计（亿元）', '累计比上年同期增长（%）'] * 2
    if len(table) < 3 or [x[0] for x in table[1]] != header:
        raise ValueError('金额单位或累计/同比栏次不匹配。')
    rows, seen = [], set()
    for row_index, row in enumerate(table[2:], 3):
        if len(row) != 5 or any(span for _, span in row):
            raise ValueError('数据行栏次或合并单元格改变，不能错列读取。')
        name, income, income_growth, cost, cost_growth = [cell[0] for cell in row]
        if not name or name in seen:
            raise ValueError('行业名缺失或重复。')
        seen.add(name)
        revenue, expense = number(income), number(cost)
        number(income_growth)
        number(cost_growth)
        if any(value is not None and value < 0 for value in (revenue, expense)):
            raise ValueError('负营业收入或营业成本须单独核对。')
        ratio = None
        if revenue is not None and expense is not None and revenue > 0:
            with localcontext() as context:
                context.prec = 40
                ratio = str(((revenue - expense) / revenue).quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP))
        rows.append({'industry': name, 'aggregate': name in AGGREGATES,
            'revenue_100m_cny': income or None, 'cost_100m_cny': cost or None,
            'revenue_yoy_percent': income_growth or None, 'cost_yoy_percent': cost_growth or None,
            'derived_gross_margin_ratio': ratio, 'source_table': '主要经济指标（三）',
            'source_row': row_index, 'source_columns': {'revenue': 2, 'cost': 4}})
    if not AGGREGATES <= seen:
        raise ValueError('预期汇总分组缺失，不完整数据不冒充完整采集。')
    return {'publisher': '广东省统计局', 'url': url, 'title': expected,
        'published_on': published.isoformat(), 'retrieved_on': retrieved_on,
        'response_sha256': sha256(raw).hexdigest(), 'region': '广东省',
        'period_start': f'{year}-01-01', 'period_end': f'{year}-{month:02}-{monthrange(year, month)[1]}',
        'population': '规模以上工业企业；不是全部企业，不涵盖批发零售、服务业或规模以下工业企业',
        'rows': rows}


def fetch(url):
    with urlopen(url, timeout=30) as response:
        if not response.geturl().startswith(('https://stats.gd.gov.cn/', 'https://guangdong.chinatax.gov.cn/')):
            raise ValueError('重定向到未知来源。')
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('来源超出本次读取上限。')
    return raw


def collect(retrieved_on):
    editions = [parse_statistics(fetch(url), period, url, retrieved_on) for period, url in SOURCES]
    tax = fetch(TAX_URL)
    text = tax.decode('utf-8')
    if not all(value in text for value in ('行业税负率根据本行业全部纳税人的平均税负生成，并无预设参考值',
                                           '2021-07-16', '国家税务总局东莞市税务局')):
        raise ValueError('税负咨询来源内容变化，须重新核对。')
    return {'schema_version': 1, 'purpose': '广东省公开统计参考数据；人工查阅，不作为审计预警阈值',
        'runtime_integration': False, 'rule_bounds': [],
        'limitations': [
            '派生毛利率=(汇总营业收入-汇总营业成本)/汇总营业收入，小数比率保留8位；不是企业毛利率的算术平均，也不是官方发布的毛利率区间。',
            '四个汇总分组与明细行业交叉，不把全部行相加；原始空白保留null，原文明示0.00保留零，零收入不计算比率。',
            '数据期间不等于未来适用期；2024、2025全年和2026上半年分别保存，不混算或外推到其他期间。',
            '没有企业分布、分位数和上下限，没有纳税人身份细分及企业业务构成匹配，不能填入R-003或R-021正式区间。',
            '本轮未找到广东省公开、同地区同行业同期且口径匹配的增值税税负上下限；历史咨询回复不能证明当前所有地区都不公开。',
            '不得以个税核定所得率、企业所得税率、全国行业数据或单家上市公司毛利率替代广东行业增值税税负参考。',
        ], 'tax_burden_source': {'publisher': '国家税务总局东莞市税务局', 'url': TAX_URL,
            'title': '怎样查询各行业税负预警值', 'replied_on': '2021-07-16', 'retrieved_on': retrieved_on,
            'response_sha256': sha256(tax).hexdigest(), 'status': '历史咨询说明；不包含数值区间'},
        'editions': editions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retrieved-on', required=True, help='实际采集日期YYYY-MM-DD，不以主机时区推测')
    parser.add_argument('--verify', type=Path, help='只读复验已采集JSON；不覆盖文件')
    args = parser.parse_args()
    date.fromisoformat(args.retrieved_on)
    data = collect(args.retrieved_on)
    if args.verify:
        saved = json.loads(args.verify.read_text(encoding='utf-8'))
        def facts(document):
            return [{k: v for k, v in edition.items() if k not in {'retrieved_on', 'response_sha256'}}
                    for edition in document['editions']]
        if facts(saved) != facts(data) or saved['rule_bounds'] or saved['runtime_integration']:
            raise ValueError('已存数据与当前官方事实不同或被错误标为正式阈值，未覆盖。')
        print(f"官方逐行复验通过：{len(data['editions'])} 个期间，{sum(len(e['rows']) for e in data['editions'])} 行；正式阈值0条。")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
