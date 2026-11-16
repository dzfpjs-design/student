"""Behavior corrections and policy upgrades must reach frozen parent advice."""
from datetime import datetime, timedelta
import json

import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.behavior_model import service as behavior_module
from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService, SnapshotStateError
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.reports import build_daily_report
from home_study_agent.repository import StudyRepository
from test_daily_report_evidence_versions import flow, CHILD


@pytest.mark.parametrize('kind', ['daily', 'weekly', 'monthly'])
def test_behavior_policy_alone_invalidates_approved_report_and_requires_new_draft(flow, monkeypatch, kind):
    db, repo, daily, delivery = flow
    parents = delivery.snapshots
    with monkeypatch.context() as old:
        old.setattr(behavior_module, 'DAILY_PROJECTION_VERSION', 'legacy_behavior_calendar')
        source = repo.get_daily_report(daily['report_id']) if kind == 'daily' else PeriodReportService(db).generate(CHILD, kind)
        previous = (parents.create_from_daily if kind == 'daily' else parents.create_from_period)(source)
        parents.edit_draft(previous['snapshot_id'], parent_note='Preserve parent work')
        parents.approve(previous['snapshot_id'])
        with db.connect() as c:
            frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (previous['snapshot_id'],)).fetchone())
            assert parents.snapshot_source_current(c, frozen)
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert not parents.snapshot_source_current(c, frozen)
        assert c.total_changes == 0
    restarted_db = Database(db.path)
    restarted = ParentReportService(restarted_db)
    fresh = StudyRepository(restarted_db).get_daily_report(daily['report_id']) if kind == 'daily' else PeriodReportService(restarted_db).get(source['period_report_id'], persist_refresh=True)
    replacement = (restarted.create_from_daily if kind == 'daily' else restarted.create_from_period)(fresh)
    assert replacement['snapshot_id'] != previous['snapshot_id']
    assert replacement['status'] == 'draft'
    assert not restarted.validate_current_for_delivery(replacement['snapshot_id'])
    with db.connect() as c:
        original = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (previous['snapshot_id'],)).fetchone()
        assert original['content_json'] == frozen['content_json']
        assert original['parent_edit_json'] == frozen['parent_edit_json']


def test_daily_retraction_refreshes_actual_behavior_and_keeps_approved_history(flow):
    db, repo, daily, delivery = flow
    sid = daily['session_id']
    service = BehaviorModelService(db)
    service.initialize()
    at = datetime.fromisoformat(repo.get_session(sid)['started_at'])
    event = ObservableEvent(child_id=CHILD, session_id=sid, occurred_at=at.isoformat(),
        event_type='work.stall', source='local_vision', confidence=.95)
    service.ingest(event)
    intervention = service.decide_intervention(CHILD, sid, (at + timedelta(minutes=1)).isoformat())
    service.record_feedback(intervention['intervention_id'], True, (at + timedelta(minutes=2)).isoformat(),
        client_action_id='daily-behavior-feedback', child_id=CHILD, session_id=sid)
    current = repo.get_daily_report(daily['report_id'])
    assert current['evidence']['behavior_intervention_feedback']
    parents = delivery.snapshots
    snapshot = parents.create_from_daily(current)
    parents.edit_draft(snapshot['snapshot_id'], parent_note='Do not discard this note')
    parents.approve(snapshot['snapshot_id'])
    with db.connect() as c:
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
    service.retract_event(event.event_id, CHILD, 'daily-behavior-retract', 'Correction')
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert not parents.snapshot_source_current(c, frozen)
        assert c.total_changes == 0
    fresh = StudyRepository(Database(db.path)).get_daily_report(daily['report_id'])
    assert fresh['evidence']['behavior_intervention_feedback'] == []
    assert fresh['evidence']['behavior_model_summary']['retracted_hypothesis_count'] == 1
    replacement = parents.create_from_daily(fresh)
    assert replacement['snapshot_id'] != snapshot['snapshot_id']
    assert replacement['status'] == 'draft'
    assert parents.get(snapshot['snapshot_id'])['parent_edit']['parent_note'] == 'Do not discard this note'
    assert parents.create_from_daily(current)['snapshot_id'] == snapshot['snapshot_id']
    assert not parents.validate_current_for_delivery(snapshot['snapshot_id'])
