"""Import the checked-in public policy snapshot without replacing business/teaching data."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.public_policy_package import load_package, import_package
from webapp import policy_graph


def install(path, package=ROOT/'data/public-policy/snapshot.json.gz'):
    payload = load_package(package, ROOT)
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('请先启动应用初始化目标数据库，不能将公共快照替换为业务库。')
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    backup = path.with_name(path.name + '.before-public-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite')
    try:
        target = sqlite3.connect(backup)
        try:
            db.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('导入前备份完整性检查失败。')
        finally:
            target.close()
        policy_graph.migrate(db)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        result = import_package(db, payload, ROOT)
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('导入后数据库完整性检查失败。')
        db.commit()
        return {**result, 'backup': str(backup), 'integrity': 'ok'}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--package', type=Path, default=ROOT/'data/public-policy/snapshot.json.gz')
    args = parser.parse_args()
    print(json.dumps(install(args.db, args.package), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
