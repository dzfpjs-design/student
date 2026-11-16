"""Opt-in single-generation synthetic feedback through the production gateway."""
from dataclasses import replace
import json
import os
import subprocess

import httpx
import pytest

from home_study_agent.model_gateway import ModelGateway
from test_diagnostic_prompt_http import environment
from test_document_goal_http import journey, BODY
from test_document_revisit import next_day, offer, say


@pytest.mark.skipif(os.environ.get('HSC_RUN_REVISIT_PAID_PROBE')!='1',reason='Explicit paid synthetic probe only')
def test_one_official_feedback_with_real_gateway_and_local_persistence(journey,monkeypatch,tmp_path):
    j=journey
    next_day(j,monkeypatch)
    offer(j)
    say(j,monkeypatch,'现在我自己试着回答','start','live-start')
    text='十二支红笔再加八支蓝笔，所以一共二十支'
    turn=say(j,monkeypatch,text,'answer','live-answer',execute=False)
    key=subprocess.run(['/usr/bin/security','find-generic-password','-s','hsc-deepseek-api-key','-w',
        os.path.expanduser('~/Library/Keychains/login.keychain-db')],capture_output=True,text=True,check=True).stdout.strip()
    with httpx.Client(base_url='https://api.deepseek.com',headers={'Authorization':'Bearer '+key},
            follow_redirects=False,timeout=20) as client:
        response=client.get('/models')
        assert response.status_code==200, str(response.status_code)
        inventory={m['id'] for m in response.json()['data']}
    model=next((m for m in ('deepseek-flash','deepseek-v4-flash','deepseek-chat') if m in inventory),None)
    assert model is not None
    settings=replace(j['settings'],model_provider='deepseek',model_base_url='https://api.deepseek.com',
        model_api_key=key,model_name=model,voice_intent_model_name=None,
        fallback_model_provider=None,fallback_model_api_key=None,fallback_model_base_url=None,fallback_model_name=None)
    official=ModelGateway(settings)
    results=[]
    async def generate(request):
        assert request.prompt_version=='document_revisit_formative_v1' and not results
        packet=json.loads(request.messages[-1]['content'])
        assert packet['question']==BODY and packet['response']==text
        value=await official.complete(replace(request,provider_preference='deepseek',pre_dispatch=request.pre_dispatch))
        results.append(value)
        return value
    monkeypatch.setattr(j['gateway'],'complete',generate)
    response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code==200,str(response.status_code)
    executed=response.json()['execution']
    report={'synthetic_only':True,'household_data_read':False,'generation_calls':len(results),
        'passed':bool(executed.get('tutor_turn')),'error':executed.get('feedback_error')}
    if results:
        result=results[0]
        report.update(model=result.model,provider=result.provider,latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,completion_tokens=result.completion_tokens,raw_response=result.content)
    (tmp_path/'live-feedback-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    assert report['passed'],report['error']
    assert executed['tutor_turn']['question']==BODY
    assert executed['tutor_turn']['output_envelope']['verification_status']=='unverified'
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?',(j['sid'],)).fetchone()[0]==1
