"""Real host admission for synthetic bound-question control; no device or cloud IO."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_content_free_voice_control_http import ControlGateway
from test_diagnostic_prompt_http import environment
from test_answer_same_turn_correction import open_exam
from test_voice_runtime_binding import capture, interpret


def test_bound_camera_control_admits_once_and_restart_requires_receipt(environment, monkeypatch):
    settings, db = environment
    quote = '把镜头歇一会儿吧，我现在不需要它'
    gateway = ControlGateway(quote, 'observation.pause')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        turn = interpret(client, sid, quote, 'camera-control-admission', voice)
        assert turn['action']['type'] == 'pause_observation'
        assert turn['requires_control_admission'] is True
        url = f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission"
        response = client.post(url)
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'admitted'
        assert client.post(url).json()['status'] == 'execution_receipt_required'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0] == 'pending'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        response = client.post(url)
        assert response.status_code == 200, response.text
        assert response.json()['status'] != 'admitted'
    assert len(gateway.requests) == 1


def test_revoked_control_cannot_receive_device_admission(environment, monkeypatch):
    settings, db = environment
    quote = '把镜头歇一会儿吧，我现在不需要它'
    with TestClient(create_app(settings, model_gateway=ControlGateway(quote, 'observation.pause'))) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        turn = interpret(client, sid, quote, 'camera-control-revoked', voice)
        with db.connect() as c:
            c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
            c.commit()
        response = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert response.status_code == 409, response.text
        with db.connect() as c:
            assert c.execute('SELECT s.status FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',
                (turn['agent_turn_id'],)).fetchone()[0] != 'running'


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_control_journal_export_erasure_and_restart_do_not_restore_authority(environment, monkeypatch, scope):
    settings, db = environment
    quote = '把镜头歇一会儿吧，我现在不需要它'
    gateway = ControlGateway(quote, 'observation.pause')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        turn = interpret(client, sid, quote, 'camera-control-erasure', voice)
        token = client.post('/v1/parent/setup', json={'pin': '7531'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        service = client.app.state.privacy_lifecycle
        exported = service.export_child('child-primary-4')
        payload = json.loads(Path(exported['path']).read_text())
        assert len(payload['voice_control_intent_requests']) == 1
        assert all(row['child_id'] == 'child-primary-4' for row in payload['voice_control_intent_requests'])
        erased = client.post('/v1/parent/children/child-primary-4/delete', json={
            'scope': scope, 'confirmation': '确认删除', 'client_action_id': 'erase-control'})
        assert erased.status_code == 200, erased.text
        assert erased.json()['status'] == 'completed'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_control_intent_request').fetchone()[0] == 0
        response = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert response.status_code == 409, response.text
    assert len(gateway.requests) == 1
