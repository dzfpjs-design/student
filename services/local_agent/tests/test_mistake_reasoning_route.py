"""Finite configured reasoning route; all traffic is synthetic MockTransport."""
import asyncio
import json
import os
import socket
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import home_study_agent
import httpx
import pytest
from normal_provider_owner import normal_provider_owner

from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway, ModelRequest, ModelResponseError

KINDS=('analyze','practice','coach','answer','verify')
FIXED='MODEL_MISTAKE_REASONING_CONFIG_REQUIRED'
OFFICIAL='https://dashscope.aliyuncs.com/compatible-mode/v1'
TUTOR='https://tutor.example.test/v1'

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts=[]
    def deny(*a,**kw):attempts.append(True);raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)
    yield
    assert attempts==[]


def request(kind='analyze'):
    return ModelRequest(request_id='public-'+kind,child_id='synthetic-child',session_id='synthetic-session',
        request_type='mistake_learning_'+kind,risk_level='medium',context_manifest={},provider_preference='primary',
        policy_version='public-finite-route',prompt_version='public-finite-v1',knowledge_version=None,
        output_type='diagnosis',evidence_refs=(),messages=({'role':'user','content':'Public JSON learning check.'},))


def settings(tmp_path,**kw):
    return replace(Settings(data_dir=tmp_path,database_path=tmp_path/'public.db',model_provider='deepseek',
        model_api_key='synthetic-ffccfae04da11132e49b',model_base_url='https://api.deepseek.com',model_name='deepseek-v4-pro',
        fallback_model_provider='qwen',fallback_model_api_key='synthetic-230f97721731ed8330a4',fallback_model_base_url=OFFICIAL,
        fallback_model_name='qwen3.7-plus', tutor_explanation_api_key='synthetic-teaching-key',
        tutor_explanation_base_url=TUTOR),**kw)


def run(config,req,handler,entry='complete',provider='qwen'):
    gateway=ModelGateway(config,transport=httpx.MockTransport(handler))
    async def execute():
        try:
            method=getattr(gateway,entry)
            return await (method(provider,req) if entry=='complete_on_provider' else method(req))
        finally:await gateway.close()
    return asyncio.run(execute())


def response(req,finish='stop',content='{"ok":true}',reasoning='PRIVATE_REASONING_MUST_NOT_LEAK'):
    return httpx.Response(200,request=req,json={'choices':[{'finish_reason':finish,'message':{'content':content,'reasoning_content':reasoning}}],
        'usage':{'prompt_tokens':20,'completion_tokens':30}})


@pytest.mark.parametrize('kind',KINDS)
def test_learning_routes_use_dedicated_deepseek_except_qwen_grading(tmp_path,normal_provider_owner,kind):
    posts=[]
    def transport(req):posts.append(req);return response(req)
    result=run(settings(tmp_path),request(kind),transport)
    assert len(posts)==1
    payload=json.loads(posts[0].content)
    if kind == 'answer':
        assert str(posts[0].url)==OFFICIAL+'/chat/completions'
        assert payload['model']=='qwen3.7-plus'
        assert payload['enable_thinking'] is True and payload['thinking_budget']==256
        assert payload['max_tokens']==6144 and result.provider=='qwen' and result.fallback_used is True
        assert 'thinking' not in payload and 'reasoning_effort' not in payload
    else:
        assert str(posts[0].url)==TUTOR+'/chat/completions'
        assert payload['model']=='deepseek-v4.1-flash'
        assert payload['thinking']=={'type':'enabled'} and payload['reasoning_effort']=='high'
        assert payload['max_tokens']==12288 and result.provider=='deepseek' and result.fallback_used is False
        assert 'thinking_budget' not in payload
    assert payload['response_format']=={'type':'json_object'} and 'temperature' not in payload
    assert result.content=='{"ok":true}' and 'PRIVATE_REASONING_MUST_NOT_LEAK' not in json.dumps(asdict(result))


def test_primary_configured_qwen_stays_primary_and_fallback_only_configuration_works(tmp_path,normal_provider_owner):
    posts=[]
    def transport(req):posts.append(req);return response(req)
    primary=run(settings(tmp_path,model_provider='qwen',model_base_url=OFFICIAL,model_name='qwen3.7-plus'),request('answer'),transport)
    assert primary.provider=='qwen' and primary.fallback_used is False
    fallback_only=run(settings(tmp_path,model_api_key=None),request('answer'),transport)
    assert fallback_only.provider=='qwen' and fallback_only.fallback_used is True
    assert len(posts)==2


@pytest.mark.parametrize('changes',[
 {'fallback_model_api_key':None},
 {'fallback_model_base_url':'https://dashscope.aliyuncs.com.evil.invalid/compatible-mode/v1'},
 {'fallback_model_name':'qwen2.5-plus'},
 {'fallback_model_name':'qwen3.7-plus-instruct'},
 {'fallback_model_name':'qwen3.7-plus-thinking'},
 {'fallback_model_name':'qwen3-asr-flash'},
])
def test_missing_or_nonhybrid_qwen_fails_before_transport(tmp_path,normal_provider_owner,changes):
    posts=[]
    def transport(req):posts.append(req);return response(req)
    with pytest.raises(ModelResponseError) as failure:run(settings(tmp_path,**changes),request('answer'),transport)
    assert failure.value.code==FIXED and failure.value.fallback_eligible is False
    assert posts==[]


@pytest.mark.parametrize('entry,provider', [('complete_on_provider','deepseek'),('complete_on_fallback','qwen')])
def test_explicit_entrypoints_cannot_bypass_finite_route(tmp_path,normal_provider_owner,entry,provider):
    posts=[]
    def transport(req):posts.append(req);return response(req)
    config=settings(tmp_path)
    if entry=='complete_on_fallback':config=replace(config,fallback_model_provider='deepseek',fallback_model_base_url='https://api.deepseek.com',fallback_model_name='deepseek-v4-pro')
    with pytest.raises(ModelResponseError) as failure:run(config,request('answer'),transport,entry,provider)
    assert failure.value.code==FIXED
    assert posts==[]


@pytest.mark.parametrize('entry',['complete','complete_on_provider','complete_on_fallback'])
@pytest.mark.parametrize('fault',[503,'network'])
def test_finite_requests_never_retry_or_fall_back_after_physical_attempt(tmp_path,normal_provider_owner,entry,fault):
    posts=[]
    def transport(req):
        posts.append(req)
        if fault=='network':raise httpx.ConnectError('public disconnected',request=req)
        return httpx.Response(fault,request=req)
    with pytest.raises(ModelResponseError):run(settings(tmp_path),request('answer'),transport,entry)
    assert len(posts)==1
    assert posts[0].url.host=='dashscope.aliyuncs.com'


def test_old_tutor_voice_and_nonofficial_payloads_unchanged():
    official=SimpleNamespace(provider='deepseek',base_url='https://api.deepseek.com',model='deepseek-v4-pro')
    other=SimpleNamespace(provider='qwen',base_url=OFFICIAL,model='qwen3.7-plus')
    messages=[{'role':'user','content':'Public JSON.'}]
    for kind in ('tutor_whole_explanation','tutor_whole_explanation_check'):
        payload=ModelGateway.request_payload(official,messages,kind)
        assert payload['thinking']=={'type':'enabled'} and payload['reasoning_effort']=='high'
        assert payload['max_tokens']==4096 and 'thinking_budget' not in payload and 'temperature' not in payload
    for kind in ('voice_agent_intent','voice_interaction','companion_dialogue','agent_plan'):
        payload=ModelGateway.request_payload(official,messages,kind)
        assert payload['thinking']=={'type':'disabled'} and 'reasoning_effort' not in payload and 'thinking_budget' not in payload
    qwen=ModelGateway.request_payload(other,messages,'tutor_whole_explanation')
    assert qwen['enable_thinking'] is False and 'thinking_budget' not in qwen and qwen['max_tokens']==4096
    assert messages==[{'role':'user','content':'Public JSON.'}]


@pytest.mark.parametrize('kind',KINDS)
def test_length_rejects_even_valid_json_without_reasoning_fallback(tmp_path,normal_provider_owner,kind):
    posts=[]
    def transport(req):posts.append(req);return response(req,finish='length')
    with pytest.raises(ModelResponseError) as failure:run(settings(tmp_path),request(kind),transport)
    assert failure.value.code=='MODEL_OUTPUT_TRUNCATED'
    assert len(posts)==1
    assert 'PRIVATE_REASONING' not in str(failure.value)


def test_reasoning_only_response_never_becomes_child_content(tmp_path,normal_provider_owner):
    posts=[]
    def transport(req):posts.append(req);return response(req,content='')
    with pytest.raises(ModelResponseError) as failure:run(settings(tmp_path),request('coach'),transport)
    assert failure.value.code=='MODEL_EMPTY_RESPONSE'
    assert len(posts)==1


def test_nonfinite_explicit_provider_and_standard_voice_routes_keep_original_models(tmp_path,normal_provider_owner):
    posts=[]
    def transport(req):posts.append(json.loads(req.content));return response(req)
    config=settings(tmp_path,voice_intent_model_name='public-existing-low-latency')
    voice=replace(request(),request_type='voice_interaction')
    run(config,voice,transport,entry='complete_on_provider',provider='deepseek')
    run(config,voice,transport)
    assert [p['model'] for p in posts]==['deepseek-v4-pro','public-existing-low-latency']
    assert all(p['thinking']=={'type':'disabled'} and 'thinking_budget' not in p and p['max_tokens']==500 for p in posts)
