"""Original gateway/HTTP/SQLite, synthetic responses and original NORMAL owner; no network."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from normal_provider_owner import normal_provider_owner
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway, ModelRequest, ModelResponseError


INVALID = '{"objective" not-json}'
VALID = '{"synthetic":true}'


def settings_for(tmp_path, fallback=True):
    return Settings(data_dir=tmp_path, database_path=tmp_path/'gateway.db',
        model_provider='deepseek', model_api_key='synthetic-primary',
        model_base_url='https://primary.invalid/v1', model_name='primary-synthetic',
        voice_intent_model_name='primary-synthetic', maintenance_enabled=False,
        fallback_model_provider='qwen' if fallback else None,
        fallback_model_api_key='synthetic-fallback' if fallback else None,
        fallback_model_base_url='https://fallback.invalid/v1' if fallback else None,
        fallback_model_name='fallback-synthetic' if fallback else None)


def original_request(kind='agent_plan', pre_dispatch=None):
    return ModelRequest(request_id='synthetic-request', child_id='child-primary-4',
        session_id='synthetic-session', request_type=kind, risk_level='medium',
        context_manifest={}, provider_preference='deepseek', policy_version='synthetic-policy',
        prompt_version='synthetic-prompt', knowledge_version=None, output_type='dialogue',
        evidence_refs=(), messages=({'role':'user','content':'Only synthetic JSON.'},),
        pre_dispatch=pre_dispatch)


class Policy:
    """Only observes the gateway's existing policy contract; HTTP tests use the real service."""
    def __init__(self, unavailable=(), denied=None):
        self.unavailable=set(unavailable); self.denied=denied; self.success=[]; self.failure=[]
    def authorize(self, kind):
        if self.denied is not None:
            raise self.denied
    def endpoint_available(self, provider, model):
        return model not in self.unavailable
    def record_success(self, provider, model):
        self.success.append((provider, model))
    def record_failure(self, provider, model, code):
        self.failure.append((provider, model, code))


def transport_for(posts, primary=INVALID, fallback=VALID, hook=None):
    def handler(request):
        host=request.url.host; assert host in {'primary.invalid','fallback.invalid'}
        posts.append(host)
        if hook:
            hook(request)
        content=primary if host=='primary.invalid' else fallback
        if type(content) is int:
            return httpx.Response(content, request=request)
        usage={'prompt_tokens':10,'completion_tokens':20} if host=='primary.invalid' else {'prompt_tokens':11,'completion_tokens':4}
        payload=json.loads(request.read())
        assert payload['response_format']=={'type':'json_object'}
        return httpx.Response(200, request=request, json={'id':'synthetic-response',
            'model':payload['model'], 'choices':[{'finish_reason':'stop','message':{'content':content}}], 'usage':usage})
    return httpx.MockTransport(handler)


def finish(gateway, request, entry='complete'):
    async def run():
        try:
            return await getattr(gateway, entry)(request)
        finally:
            await gateway.close()
    return asyncio.run(run())


@pytest.mark.parametrize('kind', ['agent_plan','atomic_knowledge_extraction'])
def test_invalid_primary_uses_only_configured_fallback_and_keeps_all_tokens(tmp_path, normal_provider_owner, kind):
    posts=[]; policy=Policy(); gateway=ModelGateway(settings_for(tmp_path),
        transport=transport_for(posts), runtime_policy=policy)
    result=finish(gateway, original_request(kind))
    assert posts==['primary.invalid','fallback.invalid']
    assert result.content==VALID and result.provider=='qwen' and result.fallback_used is True
    assert (result.prompt_tokens,result.completion_tokens)==(21,24)
    assert policy.failure==[('deepseek','primary-synthetic','MODEL_INVALID_JSON')]
    assert policy.success==[('qwen','fallback-synthetic')]


@pytest.mark.parametrize('fallback,hosts,tokens', [(True,['primary.invalid','fallback.invalid'],(21,24)),(False,['primary.invalid'],(10,20))])
def test_final_invalid_is_known_consumed_without_success_or_extra_attempt(tmp_path, normal_provider_owner, fallback, hosts, tokens):
    posts=[]; policy=Policy(); gateway=ModelGateway(settings_for(tmp_path,fallback),
        transport=transport_for(posts,fallback=INVALID),runtime_policy=policy)
    with pytest.raises(ModelResponseError) as caught:
        finish(gateway,original_request())
    error=caught.value
    assert error.code=='MODEL_INVALID_JSON' and error.response_received is True
    assert (error.prompt_tokens,error.completion_tokens)==tokens
    assert type(error.latency_ms)is int and error.latency_ms>=0
    assert posts==hosts and policy.success==[]
    assert len(policy.failure)==len(hosts)


def test_valid_primary_never_uses_fallback(tmp_path, normal_provider_owner):
    posts=[]; policy=Policy(); gateway=ModelGateway(settings_for(tmp_path),
        transport=transport_for(posts,primary=VALID),runtime_policy=policy)
    result=finish(gateway,original_request())
    assert posts==['primary.invalid'] and result.fallback_used is False
    assert (result.prompt_tokens,result.completion_tokens)==(10,20)
    assert policy.failure==[] and policy.success==[('deepseek','primary-synthetic')]


def test_unavailable_fallback_cannot_be_selected_after_invalid_primary(tmp_path, normal_provider_owner):
    posts=[]; policy=Policy(unavailable={'fallback-synthetic'})
    gateway=ModelGateway(settings_for(tmp_path),transport=transport_for(posts),runtime_policy=policy)
    with pytest.raises(ModelResponseError,match='MODEL_INVALID_JSON'):
        finish(gateway,original_request())
    assert posts==['primary.invalid'] and policy.success==[]


def test_revoked_predispatch_preserves_original_error_and_sends_no_fallback(tmp_path, normal_provider_owner):
    posts=[]; revoked=ValueError('synthetic authority revoked')
    def guard():
        if posts:
            raise revoked
    gateway=ModelGateway(settings_for(tmp_path),transport=transport_for(posts))
    with pytest.raises(ValueError) as caught:
        finish(gateway,original_request(pre_dispatch=guard))
    assert caught.value is revoked and posts==['primary.invalid']


def test_policy_denial_never_dispatches(tmp_path, normal_provider_owner):
    posts=[]; denied=ValueError('synthetic purpose denied');policy=Policy(denied=denied)
    gateway=ModelGateway(settings_for(tmp_path),transport=transport_for(posts),runtime_policy=policy)
    with pytest.raises(ValueError) as caught:
        finish(gateway,original_request())
    assert caught.value is denied and posts==[] and policy.success==policy.failure==[]


@pytest.mark.parametrize('content', [INVALID,VALID])
def test_explicit_fallback_validates_before_route_success_without_recursion(tmp_path, normal_provider_owner, content):
    posts=[];policy=Policy();gateway=ModelGateway(settings_for(tmp_path),
        transport=transport_for(posts,fallback=content),runtime_policy=policy)
    if content==INVALID:
        with pytest.raises(ModelResponseError) as caught:
            finish(gateway,original_request(),entry='complete_on_fallback')
        error=caught.value
        assert error.code=='MODEL_INVALID_JSON' and error.fallback_eligible is False
        assert error.response_received is True and (error.prompt_tokens,error.completion_tokens)==(11,4)
        assert type(error.latency_ms)is int
        assert policy.success==[] and policy.failure==[('qwen','fallback-synthetic','MODEL_INVALID_JSON')]
    else:
        result=finish(gateway,original_request(),entry='complete_on_fallback')
        assert result.content==VALID and result.fallback_used is True
        assert policy.success==[('qwen','fallback-synthetic')] and policy.failure==[]
    assert posts==['fallback.invalid']


def test_discarded_consumption_survives_later_fallback_http_failure(tmp_path, normal_provider_owner):
    posts=[];gateway=ModelGateway(settings_for(tmp_path),transport=transport_for(posts,fallback=403))
    with pytest.raises(ModelResponseError) as caught:
        finish(gateway,original_request())
    error=caught.value
    assert error.code=='MODEL_HTTP_403' and error.response_received is True
    assert (error.prompt_tokens,error.completion_tokens)==(10,20)
    assert type(error.latency_ms)is int and posts==['primary.invalid','fallback.invalid']


@pytest.mark.parametrize('valid_fallback', [True,False,403])
def test_real_http_and_database_preserve_fallback_plan_or_consumed_failure(tmp_path, monkeypatch, valid_fallback):
    from home_study_agent.app import create_app
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from test_daily_planning_state_contract import capture_session, plan
    from test_voice_runtime_binding import capture

    monkeypatch.setenv('HSC_DATA_DIR',str(tmp_path));monkeypatch.setenv('HSC_DATABASE_PATH',str(tmp_path/'gateway.db'))
    settings=settings_for(tmp_path);db=Database(settings.database_path);db.initialize()
    privacy=PrivacyLifecycleService(db,settings.data_dir);privacy.initialize();privacy.update_preferences({'cloud_model_text_allowed':True})
    quote='作业说完了，帮我安排计划。';payload=plan('planning.generate',{},quote).payload
    is_valid=valid_fallback is True
    fallback_response=json.dumps(payload,ensure_ascii=False)if is_valid else 403 if valid_fallback==403 else INVALID
    posts=[];gateway=ModelGateway(settings,transport=transport_for(posts,fallback=fallback_response))
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid=capture_session(client)
        saved=client.post(f'/v1/sessions/{sid}/homework',json={'subject':'数学','title':'原创合成练习','expected_minutes':20,'client_action_id':'synthetic-homework'})
        assert saved.status_code==201,saved.text
        voice=capture(client,settings,monkeypatch,sid,quote,key='synthetic-planning-voice')
        response=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,'voice_input_id':voice,'cloud_text_allowed':True,'client_action_id':'synthetic-plan-turn'})
        assert response.status_code==200,response.text
        heard=response.json()
        if is_valid:
            assert heard['action']['capability']=='planning.generate' and heard['action']['source']=='agent_runtime_planner'
            executed=client.post('/v1/agent-turns/'+heard['agent_turn_id']+'/execute')
            assert executed.status_code==200,executed.text
        else:
            assert heard['action']['type']=='none' and heard['action']['source']=='cloud_planner_unavailable'
        assert posts==['primary.invalid','fallback.invalid']*(2 if valid_fallback is False else 1)
        with db.connect() as c:
            calls=c.execute('SELECT *FROM model_call WHERE child_id=?AND session_id=? ORDER BY rowid',('child-primary-4',sid)).fetchall()
            assert len(calls)==(2 if valid_fallback is False else 1) and calls[0]['purpose']=='agent_plan'
            if valid_fallback is False:
                repair=calls[1]
                assert (repair['purpose'],repair['status'],repair['error_code'],repair['output_chars'])==('agent_plan_repair','failed','MODEL_INVALID_JSON',None)
                assert (repair['prompt_tokens'],repair['completion_tokens'])==(21,24)
                assert type(repair['latency_ms'])is int and repair['latency_ms']>=0
                assert json.loads(repair['context_manifest_json'])['planner_repair']=={'source_call_id':calls[0]['call_id'],'validation_error':'planner did not return JSON','attempt':1}
            row=calls[0]
            assert (row['prompt_tokens'],row['completion_tokens'])==((10,20)if valid_fallback==403 else (21,24))
            assert type(row['latency_ms'])is int and row['latency_ms']>=0
            assert c.execute('SELECT COUNT(*)FROM diagnostic_attempt').fetchone()[0]==0
            if is_valid:
                assert (row['status'],row['provider'],row['model'],row['fallback_used'],row['verification_status'])==('completed','qwen','fallback-synthetic',1,'verified')
                assert row['error_code']is None
            else:
                assert row['status']=='failed' and row['error_code']==('MODEL_HTTP_403'if valid_fallback==403 else 'MODEL_INVALID_JSON') and row['verification_status']=='rejected'
                assert c.execute('SELECT COUNT(*)FROM agent_plan WHERE agent_turn_id=?',(heard['agent_turn_id'],)).fetchone()[0]==0
    # Reopen the actual system of record; assertions are not in-memory App results.
    reopened=Database(settings.database_path)
    with reopened.connect() as c:
        assert c.execute('SELECT state FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0]==('plan_review'if is_valid else 'homework_capture')
        plans=c.execute('SELECT status FROM daily_plan WHERE child_id=?AND session_id=?',('child-primary-4',sid)).fetchall()
        assert len(plans)==(1 if is_valid else 0)
        if valid_fallback is False:
            assert c.execute('SELECT COUNT(*)FROM agent_plan').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*)FROM agent_step').fetchone()[0]==0
        if plans:
            assert plans[0]['status']=='active'
        assert c.execute('SELECT COUNT(*)FROM daily_plan WHERE child_id=?',('child-junior-3',)).fetchone()[0]==0
