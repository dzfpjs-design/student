"""Actual non-seed publication to original review, new delivery and voice answer.

Synthetic material/PCM and controlled semantic responses are not PET certification.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.learning_obligations import project_child_mastery_evidence
from home_study_agent.model_gateway import ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_cloud_first_agent import Planner
from test_daily_learning_voice_integration import set_state
from test_pet_reviewed_source_publication import prepared, publish, CHILD
from test_pet_reviewed_source_contract import packet
from test_pet_listening_resource_admission import bind_audio
from test_voice_runtime_binding import capture, interpret, submit
from test_writing_stage_review_consumers import advance_clock


class NonseedPlanner(Planner):
    async def complete(self, request):
        if request.request_type == 'voice_interaction':
            self.requests.append(request)
            data = json.loads(request.messages[-1]['content'])
            assert set(data) == {'transcript', 'state'}
            answer = data['transcript'] in {'the library is closed', 'a club meeting is postponed'}
            return ModelResult(content=json.dumps({'role': 'answer' if answer else 'control',
                'control': None if answer else 'task.resume', 'quote': data['transcript'], 'confidence': .99,
                **({} if answer else {'selector': {'subject': None, 'task_kind': None, 'relative_order': None}})}),
                provider='test', model='isolated-semantic', latency_ms=1)
        return await super().complete(request)


def play(client, sid, audio, key, mono, clock):
    base = f'/v1/sessions/{sid}/pet-media/listening'
    for index in range(audio['qualification']['required_play_count']):
        result = client.post(base + '/control', json={'media_id': audio['media_id'],
            'generation': audio['generation'], 'expected_revision': audio['revision'],
            'command': 'resume', 'client_action_id': f'{key}:{index}:resume'})
        assert result.status_code == 200, result.text
        audio = result.json()['media']
        for event, frame in [('started', 0), ('ended', audio['audio']['frames'])]:
            if event == 'ended':
                mono[0] += 1000
                clock.current += timedelta(seconds=1)
            result = client.post(base + '/playback', json={'media_id': audio['media_id'],
                'generation': audio['generation'], 'expected_revision': audio['revision'],
                'event': event, 'audio_sha256': audio['audio']['sha256'],
                'frame_position': frame, 'monotonic_ms': mono[0], 'client_action_id': f'{key}:{index}:{event}'})
            assert result.status_code == 200, result.text
            audio = result.json()['media']
    assert audio['qualification']['task_delivery_qualified']
    return audio


def spoken_answer(client, settings, monkeypatch, sid, prompt, gateway, answer, key):
    gateway.payload['next_step'] = {'capability': 'diagnostic.submit', 'arguments': {'answer': answer},
        'reason': 'answer', 'expected_evidence': []}
    gateway.payload['objective']['type'] = 'learning'
    gateway.payload['utterance_binding'] = {'role': 'answer', 'quote': answer}
    voice = capture(client, settings, monkeypatch, sid, answer, prompt, key + ':voice')
    heard = interpret(client, sid, answer, key + ':turn', voice)
    if heard['intent'] == 'pending_answer_clarification':
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code == 200
        text = '刚才那句就是我的答案'
        confirmed = capture(client, settings, monkeypatch, sid, text, prompt, key + ':confirm-voice')
        heard = interpret(client, sid, text, key + ':confirm-turn', confirmed)
        assert heard['action']['source_voice_input_id'] == voice
    action_id = f"diagnostic-answer:{prompt['prompt_id']}:{heard['agent_turn_id']}"
    result = submit(client, sid, prompt, heard, answer=answer, key=action_id)
    return result, heard, action_id, voice


def test_published_wrong_listening_enters_one_canonical_mistake_loop(tmp_path, monkeypatch):
    source = packet(prompt='What is the main message?',
        stimulus_text='Hello everyone. Our science club meeting cannot happen today. We will meet next Tuesday instead.',
        expected_answer={'local_rule': 'local_exact_v1', 'exact_answers': ['a club meeting is postponed']})
    db, supply, spec, _ = prepared(tmp_path, source)
    # Initial synthetic curriculum fixture, before any draft/approval. The gist
    # anchor remains unscored; only the published exact source can supply review.
    quote = '听懂简短通知的主要话题和结果。'
    with db.connect() as c:
        c.execute('UPDATE curriculum_standard_page SET content_text=? WHERE source_id=?', (quote, 'pet-std'))
        c.execute('UPDATE curriculum_standard_requirement SET title=?,requirement_text=?,observable_outcome=? WHERE requirement_id=?',
            ('Listening gist', quote, 'Main message', 'pet-req'))
        c.commit()
    published = publish(supply, {**spec, 'seed_item_id': 'diag-pet-listening-gist-baseline',
        'standard_quote': quote, 'explanation': 'The speaker says the meeting is postponed until next Tuesday.'})
    item_id = published['diagnostic_item_id']
    assert not item_id.startswith('diag-') and published['eligible']
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        session_token='isolated-nonseed-listening')
    clock = advance_clock(monkeypatch, datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1))
    mono = [1000]
    monkeypatch.setattr('home_study_agent.pet_media_evidence._clock_ms', lambda: mono[0])
    gateway = NonseedPlanner('task.start', {'start_selector': {
        'subject': None, 'task_kind': 'review', 'source_type': None,
        'target_quote': '开始今天的复测',
    }}, '开始今天的复测')
    with ExitStack() as stack:
        def reopen():
            stack.close()
            return stack.enter_context(TestClient(create_app(settings, model_gateway=gateway),
                headers={'X-HSC-Session': settings.session_token}))
        client = reopen()
        assert client.post('/v1/parent/setup', json={'pin': '2468'}).status_code == 200
        PrivacyLifecycleService(db, tmp_path).update_preferences({'cloud_model_text_allowed': True})
        first_sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'nonseed-first-day'}).json()['session_id']
        original = client.post(f'/v1/sessions/{first_sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': item_id, 'client_action_id': 'original-published-question'})
        assert original.status_code == 200, original.text
        old_prompt = original.json()['prompt']
        audio, _ = bind_audio(client, db, tmp_path, first_sid, old_prompt, key='reviewed-gist-original')
        old_audio = play(client, first_sid, audio, 'first-listen', mono, clock)
        original_answer, _, _, original_voice = spoken_answer(client, settings, monkeypatch, first_sid, old_prompt,
            gateway, 'the library is closed', 'original-answer')
        assert original_answer.status_code == 200, original_answer.text
        first_attempt = original_answer.json()['attempt']
        assert first_attempt['correctness'] == 0 and first_attempt['score_status'] == 'verified_local', first_attempt
        with db.connect() as c:
            first_reviews = [dict(row) for row in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (first_attempt['evidence_id'],))]
            assert {r['interval_label'] for r in first_reviews} == {'immediate', '48h', '7d'}
            assert {r['diagnostic_item_id'] for r in first_reviews} == {item_id}, first_reviews
        set_state(client, first_sid, 'reflection')
        close = client.post(f'/v1/sessions/{first_sid}/reflection/complete', json={
            'learned_text': '', 'difficult_text': '', 'client_action_id': 'nonseed-first-close'})
        assert close.status_code == 200, close.text
        assert first_attempt['evidence_id'] in str(client.get(f'/v1/sessions/{first_sid}/learning-summary').json())
        with db.connect() as c:
            mistake = c.execute(
                """SELECT * FROM mistake_case
                   WHERE child_id=? AND source_evidence_id=? AND diagnostic_item_id=?""",
                (CHILD, first_attempt['evidence_id'], item_id),
            ).fetchone()
            assert mistake is not None
            assert mistake['status'] == 'diagnosing'
            assert mistake['confirmed_at'] is not None
            assert c.execute(
                """SELECT COUNT(*) FROM review_task
                   WHERE source_evidence_id=? AND interval_label='next_day'""",
                (first_attempt['evidence_id'],),
            ).fetchone()[0] == 0
            assert c.execute(
                """SELECT COUNT(*) FROM mistake_status_event
                   WHERE mistake_id=? AND reason LIKE '%冻结的答案和评分规则%'""",
                (mistake['mistake_id'],),
            ).fetchone()[0] == 1
