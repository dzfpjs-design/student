"""One bounded, reviewed prompt in; untrusted text candidate out. No execution."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request


PROVIDERS = {
    'token-plan': ('https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                   'hsc-qwen-token-plan-api-key'),
    'deepseek': ('https://api.deepseek.com/v1', 'hsc-deepseek-api-key'),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def reviewed_prompt(path, expected):
    raw = Path(path).read_bytes()
    if not 1 <= len(raw) <= 48000:
        raise ValueError('prompt_size')
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('prompt_changed_since_review')
    text = raw.decode('utf-8')
    if re.search(r'\bsk-[A-Za-z0-9_.-]{12,}|-----BEGIN .*PRIVATE KEY-----', text):
        raise ValueError('credential_in_prompt')
    return text


def candidate_payload(model, prompt):
    return {'model': model, 'max_tokens': 4096, 'stream': False,
        'thinking': {'type': 'disabled'}, 'messages': [
            {'role': 'system', 'content': 'Produce a code or test candidate for the supplied task. '
             'You have no tools, filesystem, shell or deployment access. Do not claim tests ran. '
             'Do not request credentials. Return the candidate and its assumptions.'},
            {'role': 'user', 'content': prompt}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=PROVIDERS, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--prompt', type=Path, required=True)
    parser.add_argument('--reviewed-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # An existing directory is an explicit checkpoint, never an invitation to
    # repeat a paid request after a lost reply. Choose a new attempt explicitly.
    prompt = reviewed_prompt(args.prompt, args.reviewed_sha256)
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    status = {'provider': args.provider, 'requested_model': args.model,
        'prompt_sha256': args.reviewed_sha256, 'state': 'prepared',
        'max_output_tokens': 4096, 'max_requests': 1, 'timeout_seconds': 180,
        'candidate_executed': False}

    def checkpoint():
        path = args.output / 'status.json'
        temporary = args.output / '.status.tmp'
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)

    checkpoint()
    base, service = PROVIDERS[args.provider]
    credential_args = ['security', 'find-generic-password', '-s', service]
    if args.provider == 'token-plan':
        credential_args.extend(['-a', 'development-coordinator'])
    credential_args.append('-w')
    try:
        credential = subprocess.run(credential_args, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        status.update(state='credential_authorization_required', requests_sent=0)
        checkpoint()
        print(json.dumps(status))
        return 2
    key = credential.stdout.strip() if credential.returncode == 0 else ''
    if not key:
        status.update(state='credential_unavailable', requests_sent=0)
        checkpoint()
        print(json.dumps(status))
        return 2
    request = urllib.request.Request(base + '/chat/completions',
        data=json.dumps(candidate_payload(args.model, prompt)).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    status.update(state='requesting', requests_sent=1)
    checkpoint()
    start = time.monotonic()
    try:
        # No redirects, inherited proxies, retries, shell tools or model-owned paths.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=180) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError('response_too_large')
        result = json.loads(data)
        content = result['choices'][0]['message'].get('content')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('empty_candidate')
        content = content.replace(key, '[REDACTED]')
        (args.output / 'candidate.txt').write_text(content, encoding='utf-8')
        usage = result.get('usage') or {}
        status.update(state='needs_codex_review', actual_model=result.get('model'),
            finish_reason=result['choices'][0].get('finish_reason'),
            usage={k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)})
    except urllib.error.HTTPError as error:
        # Provider error bodies may echo inputs or headers. Never log them.
        status.update(state='http_error', http_status=error.code)
    except Exception as error:
        status.update(state='failed', error_type=type(error).__name__)
    finally:
        status['elapsed_seconds'] = round(time.monotonic() - start, 3)
        checkpoint()
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status['state'] == 'needs_codex_review' else 1


if __name__ == '__main__':
    raise SystemExit(main())
