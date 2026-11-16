"""Real completed clarification receipt, HTTP and application restart."""
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, start


def test_real_completed_clarification_does_not_claim_requested_pause(environment):
    settings, db = environment
    action_id = 'actual-clarification31'
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '暂停学习', 'client_action_id': action_id})
        assert turn.status_code == 200, turn.text
        turn = turn.json()
        assert turn['action']['type'] == 'none', turn
        # No server capability is planned for this local clarification. Mirror
        # SessionWorkspace's completed conversation outcome, not a fake execute.
        assert not turn.get('runtime_capability'), turn
        executed = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome",
            json={'final_response': turn['response_text'], 'tool_status': 'completed'})
        assert executed.status_code == 200, executed.text
        route = f'/v1/sessions/{sid}/agent-request-receipts/{action_id}'
        with db.connect() as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM agent_turn')]
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',
                (turn['agent_turn_id'],)).fetchone()[0] == 'completed'
        receipt = client.get(route)
        assert receipt.status_code == 200
        payload = receipt.json()
        assert payload['status'] == 'recorded_completed'
        assert '没有执行学习或设备操作' in payload['response_text']
        assert '已记录为完成' not in payload['response_text']
        assert receipt.headers['cache-control'] == 'no-store'
        assert payload['replay_allowed'] is False
        assert payload['mastery_update_allowed'] is False
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM agent_turn')] == before
    with TestClient(create_app(settings)) as client:
        assert client.get(route).json() == payload
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM agent_turn')] == before
