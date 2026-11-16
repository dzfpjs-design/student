import asyncio
import threading

import httpx

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent import rubric_calibration_host
from test_learning_outcome_evaluator import evaluation_fixture


def test_actual_execute_leaves_health_and_execution_status_readable(tmp_path, monkeypatch):
    database, _, _, gateway, _ = evaluation_fixture(tmp_path)
    application = create_app(Settings(data_dir=tmp_path, database_path=database.path,
        model_api_key=None, fallback_model_api_key=None), model_gateway=gateway)
    pending_controls = []
    original = rubric_calibration_host.prepare
    rendezvous = []

    def slow_validation(*args, **kwargs):
        release = threading.Event()
        pending_controls.append(release)
        rendezvous.append(release.wait(timeout=2))
        return original(*args, **kwargs)

    monkeypatch.setattr(rubric_calibration_host, "prepare", slow_validation)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://isolated") as client:
            task = asyncio.create_task(client.post("/v1/agent-turns/eval/execute"))
            try:
                checked = 0
                while not task.done():
                    if checked == len(pending_controls):
                        await asyncio.sleep(0.001)
                        continue
                    health = await client.get("/v1/health")
                    status = await client.get("/v1/agent-turns/eval/execution-status")
                    assert health.status_code == status.status_code == 200
                    assert health.json()["status"] == "ok"
                    assert status.json()["status"] == "in_progress"
                    assert not task.done()
                    pending_controls[checked].set()
                    checked += 1
                assert checked == len(pending_controls) and checked > 0
                result = await task
                assert result.status_code == 200, result.text
                finished = await client.get("/v1/agent-turns/eval/execution-status")
                assert finished.json()["status"] == "completed"
            finally:
                for release in pending_controls:
                    release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert rendezvous and all(rendezvous)
    assert len(gateway.requests) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM agent_outcome").fetchone()[0] == 1
