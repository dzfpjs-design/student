"""Reviewed weekly/monthly actions share the existing durable action ledger."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_action_evidence import ParentActionEvidenceService
from home_study_agent.parent_reports import ParentReportService, SnapshotStateError
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.report_followup import ReportFollowupService
from home_study_agent.report_delivery import ReportDeliveryService
from test_parent_coaching_feedback_semantics import report as daily_report
from test_period_report_reliable_evidence import flow, answer, report, CHILD, OTHER
from test_daily_learning_closure_audit import learning_clock
from family_budget_clock import next_family_evening
from test_parent_weakness_action_journey import weak_report
from test_period_adjustment_rebinding_http import journey


def approved(f, kind='weekly', decision='custom'):
    answer(f, '90')
    parents = ParentReportService(f.db)
    draft = parents.create_from_period(report(f, kind))
    saved = parents.approve(draft['snapshot_id'], parent_edit_patch={
        'coaching_decision': decision, 'coaching_action': '合成家长行动：一起画一步',
        'coaching_script': '先听你选哪一步。', 'coaching_duration_minutes': 2})
    return parents, saved


def record_action(actions, frozen, key='period-feedback', **patch):
    payload = dict(child_id=CHILD, snapshot_id=frozen['snapshot_id'], client_action_id=key,
        expected_revision=0, executed=True, executed_at=datetime.now(timezone.utc).isoformat(),
        effectiveness=3, stress_rating=2, notes_summary='合成自述，不是因果或掌握证明')
    payload.update(patch)
    return actions.record_snapshot_feedback(**payload), payload


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_period_review_execution_restart_retract_and_source_invalidation(flow, kind):
    f = flow
    parents, saved = approved(f, kind)
    frozen = saved['parent_action']
    assert frozen['binding_status'] == 'bound'
    assert frozen['source_kind'] == 'period_report'
    assert frozen['report_id'] is None
    assert frozen['period_report_id'] == saved['source_id'].rpartition('@')[0]
    assert frozen['review_binding']['decision'] == 'custom'
    assert frozen['coaching']['action']['what'] == '合成家长行动：一起画一步'
    actions = ParentActionEvidenceService(f.db)
    receipt, payload = record_action(actions, frozen)
    assert receipt['period_report_id'] == frozen['period_report_id']
    assert receipt['evidence_kind'] == 'parent_reported_action'
    assert receipt['action_snapshot_id'] == frozen['snapshot_id']
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    followup = ReportFollowupService(f.db)
    projected = followup.project_for_plan(CHILD, tomorrow)
    assert [item['feedback_id'] for item in projected['items'] if item['evidence_kind'] == 'parent_reported_action'] == [receipt['feedback_id']]
    assert projected['constraints'] == []
    assert projected['items'][0]['learning_effect_verified'] is False
    restarted = ParentActionEvidenceService(Database(f.db.path))
    restarted.initialize()
    assert restarted.record_snapshot_feedback(**payload) == receipt
    assert restarted.get(CHILD, frozen['snapshot_id'])['feedback'] == receipt
    with f.db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        before = c.total_changes
        assert restarted.current_for_review(c, CHILD, saved['snapshot_id'])['snapshot_id'] == frozen['snapshot_id']
        assert c.total_changes == before
        with pytest.raises(ValueError, match='NOT_FOUND_OR_NOT_OWNED'):
            restarted.current_for_review(c, OTHER, saved['snapshot_id'])
    with pytest.raises(ValueError, match='NOT_FOUND_OR_NOT_OWNED'):
        restarted.record_snapshot_feedback(**{**payload, 'child_id': OTHER})
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        restarted.record_snapshot_feedback(**{**payload, 'client_action_id': 'stale-revision'})
    ref = saved['facts']['learning_summary']['evidence'][0]['evidence_ref']
    f.reports.retract_evidence(CHILD, ref, '重新核对来源', 'withdraw-period-source')
    assert followup.project_for_plan(CHILD, tomorrow)['items'] == []
    assert parents.get(saved['snapshot_id'])['parent_action']['binding_status'] == 'unknown'
    with pytest.raises(ValueError, match='REVIEW_NOT_CURRENT'):
        restarted.record_snapshot_feedback(**{**payload, 'client_action_id': 'late-after-withdraw', 'expected_revision': 1})
    with f.db.connect() as c:
        assert restarted.feedback_for_child(c, CHILD) == []
    withdrawn = restarted.record_snapshot_feedback(child_id=CHILD, snapshot_id=frozen['snapshot_id'],
        client_action_id='withdraw-feedback', expected_revision=1, operation='retract', reason='原反馈不适用')
    assert withdrawn['revision'] == 2
    assert [event['operation'] for event in restarted.history(CHILD, frozen['snapshot_id'])] == ['record', 'retract']
    assert restarted.get(CHILD, frozen['snapshot_id'])['coaching'] == frozen['coaching']


def test_period_skip_has_no_execution_score_and_draft_has_no_bound_action(flow):
    f = flow
    parents, saved = approved(f, decision='skip')
    frozen = saved['parent_action']
    assert frozen['execution_allowed'] is False
    assert frozen['coaching']['action'] is None
    with pytest.raises(ValueError, match='SKIPPED_NOT_EXECUTABLE'):
        record_action(ParentActionEvidenceService(f.db), frozen)
    parents.revoke(saved['snapshot_id'], '重新核对')
    assert parents.get(saved['snapshot_id'])['parent_action']['binding_status'] == 'unknown'


def test_period_source_change_rolls_back_review_edit_and_approval_event(flow):
    f = flow
    answer(f)
    parents = ParentReportService(f.db)
    draft = parents.create_from_period(report(f))
    ref = draft['facts']['learning_summary']['evidence'][0]['evidence_ref']
    f.reports.retract_evidence(CHILD, ref, '待核对', 'source-withdrawal')
    with pytest.raises(SnapshotStateError):
        parents.approve(draft['snapshot_id'], parent_edit_patch={'coaching_decision': 'custom', 'coaching_action': '不能批准的旧行动'})
    with f.db.connect() as c:
        row = c.execute('SELECT status,parent_edit_json,approved_at FROM parent_report_snapshot WHERE snapshot_id=?', (draft['snapshot_id'],)).fetchone()
        assert row['status'] == 'draft' and row['approved_at'] is None
        assert json.loads(row['parent_edit_json']) == {}
        assert not c.execute("SELECT 1 FROM audit_event WHERE action='parent_report_snapshot_approved'").fetchone()


def test_mixed_source_restore_reapproval_and_period_hard_delete_preserve_other_child(flow, tmp_path):
    f = flow
    parents, saved = approved(f)
    frozen = saved['parent_action']
    actions = ParentActionEvidenceService(f.db)
    receipt, _ = record_action(actions, frozen)
    with f.db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (f.sid,))
        c.commit()
    f.repo.complete_with_reflection(f.sid, '', '', 'okay', '', True, 'close-first-child-before-switch')
    other = daily_report(f.repo, child=OTHER)['parent_action']
    memory = sqlite3.connect(':memory:')
    memory.row_factory = sqlite3.Row
    memory.execute('PRAGMA foreign_keys=ON')
    with f.db.connect() as c:
        c.backup(memory)
    memory.execute('BEGIN IMMEDIATE')
    ReportDeliveryService.quarantine_after_restore(memory, 'mixed-period-restore')
    memory.commit()
    restored_db = Database(tmp_path / 'restored-mixed.db')
    with restored_db.connect() as c:
        memory.backup(c)
    memory.close()
    restarted = ParentActionEvidenceService(restored_db)
    restarted.initialize()
    review = ParentReportService(restored_db)
    assert review.get(saved['snapshot_id'])['status'] == 'draft'
    assert review.get(saved['snapshot_id'])['approved_at'] == saved['approved_at']
    assert review.get(saved['snapshot_id'])['parent_action']['binding_status'] == 'unknown'
    assert restarted.get(CHILD, frozen['snapshot_id'])['feedback'] == receipt
    renewed = review.approve(saved['snapshot_id'])['parent_action']
    assert renewed['snapshot_id'] != frozen['snapshot_id']
    assert renewed['version'] == frozen['version'] + 1 and renewed['feedback'] is None
    assert renewed['review_binding']['approval_event_id'] != frozen['review_binding']['approval_event_id']
    with restored_db.connect() as c:
        assert restarted.feedback_for_child(c, CHILD) == []
        c.execute('DELETE FROM period_report WHERE period_report_id=?', (frozen['period_report_id'],))
        c.commit()
        assert c.execute('SELECT COUNT(*) FROM parent_action_feedback_event WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM parent_action_snapshot WHERE period_report_id=?', (frozen['period_report_id'],)).fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert restarted.get(OTHER, other['snapshot_id']) == other
    assert review.get(saved['snapshot_id'])['parent_action']['binding_status'] == 'unknown'


def test_real_next_day_plan_consumes_reviewed_period_feedback_and_only_explicit_cap(journey, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent import learning_state, parent_action_evidence
    from home_study_agent.app import create_app
    from home_study_agent.goal_program import GoalProgramService
    from home_study_agent.report_followup import ReportFollowupConflict
    from test_period_feedback_explicit_contract import bind, completed_report_pair, base
    from test_report_followup_host import intake
    f, settings, client, headers = journey
    monkeypatch.setattr(parent_action_evidence, 'datetime', learning_state.datetime)
    period_end = f.reference + timedelta(days=6)
    f.clock[0] = period_end.isoformat() + 'T10:00:00+00:00'
    GoalProgramService(f.db).cycles(CHILD)
    # Same qualified wrong-answer scenario, with the real reflection/report job closed.
    old, target, original_action = completed_report_pair(journey, 'weekly')
    day = (period_end + timedelta(days=1)).isoformat()
    followup = ReportFollowupService(f.db)
    with pytest.raises(ReportFollowupConflict, match='PLAN_ADJUSTMENT_SOURCE_CHANGED'):
        followup.propose_plan_adjustment(CHILD, old['snapshot_id'], 1,
            'daily_total_minutes_cap', 20, day, day, 'period-cap-old-implicit')
    with f.db.connect() as c:
        history = followup.coaching_experiences(c, CHILD)
        assert len(history) == 1 and history[0]['snapshot_id'] == old['snapshot_id']
        assert history[0]['revision'] == 1 and history[0]['approval_ref']
        assert not c.execute('SELECT 1 FROM parent_report_snapshot_feedback WHERE snapshot_id=?', (target['snapshot_id'],)).fetchone()

    reviewed = client.get('/v1/parent/report-snapshots/' + target['snapshot_id'], headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    action = reviewed.json()['parent_action']
    assert action['review_binding']['snapshot_id'] == target['snapshot_id'] != old['snapshot_id']
    assert action['feedback'] is None
    f.clock[0] = period_end.isoformat() + 'T10:05:00+00:00'
    root = '/v1/parent/children/' + CHILD + '/parent-actions/' + action['snapshot_id']
    executed = client.put(root + '/feedback', headers=headers, json={
        'action_snapshot_id': action['snapshot_id'], 'client_action_id': 'period-reviewed-B-execution',
        'expected_revision': 0, 'executed': True, 'executed_at': f.clock[0], 'effectiveness': 3,
        'stress_rating': 2, 'notes_summary': 'Performed the independently reviewed successor action.'})
    assert executed.status_code == 200, executed.text
    receipt = executed.json()
    assert receipt['feedback_id'] != original_action['feedback_id']
    assert [item['feedback_id'] for item in followup.project_for_plan(CHILD, day)['items']] == [receipt['feedback_id']]
    assert followup.project_for_plan(CHILD, day)['constraints'] == []
    bound = bind(client, headers, target, day, 'period-cap')
    with f.db.connect() as c:
        journal = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
    f.clock[0] = day + 'T10:00:00+00:00'
    client.finish_original_lifecycle()
    with TestClient(create_app(settings)) as reopened:
        h = {'X-HSC-Parent': reopened.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']}
        assert reopened.get(base() + '/commands/period-cap-confirm', headers=h).json()['receipt'] == bound
        assert reopened.get(root, headers=h).json()['feedback'] == receipt
        old_root = '/v1/parent/children/' + CHILD + '/parent-actions/' + original_action['action_snapshot_id']
        assert reopened.get(old_root, headers=h).json()['feedback'] == original_action
        sid = intake((f.db, f.repo, None, None))
        response = reopened.post('/v1/sessions/' + sid + '/plan/generate', json={'client_action_id': 'period-action-next-day'})
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan['plan_items']
        assert sum(item['expected_minutes'] for item in plan['plan_items'] if item['status'] not in ('rescheduled', 'cancelled')) <= 20
        with f.db.connect() as c:
            receipt_id = c.execute('SELECT receipt_id FROM report_plan_feedback_receipt WHERE plan_id=?', (plan['plan']['plan_id'],)).fetchone()[0]
        stored = followup.get_plan_receipt(CHILD, receipt_id)
        assert stored['constraints_satisfied'] and stored['inputs_current']
        assert [item.get('feedback_id') for item in stored['inputs']['items']] == [receipt['feedback_id']]
        assert [item['adjustment_id'] for item in stored['inputs']['constraints']] == [bound['adjustment_id']]
        assert not stored['learning_effect_verified']
        withdrawn = reopened.put(root + '/feedback', headers=h, json={
            'action_snapshot_id': action['snapshot_id'], 'client_action_id': 'withdraw-period-plan-feedback',
            'expected_revision': 1, 'operation': 'retract', 'reason': '重新核对执行记录'})
        assert withdrawn.status_code == 200, withdrawn.text
    followup = ReportFollowupService(Database(f.db.path))
    assert followup.get_plan_receipt(CHILD, receipt_id)['inputs_current'] is False
    assert followup.get_plan_receipt(CHILD, receipt_id)['inputs'] == stored['inputs']
    projected = followup.project_for_plan(CHILD, day)
    assert projected['items'] == []
    assert [item['adjustment_id'] for item in projected['constraints']] == [bound['adjustment_id']]
    with f.db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
