"""Combined period/daily proof must stay fresh, owned and cancellation-local."""
import asyncio
import json

import pytest

from home_study_agent import daily_behavior_source as daily
from home_study_agent import period_behavior_source as period
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.agent_runtime import _RuntimeTransactionDatabase
from test_behavior_goal_confirmation import history, automatic_records, CHILD
from test_behavior_observation_coverage import managed
from test_daily_report_evidence_versions import flow

START, END = '2026-08-01', '2026-09-02'


def prepare(managed):
    db, service = history(managed, 'distraction_candidate')
    automatic_records(db, service)
    service.report_safe_context(CHILD, 'session')
    PeriodReportService(db).initialize()
    return db


def pair(c, child=CHILD):
    return (daily.daily_behavior_fingerprint(c, child, 'session'),
            period.period_behavior_fingerprint(c, child, START, END))


def test_combined_pair_matches_independent_read_and_rechecks_raw_proof_twice(managed, monkeypatch):
    db = prepare(managed)
    proof = daily.habit_dependencies
    calls = []
    def counted(*args):
        calls.append(True)
        return proof(*args)
    monkeypatch.setattr(daily, 'habit_dependencies', counted)
    monkeypatch.setattr(period, 'habit_dependencies', counted)
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        expected = pair(c)
        calls.clear()
        result = daily._with_behavior_source_batch(c, CHILD,
            lambda d, p: (d(c, CHILD, 'session'), p(c, CHILD, START, END)))
        assert result == expected and len(calls) == 2
        assert c.in_transaction and c.total_changes == 0


@pytest.mark.parametrize('adapter', [False, True])
def test_other_child_and_adapter_cannot_use_origin_child_proof(managed, adapter):
    db = prepare(managed)
    with db.connect() as connection:
        connection.execute('BEGIN')
        c = _RuntimeTransactionDatabase(connection) if adapter else connection
        child = CHILD if adapter else 'child-junior-3'
        expected = pair(c, child)
        result = daily._with_behavior_source_batch(c, CHILD,
            lambda d, p: (d(c, child, 'session'), p(c, child, START, END)))
        assert result == expected
        assert connection.in_transaction and connection.total_changes == 0


def test_changed_source_discards_entire_pair_and_replays_fresh(managed):
    db = prepare(managed)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = pair(c)
        seen = []
        def consume(d, p):
            behavior = p(c, CHILD, START, END)
            if not seen:
                c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
            value = d(c, CHILD, 'session'), behavior
            seen.append(value)
            return value
        actual = daily._with_behavior_source_batch(c, CHILD, consume)
        assert len(seen) == 2 and actual == pair(c) and actual != before
        assert seen[0] != actual
        c.rollback()
        c.execute('BEGIN')
        assert pair(c) == before


def test_cancelled_pair_clears_scope_without_owning_callers_transaction(managed):
    db = prepare(managed)
    escaped = []
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = pair(c)
        def cancel(d, p):
            escaped.extend([d, p])
            assert (d(c, CHILD, 'session'), p(c, CHILD, START, END)) == before
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            daily._with_behavior_source_batch(c, CHILD, cancel)
        assert c.in_transaction and c.total_changes == 0
        c.rollback()
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
        result = escaped[0](c, CHILD, 'session'), escaped[1](c, CHILD, START, END)
        assert result == pair(c) and result != before
        c.rollback()


def test_period_reader_does_not_hide_transaction_aba(managed):
    db = prepare(managed)
    with db.connect() as c:
        c.execute('BEGIN')
        def changed(d, p):
            first = p(c, CHILD, START, END)
            c.rollback()
            c.execute('BEGIN')
            return first, d(c, CHILD, 'session')
        with pytest.raises(RuntimeError, match='BEHAVIOR_READ_TRANSACTION_CHANGED'):
            daily._with_behavior_source_batch(c, CHILD, changed)


def test_cancel_during_real_snapshot_writer_persists_no_partial_snapshot(flow, monkeypatch):
    db = flow[0]
    reports = PeriodReportService(db)
    parents = ParentReportService(db)
    parents.set_period_report_service(reports)
    report = reports.generate(CHILD, 'weekly')
    snapshot = parents.create_from_period(report)
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
        before = [tuple(r) for r in c.execute('SELECT * FROM parent_report_snapshot ORDER BY snapshot_id')]
    original = ParentReportService._period_learning_sources
    def cancel(c, *args, **kwargs):
        assert c.in_transaction
        original(c, *args, **kwargs)
        raise asyncio.CancelledError()
    with monkeypatch.context() as patch:
        patch.setattr(ParentReportService, '_period_learning_sources', staticmethod(cancel))
        with pytest.raises(asyncio.CancelledError):
            parents._insert_snapshot(source_kind=row['source_kind'], source_id=row['source_id'], report_id=row['report_id'],
                child_id=row['child_id'], report_type=row['report_type'], source_version=row['source_version'],
                facts=json.loads(row['facts_json']), inferences=json.loads(row['inferences_json']),
                content=json.loads(row['content_json']), confidence=json.loads(row['confidence_json']))
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM parent_report_snapshot ORDER BY snapshot_id')] == before
    assert parents.approve(snapshot['snapshot_id'])['status'] == 'approved'


def test_combined_raw_proof_recheck_distinguishes_bool_from_int(managed, monkeypatch):
    db = prepare(managed)
    proof = daily.habit_dependencies
    calls, seen = [], []
    def typed(*args):
        calls.append(True)
        return [*proof(*args), {'synthetic_typed_input': 1 if len(calls) == 1 else True}]
    monkeypatch.setattr(daily, 'habit_dependencies', typed)
    monkeypatch.setattr(period, 'habit_dependencies', typed)
    with db.connect() as c:
        c.execute('BEGIN')
        def consume(d, p):
            value = d(c, CHILD, 'session'), p(c, CHILD, START, END)
            seen.append(value)
            return value
        result = daily._with_behavior_source_batch(c, CHILD, consume)
        assert len(seen) == 2 and seen[0] != seen[1]
        assert result == pair(c)


def test_replay_context_memo_keeps_typed_validation_and_new_call_isolation(managed):
    from copy import deepcopy
    from datetime import datetime, timezone
    from home_study_agent.behavior_model.outcome_evidence import coverage_projection
    db = prepare(managed)
    from home_study_agent.behavior_model import BehaviorModelService
    with db.connect() as c:
        rows = [dict(r) for r in BehaviorModelService._coverage_records(c, CHILD)]
    def project(records):
        return coverage_projection(records, CHILD, datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 2, tzinfo=timezone.utc), include_opportunities=True)
    expected = project(rows)
    assert 'invalid_sample' not in expected['unknown_reasons']
    for version in (True, 1.0):
        changed = deepcopy(rows)
        row = next(r for r in changed if r['event_id'] == 'sample-0-40')
        payload = json.loads(row['payload_json'])
        payload['task_context']['plan_version'] = version
        row['payload_json'] = json.dumps(payload)
        actual = project(changed)
        assert 'invalid_sample' in actual['unknown_reasons']
        assert actual['observed_seconds'] < expected['observed_seconds']
        assert project(rows) == expected
