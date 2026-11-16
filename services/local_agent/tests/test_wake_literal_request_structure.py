"""Literal delimitation is not evidence of a naming request.

Actual synthetic PCM ASR/Agent HTTP; model protocol is deliberately controlled
and may misclassify quotations. No network, settings save, or real device IO.
"""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_diagnostic_prompt_http import environment
from test_protected_daily_controls import Gateway, setup_context
from test_voice_runtime_binding import capture, interpret
from test_voice_device_capability_contract import receipt


REJECTED = [
    ('“把唤醒词改成小树”', '把唤醒词改成小树'),
    ('"把唤醒词改成小树"', '把唤醒词改成小树'),
    ('他说“把唤醒词改成小树”', '把唤醒词改成小树'),
    ('“把唤醒词改成小树”，请执行', '把唤醒词改成小树'),
    ('请执行“把唤醒词改成小树”', '把唤醒词改成小树'),
]
ACCEPTED = [
    ('把唤醒词改成“小树”', '小树'),
    ('把唤醒词改成"山间小云朵"', '山间小云朵'),
    ('我想换个唤醒名字', None),
    ('以后叫你“小小星河”吧', '小小星河'),
    ('把你的名字设为「远山来客」', '远山来客'),
    ('“山间小云朵”，就用这个作你的唤醒词', '山间小云朵'),
]


@pytest.mark.parametrize('quote,candidate', REJECTED)
def test_actual_http_quoted_command_is_not_a_name_request(environment, monkeypatch, quote, candidate):
    settings, db = environment
    gateway = Gateway('voice.configure_wake_word', quote, {'candidate': candidate})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, _ = setup_context(client, db, 'study')
        voice = capture(client, settings, monkeypatch, sid, quote)
        turn = interpret(client, sid, quote, 'quoted-command', voice)
        assert turn['action']['type'] == 'none', turn['action']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM agent_step WHERE capability_name='voice.configure_wake_word'").fetchone()[0] == 0


@pytest.mark.parametrize('quote,candidate', ACCEPTED[:3])
def test_actual_bound_http_named_literal_and_null_keep_configuration_only(environment, monkeypatch, quote, candidate):
    settings, db = environment
    arguments = {} if candidate is None else {'candidate': candidate}
    gateway = Gateway('voice.configure_wake_word', quote, arguments)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, reference = setup_context(client, db, 'pending_question')
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        turn = interpret(client, sid, quote, 'real-naming', voice)
        assert turn['action']['type'] == 'configure_wake_word'
        assert turn['action'].get('candidate') == candidate
        response = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert response.status_code == 200 and response.json()['status'] == 'admitted', response.text
        acknowledged = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            'tool_status': 'completed', 'final_response': turn['response_text'],
            'result': receipt('voice.configure_wake_word', {'configuration_started': True,
                'phase': 'confirm_name' if candidate else 'naming', 'wake_word_saved': False})})
        assert acknowledged.status_code == 200, acknowledged.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT current_position FROM specialized_exam_run').fetchone()[0] == 1


@pytest.mark.parametrize('quote,candidate,allowed', [(q,n,False) for q,n in REJECTED] + [(q,n,True) for q,n in ACCEPTED])
def test_structural_name_literal_validation(quote, candidate, allowed):
    value = dict(role='control', control='voice.configure_wake_word', quote=quote, confidence=.99,
                 wake_word={'candidate': candidate})
    try:
        result = VoiceControlIntent.parse(json.dumps(value), quote)
    except ValueError:
        result = {'status': 'clarify'}
    assert (result['status'] == 'control') is allowed
