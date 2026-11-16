"""Public empty v84 schema for historical migration tests only.

Derived from a real, normally bootstrapped frozen v84 Database. The SQL exports
only migration marker rows. SQLite creates its own empty FTS engine metadata;
the original runtime initializer adds the explicit synthetic formal scope. No
family data, imported owner record, current schema downgrade, or guard bypass.
"""
from pathlib import Path
import hashlib
import os
import sqlite3

from home_study_agent.runtime_data_scope import initialize_scope

_SQL_SHA256 = "08b57619749a1a97e0faf0823f296287cf29c443a35cb6434186939b08b83631"


def seed_empty_historical_v84(path: Path) -> None:
    path = Path(path)
    if path.parent.resolve(strict=True) != path.parent or not path.is_absolute():
        raise ValueError("HISTORICAL_FIXTURE_CANONICAL_PARENT_REQUIRED")
    siblings = [Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    siblings += [path.with_name("." + path.name + suffix) for suffix in
                 (".restore-publication.lock", ".restore-publication.json", ".restore-publication.last.json",
                  ".restore-work.json", ".restore-work.json.last")]
    if os.path.lexists(path) or any(os.path.lexists(p) for p in siblings):
        raise ValueError("HISTORICAL_FIXTURE_NEW_DATABASE_ONLY")
    raw = (Path(__file__).parent / "fixtures" / "historical-empty-v84.sql").read_bytes()
    if hashlib.sha256(raw).hexdigest() != _SQL_SHA256:
        raise ValueError("HISTORICAL_FIXTURE_SQL_BYTES_CHANGED")
    # Create only this absent synthetic test DB. Never open/repair an existing
    # database, remove a current v85 control table, or reset runtime authority.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        original_id = (info.st_dev, info.st_ino)
    finally:
        os.close(fd)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(raw.decode("utf-8"))
        if connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] != 84:
            raise ValueError("HISTORICAL_FIXTURE_VERSION_CHANGED")
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name IN ('restore_asset_generation','restore_asset_member')").fetchone():
            raise ValueError("HISTORICAL_FIXTURE_FUTURE_AUTHORITY_PRESENT")
        shadows = {row[1] for row in connection.execute("PRAGMA table_list") if row[2] == "shadow"}
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if table != "schema_migration" and table not in shadows:
                count = connection.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                if count != 0:
                    raise ValueError("HISTORICAL_FIXTURE_NONEMPTY_BUSINESS_TABLE")
        # An empty persisted runtime_data_scope is invalid under the unchanged
        # original guard. Use its real initializer, never fake/disable a check.
        initialize_scope(connection, "formal")
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("HISTORICAL_FIXTURE_INTEGRITY_FAILED")
    finally:
        connection.close()
    current = path.lstat()
    if (current.st_dev, current.st_ino) != original_id or current.st_nlink != 1:
        raise ValueError("HISTORICAL_FIXTURE_IDENTITY_CHANGED")
    if any(os.path.lexists(p) for p in siblings):
        raise ValueError("HISTORICAL_FIXTURE_PENDING_JOURNAL")
