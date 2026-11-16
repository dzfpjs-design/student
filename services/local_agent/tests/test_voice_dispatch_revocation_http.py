"""Real voice APIs and gateway retries; synthetic ASR and no external network."""
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from test_diagnostic_prompt_http import environment, CHILD, OTHER
from test_answer_same_turn_correction import open_exam
from test_voice_runtime_binding import capture


@pytest.mark.parametrize('lane,fault', [
    ('control','normal'), ('control','parent_off'), ('control','arrival_deleted'),
    ('control','journal_cancelled'), ('control','voice_cancelled'), ('control','foreign_voice'),
    ('cloudvoice','normal'), ('cloudvoice','parent_off'), ('cloudvoice','claim_deleted'),
    ('cloudvoice','audit_cancelled'), ('cloudvoice','foreign_claim'),
    ('control','consent_replaced'), ('cloudvoice','consent_replaced'),
])
def test_each_voice_post_revalidates_original_authority(environment, monkeypatch, lane, fault):
    settings, db = environment
    settings = replace(settings, model_provider='qwen', model_name='isolated-intent',
        model_api_key='synthetic-fa77d7bdb2ae64fbd28ade4f', model_base_url='https://voice.invalid/v1')
    posts, changes = [], []
    application = None
    headers = None
    sid = None
    quote = '这张卷子先搁一搁，回来咱们再往下做' if lane == 'control' else '以后我叫你小星，好不好'
    voice = None

    async def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        if len(posts) == 1:
            if fault in {'parent_off', 'voice_cancelled', 'consent_replaced'}:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://testserver') as admin:
                    if fault in {'parent_off', 'consent_replaced'}:
                        result = await admin.put('/v1/parent/privacy', headers=headers, json={'cloud_model_text_allowed':False})
                        if fault == 'consent_replaced':
                            assert result.status_code == 200, result.text
                            result = await admin.put('/v1/parent/privacy', headers=headers, json={'cloud_model_text_allowed':True})
                    else:
                        result = await admin.post(f'/v1/sessions/{sid}/asr/actions/retry-voice/cancel')
                    assert result.status_code == 200, result.text
                    changes.append(result.json())
            elif fault != 'normal':
                with db.connect() as c:
                    if fault == 'arrival_deleted':
                        count = c.execute('DELETE FROM learning_input_arrival WHERE session_id=?',(sid,)).rowcount
                    elif fault == 'journal_cancelled':
                        count = c.execute("UPDATE voice_control_intent_request SET status='cancelled' WHERE session_id=?",(sid,)).rowcount
                    elif fault == 'foreign_voice':
                        count = c.execute('UPDATE voice_capture_binding SET child_id=? WHERE voice_input_id=?',(OTHER,voice)).rowcount
                    elif fault == 'claim_deleted':
                        count = c.execute('DELETE FROM cloud_voice_interaction WHERE session_id=?',(sid,)).rowcount
                    elif fault == 'foreign_claim':
                        count = c.execute('UPDATE cloud_voice_interaction SET child_id=? WHERE session_id=?',(OTHER,sid)).rowcount
                    else:
                        count = c.execute("UPDATE audit_event SET summary_json=json_set(summary_json,'$.status','cancelled') WHERE action='voice_interaction_started'").rowcount
                    assert count > 0
                    c.commit()
                    changes.append(count)
            return httpx.Response(500, json={'error':{'message':'controlled retry'}})
        value = ({'role':'control','control':'exam.pause','quote':quote,'confidence':.99} if lane == 'control'
                 else {'route':'onboarding','action':'name_candidate','candidate':'小星','response_text':'想叫我小星，对吗？'})
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)}}]})

    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    application = create_app(settings, model_gateway=gateway)
    with TestClient(application, raise_server_exceptions=False) as client:
        parent = client.post('/v1/parent/setup',json={'pin':'8264'})
        assert parent.status_code == 200, parent.text
        headers = {'X-HSC-Parent':parent.json()['parent_token']}
        assert client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True}).status_code == 200
        if lane == 'control':
            sid, run = open_exam(client, db)
            voice = capture(client,settings,monkeypatch,sid,quote,{'kind':'exam','binding':run['answer_binding']},'retry-voice')
            url = f'/v1/sessions/{sid}/agent-turn'
            payload = {'transcript':quote,'client_action_id':'retry-control','voice_input_id':voice}
        else:
            sid = client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'retry-session'}).json()['session_id']
            url = f'/v1/sessions/{sid}/voice-interaction'
            payload = {'transcript':quote,'client_action_id':'retry-onboarding','context':{'phase':'naming'}}
        result = client.post(url,json=payload)
        assert len(posts) == (2 if fault == 'normal' else 1), (fault, len(posts), result.text)
        if fault == 'normal':
            assert result.status_code == 200, result.text
            assert posts[0] == posts[1]
            if lane == 'control':
                assert result.json()['action']['type'] == 'pause_exam_simulation'
                executed = client.post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
                assert executed.status_code == 200, executed.text
                with db.connect() as c:
                    assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()[0] == 'paused'
            else:
                assert result.json()['action'] == 'name_candidate' and result.json()['source'] == 'cloud'
            assert client.post(url,json=payload).status_code == 200
            assert len(posts) == 2
        else:
            assert changes and result.status_code in {200,409}, result.text
            if result.status_code == 200:
                if lane == 'control':
                    assert result.json()['action']['type'] == 'none'
                else:
                    assert result.json()['source'] != 'cloud'
            with db.connect() as c:
                if lane == 'control':
                    assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()[0] == 'active'
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        serialized = json.dumps(posts,ensure_ascii=False)
        assert all(secret not in serialized for secret in (CHILD,OTHER,sid,'pre_dispatch','local_audit','voice_input_binding'))
        if lane == 'control':
            sent = json.loads(posts[0]['messages'][1]['content'])
            assert set(sent) == {'transcript','state'} and sent['transcript'] == quote
            assert all(type(value) is bool for value in sent['state'].values())
            assert run['current_item']['prompt'] not in serialized and voice not in serialized


@pytest.mark.parametrize('lane', ['agent_plan', 'voice_agent_intent', 'companion_dialogue'])
@pytest.mark.parametrize('fault', ['normal', 'parent_off', 'consent_replaced', 'arrival_deleted', 'foreign_voice'])
def test_orchestrator_posts_keep_original_arrival_and_consent(environment, monkeypatch, lane, fault):
    from test_cloud_first_agent import Planner
    settings, db = environment
    settings = replace(settings, agent_runtime_enabled=lane == 'agent_plan', model_provider='qwen',
        model_name='isolated-intent', model_api_key='synthetic-fa77d7bdb2ae64fbd28ade4f', model_base_url='https://voice.invalid/v1')
    quote = '陪我聊聊今天的心情吧'
    posts, target_posts = [], []
    application = None
    headers = None
    sid = voice = None

    async def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        # The companion path first uses the real legacy semantic classifier.
        is_companion = '只输出纯文本' in body['messages'][0]['content']
        if lane == 'companion_dialogue' and not is_companion:
            value = json.dumps({'intent':'supportive_chat','confidence':.99})
            return httpx.Response(200,json={'choices':[{'message':{'content':value}}]})
        target_posts.append(body)
        if len(target_posts) == 1:
            if fault in {'parent_off','consent_replaced'}:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://testserver') as admin:
                    assert (await admin.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False})).status_code == 200
                    if fault == 'consent_replaced':
                        assert (await admin.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True})).status_code == 200
            elif fault != 'normal':
                with db.connect() as c:
                    if fault == 'arrival_deleted':
                        changed = c.execute('DELETE FROM learning_input_arrival WHERE session_id=?',(sid,)).rowcount
                    else:
                        changed = c.execute('UPDATE voice_capture_binding SET child_id=? WHERE voice_input_id=?',(OTHER,voice)).rowcount
                    assert changed == 1
                    c.commit()
            return httpx.Response(500,json={'error':{'message':'controlled retry'}})
        if lane == 'agent_plan':
            planned = Planner('conversation.respond',{'response':'愿意说说今天的心情吗？'},quote,objective_type='companion').payload
            planned['spoken_transition'] = '愿意说说今天的心情吗？'
            value = json.dumps(planned,ensure_ascii=False)
        elif lane == 'voice_agent_intent':
            value = json.dumps({'intent':'plan_status','confidence':.99})
        else:
            value = '愿意说说今天的心情吗？'
        return httpx.Response(200,json={'choices':[{'message':{'content':value}}]})

    gateway = ModelGateway(settings,transport=httpx.MockTransport(transport))
    application = create_app(settings,model_gateway=gateway)
    with TestClient(application,raise_server_exceptions=False) as client:
        parent = client.post('/v1/parent/setup',json={'pin':'8264'})
        headers = {'X-HSC-Parent':parent.json()['parent_token']}
        assert client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True}).status_code == 200
        sid = client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'retry-session'}).json()['session_id']
        voice = capture(client,settings,monkeypatch,sid,quote,key='retry-voice')
        payload = {'transcript':quote,'client_action_id':'retry-orchestrator','voice_input_id':voice}
        result = client.post(f'/v1/sessions/{sid}/agent-turn',json=payload)
        assert len(target_posts) == (2 if fault == 'normal' else 1), (lane,fault,result.text,len(target_posts))
        assert result.status_code in {200,409},result.text
        if fault == 'normal':
            assert result.status_code == 200
            assert target_posts[0] == target_posts[1]
            assert result.json()['action']['source'] in {'agent_runtime_planner','cloud_classifier','cloud_companion'}
            count = len(posts)
            assert client.post(f'/v1/sessions/{sid}/agent-turn',json=payload).status_code == 200
            assert len(posts) == count
        elif result.status_code == 200:
            assert result.json()['action']['type'] == 'none'
            assert result.json()['action'].get('source') not in {'agent_runtime_planner','cloud_classifier','cloud_companion'}
        serialized = json.dumps(posts,ensure_ascii=False)
        assert all(secret not in serialized for secret in (CHILD,OTHER,sid,voice,'pre_dispatch','local_audit','voice_input_binding'))
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('lane', ['agent_plan','cloudvoice'])
def test_voice_consent_replaced_at_final_write_cannot_persist_cloud_action(environment, monkeypatch, lane):
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    from home_study_agent.cloud_voice_interaction import CloudVoiceInteractionService
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from test_cloud_first_agent import Planner
    settings, db = environment
    settings = replace(settings,model_provider='qwen',model_name='isolated-intent',
        model_api_key='synthetic-fa77d7bdb2ae64fbd28ade4f',model_base_url='https://voice.invalid/v1')
    quote = '陪我聊聊今天的心情吧' if lane == 'agent_plan' else '以后我叫你小星，好不好'
    posts = []
    def transport(request):
        posts.append(json.loads(request.content))
        if lane == 'agent_plan':
            value = Planner('conversation.respond',{'response':'愿意说说今天的心情吗？'},quote,objective_type='companion').payload
            value['spoken_transition'] = '愿意说说今天的心情吗？'
        else:
            value = {'route':'onboarding','action':'name_candidate','candidate':'小星','response_text':'想叫我小星，对吗？'}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)}}]})
    application = create_app(settings,model_gateway=ModelGateway(settings,transport=httpx.MockTransport(transport)))
    privacy = PrivacyLifecycleService(db,settings.data_dir)
    def replace_consent():
        privacy.update_preferences({'cloud_model_text_allowed':False})
        privacy.update_preferences({'cloud_model_text_allowed':True})
    if lane == 'agent_plan':
        original = AgentOrchestrator._resolve_agent_plan
        async def revoke_after_planning(self, **kwargs):
            result = await original(self, **kwargs)
            assert result is not None
            replace_consent()
            return result
        monkeypatch.setattr(AgentOrchestrator,'_resolve_agent_plan',revoke_after_planning)
    else:
        original = CloudVoiceInteractionService._parse
        def revoke_after_parse(self,*args):
            result = original(self,*args)
            replace_consent()
            return result
        monkeypatch.setattr(CloudVoiceInteractionService,'_parse',revoke_after_parse)
    with TestClient(application,raise_server_exceptions=False) as client:
        sid = client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'final-write-session'}).json()['session_id']
        payload = {'transcript':quote,'client_action_id':'final-write-voice'}
        route = 'agent-turn' if lane == 'agent_plan' else 'voice-interaction'
        if lane == 'cloudvoice':
            payload['context'] = {'phase':'naming'}
        response = client.post(f'/v1/sessions/{sid}/{route}',json=payload)
        assert len(posts) == 1,response.text
        assert response.status_code in {409,422},response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM agent_turn WHERE session_id=?',(sid,)).fetchone()[0] == 0
            if lane == 'cloudvoice':
                row = c.execute('SELECT response_json FROM cloud_voice_interaction WHERE session_id=?',(sid,)).fetchone()
                assert row and row[0] is None


def test_new_arrival_after_erased_history_is_not_permanently_cloud_blocked(environment):
    """Synthetic prior tombstone; verify the new request, not a privacy-delete workflow."""
    from voice_dispatch_helpers import capture_dispatch_guard
    settings, db = environment
    application = create_app(settings)
    with TestClient(application) as client:
        sid = client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'new-arrival-session'}).json()['session_id']
        previous = client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':'停止说话','client_action_id':'previous-local-command'})
        assert previous.status_code == 200,previous.text
        with db.connect() as c:
            c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED',action_json=? WHERE agent_turn_id=?",
                (json.dumps({'type':'none','local_only':True,'cloud_transmission_allowed':False}),previous.json()['agent_turn_id']))
            c.commit()
        guard = capture_dispatch_guard(application.state.agent_orchestrator,sid,CHILD,'陪我聊聊吧')
        guard()
        # A further mutation during this request must still invalidate its frozen history.
        with db.connect() as c:
            c.execute('UPDATE agent_turn SET response_text=?,transcript=? WHERE agent_turn_id=?',
                ('changed','changed',previous.json()['agent_turn_id']))
            c.commit()
        with pytest.raises(ValueError,match='VOICE_DISPATCH_SOURCE_CHANGED'):
            guard()
