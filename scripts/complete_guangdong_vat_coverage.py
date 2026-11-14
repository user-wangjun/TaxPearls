"""Retain supplementary official VAT sources and export evidence-only candidates.

This is the bounded Guangdong coverage delivery, not a crawler or legal approval.
The input manifest is also the offline replay record; collection never writes a
business database. Existing policy nodes and reviews are preserved.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import gzip
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unicodedata
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import repair_guangdong_corpus as repair  # noqa: E402
from src import policy_corpus  # noqa: E402
from src.public_policy_package import import_package, load_package  # noqa: E402
from webapp import policy_graph  # noqa: E402


def normalized(value):
    value = unicodedata.normalize('NFKC', value)
    return re.sub(r'[^\w]', '', value).replace('国家税务总局', '税务总局').replace('实行农产品', '试行农产品')


def citation_matches(title, number, policy):
    """Match an authority-shortened title only with the same year and number."""
    def title_core(value):
        value = normalized(value)
        return value[value.index('关于'):] if '关于' in value else value
    def issue(value):
        value = unicodedata.normalize('NFKC', value or '')
        found = re.search(r'([12]\d{3})[^\d]{0,10}(\d+)\s*号', value)
        return found.groups() if found else None
    def authority(value):
        prefix = re.split(r'[12]\d{3}', unicodedata.normalize('NFKC', value or ''), maxsplit=1)[0]
        return normalized(prefix).replace('公告', '').replace('令', '').rstrip('第')
    if authority(number) and authority(number) != authority(policy.get('document_no')):
        return False
    return (issue(number) is not None and issue(number) == issue(policy.get('document_no'))
            and title_core(title) == title_core(policy['title']))


def body_parts(raw):
    html = raw.decode('utf-8-sig')
    root = repair.DOM(html).root
    # Explicit publishing templates only: never ingest navigation as legislation.
    body = next((n for n in root.nodes() if n.attrs.get('id') == 'zoomcon'), None)
    if body is None:
        body = next((n for n in root.nodes() if 'arc_cont' in n.attrs.get('class', '').split()), None)
    if body is None:
        body = next((n for n in root.nodes() if n.attrs.get('id') in {'zoom', 'fontzoom'}), None)
    if body is None:
        body = next((n for n in root.nodes() if 'TRS_Editor' in n.attrs.get('class', '').split()), None)
    if body is None:
        body = next((n for n in root.nodes() if 'info-cont' in n.attrs.get('class', '').split()), None)
    if body is None:
        raise ValueError('No recognized official article container')
    text = repair.clean(body.text())
    if len(text) < 40:
        raise ValueError('Official article text is incomplete')
    links = [{'link': n.attrs['href'], 'label': repair.clean(n.text())}
             for n in root.nodes() if n.tag == 'a' and repair.FILE.search(n.attrs.get('href', ''))]
    images = [n.attrs['src'] for n in body.nodes() if n.tag == 'img' and n.attrs.get('src')
              and '/icons/' not in n.attrs['src']]
    meta = {n.attrs.get('name'): n.attrs.get('content') for n in root.nodes() if n.tag == 'meta'}
    return text, links, images, meta


def collect_one(package, source):
    source = dict(source)
    url = source['source_url']
    ident = repair.sha(url.encode())[:24]
    raw_path = '法规原文/增值税/COVERAGE-' + ident + '.html'
    raw_file = package / raw_path
    if raw_file.exists():
        raw = raw_file.read_bytes()
    else:
        raw = repair.fetch(url)
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_bytes(raw)
    text, links, images, meta = body_parts(raw)
    if source.get('title_probe') and normalized(source['title_probe']) not in normalized(meta.get('ArticleTitle') or text):
        raise ValueError('Official title does not match selected policy')
    text_path = raw_path + '.txt'
    (package / text_path).write_bytes(text.encode())
    attachments, supporting, gaps = [], [], []
    for link in {urljoin(url, x['link']): x for x in links}.items():
        asset_url, label = link
        suffix = Path(urlsplit(asset_url).path).suffix.lower()
        relative = '法规原文/增值税/COVERAGE-' + repair.sha(asset_url.encode())[:24] + suffix
        file = package / relative
        try:
            data = file.read_bytes() if file.exists() else repair.fetch(asset_url)
            file.write_bytes(data)
            asset = {'source_url': asset_url, 'path': relative, 'sha256': repair.sha(data), 'label': label['label']}
            try:
                detected = suffix
                if data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
                    import olefile
                    with olefile.OleFileIO(io.BytesIO(data)) as ole:
                        detected = '.doc' if ole.exists('WordDocument') else '.xls' if ole.exists('Workbook') or ole.exists('Book') else suffix
                extracted, state = repair.extract_asset(data, detected)
                if data.startswith(b'%PDF'):
                    # A wrapped quoted article is continuation text, not a new
                    # article in the attached regulation. Keep the raw PDF.
                    extracted = re.sub(r'》\n(第[一二三四五六七八九十百零〇\d]+条)', r'》\1', extracted)
                if extracted:
                    asset.update(text_path=relative + '.txt', text_sha256=repair.sha(extracted.encode()), extraction_status=state)
                    (package / asset['text_path']).write_bytes(extracted.encode())
                else:
                    gaps.append({'kind': 'attachment_text_pending', 'url': asset_url, 'status': state})
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                gaps.append({'kind': 'attachment_text_pending', 'url': asset_url, 'detail': str(exc)})
            if label['label'] in {'下载文字版', '下载图片版'}:
                supporting.append({'source_url': asset_url, 'raw_path': relative, 'raw_sha256': repair.sha(data),
                                   **({k: asset[k] for k in ['text_path', 'text_sha256']} if asset.get('text_path') else {})})
            else:
                attachments.append(asset)
        except (OSError, ValueError) as exc:
            gaps.append({'kind': 'attachment_download_pending', 'url': asset_url, 'detail': str(exc)})
    gaps.extend({'kind': 'body_image_pending', 'url': urljoin(url, img)} for img in images)
    # A mirror can print an attachment title without exposing its download link.
    if '附件' in text and not links and not source.get('attachments_inline'):
        gaps.append({'kind': 'attachment_link_unavailable', 'detail': '正文提及附件，镜像未提供可下载链接；不声称附件覆盖完整。'})
    page_text = repair.clean(repair.DOM(raw.decode('utf-8-sig')).root.text())
    observed = next((x for x in ['全文废止', '全文失效', '部分条款失效', '部分失效', '尚未生效', '全文有效'] if x in page_text), None)
    dates = repair.extract_dates(text, source.get('publish_date'), '2026-10-21', observed)
    annotation_dates = [repair.extract_dates((package / a['text_path']).read_text(encoding='utf-8'), None, '2026-10-21')
                        for a in attachments if a.get('text_path')]
    if source.get('effective_quote'):
        quote = source['effective_quote']
        if quote not in text:
            raise ValueError('Explicit effective-date quote is absent')
        dates.update(effective_from=source['effective_from'], effective_from_source='reviewed_exact_quote',
                     effective_from_evidence=quote)
        dates['gaps'] = [g for g in dates['gaps'] if g != 'effective_from_unknown']
    if source.get('effective_to_quote'):
        quote = source['effective_to_quote']
        end = date.fromisoformat(source['effective_to'])
        literal = f'{end.year}年{end.month}月{end.day}日'
        if quote not in text or literal not in re.sub(r'\s+', '', repair.normal_dates(quote)) or not re.search(r'本(?:公告|通知|办法).*执行至', quote):
            raise ValueError('Explicit self-termination quote is absent or inconsistent')
        dates.update(effective_to=end.isoformat(), effective_to_source='reviewed_exact_self_termination',
                     effective_to_evidence=quote)
        dates['gaps'] = [g for g in dates['gaps'] if g != 'effective_to_unknown']
    if dates['effective_from'] and dates['effective_from'] > '2026-10-21':
        dates['status_as_of'] = 'pending'
    historical = source.get('stop_evidence')
    if historical:
        proof = repair.read_verified(package, historical['text_path'], historical['text_sha256']).decode('utf-8-sig')
        repair.read_verified(package, historical['raw_path'], historical['raw_sha256'])
        if proof[historical['start_char']:historical['end_char_exclusive']] != historical['quote']:
            raise ValueError('Historical stop evidence does not match retained source')
        stopped_on = source['stopped_on']
        dates.update(status_as_of='expired' if stopped_on <= '2026-10-21' else 'unknown', stop_evidence=historical,
                     effective_to=(date.fromisoformat(stopped_on) - timedelta(days=1)).isoformat(),
                     effective_to_source='explicit_stop_date')
    return {'id': 'VAT-COVERAGE-' + ident, 'title': source['title'], 'classification': '增值税',
            'tax_types': ['增值税'], 'region_code': source.get('region_code', 'CN'),
            'source_url': url, 'fetched_at': source['fetched_at'], 'raw_path': raw_path,
            'raw_sha256': repair.sha(raw), 'text_path': text_path, 'text_sha256': repair.sha(text.encode()),
            'version_effective_from': dates['effective_from'], 'date_evidence': dates,
            'version_effective_to_exclusive': (date.fromisoformat(dates['effective_to']) + timedelta(days=1)).isoformat() if dates['effective_to'] else None,
            'attachments': attachments, 'document_kind': 'policy_candidate',
            'supporting_sources': supporting,
            'source_metadata': {'coverage_role': source['role'], 'content_gaps': gaps,
                'page_validity_observed': observed, 'attachment_date_evidence': annotation_dates,
                'legal_review': 'pending_human_review',
                'source_audit': {'candidate_import': 'passed' if not gaps else 'blocked_content_gaps',
                                 'historical_only': dates['status_as_of'] == 'expired'}},
            'annotation': {'document_no': source.get('document_no'), 'issuer': source.get('issuer') or '发布机关待核实',
                           'publish_date': source.get('publish_date')}}


def resolve_reviewed_reference(package, row, policies, reviews):
    """Resolve a manually reviewed citation only at its exact source occurrence."""
    matches = [r for r in reviews if r['quoted_number'] == row['document_no_quoted']
               and any(e['source_url'] == row['source_url']
                       and e['text_sha256'] == row['source_text_sha256']
                       and e['quote'] == row['quote'] and e['start_char'] == row['start_char']
                       and e['end_char_exclusive'] == row['end_char_exclusive'] for e in r['evidence'])]
    if not matches:
        return row
    if len(matches) != 1:
        raise ValueError('Ambiguous reviewed citation occurrence')
    review = matches[0]
    for proof in review['evidence']:
        text = repair.read_verified(package, proof['text_path'], proof['text_sha256']).decode('utf-8')
        if text[proof['start_char']:proof['end_char_exclusive']] != proof['quote']:
            raise ValueError('Reviewed citation evidence offset mismatch')
    targets = [p for p in policies if citation_matches(review.get('canonical_title', row['title']),
                                                     review['canonical_document_no'], p)]
    source_only = review.get('reference_only_target')
    if not targets and source_only:
        repair.read_verified(package, source_only['raw_path'], source_only['raw_sha256'])
        page = repair.read_verified(package, source_only['page_text_path'], source_only['page_text_sha256']).decode('utf-8')
        if review.get('canonical_title') not in page or review['canonical_document_no'] not in page:
            raise ValueError('Reference-only target identity is absent from retained official page')
        return {**row, 'matched_policy_ids': [], 'status': 'source_resolved_content_pending',
                'canonical_document_no': review['canonical_document_no'], 'canonical_title': review['canonical_title'],
                'match_method': 'reviewed_exact_source_identity', 'identity_conclusion': review['conclusion'],
                'identity_evidence': review['evidence'], 'target_source_url': source_only['source_url'],
                'content_gaps': source_only['content_gaps'], 'legal_review': 'pending_human_review'}
    if len(targets) != 1:
        raise ValueError('Reviewed citation requires one retained target policy')
    return {**row, 'matched_policy_ids': [targets[0]['policy_id']], 'status': 'covered',
            'canonical_document_no': review['canonical_document_no'],
            **({'canonical_title': review['canonical_title']} if review.get('canonical_title') else {}),
            'match_method': 'reviewed_exact_source_identity', 'identity_conclusion': review['conclusion'],
            'identity_evidence': review['evidence'], 'legal_review': 'pending_human_review'}


def references(package, payload):
    """A citation inventory: each title AND number occurrence keeps its offsets.

    Different years sharing one title are deliberately not merged. Title-only
    matching never claims to have resolved a numbered citation.
    """
    corpus = repair.read_json(package / '来源清单.json')
    result = []
    policies = payload['tables']['Policy']
    review_path = package / '增值税引用效力日期复核.json'
    reviews = (repair.read_json(review_path).get('conflict_identity_review_20261022', {}).get('results', [])
               if review_path.exists() else [])
    for source in corpus['documents']:
        if '增值税' not in source['tax_types']:
            continue
        text = repair.read_verified(package, source['text_path'], source['text_sha256']).decode('utf-8-sig')
        for match in re.finditer(r'《([^《》\n]{4,160})》[（(]([^）)\n]{3,100})[）)]', text):
            title, number = match[1], match[2]
            if not re.search(r'\d{4}', unicodedata.normalize('NFKC', number)) or not re.search('号|令', number):
                continue
            hits = [p['policy_id'] for p in policies if citation_matches(title, number, p)]
            non_policy = any(w in title for w in ['请示', '申请的函', '情况的通报', '通知书'])
            result.append({'source_url': source['source_url'], 'source_text_path': source['text_path'],
                           'source_text_sha256': source['text_sha256'], 'title': title, 'document_no_quoted': number,
                           'quote': match[0], 'start_char': match.start(), 'end_char_exclusive': match.end(),
                           'matched_policy_ids': hits, 'status': 'covered' if len(hits) == 1 else 'reference_material' if non_policy else 'unresolved'})
    return [resolve_reviewed_reference(package, row, policies, reviews) for row in result]


def export(package, manifest):
    ready = dict(manifest)
    ready['documents'] = [d for d in manifest['documents'] if not d['source_metadata']['content_gaps']]
    ready['graph_preparation'] = {'format': policy_corpus.FORMAT, 'documents': {d['id']: d['annotation'] for d in ready['documents']}}
    path = package / '增值税覆盖接入清单.json'
    repair.write_json(path, ready)
    plan = policy_corpus.prepare(path)
    payload = load_package(ROOT / 'data/public-policy/snapshot.json.gz', ROOT)
    for policy in plan['bundle']['Policy']:
        family = policy['policy_id'].split(':organized:')[0] + ':organized:'
        if any(p['policy_id'].startswith(family) and p['policy_id'] != policy['policy_id'] for p in payload['tables']['Policy']):
            raise ValueError('Supplementary policy metadata changed; review the existing version before export')
    originals = {x['path']: x for x in payload['originals']}
    paths = {}
    for name, item in plan['files'].items():
        data = Path(item['path']).read_bytes()
        if repair.sha(data) != item['sha256']:
            raise ValueError('Supplementary source hash mismatch')
        target = 'policy-originals/' + item['sha256'] + item['suffix']
        paths[name] = target
        if target not in originals:
            relative = 'docs/法规原文/增值税/COVERAGE-' + item['sha256'] + item['suffix']
            file = ROOT / relative
            if file.exists() and file.read_bytes() != data:
                raise ValueError('Retained source conflict')
            file.write_bytes(data)
            originals[target] = {'path': target, 'repository_path': relative, 'sha256': item['sha256']}
    for policy in plan['bundle']['Policy']:
        policy['raw_file_path'] = next(paths[n] for n, f in plan['files'].items() if f['path'] == policy['raw_file_path'])
        policy['updated_at'] = policy['fetched_at']
    for clause in plan['bundle']['Clause']:
        location = json.loads(clause['source_location'])
        for kind in ['raw', 'text']:
            location['retained_' + kind + '_path'] = paths[location[kind + '_path']]
        clause['source_location'] = policy_corpus.canonical(location)
    for table, rows in plan['bundle'].items():
        key = policy_graph.IDS[table]
        existing = {row[key]: row for row in payload['tables'][table]}
        for row in rows:
            for field in ('created_at', 'updated_at'):
                if any(field in x for x in existing.values()) and field not in row:
                    row[field] = ready['documents'][0]['fetched_at']
            if row[key] in existing and existing[row[key]] != {k: row.get(k) for k in existing[row[key]]}:
                raise ValueError('Existing policy node conflict')
            existing[row[key]] = row
        columns = sorted({k for row in existing.values() for k in row})
        payload['tables'][table] = [{k: row.get(k) for k in columns} for _, row in sorted(existing.items())]
    resolved = [r for r in references(package, payload) if r.get('match_method') == 'reviewed_exact_source_identity']
    for row in resolved:
        for proof in row['identity_evidence']:
            data = repair.read_verified(package, proof['text_path'], proof['text_sha256'])
            target = 'policy-originals/' + proof['text_sha256'] + '.txt'
            if target not in originals:
                relative = 'docs/法规原文/增值税/REVIEW-' + proof['text_sha256'] + '.txt'
                file = ROOT / relative
                if file.exists() and file.read_bytes() != data:
                    raise ValueError('Retained citation evidence conflict')
                file.write_bytes(data)
                originals[target] = {'path': target, 'repository_path': relative, 'sha256': proof['text_sha256']}
            proof['retained_text_path'] = target
    payload['citation_resolutions'] = resolved
    payload['originals'] = [originals[k] for k in sorted(originals)]
    with tempfile.TemporaryDirectory(prefix='vat-coverage-') as temporary:
        db = sqlite3.connect(Path(temporary) / 'acceptance.sqlite')
        db.row_factory = sqlite3.Row
        try:
            policy_graph.migrate(db)
            with db:
                import_package(db, payload, ROOT)
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Supplementary public graph integrity failure')
        finally:
            db.close()
    raw = policy_corpus.canonical(payload).encode()
    compressed = gzip.compress(raw, mtime=0)
    destination = ROOT / 'data/public-policy'
    result = {'format': payload['format'], 'schema_version': 1, 'snapshot': 'snapshot.json.gz',
              'compressed_sha256': repair.sha(compressed), 'content_sha256': repair.sha(raw),
              'table_counts': {t: len(rows) for t, rows in payload['tables'].items()}, 'original_file_count': len(originals)}
    (destination / 'snapshot.json.gz').write_bytes(compressed)
    repair.write_json(destination / 'manifest.json', result)
    return payload, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--collect', action='store_true')
    args = parser.parse_args()
    package = args.package.resolve()
    source_path = package / '增值税政策覆盖清单.json'
    manifest = repair.read_json(source_path)
    if args.collect:
        def run(source):
            try:
                return collect_one(package, source), None
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                return None, {'source_url': source['source_url'], 'error': str(exc)}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, manifest['selected_sources']))
        manifest['documents'] = [d for d, _ in results if d]
        manifest['failures'] = [e for _, e in results if e]
        repair.write_json(source_path, manifest)
    payload, counts = export(package, manifest)
    inventory = references(package, payload)
    ledger = {'checked_at': datetime.now(timezone.utc).isoformat(), 'as_of': '2026-10-21',
              'boundary': '补充官方原文及候选条款；附件缺口阻止接入；不批准法律效力、适用性或计算规则。历史直接引用清单仍有待查项，不能宣称广东增值税全部覆盖。',
              'reference_inventory_boundary': '仅盘点《标题》（年份文号）形式的显式引用，不含简称或无文号引用；同名不同年份分别核对；未匹配不等于确认缺失；请示、申请及通知书单列。',
              'selected': len(manifest['selected_sources']), 'retained': len(manifest['documents']),
              'imported': len([d for d in manifest['documents'] if not d['source_metadata']['content_gaps']]),
              'failures': manifest.get('failures', []), 'documents': manifest['documents'],
              'reference_inventory': inventory, 'public_package': counts}
    repair.write_json(package / '增值税政策覆盖审核.json', ledger)
    repair.write_json(ROOT / 'docs/广东增值税政策覆盖审核.json', ledger)
    print(json.dumps({k: ledger[k] for k in ['selected', 'retained', 'imported', 'public_package', 'failures']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
