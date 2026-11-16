"""Actual HTTP middleware/exception contracts; only the recognizer failure is synthetic."""
import base64
import json

import pytest
from fastapi.testclient import TestClient
from home_study_agent.config import Settings
from home_study_agent.local_media_ai import LocalCapabilityNotReadyError, LocalMediaAI


from home_study_agent.app import create_app

CREATE = create_app
ROUTES = ('/v1/standby/asr', '/v1/household/voice-entry/asr')
EXPECTED = ('ASR_NO_HUMAN_SPEECH', 'ASR_NO_MEANINGFUL_SPEECH', 'ASR_NO_SPEECH')
PRIVATE = 'sk-synthetic-905c55cec07ba8cc6cb0 CHILD_PRIVATE_AUDIO_CONTENT'


def run_error(tmp_path, monkeypatch, capsys, route, code, method='POST'):
    def recognition(*args, **kwargs):
        raise LocalCapabilityNotReadyError(code, PRIVATE)
    monkeypatch.setattr(LocalMediaAI, 'transcribe_standby', recognition)
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'public.db', data_encryption_key=b'0'*32,
                        maintenance_enabled=False)
    app = CREATE(settings)
    if route == '/v1/public-non-asr-negative':
        @app.get(route)
        async def non_asr():
            raise LocalCapabilityNotReadyError(code, PRIVATE)
    with TestClient(app) as client:
        if method == 'POST':
            result = client.post(route+'?private_query='+PRIVATE, json={
                'wav_base64': base64.b64encode(b'public-synthetic-wav'*2).decode(),
                'purpose': 'household_entry' if 'voice-entry' in route else 'household_wake',
                'client_action_id': 'public-only-diagnostic-contract',
            })
        else:
            result = client.get(route+'?private_query='+PRIVATE)
    assert result.status_code == 503
    assert result.json() == {'code': code, 'message': PRIVATE}
    assert result.headers['X-HSC-Trace']
    rows = [json.loads(line.removeprefix('HSC_DIAGNOSTIC ')) for line in capsys.readouterr().out.splitlines()
            if line.startswith('HSC_DIAGNOSTIC ')]
    row = next(x for x in rows if x['event']=='local_api_request' and x['details']['route_template']==route)
    assert row['details']['statusCode'] == 503
    assert row['correlationId'] == result.headers['X-HSC-Trace']
    assert PRIVATE not in json.dumps(row)
    assert 'private_query' not in json.dumps(row)
    return row


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('code', EXPECTED)
def test_expected_standby_non_speech_keeps_wire_error_but_logs_expected_result(tmp_path, monkeypatch, capsys, route, code):
    row = run_error(tmp_path, monkeypatch, capsys, route, code)
    assert row['level'] == 'info'
    assert row['details']['reason_code'] == code


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('code', ('ASR_MODEL_NOT_READY', 'LOCAL_ASR_BUSY'))
def test_real_standby_faults_stay_errors_with_only_closed_code(tmp_path, monkeypatch, capsys, route, code):
    row = run_error(tmp_path, monkeypatch, capsys, route, code)
    assert row['level'] == 'error'
    assert row['details'].get('reason_code', row['details'].get('error_code')) == code


def test_non_asr_route_with_same_code_is_not_downgraded(tmp_path, monkeypatch, capsys):
    row = run_error(tmp_path, monkeypatch, capsys, '/v1/public-non-asr-negative', EXPECTED[0], 'GET')
    assert row['level'] == 'error'


def test_similar_but_unregistered_code_stays_error_and_is_not_exported(tmp_path, monkeypatch, capsys):
    code = 'ASR_NO_HUMAN_SPEECH_sk_PRIVATE'
    row = run_error(tmp_path, monkeypatch, capsys, ROUTES[0], code)
    assert row['level'] == 'error'
    assert code not in json.dumps(row)
