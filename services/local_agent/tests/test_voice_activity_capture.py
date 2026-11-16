"""Real ASR host freezes activity focus before recognition; synthetic audio only."""
import base64

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.voice_capture_binding import resolve_voice_capture
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_companion_request_provenance_http import planner, planned
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_pet_media_evidence import wav_file


def started(client, settings, db, monkeypatch):
    sid = start(client)
    turn, _ = planned(client, settings, monkeypatch, sid)
    result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    with db.connect() as c:
        focus = VoiceControlIntent.activity_focus(c, sid, CHILD)
    assert focus and focus['last_turn_id'] == turn['agent_turn_id']
    return sid, focus


def payload(settings, focus, key='activity-response-capture', question=None):
    path = settings.data_dir / 'activity-response.wav'
    wav_file(path)
    return {'wav_base64': base64.b64encode(path.read_bytes()).decode(), 'purpose': 'agent_command',
            'client_action_id': key, 'capture_reference': {'child_id': CHILD,
                'activity_reference': focus, 'question_reference': question}}


def test_activity_capture_preserves_original_question_but_cannot_score_it(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        sid, focus = started(client, settings, db, monkeypatch)
        prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': 'diag-pet-speaking-2', 'client_action_id': 'preserved-question'})
        assert prepared.status_code == 200, prepared.text
        question = {k: prepared.json()['prompt'][k] for k in ('prompt_id', 'item_version')}
        calls = []
        def recognize(self, wav, **kwargs):
            calls.append(kwargs['purpose'])
            return '准备好了', 1000, 'synthetic-recognition', False
        monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
        result = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings, focus, question=question))
        assert result.status_code == 200, result.text
        assert result.json()['capture_binding']['activity_reference'] == focus
        assert calls == ['agent_command']
        assert 'speaking_media_status' not in result.json()['capture_binding']
        with db.connect() as c:
            frozen = resolve_voice_capture(c, session_id=sid, child_id=CHILD,
                voice_input_id=result.json()['voice_input_id'], transcript='准备好了')
            assert frozen['question_reference'] == {'kind': 'diagnostic', 'binding': question}
            assert frozen['activity_reference'] == focus
            assert frozen['activity_validation'] == 'valid'
            assert AgentRuntimeStore.voice_capture_snapshot(frozen)['activity_reference'] == focus
            assert not AgentRuntimeStore.capture_matches_answer(frozen, 'submit_diagnostic_answer', question)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM pet_media_binding WHERE kind='speaking'").fetchone()[0] == 0


def test_late_recognition_restart_and_replay_never_borrow_current_focus(environment, monkeypatch):
    settings, db = environment
    gateway = planner()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, focus = started(client, settings, db, monkeypatch)
        def recognize(self, wav, **kwargs):
            with db.connect() as c:
                c.execute("UPDATE companion_activity SET status='completed' WHERE activity_id=?", (focus['activity_id'],))
                c.commit()
            return '五', 1000, 'synthetic-recognition', False
        monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
        body = payload(settings, focus)
        response = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert response.status_code == 200, response.text
        original = response.json()['capture_binding']
        assert original['activity_reference'] == focus
        current = client.get(f'/v1/sessions/{sid}/voice/activity-focus')
        assert current.status_code == 200 and current.json()['activity_focus'] is None
        stale = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings, focus, 'later-old-focus'))
        assert stale.status_code == 200, stale.text
        assert stale.json()['capture_binding']['activity_validation'] == 'stale'
        assert stale.json()['capture_binding']['activity_reference'] == focus
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert replay.status_code == 200 and replay.json()['capture_binding'] == original
        changed = payload(settings, {**focus, 'version': 'changed'})
        assert client.post(f'/v1/sessions/{sid}/asr', json=changed).status_code == 409


def test_focus_api_and_foreign_or_invalid_capture_have_no_side_effect(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        sid, focus = started(client, settings, db, monkeypatch)
        view = client.get(f'/v1/sessions/{sid}/voice/activity-focus')
        assert view.status_code == 200, view.text
        assert view.json() == {'session_id': sid, 'child_id': CHILD, 'activity_focus': focus,
                              'local_only': True, 'cloud_transmission_allowed': False}
        calls = []
        monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: calls.append('unexpected'))
        for index, candidate in enumerate([{}, {**focus, 'extra': 'not allowed'}, {**focus, 'version': []}]):
            result = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings, candidate, f'invalid-activity-{index}'))
            assert result.status_code in {409, 422}, result.text
        with db.connect() as c:
            c.execute('UPDATE companion_activity SET child_id=? WHERE activity_id=?', (OTHER, focus['activity_id']))
            c.commit()
        result = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings, focus, 'foreign-activity-focus'))
        assert result.status_code == 409, result.text
        assert not calls
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM voice_capture_binding WHERE client_action_id LIKE 'invalid-activity-%' OR client_action_id='foreign-activity-focus'").fetchone()[0] == 0
