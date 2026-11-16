"""Real voice HTTP and dispatcher resume; only speech/model inference is controlled."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.repository import StudyRepository
from test_content_free_voice_control_http import ControlGateway, task_fixture
from test_diagnostic_prompt_http import environment, start, OTHER
from test_voice_runtime_binding import capture, interpret, prepare, submit


def hear(client, settings, monkeypatch, gateway, sid, prompt, quote, key, control):
    gateway.quote, gateway.control = quote, control
    voice = capture(client, settings, monkeypatch, sid, quote, prompt, key+'-voice')
    return interpret(client, sid, quote, key, voice)


def paused_case(client, settings, db, monkeypatch, gateway):
    sid = start(client)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    repo = task_fixture(db, sid)
    prompt = prepare(client, sid)
    paused = hear(client, settings, monkeypatch, gateway, sid, prompt,
        '手头这项先停一下，保留进度', 'pause-for-resume', 'task.pause')
    result = client.post(f"/v1/agent-turns/{paused['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
    return sid, repo, prompt


def test_paused_task_does_not_consume_a_real_short_answer(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        calls = len(gateway.requests)
        heard = hear(client, settings, monkeypatch, gateway, sid, prompt,
                     '90', 'short-answer-not-task-choice', 'task.resume')
        result = submit(client, sid, prompt, heard, answer='90')
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'answered'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert len(gateway.requests) == calls
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


@pytest.mark.parametrize('lost_receipt', [False, True])
def test_natural_task_resume_actual_write_restart_replay_never_restarts_newer_pause(environment, monkeypatch, lost_receipt):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        before = repo._get_plan_item_by_id('control-task-1')['active_seconds']
        heard = hear(client, settings, monkeypatch, gateway, sid, prompt,
            '刚才手头停下的那项我接着做，计时也继续', 'natural-task-resume', 'task.resume')
        assert heard['action']['type'] == 'resume_paused_task', heard
        assert heard['runtime_capability'] == 'task.resume'
        url = f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        original = StudyRepository.resume_plan_item
        if lost_receipt:
            def fail_after_write(self, *args, **kwargs):
                original(self, *args, **kwargs)
                raise OSError('controlled lost resume response')
            monkeypatch.setattr(StudyRepository, 'resume_plan_item', fail_after_write)
            assert client.post(url).status_code == 500
            monkeypatch.setattr(StudyRepository, 'resume_plan_item', original)
        else:
            result = client.post(url)
            assert result.status_code == 200, result.text
            assert result.json()['execution']['capability'] == 'task.resume'
            # This unrelated pending question must not be adopted as task work.
            assert 'diagnostic_prompt' not in result.json()['execution']['result']
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
        assert repo._get_plan_item_by_id('control-task-1')['active_seconds'] == before
        newer = hear(client, settings, monkeypatch, gateway, sid, prompt,
            '这项再停一下', 'pause-after-resume', 'task.pause')
        assert client.post(f"/v1/agent-turns/{newer['agent_turn_id']}/execute").status_code == 200
    calls = len(gateway.requests)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = client.post(url)
        assert replay.status_code == 200, replay.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert len(gateway.requests) == calls
    for request in gateway.requests:
        body = json.loads(request.messages[-1]['content'])
        assert set(body) == {'transcript', 'state'}
        assert all(type(value) is bool for value in body['state'].values())
        assert prompt['item']['prompt'] not in json.dumps(request.messages)
        assert all(secret not in json.dumps(request.messages) for secret in (sid,prompt['prompt_id'],'control-task-1'))


@pytest.mark.parametrize('change', ['newer_command', 'different_active_item', 'write_time_revocation'])
def test_task_resume_cannot_retarget_or_write_after_revocation(environment, monkeypatch, change):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        heard = hear(client, settings, monkeypatch, gateway, sid, prompt,
            '回到刚才那项，继续计时', 'resume-before-change', 'task.resume')
        assert heard['action']['type'] == 'resume_paused_task', heard
        if change == 'newer_command':
            hear(client, settings, monkeypatch, gateway, sid, prompt,'别讲了','cancel-resume-command','speech.stop')
        elif change == 'different_active_item':
            repo.start_plan_item('control-task-2', 'another-item-started')
        else:
            original = StudyRepository.resume_plan_item
            def revoke_then_write(self, item_id, key, **kwargs):
                assert kwargs['authorized_turn_id'] == heard['agent_turn_id']
                with db.connect() as c:
                    c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
                    c.commit()
                return original(self, item_id, key, **kwargs)
            monkeypatch.setattr(StudyRepository, 'resume_plan_item', revoke_then_write)
        response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0


def test_multiple_paused_tasks_are_announced_then_selected_locally_after_restart(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        repo.start_plan_item('control-task-2','second-task-start')
        repo.pause_plan_item('control-task-2','second-task-pause')
        heard = hear(client, settings, monkeypatch, gateway, sid, prompt,
            '把停下来的任务接着做', 'ambiguous-task-resume', 'task.resume')
        assert heard['action']['type'] == 'none'
        assert heard['action']['source'] == 'task_resume_clarification'
        assert len(heard['action']['task_resume_choices']) == 2
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item WHERE status='active'").fetchone()[0] == 0
    calls = len(gateway.requests)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        chosen = hear(client, settings, monkeypatch, gateway, sid, prompt,
            '第二项', 'choose-paused-task', 'task.resume')
        assert chosen['action']['type'] == 'resume_paused_task', chosen
        result = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'active'
        assert len(gateway.requests) == calls
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['cancelled_offer', 'changed_pause', 'cancel_after_selection', 'other_child'])
def test_paused_task_choice_rechecks_original_offer_and_source(environment, monkeypatch, change):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        repo.start_plan_item('control-task-2', 'choice-second-start')
        repo.pause_plan_item('control-task-2', 'choice-second-pause')
        offered = hear(client, settings, monkeypatch, gateway, sid, prompt,
            '停下的任务我想接着做', 'offer-before-change', 'task.resume')
        assert client.post(f"/v1/agent-turns/{offered['agent_turn_id']}/execute").status_code == 200
        if change == 'changed_pause':
            repo.resume_plan_item('control-task-2', 'independent-newer-resume')
            repo.pause_plan_item('control-task-2', 'independent-newer-pause')
        elif change in {'cancelled_offer', 'other_child'}:
            with db.connect() as c:
                if change == 'cancelled_offer':
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (offered['agent_turn_id'],))
                else:
                    c.execute('UPDATE voice_control_intent_request SET child_id=? WHERE session_id=?', (OTHER, sid))
                c.commit()
        gateway.quote = '第二项'
        voice = capture(client, settings, monkeypatch, sid, gateway.quote, prompt, 'choose-changed-voice')
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': gateway.quote, 'client_action_id': 'choose-changed', 'voice_input_id': voice})
        if change == 'cancel_after_selection':
            assert response.status_code == 200, response.text
            selected = response.json()
            assert selected['action']['type'] == 'resume_paused_task'
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (offered['agent_turn_id'],))
                c.commit()
            response = client.post(f"/v1/agent-turns/{selected['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'paused'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed' AND client_action_id LIKE 'agent-step:%'").fetchone()[0] == 0
