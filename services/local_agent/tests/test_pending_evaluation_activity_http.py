"""Real ASR/Agent/SQLite activity rounds with an untouched pending evaluation."""
import base64
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_learning_outcome_evaluator import evaluation_fixture
from test_pending_evaluation_control import facts
from test_pending_question_activity_control import decision
from test_voice_runtime_binding import wav_file
from test_diagnostic_prompt_http import environment
from test_learning_runtime_http import NoCloud
from test_pending_evaluation_voice_capture_http import speaking_receipt


@pytest.mark.parametrize('missing_focus', [False, True])
@pytest.mark.parametrize('source_kind', ['inline', 'speaking'])
def test_pending_evaluation_activity_replies_need_exact_capture_and_persist_two_rounds(tmp_path, monkeypatch, missing_focus, source_kind, request):
    prompt, original_words = None, '正弦看对边，余弦看邻边，都要除以斜边。'
    if source_kind == 'speaking':
        settings, db = request.getfixturevalue('environment')
        with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
            sid, prompt, original_id, original_words = speaking_receipt(client, settings, db, monkeypatch)
        evaluator = None
    else:
        db, sid, evaluator, _, _ = evaluation_fixture(tmp_path)
        settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
        original_id = 'eval'
    with db.connect() as c:
        child = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0]
    privacy = PrivacyLifecycleService(db, settings.data_dir)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = replace(settings,
        model_provider='qwen', model_api_key='synthetic-no-network', model_name='isolated',
        voice_intent_model_name='isolated')
    opening = '等评价的时候，能和我一起编个小故事吗？'
    replies = ['我想让一位小探险家当主角。', '他想先去河边看看那座桥。']
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        wire = json.dumps(body['messages'], ensure_ascii=False)
        assert all(secret not in wire for secret in ('正弦', '余弦', 'eval-action', sid, original_words))
        if prompt:
            assert prompt['prompt_id'] not in wire and prompt['item']['prompt'] not in wire
        if '只分类当前原话' in body['messages'][0]['content']:
            data = json.loads(body['messages'][-1]['content'])
            assert set(data) == {'transcript', 'state'}
            quote = data['transcript']
            content = decision(quote, 'interactive_story') if quote == opening else json.dumps({
                'role': 'activity_reply', 'control': None, 'quote': quote, 'confidence': .99})
        else:
            content = '主角来到桥边，发现一张地图。你想让他先看地图的哪一部分？'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})

    def app():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))

    def speak(client, words, key, focus=None):
        monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (words, 1000, 'synthetic-recognizer', False))
        audio = tmp_path / 'synthetic-story.wav'
        wav_file(audio)
        captured = client.post(f'/v1/sessions/{sid}/asr', json={
            'wav_base64': base64.b64encode(audio.read_bytes()).decode(), 'purpose': 'agent_command',
            'client_action_id': key + '-audio', 'capture_reference': {
                'child_id': child, 'pending_evaluation_turn_id': original_id,
                **({'question_reference': {'kind': 'diagnostic', 'binding': {
                    'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']}}} if prompt else {}),
                **({'activity_reference': focus} if focus else {})}})
        assert captured.status_code == 200, captured.text
        body = {'transcript': words, 'client_action_id': key, 'pending_evaluation_turn_id': original_id,
                'voice_input_id': captured.json()['voice_input_id']}
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
        assert response.status_code == 200, response.text
        return response.json(), body

    with TestClient(app()) as client:
        if evaluator:
            evaluator._claim(evaluator._load(original_id))
        before = facts(db)
        turn, _ = speak(client, opening, 'story-start')
        assert turn['action']['type'] == 'start_companion_activity', turn
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        for index, words in enumerate(replies):
            focus_response = client.get(f'/v1/sessions/{sid}/voice/activity-focus')
            assert focus_response.status_code == 200, focus_response.text
            focus = focus_response.json()['activity_focus']
            assert focus
            reply, last_body = speak(client, words, 'story-reply-' + str(index), None if missing_focus else focus)
            assert reply['action']['pending_evaluation_control']['evaluation_turn_id'] == original_id
            assert reply['after_execution'] == 'stop' and facts(db) == before
            with db.connect() as c:
                activity = c.execute('SELECT * FROM companion_activity').fetchone()
                if missing_focus:
                    assert reply['intent'] != 'companion_activity_reply'
                    assert activity['turn_count'] == 0
                    return
                assert reply['intent'] == 'companion_activity_reply', reply
                assert reply['runtime_capability'] == 'conversation.respond'
                assert reply['tool_status'] == 'completed'
                assert activity['turn_count'] == index + 1
        count = len(requests)
    with TestClient(app()) as client:
        replay = client.post(f'/v1/sessions/{sid}/agent-turn', json=last_body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['agent_turn_id'] == reply['agent_turn_id']
        assert len(requests) == count and facts(db) == before
        with db.connect() as c:
            assert c.execute('SELECT turn_count FROM companion_activity').fetchone()[0] == 2
