"""Actual executor receipts remain authoritative across turn completion/replay."""
import asyncio
import json
import sqlite3

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_orchestrator import InvalidAgentTurnOutcomeError
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.repository import StudyRepository
from test_agent_continuation import orchestrator_for
from test_learning_input_host import execute
from test_learning_input_replan import schedule
from test_learning_input_revision_voice import inputs, flow, CHILD


def confirmation(schedule, tmp_path):
    source = schedule[0]
    execute(source, '把口算改成30分钟', 'learning_inputs.propose_revision', {
        'kind': 'homework', 'operation': 'update', 'target_quote': '口算', 'request_quote': '把口算改成30分钟',
        'changes': {'expected_minutes': 30}, 'change_quotes': {'expected_minutes': '30分钟'}})
    orchestrator, gateway = orchestrator_for(source[0], tmp_path, {})
    turn = asyncio.run(orchestrator.interpret(source[1], '确认', 'confirm-with-replan', False))
    assert gateway.requests == []
    return source, orchestrator, turn


@pytest.mark.parametrize('late_status', ['completed', 'failed'])
def test_late_revision_only_outcome_cannot_replace_committed_replan(schedule, tmp_path, late_status):
    source, orchestrator, turn = confirmation(schedule, tmp_path)
    db, _, revisions = source
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id'])
    assert result['result']['replan']['receipt']['allocated_minutes'] == 30
    old = {'capability': result['capability'], 'result': revisions.execute(turn['agent_turn_id'])}
    finished = orchestrator.complete_turn(turn['agent_turn_id'], '旧缓存：仍需重新安排。', late_status, None, old)
    assert finished['response_text'] == result['response_text']
    assert finished['tool_status'] == 'completed'
    with db.connect() as c:
        saved = c.execute('SELECT result_json FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)', (turn['agent_turn_id'],)).fetchone()[0]
        assert json.loads(saved) == result
    replayed = asyncio.run(orchestrator.interpret(source[1], '确认', 'confirm-with-replan', False))
    assert replayed['response_text'] == result['response_text']


def test_unexecuted_server_revision_cannot_be_completed_by_client_outcome(schedule, tmp_path):
    source, orchestrator, turn = confirmation(schedule, tmp_path)
    with pytest.raises(InvalidAgentTurnOutcomeError):
        orchestrator.complete_turn(turn['agent_turn_id'], '已经修改并排好了。', 'completed', None,
            {'capability': 'learning_inputs.confirm_revision', 'result': {'status': 'applied'}})
    with source[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 0
        assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0] == 'pending'


def test_executor_commit_before_turn_completion_replays_actual_speech_without_dispatch(schedule, tmp_path):
    source, _, turn = confirmation(schedule, tmp_path)
    db, sid, _ = source
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id'])
    restarted, gateway = orchestrator_for(db, tmp_path, {})
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert restarted._get(c, turn['agent_turn_id'])['response_text'] == result['response_text']
    replayed = asyncio.run(restarted.interpret(sid, '确认', 'confirm-with-replan', False))
    assert replayed['response_text'] == result['response_text']
    assert replayed['tool_status'] == 'pending'
    assert gateway.requests == []
    assert AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id']) == result
    finished = restarted.complete_turn(turn['agent_turn_id'], '旧确认缓存', 'completed', None)
    assert finished['response_text'] == result['response_text']


def test_failed_replan_replay_stays_retryable_until_actual_executor_success(schedule, tmp_path):
    source, orchestrator, turn = confirmation(schedule, tmp_path)
    db, sid, _ = source
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_replan BEFORE INSERT ON learning_input_replan_receipt BEGIN SELECT RAISE(ABORT,'storage-failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id'])
    orchestrator.complete_turn(turn['agent_turn_id'], '本次补排没有确认完成。', 'failed', 'storage-failure')
    restarted, _ = orchestrator_for(db, tmp_path, {})
    retry = asyncio.run(restarted.interpret(sid, '确认', 'confirm-with-replan', False))
    assert retry['tool_status'] == 'failed'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 0
        c.execute('DROP TRIGGER fail_replan')
        c.commit()
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id'])
    finished = restarted.complete_turn(turn['agent_turn_id'], retry['response_text'], 'completed', None, result)
    assert finished['response_text'] == result['response_text']
    assert result['result']['replan']['receipt']['allocated_minutes'] == 30


def test_runtime_direct_completion_cannot_erase_server_receipt(schedule, tmp_path):
    source, _, turn = confirmation(schedule, tmp_path)
    db, _, _ = source
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id'])
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        AgentRuntimeStore(db).complete_turn_execution(c, turn['agent_turn_id'], True, None, {'result': {}})
        saved = c.execute('SELECT result_json FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)', (turn['agent_turn_id'],)).fetchone()[0]
        assert json.loads(saved) == result


@pytest.mark.parametrize('state', ['committed', 'unexecuted', 'cancelled'])
def test_actual_outcome_http_cannot_replace_or_forge_server_replan_receipt(schedule, tmp_path, state):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_agent_continuation import Gateway
    db = schedule[0][0]
    gateway = Gateway({})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        source, _, turn = confirmation(schedule, tmp_path)
        actual = None
        if state != 'unexecuted':
            actual = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id'])
        if state == 'cancelled':
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
                c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn['agent_turn_id'],))
                c.commit()
        response = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            'final_response': '旧缓存：已修改，仍需排程。', 'tool_status': 'completed', 'result': {'result': {}}})
        assert response.status_code == (200 if state == 'committed' else 422), response.text
        if actual and state == 'committed':
            assert response.json()['response_text'] == actual['response_text']
            replay = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert replay.status_code == 200, replay.text
            assert replay.json()['execution'] == actual
            assert replay.json()['turn']['response_text'] == actual['response_text']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == (1 if actual else 0)
            saved = c.execute('SELECT status,result_json FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)', (turn['agent_turn_id'],)).fetchone()
            if actual:
                assert json.loads(saved['result_json']) == actual
            if state == 'cancelled':
                assert saved['status'] == 'cancelled'
        assert gateway.requests == []


def test_same_key_and_runtime_recovery_http_keep_actual_replan_after_app_restart(schedule, tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_agent_continuation import Gateway
    db = schedule[0][0]
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        source, _, turn = confirmation(schedule, tmp_path)
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        actual = response.json()['execution']
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = client.post(f'/v1/sessions/{source[1]}/agent-turn', json={
            'transcript': '确认', 'client_action_id': 'confirm-with-replan', 'cloud_text_allowed': False})
        assert replay.status_code == 200, replay.text
        assert replay.json()['response_text'] == actual['response_text']
        assert replay.json()['tool_status'] == 'completed'
        status = client.get(f'/v1/sessions/{source[1]}/agent-runtime')
        assert status.status_code == 200, status.text
        assert status.json()['current_step']['status'] == 'completed'
        with db.connect() as c:
            projected = source[2].inspect_replan_obligation(CHILD, actual['result']['event_id'], connection=c)
            assert projected['status'] == 'recorded'
            assert projected['receipt'] == actual['result']['replan']['receipt']
            assert json.loads(c.execute('SELECT result_json FROM agent_step WHERE step_id=?', (status.json()['current_step']['step_id'],)).fetchone()[0]) == actual
            assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 1
        assert gateway.requests == []
