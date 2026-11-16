"""A scoped historical receipt is never a new continuation execution grant."""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from home_study_agent.agent_continuation import ContinuationStore
from home_study_agent.app import create_app
from test_continuation_lifecycle32 import prepared_root, begin


def expire(monkeypatch):
    import home_study_agent.agent_continuation as module
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc).astimezone(tz) + timedelta(minutes=6)
    monkeypatch.setattr(module, 'datetime', Later)


@pytest.mark.parametrize('phase', ['expired', 'new_input', 'privacy', 'failed', 'completed'])
def test_receipt_survives_authority_end_without_authorizing_replay(tmp_path, monkeypatch, phase):
    db, sid, settings, gateway = prepared_root(tmp_path, device=phase != 'completed')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        turn_id = begin(client, sid)
        if phase == 'completed':
            assert client.post(f'/v1/agent-turns/{turn_id}/execute').status_code == 200
        else:
            admitted = client.post(f'/v1/sessions/{sid}/agent-turns/{turn_id}/continuation-admission')
            assert admitted.json()['status'] == 'admitted', admitted.text
            assert ContinuationStore(db).claim(sid, 'root32')['status'] == 'awaiting_receipt'
        if phase == 'failed':
            response = client.put(f'/v1/agent-turns/{turn_id}/outcome', json={
                'tool_status': 'failed', 'final_response': '本机查询失败，状态未确认', 'tool_error_code': 'DEVICE_STATUS_UNAVAILABLE'})
            assert response.status_code == 200, response.text
        elif phase == 'new_input':
            response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': '停止说话', 'client_action_id': 'new-input32', 'cloud_text_allowed': False})
            assert response.status_code == 200, response.text
        elif phase == 'privacy':
            assert client.post('/v1/parent/setup', json={'pin': '2846'}).status_code == 200
            unlocked = client.post('/v1/parent/unlock', json={'pin': '2846'})
            assert unlocked.status_code == 200, unlocked.text
            response = client.put('/v1/parent/privacy', headers={'X-HSC-Parent': unlocked.json()['parent_token']},
                json={'cloud_model_text_allowed': False})
            assert response.status_code == 200, response.text
        with db.connect() as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM session_transition')]
    expire(monkeypatch)
    request_count = len(gateway.requests)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        response = client.get(f'/v1/sessions/{sid}/agent-continuation?parent_turn_id=root32')
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt['status'] == (phase if phase in {'completed', 'failed'} else 'awaiting_receipt'), receipt
        assert receipt['root_turn_id'] == receipt['parent_turn_id'] == 'root32'
        assert receipt['next_turn_id'] == turn_id and receipt['session_id'] == sid
        assert receipt['replay_allowed'] is False and receipt['mastery_update_allowed'] is False
        assert receipt['local_only'] is True and receipt['cloud_transmission_allowed'] is False
        assert not any(key in receipt for key in ('turn', 'transcript', 'arguments', 'result', 'response_text'))
        unknown = client.get(f'/v1/sessions/{sid}/agent-continuation?parent_turn_id=unrelated-root32').json()
        assert unknown['status'] == 'unavailable' and 'next_turn_id' not in unknown
        repeated = client.post(f'/v1/sessions/{sid}/agent-turns/{turn_id}/continuation-admission')
        assert repeated.json()['status'] != 'admitted'
        with db.connect() as c:
            c.execute('PRAGMA query_only=ON')
            assert not ContinuationStore.execution_allowed(c, turn_id)
            assert [tuple(r) for r in c.execute('SELECT * FROM session_transition')] == before
    assert len(gateway.requests) == request_count


@pytest.mark.parametrize('fault', ['root_quote', 'arguments', 'foreign_root', 'action_root', 'source_deleted'])
def test_neutral_origin_is_not_unbound_permission(tmp_path, fault):
    db, sid, settings, gateway = prepared_root(tmp_path, device=True)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        turn_id = begin(client, sid)
        with db.connect() as c:
            if fault == 'root_quote':
                c.execute("UPDATE agent_turn SET transcript='另一项请求' WHERE agent_turn_id='root32'")
            elif fault == 'arguments':
                c.execute("UPDATE agent_step SET arguments_json=? WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",
                    (json.dumps({'device': 'microphone'}), turn_id))
            elif fault == 'foreign_root':
                c.execute("UPDATE agent_continuation SET root_turn_id=? WHERE parent_turn_id='root32'", (turn_id,))
            elif fault == 'action_root':
                c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.root_turn_id','other-root') WHERE agent_turn_id=?", (turn_id,))
            else:
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id='root32'")
            c.commit()
        with db.connect() as c:
            assert not ContinuationStore.execution_allowed(c, turn_id)
        response = client.post(f'/v1/sessions/{sid}/agent-turns/{turn_id}/continuation-admission')
        assert response.json()['status'] != 'admitted', response.text
    assert len(gateway.requests) == 1


def test_receipt_wrong_session_and_two_children_do_not_leak(tmp_path):
    db, sid, settings, gateway = prepared_root(tmp_path, device=True)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        turn_id = begin(client, sid)
        assert client.post(f'/v1/sessions/{sid}/agent-turns/{turn_id}/continuation-admission').json()['status'] == 'admitted'
        with db.connect() as c:
            c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('other32','Other','四年级','江苏南京','active',datetime('now'),datetime('now'))")
            c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('other-session32','other32','completed','completed',datetime('now'),datetime('now'),'other-session32')")
            c.commit()
        for other in ('other-session32', 'absent-session32'):
            response = ContinuationStore(db).receipt(other, 'root32')
            assert response['status'] == 'unavailable' and 'next_turn_id' not in response
            response = client.get(f'/v1/sessions/{other}/agent-continuation?parent_turn_id=root32')
            assert response.status_code == 200 and response.json()['status'] == 'unavailable'
            assert 'next_turn_id' not in response.json()
        rejected = client.post(f'/v1/sessions/other-session32/agent-turns/{turn_id}/continuation-admission')
        assert rejected.status_code in {200, 404, 409, 422}, rejected.text
        assert rejected.json().get('status') != 'admitted'
