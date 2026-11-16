from __future__ import annotations

import json

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings


def test_request_diagnostics_record_metadata_without_body_or_query(tmp_path, capsys):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "diagnostics.db",
        data_encryption_key=b"0" * 32,
        maintenance_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/bootstrap?api_key=sk-never-log-this")

    assert response.status_code == 200
    assert response.headers["X-HSC-Trace"]
    lines = [
        line.removeprefix("HSC_DIAGNOSTIC ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("HSC_DIAGNOSTIC ")
    ]
    record = next(json.loads(line) for line in lines if json.loads(line)["event"] == "local_api_request")
    assert record["correlationId"] == response.headers["X-HSC-Trace"]
    assert record["details"]["path"] == "/v1/bootstrap"
    assert record["details"]["statusCode"] == 200
    assert record["details"]["durationMs"] >= 0
    assert "sk-never-log-this" not in json.dumps(record)
