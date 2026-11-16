import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('continuous', Path(__file__).with_name('continuous-development.py'))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@pytest.fixture
def setup(tmp_path):
    repo, root = tmp_path / 'repo', tmp_path / 'queue'
    repo.mkdir()
    root.mkdir()
    name = 'services/local_agent/src/home_study_agent/example.py'
    source = repo / name
    source.parent.mkdir(parents=True)
    source.write_text('def example():\n    return 1\n')
    jobs = [{'id': str(i), 'brief': 'Implement and test a synthetic task.',
             'files': [{'path': name, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]} for i in range(3)]
    worker.save(root / 'queue.json', {'jobs': jobs})
    return repo, root, source, jobs


def success(directory):
    (directory / 'output' / 'candidate.py').write_text('value = 2\n')
    return 0


def test_advances_and_never_replays_completed_work(setup):
    repo, root, source, jobs = setup
    original = source.read_bytes()
    calls = []
    def run(directory):
        calls.append(directory)
        return success(directory)
    for _ in range(5):
        state = worker.run_once(root, repo, run)
    assert len(calls) == 3
    assert state['phase'] == 'idle_awaiting_review_or_new_tasks'
    assert {j['phase'] for j in state['jobs'].values()} == {'awaiting_review'}
    assert source.read_bytes() == original
    assert state['automatic_integration'] is False


def test_source_change_prevents_api_call(setup):
    repo, root, source, jobs = setup
    source.write_text('value = 3')
    state = worker.run_once(root, repo, lambda _: pytest.fail('API must not run'))
    assert {j['phase'] for j in state['jobs'].values()} == {'input_changed'}


def test_bounded_failure_and_continue_next_job(setup):
    repo, root, _, _ = setup
    for _ in range(3):
        state = worker.run_once(root, repo, lambda _: 1)
    assert state['jobs']['0']['phase'] == 'failed'
    assert state['jobs']['0']['attempts'] == 2
    assert state['jobs']['1']['attempts'] == 1


def test_crash_does_not_replay_ambiguous_paid_job(setup, monkeypatch):
    repo, root, _, jobs = setup
    state = worker.run_once(root, repo, success)
    state['jobs']['0']['phase'] = 'running'
    worker.save(root / 'state.json', state)
    monkeypatch.setattr(worker, 'alive', lambda _: False)
    state = worker.run_once(root, repo, success)
    assert state['jobs']['0']['phase'] == 'interrupted_needs_review'
    assert state['jobs']['0']['attempts'] == 1
    assert state['jobs']['1']['phase'] == 'awaiting_review'


def test_existing_worker_blocks_duplicate(setup, monkeypatch):
    repo, root, _, _ = setup
    state = worker.run_once(root, repo, success)
    state['jobs']['0']['phase'] = 'running'
    worker.save(root / 'state.json', state)
    monkeypatch.setattr(worker, 'alive', lambda _: True)
    state = worker.run_once(root, repo, lambda _: pytest.fail('Duplicate worker'))
    assert state['phase'] == 'waiting_existing_worker'


def test_credentials_halt_all_jobs_without_prompt_loop(setup):
    repo, root, _, _ = setup
    worker.run_once(root, repo, lambda _: 2)
    state = worker.run_once(root, repo, lambda _: pytest.fail('Must wait for credential recovery'))
    assert state['phase'] == 'credential_blocked'
    assert state['jobs']['0']['attempts'] == 1


@pytest.mark.parametrize('path', ['../secret.py', '/tmp/secret.py', 'services/local_agent/tests/../../secret.py'])
def test_rejects_unreviewed_paths(setup, path):
    _, _, _, jobs = setup
    jobs[0]['files'][0]['path'] = path
    with pytest.raises(ValueError):
        worker.validate_job(jobs[0])


def test_secret_and_symlink_rejected(setup):
    repo, _, source, jobs = setup
    source.write_text('token = "sk-' + 'x' * 20 + '"')
    jobs[0]['files'][0]['sha256'] = worker.digest(source.read_bytes())
    with pytest.raises(ValueError):
        worker.source_packet(repo, jobs[0])
    source.unlink()
    source.symlink_to('/etc/hosts')
    with pytest.raises(ValueError):
        worker.source_packet(repo, jobs[0])


def test_task_definition_mutation_never_replays(setup):
    repo, root, _, jobs = setup
    worker.run_once(root, repo, success)
    jobs[0]['brief'] = 'Changed task needs a new ID.'
    worker.save(root / 'queue.json', {'jobs': jobs})
    state = worker.run_once(root, repo, success)
    assert state['jobs']['0']['phase'] == 'input_changed'
    assert state['jobs']['0']['attempts'] == 1


def test_success_without_files_is_not_delivery(setup):
    repo, root, _, _ = setup
    worker.run_once(root, repo, lambda _: 0)
    state = worker.run_once(root, repo, lambda _: 0)
    assert state['jobs']['0']['phase'] == 'failed'


def test_turn_budget_does_not_repeat_paid_job(setup):
    repo, root, _, _ = setup
    worker.run_once(root, repo, lambda _: 53)
    state = worker.run_once(root, repo, success)
    assert state['jobs']['0']['attempts'] == 1
    assert state['jobs']['0']['phase'] == 'failed'
    assert state['jobs']['1']['phase'] == 'awaiting_review'


def test_prompt_names_files_without_shell_discovery(setup):
    repo, root, _, jobs = setup
    directory = root / 'explicit-paths'
    worker.prepare(repo, directory, jobs[0])
    assert 'source/' + jobs[0]['files'][0]['path'] in (directory / 'TASK.md').read_text()
