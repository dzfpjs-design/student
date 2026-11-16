"""Read lifecycle facts separately from permission to execute a continuation."""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, parse_agent_plan_output
from home_study_agent.agent_continuation import ContinuationStore
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from test_agent_continuation import Gateway, next_plan
from test_agent_runtime import seed_session


def prepared_root(tmp_path, *, device=False):
    db = Database(tmp_path / 'lifecycle32.db')
    db.initialize()
    session = seed_session(db)
    runtime = AgentRuntimeStore(db)
    follow = {'capability': 'device.status', 'arguments': {'device': 'camera'}} if device else next_plan()['next_step']
    quote = '告诉我摄像头状态' if device else '安排学习'
    original = '精神很好，记下来再' + quote
    plan = parse_agent_plan_output(json.dumps({
        'objective': {'mode': 'new', 'type': 'execution', 'summary': original,
                      'success_criteria': ['状态已保存', quote], 'phase': 'prepare'},
        'next_step': {'capability': 'checkin.update_context', 'arguments': {'energy_level': 'high'}},
        'spoken_transition': '先记下你的状态。', 'requires_child_reply': False,
        'after_execution': 'replan', 'confidence': .95,
        'continuation_scope': [{**follow, 'request_quote': quote}],
    }, ensure_ascii=False), 'check_in', CapabilityRegistry())
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,orchestrator_version,created_at,client_action_id)
            VALUES('root32','child-runtime',?,?,'agent_runtime','先记录状态','{}','test',datetime('now'),'root-action32')""", (session, original))
        runtime.persist_plan(c, child_id='child-runtime', session_id=session, agent_turn_id='root32',
                             client_action_id='root-action32', decision=plan, context_manifest={})
        c.commit()
    output = {**next_plan(), 'next_step': follow}
    gateway = Gateway(output)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    return db, session, settings, gateway


def begin(client, session):
    root = client.post('/v1/agent-turns/root32/execute')
    assert root.status_code == 200, root.text
    response = client.post(f'/v1/sessions/{session}/agent-turns/root32/continue')
    assert response.status_code == 200 and response.json()['status'] == 'ready', response.text
    return response.json()['turn']['agent_turn_id']


def test_lost_planning_reply_reopens_same_saved_plan_without_new_model_request(tmp_path):
    db, session, settings, gateway = prepared_root(tmp_path)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        following = begin(client, session)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        snapshot = client.get(f'/v1/sessions/{session}/agent-continuation')
        assert snapshot.json()['status'] == 'ready'
        response = client.post(f'/v1/sessions/{session}/agent-turns/root32/continue')
        assert response.json()['turn']['agent_turn_id'] == following
        assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (session,)).fetchone()[0] == 'check_in'
            assert c.execute("SELECT count(*) FROM agent_turn WHERE intent='runtime_continuation'").fetchone()[0] == 1


def test_actual_completed_continuation_is_readable_after_restart_not_inactive(tmp_path):
    db, session, settings, gateway = prepared_root(tmp_path)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        following = begin(client, session)
        done = client.post(f'/v1/agent-turns/{following}/execute')
        assert done.status_code == 200, done.text
        assert done.json()['turn']['tool_status'] == 'completed'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        with db.connect() as c:
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (session,)).fetchone()[0] == 'classroom_recall'
        snapshot = client.get(f'/v1/sessions/{session}/agent-continuation').json()
        print({'actual_saved': 'classroom_recall', 'after_restart_snapshot': snapshot})
        assert snapshot['status'] == 'completed'
        assert len(gateway.requests) == 1


def test_admission_without_effect_remains_explicitly_unknown_after_expiry(tmp_path, monkeypatch):
    import home_study_agent.agent_continuation as module
    db, session, settings, gateway = prepared_root(tmp_path, device=True)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        following = begin(client, session)
        admitted = client.post(f'/v1/sessions/{session}/agent-turns/{following}/continuation-admission')
        assert admitted.json()['status'] == 'admitted', admitted.text
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc).astimezone(tz) + timedelta(minutes=6)
    # Advance the observation clock, never rewrite the original authority or TTL.
    monkeypatch.setattr(module, 'datetime', Future)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        snapshot = client.get(f'/v1/sessions/{session}/agent-continuation').json()
        repeat = client.post(f'/v1/sessions/{session}/agent-turns/{following}/continuation-admission')
        assert repeat.json()['status'] != 'admitted'
        with db.connect() as c:
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (following,)).fetchone()[0] == 'pending'
        print({'no_client_effect_sent': True, 'expired_snapshot': snapshot})
        assert snapshot['status'] == 'awaiting_receipt'
        assert len(gateway.requests) == 1
