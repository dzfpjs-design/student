from dataclasses import replace
from datetime import datetime, timezone
import json
from fastapi.testclient import TestClient
import pytest
from home_study_agent.app import create_app
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService, VoiceCaptureBindingError
from home_study_agent.realtime_recovery_index import read_realtime_recovery_index
from test_diagnostic_prompt_http import environment, start, CHILD


def originals(db, sid):
    service = VoiceCaptureBindingService(db)
    for key in ('recovery-original-first', 'recovery-original-second'):
        service.begin_realtime(sid, key, 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr',
            captured_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000))
    return service.complete_realtime(sid, 'recovery-original-first', '完整合成原话，不应出现在身份索引')


def test_local_identity_index_survives_reload_has_no_text_or_replay_and_rejects_other_owner(environment):
    settings, db = environment
    settings = replace(settings, session_token='synthetic-local-session-token')
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings), headers=headers) as client:
        sid = start(client); first = originals(db, sid)
        route = f'/v1/sessions/{sid}/voice/realtime/recovery-index?child_id={CHILD}'
        assert client.get(route, headers={'X-HSC-Session': 'wrong-token'}).status_code == 401
        result = client.get(route)
        assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
        value = result.json()
        assert value['entries'][0]['voice_input_id'] == first['voice_input_id']
        assert [row['status'] for row in value['entries']] == ['completed', 'prepared']
        assert '原话' not in result.text and 'transcript' not in result.text
        assert value['replay_allowed'] is False and value['cloud_transmission_allowed'] is False
        assert client.get(route.replace(CHILD, 'child-junior-3')).status_code == 409
    with TestClient(create_app(settings), headers=headers) as restarted:
        assert restarted.get(route).json() == value
        assert restarted.get(route).json() == value
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM agent_turn').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == 0
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled', 'pending'])
def test_terminal_execution_or_cancellation_is_not_restored_as_unsent(environment, terminal):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client); first = originals(db, sid)
        if terminal == 'cancelled':
            assert client.post(f'/v1/sessions/{sid}/asr/actions/recovery-original-first/cancel').status_code == 200
        else:
            with db.connect() as c:
                c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
                    orchestrator_version,tool_status,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,'synthetic',?,?,?)''',
                    ('synthetic-recovery-turn', CHILD, sid, first['original_transcript'], 'conversation', 'synthetic reply',
                     json.dumps({'voice_input_id': first['voice_input_id']}), terminal, '2026-10-06T00:00:00Z', 'different-original-runtime-key'))
                c.commit()
        result = read_realtime_recovery_index(db, sid, CHILD)
        assert len(result['entries']) == (2 if terminal == 'pending' else 1)
        assert result['replay_allowed'] is False
        with pytest.raises(VoiceCaptureBindingError): read_realtime_recovery_index(db, sid, 'child-junior-3')
