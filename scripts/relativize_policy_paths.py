"""Check/migrate retained policy paths; --apply makes a backup before updating paths."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.policy_paths import relativize_paths


def migrate(path, *, apply=False):
    path = Path(path).resolve()
    db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
    db.row_factory = sqlite3.Row
    backup = None
    try:
        if apply:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = path.with_name(path.name + '.before-relative-' + stamp + '.sqlite')
            target = sqlite3.connect(backup)
            try:
                db.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('迁移前备份完整性检查失败。')
            finally:
                target.close()
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        result = relativize_paths(db)
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('外键检查失败，未保存路径迁移。')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('数据库完整性检查失败，未保存路径迁移。')
        if apply:
            db.commit()
        else:
            db.rollback()
        return {**result, 'applied': apply, 'database': str(path),
                'backup': str(backup) if backup else None, 'integrity': 'ok', 'foreign_keys': 'ok'}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='备份后保存；默认仅核验并回滚。')
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
