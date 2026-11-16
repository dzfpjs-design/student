"""Arrival changes must be rejected before any task pause is committed."""
import json
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_companion_request_provenance_http import planner, homework_task, planned
from test_diagnostic_prompt_http import environment, start


@pytest.mark.parametrize('change', ['source', 'active_task', 'cancelled'])
def test_changed_arrival_cannot_pause_current_task_or_create_activity(environment, monkeypatch, change):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        sid = start(client)
        homework_task(client, db, sid)
        if change == 'active_task':
            with db.connect() as c:
                homework = dict(c.execute("SELECT * FROM homework_item WHERE client_action_id='homework-source'").fetchone())
                homework.update(homework_id='second-homework', client_action_id='second-homework', title='另一项作业')
                c.execute('INSERT INTO homework_item (' + ','.join(homework) + ') VALUES (' + ','.join('?' for _ in homework) + ')', tuple(homework.values()))
                c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,
                    priority,status,position,source_type,source_ref,task_type)
                    VALUES ('second-task','request-plan','second-homework','数学','另一项作业',10,1,'pending',2,
                    'school_homework','second-homework','written')''')
                c.commit()
        turn, _ = planned(client, settings, monkeypatch, sid)
        current = 'request-task'
        if change == 'source':
            with db.connect() as c:
                c.execute("UPDATE homework_item SET range_text='原话到达后的内容修订'")
                c.commit()
        elif change == 'active_task':
            response = client.post('/v1/plan-items/request-task/pause', json={
                'client_action_id': 'explicit-task-switch', 'reason': 'child_requested'})
            assert response.status_code == 200, response.text
            response = client.post('/v1/plan-items/second-task/start', json={'client_action_id': 'explicit-second-start'})
            assert response.status_code == 200, response.text
            current = 'second-task'
        else:
            with db.connect() as c:
                c.execute('UPDATE agent_plan SET status=\'cancelled\' WHERE agent_turn_id=?', (turn['agent_turn_id'],))
                c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn['agent_turn_id'],))
                c.commit()
        with db.connect() as c:
            before = tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (current,)).fetchone())
            count = c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0]
            key = c.execute('SELECT s.idempotency_key FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',
                (turn['agent_turn_id'],)).fetchone()[0]
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE client_action_id=?", (key + ':pause',)).fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0] == count
            assert tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (current,)).fetchone()) == before
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (current,)).fetchone()[0] == 'active'
            assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 0


@pytest.mark.parametrize('restart', [False, True, 'missing_receipt', 'wrong_run', 'wrong_segment'])
def test_pause_old_activity_and_new_outcome_rollback_then_retry_once(environment, monkeypatch, restart):
    settings, db = environment
    gateway = planner()
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        sid = start(client)
        homework_task(client, db, sid)
        turn, _ = planned(client, settings, monkeypatch, sid)
        old = AgentRuntimeStore(db).start_companion_activity(sid, 'word_chain', '既有活动', 'existing-activity')
        with db.connect() as c:
            task_before = tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='request-task'").fetchone())
            activity_before = tuple(c.execute('SELECT * FROM companion_activity WHERE activity_id=?', (old['activity_id'],)).fetchone())
            c.execute('''CREATE TRIGGER fail_compound_open BEFORE INSERT ON companion_activity_outcome_event
                BEGIN SELECT RAISE(ABORT,'synthetic compound storage failure'); END''')
            c.commit()
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 500, response.text
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0] == 0
            assert tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='request-task'").fetchone()) == task_before
            assert tuple(c.execute('SELECT * FROM companion_activity WHERE activity_id=?', (old['activity_id'],)).fetchone()) == activity_before
            assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM companion_activity_outcome').fetchone()[0] == 1
            c.execute('DROP TRIGGER fail_compound_open')
            c.commit()
        if not restart:
            response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert response.status_code == 200, response.text
    # A real clean shutdown records a system pause, not a child-requested pause.
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        with db.connect() as c:
            paused_before_retry = tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='request-task'").fetchone())
            if restart in {'missing_receipt', 'wrong_run', 'wrong_segment'}:
                row = c.execute("SELECT * FROM plan_item_event WHERE json_extract(payload_json,'$.reason')='runtime_shutdown'").fetchone()
                assert row is not None
                if restart == 'missing_receipt':
                    c.execute('DELETE FROM plan_item_event WHERE event_id=?', (row['event_id'],))
                else:
                    payload = json.loads(row['payload_json'])
                    payload['runtime_run_id' if restart == 'wrong_run' else 'active_segment_started_at'] = 'not-the-recorded-source'
                    c.execute('UPDATE plan_item_event SET payload_json=? WHERE event_id=?', (json.dumps(payload), row['event_id']))
                c.commit()
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        if restart in {'missing_receipt', 'wrong_run', 'wrong_segment'}:
            assert response.status_code == 409, response.text
            with db.connect() as c:
                assert tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='request-task'").fetchone()) == paused_before_retry
                assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 1
            return
        assert response.status_code == 200, response.text
        first = response.json()['execution']['result']
        replay = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert replay.status_code == 200, replay.text
        assert replay.json()['execution']['result'] == first
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused' AND json_extract(payload_json,'$.reason')='companion_activity'").fetchone()[0] == (0 if restart else 1)
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused' AND json_extract(payload_json,'$.reason')='runtime_shutdown'").fetchone()[0] == (1 if restart else 0)
            assert tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='request-task'").fetchone()) == paused_before_retry
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='request-task'").fetchone()[0] == 'paused'
            assert c.execute('SELECT status FROM companion_activity WHERE activity_id=?', (old['activity_id'],)).fetchone()[0] == 'cancelled'
            assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 2
            assert c.execute('SELECT count(*) FROM companion_activity_outcome').fetchone()[0] == 2
            assert c.execute("SELECT count(*) FROM companion_activity_outcome_event WHERE kind='opened'").fetchone()[0] == 2
