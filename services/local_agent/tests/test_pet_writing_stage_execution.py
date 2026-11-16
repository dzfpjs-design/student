"""Published writing -> generated daily slot -> real Runtime/executor prompt."""
import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository, PlanInputError
from test_writing_source_publication import prepared, publish, CHILD
from test_goal_task_prompt import command


@pytest.fixture
def stage_case(tmp_path):
    db, supply, spec, _ = prepared(tmp_path)
    published = publish(supply, spec)
    repository = StudyRepository(db)
    sid = repository.start_session(CHILD, 'writing-stage-day')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''', (CHILD,))
        c.execute('''UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id NOT IN (?,?)
            AND knowledge_node_id IN (SELECT knowledge_node_id FROM knowledge_node WHERE track='pet')''',
            (published['diagnostic_item_id'], spec['seed_item_id']))
        c.commit()
    LearningStateService(db).submit_diagnostic(sid, spec['seed_item_id'], 'I do not know how to write this yet.', 'old-reference-attempt')
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.commit()
    workspace = repository.generate_plan(sid, 'writing-stage-plan')
    slots = [item for item in workspace['plan_items'] if item['source_type'] == 'pet_daily' and item['task_type'] == 'writing']
    assert len(slots) == 1, [(row['obligation_type'], row['source_ref'], row['reason'])
        for row in workspace['constraint_decisions'] if row['obligation_type'] == 'pet_daily']
    slot = slots[0]
    assert slot['source_ref'] == published['diagnostic_item_id']
    return db, sid, slot['plan_item_id'], 'node-pet-writing-email', published


def test_generated_writing_slot_starts_exact_prompt_and_recovers_original_receipt(stage_case):
    db, sid, pid, _, published = stage_case
    turn = command(stage_case, child_id=CHILD)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn)
    assert result['result']['status'] == 'active', result
    prompt = result['result']['diagnostic_prompt']['prompt']
    assert prompt['diagnostic_item_id'] == published['diagnostic_item_id']
    assert result['requires_child_reply']
    reopened = Database(db.path)
    assert AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(turn) == result
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id=?", (pid,)).fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE diagnostic_item_id=?',
            (published['diagnostic_item_id'],)).fetchone()[0] == 0
    with pytest.raises(PlanInputError):
        StudyRepository(db).complete_plan_item(pid, 1, 'claim-without-answer')


def test_source_withdrawn_after_plan_does_not_start_a_timer_or_substitute_question(stage_case):
    db, _, pid, _, published = stage_case
    turn = command(stage_case, child_id=CHILD)
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (published['diagnostic_item_id'],))
        c.commit()
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn)
    assert result['result']['status'] == 'blocked'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id=?", (pid,)).fetchone()[0] == 0
