"""Batch raw proof is local, transaction-bound and never replaces daily checks."""
import json
import sqlite3

import pytest

from home_study_agent import daily_behavior_source as module
from home_study_agent.agent_runtime import _RuntimeTransactionDatabase
from home_study_agent.parent_reports import ParentReportService
from test_behavior_goal_confirmation import history, automatic_records, CHILD
from test_behavior_observation_coverage import managed
from test_daily_report_evidence_versions import flow


def prepare(managed):
    db, service = history(managed, 'distraction_candidate')
    automatic_records(db, service)
    service.report_safe_context(CHILD, 'session')
    return db


def test_batch_proof_twice_sessions_separate_and_escaped_reader_not_cached(managed, monkeypatch):
    db = prepare(managed)
    calls = []
    original = module.habit_dependencies
    def counted(c, child, tables):
        calls.append(child)
        return original(c, child, tables)
    monkeypatch.setattr(module, 'habit_dependencies', counted)
    escaped = []
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        expected = [module.daily_behavior_fingerprint(c, CHILD, sid) for sid in ('session', 'another-session')]
        assert expected[0] != expected[1]
        calls.clear()
        def consume(read):
            escaped.append(read)
            return [read(c, CHILD, sid) for sid in ('session', 'another-session')]
        assert module._with_daily_behavior_batch(c, CHILD, consume) == expected
        assert calls == [CHILD, CHILD]
        assert c.total_changes == 0 and c.in_transaction
        escaped[0](c, CHILD, 'session')
        assert calls == [CHILD, CHILD, CHILD]


def test_switching_child_and_adapter_fall_back_to_original_proof(managed, monkeypatch):
    db = prepare(managed)
    original = module.habit_dependencies
    calls = []
    def counted(c, child, tables):
        calls.append(child)
        return original(c, child, tables)
    monkeypatch.setattr(module, 'habit_dependencies', counted)
    with db.connect() as c:
        c.execute('BEGIN')
        other = 'child-junior-3'
        value = module._with_daily_behavior_batch(c, CHILD, lambda read: read(c, other, 'other-session'))
        assert value == module.daily_behavior_fingerprint(c, other, 'other-session')
        assert other in calls
        calls.clear()
        adapter = _RuntimeTransactionDatabase(c)
        module._with_daily_behavior_batch(adapter, CHILD, lambda read: [read(adapter, CHILD, 'session') for _ in range(3)])
        assert calls == [CHILD] * 3


def test_same_transaction_source_change_and_rollback_cannot_reuse_old_proof(managed):
    db = prepare(managed)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = module.daily_behavior_fingerprint(c, CHILD, 'session')
        did_change = []
        def consume(read):
            if not did_change:
                c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
                did_change.append(True)
            return read(c, CHILD, 'session')
        changed = module._with_daily_behavior_batch(c, CHILD, consume)
        assert changed != before
        assert changed == module.daily_behavior_fingerprint(c, CHILD, 'session')
        c.rollback()
        c.execute('BEGIN')
        assert module._with_daily_behavior_batch(c, CHILD, lambda read: read(c, CHILD, 'session')) == before


def test_rollback_then_begin_aba_is_rejected_even_without_row_changes(managed):
    db = prepare(managed)
    with db.connect() as c:
        c.execute('BEGIN')
        def consume(read):
            c.rollback()
            c.execute('BEGIN')
            return read(c, CHILD, 'session')
        with pytest.raises(RuntimeError, match='BEHAVIOR_READ_TRANSACTION_CHANGED'):
            module._with_daily_behavior_batch(c, CHILD, consume)


def test_end_proof_recheck_discards_external_authority_change(managed, monkeypatch):
    db = prepare(managed)
    actual = module.habit_dependencies
    state = [0]
    consumers = []
    def changing(c, child, tables):
        value = actual(c, child, tables)
        state[0] += 1
        return [*value, {'synthetic_external_authority_epoch': 0 if state[0] == 1 else 1}]
    monkeypatch.setattr(module, 'habit_dependencies', changing)
    with db.connect() as c:
        c.execute('BEGIN')
        def consume(read):
            value = read(c, CHILD, 'session')
            consumers.append(value)
            return value
        value = module._with_daily_behavior_batch(c, CHILD, consume)
        assert len(consumers) == 2 and consumers[0] != consumers[1]
        assert value == consumers[-1]


@pytest.mark.parametrize('field', ['reliable_learning_fingerprint', 'mistake_mastery_fingerprint',
                                  'specialized_reference_fingerprint', 'behavior_source_fingerprint'])
def test_batch_does_not_skip_individual_daily_qualifications(flow, field):
    db, _, daily, _ = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert ParentReportService.source_qualification_fingerprint(c, CHILD, 'daily_report', daily['report_id'])
        evidence = json.loads(c.execute('SELECT evidence_json FROM daily_report WHERE report_id=?', (daily['report_id'],)).fetchone()[0])
        evidence[field] = 'synthetic-stale-source'
        c.execute('UPDATE daily_report SET evidence_json=? WHERE report_id=?', (json.dumps(evidence), daily['report_id']))
        def qualify(read):
            return ParentReportService._daily_source_qualification(c, CHILD, daily['report_id'], read)
        assert module._with_daily_behavior_batch(c, CHILD, qualify) is None
        c.rollback()


@pytest.mark.parametrize('fail', [False, True])
def test_period_source_owns_only_its_new_read_transaction(managed, monkeypatch, fail):
    from home_study_agent.period_reports import PeriodReportService
    db = prepare(managed)
    PeriodReportService(db).initialize()
    actual = PeriodReportService._goal_progress
    seen = []
    def inspect(c, child, start, end):
        seen.append(c.in_transaction)
        if fail:
            raise RuntimeError('synthetic source failure')
        return actual(c, child, start, end)
    monkeypatch.setattr(PeriodReportService, '_goal_progress', staticmethod(inspect))
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert not c.in_transaction
        try:
            value = ParentReportService.period_learning_sources(c, CHILD, '2026-08-01', '2026-08-31')
        except RuntimeError as error:
            assert fail and str(error) == 'synthetic source failure'
        else:
            assert not fail and value
        assert seen == [True]
        assert not c.in_transaction and c.total_changes == 0


def test_period_source_leaves_borrowed_writer_and_changes_owned_by_caller(managed):
    from home_study_agent.period_reports import PeriodReportService
    db = prepare(managed)
    PeriodReportService(db).initialize()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE child_profile SET grade='scope-test' WHERE child_id=?", (CHILD,))
        changes = c.total_changes
        assert ParentReportService.period_learning_sources(c, CHILD, '2026-08-01', '2026-08-31')
        assert c.in_transaction and c.total_changes == changes
        assert c.execute('SELECT grade FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0] == 'scope-test'
        c.rollback()
        assert c.execute('SELECT grade FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0] != 'scope-test'


def test_savepoint_denied_uses_original_checks_without_replacing_authorizer(managed, monkeypatch):
    db = prepare(managed)
    actual = module.habit_dependencies
    calls = []
    def counted(*args):
        calls.append(True)
        return actual(*args)
    monkeypatch.setattr(module, 'habit_dependencies', counted)
    with db.connect() as c:
        c.execute('BEGIN')
        denied = []
        def authorizer(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_SAVEPOINT:
                denied.append(first)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        c.set_authorizer(authorizer)
        try:
            values = module._with_daily_behavior_batch(c, CHILD,
                lambda read: [read(c, CHILD, sid) for sid in ('session', 'other-session')])
            assert len(calls) == 2 and values[0] != values[1]
            assert denied and c.in_transaction and c.total_changes == 0
            with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
                c.execute('SAVEPOINT still_protected')
        finally:
            c.set_authorizer(None)
