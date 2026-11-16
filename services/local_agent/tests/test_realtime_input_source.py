"""Only original local capture facts; never a cloud-input/replay permission."""
from datetime import datetime, timezone
import hashlib
import hmac
import json

import pytest
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from home_study_agent.learning_input_revision import LearningInputRevisionService
from test_realtime_output_authority import case, KEY


@pytest.fixture
def input_case(case):
    db, output, _ = case
    service = VoiceCaptureBindingService(db)
    capture_action = 'synthetic_capture_action'
    service.begin_realtime(output['session_id'], capture_action, 'agent_command', {},
                           model_version='client_reported_realtime:synthetic')
    capture = service.complete_realtime(output['session_id'], capture_action, '陪我聊聊花园里的花，第一句还有第二句。')
    voice_id = capture['voice_input_id']
    scope = {key: output[key] for key in ('session_id', 'child_id', 'connection_id', 'storage_generation')}
    scope.update(client_action_id=capture_action, voice_input_id=voice_id)
    return db, scope, capture


def input_proof(scope, **patch):
    from home_study_agent.realtime_input_source import DOMAIN, FIELDS
    command = dict(scope, issued_at=datetime.now(timezone.utc).isoformat())
    command.update(patch)
    signature = hmac.new(bytes.fromhex(KEY), json.dumps([DOMAIN] + [command[f] for f in FIELDS],
        ensure_ascii=False, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
    return dict(command, signature=signature)


def test_read_original_source_after_restart_without_granting_cloud_or_replay(input_case):
    from home_study_agent.realtime_input_source import RealtimeInputSource
    from home_study_agent.database import Database
    db, scope, capture = input_case
    with db.connect() as c:
        before = c.execute('SELECT count(*) FROM audit_event').fetchone()[0]
    result = RealtimeInputSource(db, KEY).read(input_proof(scope))
    assert result['scope'] == scope and result['transcript'] == capture['original_transcript']
    assert result['status'] == 'completed' and result['source_kind'] == 'realtime_transcript'
    assert result['arrival_status'] == 'not_dispatched'
    assert result['local_only'] and not any(result[key] for key in (
        'cloud_transmission_allowed', 'replay_allowed', 'mastery_update_allowed'))
    assert 'question_reference' not in result and 'signature' not in result
    restarted = RealtimeInputSource(Database(db.path), KEY).read(input_proof(scope))
    assert restarted['source_hash'] == result['source_hash']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM audit_event').fetchone()[0] == before
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('mutation', ['child', 'voice', 'action', 'session_closed', 'generation', 'capture_cancelled'])
def test_changed_owner_source_or_generation_cannot_be_used_for_tool_resolution(input_case, mutation):
    from home_study_agent.realtime_input_source import RealtimeInputSource, RealtimeInputSourceConflict
    db, scope, _ = input_case
    scope = dict(scope)
    with db.connect() as c:
        if mutation == 'child': scope['child_id'] = 'child-junior-3'
        if mutation == 'voice': scope['voice_input_id'] = 'different_voice_id'
        if mutation == 'action': scope['client_action_id'] = 'different_action_id'
        if mutation == 'session_closed': c.execute("UPDATE daily_session SET status='cancelled'")
        if mutation == 'generation': scope['storage_generation'] = 'old_storage_generation'
        if mutation == 'capture_cancelled': c.execute("UPDATE voice_capture_binding SET status='cancelled'")
        c.commit()
    with pytest.raises(RealtimeInputSourceConflict): RealtimeInputSource(db, KEY).read(input_proof(scope))


def test_newer_arrival_does_not_delete_original_local_evidence_or_grant_replay(input_case):
    from home_study_agent.realtime_input_source import RealtimeInputSource
    db, scope, capture = input_case
    source = RealtimeInputSource(db, KEY)
    initial = source.read(input_proof(scope))
    with db.connect() as c:
        arrivals = LearningInputRevisionService(db)
        arrivals.capture_arrival(scope['session_id'], 'realtime-' + scope['voice_input_id'], capture['original_transcript'], connection=c)
        c.commit()
    assert source.read(input_proof(scope))['arrival_status'] == 'current'
    with db.connect() as c:
        arrivals.capture_arrival(scope['session_id'], 'newer_synthetic_input', '另一条合成输入', connection=c)
        c.commit()
    saved = source.read(input_proof(scope))
    assert saved['arrival_status'] == 'superseded' and not saved['replay_allowed']
    assert saved['transcript'] == initial['transcript'] and saved['source_hash'] == initial['source_hash']


def test_body_is_purpose_signed_and_cannot_supply_original_text(input_case):
    from home_study_agent.realtime_input_source import RealtimeInputSource, RealtimeInputSourceConflict
    db, scope, _ = input_case
    source = RealtimeInputSource(db, KEY)
    for value in [input_proof(scope) | {'signature': 'forged'}, input_proof(scope) | {'transcript': '改写原话'},
                  input_proof(scope) | {'voice_input_id': 'different_voice_id'},
                  input_proof(scope, issued_at='2020-01-01T00:00:00Z')]:
        with pytest.raises(RealtimeInputSourceConflict): source.read(value)


def test_actual_http_read_requires_main_proof_and_survives_restart(input_case):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from test_realtime_output_authority_http import settings_for
    db, scope, capture = input_case
    settings = settings_for(db)
    route = f"/v1/sessions/{scope['session_id']}/voice-input-source"
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings)) as client:
        assert client.post(route, json=input_proof(scope)).status_code == 401
        assert client.post(route, headers=headers, json=input_proof(scope) | {'signature': 'forged'}).status_code == 409
        result = client.post(route, headers=headers, json=input_proof(scope))
        assert result.status_code == 200, result.text
        assert result.headers['cache-control'] == 'no-store'
        assert result.json()['transcript'] == capture['original_transcript']
    with TestClient(create_app(settings)) as client:
        assert client.post(route, headers=headers, json=input_proof(scope)).json()['source_hash'] == result.json()['source_hash']
