"""D-030 original capture -> Gateway -> Runtime execution -> isolated export.

HTTP model replies are synthetic. This verifies wiring, not paid provider quality
or acoustic delivery; the output authority never accepts a renderer public flag.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from home_study_agent.public_reply_projection import VERSION
from test_cloud_first_agent import Planner
from test_realtime_output_authority import KEY, proof


@pytest.mark.parametrize('final_reply', ['我在听，我们聊聊花园。', '好。'])
def test_original_turn_executes_and_public_reply_is_independently_reserved(tmp_path, final_reply):
    utterance = '陪我聊聊花园里的花，第一句还有第二句。'
    planner = Planner('conversation.respond', {'response': final_reply}, utterance,
                      role='question', objective_type='companion')
    planner.payload['spoken_transition'] = '我在听，我们继续聊。'
    requests = []
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'journey.db', model_api_key='synthetic-key',
        session_token='synthetic-output-session-token', native_launch_attestation_key=KEY,
        agent_runtime_enabled=True, maintenance_enabled=False)
    def transport(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json={'id': 'synthetic_real_transport_reply',
            'model': settings.voice_intent_model_name, 'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(planner.payload, ensure_ascii=False)}}]})
    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    db = Database(settings.database_path)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        client.headers['X-HSC-Session'] = settings.session_token
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4',
            'client_action_id': 'synthetic-journey-start'}).json()['session_id']
        with db.connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=1 WHERE singleton_id=1'); c.commit()
        captures = VoiceCaptureBindingService(db)
        captures.begin_realtime(sid, 'synthetic-journey-capture', 'agent_command', {}, model_version='synthetic:transport_v1')
        capture = captures.complete_realtime(sid, 'synthetic-journey-capture', utterance)
        resumed = client.post(f'/v1/sessions/{sid}/voice/realtime/actions/synthetic-journey-capture/resume',
            json={'child_id': 'child-primary-4', 'voice_input_id': capture['voice_input_id']})
        assert resumed.status_code == 200, resumed.text
        turn = resumed.json()
        assert turn['transcript'] == utterance and turn['action']['local_only'] is True
        assert turn['action']['cloud_transmission_allowed'] is False
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        with db.connect() as c:
            stored = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
            assert stored['tool_status'] == 'completed' and stored['response_text'] == final_reply
            assert json.loads(stored['action_json'])['local_only'] is True
            assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (VERSION,)).fetchone()[0] == 2
            generation = c.execute('SELECT generation FROM privacy_storage_generation').fetchone()[0]
        scope = dict(session_id=sid, child_id='child-primary-4', agent_turn_id=turn['agent_turn_id'],
            connection_id='synthetic_connection', call_id='synthetic_call', voice='Tina',
            permit_id='synthetic_permit', storage_generation=generation)
        path = f'/v1/sessions/{sid}/voice-output-authority'
        reserved = client.post(path, json=proof(scope))
        assert reserved.status_code == 200, reserved.text
        assert reserved.json()['response_text'] == final_reply and not reserved.json()['local_only']
        assert reserved.json()['input_source']['voice_input_id'] == capture['voice_input_id']
        assert client.post(path, json=proof(scope)).status_code == 409
        assert client.post(path, json=proof(scope, 'bind', 'resp_synthetic_output')).json()['dispatch_allowed'] is True
        assert len(requests) == 1
    with TestClient(create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(
            lambda _: pytest.fail('Restart may not replay a model request'))))) as restarted:
        restarted.headers['X-HSC-Session'] = settings.session_token
        result = restarted.post(path, json=proof(scope, 'inspect')).json()
        assert result['state'] == 'bound' and not result['dispatch_allowed'] and 'response_text' not in result
        assert restarted.post(path, json=proof(scope)).status_code == 409
    with db.connect() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
