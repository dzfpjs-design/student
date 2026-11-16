"""Local contracts only. Synthetic planner outputs do not prove cloud semantics."""
import json

import pytest

from home_study_agent.agent_runtime import (
    CapabilityRegistry, InvalidAgentPlanError, PLANNER_PROMPT_VERSION,
    action_from_plan, build_planner_messages, parse_agent_plan_output,
)
from test_app import client_for
from test_cloud_first_agent import Planner


CURRENT = '我今天状态很好。'


def messages(state='check_in', utterance=CURRENT, continuation=False):
    context = {'session': {'state': state}, 'private_local': {
        'cloud_transmission_allowed': False, 'secret': 'synthetic-local-only'},
        'recent_dialogue': [{'role': 'assistant', 'content': '今天感觉怎么样？'}]}
    if continuation:
        context['continuation_event'] = {'authorized_actions': []}
    return build_planner_messages(context, utterance, CapabilityRegistry())


def test_real_production_message_exposes_current_self_report_write_contract():
    system, user = messages()
    payload = json.loads(user['content'])
    assert PLANNER_PROMPT_VERSION == 'agent_runtime_plan_prompt_v11_tutor_followup_relation'
    assert '孩子本人对现在或今天精力的明确自述' in system['content']
    assert '必须先选择checkin.update_context持久化' in system['content']
    assert CURRENT in system['content'] and payload['child_utterance'] == CURRENT
    spec = next(c for c in payload['available_capabilities'] if c['name'] == 'checkin.update_context')
    assert '持久化签到时孩子本人明确提供' in spec['description']
    assert spec['argument_choices']['energy_level'] == ['low', 'medium', 'high']
    assert spec['expected_evidence'] == ['planning_context_saved']
    assert 'synthetic-local-only' not in user['content']


def test_phase_and_tool_result_scope_do_not_add_self_report_rule():
    for state, continuation in [('study', False), ('check_in', True)]:
        system, user = messages(state, continuation=continuation)
        assert '必须先选择checkin.update_context持久化' not in system['content']
        assert ('original_child_request' in json.loads(user['content'])) is continuation
    assert 'checkin.update_context' not in [c['name'] for c in json.loads(messages('study')[1]['content'])['available_capabilities']]


def test_semantic_boundaries_are_exposed_without_replacing_user_speech():
    utterances = ['故事里的我今天状态很好。', '弟弟今天状态很好。', '昨天我状态很好。',
                 '如果我今天状态很好就学习。', '我今天很累又精力充沛，说不清。', '你今天状态很好。']
    for utterance in utterances:
        system, user = messages(utterance=utterance)
        assert json.loads(user['content'])['child_utterance'] == utterance
        assert all(boundary in system['content'] for boundary in ('只引用故事', '转述他人', '描述过去', '假设', '相互矛盾', '不更新精力', '不得靠关键词'))


def test_negative_wording_preserves_clear_current_low_and_only_clarifies_unknown_level():
    for utterance in ('我没精神。', '我不太有精神。', '我不是状态很好。'):
        system, user = messages(utterance=utterance)
        assert json.loads(user['content'])['child_utterance'] == utterance
        assert '否定自述' not in system['content']
        assert '否定措辞本身不排除明确低精力自述' in system['content']
        assert '我没精神’或‘我不太有精神’仍为本人当前low' in system['content']
        assert '我不是状态很好’不能自动推断low' in system['content']


@pytest.mark.parametrize('energy', ['low', 'medium', 'high'])
def test_registered_plan_parse_to_existing_action_has_no_extra_fields(energy):
    planner = Planner('checkin.update_context', {'energy_level': energy}, CURRENT, role='answer')
    planner.payload['next_step']['expected_evidence'] = ['planning_context_saved']
    decision = parse_agent_plan_output(json.dumps(planner.payload, ensure_ascii=False), 'check_in', CapabilityRegistry())
    assert action_from_plan(decision, CURRENT, None) == {
        'type': 'update_planning_context', 'capability': 'checkin.update_context',
        'execution_location': 'server', 'energy_level': energy}


@pytest.mark.parametrize('state,args', [('study', {'energy_level': 'high'}),
    ('check_in', {'energy_level': 'excellent'}), ('check_in', {'energy_level': 'high', 'session_id': 'other'})])
def test_existing_state_enum_and_reserved_field_rejections_remain(state, args):
    planner = Planner('checkin.update_context', args, CURRENT, role='answer')
    with pytest.raises(InvalidAgentPlanError):
        parse_agent_plan_output(json.dumps(planner.payload, ensure_ascii=False), state, CapabilityRegistry())


def test_real_cloud_route_receives_new_prompt_and_executes_registered_write(tmp_path):
    planner = Planner('checkin.update_context', {'energy_level': 'high'}, CURRENT, role='answer')
    planner.payload['next_step']['expected_evidence'] = ['planning_context_saved']
    with client_for(tmp_path, model_gateway=planner, agent_runtime_enabled=True) as client:
        session = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'energy-session'}).json()
        sid = session['session_id']
        result = client.post('/v1/sessions/' + sid + '/agent-turn', json={
            'transcript': CURRENT, 'client_action_id': 'energy-turn', 'cloud_text_allowed': True})
        assert result.status_code == 200
        turn = result.json()
        assert turn['action']['source'] == 'agent_runtime_planner'
        assert turn['action']['type'] == 'update_planning_context'
        assert len(planner.requests) == 1 and planner.requests[0].request_type == 'agent_plan'
        assert planner.requests[0].prompt_version == PLANNER_PROMPT_VERSION
        assert '必须先选择checkin.update_context持久化' in planner.requests[0].messages[0]['content']
        executed = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
        assert executed.status_code == 200
        database = client.app.state.agent_orchestrator.database
        with database.connect() as connection:
            assert connection.execute('SELECT energy_level FROM session_planning_context WHERE session_id=?', (sid,)).fetchone()['energy_level'] == 'high'
            step = connection.execute('SELECT s.capability_name,s.status FROM agent_step s JOIN agent_plan p ON p.plan_id=s.plan_id WHERE p.agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
            assert tuple(step) == ('checkin.update_context', 'completed')


def test_chat_still_has_no_keyword_override_and_model_semantics_remain_unproven(tmp_path):
    # Even the exact failed phrase is not turned into a write locally. A wrong cloud
    # selection can still fail the unchanged target-runtime gate after this prompt fix.
    planner = Planner('conversation.respond', {'response': '说说你想聊什么。'}, CURRENT, role='companionship', objective_type='companion')
    with client_for(tmp_path, model_gateway=planner, agent_runtime_enabled=True) as client:
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'chat-session'}).json()['session_id']
        result = client.post('/v1/sessions/' + sid + '/agent-turn', json={
            'transcript': CURRENT, 'client_action_id': 'chat-turn', 'cloud_text_allowed': True}).json()
        assert result['action']['type'] == 'none'
        assert result['action']['capability'] == 'conversation.respond'
        assert result['action']['source'] == 'agent_runtime_planner'
        with client.app.state.agent_orchestrator.database.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM session_planning_context WHERE session_id=?', (sid,)).fetchone()[0] == 0
