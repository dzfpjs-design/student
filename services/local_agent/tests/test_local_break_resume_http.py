"""Local explicit resume retains the same write fence as cloud controls."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_agent_continuation import Gateway
from test_content_free_voice_control_http import task_fixture
from test_diagnostic_prompt_http import environment, start
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('quote,execute', [
    ('继续学习', True), ('休息好了，继续学习', True),
    ('先别继续学习', False), ('题目写着继续学习', False),
    ('继续学习，先不要执行', False),
])
def test_explicit_local_break_resume_keeps_original_and_never_authorizes_negation(environment, monkeypatch, quote, execute):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            c.commit()
        repo = task_fixture(db, sid)
        repo.begin_break(sid, 'break-fixture')
        voice = capture(client, settings, monkeypatch, sid, quote, key='resume-voice')
        heard = interpret(client, sid, quote, 'local-resume', voice)
        assert heard['action']['type'] == ('resume_study' if execute else 'none')
        if execute:
            result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert result.status_code == 200, result.text
            assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
            repo.pause_plan_item('control-task-1', 'later-pause')
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        assert not gateway.requests
    if execute:
        with TestClient(create_app(settings, model_gateway=gateway)) as client:
            replay = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert replay.status_code == 200, replay.text
            assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1


def test_later_stop_revokes_local_break_resume_before_write(environment, monkeypatch):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        repo = task_fixture(db, sid)
        repo.begin_break(sid, 'break-fixture')
        voice = capture(client, settings, monkeypatch, sid, '继续学习', key='resume-voice')
        heard = interpret(client, sid, '继续学习', 'local-resume', voice)
        stop = capture(client, settings, monkeypatch, sid, '别讲了', key='stop-voice')
        interpret(client, sid, '别讲了', 'stop-later', stop)
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        assert repo.get_session(sid)['state'] == 'break'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
