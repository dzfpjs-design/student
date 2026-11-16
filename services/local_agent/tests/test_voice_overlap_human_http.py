"""Synthetic microphone captures prove HTTP retention, not acoustic diarization."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.companion_memory import CompanionMemory
from test_companion_activity_host import NoCloud
from test_diagnostic_prompt_http import environment, start
from test_voice_runtime_binding import capture, prepare, interpret, submit
from test_pet_media_evidence import media, begin, review, control


def overlapping_turn(client, sid, voice, text, key):
    response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'voice_input_id': voice, 'client_action_id': key,
        'playback_overlap_id': 'overlapping-playback', 'cloud_text_allowed': False})
    assert response.status_code == 200, response.text
    return response.json()


def register(client, sid, text):
    result = client.post(f'/v1/sessions/{sid}/voice-playback', json={
        'playback_id': 'overlapping-playback', 'text': text, 'event': 'started'})
    assert result.status_code == 200, result.text


def test_human_repetition_with_saved_voice_is_retained_and_clarified_not_silenced(environment, monkeypatch):
    settings, db = environment
    spoken = '我们先把这一步按顺序算完，再仔细核对每一个条件。'
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        sid = start(client)
        prompt = prepare(client, sid)
        register(client, sid, spoken)
        voice = capture(client, settings, monkeypatch, sid, spoken, prompt)
        heard = overlapping_turn(client, sid, voice, spoken, 'human-repetition')
        assert heard['transcript'] == spoken and heard['action']['voice_input_id'] == voice
        assert heard['action'].get('suppress_speech') is not True
        assert heard['action']['source'] == 'playback_overlap_clarification'
        assert heard['response_text']
        completed = client.put(f"/v1/agent-turns/{heard['agent_turn_id']}/outcome", json={
            'final_response': heard['response_text'], 'tool_status': 'completed'})
        assert completed.status_code == 200, completed.text
        rejected = submit(client, sid, prompt, heard, answer=spoken, key='ambiguous-echo-answer')
        assert rejected.status_code == 409, rejected.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (voice,)).fetchone()[0] == spoken
            saved = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (heard['agent_turn_id'],)).fetchone()
            assert saved['tool_status'] == 'completed'
            assert not CompanionMemory._eligible(saved)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        replay = overlapping_turn(client, sid, voice, spoken, 'human-repetition')
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        fresh_voice = capture(client, settings, monkeypatch, sid, '我的答案是90', prompt, 'fresh-answer-voice')
        fresh = interpret(client, sid, '我的答案是90', 'fresh-human-answer', fresh_voice)
        result = submit(client, sid, prompt, fresh)
        assert result.status_code == 200 and result.json()['status'] == 'answered', result.text


def test_human_pause_during_pet_playback_reaches_real_control_http(media, monkeypatch):
    db, _, sid, prompt, _, _ = media
    control(media, review(media, begin(media)))
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        register(client, sid, '暂停')
        voice = capture(client, settings, monkeypatch, sid, '暂停', prompt)
        heard = overlapping_turn(client, sid, voice, '暂停', 'human-pet-pause')
        assert heard['action']['type'] == 'control_pet_listening'
        assert heard['action']['command'] == 'pause'
        assert heard['action']['voice_input_id'] == voice
        result = client.post(f'/v1/sessions/{sid}/pet-media/listening/agent-control', json={'agent_turn_id': heard['agent_turn_id']})
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM pet_media_event WHERE operation='control' AND json_extract(payload_json,'$.command')='pause'").fetchone()[0] >= 1
