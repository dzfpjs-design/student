"""Local signed-fixture plumbing, never evidence of real scorer calibration."""
import base64
from datetime import datetime, timedelta, timezone
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.rubric_calibration_evidence import encoded
from test_companion_activity_host import NoCloud

CHILD = 'child-primary-4'
ROOT = '/v1/parent/children/' + CHILD + '/rubric-calibration'


def signed_fixture(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    trust = {'version': 'rubric_authority_trust_v1', 'issuers': [{
        'key_id': 'isolated-signer', 'public_key_pem': private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        'kinds': ['scorer_revision', 'holdout', 'blind_label'], 'child_ids': [CHILD],
        'expires_at': (now + timedelta(days=2)).isoformat()}]}
    path = tmp_path / 'trusted-public-keys.json'
    path.write_text(json.dumps(trust))
    monkeypatch.setenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE', str(path))
    envelope = {'version': 'rubric_authority_envelope_v1', 'key_id': 'isolated-signer',
        'kind': 'scorer_revision', 'child_id': CHILD, 'receipt_id': 'synthetic-release',
        'issued_at': now.isoformat(), 'expires_at': (now + timedelta(days=1)).isoformat(),
        'payload': {'receipt_id': 'synthetic-release', 'child_id': CHILD, 'status': 'current',
            'provider': 'fixture', 'model': 'fixture-model', 'model_revision': 'fixture-immutable',
            'verification_method': 'signed_release_manifest', 'evidence_hash': 'a'*64,
            'endpoint_scope_sha256': 'b'*64, 'verified_at': now.isoformat(),
            'expires_at': (now + timedelta(days=1)).isoformat()}}
    return envelope, base64.b64encode(private.sign(encoded(envelope).encode())).decode()


def test_real_parent_import_verifies_separate_trust_and_restart_revocation(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'isolated.db', maintenance_enabled=False)
    envelope, signature = signed_fixture(tmp_path, monkeypatch)
    request = {'envelope': envelope, 'signature': signature, 'client_action_id': 'import-release'}
    app = create_app(settings, model_gateway=NoCloud())
    with TestClient(app, client=('127.0.0.1', 41000)) as client:
        assert client.post(ROOT+'/authority/import', json=request).status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '7352'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        bad = {**request, 'signature': base64.b64encode(bytes(64)).decode()}
        rejected = client.post(ROOT+'/authority/import', json=bad)
        assert rejected.status_code == 409, rejected.text
        accepted = client.post(ROOT+'/authority/import', json=request)
        assert accepted.status_code == 200, accepted.text
        receipt = accepted.json()
        assert receipt['status'] == 'current' and not receipt['score_qualified']
        assert client.post(ROOT.replace(CHILD, 'child-junior-3')+'/authority/import', json=request).status_code == 409
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41000)) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin': '7352'}).json()['parent_token']
        assert client.post(ROOT+'/authority/import', json=request).json()['event_id'] == receipt['event_id']
        revoked = client.post(ROOT+'/authority/revoke', json={'kind': 'scorer_revision',
            'receipt_id': 'synthetic-release', 'expected_hash': receipt['envelope_hash'], 'client_action_id': 'revoke-release'})
        assert revoked.status_code == 200, revoked.text
        replay = client.post(ROOT+'/authority/import', json=request)
        assert replay.status_code == 200 and replay.json()['status'] == 'revoked'
