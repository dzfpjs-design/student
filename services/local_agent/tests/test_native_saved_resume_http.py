"""Exact saved-answer continuation; synthetic speech, real TCP and SQLite."""
from dataclasses import replace
import json
import copy
import httpx
from home_study_agent.model_gateway import ModelGateway
import pytest
from contextlib import contextmanager, ExitStack
from test_native_writing_shell_http import scheduled, create_app, _http_client, headers, draft, KEY
from test_native_writing_rejection_http import query
from test_native_writing_receipt_http import _process_client
from test_voice_runtime_binding import capture, interpret
from home_study_agent.diagnostic_prompt import DiagnosticPromptService, DiagnosticPromptError
from test_native_writing_receipt_http import _receipt_process as _base_receipt_process


def _synthetic_voice_process(*args):
    from home_study_agent.local_media_ai import LocalMediaAI
    LocalMediaAI._recognize_wav=lambda *a,**kw: ('继续提交本题',1000,'synthetic',False)
    import home_study_agent.native_writing_resume as module
    original=module.resume_saved
    def traced(*a,**kw):
        try:return original(*a,**kw)
        except Exception:
            import traceback
            traceback.print_exc()
            raise
    module.resume_saved=traced
    _base_receipt_process(*args)


def _interrupted_answer_process(*args):
    original=DiagnosticPromptService.resume
    def fail_after_save(service,*a,**kw):
        if type(service) is DiagnosticPromptService:
            raise DiagnosticPromptError('synthetic_interruption_after_durable_native_answer')
        return original(service,*a,**kw)
    DiagnosticPromptService.resume=fail_after_save
    _base_receipt_process(*args)


@contextmanager
def saved_answer(tmp_path, monkeypatch):
    with scheduled(tmp_path) as case:
        body=draft(case)
        base=f'/v1/sessions/{case["sid"]}'
        settings=replace(case['settings'],native_launch_attestation_key=KEY)
        case['finish_client']()
        gateway = ModelGateway(settings, transport=httpx.MockTransport(case['transport']))
        with ExitStack() as original_client_lifecycle:
            client = original_client_lifecycle.enter_context(_http_client(create_app(settings,model_gateway=gateway)))
            case['client'] = client
            case['gateway'] = gateway
            original=client.post(base+'/native-writing-original',json=body,headers=headers(body)).json()
            case['transport'].control='native_writing.submit_retained'
            def control(key):
                text='继续提交本题'
                voice=capture(client,settings,monkeypatch,case['sid'],text,case['prompt'],key=key+'-voice')
                turn=interpret(client,case['sid'],text,key,voice)
                admitted=client.post(base+f'/agent-turns/{turn["agent_turn_id"]}/control-admission')
                assert admitted.status_code==200 and admitted.json()['status']=='admitted',admitted.text
                return turn
            first=control('first-submit')
            answer=dict(prompt_id=body['prompt_id'],item_version=body['item_version'],answer=body['text'],
                client_action_id='saved-original-answer',authorized_turn_id=first['agent_turn_id'],
                writing_original_artifact_id=original['artifact_id'],writing_original_revision=original['original_revision'])
            with monkeypatch.context() as fault:
                def interrupted(*a,**kw):raise DiagnosticPromptError('synthetic_after_durable_answer')
                fault.setattr(DiagnosticPromptService,'resume',interrupted)
                assert client.post(base+'/diagnostic-prompt/answer',json=answer).status_code==409
            q=query(body,answer['client_action_id'])
            assert client.get(base+'/native-writing-receipt',params=q).json()['state']=='answer_saved'
            fresh=control('explicit-resume')
            request=dict(target=fresh['action']['native_writing_target'],authorized_turn_id=fresh['agent_turn_id'],
                client_action_id='native-writing-resume:'+fresh['agent_turn_id'],capture_action_id=body['client_action_id'])
            yield dict(case=case,body=body,base=base,client=client,original=original,answer=answer,first=first,
                request=request,query=q,settings=settings,control=control,
                finish_original_client=original_client_lifecycle.close)


def test_exact_resume_survives_loss_and_process_restart_once(tmp_path,monkeypatch):
    with saved_answer(tmp_path,monkeypatch) as value:
        case=value['case']; request=value['request']; base=value['base']
        assert request['target']['saved_answer']['answer_action_id']==value['answer']['client_action_id']
        assert request['target']['saved_answer']['authorized_turn_id']==value['first']['agent_turn_id']
        import test_native_writing_receipt_http as process_support
        monkeypatch.setattr(process_support,'_receipt_process',_synthetic_voice_process)
        # A restart retires the original parent server/lifespan before the new
        # process takes the unchanged exclusive initialization preflight lock.
        value['finish_original_client']()
        assert value['client'].is_closed
        # Separate backend process resumes the already saved answer; only its reply is lost.
        with _process_client(case,expected_losses=1,semantic=True) as (client,first_pid):
            # Startup retires running controls. Admission must be new in this process,
            # not an attempt to revive the pre-restart command.
            voice=capture(client,value['settings'],monkeypatch,case['sid'],'继续提交本题',case['prompt'],key='after-restart-voice')
            turn=interpret(client,case['sid'],'继续提交本题','after-restart-resume',voice)
            assert client.post(base+f'/agent-turns/{turn["agent_turn_id"]}/control-admission').json()['status']=='admitted'
            request={**request,'target':turn['action']['native_writing_target'],'authorized_turn_id':turn['agent_turn_id'],
                'client_action_id':'native-writing-resume:'+turn['agent_turn_id']}
            response=client.post(base+'/native-writing-resume',json=request,headers={'X-Synthetic-Receipt-Loss':'1'})
            assert response.status_code==503,response.text
        with _process_client(case) as (client,second_pid):
            q={**value['query'],'resume_action_id':request['client_action_id']}
            with case['db'].connect() as c:before=list(c.iterdump())
            receipt=client.get(base+'/native-writing-receipt',params=q)
            assert receipt.status_code==200,receipt.text
            assert receipt.json()['state']=='answered'
            assert receipt.json()['resume']['authorized_turn_id']==request['authorized_turn_id']
            assert receipt.json()['resume']['original_authorized_turn_id']==value['first']['agent_turn_id']
            replay=client.post(base+'/native-writing-resume',json=request)
            assert replay.status_code==200,replay.text
            assert replay.json()['result']['attempt']['attempt_id']==receipt.json()['result']['attempt']['attempt_id']
            with case['db'].connect() as c:
                assert list(c.iterdump())==before
                row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(value['body']['prompt_id'],)).fetchone()
                assert row['authorized_turn_id']==value['first']['agent_turn_id']
                assert row['answer_action_id']==value['answer']['client_action_id']
                assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE client_action_id=?',('diagnostic-prompt-answer:'+row['prompt_id'],)).fetchone()[0]==1
                assert c.execute("SELECT count(*) FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()[0]==1
                assert c.execute("SELECT count(*) FROM audit_event WHERE action='native_writing_resumed'").fetchone()[0]==1
            (tmp_path/'exact-resume.json').write_text(json.dumps(dict(pids=[first_pid,second_pid],
                old_authority=value['first']['agent_turn_id'],new_authority=request['authorized_turn_id'],
                resume_action_id=request['client_action_id'],answer_action_id=row['answer_action_id'],
                artifact_id=value['original']['artifact_id'],attempt_id=row['attempt_id'],
                originals=1,attempts=1,links=1,owned_processes_remaining=0,external_paid_posts=0,
                query_readonly=True,replayed_without_effects=True),indent=2))


@pytest.mark.parametrize('fault',['child','version','original_revision','answer_key','cancelled_input','deleted_original','cancelled_prompt'])
def test_saved_resume_rejects_changed_scope_and_revocation(tmp_path,monkeypatch,fault):
    with saved_answer(tmp_path,monkeypatch) as value:
        request=copy.deepcopy(value['request']); client=value['client']; base=value['base']; case=value['case']
        if fault=='child':request['target']['child_id']='child-junior-3'
        if fault=='version':request['target']['item_version']='changed-version'
        if fault=='original_revision':request['target']['saved_answer']['original_revision']='f'*64
        if fault=='answer_key':request['target']['saved_answer']['answer_action_id']='other-answer'
        if fault=='cancelled_input':
            assert client.post(base+'/asr/actions/explicit-resume-voice/cancel').status_code==200
        if fault=='deleted_original':
            parent=case['client']
            unlocked = parent.post('/v1/parent/unlock', json={'pin': '2468'})
            assert unlocked.status_code == 200, unlocked.text
            deleted=parent.request('DELETE','/v1/parent/capture-artifacts/'+value['original']['artifact_id'],
                headers={'X-HSC-Parent': unlocked.json()['parent_token']},
                json={'confirmation':'确认删除','client_action_id':'delete-original'})
            assert deleted.status_code==200,deleted.text
        if fault=='cancelled_prompt':
            assert client.post(base+'/diagnostic-prompt/cancel',json={k:value['body'][k] for k in ('prompt_id','item_version')}).status_code==200
        with case['db'].connect() as c:before=list(c.iterdump())
        result=client.post(base+'/native-writing-resume',json=request)
        assert result.status_code==409,result.text
        with case['db'].connect() as c:assert list(c.iterdump())==before


def test_final_writer_revocation_rolls_back_attempt_and_link_then_fresh_control_recovers(tmp_path,monkeypatch):
    import home_study_agent.native_writing_resume as module
    with saved_answer(tmp_path,monkeypatch) as value:
        client=value['client']; base=value['base']; case=value['case']
        original=DiagnosticPromptService.resume
        def revoke_after_write(service,*args,**kwargs):
            result=original(service,*args,**kwargs)
            with service._transaction() as c:
                c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?",(value['original']['artifact_id'],))
            return result
        with case['db'].connect() as c:before=list(c.iterdump())
        with monkeypatch.context() as fault:
            fault.setattr(DiagnosticPromptService,'resume',revoke_after_write)
            response=client.post(base+'/native-writing-resume',json=value['request'])
            assert response.status_code==409,response.text
        with case['db'].connect() as c:
            assert list(c.iterdump())==before
            assert not module.validate_active_resume(c,value['first']['agent_turn_id'],child_id=value['body']['child_id'],
                session_id=case['sid'],binding={k:value['body'][k] for k in ('prompt_id','item_version')},answer=value['body']['text'])
        q={**value['query'],'resume_action_id':value['request']['client_action_id']}
        receipt=client.get(base+'/native-writing-receipt',params=q)
        assert receipt.json()['state']=='answer_saved' and receipt.json()['resume'] is None
        # The old saved fact is not a license for generic resume after a new input.
        assert client.post(base+'/diagnostic-prompt/resume',json={k:value['body'][k] for k in ('prompt_id','item_version')}).status_code==409
        fresh=value['control']('after-interruption')
        request={
            **value['request'],'target':fresh['action']['native_writing_target'],'authorized_turn_id':fresh['agent_turn_id'],
            'client_action_id':'native-writing-resume:'+fresh['agent_turn_id']}
        result=client.post(base+'/native-writing-resume',json=request)
        assert result.status_code==200,result.text
        with case['db'].connect() as c:
            row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(value['body']['prompt_id'],)).fetchone()
            assert row['status']=='answered' and row['authorized_turn_id']==value['first']['agent_turn_id']
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE client_action_id=?',('diagnostic-prompt-answer:'+row['prompt_id'],)).fetchone()[0]==1


def test_actual_renderer_saved_resume_and_unknown_receipt_three_processes(tmp_path,monkeypatch):
    import os
    from pathlib import Path
    import subprocess
    import test_native_writing_receipt_http as process_support
    root=Path(__file__).resolve().parents[3]
    monkeypatch.setattr(process_support,'_receipt_process',_interrupted_answer_process)
    with scheduled(tmp_path/'data') as case:
        body=draft(case); pids=[]; journal=tmp_path/'browser-storage.json'
        case['finish_client']()
        assert case['client'].is_closed
        for phase in ('saved_capture','saved_resume','saved_read'):
            with _process_client(case,expected_losses=1 if phase=='saved_resume' else 0,semantic=True,
                                 session_token='public-native-writing-tcp-session') as (client,pid):
                pids.append(pid)
                config=tmp_path/(phase+'-input.json'); log=tmp_path/(phase+'-renderer.log')
                config.write_text(json.dumps(dict(base_url=str(client.base_url).rstrip('/'),body=body,key=KEY,
                    phase=phase,journal=str(journal),result=str(tmp_path/(phase+'-result.json')),
                    session_token='public-native-writing-tcp-session',runtime_epoch=len(pids))))
                with log.open('w') as output:
                    result=subprocess.run([str(root/'node_modules/.bin/vitest'),'run','src/renderer/src/NativeWritingRealTcp.test.tsx'],
                        cwd=root,env={**os.environ,'HSC_NATIVE_TCP_CASE':str(config)},stdout=output,stderr=subprocess.STDOUT,timeout=90)
                assert result.returncode==0,log.read_text()[-4000:]
        with case['db'].connect() as c:
            row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(body['prompt_id'],)).fetchone()
            assert row['status']=='answered' and row['authorized_turn_id'] is None
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE client_action_id=?',('diagnostic-prompt-answer:'+row['prompt_id'],)).fetchone()[0]==1
            assert c.execute("SELECT count(*) FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()[0]==1
            assert c.execute("SELECT count(*) FROM audit_event WHERE action='native_writing_resumed'").fetchone()[0]==1
        (tmp_path/'renderer-saved-resume.json').write_text(json.dumps(dict(pids=pids,originals=1,attempts=1,
            original_authority=None,answer_action_id=row['answer_action_id'],attempt_id=row['attempt_id'],
            owned_processes_remaining=0,external_paid_posts=0,actual_tcp_renderer=True,
            boundary='Real main signer/nativeWritingFlow/SessionWorkspace/localApi/Runtime/router/SQLite; synthetic microphone transcript and speech, jsdom not Electron or Windows.'),indent=2))
