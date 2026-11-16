"""New-control boundary probes using original captured question and real HTTP."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_diagnostic_prompt_http import environment
from test_protected_daily_controls import CASES, Gateway, setup_context
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('control,quote,action,arguments', CASES)
def test_cloud_off_keeps_question_and_does_not_infer_new_semantic_control(environment, monkeypatch, control, quote, action, arguments):
    settings, db = environment
    gateway = Gateway(control, quote, arguments)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, reference = setup_context(client, db, 'pending_question')
        # HTTP has no per-turn permission toggle: it derives authorization
        # from the real household preference, here in a disposable database.
        PrivacyLifecycleService(db, settings.data_dir).update_preferences({'cloud_model_text_allowed': False})
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'client_action_id': 'offline-control', 'voice_input_id': voice, 'cloud_text_allowed': False})
        assert response.status_code == 200, response.text
        assert response.json()['action']['type'] == 'none', response.text
        assert gateway.requests == []
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT current_position FROM specialized_exam_run').fetchone()[0] == 1


def test_mixed_control_answer_does_not_score_or_configure(environment, monkeypatch):
    settings, db = environment
    quote = '叫你小树吧，答案是四十二，不过我还没决定'
    gateway = Gateway('voice.configure_wake_word', quote, {'candidate': '小树'})
    async def mixed(request):
        if request.pre_dispatch:
            request.pre_dispatch()
        gateway.requests.append(request)
        return ModelResult(content=json.dumps(dict(role='mixed', control=None, quote=quote, confidence=.99)),
                           provider='isolated', model='mixed-protocol', latency_ms=1)
    gateway.complete = mixed
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, reference = setup_context(client, db, 'pending_question')
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        turn = interpret(client, sid, quote, 'mixed-control', voice)
        assert turn['action']['type'] == 'none', turn
        assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_admission_cannot_replace_frozen_wake_candidate_in_action_and_step(environment, monkeypatch):
    settings, db = environment
    control, quote, action, arguments = CASES[0]
    with TestClient(create_app(settings, model_gateway=Gateway(control, quote, arguments))) as client:
        sid, reference = setup_context(client, db, 'pending_question')
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        turn = interpret(client, sid, quote, 'wake-original', voice)
        with db.connect() as c:
            altered = dict(turn['action'], candidate='小花')
            c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', (json.dumps(altered), turn['agent_turn_id']))
            c.execute('UPDATE agent_step SET arguments_json=? WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)',
                      (json.dumps({'candidate': '小花'}), turn['agent_turn_id']))
            c.commit()
        response = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert response.status_code == 409, response.text


@pytest.mark.parametrize('context', ['study', 'pending_question'])
def test_model_cannot_invent_name_absent_from_current_utterance(environment, monkeypatch, context):
    settings, db = environment
    quote = '把唤醒名字改成小树'
    gateway = Gateway('voice.configure_wake_word', quote, {'candidate': '小花'})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, reference = setup_context(client, db, context)
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'client_action_id': 'wrong-name', 'voice_input_id': voice, 'cloud_text_allowed': True})
        assert response.status_code == 200, response.text
        assert response.json()['action']['type'] != 'configure_wake_word', response.text
