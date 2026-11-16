import sqlite3

import pytest

from home_study_agent.learning_outcome_coordinator import LearningOutcomeCoordinator
from home_study_agent.learning_outcome_projection import LearningOutcomeProjection
from test_learning_outcome_mastery_gate import gate, flow, CHILD, SECOND


@pytest.mark.parametrize('column', ['record_json', 'evaluation_json'])
@pytest.mark.parametrize('bad', ['{', 'null', '[]'])
def test_one_corrupt_outcome_preserves_healthy_same_goal_evidence(gate, column, bad):
    good, _, _ = gate.run()
    broken, _, turn = gate.run(SECOND, '42')
    good_id = good['evaluation']['outcome_id']
    bad_id = broken['evaluation']['outcome_id']
    with gate.db.connect() as c:
        step = c.execute('SELECT step_id FROM agent_step JOIN agent_plan USING(plan_id) WHERE agent_turn_id=?', (turn,)).fetchone()[0]
        c.execute(f'UPDATE learning_evaluation_execution SET {column}=? WHERE step_id=?', (bad, step))
        c.commit()
        frozen = [tuple(row) for row in c.execute('SELECT * FROM learning_evaluation_execution ORDER BY step_id')]
        models = c.execute('SELECT count(*) FROM model_call').fetchone()[0]
        c.execute('PRAGMA query_only=ON')
        current = gate.evaluator.current_mastery_gate(gate.objective, connection=c)
        items = {item['outcome_id']: item for item in current['outcomes']}
        assert items[good_id]['eligible'] is True
        assert items[bad_id]['eligible'] is False
        assert items[bad_id]['outcome_level'] == 'exposed'
        assert items[bad_id]['missing'] == ['evaluation_history_unreadable']
        assert current['outcome_level'] == 'independent'
        assert len(LearningOutcomeCoordinator(gate.db).inspect_child(CHILD, connection=c)) == 1
        view = LearningOutcomeProjection(gate.db).for_child(CHILD, connection=c)
        assert view['available_count'] == 1
        assert view['level_counts'] == {'independent': 1}
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evaluation_execution ORDER BY step_id')] == frozen
        assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == models


def test_replay_does_not_turn_sqlite_failure_into_empty_evidence(gate, monkeypatch):
    gate.run()
    def storage_failed(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic storage outage')
    monkeypatch.setattr(gate.evaluator, '_with_current_rubric_receipt', storage_failed)
    with pytest.raises(sqlite3.OperationalError, match='synthetic storage outage'):
        gate.evaluator.current_mastery_gate(gate.objective)
