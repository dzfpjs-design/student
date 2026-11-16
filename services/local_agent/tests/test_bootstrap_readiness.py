from pathlib import Path
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings


def test_public_bootstrap_readiness_is_only_permission_booleans_and_parent_write_stays_protected(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'public.db',
                        data_encryption_key=b'p' * 32, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        before = client.get('/v1/bootstrap').json()
        assert before['cloud_permissions'] == {'cloud_model_text_allowed': True, 'cloud_audio_allowed': False}
        assert set(before['cloud_permissions']) == {'cloud_model_text_allowed', 'cloud_audio_allowed'}
        assert all(type(value) is bool for value in before['cloud_permissions'].values())
        assert client.get('/v1/parent/privacy').status_code in (401, 403)
        assert client.put('/v1/parent/privacy', json={'cloud_audio_allowed': True}).status_code in (401, 403)
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        assert client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token},
                          json={'cloud_audio_allowed': True}).status_code == 200
        assert client.get('/v1/bootstrap').json()['cloud_permissions']['cloud_audio_allowed'] is True
    # A fresh app/service instance must read the durable preference, not an
    # installer flag, browser memory or a presumed bundled credential consent.
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get('/v1/bootstrap').json()['cloud_permissions'] == {
            'cloud_model_text_allowed': True, 'cloud_audio_allowed': True}
        assert restarted.put('/v1/parent/privacy', json={'cloud_audio_allowed': False}).status_code in (401, 403)
