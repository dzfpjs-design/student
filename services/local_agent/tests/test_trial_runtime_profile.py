import json
import pytest
from home_study_agent.config import Settings


def test_trial_backend_requires_durable_identity_and_isolates_database(tmp_path, monkeypatch):
    trial = tmp_path / 'trial'
    trial.mkdir()
    data = trial / 'family-data'
    data.mkdir()
    monkeypatch.setenv('HSC_RUNTIME_MODE', 'trial')
    monkeypatch.setenv('HSC_TRIAL_PROFILE_ID', 'isolated-1')
    monkeypatch.setenv('HSC_TRIAL_USER_DATA_DIR', str(trial))
    monkeypatch.setenv('HSC_DATA_DIR', str(data))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(data / 'home-study.db'))
    with pytest.raises(ValueError, match='MARKER_INVALID'):
        Settings.from_environment()
    (trial / 'trial-profile.json').write_text(json.dumps({'schema': 1, 'mode': 'trial', 'profileId': 'isolated-1'}))
    assert Settings.from_environment().data_dir == data
    assert Settings.from_environment().runtime_mode == 'trial'
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path / 'formal.db'))
    with pytest.raises(ValueError, match='PATH_ESCAPE'):
        Settings.from_environment()
    monkeypatch.setenv('HSC_DATABASE_PATH', str(data / 'home-study.db'))
    monkeypatch.setenv('HSC_TRIAL_PROFILE_ID', 'other')
    with pytest.raises(ValueError, match='ID_CONFLICT'):
        Settings.from_environment()


def test_trial_backend_rejects_symlink_to_formal_storage(tmp_path, monkeypatch):
    trial = tmp_path / 'trial'; trial.mkdir()
    formal = tmp_path / 'formal'; formal.mkdir()
    (trial / 'family-data').symlink_to(formal, target_is_directory=True)
    monkeypatch.setenv('HSC_RUNTIME_MODE', 'trial')
    monkeypatch.setenv('HSC_TRIAL_PROFILE_ID', 'isolated')
    monkeypatch.setenv('HSC_TRIAL_USER_DATA_DIR', str(trial))
    monkeypatch.setenv('HSC_DATA_DIR', str(trial / 'family-data'))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(trial / 'family-data' / 'study.db'))
    with pytest.raises(ValueError, match='PATH_ESCAPE'):
        Settings.from_environment()
