"""Real Gateway, local HTTP and reopened DB; public synthetic transport only."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from normal_provider_owner import normal_provider_owner
from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_agent_plan_json_fallback import settings_for
from test_daily_planning_state_contract import capture_session, plan
from test_voice_runtime_binding import capture


QUOTE = '作业说完了，帮我安排计划。'
PRIVATE_FAILED = 'PRIVATE_FAILED_MODEL_CONTENT_NOT_FOR_REPAIR'


def exercise(tmp_path, monkeypatch, *, fallback, responses, expect_plan, expected_hosts,
             first_usage=None, schema_repair=False, revoke_on_first=False):
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path/'gateway.db'))
    settings = settings_for(tmp_path, fallback=fallback)
    db = Database(settings.database_path)
    db.initialize()
    privacy = PrivacyLifecycleService(db, settings.data_dir)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    valid = json.dumps(plan('planning.generate', {}, QUOTE).payload, ensure_ascii=False)
    posts = []

    def handler(request):
        index = len(posts)
        posts.append((request.url.host, json.loads(request.read())))
        assert index < len(responses), 'UNEXPECTED_PHYSICAL_MODEL_REQUEST'
        value = responses[index]
        if revoke_on_first and index == 0:
            privacy.update_preferences({'cloud_model_text_allowed': False})
        if value == 'timeout':
            raise httpx.ReadTimeout('synthetic-only', request=request)
        if type(value) is int:
            return httpx.Response(value, request=request, json={'error': 'synthetic-only'})
        reason = 'stop'
        if value in {'length', 'content_filter'}:
            reason, content = value, PRIVATE_FAILED
        else:
            content = valid if value == 'valid' else '{}' if value == 'schema' else PRIVATE_FAILED
        return httpx.Response(200, request=request, json={
            'choices': [{'finish_reason': reason, 'message': {
                'content': content, 'reasoning_content': 'PRIVATE_REASONING_NOT_FOR_REPAIR'}}],
            'usage': {'prompt_tokens': 10+index, 'completion_tokens': 20+index}})

    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = capture_session(client)
        saved = client.post(f'/v1/sessions/{sid}/homework', json={
            'subject': '数学', 'title': '原创合成练习', 'expected_minutes': 20,
            'client_action_id': 'syntax-repair-homework'})
        assert saved.status_code == 201
        voice = capture(client, settings, monkeypatch, sid, QUOTE, key='syntax-repair-voice')
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': QUOTE, 'voice_input_id': voice, 'cloud_text_allowed': True,
            'client_action_id': 'syntax-repair-turn'})
        assert response.status_code == 200
        heard = response.json()
        if expect_plan:
            assert heard['action'].get('capability') == 'planning.generate', (
                'PLANNER_REPLY_UNAVAILABLE', heard['action'].get('source'), len(posts))
            assert heard['action']['source'] == 'agent_runtime_planner'
            executed = client.post('/v1/agent-turns/'+heard['agent_turn_id']+'/execute')
            assert executed.status_code == 200
        else:
            assert heard['action']['type'] == 'none'
            assert heard['action']['source'] == 'cloud_planner_unavailable'
        assert [host for host, _ in posts] == expected_hosts
        assert len(posts) <= 4

    reopened = Database(settings.database_path)
    with reopened.connect() as connection:
        rows = [dict(r) for r in connection.execute(
            'SELECT * FROM model_call WHERE session_id=? ORDER BY rowid', (sid,))]
        assert connection.execute('SELECT state FROM daily_session WHERE session_id=?',
                                  (sid,)).fetchone()[0] == ('plan_review' if expect_plan else 'homework_capture')
        assert connection.execute('SELECT COUNT(*) FROM daily_plan WHERE session_id=?',
                                  (sid,)).fetchone()[0] == int(expect_plan)
        assert connection.execute('SELECT COUNT(*) FROM daily_plan WHERE child_id=?',
                                  ('child-junior-3',)).fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert PRIVATE_FAILED not in json.dumps(rows, ensure_ascii=False)
        assert 'PRIVATE_REASONING' not in json.dumps(rows, ensure_ascii=False)
    repaired = len(rows) == 2
    if first_usage is not None:
        assert repaired
        first, second = rows
        assert first['purpose'] == 'agent_plan' and first['status'] == 'failed'
        assert first['error_code'] == ('MODEL_INVALID_AGENT_PLAN' if schema_repair else 'MODEL_INVALID_JSON')
        assert (first['prompt_tokens'], first['completion_tokens']) == first_usage
        assert type(first['latency_ms']) is int and first['latency_ms'] >= 0
        assert first['output_chars'] == (2 if schema_repair else None)
        assert second['purpose'] == 'agent_plan_repair'
        assert second['status'] == ('completed' if expect_plan else 'failed')
        repair = json.loads(second['context_manifest_json'])['planner_repair']
        assert repair['attempt'] == 1 and repair['source_call_id'] == first['call_id']
        if not schema_repair:
            assert repair['validation_error'] == 'planner did not return JSON'
        if revoke_on_first:
            assert len(posts) == 1
            assert privacy.preferences()['cloud_model_text_allowed'] is False
            assert second['error_code'] == 'MODEL_VOICE_DISPATCH_CONSENT_CHANGED'
            assert (second['output_chars'], second['prompt_tokens'], second['completion_tokens']) == (0, None, None)
            return rows
        first_body = posts[0][1]['messages']
        repair_start = 2 if fallback else 1
        repaired_messages = posts[repair_start][1]['messages']
        assert repaired_messages[:len(first_body)] == first_body
        suffix = repaired_messages[len(first_body):]
        assert [m['role'] for m in suffix] == (['assistant', 'user'] if schema_repair else ['user'])
        assert PRIVATE_FAILED not in json.dumps(repaired_messages, ensure_ascii=False)
        assert 'PRIVATE_REASONING' not in json.dumps(repaired_messages, ensure_ascii=False)
        if schema_repair:
            assert suffix[0]['content'] == '{}'
    else:
        assert not repaired
    return rows


def test_no_fallback_non_json_reaches_original_once_repair_and_durable_plan_review(tmp_path, monkeypatch):
    exercise(tmp_path, monkeypatch, fallback=False, responses=['invalid', 'valid'],
             expect_plan=True, expected_hosts=['primary.invalid']*2, first_usage=(10, 20))


def test_both_routes_non_json_repair_valid_keeps_four_physical_cap(tmp_path, monkeypatch):
    exercise(tmp_path, monkeypatch, fallback=True, responses=['invalid', 'invalid', 'valid'],
             expect_plan=True, expected_hosts=['primary.invalid', 'fallback.invalid', 'primary.invalid'],
             first_usage=(21, 41))


@pytest.mark.parametrize('fallback', [False, True])
def test_repair_still_non_json_finishes_without_third_semantic_attempt(tmp_path, monkeypatch, fallback):
    count = 4 if fallback else 2
    rows = exercise(tmp_path, monkeypatch, fallback=fallback, responses=['invalid']*count,
                    expect_plan=False, expected_hosts=['primary.invalid', 'fallback.invalid']*2
                    if fallback else ['primary.invalid']*2, first_usage=(21, 41) if fallback else (10, 20))
    assert rows[-1]['error_code'] == 'MODEL_INVALID_JSON'


def test_original_schema_repair_keeps_prior_assistant_and_single_repair(tmp_path, monkeypatch):
    exercise(tmp_path, monkeypatch, fallback=False, responses=['schema', 'valid'],
             expect_plan=True, expected_hosts=['primary.invalid']*2, first_usage=(10, 20), schema_repair=True)


@pytest.mark.parametrize('first,expected_count', [(403, 1), ('timeout', 1), ('content_filter', 1), ('length', 1)])
def test_non_syntax_gateway_failures_never_enter_semantic_repair(tmp_path, monkeypatch, first, expected_count):
    exercise(tmp_path, monkeypatch, fallback=False, responses=[first]*expected_count,
             expect_plan=False, expected_hosts=['primary.invalid']*expected_count)


def test_revoked_original_text_consent_after_invalid_response_blocks_repair_dispatch(tmp_path, monkeypatch):
    rows = exercise(tmp_path, monkeypatch, fallback=False, responses=['invalid'],
                    expect_plan=False, expected_hosts=['primary.invalid'], first_usage=(10, 20),
                    revoke_on_first=True)
    assert len(rows) == 2
