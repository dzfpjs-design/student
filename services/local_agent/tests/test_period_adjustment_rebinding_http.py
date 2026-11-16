from datetime import datetime, timedelta
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.report_followup import ReportFollowupService
from test_period_report_reliable_evidence import flow, learning_clock, answer, report, CHILD, OTHER


@pytest.fixture
def journey(flow, tmp_path, request):
    settings = Settings(data_dir=tmp_path / 'app', database_path=flow.db.path, maintenance_enabled=False,native_launch_attestation_key=getattr(request,'param',None))
    with ExitStack()as original_lifecycle:
        client=original_lifecycle.enter_context(TestClient(create_app(settings)))
        client.finish_original_lifecycle=original_lifecycle.close
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        yield flow, settings, client, headers


def prepare(journey, kind='weekly', finish=False):
    f, _, client, headers = journey
    answer(f)
    if finish:
        with f.db.connect() as c:
            c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (f.sid,))
            c.commit()
        f.repo.complete_with_reflection(f.sid, '合成练习已结束', '', 'okay', '', False, 'finish-original')
    parents = ParentReportService(f.db)
    generated = client.post('/v1/children/' + CHILD + '/period-reports/generate',
                            params={'report_type': kind}, headers=headers)
    assert generated.status_code == 200, generated.text
    source = generated.json()
    old = parents.create_from_period(source)
    root = '/v1/parent/report-snapshots/' + old['snapshot_id']
    assert client.post(root + '/approve', headers=headers, json={}).status_code == 200
    assert client.post(root + '/feedback', headers=headers, json={
        'helpfulness': 2, 'stress_rating': 5, 'notes': '合成家长反馈：负担偏重',
        'client_action_id': 'parent-first-rating'}).status_code == 200
    generated = client.post('/v1/children/' + CHILD + '/period-reports/generate',
                            params={'report_type': kind}, headers=headers)
    assert generated.status_code == 200, generated.text
    fresh = generated.json()
    target = parents.create_from_period(fresh)
    assert client.post('/v1/parent/report-snapshots/' + target['snapshot_id'] + '/approve',
                       headers=headers, json={}).status_code == 200
    return old, target


def base():
    return '/v1/parent/children/' + CHILD + '/report-rebindings'


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_explicit_binding_http_restart_plan_projection_and_retraction(journey, kind):
    f, settings, client, headers = journey
    old, target = prepare(journey, kind)
    response = client.get(base() + '/contexts/' + target['snapshot_id'], headers=headers)
    assert response.status_code == 200, response.text
    context = response.json()['items'][0]
    assert context['feedback']['snapshot_id'] == old['snapshot_id']
    assert context['report']['snapshot_id'] == target['snapshot_id']
    assert context['report']['content'] == target['content']
    day = (datetime.fromisoformat(f.clock[0]).date() + timedelta(days=1)).isoformat()
    payload = {'binding': context['binding'], 'terms': {'constraint_type': 'daily_total_minutes_cap',
        'value_minutes': 20, 'start_date': day, 'end_date': day}, 'client_action_id': 'explicit-proposal'}
    before = ReportFollowupService(f.db).project_for_plan(CHILD, day)
    assert before['constraints'] == []
    saved = client.post(base() + '/proposals', headers=headers, json=payload)
    assert saved.status_code == 200, saved.text
    proposal = saved.json()
    assert proposal['operation'] == 'propose'
    assert client.get(base() + '/commands/explicit-proposal', headers=headers).json()['receipt'] == proposal
    assert client.post(base() + '/proposals', headers=headers, json=payload).json() == proposal
    command = {'expected_revision': 1, 'decision': 'approve', 'review_hash': proposal['review_hash'],
               'client_action_id': 'explicit-confirm', 'evidence_seen': True}
    approved = client.post(base() + '/' + proposal['adjustment_id'] + '/review', headers=headers, json=command)
    assert approved.status_code == 200, approved.text
    saved_approval = approved.json()
    with f.db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        projection = ReportFollowupService(f.db).project_for_plan(CHILD, day, connection=c)
        assert len(projection['constraints']) == 1
        assert projection['constraints'][0]['value_minutes'] == 20
        assert not projection['constraints'][0]['learning_effect_verified']
        assert c.total_changes == 0
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 1
        assert not c.execute('SELECT 1 FROM parent_report_snapshot_feedback WHERE snapshot_id=?', (target['snapshot_id'],)).fetchone()
    client.finish_original_lifecycle()
    with TestClient(create_app(settings)) as reopened:
        token = reopened.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']
        h = {'X-HSC-Parent': token}
        result = reopened.get(base() + '/commands/explicit-confirm', headers=h).json()
        assert result['receipt'] == saved_approval
        with f.db.connect() as c:
            target_row = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (target['snapshot_id'],)).fetchone()
            source_state = {'snapshot_status': target_row['status'],
                'current_source': ParentReportService.snapshot_source_current(c, target_row),
                'experiences': len(ReportFollowupService.coaching_experiences(c, CHILD)),
                'session_state': c.execute('SELECT state FROM daily_session WHERE session_id=?', (f.sid,)).fetchone()[0]}
        assert result['current']['source_current'], source_state
        assert reopened.post(base() + '/' + proposal['adjustment_id'] + '/review', headers=h, json=command).json() == saved_approval
        withdrawal = {**command, 'expected_revision': 2, 'decision': 'retract', 'client_action_id': 'explicit-retract'}
        withdrawal['review_hash'] = result['current']['review_hash']
        assert reopened.post(base() + '/' + proposal['adjustment_id'] + '/review', headers=h, json=withdrawal).status_code == 200
    assert ReportFollowupService(Database(f.db.path)).project_for_plan(CHILD, day)['constraints'] == []
    assert ParentReportService(f.db).get(old['snapshot_id'])['content'] == old['content']


@pytest.mark.parametrize('damage', ['cross_child', 'feedback_hash', 'target_version', 'missing_experience',
                                    'feedback_retracted', 'target_revoked', 'old_explicit_revoke'])
def test_proposal_rechecks_exact_authority_and_rolls_back(journey, damage):
    f, _, client, headers = journey
    old, target = prepare(journey)
    response = client.get(base() + '/contexts/' + target['snapshot_id'], headers=headers)
    assert response.status_code == 200, response.text
    binding = response.json()['items'][0]['binding']
    if damage == 'cross_child':
        binding['child_id'] = OTHER
    elif damage == 'feedback_hash':
        binding['experience_hash'] = 'forged'
    elif damage == 'target_version':
        binding['target']['source_version'] = 'forged'
    elif damage == 'missing_experience':
        binding['feedback_event_id'] = 'not-an-input'
    elif damage == 'feedback_retracted':
        ReportFollowupService(f.db).retract_feedback(CHILD, old['snapshot_id'], 1, 'withdraw', '家长更正')
    else:
        snapshot = target if damage == 'target_revoked' else old
        ParentReportService(f.db).revoke(snapshot['snapshot_id'], '家长明确撤回')
    day = datetime.fromisoformat(f.clock[0]).date().isoformat()
    response = client.post(base() + '/proposals', headers=headers, json={
        'binding': binding, 'terms': {'constraint_type': 'daily_total_minutes_cap', 'value_minutes': 20,
        'start_date': day, 'end_date': day}, 'client_action_id': 'reject-stale-proof'})
    assert response.status_code == 409, response.text
    with f.db.connect() as c:
        assert not c.execute('SELECT 1 FROM report_plan_adjustment_event').fetchone()


def test_parent_auth_and_other_child_context_do_not_leak(journey):
    _, _, client, headers = journey
    _, target = prepare(journey)
    assert client.get(base() + '/contexts/' + target['snapshot_id']).status_code == 401
    assert client.get(base().replace(CHILD, OTHER) + '/contexts/' + target['snapshot_id'], headers=headers).status_code == 404


def propose(journey):
    f, _, client, headers = journey
    old, target = prepare(journey)
    context = client.get(base() + '/contexts/' + target['snapshot_id'], headers=headers).json()['items'][0]
    day = datetime.fromisoformat(f.clock[0]).date().isoformat()
    payload = {'binding': context['binding'], 'terms': {'constraint_type': 'daily_total_minutes_cap',
        'value_minutes': 20, 'start_date': day, 'end_date': day}, 'client_action_id': 'saved-proposal'}
    response = client.post(base() + '/proposals', headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return old, target, payload, response.json()


@pytest.mark.parametrize('damage', ['feedback_retract', 'evidence_retract', 'source_label', 'review_hash', 'not_seen', 'family_limit', 'approval_witness'])
def test_confirmation_rechecks_sources_terms_and_explicit_consent(journey, damage):
    f, _, client, headers = journey
    old, target, payload, row = propose(journey)
    command = {'expected_revision': 1, 'decision': 'approve', 'review_hash': row['review_hash'],
               'evidence_seen': True, 'client_action_id': 'denied-confirm'}
    if damage == 'feedback_retract':
        ReportFollowupService(f.db).retract_feedback(CHILD, old['snapshot_id'], 1, 'withdraw-old', '家长撤回')
    elif damage == 'evidence_retract':
        f.reports.retract_evidence(CHILD, target['facts']['learning_summary']['evidence'][0]['evidence_ref'], '核对原答', 'withdraw-evidence')
    elif damage == 'review_hash':
        command['review_hash'] = 'forged'
    elif damage == 'not_seen':
        command['evidence_seen'] = False
    else:
        with f.db.connect() as c:
            if damage == 'source_label':
                node = c.execute('SELECT knowledge_node_id FROM learning_evidence WHERE child_id=? LIMIT 1', (CHILD,)).fetchone()[0]
                assert c.execute("UPDATE knowledge_node SET title='Changed original label' WHERE knowledge_node_id=?", (node,)).rowcount == 1
            elif damage == 'family_limit':
                c.execute('UPDATE family_rule SET max_learning_minutes=10 WHERE child_id=?', (CHILD,))
            else:
                c.execute("DELETE FROM audit_event WHERE audit_id=?", (payload['binding']['target']['approval']['audit_id'],))
            c.commit()
    response = client.post(base() + '/' + row['adjustment_id'] + '/review', headers=headers, json=command)
    assert response.status_code == 409, response.text
    with f.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM report_plan_adjustment_event').fetchone()[0] == 1
    # A stale proposal can still be explicitly rejected; history is not destroyed.
    command.update(decision='reject', review_hash=row['review_hash'], client_action_id='reject-proposal')
    assert client.post(base() + '/' + row['adjustment_id'] + '/review', headers=headers, json=command).status_code == 200


def test_legacy_review_cannot_bypass_rebinding_and_cancel_serializes_late_write(journey):
    _, _, client, headers = journey
    _, target, payload, row = propose(journey)
    old_route = '/v1/parent/children/' + CHILD + '/report-plan-adjustments/' + row['adjustment_id'] + '/review'
    assert client.post(old_route, headers=headers, json={'expected_revision': 1, 'decision': 'approve',
        'client_action_id': 'legacy-bypass'}).status_code == 409
    cancel = {'target_snapshot_id': target['snapshot_id']}
    assert client.post(base() + '/commands/never-sent/cancel', headers=headers, json={'target_snapshot_id': 'another-installation'}).status_code == 404
    assert client.get(base() + '/commands/never-sent', headers=headers).status_code == 404
    assert client.post(base() + '/commands/never-sent/cancel', headers=headers, json=cancel).json() == {'status': 'cancelled_not_committed'}
    assert client.get(base() + '/commands/never-sent', headers=headers).json() == {'status': 'cancelled_not_committed'}
    assert client.post(base() + '/proposals', headers=headers, json={**payload, 'client_action_id': 'never-sent'}).status_code == 409
    assert client.post(base() + '/commands/saved-proposal/cancel', headers=headers, json=cancel).json()['receipt'] == row
    assert client.post(base() + '/proposals', headers=headers, json={**payload, 'terms': {**payload['terms'], 'value_minutes': 19}}).status_code == 409


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_next_day_real_http_plan_consumes_reviewed_binding_without_new_rating(journey, kind):
    from home_study_agent.goal_program import GoalProgramService
    from test_report_followup_host import intake
    f, _, client, headers = journey
    day = f.reference + timedelta(days=6)
    if kind == 'monthly':
        day = (f.reference.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    f.clock[0] = datetime.combine(day, datetime.min.time()).isoformat() + '+00:00'
    GoalProgramService(f.db).cycles(CHILD)
    _, target = prepare(journey, kind, finish=True)
    context = client.get(base() + '/contexts/' + target['snapshot_id'], headers=headers).json()['items'][0]
    tomorrow = (day + timedelta(days=1)).isoformat()
    payload = {'binding': context['binding'], 'terms': {'constraint_type': 'daily_total_minutes_cap',
        'value_minutes': 20, 'start_date': tomorrow, 'end_date': tomorrow}, 'client_action_id': 'next-day-proposal'}
    row = client.post(base() + '/proposals', headers=headers, json=payload).json()
    approved = client.post(base() + '/' + row['adjustment_id'] + '/review', headers=headers, json={
        'expected_revision': 1, 'decision': 'approve', 'review_hash': row['review_hash'],
        'evidence_seen': True, 'client_action_id': 'next-day-confirm'})
    assert approved.status_code == 200, approved.text
    f.clock[0] = tomorrow + 'T10:00:00+00:00'
    with f.db.connect() as c:
        journal = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
    sid = intake((f.db, f.repo, None, None))
    response = client.post('/v1/sessions/' + sid + '/plan/generate', json={'client_action_id': 'real-next-day-plan'})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['plan_items']
    assert sum(item['expected_minutes'] for item in plan['plan_items'] if item['status'] not in ('cancelled', 'rescheduled')) <= 20
    with f.db.connect() as c:
        receipt_id = c.execute('SELECT receipt_id FROM report_plan_feedback_receipt WHERE plan_id=?', (plan['plan']['plan_id'],)).fetchone()[0]
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
    receipt = ReportFollowupService(Database(f.db.path)).get_plan_receipt(CHILD, receipt_id)
    assert receipt['constraints_satisfied'] and receipt['inputs_current']
    assert receipt['inputs']['constraints'][0]['adjustment_id'] == row['adjustment_id']
    assert not receipt['learning_effect_verified']


@pytest.mark.parametrize('same_key', [True, False])
def test_concurrent_confirmation_serializes_revision_and_exact_receipt(journey, same_key):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    f, _, client, headers = journey
    _, _, _, row = propose(journey)
    barrier = Barrier(2)
    def confirm(index):
        barrier.wait(timeout=10)
        return client.post(base() + '/' + row['adjustment_id'] + '/review', headers=headers, json={
            'expected_revision': 1, 'decision': 'approve', 'review_hash': row['review_hash'],
            'evidence_seen': True, 'client_action_id': 'parallel-' + str(0 if same_key else index)})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, range(2)))
    assert sorted(item.status_code for item in results) == ([200, 200] if same_key else [200, 409])
    if same_key:
        assert results[0].json() == results[1].json()
    with f.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM report_plan_adjustment_event').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM report_rebinding_command').fetchone()[0] == 2


@pytest.mark.parametrize('damage', ['feedback_retract', 'evidence_restore', 'explicit_old_revoke'])
def test_approved_binding_withdrawal_or_restoration_never_resurrects_old_consent(journey, damage):
    f, _, client, headers = journey
    old, target, payload, row = propose(journey)
    command = {'expected_revision': 1, 'decision': 'approve', 'review_hash': row['review_hash'],
               'evidence_seen': True, 'client_action_id': 'prior-consent'}
    response = client.post(base() + '/' + row['adjustment_id'] + '/review', headers=headers, json=command)
    assert response.status_code == 200, response.text
    original_receipt = response.json()
    day = payload['terms']['start_date']
    assert ReportFollowupService(f.db).project_for_plan(CHILD, day)['constraints']
    if damage == 'feedback_retract':
        ReportFollowupService(f.db).retract_feedback(CHILD, old['snapshot_id'], 1, 'withdraw-after-consent', '家长撤回')
    elif damage == 'explicit_old_revoke':
        ParentReportService(f.db).revoke(old['snapshot_id'], '家长明确撤回')
    else:
        ref = target['facts']['learning_summary']['evidence'][0]['evidence_ref']
        f.reports.retract_evidence(CHILD, ref, '核对', 'binding-retract-evidence')
        assert not ReportFollowupService(f.db).project_for_plan(CHILD, day)['constraints']
        f.reports.restore_evidence(CHILD, ref, '已核对', 'binding-restore-evidence')
    assert not ReportFollowupService(f.db).project_for_plan(CHILD, day)['constraints']
    fetched = client.get(base() + '/commands/prior-consent', headers=headers).json()
    assert fetched['receipt'] == original_receipt
    assert not fetched['current']['source_current'] and not fetched['current']['effective_as_guard']
    if damage == 'evidence_restore':
        generated = client.post('/v1/children/' + CHILD + '/period-reports/generate', params={'report_type': 'weekly'}, headers=headers)
        assert generated.status_code == 200, generated.text
        successor = ParentReportService(f.db).create_from_period(generated.json())
        assert successor['snapshot_id'] != target['snapshot_id']
        assert client.post('/v1/parent/report-snapshots/' + successor['snapshot_id'] + '/approve', headers=headers, json={}).status_code == 200
        options = client.get(base() + '/contexts/' + successor['snapshot_id'], headers=headers).json()['items']
        assert options
        new_payload = {**payload, 'binding': options[0]['binding'], 'client_action_id': 'new-version-proposal'}
        fresh = client.post(base() + '/proposals', headers=headers, json=new_payload).json()
        assert not ReportFollowupService(f.db).project_for_plan(CHILD, day)['constraints']
        response = client.post(base() + '/' + fresh['adjustment_id'] + '/review', headers=headers, json={
            **command, 'review_hash': fresh['review_hash'], 'client_action_id': 'new-version-consent'})
        assert response.status_code == 200, response.text
        projected = ReportFollowupService(f.db).project_for_plan(CHILD, day)['constraints']
        assert len(projected) == 1 and projected[0]['adjustment_id'] == fresh['adjustment_id']


def test_binding_and_command_are_immutable_and_privacy_cascades(journey):
    import json
    from pathlib import Path
    import sqlite3
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    f, _, client, headers = journey
    _, target, _, _ = propose(journey)
    client.post(base() + '/commands/cancelled/cancel', headers=headers, json={'target_snapshot_id': target['snapshot_id']})
    tables = ('report_adjustment_binding', 'report_rebinding_command', 'report_rebinding_cancelled_command')
    with f.db.connect() as c:
        for table in tables:
            with pytest.raises(sqlite3.IntegrityError, match='APPEND_ONLY'):
                c.execute('UPDATE ' + table + " SET child_id=child_id")
            c.rollback()
        # Immutable association/receipt rows follow deletion of their source event.
        c.execute('DELETE FROM report_plan_adjustment_event WHERE child_id=?', (CHILD,))
        for table in tables[:2]:
            assert not c.execute('SELECT 1 FROM ' + table + ' WHERE child_id=?', (CHILD,)).fetchone()
        c.rollback()
    privacy = PrivacyLifecycleService(f.db, f.db.path.parent)
    exported = json.loads(Path(privacy.export_child(CHILD)['path']).read_text())
    assert exported['report_adjustment_bindings'] and exported['report_rebinding_commands']
    assert exported['report_rebinding_cancelled_commands']
    assert not json.loads(Path(privacy.export_child(OTHER)['path']).read_text())['report_adjustment_bindings']
    privacy.delete_child_scope(CHILD, 'reports', '确认删除', 'erase-synthetic-rebinding')
    with f.db.connect() as c:
        for table in tables:
            assert not c.execute('SELECT 1 FROM ' + table + ' WHERE child_id=?', (CHILD,)).fetchone()
