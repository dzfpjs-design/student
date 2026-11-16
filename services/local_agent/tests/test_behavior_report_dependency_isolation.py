"""Local report tokens bind relevant journals, never other children or caches."""
from datetime import datetime, timedelta

import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.daily_behavior_source import daily_behavior_fingerprint
from home_study_agent.period_behavior_source import period_behavior_fingerprint
from test_period_report_reliable_evidence import flow, learning_clock, CHILD, OTHER


@pytest.mark.parametrize('kind', ['daily', 'period'])
def test_tokens_are_read_only_and_preserve_child_scope_and_historical_edits(flow, kind):
    f = flow
    service = BehaviorModelService(f.db)
    service.initialize()
    start, end = f.reports._period_bounds('weekly', f.reference)

    def fingerprint(c):
        return daily_behavior_fingerprint(c, CHILD, f.sid) if kind == 'daily' else period_behavior_fingerprint(
            c, CHILD, start.isoformat(), end.isoformat())

    def token():
        with f.db.connect() as c:
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            result = fingerprint(c)
            assert c.in_transaction
            assert c.total_changes == 0
            return result

    before = token()
    service.ingest(ObservableEvent(child_id=OTHER, session_id='other-session', occurred_at=f.clock[0],
        event_type='work.stall', source='local_vision', confidence=.8))
    assert token() == before
    outside = (datetime.fromisoformat(f.clock[0]) - timedelta(days=30)).isoformat()
    service.ingest(ObservableEvent(child_id=CHILD, session_id='historical-session', occurred_at=outside,
        event_type='work.stall', source='local_vision', confidence=.8))
    assert token() == before
    event = ObservableEvent(child_id=CHILD, session_id=f.sid, occurred_at=f.clock[0],
        event_type='work.stall', source='local_vision', confidence=.8)
    service.ingest(event)
    baseline = token()
    assert baseline != before
    with f.db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE observable_event SET confidence=.7 WHERE event_id=?", (event.event_id,))
        assert fingerprint(c) != baseline
        c.rollback()
    assert token() == baseline
    with f.db.connect() as c:
        c.execute('UPDATE daily_feature SET stall_count=999 WHERE child_id=?', (CHILD,))
        c.commit()
    assert token() == baseline
    service.retract_event(event.event_id, CHILD, 'isolated-retraction', 'Correction')
    assert token() != baseline
