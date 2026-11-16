"""Real report services, synthetic children, and in-process fake mail only."""
from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_auto_delivery import verify_and_schedule_reports, recover_saved_daily_reports
from home_study_agent.report_delivery import DeliveryStateError, ReportDeliveryService
from home_study_agent.repository import utc_now
from test_report_delivery import RecordingEmail, _daily_report
import pytest


def setup(tmp_path):
    db=Database(tmp_path/'family.db'); db.initialize()
    report=_daily_report(db); child=report['child_id']
    reports=ParentReportService(db); snapshot=reports.create_from_daily(report)
    delivery=ReportDeliveryService(db,b'R'*32); delivery.initialize()
    delivery.save_channel_config('email',{'host':'127.0.0.1','port':2525,'from_address':'sender@example.test','use_ssl':False,'starttls':False})
    fake=RecordingEmail(); delivery.adapters['email']=fake
    a=delivery.add_recipient(child,'email','a@example.test','A',True)['recipient_id']
    b=delivery.add_recipient(child,'email','b@example.test','B',True)['recipient_id']
    return db,child,reports,snapshot,delivery,fake,a,b


def recipients(db):
    with db.connect() as c:
        return [(row['recipient_id'],row['status']) for row in c.execute('SELECT recipient_id,status FROM report_delivery_outbox ORDER BY recipient_id')]


def test_only_individually_tested_email_is_queued_then_second_recovers(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    outcome=verify_and_schedule_reports(reports,delivery,child,[snapshot])
    assert outcome['scheduled']==1 and 'RECIPIENT_EMAIL_TEST_REQUIRED' in outcome['blocked']
    assert recipients(db)==[(a,'scheduled')]
    assert len(fake.messages)==1  # only the explicit fake fixed test
    assert delivery.test_channel(child,'email','test-email-b',b)['status']=='passed'
    recovered=recover_saved_daily_reports(reports,delivery,child)
    assert recovered['scheduled']==1
    assert recipients(db)==sorted([(a,'scheduled'),(b,'scheduled')])
    assert recover_saved_daily_reports(reports,delivery,child)['scheduled']==0
    assert len(fake.messages)==2  # recovery never calls a mail adapter


def test_unverified_middle_recipient_does_not_block_later_verified(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    c=delivery.add_recipient(child,'email','c@example.test','C',True)['recipient_id']
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    assert delivery.test_channel(child,'email','test-email-c',c)['status']=='passed'
    result=verify_and_schedule_reports(reports,delivery,child,[snapshot])
    assert result['scheduled']==2 and 'RECIPIENT_EMAIL_TEST_REQUIRED' in result['blocked']
    assert recipients(db)==sorted([(a,'scheduled'),(c,'scheduled')])
    assert len(fake.messages)==2


def test_legacy_channel_pass_without_recipient_receipt_blocks_auto_but_keeps_manual(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    with db.connect() as c:
        c.execute("INSERT INTO report_delivery_channel_test(child_id,channel,status,message,tested_at) VALUES(?,'email','passed','legacy',?)",(child,utc_now()))
        c.commit()
    assert verify_and_schedule_reports(reports,delivery,child,[snapshot])['blocked']==['EMAIL_INITIALIZATION_INCOMPLETE']
    assert recover_saved_daily_reports(reports,delivery,child)['blocked']==['EMAIL_INITIALIZATION_INCOMPLETE']
    assert recipients(db)==[] and reports.get(snapshot['snapshot_id'])['status']=='draft'
    reports.approve(snapshot['snapshot_id'])
    assert delivery.enqueue(snapshot['snapshot_id'],a)['status']=='scheduled'
    assert len(fake.messages)==0


def test_previously_queued_auto_cannot_send_after_verification_changes(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    assert verify_and_schedule_reports(reports,delivery,child,[snapshot])['scheduled']==1
    with db.connect() as c:
        delivery_id=c.execute('SELECT delivery_id FROM report_delivery_outbox WHERE recipient_id=?',(a,)).fetchone()[0]
    delivery.save_channel_config('email',{'host':'127.0.0.1','port':2526,'from_address':'sender@example.test','use_ssl':False,'starttls':False})
    result=delivery._process_one(delivery_id)
    assert result['status']=='cancelled' and result['cancel_reason']=='recipient_email_test_required'
    assert len(fake.messages)==1
    with pytest.raises(DeliveryStateError,match='独立发送测试'):
        delivery.enqueue(snapshot['snapshot_id'],b,automatic=True)


def test_reverify_each_recipient_in_enqueue_transaction_and_continue(tmp_path,monkeypatch):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    assert delivery.test_channel(child,'email','test-email-b',b)['status']=='passed'
    original=delivery.enqueue
    def changed_before_enqueue(snapshot_id,recipient_id,**kwargs):
        if recipient_id == a:
            with db.connect() as c:
                c.execute('DELETE FROM daily_report_email_verification WHERE recipient_id=?',(a,))
                c.commit()
        return original(snapshot_id,recipient_id,**kwargs)
    monkeypatch.setattr(delivery,'enqueue',changed_before_enqueue)
    result=verify_and_schedule_reports(reports,delivery,child,[snapshot])
    assert result['scheduled']==1 and 'DeliveryStateError' in result['blocked']
    assert recipients(db)==[(b,'scheduled')] and len(fake.messages)==2


def test_legacy_unverified_automatic_queue_is_cancelled_before_transport(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    reports.approve(snapshot['snapshot_id'])
    now=utc_now()
    with db.connect() as c:
        c.execute("""INSERT INTO report_delivery_outbox(delivery_id,snapshot_id,recipient_id,channel,idempotency_key,status,scheduled_at,created_at,updated_at)
          VALUES('legacy-auto',?,?,'email','auto:legacy','scheduled',?,?,?)""", (snapshot['snapshot_id'],a,now,now,now))
        c.commit()
    result=delivery._process_one('legacy-auto')
    assert result['status']=='cancelled' and result['cancel_reason']=='recipient_email_test_required'
    assert fake.messages==[]


def test_final_data_check_rejects_removed_recipient_proof(tmp_path,monkeypatch):
    from home_study_agent.notification_channels import _check_submission
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    assert verify_and_schedule_reports(reports,delivery,child,[snapshot])['scheduled']==1
    with db.connect() as c:
        delivery_id=c.execute('SELECT delivery_id FROM report_delivery_outbox WHERE recipient_id=?',(a,)).fetchone()[0]
    class NoDataAdapter:
        attempts=0
        def send(self,*args):
            self.attempts+=1
            with db.connect() as c:
                c.execute('DELETE FROM daily_report_email_verification WHERE recipient_id=?',(a,)); c.commit()
            _check_submission()
            raise AssertionError('DATA must not be submitted after proof deletion')
    no_data=NoDataAdapter(); delivery.adapters['email']=no_data
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _child:False)
    result=delivery._process_one(delivery_id)
    assert no_data.attempts==1 and result['status']=='cancelled'
    assert result['cancel_reason']=='recipient_email_test_required'


def test_failed_second_email_keeps_first_email_verified(tmp_path):
    from home_study_agent.notification_channels import ChannelDeliveryError
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    class FailedAdapter:
        def send(self,*args): raise ChannelDeliveryError('SMTP_UNAVAILABLE','synthetic',False)
    delivery.adapters['email']=FailedAdapter()
    assert delivery.test_channel(child,'email','test-email-b',b)['status']=='failed'
    result=verify_and_schedule_reports(reports,delivery,child,[snapshot])
    assert result['scheduled']==1 and recipients(db)==[(a,'scheduled')]
    assert 'RECIPIENT_EMAIL_TEST_REQUIRED' in result['blocked']


def test_unmarked_old_email_queue_requires_address_verification(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    reports.approve(snapshot['snapshot_id'])
    now=utc_now()
    with db.connect() as c:
        c.execute("""INSERT INTO report_delivery_outbox(delivery_id,snapshot_id,recipient_id,channel,idempotency_key,status,scheduled_at,created_at,updated_at)
          VALUES('legacy-unknown',?,?,'email','initial:unknown-legacy','scheduled',?,?,?)""", (snapshot['snapshot_id'],a,now,now,now)); c.commit()
    result=delivery._process_one('legacy-unknown')
    assert result['status']=='cancelled' and result['cancel_reason']=='recipient_email_test_required'
    assert fake.messages==[]


def test_new_explicit_manual_queue_preserves_original_behavior(tmp_path,monkeypatch):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    reports.approve(snapshot['snapshot_id'])
    queued=delivery.enqueue(snapshot['snapshot_id'],a)
    assert queued['idempotency_key'].startswith('manual:')
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _child:False)
    assert delivery._process_one(queued['delivery_id'])['status']=='sent'
    assert len(fake.messages)==1  # fake adapter only


def test_quiet_hours_start_during_handshake_defers_without_data(tmp_path,monkeypatch):
    from home_study_agent.notification_channels import _check_submission
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    assert verify_and_schedule_reports(reports,delivery,child,[snapshot])['scheduled']==1
    with db.connect() as c: delivery_id=c.execute('SELECT delivery_id FROM report_delivery_outbox WHERE recipient_id=?',(a,)).fetchone()[0]
    state={'quiet':False,'data':False}
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _child:state['quiet'])
    class FakeHandshake:
        def send(self,*args):
            state['quiet']=True
            _check_submission()
            state['data']=True
    delivery.adapters['email']=FakeHandshake()
    result=delivery._process_one(delivery_id)
    assert not state['data'] and result['status']=='scheduled'
    assert result['last_error_code']=='WAITING_QUIET_HOURS' and result['attempt_count']==0


def test_deletion_intent_during_handshake_defers_without_data(tmp_path,monkeypatch):
    from home_study_agent.notification_channels import _check_submission
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    assert verify_and_schedule_reports(reports,delivery,child,[snapshot])['scheduled']==1
    with db.connect() as c: delivery_id=c.execute('SELECT delivery_id FROM report_delivery_outbox WHERE recipient_id=?',(a,)).fetchone()[0]
    state={'data':False}
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _child:False)
    class FakeHandshake:
        def send(self,*args):
            now=utc_now()
            with db.connect() as c:
                c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,created_at,updated_at) VALUES('delete',?,'reports','child_scope',?,'scanning',?,?)", (child,child,now,now)); c.commit()
            _check_submission()
            state['data']=True
    delivery.adapters['email']=FakeHandshake()
    result=delivery._process_one(delivery_id)
    assert not state['data'] and result['status']=='scheduled'
    assert result['last_error_code']=='WAITING_DELETION' and result['attempt_count']==0


def test_reverification_safely_reactivates_original_before_data_cancel(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    assert verify_and_schedule_reports(reports,delivery,child,[snapshot])['scheduled']==1
    with db.connect() as c: delivery_id=c.execute('SELECT delivery_id FROM report_delivery_outbox WHERE recipient_id=?',(a,)).fetchone()[0]
    delivery.save_channel_config('email',{'host':'127.0.0.1','port':2526,'from_address':'sender@example.test','use_ssl':False,'starttls':False})
    assert delivery._process_one(delivery_id)['cancel_reason']=='recipient_email_test_required'
    assert delivery.test_channel(child,'email','test-email-a-again',a)['status']=='passed'
    assert recover_saved_daily_reports(reports,delivery,child)['scheduled']==1
    with db.connect() as c:
        rows=c.execute('SELECT delivery_id,status FROM report_delivery_outbox').fetchall()
        assert [tuple(row) for row in rows] == [(delivery_id,'scheduled')]
    assert len(fake.messages)==2


def test_user_supplied_cancel_reason_cannot_fake_safe_recovery(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    assert verify_and_schedule_reports(reports,delivery,child,[snapshot])['scheduled']==1
    with db.connect() as c: delivery_id=c.execute('SELECT delivery_id FROM report_delivery_outbox WHERE recipient_id=?',(a,)).fetchone()[0]
    delivery.cancel(delivery_id,'recipient_email_test_required')
    assert recover_saved_daily_reports(reports,delivery,child)['scheduled']==0
    assert delivery.get_delivery(delivery_id)['status']=='cancelled'


@pytest.mark.parametrize('status',['sent','unknown'])
def test_other_report_id_same_child_day_email_never_auto_resends(tmp_path,status):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    reports.approve(snapshot['snapshot_id'])
    with db.connect() as c:
        first=c.execute('SELECT * FROM daily_report WHERE report_id=?',(snapshot['report_id'],)).fetchone()
        session=c.execute('SELECT * FROM daily_session WHERE session_id=?',(first['session_id'],)).fetchone()
        session_values=dict(session); session_values.update(session_id='second-session',client_action_id='second-session-action')
        cols=list(session_values); c.execute('INSERT INTO daily_session ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',[session_values[k] for k in cols])
        report_values=dict(first); report_values.update(report_id='second-report',session_id='second-session')
        cols=list(report_values); c.execute('INSERT INTO daily_report ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',[report_values[k] for k in cols])
        snap_values=dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot['snapshot_id'],)).fetchone())
        snap_values.update(snapshot_id='second-snapshot',source_id='second-report',report_id='second-report')
        cols=list(snap_values); c.execute('INSERT INTO parent_report_snapshot ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',[snap_values[k] for k in cols])
        now=utc_now()
        c.execute("INSERT INTO report_delivery_outbox(delivery_id,snapshot_id,recipient_id,channel,idempotency_key,status,scheduled_at,created_at,updated_at) VALUES('prior',?,?,'email','auto:prior',?,?,?,?)", (snapshot['snapshot_id'],a,status,now,now,now))
        c.commit()
    with pytest.raises(DeliveryStateError):
        delivery.enqueue('second-snapshot',a,automatic=True)
    assert recipients(db)==[(a,status)] and len(fake.messages)==1


def test_retained_sent_tombstone_blocks_old_auto_queue(tmp_path):
    from home_study_agent.daily_report_delivery_state import identity_key
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    reports.approve(snapshot['snapshot_id'])
    with db.connect() as c:
        day=c.execute('SELECT report_date FROM daily_report WHERE report_id=?',(snapshot['report_id'],)).fetchone()[0]
        token=identity_key(b'R'*32,child,day,'a@example.test'); now=utc_now()
        c.execute("INSERT INTO daily_observation_delivery_ledger VALUES(?,?,NULL,'sent',?,?)", (token,child,now,now)); c.commit()
    with pytest.raises(DeliveryStateError):
        delivery.enqueue(snapshot['snapshot_id'],a,automatic=True)
    assert recipients(db)==[] and len(fake.messages)==1
