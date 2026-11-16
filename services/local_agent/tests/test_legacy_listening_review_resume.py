"""An old serializer's actual persisted review task needs explicit fresh binding."""
import json
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent import pet_task_execution
from test_pet_media_evidence import media, CHILD
from test_listening_review_journey import next_day_case
from test_daily_learning_voice_integration import set_state
from test_voice_runtime_binding import capture, interpret


def legacy_plan(client, db, monkeypatch, sid):
    set_state(client, sid, 'homework_capture')
    with db.connect() as c:
        c.execute('UPDATE daily_session SET homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?', (sid,))
        c.execute("UPDATE family_rule SET latest_end_time='23:59',max_learning_minutes=90 WHERE child_id=?", (CHILD,))
        c.commit()
    # Compatibility fixture: the former serializer had no listening-specific
    # planning_evidence branch. All obligations, selection, allocation and writes
    # still run through the actual API. Never hand-insert a valid binding/slot.
    with monkeypatch.context() as old_serializer:
        old_serializer.setattr(pet_task_execution, 'is_listening_review_task', lambda *a: False)
        result = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'legacy-real-plan'})
    assert result.status_code == 200, result.text
    item = next(x for x in result.json()['plan_items'] if x['source_type'] == 'due_review')
    with db.connect() as c:
        saved = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone())
        decisions = c.execute('SELECT metadata_json FROM plan_constraint_decision WHERE plan_id=? AND source_ref=?',
            (saved['plan_id'], item['source_ref'])).fetchall()
        assert decisions and all(not json.loads(x[0]).get('planning_evidence', {}).get('pet_question_binding') for x in decisions)
    assert saved['started_at'] is None and saved['status'] == 'pending'
    set_state(client, sid, 'study')
    return saved


@pytest.mark.parametrize('capability,utterance', [('task.start', '开始任务'), ('task.resume', '继续刚才的听力复测')])
def test_old_current_day_review_can_be_rebound_by_actual_child_command(media, tmp_path, monkeypatch, capability, utterance):
    settings, db, gateway, review, _, _ = next_day_case(media, tmp_path, monkeypatch)
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        opened = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'legacy-next-day'})
        assert opened.status_code == 201, opened.text
        sid = opened.json()['session_id']
        old = legacy_plan(client, db, monkeypatch, sid)
        assert old['source_ref'] == review['review_task_id']
        gateway.payload['next_step'] = {'capability': capability, 'arguments': {}, 'reason': 'current request', 'expected_evidence': []}
        gateway.payload['utterance_binding'] = {'role': 'task_request', 'quote': utterance}
        voice = capture(client, settings, monkeypatch, sid, utterance, key='legacy-command-voice')
        turn = interpret(client, sid, utterance, 'legacy-rebind-command', voice)
        (tmp_path / 'legacy-response.json').write_text(json.dumps(turn, ensure_ascii=False))
        # Both natural phrases select the validated resume target. Its operation
        # is start, not a fabricated pause/resume event or a new model-supplied ID.
        assert turn['runtime_capability'] == 'task.resume', turn
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        (tmp_path / 'legacy-execution.json').write_text(executed.text)
        assert executed.status_code == 200, executed.text
        result = executed.json()['execution']['result']
        assert result['status'] == 'awaiting_resources', result
        assert result['plan_item_id'] != old['plan_item_id']
        prompt = result['diagnostic_prompt']['prompt']
        assert prompt['review_task_id'] == review['review_task_id']
        assert prompt['diagnostic_item_id'] == review['diagnostic_item_id']
        with db.connect() as c:
            prior = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (old['plan_item_id'],)).fetchone())
            assert prior['status'] in {'rescheduled', 'cancelled'} and prior['started_at'] is None
            current = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (result['plan_item_id'],)).fetchone()
            assert current['source_ref'] == old['source_ref'] and current['started_at'] is None
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0


def execute(client, turn):
    response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert response.status_code == 200, response.text
    return response.json()['execution']


def request_resume(client, settings, monkeypatch, sid, key, prompt=None):
    text = '继续刚才的听力复测'
    voice = capture(client, settings, monkeypatch, sid, text, prompt, key + ':voice')
    result = interpret(client, sid, text, key, voice)
    assert result['runtime_capability'] == 'task.resume', result
    return result


def test_legacy_review_new_audio_answer_completion_and_restart_preserve_original(media, tmp_path, monkeypatch):
    from test_pet_listening_resource_admission import bind_audio
    from test_listening_review_journey import play_original
    from test_voice_runtime_binding import submit
    settings, db, gateway, review, original_prompt, clock = next_day_case(media, tmp_path, monkeypatch)
    with ExitStack() as stack:
        def reopen():
            stack.close()
            return stack.enter_context(TestClient(create_app(settings, model_gateway=gateway),
                headers={'X-HSC-Session': settings.session_token}))
        client = reopen()
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'legacy-journey'}).json()['session_id']
        old = legacy_plan(client, db, monkeypatch, sid)
        with db.connect() as c:
            old_decisions = [tuple(r) for r in c.execute('SELECT * FROM plan_constraint_decision WHERE plan_id=? ORDER BY rowid', (old['plan_id'],))]
            version, minutes = c.execute('SELECT version,total_minutes FROM daily_plan WHERE plan_id=?', (old['plan_id'],)).fetchone()
        request = request_resume(client, settings, monkeypatch, sid, 'upgrade-original')
        first = execute(client, request)
        assert first['result']['status'] == 'awaiting_resources'
        prompt = first['result']['diagnostic_prompt']['prompt']
        pid = first['result']['plan_item_id']
        assert prompt['prompt_id'] != original_prompt['prompt_id'] and pid != old['plan_item_id']
        assert client.post('/v1/parent/setup', json={'pin': '2468'}).status_code == 200
        audio, _ = bind_audio(client, db, tmp_path, sid, prompt, key='legacy-current-original')
        assert not audio['qualification']['task_delivery_qualified']
        assert execute(client, request) == first  # Same command never starts after parent supplies audio.
        client = reopen()
        assert execute(client, request) == first
        resumed = request_resume(client, settings, monkeypatch, sid, 'upgrade-ready', prompt)
        started = execute(client, resumed)
        assert started['result']['status'] == 'active', started
        assert started['result']['plan_item_id'] == pid
        assert started['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        assert prompt['item']['stimulus_text'] not in started['response_text']
        with db.connect() as c:
            assert tuple(c.execute('SELECT version,total_minutes FROM daily_plan WHERE plan_id=?', (old['plan_id'],)).fetchone()) == (version + 1, minutes)
            assert [tuple(r) for r in c.execute('SELECT * FROM plan_constraint_decision WHERE plan_id=? ORDER BY rowid', (old['plan_id'],))][:len(old_decisions)] == old_decisions
            prior = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (old['plan_item_id'],)).fetchone())
            assert prior == {**old, 'status': 'cancelled'}
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE media_id=? AND operation IN ('playback','playback_settled')", (audio['media_id'],)).fetchone()[0] == 0
        play_original(client, sid, audio, media[5], clock)
        voice = capture(client, settings, monkeypatch, sid, '雨衣', prompt, 'legacy-answer-voice')
        heard = interpret(client, sid, '雨衣', 'legacy-answer-turn', voice)
        if heard['intent'] == 'pending_answer_clarification':
            execute(client, heard)
            confirmed = capture(client, settings, monkeypatch, sid, '刚才那句就是我的答案', prompt, 'legacy-confirm-voice')
            heard = interpret(client, sid, '刚才那句就是我的答案', 'legacy-confirm-turn', confirmed)
            assert heard['action']['source_voice_input_id'] == voice
        key = f"diagnostic-answer:{prompt['prompt_id']}:{heard['agent_turn_id']}"
        answer = submit(client, sid, prompt, heard, answer='雨衣', key=key)
        assert answer.status_code == 200, answer.text
        attempt = answer.json()['attempt']
        assert attempt['score_status'] == 'verified_local' and attempt['correctness'] == 1
        completed = client.post(f'/v1/plan-items/{pid}/complete', json={'actual_minutes': 1, 'client_action_id': 'legacy-finish'})
        assert completed.status_code == 200, completed.text
        client = reopen()
        assert execute(client, request) == first
        replayed_start = execute(client, resumed)
        assert replayed_start['result']['plan_item_id'] == pid
        assert replayed_start['result']['recorded_receipt']['status'] == 'started'
        assert submit(client, sid, prompt, heard, answer='雨衣', key=key).json()['attempt']['attempt_id'] == attempt['attempt_id']
        with db.connect() as c:
            assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (review['review_task_id'],)).fetchone()[0] == 'completed'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (review['review_task_id'],)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1
