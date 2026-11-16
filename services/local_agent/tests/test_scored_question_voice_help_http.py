"""A scored original remains a read-only voice help target; never another answer."""
import json
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import httpx
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_diagnostic_prompt_http import environment, start, prepare, answer, CHILD


@pytest.mark.parametrize('change', ['none', 'source_changed', 'new_question', 'restart', 'unscored_reference'])
def test_scored_original_voice_help_keeps_exact_source_without_regrading(environment, change):
    settings, db = environment
    settings = replace(settings, model_provider='qwen', model_api_key='synthetic-only',
        model_name='isolated-help', voice_intent_model_name='isolated-help')
    db.initialize()
    privacy = PrivacyLifecycleService(db, settings.data_dir)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    requests = []
    quote = '讲解刚才那道错题。'
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        data = json.loads(body['messages'][-1]['content'])
        assert set(data) == {'transcript', 'state'}, data
        result = dict(role='control', control='learning.help_current', quote=quote, confidence=.99)
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(result)}}]})
    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    with ExitStack() as original_client_lifecycle:
        client = original_client_lifecycle.enter_context(TestClient(create_app(settings, model_gateway=gateway)))
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,));c.commit()
        original = prepare(client, sid)['prompt']
        if change != 'unscored_reference':
            scored = answer(client, sid, original)
            assert scored.status_code == 200, scored.text
            assert scored.json()['status'] == 'answered'
        with db.connect() as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')]
        ref = dict(kind='diagnostic_help', binding={k:original[k] for k in ('prompt_id','item_version')})
        begun = client.post(f'/v1/sessions/{sid}/voice/realtime/begin',json={
            'client_action_id':'scored-help-onset', 'model_label':'synthetic-asr',
            'captured_at_ms':int(datetime.now(timezone.utc).timestamp()*1000),
            'capture_reference':{'child_id':CHILD, 'question_reference':ref}})
        assert begun.status_code == 200, begun.text
        if change == 'source_changed':
            with db.connect() as c:
                c.execute('UPDATE diagnostic_item SET prompt=prompt || ? WHERE diagnostic_item_id=?',
                    (' changed', original['diagnostic_item_id']));c.commit()
        elif change == 'new_question':
            from test_diagnostic_prompt_http import SECOND
            response=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={
                'preferred_item_id':SECOND,'client_action_id':'help-new-question'})
            assert response.status_code == 200,response.text
        completed = client.post(f'/v1/sessions/{sid}/voice/realtime/complete',json={
            'client_action_id':'scored-help-onset', 'transcript':quote})
        assert completed.status_code == 200, completed.text
        heard = client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'client_action_id':'scored-help-command', 'transcript':quote,
            'voice_input_id':completed.json()['voice_input_id'], 'cloud_text_allowed':True})
        assert heard.status_code == 200, heard.text
        action = heard.json()['action']
        assert all(json.loads(body['messages'][-1]['content'])['state']['has_current_learning_question']
            is (change in {'none','restart'}) for body in requests), requests
        if change in {'source_changed', 'new_question', 'unscored_reference'}:
            assert action['type'] == 'none', action
            assert not action.get('diagnostic_prompt_id')
            with db.connect() as c:
                assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')] == before
            return
        assert action['type'] == 'ask_tutor', action
        assert action['question'] == original['item']['prompt']
        assert action['diagnostic_prompt_id'] == original['prompt_id']
        assert action['diagnostic_item_version'] == original['item_version']
        assert action['voice_input_binding']['question_reference'] == ref
        from home_study_agent.agent_runtime import AgentRuntimeStore
        captured=action['voice_input_binding']
        assert not AgentRuntimeStore.capture_matches_answer(captured,'submit_diagnostic_answer',ref['binding'])
        assert not AgentRuntimeStore.capture_matches_answer(captured,'continue_mistake_dialogue',ref['binding'])
        if change == 'restart':
            count=len(requests)
            original_client_lifecycle.close()
            gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
            with TestClient(create_app(settings,model_gateway=gateway)) as restarted:
                replay=restarted.post(f'/v1/sessions/{sid}/agent-turn',json={
                    'client_action_id':'scored-help-command','transcript':quote,
                    'voice_input_id':completed.json()['voice_input_id'],'cloud_text_allowed':True})
                assert replay.status_code == 200,replay.text
                assert replay.json()['action']==action
                assert len(requests)==count
        assert requests
        exported = json.dumps(requests,ensure_ascii=False)
        assert original['item']['prompt'] not in exported
        assert original['prompt_id'] not in exported
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')] == before
