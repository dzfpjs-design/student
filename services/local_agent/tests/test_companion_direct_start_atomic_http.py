"""Legacy HTTP activity starts must not strand a real running task."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from test_app import advance_to_study, client_for
from test_learning_runtime_http import NoCloud


def task(db, pid):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())


def prepare(client):
    sid, pid = advance_to_study(client, action_prefix='atomic-direct')
    response = client.post(f'/v1/plan-items/{pid}/start', json={'client_action_id': 'atomic-original-start'})
    assert response.status_code == 200, response.text
    return sid, pid


BODY = {'activity_type': 'movement', 'topic': 'Short stretch', 'client_action_id': 'atomic-direct-activity'}


@pytest.mark.parametrize('table', ['companion_activity', 'companion_activity_outcome'])
def test_failed_direct_activity_write_keeps_original_timer_and_can_retry(tmp_path, table):
    db = Database(tmp_path / 'test.db')
    with TestClient(client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True).app,
                    raise_server_exceptions=False) as client:
        sid, pid = prepare(client)
        before = task(db, pid)
        with db.connect() as c:
            c.execute(f"CREATE TRIGGER fail_direct_activity BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected activity save failure'); END")
            c.commit()
        failed = client.post(f'/v1/sessions/{sid}/companion-activities/start', json=BODY)
        assert failed.status_code >= 500, failed.text
        after = task(db, pid)
        for key in ('status', 'started_at', 'active_segment_started_at', 'active_seconds'):
            assert after[key] == before[key], (key, before[key], after[key])
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0] == 0
            c.execute('DROP TRIGGER fail_direct_activity')
            c.commit()
        saved = client.post(f'/v1/sessions/{sid}/companion-activities/start', json=BODY)
        assert saved.status_code == 200, saved.text
        assert saved.json()['paused_plan_item_id'] == pid
        assert task(db, pid)['status'] == 'paused'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0] == 1


def test_old_direct_request_replay_does_not_pause_a_new_timer(tmp_path):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = prepare(client)
        saved = client.post(f'/v1/sessions/{sid}/companion-activities/start', json=BODY)
        assert saved.status_code == 200, saved.text
        ended = client.post(f'/v1/sessions/{sid}/companion-activities/end', json={'reason': 'child_requested'})
        assert ended.status_code == 200, ended.text
        resumed = client.post(f'/v1/plan-items/{pid}/resume', json={'client_action_id': 'atomic-original-resume'})
        assert resumed.status_code == 200, resumed.text
        before = task(db, pid)
        replay = client.post(f'/v1/sessions/{sid}/companion-activities/start', json=BODY)
        assert replay.status_code == 200, replay.text
        assert replay.json()['activity_id'] == saved.json()['activity_id']
        assert replay.json()['status'] == 'completed'
        assert task(db, pid) == before
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 1
