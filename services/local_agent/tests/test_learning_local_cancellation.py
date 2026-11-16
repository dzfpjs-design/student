import asyncio
import threading

import pytest

from home_study_agent.learning_outcome_evaluator import LearningEvaluationUnknownError
from test_learning_outcome_evaluator import evaluation_fixture


@pytest.mark.parametrize("phase", ["claim", "prepare", "persist"])
def test_cancel_drains_local_work_and_does_not_leave_a_rebillable_claim(tmp_path, monkeypatch, phase):
    database, _, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    if phase == "prepare":
        from home_study_agent import rubric_calibration_host
        target, name = rubric_calibration_host, "prepare"
    else:
        target, name = evaluator, "_claim" if phase == "claim" else "_persist"
    original = getattr(target, name)

    def blocked(*args, **kwargs):
        value = original(*args, **kwargs) if phase == "claim" else None
        entered.set()
        try:
            assert release.wait(timeout=3), "Test failed to release the local worker"
            return value if phase == "claim" else original(*args, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(target, name, blocked)

    async def exercise():
        task = asyncio.create_task(evaluator.evaluate("eval"))
        try:
            async def started():
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(started(), timeout=2)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), "Cancellation abandoned a live local worker"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert exited.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    with database.connect() as connection:
        state = connection.execute("SELECT state FROM learning_evaluation_execution").fetchone()[0]
        pending_calls = connection.execute("SELECT count(*) FROM model_call WHERE status='started'").fetchone()[0]
    assert state == ("completed" if phase == "persist" else "unknown")
    assert pending_calls == 0
    assert len(gateway.requests) == (1 if phase == "persist" else 0)
    monkeypatch.setattr(target, name, original)
    if phase == "persist":
        asyncio.run(evaluator.evaluate("eval"))
        assert len(gateway.requests) == 1
    else:
        with pytest.raises(LearningEvaluationUnknownError):
            asyncio.run(evaluator.evaluate("eval"))
        assert not gateway.requests
