"""Opt-in synthetic intent probe; no family data or executable model output."""
import json
import os
import subprocess

import httpx
import pytest

from home_study_agent.document_revisit import PROMPT,parse


@pytest.mark.skipif(os.environ.get('HSC_RUN_FOLLOWUP_PAID_PROBE')!='1',reason='Explicit paid synthetic probe only')
@pytest.mark.parametrize('phase',['check_feedback','target_choice'])
def test_official_semantics_distinguishes_review_actions(tmp_path,phase):
    key=subprocess.run(['/usr/bin/security','find-generic-password','-s','hsc-deepseek-api-key','-w',
        os.path.expanduser('~/Library/Keychains/login.keychain-db')],capture_output=True,text=True)
    assert key.returncode==0,'Official credential unavailable'
    cases=[('这道题先回顾到这里','finish_check'),('今天不学了，结束今天的学习','other_request'),
        ('谢谢，我听懂了','unclear'),('再给我一道不同的新题','independent_check'),
        ('刚才那道新题为什么这样算','help'),('关闭高拍仪','other_request'),
        ('刚才答案说错了，我想修改已保存的答案','other_request'),('刚才那题我答得怎么样','status')]
    if phase=='target_choice':
        cases=[('我选第一个','select_1'),('那就第二项吧','select_2'),('选第三项试试看','select_3'),
            ('都不对先不选了','resume_decline'),('就这唯一的一项吧','start'),
            ('我还没想好','unclear'),('把高拍仪关掉','other_request'),('今天不学了','other_request')]
    report={'synthetic_only':True,'results':[],'prompt_tokens':0,'completion_tokens':0}
    with httpx.Client(base_url='https://api.deepseek.com',headers={'Authorization':'Bearer '+key.stdout.strip()},
            follow_redirects=False,timeout=30) as client:
        inventory=client.get('/models')
        assert inventory.status_code==200,str(inventory.status_code)
        models={r['id'] for r in inventory.json()['data']}
        model=next((m for m in ('deepseek-flash','deepseek-v4-flash','deepseek-chat') if m in models),None)
        assert model,'No supported official model found'
        for text,expected in cases:
            response=client.post('/chat/completions',json={'model':model,'temperature':0,'max_tokens':400,
                'thinking':{'type':'disabled'},'response_format':{'type':'json_object'},'messages':[
                    {'role':'system','content':PROMPT},{'role':'user','content':json.dumps({
                        'transcript':text,'phase':phase,'current_time':'2026-09-13T18:00:00+08:00',
                        'timezone':'Asia/Shanghai'},ensure_ascii=False)}]})
            assert response.status_code==200,str(response.status_code)
            data=response.json()
            actual=parse(data['choices'][0]['message']['content'],text,'2026-09-13T18:00:00+08:00')['meaning']
            report['results'].append({'expected':expected,'actual':actual,'passed':actual==expected,'model':data.get('model')})
            for field in ('prompt_tokens','completion_tokens'):
                report[field]+=data.get('usage',{}).get(field,0)
            (tmp_path/'synthetic-semantics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    assert all(r['passed'] for r in report['results']),report['results']
