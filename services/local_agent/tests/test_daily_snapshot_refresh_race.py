"""A bounded source refresh must not turn transient learning updates into lost reports."""
import json

import pytest

from home_study_agent.goal_program import GoalProgramService
from home_study_agent.parent_reports import DailySnapshotSourceChangedError, ParentReportService, SnapshotStateError
from home_study_agent.repository import ReportNotReadyError, StudyRepository, UnknownReportError
from test_daily_report_evidence_versions import flow, CHILD


def set_target(goals, index):
    return goals.configure_program(CHILD, 'pet_path', target_statement=None,
        target_date=f'2027-02-{index + 1:02d}', target_score=None, target_score_label=None,
        event_name=None, event_rules_url=None, event_version=None, award_target=None,
        registration_deadline=None, weekly_minutes=None, weekly_sessions=None,
        client_action_id=f'report-race-goal-{index}')


@pytest.mark.parametrize('mode', ['reconcile', 'refresh'])
@pytest.mark.parametrize('continuous', [False, True])
def test_source_changes_during_freeze_retry_fresh_without_reapproving_history(flow, monkeypatch, mode, continuous):
    db, repo, initial, delivery = flow
    goals = GoalProgramService(db)
    goals.seed_defaults()
    reports = delivery.snapshots
    old = reports.create_from_daily(repo.get_daily_report(initial['report_id']))
    reports.edit_draft(old['snapshot_id'], 'Saved introduction', 'Keep this parent note')
    old = reports.approve(old['snapshot_id'])
    with db.connect() as c:
        original_content = tuple(c.execute('SELECT content_json,parent_edit_json FROM parent_report_snapshot WHERE snapshot_id=?',
            (old['snapshot_id'],)).fetchone())
    set_target(goals, 0)
    original_insert = reports._insert_snapshot
    inserts = []
    def insert(**kwargs):
        inserts.append(kwargs['source_id'])
        if continuous or len(inserts) == 1:
            set_target(goals, len(inserts))
        return original_insert(**kwargs)
    monkeypatch.setattr(reports, '_insert_snapshot', insert)
    if mode == 'reconcile':
        reports.reconcile_daily_sources(CHILD)
    else:
        reports.refresh_sources(CHILD, source_kind='daily_report')
    assert len(inserts) == 2
    with db.connect() as c:
        assert tuple(c.execute('SELECT content_json,parent_edit_json FROM parent_report_snapshot WHERE snapshot_id=?',
            (old['snapshot_id'],)).fetchone()) == original_content
        snapshots = c.execute('SELECT * FROM parent_report_snapshot WHERE report_id=? ORDER BY created_at',
            (initial['report_id'],)).fetchall()
        assert c.execute("SELECT COUNT(*) FROM report_delivery_outbox").fetchone()[0] == 0
    if continuous:
        assert len(snapshots) == 1
        assert reports.source_issues(CHILD)
        monkeypatch.setattr(reports, '_insert_snapshot', original_insert)
        reports.reconcile_daily_sources(CHILD)
    else:
        assert len(snapshots) == 2
        assert snapshots[-1]['status'] == 'draft'
        assert not reports.source_issues(CHILD)
    restarted = ParentReportService(db)
    restarted.reconcile_daily_sources(CHILD)
    with db.connect() as c:
        rows = c.execute('SELECT * FROM parent_report_snapshot WHERE report_id=?', (initial['report_id'],)).fetchall()
        assert len(rows) == 2
        assert sorted(row['status'] for row in rows) == ['draft', 'revoked']
        current = next(row for row in rows if row['status'] == 'draft')
        assert ParentReportService.snapshot_source_current(c, current)
        assert json.loads(next(row for row in rows if row['snapshot_id'] == old['snapshot_id'])['parent_edit_json'])['parent_note'] == 'Keep this parent note'


@pytest.mark.parametrize('mode', ['reconcile', 'refresh'])
@pytest.mark.parametrize('failure', [ReportNotReadyError, UnknownReportError])
def test_unavailable_source_on_retry_is_durable_issue(flow, monkeypatch, mode, failure):
    db, repo, initial, delivery = flow
    reports = delivery.snapshots
    reports.create_from_daily(repo.get_daily_report(initial['report_id']))
    goals = GoalProgramService(db)
    goals.seed_defaults()
    set_target(goals, 0)
    calls = []

    def insert(**kwargs):
        calls.append(kwargs['source_id'])
        def unavailable(*args, **kwargs):
            raise failure('source unavailable during retry')
        monkeypatch.setattr(StudyRepository, 'get_daily_report', unavailable)
        raise DailySnapshotSourceChangedError('source changed')

    monkeypatch.setattr(reports, '_insert_snapshot', insert)
    if mode == 'reconcile':
        reports.reconcile_daily_sources(CHILD)
    else:
        reports.refresh_sources(CHILD, source_kind='daily_report')
    assert len(calls) == 1
    assert ParentReportService(db).source_issues(CHILD)


def test_retry_never_adopts_another_child_report(flow, monkeypatch):
    db, repo, initial, delivery = flow
    reports = delivery.snapshots
    report = repo.get_daily_report(initial['report_id'])
    calls = []
    def create(payload):
        calls.append(payload['child_id'])
        raise DailySnapshotSourceChangedError('source changed')
    monkeypatch.setattr(reports, 'create_from_daily', create)
    monkeypatch.setattr(StudyRepository, 'get_daily_report', lambda *args, **kwargs: {**report, 'child_id': 'child-junior-3'})
    with pytest.raises(SnapshotStateError):
        reports._refresh_daily_snapshot(initial['report_id'], child_id=CHILD, initial_report=report)
    assert calls == [CHILD]


def test_unrelated_validation_failure_is_not_retried(flow, monkeypatch):
    db, repo, initial, delivery = flow
    calls = []
    def create(payload):
        calls.append(payload['report_id'])
        raise SnapshotStateError('invalid report')
    monkeypatch.setattr(delivery.snapshots, 'create_from_daily', create)
    with pytest.raises(SnapshotStateError, match='invalid report'):
        delivery.snapshots._refresh_daily_snapshot(initial['report_id'], child_id=CHILD)
    assert calls == [initial['report_id']]
