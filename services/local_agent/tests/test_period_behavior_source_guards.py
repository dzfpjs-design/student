"""Frozen parent advice must notice local behavior corrections without a rebuild."""
from datetime import datetime, timedelta
import json
import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.parent_reports import ParentReportService, SnapshotStateError
from test_period_report_reliable_evidence import flow, learning_clock, CHILD


def test_retracted_behavior_invalidates_frozen_report_without_rewriting_parent_work(flow):
    f = flow
    service = BehaviorModelService(f.db)
    service.initialize()
    at = datetime.fromisoformat(f.clock[0])
    event = ObservableEvent(child_id=CHILD, session_id=f.sid, occurred_at=at.isoformat(),
        event_type='work.stall', source='local_vision', confidence=.95)
    service.ingest(event)
    intervention = service.decide_intervention(CHILD, f.sid, (at + timedelta(minutes=1)).isoformat())
    service.record_feedback(intervention['intervention_id'], True, (at + timedelta(minutes=2)).isoformat(),
        client_action_id='feedback-source-guard', child_id=CHILD, session_id=f.sid)
    f.reports.set_behavior_context_provider(service.report_safe_context)
    parents = ParentReportService(f.db)
    parents.set_period_report_service(f.reports)
    report = f.reports.generate(CHILD, 'weekly', f.reference)
    assert any(r.startswith('behavior-feedback:') for r in report['content']['parent_coaching']['evidence_refs'])
    snapshot = parents.create_from_period(report)
    parents.edit_draft(snapshot['snapshot_id'], parent_note='Preserve this parent note')
    parents.approve(snapshot['snapshot_id'])
    with f.db.connect() as c:
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
        assert parents.snapshot_source_current(c, frozen)
    service.retract_event(event.event_id, CHILD, 'source-guard-retract', 'Synthetic correction')
    with f.db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        changes = c.total_changes
        assert not parents.snapshot_source_current(c, frozen)
        unchanged = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()
        assert unchanged['content_json'] == frozen['content_json']
        assert unchanged['parent_edit_json'] == frozen['parent_edit_json']
        assert json.loads(unchanged['parent_edit_json'])['parent_note'] == 'Preserve this parent note'
        assert c.total_changes == changes
    with pytest.raises(SnapshotStateError):
        parents.create_from_period(report)
