from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
from home_study_agent.notification_channels import ChannelReceipt


def service_for(tmp_path):
    database = Database(tmp_path / "family.db", media_root=tmp_path, data_encryption_key=b"0" * 32)
    database.initialize()
    service = PilotTestTelemetryService(database, tmp_path)
    service.initialize()
    return database, service


def seed_activity(database):
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
               VALUES ('child-junior-3','初三试用孩子','junior_3','jiangsu_nanjing','active',
                       '2026-09-25T11:55:00+00:00','2026-09-25T11:55:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
               VALUES ('session-seed-1','child-junior-3','active','study','2026-09-25T11:56:00+00:00',
                       '2026-09-25T11:56:00+00:00','pilot-session-action')"""
        )
        connection.execute(
            """INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                   action_json,orchestrator_version,tool_status,created_at,client_action_id)
               VALUES ('turn-pilot','child-junior-3','session-seed-1','我想请你看这道题','tutor_help',
                       '好，我先看你做到哪一步。','{}','test','completed',
                       '2026-09-25T12:00:00+00:00','pilot-turn-action')"""
        )
        connection.execute(
            """INSERT INTO model_call(call_id,request_id,session_id,child_id,purpose,provider,model,status,
                   latency_ms,input_chars,output_chars,prompt_version,context_manifest_json,risk_level,
                   provider_preference,policy_version,fallback_used,prompt_tokens,completion_tokens,created_at,completed_at)
               VALUES ('call-pilot','request-pilot','session-seed-1','child-junior-3','voice_turn','deepseek',
                       'deepseek-v4-pro','succeeded',420,20,30,'v1','{}','low','cloud','v1',0,20,10,
                       '2026-09-25T12:00:01+00:00','2026-09-25T12:00:02+00:00')"""
        )
        connection.commit()
    authorize_diagnostic_recipient(database)


def authorize_diagnostic_recipient(database):
    from home_study_agent.report_delivery import ReportDeliveryService
    with database.connect() as connection:
        child=connection.execute("SELECT child_id FROM child_profile LIMIT 1").fetchone()
        if child is None:
            connection.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)VALUES('diagnostic-test-child','公开合成收件授权','primary_3','jiangsu_nanjing','active','2026-09-25T00:00:00+00:00','2026-09-25T00:00:00+00:00')")
            child_id='diagnostic-test-child'
        else:
            child_id=child['child_id']
        connection.commit()
    delivery=ReportDeliveryService(database,b'0'*32);delivery.initialize()
    delivery.add_recipient(child_id,'email','guardian@example.test','合成明确授权收件人',True)
    with database.connect()as connection:
        connection.execute("INSERT OR IGNORE INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,subject,body,digest_sha256,created_at,updated_at)VALUES('2026-01-01','guardian@example.test','sent',1,'legacy family pilot','legacy-family-body','legacy','old','old')")
        connection.commit()


def test_claims_previous_day_before_send_hour_and_is_idempotent(tmp_path):
    database, service = service_for(tmp_path)
    seed_activity(database)
    now = datetime(2026, 9, 26, 5, 0, tzinfo=timezone.utc)  # 13:00 Asia/Shanghai
    claim = service.claim_due("guardian@example.test", now=now)
    assert claim is not None
    assert claim.delivery_date == "2026-09-25"
    assert "我想请你看这道题" not in claim.body
    assert "好，我先看你做到哪一步" not in claim.body
    assert "不包含孩子原话或智能体回复" in claim.body
    assert "deepseek/deepseek-v4-pro" in claim.body
    assert service.claim_due("guardian@example.test", now=now) is None


def test_sent_result_is_durable_and_not_reissued(tmp_path):
    database, service = service_for(tmp_path)
    seed_activity(database)
    now = datetime(2026, 9, 26, 5, 0, tzinfo=timezone.utc)
    claim = service.claim_due("guardian@example.test", now=now)
    owned = service.reserve_submission(claim.delivery_date, claim.recipient, claim.digest_sha256, claim.claim_token)
    service.finish_owned_submission(owned, status="sent", transport="smtp")
    result = service.record_result(claim.delivery_date, claim.recipient, status="sent", transport="simple_mapi", claim_token=claim.claim_token)
    assert result["status"] == "sent"
    restarted = PilotTestTelemetryService(database, tmp_path)
    restarted.initialize()
    assert restarted.claim_due("guardian@example.test", now=now) is None


def test_crash_during_external_effect_becomes_unknown_without_duplicate(tmp_path):
    database, service = service_for(tmp_path)
    seed_activity(database)
    now = datetime(2026, 9, 26, 5, 0, tzinfo=timezone.utc)
    claim = service.claim_due("guardian@example.test", now=now)
    assert claim
    service.reserve_submission(claim.delivery_date, claim.recipient, claim.digest_sha256, claim.claim_token)
    restarted = PilotTestTelemetryService(database, tmp_path)
    restarted.initialize()
    assert restarted.claim_due("guardian@example.test", now=now) is None
    with database.connect() as connection:
        row = connection.execute("SELECT status,error_code FROM pilot_test_digest_outbox WHERE delivery_date=?", (claim.delivery_date,)).fetchone()
        assert row["status"] == "unknown"
        assert row["error_code"] == "DELIVERY_RESULT_UNKNOWN_AFTER_RESTART"


def test_digest_never_contains_media_paths_or_diagnostic_secret_fields(tmp_path):
    database, service = service_for(tmp_path)
    seed_activity(database)
    log_dir = tmp_path / "diagnostics" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "runtime.jsonl").write_text(
        '{"timestamp":"2026-09-25T12:30:00Z","level":"error","source":"device",'
        '"event":"device.connect.failed","apiKey":"synthetic-14111632506e5a67a4c4c44a","path":"C:/secret/frame.jpg"}\n',
        encoding="utf-8",
    )
    body = service.build_digest(datetime(2026, 9, 25).date())
    assert "device.connect.failed" in body
    assert "synthetic-14111632506e5a67a4c4c44a" not in body
    assert "frame.jpg" not in body


def test_local_session_api_claim_cannot_assert_server_smtp_delivery(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "app.db",
        data_encryption_key=b"0" * 32,
        session_token="pilot-local-session",
        maintenance_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        authorize_diagnostic_recipient(app.state.pilot_test_telemetry.database)
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        target = local_now.date() if local_now.hour >= 22 else local_now.date() - timedelta(days=1)
        observed = datetime.combine(target, time(hour=12), ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc).isoformat()
        with app.state.pilot_test_telemetry.database.connect() as connection:
            connection.execute(
                """INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                   VALUES ('pilot-api-audit','system','pilot_api_test',NULL,'{}',?)""",
                (observed,),
            )
            connection.commit()
        headers = {"X-HSC-Session": "pilot-local-session"}
        claim = client.get(
            "/v1/pilot-test-digest/due",
            params={"recipient": "guardian@example.test"},
            headers=headers,
        )
        assert claim.status_code == 200, claim.text
        payload = claim.json()
        assert payload["due"] is True
        result = client.post(
            f"/v1/pilot-test-digest/{payload['delivery_date']}/result",
            json={"recipient": "guardian@example.test", "status": "sent", "transport": "simple_mapi", "claim_token": payload["claim_token"]},
            headers=headers,
        )
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "failed"


def test_local_session_api_submits_frozen_digest_through_configured_smtp(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "app.db",
        data_encryption_key=b"0" * 32,
        session_token="pilot-local-session",
        maintenance_enabled=False,
    )
    app = create_app(settings)
    sent = []

    class CapturingEmail:
        def send(self, recipient, subject, text, html, config, *, attachments=()):
            sent.append((recipient, subject, text, html, config, attachments))
            return ChannelReceipt(provider_message_id="provider-1", client_message_id="client-1")

    app.state.report_delivery.adapters["email"] = CapturingEmail()
    with TestClient(app) as client:
        authorize_diagnostic_recipient(app.state.pilot_test_telemetry.database)
        app.state.report_delivery.save_channel_config("email", {
            "host": "smtp.example.test",
            "port": 587,
            "username": "family@example.test",
            "from_address": "family@example.test",
            "starttls": True,
            "allow_external_delivery": True,
            "password": "synthetic-password",
        })
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        target = local_now.date() if local_now.hour >= 22 else local_now.date() - timedelta(days=1)
        observed = datetime.combine(target, time(hour=12), ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc).isoformat()
        with app.state.pilot_test_telemetry.database.connect() as connection:
            connection.execute(
                """INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                   VALUES ('pilot-smtp-audit','system','pilot_smtp_test',NULL,'{}',?)""",
                (observed,),
            )
            connection.commit()
        headers = {"X-HSC-Session": "pilot-local-session"}
        claim = client.get(
            "/v1/pilot-test-digest/due",
            params={"recipient": "guardian@example.test"},
            headers=headers,
        ).json()
        response = client.post(
            f"/v1/pilot-test-digest/{claim['delivery_date']}/submit-email",
            json={"recipient": claim["recipient"], "digest_sha256": claim["digest_sha256"], "claim_token": claim["claim_token"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["transport"] == "smtp"
        assert sent and sent[0][0] == "guardian@example.test"
        assert sent[0][2] == claim["body"]
        assert sent[0][4]["password"] == "synthetic-password"
        assert len(sent[0][5]) == 1 and sent[0][5][0].content_type == "application/zip"


def test_pilot_smtp_submission_rejects_digest_hash_mismatch_without_send(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "app.db",
        data_encryption_key=b"0" * 32,
        session_token="pilot-local-session",
        maintenance_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        authorize_diagnostic_recipient(app.state.pilot_test_telemetry.database)
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        target = local_now.date() if local_now.hour >= 22 else local_now.date() - timedelta(days=1)
        observed = datetime.combine(target, time(hour=12), ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc).isoformat()
        with app.state.pilot_test_telemetry.database.connect() as connection:
            connection.execute(
                """INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                   VALUES ('pilot-mismatch-audit','system','pilot_mismatch_test',NULL,'{}',?)""",
                (observed,),
            )
            connection.commit()
        headers = {"X-HSC-Session": "pilot-local-session"}
        claim = client.get(
            "/v1/pilot-test-digest/due",
            params={"recipient": "guardian@example.test"},
            headers=headers,
        ).json()
        response = client.post(
            f"/v1/pilot-test-digest/{claim['delivery_date']}/submit-email",
            json={"recipient": claim["recipient"], "digest_sha256": "0" * 64, "claim_token": claim["claim_token"]},
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "PILOT_DIGEST_CLAIM_MISMATCH"
