"""Local knowledge retrieval CLI; --offline never calls an external LLM."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from . import bundle, evaluate, fixtures, semantic_search


@contextmanager
def connect(path, *, create=False):
    path = Path(path).resolve()
    if create:
        if path.exists():
            raise ValueError("验收数据库已存在，不覆盖。")
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise ValueError("数据库不存在，请先准备知识库。")
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser('sync-knowledge', help='同步业务库中的规则、候选原文和高校仿真知识点')
    sync.add_argument('--db', type=Path, required=True)
    sync.add_argument('--manifest', type=Path, required=True)
    sync.add_argument('--originals', type=Path, required=True)
    sync.add_argument('--output', type=Path)
    for name in ("seed-demo", "build-index", "retrieve", "evaluate", "import-corpus"):
        command = commands.add_parser(name)
        command.add_argument("--db", type=Path, required=True)
        command.add_argument("--offline", action="store_true", help="本地运行；不会调用外部模型")
        command.add_argument("--model-dir", type=Path)
        command.add_argument("--output", type=Path)
        if name in {"seed-demo", "import-corpus"}:
            command.add_argument("--originals", type=Path, required=True)
        if name in {"retrieve", "evaluate"}:
            command.add_argument("--cases", type=Path, required=True)
            command.add_argument("--token-budget", type=int, default=6000)
        if name == "retrieve":
            command.add_argument("--case", required=True)
        if name == "import-corpus":
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--document-id", action="append", required=True)
    args = parser.parse_args(argv)
    if args.command == 'sync-knowledge':
        from src import settings  # noqa: F401 - load the configured local field key, never print it
        from src import policy_migration
        from src.rule_conversion import effective_rules
        from webapp.storage import Store
        from webapp.knowledge_sync import synchronize
        if not args.db.is_file():
            raise ValueError('业务库不存在；请显式选择已有业务库并先备份。')
        plan = policy_migration.prepare(args.manifest)
        store = Store(args.db)
        rules = effective_rules(store, Path(__file__).resolve().parents[2] / 'rules')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            result = synchronize(db, rules, plan, args.originals)
        if args.output:
            write(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    from webapp import policy_graph
    with connect(args.db, create=args.command == "seed-demo" or args.command == "import-corpus" and not args.db.exists()) as db:
        if args.command == "seed-demo":
            result = fixtures.seed(db, args.originals)
        elif args.command == "build-index":
            policy_graph.migrate(db)
            result = semantic_search.export_index(db, args.output or args.db.parent / "vectors", args.model_dir)
        elif args.command == "import-corpus":
            policy_graph.migrate(db)
            result = policy_graph.import_corpus(db, args.manifest, args.document_id, args.originals)
        else:
            cases = json.loads(args.cases.read_text(encoding="utf-8-sig"))["cases"]
            if args.command == "retrieve":
                case = next((c for c in cases if c["id"] == args.case), None)
                if case is None:
                    raise ValueError("案例 ID 不存在。")
                task, facts, catalog = evaluate.case_input(db, case)
                result = bundle.build(db, task, case["rule_id"], facts, topic=case.get("topic", ""),
                                      keywords=case.get("keywords", []), source_catalog=catalog, model_dir=args.model_dir,
                                      token_budget=case.get("token_budget", args.token_budget), offline=True)
            else:
                result, bundles = evaluate.evaluate(db, cases, model_dir=args.model_dir, offline=True, token_budget=args.token_budget)
                if args.output:
                    for key, item in bundles.items():
                        write(args.output.parent / "bundles" / (key + ".json"), item)
        if args.output and args.command != "build-index":
            write(args.output, result)
        compact = {key: result[key] for key in ("gate_passed", "case_count", "passed_count", "metrics", "status", "policy_count", "data_scope", "bundle_status") if key in result}
        print(json.dumps(compact or result, ensure_ascii=False))
        if args.command == "evaluate" and not result["gate_passed"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
