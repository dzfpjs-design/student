import asyncio

import pytest

from home_study_agent.learning_outcome_evaluator import LearningEvaluationUnknownError
from test_learning_outcome_evaluator import evaluation_fixture


def test_failed_call_link_closes_only_the_exact_unsent_call(tmp_path):
    database, session_id, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    unrelated = evaluator.repository.start_model_call(
        session_id, "child-runtime", "learning_outcome_evaluation", "test", "test", 0,
        "synthetic", {}, "medium", "test", "synthetic", None,
    )
    with database.connect() as connection:
        connection.execute("""CREATE TRIGGER reject_evaluation_call_link
            BEFORE UPDATE OF model_call_id ON learning_evaluation_execution
            WHEN NEW.model_call_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'synthetic link failure'); END""")
        connection.commit()

    with pytest.raises(LearningEvaluationUnknownError):
        asyncio.run(evaluator.evaluate("eval"))
    assert not gateway.requests
    with database.connect() as connection:
        execution = connection.execute("SELECT state,model_call_id FROM learning_evaluation_execution").fetchone()
        calls = connection.execute("SELECT call_id,status FROM model_call ORDER BY rowid").fetchall()
        assert tuple(execution) == ("unknown", None)
        assert len(calls) == 2
        assert tuple(calls[0]) == (unrelated, "started")
        assert calls[1]["status"] == "failed", "The unlinked unsent model call was abandoned"
        connection.execute("DROP TRIGGER reject_evaluation_call_link")
        connection.commit()
    with pytest.raises(LearningEvaluationUnknownError):
        asyncio.run(evaluator.evaluate("eval"))
    assert not gateway.requests
