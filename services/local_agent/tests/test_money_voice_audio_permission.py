import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.money_voice_policy import attach_money_voice_policy
from normal_provider_owner import normal_provider_owner


TOKEN = 'public-synthetic-private-route-token'


def application(owner, callback=None, *, missing_media=False, text_allowed=True):
    app = FastAPI()
    app.state.money_owner = owner
    app.state.privacy_lifecycle = SimpleNamespace(preferences=lambda: {"cloud_model_text_allowed": text_allowed})
    if not missing_media:
        app.state.media_ai = SimpleNamespace(_cloud_audio_allowed=callback)
    attach_money_voice_policy(app, owner, SimpleNamespace(session_token=TOKEN))
    return app


def read(app, *, token=TOKEN, host='127.0.0.1'):
    with TestClient(app, client=(host, 5000)) as client:
        return client.get('/v1/private/money-egress', headers={'X-HSC-Session': token})


@pytest.mark.parametrize('allowed', [False, None, 1, 'true'])
def test_without_explicit_true_audio_permission_original_normal_owner_stays_segmented(normal_provider_owner, allowed):
    response = read(application(normal_provider_owner, lambda: allowed))
    assert response.status_code == 200
    assert response.text == '{"version":1,"capture":"bounded_segmented"}'
    assert response.headers['cache-control'] == 'no-store'
    assert normal_provider_owner.check() is False


@pytest.mark.parametrize('missing_media', [False, True])
def test_missing_media_or_permission_reader_stays_segmented(normal_provider_owner, missing_media):
    response = read(application(normal_provider_owner, missing_media=missing_media))
    assert response.json() == {'version': 1, 'capture': 'bounded_segmented'}


def test_permission_read_failure_stays_segmented(normal_provider_owner):
    def unavailable():
        raise RuntimeError('synthetic unavailable durable permission reader')
    response = read(application(normal_provider_owner, unavailable))
    assert response.json() == {'version': 1, 'capture': 'bounded_segmented'}


def test_true_audio_permission_preserves_configured_policy(normal_provider_owner):
    response = read(application(normal_provider_owner, lambda: True))
    assert response.text == '{"version":1,"capture":"configured"}'


def test_original_owner_retirement_during_permission_read_stays_blocked(normal_provider_owner):
    def retire():
        normal_provider_owner.close()
        return True
    response = read(application(normal_provider_owner, retire))
    assert response.json() == {'version': 1, 'capture': 'blocked'}


def test_foreign_application_owner_cannot_become_configured(normal_provider_owner):
    app = application(normal_provider_owner, lambda: True)
    app.state.money_owner = object()
    assert read(app).json() == {'version': 1, 'capture': 'blocked'}


@pytest.mark.parametrize('token,host', [(None, '127.0.0.1'), (TOKEN, '192.0.2.5')])
def test_private_session_and_loopback_guards_remain_required(normal_provider_owner, token, host):
    app = application(normal_provider_owner, lambda: True)
    response = read(app, token='' if token is None else token, host=host)
    assert response.status_code == 401
    assert response.json()['detail']['code'] == 'VOICE_RUNTIME_POLICY_UNAVAILABLE'


@pytest.mark.parametrize('allowed', [False, None, 1, 'true'])
def test_normal_native_voice_also_requires_explicit_model_text_permission(normal_provider_owner, allowed):
    response = read(application(normal_provider_owner, lambda: True, text_allowed=allowed))
    assert response.text == '{"version":1,"capture":"bounded_segmented"}'
