"""Original recovery HTTP must not nest its checkpointed SQL snapshots."""
from contextlib import contextmanager
from contextvars import ContextVar
import sqlite3

import pytest

from home_study_agent import parent_restore_assets as assets
from home_study_agent import restore_publication as publication
from home_study_agent.database import Database
from test_parent_owned_portable_recovery import (
    test_fresh_parent_finalize_needs_no_source_or_passphrase as _original_fault,
)


def test_original_before_publication_recovery_admission_has_one_sql_at_a_time(tmp_path, monkeypatch):
    phase = ContextVar('original_recovery_admission', default=False)
    original_admit = assets.admit_original_recovery
    original_sql = publication._sql
    observed = []

    @contextmanager
    def observe_sql(path, **kwargs):
        with original_sql(path, **kwargs) as connection:
            if phase.get():
                originals = publication._gate(publication._path(path)).sql_connections
                assert any(value is connection for value in originals)
                assert len({id(value) for value in originals}) == len(originals)
                observed.append(len(originals))
                assert len(observed) <= 1024
            yield connection

    def observe_admit(*args, **kwargs):
        token = phase.set(True)
        try:
            return original_admit(*args, **kwargs)
        finally:
            phase.reset(token)

    monkeypatch.setattr(publication, '_sql', observe_sql)
    monkeypatch.setattr(assets, 'admit_original_recovery', observe_admit)
    # Same real backup, interrupted original restore, source removal, fresh App,
    # Parent unlock/finalize/replay and fresh consumers as the canonical body.
    _original_fault(tmp_path, monkeypatch, 'before_prepare_journal')
    assert observed and max(observed) == 1, observed


def test_original_publication_owner_still_rejects_database_alias(tmp_path):
    database = Database(tmp_path / 'original.db')
    database.initialize()
    saved = database.path.with_name('original-held-preimage.db')
    foreign = database.path.with_name('foreign.db')
    other = Database(foreign)
    other.initialize()
    original_foreign = foreign.read_bytes()
    database.path.rename(saved)
    database.path.symlink_to(foreign)
    try:
        with pytest.raises(publication.RestorePublicationError, match='ADMISSION_UNCONFIRMED|DATABASE_ALIAS'):
            with publication.publication_owner(database):
                pytest.fail('aliased database received original publication authority')
        assert database.path.is_symlink() and foreign.read_bytes() == original_foreign
    finally:
        database.path.unlink()
        saved.rename(database.path)
    with database.connect() as connection:
        assert connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_original_owner_sql_foreign_key_failure_and_rollback_remain_atomic(tmp_path):
    database = Database(tmp_path / 'original.db')
    database.initialize()
    with database.connect() as connection:
        connection.execute('CREATE TABLE recovery_test_parent(id INTEGER PRIMARY KEY)')
        connection.execute('CREATE TABLE recovery_test_child(parent_id INTEGER REFERENCES recovery_test_parent(id))')
        connection.commit()
    with publication.publication_owner(database) as owner:
        with database.connect() as connection:
            assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT INTO recovery_test_parent VALUES(1)')
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute('INSERT INTO recovery_test_child VALUES(2)')
            connection.rollback()
        with publication._cold(owner.path) as connection:
            assert connection.execute('SELECT COUNT(*) FROM recovery_test_parent').fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM recovery_test_child').fetchone()[0] == 0
            assert not connection.execute('PRAGMA foreign_key_check').fetchall()
