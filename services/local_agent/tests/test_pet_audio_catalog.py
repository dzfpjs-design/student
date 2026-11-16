import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import json
import httpx
import pytest
from normal_provider_owner import normal_provider_owner
from home_study_agent.config import Settings
from home_study_agent.pet_audio_catalog import CATALOG_ID, MODEL, RATES, VERIFIED_AT, REVIEW_BEFORE, current
from home_study_agent.pet_audio_gateway import AudioEndpoint, PetAudioGateway, PetAudioProtocolError, from_settings, MAX_OUTPUT_TOKENS
from test_pet_audio_gateway import feedback, event


@pytest.fixture(autouse=True)
def catalog_clock(monkeypatch):
    class VerifiedClock(datetime):
        @classmethod
        def now(cls, tz=None): return (datetime.fromisoformat(VERIFIED_AT) + timedelta(hours=1)).astimezone(tz)
    monkeypatch.setattr('home_study_agent.pet_audio_catalog.datetime', VerifiedClock)


def endpoint():
    return AudioEndpoint('https://dashscope.aliyuncs.com/compatible-mode/v1', MODEL, 'synthetic-no-key',
        input_second_micros=371, input_text_token_micros=7, output_token_micros=40,
        tariff_version=CATALOG_ID, catalog_id=CATALOG_ID, configuration_id='original-config-id')


def test_catalog_rates_are_upward_bounds_of_the_reviewed_official_modal_prices():
    assert tuple(int(value.to_integral_value(rounding=ROUND_CEILING)) for value in (
        Decimal('7') * Decimal('53'), Decimal('7'), Decimal('40'))) == RATES
    value = endpoint()
    assert value.reserve_cost(duration_ms=1, question='q') == value.reserve_cost(duration_ms=1000, question='q')
    assert value.reserve_cost(duration_ms=1001, question='q') - value.reserve_cost(duration_ms=1000, question='q') == 371
    assert value.reserve_cost(duration_ms=120000, question='q') < 100000


@pytest.mark.parametrize('change', [dict(model='qwen3-omni-flash'), dict(model='qwen3.5-omni-plus'), dict(model='qwen3-omni-flash-2025-12-01'),
    dict(base_url='https://dashscope-intl.aliyuncs.com/compatible-mode/v1'), dict(input_second_micros=1),
    dict(input_text_token_micros=True), dict(configuration_id=''), dict(tariff_version='unverified')])
def test_managed_catalog_rejects_alias_region_tampered_price_and_missing_configuration(change):
    value = replace(endpoint(), **change)
    assert not current(value)
    with pytest.raises(PetAudioProtocolError): value.identity()


def test_expiry_and_clock_rollback_block_live_identity_and_do_not_crash_backend_start(monkeypatch, tmp_path):
    value = endpoint()
    assert current(value, datetime.fromisoformat(VERIFIED_AT))
    assert not current(value, datetime.fromisoformat(VERIFIED_AT) - timedelta(seconds=1))
    assert not current(value, datetime.fromisoformat(REVIEW_BEFORE))
    monkeypatch.setattr('home_study_agent.pet_audio_catalog.current', lambda endpoint: False)
    with pytest.raises(PetAudioProtocolError): value.identity()
    settings = replace(Settings(data_dir=tmp_path, database_path=tmp_path/'synthetic.db'), pet_audio_enabled=True, pet_audio_api_key='synthetic', pet_audio_model=MODEL,
        pet_audio_catalog_id=CATALOG_ID, pet_audio_configuration_id='original-config-id',
        pet_audio_tariff_version=CATALOG_ID, pet_audio_input_second_micros=371,
        pet_audio_input_text_token_micros=7, pet_audio_output_token_micros=40)
    assert from_settings(settings) is None


def test_changed_configuration_identity_fences_same_model_same_key_aba():
    old = endpoint()
    assert old.identity() != replace(old, configuration_id='replacement-config-id').identity()
    assert old.identity() == replace(old).identity()


def test_exact_model_non_thinking_text_only_bounded_post_and_expiry_between_headers_and_body(monkeypatch, normal_provider_owner):
    bodies = []
    async def respond(request):
        bodies.append(json.loads(await request.aread()))
        raw = event({'model': MODEL, 'choices': [{'index': 0, 'delta': {'content': json.dumps(feedback())}, 'finish_reason': None}]})
        raw += event({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}) + b'data: [DONE]\n\n'
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=raw)
    gateway = PetAudioGateway(endpoint(), transport=httpx.MockTransport(respond))
    result = asyncio.run(gateway.feedback(question='q', audio=b'x'*44, duration_ms=1000, authorize=lambda phase: None))
    assert result['reported_model'] == MODEL
    assert bodies[0]['response_format'] == {'type': 'json_object'} and 'enable_thinking' not in bodies[0]
    assert bodies[0]['model'] == MODEL and bodies[0]['modalities'] == ['text']
    assert bodies[0]['max_tokens'] == MAX_OUTPUT_TOKENS
    def expire(phase):
        if phase == 'headers': monkeypatch.setattr('home_study_agent.pet_audio_catalog.current', lambda endpoint: False)
    with pytest.raises(PetAudioProtocolError):
        asyncio.run(gateway.feedback(question='q', audio=b'x'*44, duration_ms=1000, authorize=expire))
    assert len(bodies) == 1


def test_reviewed_snapshot_is_fixed_with_exact_thirty_day_utc_window():
    assert MODEL == 'qwen3.5-omni-plus-2026-03-15'
    start, end = datetime.fromisoformat(VERIFIED_AT), datetime.fromisoformat(REVIEW_BEFORE)
    assert start.utcoffset() == end.utcoffset() == timedelta(0)
    assert end - start == timedelta(days=30)
