"""Generated original task, ASR start, committed timer, lost HTTP response."""
import json

import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.repository import StudyRepository
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_natural_task_navigation_http import Offline, hear, execute
from test_pet_task_execution import generated, state
from test_pet_task_resume_http import application as resume_application
from test_voice_runtime_binding import submit


@pytest.mark.parametrize('change', [None, 'source_version', 'cancel_prompt', 'wrong_owner'])
def test_typed_start_lost_receipt_recovers_exact_prompt_without_retiming(tmp_path, monkeypatch, change):
    db, sid, pid, _, original = generated(tmp_path, 'reading')
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=Offline()), raise_server_exceptions=False) as client:
        confirmed = client.post(f'/v1/sessions/{sid}/transition', json={
            'expected_state': 'plan_review', 'target_state': 'study',
            'reason': 'confirmed', 'client_action_id': 'confirm-generated-plan'})
        assert confirmed.status_code == 200, confirmed.text
        heard, voice = hear(client, settings, monkeypatch, sid, '开始下一项')
        assert heard.get('runtime_capability') == 'task.start', heard
        start = StudyRepository.start_plan_item
        def lost(self, *args, **kwargs):
            start(self, *args, **kwargs)
            raise OSError('isolated committed typed start, lost response')
        with monkeypatch.context() as local:
            local.setattr(StudyRepository, 'start_plan_item', lost)
            response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert response.status_code == 500, response.text
        current = client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()
        assert current['status'] == 'waiting_answer', current
        prompt = current['prompt']
        assert prompt['diagnostic_item_id'] == original['diagnostic_item_id']
        with db.connect() as c:
            receipt = dict(c.execute("SELECT * FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone())
        if change == 'cancel_prompt':
            cancelled = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/cancel', json={
                'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']})
            assert cancelled.status_code == 200, cancelled.text
        elif change:
            with db.connect() as c:
                if change == 'source_version':
                    c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?",
                              (prompt['diagnostic_item_id'],))
                else:
                    c.execute("UPDATE diagnostic_prompt SET child_id='child-junior-3' WHERE prompt_id=?", (prompt['prompt_id'],))
                c.commit()
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        if change:
            retry = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert retry.status_code == 409, retry.text
            assert state(db, pid)['status'] == 'paused'
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
                assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1
            return
        same = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '开始下一项', 'client_action_id': 'navigation', 'voice_input_id': voice})
        assert same.status_code == 200, same.text
        assert same.json()['agent_turn_id'] == heard['agent_turn_id']
        result = execute(client, same.json())['execution']
        recovered = result['result'].get('diagnostic_prompt')
        assert recovered is not None, result
        assert recovered['prompt']['prompt_id'] == prompt['prompt_id']
        assert recovered['prompt']['item_version'] == prompt['item_version']
        assert result['replayed'] and result['replay_audio_allowed'] is False
        assert state(db, pid)['status'] == 'paused'
        again = execute(client, same.json())['execution']
        assert again['result']['diagnostic_prompt'] == recovered
        with db.connect() as c:
            assert dict(c.execute("SELECT * FROM plan_item_event WHERE event_id=?", (receipt['event_id'],)).fetchone()) == receipt
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    # A new, explicitly understood continuation, not the old receipt, resumes time.
    from dataclasses import replace
    settings = replace(settings, model_provider='qwen', model_api_key='synthetic-c7645253514c0a467fb7',
                       model_name='isolated-control', voice_intent_model_name='isolated-control')
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    requests = []
    with TestClient(resume_application(settings, requests)) as client:
        continued, _ = hear(client, settings, monkeypatch, sid, '可以继续刚才任务吗？',
                            key='new-resume-request', cloud=True, prompt=prompt)
        assert continued['runtime_capability'] == 'task.resume', continued
        resumed = execute(client, continued)['execution']['result']
        assert resumed['plan_item_id'] == pid and state(db, pid)['status'] == 'active'
        current = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
        assert current['prompt']['item_version'] == prompt['item_version']
        answer, _ = hear(client, settings, monkeypatch, sid, '90', key='original-reading-answer', prompt=prompt)
        scored = submit(client, sid, prompt, answer)
        assert scored.status_code == 200 and scored.json()['status'] == 'answered', scored.text
        complete = client.post(f'/v1/plan-items/{pid}/complete', json={
            'actual_minutes': 1, 'client_action_id': 'complete-original-reading-task'})
        assert complete.status_code == 200 and complete.json()['status'] == 'completed', complete.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='resumed'", (pid,)).fetchone()[0] == 1
    assert len(requests) == 1
    wire = json.dumps(requests, ensure_ascii=False)
    assert all(x not in wire for x in (sid, pid, prompt['prompt_id'], original['prompt']))
