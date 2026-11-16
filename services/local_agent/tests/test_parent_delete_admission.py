"""Real SQL/opaque Parent admission: no native or publication guard substitutes."""
import asyncio
from contextlib import asynccontextmanager
import sqlite3
import threading
import time

import pytest
from home_study_agent.database import Database,ExternalEffectBusyError
from home_study_agent.parent_auth import ParentAuth,ParentSessionRequiredError
from home_study_agent.repository import StudyRepository
from home_study_agent import parent_restore_privacy as parent
from home_study_agent import restore_publication as pub
from home_study_agent.privacy_operation_receipt import initialize as initialize_receipts

@pytest.fixture
def original(tmp_path):
    db=Database(tmp_path/'agent.db');db.initialize();StudyRepository(db).seed_family()
    with db.connect()as c:initialize_receipts(c);c.commit()
    auth=ParentAuth(db);token=auth.setup('7391');parent.bind_parent_auth(db,auth,native_key_present=False)
    yield db,auth,token
    parent.retire_parent_auth(db,auth)

def enter(original):
    db,auth,token=original
    return parent.admitted_parent_delete(db,auth,token,method='POST',
        path='/v1/parent/children/child-primary-4/delete',body={'scope':'learning',
        'confirmation':'确认删除','client_action_id':'admission-learning-001','operation':None})

def no_side_effects(db):
    with sqlite3.connect('file:'+str(db.path)+'?mode=ro',uri=True)as c:
        for table in('deletion_job','restore_asset_parent_authority','restore_asset_parent_erasure'):
            assert c.execute('SELECT COUNT(*)FROM '+table).fetchone()[0]==0
    gate=pub._gate(pub._path(db.path))
    assert gate.owner is None and not gate.waiting
    assert not parent._CURRENT and not parent._CAPS

@asynccontextmanager
async def held_reader(db):
    opened=asyncio.Event();release=asyncio.Event()
    async def read():
        with db.connect()as c:
            assert c.execute('SELECT COUNT(*)FROM child_profile').fetchone()[0]==2
            opened.set();await release.wait()
    task=asyncio.create_task(read());await opened.wait()
    try:yield release
    finally:release.set();await task

def test_real_reader_drains_then_original_same_task_owner_enters_once(original):
    db,_,_=original
    async def run():
        async with held_reader(db)as release:
            entered=[]
            async def command():
                async with enter(original)as cap:
                    entered.append(True);state=parent._cap(cap)
                    assert state.owner.actor[1]is asyncio.current_task()
                    assert pub._gate(pub._path(db.path)).owner is state.owner
                    with db.connect()as c:assert c.execute('SELECT COUNT(*)FROM child_profile').fetchone()[0]==2
                    async def foreign():
                        with pytest.raises(pub.RestorePublicationError,match='OWNER_REQUIRED'):pub._owner(state.owner)
                    await asyncio.create_task(foreign())
            command_task=asyncio.create_task(command());await asyncio.sleep(0.03)
            assert entered==[];release.set();await command_task;assert entered==[True]
        no_side_effects(db)
    asyncio.run(run())

def test_persistent_reader_expires_without_business_or_capability(original):
    db,_,_=original
    async def run():
        async with held_reader(db):
            start=time.monotonic()
            with pytest.raises(pub.RestorePublicationError,match='^RESTORE_PUBLICATION_BUSY$'):
                async with enter(original):pytest.fail('business entered while reader held')
            assert 1.8<=time.monotonic()-start<2.8
        no_side_effects(db)
    asyncio.run(run())

def test_cancel_waiting_admission_leaves_no_owner_then_fresh_entry_works(original):
    db,_,_=original
    async def run():
        async with held_reader(db):
            async def command():
                async with enter(original):pytest.fail('cancelled waiter entered business')
            task=asyncio.create_task(command());await asyncio.sleep(0.03);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        no_side_effects(db)
        async with enter(original)as cap:assert parent._cap(cap).database is db
        no_side_effects(db)
    asyncio.run(run())

def test_business_busy_is_not_replayed_and_original_cap_is_closed(original):
    db,_,_=original;calls=[]
    async def run():
        with pytest.raises(pub.RestorePublicationError,match='^RESTORE_PUBLICATION_BUSY$'):
            async with enter(original):
                calls.append(True);raise pub.RestorePublicationError('RESTORE_PUBLICATION_BUSY')
        assert calls==[True];no_side_effects(db)
    asyncio.run(run())

def test_poison_is_immediate_and_never_cleared(original):
    db,_,_=original;gate=pub._gate(pub._path(db.path));gate.poisoned=True
    async def run():
        start=time.monotonic()
        with pytest.raises(pub.RestorePublicationError,match='^RESTORE_PUBLICATION_CLOSE_UNCONFIRMED$'):
            async with enter(original):pytest.fail('poisoned business entered')
        assert time.monotonic()-start<0.2 and gate.poisoned
        no_side_effects(db)
    asyncio.run(run())

def test_invalid_parent_is_immediate_and_cannot_wait_into_authority(original):
    db,auth,_=original
    async def run():
        start=time.monotonic()
        with pytest.raises(ParentSessionRequiredError):
            async with parent.admitted_parent_delete(db,auth,'invalid',method='POST',path='/v1/parent/children/child-primary-4/delete',
                body={'scope':'learning','confirmation':'确认删除','client_action_id':'bad-parent-001'}):pytest.fail('invalid auth entered')
        assert time.monotonic()-start<0.2;no_side_effects(db)
    asyncio.run(run())

def test_external_guard_conflict_is_known_busy_before_capability(original):
    db,_,_=original;opened=threading.Event();release=threading.Event();errors=[]
    def hold():
        try:
            with db.external_effect_guard(timeout=0):opened.set();release.wait(5)
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=hold);thread.start();assert opened.wait(2)
    async def run():
        start=time.monotonic()
        with pytest.raises(ExternalEffectBusyError):
            async with enter(original):pytest.fail('foreign external owner was bypassed')
        assert time.monotonic()-start<0.2
    try:asyncio.run(run())
    finally:release.set();thread.join(2)
    assert not thread.is_alive()and not errors;no_side_effects(db)

def test_http_busy_has_no_job_and_same_request_succeeds_after_reader_release(tmp_path):
    from test_app import client_for
    opened=threading.Event();release=threading.Event();errors=[]
    with client_for(tmp_path)as client:
        token=client.post('/v1/parent/setup',json={'pin':'7391'}).json()['parent_token']
        db=client.app.state.privacy_lifecycle.database
        def hold():
            try:
                with db.connect()as c:
                    assert c.execute('SELECT COUNT(*)FROM child_profile').fetchone()[0]==2
                    opened.set();release.wait(6)
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=hold);thread.start();assert opened.wait(2)
        payload={'scope':'learning','confirmation':'确认删除','client_action_id':'busy-http-001'}
        try:
            started=time.monotonic()
            busy=client.post('/v1/parent/children/child-primary-4/delete',headers={'X-HSC-Parent':token},json=payload)
            assert busy.status_code==409 and busy.json()['code']=='RESTORE_PUBLICATION_BUSY'
            assert busy.json()['retryable']is True and busy.headers['Retry-After']=='1'
            assert time.monotonic()-started<3
            with db.connect()as c:assert c.execute('SELECT COUNT(*)FROM deletion_job').fetchone()[0]==0
        finally:release.set();thread.join(2)
        assert not thread.is_alive()and not errors
        completed=client.post('/v1/parent/children/child-primary-4/delete',headers={'X-HSC-Parent':token},json=payload)
        assert completed.status_code==200 and completed.json()['status']=='completed'
        repeated=client.post('/v1/parent/children/child-primary-4/delete',headers={'X-HSC-Parent':token},json=payload)
        assert repeated.status_code==200 and repeated.json()['deletion_id']==completed.json()['deletion_id']
        with db.connect()as c:assert c.execute('SELECT COUNT(*)FROM deletion_job').fetchone()[0]==1
