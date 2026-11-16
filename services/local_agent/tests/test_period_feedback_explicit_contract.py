"""Preserve V16 requirements without granting implicit authority to superseded A."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_followup import ReportFollowupConflict, ReportFollowupService
from test_period_adjustment_rebinding_http import journey, prepare, base
from test_period_report_reliable_evidence import flow, learning_clock, answer, report, CHILD, ITEM
from test_daily_report_evidence_versions import flow as daily_flow


@pytest.fixture(autouse=True)
def consistent_action_clock(monkeypatch, learning_clock):
    from home_study_agent import learning_state, parent_action_evidence
    monkeypatch.setattr(parent_action_evidence, 'datetime', learning_state.datetime)


TABLES = ('learning_evidence', 'parent_report_snapshot', 'parent_report_snapshot_feedback',
          'report_feedback_revision', 'period_report', 'audit_event', 'parent_action_snapshot',
          'parent_action_feedback_event', 'report_plan_adjustment_event', 'report_plan_feedback_receipt')


def durable_rows(connection):
    return {name: [tuple(row) for row in connection.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
            for name in TABLES}


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_old_implicit_period_rating_is_historical_not_current_or_permission(flow, kind):
    answer(flow)
    parents = ParentReportService(flow.db)
    old = parents.create_from_period(report(flow, kind))
    parents.approve(old['snapshot_id'])
    parents.record_feedback(old['snapshot_id'], 5, None, 'Synthetic useful report', 'feedback')
    followup = ReportFollowupService(flow.db)
    day = (datetime.fromisoformat(flow.clock[0]).date() + timedelta(days=1)).isoformat()
    with flow.db.connect() as c:
        before = durable_rows(c)
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        raw = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (old['snapshot_id'],)).fetchone()
        assert raw['status'] == 'approved'  # No refresh/revocation writer has run.
        current = followup.current_feedback(c, raw)
        assert current == {'feedback_current': None, 'feedback_current_revision': 1,
                           'feedback_current_status': 'source_invalid'}
        historical = followup.coaching_experiences(c, CHILD)
        assert len(historical) == 1 and historical[0]['snapshot_id'] == old['snapshot_id']
        assert historical[0]['approval_ref'] and historical[0]['revision'] == 1
        assert not historical[0]['execution_verified'] and not historical[0]['learning_effect_verified']
        first = followup.project_for_plan(CHILD, day, connection=c)
        assert first['items'] == [] and first['constraints'] == []
        assert followup.project_for_plan(CHILD, day, connection=c) == first
        assert c.total_changes == 0 and durable_rows(c) == before
    # Exact former implicit call must fail with no proposal, audit or learning write.
    with pytest.raises(ReportFollowupConflict, match='PLAN_ADJUSTMENT_SOURCE_CHANGED'):
        followup.propose_plan_adjustment(CHILD, old['snapshot_id'], 1,
            'daily_total_minutes_cap', 20, day, day, 'forbidden-implicit-cap')
    with flow.db.connect() as c:
        assert durable_rows(c) == before
    assert parents.get(old['snapshot_id'])['status'] == 'approved'
    assert parents.get(old['snapshot_id'])['content'] == old['content']


def bind(client, headers, target, day, key='explicit'):
    context = client.get(base() + '/contexts/' + target['snapshot_id'], headers=headers).json()['items'][0]
    response = client.post(base() + '/proposals', headers=headers, json={
        'binding': context['binding'], 'terms': {'constraint_type': 'daily_total_minutes_cap',
        'value_minutes': 20, 'start_date': day, 'end_date': day}, 'client_action_id': key + '-proposal'})
    assert response.status_code == 200, response.text
    row = response.json()
    response = client.post(base() + '/' + row['adjustment_id'] + '/review', headers=headers, json={
        'expected_revision': 1, 'decision': 'approve', 'review_hash': row['review_hash'],
        'evidence_seen': True, 'client_action_id': key + '-confirm'})
    assert response.status_code == 200, response.text
    return response.json()


def completed_report_pair(journey, kind):
    f, _, client, headers = journey
    answer(f)
    with f.db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (f.sid,))
        c.commit()
    response = client.post('/v1/sessions/' + f.sid + '/reflection/complete', json={
        'learned_text': 'Synthetic completed session', 'difficult_text': '', 'feeling': 'okay',
        'next_action': '', 'skipped': False, 'client_action_id': 'completed-period-learning'})
    assert response.status_code == 200, response.text
    with f.db.connect() as c:
        assert c.execute('SELECT status FROM report_job WHERE session_id=?', (f.sid,)).fetchone()[0] == 'completed'
    parents = ParentReportService(f.db)
    def generate_and_approve():
        response = client.post('/v1/children/' + CHILD + '/period-reports/generate', params={'report_type': kind}, headers=headers)
        assert response.status_code == 200, response.text
        draft = parents.create_from_period(response.json())
        response = client.post('/v1/parent/report-snapshots/' + draft['snapshot_id'] + '/approve', headers=headers, json={})
        assert response.status_code == 200, response.text
        return response.json()
    old = generate_and_approve()
    action = old['parent_action']
    old_path = '/v1/parent/children/' + CHILD + '/parent-actions/' + action['snapshot_id']
    f.clock[0] = (datetime.fromisoformat(f.clock[0]) + timedelta(minutes=1)).isoformat()
    response = client.put(old_path + '/feedback', headers=headers, json={
        'action_snapshot_id': action['snapshot_id'], 'client_action_id': 'original-A-execution',
        'expected_revision': 0, 'executed': True, 'executed_at': f.clock[0], 'effectiveness': 3,
        'stress_rating': 2, 'notes_summary': 'Synthetic original A action, not execution of its successor.'})
    assert response.status_code == 200, response.text
    old_action_feedback = response.json()
    response = client.post('/v1/parent/report-snapshots/' + old['snapshot_id'] + '/feedback', headers=headers,
        json={'helpfulness': 4, 'stress_rating': None, 'notes': 'Separate explicit next-day burden review', 'client_action_id': 'original-A-rating'})
    assert response.status_code == 200, response.text
    target = generate_and_approve()
    assert client.get(old_path, headers=headers).json()['feedback'] == old_action_feedback
    return old, target, old_action_feedback


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_current_projection_rechecks_unrefreshed_source_readonly_without_erasing_history(journey, kind):
    f, _, client, headers = journey
    old, target = prepare(journey, kind)
    followup = ReportFollowupService(f.db)
    day = (datetime.fromisoformat(f.clock[0]).date() + timedelta(days=1)).isoformat()
    binding = bind(client, headers, target, day)
    with f.db.connect() as c:
        before = durable_rows(c)
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        current = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (target['snapshot_id'],)).fetchone()
        assert ParentReportService.snapshot_source_current(c, current)
        assert followup.current_feedback(c, current) == {'feedback_current': None,
            'feedback_current_revision': 0, 'feedback_current_status': 'none'}
        projected = followup.project_for_plan(CHILD, day, connection=c)
        assert projected['items'] == []  # A rating is not copied onto B.
        assert [item['adjustment_id'] for item in projected['constraints']] == [binding['adjustment_id']]
        assert c.total_changes == 0 and durable_rows(c) == before
    with f.db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
        before = durable_rows(c)
        changes = c.total_changes
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        current = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (target['snapshot_id'],)).fetchone()
        assert current['status'] == 'approved'
        assert not ParentReportService.snapshot_source_current(c, current)
        projected = followup.project_for_plan(CHILD, day, connection=c)
        assert projected['items'] == [] and projected['constraints'] == []
        assert followup.project_for_plan(CHILD, day, connection=c) == projected
        assert len(followup.coaching_experiences(c, CHILD)) == 1  # Subjective history, not current evidence.
        assert c.total_changes == changes and durable_rows(c) == before
    parents = ParentReportService(Database(f.db.path))
    assert parents.get(target['snapshot_id'])['status'] == 'approved'
    assert parents.get(target['snapshot_id'])['content'] == target['content']
    assert parents.get(old['snapshot_id'])['content'] == old['content']


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_next_day_plan_consumes_current_reviewed_action_and_independent_explicit_cap(journey, kind):
    from home_study_agent.goal_program import GoalProgramService
    from test_report_followup_host import intake
    f, settings, client, headers = journey
    # Close the original period before next-day intake so new learning is not smuggled into B.
    day = f.reference + timedelta(days=6)
    if kind == 'monthly':
        day = (f.reference.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    f.clock[0] = day.isoformat() + 'T10:00:00+00:00'
    GoalProgramService(f.db).cycles(CHILD)
    old, target, historical_action = completed_report_pair(journey, kind)
    tomorrow = (day + timedelta(days=1)).isoformat()
    followup = ReportFollowupService(f.db)
    with pytest.raises(ReportFollowupConflict, match='PLAN_ADJUSTMENT_SOURCE_CHANGED'):
        followup.propose_plan_adjustment(CHILD, old['snapshot_id'], 1,
            'daily_total_minutes_cap', 20, tomorrow, tomorrow, 'old-implicit-permission')
    reviewed = client.get('/v1/parent/report-snapshots/' + target['snapshot_id'], headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    action = reviewed.json()['parent_action']
    assert action['review_binding']['snapshot_id'] == target['snapshot_id']
    assert action['feedback'] is None
    f.clock[0] = day.isoformat() + 'T10:05:00+00:00'
    root = '/v1/parent/children/' + CHILD + '/parent-actions/' + action['snapshot_id']
    executed = {'action_snapshot_id': action['snapshot_id'], 'client_action_id': 'actual-reviewed-B-action',
        'expected_revision': 0, 'executed': True, 'executed_at': f.clock[0], 'effectiveness': 3,
        'stress_rating': 2, 'notes_summary': 'Synthetic parent: performed the newly reviewed action after reading B.'}
    response = client.put(root + '/feedback', headers=headers, json=executed)
    assert response.status_code == 200, response.text
    action_receipt = response.json()
    before = followup.project_for_plan(CHILD, tomorrow)
    assert [item['feedback_id'] for item in before['items']] == [action_receipt['feedback_id']]
    assert action_receipt['feedback_id'] != historical_action['feedback_id']
    assert before['constraints'] == []  # Execution and rating alone never create a cap.
    bound = bind(client, headers, target, tomorrow)
    with f.db.connect() as c:
        journal = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
        assert not c.execute('SELECT 1 FROM parent_report_snapshot_feedback WHERE snapshot_id=?', (target['snapshot_id'],)).fetchone()
    f.clock[0] = tomorrow + 'T10:00:00+00:00'
    assert followup.project_for_plan(CHILD, tomorrow)['constraints'], 'cap before restart'
    client.finish_original_lifecycle()
    with TestClient(create_app(settings)) as reopened:
        h = {'X-HSC-Parent': reopened.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']}
        assert reopened.get(root, headers=h).json()['feedback'] == action_receipt
        old_root = '/v1/parent/children/' + CHILD + '/parent-actions/' + historical_action['action_snapshot_id']
        assert reopened.get(old_root, headers=h).json()['feedback'] == historical_action
        assert followup.project_for_plan(CHILD, tomorrow)['constraints'], 'cap after restart'
        sid = intake((f.db, f.repo, None, None))
        assert followup.project_for_plan(CHILD, tomorrow)['constraints'], 'cap after intake'
        response = reopened.post('/v1/sessions/' + sid + '/plan/generate', json={'client_action_id': 'real-reviewed-action-plan'})
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan['plan_items']
        assert sum(item['expected_minutes'] for item in plan['plan_items'] if item['status'] not in ('rescheduled', 'cancelled')) <= 20
        with f.db.connect() as c:
            receipt_id = c.execute('SELECT receipt_id FROM report_plan_feedback_receipt WHERE plan_id=?', (plan['plan']['plan_id'],)).fetchone()[0]
        stored = followup.get_plan_receipt(CHILD, receipt_id)
        assert stored['constraints_satisfied'] and stored['inputs_current']
        assert [item.get('feedback_id') for item in stored['inputs']['items']] == [action_receipt['feedback_id']]
        assert [item['adjustment_id'] for item in stored['inputs']['constraints']] == [bound['adjustment_id']]
        assert not stored['learning_effect_verified']
        response = reopened.put(root + '/feedback', headers=h, json={
            'action_snapshot_id': action['snapshot_id'], 'client_action_id': 'withdraw-B-action-only',
            'expected_revision': 1, 'operation': 'retract', 'reason': 'Parent corrected execution record, not the separate cap.'})
        assert response.status_code == 200, response.text
        assert followup.get_plan_receipt(CHILD, receipt_id)['inputs_current'] is False
        assert followup.get_plan_receipt(CHILD, receipt_id)['inputs'] == stored['inputs']
        projected = followup.project_for_plan(CHILD, tomorrow)
        assert projected['items'] == []
        assert [item['adjustment_id'] for item in projected['constraints']] == [bound['adjustment_id']]
    with f.db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal


def test_current_daily_rating_has_positive_readonly_projection_not_blanket_exclusion(daily_flow, learning_clock):
    db, repo, saved, _ = daily_flow
    parents = ParentReportService(db)
    old = parents.approve(parents.create_from_daily(repo.get_daily_report(saved['report_id']))['snapshot_id'])
    parents.record_feedback(old['snapshot_id'], 4, 2, 'Synthetic daily report rating', 'daily-current-rating')
    followup = ReportFollowupService(db)
    tomorrow = (datetime.fromisoformat(learning_clock[0]).date() + timedelta(days=1)).isoformat()
    with db.connect() as c:
        before = durable_rows(c)
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        raw = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (old['snapshot_id'],)).fetchone()
        assert followup.current_feedback(c, raw)['feedback_current_status'] == 'active'
        projected = followup.project_for_plan(CHILD, tomorrow, connection=c)
        assert len(projected['items']) == 1 and projected['items'][0]['snapshot_id'] == old['snapshot_id']
        assert projected['constraints'] == [] and not projected['items'][0]['learning_effect_verified']
        assert c.total_changes == 0 and durable_rows(c) == before
