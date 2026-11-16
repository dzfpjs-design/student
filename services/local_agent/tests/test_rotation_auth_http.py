import pytest
import shutil
from fastapi.testclient import TestClient

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.parent_auth import ParentAuth
from home_study_agent.rotation_auth_http import create_rotation_auth_app


def test_only_parent_auth_routes_are_available_and_pin_lockout_persists(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'family.db',
                        data_encryption_key=b'p' * 32, maintenance_enabled=False,
                        session_token='synthetic-local-session',
                        native_launch_attestation_key='synthetic-native-attestation')
    database = Database(settings.database_path, media_root=settings.data_dir,
                        data_encryption_key=settings.data_encryption_key,
                        runtime_mode=settings.runtime_mode)
    database.initialize()
    ParentAuth(database).setup('2345')
    app = create_rotation_auth_app(settings)
    with TestClient(app) as client:
        headers = {'X-HSC-Session': settings.session_token}
        assert client.get('/v1/health').status_code == 401
        health = client.get('/v1/health', headers=headers).json()
        assert health['status'] == 'rotation_auth_only' and health['normal_business_ready'] is False
        assert client.post('/v1/parent/rotation-authority', headers=headers).status_code == 401
        assert client.get('/v1/bootstrap', headers=headers).status_code == 404
        assert client.post('/v1/sessions', headers=headers).status_code == 404
        assert client.post('/v1/parent/unlock', headers=headers, json={'pin': '0000'}).status_code == 401
        token = client.post('/v1/parent/unlock', headers=headers, json={'pin': '2345'}).json()['parent_token']
        assert client.post('/v1/parent/rotation-authority', headers={**headers, 'X-HSC-Parent': token}).json() == {'authorized': True}
        for _ in range(5):
            response = client.post('/v1/parent/unlock', headers=headers, json={'pin': '0000'})
        assert response.status_code == 429
    with TestClient(create_rotation_auth_app(settings)) as restarted:
        assert restarted.get('/v1/parent/status', headers=headers).json()['locked_seconds'] > 0
        assert restarted.post('/v1/parent/rotation-authority', headers={**headers, 'X-HSC-Parent': token}).status_code == 401


def test_missing_owned_session_refuses_start(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'family.db', maintenance_enabled=False)
    with pytest.raises(RuntimeError, match='ROTATION_AUTH_SESSION_REQUIRED'):
        create_rotation_auth_app(settings)


def test_never_creates_or_initializes_family_database(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'missing.db', maintenance_enabled=False,
                        session_token='synthetic-local-session', native_launch_attestation_key='synthetic-native-attestation')
    monkeypatch.setattr(Database, 'initialize', lambda *args, **kwargs: pytest.fail('initialization must not run'))
    with pytest.raises(Exception):
        create_rotation_auth_app(settings)
    assert not settings.database_path.exists()


def test_replaced_database_is_rejected_before_parent_pin_use(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'family.db',
                        data_encryption_key=b'p' * 32, maintenance_enabled=False,
                        session_token='synthetic-local-session', native_launch_attestation_key='synthetic-native-attestation')
    database = Database(settings.database_path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key, runtime_mode=settings.runtime_mode)
    database.initialize();ParentAuth(database).setup('2345')
    replacement=tmp_path / 'replacement.db';shutil.copyfile(settings.database_path,replacement)
    import home_study_agent.rotation_auth_http as recovery
    real_connect=recovery.sqlite3.connect
    def swap(*args,**kwargs):
        settings.database_path.rename(tmp_path / 'original.db')
        replacement.rename(settings.database_path)
        return real_connect(*args,**kwargs)
    monkeypatch.setattr(recovery.sqlite3,'connect',swap)
    with pytest.raises(Exception):
        create_rotation_auth_app(settings)
