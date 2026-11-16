"""Scorer identity reflects the actual transport, not an inferred stable revision."""
from normal_provider_owner import normal_provider_owner
import asyncio
import hashlib
import json

import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_model_gateway import request_for


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


@pytest.mark.parametrize('reported',['served-2026-09-08',None,{'invalid':'private'},'not a model\nprivate'])
def test_actual_transport_identity_is_local_bounded_and_not_revision_proof(tmp_path,reported, normal_provider_owner):
    sent=[]
    def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,json={'model':reported,'system_fingerprint':'fp-123',
            'choices':[{'message':{'content':'Synthetic feedback'}}]})
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'isolated.db',model_api_key='synthetic-8ad53a5a01da34ae965e',
        model_provider='deepseek',model_base_url='https://scorer.invalid/v1',model_name='requested-alias')
    gateway=ModelGateway(settings,transport=httpx.MockTransport(transport))
    request=request_for([{'role':'system','content':'Private system instructions'},
        {'role':'user','content':'Private child answer'}],request_type='learning_outcome_evaluation')
    async def run():
        try: return await gateway.complete(request)
        finally: await gateway.close()
    result=asyncio.run(run())
    proof=result.scorer_provenance
    assert proof['requested_model']=='requested-alias'
    assert proof['reported_model']==(reported if reported=='served-2026-09-08' else None)
    # Fingerprint the actual wire prompt, including the gateway's JSON-mode
    # instruction, while leaving the caller's source snapshot untouched.
    assert sent[0]['messages']==[
        {'role':'system','content':'Return only a valid JSON object.'},
        {'role':'system','content':'Private system instructions'},
        {'role':'user','content':'Private child answer'}]
    assert list(request.messages)==[
        {'role':'system','content':'Private system instructions'},
        {'role':'user','content':'Private child answer'}]
    assert proof['system_prompt_sha256']==digest(['Return only a valid JSON object.','Private system instructions'])
    assert proof['parameters_sha256']==digest({k:v for k,v in sent[0].items() if k!='messages'})
    assert proof['provider_system_fingerprint']=='fp-123'
    assert proof['immutable_revision_verified'] is False
    assert proof['cloud_transmission_allowed'] is False
    encoded=json.dumps(proof)
    for secret in ('Private system instructions','Private child answer','synthetic-8ad53a5a01da34ae965e','scorer.invalid'):
        assert secret not in encoded


def test_fallback_records_its_own_model_prompt_parameters_and_endpoint(tmp_path, normal_provider_owner):
    calls=[]
    def transport(request):
        calls.append((request.url.host,json.loads(request.content)))
        if request.url.host=='primary.invalid': return httpx.Response(503,json={'error':'synthetic unavailable'})
        return httpx.Response(200,json={'model':'served-fallback-001','choices':[{'message':{'content':'Feedback'}}]})
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'isolated.db',model_api_key='synthetic-1eb90d4a288b922179cc',
        model_provider='qwen',model_base_url='https://primary.invalid/v1',model_name='primary-alias',
        fallback_model_api_key='synthetic-a288df15c5fcbca80827',fallback_model_provider='deepseek',
        fallback_model_base_url='https://fallback.invalid/v1',fallback_model_name='fallback-alias')
    gateway=ModelGateway(settings,transport=httpx.MockTransport(transport))
    async def run():
        try: return await gateway.complete(request_for([{'role':'system','content':'Rubric v1'},
            {'role':'user','content':'Synthetic answer'}],request_type='learning_outcome_evaluation'))
        finally: await gateway.close()
    result=asyncio.run(run())
    assert [host for host,_ in calls]==['primary.invalid','primary.invalid','fallback.invalid']
    assert result.fallback_used
    proof=result.scorer_provenance
    assert proof['provider']=='deepseek' and proof['requested_model']=='fallback-alias'
    assert proof['reported_model']=='served-fallback-001'
    assert proof['endpoint_scope_sha256']==digest('https://fallback.invalid/v1')
    assert proof['parameters_sha256']==digest({k:v for k,v in calls[-1][1].items() if k!='messages'})
    assert not proof['immutable_revision_verified']
