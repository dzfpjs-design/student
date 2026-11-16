"""Owned-transaction dependency replay must not become a permission cache."""
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

import pytest

from home_study_agent.family_clock import family_today
from home_study_agent.rubric_calibration_evidence import digest
from home_study_agent.rubric_calibration_transaction import (
    _OwnedConnection, _Transcript, _SESSION, read_current, validation_session,
)


@pytest.fixture
def prepared():
    raw = sqlite3.connect(':memory:')
    raw.row_factory = sqlite3.Row
    raw.executescript('''CREATE TABLE assessment(id TEXT PRIMARY KEY, score INTEGER);
        CREATE TABLE source(id TEXT PRIMARY KEY, text TEXT);
        INSERT INTO assessment VALUES('old',3);
        INSERT INTO source VALUES('old','original');''')
    c = _OwnedConnection(raw)
    c.execute('BEGIN')
    ticket = _Transcript(c)
    with c.recording(ticket):
        ticket.execute('SELECT * FROM assessment WHERE id=?', ('old',)).fetchall()
        ticket.execute('SELECT * FROM source WHERE id=?', ('old',)).fetchall()
    c.rollback()
    c.execute('BEGIN IMMEDIATE')
    assert ticket.compare(c)
    c.verified(ticket)
    try:
        yield c, ticket
    finally:
        raw.close()


def test_new_assessment_rechecks_affected_queries_and_preserves_old_corpus(prepared):
    c, ticket = prepared
    c.execute("INSERT INTO assessment VALUES('new',2)")
    assert c.changed_tables(ticket) == {('main', 'assessment')}
    traced = []
    c.set_trace_callback(traced.append)
    assert ticket.compare(c)
    assert len(traced) == 1 and 'assessment' in traced[0]
    c.verified(ticket)
    traced.clear()
    assert ticket.compare(c) and traced == []


def test_same_sql_after_trigger_install_cannot_hide_trigger_writes(prepared):
    c, ticket = prepared
    c.execute('INSERT INTO assessment VALUES(?,?)', ('new', 2))
    c.execute('''CREATE TRIGGER change_source AFTER INSERT ON assessment BEGIN
        UPDATE source SET text='changed' WHERE id='old'; END''')
    assert ticket.compare(c)
    c.verified(ticket)
    c.execute('INSERT INTO assessment VALUES(?,?)', ('another', 1))
    assert c.changed_tables(ticket) == {('main', 'assessment'), ('main', 'source')}
    assert not ticket.compare(c)


def test_raw_cursor_and_rollback_force_full_revalidation(prepared):
    c, ticket = prepared
    c.cursor().execute("UPDATE source SET text='raw change' WHERE id='old'")
    assert c.changed_tables(ticket) is None
    assert not ticket.compare(c)
    c.rollback()
    c.execute('BEGIN IMMEDIATE')
    assert c.changed_tables(ticket) is None
    traced = []
    c.set_trace_callback(traced.append)
    assert ticket.compare(c) and len(traced) == 2


def test_trace_consumer_cannot_remove_transaction_epoch_tracking(prepared):
    c, ticket = prepared
    traces = []
    c.set_trace_callback(traces.append)
    c.connection.commit()
    c.connection.execute('BEGIN IMMEDIATE')
    assert traces == ['COMMIT', 'BEGIN IMMEDIATE']
    assert c.changed_tables(ticket) is None
    assert ticket.compare(c)
    c.verified(ticket)
    c.set_trace_callback(None)
    c.connection.rollback()
    c.connection.execute('BEGIN IMMEDIATE')
    assert c.changed_tables(ticket) is None


def test_custom_authorizer_disables_reuse_without_changing_sqlite_behavior(prepared):
    c, ticket = prepared
    c.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
    assert not c.reusable and c.changed_tables(ticket) is None
    with pytest.raises(sqlite3.DatabaseError, match='authorized'):
        c.execute('SELECT 123')


def test_commented_transaction_boundaries_survive_statement_metadata_eviction(prepared):
    c, ticket = prepared
    for iteration in range(2):
        c.execute('/* boundary */ COMMIT')
        c.execute('/* boundary */ BEGIN IMMEDIATE')
        assert c.changed_tables(ticket) is None
        assert ticket.compare(c)
        c.verified(ticket)
        if iteration == 0:
            c.execute('PRAGMA data_version').fetchone()
            assert ticket.compare(c)
            c.verified(ticket)


def test_unrelated_read_function_does_not_invalidate_database_dependencies(prepared):
    c, ticket = prepared
    assert c.execute("SELECT lower('ABC')").fetchone()[0] == 'abc'
    assert c.changed_tables(ticket) == set()


def test_sql_storage_type_change_invalidates_even_when_numeric_values_compare_equal(prepared):
    c, _ = prepared
    c.execute('CREATE TABLE typed(value)')
    c.execute('INSERT INTO typed VALUES(?)', (1,))
    ticket = _Transcript(c)
    with c.recording(ticket):
        ticket.execute('SELECT value FROM typed').fetchall()
    c.verified(ticket)
    c.execute('UPDATE typed SET value=?', (1.0,))
    assert type(c.execute('SELECT value FROM typed').fetchone()[0]) is float
    assert not ticket.compare(c)


def test_non_live_reader_keeps_expired_status_instead_of_throwing(prepared):
    c, ticket = prepared
    now = datetime.now(timezone.utc)
    row = {'child_id': 'child', 'protocol_id': 'protocol'}
    ticket.now, ticket.day = now - timedelta(seconds=1), family_today()
    ticket.scope, ticket.math, ticket.math_size = {}, set(), 0
    ticket.result = {'score_qualified': True}
    service = SimpleNamespace(authority_resolver=None, _live=lambda *args, **kwargs: ['protocol_expired'])
    with validation_session():
        _SESSION.get()[digest(row)] = ticket
        invalidated = {'status': 'invalidated', 'score_qualified': False}
        assert read_current(service, c, row, now, lambda connection: invalidated) is invalidated


def test_custom_resolver_never_uses_prepared_authority(prepared):
    c, ticket = prepared
    row = {'child_id': 'child', 'protocol_id': 'protocol'}
    service = SimpleNamespace(authority_resolver=lambda *args: None)
    with validation_session():
        _SESSION.get()[digest(row)] = ticket
        assert read_current(service, c, row, datetime.now(timezone.utc), lambda connection: 'full') == 'full'


def test_bounded_recording_still_returns_live_rows(prepared):
    c, _ = prepared
    transcript = _Transcript(c)
    transcript.size = 16_000_000
    with c.recording(transcript):
        assert transcript.execute('SELECT text FROM source').fetchone()[0] == 'original'
    assert not transcript.supported and not transcript.reads


def test_named_parameters_fall_back_without_changing_results(prepared):
    c, _ = prepared
    transcript = _Transcript(c)
    with c.recording(transcript):
        assert transcript.execute('SELECT text FROM source WHERE id=:id', {'id': 'old'}).fetchone()[0] == 'original'
    assert not transcript.supported


def test_late_user_function_registration_disables_reuse(prepared):
    c, ticket = prepared
    c.create_function('json_extract', 2, lambda *args: 'custom value')
    assert c.execute("SELECT json_extract('{}','$.x')").fetchone()[0] == 'custom value'
    assert not c.reusable and c.changed_tables(ticket) is None


def test_read_scope_cannot_record_a_write(prepared):
    c, _ = prepared
    transcript = _Transcript(c)
    with c.recording(transcript):
        with pytest.raises(sqlite3.DatabaseError, match='authorized'):
            transcript.execute("UPDATE source SET text='not permitted'")
    assert c.execute('SELECT text FROM source').fetchone()[0] == 'original'


def test_nested_session_clears_all_tickets_at_outer_exit():
    with validation_session():
        _SESSION.get()['sentinel'] = object()
        with validation_session():
            assert 'sentinel' in _SESSION.get()
        assert 'sentinel' in _SESSION.get()
    assert _SESSION.get() is None


def test_cursor_iteration_fetchmany_and_execution_preserve_sqlite_behavior(prepared):
    c, ticket = prepared
    cursor = c.cursor()
    try:
        assert cursor.execute('SELECT id FROM source UNION ALL SELECT id FROM assessment') is cursor
        cursor.arraysize = 1
        assert len(cursor.fetchmany()) == 1
        assert len(list(cursor)) == 1
        assert cursor.executemany('INSERT INTO assessment VALUES(?,?)', [('a', 1), ('b', 2)]) is cursor
        assert cursor.rowcount == 2
        assert ticket.compare(c)
    finally:
        cursor.close()


def test_exposed_native_connection_disables_prepared_result_forever(prepared):
    c, ticket = prepared
    cursor = c.execute('SELECT 1')
    try:
        assert cursor.connection is c.connection
        c.verified(ticket)
        assert not c.reusable and c.changed_tables(ticket) is None
        row = {'child_id': 'child', 'protocol_id': 'protocol'}
        with validation_session():
            _SESSION.get()[digest(row)] = ticket
            assert read_current(SimpleNamespace(), c, row, None, lambda connection: 'full') == 'full'
    finally:
        cursor.close()


def test_cursor_reads_outside_transcript_execute_disable_capture(prepared):
    c, _ = prepared
    transcript = _Transcript(c)
    with c.recording(transcript):
        cursor = transcript.cursor()
        try:
            assert cursor.execute('SELECT text FROM source').fetchone()[0] == 'original'
        finally:
            cursor.close()
    assert not transcript.supported


def test_connection_factory_settings_preserve_sqlite_behavior_and_disable_reuse(prepared):
    c, ticket = prepared
    c.row_factory = None
    assert c.execute('SELECT 1').fetchone() == (1,)
    c.text_factory = bytes
    assert c.execute("SELECT 'text'").fetchone() == (b'text',)
    c.isolation_level = None
    assert not c.in_transaction
    assert not c.reusable and c.changed_tables(ticket) is None


def test_custom_cursor_factory_cannot_hide_raw_callback_replacement(prepared):
    c, ticket = prepared
    leaked = []

    def factory(raw):
        leaked.append(raw)
        return sqlite3.Cursor(raw)

    cursor = c.cursor(factory=factory)
    try:
        raw = leaked[0]
        raw.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        raw.set_trace_callback(None)
        raw.execute("UPDATE source SET text='changed original'")
        assert not c.reusable and not ticket.compare(c)
    finally:
        cursor.close()
