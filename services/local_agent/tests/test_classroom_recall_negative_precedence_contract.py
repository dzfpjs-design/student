"""Prompt precedence and original receipt semantics; synthetic plans are not model-quality evidence."""
import json

import pytest

from home_study_agent.agent_runtime import CapabilityRegistry, build_planner_messages, parse_agent_plan_output
from home_study_agent.agent_orchestrator import AgentOrchestrator
from test_app import client_for
from test_cloud_first_agent import Planner
from test_classroom_recall_planner_contract import ACTUAL_PUBLIC, session_at_recall

NATURAL_SAVE_CRITERION = '本轮明确的课堂内容已保存，可在当前孩子课堂回想记录中查到'


def original_question_messages(utterance):
    return build_planner_messages({'session': {'state': 'classroom_recall'},
        'recent_dialogue': [{'role': 'assistant', 'content': '今天课上学到了什么？'}]}, utterance, CapabilityRegistry())


@pytest.mark.parametrize('utterance', ['一元二次方程。', '今天数学学了一元二次方程，但不要保存。'])
def test_negative_precedence_survives_original_collection_question(utterance):
    system, user = original_question_messages(utterance)
    capture = system['content'].split('当前session.state为classroom_recall，', 1)[1]
    assert '否定和不确定性优先' in capture
    assert capture.index('否定和不确定性优先') < capture.index('课堂采集问题的回答')
    assert '即使刚问过课堂所学' in capture and '先自然澄清' in capture
    assert '不补造学科或本人课堂事实' in capture
    assert '不要保存' in capture and '优先尊重' in capture
    assert json.loads(user['content'])['child_utterance'] == utterance


def test_receipt_field_contract_does_not_conflate_human_success_criteria():
    system, user = original_question_messages(ACTUAL_PUBLIC)
    capture = system['content'].split('当前session.state为classroom_recall，', 1)[1]
    assert 'next_step.expected_evidence' in capture
    assert 'objective.success_criteria' in capture
    assert '不要求技术回执ID重复出现在自然语言目标条件中' in capture
    caps = {c['name']: c for c in json.loads(user['content'])['available_capabilities']}
    assert caps['recall.add']['expected_evidence'] == ['recall_saved']
    assert caps['recall.add_batch']['expected_evidence'] == ['recalls_saved']


@pytest.mark.parametrize('explicit_receipt', [False, True])
def test_original_parser_accepts_natural_success_criterion_and_registry_receipt(explicit_receipt):
    p = Planner('recall.add', {'subject': '数学', 'summary': ACTUAL_PUBLIC}, ACTUAL_PUBLIC, role='answer')
    p.payload['objective']['success_criteria'] = [NATURAL_SAVE_CRITERION]
    p.payload['next_step']['expected_evidence'] = ['recall_saved'] if explicit_receipt else []
    content = json.dumps(p.payload, ensure_ascii=False)
    d = parse_agent_plan_output(content, 'classroom_recall', CapabilityRegistry())
    assert d.objective.success_criteria == (NATURAL_SAVE_CRITERION,)
    assert d.step.expected_evidence == ('recall_saved',)
    assert 'recall_saved' not in d.objective.success_criteria
    assert AgentOrchestrator._validate_cloud_utterance_binding(content, ACTUAL_PUBLIC, d, None) == 'answer'


@pytest.mark.parametrize('batch', [False, True])
def test_real_http_natural_criteria_save_receipt_replay_and_reopen(tmp_path, batch):
    utterance = '今天数学课学了分式，历史课学了明清更替。' if batch else ACTUAL_PUBLIC
    cap = 'recall.add_batch' if batch else 'recall.add'
    items = [{'subject': '数学', 'summary': '数学课学了分式'}, {'subject': '历史', 'summary': '历史课学了明清更替'}]
    p = Planner(cap, {'items': items} if batch else {'subject': '数学', 'summary': utterance}, utterance, role='answer')
    receipt = 'recalls_saved' if batch else 'recall_saved'
    p.payload['objective']['success_criteria'] = [NATURAL_SAVE_CRITERION]
    p.payload['next_step']['expected_evidence'] = [receipt]
    p.payload['after_execution'] = 'stop'
    with client_for(tmp_path, model_gateway=p, agent_runtime_enabled=True) as client:
        sid = session_at_recall(client)
        response = client.post('/v1/sessions/' + sid + '/agent-turn', json={
            'transcript': utterance, 'client_action_id': 'natural-criteria-turn', 'cloud_text_allowed': True})
        assert response.status_code == 200
        turn = response.json(); assert turn['action']['capability'] == cap
        for _ in range(2):
            assert client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute').status_code == 200
        with client.app.state.agent_orchestrator.database.connect() as c:
            rows = c.execute("SELECT summary FROM classroom_recall WHERE session_id=? AND record_kind='child_statement' ORDER BY rowid", (sid,)).fetchall()
            assert [r['summary'] for r in rows] == ([v['summary'] for v in items] if batch else [utterance])
            criteria = c.execute('SELECT success_criteria_json FROM agent_objective WHERE last_agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
            assert json.loads(criteria['success_criteria_json']) == [NATURAL_SAVE_CRITERION]
            step = c.execute('SELECT expected_evidence_json FROM agent_step s JOIN agent_plan p ON p.plan_id=s.plan_id WHERE p.agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
            assert json.loads(step['expected_evidence_json']) == [receipt]
            assert c.execute("SELECT COUNT(*) FROM classroom_recall r JOIN daily_session s ON s.session_id=r.session_id WHERE s.child_id!='child-primary-4' AND r.record_kind='child_statement'").fetchone()[0] == 0
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'classroom_recall'
            assert c.execute('SELECT COUNT(*) FROM daily_plan').fetchone()[0] == 0
    with client_for(tmp_path, model_gateway=p, agent_runtime_enabled=True) as client:
        with client.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM classroom_recall WHERE session_id=? AND record_kind='child_statement'", (sid,)).fetchone()[0] == (2 if batch else 1)


@pytest.mark.parametrize('utterance,role', [('一元二次方程。', 'topic_mention'), ('今天数学学了一元二次方程，但不要保存。', 'task_request')])
def test_valid_clarification_or_refusal_response_never_writes_recall(tmp_path, utterance, role):
    p = Planner('conversation.respond', {'response': '请确认学科和课堂事实；你说不要保存时我不会记录。'}, utterance, role=role, objective_type='companion')
    p.payload['objective'].update(mode='none', success_criteria=[])
    with client_for(tmp_path, model_gateway=p, agent_runtime_enabled=True) as client:
        sid = session_at_recall(client)
        response = client.post('/v1/sessions/' + sid + '/agent-turn', json={
            'transcript': utterance, 'client_action_id': 'negative-current-turn', 'cloud_text_allowed': True})
        assert response.status_code == 200
        turn = response.json(); assert turn['action']['source'] == 'agent_runtime_planner'
        assert turn['action']['capability'] == 'conversation.respond'
        assert client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute').status_code == 200
        with client.app.state.agent_orchestrator.database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM classroom_recall').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM daily_plan').fetchone()[0] == 0
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'classroom_recall'
