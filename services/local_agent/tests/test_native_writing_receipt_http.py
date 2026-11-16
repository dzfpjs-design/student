from dataclasses import replace
import hashlib
import json
import multiprocessing
import socket
import time
from contextlib import contextmanager
from pathlib import Path
import httpx
import uvicorn
from test_native_writing_shell_http import scheduled, create_app, _http_client, headers, draft, KEY


def _receipt_process(listener, data_dir, database_path, stopped, counters, semantic=False, session_token=None):
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    async def deny_provider(request):
        if semantic:
            from test_scheduled_writing_natural_runtime_http import ControlledTransport
            transport=ControlledTransport()
            value=json.loads(json.loads(request.content)['messages'][-1]['content'])
            if set(value)=={'transcript','state'}:
                transport.control={'刚才作文交上了吗':'native_writing.submission_status',
                    '继续提交本题':'native_writing.submit_retained'}[value['transcript']]
            else:
                assert set(value)=={'question','response','rubric'},set(value)
            counters.put('synthetic_model_post')
            return await transport(request)
        counters.put('provider_post')
        raise AssertionError('No model calls permitted during native receipt recovery')
    settings=Settings(data_dir=Path(data_dir),database_path=Path(database_path),maintenance_enabled=False,
        data_encryption_key=b'r'*32,native_launch_attestation_key=KEY,
        model_api_key='synthetic-only',model_base_url='https://controlled.invalid/v1',
        model_name='controlled-model',voice_intent_model_name='controlled-model',session_token=session_token)
    gateway=ModelGateway(settings,transport=httpx.MockTransport(deny_provider))
    application=create_app(settings,model_gateway=gateway)
    async def lose_committed_receipt(scope,receive,send):
        if scope['type']=='http' and (b'x-synthetic-receipt-loss',b'1') in scope['headers']:
            messages=[]
            async def hold(message):messages.append(message)
            await application(scope,receive,hold)
            assert messages[0]['status']==200,messages[0]
            counters.put('committed_receipt_lost')
            # Real handler/SQLite commit completed; only its HTTP receipt is replaced.
            await send({'type':'http.response.start','status':503,'headers':[(b'content-length',b'0')]})
            await send({'type':'http.response.body','body':b''})
        else:await application(scope,receive,send)
    server=uvicorn.Server(uvicorn.Config(lose_committed_receipt,log_level='error',access_log=False))
    import threading
    watcher=threading.Thread(target=lambda:(stopped.wait(),setattr(server,'should_exit',True)),daemon=True)
    watcher.start()
    server.run(sockets=[listener])
    counters.put('drained')


@contextmanager
def _process_client(case, expected_losses=0, semantic=False, session_token=None):
    context=multiprocessing.get_context('spawn')
    stopped=context.Event()
    counters=context.Queue()
    listener=socket.socket()
    listener.bind(('127.0.0.1',0))
    port=listener.getsockname()[1]
    args=(listener,str(case['settings'].data_dir),str(case['db'].path),stopped,counters,semantic)
    if session_token:args+=(session_token,)
    process=context.Process(target=_receipt_process,args=args)
    process.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=20,trust_env=False,
                          headers={'X-HSC-Session':session_token}if session_token else None) as client:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                assert process.is_alive(),process.exitcode
                try:
                    if client.get('/health',timeout=.5).status_code<500:break
                except httpx.HTTPError:pass
                time.sleep(.05)
            else:raise AssertionError('receipt subprocess did not start')
            yield client,process.pid
    finally:
        stopped.set();process.join(15)
        if process.is_alive():process.terminate();process.join(5)
        listener.close()
        assert not process.is_alive()
        assert process.exitcode==0
        messages=[]
        from queue import Empty
        while True:
            try:messages.append(counters.get(timeout=.1))
            except Empty:break
        counters.close();counters.join_thread()
        process.close()
        if semantic:
            case['synthetic_model_posts']=messages.count('synthetic_model_post')
            messages=[m for m in messages if m!='synthetic_model_post']
        assert messages==['committed_receipt_lost']*expected_losses+['drained'],messages


def test_native_original_receipt_is_exact_readonly_and_does_not_repost_capture(tmp_path):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        settings = replace(case['settings'], native_launch_attestation_key=KEY)
        body = draft(case)
        base = f'/v1/sessions/{case["sid"]}'
        with _http_client(create_app(settings, model_gateway=case['gateway_factory']())) as client:
            response = client.post(base+'/native-writing-original', json=body, headers=headers(body))
            assert response.status_code == 200, response.text
            original = response.json()
            with case['db'].connect() as c:
                before = list(c.iterdump())
            calls = len(case['transport'].calls)
            receipt = client.get(base+'/native-writing-receipt', params={
                **{k:body[k] for k in ('child_id','prompt_id','item_version','diagnostic_item_id')},
                'text_sha256':hashlib.sha256(body['text'].encode()).hexdigest(),
                'capture_action_id':body['client_action_id'],
            })
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()['state'] == 'captured'
            assert receipt.json()['original'] == original
            with case['db'].connect() as c:
                assert list(c.iterdump()) == before
            assert len(case['transport'].calls) == calls


def test_native_answer_receipt_checks_original_action_scope_text_and_revocation_without_resume(tmp_path):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        settings = replace(case['settings'], native_launch_attestation_key=KEY)
        body = draft(case)
        base = f'/v1/sessions/{case["sid"]}'
        with _http_client(create_app(settings, model_gateway=case['gateway_factory']())) as client:
            original = client.post(base+'/native-writing-original',json=body,headers=headers(body)).json()
            action = 'native-writing-answer:'+original['artifact_id']
            answer = client.post(base+'/diagnostic-prompt/answer',json=dict(prompt_id=body['prompt_id'],
                item_version=body['item_version'],answer=body['text'],client_action_id=action,
                writing_original_artifact_id=original['artifact_id'],writing_original_revision=original['original_revision']))
            assert answer.status_code == 200, answer.text
            params = {**{k:body[k] for k in ('child_id','prompt_id','item_version','diagnostic_item_id')},
                'text_sha256':hashlib.sha256(body['text'].encode()).hexdigest(),
                'capture_action_id':body['client_action_id'],'answer_action_id':action}
            calls = len(case['transport'].calls)
            with case['db'].connect() as c:
                before = list(c.iterdump())
            for fault in (None,'child_id','prompt_id','item_version','diagnostic_item_id','text_sha256','answer_action_id'):
                changed = params if fault is None else {**params,fault:'0'*64}
                response = client.get(base+'/native-writing-receipt',params=changed)
                if fault:
                    assert response.status_code == 409, (fault,response.text)
                else:
                    assert response.status_code == 200,response.text
                    assert response.json()['state'] == 'answered'
                    assert response.json()['result']['attempt']['attempt_id'] == answer.json()['attempt']['attempt_id']
                    assert not response.json()['result'].get('local_learning_evaluation')
            with case['db'].connect() as c:
                assert list(c.iterdump()) == before
                c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?",(original['artifact_id'],))
                c.commit()
                revoked = list(c.iterdump())
            assert client.get(base+'/native-writing-receipt',params=params).status_code == 409
            with case['db'].connect() as c:
                assert list(c.iterdump()) == revoked
            assert len(case['transport'].calls) == calls


def test_actual_tcp_capture_and_answer_response_loss_then_two_process_restarts_read_exact_records(tmp_path):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body=draft(case)
        base=f'/v1/sessions/{case["sid"]}'
        params={**{k:body[k] for k in ('child_id','prompt_id','item_version','diagnostic_item_id')},
            'text_sha256':hashlib.sha256(body['text'].encode()).hexdigest(),'capture_action_id':body['client_action_id']}
        with _process_client(case,expected_losses=1) as (client,pid1):
            response=client.post(base+'/native-writing-original',json=body,headers={**headers(body),'X-Synthetic-Receipt-Loss':'1'})
            assert response.status_code==503,response.text
        with _process_client(case,expected_losses=1) as (client,pid2):
            assert pid2!=pid1
            with case['db'].connect() as c:before=list(c.iterdump())
            receipt=client.get(base+'/native-writing-receipt',params=params)
            assert receipt.status_code==200,receipt.text
            original=receipt.json()['original']
            with case['db'].connect() as c:assert list(c.iterdump())==before
            # A separate explicit answer submission after captured-only recovery.
            action='native-writing-answer:'+original['artifact_id']
            answer=client.post(base+'/diagnostic-prompt/answer',headers={'X-Synthetic-Receipt-Loss':'1'},json=dict(prompt_id=body['prompt_id'],
                item_version=body['item_version'],answer=body['text'],client_action_id=action,
                writing_original_artifact_id=original['artifact_id'],writing_original_revision=original['original_revision']))
            assert answer.status_code==503,answer.text
        with _process_client(case) as (client,pid3):
            assert pid3 not in {pid1,pid2}
            params['answer_action_id']=action
            with case['db'].connect() as c:
                before=list(c.iterdump())
            receipt=client.get(base+'/native-writing-receipt',params=params)
            assert receipt.status_code==200,receipt.text
            assert receipt.json()['state']=='answered'
            attempt=receipt.json()['result']['attempt']['attempt_id']
            again=client.get(base+'/native-writing-receipt',params=params)
            assert again.json()['result']['attempt']['attempt_id']==attempt
            with case['db'].connect() as c:
                assert list(c.iterdump())==before
                assert c.execute("SELECT count(*) FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()[0]==1
                assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE diagnostic_item_id=?',(body['diagnostic_item_id'],)).fetchone()[0]==1
                assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]==0
        (tmp_path/'native-process-receipts.json').write_text(json.dumps({
            'pids':[pid1,pid2,pid3],'restarts':2,'owned_processes_remaining':0,
            'original_artifact_id':original['artifact_id'],'attempt_id':attempt,
            'capture_posts':1,'answer_posts':1,'capture_receipt_http_status':503,'answer_receipt_http_status':503,
            'recovery_gets':3,'readonly_database_unchanged':True,'provider_posts':0,
            'originals':1,'attempts_for_item':1,'rubric_assessments':0,
            'boundary':'Real create_app/router/SQLite/TCP in spawned Python processes. Synthetic reviewed item and post-commit HTTP receipt loss; provider denied. No native OS/Windows claim.'},indent=2))
