"""Authored math/physics probes only; no application or family database access."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from time import perf_counter
import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from home_study_agent.document_spoken_review import PROMPT,VERSION,parse,REQUEST_TYPE,VERIFY_REQUEST_TYPE,VERIFICATION_PROMPT
from home_study_agent.document_revisit import PROMPT as INTENT_PROMPT,parse as parse_intent
from home_study_agent.model_gateway import ModelGateway

INTENTS=[('我讲完了，看看整体思路对不对','review_explanation'),
    ('前面几段合起来有什么遗漏','review_explanation'),
    ('把我的原话读回来，不用评价','status'),('你从头完整讲解这道题','help'),
    ('我讲完了','unclear'),('我还要乘回去核对总数','answer')]
CASES=[
    dict(name='math-consistent',question='24支铅笔平均分给6人，每人几支？请说明并检验。',
        segments=['我用24除以6得到4，每人4支。','我用4乘6得到24，和原来总数一样。'],
        criteria=['解释平均分的除法意义','用乘法检验总数']),
    dict(name='math-contradiction',question='24支铅笔平均分给6人，每人几支？请说明并检验。',
        segments=['24除以6等于4，所以每人4支。','我又觉得应该每人6支，因为有6人，6乘6就是24。'],
        criteria=['解释平均分的除法意义','用乘法检验总数']),
    dict(name='math-missing-reason',question='24支铅笔平均分给6人，每人几支？请说明并检验。',
        segments=['每人4支。'],criteria=['解释平均分的除法意义','用乘法检验总数']),
    dict(name='physics-contradiction',question='电源电压保持6伏，电阻由3欧变为6欧，电流如何变化？说明依据。',
        segments=['电流等于电压除以电阻，所以原来是2安。','电阻翻倍后电流也翻倍，变成4安。'],
        criteria=['使用欧姆定律计算电流','解释恒压条件下电流与电阻的关系']),
]

# Authored before comparing effort modes; never sourced from a child's work.
HELDOUT=[
    dict(name='fraction-consistent',question='一块蛋糕，小明吃了四分之一，小红吃了八分之三，一共吃了几分之几？说明理由。',
        segments=['先把四分之一化成八分之二，和八分之三相加得到八分之五。','分母相同表示每小份一样大，所以只把份数相加。'],
        criteria=['通分后相加','解释同分母分数加法'],required_kind='supported',forbidden_kind='contradiction'),
    dict(name='percent-contradiction',question='一件衣服原价100元，先涨价20%，再按涨后价降价20%，现价多少？',
        segments=['先涨20元得到120元。','涨和降都是20%，相互抵消，所以还是100元。'],
        criteria=['区分两次百分率的基准量','计算降价后的价格'],required_kind='contradiction'),
    dict(name='current-consistent',question='理想电流源使电流保持2安，电阻从3欧增至6欧，两端电压怎样变？',
        segments=['电流固定，用电压等于电流乘电阻。','原来2乘3是6伏，后来2乘6是12伏，所以电压翻倍。'],
        criteria=['识别恒流条件','用欧姆定律计算电压'],required_kind='supported',forbidden_kind='contradiction'),
    dict(name='english-contradiction',question='用一般过去时询问“你昨天去公园了吗？”，并解释动词形式。',
        segments=['Did you went to the park yesterday?','did已经是过去式，但go也要改成went。'],
        criteria=['使用did引导一般疑问句','did后实义动词使用原形'],required_kind='contradiction'),
    dict(name='geometry-insufficient',question='已知一个三角形的一条边长为6厘米，求面积。',
        segments=['我觉得面积是18平方厘米，但我不知道高是多少。'],
        criteria=['判断面积计算所需条件是否充分'],required_kind='missing'),
]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--strong-review',action='store_true')
    p.add_argument('--heldout',action='store_true')
    p.add_argument('--effort',choices=['low','high'])
    args=p.parse_args()
    report=dict(version=VERSION,synthetic_only=True,family_data_used=False,status='running',
        input_tokens=0,output_tokens=0,cases=[],suite='heldout-v1' if args.heldout else 'regression',
        experimental_effort=args.effort)
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    secret=subprocess.run(['/usr/bin/security','find-generic-password','-s','hsc-deepseek-api-key','-w'],capture_output=True,text=True)
    if secret.returncode or not secret.stdout.strip():
        report.update(status='blocked',reason='keychain_unavailable')
        save()
        return 2
    try:
        with httpx.Client(base_url='https://api.deepseek.com',timeout=60,
                headers={'Authorization':'Bearer '+secret.stdout.strip()}) as client:
            response=client.get('/models')
            response.raise_for_status()
            available={r['id'] for r in response.json()['data']}
            model=next(m for m in ('deepseek-flash','deepseek-chat','deepseek-v4-flash') if m in available)
            report['requested_model']=model
            review_model=next((m for m in ('deepseek-v4-pro','deepseek-pro','deepseek-reasoner') if m in available),None) if args.strong_review else model
            if not review_model:
                raise ValueError('NO_APPROVED_REVIEW_MODEL')
            report['review_model']=review_model
            def call(name,system,data,kind):
                config=SimpleNamespace(provider='deepseek',base_url='https://api.deepseek.com',model=model if kind=='voice_interaction' else review_model)
                body=ModelGateway.request_payload(config,[{'role':'system','content':system},
                    {'role':'user','content':json.dumps(data,ensure_ascii=False)}],kind)
                if args.effort and kind in {REQUEST_TYPE,VERIFY_REQUEST_TYPE}:
                    body['reasoning_effort']=args.effort
                start=perf_counter()
                response=client.post('/chat/completions',json=body)
                response.raise_for_status()
                value=response.json()
                report['input_tokens']+=value.get('usage',{}).get('prompt_tokens',0)
                report['output_tokens']+=value.get('usage',{}).get('completion_tokens',0)
                message=value['choices'][0]['message']
                row=dict(name=name,latency_ms=round((perf_counter()-start)*1000),model=value.get('model'),passed=False,
                    thinking=body.get('thinking'),max_tokens=body['max_tokens'],
                    reasoning_effort=body.get('reasoning_effort'),
                    reasoning_present=bool(message.get('reasoning_content')),finish_reason=value['choices'][0].get('finish_reason'))
                report['cases'].append(row)
                return row,message['content']
            for i,(text,expected) in enumerate([] if args.heldout else INTENTS):
                anchor=datetime.now(timezone.utc).isoformat()
                row,content=call('intent-'+str(i),INTENT_PROMPT,
                    dict(transcript=text,phase='check_feedback',timezone='UTC',current_time=anchor),'voice_interaction')
                try:
                    result=parse_intent(content,text,anchor)
                    row.update(expected=expected,actual=result['meaning'],passed=result['meaning']==expected)
                except (ValueError,KeyError,TypeError) as error:
                    row['error_type']=type(error).__name__
                    # Inputs are fixed authored probes. Preserve a failed
                    # candidate so the next audit does not buy another call.
                    row['synthetic_candidate']=content[:12000]
                save()
            for case in (HELDOUT+[CASES[1]] if args.heldout else CASES):
                data={k:case[k] for k in ('question','segments','criteria')}
                data['after_feedback']=True
                row,content=call(case['name'],PROMPT,data,REQUEST_TYPE)
                try:
                    if row['finish_reason']=='length':
                        raise ValueError('MODEL_OUTPUT_TRUNCATED')
                    value=parse(content,data)
                    row.update(passed=True,review=value)
                    # This is a semantic alarm, not a proof of teaching quality.
                    kinds={o['kind'] for o in value['observations']}
                    if 'contradiction' in case['name']:
                        row['passed']='contradiction' in kinds
                    if case['name']=='math-missing-reason':
                        row['passed']='missing' in kinds
                    if case['name']=='math-consistent':
                        row['passed']='contradiction' not in kinds
                    if case.get('required_kind'):
                        row['passed']=row['passed'] and case['required_kind'] in kinds
                    if case.get('forbidden_kind'):
                        row['passed']=row['passed'] and case['forbidden_kind'] not in kinds
                    check,checked=call(case['name']+'-check',VERIFICATION_PROMPT,{**data,'candidate':value},VERIFY_REQUEST_TYPE)
                    check['passed']=check['finish_reason']!='length' and json.loads(checked)=={'verdict':'supported'}
                    check['verdict']=json.loads(checked).get('verdict')
                    row['passed']=row['passed'] and check['passed']
                except (ValueError,KeyError,TypeError) as error:
                    row['error_type']=type(error).__name__
                    row['synthetic_candidate']=content[:12000]
                save()
            # An actual prior probe mistake must be rejected, not merely parse.
            data={k:v for k,v in CASES[0].items() if k!='name'}
            bad=dict(observations=[dict(kind='contradiction',quote=data['segments'][0],criterion=data['criteria'][0],
                explanation='24÷6=4，不是4；若每人4支，6人共24支，与24÷6的结果不符。')],next_prompt='24除以6到底等于几？')
            row,content=call('known-false-feedback-rejection',VERIFICATION_PROMPT,{**data,'candidate':bad},VERIFY_REQUEST_TYPE)
            row.update(verdict=json.loads(content).get('verdict'),passed=json.loads(content)=={'verdict':'conflict'})
            save()
    except Exception as error:
        report.update(status='blocked',error_type=type(error).__name__)
        save()
        print(json.dumps({'status':report['status'],'error_type':report['error_type']}))
        return 2
    report.update(status='passed' if all(r['passed'] for r in report['cases']) else 'failed',
        passed=sum(r['passed'] for r in report['cases']),total=len(report['cases']))
    save()
    print(json.dumps({k:report[k] for k in ('status','passed','total','input_tokens','output_tokens')}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
