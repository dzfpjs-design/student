"""Actual Parent HTTP, synthetic files, finite faults; no Windows/native issuer credit."""
import asyncio,shutil
import pytest
from home_study_agent import restore_publication as pub
from home_study_agent.native_privacy import epoch
from test_parent_owned_portable_restore import parent_only
from parent_owned_media_fixture import client,auth,seed_media,assert_consumers,identity,restore,backup

PHRASE='public-parent-managed-passphrase'
def exported_source(tmp_path):
    root=tmp_path/'source';root.mkdir(mode=0o700)
    with client(root,root/'agent.db')as c:
        h=auth(c,True);items=[seed_media(c,ch,i)for i,ch in enumerate(('child-primary-4','child-junior-3'))]
        for item in items:assert_consumers(c,item)
        restore(c,h,backup(c,h))
        db=c.app.state.privacy_lifecycle.database
        with db.connect()as d:
            for item in items:epoch(d,item['session'])
            d.commit()
        saved=c.post('/v1/parent/backups/portable',headers=h,json={'passphrase':PHRASE});assert saved.status_code==200,saved.text
        external=tmp_path/'public-source-managed.hsc';shutil.copyfile(saved.json()['path'],external)
    return external,items

def request(c,h,external):
    operation=identity(c,h)
    body={'path':str(external),'passphrase':PHRASE,'confirmation':'确认迁移恢复','operation':operation}
    return operation,c.post('/v1/parent/backups/portable/restore',headers=h,json=body)

def test_source_managed_two_consecutive_parent_restores_and_fresh_consumers(tmp_path):
    external,items=exported_source(tmp_path);root=tmp_path/'target';root.mkdir(mode=0o700)
    requests=[]
    for index in range(2):
        with parent_only(root)as c:
            h=auth(c,index==0);db=c.app.state.privacy_lifecycle.database
            if index:
                for item in items:assert_consumers(c,item)
                with db.connect()as d:assert d.execute('SELECT count(*)FROM controlled_launch_epoch').fetchone()[0]==2
            op,result=request(c,h,external);requests.append(op['request_id'])
            assert result.status_code==200,result.text
            assert result.json()['operation_receipt']['status']=='committed'
            for item in items:assert_consumers(c,item)
            with db.connect()as d:
                assert not d.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()or d.execute('SELECT count(*)FROM native_privacy_permit').fetchone()[0]==0
                assert d.execute("SELECT count(*)FROM restore_parent_authority WHERE state='FINISHED'").fetchone()[0]==index+1
                row=d.execute("SELECT authority_kind,native_nonce,parent_authority_id FROM restore_asset_generation WHERE state='ADOPTED'").fetchone()
                assert row[0]=='PARENT_SESSION_RESTORE'and row[1]is None and row[2]
    with parent_only(root)as fresh:
        for item in items:assert_consumers(fresh,item)
        db=fresh.app.state.privacy_lifecycle.database
        with db.connect()as d:
            assert sorted(r[0]for r in d.execute('SELECT request_id FROM restore_parent_authority'))==sorted(requests)

@pytest.mark.parametrize('point',('before_prepare_journal','after_backup'))
def test_fresh_parent_finalize_needs_no_source_or_passphrase(tmp_path,monkeypatch,point):
    external,items=exported_source(tmp_path);root=tmp_path/'target';root.mkdir(mode=0o700);seen=[]
    with parent_only(root)as c:
        h=auth(c,True);db=c.app.state.privacy_lifecycle.database
        async def quiesce():
            tasks=[t for t in asyncio.all_tasks()if t.get_coro().__qualname__=='VisualCaptureProducer.maintain']
            assert len(tasks)==1;tasks[0].cancel()
            try:await tasks[0]
            except asyncio.CancelledError:pass
        # Fault-only, actual original task cancelled/drained; no concurrent-worker credit.
        c.portal.call(quiesce)
        with pub.publication_owner(db,timeout=5):pass
        def cut(where):
            if where==point and not seen:seen.append(where);raise pub.RestorePublicationError('PUBLIC_PARENT_FAULT')
        monkeypatch.setattr(pub,'_fault',cut)
        op,result=request(c,h,external)
        assert result.status_code==409 and seen==[point],result.text
        monkeypatch.setattr(pub,'_fault',lambda where:None)
    external.unlink();assert not external.exists()
    with parent_only(root)as recovery:
        assert recovery.get('/v1/health').json()['status']=='recovery_only'
        h=auth(recovery)
        done=recovery.post('/v1/parent/privacy-operations/'+op['request_id']+'/finalize',headers=h)
        assert done.status_code==200,done.text
        assert done.json()['status']==('failed'if point=='before_prepare_journal'else'committed')
        repeat=recovery.post('/v1/parent/privacy-operations/'+op['request_id']+'/finalize',headers=h)
        assert repeat.status_code==200 and repeat.json()==done.json(),repeat.text
    with parent_only(root)as fresh:
        assert fresh.get('/v1/health').json()['status']!='recovery_only'
        if point=='after_backup':
            for item in items:assert_consumers(fresh,item)
        db=fresh.app.state.privacy_lifecycle.database
        with db.connect()as d:
            a=d.execute('SELECT state FROM restore_parent_authority WHERE request_id=?',(op['request_id'],)).fetchone()
            assert a[0]==('FAILED'if point=='before_prepare_journal'else'FINISHED')
            assert not d.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()or d.execute('SELECT count(*)FROM native_privacy_permit').fetchone()[0]==0
