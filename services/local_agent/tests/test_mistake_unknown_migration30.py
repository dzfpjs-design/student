"""Synthetic v72 mistake component projected from verified empty v84 SQL.

This is not a whole historical v72 database or a current authority downgrade.
Unknown is not zero and no original business row is rewritten.
"""
import sqlite3
from contextlib import closing

import pytest

from home_study_agent import mistake_score_migration as migration
from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from test_mistake_gate_reliability import build_flow, prepare, submit
from historical_database_fixture import seed_empty_historical_v84


@pytest.fixture
def flow(tmp_path, monkeypatch):
    return build_flow(tmp_path, monkeypatch, initialize=seed_empty_historical_v84)


def legacy(flow, *, orphan=False, extra_constraint=False):
    db = flow[0]
    book, case = prepare(flow)
    submit(book, case, action='legacy-known')
    with closing(sqlite3.connect(db.path)) as c:
        c.execute('PRAGMA legacy_alter_table=ON')
        c.execute('BEGIN IMMEDIATE')
        ddl = migration.CREATE_ATTEMPT.replace('correctness REAL CHECK', 'correctness REAL NOT NULL CHECK')
        if extra_constraint:
            ddl = ddl.replace('feedback TEXT NOT NULL', 'feedback TEXT NOT NULL CHECK(length(feedback)>0)')
        c.execute(ddl)
        names = ','.join(migration.COLUMNS)
        c.execute('INSERT INTO mistake_attempt_unknown_v73(rowid,' + names + ') SELECT 7,' + names + ' FROM mistake_attempt')
        c.execute('DROP TABLE mistake_attempt')
        c.execute('ALTER TABLE mistake_attempt_unknown_v73 RENAME TO mistake_attempt')
        c.execute('DELETE FROM schema_migration WHERE version>=73')
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 72
        c.execute('CREATE TABLE audit_child (id INTEGER PRIMARY KEY, attempt_id TEXT REFERENCES mistake_attempt(attempt_id) ON DELETE CASCADE)')
        c.execute('INSERT INTO audit_child SELECT 1,attempt_id FROM mistake_attempt')
        c.execute('CREATE INDEX audit_attempt_stage ON mistake_attempt(stage)')
        c.execute('CREATE TABLE audit_log (attempt_id TEXT)')
        c.execute('CREATE TRIGGER audit_updated AFTER UPDATE ON mistake_attempt BEGIN INSERT INTO audit_log VALUES(NEW.attempt_id); END')
        c.execute('CREATE VIEW audit_view AS SELECT attempt_id,correctness FROM mistake_attempt')
        if orphan:
            c.execute("UPDATE mistake_attempt SET diagnostic_item_id='legacy-missing-source'")
        c.commit()
        rows = c.execute('SELECT rowid,* FROM mistake_attempt').fetchall()
        violations = c.execute('PRAGMA foreign_key_check').fetchall()
    return db, rows, violations


def assert_original(db, rows, violations, *, nullable):
    with db.connect() as c:
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert [tuple(r) for r in c.execute('SELECT rowid,* FROM mistake_attempt')] == rows
        assert [tuple(r) for r in c.execute('PRAGMA foreign_key_check')] == violations
        score = next(r for r in c.execute('PRAGMA table_info(mistake_attempt)') if r['name'] == 'correctness')
        assert score['notnull'] == int(not nullable)
        assert c.execute('SELECT count(*) FROM audit_child').fetchone()[0] == 1
        assert c.execute('SELECT correctness FROM audit_view').fetchone()[0] == rows[0][8]
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='audit_attempt_stage'").fetchone()
        before = c.execute('SELECT count(*) FROM audit_log').fetchone()[0]
        c.execute("UPDATE mistake_attempt SET feedback='Updated safely' WHERE rowid=7")
        assert c.execute('SELECT count(*) FROM audit_log').fetchone()[0] == before + 1
        c.rollback()


@pytest.mark.parametrize('orphan', [False, True])
def test_v72_unique_successor_preserves_rows_fk_rowid_index_view_trigger(flow, orphan):
    db, rows, violations = legacy(flow, orphan=orphan)
    assert LATEST_SCHEMA_VERSION >= 73
    db.initialize()
    assert_original(db, rows, violations, nullable=True)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM schema_migration WHERE version=73').fetchone()[0] == 1
        c.execute('UPDATE mistake_attempt SET correctness=NULL WHERE rowid=7')
        assert c.execute('SELECT correctness FROM mistake_attempt').fetchone()[0] is None
        c.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            c.execute('INSERT INTO mistake_attempt SELECT * FROM mistake_attempt')
        c.rollback()
    db.initialize()
    assert_original(db, rows, violations, nullable=True)


def test_failed_rebuild_rolls_back_and_upgrade_restore_retries_original(flow, monkeypatch):
    db, rows, violations = legacy(flow)
    with monkeypatch.context() as injected:
        def fail(*args):
            raise RuntimeError('after real mistake rebuild')
        injected.setattr(migration, '_verify_foreign_keys', fail)
        with pytest.raises(RuntimeError, match='after real mistake rebuild'):
            db.initialize()
    assert_original(db, rows, violations, nullable=False)
    assert db.latest_upgrade_status()['status'] == 'restored_after_failure'
    with db.connect() as c:
        assert c.execute('SELECT 1 FROM schema_migration WHERE version=73').fetchone() is None
    db.initialize()
    assert_original(db, rows, violations, nullable=True)


@pytest.mark.parametrize('extension', ['column', 'constraint'])
def test_unknown_legacy_extension_is_not_silently_discarded(flow, extension):
    db, _, _ = legacy(flow, extra_constraint=extension == 'constraint')
    with db.connect() as c:
        if extension == 'column':
            c.execute('ALTER TABLE mistake_attempt ADD COLUMN audit_extension TEXT')
            c.execute("UPDATE mistake_attempt SET audit_extension='preserve'")
            c.commit()
        before = [tuple(r) for r in c.execute('SELECT rowid,* FROM mistake_attempt')]
        ddl = c.execute("SELECT sql FROM sqlite_master WHERE name='mistake_attempt'").fetchone()[0]
    with pytest.raises(sqlite3.OperationalError, match='MISTAKE_MIGRATION_SCHEMA_CHANGED'):
        db.initialize()
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT rowid,* FROM mistake_attempt')] == before
        assert c.execute("SELECT sql FROM sqlite_master WHERE name='mistake_attempt'").fetchone()[0] == ddl


def test_later_extension_is_not_rebuilt_and_active_transaction_not_owned(tmp_path):
    db = Database(tmp_path / 'later.db')
    db.initialize()
    with db.connect() as c:
        c.execute('ALTER TABLE mistake_attempt ADD COLUMN audit_extension TEXT')
        c.commit()
        c.execute('BEGIN')
        with pytest.raises(sqlite3.OperationalError, match='REQUIRES_IDLE'):
            migration.migrate_unknown_mistake_scores(c)
        assert c.in_transaction
        c.rollback()
    db.initialize()
    with db.connect() as c:
        assert any(r['name'] == 'audit_extension' for r in c.execute('PRAGMA table_info(mistake_attempt)'))
