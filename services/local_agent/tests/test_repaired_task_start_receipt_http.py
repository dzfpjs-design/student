"""A verified one-shot plan repair retains the real task-start receipt boundary."""
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_diagnostic_prompt_http import environment
from test_natural_task_navigation_http import setup_case, hear, execute
from test_cloud_first_agent import Planner


def repaired_application(settings, db, requests, tamper=None):
    settings = replace(settings, model_api_key='synthetic-not-a-key', model_provider='qwen',
                       model_name='isolated-repair', voice_intent_model_name='isolated-repair')
    PrivacyLifecycleService(db, settings.data_dir).initialize()
    PrivacyLifecycleService(db, settings.data_dir).update_preferences({'cloud_model_text_allowed': True})

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        original = json.loads(body['messages'][1]['content'])
        quote = original['child_utterance']
        payload = Planner('task.start', {'start_selector': {
            'subject': None, 'task_kind': None, 'source_type': None, 'target_quote': quote,
        }}, quote).payload
        if len(requests) == 1:
            payload['objective']['type'] = 'learning'  # rejected for task.start
        elif tamper:
            with db.connect() as c:
                if tamper == 'child':
                    c.execute("UPDATE model_call SET child_id='child-junior-3' WHERE purpose='agent_plan'")
                elif tamper == 'status':
                    c.execute("UPDATE model_call SET status='completed' WHERE purpose='agent_plan'")
                elif tamper == 'error':
                    c.execute("UPDATE model_call SET error_code='MODEL_AGENT_PLAN_FAILED' WHERE purpose='agent_plan'")
                elif tamper == 'context':
                    c.execute("UPDATE model_call SET context_manifest_json='{}' WHERE purpose='agent_plan'")
                elif tamper == 'policy':
                    c.execute("UPDATE model_call SET policy_version='unrelated-policy' WHERE purpose='agent_plan'")
                c.commit()
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(payload)}}]})

    return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))


def test_verified_repair_starts_exact_task_and_replays_without_duplicate(environment, monkeypatch):
    settings, db = environment
    db.initialize()
    requests = []
    app = repaired_application(settings, db, requests)
    with TestClient(app) as client:
        sid, repo = setup_case(client, db, 'completed')
        turn, voice = hear(client, settings, monkeypatch, sid, '开始下一项', cloud=True)
        assert turn['runtime_capability'] == 'task.start'
        result = execute(client, turn)
        assert result['execution']['result']['plan_item_id'] == 'control-task-2'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'active'
        assert len(requests) == 2
        with db.connect() as c:
            calls = c.execute('SELECT purpose,status,context_manifest_json FROM model_call ORDER BY created_at').fetchall()
            assert [(r['purpose'], r['status']) for r in calls] == [('agent_plan', 'failed'), ('agent_plan_repair', 'completed')]
            assert json.loads(calls[1]['context_manifest_json'])['task_start_selection']['utterance_sha256']
            event_count = c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id='control-task-2' AND event_type='started'").fetchone()[0]
    with TestClient(repaired_application(settings, db, requests)) as restarted:
        replay = restarted.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '开始下一项', 'client_action_id': 'navigation', 'voice_input_id': voice})
        assert replay.status_code == 200, replay.text
        assert replay.json()['agent_turn_id'] == turn['agent_turn_id']
        execute(restarted, replay.json())
        assert len(requests) == 2
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id='control-task-2' AND event_type='started'").fetchone()[0] == event_count == 1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('tamper', ['child', 'status', 'error', 'context', 'policy'])
def test_repair_cannot_borrow_an_unrelated_or_invalid_source_call(environment, monkeypatch, tamper):
    settings, db = environment
    db.initialize()
    requests = []
    with TestClient(repaired_application(settings, db, requests, tamper), raise_server_exceptions=False) as client:
        sid, repo = setup_case(client, db, 'completed')
        from test_voice_runtime_binding import capture
        voice = capture(client, settings, monkeypatch, sid, '开始下一项', None, 'tampered-repair-voice')
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '开始下一项', 'client_action_id': 'tampered-repair', 'voice_input_id': voice})
        assert response.status_code == 409, response.text
        assert 'TASK_START_PLAN_RECEIPT_CHANGED' in response.text
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM agent_turn WHERE client_action_id='tampered-repair'").fetchone()[0] == 0
