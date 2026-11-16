"""Synthetic PCM/controlled model; real ASR, two activity turns and dispatcher."""
from contextlib import ExitStack
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.companion_activity_voice import CompanionActivityVoiceService
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_companion_goal_task_arrival_http import start_goal
from test_pending_question_activity_control import POLITE, capture_quote, decision
from test_pet_task_execution import CHILD, generated
from test_voice_runtime_binding import interpret


DECLINE = '这个活动先不做了'
FEEDBACK = '不好玩。'


@pytest.fixture
def active_story(tmp_path, monkeypatch):
    case = generated(tmp_path, 'reading')
    db, sid, pid, _, _ = case
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        model_provider='qwen', model_api_key='synthetic-03504f547a5a2fc55aec', model_name='isolated-model',
        voice_intent_model_name='isolated-model')
    requests = []
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if '只分类当前原话' in body['messages'][0]['content']:
            quote = json.loads(body['messages'][-1]['content'])['transcript']
            if quote == POLITE:
                content = decision(quote, 'interactive_story')
            elif quote in {DECLINE, FEEDBACK}:
                feedback = {'operation': 'decline'} if quote == DECLINE else {
                    'operation': 'feedback', 'execution': 'unknown', 'experience': 'neutral'}
                content = json.dumps({'role': 'control', 'control': 'companion.activity_feedback',
                    'quote': quote, 'confidence': .99, 'activity_feedback': feedback})
            else:
                content = json.dumps({'role': 'activity_reply', 'control': None, 'quote': quote, 'confidence': .99})
        else:
            content = '故事来到一扇门前，你想先敲门还是看看窗户？'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    def app():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(app(), raise_server_exceptions=False))
        prompt = start_goal(client, case, CHILD)
        voice = capture_quote(client, settings, monkeypatch, sid, CHILD, prompt, POLITE, 'story-start-audio')
        start = interpret(client, sid, POLITE, 'story-start', voice)
        assert client.post(f"/v1/agent-turns/{start['agent_turn_id']}/execute").status_code == 200
        for index, quote in enumerate(('我想先敲敲门。', '我会先问问里面有没有人。')):
            focus = client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus']
            voice = capture_quote(client, settings, monkeypatch, sid, CHILD, prompt, quote,
                f'story-audio-{index}', focus=focus)
            reply = interpret(client, sid, quote, f'story-reply-{index}', voice)
            assert reply['action']['source'] == 'cloud_companion', reply
        with db.connect() as c:
            story = dict(c.execute('SELECT * FROM companion_activity').fetchone())
            assert (story['status'], story['turn_count']) == ('active', 2)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        def heard(quote=DECLINE):
            focus = client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus']
            voice = capture_quote(client, settings, monkeypatch, sid, CHILD, prompt, quote, 'stop-audio', focus=focus)
            turn = interpret(client, sid, quote, 'stop-story', voice)
            assert turn['runtime_capability'] == 'companion.activity_feedback', turn
            return turn, f"/v1/agent-turns/{turn['agent_turn_id']}/execute"
        yield db, sid, pid, story, client, app, heard, stack.close, requests


def assert_no_learning_side_effect(db, pid):
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'paused'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_captured_decline_stops_exact_activity_before_response_and_survives_restart(active_story):
    db, sid, pid, story, client, app, heard, close, requests = active_story
    _, url = heard()
    response = client.post(url)
    assert response.status_code == 200, response.text
    with db.connect() as c:
        stopped = dict(c.execute('SELECT * FROM companion_activity WHERE activity_id=?', (story['activity_id'],)).fetchone())
        assert stopped['status'] == 'cancelled'
        assert stopped['turn_count'] == 2 and stopped['end_reason'] == 'child_requested'
    assert client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus'] is None
    value = response.json()['execution']['result']
    assert value['activity_stop']['status'] == 'stopped'
    assert value['activity_stop']['source_id'] == story['activity_id']
    assert value['activity']['lifecycle']['ended'] is True
    assert not value['activity']['habit_evidence_eligible']
    assert_no_learning_side_effect(db, pid)
    assert client.post(url).json()['execution'] == response.json()['execution']
    count = len(requests)
    close()
    with TestClient(app()) as restarted:
        assert restarted.post(url).json()['execution'] == response.json()['execution']
        assert restarted.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus'] is None
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM companion_activity WHERE activity_id=?', (story['activity_id'],)).fetchone()) == stopped
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event WHERE kind='feedback'").fetchone()[0] == 1
    assert len(requests) == count
    assert_no_learning_side_effect(db, pid)


def test_negative_experience_feedback_does_not_mean_decline(active_story):
    db, sid, pid, story, client, _, heard, _, _ = active_story
    _, url = heard(FEEDBACK)
    response = client.post(url)
    assert response.status_code == 200, response.text
    with db.connect() as c:
        assert c.execute('SELECT status FROM companion_activity WHERE activity_id=?', (story['activity_id'],)).fetchone()[0] == 'active'
    assert client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus'] is not None
    assert_no_learning_side_effect(db, pid)


@pytest.mark.parametrize('restart', [False, True])
def test_decline_write_failure_rolls_back_feedback_and_stop_then_same_key_recovers(active_story, monkeypatch, restart):
    db, sid, pid, story, client, app, heard, close, _ = active_story
    _, url = heard()
    read = CompanionActivityOutcomeService._detail.__func__
    calls = []
    def fail_after_stop(cls, c, child_id, outcome_id, **kwargs):
        result = read(cls, c, child_id, outcome_id, **kwargs)
        if result['activity']['lifecycle']['end_reason'] == 'child_requested' and result['activity']['feedback']:
            assert c.in_transaction
            assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event WHERE kind='feedback'").fetchone()[0] == 1
            calls.append(outcome_id)
            raise sqlite3.OperationalError('isolated failure after source update and feedback')
        return result
    monkeypatch.setattr(CompanionActivityOutcomeService, '_detail', classmethod(fail_after_stop))
    failed = client.post(url)
    assert failed.status_code == 500, failed.text
    assert len(calls) == 1
    with db.connect() as c:
        assert c.execute('SELECT status FROM companion_activity WHERE activity_id=?', (story['activity_id'],)).fetchone()[0] == 'active'
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event WHERE kind='feedback'").fetchone()[0] == 0
    assert_no_learning_side_effect(db, pid)
    monkeypatch.setattr(CompanionActivityOutcomeService, '_detail', classmethod(read))
    if restart:
        close()
        with TestClient(app(), raise_server_exceptions=False) as reopened:
            response = reopened.post(url)
    else:
        response = client.post(url)
    assert response.status_code == 200, response.text
    with db.connect() as c:
        assert c.execute('SELECT status FROM companion_activity WHERE activity_id=?', (story['activity_id'],)).fetchone()[0] != 'active'
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event WHERE kind='feedback'").fetchone()[0] == 1
    assert_no_learning_side_effect(db, pid)


@pytest.mark.parametrize('change', ['cancel', 'source_version', 'voice', 'journal'])
def test_decline_transaction_rechecks_admitted_authority_before_any_stop(active_story, monkeypatch, change):
    db, sid, pid, story, client, _, heard, _, _ = active_story
    turn, url = heard()
    execute = CompanionActivityVoiceService.execute
    calls = []
    def changed(service, tid):
        calls.append(tid)
        with db.connect() as c:
            if change == 'cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (tid,))
            elif change == 'source_version':
                c.execute("UPDATE companion_activity SET topic=topic||' changed' WHERE activity_id=?", (story['activity_id'],))
            elif change == 'voice':
                c.execute("UPDATE voice_capture_binding SET status='cancelled' WHERE voice_input_id=(SELECT json_extract(action_json,'$.voice_input_id') FROM agent_turn WHERE agent_turn_id=?)", (tid,))
            else:
                c.execute("DELETE FROM voice_control_intent_request WHERE request_key=(SELECT json_extract(context_manifest_json,'$.voice_control_binding.request_key') FROM agent_plan WHERE agent_turn_id=?)", (tid,))
            c.commit()
        return execute(service, tid)
    monkeypatch.setattr(CompanionActivityVoiceService, 'execute', changed)
    result = client.post(url)
    assert calls == [turn['agent_turn_id']]
    assert result.status_code == 409, result.text
    with db.connect() as c:
        assert c.execute('SELECT status FROM companion_activity WHERE activity_id=?', (story['activity_id'],)).fetchone()[0] == 'active'
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event WHERE kind='feedback'").fetchone()[0] == 0
    assert_no_learning_side_effect(db, pid)


def test_old_decline_does_not_stop_replacement_activity(active_story, monkeypatch):
    db, sid, pid, story, client, _, heard, _, _ = active_story
    _, url = heard()
    execute = CompanionActivityVoiceService.execute
    replacement = []
    def changed(service, tid):
        replacement.append(AgentRuntimeStore(db).start_companion_activity(
            sid, 'word_chain', 'A different explicit activity', 'replacement-activity', pid))
        return execute(service, tid)
    monkeypatch.setattr(CompanionActivityVoiceService, 'execute', changed)
    result = client.post(url)
    assert result.status_code == 200, result.text
    value = result.json()['execution']['result']
    assert value['activity_stop']['status'] == 'already_ended'
    assert value['activity_stop']['source_id'] == story['activity_id']
    with db.connect() as c:
        new = dict(c.execute('SELECT * FROM companion_activity WHERE activity_id=?', (replacement[0]['activity_id'],)).fetchone())
        assert new['status'] == 'active' and new['ended_at'] is None
    assert_no_learning_side_effect(db, pid)


def test_retracting_feedback_does_not_revive_stopped_source(active_story):
    db, _, pid, story, client, _, heard, _, _ = active_story
    _, url = heard()
    result = client.post(url).json()['execution']['result']
    state = result['activity']
    withdrawn = CompanionActivityOutcomeService(db).retract(CHILD, state['outcome_id'],
        expected_revision=state['revision'], client_action_id='correct-decline-report',
        target_revision=result['event']['revision'], reason='Revise report, not the real stop', actor='parent')
    assert withdrawn['activity']['execution_status'] == 'unknown'
    assert withdrawn['activity']['lifecycle']['ended']
    with db.connect() as c:
        assert c.execute('SELECT status FROM companion_activity WHERE activity_id=?', (story['activity_id'],)).fetchone()[0] == 'cancelled'
    assert_no_learning_side_effect(db, pid)
