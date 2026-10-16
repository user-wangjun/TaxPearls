"""Read-only source verification for the one-off Guangdong policy collection.

No database writes, policy activation, expression evaluation, or scheduling.
--collect prints source snapshots to stdout; the curated JSON is not overwritten.
"""
from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs/广东省官方政策数据.json'
CATALOGS = (ROOT / 'docs/广东省地方政策.yaml', ROOT / 'docs/国家级税费政策.yaml')
LOCAL_SOURCES = {'gd-deed', 'gd-property', 'gd-revision'}
NATIONAL_SOURCES = {'vat-2026', 'vat-administration', 'vat-law', 'small-2023',
                    'small-2023-start', 'small-2022-start', 'rd-2023', 'rd-scope', 'rd-overseas'}
HOSTS = {'www.gd.gov.cn', 'www.gdpc.gov.cn', 'fgk.chinatax.gov.cn',
         'szs.mof.gov.cn', 'guangdong.chinatax.gov.cn'}


class PolicyLoader(yaml.SafeLoader):
    """Reject duplicate fields rather than silently overriding curated evidence."""
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError('政策YAML字段重复或不是文本。')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_catalog(text):
    # These data-only files have no need for shared references or merge keys.
    if any(isinstance(event, yaml.events.AliasEvent) or getattr(event, 'anchor', None)
           for event in yaml.parse(text)):
        raise ValueError('政策YAML不允许别名或锚点。')
    data = yaml.load(text, Loader=PolicyLoader)
    validate_catalog(data)
    return data


def validate_catalog(data):
    if not isinstance(data, dict) or data.get('policy_level') not in {'provincial', 'national'}:
        raise ValueError('政策文件必须明确国家级或省级层级。')
    local = data['policy_level'] == 'provincial'
    region = '广东省' if local else '全国'
    allowed = LOCAL_SOURCES if local else NATIONAL_SOURCES
    if data.get('applicable_regions') != [region]:
        raise ValueError('政策文件层级与适用地区不一致。')
    if not data.get('sources') or not data.get('policies'):
        raise ValueError('政策文件缺来源或政策条目。')
    for source in data['sources']:
        if source['id'] not in allowed or source.get('issuing_level') != data['policy_level']:
            raise ValueError('国家级与广东地方来源混放或层级错标。')
        if not source.get('snapshot'):
            raise ValueError('政策YAML必须保留官方原文快照。')
    for policy in data['policies']:
        if policy.get('policy_level') != data['policy_level'] or policy.get('applicable_regions') != [region]:
            raise ValueError('政策条目层级与适用地区不一致。')
        expected_scope = '广东省地方规定' if local else '全国规定在广东适用'
        if policy['region_scope'] != expected_scope:
            raise ValueError('国家政策不得归类为广东专属政策。')
    validate(data)


def load_collection(paths):
    catalogs = []
    for path in paths:
        text = path.read_text(encoding='utf-8')
        data = json.loads(text) if path.suffix == '.json' else parse_catalog(text)
        validate(data)
        catalogs.append(data)
    if not catalogs:
        raise ValueError('未指定政策文件。')
    result = {k: v for k, v in catalogs[0].items()
              if k not in {'sources', 'policies', 'policy_level', 'applicable_regions'}}
    result.update(sources=[], policies=[])
    for catalog in catalogs:
        if catalog['checked_as_of'] != result['checked_as_of']:
            raise ValueError('不能合并不同核对日期的政策目录。')
        result['sources'].extend(catalog['sources'])
        result['policies'].extend(catalog['policies'])
    validate(result)
    return result


def compact(text):
    return re.sub(r'\s+', '', text)


def official_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port:
        raise ValueError('仅允许本批已核对的官方HTTPS来源。')


class ArticleText(HTMLParser):
    """Collect article text only; never execute HTML or use hidden script data."""
    def __init__(self, target):
        super().__init__(convert_charrefs=True)
        self.target, self.depth, self.skip = target, 0, 0
        self.blocks, self.text, self.hits = [], [], 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style'}:
            self.skip += 1
        if tag == 'div':
            if self.depth:
                self.depth += 1
            elif self.target in attrs.get('class', '').split() or attrs.get('id') == self.target:
                self.depth, self.text = 1, []
                self.hits += 1
        if self.depth and tag in {'p', 'br', 'li'}:
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.skip = max(0, self.skip - 1)
        if self.depth and tag == 'div':
            self.depth -= 1
            if self.depth == 0:
                self.blocks.append(''.join(self.text))
        if self.depth and tag in {'p', 'li'}:
            self.text.append('\n')

    def handle_data(self, text):
        if self.depth and not self.skip:
            self.text.append(text)


def extract_html(raw, target):
    parser = ArticleText(target)
    parser.feed(raw.decode('utf-8'))
    blocks = [block for block in parser.blocks if compact(block)]
    if parser.depth or len(blocks) != 1:
        raise ValueError('正文容器缺失、重复或不完整，不能将导航/登录页当政策。')
    result = '\n'.join(line.strip() for line in blocks[0].splitlines() if line.strip())
    if len(result) < 50:
        raise ValueError('政策正文不足，未采集。')
    return result


def extract_pdf(raw, page_numbers):
    if not raw.startswith(b'%PDF-'):
        raise ValueError('PDF来源返回非PDF字节。')
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(BytesIO(raw)) as document:
        result = []
        for number in page_numbers:
            if type(number) is not int or not 1 <= number <= len(document):
                raise ValueError('来源页码无效。')
            page = document[number - 1]
            try:
                textpage = page.get_textpage()
                try:
                    result.append({'page': number, 'text': textpage.get_text_range()})
                finally:
                    textpage.close()
            finally:
                page.close()
    return result


def fetch(url):
    official_url(url)
    with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30) as response:
        official_url(response.geturl())
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('政策来源超过本次读取上限。')
    return raw


def snapshot(source, raw, retrieved_on):
    pages = extract_pdf(raw, source['pages']) if source['format'] == 'pdf' else []
    text = '\n'.join(page['text'] for page in pages) if pages else extract_html(raw, source['container'])
    for token in source['required_text']:
        if compact(token) not in compact(text):
            raise ValueError(f"{source['id']}：必要条款未匹配「{token}」，须人工复查。")
    return {'retrieved_on': retrieved_on, 'byte_length': len(raw), 'response_sha256': sha256(raw).hexdigest(),
            'article_text_sha256': sha256(compact(text).encode()).hexdigest(), 'text': text, 'pages': pages}


def validate(data):
    if data['runtime_integration'] is not False or data['automatic_calculation_enabled'] is not False:
        raise ValueError('本次采集不能被标为已接入或已启用计算。')
    as_of = date.fromisoformat(data['checked_as_of'])
    sources = {source['id']: source for source in data['sources']}
    if len(sources) != len(data['sources']):
        raise ValueError('来源编号重复。')
    for source in sources.values():
        official_url(source['url'])
        saved = source.get('snapshot')
        if saved:
            if date.fromisoformat(saved['retrieved_on']) != as_of:
                raise ValueError('核对日期与原文快照日期不一致。')
            if type(saved['byte_length']) is not int or saved['byte_length'] <= 0 or not re.fullmatch(r'[0-9a-f]{64}', saved['response_sha256']):
                raise ValueError('来源字节数量或指纹缺失。')
            if sha256(compact(saved['text']).encode()).hexdigest() != saved['article_text_sha256']:
                raise ValueError('已存原文指纹不一致。')
            if source['format'] == 'pdf':
                if [page['page'] for page in saved['pages']] != source['pages'] or '\n'.join(page['text'] for page in saved['pages']) != saved['text']:
                    raise ValueError('PDF原页与合并原文不一致。')
            if any(compact(token) not in compact(saved['text']) for token in source['required_text']):
                raise ValueError('已存原文缺必要条款。')
    ids = set()
    for policy in data['policies']:
        if policy['id'] in ids:
            raise ValueError('政策编号重复。')
        ids.add(policy['id'])
        if not policy['source_ids'] or set(policy['source_ids']) - sources.keys():
            raise ValueError('政策来源未收集。')
        if policy['region_scope'] not in {'广东省地方规定', '全国规定在广东适用'}:
            raise ValueError('地方与全国适用范围未分开。')
        start = date.fromisoformat(policy['effective_from'])
        end = date.fromisoformat(policy['effective_to']) if policy['effective_to'] else None
        if start > as_of or end and end < start:
            raise ValueError('所选期间无效。')
        if not policy['conditions'] or not policy['exclusions'] or not policy['calculation_notes']:
            raise ValueError('不能只收集税率，缺条件/排除项/计算说明。')
        for parameter in policy['parameters']:
            if not isinstance(parameter['value'], str) or not parameter['value'].strip():
                raise ValueError('政策参数须保留原始文本精度，不能隐式转为YAML浮点数。')
            if parameter['source_id'] not in sources:
                raise ValueError('参数来源未收集。')
            source = sources[parameter['source_id']]
            if parameter['source_id'] not in policy['source_ids'] or not parameter['clause'] or not parameter['evidence_text']:
                raise ValueError('参数未绑定具体依据。')
            if source.get('snapshot') and compact(parameter['evidence_text']) not in compact(source['snapshot']['text']):
                raise ValueError('参数证据未匹配已保存原文。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, action='append', help='指定政策YAML或原采集JSON，可重复；默认核验两份分级YAML')
    parser.add_argument('--collect', action='store_true', help='打印原文快照JSON，不覆盖文件')
    parser.add_argument('--offline', action='store_true', help='只校验已存来源、参数和指纹')
    parser.add_argument('--retrieved-on', required=True, help='本次实际核对日期YYYY-MM-DD')
    args = parser.parse_args()
    date.fromisoformat(args.retrieved_on)
    data = load_collection(args.data or CATALOGS)
    if args.offline:
        if not all(source.get('snapshot') for source in data['sources']):
            raise ValueError('官方原文尚未全部保存。')
    else:
        if args.collect:
            data['checked_as_of'] = args.retrieved_on
        for source in data['sources']:
            current = snapshot(source, fetch(source['url']), args.retrieved_on)
            saved = source.get('snapshot')
            if not args.collect and (not saved or current['article_text_sha256'] != saved['article_text_sha256']):
                raise ValueError(f"{source['id']}：官方正文变化或未保存；未覆盖，须重新核对。")
            if args.collect:
                source['snapshot'] = current
        validate(data)
    if args.collect:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"政策来源核验通过：{len(data['policies'])} 个政策主题，{len(data['sources'])} 份官方来源；自动计算未启用。")


if __name__ == '__main__':
    main()
