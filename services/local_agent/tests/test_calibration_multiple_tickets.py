"""Verifying one transcript must never erase another transcript's mutations."""
import sqlite3

import pytest

from home_study_agent.rubric_calibration_transaction import _OwnedConnection, _Transcript


@pytest.fixture
def owned():
    raw = sqlite3.connect(':memory:')
    raw.row_factory = sqlite3.Row
    raw.executescript('''CREATE TABLE source(id INTEGER, value TEXT);
        CREATE TABLE assessment(id INTEGER);
        INSERT INTO source VALUES(1,'original');''')
    c = _OwnedConnection(raw)
    c.execute('BEGIN IMMEDIATE')
    yield c
    raw.close()


def capture(c, sql):
    ticket = _Transcript(c)
    with c.recording(ticket):
        ticket.execute(sql).fetchall()
    c.verified(ticket)
    return ticket


@pytest.mark.parametrize('mutation', ['owned', 'trigger', 'raw-cursor', 'ddl'])
def test_second_ticket_cannot_reset_first_ticket_source_change(owned, mutation):
    c = owned
    if mutation == 'trigger':
        c.execute('''CREATE TRIGGER alter_source AFTER INSERT ON assessment BEGIN
            UPDATE source SET value='changed'; END''')
    first = capture(c, 'SELECT * FROM source')
    if mutation == 'owned':
        c.execute("UPDATE source SET value='changed'")
    elif mutation == 'trigger':
        c.execute('INSERT INTO assessment VALUES(1)')
    elif mutation == 'raw-cursor':
        c.cursor().execute("UPDATE source SET value='changed'")
    else:
        c.execute('ALTER TABLE source ADD COLUMN extra INTEGER')
    second = capture(c, 'SELECT * FROM assessment')
    assert second.compare(c)
    assert not first.compare(c)


def test_each_ticket_retains_own_generation_and_unrelated_writes_remain_cheap(owned):
    c = owned
    first = capture(c, 'SELECT * FROM source')
    second = capture(c, 'SELECT * FROM assessment')
    c.execute('INSERT INTO assessment VALUES(2)')
    assert first.compare(c)
    c.verified(first)
    assert not second.compare(c)
    traces = []
    c.set_trace_callback(traces.append)
    assert first.compare(c)
    assert traces == []


def test_savepoint_rollback_and_new_epoch_are_not_masked_by_other_ticket(owned):
    c = owned
    first = capture(c, 'SELECT * FROM source')
    c.execute('SAVEPOINT own_change')
    c.execute("UPDATE source SET value='temporary'")
    changed = capture(c, 'SELECT * FROM source')
    c.execute('ROLLBACK TO own_change')
    capture(c, 'SELECT * FROM assessment')
    assert first.compare(c)
    assert not changed.compare(c)
    c.rollback()
    c.execute('BEGIN IMMEDIATE')
    capture(c, 'SELECT * FROM assessment')
    assert c.changed_tables(first) is None
    assert first.compare(c)


def test_trigger_install_after_statement_cache_and_second_ticket_stays_visible(owned):
    c = owned
    c.execute('INSERT INTO assessment VALUES(?)', (0,))
    first = capture(c, 'SELECT * FROM source')
    c.execute('''CREATE TRIGGER change_source AFTER INSERT ON assessment BEGIN
        UPDATE source SET value='changed'; END''')
    assert first.compare(c)
    c.verified(first)
    c.execute('INSERT INTO assessment VALUES(?)', (1,))
    capture(c, 'SELECT * FROM assessment')
    assert not first.compare(c)


def test_replay_cannot_verify_if_callback_changes_an_already_read_dependency(owned):
    c = owned
    ticket = _Transcript(c)
    with c.recording(ticket):
        ticket.execute('SELECT * FROM source').fetchall()
        ticket.execute('SELECT * FROM assessment').fetchall()
    c.verified(ticket)
    c.execute('PRAGMA user_version')
    changed = []
    def trace(sql):
        if sql == 'SELECT * FROM assessment' and not changed:
            changed.append(True)
            c.execute("UPDATE source SET value='callback change'")
    c.set_trace_callback(trace)
    assert not ticket.compare(c)
    assert changed
