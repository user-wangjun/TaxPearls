"""Migrate retained national/Guangdong policy knowledge; never approve policy.

An explicit --db is required. Existing business records are not read or changed;
the SQLite online backup is completed before the first schema/content mutation.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import engine, policy_corpus, policy_migration, rule_conversion
from webapp import policy_graph


def migrate(db_path, manifest, originals, *, report=None, backup=True):
    plan = policy_migration.prepare(manifest)
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if backup and path.exists():
        backup_path = path.parent / (path.name + '.pre-knowledge-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite')
        source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('迁移前备份完整性检查未通过。')
        finally:
            target.close()
            source.close()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        policy_graph.migrate(db)
        rules = engine.load_rules(ROOT/'rules')
        # Use the same current rule selection as the application, without Store
        # construction (which would initialize unrelated business tables).
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'rule_overrides' in tables and db.execute('SELECT 1 FROM rule_overrides LIMIT 1').fetchone():
            raise ValueError('存在已发布规则覆盖，请通过管理员导入入口使用当前规则版本。')
        with db:
            db.execute('BEGIN IMMEDIATE')
            policy_graph.sync_rules(db, rules)
            ids = {r.id:rule_conversion.compile_rule(r)['rule_id'] for r in rules}
            counts = policy_corpus.import_plan(db, plan, originals, rule_ids=ids)
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('图谱外键不完整。')
            if db.execute('SELECT count(*) FROM kg_clause_fts').fetchone()[0] != db.execute('SELECT count(*) FROM Clause').fetchone()[0]:
                raise ValueError('全文索引条款数不一致。')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('迁移后数据库完整性检查未通过。')
        result = dict(database=str(path), backup=str(backup_path) if backup_path else None,
                      migration_version=policy_migration.VERSION, plan_counts=counts,
                      database_counts={t:db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in policy_graph.IDS},
                      coverage=plan['knowledge_migration'], integrity='ok', foreign_keys='ok',
                      formal_approvals=0, external_ai_calls=0)
        if report:
            Path(report).parent.mkdir(parents=True, exist_ok=True)
            Path(report).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, default=ROOT/'docs/法规原文/来源清单.json')
    parser.add_argument('--originals', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = migrate(args.db, args.manifest, args.originals or args.db.parent/'policy-originals', report=args.report)
    print(json.dumps({k:v for k,v in result.items() if k!='coverage'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
