from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_continuation_lifecycle32 import prepared_root, begin


def test_runtime_exposes_readonly_unknown_receipt_after_restart(tmp_path):
    db, sid, settings, gateway = prepared_root(tmp_path, device=True)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        next_id = begin(client, sid)
        admitted = client.post(f'/v1/sessions/{sid}/agent-turns/{next_id}/continuation-admission')
        assert admitted.json()['status'] == 'admitted'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        with db.connect() as c:
            original = [tuple(row) for row in c.execute('SELECT * FROM agent_turn ORDER BY rowid')]
        result = client.get(f'/v1/sessions/{sid}/agent-runtime')
        assert result.status_code == 200
        notice = result.json().get('continuation')
        assert notice and notice['status'] == 'awaiting_receipt', result.json()
        assert notice['session_id'] == sid and notice['next_turn_id'] == next_id
        assert notice['replay_allowed'] is False and notice['cloud_transmission_allowed'] is False
        with db.connect() as c:
            assert [tuple(row) for row in c.execute('SELECT * FROM agent_turn ORDER BY rowid')] == original
    assert len(gateway.requests) == 1
