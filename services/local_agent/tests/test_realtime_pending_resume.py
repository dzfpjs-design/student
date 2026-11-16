"""Original control speech retains its original evaluation, never a fresh answer."""
import json

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from test_pending_evaluation_control import host, facts


INPUT = 'pending-original-input'
WORDS = '帮我看看刚才评价怎么样了？'


def save(db, sid):
    with db.connect() as c:
        child = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0]
    service = VoiceCaptureBindingService(db)
    service.begin_realtime(sid, INPUT, 'agent_command', {'child_id': child,
        'pending_evaluation_turn_id': 'eval'}, model_version='synthetic-asr')
    return child, service.complete_realtime(sid, INPUT, WORDS)


def route(sid):
    return f'/v1/sessions/{sid}/voice/realtime/actions/{INPUT}/resume'


def test_pending_unsent_speech_uses_original_restricted_lane_and_never_scores(tmp_path):
    with host(tmp_path) as (db, sid, client, requests):
        child, voice = save(db, sid)
        original = facts(db)
        preview = client.get(route(sid), params={'child_id': child})
        assert preview.status_code == 200 and preview.json()['status'] == 'ready', preview.text
        assert preview.json()['pending_evaluation_turn_id'] == 'eval'
        assert WORDS not in preview.text and not requests
        result = client.post(route(sid), json={'child_id': child, 'voice_input_id': voice['voice_input_id']})
        assert result.status_code == 200, result.text
        turn = result.json()
        assert turn['transcript'] == WORDS
        assert turn['action']['voice_input_id'] == voice['voice_input_id']
        assert turn['action']['pending_evaluation_control']['evaluation_turn_id'] == 'eval'
        assert turn['action']['pending_evaluation_query']['turn_id'] == 'eval'
        assert turn['agent_turn_id'] != 'eval' and turn['runtime_capability'] == 'conversation.respond'
        assert len(requests) == 1 and facts(db) == original


def test_planned_pending_control_resumes_original_turn_after_restart(tmp_path):
    with host(tmp_path, control='planning.describe') as (db, sid, client, requests):
        child, voice = save(db, sid)
        original = facts(db)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': WORDS,
            'voice_input_id': voice['voice_input_id'], 'pending_evaluation_turn_id': 'eval',
            'client_action_id': 'realtime-' + voice['voice_input_id']})
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved['tool_status'] == 'pending' and saved['runtime_execution_location'] == 'server'
        assert len(requests) == 1
        app_settings = client.app.state.settings
        gateway = client.app.state.model_gateway
    with TestClient(create_app(app_settings, model_gateway=gateway)) as client:
        from home_study_agent.voice_control_intent import VoiceControlIntent
        with db.connect() as c:
            VoiceControlIntent.inspect_execution(c, saved['agent_turn_id'], allow_planned=True)
        preview = client.get(route(sid), params={'child_id': child})
        assert preview.status_code == 200 and preview.json()['status'] == 'ready', preview.text
        assert preview.json()['resume_kind'] == 'execution'
        response = client.post(route(sid), json={'child_id': child, 'voice_input_id': voice['voice_input_id']})
        assert response.status_code == 200 and response.json()['agent_turn_id'] == saved['agent_turn_id'], response.text
        result = client.post(f"/v1/agent-turns/{saved['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert len(requests) == 1 and facts(db) == original


@pytest.mark.parametrize('fault', ['cancel', 'source_deleted', 'response_changed', 'capture_cancelled', 'wrong_child', 'unknown_claim'])
def test_pending_resume_does_not_replace_erased_changed_or_unknown_original(tmp_path, fault):
    from home_study_agent.agent_request_ownership import claim
    with host(tmp_path) as (db, sid, client, requests):
        child, voice = save(db, sid)
        if fault == 'unknown_claim':
            claim(db, sid, 'realtime-' + voice['voice_input_id'], WORDS,
                  voice_input_id=voice['voice_input_id'], pending_evaluation_turn_id='eval')
        elif fault == 'capture_cancelled': VoiceCaptureBindingService(db).cancel(sid, INPUT)
        elif fault == 'wrong_child': child = 'different-child'
        else:
            with db.connect() as c:
                sql = {'cancel': "UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'",
                    'source_deleted': "UPDATE agent_step SET error_code='SOURCE_DELETED' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')",
                    'response_changed': "UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','changed') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')"}[fault]
                c.execute(sql); c.commit()
        original = facts(db)
        response = client.post(route(sid), json={'child_id': child, 'voice_input_id': voice['voice_input_id']})
        assert response.status_code == 409, response.text
        assert not requests and facts(db) == original


def test_pending_source_change_after_preview_is_rechecked_before_semantics(tmp_path):
    with host(tmp_path) as (db, sid, client, requests):
        child, voice = save(db, sid)
        assert client.get(route(sid), params={'child_id': child}).json()['status'] == 'ready'
        with db.connect() as c:
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
            c.commit()
        original = facts(db)
        result = client.post(route(sid), json={'child_id': child, 'voice_input_id': voice['voice_input_id']})
        assert result.status_code == 409 and not requests and facts(db) == original


@pytest.mark.parametrize('fault', ['cancel_original', 'regrant_consent'])
def test_planned_pending_control_rechecks_frozen_authority_before_execute(tmp_path, fault):
    with host(tmp_path, control='planning.describe') as (db, sid, client, requests):
        child, voice = save(db, sid)
        original = facts(db)
        planned = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': WORDS,
            'voice_input_id': voice['voice_input_id'], 'pending_evaluation_turn_id': 'eval',
            'client_action_id': 'realtime-' + voice['voice_input_id']})
        assert planned.status_code == 200, planned.text
        resumed = client.post(route(sid), json={'child_id': child, 'voice_input_id': voice['voice_input_id']})
        assert resumed.status_code == 200, resumed.text
        if fault == 'cancel_original':
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
                c.commit()
        else:
            from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
            PrivacyLifecycleService(db, tmp_path).update_preferences({'cloud_model_text_allowed': True})
        execution = client.post(f"/v1/agent-turns/{resumed.json()['agent_turn_id']}/execute")
        assert execution.status_code == 409, execution.text
        assert len(requests) == 1 and facts(db) == original
