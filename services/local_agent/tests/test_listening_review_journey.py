"""Qualified original listening failure must produce real, fresh review work."""
import pytest
import json
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_obligations import project_child_mastery_evidence
from test_pet_media_evidence import media, CHILD
from test_pet_scoped_listening import twice


def original_failure(media, tmp_path):
    twice(media)
    db, _, sid, prompt, _, _ = media
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        session_token='isolated-listening-review')
    return settings, sid, prompt, {
        'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
        'answer': 'at the football field', 'client_action_id': 'original-qualified-failure'}


def test_answered_failure_schedules_current_source_without_inheriting_playback(media, tmp_path):
    settings, sid, prompt, body = original_failure(media, tmp_path)
    with TestClient(create_app(settings), headers={'X-HSC-Session': settings.session_token}) as client:
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=body)
        assert response.status_code == 200, response.text
        attempt = response.json()['attempt']
        assert response.json()['status'] == 'answered'
        assert attempt['correctness'] == 0 and attempt['score_status'] == 'verified_local', attempt
        db = Database(settings.database_path, media_root=tmp_path)
        with db.connect() as c:
            evidence = project_child_mastery_evidence(c, CHILD)['node-pet-listening-detail']['effective_evidence']
            assert any(e['evidence_id'] == attempt['evidence_id'] and e['correctness'] == 0 for e in evidence)
            reviews = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',
                (attempt['evidence_id'],))]
            assert reviews, 'Qualified answered listening failure lost its review scheduling'
            assert {r['interval_label'] for r in reviews} == {'immediate', '48h', '7d'}
            assert {r['diagnostic_item_id'] for r in reviews} == {'diag-pet-listening-2'}
            assert c.execute('SELECT next_review_at FROM mastery_state WHERE child_id=? AND knowledge_node_id=?',
                (CHILD, 'node-pet-listening-detail')).fetchone()[0]
            from home_study_agent.pet_skill_gate import pet_skill_gate
            assert not pet_skill_gate(c, CHILD, 'diag-pet-listening-2')['skill_scoring_allowed']
            assert c.execute('SELECT COUNT(*) FROM pet_media_binding WHERE diagnostic_item_id=?',
                ('diag-pet-listening-2',)).fetchone()[0] == 0
        replay = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=body)
        assert replay.status_code == 200 and replay.json()['attempt']['attempt_id'] == attempt['attempt_id']
        with db.connect() as c:
            assert [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',
                (attempt['evidence_id'],))] == reviews


def test_review_scheduling_failure_rolls_back_answer_binding_and_recovers(media, tmp_path):
    import sqlite3
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    settings, sid, prompt, body = original_failure(media, tmp_path)
    db = Database(settings.database_path, media_root=tmp_path)
    with db.connect() as c:
        c.execute("""CREATE TRIGGER fail_listening_review BEFORE INSERT ON review_task
            BEGIN SELECT RAISE(ABORT,'synthetic review write failure'); END""")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic review write failure'):
        DiagnosticPromptService(db).answer(sid, **body)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',
            (prompt['prompt_id'],)).fetchone()[0] == 'answer_saved'
        c.execute('DROP TRIGGER fail_listening_review')
        c.commit()
    result = DiagnosticPromptService(Database(db.path, media_root=tmp_path)).resume(sid, prompt_id=prompt['prompt_id'])
    assert result['status'] == 'answered' and result['attempt']['score_status'] == 'verified_local'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 3


def next_day_case(media, tmp_path, monkeypatch, *, ready_source=False):
    from datetime import datetime, timedelta, timezone
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from test_cloud_first_agent import Planner
    from home_study_agent.model_gateway import ModelResult
    from test_daily_learning_voice_integration import set_state
    from test_writing_stage_review_consumers import advance_clock
    settings, sid, prompt, body = original_failure(media, tmp_path)
    with TestClient(create_app(settings), headers={'X-HSC-Session': settings.session_token}) as client:
        # This fixture isolates the PET original-audio review subsystem. The
        # HTTP application boundary additionally promotes a qualified wrong
        # answer into the higher-priority mistake loop, which is covered by
        # mistake journey tests and would intentionally suppress a duplicate
        # generic due-review plan item here.
        from home_study_agent.diagnostic_prompt import DiagnosticPromptService
        answer = DiagnosticPromptService(
            Database(settings.database_path, media_root=tmp_path)
        ).answer(sid, **body)
        assert answer['status'] == 'answered', answer
        attempt = answer['attempt']
        if ready_source:
            from test_pet_listening_resource_admission import bind_audio
            assert client.post('/v1/parent/setup', json={'pin': '2468'}).status_code == 200
            material_prompt = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
                'track': 'pet', 'preferred_item_id': 'diag-pet-listening-2',
                'client_action_id': 'previous-reviewed-material-question'})
            assert material_prompt.status_code == 200, material_prompt.text
            mp = material_prompt.json()['prompt']
            audio, _ = bind_audio(client, media[0], tmp_path, sid, mp, key='prior-reviewed-original')
            reference = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
                'prompt_id': mp['prompt_id'], 'item_version': mp['item_version'], 'answer': '雨衣',
                'client_action_id': 'unheard-prior-reference'})
            assert reference.status_code == 200 and reference.json()['attempt']['score_status'] != 'verified_local', reference.text
        set_state(client, sid, 'reflection')
        closed = client.post(f'/v1/sessions/{sid}/reflection/complete', json={
            'learned_text': '', 'difficult_text': '', 'client_action_id': 'listening-day-close'})
        assert closed.status_code == 200, closed.text
        with client.app.state.repository.database.connect() as c:
            review = dict(c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='next_day'",
                (attempt['evidence_id'],)).fetchone())
        summary = client.get(f'/v1/sessions/{sid}/learning-summary').json()
        assert attempt['evidence_id'] in str(summary), summary
    # Exercise the real next Nanjing learning day, not the runner's wall-clock
    # hour (which can leave only a few minutes before the evening cutoff).
    next_practice = datetime.fromisoformat(review['due_at']) + timedelta(hours=18)
    if ready_source:
        # The prior material response is still a real answer. Next calendar day
        # at 18:00 can be less than 24h after it on an evening test run.
        prior_response = datetime.fromisoformat(reference.json()['attempt']['created_at'])
        earliest = prior_response + timedelta(days=1, seconds=1)
        while next_practice < earliest:
            next_practice += timedelta(days=1)
    clock = advance_clock(monkeypatch, next_practice)
    from home_study_agent import learning_input_replan
    monkeypatch.setattr(learning_input_replan, 'utc_now', lambda: clock.now(timezone.utc))
    db = Database(settings.database_path, media_root=tmp_path)
    PrivacyLifecycleService(db, tmp_path).update_preferences({'cloud_model_text_allowed': True})
    class ListeningPlanner(Planner):
        async def complete(self, request):
            if request.request_type == 'voice_interaction':
                data = json.loads(request.messages[-1]['content'])
                assert set(data) == {'transcript', 'state'}
                answering = data['transcript'] == '雨衣'
                return ModelResult(content=json.dumps({'role': 'answer' if answering else 'control',
                    'control': None if answering else 'task.resume',
                    'quote': data['transcript'], 'confidence': .99,
                    **({} if answering else {'selector': {'subject': None, 'task_kind': None, 'relative_order': None}})}),
                    provider='test', model='synthetic-control', latency_ms=1)
            return await super().complete(request)
    gateway = ListeningPlanner('task.start', {'start_selector': {
        'subject': None, 'task_kind': 'review', 'source_type': None,
        'target_quote': '开始今天的复测',
    }}, '开始今天的复测')
    return settings, db, gateway, review, prompt, clock


@pytest.mark.parametrize('fresh_playback,ready_source,replacement',
    [(True, False, False), (False, False, False), (True, True, False), (True, False, True)],
    ids=['new-source-new-playback', 'no-new-playback', 'reuse-source-new-playback', 'other-prompt-cannot-complete'])
def test_day_summary_to_real_voice_review_waits_for_new_original_audio(media, tmp_path, monkeypatch, fresh_playback, ready_source, replacement, early_fault=None):
    from contextlib import ExitStack
    from test_daily_learning_voice_integration import set_state
    from test_voice_runtime_binding import capture, interpret
    settings, db, gateway, review, old_prompt, clock = next_day_case(media, tmp_path, monkeypatch, ready_source=ready_source)
    with ExitStack() as stack:
        def reopen():
            stack.close()
            return stack.enter_context(TestClient(create_app(settings, model_gateway=gateway),
                headers={'X-HSC-Session': settings.session_token}))
        client = reopen()
        started = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'listening-next-day'})
        assert started.status_code == 201, started.text
        sid = started.json()['session_id']
        # Only the day's initial context is a fixture; plan/slot/prompt/commands are production writes.
        set_state(client, sid, 'homework_capture')
        with db.connect() as c:
            c.execute('UPDATE daily_session SET homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?', (sid,))
            c.execute('UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id=?', (CHILD,))
            c.commit()
        plan = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'next-day-real-plan'})
        assert plan.status_code == 200, plan.text
        slot = next(x for x in plan.json()['plan_items'] if x['source_type'] == 'due_review')
        assert slot['source_ref'] == review['review_task_id']
        set_state(client, sid, 'study')
        voice = capture(client, settings, monkeypatch, sid, gateway.payload['utterance_binding']['quote'], key='next-review-voice')
        turn = interpret(client, sid, gateway.payload['utterance_binding']['quote'], 'next-review-command', voice)
        assert turn['runtime_capability'] == 'task.start', turn
        execution = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert execution.status_code == 200, execution.text
        result = execution.json()['execution']['result']
        assert result['status'] == ('active' if ready_source else 'awaiting_resources'), result
        prompt = result['diagnostic_prompt']['prompt']
        assert prompt['review_task_id'] == review['review_task_id']
        assert prompt['prompt_id'] != old_prompt['prompt_id']
        assert prompt['diagnostic_item_id'] == review['diagnostic_item_id']
        with db.connect() as c:
            assert (c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?', (slot['plan_item_id'],)).fetchone()[0] is not None) == ready_source
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
        from test_pet_listening_resource_admission import bind_audio
        if ready_source:
            from home_study_agent.pet_media_evidence import project_pet_media_evidence
            with db.connect() as c:
                row = c.execute('SELECT media_id FROM pet_media_binding WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
                audio = project_pet_media_evidence(c, sid, media_id=row[0], media_root=tmp_path)
                assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE media_id=? AND operation='review_source'", (row[0],)).fetchone()[0] == 0
            assert prompt['item']['stimulus_text'] not in execution.json()['execution']['response_text']
        else:
            assert client.post('/v1/parent/setup', json={'pin': '2468'}).status_code == 200
            audio, _ = bind_audio(client, db, tmp_path, sid, prompt, key='next-question-original')
            voice = capture(client, settings, monkeypatch, sid, '继续原任务', prompt, 'resource-ready-voice')
            resumed = interpret(client, sid, '继续原任务', 'resource-ready-command', voice)
            assert resumed['runtime_capability'] == 'task.resume', resumed
            resumed_url = f"/v1/agent-turns/{resumed['agent_turn_id']}/execute"
            start = client.post(resumed_url)
            assert start.status_code == 200, start.text
            assert start.json()['execution']['result']['status'] == 'active', start.text
            assert start.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
            assert prompt['item']['stimulus_text'] not in start.json()['execution']['response_text']
        assert not audio['qualification']['task_delivery_qualified']
        if replacement:
            cancelled = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/cancel', json={
                'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']})
            assert cancelled.status_code == 200, cancelled.text
            alternative = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
                'track': 'pet', 'preferred_item_id': prompt['diagnostic_item_id'],
                'review_task_id': review['review_task_id'], 'client_action_id': 'standalone-other-prompt'})
            assert alternative.status_code == 200, alternative.text
            other = alternative.json()['prompt']
            assert other['prompt_id'] != prompt['prompt_id']
            prompt = other
            audio, _ = bind_audio(client, db, tmp_path, sid, prompt, key='standalone-other-audio')
        if fresh_playback:
            play_original(client, sid, audio, media[5], clock)
        gateway.payload['next_step'] = {'capability': 'diagnostic.submit', 'arguments': {'answer': '雨衣'},
            'reason': 'answer', 'expected_evidence': []}
        gateway.payload['objective']['type'] = 'learning'
        gateway.payload['utterance_binding'] = {'role': 'answer', 'quote': '雨衣'}
        voice = capture(client, settings, monkeypatch, sid, '雨衣', prompt, 'review-original-answer')
        heard = interpret(client, sid, '雨衣', 'review-answer-turn', voice)
        if heard['intent'] == 'pending_answer_clarification':
            clarification = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert clarification.status_code == 200, clarification.text
            confirmation = '刚才那句就是我的答案'
            confirmed_voice = capture(client, settings, monkeypatch, sid, confirmation, prompt, 'review-answer-confirmation')
            heard = interpret(client, sid, confirmation, 'review-confirm-turn', confirmed_voice)
            assert heard['action']['source_voice_input_id'] == voice
        from test_voice_runtime_binding import submit
        answer_key = f"diagnostic-answer:{prompt['prompt_id']}:{heard['agent_turn_id']}"
        answer = submit(client, sid, prompt, heard, answer='雨衣', key=answer_key)
        if not fresh_playback:
            assert answer.status_code == 409, answer.text
            retained = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
            assert retained['status'] == 'waiting_answer' and retained['attempt'] is None, retained
            assert retained['deferred_answer']['formal_answer_saved'] is False
            assert retained['deferred_answer']['source_voice_input_id'] == voice
            assert retained['deferred_answer']['delivery_complete'] is False
            assert retained['deferred_answer']['message'] == '这道题还没听完，我先记住你刚才说的；听完后再确认答案'
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (review['review_task_id'],)).fetchone()[0] == 0
                source_turn = retained['deferred_answer']['source_turn_id']
                assert c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?', (source_turn,)).fetchone()[0] == '雨衣'
                assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (voice,)).fetchone()[0] == '雨衣'
                q = c.execute('SELECT answer_action_id,answer_json FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
                assert tuple(q) == (None, None)
            old_heard = heard
            client = reopen()
            retained = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
            assert retained['deferred_answer']['source_voice_input_id'] == voice
            assert retained['deferred_answer']['speech_retained'] is True
            assert retained['attempt'] is None
            assert submit(client, sid, prompt, old_heard, answer='雨衣', key=answer_key).status_code == 409
            if early_fault:
                if early_fault == 'speech_cancel':
                    assert client.post(f'/v1/sessions/{sid}/asr/actions/review-original-answer/cancel').status_code == 200
                    changed = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
                    assert changed['deferred_answer']['speech_retained'] is False
                    assert changed['deferred_answer']['message'] is None
                else:
                    assert client.post(f'/v1/sessions/{sid}/diagnostic-prompt/cancel', json={
                        'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']}).status_code == 200
                    if early_fault == 'new_question':
                        other = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
                            'track': 'pet', 'preferred_item_id': 'diag-pet-listening-1',
                            'client_action_id': 'another-original-question'}).json()
                        assert other['prompt']['prompt_id'] != prompt['prompt_id'], other
                        assert 'deferred_answer' not in other
                    changed = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
                    assert changed['status'] == 'cancelled' and changed['deferred_answer']['message'] is None
                client = reopen()
                assert submit(client, sid, prompt, old_heard, answer='雨衣', key=answer_key).status_code == 409
                with db.connect() as c:
                    assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (review['review_task_id'],)).fetchone()[0] == 0
                return
            # Real restart system pause is resumed by a new voice command, not a forged event.
            resume_voice = capture(client, settings, monkeypatch, sid, '继续原任务', prompt, 'early-restart-resume-voice')
            resumed = interpret(client, sid, '继续原任务', 'early-restart-resume', resume_voice)
            assert resumed['runtime_capability'] == 'task.resume', resumed
            resumed_result = client.post(f"/v1/agent-turns/{resumed['agent_turn_id']}/execute")
            assert resumed_result.status_code == 200, resumed_result.text
            assert resumed_result.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
            current = client.get(f'/v1/sessions/{sid}/pet-media/prompt/{prompt["prompt_id"]}',
                params={'item_version': prompt['item_version']})
            assert current.status_code == 200, current.text
            with db.connect() as c:
                from home_study_agent.pet_media_evidence import project_pet_media_evidence
                audio = project_pet_media_evidence(c, sid, media_id=audio['media_id'], media_root=tmp_path)
            play_original(client, sid, audio, media[5], clock)
            # Playback is not an answer, including after a process restart.
            read = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
            assert read['status'] == 'waiting_answer' and read['attempt'] is None
            assert read['deferred_answer']['new_confirmation_required'] is True
            assert read['deferred_answer']['delivery_complete'] is True
            assert '已经听完' in read['deferred_answer']['message']
            assert submit(client, sid, prompt, old_heard, answer='雨衣', key=answer_key).status_code == 409
            assert submit(client, sid, prompt, old_heard, answer='雨衣', key=answer_key + ':different-key').status_code == 409
            assert client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
                'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                'answer': '雨衣', 'client_action_id': 'model-cannot-confirm'}).status_code == 409
            # A fresh actual utterance and explicit role confirmation, never copied model text.
            new_voice = capture(client, settings, monkeypatch, sid, '雨衣', prompt, 'after-play-new-answer')
            new_heard = interpret(client, sid, '雨衣', 'after-play-new-turn', new_voice)
            assert new_heard['intent'] == 'pending_answer_clarification', new_heard
            assert client.post(f"/v1/agent-turns/{new_heard['agent_turn_id']}/execute").status_code == 200
            confirm_voice = capture(client, settings, monkeypatch, sid, '刚才那句就是我的答案', prompt, 'after-play-confirm-voice')
            new_heard = interpret(client, sid, '刚才那句就是我的答案', 'after-play-confirm-turn', confirm_voice)
            assert new_heard['action']['source_voice_input_id'] == new_voice
            new_answer_key = f"diagnostic-answer:{prompt['prompt_id']}:{new_heard['agent_turn_id']}"
            assert new_answer_key != answer_key
            assert submit(client, sid, prompt, new_heard, answer='雨衣', key=answer_key).status_code == 409
            submitted = submit(client, sid, prompt, new_heard, answer='雨衣', key=new_answer_key)
            assert submitted.status_code == 200, submitted.text
            actual = submitted.json()['attempt']
            assert actual['score_status'] == 'verified_local' and actual['correctness'] == 1, actual
            with db.connect() as c:
                ev = c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?', (actual['evidence_id'],)).fetchone()
                assert ev['independent'] == 0 and ev['hint_level'] == 0
                assert 'listening_review_pre_delivery_response' in ev['evidence_summary']
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (review['review_task_id'],)).fetchone()[0] == 1
            completed = client.post(f"/v1/plan-items/{slot['plan_item_id']}/complete", json={
                'actual_minutes': 1, 'client_action_id': 'new-explicit-review-complete'})
            assert completed.status_code == 200, completed.text
            client = reopen()
            restored = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
            assert restored['attempt']['attempt_id'] == actual['attempt_id']
            assert submit(client, sid, prompt, old_heard, answer='雨衣', key=answer_key).status_code == 409
            assert submit(client, sid, prompt, new_heard, answer='雨衣', key=new_answer_key).json()['attempt']['attempt_id'] == actual['attempt_id']
            return
        assert answer.status_code == 200, answer.text
        attempt = answer.json()['attempt']
        assert answer.json()['status'] == 'answered'
        if fresh_playback:
            assert attempt['score_status'] == 'verified_local' and attempt['correctness'] == 1, attempt
        else:
            assert attempt['score_status'] != 'verified_local', attempt
        complete = client.post(f"/v1/plan-items/{slot['plan_item_id']}/complete", json={
            'actual_minutes': 1, 'client_action_id': 'review-task-complete'})
        assert complete.status_code == (200 if fresh_playback and not replacement else 422), complete.text
        with db.connect() as c:
            task = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (review['review_task_id'],)).fetchone()
            assert task['status'] == ('completed' if fresh_playback else 'pending')
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (review['review_task_id'],)).fetchone()[0] == 1
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        restored = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']})
        assert restored.status_code == 200 and restored.json()['attempt']['attempt_id'] == attempt['attempt_id'], restored.text
        replay = submit(client, sid, prompt, heard, answer='雨衣', key=answer_key)
        assert replay.status_code == 200 and replay.json()['attempt']['attempt_id'] == attempt['attempt_id'], replay.text
        if fresh_playback and not replacement:
            repeated = client.post(f"/v1/plan-items/{slot['plan_item_id']}/complete", json={
                'actual_minutes': 1, 'client_action_id': 'review-task-complete'})
            assert repeated.status_code == 200, repeated.text


def play_original(client, sid, audio, mono, clock):
    from datetime import timedelta
    base = f'/v1/sessions/{sid}/pet-media'
    for count in range(2):
        body = {'media_id': audio['media_id'], 'generation': audio['generation'],
            'expected_revision': audio['revision'], 'command': 'resume', 'client_action_id': f'new-play-{count}'}
        result = client.post(base + '/listening/control', json=body)
        assert result.status_code == 200, result.text
        audio = result.json()['media']
        for event, frame in [('started', 0), ('ended', audio['audio']['frames'])]:
            if event == 'ended':
                mono[0] += 1000
                clock.current += timedelta(seconds=1)
            result = client.post(base + '/listening/playback', json={'media_id': audio['media_id'],
                'generation': audio['generation'], 'expected_revision': audio['revision'],
                'event': event, 'audio_sha256': audio['audio']['sha256'], 'frame_position': frame,
                'monotonic_ms': mono[0], 'client_action_id': f'new-play-{count}-{event}'})
            assert result.status_code == 200, result.text
            audio = result.json()['media']
    assert audio['qualification']['task_delivery_qualified']
    return audio
