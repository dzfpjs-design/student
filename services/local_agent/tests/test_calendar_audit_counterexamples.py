"""Read-only production audit; all writes target synthetic tmp_path databases."""
from datetime import date, datetime, timedelta, timezone

import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.family_clock import FAMILY_ZONE
from test_period_report_reliable_evidence import flow, learning_clock, CHILD


def observe(service, at, *, child=CHILD, session='calendar-session'):
    event = ObservableEvent(child_id=child, session_id=session,
        occurred_at=at, event_type='work.stall', source='local_vision', confidence=.95)
    service.ingest(event)
    return event.event_id


@pytest.mark.parametrize('kind,day', [('weekly', date(2026, 9, 7)), ('monthly', date(2026, 9, 1))])
def test_behavior_period_context_assigns_local_midnight_to_current_period(flow, kind, day):
    f = flow
    at = datetime.combine(day, datetime.min.time(), tzinfo=FAMILY_ZONE).astimezone(timezone.utc)
    f.clock[0] = (at + timedelta(minutes=3)).isoformat()
    behavior = BehaviorModelService(f.db)
    behavior.initialize()
    event_id = observe(behavior, at.isoformat(), session=f.sid)
    decision = behavior.decide_intervention(CHILD, f.sid, (at + timedelta(minutes=1)).isoformat())
    assert 'intervention_id' in decision, decision
    behavior.record_feedback(decision['intervention_id'], True, (at + timedelta(minutes=2)).isoformat(),
        client_action_id='calendar-feedback', child_id=CHILD, session_id=f.sid)
    observe(behavior, at.isoformat(), child='child-junior-3')
    f.reports.set_behavior_context_provider(behavior.report_safe_context)
    before = f.reports.generate(CHILD, kind, day - timedelta(days=1))['content']
    current = f.reports.generate(CHILD, kind, day)['content']
    previous_start, previous_end = f.reports._period_bounds(kind, day - timedelta(days=1))
    start, end = f.reports._period_bounds(kind, day)
    previous_context = behavior.report_safe_context(CHILD, period_start=previous_start, period_end=previous_end)
    current_context = behavior.report_safe_context(CHILD, period_start=start, period_end=end)
    with behavior._connect() as c:
        journal = [tuple(row) for row in c.execute('SELECT * FROM observable_event ORDER BY rowid')]
    behavior.retract_event(event_id, CHILD, 'calendar-retraction', 'Synthetic audit correction')
    reopened = BehaviorModelService(f.db)
    withdrawn = reopened.report_safe_context(CHILD, period_start=start, period_end=end)
    with reopened._connect() as c:
        assert journal == [tuple(row) for row in c.execute('SELECT * FROM observable_event ORDER BY rowid')]
    actual = {
        'previous_hypotheses': len(previous_context['behavior_model_summary']['active_hypotheses']),
        'current_hypotheses': len(current_context['behavior_model_summary']['active_hypotheses']),
        'previous_feedback': sum(ref.startswith('behavior-feedback:') for ref in before['parent_coaching']['evidence_refs']),
        'current_feedback': sum(ref.startswith('behavior-feedback:') for ref in current['parent_coaching']['evidence_refs']),
        'current_retracted': withdrawn['behavior_model_summary']['retracted_hypothesis_count'],
        'current_withdrawn': withdrawn['behavior_model_summary']['withdrawn_intervention_count'],
    }
    assert actual == {'previous_hypotheses': 0, 'current_hypotheses': 1,
        'previous_feedback': 0, 'current_feedback': 1, 'current_retracted': 1, 'current_withdrawn': 1}


def test_daily_feature_same_local_day_survives_restart_and_retraction(tmp_path):
    behavior = BehaviorModelService(tmp_path / 'behavior.db')
    first = observe(behavior, '2026-09-06T16:00:00+00:00')
    observe(behavior, '2026-09-07T01:00:00+08:00')
    with behavior._connect() as c:
        before = [tuple(row) for row in c.execute('SELECT day,stall_count FROM daily_feature ORDER BY day')]
    restarted = BehaviorModelService(behavior.path)
    restarted.retract_event(first, CHILD, 'calendar-retraction', 'Synthetic audit correction')
    with restarted._connect() as c:
        after = [tuple(row) for row in c.execute('SELECT day,stall_count FROM daily_feature ORDER BY day')]
    assert (before, after) == ([('2026-09-07', 2)], [('2026-09-07', 1)])


def test_daily_behavior_summary_uses_local_day(tmp_path):
    behavior = BehaviorModelService(tmp_path / 'behavior.db')
    observe(behavior, '2026-09-06T16:00:00+00:00')
    assert (behavior.behavior_model_summary(CHILD, None, '2026-09-06')['event_count'],
        behavior.behavior_model_summary(CHILD, None, '2026-09-07')['event_count']) == (0, 1)
