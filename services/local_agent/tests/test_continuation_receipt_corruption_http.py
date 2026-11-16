"""Corrupt optional planning history cannot turn read-only recovery into HTTP500."""
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_continuation_lifecycle32 import prepared_root, begin


@pytest.mark.parametrize('payload', ['[]', 'null', '1', '"old legacy value"'])
def test_wrong_shape_root_plan_redacts_receipt_without_replaying(tmp_path, payload):
    db, sid, settings, gateway = prepared_root(tmp_path, device=True)
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        turn = begin(client, sid)
        response = client.post(f'/v1/sessions/{sid}/agent-turns/{turn}/continuation-admission')
        assert response.status_code == 200 and response.json()['status'] == 'admitted'
        with db.connect() as c:
            c.execute("UPDATE agent_plan SET plan_json=? WHERE agent_turn_id='root32'", (payload,))
            c.commit()
            original = [tuple(r) for r in c.execute('SELECT * FROM agent_turn ORDER BY rowid')]
        response = client.get(f'/v1/sessions/{sid}/agent-continuation?parent_turn_id=root32')
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'unavailable'
        assert response.json()['replay_allowed'] is False
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM agent_turn ORDER BY rowid')] == original
    assert len(gateway.requests) == 1
