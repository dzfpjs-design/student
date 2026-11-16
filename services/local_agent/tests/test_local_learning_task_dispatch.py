from datetime import timedelta
import json

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.repository import StudyRepository
from test_learning_runtime_local_integration import journey, gate, flow, start, answer, approve, apply, task_turn
from test_learning_runtime_http import NoCloud


def test_actual_task_execute_http_prepares_owned_local_recheck_before_timer(journey, tmp_path):
    j = journey
    settings = Settings(data_dir=tmp_path, database_path=j.db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        approve(j, answer(j, start(j)))
        apply(j)
        j.clock[0] += timedelta(days=1)
        key = task_turn(j)
        result = client.post(f'/v1/agent-turns/{key}/execute')
        assert result.status_code == 200, result.text
        execution = result.json()['execution']
        assert execution['requires_child_reply'] is True
        assert execution['result']['status'] == 'active'
        prompt = execution['result']['diagnostic_prompt']
        assert prompt['status'] == 'waiting_answer'
        assert prompt['objective_id'] == j.objective
        assert result.json()['turn']['action']['cloud_transmission_allowed'] is False
        replay = client.post(f'/v1/agent-turns/{key}/execute')
        assert replay.status_code == 200, replay.text
        assert replay.json()['execution'] == execution
        with j.db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
            assert c.execute("SELECT status FROM agent_plan WHERE agent_turn_id=?", (key,)).fetchone()[0] == 'waiting_child'


def test_not_due_recheck_returns_blocked_without_starting_a_timer(journey):
    j = journey
    approve(j, answer(j, start(j)))
    apply(j)
    key = task_turn(j)
    result = AgentCapabilityExecutor(j.db, StudyRepository(j.db)).execute(key)
    assert result['result']['status'] == 'blocked'
    assert result['result']['reason'] == 'not_due'
    assert result['evidence'] == []
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM diagnostic_prompt WHERE status='waiting_answer'").fetchone()[0] == 0


def test_lost_timer_response_recovers_the_same_prompt_without_extra_questions(journey, monkeypatch):
    j = journey
    approve(j, answer(j, start(j)))
    apply(j)
    j.clock[0] += timedelta(days=1)
    key = task_turn(j)
    repository = StudyRepository(j.db)
    original = repository.start_plan_item
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('timer response lost')
    monkeypatch.setattr(repository, 'start_plan_item', lost)
    with pytest.raises(RuntimeError):
        AgentCapabilityExecutor(j.db, repository).execute(key)
    result = AgentCapabilityExecutor(j.db, StudyRepository(j.db)).execute(key)
    assert result['result']['status'] == 'active'
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM diagnostic_prompt WHERE status='waiting_answer'").fetchone()[0] == 1
