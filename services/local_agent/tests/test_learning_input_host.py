"""Versioned input changes through the actual executor and privacy lifecycle."""
import json
from uuid import uuid4

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, AgentPlanDecision, ObjectiveDraft, PlannedStep, CapabilityRegistry
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_learning_input_revision_voice import inputs, flow, CHILD
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_agent_continuation import Gateway


def execute(inputs, utterance, capability, arguments):
    db, sid, service = inputs
    key = str(uuid4())
    arrival = service.capture_arrival(sid, key, utterance)
    runtime = AgentRuntimeStore(db)
    cap = CapabilityRegistry().require(capability, 'study')
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,?,'input_revision','','{}','pending','test',datetime('now'),?)""", (key, CHILD, sid, utterance, key))
        runtime.persist_plan(c, child_id=CHILD, session_id=sid, agent_turn_id=key, client_action_id=key,
            context_manifest={'learning_input_arrival_id': arrival['arrival_id']}, decision=AgentPlanDecision(
                ObjectiveDraft('none', None, None, (), 'execute'),
                PlannedStep(cap, arguments, 'Revise recorded input', cap.expected_evidence), '', False, 1))
        c.commit()
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(key)
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET tool_status='completed',response_text=? WHERE agent_turn_id=?", (result['response_text'], key))
        c.commit()
    return key, result


def proposal(inputs):
    utterance = '把口算的学科改成英语'
    return execute(inputs, utterance, 'learning_inputs.propose_revision', {
        'kind': 'homework', 'operation': 'update', 'target_quote': '口算', 'request_quote': utterance,
        'changes': {'subject': '英语'}, 'change_quotes': {'subject': '英语'}})


def test_actual_executor_readback_confirmation_and_restart(inputs):
    db, _, _ = inputs
    _, offered = proposal(inputs)
    assert offered['requires_child_reply']
    assert offered['result']['cloud_transmission_allowed'] is False
    with db.connect() as c:
        assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == '数学'
    key, applied = execute(inputs, '确认', 'learning_inputs.confirm_revision', {'confirmation_quote': '确认'})
    assert applied['evidence'] == ['input_revision_applied']
    reopened = Database(db.path)
    assert AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(key) == applied
    with reopened.connect() as c:
        assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == '英语'
        assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-b'").fetchone()[0] == '数学'
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_learning_input_export_and_source_scoped_deletion(inputs, tmp_path, scope):
    db, _, _ = inputs
    proposal(inputs)
    execute(inputs, '确认', 'learning_inputs.confirm_revision', {'confirmation_quote': '确认'})
    lifecycle = PrivacyLifecycleService(db, tmp_path)
    lifecycle.initialize()
    with open(lifecycle.export_child(CHILD)['path'], encoding='utf-8') as stream:
        data = json.load(stream)
    assert len(data['learning_input_arrivals']) == 2
    assert len(data['learning_input_proposals']) == 1
    assert len(data['learning_input_revisions']) == 1
    assert len(data['learning_input_commands']) == 2
    assert lifecycle.deletion_preview(CHILD)['learning_input_revisions'] == 6
    result = lifecycle.delete_child_scope(CHILD, scope, '确认删除', str(uuid4()))
    assert result['status'] == 'completed'
    assert lifecycle.deletion_preview(CHILD)['learning_input_revisions'] == 0
    with db.connect() as c:
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_actual_http_cloud_proposal_then_local_natural_confirmation_survives_restart(inputs, tmp_path):
    db, sid, _ = inputs
    words = '把口算的学科改成英语'
    gateway = Gateway({'objective': {'mode': 'none'}, 'utterance_binding': {'role': 'correction', 'quote': words}, 'next_step': {
        'capability': 'learning_inputs.propose_revision', 'arguments': {
            'kind': 'homework', 'operation': 'update', 'target_quote': '口算', 'request_quote': words,
            'changes': {'subject': '英语'}, 'change_quotes': {'subject': '英语'}}},
        'spoken_transition': '我先核对你想改的那一项。', 'requires_child_reply': False, 'confidence': .99})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        proposed = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': words, 'client_action_id': str(uuid4())})
        assert proposed.status_code == 200, proposed.text
        first = proposed.json()
        assert first['action']['type'] == 'propose_learning_input_revision', first
        offered = client.post(f"/v1/agent-turns/{first['agent_turn_id']}/execute")
        assert offered.status_code == 200, offered.text
        with db.connect() as c:
            assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == '数学'
        calls_before = len(gateway.requests)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        confirmation = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '好的，就按你说的改吧', 'client_action_id': str(uuid4())})
        assert confirmation.status_code == 200, confirmation.text
        turn = confirmation.json()
        assert turn['action']['type'] == 'confirm_learning_input_revision', turn
        applied = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert applied.status_code == 200, applied.text
        assert len(gateway.requests) == calls_before
        replay = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert replay.status_code == 200, replay.text
        with db.connect() as c:
            assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == '英语'
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 1
            saved = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0]
            assert json.loads(saved)['cloud_transmission_allowed'] is False
