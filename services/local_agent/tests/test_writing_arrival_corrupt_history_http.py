"""Corrupt local history must not disable unrelated ASR/HTTP commands.

The original obligation comes from real paper/feedback/day closure. Only the
recognizer and model transport are controlled; no real media or network is used.
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent import writing_daily_followup
from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment
from test_voice_runtime_binding import capture
from test_writing_next_day_journey import next_day_ready


def corrupt(case, fault, monkeypatch):
    with case.db.connect() as c:
        if fault.startswith('promise'):
            value = '{broken' if fault == 'promise_json' else '[]'
            c.execute('UPDATE agent_commitment SET payload_json=? WHERE commitment_id=?',
                      (value, case.commitment['commitment_id']))
        elif fault.startswith('rubric'):
            value = '{broken' if fault == 'rubric_json' else '[]'
            c.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?',
                      (value, case.journey.prompt['item']['diagnostic_item_id']))
        c.commit()
    if fault == 'missing_task':
        # A scoped lookup returning no row is a dependency failure, not permission
        # to substitute another task. Keep the real arrival-selected item intact.
        original = writing_daily_followup._task
        monkeypatch.setattr(writing_daily_followup, '_task', lambda c, child, sid, item_id:
                            None if item_id == case.task['plan_item_id'] else original(c, child, sid, item_id))


def say(case, monkeypatch, quote, key):
    voice = capture(case.client, case.settings, monkeypatch, case.sid, quote, key=key+'-voice')
    case.voice_request = {
        'transcript': quote, 'client_action_id': key, 'voice_input_id': voice,
        'cloud_text_allowed': True}
    return case.client.post(f'/v1/sessions/{case.sid}/agent-turn', json=case.voice_request)


@pytest.mark.parametrize('fault', ['promise_json', 'promise_shape', 'rubric_json', 'rubric_shape', 'missing_task'])
def test_corrupt_daily_writing_source_does_not_disable_unrelated_voice(next_day_ready, monkeypatch, fault):
    case = next_day_ready
    case.client._transport.raise_server_exceptions = True
    monitor = case.client.post(f'/v1/sessions/{case.sid}/document-monitor/start',
                               json={'client_action_id': 'bad-history-monitor'})
    assert monitor.status_code == 200, monitor.text
    corrupt(case, fault, monkeypatch)
    gateway = case.journey.gateway
    before = len(gateway.requests)
    stopped = say(case, monkeypatch, '暂停高拍仪', 'bad-history-stop')
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()['action']['type'] == 'control_document_monitor', stopped.text
    assert stopped.json()['action']['command'] == 'pause'
    assert len(gateway.requests) == before

    quote = '今天在学校见到朋友很开心'
    original_complete = gateway.complete

    async def chat(request):
        gateway.requests.append(request)
        if request.request_type == 'agent_plan':
            planner = Planner('conversation.respond', {'response': '听起来你今天很开心。'}, quote,
                              role='question', objective_type='companion')
            planner.payload['objective'] = {'mode': 'none'}
            return await planner.complete(request)
        if request.request_type == 'voice_interaction':
            return ModelResult(content=json.dumps({'role': 'question', 'control': None,
                'quote': quote, 'confidence': .99}), provider='fixture', model='fixture', latency_ms=1)
        return await original_complete(request)

    monkeypatch.setattr(gateway, 'complete', chat)
    chatted = say(case, monkeypatch, quote, 'bad-history-chat')
    assert chatted.status_code == 200, chatted.text
    assert chatted.json()['runtime_capability'] == 'conversation.respond', chatted.text
    reply = case.client.post(f"/v1/agent-turns/{chatted.json()['agent_turn_id']}/execute")
    assert reply.status_code == 200, reply.text
    assert '听起来你今天很开心' in reply.text

    monkeypatch.setattr(gateway, 'complete', original_complete)
    gateway.control, gateway.quote = 'task.start', '开始今天的写作复习'
    blocked = say(case, monkeypatch, gateway.quote, 'bad-history-start')
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()['code'] == 'ANSWER_WRITING_DAILY_SOURCE_UNAVAILABLE', blocked.text
    for request in gateway.requests[before:]:
        sent = json.dumps({'messages': request.messages, 'context': request.context_manifest}, ensure_ascii=False)
        assert 'daily_writing_arrival' not in sent
        assert case.commitment['commitment_id'] not in sent
        assert '{broken' not in sent
    with case.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (case.sid,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type=\'started\'',
                         (case.task['plan_item_id'],)).fetchone()[0] == 0
        snapshot = json.loads(c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id=?',
                                       ('bad-history-stop',)).fetchone()[0])
        record = next(entry['record'] for entry in snapshot if entry['kind'] == writing_daily_followup.ARRIVAL_KIND)
        assert record['source']['status'] == 'blocked'
        assert record['source']['action'] is None
    prior = len(gateway.requests)
    privacy = say(case, monkeypatch, '停止全部监听', 'bad-history-privacy-stop')
    assert privacy.status_code == 200, privacy.text
    assert privacy.json()['action']['type'] == 'privacy_pause'
    assert len(gateway.requests) == prior


def test_repaired_source_requires_new_arrival_after_restart(next_day_ready, monkeypatch):
    case = next_day_ready
    gateway = case.journey.gateway
    original_complete = gateway.complete
    cancel_quote = '先取消刚才这次开始请求，保留原来的复习安排'

    async def cancel(request):
        if request.request_type == 'agent_plan':
            planner = Planner('conversation.respond', {'response': '原复习安排保留。'}, cancel_quote)
            planner.payload['objective'] = {'mode': 'cancel'}
            return await planner.complete(request)
        return await original_complete(request)

    monkeypatch.setattr(gateway, 'complete', cancel)
    cancelled = say(case, monkeypatch, cancel_quote, 'cancel-fixture-prepared-start')
    assert cancelled.status_code == 200, cancelled.text
    with case.db.connect() as c:
        goal_id = case.turn['action']['agent_runtime']['objective_id']
        assert c.execute('SELECT status FROM agent_objective WHERE objective_id=?', (goal_id,)).fetchone()[0] == 'cancelled'
        assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',
                         (case.commitment['commitment_id'],)).fetchone()[0] in {'pending', 'due'}
    monkeypatch.setattr(gateway, 'complete', original_complete)
    corrupt(case, 'promise_json', monkeypatch)
    denied = say(case, monkeypatch, '开始今天的写作复习', 'unavailable-before-restart')
    assert denied.status_code == 409, denied.text
    assert denied.json()['code'] == 'ANSWER_WRITING_DAILY_SOURCE_UNAVAILABLE'
    request = dict(case.voice_request)
    with case.db.connect() as c:
        frozen = c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id=?',
                           (request['client_action_id'],)).fetchone()[0]
        # Restore the exact synthetic record, never fill an old arrival from it.
        c.execute('UPDATE agent_commitment SET payload_json=? WHERE commitment_id=?',
                  (case.commitment['payload_json'], case.commitment['commitment_id']))
        c.commit()
    case.stack.close()
    # The closed day's report outbox may resume on boot independently of voice.
    voice_routes = {'agent_plan', 'voice_interaction'}
    sent_before_replay = sum(r.request_type in voice_routes for r in gateway.requests)
    with TestClient(create_app(case.settings, model_gateway=case.journey.gateway)) as client:
        case.client = client
        replay = client.post(f'/v1/sessions/{case.sid}/agent-turn', json=request)
        assert replay.status_code == 409, replay.text
        assert replay.json()['code'] == 'ANSWER_WRITING_DAILY_SOURCE_UNAVAILABLE'
        assert sum(r.request_type in voice_routes for r in gateway.requests) == sent_before_replay
        with case.db.connect() as c:
            assert c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id=?',
                             (request['client_action_id'],)).fetchone()[0] == frozen
        fresh = say(case, monkeypatch, '开始今天的写作复习', 'repaired-new-request')
        assert fresh.status_code == 200, fresh.text
        started = client.post(f"/v1/agent-turns/{fresh.json()['agent_turn_id']}/execute")
        assert started.status_code == 200, started.text
        result = started.json()['execution']['result']
        assert result['plan_item_id'] == case.task['plan_item_id']
        assert result['diagnostic_prompt']['status'] == 'waiting_answer'
        assert result['diagnostic_prompt']['prompt']['origin_session_id'] == case.sid
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_source_capture_does_not_hide_database_failures(monkeypatch):
    def failed(*args):
        raise sqlite3.OperationalError('synthetic disk read failure')
    monkeypatch.setattr(writing_daily_followup, '_read_scheduled_source', failed)
    with pytest.raises(sqlite3.OperationalError, match='synthetic disk read failure'):
        writing_daily_followup._capture_scheduled_source(None, None,
            {'source_type': 'agent_retention', 'source_ref': 'exact-obligation', 'plan_item_id': 'exact-task'})
