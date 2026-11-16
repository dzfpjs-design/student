from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import pytest
from test_scheduled_writing_natural_runtime_http import scheduled, create_app, _http_client, post, TEXT
from home_study_agent.native_writing import FIELDS

KEY='11'*32
def headers(body):
    at=datetime.now(timezone.utc).isoformat()
    value=json.dumps(['native_writing_capture_v1',*[body[k] for k in FIELDS],at],ensure_ascii=False,separators=(',',':'))
    return {'X-HSC-Native-Writing-Time':at,'X-HSC-Native-Writing':hmac.new(bytes.fromhex(KEY),value.encode(),hashlib.sha256).hexdigest()}

def draft(case):
    p=case['prompt']
    return dict(child_id=p['child_id'],session_id=case['sid'],prompt_id=p['prompt_id'],item_version=p['item_version'],
        diagnostic_item_id=p['diagnostic_item_id'],text=TEXT,client_action_id='native-shell-draft')


def test_native_host_capture_exact_revision_answer_feedback_and_restart(tmp_path):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        settings=replace(case['settings'],native_launch_attestation_key=KEY)
        body=draft(case)
        path=f'/v1/sessions/{case["sid"]}/native-writing-original'
        with _http_client(create_app(settings,model_gateway=case['gateway_factory']())) as client:
            assert client.post(path,json=body).status_code==409
            original=client.post(path,json=body,headers=headers(body))
            assert original.status_code==200,original.text
            original=original.json()
            assert original['text_sha256']==hashlib.sha256(TEXT.encode()).hexdigest()
            answer=dict(prompt_id=body['prompt_id'],item_version=body['item_version'],answer=TEXT,
                writing_original_artifact_id=original['artifact_id'],writing_original_revision=original['original_revision'],client_action_id='native-answer')
            for key,value in [('writing_original_artifact_id','another-original'),('writing_original_revision','0'*64),('answer',TEXT+' altered')]:
                # Each intentionally corrected invalid request is a new operation;
                # a definitively rejected native key is now durably fenced.
                rejected=client.post(f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer',json={**answer,key:value,
                    'client_action_id':'native-answer-rejected-'+key})
                assert rejected.status_code==409,(key,rejected.text)
            saved=post(client,f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer',answer)
            notice=saved['local_learning_evaluation']
            assert notice['status']=='feedback_required',notice
            evaluated=post(client,f'/v1/agent-turns/{notice["feedback_turn_id"]}/execute',{})
            assert evaluated['execution']['result']['rubric_assessment']['source_evidence_id']==saved['attempt']['evidence_id']
            before_calls=len(case['transport'].calls)
        with _http_client(create_app(settings,model_gateway=case['gateway_factory']())) as client:
            assert client.post(path,json=body,headers=headers(body)).json()==original
            replay=post(client,f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer',answer)
            assert replay['attempt']['attempt_id']==saved['attempt']['attempt_id']
            assert len(case['transport'].calls)==before_calls
            with case['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]==1
                assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0]==0
                assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('fault',['child','session','prompt','item','version','tampered_signature','withdrawn','same_key_new_text'])
def test_native_capture_never_borrows_a_different_original(tmp_path,fault):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body=draft(case)
        settings=replace(case['settings'],native_launch_attestation_key=KEY)
        path=f'/v1/sessions/{case["sid"]}/native-writing-original'
        with _http_client(create_app(settings,model_gateway=case['gateway_factory']())) as client:
            signed=headers(body)
            mapping={'child':'child_id','session':'session_id','prompt':'prompt_id','item':'diagnostic_item_id','version':'item_version'}
            if fault in mapping:
                body={**body,mapping[fault]:'wrong-scope'}
                signed=headers(body)
            elif fault=='tampered_signature':body={**body,'text':TEXT+' forged'}
            else:
                original=client.post(path,json=body,headers=signed).json()
                if fault=='withdrawn':
                    with case['db'].connect() as c:
                        c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?",(original['artifact_id'],));c.commit()
                else:body={**body,'text':TEXT+' changed'}
                signed=headers(body)
            response=client.post(path,json=body,headers=signed)
            assert response.status_code==409,response.text
            with case['db'].connect() as c:
                assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE diagnostic_item_id=?',(case['prompt']['diagnostic_item_id'],)).fetchone()
                assert not c.execute('SELECT 1 FROM rubric_assessment').fetchone()
