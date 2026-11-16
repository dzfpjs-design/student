"""Reject default imports and invalid scopes without touching family data."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


FIXTURE = Path(__file__).parent / 'fixtures/parent_camera_http.py'


def environment(home, inherited):
    return {'PATH': os.environ.get('PATH', ''), 'HOME': str(home),
            'USERPROFILE': str(home), 'PYTHONDONTWRITEBYTECODE': '1',
            'HSC_DATA_DIR': str(inherited), 'HSC_DATABASE_PATH': str(inherited / 'family.db')}


def test_parent_camera_rejects_root_before_any_product_import(tmp_path):
    inherited = tmp_path / 'must-not-exist'
    root = tmp_path / 'invalid'
    root.mkdir()
    result = subprocess.run([sys.executable, '-B', str(FIXTURE), '--data-root', str(root),
        '--device-hash', 'synthetic-camera'], env=environment(tmp_path, inherited),
        capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'only disposable parent camera data are allowed' in result.stderr
    assert not inherited.exists()
    assert not (tmp_path / '.home-study-companion').exists()


@pytest.mark.parametrize('escape', ['none', 'path', 'uri'])
def test_parent_camera_isolates_inherited_paths_and_blocks_later_escape(tmp_path, escape):
    inherited = tmp_path / 'inherited-must-not-exist'
    code = '''
import runpy, sys, uvicorn, sqlite3
from pathlib import Path
root = Path(sys.argv[2])
escape = sys.argv[3]
def run(server, **kwargs):
    assert (root / 'import-only/home-study.db').is_file()
    assert (root / 'isolated.db').is_file()
    if escape == 'path':
        sqlite3.connect(root.parent / 'escaped.db')
    elif escape == 'uri':
        sqlite3.connect((root.parent / 'escaped.db').as_uri() + '?mode=rwc', uri=True)
uvicorn.Server.run = run
fixture = sys.argv[1]
sys.argv = [fixture, '--data-root', str(root), '--device-hash', 'synthetic-camera']
runpy.run_path(fixture, run_name='__main__')
'''
    with tempfile.TemporaryDirectory(prefix='hsc-parent-camera-') as name:
        root = Path(name).resolve()
        result = subprocess.run([sys.executable, '-B', '-c', code, str(FIXTURE), str(root), str(escape)],
            env=environment(tmp_path, inherited), capture_output=True, text=True, timeout=30)
        if escape != 'none':
            assert result.returncode != 0
            assert 'PARENT_CAMERA_FIXTURE_PATH_ESCAPE' in result.stderr
        else:
            assert result.returncode == 0, result.stderr[-1600:]
        assert not inherited.exists()
        assert not (tmp_path / '.home-study-companion').exists()
