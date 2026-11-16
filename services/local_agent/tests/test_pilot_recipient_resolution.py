from fastapi.testclient import TestClient
import pytest
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from home_study_agent.app import create_app
from home_study_agent.config import Settings


def _client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, database_path=tmp_path / 'family.db',
                              data_encryption_key=b'0' * 32, session_token='synthetic', maintenance_enabled=False))
    return app, TestClient(app)


def test_saved_consent_is_recovered_without_a_built_in_address_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    app, client = _client(tmp_path)
    with client:
        assert client.get('/v1/pilot-test-digest/config').status_code in (401, 403)
        assert client.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session': 'synthetic'}).json()['recipient'] == ''
        app.state.pilot_test_telemetry.set_diagnostic_consent('guardian@example.test', consented=True)
        assert client.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session': 'synthetic'}).json()['recipient'] == 'guardian@example.test'
    app2, client2 = _client(tmp_path)
    with client2:
        assert client2.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session': 'synthetic'}).json()['recipient'] == 'guardian@example.test'
        app2.state.pilot_test_telemetry.set_diagnostic_consent('guardian@example.test', consented=False)
        assert client2.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session': 'synthetic'}).json()['recipient'] == ''
        session = {'X-HSC-Session': 'synthetic'}
        token = client2.post('/v1/parent/setup', headers=session, json={'pin': '2468'}).json()['parent_token']
        headers = {**session, 'X-HSC-Parent': token}
        status = client2.get('/v1/parent/pilot-diagnostic-consent', headers=headers).json()
        assert status['recipient'] == 'guardian@example.test' and status['consent'] == 'revoked'
        assert client2.put('/v1/parent/pilot-diagnostic-consent', headers=headers,
                           json={'recipient': 'guardian@example.test', 'consented': True}).status_code == 200
        assert client2.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == 'guardian@example.test'


def test_multiple_saved_recipients_or_missing_configuration_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    app, client = _client(tmp_path)
    with client:
        session = {'X-HSC-Session': 'synthetic'}
        service = app.state.pilot_test_telemetry
        service.set_diagnostic_consent('a@example.test', consented=True)
        service.set_diagnostic_consent('b@example.test', consented=True)
        assert client.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == ''
        service.set_diagnostic_consent('a@example.test', consented=False)
        # A different authorized address must not silently become the target.
        assert client.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == ''
        with pytest.raises(ValueError, match='PILOT_RECIPIENT_ALIAS_AMBIGUOUS'):
            service.claim_due('b@example.test', require_unambiguous=True)
        service.set_diagnostic_consent('b@example.test', consented=False)
        token = client.post('/v1/parent/setup', headers=session, json={'pin': '2468'}).json()['parent_token']
        result = client.put('/v1/parent/pilot-diagnostic-consent', headers={**session, 'X-HSC-Parent': token},
                            json={'recipient': 'new@example.test', 'consented': True})
        assert result.status_code == 422
        assert result.json()['detail']['code'] == 'PILOT_RECIPIENT_NOT_CONFIGURED'


def test_new_install_requires_an_issuer_signed_resource_without_a_builtin_address(tmp_path, monkeypatch):
    from home_study_agent import pilot_consent_issuer
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(pilot_consent_issuer, 'ISSUER_PUBLIC_KEY', key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    attestation = 'synthetic-attestation'
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'family.db', data_encryption_key=b'0' * 32,
                        session_token='synthetic', native_launch_attestation_key=attestation, maintenance_enabled=False)
    app = create_app(settings)
    recipient, bootstrap = 'guardian@example.test', '11111111-1111-4111-8111-111111111111'
    message = f'hsc-pilot-diagnostic-consent-v1\n{bootstrap}\n{recipient}'.encode()
    payload = {'recipient': recipient, 'bootstrap_id': bootstrap,
               'signature': hmac.new(attestation.encode(), message, hashlib.sha256).hexdigest(),
               'issuer_signature': base64.b64encode(key.sign(message)).decode()}
    with TestClient(app) as client:
        headers = {'X-HSC-Session': 'synthetic'}
        invalid = client.post('/v1/pilot-test-digest/provision-consent', headers=headers,
                              json={**payload, 'issuer_signature': base64.b64encode(bytes(64)).decode()})
        assert invalid.status_code == 403
        assert client.get('/v1/pilot-test-digest/config', headers=headers).json()['recipient'] == ''
        assert client.post('/v1/pilot-test-digest/provision-consent', headers=headers, json=payload).status_code == 200
        assert client.get('/v1/pilot-test-digest/config', headers=headers).json()['recipient'] == recipient
        app.state.pilot_test_telemetry.set_diagnostic_consent(recipient, consented=False)
        assert client.post('/v1/pilot-test-digest/provision-consent', headers=headers, json=payload).status_code == 200
        assert client.get('/v1/pilot-test-digest/config', headers=headers).json()['recipient'] == ''


def test_parent_selects_and_confirms_without_granting_consent_then_can_authorize(tmp_path, monkeypatch):
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    app, client = _client(tmp_path)
    address = 'guardian@example.test'
    with client:
        session = {'X-HSC-Session': 'synthetic'}
        payload = {'recipient': address, 'confirmation': address}
        assert client.put('/v1/parent/pilot-diagnostic-recipient', headers=session, json=payload).status_code in (401, 403)
        token = client.post('/v1/parent/setup', headers=session, json={'pin': '2468'}).json()['parent_token']
        headers = {**session, 'X-HSC-Parent': token}
        mismatch = client.put('/v1/parent/pilot-diagnostic-recipient', headers=headers,
                              json={'recipient': address, 'confirmation': 'other@example.test'})
        assert mismatch.status_code == 422
        assert client.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == ''
        assert client.put('/v1/parent/pilot-diagnostic-recipient', headers=headers, json=payload).status_code == 200
        assert client.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == ''
        status = client.get('/v1/parent/pilot-diagnostic-consent', headers=headers).json()
        assert status['recipient'] == address and status['consent'] == 'revoked'
        assert client.put('/v1/parent/pilot-diagnostic-consent', headers=headers,
                          json={'recipient': address, 'consented': True}).status_code == 200
        assert client.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == address
    _, client2 = _client(tmp_path)
    with client2:
        assert client2.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == address


def test_selecting_new_address_does_not_reuse_old_or_historical_consent(tmp_path, monkeypatch):
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    app, client = _client(tmp_path)
    with client:
        session = {'X-HSC-Session': 'synthetic'}
        service = app.state.pilot_test_telemetry
        service.set_diagnostic_consent('old@example.test', consented=True)
        service.set_diagnostic_consent('new@example.test', consented=True)
        token = client.post('/v1/parent/setup', headers=session, json={'pin': '2468'}).json()['parent_token']
        headers = {**session, 'X-HSC-Parent': token}
        result = client.put('/v1/parent/pilot-diagnostic-recipient', headers=headers,
                            json={'recipient': 'new@example.test', 'confirmation': 'new@example.test'})
        assert result.status_code == 200
        assert client.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == ''
        with pytest.raises(ValueError, match='PILOT_RECIPIENT_REVOKED'):
            service.claim_due('new@example.test', require_unambiguous=True)


def test_legacy_single_sent_recipient_keeps_prior_authority_after_restart(tmp_path, monkeypatch):
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    app, client = _client(tmp_path)
    with client:
        with app.state.pilot_test_telemetry.database.connect() as connection:
            connection.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,created_at,updated_at) VALUES('2026-01-01','guardian@example.test','sent','old','old')")
            connection.commit()
        assert client.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session':'synthetic'}).json()['recipient'] == 'guardian@example.test'
    app2, restarted = _client(tmp_path)
    with restarted:
        assert restarted.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session':'synthetic'}).json()['recipient'] == 'guardian@example.test'
        app2.state.pilot_test_telemetry.set_diagnostic_consent('guardian@example.test', consented=False)
        assert restarted.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session':'synthetic'}).json()['recipient'] == ''


def test_restored_older_schema_is_read_safely_until_reinitialized(tmp_path, monkeypatch):
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    app, client = _client(tmp_path)
    with client:
        service = app.state.pilot_test_telemetry
        service.set_diagnostic_consent('guardian@example.test', consented=True)
        with service.database.connect() as connection:
            connection.execute('DROP TABLE pilot_diagnostic_recipient_config')
            connection.commit()
        assert client.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session':'synthetic'}).json()['recipient'] == 'guardian@example.test'
        assert service.configure_diagnostic_recipient('guardian@example.test', 'guardian@example.test')['recipient'] == 'guardian@example.test'
        assert client.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session':'synthetic'}).json()['recipient'] == 'guardian@example.test'
        with service.database.connect() as connection:
            connection.execute('DROP TABLE pilot_diagnostic_recipient_consent')
            connection.commit()
        assert client.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session':'synthetic'}).json()['recipient'] == ''
        assert service.configure_diagnostic_recipient('guardian@example.test', 'guardian@example.test')['recipient'] == 'guardian@example.test'
        assert client.get('/v1/pilot-test-digest/config', headers={'X-HSC-Session':'synthetic'}).json()['recipient'] == ''
        assert service.diagnostic_status('guardian@example.test')['consent'] == 'revoked'


def test_changing_recipient_blocks_prior_verification_and_preserves_new_explicit_flow(tmp_path, monkeypatch):
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    app, client = _client(tmp_path)
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    with client:
        service = app.state.pilot_test_telemetry
        service.set_diagnostic_consent('old@example.test', consented=True)
        old_claim = service.reserve_verification('2026-11-04', 'old@example.test', 'old-verification', now=now)
        assert service.submission_check(old_claim) is None
        service.configure_diagnostic_recipient('new@example.test', 'new@example.test')
        assert service.submission_check(old_claim) == 'PILOT_SUBMISSION_OWNERSHIP_CHANGED'
        with pytest.raises(ValueError, match='PILOT_RECIPIENT_SELECTION_CHANGED'):
            service.reserve_verification('2026-11-04', 'old@example.test', 'second-verification', now=now)
        service.set_diagnostic_consent('new@example.test', consented=True, require_selected=True)
        new_claim = service.reserve_verification('2026-11-04', 'new@example.test', 'new-verification', now=now)
        assert service.submission_check(new_claim) is None


def test_environment_conflicting_with_selected_target_is_visible_and_blocks_sending(tmp_path, monkeypatch):
    monkeypatch.delenv('HSC_PILOT_TEST_RECIPIENT', raising=False)
    app, client = _client(tmp_path)
    with client:
        service = app.state.pilot_test_telemetry
        service.configure_diagnostic_recipient('saved@example.test', 'saved@example.test')
        service.set_diagnostic_consent('saved@example.test', consented=True, require_selected=True)
        monkeypatch.setenv('HSC_PILOT_TEST_RECIPIENT', 'other@example.test')
        session = {'X-HSC-Session':'synthetic'}
        assert client.get('/v1/pilot-test-digest/config', headers=session).json()['recipient'] == ''
        token = client.post('/v1/parent/setup', headers=session, json={'pin':'2468'}).json()['parent_token']
        headers = {**session, 'X-HSC-Parent':token}
        status = client.get('/v1/parent/pilot-diagnostic-consent', headers=headers).json()
        assert status['recipient_configuration_conflict'] is True and not status['scheduler_enabled']
        assert client.put('/v1/parent/pilot-diagnostic-consent', headers=headers,
                          json={'recipient':'other@example.test','consented':True}).status_code == 409
