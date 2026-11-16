"""Persisted storage identity, independent of the process requesting a restore."""

import sqlite3


class RuntimeDataScopeError(RuntimeError):
    pass


def read_scope(connection):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_data_scope'").fetchone():
        return 'formal'  # Historical unmarked family databases are never trial evidence.
    rows = connection.execute('SELECT mode FROM runtime_data_scope').fetchall()
    if len(rows) != 1 or rows[0][0] not in {'formal', 'trial'}:
        raise RuntimeDataScopeError('RUNTIME_DATA_SCOPE_INVALID')
    return rows[0][0]


def assert_scope(connection, expected):
    if read_scope(connection) != expected:
        raise RuntimeDataScopeError('RUNTIME_DATA_SCOPE_CONFLICT')


def initialize_scope(connection, mode):
    connection.execute("""CREATE TABLE IF NOT EXISTS runtime_data_scope (
      singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
      mode TEXT NOT NULL CHECK(mode IN ('formal','trial')),
      created_at TEXT NOT NULL)""")
    row = connection.execute('SELECT mode FROM runtime_data_scope WHERE singleton_id=1').fetchone()
    if row and row[0] != mode:
        raise RuntimeDataScopeError('RUNTIME_DATA_SCOPE_CONFLICT')
    if not row:
        connection.execute("INSERT INTO runtime_data_scope VALUES (1,?,datetime('now'))", (mode,))
    connection.execute("""CREATE TRIGGER IF NOT EXISTS runtime_data_scope_no_replace
      BEFORE INSERT ON runtime_data_scope WHEN EXISTS(SELECT 1 FROM runtime_data_scope)
      BEGIN SELECT RAISE(ABORT,'runtime scope is immutable'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS runtime_data_scope_no_update
      BEFORE UPDATE ON runtime_data_scope BEGIN SELECT RAISE(ABORT,'runtime scope is immutable'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS runtime_data_scope_no_delete
      BEFORE DELETE ON runtime_data_scope BEGIN SELECT RAISE(ABORT,'runtime scope is immutable'); END""")


def check_existing_path(path, mode):
    if not path.exists() or path.stat().st_size == 0:
        return
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        # Empty databases may be created by bootstrapping before schema initialization.
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migration'").fetchone():
            assert_scope(connection, mode)
    finally:
        connection.close()
