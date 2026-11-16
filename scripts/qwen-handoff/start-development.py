"""Start a bounded Qwen CLI task after the user's Keychain authorization."""
import argparse
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

PROVIDERS = {
    'token-plan': ('https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                   'qwen3.7-plus', 'hsc-qwen-token-plan-api-key', 'development-coordinator'),
    'deepseek': ('https://api.deepseek.com/v1', 'deepseek-v4-pro', 'hsc-deepseek-api-key', None),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--key-stdin', action='store_true', help='Use non-echoed, process-only credential input.')
    parser.add_argument('--candidate-edit', action='store_true', help='Allow edits only inside the sandboxed candidate directory.')
    parser.add_argument('--provider', choices=PROVIDERS, default='token-plan')
    args = parser.parse_args()
    work = args.work.resolve(strict=True)
    lock = (work / '.worker.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (work / 'status.json').exists():
        raise ValueError('Task already has a checkpoint; use a new reviewed task directory, not an implicit paid retry.')
    prompt = (work / 'TASK.md').read_text()
    base_url, model, key_service, key_account = PROVIDERS[args.provider]
    home = Path.home()
    node = (home / '.local/opt/node/bin/node').resolve(strict=True)
    qwen = (home / '.local/share/hsc-qwen-code').resolve(strict=True)
    for name in ('home', 'tmp'):
        (work / name).mkdir(mode=0o700, exist_ok=True)
    settings_dir = work / 'home' / '.qwen'
    settings_dir.mkdir(mode=0o700, exist_ok=True)
    (settings_dir / 'settings.json').write_text(json.dumps({
        'model': {'generationConfig': {'samplingParams': {'max_tokens': 8192}}},
    }))
    status = {'phase': 'awaiting_process_credential' if args.key_stdin else 'awaiting_keychain_authorization', 'pid': os.getpid(),
              'provider': args.provider, 'model': model, 'task_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
              'started_at': time.time(), 'max_turns': 6, 'max_tool_calls': 12,
              'max_wall_seconds': 300, 'max_output_tokens_per_request': 8192,
              'automatic_integration': False}

    def save():
        temporary = work / '.status.tmp'
        temporary.write_text(json.dumps(status, indent=2))
        os.replace(temporary, work / 'status.json')

    save()
    if args.key_stdin:
        key = getpass.getpass('Process-only API credential (hidden): ').strip()
    else:
        print('Waiting for macOS Keychain authorization. No key will be printed.', flush=True)
        credential_args = ['/usr/bin/security', 'find-generic-password', '-s', key_service]
        if key_account:
            credential_args.extend(['-a', key_account])
        credential_args.append('-w')
        try:
            result = subprocess.run(credential_args, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            status['phase'] = 'authorization_timeout'
            save()
            return 2
        if result.returncode or not result.stdout.strip():
            status.update(phase='authorization_failed', keychain_exit=result.returncode)
            save()
            return 2
        key = result.stdout.decode().strip()
    if not key.startswith('sk-'):
        status['phase'] = 'invalid_credential_input'
        save()
        return 2
    env = {'HOME': str(work / 'home'), 'TMPDIR': str(work / 'tmp'),
           'PATH': str(node.parent) + ':/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
           'OPENAI_API_KEY': key,
           'OPENAI_BASE_URL': base_url, 'OPENAI_MODEL': model, 'NO_COLOR': '1'}
    command = ['/usr/bin/sandbox-exec', '-D', 'NODE_ROOT=' + str(node.parent.parent),
        '-D', 'QWEN_ROOT=' + str(qwen), '-D', 'WORK_ROOT=' + str(work),
        '-D', 'BASE_PROFILE=' + str(Path(__file__).with_name('worker.sb').resolve()),
        '-f', str(Path(__file__).with_name('development.sb').resolve()), str(node),
        str(qwen / 'node_modules/@qwen-code/qwen-code/cli.js'),
        # Qwen 0.23.4 bare mode replaces core-tools with a list containing shell
        # and omitting write_file. The OS sandbox and isolated HOME supply isolation.
        '--bare=false', '--auth-type', 'openai', '--model', model,
        '--telemetry=false', '--openai-logging=false', '--chat-recording=false',
        '--approval-mode', 'auto-edit' if args.candidate_edit else 'default', '--core-tools', 'read_file', 'write_file', 'edit',
        '--exclude-tools', 'run_shell_command', 'web_fetch', 'web_search', 'task',
        '--max-session-turns', '6', '--max-tool-calls', '12', '--max-wall-time', '5m',
        '--prompt', prompt]
    status['phase'] = 'starting_qwen'
    save()
    print('Credential loaded; starting Qwen Code in the isolated task directory.', flush=True)
    child = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    status.update(phase='qwen_running', qwen_pid=child.pid)
    save()
    try:
        output, _ = child.communicate(timeout=330)
        code = child.returncode
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        child.terminate()
        try:
            output, _ = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            output, _ = child.communicate()
        code = 130 if isinstance(error, KeyboardInterrupt) else 55
    (work / 'candidate-output.txt').write_text(output.replace(key, '[REDACTED]'))
    status.update(phase='finished' if code == 0 else 'failed', exit_code=code)
    save()
    print('Qwen task ended; output is preserved for review. Exit code: ' + str(code), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
