"""Real native HTTP restore, original read leases and entry-only retry boundaries."""
import asyncio,inspect,json,sys,threading
from contextlib import contextmanager
import pytest
from home_study_agent import restore_publication as pub,native_privacy as native
from home_study_agent.parent_auth import ParentSessionRequiredError
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.parent_restore_security import verify_native_privacy_proof
from test_restore_incomplete_protection import seed,client,auth
from test_native_privacy_permit_http import certificate,signed_header
from privacy_restore_operation import restore_operation
from synthetic_native_restore_request import KEY


def _request(c,life,kind):
    if kind=='restore':
        backup=life.create_backup(prune=False);path='/v1/parent/backups/restore'
        body={'backup_id':backup['backup_id'],'confirmation':'确认恢复','operation':restore_operation(c)}
    else:
        backup=life.create_portable_backup('isolated native admission passphrase');path='/v1/parent/backups/portable/restore'
        body={'path':backup['path'],'passphrase':'isolated native admission passphrase','confirmation':'确认迁移恢复','operation':restore_operation(c)}
    proof=certificate(path,body,body['operation'],kind=kind,child_id=None,scope=None)
    return path,body,proof


def _held_reader(c,proof,*,release_on_entry):
    database=c.app.state.repository.database;gate=pub._gate(pub._path(database.path))
    facts={'reader_opened':False,'reader_closed':False,'reader_DML':None,'claim_calls':0,
           'restore_body_calls':0,'native_finish_calls':0,'proof_checks':0,'entry_states':[]}
    ready=threading.Event();holder={};prior={}
    async def read():
        holder['release']=asyncio.Event()
        with database.connect()as sql:
            changed=sql.total_changes;sql.execute('SELECT count(*)FROM daily_session').fetchone()
            facts['reader_opened']=True;ready.set();await holder['release'].wait()
            facts['reader_DML']=sql.total_changes-changed
        facts['reader_closed']=True
    owned=c.portal.start_task_soon(read);assert ready.wait(5)
    def trace(frame,event,arg):
        if event!='call':return None
        if frame.f_code is native.claim.__code__:facts['claim_calls']+=1
        elif frame.f_code is native.finish.__code__:facts['native_finish_calls']+=1
        elif frame.f_code in(PrivacyLifecycleService.restore_backup.__code__,PrivacyLifecycleService.restore_portable_backup.__code__):facts['restore_body_calls']+=1
        elif frame.f_code is verify_native_privacy_proof.__code__:facts['proof_checks']+=1
        elif frame.f_code is pub._writer_precheck.__code__ and frame.f_locals.get('g')is gate and not facts['claim_calls']:
            facts['entry_states'].append({'active_leases':sum(gate.active.values()),'poisoned':gate.poisoned,'request_is_async':pub._actor()[1]is not None})
            if release_on_entry:holder['release'].set()
        return None
    async def arm():prior['trace']=sys.gettrace();sys.settrace(trace)
    async def close():sys.settrace(prior['trace']);holder['release'].set()
    c.portal.call(arm)
    @contextmanager
    def scope():
        try:yield facts
        finally:
            c.portal.call(close);owned.result(timeout=5)
            assert facts['reader_closed']and facts['reader_DML']==0
    return scope()


@pytest.mark.parametrize('kind',['restore','portable_restore'])
def test_short_original_reader_yields_before_one_native_claim_and_real_restore(tmp_path,kind):
    db,life,pdf=seed(tmp_path)
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);path,body,proof=_request(c,life,kind)
        with _held_reader(c,proof,release_on_entry=True)as facts:
            result=c.post(path,headers=signed_header(proof),json=body)
        assert result.status_code==200,result.text
        assert facts['entry_states'][0]['active_leases']==1
        assert all(v['request_is_async']and not v['poisoned']for v in facts['entry_states'])
        assert facts['proof_checks']>=3
        assert facts['claim_calls']==facts['restore_body_calls']==facts['native_finish_calls']==1
        with db.connect()as sql:
            assert sql.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert sql.execute('PRAGMA foreign_key_check').fetchone()is None
            assert sql.execute('SELECT status FROM native_privacy_permit WHERE nonce=?',(proof['nonce'],)).fetchone()[0]=='finished'
        print('NATIVE_ADMISSION '+json.dumps({'kind':kind,**facts}))


def test_long_original_reader_keeps_409_and_consumes_no_native_permit(tmp_path):
    db,life,pdf=seed(tmp_path)
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);path,body,proof=_request(c,life,'restore')
        with db.connect()as sql:before=sql.execute('SELECT count(*)FROM privacy_operation_intent').fetchone()[0]
        with _held_reader(c,proof,release_on_entry=False)as facts:
            result=c.post(path,headers=signed_header(proof),json=body)
        assert result.status_code==409 and result.json()['code']=='RESTORE_PUBLICATION_BUSY',result.text
        assert result.json()['retryable']is True and result.json()['recovery_action']=='retry_original_request'
        assert 1<facts['proof_checks']-1<=201
        assert facts['claim_calls']==facts['restore_body_calls']==facts['native_finish_calls']==0
        with db.connect()as sql:
            present=sql.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()
            assert not present or sql.execute('SELECT count(*)FROM native_privacy_permit WHERE nonce=?',(proof['nonce'],)).fetchone()[0]==0
            assert sql.execute('SELECT count(*)FROM privacy_operation_intent').fetchone()[0]==before
        assert not list(tmp_path.glob('.restore-work-*'))
        print('NATIVE_HELD_ADMISSION '+json.dumps(facts))


def _admission(c):
    endpoint=next(r.endpoint for r in c.app.routes if getattr(r,'path',None)=='/v1/parent/backups/restore')
    return inspect.getclosurevars(endpoint).nonlocals['admitted_native_restore_owner']


def test_entry_rechecks_authority_and_never_retries_body_exit_cancel_or_other_error(tmp_path,monkeypatch):
    db,life,pdf=seed(tmp_path)
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);path,body,proof=_request(c,life,'restore');admit=_admission(c)
        parent=inspect.getclosurevars(admit.__wrapped__).nonlocals['parent_auth']
        original_owner=pub.publication_owner
        for mode in('body_busy','body_cancel','exit_busy','entry_poison','entry_other','entry_cancel','invalid_token','parent_revoked','proof_changed'):
            token=c.headers['X-HSC-Parent'];value=dict(proof);metadata=native.verify(value,KEY)
            entries=[];bodies=[];actors={};caught=[]
            error=(asyncio.CancelledError()if mode in{'body_cancel','entry_cancel'}else ValueError('fixed-public-test')if mode=='entry_other'
                   else pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED'if mode=='entry_poison'else'RESTORE_PUBLICATION_BUSY'))
            @contextmanager
            def observed_owner(*args,**kwargs):
                mine=pub._actor()==actors.get('actor')
                if mine:
                    entries.append(bool(bodies))
                    if mode in{'entry_poison','entry_other','entry_cancel'}:raise error
                    if mode in{'parent_revoked','proof_changed'}:
                        assert len(entries)==1
                        if mode=='parent_revoked':parent._tokens.pop(token)
                        else:value['signature']='0'*64
                        raise pub.RestorePublicationError('RESTORE_PUBLICATION_BUSY')
                with original_owner(*args,**kwargs)as owner:yield owner
                if mine and mode=='exit_busy':raise error
            async def run():
                actors['actor']=pub._actor()
                try:
                    async with admit('invalid-parent-token'if mode=='invalid_token'else token,value,metadata):
                        bodies.append(1)
                        if mode in{'body_busy','body_cancel'}:raise error
                except BaseException as observed:caught.append(observed)
            with monkeypatch.context()as patch:
                patch.setattr(pub,'publication_owner',observed_owner);c.portal.call(run)
            assert len(caught)==1 and not any(entries)
            if mode in{'body_busy','body_cancel','exit_busy','entry_poison','entry_other','entry_cancel'}:assert caught[0]is error
            if mode in{'invalid_token','parent_revoked'}:assert isinstance(caught[0],ParentSessionRequiredError)
            if mode=='proof_changed':assert caught[0].status_code==409
            assert len(bodies)==(1 if mode in{'body_busy','body_cancel','exit_busy'}else 0)
            if mode in{'parent_revoked','proof_changed','entry_poison','entry_other','entry_cancel'}:assert len(entries)==1
            if mode=='invalid_token':assert entries==[]
            assert not pub._gate(pub._path(db.path)).poisoned
            if mode=='parent_revoked':auth(c,restart=True)
            print('NATIVE_BOUNDARY '+json.dumps({'mode':mode,'entries':len(entries),'bodies':len(bodies),'same_original_error':caught[0]is error}))


def test_cancel_during_real_held_reader_consumes_no_permit_or_body(tmp_path):
    from concurrent.futures import CancelledError
    db,life,pdf=seed(tmp_path)
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);path,body,proof=_request(c,life,'restore');admit=_admission(c);started=threading.Event();reached=[]
        async def request():
            started.set()
            async with admit(c.headers['X-HSC-Parent'],proof,native.verify(proof,KEY)):reached.append(True)
        with _held_reader(c,proof,release_on_entry=False)as facts:
            original=c.portal.start_task_soon(request);assert started.wait(5)
            async def wait_for_failed_entry():
                while not facts['entry_states']:await asyncio.sleep(0)
            c.portal.call(wait_for_failed_entry);original.cancel()
            with pytest.raises(CancelledError):original.result(timeout=5)
        assert not reached and facts['claim_calls']==facts['restore_body_calls']==facts['native_finish_calls']==0
        with db.connect()as sql:
            present=sql.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()
            assert not present or sql.execute('SELECT count(*)FROM native_privacy_permit WHERE nonce=?',(proof['nonce'],)).fetchone()[0]==0
        assert not pub._gate(pub._path(db.path)).poisoned


@pytest.mark.parametrize('stage',['claim','body'])
def test_http_claim_or_body_busy_never_reenters_admission_or_dispatches_again(tmp_path,monkeypatch,stage):
    db,life,pdf=seed(tmp_path);calls={'claim':0,'body':0,'finish':0}
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);path,body,proof=_request(c,life,'restore')
        error=pub.RestorePublicationError('RESTORE_PUBLICATION_BUSY')
        original_claim=native.claim;original_finish=native.finish
        def claim(*args,**kwargs):
            calls['claim']+=1
            if stage=='claim':raise error
            return original_claim(*args,**kwargs)
        def fail_body(*args,**kwargs):
            calls['body']+=1;raise error
        def finish(*args,**kwargs):
            calls['finish']+=1;return original_finish(*args,**kwargs)
        with monkeypatch.context()as patch:
            patch.setattr(native,'claim',claim);patch.setattr(native,'finish',finish)
            patch.setattr(PrivacyLifecycleService,'restore_backup',fail_body)
            result=c.post(path,headers=signed_header(proof),json=body)
        assert result.status_code==409 and result.json()['code']=='RESTORE_PUBLICATION_BUSY',result.text
        assert calls=={'claim':1,'body':int(stage=='body'),'finish':int(stage=='body')}
        with db.connect()as sql:
            present=sql.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()
            if stage=='claim':assert not present or sql.execute('SELECT count(*)FROM native_privacy_permit WHERE nonce=?',(proof['nonce'],)).fetchone()[0]==0
            else:assert sql.execute('SELECT status FROM native_privacy_permit WHERE nonce=?',(proof['nonce'],)).fetchone()[0]=='failed'
            assert sql.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert sql.execute('PRAGMA foreign_key_check').fetchone()is None
        assert not list(tmp_path.glob('.restore-work-*'))
