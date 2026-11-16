"""Actual scheduler with synthetic local data and loopback SMTP only."""
from datetime import date,datetime,time
from types import SimpleNamespace
import pytest
from home_study_agent import daily_report_combined_delivery, daily_report_combined_schedule, daily_report_visual_submission, notification_channels
from home_study_agent.daily_report_combined_mode import enable
from home_study_agent.notification_channels import SmtpChannel
from home_study_agent.pilot_test_telemetry import PILOT_TIMEZONE
from test_combined_report_real_sources_fixture import synthetic_combined_family,SYNTHETIC_ADDRESS
from test_daily_report_combined_delivery import configured
from test_combined_report_loopback_candidate import sink


@pytest.fixture(autouse=True)
def use_synchronous_loopback_for_scheduler_contract(monkeypatch):
    # These cases exercise scheduler state transitions independently of the
    # owned-process integration, which has its own real-process tests.
    def submit_without_caller_lock(service,telemetry,snapshot_id):
        assert not service.database._external_effect_guard_owned()
        return daily_report_combined_delivery.submit(service,telemetry,snapshot_id)
    monkeypatch.setattr(daily_report_combined_schedule,'_submit',submit_without_caller_lock)
    from home_study_agent import daily_report_combined_mode, smtp_owned_worker
    monkeypatch.setattr(smtp_owned_worker,'_public_transport_proven',lambda:True)
    monkeypatch.setattr(daily_report_combined_mode,'_worker_public_capability_proven',lambda command:True)


def test_safety_gate_prevents_mode_and_scheduled_effects(synthetic_combined_family):
    f=synthetic_combined_family
    assert daily_report_combined_schedule.run_due(f['delivery'],f['telemetry'])=={'status':'legacy'}
    with f['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM pilot_test_digest_outbox').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM daily_visual_schedule').fetchone()[0]==0


def test_two_children_complete_at_22_and_restart_does_not_repeat(synthetic_combined_family,monkeypatch):
    f=synthetic_combined_family; service=f['delivery']; sibling='child-primary-4'
    sibling_recipient=service.add_recipient(sibling,'email',SYNTHETIC_ADDRESS,'synthetic',True)['recipient_id']
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    monkeypatch.setattr(notification_channels,'_admit_external_delivery_budget',lambda:None)
    with sink() as smtp:
        configured(f,None,smtp.server_address[1],monkeypatch)
        class Verification:
            def send(self,*args): return SimpleNamespace(provider_message_id=None)
        service.adapters['email']=Verification()
        assert service.test_channel(sibling,'email','sibling-verify',recipient_id=sibling_recipient)['status']=='passed'
        service.adapters['email']=SmtpChannel()
        for child in (f['child'],sibling): service.update_preferences(child,'23:00','07:00',30)
        enable(service)
        when=datetime.combine(date.fromisoformat(f['day']),time(22,5),PILOT_TIMEZONE)
        monkeypatch.setattr(daily_report_visual_submission,'_utc_now',lambda:when)
        outcome=daily_report_combined_schedule.run_due(service,f['telemetry'],now=when)
        assert outcome['status']=='sent', outcome
        assert len(smtp.messages)==2
        with f['db'].connect() as c:
            assert [r[0] for r in c.execute('SELECT status FROM combined_diagnostic_member ORDER BY child_id')]==['sent','sent']
            assert len({r[0] for r in c.execute('SELECT next_day FROM daily_visual_schedule')})==1
        again=daily_report_combined_schedule.run_due(service,f['telemetry'],now=when)
        assert again['status']=='waiting_for_cutoff'
        assert len(smtp.messages)==2


def test_lost_reply_stops_cursor_and_restart_does_not_resend(synthetic_combined_family,monkeypatch):
    from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
    f=synthetic_combined_family;service=f['delivery'];sibling='child-primary-4'
    sibling_recipient=service.add_recipient(sibling,'email',SYNTHETIC_ADDRESS,'synthetic',True)['recipient_id']
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    monkeypatch.setattr(notification_channels,'_admit_external_delivery_budget',lambda:None)
    with sink(lose_reply=True) as smtp:
        configured(f,None,smtp.server_address[1],monkeypatch)
        class Verification:
            def send(self,*args): return SimpleNamespace(provider_message_id=None)
        service.adapters['email']=Verification()
        assert service.test_channel(sibling,'email','sibling-verify',recipient_id=sibling_recipient)['status']=='passed'
        service.adapters['email']=SmtpChannel()
        for child in (f['child'],sibling):service.update_preferences(child,'23:00','07:00',30)
        enable(service)
        when=datetime.combine(date.fromisoformat(f['day']),time(22,5),PILOT_TIMEZONE)
        monkeypatch.setattr(daily_report_visual_submission,'_utc_now',lambda:when)
        result=daily_report_combined_schedule.run_due(service,f['telemetry'],now=when)
        assert result=={'status':'blocked','reason':'COMBINED_DELIVERY_RESULT_UNKNOWN'}
        assert len(smtp.messages)==1
        restarted=PilotTestTelemetryService(f['db'],f['telemetry'].data_dir);restarted.initialize()
        assert daily_report_combined_schedule.run_due(service,restarted,now=when)==result
        assert len(smtp.messages)==1
        with f['db'].connect() as c:
            assert {r[0] for r in c.execute('SELECT next_day FROM daily_visual_schedule')}=={f['day']}
            assert c.execute("SELECT status FROM combined_diagnostic_batch").fetchone()[0]=='unknown'


def test_missing_family_authority_blocks_and_creates_no_log_batch(synthetic_combined_family,monkeypatch):
    f=synthetic_combined_family
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    enable(f['delivery'])
    when=datetime.combine(date.fromisoformat(f['day']),time(22,5),PILOT_TIMEZONE)
    result=daily_report_combined_schedule.run_due(f['delivery'],f['telemetry'],now=when)
    assert result=={'status':'blocked','reason':'COMBINED_FAMILY_OR_SCHEDULE_REVIEW_REQUIRED'}
    with f['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM combined_diagnostic_batch').fetchone()[0]==0


def _schedule_fixture(f,monkeypatch,port):
    from home_study_agent import daily_report_combined_mode, smtp_owned_worker
    monkeypatch.setattr(smtp_owned_worker,'_public_transport_proven',lambda:True)
    monkeypatch.setattr(daily_report_combined_mode,'_worker_public_capability_proven',lambda command:True)
    service=f['delivery'];sibling='child-primary-4'
    sibling_recipient=service.add_recipient(sibling,'email',SYNTHETIC_ADDRESS,'synthetic',True)['recipient_id']
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    monkeypatch.setattr(notification_channels,'_admit_external_delivery_budget',lambda:None)
    configured(f,None,port,monkeypatch)
    class Verification:
        def send(self,*args): return SimpleNamespace(provider_message_id=None)
    service.adapters['email']=Verification()
    assert service.test_channel(sibling,'email','sibling-verify',recipient_id=sibling_recipient)['status']=='passed'
    service.adapters['email']=SmtpChannel()
    for child in (f['child'],sibling):service.update_preferences(child,'23:00','07:00',30)
    enable(service)
    when=datetime.combine(date.fromisoformat(f['day']),time(22,5),PILOT_TIMEZONE)
    monkeypatch.setattr(daily_report_visual_submission,'_utc_now',lambda:when)
    return service,when


def test_scheduler_reports_cleanup_without_advancing_cursor(synthetic_combined_family,monkeypatch):
    f=synthetic_combined_family
    with sink() as smtp:
        service,when=_schedule_fixture(f,monkeypatch,smtp.server_address[1])
        monkeypatch.setattr(daily_report_combined_schedule,'_submit',lambda *args:{'status':'cleanup_pending'})
        assert daily_report_combined_schedule.run_due(service,f['telemetry'],now=when)=={
            'status':'blocked','reason':'SMTP_WORKER_CLEANUP_PENDING'}
        with f['db'].connect() as c:
            assert {r[0] for r in c.execute('SELECT next_day FROM daily_visual_schedule')}=={f['day']}
        assert not smtp.messages


def test_scheduler_rebuilds_stale_unsubmitted_candidates_then_sends(synthetic_combined_family,monkeypatch):
    f=synthetic_combined_family
    with sink() as smtp:
        service,when=_schedule_fixture(f,monkeypatch,smtp.server_address[1])
        original=daily_report_combined_delivery.submit
        def wait(*args):raise ValueError('QUIET_HOURS')
        monkeypatch.setattr(daily_report_combined_delivery,'submit',wait)
        assert daily_report_combined_schedule.run_due(service,f['telemetry'],now=when)['reason']=='QUIET_HOURS'
        with f['db'].connect() as c:
            before=c.execute('SELECT snapshot_id FROM combined_daily_snapshot WHERE child_id=?',(f['child'],)).fetchone()[0]
            c.execute("UPDATE parent_report_snapshot SET parent_edit_json='{\"changed\":true}' WHERE snapshot_id=?",(f['formal']['snapshot_id'],));c.commit()
        monkeypatch.setattr(daily_report_combined_delivery,'submit',original)
        assert daily_report_combined_schedule.run_due(service,f['telemetry'],now=when)['status']=='sent'
        with f['db'].connect() as c:
            after=c.execute('SELECT snapshot_id FROM combined_daily_snapshot WHERE child_id=?',(f['child'],)).fetchone()[0]
            assert after!=before
        assert len(smtp.messages)==2


@pytest.mark.parametrize('tamper_sent_ledger',[None,'status','snapshot'])
def test_stale_waiting_sibling_after_first_sent_rebuilds_without_duplicate(synthetic_combined_family,monkeypatch,tamper_sent_ledger):
    f=synthetic_combined_family;sibling='child-primary-4'
    with sink() as smtp:
        service,when=_schedule_fixture(f,monkeypatch,smtp.server_address[1])
        original=daily_report_combined_delivery.submit;calls=[]
        def stop_after_first(*args):
            calls.append(True)
            if len(calls)==1:return original(*args)
            raise ValueError('QUIET_HOURS')
        monkeypatch.setattr(daily_report_combined_delivery,'submit',stop_after_first)
        assert daily_report_combined_schedule.run_due(service,f['telemetry'],now=when)['reason']=='QUIET_HOURS'
        assert len(smtp.messages)==1
        with f['db'].connect() as c:
            c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                SELECT 'other-session',?,'completed','completed',started_at,last_checkpoint_at,'other-action' FROM daily_session WHERE session_id=?""",(sibling,f['session']))
            c.execute("""INSERT INTO daily_report(report_id,session_id,child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at)
                SELECT 'other-report','other-session',?,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at FROM daily_report WHERE session_id=?""",(sibling,f['session']));c.commit()
            if tamper_sent_ledger=='status':
                c.execute("UPDATE daily_observation_delivery_ledger SET status='unknown' WHERE child_id=?",(f['child'],));c.commit()
            elif tamper_sent_ledger=='snapshot':
                c.execute("UPDATE daily_observation_delivery_ledger SET snapshot_id='synthetic-wrong' WHERE child_id=?",(f['child'],));c.commit()
        monkeypatch.setattr(daily_report_combined_delivery,'submit',original)
        first_message=smtp.messages[0]
        result=daily_report_combined_schedule.run_due(service,f['telemetry'],now=when)
        if tamper_sent_ledger:
            assert result=={'status':'blocked','reason':'COMBINED_SENT_SIBLING_REVIEW_REQUIRED'}
            assert len(smtp.messages)==1 and smtp.messages[0]==first_message
            with f['db'].connect() as c:
                assert [r[0] for r in c.execute('SELECT status FROM combined_diagnostic_member ORDER BY child_id')]==['sent','waiting']
            return
        assert result['status']=='sent'
        assert len(smtp.messages)==2 and smtp.messages[0]==first_message
        from email import policy
        from email.parser import BytesParser
        def archive(raw):
            message=BytesParser(policy=policy.default).parsebytes(raw)
            return next(part.get_payload(decode=True) for part in message.iter_attachments() if part.get_content_type()=='application/zip')
        assert archive(smtp.messages[0])==archive(smtp.messages[1])
        with f['db'].connect() as c:
            assert [r[0] for r in c.execute('SELECT status FROM combined_diagnostic_member ORDER BY child_id')]==['sent','sent']
