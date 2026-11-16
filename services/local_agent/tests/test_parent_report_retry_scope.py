from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.repository import ReportNotReadyError, StudyRepository


CHILD_A = "child-junior-3"
CHILD_B = "child-primary-4"


class NoModelCalls:
    configured = False

    def __init__(self) -> None:
        self.calls: list[object] = []

    async def complete(self, request):
        self.calls.append(request)
        raise AssertionError("synthetic report retry must not call an external model")


def client_for(tmp_path: Path, gateway: NoModelCalls) -> TestClient:
    return TestClient(create_app(Settings(
        data_dir=tmp_path, database_path=tmp_path / "test.db",
        data_encryption_key=b"R" * 32, maintenance_enabled=False,
        agent_runtime_enabled=False,
    ), model_gateway=gateway))


def pending_report(repository: StudyRepository, child_id: str, suffix: str = "one") -> str:
    session_id = repository.start_session(child_id, f"synthetic-start-{child_id}-{suffix}")["session_id"]
    # Only the reflection state is arranged synthetically; its completion and
    # report job use the real durable product transaction.
    with repository.database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (session_id,))
        connection.commit()
    repository.complete_with_reflection(
        session_id, "合成学习记录", "合成待核对事项", "平静", "明天核对", False,
        f"synthetic-reflection-{child_id}-{suffix}",
    )
    return session_id


def report_rows(repository: StudyRepository) -> dict:
    with repository.database.connect() as connection:
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("report_retry_request", "report_job", "daily_report", "parent_report_snapshot", "model_call")}


def retry(client: TestClient, token: str, child_id: str, action: str):
    return client.post(f"/v1/parent/children/{child_id}/reports/retry",
                       headers={"X-HSC-Parent": token}, json={"client_action_id": action})


def assert_scope_denial(response, *private_values: str) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "REPORT_NOT_READY"
    for value in private_values:
        assert value not in response.text


def test_repository_started_retry_rejects_other_child_without_writes(tmp_path: Path) -> None:
    gateway = NoModelCalls()
    with client_for(tmp_path, gateway) as client:
        repository = client.app.state.repository
        pending_report(repository, CHILD_A)
        pending_report(repository, CHILD_B)
        original = repository.prepare_report_retry(CHILD_A, "synthetic-shared-action")
        before = report_rows(repository)
        with pytest.raises(ReportNotReadyError):
            repository.prepare_report_retry(CHILD_B, "synthetic-shared-action")
        assert report_rows(repository) == before
        assert repository.prepare_report_retry(CHILD_A, "synthetic-shared-action") == original
    assert gateway.calls == []


def test_api_started_retry_cannot_generate_the_other_child_report(tmp_path: Path) -> None:
    gateway = NoModelCalls()
    with client_for(tmp_path, gateway) as client:
        repository = client.app.state.repository
        session_a = pending_report(repository, CHILD_A)
        pending_report(repository, CHILD_B)
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        original = repository.prepare_report_retry(CHILD_A, "synthetic-started-action")
        before = report_rows(repository)
        assert_scope_denial(retry(client, token, CHILD_B, "synthetic-started-action"),
                           CHILD_A, session_a, original["retry_id"], original["job_id"])
        assert report_rows(repository) == before
        accepted = retry(client, token, CHILD_A, "synthetic-started-action")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["child_id"] == CHILD_A
        assert accepted.json()["session_id"] == session_a
    assert gateway.calls == []


def test_completed_retry_remains_owned_after_restart_and_new_pending_job(tmp_path: Path) -> None:
    gateway = NoModelCalls()
    with client_for(tmp_path, gateway) as client:
        repository = client.app.state.repository
        session_a = pending_report(repository, CHILD_A)
        pending_report(repository, CHILD_B)
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        first = retry(client, token, CHILD_A, "synthetic-completed-action")
        assert first.status_code == 200, first.text
        report = first.json()
        before = report_rows(repository)
        assert_scope_denial(retry(client, token, CHILD_B, "synthetic-completed-action"),
                           CHILD_A, session_a, report["report_id"])
        assert report_rows(repository) == before
        assert retry(client, token, CHILD_A, "synthetic-completed-action").json() == report
        pending_report(repository, CHILD_A, "later")
        assert retry(client, token, CHILD_A, "synthetic-completed-action").json() == report
    with client_for(tmp_path, gateway) as restarted:
        token = restarted.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
        repository = restarted.app.state.repository
        before = report_rows(repository)
        assert retry(restarted, token, CHILD_A, "synthetic-completed-action").json() == report
        assert_scope_denial(retry(restarted, token, CHILD_B, "synthetic-completed-action"),
                           CHILD_A, session_a, report["report_id"])
        assert report_rows(repository) == before
        assert len(before["report_retry_request"]) == 1
    assert gateway.calls == []


def test_concurrent_repository_reuse_has_one_child_owner(tmp_path: Path) -> None:
    gateway = NoModelCalls()
    with client_for(tmp_path, gateway) as client:
        repository = client.app.state.repository
        pending_report(repository, CHILD_A)
        pending_report(repository, CHILD_B)
        barrier = Barrier(2)

        def prepare(child_id):
            barrier.wait(timeout=5)
            try:
                return child_id, repository.prepare_report_retry(child_id, "synthetic-concurrent-action")
            except ReportNotReadyError:
                return child_id, None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(prepare, (CHILD_A, CHILD_B)))
        winners = [(child, receipt) for child, receipt in outcomes if receipt is not None]
        assert len(winners) == 1, outcomes
        child, receipt = winners[0]
        assert receipt["child_id"] == child
        assert repository.prepare_report_retry(child, "synthetic-concurrent-action") == receipt
        rows = report_rows(repository)
        assert len(rows["report_retry_request"]) == 1
        with repository.database.connect() as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert gateway.calls == []


@pytest.mark.parametrize("binding", ["job_child", "job_session"])
def test_retry_rejects_mismatched_durable_job_binding(tmp_path: Path, binding: str) -> None:
    gateway = NoModelCalls()
    with client_for(tmp_path, gateway) as client:
        repository = client.app.state.repository
        pending_report(repository, CHILD_A)
        session_b = pending_report(repository, CHILD_B)
        receipt = repository.prepare_report_retry(CHILD_A, "synthetic-job-binding")
        with repository.database.connect() as connection:
            if binding == "job_child":
                connection.execute("UPDATE report_job SET child_id=? WHERE job_id=?", (CHILD_B, receipt["job_id"]))
            else:
                connection.execute("UPDATE report_job SET session_id=? WHERE job_id=?", (session_b, receipt["job_id"]))
            connection.commit()
        before = report_rows(repository)
        with pytest.raises(ReportNotReadyError):
            repository.prepare_report_retry(CHILD_A, "synthetic-job-binding")
        assert report_rows(repository) == before
    assert gateway.calls == []


@pytest.mark.parametrize("other_child", [CHILD_A, CHILD_B])
def test_completed_retry_rejects_foreign_child_or_session_report(tmp_path: Path, other_child: str) -> None:
    gateway = NoModelCalls()
    with client_for(tmp_path, gateway) as client:
        repository = client.app.state.repository
        session_a = pending_report(repository, CHILD_A)
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        first = retry(client, token, CHILD_A, "synthetic-bound-report")
        assert first.status_code == 200, first.text
        other_session = pending_report(repository, other_child, "other")
        other = retry(client, token, other_child, "synthetic-other-report")
        assert other.status_code == 200, other.text
        with repository.database.connect() as connection:
            connection.execute("UPDATE report_retry_request SET result_report_id=? WHERE client_action_id=?",
                               (other.json()["report_id"], "synthetic-bound-report"))
            connection.commit()
        before = report_rows(repository)
        assert_scope_denial(retry(client, token, CHILD_A, "synthetic-bound-report"),
                           session_a, other_session, other.json()["report_id"])
        assert report_rows(repository) == before
    assert gateway.calls == []


def test_new_retry_rejects_pending_job_for_another_child_session_before_insert(tmp_path: Path) -> None:
    gateway = NoModelCalls()
    with client_for(tmp_path, gateway) as client:
        repository = client.app.state.repository
        session_a = pending_report(repository, CHILD_A)
        session_b = pending_report(repository, CHILD_B)
        with repository.database.connect() as connection:
            connection.execute("UPDATE report_job SET session_id=? WHERE child_id=?", (session_b, CHILD_A))
            connection.commit()
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        before = report_rows(repository)
        assert_scope_denial(retry(client, token, CHILD_A, "synthetic-new-job-binding"), session_a, session_b, CHILD_B)
        assert report_rows(repository) == before
        assert before["report_retry_request"] == []
    assert gateway.calls == []
