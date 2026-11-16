"""Real repository reschedule hooks and adversarial source accounting."""
import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from home_study_agent.repository import StudyRepository, PlanItemStateError
from home_study_agent.learning_input_carryover import CarryoverConflict, LearningInputCarryoverService
from test_learning_input_carryover import carry, schedule, inputs, flow, CHILD, plan, grant
from test_learning_input_replan import revised
from test_learning_input_carryover_voice import say, FakeExecutor
from test_agent_continuation import orchestrator_for
from home_study_agent.agent_runtime import AgentRuntimeStore


@pytest.fixture
def allocated(carry, monkeypatch):
    from home_study_agent import repository
    db, service, _, clock = carry
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    plan(db)
    receipt = service.review_unplaced(**grant(carry))
    repo = StudyRepository(db)
    return carry, repo, receipt


def school(receipt):
    return next(item for item in receipt['newitems'] if item['source_type'] == 'school_homework')


def reclaimed(carry, iid):
    db, service, _, _ = carry
    with db.connect() as c:
        row = c.execute("SELECT obligation_id FROM learning_input_replan_unplaced WHERE json_extract(payload_json,'$.reclaim.plan_item_id')=?", (iid,)).fetchone()
    return next(item for item in service.list_unplaced(CHILD, 'today')['items'] if item['obligation_id'] == row[0])


def scope_command(carry, oid, operation='confirm_scope', minutes=5):
    db, _, _, _ = carry
    args = grant(carry, obligation_id=oid)
    quote = f'还剩第3到5题预计{minutes}分钟' if operation == 'confirm_scope' else '撤回刚才确认的剩余范围'
    scope = '第3到5题' if operation == 'confirm_scope' else None
    fields = dict(operation=operation, request_quote=quote, remaining_minutes=minutes if operation == 'confirm_scope' else None, remaining_scope_quote=scope)
    with db.connect() as c:
        c.execute("UPDATE agent_step SET capability_name='learning_inputs.review_reclaimed_scope',arguments_json=? WHERE step_id=?", (json.dumps(fields, ensure_ascii=False), args['client_action_id']))
        c.execute('UPDATE agent_turn SET transcript=? WHERE agent_turn_id=?', (quote, args['authorized_turn_id']))
        c.commit()
    args.pop('decision')
    args.pop('not_before')
    return {**args, **fields}


def test_actual_reschedule_returns_only_allocated_school_segment_and_replays(allocated, monkeypatch):
    carry, repo, receipt = allocated
    db, service, original_oid, _ = carry
    item = school(receipt)
    assert repo.reschedule_plan_item(item['plan_item_id'], '今天留到以后', 'defer')['status'] == 'rescheduled'
    view = reclaimed(carry, item['plan_item_id'])
    assert view['remaining_minutes'] == item['expected_minutes']
    assert next(i for i in service.list_unplaced(CHILD, 'today')['items'] if i['obligation_id'] == original_oid)['remaining_minutes'] == 0
    with db.connect() as c:
        assert service.owns_unplaced_item(c, CHILD, item['plan_item_id'])
        event = c.execute("SELECT event_id FROM plan_item_event WHERE client_action_id='defer'").fetchone()[0]
        c.execute('BEGIN IMMEDIATE')
        again = LearningInputCarryoverService(db).reclaim_rescheduled(CHILD, item['plan_item_id'], event, 'defer:carryover-reclaim', connection=c)
        assert again['reclaimed_minutes'] == item['expected_minutes']
        c.commit()
    new = service.review_unplaced(**grant(carry, obligation_id=view['obligation_id']))
    assert new['allocated_minutes'] == item['expected_minutes']
    assert all(i['plan_item_id'] != item['plan_item_id'] for i in new['newitems'])
    assert not service.inspect_item(CHILD, item['plan_item_id'])['eligible']


def test_started_task_needs_human_remaining_scope_not_elapsed_mastery(allocated, monkeypatch):
    carry, repo, receipt = allocated
    db, service, _, clock = carry
    item = school(receipt)
    repo.start_plan_item(item['plan_item_id'], 'start')
    clock[0] += timedelta(minutes=4)
    repo.pause_plan_item(item['plan_item_id'], '稍后做', 'pause')
    with db.connect() as c:
        before = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone())
    repo.reschedule_plan_item(item['plan_item_id'], '还有几道要核实', 'defer')
    view = reclaimed(carry, item['plan_item_id'])
    assert view['scope_review_required'] and view['remaining_minutes'] == 0
    assert view['scope_review']['maximum_remaining_minutes'] == item['expected_minutes'] - 4
    assert 'continue_today' not in view['allowed_actions']
    confirmed = service.review_reclaimed_scope(**scope_command(carry, view['obligation_id']))
    assert confirmed['remaining_minutes'] == 5 and confirmed['learning_evidence_changed'] is False
    new = service.review_unplaced(**grant(carry, obligation_id=view['obligation_id']))
    work = school(new)
    assert work['expected_minutes'] == 5 and '第3到5题' in work['title']
    with db.connect() as c:
        old = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone())
        assert old['active_seconds'] == before['active_seconds'] == 240
        assert old['completed_at'] == before['completed_at'] is None
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
    service.review_reclaimed_scope(**scope_command(carry, view['obligation_id'], 'withdraw_scope'))
    assert not service.inspect_item(CHILD, work['plan_item_id'])['eligible']
    assert reclaimed(carry, item['plan_item_id'])['status'] == 'source_invalid'


def test_rest_does_not_create_school_balance(allocated, monkeypatch):
    carry, repo, receipt = allocated
    db, _, _, _ = carry
    rest = next(item for item in receipt['newitems'] if item['source_type'] == 'planned_break')
    repo.reschedule_plan_item(rest['plan_item_id'], '今天休息结束', 'rest-defer')
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_carryover_event WHERE operation='reclaim'").fetchone()[0] == 0


def test_hook_failure_rolls_back_real_reschedule_and_can_retry(allocated, monkeypatch):
    carry, repo, receipt = allocated
    db, service, _, _ = carry
    item = school(receipt)
    original = LearningInputCarryoverService._save
    def fail(*args, **kwargs):
        raise RuntimeError('injected receipt failure')
    monkeypatch.setattr(LearningInputCarryoverService, '_save', fail)
    with pytest.raises(RuntimeError):
        repo.reschedule_plan_item(item['plan_item_id'], '延期', 'retry')
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()[0] == 'pending'
        assert not c.execute("SELECT 1 FROM plan_item_event WHERE client_action_id='retry'").fetchone()
    monkeypatch.setattr(LearningInputCarryoverService, '_save', staticmethod(original))
    assert repo.reschedule_plan_item(item['plan_item_id'], '延期', 'retry')['status'] == 'rescheduled'


@pytest.mark.parametrize('change', ['foreign', 'source', 'item', 'event'])
def test_reclaim_binding_cannot_be_retargeted(allocated, monkeypatch, change):
    carry, repo, receipt = allocated
    original_after = repo._insert_plan_event
    db, service, _, _ = carry
    iid = school(receipt)['plan_item_id']
    def changed(c, item_id, kind, now, payload, key):
        if change == 'source':
            c.execute("UPDATE homework_item SET title='changed' WHERE homework_id='math-a'")
        if change == 'item':
            c.execute('UPDATE plan_item SET expected_minutes=99 WHERE plan_item_id=?', (iid,))
        original_after(c, item_id, kind, now, payload, key)
    monkeypatch.setattr(repo, '_insert_plan_event', changed)
    if change in {'source', 'item'}:
        with pytest.raises(PlanItemStateError):
            repo.reschedule_plan_item(iid, '延期', 'change')
    else:
        repo.reschedule_plan_item(iid, '延期', 'change')
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            event_id = c.execute("SELECT event_id FROM plan_item_event WHERE client_action_id='change'").fetchone()[0]
            with pytest.raises(CarryoverConflict):
                service.reclaim_rescheduled('child-junior-3' if change == 'foreign' else CHILD, iid, event_id if change == 'foreign' else 'wrong-event', 'other', connection=c)
            c.rollback()


def test_next_day_reclaim_is_consumed_once_not_added_back_to_root(allocated, monkeypatch):
    carry, repo, receipt = allocated
    db, service, original_oid, clock = carry
    iid = school(receipt)['plan_item_id']
    repo.reschedule_plan_item(iid, '明天做', 'tomorrow')
    oid = reclaimed(carry, iid)['obligation_id']
    clock[0] += timedelta(days=1)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id='today'")
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('future',?,'active','homework_capture',?,?,'future')""", (CHILD, clock[0].isoformat(), clock[0].isoformat()))
        c.execute("INSERT INTO session_planning_context(session_id,energy_level,fixed_commitment_minutes,tomorrow_event_minutes,updated_at) VALUES ('future','medium',0,0,?)", (clock[0].isoformat(),))
        c.commit()
        assert not any(item['source_plan_item_id'] == iid for item in repo._deferred_task_candidates(c, CHILD, 'future'))
    args = grant(carry, obligation_id=oid, target_session_id='future')
    accepted = service.review_unplaced(**args)
    assert accepted['remaining_minutes'] == 15 and accepted['allocated_minutes'] == 0
    assert LearningInputCarryoverService(db).review_unplaced(**args) == accepted
    projected = service.list_unplaced(CHILD, 'future')['items']
    assert next(item for item in projected if item['obligation_id'] == original_oid)['remaining_minutes'] == 0
    assert len([item for item in projected if item['remaining_minutes']]) == 1


@pytest.mark.parametrize('minutes', [0, -1, True])
def test_scope_cannot_call_zero_or_invalid_estimate_completed(allocated, monkeypatch, minutes):
    carry, repo, receipt = allocated
    _, service, _, clock = carry
    iid = school(receipt)['plan_item_id']
    repo.start_plan_item(iid, 'start')
    clock[0] += timedelta(minutes=4)
    repo.pause_plan_item(iid, '暂停', 'pause')
    repo.reschedule_plan_item(iid, '延期', 'defer')
    view = reclaimed(carry, iid)
    with pytest.raises(CarryoverConflict, match='NOT_GROUNDED'):
        service.review_reclaimed_scope(**scope_command(carry, view['obligation_id'], minutes=minutes))
    assert reclaimed(carry, iid)['remaining_minutes'] == 0


@pytest.mark.parametrize('spent,estimate', [(15, 5), (5, 20)])
def test_new_work_estimate_can_exceed_old_budget_without_refunding_spent_time(allocated, spent, estimate):
    carry, repo, receipt = allocated
    db, service, _, clock = carry
    iid = school(receipt)['plan_item_id']
    repo.start_plan_item(iid, 'start')
    clock[0] += timedelta(minutes=spent)
    repo.pause_plan_item(iid, 'pause', 'unfinished')
    repo.reschedule_plan_item(iid, 'still unfinished', 'defer')
    view = reclaimed(carry, iid)
    assert view['remaining_minutes'] == 0
    assert view['scope_review']['maximum_remaining_minutes'] == max(0, 15 - spent)
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=30 WHERE child_id=?', (CHILD,))
        before = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (iid,)).fetchone())
        evidence_count = c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
        c.commit()
    scope = service.review_reclaimed_scope(**scope_command(carry, view['obligation_id'], minutes=estimate))
    assert scope['remaining_minutes'] == estimate and scope['allocated_minutes'] == 0
    result = service.review_unplaced(**grant(carry, obligation_id=view['obligation_id']))
    budget = result['budget']
    assert budget['actual_spent_seconds'] == spent * 60
    assert budget['effective_daily_total_minutes_cap'] == 30
    assert result['remaining_minutes'] + result['allocated_minutes'] == estimate
    assert result['allocated_minutes'] * 60 <= max(0, 30 * 60 - spent * 60 - budget['protected_remaining_seconds'])
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (iid,)).fetchone()) == before
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == evidence_count


@pytest.mark.parametrize('quote', ['后面三题没写，大概还要五分钟', '还剩两道题，需要五分钟'])
def test_spoken_new_scope_estimate_is_not_limited_by_exhausted_old_budget(allocated, quote):
    carry, repo, receipt = allocated
    _, _, _, clock = carry
    iid = school(receipt)['plan_item_id']
    repo.start_plan_item(iid, 'start')
    clock[0] += timedelta(minutes=15)
    repo.pause_plan_item(iid, 'pause', 'unfinished')
    repo.reschedule_plan_item(iid, 'unfinished', 'defer')
    candidate = reclaimed(carry, iid)
    snapshot = {'candidates': [candidate], 'dialogue': {'focus': candidate}}
    resolved = AgentRuntimeStore.resolve_unplaced_voice(snapshot, quote)
    assert resolved['arguments']['request_quote'] == quote
    assert resolved['arguments']['remaining_minutes'] == 5
    assert resolved['arguments']['remaining_scope_quote'] in quote
    assert resolved['binding'] == candidate['binding']


def test_unstarted_sibling_does_not_assume_already_done_content(allocated, monkeypatch):
    carry, repo, receipt = allocated
    _, _, _, clock = carry
    tasks = [item for item in receipt['newitems'] if item['source_type'] == 'school_homework']
    repo.start_plan_item(tasks[0]['plan_item_id'], 'start')
    clock[0] += timedelta(minutes=1)
    repo.pause_plan_item(tasks[0]['plan_item_id'], '暂停', 'pause')
    repo.reschedule_plan_item(tasks[1]['plan_item_id'], '延期第二段', 'defer-second')
    view = reclaimed(carry, tasks[1]['plan_item_id'])
    assert view['scope_review_required'] and view['remaining_minutes'] == 0


def test_original_replan_allocation_also_loses_restart_permission_after_reclaim(schedule):
    from home_study_agent.learning_input_carryover import LearningInputCarryoverService
    source, replanner, clock = schedule
    db, _, _ = source
    event = revised(schedule)
    receipt = replanner.execute_replan(CHILD, event, 'replan')
    svc = LearningInputCarryoverService(db)
    svc.initialize()
    iid = school(receipt)['plan_item_id']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        binding = svc.prepare_reschedule(CHILD, iid, connection=c)
        c.execute("UPDATE plan_item SET status='rescheduled' WHERE plan_item_id=?", (iid,))
        StudyRepository._insert_plan_event(c, iid, 'rescheduled', clock[0].isoformat(), {'reason': '延期', 'carryover_reclaim_binding': binding}, 'defer-original')
        eid = c.execute("SELECT event_id FROM plan_item_event WHERE client_action_id='defer-original'").fetchone()[0]
        result = svc.reclaim_rescheduled(CHILD, iid, eid, 'reclaim-original', connection=c)
        assert result['reclaimed_minutes'] == 15
        c.commit()
    assert not replanner.inspect_item(CHILD, iid)['eligible']


def execute_scope(service, turn):
    with service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('''SELECT p.plan_id,p.context_manifest_json,s.step_id,s.idempotency_key,s.arguments_json
            FROM agent_plan p JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0''', (turn['agent_turn_id'],)).fetchone()
        c.execute("UPDATE agent_plan SET status='running' WHERE plan_id=?", (row['plan_id'],))
        c.execute("UPDATE agent_step SET status='running' WHERE step_id=?", (row['step_id'],))
        binding = json.loads(row['context_manifest_json'])['learning_input_unplaced_binding']
        args = json.loads(row['arguments_json'])
        result = service.review_reclaimed_scope(CHILD, binding['obligation_id'], 'today', args['operation'], binding['revision'], binding['source_version'], row['idempotency_key'],
            expected_context_version=binding['context_version'], authorized_turn_id=turn['agent_turn_id'],
            request_quote=args['request_quote'], remaining_minutes=args.get('remaining_minutes'), remaining_scope_quote=args.get('remaining_scope_quote'), connection=c)
        c.execute("UPDATE agent_plan SET status='completed' WHERE plan_id=?", (row['plan_id'],))
        c.execute("UPDATE agent_step SET status='completed',result_json=? WHERE step_id=?", (json.dumps({'capability': 'learning_inputs.review_reclaimed_scope', 'response_text': '剩余范围已保存，是否今天安排？', 'result': result}), row['step_id']))
        c.commit()
        return result


def test_voice_scope_confirmation_then_explicit_real_allocation(allocated, monkeypatch, tmp_path):
    carry, repo, receipt = allocated
    db, svc, _, clock = carry
    iid = school(receipt)['plan_item_id']
    repo.start_plan_item(iid, 'start')
    clock[0] += timedelta(minutes=4)
    repo.pause_plan_item(iid, '暂停', 'pause')
    repo.reschedule_plan_item(iid, '需要确认剩余', 'defer')
    agent, gateway = orchestrator_for(db, tmp_path, {})
    question = say(agent, '昨天没排完的作业有哪些', 'ask')
    assert '具体剩哪些' in question['response_text']
    turn = say(agent, '还剩第3到5题预计五分钟', 'scope')
    assert turn['action']['type'] == 'review_learning_input_reclaimed_scope'
    reviewed = execute_scope(svc, turn)
    assert reviewed['remaining_minutes'] == 5 and reviewed['allocated_minutes'] == 0
    restarted, other_gateway = orchestrator_for(db, tmp_path, {})
    next_turn = say(restarted, '今天继续', 'continue')
    assert next_turn['action']['type'] == 'review_learning_input_unplaced'
    placed = FakeExecutor(svc).execute(next_turn)
    assert placed['allocated_minutes'] == 5 and '第3到5题' in school(placed)['title']
    assert not gateway.requests and not other_gateway.requests


def test_nested_reclaim_keeps_scope_and_parent_withdrawal_invalidates_descendants(allocated, monkeypatch):
    carry, repo, receipt = allocated
    _, svc, _, clock = carry
    first = school(receipt)['plan_item_id']
    repo.start_plan_item(first, 'start')
    clock[0] += timedelta(minutes=2)
    repo.pause_plan_item(first, '暂停', 'pause')
    repo.reschedule_plan_item(first, '延期', 'first')
    oid = reclaimed(carry, first)['obligation_id']
    svc.review_reclaimed_scope(**scope_command(carry, oid))
    next_work = school(svc.review_unplaced(**grant(carry, obligation_id=oid)))
    repo.reschedule_plan_item(next_work['plan_item_id'], '还未开始，明天做', 'second')
    child = reclaimed(carry, next_work['plan_item_id'])
    assert child['remaining_minutes'] == 5
    last = school(svc.review_unplaced(**grant(carry, obligation_id=child['obligation_id'])))
    assert '第3到5题' in last['title']
    svc.review_reclaimed_scope(**scope_command(carry, oid, 'withdraw_scope'))
    assert not svc.inspect_item(CHILD, last['plan_item_id'])['eligible']
    assert reclaimed(carry, next_work['plan_item_id'])['status'] == 'source_invalid'


def test_scope_reapproval_after_allocation_cannot_credit_same_work_twice(allocated, monkeypatch):
    carry, repo, receipt = allocated
    _, svc, _, clock = carry
    iid = school(receipt)['plan_item_id']
    repo.start_plan_item(iid, 'start')
    clock[0] += timedelta(seconds=1)
    repo.pause_plan_item(iid, '暂停', 'pause')
    repo.reschedule_plan_item(iid, '延期', 'defer')
    oid = reclaimed(carry, iid)['obligation_id']
    args = scope_command(carry, oid)
    confirmed = svc.review_reclaimed_scope(**args)
    assert svc.review_reclaimed_scope(**args) == confirmed
    svc.review_unplaced(**grant(carry, obligation_id=oid))
    with pytest.raises(CarryoverConflict, match='ALREADY_CONSUMED'):
        svc.review_reclaimed_scope(**scope_command(carry, oid))


def test_forged_embedded_source_in_reschedule_event_is_rejected(allocated, monkeypatch):
    carry, repo, receipt = allocated
    db, svc, _, _ = carry
    iid = school(receipt)['plan_item_id']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        frozen = svc.prepare_reschedule(CHILD, iid, connection=c)
        frozen['source_binding']['source']['title'] = 'fabricated source'
        c.execute("UPDATE plan_item SET status='rescheduled' WHERE plan_item_id=?", (iid,))
        StudyRepository._insert_plan_event(c, iid, 'rescheduled', '2026-09-08T10:00:00+00:00', {'carryover_reclaim_binding': frozen}, 'forged')
        eid = c.execute("SELECT event_id FROM plan_item_event WHERE client_action_id='forged'").fetchone()[0]
        with pytest.raises(CarryoverConflict, match='FROZEN_SOURCE'):
            svc.reclaim_rescheduled(CHILD, iid, eid, 'reclaim-forged', connection=c)
        assert not c.execute("SELECT 1 FROM learning_input_carryover_event WHERE operation='reclaim'").fetchone()
        c.rollback()


def test_changed_allocated_homework_source_invalidates_reclaimed_balance(allocated, monkeypatch):
    carry, repo, receipt = allocated
    db, svc, _, _ = carry
    item = school(receipt)
    repo.reschedule_plan_item(item['plan_item_id'], '延期', 'defer')
    assert reclaimed(carry, item['plan_item_id'])['source_current']
    with db.connect() as c:
        c.execute('UPDATE homework_item SET range_text=? WHERE homework_id=?', ('已经改为另一范围', item['homework_id']))
        c.commit()
    view = reclaimed(carry, item['plan_item_id'])
    assert view['status'] == 'source_invalid' and not view['allowed_actions']
    with pytest.raises(CarryoverConflict):
        svc.review_unplaced(**grant(carry, obligation_id=view['obligation_id']))


@pytest.mark.parametrize('same_key', [True, False])
def test_concurrent_repository_reschedules_return_segment_only_once(allocated, monkeypatch, same_key):
    carry, repo, receipt = allocated
    db, svc, _, _ = carry
    iid = school(receipt)['plan_item_id']
    def reschedule(key):
        try:
            return repo.reschedule_plan_item(iid, '延期', key)['status']
        except (CarryoverConflict, PlanItemStateError):
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reschedule, ['same', 'same' if same_key else 'other']))
    assert 'rescheduled' in results
    assert results == ['rescheduled', 'rescheduled'] if same_key else results.count('conflict') == 1
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_carryover_event WHERE operation='reclaim'").fetchone()[0] == 1
    assert reclaimed(carry, iid)['remaining_minutes'] == 15
