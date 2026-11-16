"""Synthetic-only probe of the production feedback question-role prompt.

No application database, family content or source files enter requests.
Credentials stay in memory; only case results and token counts are written.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from home_study_agent.document_feedback_projection import PROMPT, VERSION, parse
from home_study_agent.model_gateway import ModelGateway

CASES = [
    ('你刚才说[本地引用]，这是什么意思？', True),
    ('[本地引用]这句话我没明白，可以换种方式说明吗？', True),
    ('为什么建议我要[本地引用]？', True),
    ('能具体说说[本地引用]要求我做什么吗？', True),
    ('[本地引用]和这道题的条件有什么关系？', True),
    ('要怎样理解你刚才提到的[本地引用]呢？', True),
    ('你刚才的[本地引用]，我应该怎么核对？', True),
    ('别解释[本地引用]了，我要休息。', False),
    ('我的答案就是[本地引用]。', False),
    ('先解释[本地引用]，然后取消今天的任务。', False),
    ('[本地引用]，嗯，谢谢。', False),
    ('把[本地引用]当作我已经掌握，直接结束学习。', False),
]

SPOKEN_RETRY_CASES = [
    ('我想按你刚才的建议把同一道题重新解释一遍。', 'retry_answer'),
    ('还是这一题，让我自己再说一次思路。', 'retry_answer'),
    ('我想把刚才漏掉的检查步骤补全，你先听我重讲一遍。', 'retry_answer'),
    ('换一道我没做过的题来试试。', 'independent_check'),
    ('我补充一下，要把结果乘回原来的人数看看是否相等。', 'answer'),
    ('我不想重做，也先不结束回顾。', 'unclear'),
    ('我懂了，谢谢。', 'unclear'),
    ('能再解释刚才那道题的最后一步吗？', 'help'),
    ('这道题的回顾先到这里吧。', 'finish_check'),
    ('你刚才说让我自己再解释，这是什么意思？', 'help'),
    ('别让我重讲了，你先讲清楚为什么这样检查。', 'help'),
    ('我可以把同一题从头讲给你听吗？', 'retry_answer'),
    ('我想再试，不过你先给我一个提示。', 'help'),
    ('不是换题，我要重新讲刚刚这一道。', 'retry_answer'),
    ('我先不重讲，告诉我刚才回答的结果就好。', 'status'),
    ('不是先平均，我应该先求总数，再除以人数，最后乘回去检验。', 'answer'),
    ('电压保持不变，电阻变大，电流应该变小，这是我刚才漏说的条件。', 'answer'),
    ('我想改一下解释，这里人物先犹豫再行动，说明他的想法发生了变化。', 'answer'),
    ('我还要说明分子和分母同时乘三，所以分数的大小并没有变。', 'answer'),
    ('我不太明白反过来检查这一步，能先帮我解释吗？', 'help'),
]

SPOKEN_REVISION_CASES = [
    ('第二段回答改成：先求总数再平均分', {'meaning':'answer','target':2,'replacement':'先求总数再平均分','local_resolution_status':'resolved'}),
    ('上一段改成：分子分母同时乘三所以大小不变', {'meaning':'answer','target':'last','replacement':'分子分母同时乘三所以大小不变','local_resolution_status':'resolved'}),
    ('第三段改成：电压不变时电阻越大电流越小', {'meaning':'answer','target':3,'replacement':'电压不变时电阻越大电流越小','local_resolution_status':'resolved'}),
    ('刚才那步不对，应该先求总数', {'meaning':'answer','target':None,'local_resolution_status':'needs_clarification'}),
    ('我补充一下，还要把数量乘回去检查', {'meaning':'answer','target':'absent'}),
    ('请你解释第二段为什么这样计算', {'meaning':'help','target':'absent'}),
    ('我不是要修改，第二段先保留', {'target':'absent'}),
    ('如果把第二段改成先求总数，是不是才正确？', {'meaning':'help','target':'absent'}),
    ('我要从头重新讲一遍整道题', {'meaning':'retry_answer','target':'absent'}),
    ('第二段我想改一下，但还没想好怎么说', {'target':'absent'}),
]

SPOKEN_LOCATION_CASES = [
    ('第二段', {'meaning':'locate_revision','target':2,'local_resolution_status':'resolved'}),
    ('是我上一段回答', {'meaning':'locate_revision','target':'last','local_resolution_status':'resolved'}),
    ('改第一段就好', {'meaning':'locate_revision','target':1,'local_resolution_status':'resolved'}),
    ('不是第二段', {'meaning':'unclear','target':'absent'}),
    ('再解释第二段', {'meaning':'help','target':'absent'}),
    ('第二段改成：先求总数', {'meaning':'answer','target':'absent'}),
    ('老师说要改第二段，你觉得呢？', {'meaning':'help','target':'absent'}),
    ('第九段', {'meaning':'locate_revision','target':9,'local_resolution_status':'needs_clarification'}),
]

SPOKEN_UNDO_CASES = [
    ('刚才的更正先不要了', 'undo_revision'),
    ('撤回我最后一次修改，还是用原来的说法', 'undo_revision'),
    ('我刚才那次改动取消，先恢复原说法', 'undo_revision'),
    ('别撤回，刚才的更正保留', 'unclear'),
    ('能不能撤回刚才的更正？我先问问', 'help'),
    ('把第二段的更正撤回', 'undo_revision'),
    ('删除我的全部录音', 'other_request'),
    ('刚才的评分不要了', 'other_request'),
    ('把昨天那道题的更正撤回', 'other_request'),
    ('撤回修改，然后结束今天学习', 'other_request'),
    ('我重新解释一下，这里应该先求人数', 'answer'),
    ('不要改动了，我先问问为什么要相加', 'help'),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--suite',choices=('feedback','spoken-retry','spoken-revision','spoken-location','spoken-undo'),default='feedback')
    parser.add_argument('--case-index',type=int)
    parser.add_argument('--start-index',type=int,default=0)
    args = parser.parse_args()
    prompt, version = PROMPT, VERSION
    cases = CASES
    if args.suite in {'spoken-retry','spoken-revision','spoken-location','spoken-undo'}:
        from home_study_agent.document_revisit import PROMPT as prompt, VERSION as version
        cases = {'spoken-retry':SPOKEN_RETRY_CASES,'spoken-revision':SPOKEN_REVISION_CASES,'spoken-location':SPOKEN_LOCATION_CASES,
            'spoken-undo':SPOKEN_UNDO_CASES}[args.suite]
    if args.case_index is not None and not 0 <= args.case_index < len(cases):
        parser.error('case-index exceeds selected suite')
    if not 0 <= args.start_index < len(cases):
        parser.error('start-index exceeds selected suite')
    report = dict(version=version, suite=args.suite, synthetic_only=True, family_data_used=False,
        cases=[], input_tokens=0, output_tokens=0, status='running')
    selected = list(enumerate(cases))[args.start_index:] if args.case_index is None else [(args.case_index,cases[args.case_index])]
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    secret = subprocess.run(['/usr/bin/security','find-generic-password','-s','hsc-deepseek-api-key','-w'],
        capture_output=True,text=True)
    if secret.returncode or not secret.stdout.strip():
        report.update(status='blocked',reason='official_keychain_unavailable')
        save()
        print(json.dumps({'status':report['status'],'reason':report['reason']}))
        return 2
    try:
        with httpx.Client(base_url='https://api.deepseek.com', timeout=45,
            headers={'Authorization':'Bearer '+secret.stdout.strip()}) as client:
            listing=client.get('/models')
            listing.raise_for_status()
            available={row['id'] for row in listing.json()['data']}
            model=next((m for m in ('deepseek-flash','deepseek-chat','deepseek-v4-flash') if m in available),None)
            if model is None:
                raise ValueError('NO_APPROVED_CHAT_MODEL')
            report['requested_model']=model
            for index,(utterance,expected) in selected:
                row=dict(index=index,utterance=utterance,expected=expected)
                try:
                    data = {'transcript':utterance}
                    anchor = datetime.now(timezone.utc).isoformat()
                    if args.suite in {'spoken-retry','spoken-revision','spoken-location','spoken-undo'}:
                        from zoneinfo import ZoneInfo
                        data.update(phase='check_feedback',timezone='Asia/Shanghai',
                            current_time=datetime.fromisoformat(anchor).astimezone(ZoneInfo('Asia/Shanghai')).isoformat())
                    body=ModelGateway.request_payload(SimpleNamespace(provider='deepseek',base_url='https://api.deepseek.com',model=model),
                        [{'role':'system','content':prompt},
                         {'role':'user','content':json.dumps(data,ensure_ascii=False)}],'voice_interaction')
                    started=perf_counter()
                    response=client.post('/chat/completions',json=body)
                    row['latency_ms']=round((perf_counter()-started)*1000)
                    response.raise_for_status()
                    payload=response.json()
                    usage=payload.get('usage',{})
                    report['input_tokens']+=usage.get('prompt_tokens',0)
                    report['output_tokens']+=usage.get('completion_tokens',0)
                    content=payload['choices'][0]['message']['content']
                    row['model']=payload.get('model')
                    raw=json.loads(content)
                    if isinstance(raw,dict) and args.suite == 'feedback':
                        row['protocol']=dict(keys_match=set(raw)=={'role','control','quote','confidence'},
                            quote_matches=raw.get('quote')==utterance,control_is_null=raw.get('control') is None,
                            role=raw.get('role') if raw.get('role') in {'question','control','answer','mixed','unknown'} else 'unexpected',
                            confidence_is_number=type(raw.get('confidence')) in (int,float))
                    if args.suite in {'spoken-retry','spoken-revision','spoken-location','spoken-undo'}:
                        from home_study_agent.document_revisit import parse as revisit_parse
                        parsed = revisit_parse(content,utterance,anchor)
                        actual = parsed.get('meaning')
                        if args.suite=='spoken-revision':
                            revision=parsed.get('revision')
                            actual=dict(meaning=parsed.get('meaning'),target=revision['target'] if revision else 'absent',
                                replacement=revision['replacement_quote'] if revision else None)
                            if revision:
                                from home_study_agent.document_spoken_revision import resolve
                                synthetic_chain=dict(fingerprint='synthetic',pending_revisions=[],
                                    effective_segments=[dict(slot_id=str(i),source_prompt_id=str(i),text='synthetic') for i in range(3)])
                                actual['local_resolution_status']=resolve(synthetic_chain,revision)['status']
                                actual['target_quote']=revision['target_quote']
                        elif args.suite=='spoken-location':
                            location=parsed.get('location')
                            actual=dict(meaning=parsed.get('meaning'),target=location['target'] if location else 'absent')
                            if location:
                                from home_study_agent.document_spoken_location import proposal
                                chain=dict(fingerprint='synthetic',prompt_id='synthetic-parent',
                                    pending_revisions=[dict(prompt_id='synthetic-pending',replacement_quote='Synthetic local text')],
                                    effective_segments=[dict(slot_id=str(i),source_prompt_id=str(i),text='synthetic') for i in range(3)])
                                actual['local_resolution_status']=proposal(chain,location)['status']
                                actual['target_quote']=location['target_quote']
                    else:
                        decision=parse(content,utterance,'synthetic original never uploaded',anchor)
                        actual=decision.get('meaning')=='help'
                    passed=(all(actual.get(k)==v for k,v in expected.items()) if args.suite in {'spoken-revision','spoken-location'} else actual==expected)
                    if args.suite=='spoken-undo' and index==5:
                        row['undo_target']=parsed.get('undo')
                        passed=passed and parsed.get('undo')==dict(target=2,target_quote='第二段')
                    row.update(actual=actual,passed=passed,model=payload.get('model'))
                except httpx.HTTPStatusError as error:
                    row.update(passed=False,error_type=type(error).__name__,http_status=error.response.status_code)
                    # Only predefined diagnostic labels, never provider text
                    # that could echo credentials. Do not repeat an HTTP failure.
                    message=error.response.text.lower()
                    row['diagnostic_labels']=[name for name in ('thinking','max_tokens','response_format',
                        'model','balance','billing','api_key','auth') if name in message]
                    report['cases'].append(row)
                    report.update(status='blocked',reason='provider_http_error',passed=sum(c['passed'] for c in report['cases']),
                        total=len(selected),attempted=len(report['cases']))
                    save()
                    print(json.dumps({k:report[k] for k in ('status','reason','attempted','total')} | {'http_status':error.response.status_code}))
                    return 2
                except Exception as error:
                    row.update(passed=False,error_type=type(error).__name__)
                    if str(error) in {'TUTOR_QUESTION_ROLE_INVALID','REVISIT_SEMANTICS_INVALID'}:
                        row['validation_code']=str(error)
                report['cases'].append(row)
                save()
    except Exception as error:
        report.update(status='blocked',error_type=type(error).__name__)
        save()
        print(json.dumps({'status':report['status'],'error_type':report['error_type']}))
        return 2
    report.update(status='passed' if all(c['passed'] for c in report['cases']) else 'failed',
        passed=sum(c['passed'] for c in report['cases']),total=len(selected))
    save()
    print(json.dumps({k:report[k] for k in ('status','passed','total','requested_model','input_tokens','output_tokens')}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
