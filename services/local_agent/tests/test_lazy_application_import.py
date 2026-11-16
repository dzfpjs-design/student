import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.mark.parametrize('inherited', [False, True])
@pytest.mark.parametrize('help_only', [False, True])
def test_plain_import_and_introspection_cannot_open_family_stores(tmp_path, inherited, help_only):
    home = tmp_path / 'fake-home'
    home.mkdir()
    inherited_root = tmp_path / 'inherited-family'
    forbidden = [home / '.home-study-companion', inherited_root]
    probe = '''
import inspect, json, os, pathlib, sys
forbidden = [pathlib.Path(p).resolve() for p in json.loads(os.environ['FORBIDDEN'])]
def audit(event, args):
    if event not in {'open', 'os.mkdir', 'sqlite3.connect'}: return
    value = args[0]
    if not isinstance(value, (str, bytes, os.PathLike)): return
    path = pathlib.Path(os.fsdecode(value)).resolve()
    if any(path == root or root in path.parents for root in forbidden):
        raise AssertionError('import tried to access a family store')
sys.addaudithook(audit)
from home_study_agent.app import app, create_app
assert callable(app) and callable(create_app)
assert inspect.unwrap(app) is app
assert set(inspect.signature(app).parameters) == {'scope', 'receive', 'send'}
assert not hasattr(app, '__wrapped__')
assert all(not p.exists() for p in forbidden)
'''
    if help_only:
        probe += "\nimport runpy\nsys.argv = ['home_study_agent', '--help']\nrunpy.run_module('home_study_agent', run_name='__main__')\n"
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'USERPROFILE': str(home),
           'PYTHONPATH': str(Path(__file__).parents[1] / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
           'FORBIDDEN': json.dumps([str(p) for p in forbidden])}
    if inherited:
        env.update(HSC_DATA_DIR=str(inherited_root), HSC_DATABASE_PATH=str(inherited_root / 'home-study.db'))
    result = subprocess.run([sys.executable, '-B', '-c', probe], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-2500:]
    assert not any(path.exists() for path in forbidden)


def test_explicit_state_constructs_only_one_application_across_threads():
    from home_study_agent.lazy_application import LazyApplication
    calls = []
    class Application:
        state = object()
    application = Application()
    def factory():
        calls.append(True)
        return application
    proxy = LazyApplication(factory)
    assert inspect.unwrap(proxy) is proxy and not calls
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda _: proxy.state, range(20)))
    assert len(calls) == 1 and all(state is application.state for state in states)


def test_failed_initialization_is_not_repeated_inside_the_same_process():
    from home_study_agent.lazy_application import LazyApplication
    calls = []
    def factory():
        calls.append(True)
        raise ValueError('synthetic initialization failure')
    proxy = LazyApplication(factory)
    with pytest.raises(ValueError):
        proxy.state
    with pytest.raises(RuntimeError, match='initialization failed'):
        proxy.state
    assert len(calls) == 1


def test_actual_lifespan_routes_and_explicit_state_use_the_same_application(tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.lazy_application import LazyApplication
    from test_mistake_voice_journey import settings
    config = settings(tmp_path)
    calls = []
    def factory():
        calls.append(True)
        return create_app(config)
    proxy = LazyApplication(factory)
    assert not calls
    with TestClient(proxy) as client:
        response = client.get('/openapi.json')
        assert response.status_code == 200
        assert proxy.state.settings is config
        assert len(calls) == 1
    assert len(calls) == 1


def test_official_service_entry_starts_and_gracefully_shuts_down(tmp_path):
    import httpx
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    root, home = tmp_path / 'service', tmp_path / 'home'
    home.mkdir()
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'USERPROFILE': str(home),
           'PYTHONPATH': str(Path(__file__).parents[1] / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
           'HSC_DATA_DIR': str(root), 'HSC_DATABASE_PATH': str(root / 'home-study.db'),
           'HSC_SESSION_TOKEN': 'synthetic-local-startup-token', 'HSC_MODEL_PROVIDER': 'disabled',
           'HSC_CLOUD_ASR_PROVIDER': 'disabled', 'HSC_TTS_PROVIDER': 'disabled', 'HSC_MAINTENANCE_ENABLED': '0'}
    with (tmp_path / 'service.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-B', '-m', 'home_study_agent', '--host', '127.0.0.1',
                                    '--port', str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False,
                              headers={'X-HSC-Session': env['HSC_SESSION_TOKEN']}) as client:
                deadline = time.monotonic() + 25
                last_status = 'no local response'
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        response = client.get('/v1/health')
                        last_status = f'{response.status_code}: {response.text[:200]}'
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.05)
                else:
                    log.seek(0)
                    pytest.fail('service did not become healthy: ' + last_status + '; ' + log.read()[-2000:])
                assert response.json()['status'] == 'ok'
                stopped = client.post('/v1/runtime/shutdown')
                assert stopped.status_code == 202, stopped.text
            assert process.wait(timeout=10) == 0
            assert (root / 'home-study.db').is_file()
            assert not (home / '.home-study-companion').exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
