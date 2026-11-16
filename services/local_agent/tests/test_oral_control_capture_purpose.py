"""Actual loopback ASR routing, synthetic PCM and temporary published sources."""
import base64
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from test_pet_oral_supply_http import setup, post, CHILD, OTHER, BASE
from test_oral_runtime_feedback_http import OralGateway, begin_oral, ANSWER
from test_oral_followup_execution_http import answer
from test_pet_media_evidence import wav_file
from test_voice_runtime_binding import interpret


@pytest.fixture
def oral_case(tmp_path):
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'P' * 32)
    db = Database(initial.path, media_root=tmp_path, data_encryption_key=settings.data_encryption_key)
    gateway = OralGateway()
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=gateway), headers=headers,
                    client=('127.0.0.1', 42380)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True,
            'cloud_model_text_allowed': True, 'cloud_audio_allowed': False}).status_code == 200
        sid, prompt, original, published = begin_oral(client, db, gateway)
        yield client, settings, db, gateway, sid, prompt, published


def asr(case, monkeypatch, key, text='请让我再说一次', prompt=None, child=CHILD, purpose='agent_command', before_result=None):
    client, settings, db, gateway, sid, original, _ = case
    prompt = prompt or original
    def recognize(*a, **kw):
        if before_result:
            before_result()
        return text, 1000, 'synthetic', False
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
    path = settings.data_dir / 'purpose.wav'
    wav_file(path)
    body = {'client_action_id': key, 'purpose': purpose, 'wav_base64': base64.b64encode(path.read_bytes()).decode(),
        'capture_reference': {'child_id': child, 'question_reference': {'kind': 'diagnostic', 'binding': {
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']}}}}
    return client.post(f'/v1/sessions/{sid}/asr', json=body), body


def stored(db, voice_id):
    with db.connect() as c:
        return dict(c.execute('SELECT purpose,transcript FROM voice_input WHERE voice_input_id=?', (voice_id,)).fetchone()), c.execute(
            'SELECT COUNT(*) FROM pet_media_binding WHERE voice_input_id=?', (voice_id,)).fetchone()[0]


def test_actual_answered_control_then_new_waiting_prompt_keeps_separate_media(oral_case, monkeypatch):
    client, settings, db, gateway, sid, prompt, published = oral_case
    saved, first_voice = answer(client, settings, monkeypatch, sid, prompt, gateway, ANSWER, 'first')
    gateway.quote = '我想按刚才的建议补充一下，再说一次'
    gateway.requested_control = 'oral.retry'
    response, _ = asr(oral_case, monkeypatch, 'answered-control', gateway.quote)
    assert response.status_code == 200, response.text
    voice_id = response.json()['voice_input_id']
    voice, media_count = stored(db, voice_id)
    assert voice['purpose'] == 'agent_command' and media_count == 0
    command = interpret(client, sid, gateway.quote, 'retry-exact-original', voice_id)
    assert command['runtime_capability'] == 'oral.retry'
    prepared = post(client, f"/v1/agent-turns/{command['agent_turn_id']}/execute", {})['execution']['result']
    new_prompt = prepared['prompt']
    assert new_prompt['prompt_id'] != prompt['prompt_id']
    response, body = asr(oral_case, monkeypatch, 'new-answer-capture', ANSWER, new_prompt)
    assert response.status_code == 200, response.text
    new_voice = response.json()['voice_input_id']
    assert stored(db, new_voice)[0]['purpose'] == 'pet_speaking'
    assert stored(db, new_voice)[1] == 1
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT prompt_id FROM pet_media_binding WHERE voice_input_id=?', (new_voice,)).fetchone()[0] == new_prompt['prompt_id']
    again = client.post(f'/v1/sessions/{sid}/asr', json=body)
    assert again.status_code == 200 and again.json()['voice_input_id'] == new_voice
    assert stored(db, new_voice)[1] == 1
    # Replaying the original capture after answer/new-prompt creation remains
    # its original voice receipt. It cannot attach audio to the new question.
    original_capture, _ = asr(oral_case, monkeypatch, 'oral-answer-first-audio', ANSWER)
    assert original_capture.status_code == 200, original_capture.text
    assert original_capture.json()['voice_input_id'] == first_voice
    assert stored(db, first_voice)[1] == 1
    explicit, _ = asr(oral_case, monkeypatch, 'answered-explicit-purpose', purpose='pet_speaking')
    assert explicit.status_code == 200, explicit.text
    assert stored(db, explicit.json()['voice_input_id'])[0]['purpose'] == 'agent_command'
    assert stored(db, explicit.json()['voice_input_id'])[1] == 0


@pytest.mark.parametrize('fault', ['cancel', 'withdraw', 'version', 'answer'])
def test_current_source_is_rechecked_after_capture_admission_before_recognition(oral_case, monkeypatch, fault):
    client, settings, db, gateway, sid, prompt, published = oral_case
    original_begin = VoiceCaptureBindingService.begin
    injected = False
    def changed(service, *args, **kwargs):
        nonlocal injected
        result = original_begin(service, *args, **kwargs)
        if injected:
            return result
        injected = True
        # An independent actual HTTP writer after begin's transaction commits.
        writer = TestClient(client.app, headers=client.headers, client=('127.0.0.1', 42381))
        try:
            if fault == 'cancel':
                post(writer, f'/v1/sessions/{sid}/diagnostic-prompt/cancel', {
                    'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'], 'client_action_id': 'cancel-before-recognition'})
            elif fault == 'withdraw':
                post(writer, BASE + '/' + published['candidate_id'] + '/withdraw', {
                    'action_id': 'withdraw-before-recognition', 'expected_version': published['version'], 'note': 'Synthetic withdrawal.'})
            elif fault == 'version':
                with db.connect() as c:
                    c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
                    c.commit()
            else:
                recognize = LocalMediaAI._recognize_wav
                try:
                    answer(writer, settings, monkeypatch, sid, prompt, gateway, ANSWER, 'racing-first')
                finally:
                    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
        finally:
            writer.close()
        return result
    monkeypatch.setattr(VoiceCaptureBindingService, 'begin', changed)
    response, _ = asr(oral_case, monkeypatch, 'racing-control')
    assert response.status_code == 200, response.text
    voice, media_count = stored(db, response.json()['voice_input_id'])
    assert voice['purpose'] == 'agent_command', voice
    assert media_count == 0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == (1 if fault == 'answer' else 0)


@pytest.mark.parametrize('fault', ['cancel', 'withdraw', 'other_child'])
def test_bad_original_scope_never_creates_answer_media(oral_case, monkeypatch, fault):
    client, settings, db, gateway, sid, prompt, published = oral_case
    if fault == 'cancel':
        post(client, f'/v1/sessions/{sid}/diagnostic-prompt/cancel', {'prompt_id': prompt['prompt_id'],
            'item_version': prompt['item_version'], 'client_action_id': 'cancel-original'})
    elif fault == 'withdraw':
        post(client, BASE + '/' + published['candidate_id'] + '/withdraw', {'action_id': 'withdraw-original',
            'expected_version': published['version'], 'note': 'Synthetic withdrawal.'})
    response, _ = asr(oral_case, monkeypatch, 'invalid-source', child=OTHER if fault == 'other_child' else CHILD)
    if fault == 'other_child':
        assert response.status_code == 409, response.text
    else:
        assert response.status_code == 200, response.text
        assert stored(db, response.json()['voice_input_id'])[0]['purpose'] == 'agent_command'
        assert stored(db, response.json()['voice_input_id'])[1] == 0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM pet_media_binding').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['cancel', 'withdraw', 'raw_consent'])
def test_media_write_rechecks_after_recognition_was_admitted(oral_case, monkeypatch, fault):
    client, settings, db, gateway, sid, prompt, published = oral_case
    def mutate():
        writer = TestClient(client.app, headers=client.headers, client=('127.0.0.1', 42382))
        try:
            if fault == 'cancel':
                post(writer, f'/v1/sessions/{sid}/diagnostic-prompt/cancel', {'prompt_id': prompt['prompt_id'],
                    'item_version': prompt['item_version'], 'client_action_id': 'cancel-late'})
            elif fault == 'withdraw':
                post(writer, BASE + '/' + published['candidate_id'] + '/withdraw', {'action_id': 'withdraw-late',
                    'expected_version': published['version'], 'note': 'Synthetic withdrawal.'})
            else:
                assert writer.put('/v1/parent/privacy', json={'raw_media_storage_allowed': False}).status_code == 200
        finally:
            writer.close()
    response, _ = asr(oral_case, monkeypatch, 'late-result', ANSWER, before_result=mutate)
    assert response.status_code == 200, response.text
    voice_id = response.json()['voice_input_id']
    assert stored(db, voice_id)[1] == 0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        if fault == 'raw_consent':
            assert not c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=?', (voice_id,)).fetchone()[0]
