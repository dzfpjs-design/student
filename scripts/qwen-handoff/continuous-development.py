"""Serial, checkpointed Qwen candidate development; never a production merger."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

SECRET = re.compile(r'sk-[A-Za-z0-9_.-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
MAX_PACKET_BYTES = 240_000
MAX_ATTEMPTS = 2
TERMINAL = {'awaiting_review', 'failed', 'input_changed', 'interrupted_needs_review'}
SAFE_PREFIXES = ('services/local_agent/src/home_study_agent/', 'services/local_agent/tests/')


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def validate_job(job):
    if not re.fullmatch(r'[a-z0-9-]{1,80}', job['id']):
        raise ValueError('Invalid task identifier')
    if not isinstance(job['brief'], str) or SECRET.search(job['brief']):
        raise ValueError('Unsafe task brief')
    if job.get('provider', 'token-plan') not in {'token-plan', 'deepseek'}:
        raise ValueError('Unapproved provider')
    if not 1 <= len(job['files']) <= 16:
        raise ValueError('Invalid source packet size')
    seen = set()
    for item in job['files']:
        name = item['path']
        if (name in seen or not name.startswith(SAFE_PREFIXES) or Path(name).is_absolute()
                or '..' in Path(name).parts or not name.endswith('.py')
                or not re.fullmatch('[a-f0-9]{64}', item['sha256'])):
            raise ValueError('Unapproved source path/hash')
        seen.add(name)
    return digest(json.dumps(job, sort_keys=True, ensure_ascii=False).encode())


def source_packet(repo, job):
    payload = []
    total = len(job['brief'].encode())
    for item in job['files']:
        source = repo / item['path']
        if source.resolve() != source or not source.is_file():
            raise ValueError('Source is missing or is a symlink')
        data = source.read_bytes()
        total += len(data)
        if digest(data) != item['sha256']:
            raise ValueError('Source changed after review')
        if total > MAX_PACKET_BYTES or SECRET.search(data.decode('utf-8')):
            raise ValueError('Packet exceeds limit or contains a credential')
        payload.append((item['path'], data))
    return payload


def prepare(repo, directory, job):
    payload = source_packet(repo, job)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, data in payload:
        target = directory / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (directory / 'output').mkdir()
    rules = '''You are the implementation worker for the complete family learning companion.
Implement the assigned change, not just a plan. Read the provided code and preserve its contracts.
All files under source/ are explicitly reviewed code and synthetic tests, NOT family records.
Do not read outside this directory, browse, access credentials, execute commands, install tools,
start other agents, deploy, or restart knowledge extraction. Shell and network tools are unavailable.
Use read_file/write_file/edit. Write complete changed Python files in output/ using their original
repository-relative paths, or output/change.patch with exact unified-diff hunks for a large existing
module. Write targeted pytest tests there. Do not edit source/. Never use shell to create files.
Write output/REVIEW.md with root cause, changed behavior, tests supplied, integration points,
assumptions, and missing files needed. Tests are NOT executed here; never claim they passed.
Never weaken child/session/source ownership, idempotency, consent, rollback or durable receipts.
No fabricated model evidence, fake mastery, fixed lesson fallbacks, or silent data loss.
Do not claim the product is complete. Your code is an untrusted candidate awaiting independent review.
If code is already correct, demonstrate that using precise references and add useful regression tests.
Keep the change focused; deliver actual code and tests before writing a long explanation.

ASSIGNMENT:
'''
    sources = '\n'.join('source/' + name for name, _ in payload)
    (directory / 'TASK.md').write_text(rules + job['brief'] + '\n\nEXACT INPUT FILES (use read_file, no ls/find/shell):\n' + sources)
    save(directory / 'source-manifest.json', job)


def read_state(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def recover(record, directory):
    worker = read_state(directory / 'status.json', {})
    if alive(record.get('launcher_pid')) or alive(worker.get('pid')) or alive(worker.get('qwen_pid')):
        return False
    # A crash can occur after a billable request but before a checkpoint. Never replay it silently.
    record.update(phase='interrupted_needs_review', reason='Previous attempt stopped; inspect its candidate before retrying.')
    return True


def run_once(root, repo, runner=None):
    manifest = read_state(root / 'queue.json', {'jobs': []})
    jobs = manifest['jobs']
    fingerprints = {job['id']: validate_job(job) for job in jobs}
    if len(fingerprints) != len(jobs):
        raise ValueError('Duplicate task identifiers')
    state = read_state(root / 'state.json', {'jobs': {}, 'automatic_integration': False})
    state.update(supervisor_pid=os.getpid(), checked_at=time.time())
    if state.get('phase') == 'credential_blocked' and not (root / 'RESUME').exists():
        save(root / 'state.json', state)
        return state
    if (root / 'RESUME').exists():
        (root / 'RESUME').unlink()
    for job in jobs:
        record = state['jobs'].setdefault(job['id'], {'phase': 'queued', 'attempts': 0,
                                                    'definition_hash': fingerprints[job['id']]})
        if record['definition_hash'] != fingerprints[job['id']]:
            record.update(phase='input_changed', reason='Task definition changed; use a new reviewed task ID.')
        if record['phase'] == 'queued' and record.get('exit_code') in (2, 53, 55, 130):
            record.update(phase='failed', reason='Previous budget/credential stop is not an implicit paid retry.')
        if record['phase'] == 'running':
            if not recover(record, Path(record['directory'])):
                state['phase'] = 'waiting_existing_worker'
                save(root / 'state.json', state)
                return state
        if record['phase'] in TERMINAL:
            continue
        attempt = record['attempts'] + 1
        directory = root / 'tasks' / job['id'] / ('attempt-' + str(attempt))
        try:
            prepare(repo, directory, job)
        except (ValueError, OSError):
            record.update(phase='input_changed', reason='Approved packet unavailable, changed, or existing attempt directory.')
            continue
        record.update(phase='running', attempts=attempt, directory=str(directory), started_at=time.time())
        state['phase'] = 'working'
        save(root / 'state.json', state)
        if runner:
            code = runner(directory)
        else:
            command = [sys.executable, str(Path(__file__).with_name('start-development.py')),
                       '--work', str(directory), '--candidate-edit', '--provider', job.get('provider', 'token-plan')]
            with (directory / 'launcher.log').open('w') as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
                record['launcher_pid'] = child.pid
                save(root / 'state.json', state)
                try:
                    code = child.wait(timeout=680)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                    code = 55
        record.update(exit_code=code, ended_at=time.time())
        files = [str(p.relative_to(directory / 'output')) for p in (directory / 'output').rglob('*')
                 if p.is_file() and not p.is_symlink()]
        record['candidate_files'] = files
        if code == 0 and files:
            record['phase'] = 'awaiting_review'
        elif code in (2, 53, 130, 55):
            record.update(phase='failed', reason='Credential, cancellation or budget limit: no automatic paid retry.')
        else:
            record['phase'] = 'queued' if attempt < MAX_ATTEMPTS else 'failed'
        state['phase'] = 'working' if record['phase'] == 'queued' else 'checkpointed'
        if code == 2:
            state['phase'] = 'credential_blocked'
        save(root / 'state.json', state)
        return state
    state.update(phase='idle_awaiting_review_or_new_tasks', checked_at=time.time())
    save(root / 'state.json', state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    root, repo = args.root.resolve(strict=True), args.repo.resolve(strict=True)
    os.umask(0o077)
    lock = (root / '.supervisor.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    while not (root / 'STOP').exists():
        state = run_once(root, repo)
        print(json.dumps({'phase': state['phase'], 'jobs': {k: v['phase'] for k, v in state['jobs'].items()}}), flush=True)
        if args.once:
            break
        time.sleep(60 if state['phase'].startswith(('idle', 'waiting', 'credential')) else 2)


if __name__ == '__main__':
    main()
