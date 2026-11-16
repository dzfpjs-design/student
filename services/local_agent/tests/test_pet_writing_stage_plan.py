"""Stage-bound PET planning on disposable SQLite; minutes are not skill evidence."""
from datetime import datetime, timezone
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.pet_training_plan import allocate_pet_practice
from home_study_agent.stage_execution import resolve_stage_execution
from test_pet_training_plan import setup, CHILD


def writing_only(db):
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id IN (SELECT knowledge_node_id FROM knowledge_node WHERE track='pet' AND code NOT LIKE 'PET.WRITING.%')")
        c.commit()


def allocate(case, minutes=15, cards=30):
    with case[0].connect() as c:
        return allocate_pet_practice(c, CHILD, minutes, cards)


def test_cold_writing_real_stage_binding_keeps_full_estimate_and_vocab(setup):
    writing_only(setup[0])
    result = allocate(setup)
    item = next(i for i in result['items'] if i['task_type'] == 'writing')
    assert item['minutes'] == 12
    assert item['source_ref'] == item['planning_evidence']['diagnostic_item_id']
    evidence = item['planning_evidence']
    assert set(evidence) == {'domain_id','knowledge_node_id','diagnostic_item_id','source_version','review_task_id',
        'not_before','execution_status','stage_constraint','mastery_claim_allowed'}
    assert set(evidence['stage_constraint']) == {'version','current_stage','prerequisite_version'}
    assert evidence['stage_constraint']['current_stage'] == 'baseline'
    assert evidence['execution_status'] == 'ready' and not evidence['mastery_claim_allowed']
    assert sum(i['minutes'] for i in result['items']) <= 15
    assert any(i['task_type'] == 'memorization' for i in result['items'])


@pytest.mark.parametrize('minutes', [0,3,11])
def test_budget_shortage_is_unplaced_not_shortened_writing_or_vocab_completion(setup, minutes):
    writing_only(setup[0])
    result = allocate(setup, minutes)
    assert not any(i['task_type'] == 'writing' for i in result['items'])
    gap = next(i for i in result['unplaced_obligations'] if i['reason_code'] == 'pet_writing_budget_insufficient')
    assert gap['minutes'] == 12
    assert gap['planning_evidence']['execution_status'] == 'ready'
    assert 'writing' not in result['missing_skills']
    assert sum(i['minutes'] for i in result['items']) <= minutes


def test_no_source_has_explicit_gap_and_no_generated_or_scored_work(setup):
    writing_only(setup[0])
    with setup[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id IN (SELECT knowledge_node_id FROM knowledge_node WHERE track='pet')")
        c.commit()
        before = list(c.iterdump())
        c.execute('PRAGMA query_only=ON')
        result = allocate_pet_practice(c, CHILD, 15, 0)
        assert list(c.iterdump()) == before
    assert result['items'] == []
    assert 'writing' in result['missing_skills']
    assert all(i['reason_code'] == 'writing_stage_source_unavailable' for i in result['unplaced_obligations'])


def persist(db, sid, item, *, source_ref=None):
    now = datetime.now(timezone.utc).isoformat()
    source = source_ref or item['source_ref']
    with db.connect() as c:
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,total_minutes,generator,version,status,created_at,client_action_id)
            VALUES('pet-stage-plan',?,?,'2026-09-08',12,'test',1,'active',?,'pet-stage-plan')""", (CHILD,sid,now))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type)
            VALUES('pet-stage-item','pet-stage-plan','英语','Synthetic writing',12,1,'pending',1,'pet_daily',?,'writing')""", (source,))
        c.execute("""INSERT INTO plan_constraint_decision(decision_id,plan_id,obligation_type,source_ref,decision,reason,requested_minutes,allocated_minutes,metadata_json,created_at)
            VALUES('pet-stage-decision','pet-stage-plan','pet_daily',?,'scheduled','Synthetic',12,12,?,?)""",
            (source,json.dumps({'plan_item_id':'pet-stage-item','planning_evidence':item['planning_evidence']}),now))
        c.commit()


def test_plan_execution_consumes_exact_stage_hash_and_survives_restart(setup):
    writing_only(setup[0])
    item = next(i for i in allocate(setup)['items'] if i['task_type'] == 'writing')
    persist(setup[0], setup[2], item)
    with setup[0].connect() as c:
        before = list(c.iterdump())
        result = resolve_stage_execution(c, setup[2], 'pet-stage-item')
        assert result['status'] == 'ready', result
        assert result['source_version'] == item['planning_evidence']['source_version']
        assert result['diagnostic_item_id'] == item['source_ref']
        assert list(c.iterdump()) == before
    with Database(setup[0].path).connect() as c:
        assert resolve_stage_execution(c, setup[2], 'pet-stage-item') == result


def test_pet_source_ref_cannot_disagree_with_valid_stage_evidence(setup):
    writing_only(setup[0])
    item = next(i for i in allocate(setup)['items'] if i['task_type'] == 'writing')
    persist(setup[0], setup[2], item, source_ref='pet-vocabulary-due:5')
    with setup[0].connect() as c:
        result = resolve_stage_execution(c, setup[2], 'pet-stage-item')
    assert result['status'] == 'blocked'
    assert result['reason_code'] == 'stage_pet_source_binding_mismatch'


def test_stage_read_borrowed_transaction_sees_withdrawal_without_committing(setup):
    writing_only(setup[0])
    item = next(i for i in allocate(setup)['items'] if i['task_type'] == 'writing')
    persist(setup[0], setup[2], item)
    with setup[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE diagnostic_item SET status=\'retired\' WHERE diagnostic_item_id=?', (item['source_ref'],))
        changes = c.total_changes
        assert resolve_stage_execution(c, setup[2], 'pet-stage-item')['status'] == 'blocked'
        assert c.in_transaction and c.total_changes == changes
        c.rollback()


def test_allocator_explicit_time_is_read_only_and_wrong_child_has_no_writing_stage(setup):
    with setup[0].connect() as c:
        before = list(c.iterdump())
        now = datetime.now(timezone.utc).isoformat()
        a = allocate_pet_practice(c, CHILD, 15, 30, as_of=now)
        assert allocate_pet_practice(c, CHILD, 15, 30, as_of=now) == a
        assert list(c.iterdump()) == before
        assert allocate_pet_practice(c, 'child-junior-3', 15, 0, as_of=now)['items'] == []
        with pytest.raises(ValueError):
            allocate_pet_practice(c, CHILD, 15, 0, as_of='2026-09-08T00:00:00')
