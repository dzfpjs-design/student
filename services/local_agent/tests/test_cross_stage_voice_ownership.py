"""A saved exercise owns answers, not every later child command."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import CapabilityRegistry, parse_agent_plan_output
from home_study_agent.database import Database
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.repository import StudyRepository
from home_study_agent.pet_vocabulary import PetVocabularyService
from home_study_agent.app import create_app
from test_agent_continuation import orchestrator_for as bare_orchestrator, setup as continuation_setup
from test_exam_simulation import _database
from test_pet_vocabulary_prompt import flow, CHILD


def orchestrator_for(db, tmp_path, output):
    agent, gateway = bare_orchestrator(db, tmp_path, output)
    agent.pet_vocabulary = PetVocabularyService(db)
    agent.exam_simulation = ExamSimulationService(db)
    return agent, gateway


def interpret(agent, sid, text, key):
    return asyncio.run(agent.interpret(sid, text, key, False))


def execute(agent, db, turn):
    receipt = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id'])
    agent.complete_turn(turn['agent_turn_id'], receipt['response_text'], 'completed', None, receipt)
    return receipt


@pytest.mark.parametrize('text,action', [('今天计划做到哪了', 'describe_plan'), ('打开错题本', 'open_mistake_book')])
def test_exam_retains_pending_question_when_registered_command_interrupts(tmp_path, text, action):
    db, sid = _database(tmp_path / 'exam-commands.db')
    service = ExamSimulationService(db)
    before = service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'start')
    binding = service.arrival_binding(sid)
    agent, gateway = orchestrator_for(db, tmp_path, {})
    command = interpret(agent, sid, text, 'command')
    assert command['action']['type'] == action
    assert execute(agent, db, command)['capability'] == {'describe_plan': 'planning.describe', 'open_mistake_book': 'mistake.open'}[action]
    assert service.arrival_binding(sid) == binding
    assert service.active_for_session(sid)['answered_items'] == before['answered_items'] == 0
    restored, _ = orchestrator_for(Database(db.path), tmp_path, {})
    answer = interpret(restored, sid, '我选A', 'actual-answer')
    result = execute(restored, db, answer)
    assert result['capability'] == 'exam.submit'
    assert result['result']['exam']['answered_items'] == 1
    assert not gateway.requests


@pytest.mark.parametrize('text,action', [('今天计划做到哪了', 'describe_plan'), ('打开错题本', 'open_mistake_book')])
def test_vocabulary_retains_exact_prompt_across_command_and_restart(flow, tmp_path, text, action):
    db, service, sid = flow
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    original = service.present_next(sid)
    agent, gateway = orchestrator_for(db, tmp_path, {})
    command = interpret(agent, sid, text, 'command')
    assert command['action']['type'] == action
    assert execute(agent, db, command)['capability'] == {'describe_plan': 'planning.describe', 'open_mistake_book': 'mistake.open'}[action]
    assert service.active_prompt(sid) == original
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM pet_vocabulary_attempt').fetchone()[0] == 0
    restored, _ = orchestrator_for(Database(db.path), tmp_path, {})
    answer = interpret(restored, sid, 'unrelated', 'actual-answer')
    assert answer['action']['type'] == 'submit_pet_vocabulary_answer'
    action = answer['action']
    assert action['vocabulary_prompt_id'] == original['prompt_session_id']
    assert action['vocabulary_card_version'] == original['card_version']
    with TestClient(create_app(restored.settings, model_gateway=gateway)) as client:
        result = client.post(f'/v1/sessions/{sid}/pet-vocabulary/prompt/answer', json={
            'prompt_session_id': action['vocabulary_prompt_id'], 'card_version': action['vocabulary_card_version'],
            'answer': action['answer'], 'client_action_id': 'actual-answer-receipt',
        })
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'answered'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM pet_vocabulary_attempt WHERE child_id=?', (CHILD,)).fetchone()[0] == 1
    assert not gateway.requests


@pytest.mark.parametrize('text,action', [('今天计划做到哪了', 'describe_plan'), ('打开错题本', 'open_mistake_book')])
def test_companion_does_not_erase_registered_command_or_fake_activity_end(tmp_path, text, action):
    db, sid = _database(tmp_path / 'activity-commands.db')
    agent, gateway = orchestrator_for(db, tmp_path, {})
    agent.agent_runtime.start_companion_activity(sid, 'interactive_story', 'synthetic local story', 'start')
    before = agent.agent_runtime.active_companion_activity(sid)
    command = interpret(agent, sid, text, 'command')
    assert command['action']['type'] == action
    assert execute(agent, db, command)['capability'] == {'describe_plan': 'planning.describe', 'open_mistake_book': 'mistake.open'}[action]
    after = agent.agent_runtime.active_companion_activity(sid)
    assert after['activity_id'] == before['activity_id'] and after['status'] == 'active'
    assert not gateway.requests


@pytest.mark.parametrize('mode', ['new', 'continue', 'pause', 'cancel'])
def test_semantic_plan_query_cannot_displace_unfinished_learning_goal(tmp_path, mode):
    db, sid = _database(tmp_path / 'goal-query.db')
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    PrivacyLifecycleService(db, tmp_path).initialize()
    with db.connect() as c:
        c.execute("""INSERT INTO agent_objective(objective_id,child_id,session_id,objective_type,
            summary,success_criteria_json,phase,status,created_at,updated_at)
            VALUES('unfinished','child-primary-4',?,'learning','Synthetic unfinished goal',
            '["Explain independently"]','explain','active',datetime('now'),datetime('now'))""", (sid,))
        c.commit()
        original = dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='unfinished'").fetchone())
    quote = '我想知道今天还有哪些安排，好心里有个数'
    payload = {
        'objective': {'mode': mode, 'type': 'execution', 'summary': 'Read the plan',
                      'success_criteria': ['Read current plan'], 'phase': 'execute'},
        'next_step': {'capability': 'planning.describe', 'arguments': {}},
        'spoken_transition': '我看看当前的安排。', 'requires_child_reply': False,
        'after_execution': 'stop', 'confidence': .99,
        'utterance_binding': {'role': 'task_request', 'quote': quote},
    }
    agent, gateway = orchestrator_for(db, tmp_path, payload)
    query = asyncio.run(agent.interpret(sid, quote, 'semantic-query', True))
    assert query['action']['type'] == 'describe_plan'
    receipt = execute(agent, db, query)
    assert receipt['capability'] == 'planning.describe'
    restored, _ = orchestrator_for(Database(db.path), tmp_path, {})
    with db.connect() as c:
        assert dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='unfinished'").fetchone()) == original
        assert c.execute('SELECT COUNT(*) FROM agent_objective').fetchone()[0] == 1
        persisted = c.execute('SELECT objective_id,plan_json FROM agent_plan WHERE agent_turn_id=?',
                              (query['agent_turn_id'],)).fetchone()
        assert persisted['objective_id'] is None
        assert json.loads(persisted['plan_json'])['objective']['mode'] == 'none'
    assert restored.agent_runtime.assemble_context(sid, ())['active_objective']['objective_id'] == 'unfinished'
    replay = asyncio.run(restored.interpret(sid, quote, 'semantic-query', True))
    assert replay['agent_turn_id'] == query['agent_turn_id']
    with db.connect() as c:
        assert dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='unfinished'").fetchone()) == original
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('capability,arguments', [('planning.describe', {}), ('device.status', {'device': 'all'})])
def test_direct_query_persistence_cannot_cancel_another_execution_goal(tmp_path, capability, arguments):
    db, sid, runtime = continuation_setup(tmp_path)
    decision = parse_agent_plan_output(json.dumps({
        'objective': {'mode': 'cancel', 'phase': 'execute'},
        'next_step': {'capability': capability, 'arguments': arguments},
        'spoken_transition': 'Read current state.', 'requires_child_reply': False, 'confidence': 1,
    }), 'check_in', CapabilityRegistry())
    with db.connect() as c:
        original = dict(c.execute('SELECT * FROM agent_objective').fetchone())
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,orchestrator_version,created_at,client_action_id)
            VALUES('query','child-runtime',?,'Read current state','query','Read current state',
                   '{}','test',datetime('now'),'query-key')""", (sid,))
        runtime.persist_plan(c, child_id='child-runtime', session_id=sid, agent_turn_id='query',
                             client_action_id='query-key', decision=decision, context_manifest={})
        c.commit()
        assert dict(c.execute('SELECT * FROM agent_objective').fetchone()) == original
        assert c.execute("SELECT objective_id FROM agent_plan WHERE agent_turn_id='query'").fetchone()[0] is None


def test_local_conflicting_questions_do_not_send_answer_or_source_to_planner(flow, tmp_path):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.learning_state import LearningStateService

    db, vocabulary, sid = flow
    LearningStateService(db).seed_catalog()
    card = vocabulary.present_next(sid)
    prompts = DiagnosticPromptService(db)
    prompt = prompts.prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1',
                             client_action_id='conflicting-local-question')['prompt']
    agent, gateway = orchestrator_for(db, tmp_path, {})
    heard = asyncio.run(agent.interpret(sid, 'unrelated', 'conflicting-answer', True))
    assert heard['action']['type'] == 'none'
    assert not gateway.requests
    assert vocabulary.active_prompt(sid) == card
    assert prompts.read(sid)['prompt'] == prompt
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM pet_vocabulary_attempt').fetchone()[0] == 0
