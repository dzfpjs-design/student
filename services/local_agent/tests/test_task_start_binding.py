import json

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository, PlanInputError, PlanItemStateError
from home_study_agent.task_start_binding import TaskStartBindingError
from test_agent_executor_evidence_boundary import setup, turn


def waiting(database):
    with database.connect() as c:
        c.execute("UPDATE plan_item SET status='pending' WHERE plan_item_id='task'")
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,task_type)
            VALUES('second','daily','英语','Another task',10,2,'pending',2,'school_homework','written')""")
        c.commit()


def test_lost_start_result_replays_original_task_after_database_reopen(setup, monkeypatch):
    database, repository, _ = setup
    waiting(database)
    key = turn(setup, 'start-once', 'task.start', {})
    original = repository.start_plan_item
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('lost response after commit')
    monkeypatch.setattr(repository, 'start_plan_item', lost)
    with pytest.raises(RuntimeError, match='lost response'):
        AgentCapabilityExecutor(database, repository).execute(key)
    reopened = Database(database.path)
    result = AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(key)
    assert result['result']['plan_item_id'] == 'task'
    with reopened.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='second'").fetchone()[0] == 'pending'
        manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (key,)).fetchone()[0])
        assert manifest['task_start_binding']['plan_item_id'] == 'task'


@pytest.mark.parametrize('change', ['cancel', 'source'])
def test_cancellation_or_source_change_before_the_write_cannot_start_a_timer(setup, monkeypatch, change):
    database, repository, _ = setup
    waiting(database)
    key = turn(setup, 'cancel-before-write', 'task.start', {})
    original = repository.start_plan_item
    def raced(*args, **kwargs):
        with database.connect() as c:
            if change == 'cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (key,))
            else:
                c.execute("UPDATE plan_item SET source_ref='replacement' WHERE plan_item_id='task'")
            c.commit()
        return original(*args, **kwargs)
    monkeypatch.setattr(repository, 'start_plan_item', raced)
    with pytest.raises(TaskStartBindingError):
        AgentCapabilityExecutor(database, repository).execute(key)
    with database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item WHERE status='active'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
        if change == 'cancel':
            assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (key,)).fetchone()[0] == 'cancelled'


def test_cancel_after_committed_start_keeps_receipt_without_reviving_the_command(setup, monkeypatch):
    database, repository, _ = setup
    waiting(database)
    key = turn(setup, 'cancel-after-commit', 'task.start', {})
    original = repository.start_plan_item
    def cancelled_after(*args, **kwargs):
        result = original(*args, **kwargs)
        with database.connect() as c:
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (key,))
            c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (key,))
            c.commit()
        return result
    monkeypatch.setattr(repository, 'start_plan_item', cancelled_after)
    with pytest.raises(TaskStartBindingError, match='SUPERSEDED_AFTER_DISPATCH'):
        AgentCapabilityExecutor(database, repository).execute(key)
    with database.connect() as c:
        row = c.execute('SELECT s.status,s.result_json FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?', (key,)).fetchone()
        assert row['status'] == 'cancelled'
        assert json.loads(row['result_json'])['superseded'] is True
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1


def test_resume_rejects_a_different_event_key_and_two_active_tasks(setup):
    database, repository, _ = setup
    waiting(database)
    repository.start_plan_item('task', 'start-first')
    repository.pause_plan_item('task', 'pause-first')
    with pytest.raises(PlanInputError):
        repository.resume_plan_item('task', 'start-first')
    repository.start_plan_item('second', 'start-second')
    with pytest.raises(PlanItemStateError):
        repository.resume_plan_item('task', 'resume-first')


def test_http_cancellation_returns_actionable_conflict_without_reviving_plan(setup, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_learning_runtime_http import NoCloud
    database, _, _ = setup
    waiting(database)
    application = create_app(Settings(data_dir=tmp_path, database_path=database.path, maintenance_enabled=False),
                             model_gateway=NoCloud())
    with TestClient(application) as client:
        key = turn(setup, 'http-cancel-task', 'task.start', {})
        original = application.state.repository.start_plan_item
        def cancelled(*args, **kwargs):
            with database.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (key,))
                c.commit()
            return original(*args, **kwargs)
        monkeypatch.setattr(application.state.repository, 'start_plan_item', cancelled)
        result = client.post(f'/v1/agent-turns/{key}/execute')
        assert result.status_code == 409, result.text
        assert result.json()['detail']['code'] == 'TASK_START_AUTHORIZATION_CHANGED'
        with database.connect() as c:
            assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (key,)).fetchone()[0] == 'cancelled'
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
