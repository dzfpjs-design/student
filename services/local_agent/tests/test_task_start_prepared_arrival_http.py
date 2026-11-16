"""Actual synthetic ASR, original prompt preparation, timer and receipt boundary."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.repository import StudyRepository
from pet_listening_fixture import listening_resources
from test_pet_task_execution import generated, state
from test_natural_task_navigation_http import Offline, hear


def setup_case(tmp_path, skill):
    db, sid, pid, _, _ = generated(tmp_path, skill)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    return db, sid, pid, settings


@pytest.mark.parametrize('skill', ['listening', 'reading'])
def test_original_prepared_question_starts_and_waiting_child_receipt_replays(
        tmp_path, monkeypatch, listening_resources, skill):
    db, sid, pid, settings = setup_case(tmp_path, skill)
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        heard, voice = hear(client, settings, monkeypatch, sid, '开始下一项')
        assert heard['runtime_capability'] == 'task.start', heard
        url = f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        result = client.post(url)
        assert result.status_code == 200, result.text
        prompt = result.json()['execution']['result']['diagnostic_prompt']['prompt']
        with db.connect() as c:
            row = c.execute('''SELECT p.status,s.status FROM agent_plan p JOIN agent_step s USING(plan_id)
                WHERE p.agent_turn_id=?''', (heard['agent_turn_id'],)).fetchone()
            assert tuple(row) == ('waiting_child', 'completed')
        assert state(db, pid)['status'] == 'active'
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        replay = client.post(url)
        assert replay.status_code == 200, replay.text
        execution = replay.json()['execution']
        assert execution['replayed'] and execution['replay_audio_allowed'] is False
        assert execution['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        assert state(db, pid)['status'] == 'paused'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id=?", (pid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            action = json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (heard['agent_turn_id'],)).fetchone()[0])
            assert action['voice_input_id'] == voice


@pytest.mark.parametrize('change', ['source', 'plan_version', 'cancel', 'journal', 'prompt_owner', 'prepare_key', 'prompt_version', 'new_voice'])
def test_preparation_does_not_excuse_changed_task_or_request(
        tmp_path, monkeypatch, listening_resources, change):
    db, sid, pid, settings = setup_case(tmp_path, 'listening')
    with TestClient(create_app(settings, model_gateway=Offline()), raise_server_exceptions=False) as client:
        heard, _ = hear(client, settings, monkeypatch, sid, '开始下一项')
        original = StudyRepository.start_plan_item
        def raced(self, *args, **kwargs):
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM pet_media_binding').fetchone()[0] == 1
                if change == 'source':
                    c.execute("UPDATE plan_item SET source_ref='replaced' WHERE plan_item_id=?", (pid,))
                elif change == 'plan_version':
                    c.execute('UPDATE daily_plan SET version=version+1 WHERE session_id=?', (sid,))
                elif change == 'cancel':
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (heard['agent_turn_id'],))
                elif change == 'journal':
                    c.execute("UPDATE voice_control_intent_request SET status='cancelled' WHERE session_id=?", (sid,))
                elif change == 'prompt_owner':
                    c.execute("UPDATE diagnostic_prompt SET child_id='child-junior-3'")
                elif change == 'prepare_key':
                    c.execute("UPDATE diagnostic_prompt SET prepare_action_id='another-command'")
                elif change == 'prompt_version':
                    c.execute("UPDATE diagnostic_prompt SET item_version='replaced'")
                c.commit()
            if change == 'new_voice':
                # A concurrent request needs its own TestClient portal, not a
                # recursive call into the execute request's event-loop thread.
                other_request = TestClient(client.app)
                hear(other_request, settings, monkeypatch, sid, '别开始', key='newer-stop')
            return original(self, *args, **kwargs)
        monkeypatch.setattr(StudyRepository, 'start_plan_item', raced)
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id=?", (pid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert state(db, pid)['status'] == 'pending'


@pytest.mark.parametrize('change', ['cancel', 'quote', 'source', 'child'])
def test_waiting_child_receipt_never_revives_a_changed_source_or_owner(tmp_path, monkeypatch, change):
    db, sid, pid, settings = setup_case(tmp_path, 'reading')
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        heard, _ = hear(client, settings, monkeypatch, sid, '开始下一项')
        url = f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        result = client.post(url)
        assert result.status_code == 200, result.text
    with TestClient(create_app(settings, model_gateway=Offline()), raise_server_exceptions=False) as client:
        with db.connect() as c:
            if change == 'cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (heard['agent_turn_id'],))
            elif change == 'quote':
                c.execute("UPDATE agent_turn SET transcript='不同原话' WHERE agent_turn_id=?", (heard['agent_turn_id'],))
            elif change == 'source':
                c.execute("UPDATE plan_item SET source_ref='replaced' WHERE plan_item_id=?", (pid,))
            else:
                c.execute("UPDATE diagnostic_prompt SET child_id='child-junior-3'")
            c.commit()
            before = tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())
        result = client.post(url)
        assert result.status_code == 409, result.text
        with db.connect() as c:
            assert tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()) == before
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id=?", (pid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
