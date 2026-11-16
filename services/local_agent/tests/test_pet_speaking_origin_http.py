"""Actual local ASR/Runtime/answer and parent review, synthetic PCM only."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment, start, CHILD
from test_pet_media_evidence import local_review
from test_voice_runtime_binding import capture, interpret, submit


@pytest.mark.parametrize('original_audio', [True, False])
def test_actual_original_and_confirmation_audio_are_not_interchangeable(environment, monkeypatch, original_audio):
    settings, _ = environment
    settings = replace(settings, data_encryption_key=b'O' * 32)
    db = Database(settings.database_path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    gateway = Gateway({}); gateway.configured = False
    answer = 'I like reading in the library because it is quiet. I read story books there after school with my friends.'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True}).status_code == 200
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': 'diag-pet-speaking-2', 'client_action_id': 'spoken-question'})
        assert response.status_code == 200, response.text
        prompt = response.json()['prompt']
        voice = capture(client, settings, monkeypatch, sid, answer, prompt, 'original-audio') if original_audio else None
        candidate = interpret(client, sid, answer, 'original-answer-turn', voice)
        assert candidate['intent'] == 'pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{candidate['agent_turn_id']}/execute").status_code == 200
        confirmation = '刚才那句就是我的答案'
        confirmation_voice = capture(client, settings, monkeypatch, sid, confirmation, prompt, 'confirmation-audio')
        heard = interpret(client, sid, confirmation, 'confirmation-turn', confirmation_voice)
        assert heard['action']['source_voice_input_id'] == voice
        assert heard['action']['voice_input_id'] == confirmation_voice
        with db.connect() as c:
            c.execute('BEGIN')
            authorized = AgentRuntimeStore.resolve_local_answer_submission(c, heard['agent_turn_id'], 'diagnostic.submit')
            assert authorized['source_voice_input_id'] == voice
            assert authorized['voice_input_id'] == confirmation_voice
            assert authorized['answer'] == answer
        response = submit(client, sid, prompt, heard, answer=answer)
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'answered'
        media_path = f"/v1/sessions/{sid}/pet-media/prompt/{prompt['prompt_id']}"
        collection = client.get(media_path, params={'item_version': prompt['item_version']})
        assert collection.status_code == 200, collection.text
        assert collection.json()['selection_required'] is False
        assert [row['voice_input_id'] for row in collection.json()['speaking']] == ([voice] if original_audio else [])
        snapshot = local_review((db, None, sid, None, settings.data_dir / 'synthetic-runtime.wav', None),
                                prompt, answer, approve=False, scored=response.json())
        path = f"/v1/parent/children/{CHILD}/learning-rubrics/{snapshot['assessment_id']}"
        detail = client.get(path)
        assert detail.status_code == 200, detail.text
        context = detail.json()['audio_review']
        with db.connect() as c:
            confirmation_media = c.execute('SELECT media_id FROM pet_media_binding WHERE voice_input_id=?', (confirmation_voice,)).fetchone()[0]
        assert all(packet['media_id'] != confirmation_media for packet in context['bindings'])
        if not original_audio:
            assert context['bindings'] == []
            assert not detail.json()['qualification']['eligible_for_mastery_gate']
            return
        assert len(context['bindings']) == 1
        packet = context['bindings'][0]
        assert packet['answer_voice_origin']['source_voice_input_id'] == voice
        assert packet['answer_voice_origin']['voice_input_id'] == confirmation_voice
        payload = dict(expected_revision=0, client_action_id='parent-original-audio-review', operation='approve',
            reason='Synthetic original-frame review; not a child outcome', independent=True, hint_level=0,
            review_profile='pet_speaking_audio_v1', audio_evidence={'media_id': packet['media_id'], 'review': {
                'schema': 'pet_speaking_audio_references_v1', 'binding_version': packet['binding_version'],
                'audio_sha256': packet['audio']['sha256'], 'observations': [dict(dimension_id=name,
                    start_frame=0, end_frame=packet['audio']['frames'], heard_text='Synthetic annotation',
                    rationale='Authorized fixture review') for name in packet['dimensions_required']]}},
            dimensions=[dict(dimension_id=d['dimension_id'], score=3, response_quotes=[answer] if d['dimension_id'] in {'task','coherence','language'} else [],
                             rationale='Synthetic human dimension judgment') for d in context['dimensions']],
            criterion_evidence=[dict(criterion=c, response_quote=answer) for c in snapshot['criteria']])
        reviewed = client.post(path + '/review', json=payload)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()['qualification']['eligible_for_mastery_gate']
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin':'7391'}).json()['parent_token']
        assert client.get(path).json()['qualification']['eligible_for_mastery_gate']
        collection = client.get(media_path, params={'item_version': prompt['item_version']}).json()
        assert collection['selection_required'] is False
        assert [row['voice_input_id'] for row in collection['speaking']] == [voice]
        assert client.post(path + '/review', json=payload).json()['event'] == reviewed.json()['event']
        assert client.post(f'/v1/sessions/{sid}/asr/actions/original-audio/cancel').status_code == 200
        assert not client.get(path).json()['qualification']['eligible_for_mastery_gate']
        assert client.get(media_path, params={'item_version': prompt['item_version']}).json()['speaking'] == []
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
    assert not gateway.requests
