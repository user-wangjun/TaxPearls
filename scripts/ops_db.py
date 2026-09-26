"""Consistent SQLite backup, verification, restore, and rollback helpers.

The commands are safe to run against a live database for *backup*: SQLite's
online backup API captures a consistent snapshot, including committed WAL
changes.  Restore must be performed while the application is stopped.  Every
restore creates a verified safety backup of the current database first, so the
same command can roll back the restore if needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "instance" / "taxpearls.db"
MANIFEST_VERSION = 1


class BackupError(RuntimeError):
    """Raised when a backup cannot be trusted or safely restored."""


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _database_path(value: str | Path | None) -> Path:
    configured = os.environ.get("TAXPEARLS_DB")
    path = Path(value or configured or DEFAULT_DB)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(backup: Path) -> Path:
    return backup.with_name(f"{backup.name}.manifest.json")


def _check_database(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BackupError(f"数据库文件不存在：{path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
            check = db.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise BackupError(f"SQLite quick_check 未通过：{check[0] if check else '无结果'}")
            schema_version = int(db.execute("PRAGMA schema_version").fetchone()[0])
            tables = [
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {name: int(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in tables}
    except sqlite3.Error as exc:
        raise BackupError(f"无法读取 SQLite 数据库：{exc}") from exc
    return {"quick_check": "ok", "schema_version": schema_version, "table_counts": counts}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_backup(database: str | Path | None = None, destination: str | Path | None = None) -> dict[str, Any]:
    source = _database_path(database)
    if not source.is_file():
        raise BackupError(f"源数据库不存在：{source}")
    if destination is None:
        target = ROOT / "backups" / f"taxpearls-{_utc_stamp()}.sqlite3"
    else:
        target = Path(destination).resolve()
        if target.exists() and target.is_dir():
            target = target / f"taxpearls-{_utc_stamp()}.sqlite3"
    if source == target:
        raise BackupError("备份目标不能与源数据库相同")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        source_uri = f"file:{source.as_posix()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_db:
            with closing(sqlite3.connect(temporary, timeout=30)) as target_db:
                source_db.backup(target_db)
        verification = _check_database(temporary)
        os.replace(temporary, target)
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "backup_file": target.name,
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
            **verification,
        }
        manifest_path = _manifest_path(target)
        _write_json_atomic(manifest_path, manifest)
        return {"backup": str(target), "manifest": str(manifest_path), **manifest}
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite 在线备份失败：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def verify_backup(backup: str | Path, manifest: str | Path | None = None) -> dict[str, Any]:
    path = Path(backup).resolve()
    manifest_path = Path(manifest).resolve() if manifest else _manifest_path(path)
    if not manifest_path.is_file():
        raise BackupError(f"缺少备份清单：{manifest_path}")
    try:
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"备份清单不可读：{exc}") from exc
    if expected.get("manifest_version") != MANIFEST_VERSION:
        raise BackupError("不支持的备份清单版本")
    if expected.get("backup_file") != path.name:
        raise BackupError("备份文件名与清单不一致")
    actual_hash = _sha256(path)
    if actual_hash != expected.get("sha256"):
        raise BackupError("备份 SHA-256 校验失败")
    if path.stat().st_size != expected.get("bytes"):
        raise BackupError("备份文件大小与清单不一致")
    verification = _check_database(path)
    if verification["schema_version"] != expected.get("schema_version"):
        raise BackupError("备份 schema_version 与清单不一致")
    if verification["table_counts"] != expected.get("table_counts"):
        raise BackupError("备份表记录数与清单不一致")
    return {"backup": str(path), "manifest": str(manifest_path), "sha256": actual_hash, **verification}


def restore_backup(
    backup: str | Path,
    database: str | Path | None = None,
    manifest: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(backup).resolve()
    verified = verify_backup(source, manifest)
    target = _database_path(database)
    if source == target:
        raise BackupError("恢复源不能与目标数据库相同")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if sidecar.exists():
            raise BackupError(f"检测到 {sidecar.name}；请先停止服务并确认 SQLite 已关闭，再执行恢复")
    target.parent.mkdir(parents=True, exist_ok=True)
    safety: dict[str, Any] | None = None
    if target.exists():
        safety_target = target.with_name(f"{target.name}.pre-restore-{_utc_stamp()}.sqlite3")
        safety = create_backup(target, safety_target)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        _check_database(temporary)
        os.replace(temporary, target)
        restored = _check_database(target)
        return {
            "database": str(target),
            "restored_from": str(source),
            "restored_sha256": verified["sha256"],
            "safety_backup": safety["backup"] if safety else None,
            "safety_manifest": safety["manifest"] if safety else None,
            **restored,
        }
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TaxPearls SQLite 备份、校验与恢复")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="使用 SQLite 在线备份 API 创建一致性快照")
    backup.add_argument("--database", help="数据库路径；默认读取 TAXPEARLS_DB")
    backup.add_argument("--output", help="备份文件或目录；默认写入 backups/")
    verify = commands.add_parser("verify", help="校验 SHA-256、SQLite 完整性与表记录数")
    verify.add_argument("backup")
    verify.add_argument("--manifest")
    restore = commands.add_parser("restore", help="停止服务后恢复；自动保留恢复前安全备份")
    restore.add_argument("backup")
    restore.add_argument("--database", help="目标数据库；默认读取 TAXPEARLS_DB")
    restore.add_argument("--manifest")
    restore.add_argument("--yes", action="store_true", help="确认执行覆盖恢复")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(args.database, args.output)
        elif args.command == "verify":
            result = verify_backup(args.backup, args.manifest)
        else:
            if not args.yes:
                parser.error("restore 会替换目标数据库，必须显式传入 --yes")
            result = restore_backup(args.backup, args.database, args.manifest)
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
