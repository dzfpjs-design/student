"""Unknown requests must not become scored answers to a saved local prompt."""
from fastapi.testclient import TestClient
import json
import pytest

from home_study_agent.app import create_app
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.exam_simulation import ExamSimulationService
from test_diagnostic_prompt_http import environment, start, prepare
from test_agent_continuation import Gateway
from home_study_agent.agent_orchestrator import AgentOrchestrator


def turn(client, sid, text, key, cloud=True):
    response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'client_action_id': key, 'cloud_text_allowed': cloud})
    assert response.status_code == 200, response.text
    return response.json()


def assert_content_free_requests(requests, utterances, forbidden):
    for request in requests:
        assert request.request_type == 'voice_interaction'
        assert len(request.messages) == 2
        payload = json.loads(request.messages[1]['content'])
        assert set(payload) == {'transcript', 'state'}
        assert payload['transcript'] in utterances
        assert all(type(value) is bool for value in payload['state'].values())
        assert request.context_manifest['local_sources_included'] is False
        assert request.context_manifest['history_included'] is False
        sent = json.dumps(request.messages, ensure_ascii=False)
        assert all(secret not in sent for secret in forbidden)


@pytest.mark.parametrize('stage', ['check_in', 'study'])
def test_unrecognized_requests_are_local_clarification_and_short_answers_survive_restart(environment, stage):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute('UPDATE daily_session SET state=? WHERE session_id=?', (stage, sid))
            c.commit()
        prompt = prepare(client, sid)['prompt']
        with db.connect() as c:
            mastery_before = [tuple(row) for row in c.execute('SELECT * FROM mastery_state ORDER BY rowid')]
        utterances = ['昨天漏掉的那份今天能不能补上', '把后面的安排挪到明天行不行', '这题的意思是要把两个数合起来吗']
        for index, text in enumerate(utterances):
            heard = turn(client, sid, text, f'unknown-{index}')
            assert heard['action']['type'] not in {'submit_diagnostic_answer', 'evaluate_child_response'}
            assert heard['action']['local_only']
            assert DiagnosticPromptService(db).read(sid)['prompt'] == prompt
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert [tuple(row) for row in c.execute('SELECT * FROM mastery_state ORDER BY rowid')] == mastery_before
    request_count = len(gateway.requests)
    assert request_count > 0
    assert_content_free_requests(gateway.requests, utterances,
        [sid, prompt['prompt_id'], prompt['item_version'], prompt['item']['prompt']])
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        answer = turn(client, sid, '12', 'real-short-answer')
        assert answer['action']['type'] == 'submit_diagnostic_answer'
        assert answer['action']['diagnostic_prompt_id'] == prompt['prompt_id']
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
            'answer': answer['action']['answer'], 'client_action_id': 'real-score'})
        assert response.status_code == 200, response.text
        assert turn(client, sid, '12', 'real-short-answer')['agent_turn_id'] == answer['agent_turn_id']
    assert len(gateway.requests) == request_count


def test_exam_unknown_offline_request_never_scores_but_real_choice_uses_dispatcher(environment):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        service = ExamSimulationService(db)
        exam = service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'exam-start')
        assert exam['status'] == 'active', exam
        question = service.arrival_binding(sid)
        unknown = turn(client, sid, '我桌上那叠换一下先后顺序', 'unknown-exam', False)
        assert unknown['action']['type'] == 'none'
        assert service.arrival_binding(sid) == question
        assert service.active_for_session(sid)['answered_items'] == 0
        answer = turn(client, sid, '我选A', 'actual-choice', False)
        assert answer['action']['type'] == 'submit_exam_answer', {key: answer[key] for key in ('intent', 'response_text', 'action')}
        executed = client.post(f"/v1/agent-turns/{answer['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        assert executed.json()['execution']['result']['exam']['answered_items'] == 1
        assert service.active_for_session(sid)['run_id'] == exam['run_id']
    assert not gateway.requests


@pytest.mark.parametrize('change', ['none', 'replace', 'cancel', 'other_child', 'interruption'])
def test_free_answer_candidate_requires_explicit_role_and_same_target_after_restart(environment, change):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    original = '我把括号里的数相加再乘外面的数'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        prompt = prepare(client, sid)['prompt']
        candidate = turn(client, sid, original, 'answer-candidate')
        assert candidate['intent'] == 'pending_answer_clarification'
        assert candidate['action']['pending_answer_candidate']['answer'] == original
        executed = client.post(f"/v1/agent-turns/{candidate['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        assert executed.json()['execution']['capability'] == 'conversation.respond'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        if change in {'replace', 'cancel'}:
            DiagnosticPromptService(db).cancel(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
        if change == 'replace':
            prepare(client, sid, preferred_item_id='diag-p4-arithmetic-closed-2', client_action_id='replacement-question')
        if change == 'other_child':
            switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
                'target_child_id': 'child-junior-3', 'client_action_id': 'other-child-session'})
            assert switched.status_code == 200, switched.text
            sid = switched.json()['session_id']
        if change == 'interruption':
            turn(client, sid, '今天计划做到哪了', 'intervening-query')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        confirmation = turn(client, sid, '刚才那句就是我的答案', 'confirm-answer-role', False)
        if change == 'none':
            assert confirmation['action']['type'] == 'submit_diagnostic_answer'
            assert confirmation['action']['answer'] == original
            assert confirmation['action']['answer_source_turn_id'] == candidate['agent_turn_id']
            from home_study_agent.agent_runtime import AgentRuntimeStore
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert AgentRuntimeStore.resolve_local_answer_submission(c, confirmation['agent_turn_id'], 'diagnostic.submit')['answer'] == original
                c.rollback()
            response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
                'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                'answer': confirmation['action']['answer'], 'client_action_id': 'confirmed-answer-score'})
            assert response.status_code == 200, response.text
        else:
            assert confirmation['action']['type'] not in {'submit_diagnostic_answer', 'evaluate_child_response'}
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == (1 if change == 'none' else 0)
    assert not gateway.requests


@pytest.mark.parametrize('text,expected', [('12', True), ('十二', True), ('三分之二', True), ('x=2', True), ('x等于2', True), ('A', True), ('我选B', True),
    ('不知道', True), ('我的答案是先算括号里的数', True), ('答案是十二吗', False),
    ('把那一项挪到明天', False), ('题目是在问总数吗', False), ('12还是15', False)])
def test_answer_syntax_is_not_correctness_or_keyword_command_matching(text, expected):
    assert AgentOrchestrator._local_answer_has_shape(text, 'text') is expected


@pytest.mark.parametrize('quote', ['12', '把12分钟换成20分钟'])
def test_cloud_cannot_extract_a_number_from_a_change_request_as_an_answer(quote):
    from home_study_agent.agent_runtime import CapabilityRegistry, InvalidAgentPlanError, parse_agent_plan_output
    decision = parse_agent_plan_output(json.dumps({
        'objective': {'mode': 'none', 'phase': 'check'},
        'next_step': {'capability': 'exam.submit', 'arguments': {'answer': '12'}},
        'spoken_transition': '核对回答。', 'requires_child_reply': False, 'confidence': 1,
    }), 'study', CapabilityRegistry())
    with pytest.raises(InvalidAgentPlanError, match='complete child utterance'):
        AgentOrchestrator._validate_cloud_utterance_binding(
            json.dumps({'utterance_binding': {'role': 'answer', 'quote': quote}}),
            '把12分钟换成20分钟', decision, {'capability_name': 'exam.start'})


def test_verified_semantic_cloud_command_precedes_exam_answer_without_exporting_question(environment):
    settings, db = environment
    quote = '我想知道今天整个安排是什么样，好心里有数'
    gateway = Gateway({
        'objective': {'mode': 'new', 'type': 'execution', 'summary': 'Read plan',
                      'success_criteria': ['Read current plan'], 'phase': 'execute'},
        'next_step': {'capability': 'planning.describe', 'arguments': {}},
        'spoken_transition': '我查看一下当前安排。', 'requires_child_reply': False, 'confidence': .99,
        'utterance_binding': {'role': 'task_request', 'quote': quote},
    })
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        service = ExamSimulationService(db)
        exam = service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'semantic-exam-start')
        before = service.arrival_binding(sid)
        planned = turn(client, sid, quote, 'semantic-other-request')
        assert planned['action']['type'] == 'describe_plan'
        execution = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
        assert execution.status_code == 200, execution.text
        assert execution.json()['execution']['capability'] == 'planning.describe'
        assert service.arrival_binding(sid) == before
        assert service.active_for_session(sid)['answered_items'] == 0
        assert len(gateway.requests) == 1
        assert exam['current_item']['prompt'] not in json.dumps(gateway.requests[0].messages, ensure_ascii=False)


@pytest.mark.parametrize('revise_criteria', [False, True])
def test_generic_goal_unknown_input_preserves_original_owner_until_answer_role_is_confirmed(tmp_path, revise_criteria):
    import asyncio
    from home_study_agent.database import Database
    from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, parse_agent_plan_output
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.repository import StudyRepository
    from test_agent_runtime import seed_session
    from test_agent_continuation import orchestrator_for

    db = Database(tmp_path / 'generic-reply.db')
    db.initialize()
    sid = seed_session(db)
    runtime = AgentRuntimeStore(db)
    decision = parse_agent_plan_output(json.dumps({
        'objective': {'mode': 'new', 'type': 'learning', 'summary': 'Explain a method',
                      'success_criteria': ['Explain independently'], 'phase': 'check'},
        'next_step': {'capability': 'learning.request_response', 'arguments': {'prompt': '说说你的方法。', 'subject': '数学'}},
        'spoken_transition': '说说你的方法。', 'requires_child_reply': True, 'confidence': 1,
    }), 'check_in', CapabilityRegistry())
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id)
            VALUES('initial','child-runtime',?,'检查我的理解','learning.request_response','说说你的方法。',
            '{}','test',datetime('now'),'initial-request')""", (sid,))
        runtime.persist_plan(c, child_id='child-runtime', session_id=sid, agent_turn_id='initial',
            client_action_id='initial-request', decision=decision, context_manifest={})
        c.commit()
    agent, gateway = orchestrator_for(db, tmp_path, {})
    executor = AgentCapabilityExecutor(db, StudyRepository(db))
    first = executor.execute('initial')
    agent.complete_turn('initial', first['response_text'], 'completed', None, first)
    original_owner = runtime.pending_child_reply(sid)
    quote = '我把括号里的数相加再乘外面的数'
    candidate = asyncio.run(agent.interpret(sid, quote, 'generic-candidate', False))
    assert candidate['intent'] == 'pending_answer_clarification'
    saved = executor.execute(candidate['agent_turn_id'])
    agent.complete_turn(candidate['agent_turn_id'], saved['response_text'], 'completed', None, saved)
    restored, _ = orchestrator_for(Database(db.path), tmp_path, {})
    assert restored.agent_runtime.pending_child_reply(sid) == original_owner
    if revise_criteria:
        with db.connect() as c:
            c.execute('UPDATE agent_objective SET success_criteria_json=? WHERE objective_id=?',
                      (json.dumps(['Explain a different problem']), original_owner['objective_id']))
            c.commit()
    confirmed = asyncio.run(restored.interpret(sid, '刚才那句就是我的答案', 'generic-confirmation', False))
    with db.connect() as c:
        step = c.execute('SELECT s.capability_name,s.arguments_json FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',
                         (confirmed['agent_turn_id'],)).fetchone()
        assert step['capability_name'] == ('conversation.respond' if revise_criteria else 'learning.evaluate_response')
        if not revise_criteria:
            assert json.loads(step['arguments_json'])['response'] == quote
            c.execute('BEGIN IMMEDIATE')
            assert AgentRuntimeStore.resolve_local_answer_submission(c, confirmed['agent_turn_id'], 'learning.evaluate_response')['answer'] == quote
            c.rollback()
        assert c.execute("SELECT status FROM agent_objective WHERE objective_id=?", (original_owner['objective_id'],)).fetchone()[0] == 'active'
    assert not gateway.requests


def test_clarification_candidate_is_absent_from_later_ordinary_cloud_context(environment):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        parent = client.post('/v1/parent/setup', json={'pin': '8264'})
        assert parent.status_code == 200, parent.text
        headers = {'X-HSC-Parent': parent.json()['parent_token']}
        denied = client.put('/v1/parent/privacy', headers=headers, json={'cloud_model_text_allowed': False})
        assert denied.status_code == 200, denied.text
        sid = start(client)
        prompt = prepare(client, sid)['prompt']
        private_answer = '这是只属于本地这道题的候选说明LOCALANSWERSECRET'
        candidate = turn(client, sid, private_answer, 'private-local-candidate', cloud=False)
        assert not gateway.requests
        assert candidate['intent'] == 'pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{candidate['agent_turn_id']}/execute").status_code == 200
        DiagnosticPromptService(db).cancel(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
        allowed = client.put('/v1/parent/privacy', headers=headers, json={'cloud_model_text_allowed': True})
        assert allowed.status_code == 200, allowed.text
        quote = '今天窗外阳光很好'
        gateway.output = {
            'objective': {'mode': 'none', 'phase': 'chat'},
            'next_step': {'capability': 'conversation.respond', 'arguments': {'response': '阳光好的时候，心情也会轻快一点。'}},
            'spoken_transition': '阳光好的时候，心情也会轻快一点。', 'requires_child_reply': False, 'confidence': .99,
            'utterance_binding': {'role': 'companionship', 'quote': quote},
        }
        turn(client, sid, quote, 'ordinary-later-conversation')
        assert len(gateway.requests) == 1
        sent = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        assert private_answer not in sent and 'LOCALANSWERSECRET' not in sent
        assert prompt['item']['prompt'] not in sent
