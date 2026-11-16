"""Only private pilot API denial; no consent, outbox or SMTP success is fabricated."""
import importlib
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from home_study_agent.config import Settings
from home_study_agent.notification_channels import SmtpChannel


PUBLIC_RECIPIENT = "guardian@example.test"
PUBLIC_TOKEN = "public-synthetic-pilot-private-session"


def _requests(client, headers):
    return (
        client.get("/v1/pilot-test-digest/due",
                   params={"recipient": PUBLIC_RECIPIENT, "send_hour": 22}, headers=headers),
        client.post("/v1/pilot-test-digest/2026-10-27/result",
                    json={"recipient": PUBLIC_RECIPIENT, "status": "unknown",
                          "error_code": "PILOT_SUBMISSION_RESULT_UNKNOWN"}, headers=headers),
        client.post("/v1/pilot-test-digest/2026-10-27/submit-email",
                    json={"recipient": PUBLIC_RECIPIENT,
                          "purpose": "runtime_log_attachment_verification",
                          "client_action_id": "public-private-session-denial"}, headers=headers),
    )


def _spies(application, module, monkeypatch):
    watched = []
    for name in ("claim_due", "record_result", "reserve_verification", "reserve_submission"):
        spy = MagicMock(side_effect=AssertionError("AUTH_REJECTION_MUST_PRECEDE_SERVICE_WORK"))
        monkeypatch.setattr(application.state.pilot_test_telemetry, name, spy)
        watched.append(spy)
    delivery = MagicMock(side_effect=AssertionError("AUTH_REJECTION_MUST_PRECEDE_SMTP"))
    monkeypatch.setattr(application.state.report_delivery, "submit_diagnostic_email", delivery)
    watched.append(delivery)
    smtp = MagicMock(side_effect=AssertionError("SMTP_MUST_NEVER_BE_ENTERED"))
    monkeypatch.setattr(SmtpChannel, "send", smtp)
    watched.append(smtp)
    worker = AsyncMock(side_effect=AssertionError("AUTH_REJECTION_MUST_PRECEDE_WORKER"))
    monkeypatch.setattr(module, "run_local", worker)
    return watched, worker


def _assert_denied(responses, watched, worker):
    assert len(responses) == 3
    for response in responses:
        assert response.status_code == 401
        body = response.json()
        assert body.get("code", body.get("detail", {}).get("code")) == "LOCAL_SESSION_REQUIRED"
    for spy in watched:
        spy.assert_not_called()
    worker.assert_not_awaited()


def test_missing_provisioned_pilot_session_denies_all_valid_routes_before_work(tmp_path, monkeypatch):
    module = importlib.import_module("home_study_agent.app")
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "synthetic-no-token.db",
        data_encryption_key=b"0" * 32, session_token=None, maintenance_enabled=False,
    )
    application = module.create_app(settings)
    with TestClient(application) as client:
        watched, worker = _spies(application, module, monkeypatch)
        _assert_denied(_requests(client, {}), watched, worker)


def test_configured_pilot_session_denies_missing_or_wrong_headers_before_work(tmp_path, monkeypatch):
    module = importlib.import_module("home_study_agent.app")
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "synthetic-wrong-token.db",
        data_encryption_key=b"0" * 32, session_token=PUBLIC_TOKEN, maintenance_enabled=False,
    )
    application = module.create_app(settings)
    with TestClient(application) as client:
        watched, worker = _spies(application, module, monkeypatch)
        for headers in ({}, {"X-HSC-Session": "different-public-synthetic-token"}):
            _assert_denied(_requests(client, headers), watched, worker)
