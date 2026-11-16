"""Real revision -> bounded append-only scheduling in isolated SQLite."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import json
import os
import time

import pytest

from home_study_agent import learning_input_replan
from home_study_agent.learning_input_replan import LearningInputReplanService, ReplanConflict
from home_study_agent.learning_input_revision import LearningInputRevisionService
from test_learning_input_revision_voice import inputs, flow, CHILD, propose, confirm


@pytest.fixture
def schedule(inputs, monkeypatch):
    db, sid, revisions = inputs
    clock = [datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(learning_input_replan, "utc_now", lambda: clock[0])
    with db.connect() as c:
        c.execute("UPDATE family_rule SET latest_end_time='21:30',max_learning_minutes=120,task_segment_minutes=15,break_minutes=5,pet_min_minutes=0 WHERE child_id=?", (CHILD,))
        c.execute("INSERT OR IGNORE INTO session_planning_context(session_id,energy_level,fixed_commitment_minutes,tomorrow_event_minutes,updated_at) VALUES (?,'medium',0,0,datetime('now'))", (sid,))
        c.execute("UPDATE session_planning_context SET fixed_commitment_minutes=0 WHERE session_id=?", (sid,))
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('original',?,?,'2026-09-07','active',30,'test',3,datetime('now'),'original')""", (CHILD, sid))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref)
            VALUES ('old','original','math-a','数学','口算',20,1,'pending',1,'school_homework','math-a')""")
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref)
            VALUES ('protected','original','语文','原任务',10,1,'pending',2,'school_homework','other')""")
        c.commit()
    service = LearningInputReplanService(db)
    service.initialize()
    return inputs, service, clock


def revised(schedule, minutes=30, operation="update"):
    inputs, _, _ = schedule
    utterance = f"口算改成{minutes}分钟" if operation == "update" else "删除口算"
    propose(inputs, utterance=utterance, operation=operation,
            changes={"expected_minutes": minutes} if operation == "update" else {},
            change_quotes={"expected_minutes": f"{minutes}分钟"} if operation == "update" else {})
    return confirm(inputs)[0]["event_id"]


def test_append_actual_items_and_durable_receipt_without_changing_old_items(schedule):
    inputs, service, _ = schedule
    db, sid, _ = inputs
    event = revised(schedule)
    with db.connect() as c:
        old = [tuple(row) for row in c.execute("SELECT * FROM plan_item ORDER BY plan_item_id")]
        evidence = c.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0]
    result = service.execute_replan(CHILD, event, "replan")
    assert result["receipt"]["status"] == "scheduled"
    assert sum(row["expected_minutes"] for row in result["newitems"] if row["source_type"] == "school_homework") == 30
    assert any(row["source_type"] == "planned_break" for row in result["newitems"])
    assert result["unplaced"] == []
    with db.connect() as c:
        assert [tuple(row) for row in c.execute("SELECT * FROM plan_item WHERE plan_item_id IN ('old','protected') ORDER BY plan_item_id")] == old
        assert c.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0] == evidence
        assert c.execute("SELECT version FROM daily_plan WHERE plan_id='original'").fetchone()[0] == 4
    replay = LearningInputReplanService(db).execute_replan(CHILD, event, "replan")
    assert replay == result


def test_school_deadline_excess_is_persisted_not_automatic_overtime(schedule):
    inputs, service, _ = schedule
    with inputs[0].connect() as c:
        c.execute("UPDATE homework_item SET due_at='2026-09-07T18:25:00+08:00' WHERE homework_id='math-a'")
        c.commit()
    event = revised(schedule)
    result = service.execute_replan(CHILD, event, "deadline")
    assert result["unplaced"] and result["receipt"]["allocated_minutes"] < 30
    with inputs[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_replan_unplaced").fetchone()[0] > 0


@pytest.mark.parametrize('defer_other', [False, True])
def test_deferred_other_homework_releases_capacity_for_real_replan(schedule, defer_other):
    from home_study_agent.database import Database
    from home_study_agent.repository import StudyRepository

    inputs, service, _ = schedule
    db, _, _ = inputs
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=40 WHERE child_id=?', (CHILD,))
        c.commit()
    if defer_other:
        StudyRepository(db).reschedule_plan_item('protected', 'defer-other', '这项明天继续。')
    event = revised(schedule, minutes=30)
    result = service.execute_replan(CHILD, event, 'after-other-deferral')
    allocated = sum(p['expected_minutes'] for p in result['newitems'] if p['source_type'] == 'school_homework')
    assert allocated == (30 if defer_other else 20)
    assert result['receipt']['status'] == ('scheduled' if defer_other else 'partial')
    assert sum(p['minutes'] for p in result['unplaced']) == (0 if defer_other else 10)
    with db.connect() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM plan_item ORDER BY plan_item_id')]
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='protected'").fetchone()[0] == (
            'rescheduled' if defer_other else 'pending')
        assert not c.execute('SELECT 1 FROM diagnostic_attempt').fetchone()
    restored = LearningInputReplanService(Database(db.path))
    assert restored.execute_replan(CHILD, event, 'after-other-deferral') == result
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM plan_item ORDER BY plan_item_id')] == before


def test_stale_source_does_not_write_any_task(schedule):
    inputs, service, _ = schedule
    event = revised(schedule)
    with inputs[0].connect() as c:
        c.execute("UPDATE homework_item SET title='Changed after confirmation' WHERE homework_id='math-a'")
        c.commit()
    with pytest.raises(ReplanConflict):
        service.execute_replan(CHILD, event, "stale")
    with inputs[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_replan_receipt").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item").fetchone()[0] == 2


@pytest.mark.parametrize("change", ["budget", "fixed", "version", "retire", "session", "authorization", "unknown_source"])
def test_changed_authority_plan_or_budget_fails_closed(schedule, change):
    inputs, service, _ = schedule
    event = revised(schedule)
    with inputs[0].connect() as c:
        if change == "budget":
            c.execute("UPDATE family_rule SET max_learning_minutes=130 WHERE child_id=?", (CHILD,))
        elif change == "fixed":
            c.execute("UPDATE session_planning_context SET fixed_commitment_minutes=60")
        elif change == "version":
            c.execute("UPDATE daily_plan SET version=version+1")
        elif change == "retire":
            c.execute("UPDATE daily_plan SET status='superseded'")
        elif change == "session":
            c.execute("UPDATE daily_session SET status='cancelled'")
        elif change == "authorization":
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=(SELECT client_action_id FROM learning_input_revision_event WHERE event_id=?)", (event,))
        else:
            c.execute("UPDATE homework_item SET confirmation_status='unknown' WHERE homework_id='math-a'")
        c.commit()
    with pytest.raises(ReplanConflict):
        service.execute_replan(CHILD, event, change)
    with inputs[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_replan_receipt").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item").fetchone()[0] == 2


@pytest.mark.parametrize("boundary", ["fixed", "pet", "curfew", "past_due", "unknown_due", "unknown_actual"])
def test_budget_constraints_are_visible_persisted_unplaced_work(schedule, boundary):
    inputs, service, clock = schedule
    with inputs[0].connect() as c:
        if boundary == "fixed":
            c.execute("UPDATE family_rule SET max_learning_minutes=40 WHERE child_id=?", (CHILD,))
            c.execute("UPDATE session_planning_context SET fixed_commitment_minutes=15")
        elif boundary == "pet":
            c.execute("UPDATE family_rule SET max_learning_minutes=40,pet_min_minutes=15 WHERE child_id=?", (CHILD,))
        elif boundary == "curfew":
            c.execute("UPDATE family_rule SET latest_end_time='18:25' WHERE child_id=?", (CHILD,))
        elif boundary in {"past_due", "unknown_due"}:
            c.execute("UPDATE homework_item SET due_at=? WHERE homework_id='math-a'", ("2026-09-07T17:00:00+08:00" if boundary == "past_due" else "2026-09-07",))
        else:
            c.execute("UPDATE plan_item SET status='completed',completed_at='2026-09-07T17:30:00+08:00' WHERE plan_item_id='protected'")
        c.commit()
    event = revised(schedule)
    result = service.execute_replan(CHILD, event, boundary)
    assert result["unplaced"]
    assert result["receipt"]["allocated_minutes"] + sum(item["minutes"] for item in result["unplaced"]) == 30
    assert result["receipt"]["allocated_minutes"] < 30
    assert sum(item["expected_minutes"] * 60 for item in result["newitems"]) <= min(
        result["receipt"]["budget"]["total_budget_remaining_seconds"], result["receipt"]["budget"]["wall_remaining_seconds"])


def test_actual_timer_is_accounted_without_mutating_its_row(schedule):
    inputs, service, _ = schedule
    with inputs[0].connect() as c:
        c.execute("UPDATE plan_item SET status='active',started_at='2026-09-07T17:40:00+08:00',active_seconds=300,active_segment_started_at='2026-09-07T17:50:00+08:00',expected_minutes=20 WHERE plan_item_id='protected'")
        before = tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='protected'").fetchone())
        c.commit()
    event = revised(schedule)
    result = service.execute_replan(CHILD, event, "actual")
    assert result["receipt"]["budget"]["actual_spent_seconds"] == 900
    assert result["receipt"]["budget"]["protected_remaining_seconds"] == 300
    with inputs[0].connect() as c:
        assert tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='protected'").fetchone()) == before


def test_delete_and_undo_restore_only_cancelled_unstarted_obligations(schedule):
    inputs, service, _ = schedule
    event = revised(schedule, operation="delete")
    deleted = service.execute_replan(CHILD, event, "delete")
    assert deleted["receipt"]["status"] == "deleted" and deleted["newitems"] == [] and deleted["unplaced"] == []
    propose(inputs, utterance="撤销口算刚才的修改", operation="undo", changes={}, change_quotes={})
    undo = confirm(inputs)[0]["event_id"]
    restored = service.execute_replan(CHILD, undo, "undo")
    assert restored["receipt"]["allocated_minutes"] == 20
    with pytest.raises(ReplanConflict, match="SOURCE_CHANGED"):
        service.execute_replan(CHILD, event, "delete")


def test_undo_after_replan_invalidates_previous_source_and_does_not_duplicate_work(schedule):
    inputs, service, _ = schedule
    event = revised(schedule)
    first = service.execute_replan(CHILD, event, "first")
    first_item = next(item for item in first["newitems"] if item["source_type"] == "school_homework")
    assert service.inspect_item(CHILD, first_item["plan_item_id"])["eligible"] is True
    propose(inputs, utterance="撤销口算刚才的修改", operation="undo", changes={}, change_quotes={})
    undo = confirm(inputs)[0]["event_id"]
    assert service.inspect_item(CHILD, first_item["plan_item_id"])["eligible"] is False
    second = service.execute_replan(CHILD, undo, "second")
    assert second["receipt"]["allocated_minutes"] == 20
    with inputs[0].connect() as c:
        assert c.execute("SELECT SUM(expected_minutes) FROM plan_item WHERE homework_id='math-a' AND source_type='school_homework' AND status='pending'").fetchone()[0] == 20


def test_executed_scope_dispute_is_saved_for_review_without_overwriting_history(schedule):
    inputs, service, _ = schedule
    with inputs[0].connect() as c:
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,
            source_type,source_ref,started_at,completed_at,active_seconds,actual_minutes)
            VALUES ('done','original','math-a','数学','口算',5,1,'completed',0,'school_homework','math-a',
                    '2026-09-07T17:00:00+08:00','2026-09-07T17:05:00+08:00',300,5)""")
        original = tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='done'").fetchone())
        c.commit()
    propose(inputs)
    event = confirm(inputs)[0]["event_id"]
    result = service.execute_replan(CHILD, event, "dispute")
    assert result["newitems"] == [] and result["unplaced"][0]["reason"] == "executed_scope_requires_review"
    with inputs[0].connect() as c:
        assert tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='done'").fetchone()) == original


def test_concurrent_keys_and_same_key_share_one_write_receipt(schedule):
    inputs, service, _ = schedule
    event = revised(schedule)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda key: LearningInputReplanService(inputs[0]).execute_replan(CHILD, event, key), ["one", "one", "two", "three"]))
    assert all(value == results[0] for value in results)
    with inputs[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_replan_receipt").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM learning_input_replan_command").fetchone()[0] == 3


def test_borrowed_transaction_never_commits_and_failure_rolls_back_append(schedule):
    inputs, service, _ = schedule
    event = revised(schedule)
    with inputs[0].connect() as c:
        c.execute("BEGIN IMMEDIATE")
        service.execute_replan(CHILD, event, "borrowed", connection=c)
        assert c.in_transaction
        c.rollback()
    with inputs[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_replan_receipt").fetchone()[0] == 0
        assert c.execute("SELECT version FROM daily_plan").fetchone()[0] == 3
        c.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON learning_input_replan_receipt BEGIN SELECT RAISE(ABORT,'receipt_failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match="receipt_failure"):
        service.execute_replan(CHILD, event, "failure")
    with inputs[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item").fetchone()[0] == 2
        assert c.execute("SELECT version FROM daily_plan").fetchone()[0] == 3


@pytest.mark.parametrize("change", ["changed", "deleted", "other_child"])
def test_original_artifact_is_owned_and_immutable_at_replan(schedule, change):
    inputs, service, _ = schedule
    db, sid, _ = inputs
    with db.connect() as c:
        c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,extracted_text,details_json,status,created_at,client_action_id)
            VALUES ('artifact',?,?,'document','Original question','{}','confirmed',datetime('now'),'artifact')""", (CHILD, sid))
        c.execute("UPDATE homework_item SET source_artifact_id='artifact' WHERE homework_id='math-a'")
        c.commit()
    event = revised(schedule)
    with db.connect() as c:
        if change == "changed":
            c.execute("UPDATE capture_artifact SET extracted_text='A different question'")
        elif change == "deleted":
            c.execute("DELETE FROM capture_artifact WHERE artifact_id='artifact'")
        else:
            c.execute("UPDATE capture_artifact SET child_id='child-junior-3' WHERE artifact_id='artifact'")
        c.commit()
    with pytest.raises(ReplanConflict, match="ARTIFACT_SOURCE_CHANGED"):
        service.execute_replan(CHILD, event, "artifact")


def test_other_child_time_and_keys_do_not_change_this_child_budget_or_expose_receipts(schedule):
    inputs, service, _ = schedule
    db, _, _ = inputs
    with db.connect() as c:
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('other-session','child-junior-3','completed','completed',datetime('now'),datetime('now'),'other-session')""")
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('other-plan','child-junior-3','other-session','2026-09-07','active',900,'test',1,datetime('now'),'other-plan')""")
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,active_seconds)
            VALUES ('other-work','other-plan','数学','另一孩子的任务',900,1,'completed',1,54000)""")
        c.commit()
    event = revised(schedule)
    with pytest.raises(ReplanConflict, match="EVENT_NOT_OWNED"):
        service.execute_replan("child-junior-3", event, "same-key")
    result = service.execute_replan(CHILD, event, "same-key")
    assert result["receipt"]["allocated_minutes"] == 30
    assert result["receipt"]["budget"]["actual_spent_seconds"] == 0
    assert service.inspect_item("child-junior-3", result["newitems"][0]["plan_item_id"])["status"] == "unknown"


def test_actual_timing_protects_even_legacy_pending_item_without_started_at(schedule):
    inputs, service, _ = schedule
    with inputs[0].connect() as c:
        c.execute("UPDATE plan_item SET active_seconds=120 WHERE plan_item_id='old'")
        c.commit()
    event = revised(schedule)
    with inputs[0].connect() as c:
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='old'").fetchone()[0] == "pending"
    result = service.execute_replan(CHILD, event, "legacy-timing")
    assert result["newitems"] == []
    assert result["unplaced"][0]["reason"] == "no_cancelled_unstarted_school_obligation"


def test_old_action_key_cannot_be_reused_for_a_different_revision(schedule):
    inputs, service, _ = schedule
    event = revised(schedule)
    service.execute_replan(CHILD, event, "key")
    event2 = revised(schedule, minutes=40)
    with pytest.raises(ReplanConflict, match="IDEMPOTENCY_CONFLICT"):
        service.execute_replan(CHILD, event2, "key")


def test_privacy_hard_delete_is_allowed_and_does_not_rewrite_original_tasks(schedule):
    inputs, service, _ = schedule
    event = revised(schedule)
    service.execute_replan(CHILD, event, "privacy")
    with inputs[0].connect() as c:
        before = [tuple(row) for row in c.execute("SELECT * FROM plan_item ORDER BY plan_item_id")]
        for table in ("learning_input_replan_item", "learning_input_replan_unplaced", "learning_input_replan_command", "learning_input_replan_receipt"):
            c.execute(f"DELETE FROM {table} WHERE child_id=?", (CHILD,))
        c.commit()
        assert [tuple(row) for row in c.execute("SELECT * FROM plan_item ORDER BY plan_item_id")] == before


@pytest.mark.parametrize("utc_hour,expected", [(10, "scheduled"), (14, "deferred"), (17, "day_changed"), (31, "day_changed")])
def test_original_nanjing_day_is_not_reinterpreted_as_host_evening(schedule, utc_hour, expected):
    inputs, service, clock = schedule
    event = revised(schedule)
    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        clock[0] = datetime(2026, 9, 7 + utc_hour // 24, utc_hour % 24, tzinfo=timezone.utc)
        if expected == "day_changed":
            with pytest.raises(ReplanConflict, match="DAY_CHANGED"):
                service.execute_replan(CHILD, event, "date")
            return
        result = service.execute_replan(CHILD, event, "date")
        assert result["receipt"]["status"] == expected
        budget = result["receipt"]["budget"]
        assert budget["planning_date"] == "2026-09-07"
        assert budget["cutoff_at"] == "2026-09-07T21:30:00+08:00"
        if expected == "deferred":
            assert result["newitems"] == []
            assert result["unplaced"][0]["reason"] == "latest_end_capacity"
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


def parent_adjustments(schedule):
    from home_study_agent.repository import StudyRepository
    from home_study_agent.reports import build_daily_report
    from home_study_agent.parent_reports import ParentReportService
    from home_study_agent.report_followup import ReportFollowupService
    db = schedule[0][0]
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('prior-report',?,'completed','completed',datetime('now'),datetime('now'),'prior-report')""", (CHILD,))
        c.execute("""INSERT INTO daily_reflection(reflection_id,session_id,child_id,learned_text,difficult_text,feeling,next_action,skipped,created_at,client_action_id)
            VALUES ('prior-reflection','prior-report',?,'','','okay','',1,datetime('now'),'prior-reflection')""", (CHILD,))
        c.commit()
    summary, coaching = build_daily_report(repo.report_evidence('prior-report'))
    report = repo.save_daily_report('prior-report', summary, coaching)
    book = ParentReportService(db)
    book.initialize()
    snapshot = book.approve(book.create_from_daily(report)['snapshot_id'])
    book.record_feedback(snapshot['snapshot_id'], 4, 3, 'Helpful, not evidence of execution', 'feedback')
    service = ReportFollowupService(db)
    return service, snapshot['snapshot_id']


@pytest.mark.parametrize("state", ["approved", "proposal", "retracted", "source_retracted", "other_date"])
def test_real_parent_constraint_projection_only_current_approval_limits_replan(schedule, state):
    inputs, service, _ = schedule
    followup, snapshot = parent_adjustments(schedule)
    event = revised(schedule)
    day = '2026-09-08' if state == 'other_date' else '2026-09-07'
    for kind, value in [('daily_total_minutes_cap', 35), ('task_segment_minutes_cap', 5)]:
        proposal = followup.propose_plan_adjustment(CHILD, snapshot, 1, kind, value, day, day, 'propose-' + kind)
        if state != 'proposal':
            followup.review_plan_adjustment(CHILD, proposal['adjustment_id'], 1, 'approve', 'approve-' + kind)
        if state == 'retracted':
            followup.review_plan_adjustment(CHILD, proposal['adjustment_id'], 2, 'retract', 'retract-' + kind)
    if state == 'source_retracted':
        followup.retract_feedback(CHILD, snapshot, 1, 'retract-feedback', 'Wrong report')
    with inputs[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        expected = followup.project_for_plan(CHILD, '2026-09-07', connection=c)
        result = service.execute_replan(CHILD, event, 'parent-caps', connection=c)
        assert c.in_transaction
        assert result['receipt']['budget']['parent_constraints'] == expected['constraints']
        c.commit()
    budget = result['receipt']['budget']
    work = [row for row in result['newitems'] if row['source_type'] == 'school_homework']
    if state == 'approved':
        assert budget['effective_daily_total_minutes_cap'] == 35
        assert budget['effective_task_segment_minutes_cap'] == 5
        assert result['receipt']['allocated_minutes'] < 30
        assert max(row['expected_minutes'] for row in work) <= 5
        assert sum(row['expected_minutes'] for row in result['newitems']) + 10 <= 35
        assert result['unplaced'] and all(not row['learning_effect_verified'] for row in budget['parent_constraints'])
    else:
        assert budget['parent_constraints'] == []
        assert result['receipt']['allocated_minutes'] == 30


def test_strictest_parent_caps_apply_in_borrowed_write_transaction(schedule, monkeypatch):
    from home_study_agent.report_followup import ReportFollowupService
    inputs, service, _ = schedule
    followup, snapshot = parent_adjustments(schedule)
    event = revised(schedule)
    for index, (kind, value) in enumerate([('daily_total_minutes_cap', 35), ('daily_total_minutes_cap', 20),
                                         ('task_segment_minutes_cap', 10), ('task_segment_minutes_cap', 5)]):
        proposal = followup.propose_plan_adjustment(CHILD, snapshot, 1, kind, value, '2026-09-07', '2026-09-07', f'p{index}')
        followup.review_plan_adjustment(CHILD, proposal['adjustment_id'], 1, 'approve', f'a{index}')
    original = ReportFollowupService.project_for_plan
    calls = []
    with inputs[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        def read(instance, child, day, *, connection=None):
            assert connection is c and connection.in_transaction
            calls.append(day)
            return original(instance, child, day, connection=connection)
        monkeypatch.setattr(ReportFollowupService, 'project_for_plan', read)
        result = service.execute_replan(CHILD, event, 'tightest', connection=c)
        assert calls == ['2026-09-07']
        assert sum(row['expected_minutes'] for row in result['newitems']) + 10 <= 20
        assert result['receipt']['budget']['effective_task_segment_minutes_cap'] == 5
        c.rollback()
    with inputs[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['none', 'source', 'artifact', 'session', 'plan_day'])
def test_start_write_source_gate_is_readonly_and_rechecks_real_binding(schedule, change):
    inputs, service, _ = schedule
    db, sid, _ = inputs
    with db.connect() as c:
        c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,extracted_text,details_json,status,created_at,client_action_id)
            VALUES ('start-artifact',?,?,'document','Original question','{}','confirmed',datetime('now'),'start-artifact')""", (CHILD, sid))
        c.execute("UPDATE homework_item SET source_artifact_id='start-artifact' WHERE homework_id='math-a'")
        c.commit()
    event = revised(schedule)
    result = service.execute_replan(CHILD, event, 'source-gate')
    item = next(row for row in result['newitems'] if row['source_type'] == 'school_homework')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE plan_item SET status='active',started_at='2026-09-07T18:00:00+08:00',active_segment_started_at='2026-09-07T18:00:00+08:00' WHERE plan_item_id=?", (item['plan_item_id'],))
        if change == 'source':
            c.execute("UPDATE homework_item SET title='Other work' WHERE homework_id='math-a'")
        elif change == 'artifact':
            c.execute("UPDATE capture_artifact SET extracted_text='Other question' WHERE artifact_id='start-artifact'")
        elif change == 'session':
            c.execute("UPDATE daily_session SET status='cancelled' WHERE session_id=?", (sid,))
        elif change == 'plan_day':
            c.execute("UPDATE daily_plan SET plan_date='2026-09-08' WHERE plan_id='original'")
        changes = c.total_changes
        gate = service.inspect_item(CHILD, item['plan_item_id'], connection=c)
        assert gate['eligible'] is (change == 'none')
        assert c.total_changes == changes
        c.rollback()


def test_failed_parent_command_retries_original_confirmed_event_after_restart(schedule):
    inputs, service, _ = schedule
    db, _, revisions = inputs
    event = revised(schedule)
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON learning_input_replan_receipt BEGIN SELECT RAISE(ABORT,'temporary_failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='temporary_failure'):
        service.execute_replan(CHILD, event, 'original-http-replan')
    with db.connect() as c:
        turn = c.execute('SELECT client_action_id FROM learning_input_revision_event WHERE event_id=?', (event,)).fetchone()[0]
        c.execute("UPDATE agent_turn SET tool_status='failed' WHERE agent_turn_id=?", (turn,))
        c.execute("UPDATE agent_plan SET status='failed' WHERE agent_turn_id=?", (turn,))
        c.execute("UPDATE agent_step SET status='failed' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        c.execute('DROP TRIGGER fail_receipt')
        c.commit()
        assert c.execute("SELECT expected_minutes FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == 30
    assert revisions.inspect_replan_obligation(CHILD, event)['status'] == 'pending'
    assert revisions.execute(turn)['event_id'] == event
    reopened = LearningInputReplanService(db)
    result = reopened.execute_replan(CHILD, event, 'original-http-replan')
    assert result['receipt']['allocated_minutes'] == 30
    assert reopened.execute_replan(CHILD, event, 'original-http-replan') == result
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['scheduled', 'partial', 'deferred', 'deleted'])
def test_recorded_obligation_projection_exposes_actual_receipt_without_changing_event(schedule, mode):
    inputs, service, _ = schedule
    db, _, revisions = inputs
    with db.connect() as c:
        if mode in {'partial', 'deferred'}:
            c.execute('UPDATE family_rule SET max_learning_minutes=? WHERE child_id=?', (25 if mode == 'partial' else 10, CHILD))
            c.commit()
    event = revised(schedule, operation='delete' if mode == 'deleted' else 'update')
    assert revisions.inspect_replan_obligation(CHILD, event)['status'] == 'pending'
    with db.connect() as c:
        frozen = tuple(c.execute('SELECT * FROM learning_input_revision_event WHERE event_id=?', (event,)).fetchone())
    actual = service.execute_replan(CHILD, event, 'recorded-projection')
    assert actual['receipt']['status'] == mode
    reopened = LearningInputRevisionService(db)
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        projected = reopened.inspect_replan_obligation(CHILD, event, connection=c)
        assert projected['status'] == 'recorded'
        assert projected['source_current'] is True
        assert projected['receipt'] == actual['receipt']
        assert projected['newitems'] == actual['newitems']
        assert projected['unplaced'] == actual['unplaced']
        assert tuple(c.execute('SELECT * FROM learning_input_revision_event WHERE event_id=?', (event,)).fetchone()) == frozen
    assert service.execute_replan(CHILD, event, 'recorded-projection') == actual


@pytest.mark.parametrize('change', ['source', 'artifact', 'undo', 'plan_progress'])
def test_recorded_receipt_is_history_not_false_current_source_after_retraction(schedule, change):
    inputs, service, _ = schedule
    db, sid, revisions = inputs
    with db.connect() as c:
        c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,extracted_text,details_json,status,created_at,client_action_id)
            VALUES ('projection-artifact',?,?,'document','Original question','{}','confirmed',datetime('now'),'projection-artifact')""", (CHILD, sid))
        c.execute("UPDATE homework_item SET source_artifact_id='projection-artifact' WHERE homework_id='math-a'")
        c.commit()
    event = revised(schedule)
    actual = service.execute_replan(CHILD, event, 'history-projection')
    if change == 'undo':
        propose(inputs, utterance='撤销口算刚才的修改', operation='undo', changes={}, change_quotes={})
        confirm(inputs)
    else:
        with db.connect() as c:
            if change == 'source':
                c.execute("UPDATE homework_item SET title='Other work' WHERE homework_id='math-a'")
            elif change == 'artifact':
                c.execute("DELETE FROM capture_artifact WHERE artifact_id='projection-artifact'")
            else:
                c.execute("UPDATE plan_item SET active_seconds=60,actual_minutes=1,status='completed' WHERE plan_item_id=?", (actual['newitems'][0]['plan_item_id'],))
                c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='original'")
            c.commit()
    view = revisions.inspect_replan_obligation(CHILD, event)
    assert view['status'] == 'recorded'
    assert view['receipt'] == actual['receipt']
    assert view['source_current'] is (change == 'plan_progress')
    from home_study_agent.learning_input_revision import InputRevisionError
    with pytest.raises(InputRevisionError, match='EVENT_NOT_FOUND'):
        revisions.inspect_replan_obligation('child-junior-3', event)
