"""Actual root planning/execution/continuation with a synthetic voice and real gateway."""
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from test_agent_continuation import plan, next_plan
from test_diagnostic_prompt_http import environment, CHILD, OTHER
from test_voice_runtime_binding import capture


@pytest.mark.parametrize('fault', ['normal','parent_off','consent_replaced','cancel','foreign_parent','bound_voice'])
def test_continuation_retry_keeps_original_parent_and_voice(environment, monkeypatch, fault):
    settings, db = environment
    settings = replace(settings,model_provider='qwen',model_name='isolated-intent',
        model_api_key='synthetic-fa77d7bdb2ae64fbd28ade4f',model_base_url='https://voice.invalid/v1')
    quote = '精神很好，记下来再安排学习'
    root = plan().to_dict()
    root['next_step'] = {key:root['next_step'][key] for key in ('capability','arguments','reason','expected_evidence')}
    root['utterance_binding'] = {'role':'task_request','quote':quote}
    posts, continued = [], []
    application = None
    headers = None
    sid = voice = None

    async def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        if len(posts) == 1:
            value = root
        else:
            continued.append(body)
            if len(continued) == 1:
                if fault in {'parent_off','consent_replaced','cancel'}:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://testserver') as client:
                        if fault == 'cancel':
                            result = await client.post(f'/v1/sessions/{sid}/agent-turn',json={
                                'transcript':'停止说话','client_action_id':'interrupt-continuation'})
                            assert result.status_code == 200,result.text
                            assert result.json()['action']['type'] == 'stop_speech'
                        else:
                            assert (await client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False})).status_code == 200
                            if fault == 'consent_replaced':
                                assert (await client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True})).status_code == 200
                elif fault == 'foreign_parent':
                    with db.connect() as c:
                        assert c.execute('UPDATE agent_turn SET child_id=? WHERE agent_turn_id=?',(OTHER,turn_id)).rowcount == 1
                        c.commit()
                return httpx.Response(500,json={'error':{'message':'controlled retry'}})
            value = next_plan()
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)}}]})

    gateway = ModelGateway(settings,transport=httpx.MockTransport(transport))
    application = create_app(settings,model_gateway=gateway)
    with TestClient(application,raise_server_exceptions=False) as client:
        parent = client.post('/v1/parent/setup',json={'pin':'8264'})
        headers = {'X-HSC-Parent':parent.json()['parent_token']}
        assert client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True}).status_code == 200
        sid = client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'continuation-session'}).json()['session_id']
        voice = capture(client,settings,monkeypatch,sid,quote,key='continuation-voice') if fault == 'bound_voice' else None
        first = client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':quote,'voice_input_id':voice,'client_action_id':'continuation-root'})
        assert first.status_code == 200,first.text
        assert first.json().get('runtime_capability') == 'checkin.update_context',first.text
        turn_id = first.json()['agent_turn_id']
        executed = client.post(f'/v1/agent-turns/{turn_id}/execute')
        assert executed.status_code == 200,executed.text
        result = client.post(f'/v1/sessions/{sid}/agent-turns/{turn_id}/continue')
        assert result.status_code == 200,result.text
        if fault == 'bound_voice':
            assert first.json()['action']['local_only'] is True
            assert first.json()['action']['cloud_transmission_allowed'] is False
            assert result.json() == {
                'status': 'blocked',
                'reason': 'LOCAL_CONTINUATION_AUTHORIZATION_REQUIRED',
            }
            assert not continued
            return
        assert len(continued) == (2 if fault == 'normal' else 1),result.text
        if fault == 'normal':
            assert result.json()['status'] == 'ready',result.text
            next_id = result.json()['turn']['agent_turn_id']
            assert continued[0] == continued[1]
            actual = client.post(f'/v1/agent-turns/{next_id}/execute')
            assert actual.status_code == 200,actual.text
            with db.connect() as c:
                assert c.execute('SELECT state FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0] == 'classroom_recall'
        else:
            assert result.json()['status'] in {'blocked','superseded'},result.text
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE intent='runtime_continuation'").fetchone()[0] == 0
        serialized = json.dumps(posts,ensure_ascii=False)
        assert all(secret not in serialized for secret in (CHILD,OTHER,sid,'pre_dispatch','voice_input_binding'))
    reopened = create_app(settings,model_gateway=gateway)
    with TestClient(reopened) as client:
        before = len(posts)
        replay = client.post(f'/v1/sessions/{sid}/agent-turns/{turn_id}/continue')
        assert replay.status_code == 200,replay.text
        assert len(posts) == before
