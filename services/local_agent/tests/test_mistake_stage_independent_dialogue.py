"""Actual isolated HTTP/SQLite and canonical voice, synthetic provider."""
from datetime import datetime, timezone
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_mistake_voice_journey import settings, fixture, begin, say, focus
from test_pet_vocabulary_prompt_integration import Planner
from test_foreground_independent_dialogue import foreground_models
from test_pending_evaluation_independent_tutor import QUESTION, tutor_models, lesson_body, tutor_calls
from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError

QUOTE = '先不看刚才那道题，你能告诉我有没有PET学习资料吗？'


@pytest.fixture(params=['diagnosis', 'remediation'])
def stage_context(tmp_path, request):
    config = settings(tmp_path)
    gateway = Planner()
    gateway.configured = False
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        sid, db = fixture(client)
        begin(client, sid)
        for index, text in enumerate(('第四题', '对', '我选B', '是的')):
            say(client, sid, text, f'prepare-stage-{index}')
        if request.param == 'remediation':
            say(client, sid, '我是看拼写选的，不知道比较读音的方法', 'prepare-stage-remediation')
        original = focus(client, sid)
        assert original['phase'] == request.param
        assert not original.get('answer_binding')
        yield {'settings': config, 'db': db, 'sid': sid, 'client': client, 'gateway': gateway, 'original': original}


def ask(j, quote=QUOTE):
    reference = {'child_id': j['original']['child_id']}
    if j['original'].get('dialogue_binding'):
        reference['question_reference'] = {'kind': 'mistake', 'binding': j['original']['dialogue_binding']}
    started = j['client'].post(f"/v1/sessions/{j['sid']}/voice/realtime/begin", json={
        'client_action_id': 'mistake-stage-voice', 'model_label': 'synthetic-asr',
        'captured_at_ms': int(datetime.now(timezone.utc).timestamp()*1000), 'capture_reference': reference})
    assert started.status_code == 200, started.text
    completed = j['client'].post(f"/v1/sessions/{j['sid']}/voice/realtime/complete", json={
        'client_action_id': 'mistake-stage-voice', 'transcript': quote})
    assert completed.status_code == 200, completed.text
    return j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json={
        'client_action_id': 'mistake-stage-independent', 'transcript': quote,
        'voice_input_id': completed.json()['voice_input_id'], 'cloud_text_allowed': True})


def test_independent_resources_do_not_become_original_diagnosis_or_understanding(stage_context, monkeypatch):
    j = stage_context
    foreground_models(j, monkeypatch, QUOTE, capability='system.inspect_learning',
                      arguments={'scope': 'resources', 'track': 'pet'})
    planned = ask(j)
    assert planned.status_code == 200, planned.text
    turn = planned.json()
    assert turn['action']['type'] == 'inspect_learning_system', turn['action']
    executed = j['client'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert executed.status_code == 200, executed.text
    assert focus(j['client'], j['sid'])['state'] == j['original']['state']


def learning_facts(j):
    with j['db'].connect() as c:
        return {name: [tuple(row) for row in c.execute('SELECT * FROM ' + name)] for name in (
            'mistake_case', 'mistake_attempt', 'mistake_diagnosis', 'mistake_dialogue',
            'learning_evidence', 'diagnostic_attempt', 'rubric_assessment')}


def test_actual_tutor_restarts_without_repeating_or_advancing_original_mistake(stage_context, monkeypatch):
    j = stage_context
    tutor_models(j, monkeypatch)
    before = learning_facts(j)
    planned = ask(j, QUESTION)
    assert planned.status_code == 200, planned.text
    body = lesson_body(planned.json())
    taught = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=body)
    assert taught.status_code == 200, taught.text
    assert '分配律' in taught.json()['answer']
    assert learning_facts(j) == before
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        replay = restarted.post(f"/v1/sessions/{j['sid']}/tutor", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['turn_id'] == taught.json()['turn_id']
        assert focus(restarted, j['sid'])['dialogue_binding'] == j['original']['dialogue_binding']
    assert len(tutor_calls(j)) == 1
    assert learning_facts(j) == before
    with j['db'].connect() as c:
        assert AgentRuntimeStore.local_mistake_answer_target(c, j['sid'], j['original']['child_id']) is None
        with pytest.raises(InvalidAgentPlanError):
            AgentRuntimeStore.resolve_local_answer_submission(c, planned.json()['agent_turn_id'], 'mistake.dialogue')
    for request in j['gateway'].requests:
        exported = json.dumps({'messages': request.messages, 'manifest': request.context_manifest,
                               'refs': request.evidence_refs}, ensure_ascii=False)
        for private in [j['original']['mistake_id'], j['original']['child_id'], j['sid'],
                        j['original']['state']['question_text']]:
            assert private not in exported


def invalidate(j, fault):
    if fault == 'consent':
        j['client'].app.state.privacy_lifecycle.update_preferences({'cloud_model_text_allowed': False})
        return
    with j['db'].connect() as c:
        if fault == 'paused':
            c.execute("UPDATE mistake_dialogue SET phase='paused' WHERE session_id=?", (j['sid'],))
        elif fault == 'excluded':
            c.execute("UPDATE mistake_case SET status='excluded' WHERE mistake_id=?", (j['original']['mistake_id'],))
        elif fault == 'page':
            c.execute("UPDATE capture_artifact SET extracted_text=extracted_text||' revised' WHERE artifact_id=?",
                      (j['original']['state']['artifact_id'],))
        elif fault == 'pending':
            c.execute("UPDATE mistake_dialogue SET state_json=json_set(state_json,'$.pending_operation',json(?)) WHERE session_id=?",
                      (json.dumps({'utterance': 'original pending write'}), j['sid']))
        elif fault == 'foundation':
            c.execute("UPDATE mistake_dialogue SET state_json=json_set(state_json,'$.foundation_origin',json(?)) WHERE session_id=?",
                      (json.dumps({'plan_item_id': 'missing-foundation-task'}), j['sid']))
        elif fault == 'session':
            c.execute("UPDATE daily_session SET state='break' WHERE session_id=?", (j['sid'],))
        else:
            raise AssertionError(fault)
        c.commit()


def test_derived_foundation_hold_cannot_publish_readonly_reference(stage_context):
    j = stage_context
    invalidate(j, 'foundation')
    current = focus(j['client'], j['sid'])
    assert current['state']['foundation_hold']['reason'] == 'source_changed'
    assert current.get('dialogue_binding') is None


@pytest.mark.parametrize('fault', ['paused', 'excluded', 'page', 'pending', 'foundation', 'session', 'consent'])
@pytest.mark.parametrize('timing', ['before_dispatch', 'after_dispatch'])
def test_changed_origin_or_consent_cannot_accept_late_independent_teaching(stage_context, monkeypatch, fault, timing):
    j = stage_context
    tutor_models(j, monkeypatch, after_submission=(lambda: invalidate(j, fault)) if timing == 'after_dispatch' else None)
    planned = ask(j, QUESTION)
    assert planned.status_code == 200, planned.text
    if timing == 'before_dispatch':
        invalidate(j, fault)
    before = learning_facts(j)
    result = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=lesson_body(planned.json()))
    assert result.status_code in {403, 409, 422}, result.text
    assert len(tutor_calls(j)) == int(timing == 'after_dispatch')
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
    if timing == 'before_dispatch':
        assert learning_facts(j) == before


@pytest.mark.parametrize('role,scope', [('question', 'current'), ('answer', None), ('mixed', None), ('unknown', None)])
def test_non_independent_meaning_never_borrows_readonly_binding_for_tutor(stage_context, monkeypatch, role, scope):
    j = stage_context
    quote = '这道题给我一个提示。' if role == 'question' else '我是看拼写选的，不知道比较读音的方法'
    foreground_models(j, monkeypatch, quote, role=role, scope=scope)
    planned = ask(j, quote)
    assert planned.status_code == 200, planned.text
    action = planned.json()['action']
    assert not action.get('tutor_request_action_id')
    assert not any(r.request_type == 'agent_plan' for r in j['gateway'].requests)
    with j['db'].connect() as c:
        assert AgentRuntimeStore.local_mistake_answer_target(c, j['sid'], j['original']['child_id']) is None
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0


def test_independent_lesson_does_not_attach_original_mistake_or_open_a_new_reply(stage_context, monkeypatch):
    j = stage_context
    quote = '数学里两个负数相乘为什么得到正数？'
    foreground_models(j, monkeypatch, quote, capability='learning.explain',
                      arguments={'question': quote, 'subject': '数学'})
    planned = ask(j, quote)
    assert planned.status_code == 200, planned.text
    turn = planned.json()
    assert turn['action']['type'] == 'ask_tutor', turn['action']
    assert turn['action']['question'] == quote
    assert turn['action']['tutor_request_action_id'].startswith('foreground-question:')
    with j['db'].connect() as c:
        row = c.execute('SELECT objective_id,plan_json FROM agent_plan WHERE agent_turn_id=?',
                        (turn['agent_turn_id'],)).fetchone()
        assert row['objective_id'] is None
        assert not json.loads(row['plan_json'])['requires_child_reply']
    for request in j['gateway'].requests:
        exported = json.dumps({'messages': request.messages, 'manifest': request.context_manifest,
                               'refs': request.evidence_refs}, ensure_ascii=False)
        for private in [j['original']['mistake_id'], j['original']['child_id'], j['sid'],
                        j['original']['state']['question_text']]:
            assert private not in exported
    assert focus(j['client'], j['sid'])['state'] == j['original']['state']
