import importlib.util
import json
import os
from pathlib import Path


spec = importlib.util.spec_from_file_location('fixed_regression', Path(__file__).with_name('run-fixed-backend-regression.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_runner_does_not_inherit_live_probe_keys_or_family_locations():
    actual = runner.isolated_environment(dict(PATH='/bin', HSC_DATA_DIR='/family', HSC_REAL_CONTINUATION='1',
        HSC_RUN_CLOUD_DEVICE_PLANNER='1', DEEPSEEK_API_KEY='secret', SOME_TOKEN='secret', OTHER_PASSWORD='secret'))
    assert actual['PATH'] == '/bin'
    assert actual['HSC_REAL_CONTINUATION'] == '0' and actual['HSC_RUN_CLOUD_DEVICE_PLANNER'] == '0'
    assert 'HSC_DATA_DIR' not in actual
    assert not {'DEEPSEEK_API_KEY','SOME_TOKEN','OTHER_PASSWORD'} & actual.keys()
    assert 'secret' not in json.dumps(actual)


def test_atomic_status_and_real_pid_probe(tmp_path):
    path = tmp_path / 'status.json'
    runner.save(path, {'status': 'running'})
    runner.save(path, {'status': 'failed', 'exit_code': 1})
    assert json.loads(path.read_text()) == {'status': 'failed', 'exit_code': 1}
    assert not path.with_suffix('.tmp').exists()
    assert runner.live(os.getpid()) and not runner.live(None) and not runner.live(-1)


def test_fingerprint_includes_code_tests_config_and_runner_but_not_bytecode():
    values = runner.fingerprints()
    assert 'services/local_agent/src/home_study_agent/app.py' in values
    assert 'services/local_agent/tests/conftest.py' in values
    assert 'services/local_agent/pyproject.toml' in values
    assert 'scripts/run-fixed-backend-regression.py' in values
    assert all(len(value) == 64 for value in values.values())
    assert not any('__pycache__' in path or path.endswith('.pyc') for path in values)


def test_pytest_command_uses_parallel_flags_only_when_xdist_is_available(tmp_path):
    serial, serial_xdist = runner.pytest_command(tmp_path, 4, xdist_available=False)
    parallel, parallel_xdist = runner.pytest_command(tmp_path, 4, xdist_available=True)
    assert serial_xdist is False
    assert '-n' not in serial and '--dist' not in serial
    assert parallel_xdist is True
    assert parallel[parallel.index('-n') + 1] == '4'
    assert parallel[parallel.index('--dist') + 1] == 'loadfile'


def test_full_run_keeps_logs_durable_and_fixtures_in_owned_system_temp(tmp_path, monkeypatch):
    commands = []
    class Process:
        pid = 123
        def __init__(self, command, **kwargs):
            commands.append(command)
        def wait(self):
            return 0
    monkeypatch.setattr(runner.subprocess, 'Popen', Process)
    monkeypatch.setattr(runner.platform, 'platform', lambda: 'isolated-test')
    monkeypatch.setattr(runner.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(runner, 'fingerprints', lambda: {'fixture.py': 'a' * 64})
    assert runner.run(tmp_path, 4) == 0
    fixture = runner.fixture_root(tmp_path)
    assert fixture.parent == Path(runner.tempfile.gettempdir())
    assert fixture.name.startswith('pytest-hsc-backend-fixed-')
    assert fixture != runner.fixture_root(tmp_path / 'another-run')
    assert '--basetemp=' + str(fixture) in commands[0]
    status = json.loads((tmp_path / 'status.json').read_text())
    assert status['fixture_root'] == str(fixture)
    assert status['requested_workers'] == 4 and status['effective_workers'] == 1
    assert status['xdist_available'] is False
    assert status['python_executable'] == runner.sys.executable
    assert status['python_prefix'] == runner.sys.prefix
    assert status['python_base_prefix'] == runner.sys.base_prefix
    assert status['virtual_environment'] == (
        runner.sys.prefix if runner.sys.prefix != runner.sys.base_prefix else None
    )
