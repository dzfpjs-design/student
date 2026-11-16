import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

spec = importlib.util.spec_from_file_location('request_candidate', Path(__file__).with_name('request-candidate.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('raw,error', [(b'', 'prompt_size'),
    (b'x' * 48001, 'prompt_size'), (b'sk-synthetic-59c66afbe69fd409276c', 'credential_in_prompt')])
def test_rejects_unreviewable_input(tmp_path, raw, error):
    path = tmp_path / 'prompt.txt'
    path.write_bytes(raw)
    with pytest.raises(ValueError, match=error):
        module.reviewed_prompt(path, hashlib.sha256(raw).hexdigest())


def test_input_hash_prevents_post_review_change(tmp_path):
    path = tmp_path / 'prompt.txt'
    path.write_text('synthetic arithmetic')
    with pytest.raises(ValueError, match='prompt_changed'):
        module.reviewed_prompt(path, '0' * 64)


def test_payload_has_no_tools_or_stream_and_redirects_are_rejected():
    payload = module.candidate_payload('synthetic-model', 'synthetic arithmetic')
    assert payload['max_tokens'] == 4096
    assert payload['stream'] is False
    assert 'tools' not in payload
    assert module.NoRedirect().redirect_request(None, None, None, None, None, None) is None


@pytest.mark.parametrize('available', [False, True])
def test_checkpoint_rejects_duplicate_and_never_executes_candidate(tmp_path, monkeypatch, available):
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('synthetic arithmetic')
    output = tmp_path / 'attempt'
    monkeypatch.setattr(sys, 'argv', ['request-candidate', '--provider', 'deepseek',
        '--model', 'synthetic-model', '--prompt', str(prompt), '--output', str(output),
        '--reviewed-sha256', hashlib.sha256(prompt.read_bytes()).hexdigest()])
    calls = []
    def credential(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0 if available else 44, 'synthetic-key' if available else '', '')
    monkeypatch.setattr(module.subprocess, 'run', credential)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            assert limit == 1048577
            return json.dumps({'model': 'synthetic-model', 'choices': [{'message': {
                'content': 'UNTRUSTED synthetic-key'}, 'finish_reason': 'stop'}],
                'usage': {'total_tokens': 17, 'unexpected': {'nested': 'data'}}}).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://api.deepseek.com/v1/chat/completions'
            assert timeout == 180
            return Response()
    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *args: Opener())
    assert module.main() == (0 if available else 2)
    state = json.loads((output / 'status.json').read_text())
    assert state['candidate_executed'] is False
    assert state['requests_sent'] == int(available)
    assert state['state'] == ('needs_codex_review' if available else 'credential_unavailable')
    if available:
        assert (output / 'candidate.txt').read_text() == 'UNTRUSTED [REDACTED]'
        assert state['usage'] == {'total_tokens': 17}
    with pytest.raises(FileExistsError):
        module.main()
    assert len(calls) == 1


def test_keychain_authorization_timeout_is_checkpointed_without_request(tmp_path, monkeypatch):
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('synthetic arithmetic')
    output = tmp_path / 'attempt'
    monkeypatch.setattr(sys, 'argv', ['request-candidate', '--provider', 'token-plan',
        '--model', 'synthetic-model', '--prompt', str(prompt), '--output', str(output),
        '--reviewed-sha256', hashlib.sha256(prompt.read_bytes()).hexdigest()])
    def credential(args, **kwargs):
        assert args[-3:] == ['-a', 'development-coordinator', '-w']
        raise subprocess.TimeoutExpired(args, 15)
    monkeypatch.setattr(module.subprocess, 'run', credential)
    def no_network(*args, **kwargs):
        pytest.fail('authorization failure must not send an API request')
    monkeypatch.setattr(module.urllib.request, 'build_opener', no_network)
    assert module.main() == 2
    state = json.loads((output / 'status.json').read_text())
    assert state['state'] == 'credential_authorization_required'
    assert state['requests_sent'] == 0 and state['candidate_executed'] is False
