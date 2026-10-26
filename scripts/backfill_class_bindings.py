"""教学班 → 班级实体归属回填（FR-U01 尾巴，可预演、幂等）。

为什么需要它
------------
`training_classes.college_class_id` 是院系/专业维度报表的唯一归属凭据。历史数据
里这一列全是 NULL，所以「管理报表」里所有教学班都落在「未归属」，院系合计是 0。
本脚本按**同院校内同名实体恰好一个**认领一次，判过的行写 `binding_source`，
以后不再重复评估。

三条边界（与 `webapp.training_reports.backfill_bindings` 同源）
--------------------------------------------------------------
1. 只碰 `college_class_id IS NULL AND binding_source IS NULL` 的行；
   管理员绑定（manual）或解绑（unbound）过的行一律不动。
2. 同名实体 0 个或多个 → 留 NULL，记 `unbound`。宁可未归属，也不猜。
3. 判定即定案，重复执行不会重复改数。

用法
----
    python -B -X utf8 scripts/backfill_class_bindings.py --db path/to/taxpearls.db --dry-run
    python -B -X utf8 scripts/backfill_class_bindings.py --db path/to/taxpearls.db --apply

默认 `--dry-run`：只打印将要发生的绑定，不落库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(description="按同院校内同名唯一回填教学班归属")
    parser.add_argument("--db", default=os.environ.get("TAXPEARLS_DB", ""),
                        help="目标数据库；缺省读 TAXPEARLS_DB")
    parser.add_argument("--college", default="", help="只处理该院校（缺省全部）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True, help="只预演，不落库（默认）")
    group.add_argument("--apply", dest="dry_run", action="store_false", help="真正写库")
    args = parser.parse_args()
    if not args.db:
        print("缺少 --db 或 TAXPEARLS_DB。", file=sys.stderr)
        return 2
    load_env()
    from webapp.storage import Store
    from webapp.training_reports import backfill_bindings

    store = Store(Path(args.db), keepalive=True)
    try:
        with store.connect() as db:
            if not args.dry_run:
                db.execute("BEGIN IMMEDIATE")
            result = backfill_bindings(db, college=args.college or None, dry_run=args.dry_run)
            if not args.dry_run:
                db.execute("COMMIT")
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("预演结束，未落库。确认无误后改用 --apply。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
