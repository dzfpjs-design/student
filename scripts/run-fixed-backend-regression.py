"""Durable, source-fingerprinted backend regression; never a readiness certificate."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def live(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def isolated_environment(environment):
    cleaned = {key: value for key, value in environment.items()
               if not key.startswith('HSC_') and not any(part in key.upper()
                   for part in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
    cleaned.update(PYTHONPATH=str(ROOT / 'services/local_agent/src'), PYTHONDONTWRITEBYTECODE='1',
                   HSC_RUN_CLOUD_DEVICE_PLANNER='0', HSC_REAL_CONTINUATION='0')
    return cleaned


def fingerprints():
    files = {Path(__file__).resolve(), ROOT / 'services/local_agent/pyproject.toml'}
    for folder in ('services/local_agent/src', 'services/local_agent/tests'):
        files.update(path for path in (ROOT / folder).rglob('*') if path.is_file()
                     and not any(part in {'__pycache__', '.pytest_cache'} for part in path.parts)
                     and path.suffix != '.pyc')
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files)}


def fixture_root(output):
    # Durable logs can live in the repository; subprocess witnesses deliberately
    # accept synthetic databases only inside the OS temporary directory.
    identity = hashlib.sha256(str(output.resolve()).encode('utf8')).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / ('pytest-hsc-backend-fixed-' + identity)


def pytest_command(output, workers, *, xdist_available=None):
    if xdist_available is None:
        xdist_available = importlib.util.find_spec('xdist') is not None
    command = [sys.executable, '-m', 'pytest', 'services/local_agent/tests', '-q']
    if xdist_available:
        command.extend(('-n', str(workers), '--dist', 'loadfile'))
    command.extend(('--basetemp=' + str(fixture_root(output)),
                    '--junitxml=' + str(output / 'pytest.xml')))
    return command, bool(xdist_available)


def run(output, workers):
    command, xdist_available = pytest_command(output, workers)
    state = dict(status='running', runner_pid=os.getpid(), started_at=stamp(),
                 python=sys.version.split()[0], python_executable=sys.executable,
                 python_prefix=sys.prefix, python_base_prefix=sys.base_prefix,
                 virtual_environment=sys.prefix if sys.prefix != sys.base_prefix else None,
                 platform=platform.platform(),
                 requested_workers=workers,
                 effective_workers=workers if xdist_available else 1, xdist_available=xdist_available,
                 source_stable=None, paid_probes_enabled=False, fixture_root=str(fixture_root(output)))
    save(output / 'status.json', state)
    try:
        before = fingerprints()
        save(output / 'sources-before.json', before)
        with (output / 'pytest.log').open('wb') as log:
            child = subprocess.Popen(command, cwd=ROOT, env=isolated_environment(os.environ), stdout=log, stderr=subprocess.STDOUT)
            state['pytest_pid'] = child.pid
            save(output / 'status.json', state)
            exit_code = child.wait()
        after = fingerprints()
        save(output / 'sources-after.json', after)
        changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
        counts = None
        if (output / 'pytest.xml').is_file():
            suites = list(ET.parse(output / 'pytest.xml').getroot().iter('testsuite'))
            counts = {key: sum(int(suite.get(key, '0')) for suite in suites)
                      for key in ('tests', 'failures', 'errors', 'skipped')}
        state.update(status='source_changed' if changed else 'passed' if exit_code == 0 else 'failed',
                     exit_code=exit_code, source_stable=not changed, changed_files=changed,
                     file_count=len(before), counts=counts, finished_at=stamp())
    except Exception as error:
        state.update(status='runner_failed', error_type=type(error).__name__, finished_at=stamp())
        save(output / 'status.json', state)
        raise
    save(output / 'status.json', state)
    return 0 if state['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 17))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--launch', action='store_true')
    mode.add_argument('--status', action='store_true')
    mode.add_argument('--run', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if args.status:
        value = json.loads((output / 'status.json').read_text(encoding='utf-8'))
        value.update(runner_alive=live(value.get('runner_pid')), pytest_alive=live(value.get('pytest_pid')))
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if args.launch:
        output.mkdir(parents=True, exist_ok=False)
        with (output / 'runner.log').open('wb') as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--run',
                '--output', str(output), '--workers', str(args.workers)], cwd=ROOT,
                env=isolated_environment(os.environ), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        print(json.dumps({'runner_pid': child.pid, 'output': str(output), 'status': 'launched'}))
        return 0
    return run(output, args.workers)


if __name__ == '__main__':
    raise SystemExit(main())
