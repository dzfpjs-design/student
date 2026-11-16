"""Real HTTP and durable records with synthetic speech and a bounded model fixture."""
from datetime import datetime, timedelta, timezone
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from home_study_agent.oral_daily_reminder import voice_after_offer
from home_study_agent.writing_ocr_authorization import _voice
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, http, start, CHILD


def test_concurrent_same_original_has_one_runtime_owner_before_model_send(environment):
    settings, db = environment
    words = '小知，帮我看看纸上这道题。'
    entered, release = Event(), Event()
    class Delayed(Planner):
        async def complete(self, request):
            result = await super().complete(request)
            if len(self.requests) == 1:
                entered.set()
                assert await asyncio.to_thread(release.wait, 5)
            return result
    planner = Delayed('learning.capture_question', {}, words)
    with TestClient(create_app(settings, model_gateway=planner), raise_server_exceptions=False) as client:
        sid = start(client)
        service = VoiceCaptureBindingService(db)
        service.begin_realtime(sid, 'concurrent-input', 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr')
        voice = service.complete_realtime(sid, 'concurrent-input', words)
        body = {'transcript': words, 'voice_input_id': voice['voice_input_id'],
                'client_action_id': 'realtime-' + voice['voice_input_id']}
        path = f'/v1/sessions/{sid}/agent-turn'
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(client.post, path, json=body)
            assert entered.wait(3)
            try:
                second = workers.submit(client.post, path, json=body).result(timeout=3)
            finally:
                release.set()
            original = first.result(timeout=5)
        assert second.status_code == 409, second.text
        assert original.status_code == 200, original.text
        assert len(planner.requests) == 1
        replay = client.post(path, json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['agent_turn_id'] == original.json()['agent_turn_id']
        assert len(planner.requests) == 1


def test_real_http_complete_original_reaches_one_durable_agent_turn_across_restart(environment):
    settings, db = environment
    text = '小知，我把不会的那一页摆好了，你瞅瞅呗'
    planner = Planner('learning.capture_question', {}, text)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        sid = start(client)
        root = f'/v1/sessions/{sid}/voice/realtime'
        begin = client.post(root + '/begin', json={'client_action_id': 'original-realtime-input',
            'capture_reference': {'child_id': CHILD}, 'model_label': 'synthetic-asr',
            'captured_at_ms': int(datetime.now(timezone.utc).timestamp()*1000)})
        assert begin.status_code == 200, begin.text
        complete = client.post(root + '/complete', json={'client_action_id': 'original-realtime-input', 'transcript': text})
        assert complete.status_code == 200, complete.text
        original = complete.json()
        body = {'transcript': text, 'client_action_id': 'realtime-' + original['voice_input_id'],
            'voice_input_id': original['voice_input_id'], 'cloud_text_allowed': True}
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
        assert turn.status_code == 200, turn.text
        result = turn.json()
        assert result['transcript'] == text
        assert result['action']['type'] == 'request_capture'
        assert result['action']['voice_input_id'] == original['voice_input_id']
        snapshot = result['action']['voice_input_binding']
        assert snapshot['source_kind'] == 'realtime_transcript'
        assert snapshot['speech_started_at'] == original['speech_started_at']
        assert client.post(f'/v1/sessions/{sid}/agent-turn', json=body).json()['agent_turn_id'] == result['agent_turn_id']
    with TestClient(create_app(settings, model_gateway=planner)) as restarted:
        repeat = restarted.post(f'/v1/sessions/{sid}/agent-turn', json=body)
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()['agent_turn_id'] == result['agent_turn_id']
        changed = restarted.post(f'/v1/sessions/{sid}/agent-turn', json={**body, 'transcript': '我选B'})
        assert changed.status_code == 409
    assert len(planner.requests) == 1
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 1
        assert c.execute('SELECT transcript FROM voice_input').fetchone()[0] == text
        manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan').fetchone()[0])
        assert manifest['voice_input_binding'] == snapshot


@pytest.mark.parametrize('onset', ['before_offer', 'after_offer', 'unknown'])
def test_late_transcript_cannot_confirm_a_later_offer_using_admission_time(http, onset):
    _client, db, sid = http
    now = datetime.now(timezone.utc)
    offered = now - timedelta(seconds=20)
    captured = now - timedelta(seconds=40 if onset == 'before_offer' else 5)
    service = VoiceCaptureBindingService(db)
    service.begin_realtime(sid, 'delayed-offer-input', 'agent_command', {'child_id': CHILD},
        model_version='synthetic-asr', captured_at_ms=None if onset == 'unknown' else int(captured.timestamp()*1000))
    voice = service.complete_realtime(sid, 'delayed-offer-input', '我愿意试试')
    with db.connect() as c:
        assert voice_after_offer(c, CHILD, sid, voice['voice_input_id'], offered.isoformat()) == (onset == 'after_offer')
        assert not voice_after_offer(c, 'another-child', sid, voice['voice_input_id'], offered.isoformat())
        with pytest.raises(ValueError, match='OCR_VOICE_SOURCE_REQUIRED'):
            _voice(c, sid, CHILD, voice['voice_input_id'], voice['original_transcript'])


def test_v80_upgrade_preserves_realtime_body_and_does_not_invent_an_onset(tmp_path):
    from historical_component_fixture import family
    db, _repo, sid = family(tmp_path / 'legacy-realtime.db', CHILD, 'legacy-realtime-session')
    service = VoiceCaptureBindingService(db)
    service.begin_realtime(sid, 'v80-original-input', 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr')
    original = service.complete_realtime(sid, 'v80-original-input', '历史完整原话')
    with db.connect() as c:
        c.execute('ALTER TABLE voice_capture_binding DROP COLUMN speech_started_at')
        c.execute('DELETE FROM schema_migration WHERE version>=81')
        c.commit()
    db.initialize()
    recovered = VoiceCaptureBindingService(db).complete_realtime(sid, 'v80-original-input', '历史完整原话')
    assert recovered == original and recovered['onset_provenance'] == 'unknown'
    status = db.latest_upgrade_status()
    from home_study_agent.database import LATEST_SCHEMA_VERSION
    assert status['source_schema_version'] == 80 and status['target_schema_version'] == LATEST_SCHEMA_VERSION
    assert status['status'] == 'completed'


def test_actual_privacy_cancellation_before_onset_post_fences_late_begin(http):
    client, db, sid = http
    key = 'cancel-before-onset'
    cancelled = client.post(f'/v1/sessions/{sid}/asr/actions/{key}/cancel')
    assert cancelled.status_code == 200, cancelled.text
    response = client.post(f'/v1/sessions/{sid}/voice/realtime/begin', json={
        'client_action_id': key, 'capture_reference': {'child_id': CHILD}, 'model_label': 'synthetic-asr',
        'captured_at_ms': int(datetime.now(timezone.utc).timestamp()*1000)})
    assert response.status_code == 409, response.text
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM voice_capture_binding WHERE client_action_id=?', (key,)).fetchone()
        assert not c.execute('SELECT 1 FROM voice_input WHERE client_action_id=?', (key,)).fetchone()
