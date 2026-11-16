"""The evaluator must leave the event loop available for voice controls."""
import asyncio
import threading

import pytest

from test_learning_outcome_evaluator import evaluation_fixture


@pytest.mark.parametrize("phase", ["prepare", "persist"])
def test_local_scoring_work_does_not_starve_voice_control(tmp_path, monkeypatch, phase):
    database, _, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    pending_controls = []
    observations = []

    if phase == "prepare":
        from home_study_agent import rubric_calibration_host
        target, name = rubric_calibration_host, "prepare"
    else:
        target, name = evaluator, "_persist"
    original = getattr(target, name)

    def slow_local_validation(*args, **kwargs):
        # Every validation (including write revalidation) needs its own probe;
        # one already-set event would hide starvation on later invocations.
        received = threading.Event()
        pending_controls.append(received)
        observations.append(received.wait(timeout=0.5))
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, slow_local_validation)

    async def exercise():
        async def receive_control():
            while True:
                for received in pending_controls:
                    received.set()
                await asyncio.sleep(0.001)

        control = asyncio.create_task(receive_control())
        try:
            result = await evaluator.evaluate("eval")
            return result
        finally:
            control.cancel()
            await asyncio.gather(control, return_exceptions=True)

    result = asyncio.run(exercise())
    assert result["evaluation"]["certification_status"] == "candidate"
    assert len(gateway.requests) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM agent_outcome").fetchone()[0] == 1
        assert connection.execute(
            "SELECT state FROM learning_evaluation_execution"
        ).fetchone()[0] == "completed"
    assert observations and all(observations), "Local scoring blocked receipt of a concurrent voice control"
