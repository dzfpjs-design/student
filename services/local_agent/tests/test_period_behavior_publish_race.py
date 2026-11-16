"""Local source correction between aggregation and the writer must not publish."""
from datetime import datetime

import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.period_reports import PeriodReportSourceError
from test_period_report_reliable_evidence import flow, learning_clock, CHILD


@pytest.mark.parametrize('operation', ['generate', 'refresh'])
def test_behavior_changed_after_aggregation_preserves_stored_period_and_can_retry(flow, monkeypatch, operation):
    f = flow
    behavior = BehaviorModelService(f.db)
    behavior.initialize()
    event = ObservableEvent(child_id=CHILD, session_id=f.sid, occurred_at=f.clock[0],
        event_type='work.stall', source='local_vision', confidence=.95)
    behavior.ingest(event)
    f.reports.set_behavior_context_provider(behavior.report_safe_context)
    previous = f.reports.generate(CHILD, 'weekly', f.reference)
    with f.db.connect() as c:
        stored = c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',
            (previous['period_report_id'],)).fetchone()[0]
    build = f.reports._build_content

    def retract_after_read(*args, **kwargs):
        result = build(*args, **kwargs)
        behavior.retract_event(event.event_id, CHILD, 'period-publish-race', 'Correction')
        return result

    with monkeypatch.context() as change:
        change.setattr(f.reports, '_build_content', retract_after_read)
        with pytest.raises(PeriodReportSourceError, match='PERIOD_BEHAVIOR_SOURCE_CHANGED'):
            if operation == 'generate':
                f.reports.generate(CHILD, 'weekly', f.reference)
            else:
                f.reports.get(previous['period_report_id'], persist_refresh=True)
    with f.db.connect() as c:
        assert c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',
            (previous['period_report_id'],)).fetchone()[0] == stored
        assert c.execute('SELECT COUNT(*) FROM observable_event WHERE event_id=?', (event.event_id,)).fetchone()[0] == 1
    fresh = f.reports.get(previous['period_report_id'], persist_refresh=True)
    assert fresh['content']['behavior_source_fingerprint'] != previous['content']['behavior_source_fingerprint']
