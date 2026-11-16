"""Audit-only counterexamples; real service writes to synthetic temporary SQLite."""
from datetime import date
import pytest

from home_study_agent.behavior_model import ObservableEvent
from home_study_agent.period_behavior_source import period_behavior_fingerprint
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.parent_reports import ParentReportService
from test_behavior_goal_confirmation import history, automatic_records, confirm, CHILD, NOW
from test_behavior_observation_coverage import managed


def frozen_report(managed):
    db, behavior = history(managed, 'distraction_candidate')
    automatic_records(db, behavior)
    periods = PeriodReportService(db, behavior.report_safe_context)
    report = periods.generate(CHILD, 'monthly', date(2026, 8, 31))
    parents = ParentReportService(db)
    parents.set_period_report_service(periods)
    snapshot = parents.create_from_period(report)
    parents.approve(snapshot['snapshot_id'])
    with db.connect() as c:
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
        assert parents.snapshot_source_current(c, frozen)
    return db, behavior, periods, parents, report, frozen


@pytest.mark.parametrize('mutation', ['goal_confirmation', 'task_source_metadata', 'task_event'])
def test_changed_actual_habit_result_invalidates_approved_source(managed, mutation):
    db, behavior, periods, parents, report, frozen = frozen_report(managed)
    before = report['content']['habit_experiments'][0]['outcome']
    with db.connect() as c:
        old_fingerprint = period_behavior_fingerprint(c, CHILD, '2026-08-01', '2026-08-31')
    if mutation == 'goal_confirmation':
        item = behavior.goal_behavior_opportunities(CHILD, 'goal', now=NOW)['opportunities'][-1]
        confirm(behavior, item, success=False, key='audit-correction')
    else:
        with db.connect() as c:
            if mutation == 'task_source_metadata':
                c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
            else:
                c.execute("DELETE FROM plan_item_event WHERE event_id='started-11'")
            c.commit()
    after = periods.get(report['period_report_id'])['content']['habit_experiments'][0]['outcome']
    assert before != after, 'Counterexample must change the real report outcome first'
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        current = parents.snapshot_source_current(c, frozen)
        new_fingerprint = period_behavior_fingerprint(c, CHILD, '2026-08-01', '2026-08-31')
    assert (current, old_fingerprint == new_fingerprint) == (False, False)


def test_provider_first_finalize_and_outcome_cache_refresh_are_stable(managed):
    db, behavior, periods, parents, report, frozen = frozen_report(managed)
    # frozen_report's first provider read finalizes the old active habit.
    with db.connect() as c:
        assert c.execute("SELECT status FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0] == 'completed'
        before = period_behavior_fingerprint(c, CHILD, '2026-08-01', '2026-08-31')
    for _ in range(2):
        behavior.report_safe_context(CHILD, period_start=date(2026, 8, 1), period_end=date(2026, 8, 31))
    behavior.ingest(ObservableEvent(child_id='child-junior-3', session_id='other-session',
        event_id='other-child', event_type='work.stall', source='local_vision', confidence=.9,
        occurred_at='2026-08-25T10:00:00Z'))
    with db.connect() as c:
        assert period_behavior_fingerprint(c, CHILD, '2026-08-01', '2026-08-31') == before
        assert parents.snapshot_source_current(c, frozen)


def test_event_outside_report_and_all_habit_windows_does_not_revoke_unchanged_advice(managed):
    db, behavior, periods, parents, report, frozen = frozen_report(managed)
    behavior.ingest(ObservableEvent(child_id=CHILD, session_id='session',
        event_id='outside-every-window', event_type='work.reading', source='local_vision', confidence=.9,
        occurred_at='2026-05-01T10:00:00Z'))
    after = periods.get(report['period_report_id'])['content']
    before = {k: v for k, v in report['content'].items() if k != 'behavior_source_fingerprint'}
    assert before == {k: v for k, v in after.items() if k != 'behavior_source_fingerprint'}
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert parents.snapshot_source_current(c, frozen)
