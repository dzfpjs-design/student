"""Content-free break intent -> actual dispatcher and repository transactions."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.repository import StudyRepository
from test_content_free_voice_control_http import ControlGateway, task_fixture
from test_diagnostic_prompt_http import environment, start
from test_voice_runtime_binding import capture, interpret, prepare
from test_agent_continuation import Gateway


@pytest.mark.parametrize('lost_response', [False, True])
def test_break_and_exact_original_resume_survive_restart_and_lost_response(environment, monkeypatch, lost_response):
    settings, db = environment
    quote = '我想休息一下'
    gateway = ControlGateway(quote, 'break.begin')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        repo = task_fixture(db, sid)
        prompt = prepare(client, sid)
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'capture-break-begin')
        heard = interpret(client, sid, quote, 'break-voice-begin', voice)
        assert heard['action']['type'] == 'begin_break'
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert repo.get_session(sid)['state'] == 'break'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        gateway.quote = '缓过来了，把刚才停下的接着做吧'
        gateway.control = 'break.resume'
        voice = capture(client, settings, monkeypatch, sid, gateway.quote, prompt, 'capture-break-resume')
        heard = interpret(client, sid, gateway.quote, 'break-voice-resume', voice)
        assert heard['action']['type'] == 'resume_study'
        url = f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        original = StudyRepository.resume_after_break
        if lost_response:
            def fail(self, *args, **kwargs):
                original(self, *args, **kwargs)
                raise OSError('lost break resume response')
            monkeypatch.setattr(StudyRepository, 'resume_after_break', fail)
            assert client.post(url).status_code == 500
            monkeypatch.setattr(StudyRepository, 'resume_after_break', original)
        else:
            response = client.post(url)
            assert response.status_code == 200, response.text
        assert repo.get_session(sid)['state'] == 'study'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
        repo.pause_plan_item('control-task-1', 'newer-child-pause')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        response = client.post(url)
        assert response.status_code == 200, response.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM break_control_receipt').fetchone()[0] == 2
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1
    assert len(gateway.requests) == 2


def test_break_arrival_does_not_follow_new_active_task(environment, monkeypatch):
    settings, db = environment
    quote = '我想休息一下'
    with TestClient(create_app(settings, model_gateway=ControlGateway(quote, 'break.begin'))) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        repo = task_fixture(db, sid)
        prompt = prepare(client, sid)
        voice = capture(client, settings, monkeypatch, sid, quote, prompt)
        heard = interpret(client, sid, quote, 'break-target-arrival', voice)
        repo.pause_plan_item('control-task-1', 'different-command-pause')
        repo.start_plan_item('control-task-2', 'different-command-start')
        response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        assert repo.get_session(sid)['state'] == 'study'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'active'


def test_general_planner_break_reuses_verified_full_request_without_second_model_call(environment, monkeypatch):
    settings, db = environment
    quote = '把学习这一段先留在这里，等我缓过来再往下走'
    gateway = Gateway({'objective': {'mode': 'none', 'phase': 'execute'},
        'next_step': {'capability': 'break.begin', 'arguments': {}},
        'spoken_transition': '我会保留这项任务的进度。', 'requires_child_reply': False, 'confidence': .99,
        'utterance_binding': {'role': 'task_request', 'quote': quote}})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        repo = task_fixture(db, sid)
        voice = capture(client, settings, monkeypatch, sid, quote)
        heard = interpret(client, sid, quote, 'general-semantic-break', voice)
        assert heard['action']['type'] == 'begin_break'
        assert heard['action']['source'] == 'verified_general_plan_control'
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert repo.get_session(sid)['state'] == 'break'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
    assert len(gateway.requests) == 1 and gateway.requests[0].request_type == 'agent_plan'
