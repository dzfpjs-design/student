"""Durable local readiness and no duplicate release; fake adapter only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from home_study_agent.daily_report_delivery_state import reserve_local
from home_study_agent.daily_report_observation import prepare
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from local_restore_operation_fixture import restore_local
from test_daily_report_candidate import synthetic
from test_daily_report_email_verification import setup

DAYTIME = datetime(2026,11,4,4,0,tzinfo=timezone.utc)
NIGHT = datetime(2026,11,4,15,0,tzinfo=timezone.utc)


def prepared(synthetic):
    db,day=synthetic
    _,service,fake=setup(synthetic)
    service.save_channel_config('email',{'host':'127.0.0.1','port':25,'from_address':'x@example.test','allow_external_delivery':True})
    assert service.test_channel('a','email','verify-recipient')['status']=='passed'
    with db.connect() as c: source = prepare(c,'a',day,'recipient-a')
    snapshot=service.freeze_daily_observation('a',day,'recipient-a',source)['snapshot_id']
    return db,day,service,fake,snapshot


def test_local_readiness_quiet_hours_and_concurrent_identity(synthetic):
    db,day,service,fake,snapshot=prepared(synthetic)
    with pytest.raises(ValueError,match='QUIET_HOURS'):
        reserve_local(service,'a',day,'recipient-a',snapshot,now=NIGHT)
    def run(_): return reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,range(2)))
    assert all(x['status']=='ready_local_only' and x['external_delivery_permitted'] is False for x in results)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_observation_delivery_ledger').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM report_delivery_outbox').fetchone()[0] == 0
    assert len(fake.calls)==1  # fixed verification only; local readiness never sends
    with pytest.raises(ValueError,match='QUIET_HOURS'):
        reserve_local(service,'a',day,'recipient-a',snapshot,now=NIGHT)


@pytest.mark.parametrize('status',['processing','unknown','sent'])
def test_durable_previous_receipt_never_repeats(synthetic,status):
    db,day,service,_,snapshot=prepared(synthetic)
    reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)
    with db.connect() as c:
        c.execute('UPDATE daily_observation_delivery_ledger SET status=?',(status,)); c.commit()
    with pytest.raises(ValueError,match='PRIOR_DELIVERY_UNRESOLVED_OR_SENT'):
        reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)


def test_replaced_data_key_cannot_bypass_prior_receipt(synthetic):
    from home_study_agent.report_delivery import ReportDeliveryService
    db,day,service,_,snapshot=prepared(synthetic)
    reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)
    with db.connect() as c:
        c.execute("UPDATE daily_observation_delivery_ledger SET status='unknown'")
        c.commit()
    with pytest.raises(ValueError,match='DELIVERY_KEY_HISTORY_UNRECONCILED'):
        reserve_local(ReportDeliveryService(db,b'x'*32),'a',day,'recipient-a',snapshot,now=DAYTIME)


def test_unknown_blocks_new_snapshot_version_same_child_day_email(synthetic):
    from home_study_agent.repository import utc_now
    db,day,service,_,snapshot=prepared(synthetic)
    reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)
    with db.connect() as c:
        old_source = c.execute("SELECT source_id FROM daily_observation_report_outbox WHERE recipient_id='recipient-a'").fetchone()[0]
        c.execute("INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at) VALUES('later','a','s','writing_like',.9,'synthetic',0,'synthetic',0,?)", (utc_now(),))
        c.commit()
        new_source = prepare(c,'a',day,'recipient-a',expected_source_id=old_source)
    replacement = service.freeze_daily_observation('a',day,'recipient-a',new_source)['snapshot_id']
    with db.connect() as c:
        c.execute("UPDATE daily_observation_delivery_ledger SET status='unknown'")
        c.commit()
    with pytest.raises(ValueError,match='PRIOR_DELIVERY_UNRESOLVED_OR_SENT'):
        reserve_local(service,'a',day,'recipient-a',replacement,now=DAYTIME)


def test_email_config_or_authority_change_blocks_readiness(synthetic):
    db,day,service,_,snapshot=prepared(synthetic)
    service._runtime_secrets['email_password']='new-synthetic-secret'
    with pytest.raises(ValueError,match='RECIPIENT_EMAIL_TEST_REQUIRED'):
        reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)
    service._runtime_secrets.clear()
    service.revoke_recipient('recipient-a')
    with pytest.raises(ValueError,match='RECIPIENT_NOT_AUTHORIZED'):
        reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)


def test_latest_failed_channel_test_blocks_older_success(synthetic):
    db,day,service,_,snapshot=prepared(synthetic)
    with db.connect() as c:
        c.execute("UPDATE report_delivery_channel_test SET status='failed' WHERE child_id='a' AND channel='email'")
        c.commit()
    with pytest.raises(ValueError,match='EMAIL_CHANNEL_TEST_REQUIRED'):
        reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)


def test_deleting_unknown_history_sets_barrier(synthetic,tmp_path):
    db,day,service,_,snapshot=prepared(synthetic)
    reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)
    with db.connect() as c:
        c.execute("UPDATE daily_observation_delivery_ledger SET status='unknown'")
        c.commit()
    privacy=PrivacyLifecycleService(db,tmp_path,b's'*32); privacy.initialize()
    assert privacy.delete_child_scope('a','reports','确认删除','delete-report-history')['status']=='completed'
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM daily_observation_delivery_ledger WHERE child_id='a'").fetchone()[0] == 0
        assert tuple(c.execute('SELECT blocked,reason FROM daily_observation_delivery_barrier').fetchone()) == (1,'DELIVERY_HISTORY_ERASED')
def test_restore_before_unknown_creates_durable_global_barrier(synthetic,tmp_path):
    db,day,service,_,snapshot=prepared(synthetic)
    privacy=PrivacyLifecycleService(db,tmp_path,b's'*32); privacy.initialize()
    backup=privacy.create_backup(prune=False)
    reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)
    with db.connect() as c:
        c.execute("UPDATE daily_observation_delivery_ledger SET status='unknown'")
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=last_checkpoint_at WHERE session_id='s'")
        c.commit()
    assert restore_local(privacy,backup['backup_id'],'确认恢复')
    with db.connect() as c:
        barrier=c.execute('SELECT blocked,reason FROM daily_observation_delivery_barrier').fetchone()
        assert tuple(barrier)==(1,'RESTORE_HISTORY_UNRECONCILED')
    with pytest.raises(ValueError,match='RECONCILIATION_REQUIRED'):
        reserve_local(service,'a',day,'recipient-a',snapshot,now=DAYTIME)
