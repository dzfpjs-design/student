"""Real ASR/Agent/dispatcher on synthetic plans; no network or family records."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResponseError
from home_study_agent.repository import StudyRepository
from test_diagnostic_prompt_http import environment, start
from test_content_free_voice_control_http import task_fixture, ControlGateway
from test_voice_runtime_binding import capture


class Offline:
    configured = False

    async def complete(self, request):
        raise AssertionError('offline must not dispatch a model')


def setup_case(client, db, state):
    sid = start(client)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    repo = task_fixture(db, sid)
    if state == 'completed':
        repo.complete_plan_item('control-task-1', 1, 'first-actually-completed')
    elif state == 'paused':
        repo.pause_plan_item('control-task-1', 'first-actually-paused')
    return sid, repo


def hear(client, settings, monkeypatch, sid, quote, key='navigation', cloud=False, prompt=None):
    voice = capture(client, settings, monkeypatch, sid, quote, prompt, key+'-voice')
    response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': quote, 'client_action_id': key, 'voice_input_id': voice,
        'cloud_text_allowed': cloud})
    assert response.status_code == 200, response.text
    return response.json(), voice


def execute(client, heard):
    response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('quote', ['开始下一项', '开始', '那我们继续吧'])
def test_offline_explicit_next_after_real_completion_starts_exact_next(environment, monkeypatch, quote):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        sid, repo = setup_case(client, db, 'completed')
        heard, _ = hear(client, settings, monkeypatch, sid, quote)
        assert heard['runtime_capability'] == 'task.start', heard
        assert '计时开始' not in heard['response_text']
        result = execute(client, heard)
        assert result['execution']['result']['plan_item_id'] == 'control-task-2'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'completed'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'active'


@pytest.mark.parametrize('state,quote,capability', [
    ('active', '开始下一项', 'conversation.respond'),
    ('active', '那我们继续吧', 'conversation.respond'),
    ('paused', '开始下一项', 'conversation.respond'),
    ('paused', '那我们继续吧', 'task.resume'),
    ('completed', '接下来做什么', 'planning.describe'),
    ('completed', '别开始', 'conversation.respond'),
    ('completed', '他说开始任务', 'conversation.respond'),
    ('active', '上一项没做好', 'conversation.respond'),
])
def test_navigation_does_not_invent_completion_or_execute_reported_speech(environment, monkeypatch, state, quote, capability):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        sid, repo = setup_case(client, db, state)
        with db.connect() as c:
            before = c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
            events = c.execute('SELECT COUNT(*) FROM plan_item_event').fetchone()[0]
        heard, _ = hear(client, settings, monkeypatch, sid, quote)
        assert heard['runtime_capability'] == capability, heard
        execute(client, heard)
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == ('active' if capability == 'task.resume' else state)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == before
            assert c.execute('SELECT COUNT(*) FROM plan_item_event').fetchone()[0] == events + (capability == 'task.resume')
