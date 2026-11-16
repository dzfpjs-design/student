"""Counterexamples against the SHA-frozen production wrapper, no patched guards."""
import sqlite3

import pytest

from home_study_agent.rubric_calibration_transaction import _OwnedConnection, _Transcript


@pytest.fixture
def captured(tmp_path):
    database = tmp_path / 'audit.db'
    raw = sqlite3.connect(database)
    raw.row_factory = sqlite3.Row
    raw.executescript("""CREATE TABLE source(id TEXT PRIMARY KEY, value);
        CREATE TABLE output(id TEXT PRIMARY KEY);
        INSERT INTO source VALUES('original', 1);""")
    c = _OwnedConnection(raw)
    c.execute('BEGIN IMMEDIATE')
    ticket = _Transcript(c)
    with c.recording(ticket):
        ticket.execute('SELECT value FROM source WHERE id=?', ('original',)).fetchall()
    c.verified(ticket)
    try:
        yield c, ticket, database
    finally:
        try:
            raw.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
            raw.rollback()
        finally:
            raw.close()


def test_control_typed_rows_reject_int_to_equal_real(captured):
    c, ticket, _ = captured
    c.execute('UPDATE source SET value=?', (1.0,)).close()
    assert not ticket.compare(c)


def test_control_trigger_and_new_transaction_replay(captured):
    c, ticket, database = captured
    c.execute("CREATE TRIGGER late AFTER INSERT ON output BEGIN UPDATE source SET value=2; END").close()
    assert ticket.compare(c)
    c.verified(ticket)
    c.execute("INSERT INTO output VALUES('first')").close()
    assert not ticket.compare(c)
    c.rollback()
    with sqlite3.connect(database) as other:
        other.execute('UPDATE source SET value=3').close()
    other.close()
    c.execute('BEGIN IMMEDIATE').close()
    assert not ticket.compare(c)


def test_returned_cursor_authorizer_replacement_cannot_hide_trigger_write(captured):
    c, ticket, _ = captured
    sql = 'INSERT INTO output VALUES(?)'
    cursor = c.execute(sql, ('first',))
    try:
        assert ticket.compare(c)
        c.verified(ticket)
        # The returned SQLite cursor publicly exposes the unprotected connection.
        cursor.connection.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
        c.execute("CREATE TRIGGER late AFTER INSERT ON output BEGIN UPDATE source SET value=2; END").close()
        c.execute(sql, ('second',)).close()
        assert c.execute('SELECT value FROM source').fetchone()[0] == 2
        assert not ticket.compare(c), 'Changed original accepted after cursor.connection replaced authorizer'
    finally:
        cursor.close()


def test_returned_cursor_trace_replacement_cannot_hide_new_transaction(captured):
    c, ticket, database = captured
    cursor = c.execute('SELECT 1')
    try:
        cursor.connection.set_trace_callback(None)
        c.rollback()
        other = sqlite3.connect(database)
        try:
            other.execute('UPDATE source SET value=2').close()
            other.commit()
        finally:
            other.close()
        c.execute('BEGIN IMMEDIATE').close()
        assert c.execute('SELECT value FROM source').fetchone()[0] == 2
        assert not ticket.compare(c), 'Changed original accepted after cursor.connection removed epoch tracking'
    finally:
        cursor.close()
