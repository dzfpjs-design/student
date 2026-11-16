"""Real Parent portable flow and original short-reader admission boundaries."""
import asyncio,json,sys,threading
from contextlib import contextmanager
from dataclasses import replace
from fastapi.testclient import TestClient
import pytest
from home_study_agent.app import create_app
from home_study_agent.parent_auth import ParentSessionRequiredError
from home_study_agent import parent_restore_parent as parent_restore
from home_study_agent import parent_restore_privacy as parent_privacy
from home_study_agent import restore_publication as publication
from privacy_restore_operation import restore_operation
from test_diagnostic_prompt_http import environment
from test_companion_request_provenance_http import planner
from test_voice_activity_capture_lifecycle import captured,parent
from test_voice_activity_capture_lifecycle import test_restored_activity_recording_stays_cancelled_and_never_restores_focus as original_body

def _snapshot(database):
    with database.connect()as c:
        names={r[0]for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {n:tuple(sorted(tuple(r)for r in c.execute('SELECT *FROM '+n)))for n in
            ('privacy_operation_intent','restore_parent_authority','restore_asset_generation','restore_asset_member','daily_session','voice_capture_binding')if n in names}

def _hold_original_reader(monkeypatch,*,release_on_attempt):
    facts={'original_body_executions':0,'portable_restore_POSTs':0,'admission_attempts':[],
        'original_reader_opened':False,'original_reader_closed':False,'reader_SQL_changes':None}
    original_post=TestClient.post
    def observed_post(client,url,*args,**kwargs):
        if url!='/v1/parent/backups/portable/restore':return original_post(client,url,*args,**kwargs)
        facts['portable_restore_POSTs']+=1;assert facts['portable_restore_POSTs']==1
        db=client.app.state.repository.database;gate=publication._gate(publication._path(db.path))
        before=_snapshot(db);ready=threading.Event();release_holder={};old_trace={}
        async def original_reader():
            release=asyncio.Event();release_holder['event']=release
            with db.connect()as c:
                before=c.total_changes;c.execute('SELECT COUNT(*)FROM daily_session').fetchone()
                facts['original_reader_opened']=True;ready.set()
                await release.wait()
                facts['reader_SQL_changes']=c.total_changes-before
            facts['original_reader_closed']=True
        reader=client.portal.start_task_soon(original_reader)
        assert ready.wait(5),'ORIGINAL_READER_DID_NOT_OPEN'
        def trace(frame,event,arg):
            if(frame.f_code.co_filename.endswith('/src/home_study_agent/restore_publication.py')
                and frame.f_code.co_name=='_writer_precheck'and event=='call'and frame.f_locals.get('g')is gate):
                actor=frame.f_locals['actor']
                facts['admission_attempts'].append({'sql_opening':gate.sql_opening,'sql_connections':len(gate.sql_connections),
                    'active_actors':len(gate.active),'active_leases':sum(gate.active.values()),'waiting':gate.waiting,
                    'poisoned':gate.poisoned,'request_is_async':actor[1]is not None})
                # Only the real holder's event is signalled. It leaves its
                # original Database CM on its next event-loop turn.
                if release_on_attempt:release_holder['event'].set()
            return None
        async def arm():old_trace['value']=sys.gettrace();sys.settrace(trace)
        async def disarm():sys.settrace(old_trace['value']);release_holder['event'].set()
        client.portal.call(arm)
        try:
            response=original_post(client,url,*args,**kwargs)
            facts['actual_restore_status']=response.status_code
            body=response.json();facts['actual_code']=body.get('code')if isinstance(body,dict)else None
            return response
        finally:
            client.portal.call(disarm);reader.result(timeout=5)
            facts['restore_business_state_unchanged']=before==_snapshot(db)
    monkeypatch.setattr(TestClient,'post',observed_post)
    return facts

def test_transient_original_reader_yields_before_same_original_portable_body(environment,monkeypatch):
    facts=_hold_original_reader(monkeypatch,release_on_attempt=True)
    try:
        facts['original_body_executions']+=1
        original_body(environment,monkeypatch,True)
        assert facts['actual_restore_status']==200
        assert facts['portable_restore_POSTs']==facts['original_body_executions']==1
        assert facts['admission_attempts'][0]['active_leases']==1
        assert all(v['request_is_async']and not v['poisoned']for v in facts['admission_attempts'])
        assert facts['original_reader_closed']and facts['reader_SQL_changes']==0
    finally:print('ORIGINAL_READER_METADATA '+json.dumps(facts))

def test_reader_that_remains_held_still_returns_busy_without_restore_writes(environment,monkeypatch):
    settings,db=environment;settings=replace(settings,data_encryption_key=b'L'*32,native_launch_attestation_key=None)
    facts=_hold_original_reader(monkeypatch,release_on_attempt=False)
    try:
        with TestClient(create_app(settings,model_gateway=planner()))as client:
            parent(client);sid,focus,body,vid=captured(client,settings,db,monkeypatch)
            saved=client.post('/v1/parent/backups/portable',json={'passphrase':'isolated activity backup passphrase'})
            assert saved.status_code==200,saved.text
            assert client.post(f'/v1/sessions/{sid}/asr/actions/{body["client_action_id"]}/cancel').status_code==200
            with db.connect()as c:
                c.execute("UPDATE daily_session SET status='completed'WHERE session_id=?",(sid,));c.commit()
            result=client.post('/v1/parent/backups/portable/restore',json={'path':saved.json()['path'],
                'passphrase':'isolated activity backup passphrase','confirmation':'确认迁移恢复','operation':restore_operation(client)})
            assert result.status_code==409,result.text
            assert result.json()=={'code':'RESTORE_PUBLICATION_BUSY','message':'数据正在使用，请稍后重试原操作。',
                'retryable':True,'recovery_action':'retry_original_request'}
            assert facts['portable_restore_POSTs']==1 and 1<len(facts['admission_attempts'])<=201
            assert facts['restore_business_state_unchanged']and facts['original_reader_closed']and facts['reader_SQL_changes']==0
    finally:print('ORIGINAL_HELD_READER_METADATA '+json.dumps(facts))

def test_only_writer_entry_busy_retries_not_body_exit_cancel_auth_or_native_policy(environment,monkeypatch):
    settings,db=environment;settings=replace(settings,native_launch_attestation_key=None)
    with TestClient(create_app(settings,model_gateway=planner()))as client:
        parent(client);database=client.app.state.repository.database
        auth=parent_privacy._registered(database);token=client.headers['X-HSC-Parent']
        original_owner=publication.publication_owner
        for mode in('body_busy','body_cancel','exit_busy','entry_poison','native_header','invalid_token'):
            calls=[];body_calls=[];actor_holder={};coroutine_errors=[]
            error=asyncio.CancelledError()if mode=='body_cancel'else publication.RestorePublicationError(
                'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED'if mode=='entry_poison'else'RESTORE_PUBLICATION_BUSY')
            @contextmanager
            def observed_owner(*args,**kwargs):
                actor=publication._actor();expected=actor_holder.get('value')
                mine=expected is not None and actor[0]==expected[0]and actor[1]is expected[1]
                if mine:calls.append({'body_started':bool(body_calls)})
                if mine and mode=='entry_poison':raise error
                with original_owner(*args,**kwargs)as owner:yield owner
                if mine and mode=='exit_busy':raise error
            async def check():
                actor_holder['value']=publication._actor()
                try:
                    async with parent_restore.admitted_parent_restore_owner(database,auth,
                            'invalid-parent-token'if mode=='invalid_token'else token,native_header_present=mode=='native_header'):
                        body_calls.append(1)
                        if mode in{'body_busy','body_cancel'}:raise error
                except BaseException as observed:
                    coroutine_errors.append(observed)
                    raise
            with monkeypatch.context()as patch:
                patch.setattr(publication,'publication_owner',observed_owner)
                if mode=='invalid_token':
                    with pytest.raises(ParentSessionRequiredError):asyncio.run(check())
                    assert calls==body_calls==[]
                elif mode=='native_header':
                    with pytest.raises(publication.RestorePublicationError,match=parent_restore.CODE):asyncio.run(check())
                    assert calls==body_calls==[]
                else:
                    with pytest.raises(type(error))as caught:asyncio.run(check())
                    assert len(coroutine_errors)==1 and coroutine_errors[0]is error
                    if mode!='body_cancel':assert caught.value is error
                    if mode=='entry_poison':assert len(calls)==1
                    else:assert calls and not any(v['body_started']for v in calls)
                    assert len(body_calls)==(0 if mode=='entry_poison'else 1)
            print('OWNER_BOUNDARY_METADATA '+json.dumps({'mode':mode,'entry_calls':calls,'body_calls':len(body_calls)}))
            assert not publication._gate(publication._path(database.path)).poisoned
