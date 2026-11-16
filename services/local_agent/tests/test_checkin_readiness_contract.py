"""Prompt contracts and synthetic planner/service execution; no cloud-quality claim."""
import json

import pytest

from home_study_agent.agent_runtime import (
    CapabilityRegistry, InvalidAgentPlanError, action_from_plan,
    build_planner_messages, parse_agent_plan_output,
)
from test_app import client_for
from test_cloud_first_agent import Planner


ACTUAL_F55_UTTERANCE = '准备好了。'


def messages(state='check_in', utterance=ACTUAL_F55_UTTERANCE, continuation=False):
    context = {'session': {'state': state}, 'planning_context': {'energy_level': 'high'}}
    if continuation:
        context['continuation_event'] = {'authorized_actions': []}
    return build_planner_messages(context, utterance, CapabilityRegistry())


def test_actual_failed_utterance_has_explicit_current_stage_completion_contract():
    system, user = messages()
    assert '明确表示本人现在已准备好结束签到' in system['content']
    assert 'session.transition' in system['content']
    assert 'arguments.target=classroom_recall' in system['content']
    assert 'session_transition' in system['content']
    assert 'after_execution=stop' in system['content']
    assert '不能改成planning.generate' in system['content']
    assert json.loads(user['content'])['child_utterance'] == ACTUAL_F55_UTTERANCE


@pytest.mark.parametrize('state,continuation', [
    ('study', False), ('completed', False), ('classroom_recall', False), ('check_in', True),
])
def test_readiness_contract_is_current_checkin_only(state, continuation):
    system, _ = messages(state, continuation=continuation)
    assert '明确表示本人现在已准备好结束签到' not in system['content']


def test_contract_exposes_semantic_boundaries_and_preserves_energy_first():
    system, _ = messages()
    for boundary in ('未准备好', '先别开始', '取消', '转述他人', '故事', '过去', '条件假设',
                     '询问你是否准备好', '不能从已有high精力推断', '不能靠关键词',
                     '本轮同时提供新的精力或固定安排', '先选择checkin.update_context保存'):
        assert boundary in system['content']
    assert '必须先选择checkin.update_context持久化' in system['content']


def test_registered_transition_description_includes_checkin_without_new_capability():
    _, user = messages()
    capability = next(c for c in json.loads(user['content'])['available_capabilities']
                      if c['name'] == 'session.transition')
    assert '签到结束进入课堂回想' in capability['description']
    assert capability['expected_evidence'] == ['session_transition']


def planned(capability='session.transition', arguments=None, utterance=ACTUAL_F55_UTTERANCE):
    gateway = Planner(capability, arguments if arguments is not None else {'target': 'classroom_recall'},
                      utterance, role='acknowledgement', objective_type='companion' if capability == 'conversation.respond' else 'execution')
    gateway.payload['next_step']['expected_evidence'] = ['session_transition'] if capability == 'session.transition' else []
    return gateway


def test_grounded_planner_transition_persists_reopens_replays_and_isolates_child(tmp_path):
    gateway = planned()
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'ready-session'}).json()['session_id']
        payload = {'transcript': ACTUAL_F55_UTTERANCE, 'client_action_id': 'ready-turn', 'cloud_text_allowed': True}
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json=payload)
        assert response.status_code == 200
        turn = response.json()
        assert turn['action']['source'] == 'agent_runtime_planner'
        assert turn['action']['capability'] == 'session.transition'
        assert turn['action']['target'] == 'classroom_recall'
        assert '明确表示本人现在已准备好结束签到' in gateway.requests[0].messages[0]['content']
        executed = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
        assert executed.status_code == 200, executed.text
        turn_id = turn['agent_turn_id']
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as reopened:
        replay = reopened.post(f'/v1/sessions/{sid}/agent-turn', json=payload)
        assert replay.status_code == 200 and replay.json()['agent_turn_id'] == turn_id
        assert reopened.post('/v1/agent-turns/' + turn_id + '/execute').status_code == 200
        with reopened.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'classroom_recall'
            assert c.execute('SELECT COUNT(*) FROM daily_session WHERE child_id=?', ('child-junior-3',)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM session_transition WHERE session_id=? AND from_state='check_in' AND to_state='classroom_recall'", (sid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM daily_plan').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM plan_item').fetchone()[0] == 0
            row = c.execute('SELECT s.capability_name,s.status FROM agent_step s JOIN agent_plan p ON p.plan_id=s.plan_id WHERE p.agent_turn_id=?', (turn_id,)).fetchone()
            assert tuple(row) == ('session.transition', 'completed')
        assert len(gateway.requests) == 1


@pytest.mark.parametrize('utterance', [
    ACTUAL_F55_UTTERANCE, '还没准备好。', '准备好了但先别开始。', '取消开始。', '弟弟准备好了。',
    '故事里的孩子准备好了。', '昨天我准备好了。', '如果准备好了就开始。', '你准备好了吗？',
])
def test_wrong_or_negative_chat_plan_is_never_locally_overridden(tmp_path, utterance):
    # These supplied decisions exercise routing, not model understanding.
    gateway = planned('conversation.respond', {'response': '我听着，请说说现在想怎么安排。'}, utterance)
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'chat-session'}).json()['session_id']
        result = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': utterance, 'client_action_id': 'chat-turn', 'cloud_text_allowed': True}).json()
        assert result['action']['capability'] == 'conversation.respond'
        assert result['action']['source'] == 'agent_runtime_planner'
        assert client.post('/v1/agent-turns/' + result['agent_turn_id'] + '/execute').status_code == 200
        with client.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'check_in'
            assert c.execute("SELECT COUNT(*) FROM session_transition WHERE from_state='check_in'").fetchone()[0] == 0
        assert len(gateway.requests) == 1


@pytest.mark.parametrize('arguments', [
    {'target': 'classroom_recall', 'session_id': 'other'},
    {'target': 'classroom_recall', 'child_id': 'child-junior-3'},
])
def test_reserved_cross_scope_arguments_still_rejected(arguments):
    gateway = planned(arguments=arguments)
    with pytest.raises(InvalidAgentPlanError):
        parse_agent_plan_output(json.dumps(gateway.payload, ensure_ascii=False), 'check_in', CapabilityRegistry())


def test_existing_state_machine_rejects_plan_skip_and_stale_stage(tmp_path):
    from home_study_agent.repository import InvalidTransitionError, StaleSessionStateError
    gateway = planned()
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'guard-session'}).json()['session_id']
        repository = client.app.state.agent_orchestrator.repository
        with pytest.raises(InvalidTransitionError):
            repository.transition(sid, 'check_in', 'study', 'agent_runtime', 'skip')
        with pytest.raises(StaleSessionStateError):
            repository.transition(sid, 'classroom_recall', 'homework_capture', 'agent_runtime', 'stale')
        with repository.database.connect() as c:
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'check_in'
            assert c.execute('SELECT COUNT(*) FROM daily_plan').fetchone()[0] == 0
