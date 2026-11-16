"""Report reads must not disable owned calibration proofs via redundant setters."""
import sqlite3
from contextlib import contextmanager
import asyncio

import pytest

from home_study_agent.behavior_model.report_dependencies import rows
from home_study_agent.daily_behavior_source import daily_behavior_fingerprint
from home_study_agent.rubric_calibration_transaction import calibration_connection, validation_session
from home_study_agent.report_source_reads import report_source_connection
from test_behavior_combined_source_scope import prepare, CHILD
from test_behavior_observation_coverage import managed


@pytest.mark.parametrize('reader', ['rows', 'daily'])
def test_report_read_preserves_owned_native_row_factory(managed, reader):
    db = prepare(managed)
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN')
        assert c.reusable and c.row_factory is sqlite3.Row
        if reader == 'rows':
            assert rows(c, 'SELECT 1 AS n') == [{'n': 1}]
        else:
            assert daily_behavior_fingerprint(c, CHILD, 'session')
        assert c.reusable, 'Report reads disabled the newly owned calibration connection'


def test_custom_row_factory_still_uses_dict_rows_and_disables_reuse(managed):
    db = prepare(managed)
    with validation_session(), calibration_connection(db) as c:
        c.row_factory = None
        assert rows(c, 'SELECT 1 AS n') == [{'n': 1}]
        assert not c.reusable


def test_unknown_database_keeps_borrowed_connection_and_authorizer(managed):
    db = prepare(managed)
    with db.connect() as original:
        original.execute('BEGIN')
        original.set_authorizer(lambda action, *args:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
        class Borrowed:
            @contextmanager
            def connect(self):
                yield original
        with report_source_connection(Borrowed()) as c:
            assert c is original and c.in_transaction
            with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
                c.execute('SELECT 1')
        assert original.in_transaction
        original.set_authorizer(None)


def test_cancelled_report_connection_discards_validation_scope(managed):
    from home_study_agent.rubric_calibration_transaction import _SESSION
    db = prepare(managed)
    with db.connect() as c:
        c.execute('CREATE TABLE rubric_calibration_protocol(protocol_id TEXT)')
        c.execute("INSERT INTO rubric_calibration_protocol VALUES('scope-only')")
        c.commit()
    assert _SESSION.get() is None
    with pytest.raises(asyncio.CancelledError):
        with report_source_connection(db) as c:
            c.execute('BEGIN IMMEDIATE')
            assert _SESSION.get() is not None
            raise asyncio.CancelledError()
    assert _SESSION.get() is None
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.rollback()
