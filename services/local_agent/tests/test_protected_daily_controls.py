"""Synthetic ASR + actual host/SQLite, inert model protocol, no network/device IO."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.model_gateway import ModelResult
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_diagnostic_prompt_http import environment, start
from test_answer_same_turn_correction import open_exam
from test_voice_runtime_binding import capture, interpret
from test_voice_device_capability_contract import receipt


CASES = [
    ('voice.configure_wake_word', '把唤醒词改成“小树”', 'configure_wake_word', {'candidate': '小树'}),
    ('device.status', '告诉我此刻哪些采集设备还在运行', 'query_device_status', {'device': 'all'}),
    ('parent.request', '我现在要找家长一起看看安排', 'find_parent', {}),
]


class Gateway:
    configured = True

    def __init__(self, control, quote, arguments):
        self.control, self.quote, self.arguments = control, quote, arguments
        self.requests = []

    async def complete(self, request):
        if request.pre_dispatch:
            request.pre_dispatch()
        self.requests.append(request)
        payload = json.loads(request.messages[-1]['content'])
        if request.context_manifest.get('lane') == 'content_free_voice_control_v1':
            assert set(payload) == {'transcript', 'state'}
            assert all(type(v) is bool for v in payload['state'].values())
            catalog = request.messages[0]['content'].split('静态控制候选：')[1].split('。')[0]
            assert self.control in catalog.split(', ')
            result = dict(role='control', control=self.control, quote=self.quote, confidence=.99)
            if self.control == 'voice.configure_wake_word':
                result['wake_word'] = {'candidate': self.arguments.get('candidate')}
            if self.control == 'device.connect':
                result['device'] = self.arguments['device']
        else:
            capability = 'safety.find_parent' if self.control == 'parent.request' else self.control
            result = {'objective': {'mode': 'none'},
                'next_step': {'capability': capability, 'arguments': self.arguments},
                'spoken_transition': '我先核对请求，确认执行结果后再继续。',
                'requires_child_reply': False, 'after_execution': 'stop', 'confidence': .99,
                'utterance_binding': {'role': 'task_request', 'quote': self.quote}}
        return ModelResult(content=json.dumps(result, ensure_ascii=False), provider='isolated', model='protocol', latency_ms=1)


def setup_context(client, db, context):
    if context == 'pending_question':
        sid, run = open_exam(client, db)
        return sid, {'kind': 'exam', 'binding': run['answer_binding']}
    sid = start(client)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    if context == 'activity_feedback':
        runtime = AgentRuntimeStore(db)
        runtime.start_companion_activity(sid, 'movement', 'PRIVATE_ACTIVITY_TITLE', 'original-activity')
        runtime.end_companion_activity(sid)
    return sid, None


def test_spoken_connect_both_devices_reaches_one_bounded_client_bridge_action(environment, monkeypatch):
    settings, db = environment
    quote = '连接摄像头和高拍仪'
    gateway = Gateway('device.connect', quote, {'device': 'all'})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, _ = setup_context(client, db, 'study')
        voice = capture(client, settings, monkeypatch, sid, quote, None)
        turn = interpret(client, sid, quote, 'connect-both-devices', voice)
        assert turn['action']['type'] == 'connect_devices'
        assert turn['action']['device'] == 'all'
        assert turn['runtime_capability'] == 'device.connect'
        assert turn['runtime_execution_location'] == 'client_device_bridge'
        assert not turn.get('requires_control_admission')
        assert len(gateway.requests) == 1


@pytest.mark.parametrize('context', ['study', 'pending_question', 'activity_feedback'])
@pytest.mark.parametrize('control,quote,action,arguments', CASES)
def test_daily_control_asr_host_keeps_original_pending(environment, monkeypatch, context, control, quote, action, arguments):
    settings, db = environment
    gateway = Gateway(control, quote, arguments)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, reference = setup_context(client, db, context)
        with db.connect() as c:
            before = {table: [tuple(r) for r in c.execute('SELECT * FROM '+table)]
                for table in ('specialized_exam_run', 'companion_activity_outcome', 'privacy_preference')}
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        turn = interpret(client, sid, quote, 'daily-control', voice)
        assert turn['action']['type'] == action, turn
        assert len(gateway.requests) == 1
        if context != 'study':
            assert turn['requires_control_admission'] is True
            admission = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
            assert admission.status_code == 200, admission.text
            assert admission.json()['status'] == 'admitted'
            messages = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
            assert sid not in messages and voice not in messages and 'PRIVATE_ACTIVITY_TITLE' not in messages
        assert all(turn['action'].get(k) == v for k, v in arguments.items())
        capability = 'safety.find_parent' if control == 'parent.request' else control
        if control == 'voice.configure_wake_word':
            output = {'configuration_started': True, 'phase': 'confirm_name', 'wake_word_saved': False}
        elif control == 'parent.request':
            output = {'parent_contact_prompted': True}
        else:
            output = {'checked_at': datetime.now(timezone.utc).isoformat(), 'devices': [
                dict(device_type=kind, track_live=False, frame_confirmed=False, last_frame_at=None,
                     last_error_code=None, monitor_active=False) for kind in ('camera', 'document_camera')]}
        acknowledged = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            'tool_status': 'completed', 'final_response': turn['response_text'], 'result': receipt(capability, output)})
        assert acknowledged.status_code == 200, acknowledged.text
        # Actual HTTP envelopes for mounted receiver tests; synthetic sources only.
        if os.environ.get('HSC_PROTECTED_CONTROL_PACKETS'):
            packets = Path(os.environ['HSC_PROTECTED_CONTROL_PACKETS'])
            packets.mkdir(parents=True, exist_ok=True)
            (packets / (context+'-'+control+'.json')).write_text(json.dumps(turn, ensure_ascii=False))
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            for table, rows in before.items():
                assert [tuple(r) for r in c.execute('SELECT * FROM '+table)] == rows
        turn_id = turn['agent_turn_id']
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, quote, 'daily-control', voice)
        assert replay['agent_turn_id'] == turn_id and replay['tool_status'] == 'completed'
        assert len(gateway.requests) == 1


@pytest.mark.parametrize('quote,candidate,allowed', [
    ('把唤醒词改成“小树”', '小树', True),
    ('叫你小树吧', '小树', True),
    ('我想换个唤醒名字', None, True),
    ('他说“把唤醒词改成小树”', '小树', False),
    ('不要把唤醒词改成小树', '小树', False),
    ('如果把唤醒词改成小树', '小树', False),
    ('把唤醒词改成小树', '小花', False),
])
def test_wake_literal_is_exact_request_not_quoted_instruction(quote, candidate, allowed):
    body = dict(role='control', control='voice.configure_wake_word', quote=quote, confidence=.99,
                wake_word={'candidate': candidate})
    try:
        result = VoiceControlIntent.parse(json.dumps(body), quote)
    except ValueError:
        result = {'status': 'clarify'}
    assert (result['status'] == 'control') is allowed


@pytest.mark.parametrize('control,quote,action,arguments', CASES)
@pytest.mark.parametrize('change', ['cancel', 'new_turn', 'other_child', 'candidate'])
def test_control_admission_rechecks_original_authority(environment, monkeypatch, control, quote, action, arguments, change):
    settings, db = environment
    gateway = Gateway(control, quote, arguments)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, reference = setup_context(client, db, 'pending_question')
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        turn = interpret(client, sid, quote, 'first-request', voice)
        assert turn['action']['type'] == action
        if change == 'new_turn':
            capture(client, settings, monkeypatch, sid, '取消刚才', reference, key='second-voice')
            client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '取消刚才', 'client_action_id': 'new-request', 'cloud_text_allowed': False})
        else:
            with db.connect() as c:
                if change == 'cancel':
                    c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
                elif change == 'other_child':
                    c.execute("UPDATE voice_control_intent_request SET child_id='child-junior-3'")
                else:
                    # Persisted command tampering, not a fake execution state.
                    c.execute("UPDATE agent_turn SET transcript=transcript || '不是这个' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
                c.commit()
        response = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert response.status_code == 409 or response.json().get('status') != 'admitted', response.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT current_position FROM specialized_exam_run').fetchone()[0] == 1


@pytest.mark.parametrize('control', ['voice.configure_wake_word', 'device.status', 'parent.request'])
@pytest.mark.parametrize('role,quote', [('mixed', '把名字改为小树，答案是四十二'), ('control', '他说“找家长”'), ('control', '不要执行刚才的请求')])
def test_nonaffirmative_model_output_cannot_authorize(control, role, quote):
    value = dict(role=role, control=control if role == 'control' else None, quote=quote, confidence=.99)
    if role == 'control' and control == 'voice.configure_wake_word':
        value['wake_word'] = {'candidate': None}
    try:
        result = VoiceControlIntent.parse(json.dumps(value), quote)
    except ValueError:
        result = {'status': 'clarify'}
    assert result['status'] != 'control'


def test_evaluation_catalog_does_not_gain_naming_or_general_evaluation_access():
    from home_study_agent.pending_evaluation_control import ALLOWED_CONTROLS
    assert 'voice.configure_wake_word' not in ALLOWED_CONTROLS
    for control in ('device.status', 'parent.request'):
        body = dict(role='control', control=control, quote='请核对当前情况', confidence=.99)
        assert VoiceControlIntent.parse(json.dumps(body), body['quote'], restricted=True)['status'] == 'control'
    for control, restricted in [('evaluation.status', False), ('voice.configure_wake_word', True)]:
        body = dict(role='control', control=control, quote='帮我换个唤醒名字', confidence=.99)
        if control.startswith('voice.'):
            body['wake_word'] = {'candidate': None}
        with pytest.raises(ValueError):
            VoiceControlIntent.parse(json.dumps(body), body['quote'], restricted=restricted)
