import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('launcher', Path(__file__).with_name('start-development.py'))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home, work = tmp_path / 'user', tmp_path / 'work'
    node = home / '.local/opt/node/bin/node'
    node.parent.mkdir(parents=True)
    node.touch()
    (home / '.local/share/hsc-qwen-code').mkdir(parents=True)
    work.mkdir()
    (work / 'TASK.md').write_text('Synthetic code-only task')
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr('sys.argv', ['start-development.py', '--work', str(work), '--candidate-edit'])
    return work


def test_client_is_bounded_and_does_not_inherit_host_environment(setup, monkeypatch):
    key = 'sk-' + 'synthetic' * 4
    monkeypatch.setenv('PRIVATE_FAMILY_SETTING', 'not-for-worker')
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout=key.encode()))
    def launch(command, **kwargs):
        assert '--bare=false' in command and '--bare' not in command
        assert command[command.index('--approval-mode') + 1] == 'auto-edit'
        assert command[command.index('--core-tools') + 1:command.index('--exclude-tools')] == ['read_file', 'write_file', 'edit']
        assert 'run_shell_command' in command[command.index('--exclude-tools') + 1:]
        assert key not in ' '.join(command)
        assert 'PRIVATE_FAMILY_SETTING' not in kwargs['env']
        assert kwargs['env']['OPENAI_API_KEY'] == key
        assert kwargs['stdin'] == launcher.subprocess.DEVNULL
        return SimpleNamespace(pid=12345, returncode=0,
                               communicate=lambda timeout: ('candidate echoed ' + key, None))
    monkeypatch.setattr(launcher.subprocess, 'Popen', launch)
    assert launcher.main() == 0
    assert key not in (setup / 'candidate-output.txt').read_text()
    assert '[REDACTED]' in (setup / 'candidate-output.txt').read_text()
    state = json.loads((setup / 'status.json').read_text())
    assert state['phase'] == 'finished' and state['automatic_integration'] is False
    settings = json.loads((setup / 'home/.qwen/settings.json').read_text())
    assert settings['model']['generationConfig']['samplingParams']['max_tokens'] == 8192
    with pytest.raises(ValueError, match='checkpoint'):
        launcher.main()


def test_keychain_failure_cannot_launch_client(setup, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=44, stdout=b''))
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('No credential'))
    assert launcher.main() == 2
    assert json.loads((setup / 'status.json').read_text())['phase'] == 'authorization_failed'


def test_explicit_fallback_uses_official_key_and_endpoint_only(setup, monkeypatch):
    monkeypatch.setattr('sys.argv', ['start-development.py', '--work', str(setup), '--provider', 'deepseek'])
    def credential(args, **kwargs):
        assert args == ['/usr/bin/security', 'find-generic-password', '-s', 'hsc-deepseek-api-key', '-w']
        return SimpleNamespace(returncode=0, stdout=b'sk-synthetic-160d79910dad6792f7f7')
    monkeypatch.setattr(launcher.subprocess, 'run', credential)
    def launch(command, **kwargs):
        assert kwargs['env']['OPENAI_BASE_URL'] == 'https://api.deepseek.com/v1'
        assert kwargs['env']['OPENAI_MODEL'] == 'deepseek-v4-pro'
        assert command[command.index('--model') + 1] == 'deepseek-v4-pro'
        return SimpleNamespace(pid=12345, returncode=0, communicate=lambda timeout: ('candidate', None))
    monkeypatch.setattr(launcher.subprocess, 'Popen', launch)
    assert launcher.main() == 0
    assert json.loads((setup / 'status.json').read_text())['provider'] == 'deepseek'
