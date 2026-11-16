"""Local HTTP receipt lookup must not repeat models, actions or private text."""
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, start


def test_real_interpretation_receipt_survives_restart_without_replay(environment):
    settings, db = environment
    action = 'receipt-original-operation'
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '这是只留在本机的测试原话', 'client_action_id': action})
        assert response.status_code == 200, response.text
        with db.connect() as c:
            original = list(c.execute('SELECT * FROM agent_turn'))
        route = f'/v1/sessions/{sid}/agent-request-receipts/{action}'
        first = client.get(route)
        assert first.status_code == 200
        assert first.headers['cache-control'] == 'no-store'
        payload = first.json()
        assert payload['status'] in {'interpreted', 'recorded_completed'}
        assert payload['replay_allowed'] is False and payload['mastery_update_allowed'] is False
        assert payload['cloud_transmission_allowed'] is False
        assert '这是只留在本机的测试原话' not in first.text
        assert 'action_json' not in first.text and 'agent_turn_id' not in first.text
        with db.connect() as c:
            assert [tuple(x) for x in c.execute('SELECT * FROM agent_turn')] == [tuple(x) for x in original]
    with TestClient(create_app(settings)) as client:
        assert client.get(route).json() == payload
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM agent_turn WHERE client_action_id=?', (action,)).fetchone()[0] == 1


def test_receipt_lookup_has_no_arrival_side_effect_and_is_session_scoped(environment):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        with db.connect() as c:
            count = c.execute('SELECT COUNT(*) FROM learning_input_arrival').fetchone()[0]
        empty = client.get(f'/v1/sessions/{sid}/agent-request-receipts/unknown-operation')
        assert empty.status_code == 200 and empty.json()['status'] == 'unconfirmed'
        assert '不能据此断定没有执行' in empty.json()['response_text']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_arrival').fetchone()[0] == count
        assert client.get('/v1/sessions/other-session/agent-request-receipts/unknown-operation').status_code == 404
        assert client.get(f'/v1/sessions/{sid}/agent-request-receipts/invalid!').status_code == 422


def test_other_child_cannot_read_receipt_and_received_does_not_claim_running(environment):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        action = 'first-child-request'
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '请核对学习安排', 'client_action_id': action})
        assert response.status_code == 200
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': 'child-junior-3', 'client_action_id': 'switch-receipt-owner'})
        assert switched.status_code == 200, switched.text
        other = switched.json()['session_id']
        hidden = client.get(f'/v1/sessions/{other}/agent-request-receipts/{action}').json()
        assert hidden['status'] == 'unconfirmed'
        from home_study_agent.learning_input_revision import LearningInputRevisionService
        LearningInputRevisionService(db).capture_arrival(other, 'arrived-without-turn', '未完成处理的合成原话')
        pending = client.get(f'/v1/sessions/{other}/agent-request-receipts/arrived-without-turn').json()
        assert pending['status'] == 'received'
        assert '不代表它仍在运行' in pending['response_text']


def test_terminal_receipt_does_not_infer_mastery_or_return_old_content(environment):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        action = 'terminal-request-test'
        assert client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '一段私有测试原话', 'client_action_id': action}).status_code == 200
        for state in ['completed', 'failed']:
            with db.connect() as c:
                c.execute('UPDATE agent_turn SET tool_status=? WHERE client_action_id=?', (state, action))
                c.commit()
            result = client.get(f'/v1/sessions/{sid}/agent-request-receipts/{action}').json()
            assert result['status'] == 'recorded_' + state
            assert result['mastery_update_allowed'] is False and result['replay_allowed'] is False
            assert '私有测试原话' not in str(result)
