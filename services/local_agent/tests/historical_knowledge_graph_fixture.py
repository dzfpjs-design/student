"""Actual git8c07c69 public fresh66 schema; no current authority is deleted.

Normal original bootstrap produced only migration markers, one clean/unowned
runtime_state and one kg_schema_migration row; every business table is empty.
This does not claim v47 or imported family history compatibility.
"""
from pathlib import Path
import hashlib
import os
import sqlite3

_SQL_SHA256 = '1589fd8bf72dfd55467b994957f07243de60983725e7352b383cbbadd8823133'


def seed_public_historical_v66(path: Path) -> None:
    path = Path(path)
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise ValueError('HISTORICAL_KG_CANONICAL_PARENT_REQUIRED')
    siblings = [Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')]
    siblings += [path.with_name('.' + path.name + suffix) for suffix in
                 ('.restore-publication.lock', '.restore-publication.json', '.restore-publication.last.json',
                  '.restore-work.json', '.restore-work.json.last')]
    if os.path.lexists(path) or any(os.path.lexists(p) for p in siblings):
        raise ValueError('HISTORICAL_KG_NEW_DATABASE_ONLY')
    raw = (Path(__file__).parent / 'fixtures' / 'historical-public-v66.sql').read_bytes()
    if hashlib.sha256(raw).hexdigest() != _SQL_SHA256:
        raise ValueError('HISTORICAL_KG_SQL_BYTES_CHANGED')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        original_id = (info.st_dev, info.st_ino)
    finally:
        os.close(fd)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(raw.decode('utf-8'))
        if connection.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] != 66:
            raise ValueError('HISTORICAL_KG_VERSION_CHANGED')
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'restore_%' OR name LIKE 'native_%' OR name LIKE 'money_%' OR name='runtime_data_scope'").fetchone():
            raise ValueError('HISTORICAL_KG_FUTURE_AUTHORITY_PRESENT')
        if connection.execute('SELECT * FROM runtime_state').fetchall() != [(1, 1, None, None, None)]:
            raise ValueError('HISTORICAL_KG_ORIGINAL_RUNTIME_ROW_CHANGED')
        if connection.execute('SELECT version FROM kg_schema_migration').fetchall() != [(18,)]:
            raise ValueError('HISTORICAL_KG_ORIGINAL_GRAPH_VERSION_CHANGED')
        shadows = {row[1] for row in connection.execute('PRAGMA table_list') if row[2] == 'shadow'}
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if table not in shadows | {'schema_migration', 'runtime_state', 'kg_schema_migration'}:
                if connection.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]:
                    raise ValueError('HISTORICAL_KG_BUSINESS_ROWS_PRESENT')
        if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)] or connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('HISTORICAL_KG_INTEGRITY_FAILED')
    finally:
        connection.close()
    current = path.lstat()
    if (current.st_dev, current.st_ino) != original_id or current.st_nlink != 1:
        raise ValueError('HISTORICAL_KG_IDENTITY_CHANGED')
    if any(os.path.lexists(p) for p in siblings):
        raise ValueError('HISTORICAL_KG_PENDING_JOURNAL')
