import asyncio
from pathlib import Path
import json
import httpx
import pytest
from normal_provider_owner import normal_provider_owner
from home_study_agent.config import Settings, validate_tutor_explanation_url, validate_tutor_explanation_model
from home_study_agent.model_gateway import ModelGateway, ModelRequest, ModelResponseError

TRUSTED = 'https://tutor.example.test:7791/v1'
KEY = 'synthetic-tutor-secret'

def request(kind='tutor_whole_explanation'):
    return ModelRequest(request_id='test-1', child_id='synthetic', session_id='synthetic',
        request_type=kind, risk_level='medium', context_manifest={}, provider_preference='qwen',
        policy_version='p1', prompt_version='p1', knowledge_version=None, output_type='tutoring',
        evidence_refs=(), messages=({'role':'user','content':'synthetic'},))

def settings(tmp_path, **kwargs):
    return Settings(data_dir=tmp_path, database_path=tmp_path/'test.db', model_api_key='synthetic-general',
        model_base_url='https://general.example.test/v1', **kwargs)

@pytest.mark.parametrize('value', [None, '', 'http://tutor.example.test/v1',
    'https://user:pw@tutor.example.test/v1', 'https://tutor.example.test/v1?x=1',
    'https://tutor.example.test/v1#fragment', 'https://localhost/v1', 'https://127.0.0.1/v1',
    'https://tutor.example.test:99999/v1', 'https://tutor.example.test:0/v1',
    'https://tutor.example.test:/v1', 'https://0x7f.0.0.1/v1', 'https://tutor.example.test/v1/../v1',
    'https://tutor.example.test\\@attacker.test/v1', 'https://tutor.example.test/%2e%2e/v1'])
def test_invalid_explanation_urls_are_not_allowed(value):
    assert validate_tutor_explanation_url(value) is None

def test_safe_endpoint_and_model_are_canonicalized():
    assert validate_tutor_explanation_url(TRUSTED+'/') == TRUSTED
    assert validate_tutor_explanation_url('https://tutor.example.test:443/v1') == 'https://tutor.example.test/v1'
    assert validate_tutor_explanation_model('deepseek-v4.1-flash') == 'deepseek-v4.1-flash'
    for bad in ('qwen-fast', '', 'deepseek-../x', 'deepseek-v4?x', 'deepseek-a\n'):
        assert validate_tutor_explanation_model(bad) is None

@pytest.mark.parametrize('key,url,model',[(None,TRUSTED,'deepseek-v4.1-flash'),(KEY,None,'deepseek-v4.1-flash'),
    (KEY,'https://localhost/v1','deepseek-v4.1-flash'),(KEY,TRUSTED,'qwen-fast'),('bad key\n',TRUSTED,'deepseek-v4.1-flash')])
def test_teaching_fails_closed_without_complete_trusted_route(tmp_path, normal_provider_owner, key,url,model):
    calls=[]
    def handler(req): calls.append(req); return httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}]})
    gw=ModelGateway(settings(tmp_path,tutor_explanation_api_key=key,tutor_explanation_base_url=url,tutor_explanation_model=model), transport=httpx.MockTransport(handler))
    async def exercise():
        try:
            assert not gw.configured_for('tutor_whole_explanation')
            with pytest.raises(ModelResponseError, match='MODEL_TUTOR_EXPLANATION_CONFIG_REQUIRED'):
                await gw.complete(request())
            with pytest.raises(ModelResponseError):
                await gw.complete_on_fallback(request())
            assert calls == []
        finally:await gw.close()
    asyncio.run(exercise())

def test_teaching_only_uses_exact_configured_endpoint_and_no_redirect(tmp_path, normal_provider_owner):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(302,headers={'location':'https://attacker.example.test/steal'})
    gw=ModelGateway(settings(tmp_path,tutor_explanation_api_key=KEY,tutor_explanation_base_url=TRUSTED,
        tutor_explanation_model='deepseek-v4.1-flash'),transport=httpx.MockTransport(handler))
    async def exercise():
        try:
            assert gw.configured_for('tutor_whole_explanation')
            with pytest.raises(ModelResponseError):await gw.complete(request())
            assert len(calls)==1
            assert str(calls[0].url)==TRUSTED+'/chat/completions'
            assert calls[0].headers['authorization']=='Bearer '+KEY
            payload=json.loads(calls[0].content)
            assert payload['model']=='deepseek-v4.1-flash' and payload['thinking']=={'type':'enabled'}
        finally:await gw.close()
    asyncio.run(exercise())

def test_dedicated_endpoint_model_changes_invalidate_analysis_identity(tmp_path):
    from home_study_agent.mistake_learning import MistakeLearningService
    from types import SimpleNamespace
    a=SimpleNamespace(gateway=SimpleNamespace(settings=settings(tmp_path,tutor_explanation_api_key=KEY,
        tutor_explanation_base_url=TRUSTED,tutor_explanation_model='deepseek-v4.1-flash')))
    b=SimpleNamespace(gateway=SimpleNamespace(settings=settings(tmp_path,tutor_explanation_api_key=KEY,
        tutor_explanation_base_url='https://another.example.test/v1',tutor_explanation_model='deepseek-v4.1-flash')))
    c=SimpleNamespace(gateway=SimpleNamespace(settings=settings(tmp_path,tutor_explanation_api_key=KEY,
        tutor_explanation_base_url=TRUSTED,tutor_explanation_model='deepseek-v4.2-flash')))
    assert len({MistakeLearningService._analysis_context_key(o,None) for o in (a,b,c)})==3


def test_legacy_unidentified_dedicated_analysis_does_not_cross_routes():
    from home_study_agent.mistake_learning import analysis_context_key, analysis_for_graph
    graph={'graph_version':'synthetic-v1','status':'unmatched'}
    route={'provider':'deepseek','endpoint':'https://synthetic.invalid/v1','model':'synthetic-a'}
    row={'analysis_json':'{"summary":"synthetic"}','analysis_context_key':analysis_context_key(graph,True)}
    assert analysis_for_graph(row,graph,route) is None
    assert analysis_for_graph(row,graph,None) is None
    row['analysis_context_key']=analysis_context_key(graph,False)
    assert analysis_for_graph(row,graph,None) is None
    assert analysis_for_graph(row,graph,route) is None
    row['analysis_context_key']=analysis_context_key(graph,route)
    assert analysis_for_graph(row,graph,route)=={'summary':'synthetic'}
    assert analysis_for_graph(row,graph,route | {'model':'synthetic-b'}) is None
    assert analysis_for_graph(row,graph,route | {'endpoint':'https://other.invalid/v1'}) is None


def test_route_identity_requires_the_real_gateway_endpoint_not_just_settings(tmp_path):
    from dataclasses import replace
    from home_study_agent.mistake_learning import _tutor_route_identity
    configured=settings(tmp_path,tutor_explanation_api_key=KEY,tutor_explanation_base_url=TRUSTED,
                        tutor_explanation_model='deepseek-v4.1-flash')
    gateway=ModelGateway(configured,transport=httpx.MockTransport(lambda request:httpx.Response(200,request=request)))
    try:
        assert gateway.configured_for('mistake_learning_analyze') is True
        assert _tutor_route_identity(gateway)['model']=='deepseek-v4.1-flash'
        gateway.settings=replace(configured,tutor_explanation_model='deepseek-synthetic-other')
        assert _tutor_route_identity(gateway) is None
        gateway.settings=replace(configured,tutor_explanation_api_key=None)
        assert _tutor_route_identity(gateway) is None
    finally:
        asyncio.run(gateway.close())

@pytest.mark.parametrize('kind', ['voice_agent_intent', 'paper_scoring_authority_draft', 'learning_outcome_evaluation'])
def test_unrelated_capabilities_keep_general_primary_and_fallback(tmp_path, normal_provider_owner, kind):
    calls=[]
    def handler(req):
        calls.append(req)
        if req.url.host=='general.example.test':
            return httpx.Response(503,request=req)
        return httpx.Response(200,request=req,json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
    gw=ModelGateway(settings(tmp_path,tutor_explanation_api_key=KEY,tutor_explanation_base_url=TRUSTED,
        fallback_model_provider='deepseek',fallback_model_api_key='synthetic-fallback',
        fallback_model_base_url='https://fallback.example.test/v1',fallback_model_name='deepseek-fallback'),
        transport=httpx.MockTransport(handler))
    async def exercise():
        try:
            result=await gw.complete(request(kind))
            assert result.fallback_used and result.provider=='deepseek'
            assert {r.url.host for r in calls} == {'general.example.test','fallback.example.test'}
            assert all(r.headers['authorization']!='Bearer '+KEY for r in calls)
        finally:await gw.close()
    asyncio.run(exercise())

def test_answer_grading_retains_qwen_reasoning_route(tmp_path, normal_provider_owner):
    calls=[]
    def handler(req):
        calls.append(req);return httpx.Response(200,request=req,json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
    gw=ModelGateway(Settings(data_dir=tmp_path,database_path=tmp_path/'d.db',
        model_provider='qwen',model_api_key='synthetic-grading',model_name='qwen3.7-plus',
        tutor_explanation_api_key=KEY,tutor_explanation_base_url=TRUSTED),transport=httpx.MockTransport(handler))
    async def exercise():
        try:
            result=await gw.complete(request('mistake_learning_answer'))
            assert result.provider=='qwen' and len(calls)==1
            assert calls[0].headers['authorization']=='Bearer synthetic-grading'
            assert calls[0].url.host=='dashscope.aliyuncs.com'
        finally:await gw.close()
    asyncio.run(exercise())

@pytest.mark.parametrize('status,expected',[(429,'MODEL_HTTP_429'),(503,'MODEL_HTTP_503')])
def test_dedicated_route_never_retries_or_switches_after_provider_failure(tmp_path,normal_provider_owner,status,expected):
    calls=[]
    def handler(req):calls.append(req);return httpx.Response(status,request=req)
    gw=ModelGateway(settings(tmp_path,tutor_explanation_api_key=KEY,tutor_explanation_base_url=TRUSTED,
        fallback_model_provider='deepseek',fallback_model_api_key='synthetic-fallback',
        fallback_model_base_url='https://fallback.example.test/v1',fallback_model_name='deepseek-fallback'),transport=httpx.MockTransport(handler))
    async def exercise():
        try:
            with pytest.raises(ModelResponseError,match=expected):await gw.complete(request())
            assert len(calls)==1 and calls[0].url.host=='tutor.example.test'
        finally:await gw.close()
    asyncio.run(exercise())

def test_endpoint_repr_does_not_disclose_credentials_or_private_origin(tmp_path):
    gw=ModelGateway(settings(tmp_path,tutor_explanation_api_key=KEY,tutor_explanation_base_url=TRUSTED))
    try:
        description=repr(gw._explanation_endpoint)
        assert KEY not in description and TRUSTED not in description
    finally:asyncio.run(gw.close())


def test_environment_parsing_does_not_supply_or_log_private_endpoint_by_default(tmp_path, monkeypatch):
    for name in ('HSC_TUTOR_EXPLANATION_BASE_URL', 'HSC_TUTOR_EXPLANATION_API_KEY', 'HSC_TUTOR_EXPLANATION_MODEL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HSC_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH',str(tmp_path/'test.db'))
    empty=Settings.from_environment()
    assert empty.tutor_explanation_base_url is None and empty.tutor_explanation_api_key is None
    monkeypatch.setenv('HSC_TUTOR_EXPLANATION_BASE_URL',TRUSTED+'/')
    monkeypatch.setenv('HSC_TUTOR_EXPLANATION_API_KEY',KEY)
    configured=Settings.from_environment()
    assert configured.tutor_explanation_base_url==TRUSTED and configured.tutor_explanation_api_key==KEY
    assert TRUSTED not in repr(configured) and KEY not in repr(configured)

@pytest.mark.parametrize('kind,expected', [('tutor_hint_level_1','MODEL_TUTOR_EXPLANATION_CONFIG_REQUIRED'),
    ('mistake_learning_analyze','MODEL_MISTAKE_REASONING_CONFIG_REQUIRED'),
    ('mistake_learning_practice','MODEL_MISTAKE_REASONING_CONFIG_REQUIRED')])
def test_partial_dedicated_settings_cannot_route_through_general_or_fallback(tmp_path,normal_provider_owner,kind,expected):
    calls=[]
    def handler(req):calls.append(req);return httpx.Response(200,request=req,json={'choices':[{'message':{'content':'{}'}}]})
    gw=ModelGateway(settings(tmp_path,tutor_explanation_api_key=KEY,tutor_explanation_base_url=None,
        fallback_model_provider='deepseek',fallback_model_api_key='synthetic-fallback',
        fallback_model_base_url='https://fallback.example.test/v1',fallback_model_name='deepseek-fallback'),transport=httpx.MockTransport(handler))
    async def exercise():
        try:
            assert not gw.configured_for(kind)
            with pytest.raises(ModelResponseError,match=expected):await gw.complete(request(kind))
            with pytest.raises(ModelResponseError):await gw.complete_on_provider('qwen',request(kind))
            with pytest.raises(ModelResponseError):await gw.complete_on_fallback(request(kind))
            assert calls==[]
        finally:await gw.close()
    asyncio.run(exercise())
