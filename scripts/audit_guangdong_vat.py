"""Audit the retained Guangdong VAT batch and package candidate public knowledge.

The live capture is evidence of a source comparison, not legal approval. Offline
replay uses that exact capture. No enterprise database or credentials are read.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import repair_guangdong_corpus as repair  # noqa: E402
from src import policy_corpus  # noqa: E402
from src.public_policy_package import load_package, import_package  # noqa: E402
from webapp import policy_graph  # noqa: E402


def vat_documents(manifest):
    return [d for d in manifest['documents'] if '增值税' in d.get('tax_types', [])]


def capture(package, documents, assets):
    requests = {d['source_url']: {'kind': 'page', 'retained_sha256': d['raw_sha256']} for d in documents}
    for url in {u for d in documents for u in d['attachment_urls'] + d['body_image_urls']}:
        requests[url] = {'kind': 'asset', 'retained_sha256': assets[url]['raw_sha256']}
    stamp = datetime.now(timezone.utc).isoformat()
    def read(item):
        url, meta = item
        try:
            data = repair.fetch(url)
            record = {**meta, 'url': url, 'sha256': repair.sha(data), 'bytes': len(data),
                      'capture_path': 'sources/' + repair.sha(url.encode())}
            return record, data
        except (OSError, ValueError) as exc:
            return {**meta, 'url': url, 'error': str(exc)}, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(read, sorted(requests.items())))
    value = {'checked_at': stamp, 'sources': [record for record, _ in results]}
    temporary = package / '增值税来源复核快照.tmp.zip'
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for record, data in results:
            if data is not None:
                archive.writestr(record['capture_path'], data)
        archive.writestr('capture.json', json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(package / '增值税来源复核快照.zip')
    return value


def audit(package, *, online=False):
    manifest = repair.read_json(package / '来源清单.json')
    documents = vat_documents(manifest)
    assets = {a['url']: a for a in repair.read_json(package / '附件清单.json')['assets']}
    cross = {r['document_id']: r for r in repair.read_json(package / '效力核对结果.json')['documents']}
    if online:
        capture(package, documents, assets)
    path = package / '增值税来源复核快照.zip'
    rows = []
    with zipfile.ZipFile(path) as archive:
        live = json.loads(archive.read('capture.json'))
        captured = {r['url']: r for r in live['sources']}
        for document in documents:
            blockers, notes = [], []
            url = document['source_url']
            current = captured.get(url, {})
            retained = repair.read_verified(package, document['raw_path'], document['raw_sha256'])
            text = repair.read_verified(package, document['text_path'], document['text_sha256']).decode('utf-8')
            if current.get('error') or not current.get('capture_path'):
                blockers.append('官方正文未能在线复核')
            else:
                raw = archive.read(current['capture_path'])
                if repair.sha(raw) != current['sha256']:
                    raise ValueError('在线复核快照哈希不一致。')
                old_body = repair.html_parts(retained.decode('utf-8'))[0]
                new_body = repair.html_parts(raw.decode('utf-8'))[0]
                if repair.clean(old_body) != repair.clean(new_body):
                    blockers.append('官方正文与留存版本不同，须另建版本')
                if not new_body.strip():
                    blockers.append('官方正文容器为空')
            items = []
            for link in document['attachment_urls'] + document['body_image_urls']:
                asset, observed = assets[link], captured.get(link, {})
                repair.read_verified(package, asset['raw_path'], asset['raw_sha256'])
                if asset.get('text_path'):
                    repair.read_verified(package, asset['text_path'], asset['text_sha256'])
                if observed.get('error') or observed.get('sha256') != asset['raw_sha256']:
                    blockers.append('附件当前来源未核对一致：' + link)
                elif repair.sha(archive.read(observed['capture_path'])) != observed['sha256']:
                    raise ValueError('附件在线复核快照哈希不一致。')
                items.append({'url': link, 'status': asset['status'], 'raw_sha256': asset['raw_sha256'],
                              'text_sha256': asset.get('text_sha256'), 'quality_review': asset.get('quality_review')})
            cleanup = cross[document['document_id']]
            history = (cleanup['cleanup_verdict'] == 'expired' or document.get('validity_observed') in repair.EXPIRED
                       or document['date_coverage'].get('status_as_of') == 'expired')
            if history:
                notes.append('历史版本，仅供历史期间复核，不作为当前有效政策')
            if (cleanup['cleanup_verdict'] in {'partial', 'modified'} or document.get('validity_observed') in repair.PARTIAL
                    or document['date_coverage'].get('status_as_of') == 'partial'):
                notes.append('存在部分失效或修改，须核对具体条款')
            if not document['date_coverage']['effective_from']:
                notes.append('施行日期缺少可直接采用的证据')
            if document['date_coverage']['effective_to'] is None:
                notes.append('无明确终止日，尚未取得覆盖业务期间的人工复核')
            notes.extend(document['content_gaps'])
            rows.append({'document_id': document['document_id'], 'source_url': url, 'title': document['title'],
                         'document_no': document.get('document_no'), 'scope': repair.source_scope(text),
                         'date_evidence': document['date_coverage'], 'page_validity_observed': document.get('validity_observed'),
                         'cleanup': cleanup, 'assets': items, 'historical_only': history,
                         'legal_research': document.get('legal_research', {}),
                         'candidate_import': 'blocked' if blockers else 'passed', 'blockers': blockers,
                         'legal_review': 'pending_human_review', 'review_notes': notes})
    # Exact source bytes and normalized text both participate in duplicate review;
    # document numbers alone never collapse distinct issuers or editions.
    groups = {}
    for document in documents:
        groups.setdefault((document['raw_sha256'], document['text_sha256']), []).append(document['document_id'])
    report = {'scope': '广东增值税候选资料清洗及接入审核，不批准政策效力或企业适用性',
              'checked_at': live['checked_at'], 'capture_sha256': repair.sha(path.read_bytes()),
              'documents': rows, 'document_count': len(rows), 'source_count': len(live['sources']),
              'source_errors': sum('error' in r for r in live['sources']),
              'candidate_import_counts': dict(Counter(r['candidate_import'] for r in rows)),
              'historical_count': sum(r['historical_only'] for r in rows),
              'exact_duplicate_groups': [ids for ids in groups.values() if len(ids) > 1],
              'formal_approvals': 0, 'business_database_writes': 0}
    repair.write_json(package / '增值税接入审核.json', report)
    return report


def export_public(package, report):
    """Append accepted candidate sources to the bundled public graph, preserving 8 tables."""
    manifest = repair.read_json(package / '图谱候选清单.json')
    accepted = {'GD-' + repair.sha(row['source_url'].encode())[:20]: row for row in report['documents']
                if row['candidate_import'] == 'passed'}
    manifest['documents'] = [item for item in manifest['documents'] if item['id'] in accepted]
    for item in manifest['documents']:
        source = accepted[item['id']]
        item['scope'] = source['scope']
        item['date_evidence'] = source['date_evidence']
        item['source_metadata']['legal_research'] = source.get('legal_research', {})
        item['source_metadata']['attachment_quality'] = [a for a in source['assets'] if a.get('quality_review')]
        item['source_metadata']['source_audit'] = {k: source[k] for k in ['candidate_import', 'legal_review', 'historical_only']}
        item['source_metadata']['source_audit']['retained_source_sha256'] = item['raw_sha256']
    prepared = package / '增值税接入清单.json'
    repair.write_json(prepared, manifest)
    plan = policy_corpus.prepare(prepared)
    # Research evidence is retained with the package but is not misclassified as
    # an attachment clause of the older policy being repealed or amended.
    for row in report['documents']:
        research = row.get('legal_research', {})
        sources = [*research.get('events', []), *research.get('clause_date_candidates', [])]
        sources += [research[k] for k in ['own_enactment', 'relative_enactment'] if k in research]
        if research.get('referenced_material_gap', {}).get('change_evidence'):
            sources.append(research['referenced_material_gap']['change_evidence'])
        for source in sources:
            for kind in ['raw', 'text']:
                raw = repair.read_verified(package, source[kind + '_path'], source[kind + '_sha256'])
                file = repair.checked_path(package, source[kind + '_path'])
                plan['files']['research-' + source[kind + '_sha256'] + file.suffix] = {
                    'path': str(file), 'sha256': repair.sha(raw), 'suffix': file.suffix}
    payload = load_package(ROOT / 'data/public-policy/snapshot.json.gz', ROOT)
    originals = {item['path']: item for item in payload['originals']}
    file_paths = {}
    for name, item in plan['files'].items():
        raw = Path(item['path']).read_bytes()
        if repair.sha(raw) != item['sha256']:
            raise ValueError('待接入原件哈希不一致。')
        target = 'policy-originals/' + item['sha256'] + item['suffix']
        file_paths[name] = target
        if target not in originals:
            repository_path = 'docs/法规原文/增值税/GD-' + item['sha256'] + item['suffix']
            saved = ROOT / repository_path
            if saved.exists() and saved.read_bytes() != raw:
                raise ValueError('仓库原件已存在内容冲突，未覆盖。')
            saved.write_bytes(raw)
            originals[target] = {'path': target, 'repository_path': repository_path, 'sha256': item['sha256']}
    incoming = plan['bundle']
    for policy in incoming['Policy']:
        policy['raw_file_path'] = file_paths[next(n for n, f in plan['files'].items() if f['path'] == policy['raw_file_path'])]
        policy['updated_at'] = policy['fetched_at']
    for clause in incoming['Clause']:
        location = json.loads(clause['source_location'])
        for kind in ['raw', 'text']:
            location['retained_' + kind + '_path'] = file_paths[location[kind + '_path']]
        clause['source_location'] = policy_corpus.canonical(location)
    for table in policy_graph.IDS:
        rows = incoming.get(table, [])
        key = policy_graph.IDS[table]
        existing = {row[key]: row for row in payload['tables'][table]}
        for row in rows:
            for field in ('created_at', 'updated_at'):
                if any(field in r for r in existing.values()) and field not in row:
                    row[field] = incoming['Policy'][0]['fetched_at']
            if row[key] in existing and any(existing[row[key]].get(field) != row.get(field)
                    for field in existing[row[key]].keys() | row.keys()):
                raise ValueError('公共包已有节点内容冲突，须核对版本。')
            existing[row[key]] = row
        # A public import uses one column set per table. Old rows receive only
        # NULL optional fields; local reviewed values are still preserved.
        columns = sorted({k for row in existing.values() for k in row})
        payload['tables'][table] = [{k: row.get(k) for k in columns} for _, row in sorted(existing.items())]
    payload['originals'] = [originals[k] for k in sorted(originals)]
    payload['boundary'] = '公共知识候选资料；广东增值税来源通过技术审核，政策效力及企业适用仍须人工审核；不扩大业务税种。'
    # Observation timestamps identify an audit, not a new edition of a policy.
    # Rechecking unchanged bytes must never create duplicate policy versions.
    payload['source_audit'] = {'checked_at': report['checked_at'], 'capture_sha256': report['capture_sha256'],
                              'guangdong_vat_documents': len(accepted), 'ledger': 'docs/广东增值税接入审核.json'}
    raw = policy_corpus.canonical(payload).encode()
    compressed = gzip.compress(raw, mtime=0)
    # Verify the full delivery through the actual importer before replacing the
    # bundled snapshot. No real Store, local reviews or enterprise rows exist.
    with tempfile.TemporaryDirectory(prefix='guangdong-vat-package-') as temporary:
        db = sqlite3.connect(Path(temporary) / 'acceptance.sqlite')
        db.row_factory = sqlite3.Row
        try:
            policy_graph.migrate(db)
            with db:
                import_package(db, payload, ROOT)
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('公共包接入完整性验收失败。')
        finally:
            db.close()
    output = ROOT / 'data/public-policy'
    (output / 'snapshot.json.gz').write_bytes(compressed)
    package_manifest = {'format': payload['format'], 'schema_version': 1, 'snapshot': 'snapshot.json.gz',
                        'compressed_sha256': repair.sha(compressed), 'content_sha256': repair.sha(raw),
                        'table_counts': {t: len(rows) for t, rows in payload['tables'].items()},
                        'original_file_count': len(originals)}
    repair.write_json(output / 'manifest.json', package_manifest)
    return package_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--export-public', action='store_true')
    args = parser.parse_args()
    report = audit(args.package.resolve(), online=args.fetch)
    if args.export_public:
        report['public_package'] = export_public(args.package.resolve(), report)
        repair.write_json(args.package / '增值税接入审核.json', report)
    print(json.dumps({k: v for k, v in report.items() if k != 'documents'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
