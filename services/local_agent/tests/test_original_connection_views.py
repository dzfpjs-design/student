"""Production SQL views retain original leases without becoming transaction owners."""
import asyncio
from datetime import datetime, timezone
import sqlite3
from threading import Thread
from types import SimpleNamespace

import pytest

from home_study_agent.database import Database, StudyConnection
from home_study_agent import original_connection_read as reads
from home_study_agent import restore_publication as pub
from home_study_agent.diagnostic_prompt import _BorrowedConnection, _TransactionDatabase
from home_study_agent.exam_simulation import _ExamDatabase
from home_study_agent.rubric_calibration_rows import ReadRows
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent import rubric_calibration_transaction as tx
from home_study_agent.standard_relocation_provenance import business_snapshot, snapshot_read


def database(tmp_path):
    db = Database(tmp_path / 'public.db')
    db.initialize()
    with db.connect() as c:
        c.execute('CREATE TABLE wrapper_probe(value TEXT)')
        c.execute("INSERT INTO wrapper_probe VALUES('original')")
        c.commit()
    return db


def test_original_views_share_one_connection_and_retire_in_factory_order(tmp_path):
    db = database(tmp_path)
    with tx.validation_session(), tx.calibration_connection(db) as owned:
        raw = object.__getattribute__(owned, '_connection')
        owned.execute('BEGIN IMMEDIATE')
        owned.execute("UPDATE wrapper_probe SET value='uncommitted'")
        assert owned.reusable
        with _TransactionDatabase(owned).connect() as borrowed:
            with _ExamDatabase(borrowed).connect() as exam:
                with ReadRows(exam) as rows:
                    assert reads.original_connection(rows) is raw
                    with business_snapshot(rows) as original:
                        assert original is raw
                        assert raw.in_transaction
                with pytest.raises(pub.RestorePublicationError):
                    reads.original_connection(rows)
            with pytest.raises(pub.RestorePublicationError):
                reads.original_connection(exam)
        with pytest.raises(pub.RestorePublicationError):
            reads.original_connection(borrowed)
        assert raw.in_transaction and owned.reusable
        owned.rollback()
    with pytest.raises(pub.RestorePublicationError):
        reads.original_connection(owned)
    assert not db._original_connections


def test_forged_views_and_side_effecting_duck_getters_receive_no_capability(tmp_path):
    db = database(tmp_path)
    with db.connect() as raw:
        fake = _BorrowedConnection(raw)
        fake._original_read_locator = raw._original_read_locator
        with pytest.raises(pub.RestorePublicationError):
            reads.original_connection(fake)
        with pytest.raises(pub.RestorePublicationError):
            with reads.original_connection_view(fake, raw):
                pytest.fail('foreign issuer accepted')
        class Duck:
            @property
            def connection(self):
                pytest.fail('generic property unwrap')
        with pytest.raises(pub.RestorePublicationError):
            reads.original_connection(Duck())


def test_view_rejects_changed_chain_and_cross_actor_before_original_sql(tmp_path):
    db = database(tmp_path)
    with db.connect() as first, db.connect() as other:
        with _TransactionDatabase(first).connect() as view:
            view.connection = other
            try:
                with pytest.raises(pub.RestorePublicationError):
                    reads.original_connection(view)
            finally:
                view.connection = first
            errors = []
            def foreign():
                try:
                    reads.original_connection(view)
                except pub.RestorePublicationError as error:
                    errors.append(str(error))
            thread = Thread(target=foreign)
            thread.start(); thread.join()
            assert errors == [reads.CODE]
            assert reads.original_connection(view) is first


def test_readonly_capture_unwinds_before_full_source_read_without_repeating_body(tmp_path, monkeypatch):
    db = database(tmp_path)
    observed = []
    @snapshot_read
    def source(connection):
        assert type(connection) is StudyConnection
        assert tx._CAPTURE.get() is None
        assert owned.observer is None
        observed.append('body')
        return connection.execute('SELECT COUNT(*) AS n FROM wrapper_probe').fetchone()['n']
    service = RubricCalibrationService(db)
    def read_rows(connection, row, now, **kwargs):
        observed.append('read')
        return {'score_qualified': True, 'n': source(connection)}
    monkeypatch.setattr(service, '_read_rows', read_rows)
    with tx.validation_session(), tx.calibration_connection(db) as owned:
        owned.execute('BEGIN IMMEDIATE')
        owned.execute("UPDATE wrapper_probe SET value='pending'")
        raw = object.__getattribute__(owned, '_connection')
        changes = raw.total_changes
        first = service._read(owned,
            {'child_id':'child-primary-5','protocol_id':'synthetic',
                             'scorer_json':'{"modality":"typed_text"}'}, datetime.now(timezone.utc))
        assert first['n'] == 1 and first['score_qualified']
        assert observed == ['read', 'read', 'body']
        assert not tx._SESSION.get() and tx._CAPTURE.get() is None and owned.observer is None
        assert raw.in_transaction and raw.total_changes == changes
        assert owned.reusable
        # The original SELECT-only observer is restored normally; writes were
        # never granted during capture. The caller still owns its transaction.
        owned.rollback()


@pytest.mark.parametrize('error', [RuntimeError('original programming failure'), asyncio.CancelledError()])
def test_non_source_error_is_not_retried_and_original_capture_always_retires(tmp_path, error):
    db = database(tmp_path)
    calls = []
    def fail(connection):
        calls.append(connection)
        raise error
    with tx.validation_session(), tx.calibration_connection(db) as owned:
        owned.execute('BEGIN')
        with pytest.raises(type(error)) as raised:
            tx.read_current(SimpleNamespace(authority_resolver=None), owned,
                {'child_id':'child-primary-5','protocol_id':'synthetic'}, datetime.now(timezone.utc), fail)
        assert raised.value is error and len(calls) == 1
        assert tx._CAPTURE.get() is None and owned.observer is None
        assert owned.in_transaction
        owned.rollback()


def test_unenrolled_legacy_sqlite_rows_are_not_upgraded_or_blocked():
    raw = sqlite3.connect(':memory:', factory=StudyConnection)
    try:
        raw.execute('BEGIN')
        with ReadRows(raw) as rows:
            assert rows.execute('SELECT 1').fetchone()[0] == 1
            assert not reads.registered_view(rows)
            with pytest.raises(pub.RestorePublicationError):
                reads.original_connection(rows)
        assert raw.in_transaction
    finally:
        raw.close()


@pytest.mark.parametrize('operation', ['rollback', 'commit'])
def test_no_dml_transaction_replacement_through_live_borrowed_view_is_rejected(tmp_path, operation):
    db = database(tmp_path)
    with db.connect() as raw:
        raw.execute('BEGIN')
        with pytest.raises(pub.RestorePublicationError, match='RESTORE_PUBLICATION_SNAPSHOT_UNCONFIRMED'):
            with _TransactionDatabase(raw).connect() as view:
                with business_snapshot(view) as original:
                    assert original is raw
                    changes = raw.total_changes
                    getattr(sqlite3.Connection, operation)(raw)
                    sqlite3.Connection.execute(raw, 'BEGIN')
                    assert raw.in_transaction and raw.total_changes == changes
        assert pub._gate(pub._path(db.path)).poisoned
    assert pub._gate(pub._path(db.path)).retained
    assert not db._original_connections


def test_foreign_read_callback_cannot_use_source_signal_as_retry_authority(tmp_path):
    db = database(tmp_path)
    calls = []
    def not_original(connection):
        calls.append(connection)
        raise reads._UnrecordedSourceRead()
    with tx.validation_session(), tx.calibration_connection(db) as owned:
        owned.execute('BEGIN')
        with pytest.raises(ValueError, match='READ_SCOPE_CHANGED'):
            tx.read_current(SimpleNamespace(authority_resolver=None), owned,
                {'child_id':'child-primary-5','protocol_id':'synthetic'}, datetime.now(timezone.utc), not_original)
        assert len(calls) == 1 and owned.observer is None and tx._CAPTURE.get() is None
        assert owned.in_transaction
        owned.rollback()


def test_changed_owned_target_restores_only_original_authorizer_and_blocks_fallback(tmp_path, monkeypatch):
    db = database(tmp_path)
    foreign_db = Database(tmp_path / 'foreign.db'); foreign_db.initialize()
    with foreign_db.connect() as foreign:
        foreign.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
        service = RubricCalibrationService(db)
        calls = []
        def changed(rows, row, now, **kwargs):
            calls.append(rows)
            object.__setattr__(owned, '_connection', foreign)
            raise reads._UnrecordedSourceRead()
        monkeypatch.setattr(service, '_read_rows', changed)
        with tx.validation_session():
            with pytest.raises(pub.RestorePublicationError, match=reads.CODE):
                with tx.calibration_connection(db) as owned:
                    original = object.__getattribute__(owned, '_connection')
                    owned.execute('BEGIN')
                    try:
                        service._read(owned,
                            {'child_id':'child-primary-5','protocol_id':'synthetic',
                             'scorer_json':'{"modality":"typed_text"}'}, datetime.now(timezone.utc))
                    finally:
                        assert owned.observer is None and tx._CAPTURE.get() is None
                        # Original cleanup is allowed to retire the original
                        # registered chain; it never repairs the poisoned lease.
                        object.__setattr__(owned, '_connection', original)
            assert len(calls) == 1
            assert pub._gate(pub._path(db.path)).poisoned
            assert pub._gate(pub._path(db.path)).retained
        with pytest.raises(sqlite3.DatabaseError, match='authorized'):
            foreign.execute('SELECT 1')
