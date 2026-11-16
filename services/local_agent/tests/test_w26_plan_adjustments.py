from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from home_study_agent.report_followup import ReportFollowupService, ReportFollowupConflict
from test_daily_report_evidence_versions import CHILD, flow, retire_source
from test_w26_report_followup import setup_feedback, plan


def propose(service, snapshot, kind='daily_total_minutes_cap', value=10):
    start = (date.today()+timedelta(days=1)).isoformat()
    end = (date.today()+timedelta(days=60)).isoformat()
    return service.propose_plan_adjustment(CHILD, snapshot['snapshot_id'], 1, kind, value, start, end, 'propose-'+kind)


def test_candidate_cannot_constrain_until_explicit_approval_and_guards_real_plan(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    candidate = propose(service, snapshot)
    assert service.project_for_plan(CHILD, day)['constraints'] == []
    assert len(service.project_for_plan(CHILD, day)['candidates']) == 1
    approved = service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, 'approve', 'approve')
    assert approved['revision'] == 2
    with flow[0].connect() as c:
        c.execute("UPDATE daily_plan SET total_minutes=30 WHERE plan_id='next-plan'")
        c.commit()
    inputs = service.project_for_plan(CHILD, day)
    with pytest.raises(ReportFollowupConflict, match='PLAN_CONSTRAINT_VIOLATION'):
        service.record_plan_read(CHILD, 'next-plan', inputs['fingerprint'], 'invalid-plan')
    with flow[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM report_plan_feedback_receipt').fetchone()[0] == 0
        c.execute("UPDATE daily_plan SET total_minutes=5 WHERE plan_id='next-plan'")
        c.commit()
    receipt = service.record_plan_read(CHILD, 'next-plan', inputs['fingerprint'], 'valid-plan')
    assert receipt['constraints_satisfied'] is True
    assert receipt['learning_effect_verified'] is False
    with flow[0].connect() as c:
        c.execute("UPDATE daily_plan SET total_minutes=6 WHERE plan_id='next-plan'")
        c.commit()
    assert service.get_plan_receipt(CHILD, receipt['receipt_id'])['plan_current'] is False
    assert ReportFollowupService(flow[0]).get_adjustment(CHILD, candidate['adjustment_id'])['revision'] == 2
    future = (date.today()+timedelta(days=30)).isoformat()
    assert len(service.project_for_plan(CHILD, future)['constraints']) == 1
    assert service.project_for_plan(CHILD, (date.today()+timedelta(days=61)).isoformat())['constraints'] == []


def test_source_withdrawal_keeps_parent_safety_guard_but_explicit_retraction_releases_it(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    candidate = propose(service, snapshot)
    with pytest.raises(KeyError):
        service.review_plan_adjustment('child-junior-3', candidate['adjustment_id'], 1, 'approve', 'wrong-child')
    service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, 'approve', 'approve')
    service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 2, 'retract', 'retract')
    assert service.project_for_plan(CHILD, day)['constraints'] == []
    assert len(service.get_adjustment(CHILD, candidate['adjustment_id'])['history']) == 3
    with pytest.raises(ReportFollowupConflict):
        service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 2, 'approve', 'stale')
    second = propose(service, snapshot, 'task_segment_minutes_cap', 5)
    service.review_plan_adjustment(CHILD, second['adjustment_id'], 1, 'approve', 'approve-second')
    retire_source(flow[0], flow[1], flow[2])
    inputs = service.project_for_plan(CHILD, day)
    assert inputs['items'] == []
    assert inputs['constraints'][0]['authority'] == 'retained_parent_safety_guard'
    assert inputs['approved_constraints_requiring_review'] == inputs['constraints']
    detail = service.get_adjustment(CHILD, second['adjustment_id'])
    assert detail['source_current'] is False and detail['requires_review'] is True
    assert detail['authority'] == 'retained_parent_safety_guard'


@pytest.mark.parametrize('kind,value', [('daily_total_minutes_cap',100000), ('task_segment_minutes_cap',0), ('daily_total_minutes_cap',True), ('invent_mastery',10)])
def test_cannot_loosen_family_limits_or_infer_unapproved_constraints(flow, kind, value):
    book, snapshot, service = setup_feedback(flow)
    with pytest.raises(ValueError):
        propose(service, snapshot, kind, value)


def test_budget_excludes_deferred_cancelled_and_segment_excludes_breaks(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    candidate = propose(service, snapshot, 'task_segment_minutes_cap', 5)
    service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, 'approve', 'approve')
    with flow[0].connect() as c:
        for identifier, minutes, status, source in [('study',5,'pending','school_homework'),
                ('deferred',80,'rescheduled','school_homework'),('cancelled',80,'cancelled','school_homework'),
                ('break',10,'pending','planned_break')]:
            c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type)
                VALUES (?,'next-plan','math','Synthetic',?,1,?,1,?)''', (identifier,minutes,status,source))
        c.execute("UPDATE daily_plan SET total_minutes=15 WHERE plan_id='next-plan'")
        c.commit()
    inputs = service.project_for_plan(CHILD, day)
    assert service.record_plan_read(CHILD, 'next-plan', inputs['fingerprint'], 'read')['constraints_satisfied'] is True
    cap = propose(service, snapshot, 'daily_total_minutes_cap', 10)
    service.review_plan_adjustment(CHILD, cap['adjustment_id'], 1, 'approve', 'approve-total')
    inputs = service.project_for_plan(CHILD, day)
    with pytest.raises(ReportFollowupConflict, match='PLAN_CONSTRAINT_VIOLATION'):
        service.record_plan_read(CHILD, 'next-plan', inputs['fingerprint'], 'read-total')


def test_feedback_revision_invalidates_candidate_and_parent_review_is_compare_and_swap(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    candidate = propose(service, snapshot)
    def review(decision):
        try:
            return service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, decision, decision)['operation']
        except ReportFollowupConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(review, ['approve','reject']))
    assert results.count('conflict') == 1
    winner = next(result for result in results if result != 'conflict')
    assert service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, winner, winner)['revision'] == 2
    with pytest.raises(ReportFollowupConflict):
        service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, 'retract', winner)
    book.record_feedback(snapshot['snapshot_id'], 2, None, 'Revised', 'feedback-revised')
    assert service.get_adjustment(CHILD, candidate['adjustment_id'])['source_current'] is False
    assert len(service.project_for_plan(CHILD, day)['constraints']) == (1 if winner == 'approve' else 0)


def test_stale_source_cannot_relax_budget_or_be_reapproved_by_replaying_old_action(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    candidate = propose(service, snapshot, value=20)
    service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, 'approve', 'approve')
    before = service.project_for_plan(CHILD, day)
    receipt = service.record_plan_read(CHILD, 'next-plan', before['fingerprint'], 'before')
    retire_source(flow[0], flow[1], flow[2])
    after = ReportFollowupService(flow[0]).project_for_plan(CHILD, day)
    assert after['constraints'][0]['value_minutes'] == 20
    assert after['fingerprint'] != before['fingerprint']
    assert not service.get_plan_receipt(CHILD, receipt['receipt_id'])['inputs_current']
    replay = service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, 'approve', 'approve')
    assert replay['requires_review'] and len(replay['history']) == 2
    with pytest.raises(ReportFollowupConflict):
        service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 2, 'approve', 'fake-recheck')
    with flow[0].connect() as c:
        c.execute("UPDATE daily_plan SET total_minutes=60 WHERE plan_id='next-plan'")
        c.commit()
    with pytest.raises(ReportFollowupConflict, match='PLAN_CONSTRAINT_VIOLATION'):
        service.record_plan_read(CHILD, 'next-plan', after['fingerprint'], 'after')
    with flow[0].connect() as c:
        c.execute('BEGIN')
        windows = service.constraint_windows(CHILD, day, day, connection=c)
        assert windows[0]['constraints'] == after['constraints']


def test_explicit_feedback_retraction_releases_guard_and_later_record_cannot_resurrect_it(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    candidate = propose(service, snapshot)
    service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, 'approve', 'approve')
    book.record_feedback(snapshot['snapshot_id'], 2, None, 'Revised', 'revise')
    assert service.project_for_plan(CHILD, day)['constraints'][0]['requires_review']
    service.retract_feedback(CHILD, snapshot['snapshot_id'], 2, 'withdraw', 'Explicitly withdraw')
    book.record_feedback(snapshot['snapshot_id'], 4, None, 'New feedback', 'new-feedback')
    assert service.project_for_plan(CHILD, day)['constraints'] == []
    assert service.get_adjustment(CHILD, candidate['adjustment_id'])['release_reason'] == 'feedback_retracted'


@pytest.mark.parametrize('start,end', [('2026-09-10','2026-09-09'), ('2026-09-10','2028-09-10'), ('2026-02-30','2026-03-01')])
def test_adjustment_date_window_must_be_valid_and_bounded(flow, start, end):
    book, snapshot, service = setup_feedback(flow)
    with pytest.raises(ValueError):
        service.propose_plan_adjustment(CHILD, snapshot['snapshot_id'], 1, 'daily_total_minutes_cap', 10, start, end, 'dates')


def test_review_refuses_newly_tightened_family_limits(flow):
    book, snapshot, service = setup_feedback(flow)
    candidate = propose(service, snapshot)
    with flow[0].connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=5 WHERE child_id=?', (CHILD,))
        c.commit()
    with pytest.raises(ReportFollowupConflict, match='FAMILY_RULE_CHANGED'):
        service.review_plan_adjustment(CHILD, candidate['adjustment_id'], 1, 'approve', 'approve')
