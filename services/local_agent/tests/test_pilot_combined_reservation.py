"""Synthetic durable switching of the existing diagnostic sender; no SMTP."""
from datetime import date, datetime, time
import pytest
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService, PILOT_TIMEZONE
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from local_restore_operation_fixture import restore_local
from test_combined_report_real_sources_fixture import synthetic_combined_family, SYNTHETIC_ADDRESS, SYNTHETIC_KEY


def _at_send_time(day):
    return datetime.combine(date.fromisoformat(day), time(23), PILOT_TIMEZONE)


def test_reservation_survives_restart_blocks_old_claim_and_is_adopted(synthetic_combined_family):
    f=synthetic_combined_family
    f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    restarted=PilotTestTelemetryService(f['db'],f['telemetry'].data_dir)
    restarted.initialize()
    restarted.reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    assert restarted.claim_due(SYNTHETIC_ADDRESS,now=_at_send_time(f['day'])) is None
    batch=restarted.create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)['batch_id']
    with f['db'].connect() as c:
        row=c.execute('SELECT status,error_code,combined_batch_id FROM pilot_test_digest_outbox WHERE delivery_date=?',(f['day'],)).fetchone()
        assert tuple(row)==('processing',None,batch)
    with pytest.raises(ValueError,match='PILOT_COMBINED_DELIVERY_OWNED'):
        restarted.claim_due(SYNTHETIC_ADDRESS,now=_at_send_time(f['day']))


def test_selecting_new_log_recipient_invalidates_existing_batch_and_old_reservation(synthetic_combined_family):
    f = synthetic_combined_family
    batch = f['telemetry'].create_combined_batch(f['day'], SYNTHETIC_ADDRESS, [f['child']], SYNTHETIC_KEY)['batch_id']
    f['telemetry'].configure_diagnostic_recipient('new@example.test', 'new@example.test')
    with pytest.raises(ValueError, match='PILOT_RECIPIENT_SELECTION_CHANGED'):
        f['telemetry'].reserve_combined_day(f['day'], SYNTHETIC_ADDRESS)
    with pytest.raises(ValueError, match='PILOT_RECIPIENT_SELECTION_CHANGED'):
        f['telemetry'].inspect_combined_batch(batch, SYNTHETIC_KEY)
    # Selection never silently consents either the new or prior address.
    assert f['telemetry'].diagnostic_status('new@example.test')['consent'] == 'revoked'
    assert f['telemetry'].diagnostic_status(SYNTHETIC_ADDRESS)['consent'] == 'revoked'


def test_recreated_legacy_consent_table_supports_explicit_combined_flow_and_restart(synthetic_combined_family):
    from home_study_agent.daily_report_combined_schema import schema_valid
    f = synthetic_combined_family
    with f['db'].connect() as connection:
        connection.execute('DROP TABLE pilot_diagnostic_recipient_consent')
        connection.commit()
    f['telemetry'].configure_diagnostic_recipient(SYNTHETIC_ADDRESS, SYNTHETIC_ADDRESS)
    assert f['telemetry'].diagnostic_status(SYNTHETIC_ADDRESS)['consent'] == 'revoked'
    with f['db'].connect() as connection:
        assert schema_valid(connection)
        assert connection.execute('SELECT authority_generation FROM pilot_diagnostic_recipient_consent WHERE recipient=?',(SYNTHETIC_ADDRESS,)).fetchone()[0]
    f['telemetry'].set_diagnostic_consent(SYNTHETIC_ADDRESS, consented=True, require_selected=True)
    f['telemetry'].reserve_combined_day(f['day'], SYNTHETIC_ADDRESS)
    batch = f['telemetry'].create_combined_batch(f['day'], SYNTHETIC_ADDRESS, [f['child']], SYNTHETIC_KEY)['batch_id']
    restarted = PilotTestTelemetryService(f['db'], f['telemetry'].data_dir)
    restarted.initialize()
    assert restarted.inspect_combined_batch(batch, SYNTHETIC_KEY)['batch_id'] == batch


def test_legacy_explicit_consent_gains_generation_without_silent_reauthorization(synthetic_combined_family):
    f = synthetic_combined_family
    with f['db'].connect() as connection:
        connection.execute('DROP TABLE pilot_diagnostic_recipient_consent')
        connection.execute("CREATE TABLE pilot_diagnostic_recipient_consent (recipient TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('consented','revoked')), updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO pilot_diagnostic_recipient_consent VALUES (?,'consented','old')", (SYNTHETIC_ADDRESS,))
        connection.commit()
    f['telemetry'].configure_diagnostic_recipient(SYNTHETIC_ADDRESS, SYNTHETIC_ADDRESS)
    with f['db'].connect() as connection:
        row=connection.execute('SELECT status,authority_generation FROM pilot_diagnostic_recipient_consent WHERE recipient=?',(SYNTHETIC_ADDRESS,)).fetchone()
        assert row['status'] == 'consented' and row['authority_generation']


def test_legacy_revocation_stays_revoked_when_generation_is_added(synthetic_combined_family):
    f = synthetic_combined_family
    with f['db'].connect() as connection:
        connection.execute('DROP TABLE pilot_diagnostic_recipient_consent')
        connection.execute("CREATE TABLE pilot_diagnostic_recipient_consent (recipient TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('consented','revoked')), updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO pilot_diagnostic_recipient_consent VALUES (?,'revoked','old')", (SYNTHETIC_ADDRESS,))
        connection.commit()
    f['telemetry'].configure_diagnostic_recipient(SYNTHETIC_ADDRESS, SYNTHETIC_ADDRESS)
    with f['db'].connect() as connection:
        row=connection.execute('SELECT status,authority_generation FROM pilot_diagnostic_recipient_consent WHERE recipient=?',(SYNTHETIC_ADDRESS,)).fetchone()
        assert row['status'] == 'revoked' and row['authority_generation']
    assert not f['telemetry'].resolve_persisted_recipient()


def test_malformed_generation_schema_refuses_to_save_selection(synthetic_combined_family):
    f = synthetic_combined_family
    with f['db'].connect() as connection:
        connection.execute('DROP TABLE pilot_diagnostic_recipient_consent')
        connection.execute("CREATE TABLE pilot_diagnostic_recipient_consent (recipient TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL, authority_generation INTEGER)")
        connection.execute("INSERT INTO pilot_diagnostic_recipient_consent VALUES (?,'consented','old',7)", (SYNTHETIC_ADDRESS,))
        connection.commit()
    with pytest.raises(ValueError, match='PILOT_CONSENT_SCHEMA_UNAVAILABLE'):
        f['telemetry'].configure_diagnostic_recipient(SYNTHETIC_ADDRESS, SYNTHETIC_ADDRESS)
    with f['db'].connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM pilot_diagnostic_recipient_config').fetchone()[0] == 0
        assert tuple(connection.execute('SELECT status,authority_generation FROM pilot_diagnostic_recipient_consent').fetchone()) == ('consented',7)


def test_explicit_authorization_on_legacy_consent_table_also_creates_generation(synthetic_combined_family):
    f = synthetic_combined_family
    with f['db'].connect() as connection:
        connection.execute('DROP TABLE pilot_diagnostic_recipient_consent')
        connection.execute("CREATE TABLE pilot_diagnostic_recipient_consent (recipient TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('consented','revoked')), updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO pilot_diagnostic_recipient_consent VALUES (?,'revoked','old')", (SYNTHETIC_ADDRESS,))
        connection.commit()
    f['telemetry'].set_diagnostic_consent(SYNTHETIC_ADDRESS, consented=True, require_selected=True)
    with f['db'].connect() as connection:
        row=connection.execute('SELECT status,authority_generation FROM pilot_diagnostic_recipient_consent WHERE recipient=?',(SYNTHETIC_ADDRESS,)).fetchone()
        assert row['status'] == 'consented' and row['authority_generation']


@pytest.mark.parametrize('status',['sent','unknown','processing','failed'])
def test_existing_history_refuses_ownership_switch(synthetic_combined_family,status):
    f=synthetic_combined_family
    with f['db'].connect() as c:
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,created_at,updated_at) VALUES(?,?,?,1,'old','old')",(f['day'],SYNTHETIC_ADDRESS,status)); c.commit()
    with pytest.raises(ValueError,match='PILOT_JOINT_HISTORY_UNRESOLVED'):
        f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    with f['db'].connect() as c:
        assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()[0]==status


def test_old_backup_restores_reservation_as_unknown_tombstone(synthetic_combined_family,tmp_path):
    f=synthetic_combined_family
    privacy=PrivacyLifecycleService(f['db'],tmp_path,SYNTHETIC_KEY); privacy.initialize()
    backup=privacy.create_backup(prune=False)
    f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    assert restore_local(privacy,backup['backup_id'],'确认恢复')
    with f['db'].connect() as c:
        status,error=c.execute('SELECT status,error_code FROM pilot_test_digest_outbox WHERE delivery_date=?',(f['day'],)).fetchone()
        assert status=='unknown' and error=='COMBINED_PRIVACY_TOMBSTONE'
        # Simulate subsequent parent reauthorization; the tombstone must still
        # prevent the old sender from reclaiming this date.
        c.execute("UPDATE report_delivery_recipient SET consent_status='consented',revoked_at=NULL WHERE address=?",(SYNTHETIC_ADDRESS,))
        c.execute("UPDATE pilot_diagnostic_recipient_consent SET status='consented' WHERE recipient=?",(SYNTHETIC_ADDRESS,))
        c.commit()
    assert f['telemetry'].claim_due(SYNTHETIC_ADDRESS,now=_at_send_time(f['day'])) is None


def test_running_app_old_due_endpoint_does_not_submit_reserved_day(synthetic_combined_family,tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from datetime import timedelta
    f=synthetic_combined_family
    settings=Settings(data_dir=tmp_path,database_path=f['db'].path,data_encryption_key=SYNTHETIC_KEY,
                      session_token='synthetic-session',maintenance_enabled=False)
    app=create_app(settings)
    with TestClient(app) as client:
        local_now=datetime.now(PILOT_TIMEZONE)
        target=local_now.date() if local_now.hour>=22 else local_now.date()-timedelta(days=1)
        app.state.pilot_test_telemetry.reserve_combined_day(target.isoformat(),SYNTHETIC_ADDRESS)
        response=client.get('/v1/pilot-test-digest/due',params={'recipient':SYNTHETIC_ADDRESS},
                            headers={'X-HSC-Session':'synthetic-session'})
        assert response.status_code==200 and response.json()=={'due':False}
        with f['db'].connect() as c:
            row=c.execute('SELECT status,attempt_count FROM pilot_test_digest_outbox WHERE delivery_date=?',(target.isoformat(),)).fetchone()
            assert tuple(row)==('pending',0)


def test_corrupt_reservation_blocks_both_owners(synthetic_combined_family):
    f=synthetic_combined_family
    f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    with f['db'].connect() as c:
        c.execute("UPDATE pilot_test_digest_outbox SET attachment_sha256='corrupt' WHERE delivery_date=?",(f['day'],));c.commit()
    assert f['telemetry'].claim_due(SYNTHETIC_ADDRESS,now=_at_send_time(f['day'])) is None
    with pytest.raises(ValueError,match='PILOT_JOINT_HISTORY_UNRESOLVED'):
        f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)


def test_legacy_claim_and_reservation_cannot_both_own_one_date(synthetic_combined_family):
    from concurrent.futures import ThreadPoolExecutor
    f=synthetic_combined_family
    def reserve():
        try:
            f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
            return True
        except ValueError as error:
            assert str(error)=='PILOT_JOINT_HISTORY_UNRESOLVED'
            return False
    def legacy():
        return f['telemetry'].claim_due(SYNTHETIC_ADDRESS,now=_at_send_time(f['day']))
    with ThreadPoolExecutor(max_workers=2) as executor:
        reservation=executor.submit(reserve)
        claim=executor.submit(legacy)
        reserved,claimed=reservation.result(),claim.result()
    assert reserved != (claimed is not None)


def test_unattempted_formal_queue_is_taken_over_with_durable_evidence(synthetic_combined_family):
    from home_study_agent.daily_report_observation import _legacy_status
    from home_study_agent.report_delivery import DeliveryStateError
    f=synthetic_combined_family
    delivery=f['delivery'].enqueue(f['formal']['snapshot_id'],f['recipient'])
    f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    with f['db'].connect() as c:
        row=c.execute('SELECT status,attempt_count,cancel_reason FROM report_delivery_outbox WHERE delivery_id=?',(delivery['delivery_id'],)).fetchone()
        assert tuple(row)==('cancelled',0,'combined_daily_formal_takeover')
        assert _legacy_status(c,f['child'],f['day'],SYNTHETIC_ADDRESS) is None
        assert c.execute('SELECT 1 FROM parent_report_snapshot WHERE snapshot_id=?',(f['formal']['snapshot_id'],)).fetchone()
    with pytest.raises(DeliveryStateError,match='综合日报'):
        f['delivery'].enqueue(f['formal']['snapshot_id'],f['recipient'])
    restarted=PilotTestTelemetryService(f['db'],f['telemetry'].data_dir);restarted.initialize()
    assert restarted.claim_due(SYNTHETIC_ADDRESS,now=_at_send_time(f['day'])) is None
    with f['db'].connect() as c:
        assert _legacy_status(c,f['child'],f['day'],SYNTHETIC_ADDRESS) is None


@pytest.mark.parametrize('status,attempts', [('processing',1),('unknown',1),('sent',1),('failed',1),('cancelled',0)])
def test_formal_prior_attempt_or_explicit_cancellation_prevents_switch(synthetic_combined_family,status,attempts):
    f=synthetic_combined_family
    delivery=f['delivery'].enqueue(f['formal']['snapshot_id'],f['recipient'])
    with f['db'].connect() as c:
        c.execute('UPDATE report_delivery_outbox SET status=?,attempt_count=? WHERE delivery_id=?',(status,attempts,delivery['delivery_id']));c.commit()
    with pytest.raises(ValueError,match='COMBINED_FORMAL_HISTORY_UNRESOLVED'):
        f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    with f['db'].connect() as c:
        assert not c.execute('SELECT 1 FROM pilot_test_digest_outbox').fetchone()
        assert tuple(c.execute('SELECT status,attempt_count FROM report_delivery_outbox WHERE delivery_id=?',(delivery['delivery_id'],)).fetchone())==(status,attempts)


def test_takeover_remains_safe_after_restore_to_old_queued_formal(synthetic_combined_family,tmp_path):
    from home_study_agent.daily_report_observation import _legacy_status
    f=synthetic_combined_family
    delivery=f['delivery'].enqueue(f['formal']['snapshot_id'],f['recipient'])
    privacy=PrivacyLifecycleService(f['db'],tmp_path,SYNTHETIC_KEY);privacy.initialize()
    backup=privacy.create_backup(prune=False)
    f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    assert restore_local(privacy,backup['backup_id'],'确认恢复')
    with f['db'].connect() as c:
        assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()[0]=='unknown'
        assert c.execute('SELECT status FROM report_delivery_outbox WHERE delivery_id=?',(delivery['delivery_id'],)).fetchone()[0]=='unknown'
    # Restore quarantines the old queue entry; the worker cannot reserve it.
    f['delivery']._process_one(delivery['delivery_id'])
    with f['db'].connect() as c:
        assert tuple(c.execute('SELECT status,attempt_count FROM report_delivery_outbox WHERE delivery_id=?',(delivery['delivery_id'],)).fetchone())==('unknown',0)
        assert _legacy_status(c,f['child'],f['day'],SYNTHETIC_ADDRESS) is not None


def test_taken_over_formal_can_join_the_local_combined_snapshot(synthetic_combined_family):
    from home_study_agent.daily_report_combined_snapshot import freeze
    f=synthetic_combined_family
    f['delivery'].enqueue(f['formal']['snapshot_id'],f['recipient'])
    f['telemetry'].reserve_combined_day(f['day'],SYNTHETIC_ADDRESS)
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)['batch_id']
    snapshot=freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)
    assert snapshot['external_delivery_permitted'] is False
