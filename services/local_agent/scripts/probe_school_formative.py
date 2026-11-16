"""Bounded official API probe; fixed authored cases only, never family databases."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'eval')]
from school_formative_cases import VERSION,cases,compare,compare_review
from home_study_agent.learning_outcome_evaluator import (
    LEGACY_FORMATIVE_SYSTEM_PROMPT,LearningOutcomeEvaluator,parse_learning_evaluation)
from home_study_agent.model_gateway import ModelGateway


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--suite',choices=['regression','heldout'],default='regression')
    p.add_argument('--case-id')
    p.add_argument('--model', choices=['deepseek-flash','deepseek-chat','deepseek-v4-flash','deepseek-v4-pro'])
    modes=p.add_mutually_exclusive_group()
    modes.add_argument('--criterion-review',action='store_true',default=True)
    modes.add_argument('--legacy-protocol',dest='criterion_review',action='store_false')
    args=p.parse_args()
    selected=[case for case in cases(args.suite) if not args.case_id or case['case_id']==args.case_id]
    if not selected:
        p.error('unknown fixed case')
    # One run per artifact. Existing paid evidence is never silently overwritten.
    if args.output.exists():
        p.error('output exists; inspect existing results before a new paid run')
    fingerprint=lambda value:hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    from home_study_agent import formative_criterion_review as review
    prompt=review.SYSTEM_PROMPT if args.criterion_review else LEGACY_FORMATIVE_SYSTEM_PROMPT
    report=dict(version=VERSION,suite=args.suite,created_at=datetime.now(timezone.utc).isoformat(),
        synthetic_only=True,family_data_used=False,calibration_authority=False,status='running',
        prompt_hash=fingerprint(prompt),cases_hash=fingerprint(selected),criterion_review=args.criterion_review,
        input_tokens=0,output_tokens=0,cases=[])
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    credential=subprocess.run(['/usr/bin/security','find-generic-password','-s','hsc-deepseek-api-key','-w'],
        capture_output=True,text=True)
    if credential.returncode or not credential.stdout.strip():
        report.update(status='blocked',reason='official_keychain_unavailable')
        save()
        return 2
    secret=credential.stdout.strip()
    try:
        with httpx.Client(base_url='https://api.deepseek.com',timeout=180,
                headers={'Authorization':'Bearer '+secret}) as client:
            listing=client.get('/models')
            listing.raise_for_status()
            available={row['id'] for row in listing.json()['data']}
            model=(args.model if args.model in available else None) if args.model else next(
                (m for m in ('deepseek-flash','deepseek-chat','deepseek-v4-flash') if m in available),None)
            if model is None:
                raise ValueError('NO_APPROVED_CHAT_MODEL')
            report['requested_model']=model
            endpoint=SimpleNamespace(provider='deepseek',base_url='https://api.deepseek.com',model=model)
            for case in selected:
                body=ModelGateway.request_payload(endpoint,[{'role':'system','content':prompt},
                    {'role':'user','content':json.dumps(case['payload'],ensure_ascii=False)}],'learning_outcome_evaluation')
                row=dict(case_id=case['case_id'],subject=case['subject'],grade=case['grade'],
                    payload_hash=fingerprint(case['payload']),request_hash=fingerprint(body),max_tokens=body['max_tokens'],passed=False)
                report['cases'].append(row)
                save()
                start=perf_counter()
                response=client.post('/chat/completions',json=body)
                row['latency_ms']=round((perf_counter()-start)*1000)
                response.raise_for_status()
                value=response.json()
                usage=value.get('usage',{})
                row.update(model=value.get('model'),finish_reason=value['choices'][0].get('finish_reason'),
                    prompt_tokens=usage.get('prompt_tokens',0),completion_tokens=usage.get('completion_tokens',0))
                report['input_tokens']+=row['prompt_tokens']
                report['output_tokens']+=row['completion_tokens']
                content=value['choices'][0]['message'].get('content') or ''
                row['candidate']=content.replace(secret,'[REDACTED]')
                try:
                    if row['finish_reason']=='length':
                        raise ValueError('MODEL_OUTPUT_TRUNCATED')
                    evaluation=review.parse(case['payload'],content) if args.criterion_review else parse_learning_evaluation(content)
                    LearningOutcomeEvaluator._validate_formative_receipt(case['payload'],evaluation)
                    row.update(protocol_valid=True,**(compare_review if args.criterion_review else compare)(case,evaluation))
                except (ValueError,KeyError,TypeError) as error:
                    row.update(protocol_valid=False,error_type=type(error).__name__,error_code=str(error)[:120].replace(secret,'[REDACTED]'))
                save()
    except Exception as error:
        report.update(status='blocked',error_type=type(error).__name__)
        if isinstance(error,httpx.HTTPStatusError):
            report['http_status']=error.response.status_code
        save()
        print(json.dumps({k:report[k] for k in ('status','error_type')}))
        return 2
    report.update(status='passed' if all(row['passed'] for row in report['cases']) else 'failed',
        passed=sum(row['passed'] for row in report['cases']),total=len(report['cases']))
    save()
    print(json.dumps({key:report[key] for key in ('status','passed','total','input_tokens','output_tokens')}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
