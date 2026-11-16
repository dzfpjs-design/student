from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pytest
from home_study_agent.database import Database
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService


def test_orphan_reclaim_rejects_old_submit_and_result(tmp_path, monkeypatch):
    db = Database(tmp_path/'data.db',media_root=tmp_path,data_encryption_key=b'0'*32); db.initialize()
    service = PilotTestTelemetryService(db,tmp_path); service.initialize()
    recipient='guardian@example.test'; service.set_diagnostic_consent(recipient,consented=True)
    monkeypatch.setattr(service,'_has_activity',lambda *_: True)
    now=datetime.now(timezone.utc); a=service.claim_due(recipient,now=now); assert a and a.claim_token
    with db.connect() as c:
        c.execute('UPDATE pilot_test_digest_outbox SET updated_at=?',((now-timedelta(minutes=3)).isoformat(),));c.commit()
    b=service.claim_due(recipient,now=now)
    assert b and b.claim_token != a.claim_token and b.digest_sha256==a.digest_sha256
    with pytest.raises(ValueError,match='PILOT_DIGEST_CLAIM_MISMATCH'):
        service.reserve_submission(a.delivery_date,recipient,a.digest_sha256,a.claim_token)
    owner=service.reserve_submission(b.delivery_date,recipient,b.digest_sha256,b.claim_token)
    with pytest.raises(ValueError,match='PILOT_DIGEST_CLAIM_MISMATCH'):
        service.record_result(a.delivery_date,recipient,status='unknown',claim_token=a.claim_token)
    with db.connect() as c:
        row=c.execute('SELECT status,submission_token FROM pilot_test_digest_outbox').fetchone()
        assert row['status']=='processing' and row['submission_token']==owner.submission_token
    assert service.claim_due(recipient,now=now) is None
    service.finish_owned_submission(owner,status='sent',transport='synthetic')
    assert service.record_result(b.delivery_date,recipient,status='sent',claim_token=b.claim_token)['status']=='sent'


def test_restart_only_recovers_unsubmitted_versioned_claim(tmp_path, monkeypatch):
    db=Database(tmp_path/'data.db',media_root=tmp_path,data_encryption_key=b'0'*32);db.initialize()
    svc=PilotTestTelemetryService(db,tmp_path);svc.initialize();recipient='guardian@example.test';svc.set_diagnostic_consent(recipient,consented=True)
    monkeypatch.setattr(svc,'_has_activity',lambda *_: True)
    now=datetime.now(timezone.utc);old=svc.claim_due(recipient,now=now);assert old
    restarted=PilotTestTelemetryService(db,tmp_path);restarted.initialize();new=restarted.claim_due(recipient,now=now)
    assert new and new.claim_token != old.claim_token
    with pytest.raises(ValueError,match='PILOT_DIGEST_CLAIM_MISMATCH'):
        restarted.reserve_submission(old.delivery_date,recipient,old.digest_sha256,old.claim_token)
    restarted.reserve_submission(new.delivery_date,recipient,new.digest_sha256,new.claim_token)
    again=PilotTestTelemetryService(db,tmp_path);again.initialize()
    assert again.claim_due(recipient,now=now) is None
    with db.connect() as c: assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()['status']=='unknown'


def test_http_old_generation_cannot_submit_or_mutate_new_generation(tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.notification_channels import ChannelReceipt
    from zoneinfo import ZoneInfo
    from datetime import time
    app=create_app(Settings(data_dir=tmp_path,database_path=tmp_path/'family.db',data_encryption_key=b'0'*32,session_token='synthetic-session',maintenance_enabled=False))
    sent=[]
    class SMTP:
        def send(self,recipient,subject,text,html,config,*,attachments=()):
            sent.append(recipient);return ChannelReceipt(provider_message_id='synthetic',client_message_id='synthetic')
    app.state.report_delivery.adapters['email']=SMTP()
    with TestClient(app) as client:
        service=app.state.pilot_test_telemetry; service.set_diagnostic_consent('guardian@example.test',consented=True)
        app.state.report_delivery.save_channel_config('email',{'host':'smtp.example.test','port':587,'username':'sender@example.test','from_address':'sender@example.test','starttls':True,'allow_external_delivery':True,'password':'synthetic'})
        local=datetime.now(ZoneInfo('Asia/Shanghai'));day=local.date() if local.hour>=22 else local.date()-timedelta(days=1)
        observed=datetime.combine(day,time(hour=12),ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc).isoformat()
        with service.database.connect() as c:
            c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES('synthetic','system','synthetic',NULL,'{}',?)",(observed,));c.commit()
        headers={'X-HSC-Session':'synthetic-session'}
        a=client.get('/v1/pilot-test-digest/due',params={'recipient':'guardian@example.test'},headers=headers).json();assert a['due']
        with service.database.connect() as c:
            c.execute('UPDATE pilot_test_digest_outbox SET updated_at=?',((datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat(),));c.commit()
        b=client.get('/v1/pilot-test-digest/due',params={'recipient':'guardian@example.test'},headers=headers).json();assert b['due'] and a['claim_token']!=b['claim_token']
        url='/v1/pilot-test-digest/'+a['delivery_date']
        old={'recipient':a['recipient'],'digest_sha256':a['digest_sha256'],'claim_token':a['claim_token']}
        assert client.post(url+'/submit-email',json=old,headers=headers).status_code==422
        assert client.post(url+'/result',json={'recipient':a['recipient'],'status':'unknown','claim_token':a['claim_token']},headers=headers).status_code==422
        current={'recipient':b['recipient'],'digest_sha256':b['digest_sha256'],'claim_token':b['claim_token']}
        result=client.post(url+'/submit-email',json=current,headers=headers)
        assert result.status_code==200 and result.json()['accepted'] is True and sent==['guardian@example.test']


def test_new_install_unprovisioned_recipient_yields_specific_422(tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    app=create_app(Settings(data_dir=tmp_path,database_path=tmp_path/'new.db',data_encryption_key=b'0'*32,session_token='synthetic',maintenance_enabled=False))
    with TestClient(app) as client:
        response=client.get('/v1/pilot-test-digest/due',params={'recipient':'guardian@example.test'},headers={'X-HSC-Session':'synthetic'})
        assert response.status_code==422 and response.json()['detail']['code']=='PILOT_RECIPIENT_NOT_AUTHORIZED'


def test_http_timeout_result_cannot_override_inflight_smtp_success(tmp_path):
    from threading import Event
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.notification_channels import ChannelReceipt
    from zoneinfo import ZoneInfo
    from datetime import time
    app=create_app(Settings(data_dir=tmp_path,database_path=tmp_path/'slow.db',data_encryption_key=b'0'*32,session_token='synthetic-session',maintenance_enabled=False))
    entered=Event();release=Event()
    class SlowSMTP:
        def send(self,recipient,subject,text,html,config,*,attachments=()):
            entered.set();assert release.wait(10)
            return ChannelReceipt(provider_message_id='synthetic',client_message_id='synthetic')
    app.state.report_delivery.adapters['email']=SlowSMTP()
    with TestClient(app) as client:
        svc=app.state.pilot_test_telemetry;svc.set_diagnostic_consent('guardian@example.test',consented=True)
        app.state.report_delivery.save_channel_config('email',{'host':'smtp.example.test','port':587,'username':'sender@example.test','from_address':'sender@example.test','starttls':True,'allow_external_delivery':True,'password':'synthetic'})
        local=datetime.now(ZoneInfo('Asia/Shanghai'));day=local.date() if local.hour>=22 else local.date()-timedelta(days=1)
        observed=datetime.combine(day,time(hour=12),ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc).isoformat()
        with svc.database.connect() as c:
            c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES('synthetic','system','synthetic',NULL,'{}',?)",(observed,));c.commit()
        headers={'X-HSC-Session':'synthetic-session'}
        claim=client.get('/v1/pilot-test-digest/due',params={'recipient':'guardian@example.test'},headers=headers).json();assert claim['due']
        url='/v1/pilot-test-digest/'+claim['delivery_date']
        body={k:claim[k] for k in ('recipient','digest_sha256','claim_token')}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.post,url+'/submit-email',json=body,headers=headers)
            try:
                assert entered.wait(5)
                timeout=client.post(url+'/result',json={'recipient':claim['recipient'],'status':'unknown','claim_token':claim['claim_token']},headers=headers)
                assert timeout.status_code==200 and timeout.json()['status']=='processing'
            finally:
                release.set()
            result=future.result(timeout=10)
        assert result.status_code==200 and result.json()['accepted'] is True
        with svc.database.connect() as c: assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()['status']=='sent'


def test_result_before_reserve_retries_without_allowing_delayed_submit(tmp_path, monkeypatch):
    db=Database(tmp_path/'data.db',media_root=tmp_path,data_encryption_key=b'0'*32);db.initialize()
    svc=PilotTestTelemetryService(db,tmp_path);svc.initialize();recipient='guardian@example.test';svc.set_diagnostic_consent(recipient,consented=True)
    monkeypatch.setattr(svc,'_has_activity',lambda *_: True)
    now=datetime.now(timezone.utc);old=svc.claim_due(recipient,now=now);assert old
    receipt=svc.record_result(old.delivery_date,recipient,status='unknown',claim_token=old.claim_token)
    assert receipt['status']=='failed'
    current=svc.claim_due(recipient,now=now);assert current and current.claim_token!=old.claim_token
    with pytest.raises(ValueError,match='PILOT_DIGEST_CLAIM_MISMATCH'):
        svc.reserve_submission(old.delivery_date,recipient,old.digest_sha256,old.claim_token)
    owner=svc.reserve_submission(current.delivery_date,recipient,current.digest_sha256,current.claim_token)
    svc.finish_owned_submission(owner,status='failed',error_code='SYNTHETIC_FAILURE')
    next_claim=svc.claim_due(recipient,now=now);assert next_claim and next_claim.claim_token!=current.claim_token


def test_concurrent_result_and_reserve_have_one_consistent_owner(tmp_path, monkeypatch):
    from threading import Barrier
    db=Database(tmp_path/'data.db',media_root=tmp_path,data_encryption_key=b'0'*32);db.initialize()
    svc=PilotTestTelemetryService(db,tmp_path);svc.initialize();recipient='guardian@example.test';svc.set_diagnostic_consent(recipient,consented=True)
    monkeypatch.setattr(svc,'_has_activity',lambda *_: True)
    now=datetime.now(timezone.utc);claim=svc.claim_due(recipient,now=now);assert claim
    barrier=Barrier(2)
    def finish():
        barrier.wait();return svc.record_result(claim.delivery_date,recipient,status='unknown',claim_token=claim.claim_token)
    def reserve():
        barrier.wait()
        try:return svc.reserve_submission(claim.delivery_date,recipient,claim.digest_sha256,claim.claim_token)
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        f=pool.submit(finish);r=pool.submit(reserve);result=f.result();owner=r.result()
    with db.connect() as c: row=c.execute('SELECT status,submission_token FROM pilot_test_digest_outbox').fetchone()
    if owner is None:
        assert result['status']=='failed' and row['status']=='failed' and row['submission_token'] is None
    else:
        assert result['status']=='processing' and row['status']=='processing' and row['submission_token']==owner.submission_token
