from pathlib import Path

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings


def test_parent_answer_attempt_ledger_requires_parent_and_is_child_scoped(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "study.db",
        data_encryption_key=b"0" * 32,
        maintenance_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        denied = client.get("/v1/parent/children/child-junior-3/answer-attempt-ledger")
        assert denied.status_code == 401
        token = client.post("/v1/parent/setup", json={"pin": "7391"}).json()["parent_token"]
        response = client.get(
            "/v1/parent/children/child-junior-3/answer-attempt-ledger?limit=25",
            headers={"X-HSC-Parent": token},
        )
        assert response.status_code == 200
        assert response.json() == {"items": []}
        missing = client.get(
            "/v1/parent/children/not-a-child/answer-attempt-ledger",
            headers={"X-HSC-Parent": token},
        )
        assert missing.status_code == 404

