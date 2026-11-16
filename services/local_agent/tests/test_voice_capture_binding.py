"""Actual ASR host path with synthetic PCM and a controlled recognition port."""
import base64
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.voice_capture_binding import resolve_voice_capture, VoiceCaptureBindingError
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_pet_media_evidence import wav_file
from test_agent_continuation import Gateway


@pytest.mark.parametrize('isolation', [{'pending_evaluation_turn_id': 'original-evaluation'},
                                     {'local_recovery_only': True}], ids=['pending', 'local_recovery'])
@pytest.mark.parametrize('kind,action,binding', [
    ('diagnostic', 'submit_diagnostic_answer', {'prompt_id': 'p', 'item_version': 'v'}),
    ('exam', 'submit_exam_answer', {'run_id': 'r', 'position': 0, 'item_id': 'i', 'source_version': 'v'}),
    ('vocabulary', 'submit_pet_vocabulary_answer', {'prompt_session_id': 'p', 'card_version': 'v'}),
    ('mistake', 'continue_mistake_dialogue', {'session_id': 's', 'child_id': CHILD,
        'mistake_id': 'm', 'phase': 'check', 'item_id': 'i', 'source_version': 'v'}),
])
def test_pending_control_capture_cannot_satisfy_any_answer_gate(kind, action, binding, isolation):
    from home_study_agent.agent_runtime import AgentRuntimeStore
    from home_study_agent.voice_capture_binding import normalize_question_reference
    capture = {'voice_input_id': 'control-voice', 'child_id': CHILD, 'session_id': 's',
               'question_reference': normalize_question_reference({'kind': kind, 'binding': binding}),
               'question_validation': 'valid', 'status': 'completed'}
    assert AgentRuntimeStore.capture_matches_answer(capture, action, binding)
    capture.update(isolation)
    assert not AgentRuntimeStore.capture_matches_answer(capture, action, binding)
    snapshot = AgentRuntimeStore.voice_capture_snapshot(capture)
    assert all(snapshot[key] == value for key, value in isolation.items())
    assert snapshot['question_reference'] == capture['question_reference']
    assert not AgentRuntimeStore.capture_matches_answer(snapshot, action, binding)


@pytest.mark.parametrize('reference', [
    [], {'kind': [], 'binding': {}},
    {'kind': 'exam', 'binding': {'run_id': 'run', 'position': True, 'item_id': 'item', 'source_version': 'v1'}},
    {'kind': 'exam', 'binding': {'run_id': 'run', 'position': -1, 'item_id': 'item', 'source_version': 'v1'}},
    {'kind': 'diagnostic', 'binding': {'prompt_id': 'prompt', 'item_version': 'v1', 'run_id': 'run'}},
    {'kind': 'diagnostic', 'binding': {'prompt_id': ' ', 'item_version': 'v1'}},
    {'kind': 'vocabulary', 'binding': {'prompt_session_id': 'prompt', 'card_version': 1}},
    {'kind': 'diagnostic', 'binding': {'prompt_id': 'prompt', 'item_version': 'v1'}, 'trusted': True},
])
def test_malformed_typed_question_is_rejected_before_any_capture(reference):
    from home_study_agent.voice_capture_binding import normalize_question_reference
    with pytest.raises(VoiceCaptureBindingError, match='VOICE_CAPTURE_QUESTION_INVALID'):
        normalize_question_reference(reference)


def test_typed_question_is_a_detached_exact_binding_and_legacy_remains_compatible():
    from home_study_agent.voice_capture_binding import normalize_question_reference
    source = {'kind': 'exam', 'binding': {'run_id': 'run', 'position': 1, 'item_id': 'item', 'source_version': 'v1'}}
    normalized = normalize_question_reference(source)
    source['binding']['item_id'] = 'next-item'
    assert normalized['binding']['item_id'] == 'item'
    assert normalize_question_reference({'prompt_id': 'p', 'item_version': 'v'}) == {
        'kind': 'diagnostic', 'binding': {'prompt_id': 'p', 'item_version': 'v'}}
    assert normalize_question_reference(None) is None


def test_actual_asr_rejects_mixed_question_before_recognition_or_persistence(environment, monkeypatch):
    settings, db = environment
    calls = []
    def recognize(self, wav, **kwargs):
        calls.append(kwargs)
        raise AssertionError('Malformed ownership must never reach recognition')
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
    audio = settings.data_dir / 'invalid-reference.wav'
    wav_file(audio)
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        for index, question in enumerate([
            {'kind': [], 'binding': {}},
            {'kind': 'exam', 'binding': {'run_id': 'run', 'position': True, 'item_id': 'item', 'source_version': 'v1'}},
            {'kind': 'diagnostic', 'binding': {'prompt_id': 'prompt', 'item_version': 'v1', 'run_id': 'run'}},
        ]):
            response = client.post(f'/v1/sessions/{sid}/asr', json={
                'wav_base64': base64.b64encode(audio.read_bytes()).decode(), 'purpose': 'agent_command',
                'client_action_id': f'malformed-question-{index}', 'capture_reference': {
                    'child_id': CHILD, 'question_reference': question}})
            assert response.status_code == 409, response.text
            assert response.json()['code'] == 'VOICE_CAPTURE_QUESTION_INVALID'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_capture_binding').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert not calls


@pytest.mark.parametrize('retain', [False, True])
def test_actual_asr_binds_original_speaking_prompt_without_new_retention(environment, monkeypatch, retain):
    settings, db = environment
    settings = replace(settings, data_encryption_key=b'V'*32)
    gateway = Gateway({}); gateway.configured = False
    text = 'I like reading in the library because it is quiet.'
    calls = []
    def recognize(self, wav, **kwargs):
        calls.append(kwargs['purpose'])
        return text, 1000, 'synthetic-recognition-port', False
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
    audio = settings.data_dir / 'public-fixture.wav'
    wav_file(audio)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': retain}).status_code == 200
        result = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': 'diag-pet-speaking-2', 'client_action_id': 'capture-speaking-prompt'})
        assert result.status_code == 200, result.text
        prompt = result.json()['prompt']
        payload = {'wav_base64': base64.b64encode(audio.read_bytes()).decode(), 'purpose': 'agent_command',
            'client_action_id': 'original-captured-voice', 'capture_reference': {'child_id': CHILD,
            'question_reference': {k: prompt[k] for k in ('prompt_id', 'item_version')}}}
        original = client.post(f'/v1/sessions/{sid}/asr', json=payload)
        assert original.status_code == 200, original.text
        voice = original.json()
        assert voice['capture_binding']['prompt_id'] == prompt['prompt_id']
        assert voice['capture_binding']['speaking_media_status'] == ('bound' if retain else 'raw_audio_not_retained')
        assert bool(voice['local_path']) == retain
        with db.connect() as c:
            binding = resolve_voice_capture(c, session_id=sid, child_id=CHILD, voice_input_id=voice['voice_input_id'], transcript=text)
            assert binding['raw_audio_available'] == retain
            assert c.execute('SELECT COUNT(*) FROM pet_media_binding WHERE kind="speaking"').fetchone()[0] == int(retain)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert client.post(f'/v1/sessions/{sid}/asr', json={**payload, 'capture_reference': {'child_id': OTHER}}).status_code == 409
        changed = base64.b64encode(audio.read_bytes() + b'x').decode()
        assert client.post(f'/v1/sessions/{sid}/asr', json={**payload, 'wav_base64': changed}).status_code == 409
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        retry = client.post(f'/v1/sessions/{sid}/asr', json=payload)
        assert retry.status_code == 200, retry.text
        assert retry.json()['voice_input_id'] == voice['voice_input_id']
        assert calls == ['pet_speaking']
        assert client.post(f'/v1/sessions/{sid}/asr/actions/original-captured-voice/cancel').status_code == 200
        assert client.post(f'/v1/sessions/{sid}/asr', json=payload).status_code == 409
    assert not gateway.requests


def test_voice_binding_does_not_join_english_words_or_accept_changed_text(environment, monkeypatch):
    settings, db = environment
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *args, **kwargs: ('a part', 1000, 'fixture', False))
    path = settings.data_dir / 'fixture.wav'; wav_file(path)
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        response = client.post(f'/v1/sessions/{sid}/asr', json={'wav_base64': base64.b64encode(path.read_bytes()).decode(),
            'purpose': 'agent_command', 'client_action_id': 'separate-words-asr', 'capture_reference': {'child_id': CHILD}})
        assert response.status_code == 200, response.text
        with db.connect() as c:
            args = dict(session_id=sid, child_id=CHILD, voice_input_id=response.json()['voice_input_id'])
            assert resolve_voice_capture(c, **args, transcript=' a  part ')['original_transcript'] == 'a part'
            with pytest.raises(VoiceCaptureBindingError, match='TRANSCRIPT_CHANGED'):
                resolve_voice_capture(c, **args, transcript='apart')


@pytest.mark.parametrize('change', ['child_switch', 'revoke_retention', 'cancel_capture'])
def test_asr_publication_rechecks_changes_during_recognition(environment, monkeypatch, change):
    settings, db = environment
    settings = replace(settings, data_encryption_key=b'V' * 32)
    action = 'publication-race-' + change
    def recognize(self, wav, **kwargs):
        with db.connect() as c:
            if change == 'child_switch':
                c.execute('UPDATE daily_session SET child_id=? WHERE session_id=?', (OTHER, kwargs['session']['session_id']))
            elif change == 'revoke_retention':
                c.execute('UPDATE privacy_preference SET raw_media_storage_allowed=0 WHERE singleton_id=1')
            else:
                c.execute("UPDATE voice_capture_binding SET status='cancelled' WHERE client_action_id=?", (action,))
            c.commit()
        return 'This late answer must not cross its original boundary.', 1000, 'fixture', False
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
    path = settings.data_dir / 'fixture.wav'
    wav_file(path)
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True}).status_code == 200
        response = client.post(f'/v1/sessions/{sid}/asr', json={
            'wav_base64': base64.b64encode(path.read_bytes()).decode(), 'purpose': 'agent_command',
            'client_action_id': action, 'capture_reference': {'child_id': CHILD}})
        assert response.status_code == {'child_switch': 422, 'cancel_capture': 409, 'revoke_retention': 200}[change], response.text
        with db.connect() as c:
            voices = c.execute('SELECT * FROM voice_input WHERE client_action_id=?', (action,)).fetchall()
            if change == 'revoke_retention':
                assert len(voices) == 1 and voices[0]['local_path'] is None
                assert response.json()['capture_binding']['raw_audio_available'] is False
            else:
                assert not voices
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert not list((settings.data_dir / 'media').rglob('*.enc'))


def test_actual_media_deletion_during_asr_does_not_recreate_a_late_transcript(environment, monkeypatch):
    settings, db = environment
    settings = replace(settings, data_encryption_key=b'V' * 32)
    deletion_receipts = []
    def recognize(self, wav, **kwargs):
        deleted = client.post(f'/v1/parent/children/{CHILD}/delete', json={
            'scope': 'media', 'confirmation': '确认删除', 'client_action_id': 'delete-during-recognition'})
        assert deleted.status_code == 200, deleted.text
        deletion_receipts.append(deleted.json())
        return 'A private sentence that arrived after deletion.', 1000, 'fixture', False
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
    path = settings.data_dir / 'fixture.wav'
    wav_file(path)
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        response = client.post(f'/v1/sessions/{sid}/asr', json={
            'wav_base64': base64.b64encode(path.read_bytes()).decode(), 'purpose': 'agent_command',
            'client_action_id': 'late-after-media-deletion', 'capture_reference': {'child_id': CHILD}})
        assert response.status_code == 409, response.text
        assert len(deletion_receipts) == 1 and deletion_receipts[0]['status'] == 'completed'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 0
            assert c.execute('SELECT status FROM voice_capture_binding').fetchone()[0] == 'cancelled'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    with TestClient(create_app(settings)) as client:
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 0
            assert c.execute('SELECT status FROM voice_capture_binding').fetchone()[0] == 'cancelled'
