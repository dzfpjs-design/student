"""Real audited cursor reads must preserve ownership without weakening rejection."""
import sqlite3

import pytest

from home_study_agent.daily_behavior_source import _with_behavior_source_batch
from home_study_agent.document_revisit_memory import _NamedRows, report_memory
from home_study_agent.rubric_calibration_transaction import calibration_connection, validation_session
from test_behavior_combined_source_scope import prepare, CHILD, START, END
from test_behavior_observation_coverage import managed


def test_revisit_reads_preserve_real_owned_behavior_transaction(managed):
    db = prepare(managed)
    with validation_session(), calibration_connection(db) as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        epoch, changes = c.epoch, c.total_changes
        def consume(daily, period):
            before = (daily(c, CHILD, 'session'), period(c, CHILD, START, END))
            first = report_memory(c, CHILD, 'session')
            assert report_memory(c, CHILD, 'session') == first
            assert c.reusable and c.epoch == epoch
            assert c.total_changes == changes
            return before, (daily(c, CHILD, 'session'), period(c, CHILD, START, END))
        before, after = _with_behavior_source_batch(c, CHILD, consume)
        assert before == after
        assert c.reusable and c.in_transaction and c.epoch == epoch
        assert c.total_changes == changes


@pytest.mark.parametrize('change', ['cursor_factory', 'raw_owner', 'epoch'])
def test_actual_cursor_owner_or_epoch_change_still_rejected(managed, change):
    db = prepare(managed)
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN')
        epoch = c.epoch
        def consume(daily, period):
            first = period(c, CHILD, START, END)
            if change == 'cursor_factory':
                cursor = c.cursor()
                try:
                    cursor.row_factory = None
                    assert type(cursor.execute('SELECT 1 AS n').fetchone()) is tuple
                    assert not c.reusable
                finally:
                    cursor.close()
            elif change == 'raw_owner':
                assert isinstance(c.connection, sqlite3.Connection)
                assert not c.reusable
            else:
                c.execute('SAVEPOINT changed')
                c.execute('RELEASE SAVEPOINT changed')
                assert c.reusable and c.in_transaction and c.epoch != epoch
            return first, daily(c, CHILD, 'session')
        with pytest.raises(RuntimeError, match='BEHAVIOR_READ_TRANSACTION_CHANGED'):
            _with_behavior_source_batch(c, CHILD, consume)


def test_plain_tuple_cursor_still_returns_named_sqlite_rows(managed):
    db = prepare(managed)
    with db.connect() as c:
        c.row_factory = None
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        changes = c.total_changes
        cursor = _NamedRows(c).execute('SELECT 7 AS answer')
        try:
            row = cursor.fetchone()
            assert isinstance(row, sqlite3.Row) and row['answer'] == 7
        finally:
            cursor.close()
        assert c.row_factory is None and c.in_transaction
        assert report_memory(c, CHILD, 'session')['items'] == []
        assert c.total_changes == changes
