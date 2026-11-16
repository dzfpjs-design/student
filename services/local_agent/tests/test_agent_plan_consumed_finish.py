"""Mock transport and real local HTTP/SQLite. No provider requests."""
import asyncio
import json
import httpx
import pytest
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway, ModelResponseError, ModelNotConfiguredError, ModelResult
from test_model_gateway import request_for
from test_app import client_for
from normal_provider_owner import normal_provider_owner

@pytest.mark.parametrize('reason,code',[('length','MODEL_OUTPUT_TRUNCATED'),('content_filter','MODEL_OUTPUT_TERMINATED'),('tool_calls','MODEL_OUTPUT_TERMINATED')])
@pytest.mark.parametrize('content',['','{"partial":'])
def test_agent_plan_consumed_end_never_retries_or_falls_back(tmp_path,reason,code,content,normal_provider_owner):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,request=request,json={'choices':[{'finish_reason':reason,'message':{'content':content,'reasoning_content':'PRIVATE_REASONING'}}],'usage':{'prompt_tokens':7903,'completion_tokens':1600}})
    gateway=ModelGateway(Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',model_api_key='synthetic',fallback_model_provider='deepseek',fallback_model_api_key='synthetic',fallback_model_base_url='https://fallback.invalid',fallback_model_name='synthetic'),transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelResponseError) as caught:
            asyncio.run(gateway.complete(request_for([{'role':'user','content':'Return JSON only'}],request_type='agent_plan')))
    finally:asyncio.run(gateway.close())
    e=caught.value
    assert e.code==code and e.response_received and not e.fallback_eligible
    assert (e.prompt_tokens,e.completion_tokens)==(7903,1600) and type(e.latency_ms) is int and len(calls)==1
    assert 'PRIVATE' not in str(e) and not hasattr(e,'content')

def test_stop_content_and_original_generation_parameters_remain(tmp_path,normal_provider_owner):
    calls=[]
    def handler(request):
        calls.append(json.loads(request.read()))
        return httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':'{"public":true}','reasoning_content':'PRIVATE_REASONING'}}]})
    gateway=ModelGateway(Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',model_api_key='synthetic'),transport=httpx.MockTransport(handler))
    try:value=asyncio.run(gateway.complete(request_for([{'role':'user','content':'Return JSON only'}],request_type='agent_plan')))
    finally:asyncio.run(gateway.close())
    assert value.content=='{"public":true}' and value.reply_provenance['finish_reason']=='stop'
    assert calls[0]['max_tokens']==1600 and calls[0]['temperature']==0 and calls[0]['response_format']=={'type':'json_object'} and len(calls)==1

class FailedPlanner:
    configured=True
    def __init__(self,error,repair=False):self.error,self.repair,self.requests=error,repair,[]
    async def complete(self,request):
        self.requests.append(request)
        if self.repair and len(self.requests)==1:return ModelResult(content='not JSON',provider='synthetic',model='synthetic',latency_ms=12,prompt_tokens=7,completion_tokens=8)
        raise self.error
    async def close(self):pass

def turn(client):
    r=client.post('/v1/sessions',json={'child_id':'child-primary-4','client_action_id':'consumed-session'});assert r.status_code==201
    sid=r.json()['session_id'];r=client.post('/v1/sessions/'+sid+'/agent-turn',json={'transcript':'我今天还有数学作业要做。','client_action_id':'consumed-turn','cloud_text_allowed':True});assert r.status_code==200
    assert r.json()['action']['source']=='cloud_planner_unavailable' and r.json()['action']['type']=='none'
    return sid

COLUMNS='purpose,status,error_code,latency_ms,output_chars,prompt_tokens,completion_tokens,verification_status,safety_status'
@pytest.mark.parametrize('code',['MODEL_OUTPUT_TRUNCATED','MODEL_OUTPUT_TERMINATED'])
@pytest.mark.parametrize('repair',[False,True])
def test_real_http_sqlite_consumed_accounting_reopens_without_extra_repair(tmp_path,code,repair):
    planner=FailedPlanner(ModelResponseError(code,response_received=True,prompt_tokens=8204,completion_tokens=216,latency_ms=1953),repair)
    expected={'purpose':'agent_plan_repair' if repair else 'agent_plan','status':'failed','error_code':code,'latency_ms':1953,'output_chars':None,'prompt_tokens':8204,'completion_tokens':216,'verification_status':'rejected','safety_status':'degraded'}
    with client_for(tmp_path,model_gateway=planner,agent_runtime_enabled=True) as client:
        sid=turn(client)
        with client.app.state.agent_orchestrator.database.connect() as c:
            rows=[dict(x) for x in c.execute('SELECT '+COLUMNS+' FROM model_call WHERE session_id=? ORDER BY rowid',(sid,))]
            assert len(rows)==(2 if repair else 1)
            if repair:assert rows[0]['error_code']=='MODEL_INVALID_AGENT_PLAN' and (rows[0]['prompt_tokens'],rows[0]['completion_tokens'])==(7,8)
            assert rows[-1]==expected
            assert c.execute('SELECT COUNT(*) FROM agent_step').fetchone()[0]==0 and c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0]==0
        assert len(planner.requests)==(2 if repair else 1) and all(x.request_type=='agent_plan' for x in planner.requests)
    with client_for(tmp_path,model_gateway=planner,agent_runtime_enabled=True) as reopened:
        with reopened.app.state.agent_orchestrator.database.connect() as c:assert dict(c.execute('SELECT '+COLUMNS+' FROM model_call WHERE session_id=? ORDER BY rowid DESC LIMIT 1',(sid,)).fetchone())==expected
        assert len(planner.requests)==(2 if repair else 1)

@pytest.mark.parametrize('error',[ModelResponseError('PRIVATE_UNKNOWN',response_received=True,prompt_tokens=8,completion_tokens=9,latency_ms=10),ModelResponseError('MODEL_OUTPUT_TRUNCATED',response_received=False,prompt_tokens=8,completion_tokens=9,latency_ms=10),ModelNotConfiguredError(), *[ModelResponseError(code,response_received=True,prompt_tokens=8,completion_tokens=9,latency_ms=10) for code in ('MODEL_HTTP_200','MODEL_HTTP_600','MODEL_HTTP_403_PRIVATE')]])
def test_unproven_or_unknown_error_keeps_original_generic_accounting(tmp_path,error):
    planner=FailedPlanner(error)
    with client_for(tmp_path,model_gateway=planner,agent_runtime_enabled=True) as client:
        sid=turn(client)
        with client.app.state.agent_orchestrator.database.connect() as c:assert tuple(c.execute('SELECT error_code,latency_ms,output_chars,prompt_tokens,completion_tokens FROM model_call WHERE session_id=?',(sid,)).fetchone())==('MODEL_AGENT_PLAN_FAILED',None,0,None,None)
        assert len(planner.requests)==1


@pytest.mark.parametrize('transport_error,code,physical_fallback_calls',[
    (httpx.ReadTimeout,'MODEL_TIMEOUT',1),
    (httpx.ConnectError,'MODEL_NETWORK_ERROR',2),
])
def test_real_gateway_later_transport_failure_preserves_only_known_primary_consumption(tmp_path,transport_error,code,physical_fallback_calls):
    from home_study_agent.database import Database
    from test_agent_plan_json_fallback import settings_for, transport_for

    posts=[]
    original=transport_for(posts)
    def handler(request):
        if request.url.host=='fallback.invalid':
            posts.append(request.url.host)
            raise transport_error('synthetic transport failure',request=request)
        return original.handler(request)
    gateway=ModelGateway(settings_for(tmp_path),transport=httpx.MockTransport(handler))
    with client_for(tmp_path,model_gateway=gateway,agent_runtime_enabled=True)as client:
        sid=turn(client)
        with client.app.state.agent_orchestrator.database.connect()as c:
            rows=c.execute('SELECT '+COLUMNS+' FROM model_call WHERE session_id=?',(sid,)).fetchall()
            assert len(rows)==1
            expected=dict(rows[0])
            assert expected['purpose']=='agent_plan'and expected['status']=='failed'and expected['error_code']==code
            assert type(expected['latency_ms'])is int and expected['latency_ms']>=0
            assert expected['output_chars']is None
            assert (expected['prompt_tokens'],expected['completion_tokens'])==(10,20)
            assert (expected['verification_status'],expected['safety_status'])==('rejected','degraded')
            assert c.execute('SELECT COUNT(*)FROM agent_plan').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*)FROM daily_plan').fetchone()[0]==0
        assert posts==['primary.invalid']+['fallback.invalid']*physical_fallback_calls
    # Read the actual persisted model_call through a new real Database, without sending again.
    with Database(tmp_path/'test.db').connect()as c:
        assert dict(c.execute('SELECT '+COLUMNS+' FROM model_call WHERE session_id=?',(sid,)).fetchone())==expected
        assert c.execute('SELECT COUNT(*)FROM agent_plan').fetchone()[0]==0
    assert posts==['primary.invalid']+['fallback.invalid']*physical_fallback_calls
