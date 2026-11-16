"""Real local prompt/guard/HTTP/SQLite contracts; planner results are synthetic, not model quality."""
import json
from pathlib import Path

import pytest

from home_study_agent.agent_runtime import CapabilityRegistry, InvalidAgentPlanError, PLANNER_PROMPT_VERSION, build_planner_messages, parse_agent_plan_output
from home_study_agent.agent_orchestrator import AgentOrchestrator
from test_app import client_for
from test_cloud_first_agent import Planner

ACTUAL_PUBLIC = '数学课今天学了把一元二次方程写成标准形式。'
VERSION = 'agent_runtime_plan_prompt_v11_tutor_followup_relation'


def messages(utterance=ACTUAL_PUBLIC, state='classroom_recall', continuation=False):
    context = {'session': {'state': state}, 'recent_dialogue': [{'role': 'assistant', 'content': '今天课上学到了什么？'}],
               'private_local': {'cloud_transmission_allowed': False, 'secret': 'SYNTHETIC_PRIVATE_NEVER_SEND'}}
    if continuation:
        context['continuation_event'] = {'authorized_actions': []}
    return build_planner_messages(context, utterance, CapabilityRegistry())


def test_prompt_contract_maps_classroom_self_report_to_existing_answer_role():
    system, user = messages()
    payload = json.loads(user['content'])
    assert '课堂采集问题的回答' in system['content']
    assert 'utterance_binding.role=answer' in system['content']
    assert 'quote逐字引用本轮完整原话' in system['content']
    assert payload['child_utterance'] == ACTUAL_PUBLIC
    assert 'statement' not in payload['output_schema']['utterance_binding']['role']
    assert 'SYNTHETIC_PRIVATE_NEVER_SEND' not in user['content']


def test_prompt_contract_requires_saved_execution_evidence_and_keeps_stage():
    system, user = messages()
    assert 'objective.type=execution' in system['content']
    assert 'recall_saved' in system['content'] and 'recalls_saved' in system['content']
    assert 'after_execution=stop' in system['content'] and '不自动跳阶段' in system['content']
    caps = {x['name']: x for x in json.loads(user['content'])['available_capabilities']}
    assert caps['recall.add']['expected_evidence'] == ['recall_saved']
    assert caps['recall.add_batch']['expected_evidence'] == ['recalls_saved']


def test_prompt_contract_has_distinct_observable_version():
    assert PLANNER_PROMPT_VERSION == VERSION


@pytest.mark.parametrize('state,continuation', [('check_in', False), ('homework_capture', False), ('study', False), ('classroom_recall', True)])
def test_new_capture_rule_is_not_added_outside_original_child_collection(state, continuation):
    system, _ = messages(state=state, continuation=continuation)
    assert '课堂采集问题的回答' not in system['content']


NEGATIVES = [
    ('一元二次方程。', '裸知识名词'),
    ('我弟今天数学课学了一元二次方程。', '转述他人'),
    ('故事里我今天数学课学了一元二次方程。', '故事'),
    ('如果今天数学课学一元二次方程，我就告诉你。', '条件假设'),
    ('今天数学学了一元二次方程，但不要保存。', '不保存'),
    ('你今天数学课学了什么？', '询问'),
]


@pytest.mark.parametrize('utterance,boundary', NEGATIVES)
def test_negative_semantics_are_explicit_without_changing_speech(utterance, boundary):
    system, user = messages(utterance)
    assert boundary in system['content']
    assert json.loads(user['content'])['child_utterance'] == utterance
    assert '不能靠关键词' in system['content']


def planned(utterance=ACTUAL_PUBLIC, capability='recall.add', arguments=None, role='answer'):
    p = Planner(capability, arguments or {'subject': '数学', 'summary': utterance}, utterance, role=role)
    p.payload['next_step']['expected_evidence'] = ['recalls_saved'] if capability == 'recall.add_batch' else ['recall_saved']
    p.payload['after_execution'] = 'stop'
    return p


@pytest.mark.parametrize('role', ['greeting', 'topic_mention', 'unknown'])
def test_original_ambiguity_guard_still_rejects_nonconversation_even_for_real_phrase(role):
    p = planned(role=role)
    content = json.dumps(p.payload, ensure_ascii=False)
    decision = parse_agent_plan_output(content, 'classroom_recall', CapabilityRegistry())
    with pytest.raises(InvalidAgentPlanError, match='ambiguous speech cannot trigger a capability'):
        AgentOrchestrator._validate_cloud_utterance_binding(content, ACTUAL_PUBLIC, decision, None)


@pytest.mark.parametrize('kind', ['invented_quote', 'new_statement_role'])
def test_original_grounding_and_role_schema_guards_are_not_relaxed(kind):
    p = planned()
    if kind == 'invented_quote':
        p.payload['utterance_binding']['quote'] = '我没有说过的内容'
    else:
        p.payload['utterance_binding']['role'] = 'statement'
    content = json.dumps(p.payload, ensure_ascii=False)
    decision = parse_agent_plan_output(content, 'classroom_recall', CapabilityRegistry())
    with pytest.raises(InvalidAgentPlanError):
        AgentOrchestrator._validate_cloud_utterance_binding(content, ACTUAL_PUBLIC, decision, None)


def session_at_recall(client):
    response = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'recall-session'})
    assert response.status_code == 201
    sid = response.json()['session_id']
    transition = client.post('/v1/sessions/' + sid + '/transition', json={'expected_state': 'check_in', 'target_state': 'classroom_recall', 'reason': 'synthetic ready', 'client_action_id': 'enter-recall'})
    assert transition.status_code == 200
    return sid


@pytest.mark.parametrize('batch', [False, True])
def test_real_http_execute_replay_and_reopen_save_only_bound_child_without_stage_skip(tmp_path, batch):
    utterance = '今天数学课学了分式，历史课学了明清更替。' if batch else ACTUAL_PUBLIC
    items = [{'subject': '数学', 'summary': '数学课学了分式'}, {'subject': '历史', 'summary': '历史课学了明清更替'}]
    p = planned(utterance, 'recall.add_batch' if batch else 'recall.add', {'items': items} if batch else {'subject': '数学', 'summary': utterance})
    with client_for(tmp_path, model_gateway=p, agent_runtime_enabled=True) as client:
        sid = session_at_recall(client)
        request = {'transcript': utterance, 'client_action_id': 'recall-turn', 'cloud_text_allowed': True}
        result = client.post('/v1/sessions/' + sid + '/agent-turn', json=request)
        assert result.status_code == 200
        turn = result.json()
        assert turn['action']['source'] == 'agent_runtime_planner'
        assert turn['action']['capability'] == ('recall.add_batch' if batch else 'recall.add')
        assert p.requests[0].prompt_version == VERSION
        assert '课堂采集问题的回答' in p.requests[0].messages[0]['content']
        for _ in range(2):
            response = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
            assert response.status_code == 200
        with client.app.state.agent_orchestrator.database.connect() as c:
            rows = c.execute("SELECT subject,summary,confirmation_status,record_kind FROM classroom_recall WHERE session_id=? AND record_kind='child_statement' ORDER BY rowid", (sid,)).fetchall()
            assert len(rows) == (2 if batch else 1)
            assert [r['summary'] for r in rows] == ([item['summary'] for item in items] if batch else [utterance])
            assert all(r['confirmation_status'] == 'child_confirmed' and r['record_kind'] == 'child_statement' for r in rows)
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'classroom_recall'
            assert c.execute("SELECT COUNT(*) FROM classroom_recall r JOIN daily_session s ON s.session_id=r.session_id WHERE s.child_id!='child-primary-4'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM daily_plan').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    with client_for(tmp_path, model_gateway=p, agent_runtime_enabled=True) as reopened:
        with reopened.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM classroom_recall WHERE session_id=? AND record_kind='child_statement'", (sid,)).fetchone()[0] == (2 if batch else 1)
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'classroom_recall'


def test_wrong_model_conversation_on_exact_phrase_is_not_overridden_by_local_keywords(tmp_path):
    p = Planner('conversation.respond', {'response': '请继续说。'}, ACTUAL_PUBLIC, role='topic_mention', objective_type='companion')
    with client_for(tmp_path, model_gateway=p, agent_runtime_enabled=True) as client:
        sid = session_at_recall(client)
        response = client.post('/v1/sessions/' + sid + '/agent-turn', json={'transcript': ACTUAL_PUBLIC, 'client_action_id': 'wrong-model-turn', 'cloud_text_allowed': True})
        assert response.status_code == 200 and response.json()['action']['source'] == 'agent_runtime_planner'
        assert response.json()['action']['capability'] == 'conversation.respond'
        with client.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM classroom_recall').fetchone()[0] == 0


def test_collection_answer_role_does_not_authorize_scoring_without_pending_reply():
    p = Planner('learning.evaluate_response', {'subject': '数学', 'response': ACTUAL_PUBLIC}, ACTUAL_PUBLIC,
                role='answer', objective_type='learning')
    content = json.dumps(p.payload, ensure_ascii=False)
    decision = parse_agent_plan_output(content, 'classroom_recall', CapabilityRegistry())
    with pytest.raises(InvalidAgentPlanError, match='no pending learning response'):
        AgentOrchestrator._validate_cloud_utterance_binding(content, ACTUAL_PUBLIC, decision, None)
