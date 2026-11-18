"""Retained legacy report is parent-only, child-scoped, read-only and restart durable."""
import json
from home_study_agent.database import Database
from test_app import client_for
from test_report_delivery import _pending_report_job

TOKEN='synthetic-local-token'
CHILD='child-primary-4'
REPORT='legacy-original-report'


def _seed(db):
    _pending_report_job(db)
    summary={'headline':'原始合成日报','facts':['保留的原始学习摘要'],'interpretation':'原解释','evidence_count':1}
    coaching={'scenario':'原沟通场景','why_this_fits':'原依据','script':['原建议'],
              'action':{'what':'原行动','steps':['原步骤'],'duration_minutes':5},'avoid':[]}
    with db.connect() as c:
        c.execute('''INSERT INTO daily_report(report_id,session_id,child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)''',(REPORT,'pending-report-session',CHILD,'2026-01-01','legacy',
            json.dumps({'private_evidence':'SHOULD_NOT_LEAVE'}),json.dumps(summary,ensure_ascii=False),json.dumps(coaching,ensure_ascii=False),'2026-01-01'))
        c.commit()
    return summary,coaching


def test_legacy_original_requires_local_session_and_parent_and_survives_restart(tmp_path):
    url=f'/v1/parent/children/{CHILD}/legacy-daily-reports/{REPORT}/original'
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');summary,coaching=_seed(db)
        assert client.get(url).status_code==401
        parent=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        headers={'X-HSC-Parent':parent}
        assert client.get(url,headers={'X-HSC-Session':'wrong','X-HSC-Parent':parent}).status_code==401
        assert client.get(url.replace(CHILD,'child-junior-3'),headers=headers).status_code==404
        listing=client.get(f'/v1/children/{CHILD}/reports',headers=headers)
        assert listing.status_code==200
        assert not any(item['report_id']==REPORT for item in listing.json()['items'])
        issues=client.get(f'/v1/parent/children/{CHILD}/report-source-issues',headers=headers)
        assert issues.status_code==200
        assert any(item['source_id']==REPORT and item['error_code']=='REPORT_TASK_SOURCE_LEGACY_UNVERIFIED'
                   for item in issues.json()['items'])
        with db.connect() as c:
            before=tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(REPORT,)).fetchone())
            audit_before=c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0]
        response=client.get(url,headers=headers)
        assert response.status_code==200,response.text
        body=response.json()
        assert body['child_id']==CHILD and body['report_id']==REPORT
        assert body['summary']==summary and body['parent_coaching']==coaching
        assert body['verification']=='legacy_unverified' and body['can_send'] is False and body['can_create_trusted_report'] is False
        status=client.get(url+'/status',headers=headers)
        assert status.status_code==200 and status.json()['source_digest']==body['source_digest']
        assert 'summary' not in status.json() and 'SHOULD_NOT_LEAVE' not in status.text
        assert 'SHOULD_NOT_LEAVE' not in response.text and 'no-store' in response.headers['cache-control']
        with db.connect() as c:
            assert tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(REPORT,)).fetchone())==before
            assert c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0]==audit_before
    with client_for(tmp_path,session_token=TOKEN) as restarted:
        restarted.headers.update({'X-HSC-Session':TOKEN})
        parent=restarted.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
        response=restarted.get(url,headers={'X-HSC-Parent':parent})
        assert response.status_code==200 and response.json()['summary']==summary


def test_legacy_original_rejects_wrong_scope_corrupt_and_active_deletion(tmp_path):
    url=f'/v1/parent/children/{CHILD}/legacy-daily-reports/{REPORT}/original'
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');_seed(db)
        parent=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token'];headers={'X-HSC-Parent':parent}
        with db.connect() as c:
            c.execute("UPDATE daily_report SET evidence_json='{\"report_task_source_fingerprint\":\"not-legacy\"}' WHERE report_id=?",(REPORT,));c.commit()
        assert client.get(url,headers=headers).status_code==409
        with db.connect() as c:
            c.execute("UPDATE daily_report SET evidence_json='not-json' WHERE report_id=?",(REPORT,));c.commit()
        assert client.get(url,headers=headers).status_code==409
        with db.connect() as c:
            c.execute("UPDATE daily_report SET evidence_json='{}' WHERE report_id=?",(REPORT,))
            c.execute("""INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,created_at,updated_at)
                VALUES('d',?,'reports','daily_report',?,'processing','2026-01-01','2026-01-01')""",(CHILD,REPORT));c.commit()
        assert client.get(url,headers=headers).status_code==409
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM daily_report WHERE report_id=?',(REPORT,)).fetchone()[0]==1


def test_parent_authority_is_rechecked_after_read(tmp_path,monkeypatch):
    url=f'/v1/parent/children/{CHILD}/legacy-daily-reports/{REPORT}/original'
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        _seed(Database(tmp_path/'test.db'))
        parent=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        service=client.app.state.parent_reports; original=service.read_legacy_daily_original
        def expire(*args):
            result=original(*args)
            client.app.state.parent_auth._tokens.pop(parent,None)
            return result
        monkeypatch.setattr(service,'read_legacy_daily_original',expire)
        response=client.get(url,headers={'X-HSC-Parent':parent})
        assert response.status_code==401 and '原始合成日报' not in response.text


def test_deletion_started_after_read_blocks_original_response(tmp_path,monkeypatch):
    url=f'/v1/parent/children/{CHILD}/legacy-daily-reports/{REPORT}/original'
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');_seed(db)
        parent=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        service=client.app.state.parent_reports; original=service.read_legacy_daily_original
        def delete_after_read(*args):
            result=original(*args)
            with db.connect() as c:
                c.execute("""INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,created_at,updated_at)
                    VALUES('race',?,'reports','daily_report',?,'processing','2026-01-01','2026-01-01')""",(CHILD,REPORT))
                c.commit()
            return result
        monkeypatch.setattr(service,'read_legacy_daily_original',delete_after_read)
        response=client.get(url,headers={'X-HSC-Parent':parent})
        assert response.status_code==409 and '原始合成日报' not in response.text


def test_completed_deletion_and_changed_generation_after_read_block_original(tmp_path,monkeypatch):
    url=f'/v1/parent/children/{CHILD}/legacy-daily-reports/{REPORT}/original'
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');_seed(db)
        parent=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        service=client.app.state.parent_reports; original=service.read_legacy_daily_original
        def completed_after_read(*args):
            result=original(*args)
            with db.connect() as c:
                c.execute("""INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,created_at,updated_at)
                    VALUES('finished-race',?,'reports','daily_report',?,'completed','2026-01-01','2026-01-01')""",(CHILD,REPORT))
                c.execute("UPDATE privacy_storage_generation SET generation='changed-synthetic-generation' WHERE singleton=1")
                c.commit()
            return result
        monkeypatch.setattr(service,'read_legacy_daily_original',completed_after_read)
        response=client.get(url,headers={'X-HSC-Parent':parent})
        assert response.status_code==409 and '原始合成日报' not in response.text


def test_original_response_cancellation_releases_privacy_guard(tmp_path):
    import asyncio
    import pytest
    from home_study_agent.database import ExternalEffectBusyError
    from home_study_agent.legacy_original_response import LegacyOriginalResponse
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');_seed(db)
        token=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        response=LegacyOriginalResponse(db,client.app.state.parent_reports,client.app.state.parent_auth,token,CHILD,REPORT)
        async def check():
            started=asyncio.Event()
            async def send(_message):
                started.set()
                await asyncio.Future()
            async def receive():
                await asyncio.Future()
            task=asyncio.create_task(response({'type':'http'},receive,send))
            await asyncio.wait_for(started.wait(),5)
            def try_guard():
                with db.external_effect_guard(timeout=0):
                    return True
            with pytest.raises(ExternalEffectBusyError):
                await asyncio.to_thread(try_guard)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(try_guard) is True
        asyncio.run(check())


def test_original_response_slow_send_times_out_and_releases_guard(tmp_path,monkeypatch):
    import asyncio
    import pytest
    from home_study_agent import legacy_original_response
    monkeypatch.setattr(legacy_original_response,'LEGACY_SEND_TIMEOUT_SECONDS',0.05)
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');_seed(db)
        token=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        response=legacy_original_response.LegacyOriginalResponse(db,client.app.state.parent_reports,client.app.state.parent_auth,token,CHILD,REPORT)
        async def check():
            async def send(_message):
                await asyncio.Future()
            async def receive():
                await asyncio.Future()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(response({'type':'http'},receive,send),3)
            def try_guard():
                with db.external_effect_guard(timeout=0):
                    return True
            assert await asyncio.to_thread(try_guard) is True
        asyncio.run(check())


def test_revoked_parent_during_header_send_never_receives_original_body(tmp_path):
    import asyncio
    import pytest
    from home_study_agent.legacy_original_response import LegacyOriginalResponse
    from home_study_agent.parent_auth import ParentSessionRequiredError
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');_seed(db)
        token=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        auth=client.app.state.parent_auth
        response=LegacyOriginalResponse(db,client.app.state.parent_reports,auth,token,CHILD,REPORT)
        async def check():
            started=asyncio.Event(); resume=asyncio.Event(); bodies=[]
            async def send(message):
                if message['type']=='http.response.start':
                    started.set(); await resume.wait()
                else:
                    bodies.append(message.get('body',b''))
            async def receive():
                await asyncio.Future()
            task=asyncio.create_task(response({'type':'http'},receive,send))
            await asyncio.wait_for(started.wait(),5)
            auth._tokens.pop(token,None)
            resume.set()
            with pytest.raises(ParentSessionRequiredError):
                await task
            assert not bodies
        asyncio.run(check())


def test_revoked_parent_during_pending_body_send_cancels_cooperative_transport(tmp_path):
    import asyncio
    import pytest
    from home_study_agent.legacy_original_response import LegacyOriginalResponse
    from home_study_agent.parent_auth import ParentSessionRequiredError
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');_seed(db)
        token=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        auth=client.app.state.parent_auth
        response=LegacyOriginalResponse(db,client.app.state.parent_reports,auth,token,CHILD,REPORT)
        async def check():
            started=asyncio.Event(); resume=asyncio.Event(); bodies=[]
            async def send(message):
                if message['type']=='http.response.body':
                    started.set(); await resume.wait(); bodies.append(message['body'])
            async def receive():
                await asyncio.Future()
            task=asyncio.create_task(response({'type':'http'},receive,send))
            await asyncio.wait_for(started.wait(),5)
            auth._tokens.pop(token,None)
            try:
                with pytest.raises(ParentSessionRequiredError):
                    await asyncio.wait_for(task,1)
                assert not bodies
            finally:
                resume.set()
        asyncio.run(check())


def test_uncooperative_transport_keeps_privacy_fence_until_it_settles(tmp_path):
    """Fail closed: ASGI may retain already submitted bytes after cancellation."""
    import asyncio
    import pytest
    from home_study_agent.database import ExternalEffectBusyError
    from home_study_agent.legacy_original_response import LegacyOriginalResponse
    with client_for(tmp_path,session_token=TOKEN) as client:
        client.headers.update({'X-HSC-Session':TOKEN})
        db=Database(tmp_path/'test.db');_seed(db)
        token=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        response=LegacyOriginalResponse(db,client.app.state.parent_reports,client.app.state.parent_auth,token,CHILD,REPORT)
        async def check():
            started=asyncio.Event(); cancelled=asyncio.Event(); release=asyncio.Event()
            async def send(_message):
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
            async def receive():
                await asyncio.Future()
            def guard_available():
                try:
                    with db.external_effect_guard(timeout=0): return True
                except ExternalEffectBusyError: return False
            task=asyncio.create_task(response({'type':'http'},receive,send))
            try:
                await asyncio.wait_for(started.wait(),5)
                task.cancel()
                await asyncio.wait_for(cancelled.wait(),2)
                assert not task.done() and not await asyncio.to_thread(guard_available)
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task,2)
            assert await asyncio.to_thread(guard_available)
        asyncio.run(check())


def test_legacy_original_requires_enabled_local_session_even_with_parent(tmp_path):
    url=f'/v1/parent/children/{CHILD}/legacy-daily-reports/{REPORT}/original'
    with client_for(tmp_path,session_token=None) as client:
        _seed(Database(tmp_path/'test.db'))
        parent=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        assert client.get(url,headers={'X-HSC-Parent':parent}).status_code==401
