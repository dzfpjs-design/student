"""Real local lifecycle routes, isolated signed metadata and no model calls."""
from datetime import timedelta
import json
from pathlib import Path

import pytest
from privacy_restore_operation import restore_operation
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.rubric_calibration_authority import resolve
from test_calibration_evaluator_host import signer, ROOT, CHILD
from test_companion_activity_host import NoCloud

OTHER = 'child-junior-3'


def release(child, now):
    return {'child_id': child, 'receipt_id': 'release', 'status': 'current',
        'provider': 'isolated', 'model': 'isolated-model', 'model_revision': 'isolated-fixed-revision',
        'verification_method': 'signed_release_manifest', 'evidence_hash': 'a' * 64,
        'endpoint_scope_sha256': 'b' * 64, 'verified_at': now.isoformat(),
        'expires_at': (now + timedelta(days=1)).isoformat()}


def local_app(tmp_path, *, native_launch_attestation_key=None):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'isolated.db', maintenance_enabled=False,
                        native_launch_attestation_key=native_launch_attestation_key)
    return create_app(settings, model_gateway=NoCloud())


def authenticate(client, *, setup=False):
    result = client.post('/v1/parent/' + ('setup' if setup else 'unlock'), json={'pin': '8371'})
    assert result.status_code == 200, result.text
    client.headers['X-HSC-Parent'] = result.json()['parent_token']


@pytest.mark.parametrize('scope', ['learning', 'knowledge', 'media', 'all_history'])
def test_signed_authority_export_real_delete_and_other_child_survive_restart(tmp_path, monkeypatch, scope):
    sign, now = signer(tmp_path, monkeypatch, children=[CHILD, OTHER])
    with TestClient(local_app(tmp_path), client=('127.0.0.1', 42000)) as client:
        authenticate(client, setup=True)
        for child in (CHILD, OTHER):
            accepted = client.post(ROOT.replace(CHILD, child) + '/authority/import', json=sign('scorer_revision', release(child, now)))
            assert accepted.status_code == 200, accepted.text
        exported = client.post(f'/v1/parent/children/{CHILD}/export')
        assert exported.status_code == 200, exported.text
        data = json.loads(Path(exported.json()['path']).read_text())
        assert len(data['rubric_calibration_authorities']) == 1
        assert data['rubric_calibration_authorities'][0]['child_id'] == CHILD
        deleted = client.post(f'/v1/parent/children/{CHILD}/delete', json={
            'scope': scope, 'confirmation': '确认删除', 'client_action_id': 'erase-authority'})
        assert deleted.status_code == 200 and deleted.json()['status'] == 'completed', deleted.text
    app = local_app(tmp_path)
    with TestClient(app, client=('127.0.0.1', 42000)) as client:
        authenticate(client)
        from home_study_agent.database import Database
        with Database(tmp_path / 'isolated.db').connect() as c:
            assert resolve(c, child_id=CHILD, kind='scorer_revision', reference_id='release') is None
            assert resolve(c, child_id=OTHER, kind='scorer_revision', reference_id='release') is not None


def test_online_restore_quarantines_original_import_and_same_key_cannot_reauthorize(tmp_path, monkeypatch):
    from synthetic_native_restore_request import signed_restore_post, KEY

    sign, now = signer(tmp_path, monkeypatch)
    request = sign('scorer_revision', release(CHILD, now))
    with TestClient(local_app(tmp_path, native_launch_attestation_key=KEY), client=('127.0.0.1', 42000)) as client:
        authenticate(client, setup=True)
        original = client.post(ROOT + '/authority/import', json=request)
        assert original.status_code == 200, original.text
        saved = client.post('/v1/parent/backups')
        assert saved.status_code == 200, saved.text
        revoked = client.post(ROOT + '/authority/revoke', json={
            'kind': 'scorer_revision', 'receipt_id': 'release', 'expected_hash': original.json()['envelope_hash'],
            'client_action_id': 'revoke-after-backup'})
        assert revoked.status_code == 200, revoked.text
        restored = signed_restore_post(client, headers={'X-HSC-Parent': client.headers['X-HSC-Parent']}, json={
            'backup_id': saved.json()['backup_id'], 'confirmation': '确认恢复',
            'operation': restore_operation(client)})
        assert restored.status_code == 200, restored.text
        authenticate(client)
        replay = client.post(ROOT + '/authority/import', json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()['event_id'] == original.json()['event_id']
        assert replay.json()['status'] == 'restore_quarantined'


def test_even_signed_payload_cannot_smuggle_original_body_or_unknown_fields(tmp_path, monkeypatch):
    sign, now = signer(tmp_path, monkeypatch)
    with TestClient(local_app(tmp_path), client=('127.0.0.1', 42000)) as client:
        authenticate(client, setup=True)
        rejected = client.post(ROOT + '/authority/import', json=sign('scorer_revision',
            {**release(CHILD, now), 'original_answer': 'PRIVATE ORIGINAL MUST NOT ENTER AUTHORITY JOURNAL'}))
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()['detail']['code'] == 'RUBRIC_CALIBRATION_AUTHORITY_PAYLOAD_FIELDS'
        from home_study_agent.database import Database
        with Database(tmp_path / 'isolated.db').connect() as c:
            assert not c.execute("SELECT 1 FROM audit_event WHERE action='rubric_authority_import_v1'").fetchone()
