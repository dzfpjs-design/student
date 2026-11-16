"""Open speech stays a recoverable original candidate, not an implicit score."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_content_free_voice_control_http import ControlGateway
from test_diagnostic_prompt_http import environment, start
from test_voice_runtime_binding import capture, interpret, submit


def test_open_speaking_candidate_survives_control_unknown_and_restart(environment, monkeypatch):
    settings, db = environment
    original = 'I like reading in the library because it is quiet. I read story books there after school with my friends.'
    gateway = ControlGateway(original, None, role='unknown')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': 'diag-pet-speaking-2', 'client_action_id': 'open-speaking-prompt'})
        assert response.status_code == 200, response.text
        prompt = response.json()['prompt']
        voice = capture(client, settings, monkeypatch, sid, original, prompt)
        heard = interpret(client, sid, original, 'open-speaking-original', voice)
        assert heard['intent'] == 'pending_answer_clarification'
        assert heard['action']['pending_answer_candidate']['answer'] == original
        assert heard['action']['voice_input_id'] == voice
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, original, 'open-speaking-original', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        assert replay['action']['pending_answer_candidate']['answer'] == original
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '刚才那句就是我的答案', 'client_action_id': 'open-speaking-confirmed'})
        assert response.status_code == 200, response.text
        confirmed = response.json()
        assert confirmed['action']['source_voice_input_id'] == voice
        scored = submit(client, sid, prompt, confirmed, answer=original, key='open-speaking-score')
        assert scored.status_code == 200, scored.text
        assert scored.json()['status'] == 'answered'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
    assert len(gateway.requests) == 1
