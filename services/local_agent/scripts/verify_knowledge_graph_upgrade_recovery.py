"""Verify that the latest pre-upgrade database snapshot can be upgraded in isolation."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.knowledge_graph.service import KnowledgeGraphService


def _latest_snapshot(app_db: Path) -> tuple[Path, dict]:
    backup_dir = app_db.parent / "upgrade-backups"
    metadata_paths = sorted(
        backup_dir.glob("pre-upgrade-*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for metadata_path in metadata_paths:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            snapshot = backup_dir / str(metadata["backup_file"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if snapshot.is_file():
            return snapshot, metadata
    raise RuntimeError("no readable pre-upgrade snapshot is available")


def _backup(source: Path, target: Path) -> None:
    # sqlite3 connections only manage transactions in a plain ``with`` block;
    # explicitly close them so Windows can remove the disposable replay file.
    with closing(sqlite3.connect(source)) as source_connection:
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)
            # SQLite backup carries the source journal mode.  A fresh copied WAL
            # database has no -shm sidecar yet, while Database.initialize first
            # performs a read-only schema check.  Checkpoint it to DELETE mode in
            # this disposable verification copy before that read-only check.
            target_connection.execute("PRAGMA journal_mode=DELETE")


def _schema_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0])


def _integrity(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def verify_upgrade_recovery(app_db: Path) -> dict:
    """Re-run the production snapshot upgrade on a temporary copy, never in place."""
    snapshot, metadata = _latest_snapshot(app_db)
    source_version = _schema_version(snapshot)
    source_integrity = _integrity(snapshot)
    if source_integrity != "ok":
        raise RuntimeError(f"pre-upgrade snapshot failed integrity check: {source_integrity}")
    with tempfile.TemporaryDirectory(prefix="hsc-kg-upgrade-") as temporary:
        replay_path = Path(temporary).resolve(strict=True) / "recovery-replay.db"
        _backup(snapshot, replay_path)
        database = Database(replay_path)
        database.initialize()
        KnowledgeGraphService(database).ensure_ready()
        upgraded_version = _schema_version(replay_path)
        upgraded_integrity = _integrity(replay_path)
        with database.connect() as connection:
            graph_tables = int(connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'kg_%'"
            ).fetchone()[0])
        if upgraded_version != LATEST_SCHEMA_VERSION or upgraded_integrity != "ok" or graph_tables < 10:
            raise RuntimeError("isolated upgrade replay did not produce a healthy graph-ready database")
        return {
            "passed": True,
            "production_database_written": False,
            "temporary_replay_removed_after_verification": True,
            "snapshot_source_schema_version": source_version,
            "metadata_source_schema_version": metadata.get("source_schema_version"),
            "replayed_target_schema_version": upgraded_version,
            "source_integrity": source_integrity,
            "replayed_integrity": upgraded_integrity,
            "graph_table_count": graph_tables,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(verify_upgrade_recovery(arguments.app_db), ensure_ascii=False))


if __name__ == "__main__":
    main()
