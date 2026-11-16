"""Explicit contention recovery at the real voice HTTP boundary, no model IO."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.app import create_app
from home_study_agent.rubric_calibration_transaction import CalibrationWriterBusyError
from test_diagnostic_prompt_http import environment, start
from test_learning_runtime_http import NoCloud


def test_voice_prewrite_busy_has_bounded_recovery_contract_without_private_error(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        sid = start(client)
        async def busy(*args, **kwargs):
            raise CalibrationWriterBusyError('internal-private-storage-details')
        monkeypatch.setattr(AgentOrchestrator, 'interpret', busy)
        with db.connect() as c:
            before = c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0]
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '帮我看看刚才做的题', 'client_action_id': 'original-voice-busy'})
        assert response.status_code == 503, response.text
        assert response.headers['Retry-After'] == '1'
        assert response.json() == {
            'code': 'CALIBRATION_WRITER_BUSY',
            'message': '正在同步学习记录，请稍后用原请求重试。',
            'retryable': True, 'recovery_action': 'retry_same_request'}
        assert 'internal-private' not in response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == before


@pytest.mark.parametrize('error', [sqlite3.OperationalError('disk I/O error'),
    sqlite3.OperationalError('database is locked'), RuntimeError('unknown result')])
def test_arbitrary_storage_or_unknown_outcomes_never_gain_retry_permission(environment, monkeypatch, error):
    settings, _ = environment
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        sid = start(client)
        async def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(AgentOrchestrator, 'interpret', fail)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '继续学习', 'client_action_id': 'original-unknown-outcome'})
        assert response.status_code == 500
        assert 'CALIBRATION_WRITER_BUSY' not in response.text
        assert 'Retry-After' not in response.headers


def test_original_voice_retries_after_saved_arrival_and_survives_restart_without_double_start(environment, monkeypatch):
    from home_study_agent.agent_runtime import AgentRuntimeStore
    from test_natural_task_navigation_cloud_http import application
    from test_natural_task_navigation_http import setup_case, execute
    from test_voice_runtime_binding import capture

    settings, db = environment
    db.initialize()
    requests = []
    with TestClient(application(settings, db, requests), raise_server_exceptions=False) as client:
        sid, repo = setup_case(client, db, 'completed')
        quote = '能开始下一项吗？'
        voice = capture(client, settings, monkeypatch, sid, quote, key='busy-original-capture')
        path = f'/v1/sessions/{sid}/agent-turn'
        body = {'transcript': quote, 'client_action_id': 'busy-original-operation',
                'voice_input_id': voice, 'cloud_text_allowed': True}
        async_error_calls = []
        def contention(*args, **kwargs):
            async_error_calls.append(True)
            raise CalibrationWriterBusyError('CALIBRATION_WRITER_BUSY')
        with monkeypatch.context() as busy_patch:
            busy_patch.setattr(AgentRuntimeStore, 'reconcile_learning_state', contention)
            busy = client.post(path, json=body)
        assert busy.status_code == 503, busy.text
        assert len(async_error_calls) == 1 and requests == []
        with db.connect() as c:
            arrival = c.execute('SELECT arrival_id FROM learning_input_arrival WHERE client_action_id=?',
                                (body['client_action_id'],)).fetchall()
            assert len(arrival) == 1
            assert c.execute('SELECT COUNT(*) FROM agent_turn WHERE client_action_id=?',
                             (body['client_action_id'],)).fetchone()[0] == 0
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        heard = response.json()
        assert heard['runtime_capability'] == 'task.start'
        execute(client, heard)
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'active'
        assert len(requests) == 1
    with TestClient(application(settings, db, requests), raise_server_exceptions=False) as client:
        replay = client.post(path, json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['agent_turn_id'] == heard['agent_turn_id']
        execute(client, replay.json())
        assert len(requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_arrival WHERE client_action_id=?',
                             (body['client_action_id'],)).fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id='control-task-2' AND event_type='started'").fetchone()[0] == 1
