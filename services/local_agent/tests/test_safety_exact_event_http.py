from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_agent_continuation import Gateway
from test_pending_answer_arbitration_http import turn


def test_exact_safety_read_and_ack_beyond_dashboard_window_survive_restart(environment):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        sid = start(client)
        for index in range(52):
            turn(client, sid, '有人在学校欺负我，还威胁我别说', f'safety-window-{index}', cloud=False)
        with db.connect() as c:
            event_id = c.execute('SELECT safety_event_id FROM child_safety_event ORDER BY created_at,rowid LIMIT 1').fetchone()[0]
        root = f'/v1/parent/children/{CHILD}/safety-events/{event_id}'
        assert client.get(root).status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        assert client.get(root.replace(CHILD, OTHER)).status_code == 404
        exact = client.get(root)
        assert exact.status_code == 200, exact.text
        assert exact.headers['Cache-Control'] == 'no-store'
        assert exact.json()['safety_event_id'] == event_id
        assert '有人在学校欺负我' not in exact.text
        ack = client.post(f'/v1/parent/safety-events/{event_id}/acknowledge', json={'client_action_id': 'exact-ack'})
        assert ack.status_code == 200, ack.text
        saved = ack.json()
        assert saved['status'] == 'acknowledged'
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        assert client.get(root).json() == saved
        replay = client.post(f'/v1/parent/safety-events/{event_id}/acknowledge', json={'client_action_id': 'exact-ack'})
        assert replay.status_code == 200 and replay.json() == saved
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='child_safety_event_acknowledged'").fetchone()[0] == 1
    assert not gateway.requests
