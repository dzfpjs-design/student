from __future__ import annotations

from pathlib import Path
import httpx
import pytest

from normal_provider_owner import normal_provider_owner
from home_study_agent.cloud_asr import CloudAsrError, CloudAsrGateway
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.local_media_ai import LocalMediaAI, LocalCapabilityNotReadyError
from home_study_agent.model_runtime_policy import ModelRuntimePolicyService
from test_cloud_asr_audio_permission import consent_db, permitted


ROOT = next(path for path in Path(__file__).resolve().parents
            if (path / 'services/local_agent/tests/fixtures').is_dir())
WAV = ROOT / 'services/local_agent/tests/fixtures/short-mandarin-dui-trimmed.wav'


def settings(tmp_path):
    return Settings(data_dir=tmp_path, database_path=tmp_path / 'agent.db',
                    cloud_asr_provider='dashscope', cloud_asr_api_key='synthetic-key',
                    cloud_asr_base_url='https://synthetic.invalid/v1',
                    cloud_asr_model='qwen3-asr-flash')


def authorized_gateway(tmp_path, policy):
    database = consent_db(tmp_path, 1)
    return CloudAsrGateway(settings(tmp_path), runtime_policy=policy,
                           cloud_audio_authorized=lambda: permitted(database))


class PolicySpy:
    def __init__(self): self.failures, self.successes = [], []
    def endpoint_available(self, provider, model): return True
    def record_failure(self, provider, model, code): self.failures.append(code)
    def record_success(self, provider, model): self.successes.append((provider, model))


def client(monkeypatch, contents):
    calls = []
    outcomes = iter(contents)
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def post(self, url, headers, json):
            calls.append(url)
            value = next(outcomes)
            if isinstance(value, Exception): raise value
            if isinstance(value, httpx.Response): return value
            return httpx.Response(200, request=httpx.Request('POST', url), json=value)
    monkeypatch.setattr(httpx, 'Client', Client)
    return calls


def body(text):
    return {'choices': [{'message': {'content': text}}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 0}}


def test_empty_successes_do_not_trip_route_and_next_wake_reaches_provider(tmp_path, monkeypatch, normal_provider_owner):
    database = Database(tmp_path / 'route.db'); database.initialize()
    policy = ModelRuntimePolicyService(database); policy.initialize()
    calls = client(monkeypatch, [body(' \n ')] * 8 + [body('小知')])
    gateway = authorized_gateway(tmp_path, policy)
    for _ in range(8):
        with pytest.raises(CloudAsrError, match='CLOUD_ASR_EMPTY_TRANSCRIPT'):
            gateway.transcribe(b'RIFF-synthetic-protocol-fixture')
        assert policy.endpoint_available('qwen', 'qwen3-asr-flash') is True
    result = gateway.transcribe(b'RIFF-synthetic-protocol-fixture')
    assert result.transcript == '小知'
    with database.connect() as connection:
        row = connection.execute('SELECT * FROM model_route_state').fetchone()
    assert row['status'] == 'healthy' and row['consecutive_failures'] == 0
    assert row['last_error_code'] is None and row['blocked_until'] is None
    assert len(calls) == 9


def test_no_speech_has_typed_bounded_usage_and_does_not_report_service_failure(tmp_path, monkeypatch, normal_provider_owner):
    policy = PolicySpy(); client(monkeypatch, [body('')])
    with pytest.raises(CloudAsrError) as caught:
        authorized_gateway(tmp_path, policy).transcribe(b'RIFF-synthetic')
    assert type(caught.value).__name__ == 'CloudAsrNoSpeechError'
    assert str(caught.value) == 'CLOUD_ASR_EMPTY_TRANSCRIPT'
    result = caught.value.result
    assert result.transcript == '' and result.model == 'qwen3-asr-flash'
    assert result.prompt_tokens == 11 and result.completion_tokens == 0
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
    assert policy.failures == [] and policy.successes == [('qwen', 'qwen3-asr-flash')]


@pytest.mark.parametrize('purpose', ['wake_word_setup', 'agent_command'])
def test_no_content_is_discarded_without_local_guess_or_service_failure(tmp_path, monkeypatch, normal_provider_owner, purpose):
    policy = PolicySpy(); calls = client(monkeypatch, [body('')])
    gateway = authorized_gateway(tmp_path, policy)
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)
    completions = []
    monkeypatch.setattr(media, '_start_cloud_asr_call', lambda *args, **kwargs: 'synthetic-call')
    monkeypatch.setattr(media, '_finish_cloud_asr_call', lambda call_id, **values: completions.append((call_id, values)))
    monkeypatch.setattr(media, '_recognize_locally', lambda *args: pytest.fail('an empty successful cloud result must not guess local content'))
    with pytest.raises(LocalCapabilityNotReadyError) as caught:
        media._recognize_wav(WAV.read_bytes(), prefer_cloud=True, purpose=purpose)
    assert caught.value.code == 'ASR_NO_MEANINGFUL_SPEECH'
    assert len(calls) == 1 and policy.failures == []
    assert completions == [('synthetic-call', {'status': 'completed', 'latency_ms': completions[0][1]['latency_ms'],
            'output_chars': 0, 'prompt_tokens': 11, 'completion_tokens': 0, 'model': 'qwen3-asr-flash'})]


@pytest.mark.parametrize('content', [None, 123, ['小知'], {'text': '小知'}])
def test_non_text_content_is_malformed_and_never_becomes_a_transcript(tmp_path, monkeypatch, normal_provider_owner, content):
    policy = PolicySpy(); client(monkeypatch, [body(content)])
    with pytest.raises(CloudAsrError, match='CLOUD_ASR_REQUEST_FAILED'):
        authorized_gateway(tmp_path, policy).transcribe(b'RIFF-synthetic')
    assert policy.failures == ['MODEL_RESPONSE_INVALID'] and policy.successes == []


@pytest.mark.parametrize('response', [{}, {'choices': []}, {'choices': [{'message': {}}]}])
def test_missing_response_fields_remain_service_failures(tmp_path, monkeypatch, normal_provider_owner, response):
    policy = PolicySpy(); client(monkeypatch, [response])
    with pytest.raises(CloudAsrError, match='CLOUD_ASR_REQUEST_FAILED'):
        authorized_gateway(tmp_path, policy).transcribe(b'RIFF-synthetic')
    assert policy.failures == ['MODEL_RESPONSE_INVALID'] and policy.successes == []


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_http_failures_keep_closed_and_record_real_service_failure(tmp_path, monkeypatch, normal_provider_owner, status):
    policy = PolicySpy()
    client(monkeypatch, [httpx.Response(status, request=httpx.Request('POST', 'https://synthetic.invalid/'))])
    code = 'CLOUD_ASR_RATE_LIMITED' if status == 429 else f'CLOUD_ASR_HTTP_{status}'
    with pytest.raises(CloudAsrError, match=code):
        authorized_gateway(tmp_path, policy).transcribe(b'RIFF-synthetic')
    assert policy.failures == [f'MODEL_HTTP_{status}'] and policy.successes == []


@pytest.mark.parametrize('error,code,failure', [(httpx.ReadTimeout('synthetic timeout'), 'CLOUD_ASR_TIMEOUT', 'MODEL_TIMEOUT'),
        (httpx.ConnectError('synthetic network failure'), 'CLOUD_ASR_REQUEST_FAILED', 'MODEL_NETWORK_ERROR')])
def test_network_and_timeout_failures_remain_service_failures(tmp_path, monkeypatch, normal_provider_owner, error, code, failure):
    policy = PolicySpy(); client(monkeypatch, [error])
    with pytest.raises(CloudAsrError, match=code):
        authorized_gateway(tmp_path, policy).transcribe(b'RIFF-synthetic')
    assert policy.failures == [failure] and policy.successes == []


def test_real_timeouts_still_trigger_persisted_cooldown(tmp_path, monkeypatch, normal_provider_owner):
    database = Database(tmp_path / 'route.db'); database.initialize()
    policy = ModelRuntimePolicyService(database); policy.initialize()
    calls = client(monkeypatch, [httpx.ReadTimeout('synthetic')] * 4)
    gateway = authorized_gateway(tmp_path, policy)
    for _ in range(4):
        with pytest.raises(CloudAsrError, match='CLOUD_ASR_TIMEOUT'):
            gateway.transcribe(b'RIFF-synthetic')
    assert policy.endpoint_available('qwen', 'qwen3-asr-flash') is False
    with pytest.raises(CloudAsrError, match='CLOUD_ASR_ROUTE_UNAVAILABLE'):
        gateway.transcribe(b'RIFF-synthetic')
    assert len(calls) == 4
