"""Build identity is owned by the backend binary, never caller configuration."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.build_info import BUILD_COMPONENT, BUILD_VERSION
from home_study_agent.config import Settings


def test_backend_source_build_version_matches_desktop_metadata_and_cli_isolated(tmp_path):
    root = Path(__file__).resolve().parents[3]
    metadata = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    lock = json.loads((root / 'package-lock.json').read_text(encoding='utf-8'))
    project = (root / 'services/local_agent/pyproject.toml').read_text(encoding='utf-8')
    assert metadata['version'] == lock['version'] == lock['packages']['']['version'] == BUILD_VERSION
    assert re.search(r'^version\s*=\s*["\']' + re.escape(BUILD_VERSION) + r'["\']\s*$', project, re.M)
    env = {key: value for key, value in os.environ.items() if not key.startswith('HSC_')}
    env['HSC_DATA_DIR'] = str(tmp_path / 'must-not-be-created')
    env['HSC_BUILD_VERSION'] = 'old-backend-cannot-be-overridden'
    result = subprocess.run([sys.executable, '-m', 'home_study_agent', '--build-info'],
                            env=env, capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(result.stdout) == {'component': BUILD_COMPONENT, 'version': BUILD_VERSION}
    assert not (tmp_path / 'must-not-be-created').exists()


def test_normal_health_publishes_compiled_identity(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'synthetic.db',
                        maintenance_enabled=False, model_api_key=None, fallback_model_api_key=None)
    with TestClient(create_app(settings)) as client:
        normal = client.get('/v1/health').json()
        assert normal['status'] == 'ok'
        assert (normal['version'], normal['build_version'], normal['build_component']) == (BUILD_VERSION, BUILD_VERSION, BUILD_COMPONENT)
