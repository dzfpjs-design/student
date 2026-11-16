"""Real planner/HTTP/SQLite contracts; synthetic model decisions are not cloud quality."""
import json

import pytest

from home_study_agent.agent_runtime import (
    CapabilityRegistry, InvalidAgentPlanError, build_planner_messages, parse_agent_plan_output,
)
from test_app import client_for
from test_cloud_first_agent import Planner


HOMEWORK = '数学练习是做家长选好的三道原创合成练习。'
VERSION = 'agent_runtime_plan_prompt_v11_tutor_followup_relation'


def plan(capability, arguments, speech):
    gateway = Planner(capability, arguments, speech, role='answer' if capability == 'homework.add' else 'task_request')
    gateway.payload['after_execution'] = 'stop'
    return gateway


@pytest.mark.parametrize('state,target', [
    ('check_in', 'study'), ('classroom_recall', 'study'), ('homework_capture', 'study'),
    ('plan_review', 'reflection'), ('completed', 'classroom_recall'),
])
def test_planner_cannot_accept_an_edge_the_actual_session_will_reject(state, target):
    gateway = plan('session.transition', {'target': target}, '这个计划可以。')
    with pytest.raises(InvalidAgentPlanError, match='SESSION_TRANSITION_TARGET_NOT_AVAILABLE'):
        parse_agent_plan_output(json.dumps(gateway.payload, ensure_ascii=False), state, CapabilityRegistry())


@pytest.mark.parametrize('state,targets', [
    ('check_in', ['classroom_recall']), ('classroom_recall', ['homework_capture']),
    ('homework_capture', ['plan_review']), ('plan_review', ['study']),
    ('study', ['break', 'reflection']), ('break', ['study']),
    ('reflection', ['evolving']), ('evolving', ['completed']), ('completed', []),
])
def test_catalog_and_parser_share_actual_current_phase_edges(state, targets):
    catalog = CapabilityRegistry().catalog(state)
    spec = next(row for row in catalog if row['name'] == 'session.transition')
    assert list(spec['argument_choices']['target']) == targets
    _, user = build_planner_messages({'session': {'state': state}}, '继续下一阶段。', CapabilityRegistry())
    actual_spec = next(row for row in json.loads(user['content'])['available_capabilities']
                       if row['name'] == 'session.transition')
    assert actual_spec['argument_choices']['target'] == targets
    for target in targets:
        gateway = plan('session.transition', {'target': target}, '继续下一阶段。')
        decision = parse_agent_plan_output(json.dumps(gateway.payload, ensure_ascii=False), state, CapabilityRegistry())
        assert decision.step.arguments == {'target': target}


def test_homework_phase_prompt_keeps_contextual_answers_and_explicit_authorization():
    for utterance in [HOMEWORK, '英语练习册两页。', '弟弟今天要做三道题。', '不要保存，只是举例。', '作业说完了，帮我安排计划。']:
        system, user = build_planner_messages({'session': {'state': 'homework_capture'}}, utterance, CapabilityRegistry())
        assert '当前作业采集问题的回答' in system['content']
        assert '不要求孩子重复“作业”' in system['content']
        assert 'homework.add' in system['content'] and 'homework.add_batch' in system['content']
        assert 'planning.generate' in system['content'] and 'plan_review' in system['content']
        assert all(boundary in system['content'] for boundary in ['转述他人', '假设', '不保存', '不编造', '不能靠关键词'])
        assert json.loads(user['content'])['child_utterance'] == utterance


def capture_session(client):
    sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'planning-session'}).json()['session_id']
    for expected, target in [('check_in', 'classroom_recall'), ('classroom_recall', 'homework_capture')]:
        if expected == 'classroom_recall':
            saved = client.post(f'/v1/sessions/{sid}/recalls', json={'subject': '数学', 'summary': '运算顺序', 'client_action_id': 'planning-recall'})
            assert saved.status_code == 201
        response = client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': expected, 'target_state': target,
            'reason': 'synthetic current child authorization', 'client_action_id': 'enter-' + target})
        assert response.status_code == 200
    return sid


def execute(client, sid, gateway, capability, arguments, speech, key):
    gateway.payload = plan(capability, arguments, speech).payload
    result = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': speech, 'client_action_id': key, 'cloud_text_allowed': True})
    assert result.status_code == 200, result.text
    value = result.json()
    assert value['action']['capability'] == capability
    response = client.post('/v1/agent-turns/' + value['agent_turn_id'] + '/execute')
    assert response.status_code == 200, response.text
    return value


def test_real_capture_plan_confirm_replay_and_reopen_keep_durable_phase_facts(tmp_path):
    gateway = plan('homework.add', {'subject': '数学', 'title': HOMEWORK}, HOMEWORK)
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = capture_session(client)
        runtime = client.app.state.agent_orchestrator.agent_runtime
        facts = runtime.assemble_context(sid, ())['daily_planning_state']
        assert facts == {'saved_homework_count': 0, 'homework_declared_empty': False, 'active_plan_count': 0}
        homework = execute(client, sid, gateway, 'homework.add', {'subject': '数学', 'title': HOMEWORK}, HOMEWORK, 'homework-turn')
        facts = runtime.assemble_context(sid, ())['daily_planning_state']
        assert facts == {'saved_homework_count': 1, 'homework_declared_empty': False, 'active_plan_count': 0}
        assert gateway.requests[-1].prompt_version == VERSION
        assert '当前作业采集问题的回答' in gateway.requests[-1].messages[0]['content']
        execute(client, sid, gateway, 'planning.generate', {}, '作业说完了，帮我安排计划。', 'generate-turn')
        assert runtime.assemble_context(sid, ())['session']['state'] == 'plan_review'
        assert runtime.assemble_context(sid, ())['daily_planning_state']['active_plan_count'] == 1
        execute(client, sid, gateway, 'session.transition', {'target': 'study'}, '这个计划可以。', 'confirm-turn')
        before = len(gateway.requests)
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as reopened:
        runtime = reopened.app.state.agent_orchestrator.agent_runtime
        context = runtime.assemble_context(sid, ())
        assert context['session']['state'] == 'study'
        assert context['daily_planning_state'] == {'saved_homework_count': 1, 'homework_declared_empty': False, 'active_plan_count': 1}
        assert reopened.post('/v1/agent-turns/' + homework['agent_turn_id'] + '/execute').status_code == 200
        assert len(gateway.requests) == before
        with reopened.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute('SELECT count(*) FROM homework_item WHERE child_id=?', ('child-primary-4',)).fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM homework_item WHERE child_id=?', ('child-junior-3',)).fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 1


def test_invalid_confirmation_is_rejected_before_a_failed_tool_or_state_write(tmp_path):
    gateway = plan('session.transition', {'target': 'study'}, '这个计划可以。')
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = capture_session(client)
        result = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '这个计划可以。',
            'client_action_id': 'invalid-confirm', 'cloud_text_allowed': True})
        assert result.status_code == 200
        assert result.json()['action']['type'] == 'none'
        assert result.json()['action']['source'] == 'cloud_planner_unavailable'
        with client.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'homework_capture'
            assert c.execute('SELECT count(*) FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM agent_step WHERE status='failed'").fetchone()[0] == 0
            assert c.execute('SELECT error_code FROM model_call WHERE session_id=?', (sid,)).fetchone()[0] == 'MODEL_SESSION_TRANSITION_TARGET_NOT_AVAILABLE'


@pytest.mark.parametrize('add_after_none', [False, True])
def test_no_homework_is_an_explicit_durable_fact_and_new_work_clears_it(tmp_path, add_after_none):
    with client_for(tmp_path) as client:
        sid = capture_session(client)
        runtime = client.app.state.agent_orchestrator.agent_runtime
        premature = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'premature-plan'})
        assert premature.status_code == 422
        assert runtime.assemble_context(sid, ())['daily_planning_state']['active_plan_count'] == 0
        declared = client.post(f'/v1/sessions/{sid}/homework/none', json={'client_action_id': 'explicit-none'})
        assert declared.status_code == 200
        assert runtime.assemble_context(sid, ())['daily_planning_state'] == {
            'saved_homework_count': 0, 'homework_declared_empty': True, 'active_plan_count': 0}
        if add_after_none:
            saved = client.post(f'/v1/sessions/{sid}/homework', json={'subject': '英语', 'title': '练习册两页',
                'expected_minutes': 15, 'client_action_id': 'new-homework'})
            assert saved.status_code == 201
            rejected = client.post(f'/v1/sessions/{sid}/homework/none', json={'client_action_id': 'contradictory-none'})
            assert rejected.status_code == 422
        generated = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'after-declaration-plan'})
        assert generated.status_code == 200
    with client_for(tmp_path) as reopened:
        facts = reopened.app.state.agent_orchestrator.agent_runtime.assemble_context(sid, ())['daily_planning_state']
        assert facts == {'saved_homework_count': int(add_after_none),
            'homework_declared_empty': not add_after_none, 'active_plan_count': 1}


def test_previous_child_and_same_child_old_session_cannot_fill_current_planning_facts(tmp_path):
    with client_for(tmp_path) as client:
        sid = capture_session(client)
        assert client.post(f'/v1/sessions/{sid}/homework', json={'subject': '数学', 'title': '旧会话的数学作业',
            'expected_minutes': 15, 'client_action_id': 'previous-homework'}).status_code == 201
        assert client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'previous-plan'}).status_code == 200
        repo = client.app.state.repository
        runtime = client.app.state.agent_orchestrator.agent_runtime
        junior = repo.switch_active_session_child(sid, 'child-junior-3', 'switch-to-junior')
        assert runtime.assemble_context(junior['session_id'], ())['daily_planning_state'] == {
            'saved_homework_count': 0, 'homework_declared_empty': False, 'active_plan_count': 0}
        primary = repo.switch_active_session_child(junior['session_id'], 'child-primary-4', 'switch-back-primary')
        assert primary['session_id'] != sid
        assert runtime.assemble_context(primary['session_id'], ())['daily_planning_state'] == {
            'saved_homework_count': 0, 'homework_declared_empty': False, 'active_plan_count': 0}
        with client.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute('SELECT count(*) FROM homework_item WHERE session_id=?', (sid,)).fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM daily_plan WHERE session_id=? AND status='active'", (sid,)).fetchone()[0] == 1
