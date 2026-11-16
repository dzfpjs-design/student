"""Real ASR metadata, local orchestration and actual answer transaction boundaries."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_agent_continuation import Gateway
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_pet_media_evidence import wav_file


def prepare(client, sid, key='question', item='diag-p4-arithmetic-closed-1'):
    response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
        'track': 'school', 'preferred_item_id': item, 'client_action_id': key})
    assert response.status_code == 200, response.text
    return response.json()['prompt']


def capture(client, settings, monkeypatch, sid, text, prompt=None, key='capture-voice', *, child=CHILD):
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (text, 1000, 'fixture', False))
    audio = settings.data_dir / 'synthetic-runtime.wav'
    wav_file(audio)
    reference = {'child_id': child}
    if prompt:
        reference['question_reference'] = prompt if 'kind' in prompt else {k: prompt[k] for k in ('prompt_id', 'item_version')}
    response = client.post(f'/v1/sessions/{sid}/asr', json={
        'wav_base64': base64.b64encode(audio.read_bytes()).decode(), 'purpose': 'agent_command',
        'client_action_id': key, 'capture_reference': reference})
    assert response.status_code == 200, response.text
    return response.json()['voice_input_id']


def interpret(client, sid, text, key, voice_id):
    result = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'client_action_id': key, 'cloud_text_allowed': True, 'voice_input_id': voice_id})
    assert result.status_code == 200, result.text
    return result.json()


def submit(client, sid, prompt, heard, answer='90', key='answer-voice'):
    return client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
        'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
        'answer': answer, 'client_action_id': key, 'authorized_turn_id': heard['agent_turn_id']})


def test_real_asr_without_raw_can_score_text_and_replay_original_voice_after_restart(environment, monkeypatch):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        prompt = prepare(client, sid)
        voice = capture(client, settings, monkeypatch, sid, '我的答案是90', prompt)
        heard = interpret(client, sid, '我的答案是90', 'spoken-answer', voice)
        assert heard['action']['source_voice_input_id'] == voice
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            resolved = AgentRuntimeStore.resolve_local_answer_submission(c, heard['agent_turn_id'], 'diagnostic.submit')
            assert resolved['source_voice_input_id'] == voice
            assert resolved['raw_audio_available'] is False
        result = submit(client, sid, prompt, heard)
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'answered'
        assert client.post(f'/v1/sessions/{sid}/asr/actions/capture-voice/cancel').status_code == 200
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, '我的答案是90', 'spoken-answer', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        assert replay['action']['source_voice_input_id'] == voice
        assert submit(client, sid, prompt, replay).json()['status'] == 'answered'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
    assert not gateway.requests


@pytest.mark.parametrize('original_prompt', [True, False])
def test_late_asr_never_borrows_new_question(environment, monkeypatch, original_prompt):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        original = prepare(client, sid) if original_prompt else None
        voice = capture(client, settings, monkeypatch, sid, '90', original)
        if original:
            DiagnosticPromptService(db).cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
        current = prepare(client, sid, 'replacement', 'diag-p4-arithmetic-closed-2')
        heard = interpret(client, sid, '90', 'late-transcription', voice)
        assert heard['action']['type'] == 'none'
        assert heard['intent'] == 'pending_answer_clarification'
        assert 'pending_answer_candidate' not in heard['action']
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (current['prompt_id'],)).fetchone()[0] == 'waiting_answer'
    assert not gateway.requests


@pytest.mark.parametrize('revoke', [None, 'capture-voice', 'confirmation-audio'])
def test_role_confirmation_keeps_original_recording_not_confirmation_audio(environment, monkeypatch, revoke):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Gateway({}))) as client:
        sid = start(client)
        prompt = prepare(client, sid)
        voice = capture(client, settings, monkeypatch, sid, '九十是我的计算结果', prompt)
        candidate = interpret(client, sid, '九十是我的计算结果', 'candidate', voice)
        assert candidate['action']['source'] == 'pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{candidate['agent_turn_id']}/execute").status_code == 200
    with TestClient(create_app(settings, model_gateway=Gateway({}))) as client:
        confirmation_voice = capture(client, settings, monkeypatch, sid, '刚才那句就是我的答案', prompt, 'confirmation-audio')
        heard = interpret(client, sid, '刚才那句就是我的答案', 'confirmation', confirmation_voice)
        assert heard['action']['source_voice_input_id'] == voice
        assert heard['action']['voice_input_id'] == confirmation_voice
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source = AgentRuntimeStore.resolve_local_answer_submission(c, heard['agent_turn_id'], 'diagnostic.submit')
            assert source['answer'] == '九十是我的计算结果'
            assert source['source_voice_input_id'] == voice
        if revoke:
            assert client.post(f'/v1/sessions/{sid}/asr/actions/{revoke}/cancel').status_code == 200
        result = submit(client, sid, prompt, heard, answer='九十是我的计算结果')
        if revoke:
            assert result.status_code == 409, result.text
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        else:
            assert result.status_code == 200, result.text
            assert result.json()['status'] == 'answered'


def test_capture_revoked_between_planning_and_actual_score_is_not_authority(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Gateway({}))) as client:
        sid = start(client)
        prompt = prepare(client, sid)
        voice = capture(client, settings, monkeypatch, sid, '90', prompt)
        heard = interpret(client, sid, '90', 'answer-before-revoke', voice)
        assert client.post(f'/v1/sessions/{sid}/asr/actions/capture-voice/cancel').status_code == 200
        result = submit(client, sid, prompt, heard)
        assert result.status_code == 409, result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_changed_transcript_or_owner_rejected_before_arrival(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Gateway({}))) as client:
        sid = start(client)
        prompt = prepare(client, sid)
        voice = capture(client, settings, monkeypatch, sid, 'a part', prompt)
        changed = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': 'apart', 'client_action_id': 'changed-text', 'voice_input_id': voice})
        assert changed.status_code == 409, changed.text
        assert 'a part' not in changed.text and 'apart' not in changed.text
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': OTHER, 'client_action_id': 'switch-capture-owner'})
        assert switched.status_code == 200, switched.text
        other = switched.json()['session_id']
        changed = client.post(f'/v1/sessions/{other}/agent-turn', json={
            'transcript': 'a part', 'client_action_id': 'changed-owner', 'voice_input_id': voice})
        assert changed.status_code == 409, changed.text
        assert 'a part' not in changed.text
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM learning_input_arrival WHERE client_action_id IN ('changed-text','changed-owner')").fetchone()[0] == 0


def test_unbound_ordinary_conversation_can_use_cloud_without_capture_metadata(environment, monkeypatch):
    settings, db = environment
    text = '能帮我看看今天有哪些安排吗'
    gateway = Planner('planning.describe', {}, text)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        voice = capture(client, settings, monkeypatch, sid, text)
        heard = interpret(client, sid, text, 'ordinary-voice-query', voice)
        assert heard['action']['voice_input_id'] == voice
        assert len(gateway.requests) == 1
        exported = str(gateway.requests)
        for private in (voice, 'voice_input_binding', 'source_voice_input_id', 'original_transcript', 'raw_audio_available'):
            assert private not in exported
        with db.connect() as c:
            manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                           (heard['agent_turn_id'],)).fetchone()[0])
            assert manifest['voice_input_binding']['voice_input_id'] == voice
            assert 'original_transcript' not in manifest['voice_input_binding']


def test_exam_capture_without_exam_question_reference_does_not_guess_active_item(environment, monkeypatch):
    from test_answer_same_turn_correction import open_exam
    settings, db = environment
    gateway = Planner('exam.submit', {'answer': 'A'}, 'A', role='answer')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, _ = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, 'A')
        heard = interpret(client, sid, 'A', 'unbound-exam-answer', voice)
        assert heard['action']['type'] == 'none'
        assert 'pending_answer_candidate' not in heard['action']
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert not gateway.requests


def test_local_playback_clarification_keeps_capture_identity_on_http_replay(environment, monkeypatch):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': 'diag-pet-listening-1', 'client_action_id': 'listening-question'})
        assert response.status_code == 200, response.text
        voice = capture(client, settings, monkeypatch, sid, '继续听题', response.json()['prompt'])
        heard = interpret(client, sid, '继续听题', 'local-playback-command', voice)
        assert heard['action']['voice_input_id'] == voice
        assert heard['action']['type'] == 'none'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, '继续听题', 'local-playback-command', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
    assert not gateway.requests
