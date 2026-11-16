"""Subjective coaching evidence stays separate from live report/plan authority."""
import json
from datetime import datetime, timedelta

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService, PeriodReportSourceError
from home_study_agent.report_followup import ReportFollowupService
from test_daily_report_evidence_versions import flow, CHILD, retire_source
from test_period_learning_source_audit import approved


def experiences(db, child=CHILD):
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        value = ReportFollowupService.coaching_experiences(c, child)
        assert c.total_changes == 0 and c.in_transaction
        return value


def test_historical_subjective_feedback_survives_source_loss_but_cannot_authorize_plan(flow):
    db, repo, daily, delivery = flow
    parents = delivery.snapshots
    daily = repo.get_daily_report(daily['report_id'])
    snapshot = parents.create_from_daily(daily)
    parents.approve(snapshot['snapshot_id'])
    parents.record_feedback(snapshot['snapshot_id'], 2, 5, 'Parent observed pressure', 'historical-experience')
    before = experiences(db)
    assert len(before) == 1
    assert before[0]['evidence_kind'] == 'subjective_report_experience'
    assert not before[0]['execution_verified'] and not before[0]['learning_effect_verified']
    planning_day = (datetime.fromisoformat(before[0]['created_at']).date() + timedelta(days=1)).isoformat()
    assert ReportFollowupService(db).project_for_plan(CHILD, planning_day)['items']
    retire_source(db, repo, daily)
    with db.connect() as c:
        frozen = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()
        assert not parents.snapshot_source_current(c, frozen)
        assert ReportFollowupService.current_feedback(c, frozen)['feedback_current'] is None
    assert experiences(Database(db.path)) == before
    plan = ReportFollowupService(db).project_for_plan(CHILD, planning_day)
    assert not plan['items'], plan
    parents.revoke(snapshot['snapshot_id'], 'Withdraw outdated learning report')
    assert experiences(Database(db.path)) == before


def test_feedback_retraction_invalidates_coaching_and_survives_restart(flow):
    db = flow[0]
    periods, parents, old, snapshot = approved(flow)
    parents.record_feedback(snapshot['snapshot_id'], 2, 5, 'Pressure', 'coaching-stress')
    fresh = periods.get(old['period_report_id'], persist_refresh=True)
    assert fresh['content']['parent_coaching']['action']['duration_minutes'] == 5
    replacement = parents.create_from_period(fresh)
    parents.approve(replacement['snapshot_id'])
    with db.connect() as c:
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (replacement['snapshot_id'],)).fetchone())
        assert parents.snapshot_source_current(c, frozen)
    assert len(experiences(db)) == 1
    ReportFollowupService(db).retract_feedback(CHILD, snapshot['snapshot_id'], 1, 'withdraw-coaching-experience', 'Parent correction')
    assert experiences(Database(db.path)) == []
    with db.connect() as c:
        assert not parents.snapshot_source_current(c, frozen)
    after = PeriodReportService(Database(db.path)).get(old['period_report_id'], persist_refresh=True)
    assert after['content']['parent_coaching']['action']['duration_minutes'] == 8


def test_approval_without_feedback_and_other_child_do_not_change_coaching_sources(flow):
    db, repo, daily, delivery = flow
    periods, parents, report, _ = approved(flow)
    with db.connect() as c:
        before = ParentReportService.period_learning_sources(c, CHILD, report['period_start'], report['period_end'])
    second = parents.create_from_daily(daily)
    parents.approve(second['snapshot_id'])
    assert experiences(db) == []
    with db.connect() as c:
        assert before == ParentReportService.period_learning_sources(c, CHILD, report['period_start'], report['period_end'])
    parents.record_feedback(second['snapshot_id'], 1, 5, 'One child only', 'owned-experience')
    assert len(experiences(db, CHILD)) == 1
    assert experiences(db, 'child-junior-3') == []


@pytest.mark.parametrize('damage', ['approval_absent', 'feedback_changed', 'revision_owner', 'revision_gap',
                                    'source_version', 'snapshot_evidence', 'future_feedback'])
def test_incomplete_or_cross_owner_experience_proof_is_excluded(flow, damage):
    db = flow[0]
    _, parents, _, snapshot = approved(flow)
    parents.record_feedback(snapshot['snapshot_id'], 1, 5, 'Original note', 'damaged-experience')
    assert len(experiences(db)) == 1
    with db.connect() as c:
        if damage == 'approval_absent':
            c.execute("DELETE FROM audit_event WHERE action='parent_report_snapshot_approved'")
        elif damage == 'feedback_changed':
            c.execute("UPDATE parent_report_snapshot_feedback SET stress_rating=1 WHERE snapshot_id=?", (snapshot['snapshot_id'],))
        elif damage == 'source_version':
            c.execute("UPDATE parent_report_snapshot SET source_version='other:version' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
        elif damage == 'snapshot_evidence':
            c.execute("UPDATE parent_report_snapshot SET evidence_version='sha256:wrong' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
        else:
            c.execute('DROP TRIGGER report_feedback_revision_no_update')
            if damage == 'revision_owner':
                c.execute("UPDATE report_feedback_revision SET child_id='child-junior-3' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
            elif damage == 'revision_gap':
                c.execute('UPDATE report_feedback_revision SET revision=2 WHERE snapshot_id=?', (snapshot['snapshot_id'],))
            else:
                c.execute("UPDATE report_feedback_revision SET created_at='2099-01-01T00:00:00Z' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
                c.execute("UPDATE parent_report_snapshot_feedback SET created_at='2099-01-01T00:00:00Z' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
        c.commit()
    assert experiences(Database(db.path)) == []


def test_draft_feedback_never_becomes_historical_approval_without_witness(flow):
    db, _, daily, delivery = flow
    parents = delivery.snapshots
    snapshot = parents.create_from_daily(daily)
    parents.record_feedback(snapshot['snapshot_id'], 1, 5, 'Draft note only', 'draft-experience')
    assert experiences(db) == []
    parents.approve(snapshot['snapshot_id'])
    assert experiences(db) == [], 'Later approval must not backdate a draft experience witness'


@pytest.mark.parametrize('mutation', ['feedback', 'goal'])
def test_sources_changed_after_build_are_rejected_by_final_writer(flow, monkeypatch, mutation):
    from home_study_agent.goal_program import GoalProgramService
    db = flow[0]
    GoalProgramService(db).cycles(CHILD)
    periods, parents, report, snapshot = approved(flow)
    build = periods._build_content
    def changed(*args, **kwargs):
        content = build(*args, **kwargs)
        if mutation == 'feedback':
            parents.record_feedback(snapshot['snapshot_id'], 1, 5, 'Between build and writer', 'writer-pressure')
        else:
            with db.connect() as c:
                c.execute('UPDATE goal_cycle SET target_minutes=target_minutes+15 WHERE child_id=?', (CHILD,))
                c.commit()
        return content
    monkeypatch.setattr(periods, '_build_content', changed)
    with pytest.raises(PeriodReportSourceError, match='PERIOD_LEARNING_SOURCE_CHANGED'):
        periods.get(report['period_report_id'], persist_refresh=True)


def test_goal_plan_proof_limits_scope_but_tracks_current_completed_work(flow):
    from home_study_agent.goal_program import GoalProgramService
    from home_study_agent.repository import utc_now
    db, _, daily, _ = flow
    goals = GoalProgramService(db)
    goals.cycles(CHILD)
    goals.cycles('child-junior-3')
    _, _, report, _ = approved(flow)
    start, end = report['period_start'], report['period_end']
    with db.connect() as c:
        def stamp():
            return ParentReportService.period_learning_sources(c, CHILD, start, end)
        before = stamp()
        c.execute('BEGIN IMMEDIATE')
        c.execute('''INSERT INTO daily_plan
            (plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            ('proof-plan', CHILD, daily['session_id'], start, 'completed', 8, 'synthetic-scope', 1, utc_now(), 'proof-plan'))
        c.execute('''INSERT INTO plan_item
            (plan_item_id,plan_id,subject,title,expected_minutes,actual_minutes,priority,status,position,source_type)
            VALUES ('proof-item','proof-plan','English','Synthetic PET task',8,8,1,'completed',1,'pet_daily')''')
        completed = stamp()
        assert completed != before
        c.execute("UPDATE plan_item SET actual_minutes=12 WHERE plan_item_id='proof-item'")
        assert stamp() != completed
        c.execute("UPDATE plan_item SET status='pending' WHERE plan_item_id='proof-item'")
        assert stamp() == before
        c.execute("UPDATE plan_item SET status='completed' WHERE plan_item_id='proof-item'")
        c.execute("UPDATE daily_plan SET plan_date='2000-01-01' WHERE plan_id='proof-plan'")
        assert stamp() == before
        c.execute("UPDATE goal_cycle SET target_minutes=target_minutes+100 WHERE child_id='child-junior-3'")
        c.execute("UPDATE goal_cycle SET completed_minutes=999,updated_at='2099-01-01' WHERE child_id=?", (CHILD,))
        assert stamp() == before, 'Other child and cached outcome metadata are not this report source'
        c.rollback()
        assert stamp() == before
