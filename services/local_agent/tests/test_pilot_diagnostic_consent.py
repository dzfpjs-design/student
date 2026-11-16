from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService


@pytest.fixture(autouse=True)
def configured_synthetic_recipient(monkeypatch):
    monkeypatch.setenv('HSC_PILOT_TEST_RECIPIENT', 'guardian@example.test')


def test_fresh_install_consent_survives_restart_and_revocation(tmp_path):
    db = Database(tmp_path / 'family.db', media_root=tmp_path, data_encryption_key=b'0'*32)
    db.initialize()
    service = PilotTestTelemetryService(db, tmp_path)
    service.initialize()
    address = 'guardian@example.test'
    now = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match='PILOT_RECIPIENT_NOT_AUTHORIZED'):
        service.claim_due(address, now=now)
    service.set_diagnostic_consent(address, consented=True)
    with db.connect() as connection:
        service._require_recipient(connection, address.upper())
        assert connection.execute('SELECT COUNT(*) FROM pilot_test_digest_outbox').fetchone()[0] == 0
    restarted = PilotTestTelemetryService(db, tmp_path)
    restarted.initialize()
    with db.connect() as connection:
        restarted._require_recipient(connection, address)
        with pytest.raises(ValueError, match='PILOT_RECIPIENT_NOT_AUTHORIZED'):
            restarted._require_recipient(connection, 'other@example.test')
    restarted.set_diagnostic_consent(address, consented=False)
    with db.connect() as connection:
        connection.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,created_at,updated_at) VALUES('2026-01-01',?,'sent','old','old')", (address,))
        connection.commit()
        with pytest.raises(ValueError, match='PILOT_RECIPIENT_REVOKED'):
            restarted._require_recipient(connection, address)


def test_consent_endpoint_requires_parent_auth(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'family.db', data_encryption_key=b'0'*32, session_token='synthetic', maintenance_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.put('/v1/parent/pilot-diagnostic-consent', headers={'X-HSC-Session':'synthetic'}, json={'recipient':'guardian@example.test','consented':True})
        assert response.status_code in (401, 403)
        with app.state.pilot_test_telemetry.database.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM pilot_diagnostic_recipient_consent').fetchone()[0] == 0


def test_parent_can_authorize_and_revoke_configured_address(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'family.db', data_encryption_key=b'0'*32, session_token='synthetic', maintenance_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        session = {'X-HSC-Session':'synthetic'}
        token = client.post('/v1/parent/setup', headers=session, json={'pin':'2468'}).json()['parent_token']
        headers = {**session, 'X-HSC-Parent':token}
        status = client.get('/v1/parent/pilot-diagnostic-consent', headers=headers).json()
        assert status['consent'] == 'not_authorized'
        body = {'recipient':status['recipient'], 'consented':True}
        assert client.put('/v1/parent/pilot-diagnostic-consent', headers=headers, json=body).status_code == 200
        assert client.get('/v1/parent/pilot-diagnostic-consent', headers=headers).json()['consent'] == 'consented'
        assert client.put('/v1/parent/pilot-diagnostic-consent', headers=headers, json={**body,'recipient':'other@example.test'}).status_code == 422
        assert client.put('/v1/parent/pilot-diagnostic-consent', headers=headers, json={**body,'consented':False}).status_code == 200
        assert client.get('/v1/parent/pilot-diagnostic-consent', headers=headers).json()['consent'] == 'revoked'


def test_legacy_backup_quarantine_does_not_reauthorize_from_sent_history(tmp_path):
    from home_study_agent.report_delivery import ReportDeliveryService
    db = Database(tmp_path/'family.db', media_root=tmp_path, data_encryption_key=b'0'*32)
    db.initialize()
    service = PilotTestTelemetryService(db, tmp_path)
    service.initialize()
    with db.connect() as c:
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,created_at,updated_at) VALUES('2026-01-01','guardian@example.test','sent','old','old')")
        c.execute('DROP TABLE pilot_diagnostic_recipient_consent')
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        ReportDeliveryService.quarantine_after_restore(c, 'public-restore')
        c.commit()
    service.initialize()
    with db.connect() as c:
        with pytest.raises(ValueError, match='PILOT_RECIPIENT_NOT_AUTHORIZED'):
            service._require_recipient(c, 'guardian@example.test')
    service.set_diagnostic_consent('guardian@example.test', consented=True)
    with db.connect() as c:
        service._require_recipient(c, 'guardian@example.test')


def test_child_revocation_blocks_family_mail_and_identifies_child(tmp_path):
    from home_study_agent.report_delivery import ReportDeliveryService
    db = Database(tmp_path/'family.db', media_root=tmp_path, data_encryption_key=b'0'*32)
    db.initialize()
    service = PilotTestTelemetryService(db, tmp_path)
    service.initialize()
    with db.connect() as c:
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('c','小明','primary_3','jiangsu_nanjing','active','x','x')")
        c.commit()
    delivery = ReportDeliveryService(db,b'0'*32)
    delivery.initialize()
    row = delivery.add_recipient('c','email','guardian@example.test','家长',True)
    service.set_diagnostic_consent('guardian@example.test', consented=True)
    delivery.revoke_recipient(row['recipient_id'])
    result = service.diagnostic_status('guardian@example.test')
    assert result['consent'] == 'revoked' and result['blocked_children'] == ['小明']
    with db.connect() as c:
        with pytest.raises(ValueError, match='PILOT_RECIPIENT_REVOKED'):
            service._require_recipient(c, 'guardian@example.test')


def test_private_provision_requires_attestation_and_never_regrants_revocation(tmp_path):
    import hashlib, hmac
    key = 'synthetic-attestation-key'
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'family.db', data_encryption_key=b'0'*32, session_token='synthetic', native_launch_attestation_key=key, maintenance_enabled=False)
    app = create_app(settings)
    recipient = 'guardian@example.test'
    bootstrap = '11111111-1111-4111-8111-111111111111'
    sig = hmac.new(key.encode(), f'hsc-pilot-diagnostic-consent-v1\n{bootstrap}\n{recipient}'.encode(), hashlib.sha256).hexdigest()
    payload = {'recipient':recipient,'bootstrap_id':bootstrap,'signature':sig}
    with TestClient(app) as client:
        headers = {'X-HSC-Session':'synthetic'}
        assert client.post('/v1/pilot-test-digest/provision-consent', headers=headers, json={**payload,'signature':'0'*64}).status_code == 403
        assert client.post('/v1/pilot-test-digest/provision-consent', headers=headers, json=payload).status_code == 200
        service = app.state.pilot_test_telemetry
        assert service.diagnostic_status(recipient)['consent'] == 'consented'
        service.set_diagnostic_consent(recipient, consented=False)
        assert client.post('/v1/pilot-test-digest/provision-consent', headers=headers, json=payload).status_code == 200
        assert service.diagnostic_status(recipient)['consent'] == 'revoked'


def test_quarantined_restore_refuses_provision_across_restart(tmp_path):
    import hashlib, hmac
    from home_study_agent.report_delivery import ReportDeliveryService
    key, recipient = 'synthetic-attestation-key', 'guardian@example.test'
    bootstrap = '11111111-1111-4111-8111-111111111111'
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'family.db', data_encryption_key=b'0'*32,
                        session_token='synthetic', native_launch_attestation_key=key, maintenance_enabled=False)
    message = f'hsc-pilot-diagnostic-consent-v1\n{bootstrap}\n{recipient}'.encode()
    payload = {'recipient':recipient,'bootstrap_id':bootstrap,'signature':hmac.new(key.encode(), message, hashlib.sha256).hexdigest()}
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.pilot_test_telemetry.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            ReportDeliveryService.quarantine_after_restore(connection, 'synthetic-restore')
            connection.commit()
        response = client.post('/v1/pilot-test-digest/provision-consent', headers={'X-HSC-Session':'synthetic'}, json=payload)
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'PILOT_CONSENT_RESTORE_QUARANTINED'
    with TestClient(create_app(settings)) as restarted:
        response = restarted.post('/v1/pilot-test-digest/provision-consent', headers={'X-HSC-Session':'synthetic'}, json=payload)
        assert response.status_code == 409


