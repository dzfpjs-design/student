"""Runtime's original transaction adapter grants only a live scoped read view."""
import os
import sqlite3
from threading import Thread

import pytest

from home_study_agent.agent_runtime import _RuntimeTransactionDatabase
from home_study_agent.database import Database
from home_study_agent import original_connection_read as reads
from home_study_agent import restore_publication as pub
from home_study_agent.rubric_calibration_transaction import calibration_connection, validation_session
from home_study_agent.standard_relocation_provenance import business_snapshot


def prepared(tmp_path):
    db = Database(tmp_path / 'public.db'); db.initialize()
    with db.connect() as c:
        c.execute('CREATE TABLE runtime_view_probe(value TEXT)')
        c.execute("INSERT INTO runtime_view_probe VALUES('original')")
        c.commit()
    return db


def test_nested_runtime_and_calibration_views_preserve_original_atomic_write(tmp_path):
    db = prepared(tmp_path)
    with db.connect() as raw:
        raw.execute('BEGIN IMMEDIATE')
        raw.execute("UPDATE runtime_view_probe SET value='uncommitted'")
        adapter = _RuntimeTransactionDatabase(raw)
        with adapter.connect() as first:
            assert type(first) is _RuntimeTransactionDatabase and first is not adapter
            with first.connect() as second:
                assert second is not first and reads.original_connection(second) is raw
                for view in (first, second):
                    assert view.execute('BEGIN') is None
                    assert view.execute('BEGIN IMMEDIATE') is None
                    view.commit(); view.rollback()
                    assert raw.in_transaction
                with validation_session(), calibration_connection(second) as owned:
                    assert reads.original_connection(owned) is raw
                    with business_snapshot(owned) as same:
                        assert same is raw
                        assert same.execute('SELECT value FROM runtime_view_probe').fetchone()[0] == 'uncommitted'
                    owned.commit(); owned.rollback()
                    assert raw.in_transaction
                with pytest.raises(pub.RestorePublicationError):reads.original_connection(owned)
            with pytest.raises(pub.RestorePublicationError):reads.original_connection(second)
            assert reads.original_connection(first) is raw
        with pytest.raises(pub.RestorePublicationError):reads.original_connection(first)
        assert raw.in_transaction
        raw.rollback()
        assert raw.execute('SELECT value FROM runtime_view_probe').fetchone()[0] == 'original'
    assert not db._original_connections


def test_runtime_constructor_or_unknown_object_is_not_a_read_capability(tmp_path):
    db = prepared(tmp_path)
    with db.connect() as raw:
        unissued = _RuntimeTransactionDatabase(raw)
        with pytest.raises(pub.RestorePublicationError):reads.original_connection(unissued)
        with pytest.raises(pub.RestorePublicationError):
            with reads.original_connection_view(unissued, raw):pytest.fail('foreign issuer')
        class Unknown:
            @property
            def connection(self):pytest.fail('generic getter must not be evaluated')
        with pytest.raises(pub.RestorePublicationError):reads.original_connection(Unknown())


def test_runtime_view_checks_actor_pid_and_original_target_before_sql(tmp_path, monkeypatch):
    db = prepared(tmp_path)
    other = Database(tmp_path / 'other.db'); other.initialize()
    with db.connect() as raw, other.connect() as foreign:
        with _RuntimeTransactionDatabase(raw).connect() as view:
            errors = []
            def wrong_actor():
                try:reads.original_connection(view)
                except pub.RestorePublicationError as error:errors.append(str(error))
            thread = Thread(target=wrong_actor); thread.start(); thread.join()
            assert errors == [reads.CODE]
            actual_pid = os.getpid()
            with monkeypatch.context() as patch:
                patch.setattr(reads.os, 'getpid', lambda: actual_pid + 1)
                with pytest.raises(pub.RestorePublicationError):reads.original_connection(view)
            view.connection = foreign
            try:
                with pytest.raises(pub.RestorePublicationError):reads.original_connection(view)
            finally:view.connection = raw
            assert reads.original_connection(view) is raw


def test_runtime_view_expiry_and_original_exception_are_not_hidden(tmp_path):
    db = prepared(tmp_path); error = RuntimeError('synthetic original business failure')
    with db.connect() as raw:
        with pytest.raises(RuntimeError) as caught:
            with _RuntimeTransactionDatabase(raw).connect() as view:
                raw.execute('BEGIN')
                raise error
        assert caught.value is error and raw.in_transaction
        with pytest.raises(pub.RestorePublicationError):reads.original_connection(view)
        raw.rollback()
    with pytest.raises(pub.RestorePublicationError):reads.original_connection(view)


@pytest.mark.parametrize('operation', ['rollback', 'commit'])
def test_runtime_view_cannot_hide_no_dml_transaction_replacement(tmp_path, operation):
    db = prepared(tmp_path)
    with db.connect() as raw:
        raw.execute('BEGIN')
        with pytest.raises(pub.RestorePublicationError, match='RESTORE_PUBLICATION_SNAPSHOT_UNCONFIRMED'):
            with _RuntimeTransactionDatabase(raw).connect() as view:
                with business_snapshot(view) as same:
                    assert same is raw
                    changes = raw.total_changes
                    getattr(sqlite3.Connection, operation)(raw)
                    sqlite3.Connection.execute(raw, 'BEGIN')
                    assert raw.total_changes == changes
        assert pub._gate(pub._path(db.path)).poisoned
    assert pub._gate(pub._path(db.path)).retained and not db._original_connections


def test_recording_nested_runtime_target_change_cannot_touch_foreign_authorizer(tmp_path):
    from home_study_agent.rubric_calibration_transaction import _Transcript
    db = prepared(tmp_path)
    other = Database(tmp_path / 'foreign-policy.db'); other.initialize()
    original_error = RuntimeError('synthetic original recording failure')
    with other.connect() as foreign:
        foreign.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
        with db.connect() as raw:
            raw.execute('BEGIN')
            with validation_session():
                with pytest.raises(RuntimeError) as caught:
                    with calibration_connection(_RuntimeTransactionDatabase(raw)) as owned:
                        runtime_view = object.__getattribute__(owned, '_connection')
                        assert type(runtime_view) is _RuntimeTransactionDatabase
                        try:
                            with owned.recording(_Transcript(owned)):
                                runtime_view.connection = foreign
                                raise original_error
                        finally:
                            assert owned.observer is None
                            # Original test cleanup restores only a known field;
                            # the original poisoned lease is never cleared.
                            runtime_view.connection = raw
                assert caught.value is original_error
                assert pub._gate(pub._path(db.path)).poisoned
        assert pub._gate(pub._path(db.path)).retained
        # This is a real SQLite authorizer on a second admitted synthetic DB.
        # It must stay DENY even if the original recording chain was changed.
        with pytest.raises(sqlite3.DatabaseError, match='authorized'):
            foreign.execute('SELECT 1')


def test_nested_runtime_recording_keeps_select_only_and_original_writer_transaction(tmp_path):
    from home_study_agent.rubric_calibration_transaction import _Transcript
    db = prepared(tmp_path)
    with db.connect() as raw:
        raw.execute('BEGIN IMMEDIATE')
        with validation_session(), calibration_connection(_RuntimeTransactionDatabase(raw)) as owned:
            with owned.recording(_Transcript(owned)) as ignored:
                with pytest.raises(sqlite3.DatabaseError, match='authorized'):
                    owned.execute("UPDATE runtime_view_probe SET value='not allowed in capture'")
            assert owned.observer is None and raw.in_transaction
            raw.execute("UPDATE runtime_view_probe SET value='original writer'")
            assert raw.execute('SELECT value FROM runtime_view_probe').fetchone()[0] == 'original writer'
        assert raw.in_transaction
        raw.rollback()
        assert raw.execute('SELECT value FROM runtime_view_probe').fetchone()[0] == 'original'


def test_legacy_closed_sqlite_setup_error_still_retires_original_observer():
    from home_study_agent.rubric_calibration_transaction import _OwnedConnection, _Transcript
    raw = sqlite3.connect(':memory:')
    owned = _OwnedConnection(raw)
    raw.close()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        with owned.recording(_Transcript(owned)):
            pytest.fail('closed original SQL entered body')
    assert owned.observer is None
    # No admitted production read lease/native cleanup credit is minted here.
    assert not reads.registered_view(owned)
