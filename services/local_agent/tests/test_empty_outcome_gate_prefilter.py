import json
import sqlite3

import pytest

from home_study_agent.learning_outcome_evaluator import (
    InvalidLearningEvaluationError, LearningOutcomeEvaluator,
)


@pytest.fixture
def gate():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE agent_objective (objective_id, child_id, success_criteria_json)')
    connection.execute('CREATE TABLE agent_outcome (outcome_id, objective_id, child_id)')
    connection.execute("INSERT INTO agent_objective VALUES ('goal', 'child', '[\"criterion\"]')")
    evaluator = object.__new__(LearningOutcomeEvaluator)
    evaluator.database = None
    try:
        yield evaluator, connection
    finally:
        connection.close()


def test_empty_objective_requires_no_whole_child_receipts(gate, monkeypatch):
    evaluator, connection = gate
    monkeypatch.setattr(evaluator, '_replay_gates', lambda *a: pytest.fail('unused whole-child replay'))
    result = evaluator.current_mastery_gate('goal', connection=connection)
    assert result['outcome_level'] == 'exposed' and result['outcomes'] == []


def test_cross_child_outcome_cannot_promote_empty_objective(gate, monkeypatch):
    evaluator, connection = gate
    connection.execute("INSERT INTO agent_outcome VALUES ('foreign', 'goal', 'other')")
    monkeypatch.setattr(evaluator, '_replay_gates', lambda *a: pytest.fail('foreign outcome replay'))
    assert evaluator.current_mastery_gate('goal', connection=connection)['outcomes'] == []


def test_nonempty_objective_uses_full_replay_after_own_write(gate, monkeypatch):
    evaluator, connection = gate
    calls = []
    def replay(*args):
        calls.append(args)
        return [{'outcome_level': 'exposed', 'missing': ['evaluation_history_unreadable']}], {}, []
    monkeypatch.setattr(evaluator, '_replay_gates', replay)
    evaluator.current_mastery_gate('goal', connection=connection)
    assert not calls
    connection.execute("INSERT INTO agent_outcome VALUES ('bad', 'goal', 'child')")
    result = evaluator.current_mastery_gate('goal', connection=connection)
    assert len(calls) == 1
    assert result['outcomes'][0]['missing'] == ['evaluation_history_unreadable']


def test_empty_objective_keeps_criteria_decode_failure(gate):
    evaluator, connection = gate
    connection.execute("UPDATE agent_objective SET success_criteria_json='broken'")
    with pytest.raises(json.JSONDecodeError):
        evaluator.current_mastery_gate('goal', connection=connection)


def test_unknown_objective_and_sql_failures_are_not_swallowed(gate):
    evaluator, connection = gate
    with pytest.raises(InvalidLearningEvaluationError):
        evaluator.current_mastery_gate('missing', connection=connection)
    connection.execute('DROP TABLE agent_objective')
    with pytest.raises(sqlite3.OperationalError):
        evaluator.current_mastery_gate('goal', connection=connection)
