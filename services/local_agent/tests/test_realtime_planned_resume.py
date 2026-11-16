"""Resume original planned work without reinterpreting the recovery utterance."""
import json

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, start, CHILD


WORDS = '请帮我看看本机PET学习资料'
INPUT = 'planned-resume-original'


def seed(client, db):
    sid = start(client)
    source = VoiceCaptureBindingService(db)
    source.begin_realtime(sid, INPUT, 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr')
    voice = source.complete_realtime(sid, INPUT, WORDS)
    result = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': WORDS,
        'voice_input_id': voice['voice_input_id'], 'client_action_id': 'realtime-' + voice['voice_input_id']})
    assert result.status_code == 200, result.text
    turn = result.json()
    assert turn['tool_status'] == 'pending' and turn['runtime_execution_location'] == 'server'
    return sid, voice, turn


def route(sid):
    return f'/v1/sessions/{sid}/voice/realtime/actions/{INPUT}/resume'


def planner():
    return Planner('system.inspect_learning', {'scope': 'resources', 'track': 'pet'}, WORDS)


def test_planned_original_survives_restart_and_executes_without_new_model_call(environment):
    settings, db = environment
    brain = planner()
    with TestClient(create_app(settings, model_gateway=brain)) as client:
        sid, voice, original = seed(client, db)
        preview = client.get(route(sid), params={'child_id': CHILD})
        assert preview.status_code == 200, preview.text
        assert preview.json()['status'] == 'ready', preview.text
        assert preview.json()['resume_kind'] == 'execution'
        assert preview.json()['agent_turn_id'] == original['agent_turn_id']
        assert WORDS not in preview.text
    with TestClient(create_app(settings, model_gateway=brain)) as client:
        resumed = client.post(route(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json() == original
        executed = client.post(f"/v1/agent-turns/{original['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        assert client.post(route(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']}).status_code == 409
        assert len(brain.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['running', 'failed', 'cancelled', 'other_key', 'bridge', 'answer', 'source', 'result'])
def test_browser_cannot_turn_unsafe_original_into_executable_work(environment, change):
    settings, db = environment
    brain = planner()
    with TestClient(create_app(settings, model_gateway=brain)) as client:
        sid, voice, original = seed(client, db)
        with db.connect() as c:
            if change in {'running', 'failed', 'cancelled'}:
                c.execute('UPDATE agent_step SET status=?', (change,))
            elif change == 'other_key': c.execute("UPDATE agent_turn SET client_action_id='different-engine-key'")
            elif change == 'bridge': c.execute("UPDATE agent_step SET execution_location='client_device_bridge'")
            elif change == 'answer': c.execute("UPDATE agent_step SET capability_name='learning.evaluate_response'")
            elif change == 'source':
                row = c.execute('SELECT action_json FROM agent_turn').fetchone()
                action = json.loads(row[0]); action['voice_input_binding'] = {}
                c.execute('UPDATE agent_turn SET action_json=?', (json.dumps(action),))
            else: c.execute("UPDATE agent_step SET result_json='{}'")
            c.commit()
        preview = client.get(route(sid), params={'child_id': CHILD})
        assert preview.status_code == 200 and preview.json()['status'] == 'blocked', preview.text
        resumed = client.post(route(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
        assert resumed.status_code == 409, resumed.text
        assert len(brain.requests) == 1


def test_cancel_after_ready_preview_does_not_execute_original(environment):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        sid, voice, original = seed(client, db)
        assert client.get(route(sid), params={'child_id': CHILD}).json()['status'] == 'ready'
        assert client.post(f'/v1/sessions/{sid}/asr/actions/{INPUT}/cancel').status_code == 200
        response = client.post(route(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
        assert response.status_code == 409, response.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM agent_step').fetchone()[0] == 'planned'


def test_resume_reply_is_not_permission_after_capture_is_cancelled(environment):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        sid, voice, original = seed(client, db)
        resumed = client.post(route(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
        assert resumed.status_code == 200, resumed.text
        assert client.post(f'/v1/sessions/{sid}/asr/actions/{INPUT}/cancel').status_code == 200
        result = client.post(f"/v1/agent-turns/{original['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM agent_step').fetchone()[0] == 'planned'


def test_two_resumes_share_original_execution_and_do_not_replay_saved_audio(environment):
    settings, db = environment
    brain = planner()
    with TestClient(create_app(settings, model_gateway=brain)) as client:
        sid, voice, original = seed(client, db)
        body = {'child_id': CHILD, 'voice_input_id': voice['voice_input_id']}
        for _ in range(2):
            resumed = client.post(route(sid), json=body)
            assert resumed.status_code == 200 and resumed.json()['agent_turn_id'] == original['agent_turn_id']
        executed = client.post(f"/v1/agent-turns/{original['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        repeated = client.post(f"/v1/agent-turns/{original['agent_turn_id']}/execute")
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['execution']['replay_audio_allowed'] is False
        assert repeated.json()['execution']['recorded_receipt'] is True
        assert repeated.json()['execution']['evidence'] == []
        assert len(brain.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 1
