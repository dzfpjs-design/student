"""Every physical attempt must retain the caller's current local authority."""
from normal_provider_owner import normal_provider_owner
import asyncio
import json
from dataclasses import asdict, replace

import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_model_gateway import request_for


@pytest.mark.parametrize('entry',['complete','complete_on_provider','complete_on_fallback'])
@pytest.mark.parametrize('failure',[503,429,'network'])
def test_revoked_authority_stops_internal_retry_for_every_gateway_entry(tmp_path,entry,failure, normal_provider_owner):
    checks,posts = [],[]
    def authorize():
        checks.append(True)
        if posts:
            raise ValueError('isolated authority revoked')
    def transport(request):
        posts.append(request)
        if failure=='network':
            raise httpx.ConnectError('synthetic disconnect',request=request)
        return httpx.Response(failure,request=request)
    settings = Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',model_provider='deepseek',
        model_api_key='synthetic-not-secret',model_base_url='https://primary.invalid',
        fallback_model_provider='qwen',fallback_model_api_key='synthetic-fallback',
        fallback_model_base_url='https://fallback.invalid',fallback_model_name='isolated')
    gateway = ModelGateway(settings,transport=httpx.MockTransport(transport))
    request = replace(request_for([{'role':'user','content':'synthetic permitted original'}]),pre_dispatch=authorize)
    async def run():
        try:
            method = getattr(gateway,entry)
            with pytest.raises(ValueError,match='isolated authority revoked'):
                await (method('deepseek',request) if entry=='complete_on_provider' else method(request))
        finally:
            await gateway.close()
    asyncio.run(run())
    assert len(posts)==1 and len(checks)==2
    payload = json.loads(posts[0].read())
    assert not {'pre_dispatch','context_manifest','evidence_refs','child_id','session_id'} & set(payload)
    assert 'authorize' not in repr(request)
    assert 'pre_dispatch' not in asdict(request)
    json.dumps(asdict(request))


def test_route_fallback_must_recheck_before_sending_to_second_endpoint(tmp_path, normal_provider_owner):
    posts = []
    def authorize():
        if posts:
            raise ValueError('purpose revoked before fallback')
    def transport(request):
        posts.append(request)
        return httpx.Response(403,request=request)
    gateway = ModelGateway(Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',
        model_api_key='synthetic',model_base_url='https://primary.invalid',
        fallback_model_provider='qwen',fallback_model_api_key='synthetic-fallback',
        fallback_model_base_url='https://fallback.invalid',fallback_model_name='isolated'),
        transport=httpx.MockTransport(transport))
    request = replace(request_for([{'role':'user','content':'synthetic original'}]),pre_dispatch=authorize)
    async def run():
        try:
            with pytest.raises(ValueError,match='purpose revoked before fallback'):
                await gateway.complete(request)
        finally:
            await gateway.close()
    asyncio.run(run())
    assert len(posts)==1 and posts[0].url.host=='primary.invalid'
