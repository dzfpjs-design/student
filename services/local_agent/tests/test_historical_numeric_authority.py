"""Parent review of numeric30: original correct speech survives read/restart/end."""
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.learning_state import LearningStateService
from test_diagnostic_prompt_http import environment, start, endpoint, binding
from test_specialized_closed_http import generated_http
from test_spoken_numeric import numeric_generated
import test_specialized_closed_contract as closed


@pytest.mark.parametrize('phase', ['read_only', 'transaction', 'session_ended'])
def test_correct_original_retains_historical_authority(environment, monkeypatch, phase):
    settings, db = environment
    monkeypatch.setattr(closed, 'generated', numeric_generated)
    gateway = closed.ClosedGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, rows = generated_http(client, db)
        row = next(r for r in rows if r['stage'] == 'parallel')
        approved = client.post(f"/v1/parent/specialized-supply/candidates/{row['candidate_id']}/review",
            headers=headers, json={'decision': 'approve', 'note': 'Synthetic scalar review',
            'client_action_id': 'parent-review31'}).json()
        sid = start(client)
        prompt = client.post(endpoint(sid, 'prepare'), json={'track': 'competition_math',
            'preferred_item_id': approved['diagnostic_item_id'], 'client_action_id': 'prepare31'}).json()['prompt']
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '答案是六',
            'client_action_id': 'original31', 'cloud_text_allowed': True}).json()
        assert turn['action']['type'] == 'submit_diagnostic_answer', turn
        response = client.post(endpoint(sid, 'answer'), json={**binding(prompt),
            'answer': turn['action']['answer'], 'client_action_id': 'answer31',
            'authorized_turn_id': turn['agent_turn_id']})
        assert response.status_code == 200, response.text
        receipt = response.json()['attempt']
        assert receipt['correctness'] == 1 and receipt['mastery_update_allowed'], receipt
        if phase == 'session_ended':
            with db.connect() as c:
                c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at='2026-09-11T01:00:00Z' WHERE session_id=?", (sid,))
                c.commit()
        with db.connect() as c:
            if phase != 'read_only':
                c.execute('BEGIN')
            current = LearningStateService._attempt(c, receipt['attempt_id'])
            assert current['correctness'] == 1
            assert current['mastery_update_allowed'], (phase, current['score_status'], current.get('score_reason'))
