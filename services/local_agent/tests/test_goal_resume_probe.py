"""Exercise the opt-in journey with bounded fake transport before any paid call."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('goal_probe', ROOT/'scripts/probe-goal-resume-journey.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize('role', ['control', 'unknown'])
def test_real_goal_journey_and_single_post(tmp_path, role):
    from home_study_agent.config import Settings
    db, receipt = tmp_path/'goal.db', tmp_path/'seed.json'
    result = subprocess.run([sys.executable,str(ROOT/'scripts/seed-goal-check-native.py'),str(db),str(receipt)],
        env={'PATH':'/usr/bin:/bin','HOME':'/private/tmp'},capture_output=True,text=True)
    assert result.returncode == 0, result.stderr[-1000:]
    settings = Settings(data_dir=tmp_path,database_path=db,maintenance_enabled=False,
        model_provider='deepseek',model_base_url='https://api.deepseek.com',model_api_key='synthetic-key',
        model_name='deepseek-flash',voice_intent_model_name='deepseek-flash')
    seen=[]
    def respond(request):
        seen.append(request)
        return httpx.Response(200,json={'model':'deepseek-flash','usage':{'prompt_tokens':3,'completion_tokens':4},
            'choices':[{'message':{'content':json.dumps({'role':role,'control':'task.resume' if role=='control' else None,
                'quote':probe.QUOTE,'confidence':.99,
                **({'selector':{'subject':None,'task_kind':None,'relative_order':None}} if role=='control' else {})})}}]})
    output=probe.journey(settings,json.loads(receipt.read_text()),httpx.MockTransport(respond))
    assert output['passed'] is (role=='control'), output
    assert len(seen)==output['posts']==1
    assert output['rejected']==0
    assert output['usage']['completion_tokens']==4
    assert 'synthetic-key' not in json.dumps(output)
    if role=='control':
        assert output['official_model'] is False


@pytest.mark.parametrize('fault',['second_post','private_field','private_system','wrong_host'])
def test_transport_refuses_out_of_scope_without_sending(fault):
    sent=[]
    wire=probe.OneControlPost('deepseek-flash',['PRIVATE'],httpx.MockTransport(lambda r: sent.append(r)))
    body={'model':'deepseek-flash','max_tokens':768,'messages':[
        {'role':'system','content':'只分类当前原话，不执行、不评分、不回答题目。'},
        {'role':'user','content':json.dumps({'transcript':probe.QUOTE,'state':{'has_paused_task':True}})}]}
    if fault=='second_post':wire.posts=1
    if fault=='private_field':body['messages'][1]['content']=json.dumps({'transcript':probe.QUOTE,'state':{},'title':'PRIVATE'})
    if fault=='private_system':body['messages'][0]['content']+='PRIVATE'
    url='https://elsewhere.invalid/chat/completions' if fault=='wrong_host' else 'https://api.deepseek.com/chat/completions'
    with pytest.raises(RuntimeError,match='SYNTHETIC_PROBE_SCOPE_REJECTED'):
        asyncio.run(wire.handle_async_request(httpx.Request('POST',url,json=body)))
    assert not sent and wire.rejected==1
