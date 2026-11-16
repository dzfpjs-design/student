from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore, AgentPlanDecision, ObjectiveDraft, PlannedStep, CapabilityRegistry
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item, CHILD, change


def command(execution, child_id=CHILD):
    db, sid, pid, _, _ = execution
    runtime = AgentRuntimeStore(db)
    key = str(uuid4())
    cap = CapabilityRegistry().require('task.start', 'study')
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.execute("UPDATE plan_item SET status='completed' WHERE plan_item_id!=?", (pid,))
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,'开始下一项','start_task','', '{"type":"start_next_task"}','pending','test',?,?)""",
            (key, child_id, sid, now, key))
        runtime.persist_plan(c, child_id=child_id, session_id=sid, agent_turn_id=key, client_action_id=key,
            context_manifest={}, decision=AgentPlanDecision(ObjectiveDraft('new', 'execution', 'Start task', ('Task started',), 'execution'),
                PlannedStep(cap, {}, 'Begin the scheduled action', cap.expected_evidence), '', False, 1))
        c.commit()
    return key


def test_real_dispatch_uses_exact_roadmap_node_and_saves_prompt_and_timer(execution):
    db, sid, pid, node, exact = execution
    key = command(execution)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(key)
    assert result['result']['status'] == 'active', result
    assert result['requires_child_reply'] is True
    prompt = result['result']['diagnostic_prompt']['prompt']
    assert prompt['diagnostic_item_id'] == exact[0]
    assert prompt['item']['knowledge_node_id'] == node
    with db.connect() as c:
        action = json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (key,)).fetchone()[0])
        assert action['cloud_transmission_allowed'] is False
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
    reopened = Database(db.path)
    assert AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(key) == result


def test_withdrawn_roadmap_source_blocks_without_a_prompt_or_timer(execution):
    db, _, _, _, exact = execution
    key = command(execution)
    change(db, "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (exact[0],))
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(key)
    assert result['result']['status'] == 'blocked'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0


def test_lost_timer_receipt_recovers_original_roadmap_prompt(execution, monkeypatch):
    db, _, _, _, _ = execution
    key = command(execution)
    repository = StudyRepository(db)
    original = repository.start_plan_item
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('lost started receipt')
    monkeypatch.setattr(repository, 'start_plan_item', lost)
    with pytest.raises(RuntimeError):
        AgentCapabilityExecutor(db, repository).execute(key)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(key)
    assert result['result']['status'] == 'active'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1


@pytest.mark.parametrize('objective_status', ['cancelled', 'paused', 'completed'])
def test_started_prompt_rechecks_objective_without_erasing_execution(execution, objective_status):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    db, sid, _, _, _ = execution
    key = command(execution)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(key)
    prompt = result['result']['diagnostic_prompt']['prompt']
    with db.connect() as c:
        before = tuple(c.execute('''SELECT p.status,s.status FROM agent_plan p
            JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=?''', (key,)).fetchone())
        c.execute('''UPDATE agent_objective SET status=? WHERE objective_id=
            (SELECT objective_id FROM agent_plan WHERE agent_turn_id=?)''', (objective_status, key))
        c.commit()
    restored = DiagnosticPromptService(Database(db.path)).read(sid, prompt_id=prompt['prompt_id'])
    if objective_status == 'cancelled':
        assert restored['status'] == 'invalidated'
        assert restored['reason'] == 'scheduled_objective_cancelled'
    else:
        assert restored['status'] == 'waiting_answer'
    with db.connect() as c:
        after = tuple(c.execute('''SELECT p.status,s.status FROM agent_plan p
            JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=?''', (key,)).fetchone())
        assert after == before
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('change_kind', ['cancel', 'source', 'retire_plan'])
def test_prepared_question_cannot_consume_answer_after_start_authority_changes(execution, monkeypatch, change_kind):
    from home_study_agent.task_start_binding import TaskStartBindingError
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    db, sid, pid, _, _ = execution
    key = command(execution)
    repository = StudyRepository(db)
    original = repository.start_plan_item
    def raced(*args, **kwargs):
        with db.connect() as c:
            if change_kind == 'cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (key,))
            elif change_kind == 'source':
                c.execute("UPDATE plan_item SET source_ref='changed' WHERE plan_item_id=?", (pid,))
            else:
                c.execute("UPDATE daily_plan SET status='superseded' WHERE plan_id IN (SELECT plan_id FROM plan_item WHERE plan_item_id=?)", (pid,))
            c.commit()
        return original(*args, **kwargs)
    monkeypatch.setattr(repository, 'start_plan_item', raced)
    with pytest.raises(TaskStartBindingError):
        AgentCapabilityExecutor(db, repository).execute(key)
    with db.connect() as c:
        prompt = dict(c.execute('SELECT * FROM diagnostic_prompt').fetchone())
    result = DiagnosticPromptService(Database(db.path)).answer(sid, prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], answer='90', client_action_id=str(uuid4()))
    assert result['status'] == 'invalidated'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_prompt WHERE status='waiting_answer'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
