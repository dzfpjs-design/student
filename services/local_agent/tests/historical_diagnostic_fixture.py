"""Public empty v67 schema for historical migration tests only.

Derived from complete git d2da242 normally bootstrapped v67 Database. The SQL exports
only migration marker rows. SQLite creates its own empty FTS engine metadata;
the unchanged runtime interprets the unmarked source as formal. No
family data, imported owner record, current schema downgrade, or guard bypass.
"""
from pathlib import Path
import hashlib
import os
import sqlite3


_SQL_SHA256 = "da14cbc5cace1fb44295c5da6be523b05e3fbb206acd1ed5090ae7bf2d7d8ca6"


def seed_empty_historical_v67(path: Path) -> None:
    path = Path(path)
    if path.parent.resolve(strict=True) != path.parent or not path.is_absolute():
        raise ValueError("HISTORICAL_FIXTURE_CANONICAL_PARENT_REQUIRED")
    siblings = [Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    siblings += [path.with_name("." + path.name + suffix) for suffix in
                 (".restore-publication.lock", ".restore-publication.json", ".restore-publication.last.json",
                  ".restore-work.json", ".restore-work.json.last")]
    if os.path.lexists(path) or any(os.path.lexists(p) for p in siblings):
        raise ValueError("HISTORICAL_FIXTURE_NEW_DATABASE_ONLY")
    raw = (Path(__file__).parent / "fixtures" / "historical-empty-v67.sql").read_bytes()
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
        if connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] != 67:
            raise ValueError("HISTORICAL_FIXTURE_VERSION_CHANGED")
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name IN ('restore_asset_generation','restore_asset_member')").fetchone():
            raise ValueError("HISTORICAL_FIXTURE_FUTURE_AUTHORITY_PRESENT")
        shadows = {row[1] for row in connection.execute("PRAGMA table_list") if row[2] == "shadow"}
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if table != "schema_migration" and table not in shadows:
                count = connection.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                if count != 0:
                    raise ValueError("HISTORICAL_FIXTURE_NONEMPTY_BUSINESS_TABLE")
        # A real v67 source has no runtime_data_scope table. The unchanged
        # runtime reads historical unmarked DBs as formal; do not add a v79
        # table or relabel the version to manufacture a newer contract.
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_data_scope'").fetchone():
            raise ValueError("HISTORICAL_FIXTURE_FUTURE_SCOPE_PRESENT")
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


_BUSINESS_SHA256 = "efb2ac4e479e551262dd779b491c07b789b777f464db9cae57e50478f7bd9668"
_BUSINESS_TABLES = {"specialized_supply_job", "specialized_supply_candidate",
                    "curriculum_supply_selection", "curriculum_supply_package", "curriculum_supply_event"}


def seed_public_pre70_business(path: Path) -> None:
    """Actual git67 core plus actual public unversioned supply-service output.

    Never pretends these optional business tables were part of released67.
    The generated source core/ledger remain exact; no future restore/native/
    money/asset authority or modern agent tables are installed.
    """
    seed_empty_historical_v67(path)
    raw = (Path(__file__).parent / "fixtures" / "pre70-public-supply-business.sql").read_bytes()
    if hashlib.sha256(raw).hexdigest() != _BUSINESS_SHA256:
        raise ValueError("PRE70_PUBLIC_BUSINESS_BYTES_CHANGED")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        before = {row[1]: tuple(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master")}
        versions = connection.execute("SELECT * FROM schema_migration ORDER BY version").fetchall()
        connection.executescript(raw.decode("utf-8"))
        after = {row[1]: tuple(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master")}
        if any(after.get(name) != value for name, value in before.items()):
            raise ValueError("PRE70_ORIGINAL_CORE_CHANGED")
        if {row[1] for name, row in after.items() if name not in before and row[0] == "table"} != _BUSINESS_TABLES:
            raise ValueError("PRE70_UNEXPECTED_BUSINESS_TABLE")
        if connection.execute("SELECT * FROM schema_migration ORDER BY version").fetchall() != versions:
            raise ValueError("PRE70_ORIGINAL_LEDGER_CHANGED")
        if connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'restore_%' OR name LIKE 'native_%' OR name LIKE 'money_%' OR name='agent_objective'").fetchone():
            raise ValueError("PRE70_FUTURE_AUTHORITY_PRESENT")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("PRE70_PUBLIC_BUSINESS_INTEGRITY_FAILED")
    finally:
        connection.close()
