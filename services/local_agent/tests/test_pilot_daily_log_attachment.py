from datetime import datetime,timezone
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
import json
import threading
from types import SimpleNamespace
from zipfile import ZipFile
import pytest
from home_study_agent.database import Database
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
from home_study_agent import pilot_test_telemetry as telemetry
from home_study_agent import notification_channels as notification
from home_study_agent.notification_channels import SmtpChannel,ChannelDeliveryError,submission_guard

RECIPIENT='parent@example.test'
NOW=datetime(2026,10,26,14,0,tzinfo=timezone.utc)

def zip_bytes(marker):
    with BytesIO() as out:
        with ZipFile(out,'w')as z:z.writestr('events.jsonl',marker+'\n')
        return out.getvalue()

@pytest.fixture
def ctx(tmp_path,monkeypatch):
    db=Database(tmp_path/'synthetic.db',media_root=tmp_path,data_encryption_key=b'0'*32);db.initialize()
    with db.connect()as c:
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)VALUES('child-junior-3','公开合成孩子','junior_3','jiangsu_nanjing','active','2026-10-26T12:00:00+00:00','2026-10-26T12:00:00+00:00')")
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)VALUES('session-1','child-junior-3','active','study','2026-10-26T12:00:00+00:00','2026-10-26T12:00:00+00:00','public-session')");c.commit()
    from home_study_agent.report_delivery import ReportDeliveryService
    delivery=ReportDeliveryService(db,b'0'*32);delivery.initialize()
    delivery.add_recipient('child-junior-3','email',RECIPIENT,'合成已授权家长',True)
    service=PilotTestTelemetryService(db,tmp_path);service.initialize();calls=[]
    # Explicit pre-upgrade family-pilot delivery history; child-report consent alone is never diagnostic authority.
    with db.connect()as c:
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES('2026-10-23',?,'sent',1,'old family pilot','legacy-family-body',?,'old','old')",(RECIPIENT,sha256(b'legacy-family-body').hexdigest()));c.commit()
    def build(data_dir,*,start_utc,end_utc,release_info=None):
        calls.append((start_utc,end_utc));content=zip_bytes('public-log-'+str(len(calls)))
        return SimpleNamespace(filename='运行诊断日志.zip',content=content,sha256=sha256(content).hexdigest(),manifest={'schema':'synthetic_log_bundle_v1','start_utc':start_utc.isoformat(),'end_utc':end_utc.isoformat(),'events_included':1,'counts':{'safe_events_seen':1},'coverage':{'truncated':False}})
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',build,raising=False)
    return db,service,calls

def test_failed_retry_reuses_frozen_body_zip_and_digest_after_restart(ctx,monkeypatch):
    db,s,calls=ctx;a=s.claim_due(RECIPIENT,now=NOW);assert a
    assert len(a.attachments)==1 and len(calls)==1
    own=s.reserve_submission(a.delivery_date,a.recipient,a.digest_sha256, a.claim_token)
    s.finish_submission(a.delivery_date,a.recipient,a.digest_sha256,submission_token=own.submission_token,status='failed',error_code='SMTP_UNAVAILABLE')
    before=(a.body,a.digest_sha256,a.attachments[0].content,a.attachment_manifest)
    def cannot_rebuild(*a,**k):raise AssertionError('FROZEN_RETRY_MUST_NOT_RECAPTURE')
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',cannot_rebuild)
    restarted=PilotTestTelemetryService(db,s.data_dir);restarted.initialize();b=restarted.claim_due(RECIPIENT,now=NOW)
    assert b and(before==(b.body,b.digest_sha256,b.attachments[0].content,b.attachment_manifest))
    with db.connect()as c:assert c.execute("SELECT attempt_count FROM pilot_test_digest_outbox WHERE delivery_date='2026-10-26'").fetchone()[0]==2

def test_submission_cas_allows_one_owner_and_unknown_never_reissues(ctx):
    db,s,calls=ctx;a=s.claim_due(RECIPIENT,now=NOW);barrier=threading.Barrier(2);results=[]
    def reserve():
        barrier.wait()
        try:results.append(s.reserve_submission(a.delivery_date,a.recipient,a.digest_sha256, a.claim_token))
        except ValueError as e:results.append(str(e))
    threads=[threading.Thread(target=reserve)for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(10);assert not t.is_alive()
    owners=[r for r in results if not isinstance(r,str)];assert len(owners)==1
    assert 'PILOT_DIGEST_SUBMISSION_ALREADY_STARTED'in results
    owner=owners[0];assert s.submission_check(owner)is None
    s.finish_submission(a.delivery_date,a.recipient,a.digest_sha256,submission_token=owner.submission_token,status='unknown',error_code='SMTP_DELIVERY_UNKNOWN')
    assert s.claim_due(RECIPIENT,now=NOW)is None
    PilotTestTelemetryService(db,s.data_dir).initialize();assert s.claim_due(RECIPIENT,now=NOW)is None

def test_old_submission_completion_cannot_overwrite_new_retry_owner(ctx):
    db,s,calls=ctx;a=s.claim_due(RECIPIENT,now=NOW);first=s.reserve_submission(a.delivery_date,a.recipient,a.digest_sha256, a.claim_token)
    s.finish_submission(a.delivery_date,a.recipient,a.digest_sha256,submission_token=first.submission_token,status='failed')
    retry=s.claim_due(RECIPIENT,now=NOW);second=s.reserve_submission(retry.delivery_date,retry.recipient,retry.digest_sha256, retry.claim_token)
    assert second.submission_token!=first.submission_token
    with pytest.raises(ValueError,match='PILOT_SUBMISSION_OWNERSHIP_CHANGED'):
        s.finish_submission(a.delivery_date,a.recipient,a.digest_sha256,submission_token=first.submission_token,status='unknown')
    assert s.submission_check(second)is None
    s.finish_submission(a.delivery_date,a.recipient,a.digest_sha256,submission_token=second.submission_token,status='sent',transport='smtp')
    with pytest.raises(ValueError,match='PILOT_DIGEST_CLAIM_MISMATCH'):
        s.record_result(a.delivery_date,a.recipient,status='failed',claim_token=a.claim_token)
    assert s.record_result(retry.delivery_date,retry.recipient,status='failed',claim_token=retry.claim_token)['status']=='sent'

def test_22_hour_window_includes_late_evening_and_offline_catchup(ctx):
    db,s,calls=ctx
    initial=s.claim_due(RECIPIENT,now=datetime(2026,10,25,14,tzinfo=timezone.utc));owned=s.reserve_submission(initial.delivery_date,initial.recipient,initial.digest_sha256, initial.claim_token)
    s.finish_owned_submission(owned,status='sent');calls.clear()
    a=s.claim_due(RECIPIENT,now=NOW)
    assert calls==[(datetime(2026,10,25,14,tzinfo=timezone.utc),datetime(2026,10,26,14,tzinfo=timezone.utc))]
    own=s.reserve_submission(a.delivery_date,a.recipient,a.digest_sha256, a.claim_token);s.finish_submission(a.delivery_date,a.recipient,a.digest_sha256,submission_token=own.submission_token,status='sent')
    with db.connect()as c:
        c.execute("UPDATE daily_session SET status='completed',ended_at='2026-10-26T13:00:00+00:00'WHERE session_id='session-1'")
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)VALUES('late-session','child-junior-3','active','study','2026-10-26T15:01:00+00:00','2026-10-26T15:01:00+00:00','late-public-session')");c.commit()
    b=s.claim_due(RECIPIENT,now=datetime(2026,10,30,14,tzinfo=timezone.utc));assert b
    assert calls[-1]==(datetime(2026,10,26,14,tzinfo=timezone.utc),datetime(2026,10,30,14,tzinfo=timezone.utc))
    # Keep the full catch-up log window without publishing per-child timestamps.
    assert '2026-10-26T15:01:00+00:00' not in b.body

@pytest.mark.parametrize('column,value',[('body','altered'),('attachment_content',b'altered'),('attachment_sha256','0'*64),('attachment_manifest_json','{}')])
def test_corrupt_frozen_member_cannot_reserve_submission(ctx,column,value):
    db,s,calls=ctx;a=s.claim_due(RECIPIENT,now=NOW)
    with db.connect()as c:c.execute('UPDATE pilot_test_digest_outbox SET '+column+'=? WHERE delivery_date=? AND recipient=?',(value,a.delivery_date,a.recipient));c.commit()
    with pytest.raises(ValueError,match='PILOT_.*(MISMATCH|INVALID)'):s.reserve_submission(a.delivery_date,a.recipient,a.digest_sha256, a.claim_token)

class FakeSmtp:
    messages=[];opened=0
    def __init__(self,*a,**k):type(self).opened+=1
    def starttls(self,**k):pass
    def login(self,*a):pass
    def send_message(self,message):type(self).messages.append(message);return{}
    def quit(self):pass
    def close(self):pass

@pytest.fixture
def smtp(monkeypatch):
    FakeSmtp.messages=[];FakeSmtp.opened=0
    monkeypatch.setattr(notification.smtplib,'SMTP',FakeSmtp)
    monkeypatch.setattr(notification,'_admit_external_delivery_budget',lambda:None)
    return {'host':'smtp.synthetic.invalid','from_address':'sender@example.test','starttls':False,'allow_external_delivery':True}

def test_smtp_keeps_html_attachment_and_adds_exact_bounded_zip(smtp):
    Attachment=getattr(notification,'EmailAttachment');content=zip_bytes('public')
    SmtpChannel().send(RECIPIENT,'日报','text','<p>html</p>',smtp,attachments=(Attachment('运行日志.zip',content),))
    parsed=BytesParser(policy=policy.default).parsebytes(FakeSmtp.messages[0].as_bytes())
    parts=list(parsed.iter_attachments());assert len(parts)==2
    assert parts[0].get_filename()=='学习成长报告.html'
    assert parts[1].get_filename()=='运行日志.zip'and parts[1].get_content_type()=='application/zip'
    assert parts[1].get_payload(decode=True)==content
    FakeSmtp.messages.clear();SmtpChannel().send(RECIPIENT,'普通报告','text','<p>html</p>',smtp)
    assert len(list(FakeSmtp.messages[0].iter_attachments()))==1

@pytest.mark.parametrize('name,media',[('../runtime.zip','application/zip'),('C:\\family.zip','application/zip'),('bad\r\nX.zip','application/zip'),('logs.zip','application/x-executable')])
def test_bad_attachment_metadata_rejected_before_transport(smtp,name,media):
    Attachment=getattr(notification,'EmailAttachment')
    with pytest.raises(ChannelDeliveryError):SmtpChannel().send(RECIPIENT,'日报','t','h',smtp,attachments=(Attachment(name,b'public',media),))
    assert FakeSmtp.opened==0 and FakeSmtp.messages==[]

def test_submission_guard_still_blocks_DATA_with_zip(ctx,smtp):
    from home_study_agent.report_delivery import ReportDeliveryService
    db,s,calls=ctx;service=ReportDeliveryService(db,b'0'*32);service._config=lambda channel:smtp
    Attachment=getattr(notification,'EmailAttachment')
    with submission_guard(lambda:'CURRENT_SCOPE_REVOKED'):
        with pytest.raises(ChannelDeliveryError,match='CURRENT_SCOPE_REVOKED'):
            service.submit_diagnostic_email(RECIPIENT,'日报','body',attachments=(Attachment('日志.zip',zip_bytes('public')),))
    assert FakeSmtp.messages==[]


def test_first_zip_catches_legacy_sent_day_from_midnight_without_rewriting_sent(ctx):
    db,s,calls=ctx
    with db.connect()as c:
        for day in ('2026-10-25','not-a-daily-date'):
            c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES(?,?,'sent',1,'old','legacy-body',?,'old','old')",(day,RECIPIENT,sha256(b'legacy-body').hexdigest()))
        c.commit();before=[tuple(r)for r in c.execute("SELECT * FROM pilot_test_digest_outbox WHERE status='sent'ORDER BY delivery_date")]
    a=s.claim_due(RECIPIENT,now=NOW);assert a
    assert calls==[(datetime(2026,10,24,16,tzinfo=timezone.utc),datetime(2026,10,26,14,tzinfo=timezone.utc))]
    assert a.window_policy['first_legacy_date']=='2026-10-25'and '首次补齐'in a.body
    with db.connect()as c:assert before==[tuple(r)for r in c.execute("SELECT * FROM pilot_test_digest_outbox WHERE status='sent'ORDER BY delivery_date")]


def test_runtime_only_activity_sends_but_sent_does_not_rescan(ctx,monkeypatch):
    db,s,calls=ctx
    with db.connect()as c:c.execute('DELETE FROM daily_session');c.commit()
    assert not s._has_activity('2026-10-25T14:00:00+00:00','2026-10-26T14:00:00+00:00')
    a=s.claim_due(RECIPIENT,now=NOW);assert a and len(calls)==1
    own=s.reserve_submission(a.delivery_date,a.recipient,a.digest_sha256, a.claim_token)
    s.finish_submission(a.delivery_date,a.recipient,a.digest_sha256,submission_token=own.submission_token,status='sent')
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',lambda *a,**k:pytest.fail('SENT_MUST_NOT_RESCAN_LOGS'))
    assert s.claim_due(RECIPIENT,now=NOW)is None


def test_empty_DB_and_empty_safe_log_sends_truthful_daily_status(ctx,monkeypatch):
    db,s,calls=ctx
    with db.connect()as c:c.execute('DELETE FROM daily_session');c.commit()
    content=zip_bytes('empty')
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',lambda *a,**k:SimpleNamespace(filename='运行日志.zip',content=content,sha256=sha256(content).hexdigest(),manifest={'events_included':0,'counts':{'safe_events_seen':0}}))
    claim=s.claim_due(RECIPIENT,now=NOW)
    assert claim is not None and '无学习会话记录' in claim.body
    with db.connect()as c:assert c.execute('SELECT COUNT(*)FROM pilot_test_digest_outbox WHERE attachment_content IS NOT NULL').fetchone()[0]==1


def test_legacy_failed_without_zip_remains_exact_and_blocked(ctx):
    db,s,calls=ctx
    with db.connect()as c:
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES('2026-10-26',?,'failed',1,'old','legacy-body',?,'old','old')",(RECIPIENT,sha256(b'legacy-body').hexdigest()));c.commit();before=[tuple(r)for r in c.execute('SELECT *FROM pilot_test_digest_outbox ORDER BY delivery_date')]
    with pytest.raises(ValueError,match='PILOT_LEGACY_ATTACHMENT_SNAPSHOT_MISSING'):s.claim_due(RECIPIENT,now=NOW)
    assert calls==[]
    with db.connect()as c:assert before==[tuple(r)for r in c.execute('SELECT *FROM pilot_test_digest_outbox ORDER BY delivery_date')]


def test_unconfirmed_client_result_cannot_claim_reserved_SMTP_success(ctx):
    db,s,calls=ctx;a=s.claim_due(RECIPIENT,now=NOW);owner=s.reserve_submission(a.delivery_date,a.recipient,a.digest_sha256, a.claim_token)
    result=s.record_result(a.delivery_date,a.recipient,status='sent', claim_token=a.claim_token)
    assert result['status']=='processing'and 'attachment_content'not in result and 'submission_token'not in result
    assert s.claim_due(RECIPIENT,now=NOW)is None
    s.finish_owned_submission(owner,status='sent',transport='smtp')
    assert s.record_result(a.delivery_date,a.recipient,status='unknown',claim_token=a.claim_token)['status']=='sent'


def test_attachment_count_and_size_limits_precede_transport(smtp):
    Attachment=getattr(notification,'EmailAttachment');channel=SmtpChannel()
    for entries in ((Attachment('logs.zip',b'x'),)*4,(Attachment('logs.zip',b'x'*(notification.MAX_EMAIL_ATTACHMENT_BYTES+1)),)):
        with pytest.raises(ChannelDeliveryError):channel.send(RECIPIENT,'日报','t','h',smtp,attachments=entries)
    assert FakeSmtp.opened==0 and FakeSmtp.messages==[]


def test_real_safe_bundle_sqlite_SMTP_MIME_and_terminal_are_one_frozen_delivery(ctx,smtp,monkeypatch):
    from home_study_agent.daily_diagnostic_bundle import build_daily_diagnostic_bundle
    from home_study_agent.report_delivery import ReportDeliveryService
    db,s,calls=ctx;folder=s.data_dir/'diagnostics/logs';folder.mkdir(parents=True)
    (folder/'runtime.jsonl').write_text(json.dumps({'timestamp':'2026-10-26T12:20:00Z','privacy_policy_version':'diagnostic_metadata_v2','source':'main','event':'app_process_started','level':'info','details':{'version':'0.1.0','platform':'win32','architecture':'x64','packaged':True,'apiKey':'PRIVATE_TEST_CANARY','transcript':'PRIVATE_TEST_TRANSCRIPT'}})+'\n')
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',build_daily_diagnostic_bundle)
    claim=s.claim_due(RECIPIENT,now=NOW);assert claim and len(claim.attachments)==1
    with ZipFile(BytesIO(claim.attachments[0].content))as z:
        text='\n'.join(z.read(n).decode()for n in z.namelist());assert 'PRIVATE_TEST_CANARY'not in text and 'PRIVATE_TEST_TRANSCRIPT'not in text
        assert json.loads(z.read('manifest.json'))==claim.attachment_manifest
        assert json.loads(z.read('events.jsonl').splitlines()[0])['event']=='app_process_started'
    owner=s.reserve_submission(claim.delivery_date,claim.recipient,claim.digest_sha256, claim.claim_token)
    service=ReportDeliveryService(db,b'0'*32);service._config=lambda channel:smtp
    with submission_guard(lambda:s.submission_check(owner)):
        receipt=service.submit_diagnostic_email(owner.recipient,owner.subject,owner.body,attachments=owner.attachments)
    assert receipt['accepted']is True
    result=s.finish_submission(owner.delivery_date,owner.recipient,owner.digest_sha256,submission_token=owner.submission_token,status='sent',transport='smtp')
    assert result['status']=='sent'and len(FakeSmtp.messages)==1
    zipped=list(FakeSmtp.messages[0].iter_attachments())[1];assert zipped.get_payload(decode=True)==owner.attachments[0].content
    restarted=PilotTestTelemetryService(db,s.data_dir);restarted.initialize();assert restarted.claim_due(RECIPIENT,now=NOW)is None
    assert len(FakeSmtp.messages)==1


def test_schema_upgrade_preserves_old_sent_and_quarantines_processing(ctx):
    db,s,calls=ctx
    with db.connect()as c:
        c.execute('DROP TABLE combined_daily_delivery')
        c.execute('DROP TABLE combined_daily_snapshot')
        c.execute('DROP TABLE combined_diagnostic_member')
        c.execute('DROP TABLE pilot_test_digest_outbox')
        c.execute('DROP TABLE combined_diagnostic_batch')
        c.execute('DROP TABLE daily_visual_schedule')
        c.execute('DELETE FROM schema_migration WHERE version>95')
        c.execute("CREATE TABLE pilot_test_digest_outbox(delivery_date TEXT NOT NULL,recipient TEXT NOT NULL,status TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,subject TEXT,body TEXT,digest_sha256 TEXT,transport TEXT,error_code TEXT,accepted_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(delivery_date,recipient))")
        for day,status in [('2026-10-24','sent'),('2026-10-25','processing')]:
            c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES(?,?,?,1,'legacy','legacy-body',?,'old','old')",(day,RECIPIENT,status,sha256(b'legacy-body').hexdigest()))
        c.commit();before=dict(c.execute("SELECT *FROM pilot_test_digest_outbox WHERE status='sent'").fetchone())
    db.initialize()
    s.initialize()
    with db.connect()as c:
        sent=dict(c.execute("SELECT *FROM pilot_test_digest_outbox WHERE status='sent'").fetchone());unknown=dict(c.execute("SELECT *FROM pilot_test_digest_outbox WHERE delivery_date='2026-10-25'").fetchone())
    assert all(sent[k]==v for k,v in before.items())
    assert unknown['status']=='unknown'and unknown['error_code']=='DELIVERY_RESULT_UNKNOWN_AFTER_RESTART'
    assert unknown['attachment_content']is None and calls==[]


def test_verification_is_distinct_durable_and_replay_never_creates_daily_permission(ctx):
    from home_study_agent.pilot_test_telemetry import PilotSubmissionReplay
    db,s,calls=ctx;daily=s.claim_due(RECIPIENT,now=NOW);owned=s.reserve_submission(daily.delivery_date,daily.recipient,daily.digest_sha256, daily.claim_token)
    s.finish_owned_submission(owned,status='sent',transport='smtp')
    with db.connect()as c:before=[tuple(r)for r in c.execute('SELECT *FROM pilot_test_digest_outbox ORDER BY delivery_date')]
    action=s.reserve_verification('2026-10-26',RECIPIENT,'verification-action-1',now=NOW)
    assert action.purpose=='runtime_log_attachment_verification'and action.client_action_id=='verification-action-1'
    assert '附件验证'in action.subject and '不是新学习日报'in action.body
    assert action.window_start_utc=='2026-10-25T16:00:00+00:00'and action.window_end_utc=='2026-10-26T14:00:00+00:00'
    assert s.submission_check(action)is None
    with pytest.raises(PilotSubmissionReplay)as active:s.reserve_verification('2026-10-26',RECIPIENT,'verification-action-1',now=NOW)
    assert active.value.result['status']=='processing'and active.value.result['accepted']is False
    s.finish_owned_submission(action,status='sent',transport='smtp')
    with pytest.raises(PilotSubmissionReplay)as sent:s.reserve_verification('2026-10-26',RECIPIENT,'verification-action-1',now=NOW)
    assert sent.value.result['accepted']is True and sent.value.result['status']=='sent'
    assert 'submission_token'not in sent.value.result and 'attachment_content'not in sent.value.result
    with db.connect()as c:assert before==[tuple(r)for r in c.execute('SELECT *FROM pilot_test_digest_outbox ORDER BY delivery_date')]
    assert len(calls)==2


def test_verification_unknown_survives_restart_without_retry_or_cursor(ctx):
    from home_study_agent.pilot_test_telemetry import PilotSubmissionReplay
    db,s,calls=ctx;action=s.reserve_verification('2026-10-26',RECIPIENT,'verification-unknown-1',now=NOW)
    s.finish_owned_submission(action,status='unknown',error_code='SMTP_DELIVERY_UNKNOWN')
    s.initialize()
    with pytest.raises(PilotSubmissionReplay)as replay:s.reserve_verification('2026-10-26',RECIPIENT,'verification-unknown-1',now=NOW)
    assert replay.value.result['status']=='unknown'and replay.value.result['accepted']is False and len(calls)==1
    with db.connect()as c:assert c.execute('SELECT COUNT(*)FROM pilot_test_digest_outbox WHERE attachment_content IS NOT NULL').fetchone()[0]==0


def test_verification_known_preDATA_failure_retries_exact_packet_and_nonce_scope(ctx,monkeypatch):
    from home_study_agent.report_delivery import ReportDeliveryService
    db,s,calls=ctx;action=s.reserve_verification('2026-10-26',RECIPIENT,'verification-failed-1',now=NOW)
    s.finish_owned_submission(action,status='failed',error_code='SMTP_UNAVAILABLE')
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',lambda *a,**k:pytest.fail('VERIFICATION_RETRY_RECAPTURED'))
    retry=s.reserve_verification('2026-10-26',RECIPIENT,'verification-failed-1',now=NOW)
    assert retry.digest_sha256==action.digest_sha256 and retry.attachments==action.attachments and retry.body==action.body
    assert retry.submission_token!=action.submission_token
    service=ReportDeliveryService(db,b'0'*32);service.add_recipient('child-junior-3','email','second@example.test','另一合成家长',True)
    with db.connect()as c:
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES('2026-10-23','second@example.test','sent',1,'old family pilot','legacy-family-body','legacy','old','old')");c.commit()
    with pytest.raises(ValueError,match='PILOT_VERIFICATION_ACTION_CONFLICT'):s.reserve_verification('2026-10-26','second@example.test','verification-failed-1',now=NOW)
    with pytest.raises(ValueError,match='PILOT_VERIFICATION_DATE_INVALID'):s.reserve_verification('2026-10-25',RECIPIENT,'verification-wrong-date',now=NOW)
    assert len(calls)==1


def test_payload_cannot_redirect_daily_or_verification_to_unconsented_address(ctx):
    db,s,calls=ctx
    with pytest.raises(ValueError,match='PILOT_RECIPIENT_NOT_AUTHORIZED'):s.claim_due('other@example.test',now=NOW)
    with pytest.raises(ValueError,match='PILOT_RECIPIENT_NOT_AUTHORIZED'):s.reserve_verification('2026-10-26','other@example.test','unauthorized-action',now=NOW)
    assert calls==[]


def test_legacy_sent_address_authorizes_but_explicit_revoke_always_wins(ctx,smtp):
    from home_study_agent.report_delivery import ReportDeliveryService
    db,s,calls=ctx;service=ReportDeliveryService(db,b'0'*32)
    # This isolated migration fixture models a genuinely old sent diagnostic recipient.
    with db.connect()as c:
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES('2026-10-25','legacy@example.test','sent',1,'old','legacy',?,'old','old')",(sha256(b'legacy').hexdigest(),));c.commit()
    action=s.reserve_verification('2026-10-26','legacy@example.test','legacy-authorized-action',now=NOW)
    recipient=service.add_recipient('child-junior-3','email','LEGACY@example.test','历史合成收件人',True)
    service.revoke_recipient(recipient['recipient_id'])
    assert s.submission_check(action)=='PILOT_SUBMISSION_OWNERSHIP_CHANGED'
    with submission_guard(lambda:s.submission_check(action)):
        with pytest.raises(ChannelDeliveryError):service._config=lambda channel:smtp;service.submit_diagnostic_email(action.recipient,action.subject,action.body,attachments=action.attachments)
    assert FakeSmtp.messages==[]
    with pytest.raises(ValueError,match='PILOT_RECIPIENT_REVOKED'):s.claim_due('legacy@example.test',now=NOW)
    with pytest.raises(ValueError,match='PILOT_RECIPIENT_REVOKED'):s.reserve_verification('2026-10-26','legacy@example.test','after-revocation-action',now=NOW)


def test_verification_processing_after_crash_becomes_unknown_not_new_send(ctx):
    from home_study_agent.pilot_test_telemetry import PilotSubmissionReplay
    db,s,calls=ctx;s.reserve_verification('2026-10-26',RECIPIENT,'verification-crash-action',now=NOW);s.initialize()
    with pytest.raises(PilotSubmissionReplay)as replay:s.reserve_verification('2026-10-26',RECIPIENT,'verification-crash-action',now=NOW)
    assert replay.value.result['status']=='unknown'and len(calls)==1


def test_daily_recipient_case_alias_cannot_duplicate_a_legacy_sent_claim(ctx):
    db,s,calls=ctx
    with db.connect()as c:
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES('2026-10-26',?,'sent',1,'old','old','legacy','old','old')",(RECIPIENT.upper(),));c.commit()
    assert s.claim_due(RECIPIENT,now=NOW)is None and calls==[]


def test_http_verification_never_sends_to_old_recipient_after_selection_change(tmp_path,smtp,monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from zoneinfo import ZoneInfo
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'http-switch.db',data_encryption_key=b'0'*32,session_token='synthetic-session',maintenance_enabled=False)
    app=create_app(settings)
    with TestClient(app) as client:
        service=app.state.pilot_test_telemetry
        app.state.report_delivery.save_channel_config('email',dict(smtp,starttls=True))
        service.set_diagnostic_consent(RECIPIENT,consented=True)
        service.configure_diagnostic_recipient('new@example.test','new@example.test')
        day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        response=client.post('/v1/pilot-test-digest/'+day+'/submit-email',headers={'X-HSC-Session':'synthetic-session'},
            json={'recipient':RECIPIENT,'purpose':'runtime_log_attachment_verification','client_action_id':'verify-old-address'})
        assert response.status_code == 422
        assert response.json()['detail']['code'] == 'PILOT_RECIPIENT_SELECTION_CHANGED'
        assert FakeSmtp.messages == [] and FakeSmtp.opened == 0


def test_real_HTTP_verification_freezes_mime_terminal_and_replays_without_a_second_send(tmp_path,smtp):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from zoneinfo import ZoneInfo
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'http.db',data_encryption_key=b'0'*32,session_token='public-synthetic-report-session',maintenance_enabled=False)
    app=create_app(settings)
    with TestClient(app)as client:
        delivery=app.state.report_delivery;db=app.state.pilot_test_telemetry.database
        delivery.save_channel_config('email',dict(smtp,starttls=True))
        with db.connect()as c:
            c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES('2026-01-01',?,'sent',1,'legacy','legacy','legacy','old','old')",(RECIPIENT,));c.commit()
        folder=tmp_path/'diagnostics/logs';folder.mkdir(parents=True)
        stamp=datetime.now(timezone.utc)
        (folder/'runtime.jsonl').write_text(json.dumps({'timestamp':stamp.isoformat(),'privacy_policy_version':'diagnostic_metadata_v2','source':'main','event':'app_process_started','level':'info','details':{'version':'0.1.0','platform':'win32','architecture':'x64','packaged':True}})+'\n')
        day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat();url='/v1/pilot-test-digest/'+day+'/submit-email'
        payload={'recipient':RECIPIENT,'purpose':'runtime_log_attachment_verification','client_action_id':'http-explicit-verification'};headers={'X-HSC-Session':'public-synthetic-report-session'}
        response=client.post(url,json=payload,headers=headers);assert response.status_code==200,response.text
        assert response.json()['accepted']is True and len(FakeSmtp.messages)==1
        message=FakeSmtp.messages[0];assert '附件验证'in message['Subject']
        attachments=list(message.iter_attachments());assert len(attachments)==2 and attachments[1].get_content_type()=='application/zip'
        with ZipFile(BytesIO(attachments[1].get_payload(decode=True)))as z:assert len(z.read('events.jsonl').splitlines())==1
        replay=client.post(url,json=payload,headers=headers);assert replay.status_code==200,replay.text
        assert replay.json()['status']=='sent'and replay.json()['replayed']is True and len(FakeSmtp.messages)==1
        rejected=client.post(url,json={'recipient':RECIPIENT,'purpose':'runtime_log_attachment_verification','client_action_id':'mixed-http-action','digest_sha256':'0'*64},headers=headers)
        assert rejected.status_code==422 and len(FakeSmtp.messages)==1
        with db.connect()as c:
            assert c.execute('SELECT status FROM pilot_test_attachment_verification_outbox').fetchone()[0]=='sent'
            assert c.execute('SELECT COUNT(*)FROM pilot_test_digest_outbox').fetchone()[0]==1


@pytest.mark.parametrize('stamp',['2026-10-25T14:00:00','PRIVATE_INVALID_TIMESTAMP'])
def test_corrupt_window_reports_only_the_closed_code(ctx,stamp):
    db,s,calls=ctx;claim=s.claim_due(RECIPIENT,now=NOW)
    with db.connect()as c:c.execute('UPDATE pilot_test_digest_outbox SET window_start_utc=? WHERE delivery_date=? AND recipient=?',(stamp,claim.delivery_date,claim.recipient));c.commit()
    with pytest.raises(ValueError)as error:s.reserve_submission(claim.delivery_date,claim.recipient,claim.digest_sha256, claim.claim_token)
    assert str(error.value)=='PILOT_ATTACHMENT_WINDOW_INVALID'


def test_private_claim_repr_does_not_log_body_zip_or_attempt_capability(ctx):
    db,s,calls=ctx;claim=s.claim_due(RECIPIENT,now=NOW);owned=s.reserve_submission(claim.delivery_date,claim.recipient,claim.digest_sha256, claim.claim_token)
    text=repr(owned);assert owned.body not in text and owned.submission_token not in text
    assert repr(owned.attachments[0].content)not in repr(owned.attachments[0])


def test_client_claim_without_reserve_cannot_assert_SMTP_delivery_or_advance_attachment_cursor(tmp_path,smtp):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from zoneinfo import ZoneInfo
    from datetime import time,timedelta
    app=create_app(Settings(data_dir=tmp_path,database_path=tmp_path/'unreserved.db',data_encryption_key=b'0'*32,session_token='public-unreserved-session',maintenance_enabled=False))
    with TestClient(app)as client:
        db=app.state.pilot_test_telemetry.database;now=datetime.now(ZoneInfo('Asia/Shanghai'))
        day=now.date()if now.hour>=22 else now.date()-timedelta(days=1)
        observed=datetime.combine(day,time(12),ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc).isoformat()
        with db.connect()as c:
            c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES('2026-01-01',?,'sent',1,'legacy','legacy','legacy','old','old')",(RECIPIENT,))
            c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)VALUES('public-unreserved-audit','system','public_diagnostic_test',NULL,'{}',?)",(observed,));c.commit()
        headers={'X-HSC-Session':'public-unreserved-session'}
        due=client.get('/v1/pilot-test-digest/due',params={'recipient':RECIPIENT},headers=headers);assert due.status_code==200,due.text
        claim=due.json();assert claim['due']is True
        with db.connect()as c:
            row=c.execute("SELECT submission_token,attachment_content FROM pilot_test_digest_outbox WHERE delivery_date=?AND recipient=?",(claim['delivery_date'],RECIPIENT)).fetchone()
            assert row['submission_token']is None and row['attachment_content']is not None
        asserted=client.post('/v1/pilot-test-digest/'+claim['delivery_date']+'/result',json={'recipient':RECIPIENT,'status':'sent','transport':'simple_mapi','claim_token':claim['claim_token']},headers=headers)
        assert asserted.status_code==200,asserted.text
        assert asserted.json()['status']=='failed'and asserted.json()['accepted_at']is None
        with db.connect()as c:
            assert c.execute("SELECT COUNT(*)FROM pilot_test_digest_outbox WHERE status='sent'AND attachment_content IS NOT NULL").fetchone()[0]==0
        repeat=client.get('/v1/pilot-test-digest/due',params={'recipient':RECIPIENT},headers=headers);assert repeat.status_code==200 and repeat.json()['due']is True and repeat.json()['claim_token']!=claim['claim_token']
        assert FakeSmtp.opened==0 and FakeSmtp.messages==[]


@pytest.mark.parametrize('purpose',['daily','verification'])
def test_bundle_collection_does_not_hold_SQLite_writer_and_concurrent_claim_has_one_winner(ctx,monkeypatch,purpose):
    import sqlite3
    from home_study_agent.pilot_test_telemetry import PilotDigestClaim,PilotSubmissionReplay
    db,s,calls=ctx;entered=threading.Event();release=threading.Event();results=[];original=telemetry.build_daily_diagnostic_bundle
    def paused(*a,**k):
        if not entered.is_set():
            entered.set();assert release.wait(4),'BUILDER_TEST_RELEASE_NOT_DELIVERED'
        return original(*a,**k)
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',paused)
    def claim():
        try:
            results.append(s.claim_due(RECIPIENT,now=NOW)if purpose=='daily'else s.reserve_verification('2026-10-26',RECIPIENT,'writer-verification-action',now=NOW))
        except Exception as e:results.append(e)
    first=threading.Thread(target=claim);first.start();assert entered.wait(4)
    writer_ok=False
    try:
        with sqlite3.connect(db.path,timeout=0.15)as other:
            other.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)VALUES('concurrent-public-writer','system','public_test',NULL,'{}','2026-10-26T12:00:00+00:00')");other.commit();writer_ok=True
    except sqlite3.OperationalError:
        pass
    second=threading.Thread(target=claim);second.start();release.set();first.join(5);second.join(5)
    assert not first.is_alive()and not second.is_alive()
    assert writer_ok,'SQLITE_WRITER_HELD_DURING_BUNDLE_IO'
    winners=[x for x in results if isinstance(x,PilotDigestClaim)];assert len(winners)==1
    assert all(x is None or isinstance(x,(PilotDigestClaim,PilotSubmissionReplay))for x in results)
    with db.connect()as c:
        table='pilot_test_digest_outbox'if purpose=='daily'else'pilot_test_attachment_verification_outbox'
        row=c.execute('SELECT attempt_count FROM '+table+" WHERE status='processing'").fetchone();assert row['attempt_count']==1


def test_only_child_A_report_consent_never_authorizes_family_pilot_body_with_child_B(tmp_path,smtp,monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from datetime import time,timedelta
    from zoneinfo import ZoneInfo
    app=create_app(Settings(data_dir=tmp_path,database_path=tmp_path/'child-scope.db',data_encryption_key=b'0'*32,session_token='public-child-scope-session',maintenance_enabled=False))
    with TestClient(app)as client:
        db=app.state.pilot_test_telemetry.database;now=datetime.now(ZoneInfo('Asia/Shanghai'));day=now.date()if now.hour>=22 else now.date()-timedelta(days=1);stamp=datetime.combine(day,time(12),ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc).isoformat()
        with db.connect()as c:
            for child in ('scope-child-A','scope-child-B'):
                c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)VALUES(?,?,'primary_3','jiangsu_nanjing','active',?,?)",(child,'公开合成'+child,stamp,stamp))
            c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)VALUES('scope-B-session','scope-child-B','active','study',?,?,'scope-B-public-session')",(stamp,stamp))
            c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,tool_status,created_at,client_action_id)VALUES('scope-B-turn','scope-child-B','scope-B-session','公开合成B的本孩原文','tutor_help','公开合成回复','{}','test','completed',?,'scope-B-public-turn')",(stamp,));c.commit()
        app.state.report_delivery.add_recipient('scope-child-A','email','teacher-A@example.test','仅甲孩报告老师',True)
        headers={'X-HSC-Session':'public-child-scope-session'}
        due=client.get('/v1/pilot-test-digest/due',params={'recipient':'teacher-A@example.test'},headers=headers)
        assert due.status_code==422 and due.json()['detail']['code']=='PILOT_RECIPIENT_NOT_AUTHORIZED'
        verification=client.post('/v1/pilot-test-digest/'+now.date().isoformat()+'/submit-email',json={'recipient':'teacher-A@example.test','purpose':'runtime_log_attachment_verification','client_action_id':'child-only-verification'},headers=headers)
        assert verification.status_code==422 and verification.json()['detail']['code']=='PILOT_RECIPIENT_NOT_AUTHORIZED'
        with db.connect()as c:
            assert c.execute('SELECT COUNT(*)FROM pilot_test_digest_outbox').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*)FROM pilot_test_attachment_verification_outbox').fetchone()[0]==0
        assert FakeSmtp.opened==0 and FakeSmtp.messages==[]


def test_cursor_change_during_collection_discards_unpublished_bundle(ctx,monkeypatch):
    db,s,calls=ctx;prior=s.claim_due(RECIPIENT,now=datetime(2026,10,25,14,tzinfo=timezone.utc));owned=s.reserve_submission(prior.delivery_date,prior.recipient,prior.digest_sha256, prior.claim_token)
    entered=threading.Event();release=threading.Event();results=[];original=telemetry.build_daily_diagnostic_bundle
    def paused(*a,**k):entered.set();assert release.wait(4);return original(*a,**k)
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',paused)
    thread=threading.Thread(target=lambda:results.append(s.claim_due(RECIPIENT,now=NOW)));thread.start();assert entered.wait(4)
    s.finish_owned_submission(owned,status='sent');release.set();thread.join(5);assert not thread.is_alive()
    assert results==[None]
    with db.connect()as c:assert c.execute("SELECT COUNT(*)FROM pilot_test_digest_outbox WHERE delivery_date='2026-10-26'").fetchone()[0]==0
    monkeypatch.setattr(telemetry,'build_daily_diagnostic_bundle',original)
    fresh=s.claim_due(RECIPIENT,now=NOW);assert fresh and fresh.window_start_utc=='2026-10-25T14:00:00+00:00'
