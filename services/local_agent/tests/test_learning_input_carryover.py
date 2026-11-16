"""Unplaced work is consumed explicitly, with real rows and durable accounting."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from uuid import uuid4

import pytest

from home_study_agent.learning_input_carryover import LearningInputCarryoverService, CarryoverConflict
from test_learning_input_replan import schedule, revised, parent_adjustments
from test_learning_input_revision_voice import inputs, flow, CHILD, propose, confirm


@pytest.fixture
def carry(schedule, request):
    source, replan, clock = schedule
    db, original_sid, _ = source
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=10 WHERE child_id=?', (CHILD,))
        if getattr(request, 'param', {}).get('private_markers'):
            c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,extracted_text,details_json,status,created_at,client_action_id)
                VALUES ('private-artifact',?,?,'document','LOCAL_STEM_PRIVATE_8ab','{"evidence":"LOCAL_EVIDENCE_PRIVATE_9bc","cloud_transmission_allowed":false}','confirmed',datetime('now'),'private-artifact')""", (CHILD, original_sid))
            c.execute("UPDATE homework_item SET title='口算_LOCAL_TITLE_PRIVATE_7cd',source_artifact_id='private-artifact' WHERE homework_id='math-a'")
        if getattr(request, 'param', {}).get('due_at'):
            c.execute("UPDATE homework_item SET due_at=? WHERE homework_id='math-a'", (request.param['due_at'],))
        c.commit()
    event = revised(schedule)
    initial = replan.execute_replan(CHILD, event, 'unplaced-source')
    assert initial['unplaced'][0]['minutes'] == 30
    if getattr(request, 'param', {}).get('two'):
        with db.connect() as c:
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref)
                VALUES ('old-b','original','math-b','数学','应用题',20,1,'pending',3,'school_homework','math-b')""")
            c.commit()
        propose(source, utterance='应用题改成25分钟', target='应用题', changes={'expected_minutes': 25}, change_quotes={'expected_minutes': '25分钟'})
        second = confirm(source)[0]['event_id']
        assert replan.execute_replan(CHILD, second, 'second-root')['unplaced']
    clock[0] = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id=?", (clock[0].isoformat(), original_sid))
        c.execute('UPDATE family_rule SET max_learning_minutes=120 WHERE child_id=?', (CHILD,))
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('today',?,'active','homework_capture',?,?,'today')""", (CHILD, clock[0].isoformat(), clock[0].isoformat()))
        c.execute("INSERT INTO session_planning_context(session_id,energy_level,fixed_commitment_minutes,tomorrow_event_minutes,updated_at) VALUES ('today','medium',0,0,?)", (clock[0].isoformat(),))
        c.commit()
    service = LearningInputCarryoverService(db)
    service.initialize()
    return db, service, initial['unplaced'][0]['obligation_id'], clock


def plan(db, *, total=10):
    with db.connect() as c:
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('today-plan',?,'today','2026-09-08','active',?,'test',1,'2026-09-08T10:00:00+00:00','today-plan')""", (CHILD, total))
        if total:
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type)
                VALUES ('today-existing','today-plan','语文','保留任务',?,1,'pending',1,'school_homework')""", (total,))
        c.execute("UPDATE daily_session SET state='study' WHERE session_id='today'")
        c.commit()


def grant(carry, decision='continue_today', *, not_before=None, key=None, obligation_id=None, target_session_id='today'):
    db, service, oid, _ = carry
    oid = obligation_id or oid
    view = service.list_unplaced(CHILD, target_session_id)
    candidate = next(item for item in view['items'] if item['obligation_id'] == oid)
    binding = candidate['binding']
    key = key or str(uuid4())
    quote = {'continue_today': '今天继续安排口算', 'drop': '取消剩余口算的安排', 'keep': f'口算保留到{not_before}'}[decision]
    arguments = {'decision': decision, 'request_quote': quote, 'not_before': not_before}
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,?,'input_carryover','','{}','pending','test',datetime('now'),?)""", (key, CHILD, target_session_id, quote, key))
        c.execute("""INSERT INTO agent_plan(plan_id,child_id,session_id,agent_turn_id,planner_version,context_manifest_json,plan_json,status,created_at,updated_at)
            VALUES (?,?,?,?,'test',?,'{}','running',datetime('now'),datetime('now'))""", (key, CHILD, target_session_id, key, json.dumps({'learning_input_unplaced_binding': binding})))
        c.execute("""INSERT INTO agent_step(step_id,plan_id,step_index,capability_name,capability_version,execution_location,risk_level,arguments_json,expected_evidence_json,status,idempotency_key,created_at,updated_at)
            VALUES (?,?,0,'learning_inputs.review_unplaced','1','server','medium',?,'[]','running',?,datetime('now'),datetime('now'))""", (key, key, json.dumps(arguments), key))
        c.commit()
    return dict(child_id=CHILD, obligation_id=oid, target_session_id=target_session_id, decision=decision,
        expected_revision=candidate['revision'], expected_source_version=candidate['source_version'],
        expected_context_version=view['context_version'], client_action_id=key,
        authorized_turn_id=key, request_quote=quote, not_before=not_before)


def test_active_plan_consumes_real_remaining_work_without_reviving_old_tasks(carry):
    db, service, oid, _ = carry
    plan(db)
    with db.connect() as c:
        before = [tuple(row) for row in c.execute("SELECT * FROM plan_item WHERE plan_id='original' ORDER BY plan_item_id")]
    command = grant(carry)
    result = service.review_unplaced(**command)
    assert result['status'] == 'scheduled' and result['allocated_minutes'] == 30
    assert result['remaining_minutes'] == 0 and result['newitems']
    assert service.review_unplaced(**command) == result
    assert LearningInputCarryoverService(db).review_unplaced(**command) == result
    with db.connect() as c:
        assert [tuple(row) for row in c.execute("SELECT * FROM plan_item WHERE plan_id='original' ORDER BY plan_item_id")] == before
        assert c.execute("SELECT SUM(expected_minutes) FROM plan_item WHERE plan_id='today-plan' AND source_type='school_homework'").fetchone()[0] == 40
    assert service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 0


def test_no_plan_acceptance_is_real_homework_not_allocation_and_postplan_hook_accounts_it(carry):
    db, service, oid, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    assert accepted['status'] == 'accepted_for_planning' and accepted['allocated_minutes'] == 0
    assert accepted['remaining_minutes'] == 30 and accepted['newitems'] == []
    hid = accepted['target_homework_id']
    with db.connect() as c:
        assert c.execute('SELECT expected_minutes FROM homework_item WHERE homework_id=?', (hid,)).fetchone()[0] == 30
    plan(db, total=0)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for index, minutes in enumerate([15, 15]):
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref)
                VALUES (?,'today-plan',?,'数学','口算',?,1,'pending',?,'school_homework',?)""", (f'generated-{index}', hid, minutes, index * 2 + 1, hid))
        c.execute("INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type) VALUES ('rest','today-plan','休息','休息',5,1,'pending',2,'planned_break')")
        c.execute("UPDATE daily_plan SET total_minutes=35 WHERE plan_id='today-plan'")
        receipt = service.bind_generated_plan(CHILD, 'today', 'today-plan', 'generated-plan', connection=c)
        assert c.in_transaction
        assert receipt['allocated_minutes'] == 30
        c.commit()
    assert service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 0
    assert service.inspect_item(CHILD, 'generated-0')['eligible']


@pytest.mark.parametrize('decision', ['drop', 'keep'])
def test_explicit_drop_keep_are_durable_not_learning_completion(carry, decision):
    db, service, _, _ = carry
    command = grant(carry, decision, not_before='2026-09-10' if decision == 'keep' else None)
    result = service.review_unplaced(**command)
    assert result['allocated_minutes'] == 0 and result['newitems'] == []
    assert result['learning_evidence_changed'] is False
    assert LearningInputCarryoverService(db).review_unplaced(**command) == result
    candidate = service.list_unplaced(CHILD, 'today')['items'][0]
    assert 'continue_today' not in candidate['allowed_actions']
    assert candidate['remaining_minutes'] == (30 if decision == 'keep' else 0)


@pytest.mark.parametrize('change', ['source', 'version', 'budget', 'day', 'cancel', 'quote', 'child'])
def test_stale_binding_is_rejected_before_any_carryover_write(carry, change):
    db, service, _, clock = carry
    plan(db)
    args = grant(carry)
    with db.connect() as c:
        if change == 'source':
            c.execute("UPDATE homework_item SET title='other source' WHERE homework_id='math-a'")
        elif change == 'version':
            c.execute("UPDATE daily_plan SET version=2 WHERE plan_id='today-plan'")
        elif change == 'budget':
            c.execute('UPDATE family_rule SET max_learning_minutes=130 WHERE child_id=?', (CHILD,))
        elif change == 'cancel':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE plan_id=?", (args['authorized_turn_id'],))
        elif change == 'quote':
            args['request_quote'] = '别继续'
        elif change == 'child':
            args['child_id'] = 'child-junior-3'
        else:
            clock[0] = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
        c.commit()
    with pytest.raises(CarryoverConflict):
        service.review_unplaced(**args)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 0


def generated(c, hid, minutes=(15, 15), *, rest=True, residual=0):
    for index, value in enumerate(minutes):
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref)
            VALUES (?,'today-plan',?,'数学','口算',?,1,'pending',?,'school_homework',?)""", (f'g{index}', hid, value, index * 2 + 1, hid))
        if index and rest:
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type)
                VALUES (?,'today-plan','休息','休息',5,1,'pending',?,'planned_break')""", (f'r{index}', index * 2))
    if residual:
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref)
            VALUES ('residual','today-plan',?,'数学','口算',?,1,'rescheduled',99,'school_homework',?)""", (hid, residual, hid))


def test_generated_plan_without_required_rest_rolls_back_receipt(carry):
    db, service, _, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    plan(db, total=0)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        generated(c, accepted['target_homework_id'], rest=False)
        with pytest.raises(CarryoverConflict, match='REST'):
            service.bind_generated_plan(CHILD, 'today', 'today-plan', 'generated', connection=c)
        assert c.in_transaction
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_plan_receipt').fetchone()[0] == 0
        c.rollback()


def test_generated_residual_is_not_allocated_reserved_or_restartable(carry):
    db, service, _, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    plan(db, total=0)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE family_rule SET max_learning_minutes=15 WHERE child_id=?', (CHILD,))
        generated(c, accepted['target_homework_id'], minutes=(15,), residual=15)
        result = service.bind_generated_plan(CHILD, 'today', 'today-plan', 'generated', connection=c)
        assert result['allocated_minutes'] == 15
        assert result['allocations'][0]['remaining_minutes'] == 15
        c.commit()
    assert not service.inspect_item(CHILD, 'residual')['eligible']
    with db.connect() as c:
        assert c.execute("SELECT 1 FROM plan_item_event WHERE plan_item_id='residual' AND event_type='revision_replanned'").fetchone()
        c.execute("UPDATE plan_item SET status='pending' WHERE plan_item_id='residual'")
        c.commit()
    assert not service.replanner.inspect_item(CHILD, 'residual')['eligible']


def test_child_retirement_and_source_deletion_block_allocated_items(carry):
    db, service, _, _ = carry
    plan(db)
    result = service.review_unplaced(**grant(carry))
    iid = next(row['plan_item_id'] for row in result['newitems'] if row['source_type'] == 'school_homework')
    assert service.replanner.inspect_item(CHILD, iid)['eligible']
    with db.connect() as c:
        c.execute("UPDATE child_profile SET status='archived' WHERE child_id=?", (CHILD,))
        c.commit()
    assert not service.replanner.inspect_item(CHILD, iid)['eligible']


@pytest.mark.parametrize('decision', ['drop', 'keep'])
def test_acceptance_can_be_cancelled_without_resurrecting_it_on_generation(carry, decision):
    db, service, _, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    next_command = grant(carry, decision, not_before='2026-09-10' if decision == 'keep' else None)
    result = service.review_unplaced(**next_command)
    with db.connect() as c:
        assert c.execute('SELECT status FROM homework_item WHERE homework_id=?', (accepted['target_homework_id'],)).fetchone()[0] == 'cancelled'
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
    plan(db, total=0)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert service.bind_generated_plan(CHILD, 'today', 'today-plan', 'empty-plan', connection=c)['allocations'] == []
        c.commit()
    assert result['learning_evidence_changed'] is False


@pytest.mark.parametrize('change', ['source', 'cancel', 'budget', 'segment', 'executed', 'target'])
def test_generation_revalidates_acceptance_in_its_write_transaction(carry, change):
    db, service, _, _ = carry
    command = grant(carry)
    accepted = service.review_unplaced(**command)
    plan(db, total=0)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        generated(c, accepted['target_homework_id'])
        if change == 'source':
            c.execute("UPDATE homework_item SET title='别题' WHERE homework_id='math-a'")
        elif change == 'cancel':
            c.execute("UPDATE agent_step SET status='cancelled' WHERE step_id=?", (command['client_action_id'],))
        elif change == 'budget':
            c.execute('UPDATE family_rule SET max_learning_minutes=20 WHERE child_id=?', (CHILD,))
        elif change == 'segment':
            c.execute('UPDATE family_rule SET task_segment_minutes=5 WHERE child_id=?', (CHILD,))
        elif change == 'executed':
            c.execute("UPDATE plan_item SET active_seconds=1 WHERE plan_item_id='g0'")
        else:
            c.execute('UPDATE homework_item SET title=? WHERE homework_id=?', ('另题', accepted['target_homework_id']))
        with pytest.raises(CarryoverConflict):
            service.bind_generated_plan(CHILD, 'today', 'today-plan', 'generated', connection=c)
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_plan_receipt').fetchone()[0] == 0
        c.rollback()


def test_generated_receipt_and_allocation_share_caller_rollback_and_restart(carry):
    from home_study_agent.database import Database
    db, service, _, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    plan(db, total=0)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        generated(c, accepted['target_homework_id'])
        service.bind_generated_plan(CHILD, 'today', 'today-plan', 'generated', connection=c)
        c.rollback()
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 1
        assert not c.execute("SELECT 1 FROM plan_item WHERE plan_id='today-plan'").fetchone()
        c.execute('BEGIN IMMEDIATE')
        generated(c, accepted['target_homework_id'])
        result = service.bind_generated_plan(CHILD, 'today', 'today-plan', 'generated', connection=c)
        c.commit()
    restarted = LearningInputCarryoverService(Database(db.path))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert restarted.bind_generated_plan(CHILD, 'today', 'today-plan', 'generated', connection=c) == result
        with pytest.raises(CarryoverConflict, match='REPLAY'):
            restarted.bind_generated_plan(CHILD, 'today', 'today-plan', 'other-key', connection=c)
        c.commit()


@pytest.mark.parametrize('same_key', [True, False])
def test_concurrent_writes_allocate_once_and_reject_stale_revision(carry, same_key):
    db, service, _, _ = carry
    plan(db)
    one = grant(carry)
    two = one if same_key else grant(carry)
    def run(args):
        try:
            return LearningInputCarryoverService(db).review_unplaced(**args)
        except CarryoverConflict as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [one, two]))
    if same_key:
        assert results[0] == results[1]
    else:
        assert sum(isinstance(result, dict) for result in results) == 1
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 1
        assert c.execute("SELECT SUM(expected_minutes) FROM plan_item WHERE plan_id='today-plan' AND source_type='school_homework'").fetchone()[0] == 40


def test_same_key_different_payload_cannot_change_completed_or_dropped_receipt(carry):
    db, service, _, _ = carry
    args = grant(carry, 'drop')
    result = service.review_unplaced(**args)
    assert service.review_unplaced(**args) == result
    with pytest.raises(CarryoverConflict, match='IDEMPOTENCY'):
        service.review_unplaced(**{**args, 'decision': 'continue_today'})
    with pytest.raises(CarryoverConflict):
        service.review_unplaced(**grant(carry))


@pytest.mark.parametrize('kind,allocated', [('fixed', 10), ('pet', 10), ('actual', 10), ('curfew', 5)])
def test_real_budget_reserves_other_work_time_breaks_and_end_time(carry, kind, allocated):
    db, service, _, clock = carry
    plan(db)
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=40 WHERE child_id=?', (CHILD,))
        if kind == 'fixed':
            c.execute("UPDATE session_planning_context SET fixed_commitment_minutes=15 WHERE session_id='today'")
        elif kind == 'pet':
            c.execute('UPDATE family_rule SET pet_min_minutes=15 WHERE child_id=?', (CHILD,))
        elif kind == 'actual':
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,active_seconds)
                VALUES ('done','today-plan','数学','已做',15,1,'completed',0,'school_homework',900)""")
        else:
            clock[0] = datetime(2026, 9, 8, 13, 10, tzinfo=timezone.utc)
        c.commit()
    result = service.review_unplaced(**grant(carry))
    assert result['allocated_minutes'] == allocated
    assert result['remaining_minutes'] == 30 - allocated
    budget = result['budget']
    assert (sum(item['expected_minutes'] for item in result['newitems']) * 60
        + sum(budget[key] for key in ('actual_spent_seconds', 'protected_remaining_seconds', 'fixed_reserved_seconds', 'pet_reserved_seconds')) <= 40 * 60)


@pytest.mark.parametrize('carry', [{'due_at': '2026-09-08T18:20:00+08:00'}, {'due_at': '2026-09-08'}], indirect=True)
def test_school_deadline_or_unknown_time_never_implicitly_extends_day(carry):
    db, service, _, _ = carry
    plan(db)
    result = service.review_unplaced(**grant(carry))
    assert result['allocated_minutes'] in {0, 5}
    assert result['remaining_minutes'] >= 25


def test_approved_parent_caps_are_read_in_actual_carryover_transaction(carry, schedule):
    db, service, _, _ = carry
    followup, snapshot = parent_adjustments(schedule)
    for kind, value in [('daily_total_minutes_cap', 20), ('task_segment_minutes_cap', 5)]:
        p = followup.propose_plan_adjustment(CHILD, snapshot, 1, kind, value, '2026-09-08', '2026-09-08', kind)
        followup.review_plan_adjustment(CHILD, p['adjustment_id'], 1, 'approve', 'approve-' + kind)
    plan(db)
    args = grant(carry)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        result = service.review_unplaced(**args, connection=c)
        assert result['allocated_minutes'] == 5 and result['remaining_minutes'] == 25
        assert result['budget']['effective_daily_total_minutes_cap'] == 20
        assert result['budget']['effective_task_segment_minutes_cap'] == 5
        assert c.in_transaction
        c.commit()


@pytest.mark.parametrize('carry', [{'two': True}], indirect=True)
def test_two_obligations_share_actual_budget_and_plan_version(carry):
    db, service, _, _ = carry
    plan(db, total=0)
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=45 WHERE child_id=?', (CHILD,))
        c.commit()
    candidates = service.list_unplaced(CHILD, 'today')['items']
    assert len(candidates) == 2
    first = grant(carry)
    other_id = next(item['obligation_id'] for item in candidates if item['obligation_id'] != carry[2])
    second = grant(carry, obligation_id=other_id)
    assert service.review_unplaced(**first)['allocated_minutes'] == 30
    with pytest.raises(CarryoverConflict):
        service.review_unplaced(**second)
    result = service.review_unplaced(**grant(carry, obligation_id=other_id))
    assert result['allocated_minutes'] == 5 and result['remaining_minutes'] == 20
    with db.connect() as c:
        assert c.execute("SELECT SUM(expected_minutes) FROM plan_item WHERE plan_id='today-plan'").fetchone()[0] == 45


def test_other_child_budget_and_candidate_are_isolated(carry):
    db, service, _, clock = carry
    plan(db)
    with db.connect() as c:
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('other','child-junior-3','completed','completed',?,?,'other')""", (clock[0].isoformat(), clock[0].isoformat()))
        c.execute("INSERT INTO session_planning_context(session_id,energy_level,fixed_commitment_minutes,tomorrow_event_minutes,updated_at) VALUES ('other','medium',0,0,?)", (clock[0].isoformat(),))
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('other-plan','child-junior-3','other','2026-09-08','active',900,'test',1,?,'other-plan')""", (clock[0].isoformat(),))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,active_seconds)
            VALUES ('other-work','other-plan','数学','另一孩子',900,1,'completed',1,54000)""")
        c.commit()
    with pytest.raises(CarryoverConflict):
        service.list_unplaced('child-junior-3', 'today')
    result = service.review_unplaced(**grant(carry))
    assert result['allocated_minutes'] == 30 and result['budget']['actual_spent_seconds'] == 0
    assert not service.replanner.inspect_item('child-junior-3', result['newitems'][0]['plan_item_id'])['eligible']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id='today'")
        c.execute("UPDATE daily_session SET status='active',state='study' WHERE session_id='other'")
        c.commit()
    assert service.list_unplaced('child-junior-3', 'other')['items'] == []


@pytest.mark.parametrize('when', ['2026-09-08', '2026-09-07', 'not-a-date'])
def test_keep_cannot_invent_or_backdate_future_day(carry, when):
    _, service, _, _ = carry
    with pytest.raises(CarryoverConflict, match='KEEP_DATE'):
        service.review_unplaced(**grant(carry, 'keep', not_before=when))


def test_kept_obligation_becomes_actionable_on_explicit_future_session(carry):
    db, service, _, clock = carry
    kept_args = grant(carry, 'keep', not_before='2026-09-10')
    kept = service.review_unplaced(**kept_args)
    with pytest.raises(CarryoverConflict):
        service.review_unplaced(**grant(carry))
    clock[0] = datetime(2026, 9, 10, 10, tzinfo=timezone.utc)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id='today'")
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('future',?,'active','homework_capture',?,?,'future')""", (CHILD, clock[0].isoformat(), clock[0].isoformat()))
        c.execute("INSERT INTO session_planning_context(session_id,energy_level,fixed_commitment_minutes,tomorrow_event_minutes,updated_at) VALUES ('future','medium',0,0,?)", (clock[0].isoformat(),))
        c.commit()
    view = service.list_unplaced(CHILD, 'future')['items'][0]
    assert view['revision'] == 1 and 'continue_today' in view['allowed_actions']
    assert service.review_unplaced(**kept_args) == kept  # Historical replay, not current state.
    accepted = service.review_unplaced(**grant(carry, target_session_id='future'))
    assert accepted['revision'] == 2 and accepted['status'] == 'accepted_for_planning'
    assert accepted['remaining_minutes'] == 30


def test_host_plan_date_is_not_silently_replaced_by_shanghai_date(carry, monkeypatch):
    import os
    import time
    db, service, _, clock = carry
    old_tz = os.environ.get('TZ')
    try:
        os.environ['TZ'] = 'America/Los_Angeles'
        time.tzset()
        clock[0] = datetime(2026, 9, 8, 0, 30, tzinfo=timezone.utc)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET started_at=? WHERE session_id='today'", (clock[0].isoformat(),))
            c.commit()
        # The planner's family date is authoritative, not the worker host date.
        assert service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 30
        clock[0] = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
        plan(db)
        with db.connect() as c:
            c.execute("UPDATE daily_plan SET plan_date='2026-09-07' WHERE plan_id='today-plan'")
            c.commit()
        with pytest.raises(CarryoverConflict, match='PLAN_DAY'):
            service.list_unplaced(CHILD, 'today')
        with db.connect() as c:
            assert c.execute("SELECT plan_date FROM daily_plan WHERE plan_id='today-plan'").fetchone()[0] == '2026-09-07'
    finally:
        if old_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old_tz
        time.tzset()


def test_fault_after_actual_rows_rolls_back_and_same_command_can_retry(carry, monkeypatch):
    db, service, _, _ = carry
    plan(db)
    args = grant(carry)
    original = service._bind
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('injected storage failure')
    monkeypatch.setattr(service, '_bind', fail)
    with pytest.raises(sqlite3.OperationalError):
        service.review_unplaced(**args)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item WHERE plan_id='today-plan'").fetchone()[0] == 1
        assert c.execute("SELECT version FROM daily_plan WHERE plan_id='today-plan'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM homework_item WHERE session_id='today'").fetchone()[0] == 0
    monkeypatch.setattr(service, '_bind', original)
    assert service.review_unplaced(**args)['allocated_minutes'] == 30


def test_read_only_projection_and_hard_delete_do_not_certify_or_resurrect(carry):
    db, service, _, _ = carry
    plan(db)
    result = service.review_unplaced(**grant(carry))
    iid = result['newitems'][0]['plan_item_id']
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert service.list_unplaced(CHILD, 'today', connection=c)['items'][0]['remaining_minutes'] == 0
        assert service.inspect_item(CHILD, iid, connection=c)['eligible']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM learning_input_carryover_plan_receipt WHERE child_id=?', (CHILD,))
        c.execute('DELETE FROM learning_input_carryover_event WHERE child_id=?', (CHILD,))
        c.execute('DELETE FROM learning_input_replan_unplaced WHERE child_id=?', (CHILD,))
        assert c.in_transaction
        c.commit()
    assert not service.replanner.inspect_item(CHILD, iid)['eligible']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0


@pytest.mark.parametrize('allocate', [True, False])
def test_generated_suffix_is_exact_and_empty_allocation_remains_consumable(carry, allocate):
    db, service, _, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    plan(db, total=0)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        generated(c, accepted['target_homework_id'], minutes=(15, 15) if allocate else (), residual=0 if allocate else 30)
        if allocate:
            c.execute("UPDATE plan_item SET title='口算（续2）' WHERE plan_item_id='g1'")
        else:
            c.execute('UPDATE family_rule SET max_learning_minutes=1 WHERE child_id=?', (CHILD,))
        receipt = service.bind_generated_plan(CHILD, 'today', 'today-plan', 'generated', connection=c)
        assert receipt['allocated_minutes'] == (30 if allocate else 0)
        assert receipt['allocations'][0]['remaining_minutes'] == (0 if allocate else 30)
        c.commit()
    if not allocate:
        view = service.list_unplaced(CHILD, 'today')['items'][0]
        assert view['status'] == 'deferred' and 'continue_today' in view['allowed_actions']
        with db.connect() as c:
            assert service.owns_unplaced_item(c, CHILD, 'residual')


def test_allocated_item_cannot_start_on_another_day_with_old_active_plan(carry):
    db, service, _, clock = carry
    plan(db)
    result = service.review_unplaced(**grant(carry))
    iid = result['newitems'][0]['plan_item_id']
    assert service.replanner.inspect_item(CHILD, iid)['eligible']
    clock[0] = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
    assert not service.replanner.inspect_item(CHILD, iid)['eligible']
