from datetime import date, timedelta
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.family_clock import family_today
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.goal_roadmap import GoalRoadmapService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_followup import ReportFollowupService
from test_daily_report_evidence_versions import CHILD, flow
from test_goal_roadmap import configure
from test_goal_program import goal_db, exact_goal_item, _weak_goal_session, _qualify_fresh_goal_items


TRACK = 'competition_math_path'
NODE = 'node-oly-arithmetic-structure'


@pytest.fixture
def route(flow):
    db, repo, saved, delivery = flow
    goals = GoalProgramService(db)
    goals.seed_defaults()
    with db.connect() as c:
        c.execute("UPDATE goal_program SET status='paused' WHERE child_id=? AND track='pet_path'", (CHILD,))
        c.execute("UPDATE knowledge_node SET status='inactive' WHERE track='competition_math' AND knowledge_node_id!=?", (NODE,))
        c.execute('DELETE FROM knowledge_edge WHERE target_node_id=?', (NODE,))
        c.execute('UPDATE family_rule SET max_learning_minutes=120,task_segment_minutes=60,break_minutes=5 WHERE child_id=?', (CHILD,))
        c.commit()
    configure(goals, target_date=(family_today()+timedelta(days=12)).isoformat(), weekly_minutes=150, weekly_sessions=7)
    book = ParentReportService(db)
    snapshot = book.create_from_daily(repo.get_daily_report(saved['report_id']))
    book.approve(snapshot['snapshot_id'])
    book.record_feedback(snapshot['snapshot_id'], 4, None, 'Synthetic explicit workload discussion', 'feedback')
    return db, goals, book, snapshot, ReportFollowupService(db)


def approve(route, kind, minutes, start=None, end=None):
    db, goals, book, snapshot, followup = route
    first = start or family_today()
    last = end or (family_today()+timedelta(days=12))
    proposal = followup.propose_plan_adjustment(CHILD, snapshot['snapshot_id'],
        book.get(snapshot['snapshot_id'])['feedback_current_revision'], kind, minutes,
        first.isoformat(), last.isoformat(), 'proposal-'+kind)
    return followup.review_plan_adjustment(CHILD, proposal['adjustment_id'], 1, 'approve', 'approve-'+kind)


def test_approved_caps_leave_whole_teaching_work_unplaced_without_reducing_goal_or_estimates(route):
    db, goals, book, snapshot, followup = route
    baseline = GoalRoadmapService(db).read(CHILD, TRACK)
    approve(route, 'daily_total_minutes_cap', 20)
    approve(route, 'task_segment_minutes_cap', 5)
    result = goals.roadmap(CHILD, TRACK)
    decomposition = result['daily_decomposition']
    assert result['source_version'] == baseline['source_version']
    assert result['prerequisite_path'] == baseline['prerequisite_path']
    assert result['burden']['estimated_minutes'] == baseline['burden']['estimated_minutes']
    assert decomposition['allocations'] == []
    assert decomposition['unplaced_minutes'] == baseline['burden']['estimated_minutes']['max']
    assert decomposition['requires_parent_negotiation'] is True
    assert all(day['total_cap_minutes']==20 and day['segment_cap_minutes']==5 for day in decomposition['days'])
    assert all(row['reason_code'] in {'whole_activity_exceeds_segment_cap','predecessor_unplaced'} for row in decomposition['unplaced'])


def test_expiry_retraction_and_feedback_revision_recompute_calendar_after_restart(route):
    db, goals, book, snapshot, followup = route
    end = family_today()+timedelta(days=2)
    adjustment = approve(route, 'task_segment_minutes_cap', 5, end=end)
    before = goals.roadmap(CHILD, TRACK)['daily_decomposition']
    assert before['allocations']
    assert all(row['date'] > end.isoformat() for row in before['allocations'])
    assert GoalProgramService(Database(db.path)).roadmap(CHILD, TRACK)['daily_decomposition'] == before
    followup.review_plan_adjustment(CHILD, adjustment['adjustment_id'], 2, 'retract', 'retract')
    after = goals.roadmap(CHILD, TRACK)['daily_decomposition']
    assert after['source_version'] != before['source_version']
    assert after['allocations'][0]['date'] == family_today().isoformat()
    # A revision invalidates an earlier approval instead of silently inheriting it.
    approved = approve(route, 'daily_total_minutes_cap', 5)
    old = goals.roadmap(CHILD, TRACK)['daily_decomposition']['source_version']
    book.record_feedback(snapshot['snapshot_id'], 2, None, 'Revised discussion', 'revise')
    assert not followup.get_adjustment(CHILD, approved['adjustment_id'])['source_current']
    assert goals.roadmap(CHILD, TRACK)['daily_decomposition']['source_version'] != old


def test_existing_school_tasks_and_breaks_reduce_capacity_but_deferred_tasks_do_not(route):
    db, goals, book, snapshot, followup = route
    with db.connect() as c:
        sid = c.execute('SELECT session_id FROM daily_session WHERE child_id=? LIMIT 1', (CHILD,)).fetchone()[0]
        c.execute('INSERT INTO daily_plan VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('reserved',CHILD,sid,family_today().isoformat(),'ready',115,'local',1,'2026-01-01T00:00:00+00:00','reserved'))
        for key,minutes,status,source in [('school',110,'pending','school_homework'),('break',5,'pending','planned_break'),('later',200,'rescheduled','school_homework')]:
            c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type)
                VALUES (?,'reserved','math','Synthetic',?,1,?,1,?)''', (key,minutes,status,source))
        c.commit()
    result = goals.roadmap(CHILD, TRACK)['daily_decomposition']
    assert result['days'][0]['committed_minutes'] == 115
    assert result['days'][0]['available_minutes'] == 5
    assert all(item['date'] != family_today().isoformat() for item in result['allocations'])


def test_true_deadline_and_indivisible_delay_are_not_replaced_with_a_weekly_average(route):
    db, goals, book, snapshot, followup = route
    configure(goals, action='near-deadline', target_date=family_today().isoformat(), weekly_minutes=150, weekly_sessions=7)
    result = goals.roadmap(CHILD, TRACK)
    decomposition = result['daily_decomposition']
    assert len(decomposition['days']) == 1
    assert decomposition['unplaced']
    assert decomposition['requires_parent_negotiation']
    for item in decomposition['allocations']:
        assert item['planned_minutes'] == item['estimated_minutes']['max']
    assert result['goal_attainment'] == 'not_established'


def test_multiple_goals_share_one_daily_family_budget_and_preserve_all_work(route):
    db, goals, book, snapshot, followup = route
    with db.connect() as c:
        c.execute("UPDATE goal_program SET status='active' WHERE child_id=? AND track='pet_path'", (CHILD,))
        c.commit()
    configure(goals, action='pet-date', track='pet_path', target_date=(family_today()+timedelta(days=12)).isoformat(), weekly_minutes=50, weekly_sessions=7)
    from home_study_agent.repository import StudyRepository
    snapshot = book.create_from_daily(StudyRepository(db).get_daily_report(snapshot['report_id']))
    book.approve(snapshot['snapshot_id'])
    book.record_feedback(snapshot['snapshot_id'], 4, None, 'After catalog initialization', 'multi-goal-feedback')
    route = db, goals, book, snapshot, followup
    approve(route, 'daily_total_minutes_cap', 20)
    maps = [goals.roadmap(CHILD, track) for track in ('pet_path',TRACK)]
    for index in range(13):
        entries = [item['daily_decomposition']['days'][index] for item in maps]
        assert sum(item['goal_allocated_minutes'] for item in entries) <= 20
    for roadmap in maps:
        forecast = roadmap['daily_decomposition']
        assert len(forecast['allocations'])+len(forecast['unplaced']) == sum(len(n['steps']) for n in roadmap['prerequisite_path'])
        assert sum(item['planned_minutes'] for item in forecast['allocations'])+forecast['unplaced_minutes'] == roadmap['burden']['estimated_minutes']['max']


def test_readonly_route_and_child_isolation_preserve_original_action_amount(goal_db, exact_goal_item):
    goals = GoalProgramService(goal_db)
    goals.seed_defaults()
    child = 'child-junior-3'
    with goal_db.connect() as c:
        node = c.execute('SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?', (exact_goal_item[0],)).fetchone()[0]
        c.execute("UPDATE knowledge_node SET status='inactive' WHERE grade_band LIKE '初三%' AND knowledge_node_id!=?", (node,))
        c.execute('DELETE FROM knowledge_edge WHERE target_node_id=?', (node,))
        c.execute('UPDATE family_rule SET task_segment_minutes=5,latest_end_time=NULL WHERE child_id=?', (child,))
        c.commit()
        before = '\n'.join(c.iterdump())
    original = GoalRoadmapService(goal_db).read(child, 'junior_exam_readiness')['next_daily_action']
    assert original['dispatchable'] is True
    result = goals.roadmap(child, 'junior_exam_readiness')
    assert result['next_daily_action']['minutes'] == original['minutes']
    assert result['next_daily_action']['dispatchable'] is False
    assert result['next_daily_action']['status'] == 'unplaced_parent_negotiation'
    with goal_db.connect() as c:
        assert '\n'.join(c.iterdump()) == before
    assert all(row['segment_cap_minutes'] != 5 for row in goals.roadmap(CHILD, TRACK)['daily_decomposition']['days'])


def test_short_cap_preserves_existing_review_until_explicit_eligible_prepare(goal_db, exact_goal_item):
    repo, session, original = _weak_goal_session(goal_db, exact_goal_item)
    assert _qualify_fresh_goal_items(goal_db)
    goals = GoalProgramService(goal_db)
    with goal_db.connect() as c:
        c.execute("UPDATE family_rule SET task_segment_minutes=5,latest_end_time=NULL WHERE child_id='child-junior-3'")
        c.commit()
        before = [tuple(row) for row in c.execute('SELECT * FROM review_task ORDER BY review_task_id')]
    assert goals.prepare_daily_action('child-junior-3') is None
    assert goals.cycles('child-junior-3')[0]['execution']['status'] == 'budget_limited'
    with goal_db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM review_task ORDER BY review_task_id')] == before
        c.execute("UPDATE family_rule SET task_segment_minutes=60 WHERE child_id='child-junior-3'")
        c.commit()
    goals.prepare_daily_action('child-junior-3')
    goals.prepare_daily_action('child-junior-3')
    with goal_db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == len(before)
