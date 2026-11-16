"""Synthetic source-version tests; no report generation or cloud transport."""
import re
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.behavior_model.service import BehaviorModelService
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.parent_action_evidence import ParentActionEvidenceService
from home_study_agent.report_source_fingerprint import report_source_fingerprint
from test_report_delivery import _pending_report_job

SID = 'pending-report-session'
CHILD = 'child-primary-4'


def insert(c, table, **values):
    c.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / 'source.db'); db.initialize(); _pending_report_job(db)
    GoalProgramService(db).seed_defaults()
    ParentReportService(db).initialize(); ParentActionEvidenceService(db).initialize()
    BehaviorModelService(db).initialize()
    with db.connect() as c:
        today = c.execute("SELECT date('now','localtime')").fetchone()[0]
        insert(c, 'daily_plan', plan_id='plan', child_id=CHILD, session_id=SID, plan_date=today,
               status='completed', total_minutes=20, generator='test', version=1, created_at=today, client_action_id='plan')
        insert(c, 'plan_item', plan_item_id='item', plan_id='plan', subject='数学', title='第一步',
               expected_minutes=20, actual_minutes=15, priority=1, status='completed', position=0)
        insert(c, 'plan_item_event', event_id='rescheduled', plan_item_id='item', event_type='rescheduled',
               occurred_at=today, payload_json='{"reason":"明天再核对"}', client_action_id='rescheduled')
        insert(c, 'classroom_recall', recall_id='recall', session_id=SID, subject='数学', summary='条件',
               confidence=.8, confirmation_status='child_confirmed', source_refs_json='[]', created_at=today, client_action_id='recall')
        insert(c, 'session_planning_context', session_id=SID, energy_level='medium', fixed_commitment_minutes=0, tomorrow_event_minutes=0, updated_at=today)
        insert(c, 'weekly_goal', weekly_goal_id='week', child_id=CHILD, week_start=today, week_end=today,
               track='pet', target_minutes=20, target_sessions=1, status='on_track', generator='test', knowledge_version='test', created_at=today, updated_at=today)
        program = c.execute('SELECT program_id FROM goal_program WHERE child_id=? ORDER BY program_id', (CHILD,)).fetchone()[0]
        insert(c, 'goal_cycle', cycle_id='cycle', program_id=program, child_id=CHILD, week_start=today, week_end=today,
               target_minutes=30, target_sessions=2, focus_json='[]', status='on_track', generator='test', created_at=today, updated_at=today)
        insert(c, 'daily_report', report_id='report', session_id=SID, child_id=CHILD, report_date=today,
               source='local_rules_v2', evidence_json='{}', summary_json='{}', parent_coaching_json='{}', created_at=today)
        insert(c, 'parent_report_snapshot', snapshot_id='snapshot', source_kind='daily_report', source_id='report',
               report_id='report', child_id=CHILD, report_type='daily', source_version='v1', template_version='v1',
               evidence_version='v1', facts_json='[]', inferences_json='[]', content_json='{}', confidence_json='{}', status='draft', created_at=today)
        insert(c, 'parent_report_snapshot_feedback', feedback_id='feedback', snapshot_id='snapshot', helpfulness=3, stress_rating=2, notes='核对', created_at=today)
        insert(c, 'report_feedback_revision', event_id='revision', child_id=CHILD, snapshot_id='snapshot', revision=1,
               operation='record', feedback_id='feedback', payload_json='{}', client_action_id='revision', created_at=today)
        insert(c, 'parent_action_snapshot', snapshot_id='action', child_id=CHILD, report_id='report', version=1,
               fingerprint='v1', payload_json='{}', created_at=today)
        insert(c, 'parent_action_feedback_event', event_id='action-feedback', snapshot_id='action', child_id=CHILD,
               revision=1, operation='record', payload_json='{}', request_json='{}', client_action_id='action-feedback', created_at=today)
        insert(c, 'observable_event', event_id='observation', child_id=CHILD, session_id=SID, occurred_at=today,
               event_type='work.stall', source='local', confidence=.8, source_refs_json='[]', missing_sources_json='[]', payload_json='{}', alternatives_json='[]')
        insert(c, 'probabilistic_state', state_id='state', event_id='observation', child_id=CHILD, session_id=SID,
               occurred_at=today, state='blocked_candidate', probability=.7, evidence_json='{}', alternatives_json='[]')
        insert(c, 'intervention', intervention_id='intervention', child_id=CHILD, session_id=SID, source_event_id='observation',
               decided_at=today, candidate_state='blocked_candidate', message='要帮助吗', feedback='accepted', feedback_at=today)
        insert(c, 'habit_experiment', experiment_id='habit', child_id=CHILD, target_state='blocked_candidate', title='圈出卡点',
               child_action='圈出一步', success_signal='主动提问', duration_days=7, baseline_json='{}', status='active',
               proposed_at=today, started_at=today, ends_at='2099-01-01', outcome_json='{}')
        c.commit()
    return db


@pytest.mark.parametrize('sql', [
    "UPDATE daily_reflection SET learned_text='新的复盘'",
    "UPDATE daily_reflection SET difficult_text='另一处困难'",
    "UPDATE classroom_recall SET summary='新条件'",
    "UPDATE classroom_recall SET confirmation_status='unconfirmed'",
    "UPDATE plan_item SET title='第二步'",
    "UPDATE plan_item SET actual_minutes=19",
    "UPDATE plan_item_event SET payload_json='{\"reason\":\"改为后天\"}'",
    "UPDATE session_planning_context SET energy_level='low'",
    "UPDATE child_profile SET nickname='新的称呼' WHERE child_id='child-primary-4'",
    "UPDATE family_rule SET max_learning_minutes=25 WHERE child_id='child-primary-4'",
    "UPDATE weekly_goal SET target_minutes=31",
    "UPDATE goal_program SET target_statement='新目标' WHERE child_id='child-primary-4'",
    "UPDATE goal_cycle SET target_sessions=3",
    "UPDATE parent_report_snapshot SET status='revoked'",
    "UPDATE parent_report_snapshot_feedback SET notes='不再适用'",
    "INSERT INTO report_feedback_revision VALUES ('revision2','child-primary-4','snapshot',2,'retract',NULL,'{}','revision2','2026-09-09')",
    "INSERT INTO parent_action_feedback_event VALUES ('action-feedback2','action','child-primary-4',2,'retract','{}','{}','action-feedback2','2026-09-09')",
    "UPDATE observable_event SET payload_json='{\"model_version\":\"changed\"}'",
    "UPDATE probabilistic_state SET probability=.3",
    "UPDATE intervention SET feedback='declined'",
    "UPDATE habit_experiment SET child_action='换一个行动'",
    "UPDATE habit_experiment SET ends_at='2000-01-01'",
    "INSERT INTO behavior_retraction VALUES ('observation','child-primary-4','correction','撤回','2026-01-01')",
])
def test_relevant_source_change_is_visible_inside_borrowed_transaction_and_rolls_back(db, sql):
    with db.connect() as c:
        before = report_source_fingerprint(c, SID)
        c.execute('BEGIN IMMEDIATE'); c.execute(sql)
        assert report_source_fingerprint(c, SID) != before
        assert c.in_transaction
        c.rollback()
        assert report_source_fingerprint(c, SID) == before


def test_other_child_and_non_source_activity_and_derived_cache_do_not_invalidate(db):
    with db.connect() as c:
        before = report_source_fingerprint(c, SID)
        c.execute("UPDATE child_profile SET nickname='另孩更新' WHERE child_id='child-junior-3'")
        c.execute("UPDATE family_rule SET max_learning_minutes=31 WHERE child_id='child-junior-3'")
        c.execute("UPDATE goal_program SET title='另孩目标' WHERE child_id='child-junior-3'")
        c.execute("UPDATE report_job SET attempts=attempts+1")
        c.execute("UPDATE goal_cycle SET updated_at='2099-01-01',completed_minutes=999")
        c.execute("UPDATE habit_experiment SET outcome_json='{\"window_end\":\"2099-01-01\"}'")
        insert(c, 'audit_event', audit_id='unrelated', actor='system', action='unrelated', child_id=CHILD, summary_json='{}', occurred_at='2026-01-01')
        c.commit()
        assert report_source_fingerprint(c, SID) == before
    from home_study_agent.repository import StudyRepository
    repo = StudyRepository(db)
    repo.start_model_call(SID, CHILD, 'parent_report_coaching', 'synthetic', 'synthetic', 1, 'test', {}, 'low', 'synthetic', 'test', None)
    with db.connect() as c:
        assert report_source_fingerprint(c, SID) == before


def test_optional_schema_initialization_is_not_a_fact(tmp_path):
    db = Database(tmp_path / 'empty.db'); db.initialize(); _pending_report_job(db)
    with db.connect() as c:
        before = report_source_fingerprint(c, SID)
    ParentReportService(db).initialize(); ParentActionEvidenceService(db).initialize()
    GoalProgramService(db).ensure_schema(); BehaviorModelService(db).initialize()
    with db.connect() as c:
        assert report_source_fingerprint(c, SID) == before


def test_select_only_no_transaction_ownership_and_readonly_restart(db):
    with db.connect() as c:
        trace = []; c.set_trace_callback(trace.append)
        changes = c.total_changes
        before = report_source_fingerprint(c, SID)
        assert re.fullmatch(r'report_source_v1:[0-9a-f]{64}', before)
        assert not c.in_transaction and c.total_changes == changes
        c.execute('BEGIN IMMEDIATE'); trace.clear()
        read_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                        sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        c.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK
                         if action in read_actions else sqlite3.SQLITE_DENY)
        try:
            assert report_source_fingerprint(c, SID) == before
        finally:
            # Python 3.9 does not support None to disable this callback.
            c.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
        assert c.in_transaction and c.total_changes == changes
        assert all(q.lstrip().upper().startswith(('SELECT', 'WITH RECURSIVE')) for q in trace)
        c.rollback()
    with sqlite3.connect(f'file:{db.path}?mode=ro', uri=True) as c:
        c.execute('PRAGMA query_only=ON')
        assert report_source_fingerprint(c, SID) == before
        with pytest.raises(ValueError, match='REPORT_SOURCE_SESSION_NOT_FOUND'):
            report_source_fingerprint(c, 'missing')


def test_another_child_observations_and_session_facts_do_not_change_this_report(db):
    with db.connect() as c:
        before = report_source_fingerprint(c, SID)
        insert(c, 'daily_session', session_id='other-session', child_id='child-junior-3', status='completed',
               state='reflection', started_at='2026-09-08T10:00:00+00:00', ended_at='2026-09-08T10:00:00+00:00', last_checkpoint_at='2026-09-08T10:00:00+00:00', client_action_id='other')
        insert(c, 'daily_reflection', reflection_id='other-reflection', child_id='child-junior-3', session_id='other-session',
               learned_text='另一孩子的原话', difficult_text='', feeling='正常', next_action='', skipped=0, created_at='2026-09-08', client_action_id='other-reflection')
        insert(c, 'observable_event', event_id='other-observation', child_id='child-junior-3', session_id='other-session', occurred_at='2026-09-08',
               event_type='work.stall', source='local', confidence=.8, source_refs_json='[]', missing_sources_json='[]', payload_json='{}', alternatives_json='[]')
        c.commit()
        assert report_source_fingerprint(c, SID) == before
        assert report_source_fingerprint(c, 'other-session') != before


def test_relevant_deletion_and_linked_approval_evidence_are_not_arbitrary_audit_logs(db):
    with db.connect() as c:
        before = report_source_fingerprint(c, SID)
        c.execute('BEGIN IMMEDIATE'); c.execute("DELETE FROM classroom_recall WHERE recall_id='recall'")
        assert report_source_fingerprint(c, SID) != before
        c.rollback()
        insert(c, 'audit_event', audit_id='approval', actor='parent', action='parent_report_snapshot_approved', child_id=CHILD,
               summary_json='{"snapshot_id":"snapshot"}', occurred_at='2026-09-08')
        assert report_source_fingerprint(c, SID) != before
        c.rollback()
        assert report_source_fingerprint(c, SID) == before
