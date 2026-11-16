"""Persisted ledger integration with strictly local synthetic SMTP only."""
from datetime import datetime, timezone
import subprocess
import sys
from pathlib import Path
import pytest
from home_study_agent import daily_report_combined_delivery as combined, daily_report_visual_submission as visual, notification_channels
from home_study_agent.daily_report_delivery_state import reserve_local
from home_study_agent.notification_channels import SmtpChannel
from test_combined_report_real_sources_fixture import synthetic_combined_family
from test_daily_report_combined_snapshot import candidate
from test_combined_report_loopback_candidate import sink, assert_three_sources

DAY=datetime(2026,11,4,4,0,tzinfo=timezone.utc)
NIGHT=datetime(2026,11,4,15,0,tzinfo=timezone.utc)


def configured(f,snapshot,port,monkeypatch):
    service=f['delivery']
    service.save_channel_config('email',{'host':'127.0.0.1','port':port,'from_address':'sender@example.test','starttls':False,'allow_external_delivery':True})
    class Verification:
        def send(self,*args):
            from types import SimpleNamespace
            return SimpleNamespace(provider_message_id=None)
    service.adapters['email']=Verification()
    assert service.test_channel(f['child'],'email','fixture-email-test',recipient_id=f['recipient'])['status']=='passed'
    service.adapters['email']=SmtpChannel()
    reserve_local(service,f['child'],f['day'],f['recipient'],f['visual'],now=DAY)
    with f['db'].connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_daily_delivery'").fetchone(): c.execute(combined.DELIVERY_SCHEMA)
        c.commit()
    monkeypatch.setattr(visual,'_utc_now',lambda:DAY)
    monkeypatch.setattr(notification_channels,'_admit_external_delivery_budget',lambda:None)
    return service


def test_production_gate_refuses_before_any_claim(candidate):
    f,_,snapshot,_=candidate
    with pytest.raises(ValueError,match='COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE'):
        combined.submit(f['delivery'],f['telemetry'],snapshot)


def test_observations_and_log_can_be_sent_without_child_reflection(synthetic_combined_family,monkeypatch):
    from home_study_agent.daily_report_combined_snapshot import freeze
    from test_combined_report_real_sources_fixture import SYNTHETIC_ADDRESS, SYNTHETIC_KEY, OCR_TEXT
    from email import policy
    from email.parser import BytesParser
    f=synthetic_combined_family
    with f['db'].connect() as c:
        c.execute('DELETE FROM parent_report_snapshot WHERE snapshot_id=?',(f['formal']['snapshot_id'],))
        c.execute('DELETE FROM daily_report WHERE session_id=?',(f['session'],))
        c.commit()
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)['batch_id']
    snapshot=freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)['snapshot_id']
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
        assert combined.submit(service,f['telemetry'],snapshot)['status']=='sent'
        message=BytesParser(policy=policy.default).parsebytes(smtp.messages[0])
        assert OCR_TEXT in message.get_body(preferencelist=('plain',)).get_content()
        assert '已完成任务的记录计时' in message.get_body(preferencelist=('plain',)).get_content()
        assert any(part.get_content_type()=='application/zip' for part in message.iter_attachments())


def test_atomic_three_ledger_success_and_pilot_cursor(candidate,monkeypatch):
    f,batch,snapshot,_=candidate
    checked=[]
    def writer_during_network():
        with f['db'].connect() as c:
            c.execute('BEGIN IMMEDIATE'); checked.append(True); c.rollback()
    with sink(on_data=writer_during_network) as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
        assert combined.submit(service,f['telemetry'],snapshot)['status']=='sent'
        assert len(smtp.messages)==1 and checked==[True]
        with f['db'].connect() as c:
            ziphash=c.execute('SELECT attachment_sha256 FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(batch,)).fetchone()[0]
            assert [c.execute('SELECT status FROM '+table).fetchone()[0] for table in ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member','combined_diagnostic_batch','pilot_test_digest_outbox')]==['sent']*5
        assert_three_sources(smtp.messages[0],ziphash)
        with pytest.raises(ValueError): combined.submit(service,f['telemetry'],snapshot)
        assert len(smtp.messages)==1


def test_formal_source_change_during_handshake_blocks_data(candidate,monkeypatch):
    f,batch,snapshot,_=candidate
    def withdraw():
        with f['db'].connect() as c:
            c.execute("UPDATE parent_report_snapshot SET status='revoked' WHERE snapshot_id=?",(f['formal']['snapshot_id'],)); c.commit()
    with sink(on_connect=withdraw) as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
        assert combined.submit(service,f['telemetry'],snapshot)['status']=='unknown'
        assert smtp.data_commands==0
        with f['db'].connect() as c:
            assert [c.execute('SELECT status FROM '+table).fetchone()[0] for table in ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member')]==['unknown']*3


def test_quiet_starts_during_handshake_and_later_sends_once(candidate,monkeypatch):
    f,batch,snapshot,_=candidate; connections=[]
    def cross_quiet():
        connections.append(True)
        if len(connections)==1: monkeypatch.setattr(visual,'_utc_now',lambda:NIGHT)
    with sink(on_connect=cross_quiet) as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
        assert combined.submit(service,f['telemetry'],snapshot)['status']=='ready'
        assert smtp.data_commands==0
        with f['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM combined_daily_delivery').fetchone()[0]==0
            assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0]=='ready'
            assert c.execute('SELECT status FROM combined_diagnostic_member').fetchone()[0]=='waiting'
        monkeypatch.setattr(visual,'_utc_now',lambda:DAY)
        assert combined.submit(service,f['telemetry'],snapshot)['status']=='sent'
        assert smtp.data_commands==1 and len(smtp.messages)==1


def test_recovery_mismatch_rolls_back_all_rows(candidate,monkeypatch):
    f,batch,snapshot,_=candidate
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        with f['db'].connect() as c:
            identity=c.execute('SELECT identity FROM daily_observation_delivery_ledger').fetchone()[0]
            c.execute("UPDATE combined_diagnostic_member SET status='processing',claim_token='t' WHERE batch_id=?",(batch,))
            c.execute("INSERT INTO combined_daily_delivery VALUES(?,?,?,?,'processing','t','old','old')",(identity,batch,f['child'],snapshot)); c.commit()
        with pytest.raises(ValueError,match='COMBINED_CLAIM_LOST'): combined.recover(service)
        with f['db'].connect() as c:
            assert c.execute('SELECT status FROM combined_daily_delivery').fetchone()[0]=='processing'
            assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0]=='ready'
            assert c.execute('SELECT status FROM combined_diagnostic_member').fetchone()[0]=='processing'


@pytest.mark.parametrize('pilot_status',['sent','unknown'])
def test_pilot_terminal_state_blocks_first_child_before_smtp(candidate,monkeypatch,pilot_status):
    f,batch,snapshot,_=candidate
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        with f['db'].connect() as c:
            c.execute('UPDATE pilot_test_digest_outbox SET status=? WHERE combined_batch_id=?',(pilot_status,batch)); c.commit()
        monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
        with pytest.raises(ValueError,match='COMBINED_PILOT_OWNERSHIP_CHANGED'):
            combined.submit(service,f['telemetry'],snapshot)
        assert smtp.data_commands==0
        with f['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM combined_daily_delivery').fetchone()[0]==0
            assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0]=='ready'


def test_crash_after_lost_reply_recovers_all_three_unknown_without_resend(candidate,monkeypatch):
    f,batch,snapshot,tmp_path=candidate
    with sink(lose_reply=True) as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        worker=str(Path(__file__).with_name('combined_delivery_candidate_worker.py'))
        process=subprocess.run([sys.executable,worker,str(f['db'].path),str(tmp_path),snapshot],capture_output=True,timeout=30)
        assert process.returncode==91, process.stderr.decode(errors='replace')[-1000:]
        assert len(smtp.messages)==1
        with f['db'].connect() as c:
            assert [c.execute('SELECT status FROM '+table).fetchone()[0] for table in ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member')]==['processing']*3
        assert combined.recover(service)==1 and combined.recover(service)==0
        with f['db'].connect() as c:
            assert [c.execute('SELECT status FROM '+table).fetchone()[0] for table in ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member','combined_diagnostic_batch','pilot_test_digest_outbox')]==['unknown','unknown','unknown','unknown','processing']
        monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
        with pytest.raises(ValueError): combined.submit(service,f['telemetry'],snapshot)
        assert len(smtp.messages)==1


def test_real_create_app_services_only_with_synthetic_gate(candidate,monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    f,batch,snapshot,tmp_path=candidate
    settings=Settings(data_dir=tmp_path,database_path=f['db'].path,data_encryption_key=b's'*32,maintenance_enabled=False)
    app=create_app(settings)
    with TestClient(app),sink() as smtp:
        f['delivery']=app.state.report_delivery
        f['telemetry']=app.state.pilot_test_telemetry
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        with pytest.raises(ValueError,match='COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE'):
            combined.submit(service,f['telemetry'],snapshot)
        assert smtp.data_commands==0
        monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
        assert combined.submit(service,f['telemetry'],snapshot)['status']=='sent'
        assert smtp.data_commands==1
