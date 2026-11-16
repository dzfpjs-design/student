"""Original numeric evidence over real loopback TCP, SQLite and app restarts."""
import json
import socket
import sqlite3

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from home_study_agent.app import create_app
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import utc_now
from test_diagnostic_prompt_http import environment, endpoint, binding, CHILD
from test_numeric_tcp_audit30 import TCPClient
import test_numeric_history_sources31 as history


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {'127.0.0.1', '::1'}, 'External socket forbidden'
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)


@pytest.mark.parametrize('confirmed', [False, True])
def test_tcp_accepted_speech_survives_completed_turn_ended_session_and_restart(environment, monkeypatch, confirmed):
    settings, db = environment
    gateway = history.closed.ClosedGateway()
    with TCPClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, source, receipt = history.accepted(client, db, monkeypatch,
            confirmed=confirmed, settings=settings)
        reply = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            'final_response': '作答已保存', 'tool_status': 'completed',
            'result': {'execution_receipt': {'capability': 'diagnostic.submit',
                'execution_location': 'client_device_bridge', 'action_type': 'submit_diagnostic_answer',
                'status': 'completed', 'completed_at': utc_now(), 'output': {'attempt': receipt}}}})
        assert reply.status_code == 200, reply.text
        assert reply.json()['tool_status'] == 'completed'
        assert history.current(db, receipt)['mastery_update_allowed']
        stages = ['check_in', 'classroom_recall', 'homework_capture', 'plan_review',
                  'study', 'reflection', 'evolving', 'completed']
        for before, after in zip(stages, stages[1:]):
            if before == 'classroom_recall':
                empty = client.post(f'/v1/sessions/{sid}/recalls/none', json={'client_action_id': 'history-no-recall31'})
                assert empty.status_code == 200, empty.text
            response = client.post(f'/v1/sessions/{sid}/transition', json={
                'expected_state': before, 'target_state': after, 'reason': 'history-lifecycle31',
                'client_action_id': f'history-end31-{after}'})
            assert response.status_code == 200, response.text
        assert history.current(db, receipt)['mastery_update_allowed']
        with db.connect() as c:
            original = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (receipt['attempt_id'],)).fetchone())
            assert c.execute('SELECT ended_at FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0]
    requests_before_restart = len(gateway.requests)
    with TCPClient(create_app(settings, model_gateway=gateway)) as client:
        original_key = 'source-confirm31' if confirmed else 'source-original31'
        recovered = client.get(f'/v1/sessions/{sid}/agent-request-receipts/{original_key}')
        assert recovered.status_code == 200 and recovered.json()['status'] == 'recorded_completed', recovered.text
        assert not recovered.json()['mastery_update_allowed']
        assert history.current(db, receipt)['mastery_update_allowed']
        assert history.current(db, receipt, transaction=True)['mastery_update_allowed']
        with db.connect() as c:
            c.execute('BEGIN')
            with pytest.raises(InvalidAgentPlanError):
                AgentRuntimeStore.validate_local_answer_write(c, turn['agent_turn_id'], 'diagnostic.submit',
                    child_id=CHILD, session_id=sid, binding=binding(prompt), answer=receipt['submitted_answer'])
            assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (receipt['attempt_id'],)).fetchone()) == original
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
            c.rollback()
        rejected = client.post(endpoint(sid, 'answer'), json={**binding(prompt), 'answer': receipt['submitted_answer'],
            'client_action_id': 'not-a-replay31', 'authorized_turn_id': turn['agent_turn_id']})
        assert rejected.status_code in {409, 422}, rejected.text
        assert history.current(db, receipt)['mastery_update_allowed']
    assert len(gateway.requests) == requests_before_restart
    print({'persisted_db': str(db.path), 'confirmed': confirmed, 'original': receipt['submitted_answer'],
        'score': 1, 'history_strong': True, 'new_answer_http': rejected.status_code, 'owned_workers': 0})


@pytest.mark.parametrize('lossy', [False, True])
def test_tcp_legacy_lossy_has_matched_positive_control_active_and_ended(environment, monkeypatch, lossy):
    monkeypatch.setattr(history, 'TestClient', TCPClient)
    history.test_legacy_lossy_is_excluded_in_active_transaction_and_ended_but_control_is_retained(environment, monkeypatch, lossy)
    print({'persisted_db': str(environment[1].path), 'legacy_lossy': lossy, 'owned_workers': 0})


def test_historical_guard_does_not_swallow_sql_errors(environment, monkeypatch):
    settings, db = environment
    with TCPClient(create_app(settings, model_gateway=history.closed.ClosedGateway())) as client:
        _, _, _, _, receipt = history.accepted(client, db, monkeypatch)
    with db.connect() as c:
        def authorizer(action, table, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'agent_turn' else sqlite3.SQLITE_OK
        c.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            LearningStateService._attempt(c, receipt['attempt_id'])
