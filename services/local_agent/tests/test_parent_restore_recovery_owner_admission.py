"""Real restricted HTTP with synthetic original failed native restore; no cloud/Windows."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import socket
import sqlite3
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.parent_auth import ParentAuth
from home_study_agent import restore_publication as pub, parent_restore_assets as assets
from home_study_agent import parent_restore_ownership as ownership, original_connection_read as read_identity
from home_study_agent.privacy_operation_receipt import PrivacyOperationReceipts
from privacy_restore_operation import restore_operation
from synthetic_native_restore_request import KEY
from test_native_privacy_permit_http import certificate, signed_header
from test_learning_runtime_http import NoCloud


@pytest.fixture(scope='module')
def original(tmp_path_factory):
    root=tmp_path_factory.mktemp('original-sticky-recovery').resolve(strict=True)
    patch=pytest.MonkeyPatch()
    patch.setattr(socket.socket,'connect',lambda *a,**k:(_ for _ in ()).throw(AssertionError('NETWORK_FORBIDDEN')))
    settings=Settings(data_dir=root,database_path=root/'family.db',data_encryption_key=b'0'*32,
        native_launch_attestation_key=KEY,maintenance_enabled=False,agent_runtime_enabled=False,
        session_token='public-synthetic-original-session')
    try:
        with TestClient(create_app(settings,model_gateway=NoCloud()),raise_server_exceptions=False)as c:
            c.headers['X-HSC-Session']=settings.session_token
            setup=c.post('/v1/parent/setup',json={'pin':'4826'});assert setup.status_code==200,setup.text
            headers={'X-HSC-Parent':setup.json()['parent_token']}
            backup=c.post('/v1/parent/backups/portable',headers=headers,json={'passphrase':'public original valid portable phrase'})
            assert backup.status_code==200,backup.text
            operation=restore_operation(c,headers=headers)
            path='/v1/parent/backups/portable/restore'
            body={'path':backup.json()['path'],'passphrase':'public intentionally wrong portable phrase','confirmation':'确认迁移恢复','operation':operation}
            proof=certificate(path,body,operation,kind='portable_restore',child_id=None,scope=None)
            failed=c.post(path,headers={**headers,**signed_header(proof)},json=body)
            assert failed.status_code==422 and failed.json()['code']=='INVALID_BACKUP',failed.text
            assert c.get('/v1/health').json()['status']=='recovery_only'
            fence=c.post('/v1/parent/native-privacy-permits/fence',headers=headers,json=proof)
            assert fence.status_code==200 and fence.json()['status']=='failed',fence.text
            assert c.get('/v1/parent/status').json()['configured']is True
            env=SimpleNamespace(client=c,db=c.app.state.privacy_lifecycle.database,auth=c.app.state.parent_auth,
                operation=operation,proof=proof,real_owner=pub.publication_owner,session=settings.session_token)
            assert_unchanged(env)
            yield env
            assert_unchanged(env)
    finally:patch.undo()


def fresh_headers(env):
    result=env.client.post('/v1/parent/unlock',json={'pin':'4826'})
    assert result.status_code==200,result.text
    return {'X-HSC-Parent':result.json()['parent_token']}


def assert_unchanged(env):
    with env.real_owner(env.db,timeout=5)as owner:
        with pub._cold(env.db.path,owner=owner)as sql:
            anchor=ownership._anchor(sql)
            row=sql.execute('SELECT state,finalized,reference_type,reference_id,result_generation,origin_generation FROM privacy_operation_intent WHERE request_id=?',(env.operation['request_id'],)).fetchone()
            generation=sql.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()[0]
            native=sql.execute('SELECT status FROM native_privacy_permit').fetchall()
            assert anchor['state']=='DONE' and anchor['request_id']==env.operation['request_id']
            assert tuple(row)==('failed',0,None,None,None,generation)
            assert [x[0]for x in native]==['failed']
            assert sql.execute('SELECT count(*) FROM privacy_operation_intent').fetchone()[0]==1
            assert sql.execute('SELECT count(*) FROM model_call').fetchone()[0]==0
    health=env.client.get('/v1/health')
    assert health.status_code==200 and health.json()['normal_business_ready']is False
    assert env.client.get('/v1/children').status_code==503


@contextmanager
def held_reader(env,release_after=None):
    ready=threading.Event();state={'opened':False,'closed':False,'DML':None,'ticks':0}
    async def read():
        state['release']=asyncio.Event()
        with env.db.connect()as sql:
            before=sql.total_changes;sql.execute('SELECT count(*) FROM daily_session').fetchone()
            state['actor']=pub._actor();state['connection']=sql;state['ticket']=sql._original_read_locator
            state['admission']=read_identity._CONNECTIONS[state['ticket']][2]()
            state['opened']=True;ready.set();await state['release'].wait();state['DML']=sql.total_changes-before
        state['closed']=True
    task=env.client.portal.start_task_soon(read);assert ready.wait(5)
    timer=None
    if release_after is not None:
        async def later():
            for _ in range(5):await asyncio.sleep(release_after/5);state['ticks']+=1
            state['release'].set()
        timer=env.client.portal.start_task_soon(later)
    try:yield state
    finally:
        async def release():state['release'].set()
        env.client.portal.call(release);task.result(5)
        if timer is not None:timer.result(5)
        assert state['closed']and state['DML']==0


def spy_unlock(env,monkeypatch,error=None):
    old=ParentAuth.unlock;calls=[]
    def once(auth,pin):
        if auth is env.auth:
            calls.append(True)
            if error is not None:raise error
        return old(auth,pin)
    monkeypatch.setattr(ParentAuth,'unlock',once)
    return calls


def spy_owner(env,monkeypatch,entry_error=None,exit_error=None):
    old=pub.publication_owner;facts={'enters':0,'exits':0,'exit_exceptions':[],'entry_actors':set(),'acquired':0,'acquired_owners':[]}
    @contextmanager
    def observe(database,*args,**kwargs):
        if database is not env.db:
            with old(database,*args,**kwargs)as owner:yield owner
            return
        facts['enters']+=1;facts['entry_actors'].add(pub._actor())
        if entry_error is not None:raise entry_error
        with old(database,*args,**kwargs)as owner:
            facts['acquired']+=1;facts['acquired_owners'].append(owner)
            try:yield owner
            except BaseException as error:
                facts['exit_exceptions'].append(type(error).__name__);raise
            finally:facts['exits']+=1
        if exit_error is not None:raise exit_error
    monkeypatch.setattr(pub,'publication_owner',observe)
    return facts


def test_transient_original_reader_waits_then_unlocks_once(original,monkeypatch):
    env=original;calls=spy_unlock(env,monkeypatch);owner=spy_owner(env,monkeypatch)
    with held_reader(env,release_after=0.06)as reader:
        async def concurrent_health():
            await asyncio.sleep(0.02)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.client.app),base_url='http://testserver',headers={'X-HSC-Session':env.session})as client:
                result=await client.get('/v1/health')
            assert reader['opened']and not reader['closed']and len(calls)==0
            return result.status_code,result.json()['status']
        health=env.client.portal.start_task_soon(concurrent_health)
        response=env.client.post('/v1/parent/unlock',json={'pin':'4826'})
        assert health.result(5)==(200,'recovery_only')
    assert response.status_code==200,response.text
    assert len(calls)==1 and owner['enters']>=2 and owner['exits']==1 and reader['ticks']==5
    status=env.client.get('/v1/parent/recovery-status',headers={'X-HSC-Parent':response.json()['parent_token']})
    assert status.status_code==200 and status.json()['state']=='RESOLVED_NOT_APPLIED'
    assert status.json()['request_id']==env.operation['request_id']
    assert_unchanged(env)


def test_persistent_reader_has_bounded_exact_busy_without_business(original,monkeypatch):
    env=original;calls=spy_unlock(env,monkeypatch);owner=spy_owner(env,monkeypatch)
    with held_reader(env):
        started=time.monotonic();response=env.client.post('/v1/parent/unlock',json={'pin':'4826'});elapsed=time.monotonic()-started
        assert env.client.get('/v1/health').json()['status']=='recovery_only'
    assert response.status_code==409 and response.json()=={'code':'RESTORE_PUBLICATION_BUSY','message':'数据正在使用，请稍后重试原操作。','retryable':True,'recovery_action':'retry_original_request'},response.text
    assert response.headers['Retry-After']=='1'and response.headers['Cache-Control']=='no-store'
    assert 1.8<=elapsed<3.5 and 2<=owner['enters']<=201 and owner['exits']==0 and calls==[]
    assert_unchanged(env)


@pytest.mark.parametrize('stage',['entry','reconcile','admit','body','exit'])
def test_only_entry_busy_retries_other_stages_are_single(original,monkeypatch,stage):
    env=original;calls=spy_unlock(env,monkeypatch,pub.RestorePublicationError('RESTORE_PUBLICATION_BUSY')if stage=='body'else None)
    owner=spy_owner(env,monkeypatch,entry_error=pub.RestorePublicationError('RESTORE_PUBLICATION_DATABASE_ALIAS')if stage=='entry'else None,exit_error=pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')if stage=='exit'else None)
    if stage in{'reconcile','admit'}:
        def fail(*a,**k):raise pub.RestorePublicationError('RESTORE_PUBLICATION_BUSY')
        monkeypatch.setattr(pub if stage=='reconcile'else assets,'reconcile_publication'if stage=='reconcile'else'admit_original_recovery',fail)
    started=time.monotonic();response=env.client.post('/v1/parent/unlock',json={'pin':'4826'});elapsed=time.monotonic()-started
    assert response.status_code==409 and elapsed<1.0,response.text
    assert owner['enters']==1 and owner['exits']==(0 if stage=='entry'else 1)
    assert len(calls)==(1 if stage in{'body','exit'}else 0)
    assert response.json()['code']==('RESTORE_PUBLICATION_RECOVERY_REQUIRED'if stage in{'entry','exit'}else'RESTORE_PUBLICATION_BUSY')
    if stage=='body':assert owner['exit_exceptions']==['RestorePublicationError']


def test_parent_authority_is_rechecked_after_wait(original,monkeypatch):
    env=original;headers=fresh_headers(env);calls=[];old=PrivacyOperationReceipts.read
    def read(self,request_id):
        if self.database is env.db:calls.append(True)
        return old(self,request_id)
    monkeypatch.setattr(PrivacyOperationReceipts,'read',read)
    with held_reader(env)as reader:
        async def revoke():
            await asyncio.sleep(0.04);env.auth._tokens.pop(headers['X-HSC-Parent']);reader['release'].set()
        task=env.client.portal.start_task_soon(revoke)
        response=env.client.get('/v1/parent/recovery-status',headers=headers);task.result(5)
    assert response.status_code==401 and response.json()['code']=='PARENT_SESSION_REQUIRED'and calls==[],response.text
    assert_unchanged(env)


def test_exact_busy_from_original_exit_does_not_reenter_or_replay(original,monkeypatch):
    env=original;calls=spy_unlock(env,monkeypatch)
    owner=spy_owner(env,monkeypatch,exit_error=pub.RestorePublicationError('RESTORE_PUBLICATION_BUSY'))
    response=env.client.post('/v1/parent/unlock',json={'pin':'4826'})
    assert response.status_code==409 and response.json()['code']=='RESTORE_PUBLICATION_BUSY',response.text
    assert owner['enters']==owner['exits']==len(calls)==1
    assert_unchanged(env)


def cancel_waiting_original_request(env,owner):
    async def cancel_request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.client.app),base_url='http://testserver',headers={'X-HSC-Session':env.session})as client:
            request=asyncio.create_task(client.post('/v1/parent/unlock',json={'pin':'4826'}))
            until=asyncio.get_running_loop().time()+1
            while owner['enters']<2 and asyncio.get_running_loop().time()<until:await asyncio.sleep(0.001)
            assert owner['enters']>=2;request.cancel()
            with pytest.raises(asyncio.CancelledError):await request
    env.client.portal.call(cancel_request)


def assert_original_endpoint_actor_retired(env,owner,*,expected_acquired):
    # Owner entry runs in the real endpoint Task, which differs from HTTPX's
    # outer request Task under the original middleware. Track that exact actor.
    assert len(owner['entry_actors'])==1 and owner['acquired']==owner['exits']==expected_acquired
    assert len(owner['acquired_owners'])==expected_acquired
    actor=next(iter(owner['entry_actors']))
    assert actor[1]is not None and actor[1].done()
    gate=pub._gate(pub._path(env.db.path))
    with gate.condition:
        assert gate.owner is None and not gate.sql_connections and gate.sql_opening==0 and not gate.poisoned
        assert not gate.active.get(pub._actor_key(actor),0)
        assert all(type(value)is pub._Owner and value.actor==actor and value.gate is gate and value.active is False for value in owner['acquired_owners'])
        # Original admission tracks every request reader under this exact actor;
        # unrelated lifespan readers may still be admitted concurrently.
    return actor


def assert_original_reader_retired(env,reader):
    assert reader['closed']and reader['DML']==0 and reader['actor'][1].done()
    assert not pub._READ_LEASES[reader['admission']]['active']
    assert reader['ticket']not in read_identity._CONNECTIONS
    assert id(reader['connection'])not in env.db._original_connections
    gate=pub._gate(pub._path(env.db.path))
    with gate.condition:assert not gate.active.get(pub._actor_key(reader['actor']),0)and not gate.poisoned
    async def closed_sqlite():
        with pytest.raises(sqlite3.ProgrammingError,match='closed database'):
            reader['connection'].execute('SELECT 1')
    env.client.portal.call(closed_sqlite)


def test_wait_cancellation_releases_entry_without_business(original,monkeypatch):
    env=original;calls=spy_unlock(env,monkeypatch);owner=spy_owner(env,monkeypatch)
    with held_reader(env)as reader:cancel_waiting_original_request(env,owner)
    assert calls==[] and owner['exits']==0
    assert_original_endpoint_actor_retired(env,owner,expected_acquired=0)
    assert_original_reader_retired(env,reader)
    assert_unchanged(env)


def assert_original_lifespan_alive(env):
    async def original_lifespan_alive():
        code=env.client.app.state.visual_capture.maintain.__func__.__code__
        assert any(getattr(task.get_coro(),'cr_code',None)is code and not task.done()for task in asyncio.all_tasks())
    env.client.portal.call(original_lifespan_alive)


def assert_live_original_foreign_reader(env,reader,actor):
    assert reader['actor']!=actor and not reader['closed']and not reader['actor'][1].done()
    gate=pub._gate(pub._path(env.db.path))
    with gate.condition:
        assert gate.active.get(pub._actor_key(reader['actor']),0)==1
        assert pub._READ_LEASES[reader['admission']]['active']
        assert reader['ticket']in read_identity._CONNECTIONS
        assert env.db._original_connections[id(reader['connection'])]is reader['connection']


def test_wait_cancellation_does_not_misclassify_live_original_foreign_reader(original,monkeypatch):
    env=original;calls=spy_unlock(env,monkeypatch);owner=spy_owner(env,monkeypatch)
    assert_original_lifespan_alive(env)
    with held_reader(env)as reader:
        cancel_waiting_original_request(env,owner)
        actor=assert_original_endpoint_actor_retired(env,owner,expected_acquired=0)
        assert_live_original_foreign_reader(env,reader,actor)
        assert calls==[] and owner['acquired']==owner['exits']==0
        assert_original_lifespan_alive(env)
    assert_original_reader_retired(env,reader)
    assert_original_endpoint_actor_retired(env,owner,expected_acquired=0)
    assert_unchanged(env)


def test_body_cancellation_gets_one_original_exit_with_exception(original,monkeypatch):
    env=original;calls=spy_unlock(env,monkeypatch,asyncio.CancelledError());owner=spy_owner(env,monkeypatch)
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.client.app),base_url='http://testserver',headers={'X-HSC-Session':env.session})as client:
            try:await client.post('/v1/parent/unlock',json={'pin':'4826'})
            except BaseException:pass
    env.client.portal.call(request)
    assert len(calls)==1 and owner['enters']==owner['exits']==1 and owner['exit_exceptions']==['CancelledError']
    assert_original_lifespan_alive(env)
    with held_reader(env)as reader:
        actor=assert_original_endpoint_actor_retired(env,owner,expected_acquired=1)
        assert_live_original_foreign_reader(env,reader,actor)
        assert_original_lifespan_alive(env)
    assert_original_reader_retired(env,reader)
    assert_original_endpoint_actor_retired(env,owner,expected_acquired=1)
    assert_unchanged(env)


def test_parent_permissions_foreign_proof_and_sticky_pause_unchanged(original,monkeypatch):
    env=original;headers=fresh_headers(env);owner=spy_owner(env,monkeypatch)
    for h in[{}, {'X-HSC-Parent':'foreign-original-token'}]:
        response=env.client.get('/v1/parent/recovery-status',headers=h)
        assert response.status_code==401
    assert owner['enters']==0
    foreign=dict(env.proof,request_id='foreign-synthetic-request')
    # A malformed proof never reaches original recovery or receipt side effects.
    response=env.client.post('/v1/parent/native-privacy-permits/fence',headers=headers,json=foreign)
    assert response.status_code==409 and owner['enters']==0
    assert_unchanged(env)
