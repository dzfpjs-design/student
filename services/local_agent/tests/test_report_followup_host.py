"""Report input history through real planning, parent HTTP and data lifecycle."""
from datetime import date, datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.report_followup import ReportFollowupService, ReportFollowupConflict
from home_study_agent.family_clock import family_today
from family_budget_clock import next_family_evening
from test_daily_report_evidence_versions import CHILD, flow
from test_companion_activity_host import NoCloud


def yesterday_feedback(flow, monkeypatch):
    from home_study_agent import parent_reports
    from home_study_agent.goal_program import GoalProgramService
    db, repo, report, _ = flow
    GoalProgramService(db).seed_defaults()
    report = repo.get_daily_report(report['report_id'])
    book = ParentReportService(db)
    snapshot = book.approve(book.create_from_daily(report)['snapshot_id'])
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with monkeypatch.context() as clock:
        clock.setattr(parent_reports, 'utc_now', lambda: yesterday)
        book.record_feedback(snapshot['snapshot_id'], 4, 3, 'Helpful explanation, no action attempted', 'prior-feedback')
    return snapshot['snapshot_id']


def intake(flow):
    db, repo, _, _ = flow
    sid = repo.start_session(CHILD, str(uuid4()))['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=60 WHERE child_id=?', (CHILD,))
        c.commit()
    repo.add_homework(sid, '数学', '今天的校内作业', 15, 'written', None, None, None, None, None, str(uuid4()))
    return sid


def test_real_plan_reads_prior_feedback_atomically_without_claiming_adoption(flow, monkeypatch):
    db, repo, _, _ = flow
    snapshot = yesterday_feedback(flow, monkeypatch)
    sid = intake(flow)
    result = repo.generate_plan(sid, 'new-feedback-plan')
    assert result['plan_items']
    with db.connect() as c:
        row = c.execute('SELECT * FROM report_plan_feedback_receipt WHERE child_id=?', (CHILD,)).fetchone()
        assert row is not None
        assert json.loads(row['inputs_json'])['items'][0]['snapshot_id'] == snapshot
        assert c.execute('SELECT total_minutes FROM daily_plan WHERE plan_id=?', (row['plan_id'],)).fetchone()[0] <= 60
    reopened = ReportFollowupService(Database(db.path))
    receipt = reopened.get_plan_receipt(CHILD, row['receipt_id'])
    assert receipt['inputs_current'] and receipt['plan_current']
    assert receipt['status'] == 'read_only'
    assert not receipt['plan_adjustment_verified'] and not receipt['learning_effect_verified']
    reopened.retract_feedback(CHILD, snapshot, 1, 'correct-feedback', 'Selected the wrong rating')
    assert not reopened.get_plan_receipt(CHILD, row['receipt_id'])['inputs_current']


def test_report_receipt_failure_rolls_back_real_plan_not_just_receipt(flow, monkeypatch):
    db, repo, _, _ = flow
    sid = intake(flow)
    def fail(*args, **kwargs):
        assert kwargs['connection'].in_transaction
        raise ReportFollowupConflict('PLAN_FEEDBACK_CHANGED')
    monkeypatch.setattr(ReportFollowupService, 'record_plan_read', fail)
    with pytest.raises(ReportFollowupConflict):
        repo.generate_plan(sid, 'failed-feedback-plan')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 0
        assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'homework_capture'
        assert c.execute('SELECT COUNT(*) FROM report_plan_feedback_receipt').fetchone()[0] == 0


def test_parent_http_feedback_retraction_auth_ownership_and_restart(flow, monkeypatch, tmp_path):
    db, _, _, _ = flow
    snapshot = yesterday_feedback(flow, monkeypatch)
    root = f'/v1/parent/children/{CHILD}/report-snapshots/{snapshot}'
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    body = {'expected_revision': 1, 'client_action_id': 'parent-correction', 'reason': 'Wrong rating'}
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        assert client.get(root + '/feedback-history').status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        assert client.get(root.replace(CHILD, 'child-junior-3') + '/feedback-history', headers=headers).status_code == 404
        original = client.get(root + '/feedback-history', headers=headers)
        assert original.headers['cache-control'] == 'no-store'
        assert len(original.json()['items']) == 1
        first = client.post(root + '/feedback-retractions', headers=headers, json=body)
        assert first.status_code == 200, first.text
        assert client.post(root + '/feedback-retractions', headers=headers, json=body).json()['event_id'] == first.json()['event_id']
        assert client.post(root + '/feedback-retractions', headers=headers, json={**body, 'client_action_id': 'stale-request'}).status_code == 409
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '7391'}).json()['parent_token']
        history = client.get(root + '/feedback-history', headers={'X-HSC-Parent': token}).json()['items']
        assert [row['operation'] for row in history] == ['record', 'retract']


def test_report_scope_export_and_delete_erases_frozen_plan_inputs(flow, monkeypatch, tmp_path):
    db, repo, _, _ = flow
    yesterday_feedback(flow, monkeypatch)
    sid = intake(flow)
    repo.generate_plan(sid, 'privacy-feedback-plan')
    lifecycle = PrivacyLifecycleService(db, tmp_path)
    lifecycle.initialize()
    with open(lifecycle.export_child(CHILD)['path'], encoding='utf-8') as stream:
        exported = json.load(stream)
    assert len(exported['report_feedback_revisions']) == 1
    assert len(exported['report_plan_feedback_receipts']) == 1
    lifecycle.delete_child_scope('child-junior-3', 'reports', '确认删除', str(uuid4()))
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM report_plan_feedback_receipt').fetchone()[0] == 1
    assert lifecycle.delete_child_scope(CHILD, 'reports', '确认删除', str(uuid4()))['status'] == 'completed'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM report_feedback_revision').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM report_plan_feedback_receipt').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_approved_feedback_caps_constrain_real_short_evening_plan(flow, monkeypatch):
    db, repo, _, _ = flow
    snapshot = yesterday_feedback(flow, monkeypatch)
    _, today = next_family_evening(monkeypatch)
    sid = intake(flow)
    service = ReportFollowupService(db)
    for kind, value in [('daily_total_minutes_cap', 20), ('task_segment_minutes_cap', 5)]:
        proposal = service.propose_plan_adjustment(CHILD, snapshot, 1, kind, value, today, today, 'propose-' + kind)
        service.review_plan_adjustment(CHILD, proposal['adjustment_id'], 1, 'approve', 'approve-' + kind)
    workspace = repo.generate_plan(sid, 'short-evening-plan')
    scheduled = [row for row in workspace['plan_items'] if row['status'] not in ('rescheduled', 'cancelled')]
    assert any(row['source_type'] == 'school_homework' for row in scheduled)
    assert sum(row['expected_minutes'] for row in scheduled) <= 20
    assert max(row['expected_minutes'] for row in scheduled if row['source_type'] != 'planned_break') <= 5
    assert any(row['status'] == 'rescheduled' for row in workspace['plan_items'])
    with db.connect() as c:
        row = c.execute('SELECT receipt_id FROM report_plan_feedback_receipt WHERE child_id=?', (CHILD,)).fetchone()
    receipt = service.get_plan_receipt(CHILD, row[0])
    assert len(receipt['inputs']['constraints']) == 2
    assert receipt['constraints_satisfied'] and not receipt['learning_effect_verified']


@pytest.mark.parametrize('explicit_release', [False, True])
def test_source_change_keeps_parent_cap_until_explicit_release_in_real_plan(flow, monkeypatch, explicit_release):
    from test_daily_report_evidence_versions import retire_source
    db, repo, report, _ = flow
    snapshot = yesterday_feedback(flow, monkeypatch)
    service = ReportFollowupService(db)
    _, today = next_family_evening(monkeypatch)
    proposal = service.propose_plan_adjustment(CHILD, snapshot, 1, 'daily_total_minutes_cap', 20, today, today, 'cap-proposal')
    service.review_plan_adjustment(CHILD, proposal['adjustment_id'], 1, 'approve', 'cap-approved')
    retire_source(db, repo, report)
    if explicit_release:
        service.review_plan_adjustment(CHILD, proposal['adjustment_id'], 2, 'retract', 'explicit-release')
    sid = intake(flow)
    repo.generate_plan(sid, 'source-changed-daily-plan')
    with db.connect() as c:
        receipt = c.execute('SELECT * FROM report_plan_feedback_receipt WHERE child_id=?', (CHILD,)).fetchone()
        total = c.execute('SELECT total_minutes FROM daily_plan WHERE plan_id=?', (receipt['plan_id'],)).fetchone()[0]
    inputs = json.loads(receipt['inputs_json'])
    assert inputs['items'] == []
    if explicit_release:
        assert inputs['constraints'] == []
    else:
        assert total <= 20
        assert inputs['constraints'][0]['authority'] == 'retained_parent_safety_guard'
        assert inputs['constraints'][0]['requires_review'] is True
    assert ReportFollowupService(Database(db.path)).get_plan_receipt(CHILD, receipt['receipt_id'])['constraints_satisfied']


def test_withdrawn_report_rating_does_not_reenter_report_coaching(flow, monkeypatch):
    db, repo, report, _ = flow
    snapshot = yesterday_feedback(flow, monkeypatch)
    sid = report['session_id']
    assert repo.report_evidence(sid)['report_feedback']
    ReportFollowupService(db).retract_feedback(CHILD, snapshot, 1, 'withdraw-before-next-report', 'Not applicable')
    assert repo.report_evidence(sid)['report_feedback'] == []


def test_parent_http_adjustment_survives_restart_and_does_not_bypass_review(flow, monkeypatch, tmp_path):
    db, repo, report, delivery = flow
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    values = {'expected_feedback_revision': 1, 'constraint_type': 'daily_total_minutes_cap', 'value_minutes': 30,
              'start_date': family_today().isoformat(), 'end_date': family_today().isoformat(), 'client_action_id': 'parent-proposal-cap'}
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        # App startup adds catalog sources. Approve the current report, not the
        # pre-migration fixture that startup correctly supersedes.
        current_report = repo.get_daily_report(report['report_id'])
        snapshot = yesterday_feedback((db, repo, current_report, delivery), monkeypatch)
        root = f'/v1/parent/children/{CHILD}/report-snapshots/{snapshot}/plan-adjustments'
        assert client.post(root, json=values).status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        proposed = client.post(root, json=values, headers=headers)
        assert proposed.status_code == 200, proposed.text
        aid = proposed.json()['adjustment_id']
        inputs = ReportFollowupService(db).project_for_plan(CHILD, family_today().isoformat())
        assert inputs['constraints'] == [] and len(inputs['candidates']) == 1
        assert client.get(root.replace(CHILD, 'child-junior-3'), headers=headers).status_code == 404
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '7391'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        saved = client.get(root, headers=headers)
        assert saved.status_code == 200 and saved.headers['cache-control'] == 'no-store'
        assert saved.json()['items'][0]['adjustment_id'] == aid
        review_path = f'/v1/parent/children/{CHILD}/report-plan-adjustments/{aid}/review'
        body = {'expected_revision': 1, 'decision': 'approve', 'client_action_id': 'parent-approve-cap'}
        approved = client.post(review_path, json=body, headers=headers)
        assert approved.status_code == 200, approved.text
        assert client.post(review_path, json=body, headers=headers).json()['revision'] == 2
        assert client.post(review_path, json={**body, 'client_action_id': 'stale-approve-cap'}, headers=headers).status_code == 409
        assert len(ReportFollowupService(db).project_for_plan(CHILD, family_today().isoformat())['constraints']) == 1
