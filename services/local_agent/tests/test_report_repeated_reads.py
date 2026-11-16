"""Owned report scopes must not manufacture dependency changes by reading."""
from types import SimpleNamespace
import asyncio
import sqlite3

import pytest

from home_study_agent.daily_behavior_source import _with_behavior_source_batch
from home_study_agent.behavior_model.report_dependencies import rows
from home_study_agent.rubric_calibration_transaction import validation_session, calibration_connection
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from test_behavior_combined_source_scope import prepare, CHILD, START, END
from test_behavior_observation_coverage import managed
from test_daily_report_evidence_versions import flow
from test_behavior_combined_source_scope import pair


def test_native_report_rows_preserve_owned_read_write_tracking(managed):
    db = prepare(managed)
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN')
        marker = SimpleNamespace()
        c.verified(marker)
        assert rows(c, 'SELECT 1 AS n') == [{'n': 1}]
        assert c.changed_tables(marker) == set(), 'A read-only cursor cleared known write dependencies'


def test_owned_behavior_read_does_not_change_its_transaction_epoch(managed):
    db = prepare(managed)
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN')
        epoch = c.epoch
        result = _with_behavior_source_batch(c, CHILD,
            lambda daily, period: (daily(c, CHILD, 'session'), period(c, CHILD, START, END)))
        assert all(result)
        assert c.epoch == epoch, 'Read-only helper invalidated the owned transaction proof'


def test_period_freeze_returns_its_single_fresh_enriched_read(flow, monkeypatch):
    db = flow[0]
    periods = PeriodReportService(db)
    parents = ParentReportService(db)
    report = periods.generate(CHILD, 'weekly')
    get = parents.get
    calls = []
    def counted(snapshot_id):
        calls.append(snapshot_id)
        return get(snapshot_id)
    monkeypatch.setattr(parents, 'get', counted)
    value = parents.create_from_period(report)
    assert calls == [value['snapshot_id']]
    assert value['correction_status']['source_current']
    assert value['parent_edit']['coaching_decision'] == 'adopt'


@pytest.mark.parametrize('action', ['rollback', 'commit', 'cursor_rollback',
    'authorizer', 'raw_connection', 'factory', 'savepoint'])
def test_owned_batch_rejects_transaction_or_ownership_change(managed, action):
    db = prepare(managed)
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN')
        def change(d, p):
            first = p(c, CHILD, START, END)
            if action in ('rollback', 'commit'):
                getattr(c, action)()
                c.execute('BEGIN')
            elif action == 'cursor_rollback':
                cursor = c.cursor()
                try:
                    cursor.execute('ROLLBACK')
                    cursor.execute('BEGIN')
                finally:
                    cursor.close()
            elif action == 'authorizer':
                c.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
            elif action == 'raw_connection':
                cursor = c.execute('SELECT 1')
                try:
                    assert cursor.connection is not None
                finally:
                    cursor.close()
            elif action == 'factory':
                c.row_factory = sqlite3.Row
            else:
                c.execute('SAVEPOINT nested')
                c.execute('RELEASE SAVEPOINT nested')
            return first, d(c, CHILD, 'session')
        with pytest.raises(RuntimeError, match='BEHAVIOR_READ_TRANSACTION_CHANGED'):
            _with_behavior_source_batch(c, CHILD, change)


def test_owned_batch_source_write_replays_and_rollback_restores(managed):
    db = prepare(managed)
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN IMMEDIATE')
        before, seen = pair(c), []
        def change(d, p):
            first = p(c, CHILD, START, END)
            if not seen:
                c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
            result = d(c, CHILD, 'session'), first
            seen.append(result)
            return result
        result = _with_behavior_source_batch(c, CHILD, change)
        assert len(seen) == 2 and result == pair(c) and result != before
        c.rollback()
        c.execute('BEGIN')
        assert pair(c) == before


def test_owned_batch_cancellation_and_child_boundary(managed):
    db = prepare(managed)
    escaped = []
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN IMMEDIATE')
        before = pair(c)
        other = pair(c, 'child-junior-3')
        def cancel(d, p):
            escaped.extend([d, p])
            assert (d(c, 'child-junior-3', 'session'),
                p(c, 'child-junior-3', START, END)) == other
            assert (d(c, CHILD, 'session'), p(c, CHILD, START, END)) == before
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            _with_behavior_source_batch(c, CHILD, cancel)
        assert c.in_transaction
        c.rollback()
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
        assert (escaped[0](c, CHILD, 'session'), escaped[1](c, CHILD, START, END)) == pair(c)
        assert pair(c) != before
        c.rollback()


def test_owned_batch_typed_raw_proof_changes_replay(managed, monkeypatch):
    from home_study_agent import daily_behavior_source as daily
    from home_study_agent import period_behavior_source as period
    db = prepare(managed)
    original = daily.habit_dependencies
    calls, seen = [], []
    def typed(*args):
        calls.append(True)
        return [*original(*args), {'typed_boundary': 1 if len(calls) == 1 else True}]
    monkeypatch.setattr(daily, 'habit_dependencies', typed)
    monkeypatch.setattr(period, 'habit_dependencies', typed)
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN')
        def consume(d, p):
            value = d(c, CHILD, 'session'), p(c, CHILD, START, END)
            seen.append(value)
            return value
        result = _with_behavior_source_batch(c, CHILD, consume)
        assert len(seen) == 2 and seen[0] != seen[1]
        assert result == pair(c)


def test_period_source_business_marker_precedes_owned_batch_and_keeps_borrowed_write(managed, monkeypatch):
    from home_study_agent.standard_relocation_provenance import business_snapshot
    from home_study_agent.original_connection_read import original_connection
    db = prepare(managed)
    original = ParentReportService._period_learning_sources
    nested = []
    def source_read(c, *args, **kwargs):
        before = c.epoch
        with business_snapshot(c):
            value = original(c, *args, **kwargs)
        nested.append((before, c.epoch))
        return value
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN IMMEDIATE')
        before = c.execute("SELECT confirmation_status FROM homework_item WHERE homework_id='homework-11'").fetchone()[0]
        c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
        changes = c.total_changes
        expected = ParentReportService._period_source_fingerprints(original_connection(c), CHILD, START, END)
        with monkeypatch.context() as patch:
            patch.setattr(ParentReportService, '_period_learning_sources', staticmethod(source_read))
            result = ParentReportService._period_source_fingerprints(c, CHILD, START, END)
        assert result == expected
        assert nested and all(first == last for first, last in nested)
        assert c.in_transaction and c.total_changes == changes
        assert c.execute("SELECT confirmation_status FROM homework_item WHERE homework_id='homework-11'").fetchone()[0] == 'unconfirmed'
        c.rollback()
        c.execute('BEGIN')
        assert c.execute("SELECT confirmation_status FROM homework_item WHERE homework_id='homework-11'").fetchone()[0] == before


@pytest.mark.parametrize('error', [RuntimeError('synthetic original read failed'), asyncio.CancelledError()])
def test_period_business_scope_keeps_original_error_and_borrowed_transaction(managed, monkeypatch, error):
    from home_study_agent.standard_relocation_provenance import business_snapshot
    db = prepare(managed)
    original = ParentReportService._period_learning_sources
    def failed(c, *args, **kwargs):
        with business_snapshot(c):
            original(c, *args, **kwargs)
            raise error
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN IMMEDIATE')
        before = c.execute("SELECT confirmation_status FROM homework_item WHERE homework_id='homework-11'").fetchone()[0]
        c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
        changes = c.total_changes
        with monkeypatch.context() as patch:
            patch.setattr(ParentReportService, '_period_learning_sources', staticmethod(failed))
            with pytest.raises(type(error)) as caught:
                ParentReportService._period_source_fingerprints(c, CHILD, START, END)
        assert caught.value is error
        assert c.in_transaction and c.total_changes == changes
        assert c.execute("SELECT confirmation_status FROM homework_item WHERE homework_id='homework-11'").fetchone()[0] == 'unconfirmed'
        c.rollback()
        c.execute('BEGIN')
        assert c.execute("SELECT confirmation_status FROM homework_item WHERE homework_id='homework-11'").fetchone()[0] == before
