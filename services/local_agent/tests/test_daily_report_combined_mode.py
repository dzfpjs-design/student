"""Schema, real old sender and restore safety on synthetic local data only."""
from datetime import date, datetime, time, timedelta
import pytest
from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.daily_report_combined_mode import enable,state
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.pilot_test_telemetry import PILOT_TIMEZONE
from local_restore_operation_fixture import restore_local
from test_combined_report_real_sources_fixture import synthetic_combined_family, SYNTHETIC_ADDRESS, SYNTHETIC_KEY


@pytest.fixture(autouse=True)
def synthetic_public_readiness(monkeypatch):
    # These tests exercise ownership transitions with no external transport.
    from home_study_agent import daily_report_combined_mode as mode, smtp_owned_worker
    monkeypatch.setattr(smtp_owned_worker, '_public_transport_proven', lambda: True)
    monkeypatch.setattr(mode, '_worker_public_capability_proven', lambda command: True)


def test_v96_upgrade_defaults_to_legacy_and_same_version_damage_fails_closed(tmp_path):
    db=Database(tmp_path/'mode.db');db.initialize()
    with db.connect() as c:
        c.execute('DROP TABLE IF EXISTS smtp_owned_worker_member');c.execute('DROP TABLE IF EXISTS smtp_owned_worker_binding')
        c.execute('DROP TABLE smtp_owned_worker_journal');c.execute('DROP TABLE combined_daily_mode');c.execute('DELETE FROM schema_migration WHERE version>=97');c.commit()
    Database(db.path).initialize()
    with db.connect() as c:
        assert tuple(state(c))==('legacy','')
        assert c.execute('SELECT max(version) FROM schema_migration').fetchone()[0]==LATEST_SCHEMA_VERSION
        c.execute('DELETE FROM combined_daily_mode');c.commit()
    with pytest.raises(Exception,match='COMBINED_MODE_V97_SCHEMA_INVALID'):
        Database(db.path).initialize()
    with db.connect() as c: assert c.execute('SELECT count(*) FROM combined_daily_mode').fetchone()[0]==0


def test_closed_gate_cannot_enable_or_change_legacy(synthetic_combined_family):
    f=synthetic_combined_family
    with pytest.raises(ValueError,match='COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE'):
        enable(f['delivery'])
    with f['db'].connect() as c: assert state(c)['mode']=='legacy'


@pytest.mark.parametrize('condition,reason', [
    ('public_gate', 'SMTP_PUBLIC_TRANSPORT_UNAVAILABLE'),
    ('missing_worker', 'SMTP_WORKER_PACKAGE_INVALID'),
    ('untrusted_worker', 'SMTP_WORKER_PUBLIC_CAPABILITY_UNCONFIRMED'),
])
def test_incomplete_public_readiness_preserves_legacy(synthetic_combined_family, monkeypatch, condition, reason):
    from home_study_agent import daily_report_combined_delivery, daily_report_combined_mode, smtp_owned_process, smtp_owned_worker
    f=synthetic_combined_family
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    if condition == 'public_gate':
        monkeypatch.setattr(smtp_owned_worker,'_public_transport_proven',lambda:False)
    elif condition == 'missing_worker':
        def missing(self): raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
        monkeypatch.setattr(smtp_owned_process.OwnedSmtpProcess,'_worker_command',missing)
    else:
        monkeypatch.setattr(daily_report_combined_mode,'_worker_public_capability_proven',lambda command:False)
    assert daily_report_combined_mode.readiness_reason()==reason
    with pytest.raises(ValueError,match=reason): enable(f['delivery'])
    with f['db'].connect() as c:
        assert state(c)['mode']=='legacy'
        assert c.execute('SELECT count(*) FROM daily_visual_schedule').fetchone()[0]==0


def test_actual_closed_worker_probe_is_not_public_ready(monkeypatch):
    from home_study_agent import daily_report_combined_mode as mode, smtp_owned_process
    # Undo the module's synthetic probe for this test; use the actual worker.
    monkeypatch.undo()
    command=smtp_owned_process.OwnedSmtpProcess(lambda:None,lambda:True,lambda *args:None)._worker_command()
    assert not mode._worker_public_capability_proven(command)


def test_worker_probe_requires_exact_protocol_response(monkeypatch):
    import sys
    from home_study_agent import daily_report_combined_mode as mode
    monkeypatch.undo()
    ready=[sys.executable,'-c',"import sys; sys.stdout.buffer.write(b'HSC_SMTP_PUBLIC_READY_V1\\n')"]
    assert mode._worker_public_capability_proven(ready)
    assert not mode._worker_public_capability_proven([sys.executable,'-c',"print('different')"])
    assert not mode._worker_public_capability_proven(['/missing/smtp/worker'])


def test_capability_probe_reads_independent_worker_gate_from_synthetic_copy(tmp_path,monkeypatch):
    import sys
    from pathlib import Path
    from home_study_agent import daily_report_combined_mode as mode, smtp_owned_worker
    monkeypatch.undo()
    original=Path(smtp_owned_worker.__file__).read_text()
    closed='def _public_transport_proven():\n    # Independent onedir worker boundary: requests and the parent environment\n    # cannot enable Internet access. Opening this requires a reviewed rebuild.\n    return False'
    assert original.count(closed)==1
    worker=tmp_path/'synthetic_worker.py'
    worker.write_text(original.replace(closed,closed.replace('return False','return True'),1))
    assert mode._worker_public_capability_proven([sys.executable,'-I','-S','-B',str(worker)])


def test_real_app_does_not_claim_ready_or_take_legacy_when_worker_gate_closed(synthetic_combined_family,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent import daily_report_combined_delivery, smtp_owned_worker
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    f=synthetic_combined_family
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    monkeypatch.setattr(smtp_owned_worker,'_public_transport_proven',lambda:False)
    settings=Settings(data_dir=tmp_path,database_path=f['db'].path,data_encryption_key=SYNTHETIC_KEY,
                      session_token='synthetic-session',maintenance_enabled=False)
    app=create_app(settings)
    monkeypatch.setattr(app.state.parent_auth,'require',lambda token:None)
    with TestClient(app) as client:
        result=client.get(f"/v1/parent/children/{f['child']}/combined-daily/status",headers={'X-HSC-Session':'synthetic-session'})
        assert result.status_code==200 and result.json()['sending_safety_ready'] is False
        with f['db'].connect() as c: assert state(c)['mode']=='legacy'


def test_enabled_mode_blocks_old_due_and_formal_enqueue(synthetic_combined_family,monkeypatch):
    from home_study_agent import daily_report_combined_delivery
    from home_study_agent.report_delivery import DeliveryStateError
    f=synthetic_combined_family
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    assert enable(f['delivery'])=={'mode':'combined'}
    now=datetime.now(PILOT_TIMEZONE);target=now.date() if now.hour>=22 else now.date()-timedelta(days=1)
    assert f['telemetry'].claim_due(SYNTHETIC_ADDRESS,now=datetime.combine(target,time(23),PILOT_TIMEZONE)) is None
    with pytest.raises(DeliveryStateError,match='综合日报'):
        f['delivery'].enqueue(f['formal']['snapshot_id'],f['recipient'])


def test_restore_older_legacy_backup_blocks_both_delivery_owners(synthetic_combined_family,tmp_path,monkeypatch):
    from home_study_agent import daily_report_combined_delivery
    from home_study_agent.report_delivery import DeliveryStateError
    f=synthetic_combined_family
    privacy=PrivacyLifecycleService(f['db'],tmp_path,SYNTHETIC_KEY);privacy.initialize()
    backup=privacy.create_backup(prune=False)
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    enable(f['delivery'])
    assert restore_local(privacy,backup['backup_id'],'确认恢复')
    with f['db'].connect() as c:
        assert state(c)['mode']=='blocked'
        c.execute("UPDATE report_delivery_recipient SET consent_status='consented',revoked_at=NULL WHERE address=?",(SYNTHETIC_ADDRESS,))
        c.execute("UPDATE pilot_diagnostic_recipient_consent SET status='consented' WHERE recipient=?",(SYNTHETIC_ADDRESS,))
        c.execute("UPDATE parent_report_snapshot SET status='approved' WHERE snapshot_id=?",(f['formal']['snapshot_id'],));c.commit()
    now=datetime.now(PILOT_TIMEZONE);target=now.date() if now.hour>=22 else now.date()-timedelta(days=1)
    assert f['telemetry'].claim_due(SYNTHETIC_ADDRESS,now=datetime.combine(target,time(23),PILOT_TIMEZONE)) is None
    with pytest.raises(DeliveryStateError,match='综合日报'):
        f['delivery'].enqueue(f['formal']['snapshot_id'],f['recipient'])


def test_parent_status_explains_owned_cleanup_and_completion(synthetic_combined_family,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent import daily_report_combined_owned_delivery as owned
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    f=synthetic_combined_family
    settings=Settings(data_dir=tmp_path,database_path=f['db'].path,data_encryption_key=SYNTHETIC_KEY,
                      session_token='synthetic-session',maintenance_enabled=False)
    app=create_app(settings)
    monkeypatch.setattr(app.state.parent_auth,'require',lambda _token:None)
    selected={'status':'cleanup_pending'}
    monkeypatch.setattr(owned,'cleanup_status_for_child',lambda *args:selected.copy())
    with TestClient(app) as client:
        client.headers.update({'X-HSC-Session':'synthetic-session'})
        url=f"/v1/parent/children/{f['child']}/combined-daily/status"
        response=client.get(url).json()['last_check']
        assert response['reason']=='SMTP_WORKER_CLEANUP_PENDING' and '暂不重复发送' in response['message']
        selected['status']='not_submitted'
        response=client.get(url).json()['last_check']
        assert response['reason']=='SMTP_WORKER_ADMISSION_FAILED' and '未进入发送' in response['message']
        selected['status']='unknown'
        response=client.get(url).json()['last_check']
        assert response['reason']=='COMBINED_DELIVERY_RESULT_UNKNOWN' and '不确定' in response['message']
        selected['status']='sent'
        assert client.get(url).json()['last_check']=={'status':'sent'}


def test_real_app_claim_cannot_win_combined_mode_startup(synthetic_combined_family,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent import daily_report_combined_delivery
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    f=synthetic_combined_family
    monkeypatch.setattr(daily_report_combined_delivery,'_source_withdrawal_guard_proven',lambda:True)
    settings=Settings(data_dir=tmp_path,database_path=f['db'].path,data_encryption_key=SYNTHETIC_KEY,
                      session_token='synthetic-session',maintenance_enabled=False)
    app=create_app(settings)
    with TestClient(app) as client:
        result=client.get('/v1/pilot-test-digest/due',params={'recipient':SYNTHETIC_ADDRESS},headers={'X-HSC-Session':'synthetic-session'})
        assert result.status_code==200 and result.json()=={'due':False}
        with f['db'].connect() as c: assert state(c)['mode']=='combined'
