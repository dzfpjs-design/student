from io import BytesIO
from pathlib import Path
import base64
from dataclasses import replace
import hashlib
import json
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.cloud_asr import CloudAsrError, CloudAsrResult
from home_study_agent.local_media_ai import InvalidMediaInputError, LocalMediaAI, LocalCapabilityNotReadyError, voice_activity_evidence
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
from home_study_agent.voice_capture_binding import resolve_voice_capture
from test_agent_continuation import Gateway
from test_cloud_asr import FakeCloudAsr
from test_cloud_asr_audio_permission import consent_db, set_permission
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_content_free_voice_control_http import ControlGateway


FIXTURES = next(p for p in Path(__file__).resolve().parents if (p / 'services/local_agent/tests/fixtures').is_dir()) / 'services/local_agent/tests/fixtures'


def pcm_wav(samples, rate=16000):
    output = BytesIO()
    with wave.open(output, 'wb') as source:
        source.setnchannels(1)
        source.setsampwidth(2)
        source.setframerate(rate)
        source.writeframes(np.clip(samples * 32768, -32768, 32767).astype(np.int16).tobytes())
    return output.getvalue()


@pytest.mark.parametrize('name', ['short-mandarin-dui.wav', 'short-mandarin-dui-trimmed.wav'])
def test_complete_synthetic_short_word_reaches_cloud_through_real_vad(tmp_path, name):
    wav = (FIXTURES / name).read_bytes()
    with wave.open(BytesIO(wav), 'rb') as source:
        duration = round(source.getnframes() / source.getframerate() * 1000)
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        evidence = voice_activity_evidence(samples, source.getframerate())
    assert evidence['human_voice_detected'] is True
    gateway = FakeCloudAsr(CloudAsrResult('对', 'controlled-asr-port', 1))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)
    text, elapsed, model, cloud = media._recognize_wav(wav, prefer_cloud=True, purpose='agent_command')
    assert (text, elapsed, model, cloud) == ('对', duration, 'cloud:qwen:controlled-asr-port', True)
    assert gateway.calls == 1


@pytest.mark.parametrize('noise', ['silence', 'keyboard', 'impact', 'white_noise', 'fan'])
def test_noise_never_reaches_cloud_asr_after_short_word_gate_change(tmp_path, noise):
    rate = 16000
    samples = np.zeros(rate * 2, dtype=np.float32)
    if noise == 'keyboard':
        for start in range(1000, len(samples), 1600):
            samples[start:start + 24] = np.linspace(.9, 0, 24)
    elif noise == 'impact':
        samples[1000:2400] = np.random.default_rng(41).normal(0, .2, 1400) * np.linspace(1, 0, 1400)
    elif noise == 'white_noise':
        samples = np.random.default_rng(9).normal(0, .03, len(samples)).astype(np.float32)
    elif noise == 'fan':
        samples = (.03 * np.sin(np.arange(len(samples)) * 2 * np.pi * 200 / rate)).astype(np.float32)
    gateway = FakeCloudAsr(CloudAsrResult('不应生成的语义', 'controlled-asr-port', 1))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)
    with pytest.raises(LocalCapabilityNotReadyError) as rejected:
        media._recognize_wav(pcm_wav(samples), prefer_cloud=True, purpose='agent_command')
    assert rejected.value.code == 'ASR_NO_HUMAN_SPEECH'
    assert gateway.calls == 0


@pytest.mark.parametrize('milliseconds', [0, 30, 90, 119])
def test_incomplete_audio_never_reaches_cloud(tmp_path, milliseconds):
    gateway = FakeCloudAsr(CloudAsrResult('不应转写', 'controlled-asr-port', 1))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)
    with pytest.raises(InvalidMediaInputError):
        media._recognize_wav(pcm_wav(np.zeros(milliseconds * 16)), prefer_cloud=True, purpose='agent_command')
    assert gateway.calls == 0


@pytest.mark.parametrize('transcript', ['嗯', '嗯嗯', '哦？', '啊'])
@pytest.mark.parametrize('purpose', ['agent_command', 'pet_speaking', 'wake_word_setup'])
def test_dialogue_marker_keeps_original_cloud_transcript_without_local_semantic_decision(tmp_path, transcript, purpose):
    gateway = FakeCloudAsr(CloudAsrResult(transcript, 'controlled-asr-port', 1))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)
    result = media._recognize_wav((FIXTURES / 'short-mandarin-dui-trimmed.wav').read_bytes(),
        prefer_cloud=True, purpose=purpose)
    assert result[0] == transcript and result[3] is True
    assert gateway.calls == 1


@pytest.mark.parametrize('transcript', ['嗯', '嗯嗯', '哦？', '啊', '', '，。'])
def test_wake_standby_keeps_non_dialogue_noise_filter(tmp_path, monkeypatch, transcript):
    gateway = FakeCloudAsr(CloudAsrResult(transcript, 'controlled-asr-port', 1))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)
    monkeypatch.setattr(media, '_asr_files', lambda _: {})
    monkeypatch.setattr(media, '_recognize_locally', lambda *_: transcript)
    with pytest.raises(LocalCapabilityNotReadyError) as rejected:
        media._recognize_wav((FIXTURES / 'short-mandarin-dui-trimmed.wav').read_bytes(),
            prefer_cloud=True, purpose='wake_word')
    assert rejected.value.code == 'ASR_NO_MEANINGFUL_SPEECH'
    assert gateway.calls == 0


@pytest.mark.parametrize('transcript', ['', '  ', '，。！？'])
def test_empty_cloud_dialogue_never_creates_a_turn(tmp_path, transcript):
    gateway = FakeCloudAsr(CloudAsrResult(transcript, 'controlled-asr-port', 1))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)
    with pytest.raises(LocalCapabilityNotReadyError):
        media._recognize_wav((FIXTURES / 'short-mandarin-dui-trimmed.wav').read_bytes(),
            prefer_cloud=True, purpose='agent_command')


@pytest.mark.parametrize('purpose', ['agent_command', 'pet_speaking', 'wake_word'])
def test_local_recovery_transcript_obeys_the_same_contextual_admission(tmp_path, monkeypatch, purpose):
    media = LocalMediaAI(None, tmp_path, tmp_path)
    monkeypatch.setattr(media, '_asr_files', lambda _: {})
    monkeypatch.setattr(media, '_recognize_locally', lambda *_: '嗯')
    wav = (FIXTURES / 'short-mandarin-dui-trimmed.wav').read_bytes()
    if purpose == 'wake_word':
        with pytest.raises(LocalCapabilityNotReadyError) as rejected:
            media._recognize_wav(wav, purpose=purpose)
        assert rejected.value.code == 'ASR_NO_MEANINGFUL_SPEECH'
    else:
        result = media._recognize_wav(wav, purpose=purpose)
        assert result[0] == '嗯' and result[3] is False


@pytest.mark.parametrize('transcript', ['嗯', '嗯嗯', '哦？', '啊'])
def test_dialogue_marker_http_runtime_and_restart_preserve_voice_without_scoring(environment, transcript):
    settings, db = environment
    db.initialize()
    set_permission(db, 1)
    model = ControlGateway(transcript, None, role='acknowledgement')
    recognizer = FakeCloudAsr(CloudAsrResult(transcript, 'controlled-asr-port', 1))
    def application(gateway):
        app = create_app(settings, model_gateway=model)
        app.state.media_ai._cloud_asr = gateway
        return app
    with TestClient(application(recognizer)) as client:
        sid = start(client)
        prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'school', 'preferred_item_id': 'diag-p4-arithmetic-closed-1',
            'client_action_id': 'prepare-dialogue-marker'})
        assert prepared.status_code == 200, prepared.text
        prompt = prepared.json()['prompt']
        audio = {'wav_base64': base64.b64encode((FIXTURES / 'short-mandarin-dui-trimmed.wav').read_bytes()).decode(),
            'purpose': 'agent_command', 'client_action_id': 'dialogue-marker-voice',
            'capture_reference': {'child_id': CHILD,
                'question_reference': {key: prompt[key] for key in ('prompt_id', 'item_version')}}}
        response = client.post(f'/v1/sessions/{sid}/asr', json=audio)
        assert response.status_code == 200, response.text
        voice = response.json()
        assert voice['transcript'] == transcript and voice['duration_ms'] == 195
        request = {'transcript': transcript, 'voice_input_id': voice['voice_input_id'],
            'cloud_text_allowed': True, 'client_action_id': 'dialogue-marker-turn'}
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json=request)
        assert turn.status_code == 200, turn.text
        heard = turn.json()
        assert heard['action']['type'] == 'none'
        assert heard['action']['voice_input_id'] == voice['voice_input_id']
        executed = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
    recovered = FakeCloudAsr(CloudAsrResult('不得重识别', 'controlled-asr-port', 1))
    calls_before = len(model.requests)
    with TestClient(application(recovered)) as client:
        retry = client.post(f'/v1/sessions/{sid}/asr', json=audio)
        assert retry.status_code == 200, retry.text
        assert retry.json()['voice_input_id'] == voice['voice_input_id']
        assert retry.json()['transcript'] == transcript
        replay = client.post(f'/v1/sessions/{sid}/agent-turn', json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()['agent_turn_id'] == heard['agent_turn_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',
                (prompt['prompt_id'],)).fetchone()[0] == 'waiting_answer'
    assert recognizer.calls == 1 and recovered.calls == 0
    assert len(model.requests) == calls_before


def isolated_application(settings, recognizer):
    model = Gateway({})
    model.configured = False
    application = create_app(settings, model_gateway=model)
    application.state.media_ai._cloud_asr = recognizer
    return application


@pytest.mark.parametrize('retain', [False, True])
def test_short_word_actual_http_vad_audit_retention_and_restart(environment, retain):
    settings, db = environment
    db.initialize()
    set_permission(db, 1)
    settings = replace(settings, data_encryption_key=b'S' * 32)
    wav = (FIXTURES / 'short-mandarin-dui-trimmed.wav').read_bytes()
    gateway = FakeCloudAsr(CloudAsrResult('对', 'controlled-asr-port', 1))
    with TestClient(isolated_application(settings, gateway)) as client:
        sid = start(client)
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        privacy = client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': retain})
        assert privacy.status_code == 200, privacy.text
        prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': 'diag-pet-speaking-2',
            'client_action_id': 'prepare-short-word-original'})
        assert prepared.status_code == 200, prepared.text
        prompt = prepared.json()['prompt']
        payload = {'wav_base64': base64.b64encode(wav).decode(), 'purpose': 'agent_command',
            'client_action_id': 'short-word-original', 'capture_reference': {'child_id': CHILD,
                'question_reference': {k: prompt[k] for k in ('prompt_id', 'item_version')}}}
        response = client.post(f'/v1/sessions/{sid}/asr', json=payload)
        assert response.status_code == 200, response.text
        voice = response.json()
        assert voice['transcript'] == '对' and voice['duration_ms'] == 195
        assert voice['capture_binding']['prompt_id'] == prompt['prompt_id']
        # A short conversational acknowledgement is not a qualifying PET sample.
        # Keep its original source, but do not relax the separate audio skill gate.
        assert voice['capture_binding']['speaking_media_status'] == ('invalidated' if retain else 'raw_audio_not_retained')
        assert bool(voice['local_path']) is retain
        conflict = client.post(f'/v1/sessions/{sid}/asr', json={**payload,
            'capture_reference': {'child_id': OTHER}})
        assert conflict.status_code == 409, conflict.text
    recovered_gateway = FakeCloudAsr(CloudAsrResult('不得重复识别', 'controlled-asr-port', 1))
    with TestClient(isolated_application(settings, recovered_gateway)) as client:
        retry = client.post(f'/v1/sessions/{sid}/asr', json=payload)
        assert retry.status_code == 200, retry.text
        assert retry.json()['voice_input_id'] == voice['voice_input_id']
        assert retry.json()['duration_ms'] == 195
        with db.connect() as c:
            binding = resolve_voice_capture(c, session_id=sid, child_id=CHILD,
                voice_input_id=voice['voice_input_id'], transcript='对')
            assert binding['raw_audio_available'] is retain
            capture = c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?',
                (voice['voice_input_id'],)).fetchone()
            assert capture['wav_sha256'] == hashlib.sha256(wav).hexdigest()
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 0
            calls = c.execute('SELECT * FROM model_call').fetchall()
            assert len(calls) == 1 and calls[0]['status'] == 'completed'
            manifest = json.loads(calls[0]['context_manifest_json'])
            assert manifest['duration_ms'] == 195
            assert manifest['speech_admission'] == {
                'admission_policy': 'webrtc-sustained-short-v2', 'speech_frames': 5,
                'longest_speech_run': 5, 'frame_ms': 30,
                'minimum_speech_frames': 4, 'minimum_contiguous_frames': 3}
            assert 'base64' not in calls[0]['context_manifest_json']
            assert 'transcript' not in manifest and 'text' not in manifest
            media_id = c.execute('SELECT media_id FROM pet_media_binding WHERE voice_input_id=?',
                (voice['voice_input_id'],)).fetchone()
            assert bool(media_id) is retain
        if retain:
            media = PetMediaEvidenceService(db, media_root=settings.data_dir,
                data_encryption_key=b'S' * 32).read(sid, media_id=media_id[0])
            assert media['audio']['sha256'] == hashlib.sha256(wav).hexdigest()
            assert media['audio']['status'] == 'unavailable_or_unsupported_audio'
            assert media['qualification']['skill_qualified'] is False
    assert gateway.calls == 1 and recovered_gateway.calls == 0


@pytest.mark.parametrize('kind', ['silence', 'cloud_failure'])
def test_rejected_short_capture_http_has_no_answer_or_local_guess(environment, kind):
    settings, db = environment
    db.initialize()
    set_permission(db, 1)
    result = CloudAsrError('CLOUD_ASR_UNAVAILABLE') if kind == 'cloud_failure' else CloudAsrResult('不应转写', 'controlled-asr-port', 1)
    gateway = FakeCloudAsr(result)
    wav = ((FIXTURES / 'short-mandarin-dui-trimmed.wav').read_bytes()
        if kind == 'cloud_failure' else pcm_wav(np.zeros(3200)))
    with TestClient(isolated_application(settings, gateway)) as client:
        sid = start(client)
        response = client.post(f'/v1/sessions/{sid}/asr', json={
            'wav_base64': base64.b64encode(wav).decode(), 'purpose': 'agent_command',
            'client_action_id': 'reject-short-word', 'capture_reference': {'child_id': CHILD}})
        assert response.status_code != 200, response.text
        assert response.json()['code'] == ('ASR_CLOUD_DIALOGUE_UNAVAILABLE' if kind == 'cloud_failure' else 'ASR_NO_HUMAN_SPEECH')
        with db.connect() as c:
            for table in ('voice_input', 'diagnostic_attempt', 'agent_turn', 'pet_media_binding'):
                assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
            calls = c.execute('SELECT status FROM model_call').fetchall()
            assert [row[0] for row in calls] == (['failed'] if kind == 'cloud_failure' else [])
    assert gateway.calls == int(kind == 'cloud_failure')
