"""A spoken deferral owns the unavailable original and survives restarts."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_pet_task_execution import generated, command, CHILD
from test_primary_mixed_voice_journey import MixedGateway
from test_task_source_recovery_http import hear
from test_task_start_choices_http import capture_choice
from test_voice_runtime_binding import interpret


def setup(tmp_path):
    case = generated(tmp_path, 'listening')
    db, sid, pid, _, _ = case
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    config = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    return case, config, MixedGateway('')


def start(client, case):
    tid = command(case, child_id=CHILD)
    response = client.post(f'/v1/agent-turns/{tid}/execute')
    assert response.status_code == 200, response.text
    result = response.json()['execution']['result']
    assert result['status'] == 'awaiting_resources'
    assert result['task_resource_gap']['snapshot']['item']['plan_item_id'] == case[2]
    return tid


def test_saved_asr_can_defer_exact_unavailable_original_after_restart(tmp_path, monkeypatch):
    case, config, gateway = setup(tmp_path)
    db, sid, pid, _, original = case
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        tid = start(client, case)
        ref = client.get(f'/v1/sessions/{sid}/task-start-choice').json()['reference']
        assert ref['offer_turn_id'] == tid
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        assert client.get(f'/v1/sessions/{sid}/task-start-choice').json()['reference'] == ref
        turn = hear(client, config, monkeypatch, gateway, sid, None,
                    '把这项任务延期到明天。', 'defer-original', 'task.reschedule')
        assert turn['action']['type'] == 'reschedule_current_task', turn['response_text']
        url = f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'
        response = client.post(url)
        assert response.status_code == 200, response.text
        assert response.json()['execution']['result']['plan_item_id'] == pid
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        assert client.post(url).status_code == 200
        assert client.get(f'/v1/sessions/{sid}/task-start-choice').json()['reference'] is None
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'rescheduled'
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='rescheduled'").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?',
                         (original['diagnostic_item_id'],)).fetchone()[0] == original['prompt']
    for request in gateway.requests:
        body = json.dumps(request.messages)
        assert original['diagnostic_item_id'] not in body and ref['source_version'] not in body


@pytest.mark.parametrize('change', ['source', 'order', 'plan', 'owner', 'prompt', 'offer', 'audio-ready', 'audio-changed', 'unexecuted'])
def test_changed_unavailable_offer_cannot_defer_later_at_execution(tmp_path, monkeypatch, change):
    case, config, gateway = setup(tmp_path)
    db, sid, pid, _, original = case
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        tid = command(case, child_id=CHILD) if change == 'unexecuted' else start(client, case)
        if change == 'unexecuted':
            assert client.get(f'/v1/sessions/{sid}/task-start-choice').json()['reference'] is None
        turn = hear(client, config, monkeypatch, gateway, sid, None,
                    '把这项任务延期到明天。', 'late-defer', 'task.reschedule')
        if change != 'unexecuted':
            assert turn['action']['type'] == 'reschedule_current_task'
            if change in {'audio-ready', 'audio-changed'}:
                from pet_listening_fixture import reviewed_audio
                prompt = client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()['prompt']
                reviewed_audio(db, tmp_path, prompt)
            with db.connect() as c:
                if change == 'audio-changed':
                    c.execute("UPDATE pet_media_binding SET source_version='changed' WHERE prompt_id=?", (prompt['prompt_id'],))
                elif change == 'audio-ready':
                    pass
                elif change == 'source':
                    c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (original['diagnostic_item_id'],))
                elif change == 'order':
                    c.execute('UPDATE plan_item SET position=position+1 WHERE plan_item_id=?', (pid,))
                elif change == 'plan':
                    c.execute('UPDATE daily_plan SET version=version+1 WHERE session_id=?', (sid,))
                elif change == 'owner':
                    c.execute("UPDATE daily_plan SET child_id='child-junior-3' WHERE session_id=?", (sid,))
                elif change == 'prompt':
                    c.execute("UPDATE diagnostic_prompt SET status='cancelled' WHERE origin_session_id=?", (sid,))
                else:
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (tid,))
                c.commit()
            assert client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute').status_code == 409
        else:
            assert turn['action']['type'] != 'reschedule_current_task'
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'pending'
        assert not c.execute("SELECT 1 FROM plan_item_event WHERE event_type IN ('started','rescheduled')").fetchone()


@pytest.mark.parametrize('mode', ['missing-reference', 'old-recording', 'denied', 'quoted'])
def test_unbound_old_or_noncommand_audio_does_not_acquire_original(tmp_path, monkeypatch, mode):
    case, config, gateway = setup(tmp_path)
    db, sid, pid, _, _ = case
    text = {'denied': '不要把这项任务延期到明天。', 'quoted': '老师说“把这项任务延期到明天”。'}.get(mode, '把这项任务延期到明天。')
    gateway.quote, gateway.control = text, 'task.reschedule'
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        if mode == 'old-recording':
            start(client, case)
            ref = client.get(f'/v1/sessions/{sid}/task-start-choice').json()['reference']
        else:
            start(client, case)
            ref = None if mode == 'missing-reference' else client.get(f'/v1/sessions/{sid}/task-start-choice').json()['reference']
        voice = capture_choice(client, (config, db, sid, gateway), monkeypatch, ref, text, mode+'-capture').json()['voice_input_id']
        if mode == 'old-recording':
            start(client, case)
        turn = interpret(client, sid, text, mode+'-turn', voice)
        assert turn['action']['type'] != 'reschedule_current_task', turn['response_text']
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'pending'
