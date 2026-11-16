import json
import sqlite3
from contextlib import contextmanager

import pytest

from home_study_agent.agent_request_receipt import read_agent_request_receipt as read_receipt

class Database:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        try:
            yield connection
        finally:
            connection.close()


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'receipt.db')
    with db.connect() as c:
        c.execute('CREATE TABLE daily_session(session_id TEXT,child_id TEXT)')
        c.execute('CREATE TABLE agent_turn(session_id TEXT,child_id TEXT,client_action_id TEXT,tool_status TEXT,action_json TEXT)')
        c.execute('CREATE TABLE learning_input_arrival(session_id TEXT,child_id TEXT,client_action_id TEXT)')
        c.execute("INSERT INTO daily_session VALUES('session-one','child-one')")
        c.commit()
    return db


def put(database, action):
    with database.connect() as c:
        c.execute('INSERT INTO agent_turn VALUES(?,?,?,?,?)', ('session-one', 'child-one', 'action-one', 'completed', action))
        c.commit()


@pytest.mark.parametrize('reason', ['MODEL_UNAVAILABLE', 'CONTROL_MODEL_FAILED', 'CLOUD_TEXT_DISABLED'])
def test_finished_clarification_is_not_reported_as_finished_child_command(database, reason):
    put(database, json.dumps({'type': 'none', 'source': 'content_free_control_clarification', 'understanding_failure_code': reason}))
    before = database.path.read_bytes()
    result = read_receipt(database, 'session-one', 'action-one')
    assert result['status'] == 'recorded_completed'
    assert '没有执行' in result['response_text']
    assert '已记录为完成' not in result['response_text']
    assert result['replay_allowed'] is False and result['mastery_update_allowed'] is False
    assert database.path.read_bytes() == before


def test_completed_conversation_does_not_claim_a_device_action(database):
    put(database, json.dumps({'type': 'none'}))
    result = read_receipt(database, 'session-one', 'action-one')
    assert '对话回复' in result['response_text'] and '没有执行' in result['response_text']


@pytest.mark.parametrize('action', ['{broken', 'null', '[]', '{}'])
def test_missing_action_scope_cannot_be_presented_as_finished_operation(database, action):
    put(database, action)
    result = read_receipt(database, 'session-one', 'action-one')
    assert '不能证明' in result['response_text']
    assert '已记录为完成' not in result['response_text']


def test_actual_recorded_action_keeps_its_bounded_non_replaying_receipt(database):
    put(database, json.dumps({'type': 'pause_active_task', 'private_extra': 'never returned'}))
    result = read_receipt(database, 'session-one', 'action-one')
    assert '已记录为完成' in result['response_text']
    assert 'private_extra' not in str(result)
    assert result['cloud_transmission_allowed'] is False
