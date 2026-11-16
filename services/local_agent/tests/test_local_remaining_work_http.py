"""A non-camera voice plan retains work without authorizing a second model call."""
from dataclasses import replace
import json

import httpx
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, start, OTHER
from test_voice_runtime_binding import capture, interpret


def test_voice_checkin_remaining_work_survives_app_restart_without_export(environment, monkeypatch):
    settings, db = environment
    settings = replace(settings, model_api_key='synthetic-only',
        model_base_url='https://remaining-work.invalid/v1', model_name='controlled-plan', agent_runtime_enabled=True)
    utterance = '我精神很好，记下来再安排学习'
    payload = Planner('checkin.update_context', {'energy_level': 'high'}, utterance).payload
    scope = [{'capability': 'session.transition', 'arguments': {'target': 'classroom_recall'},
        'request_quote': '安排学习'}]
    payload.update(after_execution='replan', continuation_scope=scope)
    calls = []

    async def transport(request):
        assert request.url.host == 'remaining-work.invalid'
        calls.append(json.loads(request.content))
        assert len(calls) == 1, 'A saved local continuation must not dispatch to a model'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(payload, ensure_ascii=False)}}]})

    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '8264'}).json()['parent_token']
        assert client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token},
            json={'cloud_model_text_allowed': True}).status_code == 200
        sid = start(client)
        voice = capture(client, settings, monkeypatch, sid, utterance, None, 'remaining-voice')
        root = interpret(client, sid, utterance, 'remaining-root', voice)
        assert root['runtime_capability'] == 'checkin.update_context', root
        assert root['action']['local_only'] is True
        assert root['action']['cloud_transmission_allowed'] is False
        executed = client.post(f'/v1/agent-turns/{root["agent_turn_id"]}/execute')
        assert executed.status_code == 200, executed.text
        assert executed.json()['turn']['tool_status'] == 'completed', executed.text
        assert len(calls) == 1
        expected = {'status': 'blocked', 'reason': 'LOCAL_CONTINUATION_AUTHORIZATION_REQUIRED'}
        for _ in range(2):
            response = client.post(f'/v1/sessions/{sid}/agent-turns/{root["agent_turn_id"]}/continue')
            assert response.status_code == 200, response.text
            assert response.json() == expected
        with db.connect() as c:
            assert c.execute('SELECT energy_level FROM session_planning_context WHERE session_id=?', (sid,)).fetchone()[0] == 'high'
            assert json.loads(c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?',
                (root['agent_turn_id'],)).fetchone()[0])['continuation_scope'] == scope

    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert client.get(f'/v1/sessions/{sid}/agent-continuation').json() == {
            **expected, 'parent_turn_id': root['agent_turn_id']}
        assert client.post(f'/v1/sessions/{sid}/agent-turns/{root["agent_turn_id"]}/continue').json() == expected
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': OTHER, 'client_action_id': 'other-remaining-session'})
        assert switched.status_code == 200, switched.text
        other = switched.json()['session_id']
        assert client.get(f'/v1/sessions/{other}/agent-continuation').json() == {'status': 'none'}
        assert client.post(f'/v1/sessions/{other}/agent-turns/{root["agent_turn_id"]}/continue').json() == {'status': 'none'}
        assert len(calls) == 1
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM agent_continuation').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM agent_turn').fetchone()[0] == 1
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
