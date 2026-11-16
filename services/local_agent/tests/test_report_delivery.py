from __future__ import annotations

import json
import base64
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from importlib import import_module
import asyncore
import smtpd
import threading
import time
from email import policy
from email.parser import BytesParser

import pytest
from normal_provider_owner import normal_provider_owner
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

import home_study_agent.app as app_module
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.family_clock import FAMILY_ZONE
from home_study_agent.notification_channels import ChannelDeliveryError, ChannelReceipt
from home_study_agent.notification_channels import SmtpChannel, WeComBotChannel
from home_study_agent.model_gateway import ModelResponseError, ModelResult
from home_study_agent.parent_reports import ParentReportService, SnapshotFeedbackConflictError, SnapshotStateError
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.report_delivery import ChannelTestActionConflictError, DeliveryStateError, ReportDeliveryService, _next_quiet_end, render_message
from home_study_agent.report_auto_delivery import verify_and_schedule_reports
from home_study_agent.repository import StudyRepository, coaching_evidence_ref, utc_now
from home_study_agent.reports import build_daily_report
from home_study_agent.learning_state import LearningStateService
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.cloud_parent_reports import _merge_cloud_wording, _validated_update
from home_study_agent.period_reports import PeriodEvidenceActionConflictError, PeriodReportService


class RecordingEmail:
    channel = "email"

    def __init__(self, failure: ChannelDeliveryError | None = None) -> None:
        self.failure = failure
        self.messages: list[tuple[str, str, str]] = []

    def send(self, recipient: str, subject: str, text: str, html: str, config: dict) -> ChannelReceipt:
        if self.failure:
            raise self.failure
        self.messages.append((recipient, subject, text))
        return ChannelReceipt(provider_message_id="local-mail-sink-001")


class SlowRecordingEmail(RecordingEmail):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def send(self, recipient: str, subject: str, text: str, html: str, config: dict) -> ChannelReceipt:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.04)
        with self._lock:
            self.active -= 1
        return super().send(recipient, subject, text, html, config)


class CloudReportGateway:
    configured = True

    async def complete(self, _request):
        return ModelResult(
            content=json.dumps({
                "scenario": "只处理今天已确认的一处不确定。", "why_this_fits": "建议只依据已确认的学习记录。",
                "script": ["谢谢你把进度留下来。", "我们只看下一小步。"],
                "action": {"what": "圈出明天第一步", "duration_minutes": 8, "steps": ["孩子先选一处", "到时停止"], "stop_condition": "8分钟后停止"},
                "avoid": [{"do_not_say": "你怎么又没做好？", "say_instead": "我们只核对这一小步。"}],
            }, ensure_ascii=False), provider="qwen", model="test-cloud-report", latency_ms=12, prompt_tokens=20, completion_tokens=30,
        )


class TimedOutCloudReportGateway:
    configured = True

    async def complete(self, _request):
        raise ModelResponseError("MODEL_TIMEOUT", fallback_eligible=True)


class MalformedCloudReportGateway:
    configured = True

    async def complete(self, _request):
        return ModelResult(
            content=json.dumps({
                "scenario": "缺少行动步骤。", "script": ["第一句", "第二句"],
                "action": {"what": "不完整行动"},
                "avoid": [{"do_not_say": "别追问", "say_instead": "只问一件具体事实"}],
            }, ensure_ascii=False), provider="qwen", model="malformed-cloud-report", latency_ms=4, prompt_tokens=4, completion_tokens=4,
        )


class LocalMailSink(smtpd.SMTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), None)
        self.messages: list[tuple[str, list[str], bytes]] = []

    def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
        self.messages.append((mailfrom, rcpttos, data))
        return None


def _local_coaching_baseline(duration_minutes: int = 8) -> dict:
    return {
        "scenario": "本地证据场景。",
        "why_this_fits": "本地理由：只根据孩子确认的事实和已保存反馈。",
        "evidence_basis": ["孩子确认：今天只处理一道题"],
        "adaptation": {"basis": "parent_feedback", "feedback_count": 2, "note": "过去反馈压力偏高，缩短时长。"},
        "personalization": {"child_stage": "四年级", "goal_context": "PET目标", "feedback_considered": 2},
        "script": ["本地话术一。", "本地话术二。"],
        "action": {"what": "本地行动", "duration_minutes": duration_minutes, "steps": ["本地第一步"], "stop_condition": "本地停止条件。"},
        "avoid": [{"do_not_say": "本地禁语", "say_instead": "本地替代"}],
    }


def _cloud_wording(action: dict | None = None, **extra: object) -> dict:
    return {
        "scenario": "云端润色后的场景。",
        "script": ["云端话术一。", "云端话术二。"],
        "action": action or {"what": "云端行动", "steps": ["云端第一步"]},
        "avoid": [{"do_not_say": "云端禁语", "say_instead": "云端替代"}],
        **extra,
    }


def test_cloud_coaching_allows_only_wording_and_preserves_local_personalization() -> None:
    baseline = _local_coaching_baseline()
    update = _validated_update(json.dumps(_cloud_wording(
        {"what": "云端行动", "steps": ["云端第一步"], "duration_minutes": 30, "stop_condition": "云端停止"},
        why_this_fits="云端不能替换本地理由。", evidence_basis=["伪造依据"],
        adaptation={"basis": "cloud"}, personalization={"child_stage": "伪造"}, unexpected="ignored",
    ), ensure_ascii=False))
    merged = _merge_cloud_wording(baseline, update)
    assert merged["scenario"] == "云端润色后的场景。"
    assert merged["script"] == ["云端话术一。", "云端话术二。"]
    assert merged["avoid"] == [{"do_not_say": "云端禁语", "say_instead": "云端替代"}]
    assert merged["why_this_fits"] == baseline["why_this_fits"]
    assert merged["evidence_basis"] == baseline["evidence_basis"]
    assert merged["adaptation"] == baseline["adaptation"]
    assert merged["personalization"] == baseline["personalization"]
    assert merged["action"] == {"what": "云端行动", "duration_minutes": 8, "steps": ["云端第一步"], "stop_condition": "本地停止条件。"}


@pytest.mark.parametrize("payload", [
    {"script": ["一句", "两句"], "action": {"what": "动作", "steps": ["一步"]}, "avoid": [{"do_not_say": "别说", "say_instead": "改说"}]},
    {"scenario": "场景", "script": ["一句", "两句"], "action": {"what": "动作"}, "avoid": [{"do_not_say": "别说", "say_instead": "改说"}]},
])
def test_cloud_coaching_rejects_missing_required_wording(payload: dict) -> None:
    with pytest.raises(ValueError, match="PARENT_REPORT_INVALID"):
        _validated_update(json.dumps(payload, ensure_ascii=False))


def test_cloud_coaching_ignores_non_integer_duration_and_keeps_local_boundary() -> None:
    baseline = _local_coaching_baseline()
    update = _validated_update(json.dumps(_cloud_wording({"what": "云端行动", "steps": ["云端第一步"], "duration_minutes": "30", "stop_condition": 30}), ensure_ascii=False))
    merged = _merge_cloud_wording(baseline, update)
    assert merged["action"]["duration_minutes"] == 8
    assert merged["action"]["stop_condition"] == "本地停止条件。"


def test_cloud_coaching_cannot_add_action_when_local_evidence_is_insufficient() -> None:
    baseline = _local_coaching_baseline(duration_minutes=0)
    baseline["focus"] = "insufficient_evidence"
    update = _validated_update(json.dumps(_cloud_wording(
        {"what": "云端追加30分钟", "steps": ["必须解释后再加练"], "duration_minutes": 30},
        scenario="今晚必须加练。", avoid=[{"do_not_say": "先休息", "say_instead": "必须解释"}],
    ), ensure_ascii=False))
    merged = _merge_cloud_wording(baseline, update)
    assert merged == baseline
    assert "必须加练" not in json.dumps(merged, ensure_ascii=False)
    assert "必须解释" not in json.dumps(merged, ensure_ascii=False)


@pytest.mark.parametrize("baseline", [
    {**_local_coaching_baseline(), "action": None},
    {**_local_coaching_baseline(), "action": {**_local_coaching_baseline()["action"], "duration_minutes": "8"}},
    {**_local_coaching_baseline(), "action": {**_local_coaching_baseline()["action"], "duration_minutes": -1}},
    {**_local_coaching_baseline(), "focus": "insufficient_evidence"},
])
def test_cloud_coaching_fails_closed_without_a_safe_local_boundary(baseline: dict) -> None:
    update = _validated_update(json.dumps(_cloud_wording(
        {"what": "云端强制行动", "steps": ["今晚必须解释"], "duration_minutes": 30},
        scenario="今晚加练。", avoid=[{"do_not_say": "可以休息", "say_instead": "必须解释"}],
    ), ensure_ascii=False))
    assert _merge_cloud_wording(baseline, update) == baseline


def test_cloud_coaching_cannot_extend_local_duration_with_malicious_value() -> None:
    baseline = _local_coaching_baseline(duration_minutes=6)
    update = _validated_update(json.dumps(_cloud_wording({"what": "云端行动", "steps": ["云端第一步"], "duration_minutes": 9999, "stop_condition": "无限时"}), ensure_ascii=False))
    merged = _merge_cloud_wording(baseline, update)
    assert merged["action"]["duration_minutes"] == 6
    assert merged["action"]["stop_condition"] == "本地停止条件。"


def test_smtp_adapter_submits_html_and_plain_text_to_local_mail_sink(normal_provider_owner) -> None:
    sink = LocalMailSink()
    def serve() -> None:
        try:
            asyncore.loop(timeout=0.01)
        except OSError:
            # Closing the local sink wakes select() on macOS with EBADF.
            pass
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        port = sink.socket.getsockname()[1]
        receipt = SmtpChannel().send("parent@example.test", "日报", "纯文本内容", "<p>HTML内容</p>", {
            "host": "127.0.0.1", "port": port, "from_address": "report@example.test",
            "starttls": False, "allow_external_delivery": True,
        })
        assert receipt.provider_message_id is None
        for _ in range(50):
            if sink.messages: break
            threading.Event().wait(0.01)
        assert sink.messages and sink.messages[0][0] == "report@example.test"
        parsed = BytesParser(policy=policy.default).parsebytes(sink.messages[0][2])
        assert any(part.get_filename() == "学习成长报告.html" for part in parsed.iter_attachments())
    finally:
        sink.close()
        worker.join(timeout=1)


def test_approved_daily_snapshot_reaches_local_smtp_sink_once_after_restart(tmp_path: Path, normal_provider_owner) -> None:
    """Production-shaped local acceptance: no network, no real family data, no mocks."""
    database = Database(tmp_path / "daily-smtp-acceptance.db")
    database.initialize()
    report = _daily_report(database)  # synthetic fixture text only
    sink = LocalMailSink()

    def serve() -> None:
        try:
            asyncore.loop(timeout=0.01)
        except OSError:
            pass

    worker_thread = threading.Thread(target=serve, daemon=True)
    worker_thread.start()
    try:
        delivery = ReportDeliveryService(database, b"A" * 32)
        delivery.initialize()
        # Keep this test outside quiet hours regardless of the wall clock.
        start = (datetime.now(FAMILY_ZONE).hour + 1) % 24
        end = (datetime.now(FAMILY_ZONE).hour + 2) % 24
        delivery.update_preferences("child-junior-3", f"{start:02d}:00", f"{end:02d}:00", 90)
        snapshot = delivery.snapshots.approve(delivery.snapshots.create_from_daily(report)["snapshot_id"])
        recipient = delivery.add_recipient("child-junior-3", "email", "mailbox@local.test", "本机验收收件箱", True)
        delivery.save_channel_config("email", {
            "host": "127.0.0.1", "port": sink.socket.getsockname()[1],
            "from_address": "report@local.test", "starttls": False,
            "allow_external_delivery": True,
        })
        queued = delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
        assert delivery.process_due() == [delivery.get_delivery(queued["delivery_id"])]
        assert delivery.get_delivery(queued["delivery_id"])["status"] == "sent"
        for _ in range(50):
            if sink.messages:
                break
            threading.Event().wait(0.01)
        assert len(sink.messages) == 1
        parsed = BytesParser(policy=policy.default).parsebytes(sink.messages[0][2])
        assert "计划20分钟，已记录完成20分钟" in parsed.get_body(("plain",)).get_content()
        assert "建议这样和孩子说" in parsed.get_body(("html",)).get_content()
        assert any(part.get_filename() == "学习成长报告.html" for part in parsed.iter_attachments())

        # Reopening the durable SQLite outbox cannot resend an accepted delivery.
        restarted = ReportDeliveryService(database, b"A" * 32)
        restarted.initialize()
        assert restarted.process_due() == []
        assert len(sink.messages) == 1
        persisted = restarted.get_delivery(queued["delivery_id"])
        assert persisted["status"] == "sent" and persisted["attempt_count"] == 1
    finally:
        sink.close()
        worker_thread.join(timeout=1)


def test_channel_test_status_is_child_scoped_persisted_and_wecom_is_truthfully_blocked(tmp_path: Path) -> None:
    database = Database(tmp_path / "channel-test-status.db")
    database.initialize(); _daily_report(database)
    service = ReportDeliveryService(database, b"T" * 32); service.initialize()
    missing = service.test_channel("child-junior-3", "email", "mail-test-action-0001")
    assert missing["status"] == "failed" and "白名单" in missing["message"]
    assert service.test_channel("child-junior-3", "email", "mail-test-action-0001") == missing
    service.add_recipient("child-junior-3", "email", "parent@local.test", "妈妈", True)
    not_configured = service.test_channel("child-junior-3", "email", "mail-test-action-0002")
    assert not_configured["status"] == "failed" and "保存邮件发送设置" in not_configured["message"]
    wecom = service.test_channel("child-junior-3", "wechat_work_bot", "mail-test-action-0003")
    assert wecom["status"] == "blocked" and "个人微信" in wecom["message"]
    with pytest.raises(ChannelTestActionConflictError):
        service.test_channel("child-primary-4", "email", "mail-test-action-0003")
    restarted = ReportDeliveryService(database, b"T" * 32); restarted.initialize()
    statuses = {item["channel"]: item for item in restarted.channel_test_status("child-junior-3")}
    assert statuses["email"]["status"] == "failed"
    assert statuses["wechat_work_bot"]["status"] == "blocked"
    assert restarted.channel_test_status("child-primary-4")[0]["status"] is None
    with database.connect() as connection:
        audit = connection.execute("SELECT action, summary_json FROM audit_event WHERE action = 'report_delivery_channel_tested'").fetchall()
    assert len(audit) == 3 and all("parent@local.test" not in row["summary_json"] for row in audit)


def test_smtp_test_mail_is_idempotent_local_only_and_never_creates_a_report_delivery(tmp_path: Path, normal_provider_owner) -> None:
    database = Database(tmp_path / "smtp-test-mail.db")
    database.initialize(); _daily_report(database)
    service = ReportDeliveryService(database, b"M" * 32); service.initialize()
    recipient = service.add_recipient("child-junior-3", "email", "sink@local.test", "本机验收收件箱", True)
    sink = LocalMailSink()

    def serve() -> None:
        try:
            asyncore.loop(timeout=0.01)
        except OSError:
            pass

    worker = threading.Thread(target=serve, daemon=True); worker.start()
    try:
        service.save_channel_config("email", {
            "host": "127.0.0.1", "port": sink.socket.getsockname()[1], "from_address": "report@local.test",
            "starttls": False, "allow_external_delivery": True,
        })
        action_id = "smtp-sink-test-action-0001"
        accepted = service.test_channel("child-junior-3", "email", action_id)
        assert accepted["status"] == "passed" and "本机 SMTP sink已接受" in accepted["message"]
        for _ in range(50):
            if sink.messages:
                break
            threading.Event().wait(0.01)
        assert len(sink.messages) == 1
        parsed = BytesParser(policy=policy.default).parsebytes(sink.messages[0][2])
        body = parsed.get_body(("plain",)).get_content() + parsed.get_body(("html",)).get_content()
        with database.connect() as connection:
            nickname = connection.execute("SELECT nickname FROM child_profile WHERE child_id = 'child-junior-3'").fetchone()["nickname"]
            assert connection.execute("SELECT COUNT(*) FROM report_delivery_outbox").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM parent_report_snapshot").fetchone()[0] == 0
        assert "这是一封本机发送测试" in body
        assert nickname not in body and "计划20分钟" not in body and "判别式" not in body
        assert service.test_channel("child-junior-3", "email", action_id) == accepted
        assert len(sink.messages) == 1
        with database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action = 'report_delivery_channel_tested'").fetchone()[0] == 1
    finally:
        sink.close(); worker.join(timeout=1)


def test_plaintext_smtp_is_limited_to_a_local_sink(tmp_path: Path) -> None:
    database = Database(tmp_path / "smtp-tls-boundary.db"); database.initialize()
    service = ReportDeliveryService(database, b"L" * 32); service.initialize()
    with pytest.raises(ValueError, match="外部邮箱必须使用 SSL/TLS"):
        service.save_channel_config("email", {"host": "smtp.example.test", "from_address": "report@example.test", "starttls": False, "allow_external_delivery": True})
    service.save_channel_config("email", {"host": "localhost", "from_address": "report@local.test", "starttls": False, "allow_external_delivery": True})
    service.save_channel_config("email", {"host": "smtp.example.test", "port": 465, "from_address": "report@example.test", "use_ssl": True, "starttls": False, "allow_external_delivery": True})


def test_channel_test_action_schema_upgrade_preserves_unknown_recovery_and_foreign_key(tmp_path: Path) -> None:
    database = Database(tmp_path / "channel-test-migration.db"); database.initialize(); StudyRepository(database).seed_family()
    with database.connect() as connection:
        connection.execute("""
          CREATE TABLE report_delivery_channel_test_action (
            client_action_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
            channel TEXT NOT NULL CHECK(channel IN ('email', 'wechat_work_bot')),
            status TEXT NOT NULL CHECK(status IN ('processing', 'passed', 'failed', 'blocked')),
            message TEXT, tested_at TEXT, created_at TEXT NOT NULL
          )
        """)
        connection.execute("""
          INSERT INTO report_delivery_channel_test_action(client_action_id, child_id, channel, status, created_at)
          VALUES ('legacy-processing-action-001', 'child-junior-3', 'email', 'processing', ?)
        """, (utc_now(),))
        connection.commit()
    service = ReportDeliveryService(database, b"U" * 32); service.initialize()
    statuses = {item["channel"]: item for item in service.channel_test_status("child-junior-3")}
    assert statuses["email"]["status"] == "unknown" and statuses["email"]["client_action_id"] == "legacy-processing-action-001"
    with database.connect() as connection:
        action = connection.execute("SELECT status, replacement_action_id FROM report_delivery_channel_test_action WHERE client_action_id = 'legacy-processing-action-001'").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_list(report_delivery_channel_test_action)").fetchall()
        indexes = connection.execute("PRAGMA index_list(report_delivery_channel_test_action)").fetchall()
    assert action["status"] == "unknown" and action["replacement_action_id"] is None
    assert any(row["table"] == "child_profile" for row in foreign_keys)
    assert any(row["origin"] == "pk" for row in indexes)


def test_outbox_schema_upgrade_preserves_attempts_indexes_and_foreign_keys(tmp_path: Path) -> None:
    database = Database(tmp_path / "outbox-migration.db"); database.initialize()
    snapshot = ParentReportService(database).create_from_daily(_daily_report(database))
    now = utc_now()
    with database.connect() as connection:
        connection.executescript("""
          CREATE TABLE report_delivery_recipient (
            recipient_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
            channel TEXT NOT NULL, address TEXT NOT NULL, label TEXT NOT NULL, consent_status TEXT NOT NULL,
            consented_at TEXT, revoked_at TEXT, created_at TEXT NOT NULL, UNIQUE(child_id, channel, address)
          );
          CREATE TABLE report_delivery_outbox (
            delivery_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE RESTRICT,
            recipient_id TEXT NOT NULL REFERENCES report_delivery_recipient(recipient_id) ON DELETE RESTRICT,
            channel TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('scheduled', 'processing', 'sent', 'failed', 'cancelled')),
            scheduled_at TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, last_error_message TEXT,
            provider_message_id TEXT, sent_at TEXT, cancelled_at TEXT, cancel_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
          );
          CREATE INDEX idx_report_delivery_due ON report_delivery_outbox(status, scheduled_at);
          CREATE TABLE report_delivery_attempt (
            attempt_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES report_delivery_outbox(delivery_id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL, status TEXT NOT NULL, error_code TEXT, error_message TEXT, provider_message_id TEXT, created_at TEXT NOT NULL
          );
        """)
        connection.execute("INSERT INTO report_delivery_recipient VALUES ('legacy-recipient', 'child-junior-3', 'email', 'legacy@local.test', '监护人', 'consented', ?, NULL, ?)", (now, now))
        for status in ("sent", "failed", "scheduled", "processing", "cancelled"):
            delivery_id = f"legacy-{status}"
            connection.execute("""
              INSERT INTO report_delivery_outbox(delivery_id, snapshot_id, recipient_id, channel, idempotency_key, status, scheduled_at, attempt_count, created_at, updated_at)
              VALUES (?, ?, 'legacy-recipient', 'email', ?, ?, ?, 1, ?, ?)
            """, (delivery_id, snapshot["snapshot_id"], f"legacy-key-{status}", status, now, now, now))
            connection.execute("INSERT INTO report_delivery_attempt VALUES (?, ?, 1, ?, NULL, NULL, NULL, ?)", (f"legacy-attempt-{status}", delivery_id, status, now))
        connection.execute("INSERT INTO report_delivery_attempt VALUES ('legacy-attempt-sent-2', 'legacy-sent', 2, 'sent', NULL, NULL, 'provider-legacy', ?)", (now,))
        connection.commit()
    migrated = ReportDeliveryService(database, b"G" * 32); migrated.initialize()
    with database.connect() as connection:
        statuses = {row["delivery_id"]: row["status"] for row in connection.execute("SELECT delivery_id, status FROM report_delivery_outbox").fetchall()}
        attempts = connection.execute("SELECT COUNT(*) FROM report_delivery_attempt").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_list(report_delivery_outbox)").fetchall()
        indexes = connection.execute("PRAGMA index_list(report_delivery_outbox)").fetchall()
        leftovers = connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'report_delivery_%_legacy'").fetchall()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert statuses == {"legacy-sent": "sent", "legacy-failed": "failed", "legacy-scheduled": "scheduled", "legacy-processing": "unknown", "legacy-cancelled": "cancelled"}
    assert attempts == 7 and not leftovers and not violations and enabled == 1
    assert {row["table"] for row in foreign_keys} == {"parent_report_snapshot", "report_delivery_recipient"}
    assert any(row["name"] == "idx_report_delivery_due" for row in indexes)


def test_outbox_schema_upgrade_rolls_back_without_legacy_tables_on_integrity_failure(tmp_path: Path) -> None:
    database = Database(tmp_path / "outbox-migration-rollback.db"); database.initialize(); StudyRepository(database).seed_family()
    now = utc_now()
    with database.connect() as connection:
        connection.executescript("""
          CREATE TABLE report_delivery_recipient (recipient_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id), channel TEXT NOT NULL, address TEXT NOT NULL, label TEXT NOT NULL, consent_status TEXT NOT NULL, consented_at TEXT, revoked_at TEXT, created_at TEXT NOT NULL);
          CREATE TABLE report_delivery_outbox (delivery_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id), recipient_id TEXT NOT NULL REFERENCES report_delivery_recipient(recipient_id), channel TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL CHECK(status IN ('scheduled', 'processing', 'sent', 'failed', 'cancelled')), scheduled_at TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, last_error_message TEXT, provider_message_id TEXT, sent_at TEXT, cancelled_at TEXT, cancel_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
          CREATE INDEX idx_report_delivery_due ON report_delivery_outbox(status, scheduled_at);
          CREATE TABLE report_delivery_attempt (attempt_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES report_delivery_outbox(delivery_id), attempt_number INTEGER NOT NULL, status TEXT NOT NULL, error_code TEXT, error_message TEXT, provider_message_id TEXT, created_at TEXT NOT NULL);
        """)
        connection.commit(); connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("INSERT INTO report_delivery_attempt VALUES ('orphan-attempt', 'missing-delivery', 1, 'processing', NULL, NULL, NULL, ?)", (now,))
        connection.commit(); connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(DeliveryStateError, match="升级校验失败"):
        ReportDeliveryService(database, b"H" * 32).initialize()
    with database.connect() as connection:
        names = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        connection.execute("DELETE FROM report_delivery_attempt WHERE attempt_id = 'orphan-attempt'"); connection.commit()
    assert "report_delivery_outbox" in names and "report_delivery_attempt" in names and not any(name.endswith("_legacy") for name in names) and enabled == 1
    ReportDeliveryService(database, b"H" * 32).initialize()


def test_unknown_test_action_never_resends_and_reissue_is_atomic(tmp_path: Path) -> None:
    database = Database(tmp_path / "channel-test-reissue.db"); database.initialize(); _daily_report(database)
    service = ReportDeliveryService(database, b"R" * 32); service.initialize()
    service.add_recipient("child-junior-3", "email", "parent@local.test", "妈妈", True)
    service.save_channel_config("email", {"host": "127.0.0.1", "from_address": "report@local.test", "starttls": False, "allow_external_delivery": True})
    sender = RecordingEmail(); service.adapters["email"] = sender
    with database.connect() as connection:
        connection.execute("""
          INSERT INTO report_delivery_channel_test_action(client_action_id, child_id, channel, status, message, tested_at, created_at)
          VALUES ('unknown-test-action-001', 'child-junior-3', 'email', 'unknown', '上次测试结果未知，可能已发出。', ?, ?)
        """, (utc_now(), utc_now()))
        selected = service._test_recipient('child-junior-3','email')
        service._record_channel_test_selection(connection,'child-junior-3','unknown-test-action-001',
            {'recipient_id':selected['recipient_id'],'address_tag':service._channel_test_address_tag('child-junior-3',selected)},utc_now())
        connection.commit()
    replay = service.test_channel("child-junior-3", "email", "unknown-test-action-001")
    assert replay["status"] == "unknown" and sender.messages == []
    kept = service.keep_unknown_channel_test("child-junior-3", "email", "unknown-test-action-001")
    assert kept["kept_without_resend"] is True
    assert service.keep_unknown_channel_test("child-junior-3", "email", "unknown-test-action-001") == kept
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action = 'report_delivery_channel_test_kept_without_resend'").fetchone()[0] == 1

    barrier = threading.Barrier(2); outcomes: list[dict] = []; errors: list[Exception] = []
    def reissue(action_id: str) -> None:
        try:
            barrier.wait(timeout=2)
            outcomes.append(service.reissue_unknown_channel_test("child-junior-3", "email", "unknown-test-action-001", action_id))
        except Exception as error:
            errors.append(error)
    first = threading.Thread(target=reissue, args=("replacement-test-action-001",))
    second = threading.Thread(target=reissue, args=("replacement-test-action-002",))
    first.start(); second.start(); first.join(timeout=3); second.join(timeout=3)
    assert len(outcomes) == 1 and outcomes[0]["status"] == "passed"
    assert len(errors) == 1 and isinstance(errors[0], ChannelTestActionConflictError)
    assert len(sender.messages) == 1
    with database.connect() as connection:
        previous = connection.execute("SELECT status, replacement_action_id FROM report_delivery_channel_test_action WHERE client_action_id = 'unknown-test-action-001'").fetchone()
    assert previous["status"] == "superseded" and previous["replacement_action_id"] in {"replacement-test-action-001", "replacement-test-action-002"}
    assert service.reissue_unknown_channel_test("child-junior-3", "email", "unknown-test-action-001", previous["replacement_action_id"])["status"] == "passed"
    assert len(sender.messages) == 1


def test_processing_channel_test_is_visible_after_refresh_and_becomes_unknown_after_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "channel-test-processing.db"); database.initialize(); _daily_report(database)
    service = ReportDeliveryService(database, b"P" * 32); service.initialize()
    with database.connect() as connection:
        connection.execute("""
          INSERT INTO report_delivery_channel_test_action(client_action_id, child_id, channel, status, created_at)
          VALUES ('processing-test-action-001', 'child-junior-3', 'email', 'processing', ?)
        """, (utc_now(),))
        connection.commit()
    processing = {item["channel"]: item for item in service.channel_test_status("child-junior-3")}["email"]
    assert processing["status"] == "processing" and processing["client_action_id"] == "processing-test-action-001"
    service.adapters["email"] = RecordingEmail()
    assert service.test_channel("child-junior-3", "email", "processing-test-action-001")["status"] == "processing"
    assert service.adapters["email"].messages == []
    restarted = ReportDeliveryService(database, b"P" * 32); restarted.initialize()
    unknown = {item["channel"]: item for item in restarted.channel_test_status("child-junior-3")}["email"]
    assert unknown["status"] == "unknown" and "可能已发出" in unknown["message"]


def test_delivery_channel_password_is_memory_only_and_never_returned_or_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "credentials.db")
    database.initialize()
    service = ReportDeliveryService(database, b"7" * 32)
    service.initialize()
    service.save_channel_config("email", {
        "host": "smtp.example.test", "username": "report@example.test", "password": "synthetic-da3fd0c5abaa850aa76db57f",
        "from_address": "report@example.test", "allow_external_delivery": True,
    })
    with database.connect() as connection:
        stored = connection.execute("SELECT encrypted_json FROM notification_credential WHERE channel = 'email'").fetchone()["encrypted_json"]
    assert "synthetic-da3fd0c5abaa850aa76db57f" not in stored
    assert service._config("email")["password"] == "synthetic-da3fd0c5abaa850aa76db57f"
    # Process restart loses an in-memory test password rather than silently
    # retaining it in SQLite.  A desktop secure-store bridge must re-inject it.
    assert "password" not in ReportDeliveryService(database, b"7" * 32)._config("email")
    status = next(item for item in service.channel_status() if item["channel"] == "email")
    assert status["configured"] is True
    assert "password" not in status
    service.clear_channel_config("email")
    assert "email_password" not in service._runtime_secrets
    assert next(item for item in service.channel_status() if item["channel"] == "email")["configured"] is False
    with pytest.raises(ChannelDeliveryError, match="尚未配置"):
        service._config("email")
    service.save_channel_config("email", {
        "host": "smtp.example.test", "username": "report@example.test",
        "from_address": "report@example.test", "allow_external_delivery": True,
    })
    assert "password" not in service._config("email")
    service.save_channel_config("wechat_work_bot", {"webhook_url": "https://example.test/synthetic-webhook", "allow_external_delivery": True})
    assert service._config("wechat_work_bot")["webhook_url"] == "https://example.test/synthetic-webhook"
    service.clear_channel_config("wechat_work_bot")
    assert "wechat_work_bot_webhook" not in service._runtime_secrets
    service.save_channel_config("wechat_work_bot", {"allow_external_delivery": True})
    assert "webhook_url" not in service._config("wechat_work_bot")


def test_delivery_agent_receives_secure_launcher_secret_after_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = Database(tmp_path / "launcher-secret.db")
    database.initialize()
    key = b"S" * 32
    initial = ReportDeliveryService(database, key)
    initial.initialize()
    initial.save_channel_config("email", {
        "host": "smtp.example.test", "username": "parent@example.test", "password": "current-process-test-secret",
        "from_address": "parent@example.test", "allow_external_delivery": True,
    })
    # This models Electron safeStorage restoring the secret into the new
    # local-agent child environment.  SQLite contains only public metadata.
    monkeypatch.setenv("HSC_REPORT_DELIVERY_EMAIL_PASSWORD", "launcher-injected-test-secret")
    restarted = ReportDeliveryService(database, key)
    restarted.initialize()
    assert restarted._config("email")["password"] == "launcher-injected-test-secret"
    with database.connect() as connection:
        stored = connection.execute("SELECT encrypted_json FROM notification_credential WHERE channel = 'email'").fetchone()["encrypted_json"]
    assert "current-process-test-secret" not in stored and "launcher-injected-test-secret" not in stored


def test_delivery_initialize_scrubs_legacy_persisted_provider_secrets(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy-delivery-secret.db")
    database.initialize()
    key = b"Q" * 32
    legacy = {"host": "smtp.example.test", "username": "parent@example.test", "password": "legacy-test-only-password", "from_address": "parent@example.test", "webhook_url": "https://example.test/legacy", "allow_external_delivery": True}
    nonce = b"0" * 12
    encrypted = AESGCM(key).encrypt(nonce, json.dumps(legacy).encode(), b"hsc-notification-credential-v1")
    now = utc_now()
    with database.connect() as connection:
        connection.execute("CREATE TABLE notification_credential (credential_id TEXT PRIMARY KEY, channel TEXT NOT NULL UNIQUE, encrypted_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO notification_credential VALUES ('legacy-email', 'email', ?, ?, ?)", (base64.b64encode(nonce + encrypted).decode(), now, now))
        connection.commit()
    migrated = ReportDeliveryService(database, key)
    migrated.initialize()
    with database.connect() as connection:
        stored = connection.execute("SELECT encrypted_json FROM notification_credential WHERE channel = 'email'").fetchone()["encrypted_json"]
    raw = base64.b64decode(stored)
    public = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], b"hsc-notification-credential-v1"))
    assert "password" not in public and "webhook_url" not in public
    assert public["host"] == "smtp.example.test"


def test_wecom_group_bot_rejects_an_overlong_message_before_network_submission(normal_provider_owner) -> None:
    with pytest.raises(ChannelDeliveryError) as error:
        WeComBotChannel().send(
            "家庭学习群", "日报", "证据" * 1100, "<p>ignored</p>",
            {"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test", "allow_external_delivery": True},
        )
    assert error.value.code == "WECOM_MESSAGE_TOO_LARGE"


def _daily_report(database: Database) -> dict:
    repository = StudyRepository(database)
    repository.seed_family()
    LearningStateService(database).seed_catalog()
    with database.connect() as connection:
        connection.execute("""UPDATE family_rule SET max_learning_minutes=30,
            task_segment_minutes=20,latest_end_time=NULL WHERE child_id='child-junior-3'""")
        connection.commit()
    session_id = repository.start_session("child-junior-3", "delivery-session-action")["session_id"]
    repository.transition(session_id, "check_in", "classroom_recall", "开始学习", "delivery-check-in")
    repository.add_recall(session_id, "数学", "今天学习判别式。", "delivery-recall")
    repository.transition(session_id, "classroom_recall", "homework_capture", "回忆已记录", "delivery-homework-stage")
    repository.add_homework(
        session_id, "数学", "判别式练习", 20, "written", None, None, None, None, None, "delivery-homework",
    )
    workspace = repository.generate_plan(session_id, "delivery-plan")
    assert len(workspace["plan_items"]) == 1
    item = workspace["plan_items"][0]
    assert item["source_type"] == "school_homework" and item["expected_minutes"] == 20
    repository.transition(session_id, "plan_review", "study", "确认计划", "delivery-study")
    repository.start_plan_item(item["plan_item_id"], "delivery-task-start")
    repository.complete_plan_item(item["plan_item_id"], 20, "delivery-task-complete")
    repository.transition(session_id, "study", "reflection", "计划已执行", "delivery-reflection-stage")
    repository.complete_with_reflection(
        session_id, "记录了判别式练习的步骤。", "", "还可以", "明天复核第一步。", False, "delivery-reflection",
    )
    assert repository.get_session(session_id)["status"] == "completed"
    evidence = repository.report_evidence(session_id)
    assert len(evidence["recalls"]) == 1 and len(evidence["tasks"]) == 1
    assert evidence["reflection"]["skipped"] == 0
    summary, _ = build_daily_report(evidence)
    summary["headline"] = "完成 1 项学习计划"
    coaching = {"action": {"what": "复习判别式第一步", "duration_minutes": 10},
                "script": ["先说出你已经确定的一步。"], "avoid": []}
    saved = repository.save_daily_report(session_id, summary, coaching)
    return repository.get_daily_report(saved['report_id'])


def _pending_report_job(database: Database) -> None:
    StudyRepository(database).seed_family()
    now = utc_now()
    with database.connect() as connection:
        connection.execute("""INSERT INTO daily_session(session_id, child_id, status, state, started_at, ended_at, last_checkpoint_at, client_action_id)
          VALUES ('pending-report-session', 'child-primary-4', 'completed', 'reflection', ?, ?, ?, 'pending-report-session-action')""", (now, now, now))
        connection.execute("""INSERT INTO daily_reflection(reflection_id, session_id, child_id, learned_text, difficult_text, feeling, next_action, skipped, created_at, client_action_id)
          VALUES ('pending-report-reflection', 'pending-report-session', 'child-primary-4', '会用列式', '', '正常', '明天复习第一步', 0, ?, 'pending-report-reflection-action')""", (now,))
        connection.execute("""INSERT INTO report_job(job_id, session_id, child_id, report_type, status, created_at, updated_at)
          VALUES ('pending-report-job', 'pending-report-session', 'child-primary-4', 'daily', 'pending', ?, ?)""", (now, now))
        connection.commit()


def test_approved_snapshot_is_immutable_and_outbox_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "delivery.db")
    database.initialize()
    report = _daily_report(database)
    snapshots = ParentReportService(database)
    delivery = ReportDeliveryService(database, b"1" * 32)
    delivery.initialize()
    next_hour = (datetime.now(FAMILY_ZONE).hour + 1) % 24
    after_next_hour = (datetime.now(FAMILY_ZONE).hour + 2) % 24
    delivery.update_preferences("child-junior-3", f"{next_hour:02d}:00", f"{after_next_hour:02d}:00", 90)
    snapshot = snapshots.create_from_daily(report)
    assert snapshot["status"] == "draft"
    with pytest.raises(SnapshotStateError):
        delivery.enqueue(snapshot["snapshot_id"], "missing")
    snapshots.edit_draft(snapshot["snapshot_id"], "今晚一起看", "不追问原因")
    approved = snapshots.approve(snapshot["snapshot_id"])
    assert approved["evidence_version"].startswith("sha256:")
    with pytest.raises(SnapshotStateError):
        snapshots.edit_draft(snapshot["snapshot_id"], "改写", None)

    recipient = delivery.add_recipient("child-junior-3", "email", "parent@example.test", "妈妈", consented=True)
    first = delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    duplicate = delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    assert first["delivery_id"] == duplicate["delivery_id"]
    delivery.save_channel_config("email", {"host": "127.0.0.1", "from_address": "report@example.test", "allow_external_delivery": True})
    sink = RecordingEmail()
    delivery.adapters["email"] = sink
    processed = delivery.process_due()
    assert processed[0]["status"] == "sent"
    assert sink.messages[0][0] == "parent@example.test"
    assert "计划20分钟" in sink.messages[0][2]
    # A mutable source-table update does not affect an already-approved snapshot or resend body.
    with database.connect() as connection:
        changed_summary = {**report['summary'], 'headline': '已被修改'}
        connection.execute("UPDATE daily_report SET summary_json = ? WHERE report_id = ?", (json.dumps(changed_summary), report['report_id']))
        connection.commit()
    assert snapshots.get(snapshot["snapshot_id"])["content"]["headline"] == "完成 1 项学习计划"
    queued_for_later = delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"], "2099-01-01T00:00:00+00:00")
    corrected = snapshots.request_correction(snapshot["snapshot_id"], "计划完成记录需要先与孩子核对。")
    assert corrected["status"] == "revoked"
    assert corrected["corrections"][0]["note"] == "计划完成记录需要先与孩子核对。"
    assert delivery.get_delivery(queued_for_later["delivery_id"])["status"] == "cancelled"


def test_reports_scope_deletion_erases_snapshot_corrections(tmp_path: Path) -> None:
    database = Database(tmp_path / "correction-deletion.db")
    database.initialize()
    snapshots = ParentReportService(database)
    snapshot = snapshots.create_from_daily(_daily_report(database))
    snapshots.request_correction(snapshot["snapshot_id"], "这份记录需要删除。")
    deleted = PrivacyLifecycleService(database, tmp_path).delete_child_scope(
        "child-junior-3", "reports", "确认删除", "delete-report-correction-001"
    )
    assert deleted["deleted_counts"]["parent_report_snapshot_correction"] == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM parent_report_snapshot_correction").fetchone()[0] == 0


def test_parent_coaching_uses_persisted_snapshot_feedback_and_keeps_children_isolated(tmp_path: Path) -> None:
    database = Database(tmp_path / "coaching-feedback.db")
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    snapshots = ParentReportService(database)
    junior_report = _daily_report(database)
    primary_session = repository.start_session("child-primary-4", "primary-source-action")["session_id"]
    repository.transition(primary_session, "check_in", "classroom_recall", "开始学习", "primary-source-recall-stage")
    repository.add_recall(primary_session, "英语", "今天记录了PET听力练习。", "primary-source-recall")
    repository.transition(primary_session, "classroom_recall", "homework_capture", "回忆已记录", "primary-source-homework-stage")
    repository.add_homework(primary_session, "英语", "PET听力练习", 20, "written", None, None, None, None, None, "primary-source-homework")
    with database.connect() as connection:
        connection.execute("""UPDATE family_rule SET max_learning_minutes=90,
            task_segment_minutes=20,latest_end_time=NULL WHERE child_id='child-primary-4'""")
        connection.commit()
    workspace = repository.generate_plan(primary_session, "primary-source-plan")
    homework = [item for item in workspace["plan_items"] if item["source_type"] == "school_homework"]
    assert len(homework) == 1
    item = homework[0]
    repository.transition(primary_session, "plan_review", "study", "确认计划", "primary-source-study")
    repository.start_plan_item(item["plan_item_id"], "primary-source-start")
    repository.complete_plan_item(item["plan_item_id"], 20, "primary-source-complete")
    for extra in workspace["plan_items"]:
        if extra["plan_item_id"] != item["plan_item_id"] and extra["status"] == "pending":
            repository.reschedule_plan_item(extra["plan_item_id"], "今晚只完成已确认作业，专项练习另行安排。", f"primary-defer:{extra['plan_item_id']}")
    repository.transition(primary_session, "study", "reflection", "计划已执行", "primary-source-reflection-stage")
    repository.complete_with_reflection(primary_session, "记录了PET听力练习。", "", "还可以", "明天核对细节。", False, "primary-source-reflection")
    summary, coaching = build_daily_report(repository.report_evidence(primary_session))
    saved_primary = repository.save_daily_report(primary_session, summary, coaching)
    primary_report = StudyRepository(Database(database.path)).get_daily_report(saved_primary["report_id"])
    assert primary_report["summary"] == summary
    now = utc_now()
    with database.connect() as connection:
        for session_id, child_id, difficult, action_id in (
            ("primary-next-session", "child-primary-4", "PET听力细节", "primary-next-action"),
            ("junior-next-session", "child-junior-3", "一元二次方程", "junior-next-action"),
        ):
            connection.execute("""INSERT INTO daily_session(session_id, child_id, status, state, started_at, ended_at, last_checkpoint_at, client_action_id)
              VALUES (?, ?, 'completed', 'reflection', ?, ?, ?, ?)""", (session_id, child_id, now, now, now, action_id))
            connection.execute("""INSERT INTO daily_reflection(reflection_id, session_id, child_id, learned_text, difficult_text, feeling, next_action, skipped, created_at, client_action_id)
              VALUES (?, ?, ?, '', ?, '有点累', '只看第一步', 0, ?, ?)""", (f"{session_id}-reflection", session_id, child_id, difficult, now, f"{action_id}-reflection"))
        connection.commit()
    with pytest.raises(SnapshotStateError, match="日报内容已更新"):
        snapshots.create_from_daily({**primary_report, "evidence": {}})
    primary_snapshot = snapshots.create_from_daily(primary_report)
    junior_snapshot = snapshots.create_from_daily(repository.get_daily_report(junior_report['report_id']))
    snapshots.approve(primary_snapshot['snapshot_id'])
    snapshots.approve(junior_snapshot['snapshot_id'])
    first_primary = snapshots.record_feedback(primary_snapshot["snapshot_id"], helpfulness=2, stress_rating=5, notes="对话太长", client_action_id="primary-feedback-first")
    replayed_primary = snapshots.record_feedback(primary_snapshot["snapshot_id"], helpfulness=2, stress_rating=5, notes="对话太长", client_action_id="primary-feedback-first")
    assert len(first_primary["feedback"]) == len(replayed_primary["feedback"]) == 1
    with pytest.raises(SnapshotFeedbackConflictError):
        snapshots.record_feedback(primary_snapshot["snapshot_id"], helpfulness=4, stress_rating=2, notes="内容已改", client_action_id="primary-feedback-first")
    snapshots.record_feedback(primary_snapshot["snapshot_id"], helpfulness=4, stress_rating=2, notes="先让孩子选。", client_action_id="primary-feedback-edited")
    current_rating = repository.report_evidence("primary-next-session")["report_feedback"][0]
    assert (current_rating["helpfulness"], current_rating["stress_rating"]) == (4, 2)
    snapshots.record_feedback(primary_snapshot["snapshot_id"], helpfulness=None, stress_rating=None, notes="今晚已说明，不评分。", client_action_id="primary-feedback-note")
    snapshots.record_feedback(junior_snapshot["snapshot_id"], helpfulness=5, stress_rating=1, notes="先说步骤有效", client_action_id="junior-feedback-first")

    # Reopening the service simulates an app/backend restart: feedback remains in
    # SQLite and is then selected only for the report's own child.
    restarted = ParentReportService(Database(database.path))
    assert restarted.get(primary_snapshot["snapshot_id"])["feedback"][0]["notes"] == "今晚已说明，不评分。"
    primary_evidence = repository.report_evidence("primary-next-session")
    junior_evidence = repository.report_evidence("junior-next-session")
    assert primary_evidence["coaching_feedback"] == junior_evidence["coaching_feedback"] == []
    # A note-only current revision must not resurrect an earlier numeric rating.
    assert primary_evidence["report_feedback"] == []
    current = restarted.get(primary_snapshot["snapshot_id"])
    assert current["feedback_current_status"] == "active"
    assert current["feedback_current"]["helpfulness"] is None
    assert len(junior_evidence["report_feedback"]) == 1
    rating = junior_evidence["report_feedback"][0]
    assert (rating["snapshot_id"], rating["helpfulness"], rating["stress_rating"]) == (junior_snapshot["snapshot_id"], 5, 1)
    assert "executed" not in rating and "effectiveness" not in rating
    assert "PET" in primary_evidence["goal_context"] and "数学竞赛与奥赛思维" in primary_evidence["goal_context"]
    assert "中考学科能力与薄弱点闭环" in junior_evidence["goal_context"]
    assert all("PET" not in fact for fact in junior_evidence["recent_confirmed_facts"])
    assert all("判别式" not in fact for fact in primary_evidence["recent_confirmed_facts"])

    _, primary_coaching = build_daily_report(primary_evidence)
    _, junior_coaching = build_daily_report(junior_evidence)
    assert primary_coaching["action"]["duration_minutes"] == 10
    assert primary_coaching["adaptation"]["basis"] == "cold_start"
    assert "先让孩子选择的方式较有效" not in primary_coaching["why_this_fits"]
    assert primary_coaching["adaptation"]["average_effectiveness"] is None
    assert junior_coaching["adaptation"]["average_effectiveness"] is None
    assert "四年级阶段目标" in primary_coaching["why_this_fits"]
    assert junior_coaching["action"]["duration_minutes"] == 12
    assert "初三阶段目标：中考学科能力与薄弱点闭环" in junior_coaching["why_this_fits"]
    assert primary_coaching["personalization"]["feedback_considered"] == 0
    assert junior_coaching["personalization"]["feedback_considered"] == 0
    assert primary_coaching["report_feedback"]["feedback_count"] == 0
    assert junior_coaching["report_feedback"]["feedback_count"] == 1
    with database.connect() as connection:
        audited = connection.execute("SELECT COUNT(*) FROM audit_event WHERE action = 'parent_report_snapshot_feedback_saved'").fetchone()[0]
    assert audited == 4


def test_snapshot_feedback_api_replays_matching_action_and_rejects_conflict(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot-feedback-api.db"
    database = Database(database_path)
    database.initialize()
    snapshot = ParentReportService(database).create_from_daily(_daily_report(database))
    other_snapshot = ParentReportService(database).create_from_period({
        "period_report_id": "feedback-other-period", "child_id": "child-primary-4", "report_type": "weekly",
        "source": "local_aggregate_v2", "period_end": "2026-08-30",
        "content": {"headline": "另一份报告", "coverage": {}, "daily_signals": [], "interpretation": "仅测试动作隔离。", "next_action": {}, "parent_script": []},
    })
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"I" * 32, maintenance_enabled=False)
    payload = {"helpfulness": 3, "stress_rating": 2, "notes": "先听孩子说完", "client_action_id": "feedback-replay-001"}
    with TestClient(create_app(settings)) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        first = client.post(f"/v1/parent/report-snapshots/{snapshot['snapshot_id']}/feedback", headers=headers, json=payload)
        replay = client.post(f"/v1/parent/report-snapshots/{snapshot['snapshot_id']}/feedback", headers=headers, json=payload)
        conflict = client.post(f"/v1/parent/report-snapshots/{snapshot['snapshot_id']}/feedback", headers=headers, json={**payload, "stress_rating": 5})
        blank = client.post(f"/v1/parent/report-snapshots/{snapshot['snapshot_id']}/feedback", headers=headers, json={"helpfulness": None, "stress_rating": None, "notes": None, "client_action_id": "feedback-blank-001"})
        note_only = client.post(f"/v1/parent/report-snapshots/{snapshot['snapshot_id']}/feedback", headers=headers, json={"helpfulness": None, "stress_rating": None, "notes": "仅留审计说明", "client_action_id": "feedback-note-only-001"})
        global_conflict = client.post(f"/v1/parent/report-snapshots/{other_snapshot['snapshot_id']}/feedback", headers=headers, json=payload)
    assert first.status_code == replay.status_code == 200
    assert len(replay.json()["feedback"]) == 1
    assert conflict.status_code == 409
    assert blank.status_code == 422
    assert note_only.status_code == 200 and note_only.json()["feedback"][0]["notes"] == "仅留审计说明"
    assert global_conflict.status_code == 409


def test_parent_coaching_declares_insufficient_evidence_instead_of_assigning_a_problem() -> None:
    _, coaching = build_daily_report({
        "child": {"grade": "初三"}, "session": {},
        "reflection": {"learned_text": "", "difficult_text": "", "feeling": "", "next_action": "", "skipped": False},
        "tasks": [], "recalls": [], "knowledge_evidence": [], "mistakes": [], "coaching_feedback": [],
        "goal_context": "初三中考复习阶段：只围绕当晚已确认的校内或复习任务安排，不以临时加量回应压力。",
    })
    assert coaching["focus"] == "insufficient_evidence"
    assert coaching["action"]["duration_minutes"] == 0
    assert "不把空白记录解释成问题" in coaching["scenario"]
    assert "不追加学习" in coaching["action"]["what"]


def test_period_coaching_provides_executable_script_action_and_avoid_examples() -> None:
    coaching = PeriodReportService._period_coaching(
        "child-junior-3",
        {"grade": "初三"},
        {
            "coverage": {"eligible_evidence_count": 3},
            "pending_confirmation": [{"title": "根的判别式"}],
            "independent_variant_retests": [],
            "evidence": [{"knowledge_title": "根的判别式", "evidence_ref": "ev-1", "date": "2026-09-01"}],
        },
        feedback=[],
    )

    assert coaching["personalization"]["focus_title"] == "根的判别式"
    assert coaching["action"]["duration_minutes"] == 8
    assert len(coaching["action"]["steps"]) == 4
    assert coaching["action"]["next_observation"]
    assert "根的判别式" in coaching["script"][0]
    assert len(coaching["script_phases"]) == 5
    assert all(item["do_not_say"] and item["say_instead"] for item in coaching["avoid"])


def test_period_coaching_with_no_evidence_limits_action_to_record_check() -> None:
    coaching = PeriodReportService._period_coaching(
        "child-primary-4",
        {"grade": "四年级"},
        {
            "coverage": {"eligible_evidence_count": 0},
            "pending_confirmation": [],
            "independent_variant_retests": [],
            "evidence": [],
        },
        feedback=[{"stress_rating": 5}],
    )

    assert coaching["focus"] == "insufficient_evidence"
    assert coaching["action"]["duration_minutes"] == 5
    assert "不补题" in coaching["script"][3]
    assert "随时停止" in coaching["adaptation"]["note"]


def test_period_report_aggregates_goal_execution_without_claiming_mastery(tmp_path: Path) -> None:
    database = Database(tmp_path / "period-goals.db")
    database.initialize()
    StudyRepository(database).seed_family()
    goals = GoalProgramService(database)
    goals.cycles("child-primary-4", date.fromisoformat("2026-08-28"))
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO daily_session(session_id, child_id, status, state, started_at, last_checkpoint_at, client_action_id)
               VALUES ('goal-period-session', 'child-primary-4', 'completed', 'completed', '2026-08-27T09:00:00Z', '2026-08-27T10:00:00Z', 'goal-period-session-action')"""
        )
        connection.execute(
            """INSERT INTO daily_plan(plan_id, child_id, session_id, plan_date, status, total_minutes, generator, version, created_at, client_action_id)
               VALUES ('goal-period-plan', 'child-primary-4', 'goal-period-session', '2026-08-27', 'completed', 20, 'test', 1, '2026-08-27T09:00:00Z', 'goal-period-plan-action')"""
        )
        connection.execute(
            """INSERT INTO plan_item(plan_item_id, plan_id, subject, title, expected_minutes, actual_minutes, priority, status, position, source_type, source_ref, completed_at)
               VALUES ('goal-period-item', 'goal-period-plan', '英语', 'PET阅读', 20, 18, 1, 'completed', 1, 'pet_daily', 'pet-reading', '2026-08-27T09:30:00Z')"""
        )
        connection.commit()

    content = PeriodReportService(database).generate(
        "child-primary-4", "weekly", date.fromisoformat("2026-08-28")
    )["content"]
    pet = next(item for item in content["goal_progress"] if item["track"] == "pet_path")
    assert pet["completed_minutes"] == 18
    assert pet["completed_sessions"] == 1
    assert pet["evidence_items"] == 1
    assert "长期目标执行" in " ".join(content["parent_coaching"]["evidence_basis"])
    assert "不等同稳定掌握" in " ".join(content["parent_coaching"]["evidence_basis"])


def test_parent_coaching_uses_behavior_feedback_and_keeps_children_distinct() -> None:
    """A declined reminder and high family pressure must alter only that child's plan."""
    shared = {
        "session": {"recall_declared_empty": False, "homework_declared_empty": False},
        "reflection": {"skipped": False, "learned_text": "", "difficult_text": "", "next_action": "", "feeling": ""},
        "tasks": [{"subject": "数学", "title": "复测", "status": "completed", "expected_minutes": 10, "actual_minutes": 10}],
        "recalls": [], "knowledge_evidence": [{"knowledge_title": "一个知识点", "independent": True}], "mistakes": [],
        "planning_context": {"energy_level": "medium", "fixed_commitment_minutes": 0},
    }
    _, primary = build_daily_report({
        **shared, "child": {"grade": "四年级"}, "goal_context": "四年级阶段目标：PET周目标50分钟，已记录10分钟",
        "coaching_feedback": [{"executed": 1, "effectiveness": 2, "stress_rating": 5, "notes_summary": "先听孩子说完", "updated_at": "2026-08-28T10:00:00Z",
                               "feedback_context": {"focus": "completed_day", "child_stage": "四年级", "subjects": ["数学"], "version": "local_rules_v2"}}],
        "behavior_intervention_feedback": [{"kind": "intervention_declined", "summary": "孩子对一次温和提醒选择“稍后”", "confidence": 0.8, "evidence_ref": "evidence-primary-feedback", "source": "行为模型的孩子确认反馈", "occurred_at": "2026-08-28T10:00:00+00:00"}],
    })
    _, junior = build_daily_report({
        **shared, "child": {"grade": "初三"}, "goal_context": "初三中考复习阶段：只围绕当晚已确认的校内或复习任务安排。",
        "coaching_feedback": [{"executed": 1, "effectiveness": 5, "stress_rating": 1, "notes_summary": "先定步骤有效", "updated_at": "2026-08-28T10:00:00Z",
                               "feedback_context": {"focus": "completed_day", "child_stage": "初三", "subjects": ["数学"], "version": "local_rules_v2"}}],
        "behavior_intervention_feedback": [{"kind": "intervention_accepted", "summary": "孩子接受一次温和提醒", "confidence": 0.9, "evidence_ref": "evidence-junior-feedback", "source": "行为模型的孩子确认反馈", "occurred_at": "2026-08-28T10:00:00+00:00"}],
    })
    assert primary["action"]["duration_minutes"] < junior["action"]["duration_minutes"]
    # Unbound legacy ratings are not qualified action execution/outcome evidence.
    assert primary["adaptation"]["average_stress"] is None
    assert junior["adaptation"]["average_effectiveness"] is None
    assert "稍后" in " ".join(primary["evidence_basis"])
    assert "不追问原因" in primary["why_this_fits"]
    assert "中考复习" in junior["why_this_fits"]
    assert "PET" not in " ".join(junior["evidence_basis"])
    assert primary["script_phases"][0]["phase"] == "开场"
    assert [item["phase"] for item in primary["script_phases"]] == ["开场", "倾听", "共情", "共同选择", "收尾"]
    assert primary["uncertainty"]


def test_parent_coaching_treats_conflicting_behavior_feedback_as_uncertain() -> None:
    _, coaching = build_daily_report({
        "session": {"recall_declared_empty": False, "homework_declared_empty": False},
        "child": {"grade": "初三"}, "goal_context": "初三中考复习阶段：只围绕当晚确认的任务安排。",
        "reflection": {"skipped": False, "learned_text": "", "difficult_text": "函数图像", "next_action": "圈出条件", "feeling": ""},
        "tasks": [], "recalls": [], "knowledge_evidence": [], "mistakes": [], "coaching_feedback": [],
        "planning_context": {"energy_level": "medium", "fixed_commitment_minutes": 0},
        "behavior_intervention_feedback": [
            {"kind": "intervention_accepted", "summary": "孩子接受过一次温和学习提醒", "confidence": .8},
            {"kind": "intervention_declined", "summary": "孩子对一次温和学习提醒选择“稍后”", "confidence": .8},
        ],
    })
    assert "接受过" in " ".join(coaching["evidence_basis"])
    assert "稍后" in " ".join(coaching["evidence_basis"])
    assert "不追问原因" in coaching["why_this_fits"]
    assert "不用于判断情绪、态度、人格或因果" in coaching["uncertainty"]
    assert "诊断" not in json.dumps(coaching, ensure_ascii=False)


def test_parent_can_adopt_skip_or_customize_coaching_before_approval_and_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "coaching-edit.db"); database.initialize()
    service = ParentReportService(database)
    snapshot = service.create_from_daily(_daily_report(database))
    edited = service.edit_draft(
        snapshot["snapshot_id"], "今晚只看一件事", "不比较", coaching_decision="custom",
        coaching_script="先听完，再一起选第一步。", coaching_action="只一起圈出第一处。",
        coaching_observation="观察孩子是否愿意自己选下一步。",
    )
    assert edited["parent_edit"]["coaching_decision"] == "custom"
    assert edited["parent_edit"]["coaching_script"] == "先听完，再一起选第一步。"
    assert edited["parent_edit"]["coaching_observation"] == "观察孩子是否愿意自己选下一步。"
    _, text, _ = render_message(edited)
    assert "先听完，再一起选第一步。" in text
    assert "只一起圈出第一处。" in text
    assert "观察与明天调整" in text and "观察孩子是否愿意自己选下一步。" in text
    skipped = service.edit_draft(snapshot["snapshot_id"], "", "", coaching_decision="skip")
    assert skipped["parent_edit"]["coaching_decision"] == "skip"
    _, skipped_text, _ = render_message(skipped)
    assert "家长本次选择不执行额外行动" in skipped_text
    assert "观察与明天调整" not in skipped_text
    edited = service.edit_draft(snapshot["snapshot_id"], "今晚只看一件事", "不比较", coaching_decision="custom", coaching_script="先听完，再一起选第一步。", coaching_action="只一起圈出第一处。", coaching_observation="观察孩子是否愿意自己选下一步。")
    restarted = ParentReportService(Database(database.path)).get(snapshot["snapshot_id"])
    assert restarted["parent_edit"] == edited["parent_edit"]
    service.approve(snapshot["snapshot_id"])
    with pytest.raises(SnapshotStateError):
        service.edit_draft(snapshot["snapshot_id"], "", "", coaching_decision="skip")


def test_system_verifies_current_unedited_report_and_schedules_one_durable_email(tmp_path: Path) -> None:
    database = Database(tmp_path / "system-report-delivery.db"); database.initialize()
    report = _daily_report(database)
    snapshots = ParentReportService(database)
    snapshot = snapshots.create_from_daily(report)
    delivery = ReportDeliveryService(database, b"R" * 32); delivery.initialize()
    delivery.save_channel_config("email", {"host":"127.0.0.1","port":2525,"from_address":"report@example.test","use_ssl":False,"starttls":False})
    recipient = delivery.add_recipient(report["child_id"], "email", "parent@example.test", "家长", True)
    delivery.adapters['email'] = RecordingEmail()
    assert delivery.test_channel(report['child_id'],'email','synthetic-recipient-verification',recipient['recipient_id'])['status'] == 'passed'
    first = verify_and_schedule_reports(snapshots, delivery, report["child_id"], [snapshot])
    assert first["verified"] == 1 and first["scheduled"] == 1
    approved = snapshots.get(snapshot["snapshot_id"])
    assert approved["status"] == "approved" and approved["approved_by"] == "system-verifier"
    assert delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])["status"] == "scheduled"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_outbox WHERE snapshot_id=?", (snapshot["snapshot_id"],)).fetchone()[0] == 1


def test_system_does_not_auto_approve_parent_edited_report(tmp_path: Path) -> None:
    database = Database(tmp_path / "system-report-edited.db"); database.initialize()
    report = _daily_report(database); snapshots = ParentReportService(database)
    snapshot = snapshots.create_from_daily(report)
    snapshots.edit_draft(snapshot["snapshot_id"], parent_edit_patch={"coaching_decision":"skip"})
    with pytest.raises(SnapshotStateError):
        snapshots.approve_system_verified(snapshot["snapshot_id"])
    assert snapshots.get(snapshot["snapshot_id"])["status"] == "draft"


def test_approve_atomically_persists_unsaved_skip_and_custom_duration(tmp_path: Path) -> None:
    database = Database(tmp_path / "approve-current-coaching.db"); database.initialize()
    service = ParentReportService(database)
    skipped = service.create_from_daily(_daily_report(database))
    approved_skip = service.approve(skipped["snapshot_id"], parent_edit_patch={"coaching_decision": "skip"})
    assert approved_skip["status"] == "approved"
    assert approved_skip["parent_edit"]["coaching_decision"] == "skip"
    _, skip_text, _ = render_message(approved_skip)
    assert "家长本次选择不执行额外行动" in skip_text and "约0分钟" in skip_text
    restarted_skip = ParentReportService(Database(database.path)).get(skipped["snapshot_id"])
    _, restarted_skip_text, _ = render_message(restarted_skip)
    assert "约0分钟" in restarted_skip_text and "观察与明天调整" not in restarted_skip_text

    custom = service.create_from_period(PeriodReportService(database).generate('child-primary-4', 'weekly'))
    approved_custom = service.approve(custom["snapshot_id"], parent_edit_patch={
        "coaching_decision": "custom", "coaching_script": "先听完再选择。", "coaching_action": "只圈一处。", "coaching_duration_minutes": 6,
    })
    _, custom_text, _ = render_message(approved_custom)
    assert approved_custom["parent_edit"]["coaching_duration_minutes"] == 6
    assert "只圈一处。" in custom_text and "约6分钟" in custom_text
    restarted_custom = ParentReportService(Database(database.path)).get(custom["snapshot_id"])
    _, restarted_custom_text, _ = render_message(restarted_custom)
    assert "只圈一处。" in restarted_custom_text and "约6分钟" in restarted_custom_text
    with pytest.raises(SnapshotStateError):
        service.edit_draft(custom["snapshot_id"], parent_edit_patch={"coaching_duration_minutes": 31})


def test_parent_edit_patch_preserves_existing_choice_on_old_client_and_replay(tmp_path: Path) -> None:
    database = Database(tmp_path / "coaching-patch.db"); database.initialize()
    service = ParentReportService(database)
    snapshot = service.create_from_daily(_daily_report(database))
    service.edit_draft(snapshot["snapshot_id"], parent_edit_patch={"coaching_decision": "custom", "coaching_script": "先听。", "coaching_action": "只做一处。", "coaching_duration_minutes": 5})
    old_client = service.edit_draft(snapshot["snapshot_id"], parent_edit_patch={"delivery_intro": "今晚一起看"})
    replay = service.edit_draft(snapshot["snapshot_id"], parent_edit_patch={"delivery_intro": "今晚一起看"})
    assert old_client["parent_edit"]["coaching_decision"] == replay["parent_edit"]["coaching_decision"] == "custom"
    assert replay["parent_edit"]["coaching_script"] == "先听。"
    assert replay["parent_edit"]["delivery_intro"] == "今晚一起看"


def test_next_daily_coaching_explains_approved_choice_and_latest_feedback_after_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "coaching-next-day.db"); database.initialize()
    report = _daily_report(database)
    parent_reports = ParentReportService(database)
    snapshot = parent_reports.create_from_daily(report)
    parent_reports.approve(snapshot["snapshot_id"], parent_edit_patch={"coaching_decision": "custom", "coaching_script": "先听再选。", "coaching_action": "只圈一处。", "coaching_duration_minutes": 6})
    _, approved_text, _ = render_message(ParentReportService(Database(database.path)).get(snapshot["snapshot_id"]))
    assert "先听再选。" in approved_text and "约6分钟" in approved_text
    parent_reports.record_feedback(snapshot["snapshot_id"], helpfulness=2, stress_rating=5, notes="今天先停", client_action_id="next-day-feedback-001")
    now = utc_now()
    with database.connect() as connection:
        connection.execute("""INSERT INTO daily_session(session_id, child_id, status, state, started_at, ended_at, last_checkpoint_at, client_action_id)
          VALUES ('next-day-session', 'child-junior-3', 'completed', 'reflection', ?, ?, ?, 'next-day-session-action')""", (now, now, now))
        connection.execute("""INSERT INTO daily_reflection(reflection_id, session_id, child_id, learned_text, difficult_text, feeling, next_action, skipped, created_at, client_action_id)
          VALUES ('next-day-reflection', 'next-day-session', 'child-junior-3', '', '函数图像', '', '圈出第一步', 0, ?, 'next-day-reflection-action')""", (now,))
        connection.commit()
    evidence = StudyRepository(Database(database.path)).report_evidence("next-day-session")
    _, coaching = build_daily_report(evidence)
    assert evidence["coaching_decisions"][0]["decision"] == "custom"
    assert evidence["coaching_feedback"] == []
    assert evidence["report_feedback"][0]["stress_rating"] == 5
    assert "上次家长修改了建议" in coaching["adaptation"]["note"]
    assert "沟通压力偏高" not in coaching["adaptation"]["note"]
    assert coaching["adaptation"]["average_stress"] is None
    assert coaching["personalization"]["previous_decision"] == "custom"

    with database.connect() as connection:
        foreign = connection.execute("SELECT parent_edit_json FROM parent_report_snapshot WHERE child_id = 'child-primary-4'").fetchall()
    assert all(json.loads(row["parent_edit_json"]).get("coaching_decision") != "custom" for row in foreign)


def test_skip_delivery_remains_zero_and_has_no_old_action_after_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "skip-restart.db"); database.initialize()
    snapshots = ParentReportService(database)
    snapshot = snapshots.create_from_daily(_daily_report(database))
    snapshots.approve(snapshot["snapshot_id"], parent_edit_patch={"coaching_decision": "skip", "coaching_observation": "旧观察不得发送"})
    _, text, _ = render_message(ParentReportService(Database(database.path)).get(snapshot["snapshot_id"]))
    assert "家长本次选择不执行额外行动" in text and "约0分钟" in text
    assert "复习判别式第一步" not in text and "旧观察不得发送" not in text and "观察与明天调整" not in text


def test_parent_coaching_review_api_persists_only_the_selected_child_snapshot(tmp_path: Path) -> None:
    database_path = tmp_path / "coaching-review-api.db"
    database = Database(database_path); database.initialize()
    reports = ParentReportService(database)
    report = _daily_report(database)
    primary = reports.create_from_daily(report)
    other = reports.create_from_period({
        "period_report_id": "coaching-other", "child_id": "child-primary-4", "report_type": "weekly", "source": "local_aggregate_v2", "period_end": "2026-08-30",
        "content": {"headline": "另一位孩子的周报", "coverage": {}, "daily_signals": [], "interpretation": "只测试隔离。", "parent_coaching": {"action": None, "script": []}},
    })
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"C" * 32, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        # Startup owns catalog/source revalidation; approve its current stored period, not an invented source ID.
        period = client.app.state.period_reports.generate('child-junior-3', 'weekly')
        unsaved_skip = client.app.state.parent_reports.create_from_period(period)
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        response = client.put(f"/v1/parent/report-snapshots/{primary['snapshot_id']}", headers={"X-HSC-Parent": token}, json={
            "delivery_intro": "今晚先听", "parent_note": "不比较", "coaching_decision": "skip",
            "coaching_script": "", "coaching_action": "", "coaching_observation": "若孩子拒绝，明天不追加。",
        })
        approved = client.post(f"/v1/parent/report-snapshots/{unsaved_skip['snapshot_id']}/approve", headers={"X-HSC-Parent": token}, json={"coaching_decision": "skip"})
    assert response.status_code == 200
    assert approved.status_code == 200 and approved.json()["parent_edit"]["coaching_decision"] == "skip"
    _, approved_text, _ = render_message(ParentReportService(Database(database_path)).get(unsaved_skip["snapshot_id"]))
    assert "家长本次选择不执行额外行动" in approved_text and "约0分钟" in approved_text
    assert response.json()["parent_edit"]["coaching_decision"] == "skip"
    assert ParentReportService(Database(database_path)).get(primary["snapshot_id"])["parent_edit"]["coaching_observation"] == "若孩子拒绝，明天不追加。"
    assert ParentReportService(Database(database_path)).get(other["snapshot_id"])["parent_edit"]["coaching_decision"] == "adopt"


def test_failed_delivery_persists_retry_and_consent_revocation_blocks_send(tmp_path: Path) -> None:
    database = Database(tmp_path / "failure.db")
    database.initialize()
    snapshot = ParentReportService(database).create_from_daily(_daily_report(database))
    service = ReportDeliveryService(database, b"2" * 32)
    service.initialize()
    next_hour = (datetime.now(FAMILY_ZONE).hour + 1) % 24
    after_next_hour = (datetime.now(FAMILY_ZONE).hour + 2) % 24
    service.update_preferences("child-junior-3", f"{next_hour:02d}:00", f"{after_next_hour:02d}:00", 90)
    snapshot = service.snapshots.approve(snapshot["snapshot_id"])
    recipient = service.add_recipient("child-junior-3", "email", "parent@example.test", "爸爸", consented=True)
    service.save_channel_config("email", {"host": "127.0.0.1", "from_address": "report@example.test", "allow_external_delivery": True})
    service.adapters["email"] = RecordingEmail(ChannelDeliveryError("SMTP_CREDENTIALS_INVALID", "bad password", retryable=False))
    queued = service.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    assert service.process_due()[0]["status"] == "failed"
    failed = service.get_delivery(queued["delivery_id"])
    assert failed["last_error_code"] == "SMTP_CREDENTIALS_INVALID"
    assert failed["attempts"][-1]["status"] == "failed"
    # The durable task can be retried after credentials are corrected.
    service.retry(queued["delivery_id"])
    service.adapters["email"] = RecordingEmail()
    assert service.process_due()[0]["status"] == "sent"
    second = service.enqueue(snapshot["snapshot_id"], recipient["recipient_id"], "2099-01-01T00:00:00+00:00")
    service.revoke_recipient(recipient["recipient_id"])
    # Revocation cancels durable queued work immediately rather than waiting for its due time.
    assert service.get_delivery(second["delivery_id"])["status"] == "cancelled"
    with database.connect() as connection:
        audit = connection.execute("SELECT action FROM audit_event WHERE action = 'report_delivery_recipient_revoked'").fetchone()
    assert audit["action"] == "report_delivery_recipient_revoked"


def test_temporary_smtp_interruption_is_durably_retried_after_recovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "network-retry.db")
    database.initialize()
    snapshot = ParentReportService(database).create_from_daily(_daily_report(database))
    service = ReportDeliveryService(database, b"8" * 32)
    service.initialize()
    service.update_preferences("child-junior-3", "12:00", "12:01", 90)
    service.snapshots.approve(snapshot["snapshot_id"])
    recipient = service.add_recipient("child-junior-3", "email", "parent@example.test", "妈妈", consented=True)
    service.save_channel_config("email", {"host": "127.0.0.1", "from_address": "report@example.test", "allow_external_delivery": True})
    queued = service.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    service.adapters["email"] = RecordingEmail(ChannelDeliveryError("SMTP_UNAVAILABLE", "connection interrupted", retryable=True))
    retrying = service.process_due()[0]
    assert retrying["status"] == "scheduled"
    assert retrying["last_error_code"] == "SMTP_UNAVAILABLE"
    assert retrying["attempts"][-1]["status"] == "scheduled"
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET scheduled_at = ? WHERE delivery_id = ?", (utc_now(), queued["delivery_id"]))
        connection.commit()
    service.adapters["email"] = RecordingEmail()
    assert service.process_due()[0]["status"] == "sent"


def test_parent_api_exposes_preview_approval_and_delivery_history(tmp_path: Path) -> None:
    database_path = tmp_path / "api.db"
    database = Database(database_path)
    database.initialize()
    _daily_report(database)
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"3" * 32, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        snapshots = client.get("/v1/parent/children/child-junior-3/report-snapshots", headers=headers)
        assert snapshots.status_code == 200 and snapshots.json()["items"]
        snapshot_id = next(item["snapshot_id"] for item in snapshots.json()["items"] if item["report_type"] == "daily")
        period = client.post("/v1/children/child-junior-3/period-reports/generate?report_type=weekly", headers=headers)
        assert period.status_code == 200 and period.json()["child_id"] == "child-junior-3"
        refreshed_snapshots = client.get("/v1/parent/children/child-junior-3/report-snapshots", headers=headers).json()["items"]
        weekly_snapshot = next(item for item in refreshed_snapshots if item["report_type"] == "weekly")
        assert weekly_snapshot["child_id"] == "child-junior-3" and weekly_snapshot["status"] == "draft"
        other_child = client.get("/v1/parent/children/child-primary-4/report-snapshots", headers=headers).json()["items"]
        assert not any(item["report_type"] == "weekly" for item in other_child)
        # Refreshing an unchanged calendar period must reuse its frozen approval draft:
        # it must neither create a second copy nor revoke an approved same-version report.
        assert client.post(f"/v1/parent/report-snapshots/{weekly_snapshot['snapshot_id']}/approve", headers=headers).status_code == 200
        repeated_period = client.post("/v1/children/child-junior-3/period-reports/generate?report_type=weekly", headers=headers)
        assert repeated_period.status_code == 200
        repeated_snapshots = client.get("/v1/parent/children/child-junior-3/report-snapshots", headers=headers).json()["items"]
        same_week = [item for item in repeated_snapshots if item["report_type"] == "weekly"]
        assert len(same_week) == 1
        assert same_week[0]["snapshot_id"] == weekly_snapshot["snapshot_id"] and same_week[0]["status"] == "approved"
        monthly_period = client.post("/v1/children/child-junior-3/period-reports/generate?report_type=monthly", headers=headers)
        assert monthly_period.status_code == 200
        monthly_snapshots = client.get("/v1/parent/children/child-junior-3/report-snapshots", headers=headers).json()["items"]
        monthly_snapshot = next(item for item in monthly_snapshots if item["report_type"] == "monthly")
        assert monthly_snapshot["status"] == "draft"
        assert client.post(f"/v1/parent/report-snapshots/{monthly_snapshot['snapshot_id']}/approve", headers=headers).status_code == 200
        assert client.post("/v1/children/child-junior-3/period-reports/generate?report_type=monthly", headers=headers).status_code == 200
        latest_monthly = [item for item in client.get("/v1/parent/children/child-junior-3/report-snapshots", headers=headers).json()["items"] if item["report_type"] == "monthly"]
        assert len(latest_monthly) == 1 and latest_monthly[0]["snapshot_id"] == monthly_snapshot["snapshot_id"] and latest_monthly[0]["status"] == "approved"
        other_child = client.get("/v1/parent/children/child-primary-4/report-snapshots", headers=headers).json()["items"]
        assert not any(item["report_type"] in {"weekly", "monthly"} for item in other_child)
        assert client.put(f"/v1/parent/report-snapshots/{snapshot_id}", headers=headers, json={"delivery_intro": "今晚一起看", "parent_note": "不比较"}).status_code == 200


        assert client.post(f"/v1/parent/report-snapshots/{snapshot_id}/approve", headers=headers).status_code == 200
        feedback = client.post(f"/v1/parent/report-snapshots/{snapshot_id}/feedback", headers=headers, json={"helpfulness": 4, "stress_rating": 2, "notes": "先听孩子说完。", "client_action_id": "api-preview-feedback-001"})
        assert feedback.status_code == 200 and feedback.json()["feedback"][0]["helpfulness"] == 4
        recipient = client.post("/v1/parent/report-delivery/recipients", headers=headers, json={"child_id": "child-junior-3", "channel": "email", "address": "parent@example.test", "label": "妈妈", "consented": True})
        queued = client.post(f"/v1/parent/report-snapshots/{snapshot_id}/deliveries", headers=headers, json={"recipient_id": recipient.json()["recipient_id"]})
        assert queued.status_code == 200 and queued.json()["status"] == "scheduled"
        history = client.get(f"/v1/parent/report-snapshots/{snapshot_id}/deliveries", headers=headers)
        assert history.status_code == 200 and history.json()["items"][0]["delivery_id"] == queued.json()["delivery_id"]
        correction = client.post(f"/v1/parent/report-snapshots/{snapshot_id}/corrections", headers=headers, json={"note": "计划完成记录需要先核对。"})
        assert correction.status_code == 200 and correction.json()["status"] == "revoked"
        assert correction.json()["corrections"][0]["note"] == "计划完成记录需要先核对。"
        assert client.get(f"/v1/parent/report-snapshots/{snapshot_id}/deliveries", headers=headers).json()["items"][0]["status"] == "cancelled"


def test_parent_http_journey_uses_local_smtp_sink_with_explicit_retry_and_restart(tmp_path: Path) -> None:
    """Parent-facing HTTP flow: evidence draft → approval → local socket → retry → restart.

    Everything in this test is synthetic and loopback-only.  The first failure is
    a deterministic adapter refusal so the retry UI/API path can be exercised;
    the successful retry uses the real SMTP adapter and an actual local TCP sink.
    """
    database_path = tmp_path / "parent-http-local-sink.db"
    database = Database(database_path)
    database.initialize()
    _daily_report(database)
    sink = LocalMailSink()

    def serve() -> None:
        try:
            asyncore.loop(timeout=0.01)
        except OSError:
            pass

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"J" * 32, maintenance_enabled=False)
    application = create_app(settings)
    try:
        with TestClient(application) as client:
            token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
            headers = {"X-HSC-Parent": token}

            # Selecting a child exposes its actual daily/weekly/monthly drafts,
            # while the other child never inherits this child's period reports.
            daily = client.get("/v1/parent/children/child-junior-3/report-snapshots", headers=headers)
            assert daily.status_code == 200
            daily_snapshot = next(item for item in daily.json()["items"] if item["report_type"] == "daily")
            assert daily_snapshot["facts"]["evidence_manifest"] and daily_snapshot["confidence"]["uncertainty"]
            for report_type in ("weekly", "monthly"):
                assert client.post(f"/v1/children/child-junior-3/period-reports/generate?report_type={report_type}", headers=headers).status_code == 200
            junior_types = {item["report_type"] for item in client.get("/v1/parent/children/child-junior-3/report-snapshots", headers=headers).json()["items"]}
            primary_types = {item["report_type"] for item in client.get("/v1/parent/children/child-primary-4/report-snapshots", headers=headers).json()["items"]}
            assert {"daily", "weekly", "monthly"} <= junior_types
            assert "weekly" not in primary_types and "monthly" not in primary_types

            # A loopback sink is the only intentionally plaintext SMTP target.
            config = {
                "host": "127.0.0.1", "port": sink.socket.getsockname()[1],
                "from_address": "report@local.test", "starttls": False,
                "allow_external_delivery": True,
            }
            configured = client.put("/v1/parent/report-delivery/channels/email", headers=headers, json={"config": config})
            assert configured.status_code == 200
            quiet_start = (datetime.now(FAMILY_ZONE).hour + 1) % 24
            quiet_end = (datetime.now(FAMILY_ZONE).hour + 2) % 24
            assert client.put("/v1/parent/children/child-junior-3/report-delivery/preferences", headers=headers, json={
                "quiet_start": f"{quiet_start:02d}:00", "quiet_end": f"{quiet_end:02d}:00", "retention_days": 90, "redaction_level": "standard",
            }).status_code == 200
            with database.connect() as connection:
                encrypted = connection.execute("SELECT encrypted_json FROM notification_credential WHERE channel = 'email'").fetchone()["encrypted_json"]
            assert "127.0.0.1" not in encrypted and "report@local.test" not in encrypted

            recipient = client.post("/v1/parent/report-delivery/recipients", headers=headers, json={
                "child_id": "child-junior-3", "channel": "email", "address": "parent@local.test", "label": "本机验收家长", "consented": True,
            })
            assert recipient.status_code == 200
            # Explicit approval remains a hard gate before the report can queue.
            assert client.post(f"/v1/parent/report-snapshots/{daily_snapshot['snapshot_id']}/approve", headers=headers).status_code == 200
            queued = client.post(f"/v1/parent/report-snapshots/{daily_snapshot['snapshot_id']}/deliveries", headers=headers, json={"recipient_id": recipient.json()["recipient_id"]})
            assert queued.status_code == 200 and queued.json()["status"] == "scheduled"
            delivery_id = queued.json()["delivery_id"]

            # A parent sees a durable failure, then must explicitly choose retry.
            application.state.report_delivery.adapters["email"] = RecordingEmail(ChannelDeliveryError("SMTP_CREDENTIALS_INVALID", "synthetic local refusal", retryable=False))
            processed = client.post("/v1/parent/report-deliveries/process", headers=headers)
            assert processed.status_code == 200 and processed.json()["items"][0]["status"] == "failed"
            failed = client.get(f"/v1/parent/report-snapshots/{daily_snapshot['snapshot_id']}/deliveries", headers=headers).json()["items"]
            assert failed[0]["delivery_id"] == delivery_id and failed[0]["last_error_code"] == "SMTP_CREDENTIALS_INVALID"
            summary = client.get("/v1/parent/children/child-junior-3/report-delivery/status", headers=headers).json()
            assert summary["failed"] == 1

            application.state.report_delivery.adapters["email"] = SmtpChannel()
            retried = client.post(f"/v1/parent/report-deliveries/{delivery_id}/retry", headers=headers)
            assert retried.status_code == 200 and retried.json()["status"] == "scheduled"
            assert client.post("/v1/parent/report-deliveries/process", headers=headers).json()["items"][0]["status"] == "sent"
            for _ in range(50):
                if sink.messages:
                    break
                threading.Event().wait(0.01)
            assert len(sink.messages) == 1
            message = BytesParser(policy=policy.default).parsebytes(sink.messages[0][2])
            assert message.get_body(("plain",)).get_content()
            assert message.get_body(("html",)).get_content()
            assert any(part.get_filename() == "学习成长报告.html" for part in message.iter_attachments())
            # Audit is diagnostic but deliberately excludes report text and recipient address.
            with database.connect() as connection:
                audit = [row["summary_json"] for row in connection.execute("SELECT summary_json FROM audit_event WHERE action IN ('report_delivery_sent', 'report_delivery_failed')").fetchall()]
            assert audit and all("parent@local.test" not in item and "计划20分钟" not in item for item in audit)

        # Restarting the local service reloads durable outbox state without sending
        # the already sink-accepted report a second time.
        with TestClient(create_app(settings)) as restarted_client:
            restarted_token = restarted_client.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
            restarted_headers = {"X-HSC-Parent": restarted_token}
            history = restarted_client.get(f"/v1/parent/report-snapshots/{daily_snapshot['snapshot_id']}/deliveries", headers=restarted_headers)
            assert history.status_code == 200 and history.json()["items"][0]["status"] == "sent"
            assert restarted_client.post("/v1/parent/report-deliveries/process", headers=restarted_headers).json()["items"] == []
            assert len(sink.messages) == 1
    finally:
        sink.close()
        worker.join(timeout=1)


def test_unknown_delivery_api_requires_parent_and_replays_explicit_reissue(tmp_path: Path) -> None:
    database_path = tmp_path / "unknown-delivery-api.db"; database = Database(database_path); database.initialize()
    service = ReportDeliveryService(database, b"Z" * 32); service.initialize()
    snapshot = service.snapshots.approve(service.snapshots.create_from_daily(_daily_report(database))["snapshot_id"])
    recipient = service.add_recipient("child-junior-3", "email", "api-unknown@local.test", "监护人", True)
    unknown = service.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET status = 'processing' WHERE delivery_id = ?", (unknown["delivery_id"],)); connection.commit()
    service.initialize()
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"Z" * 32, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        assert client.post(f"/v1/parent/report-deliveries/{unknown['delivery_id']}/unknown/keep").status_code == 401
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        assert client.post(f"/v1/parent/report-deliveries/{unknown['delivery_id']}/unknown/keep", headers=headers).json()["status"] == "unknown_acknowledged"
        payload = {"client_action_id": "unknown-delivery-api-reissue-001"}
        first = client.post(f"/v1/parent/report-deliveries/{unknown['delivery_id']}/unknown/reissue", headers=headers, json=payload)
        replay = client.post(f"/v1/parent/report-deliveries/{unknown['delivery_id']}/unknown/reissue", headers=headers, json=payload)
        assert first.status_code == 200 and replay.status_code == 200 and replay.json()["delivery_id"] == first.json()["delivery_id"]
        assert client.post(f"/v1/parent/report-deliveries/{unknown['delivery_id']}/unknown/reissue", headers=headers, json={"client_action_id": "unknown-delivery-api-reissue-002"}).status_code == 409


def test_app_provider_contract_is_authoritative_and_child_isolated(tmp_path: Path) -> None:
    database_path = tmp_path / "provider-contract.db"; database = Database(database_path); database.initialize(); _daily_report(database)
    def provider(*, child_id, **_kwargs):
        response = "accepted" if child_id == "child-junior-3" else "declined"
        return {"behavior_context_authoritative": True, "behavior_intervention_feedback": [{"kind": f"intervention_{response}", "summary": f"{child_id}-{response}", "evidence_ref": f"safe-{child_id}", "source": "行为模型", "occurred_at": utc_now()}], "behavior_model_summary": {}}
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"B" * 32, maintenance_enabled=False)
    with TestClient(create_app(settings, behavior_context_provider=provider)) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]; headers = {"X-HSC-Parent": token}
        junior = client.post("/v1/children/child-junior-3/period-reports/generate?report_type=weekly", headers=headers)
        primary = client.post("/v1/children/child-primary-4/period-reports/generate?report_type=weekly", headers=headers)
    assert junior.status_code == primary.status_code == 200
    assert "child-junior-3-accepted" in json.dumps(junior.json(), ensure_ascii=False)
    assert "child-primary-4-declined" not in json.dumps(junior.json(), ensure_ascii=False)
    assert "child-primary-4-declined" in json.dumps(primary.json(), ensure_ascii=False)


def test_app_provider_error_and_audit_error_fail_open_without_legacy(tmp_path: Path) -> None:
    database_path = tmp_path / "provider-error.db"; database = Database(database_path); database.initialize(); _daily_report(database)
    def raising_provider(**_kwargs): raise RuntimeError("provider offline")
    def failing_audit(_child_id): raise RuntimeError("database locked")
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"E" * 32, maintenance_enabled=False)
    with TestClient(create_app(settings, behavior_context_provider=raising_provider, behavior_context_audit=failing_audit)) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]; headers = {"X-HSC-Parent": token}
        response = client.post("/v1/children/child-junior-3/period-reports/generate?report_type=weekly", headers=headers)
    assert response.status_code == 200
    rendered = json.dumps(response.json(), ensure_ascii=False)
    assert "本次行为上下文不可用，不作推断" in rendered and "稍后" not in rendered


def test_low_spec_worker_recovers_scheduled_outbox_and_bounds_parallel_sends(tmp_path: Path) -> None:
    """No GPU/model is loaded: this is the Windows-family-PC scheduling path."""
    database = Database(tmp_path / "restart.db")
    database.initialize()
    snapshot = ParentReportService(database).create_from_daily(_daily_report(database))
    worker = ReportDeliveryService(database, b"4" * 32)
    worker.initialize()
    start = (datetime.now(FAMILY_ZONE).hour + 1) % 24
    end = (datetime.now(FAMILY_ZONE).hour + 2) % 24
    worker.update_preferences("child-junior-3", f"{start:02d}:00", f"{end:02d}:00", 90)
    worker.snapshots.approve(snapshot["snapshot_id"])
    worker.save_channel_config("email", {"host": "127.0.0.1", "from_address": "report@example.test", "allow_external_delivery": True})
    first = worker.add_recipient("child-junior-3", "email", "first@example.test", "妈妈", True)
    second = worker.add_recipient("child-junior-3", "email", "second@example.test", "爸爸", True)
    worker.enqueue(snapshot["snapshot_id"], first["recipient_id"])
    worker.enqueue(snapshot["snapshot_id"], second["recipient_id"])
    mail_sink = SlowRecordingEmail()
    worker.adapters["email"] = mail_sink
    assert {item["status"] for item in worker.process_due(max_workers=2)} == {"sent"}
    # Two dispatch workers must finish without holding the guard in the parent;
    # actual sends serialize with database restore through the durable receipt.
    assert mail_sink.max_active == 1

    # Simulate a Windows app restart while a later task is durable but not sent.
    third = worker.add_recipient("child-junior-3", "email", "third@example.test", "监护人", True)
    deferred = worker.enqueue(snapshot["snapshot_id"], third["recipient_id"], "2099-01-01T00:00:00+00:00")
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET scheduled_at = ? WHERE delivery_id = ?", (utc_now(), deferred["delivery_id"]))
        connection.commit()
    restarted_worker = ReportDeliveryService(database, b"4" * 32)
    restarted_worker.initialize()
    restarted_worker.adapters["email"] = RecordingEmail()
    assert restarted_worker.process_due()[0]["status"] == "sent"


def test_restart_marks_claimed_outbox_unknown_and_never_auto_retries(tmp_path: Path) -> None:
    database = Database(tmp_path / "claimed-restart.db")
    database.initialize()
    snapshot = ParentReportService(database).create_from_daily(_daily_report(database))
    worker = ReportDeliveryService(database, b"8" * 32)
    worker.initialize()
    worker.update_preferences("child-junior-3", "12:00", "12:01", 90)
    worker.snapshots.approve(snapshot["snapshot_id"])
    recipient = worker.add_recipient("child-junior-3", "email", "restart@example.test", "监护人", True)
    queued = worker.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET status = 'processing' WHERE delivery_id = ?", (queued["delivery_id"],))
        connection.commit()
    recovered = ReportDeliveryService(database, b"8" * 32)
    recovered.initialize()
    item = recovered.get_delivery(queued["delivery_id"])
    assert item["status"] == "unknown"
    assert item["last_error_code"] == "UNKNOWN_AFTER_RESTART"
    recovered.adapters["email"] = RecordingEmail()
    assert recovered.process_due() == []
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action = 'report_delivery_unknown_after_restart'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_attempt WHERE delivery_id = ? AND status = 'unknown'", (queued["delivery_id"],)).fetchone()[0] == 1


def test_smtp_accept_then_crash_stays_unknown_until_one_explicit_reissue(tmp_path: Path, normal_provider_owner) -> None:
    """A real local socket may accept before SQLite can record `sent`; never auto-send it again."""
    database = Database(tmp_path / "accepted-then-crash.db"); database.initialize()
    report = _daily_report(database)
    service = ReportDeliveryService(database, b"U" * 32); service.initialize()
    service.update_preferences("child-junior-3", "12:00", "12:01", 90)
    snapshot = service.snapshots.approve(service.snapshots.create_from_daily(report)["snapshot_id"])
    recipient = service.add_recipient("child-junior-3", "email", "crash@local.test", "本机验收收件箱", True)
    sink = LocalMailSink()

    def serve() -> None:
        try: asyncore.loop(timeout=0.01)
        except OSError: pass

    thread = threading.Thread(target=serve, daemon=True); thread.start()
    try:
        config = {"host": "127.0.0.1", "port": sink.socket.getsockname()[1], "from_address": "report@local.test", "starttls": False, "allow_external_delivery": True}
        service.save_channel_config("email", config)
        queued = service.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])

        class CrashAfterAccepted:
            channel = "email"
            def send(self, *args, **kwargs):
                SmtpChannel().send(*args, **kwargs)
                raise SystemExit("simulated loss after SMTP accepted message")

        service.adapters["email"] = CrashAfterAccepted()
        with pytest.raises(SystemExit, match="SMTP accepted"):
            service.process_due()
        for _ in range(50):
            if sink.messages: break
            threading.Event().wait(0.01)
        assert len(sink.messages) == 1
        assert service.get_delivery(queued["delivery_id"])["status"] == "processing"

        restarted = ReportDeliveryService(database, b"U" * 32); restarted.initialize(); restarted.adapters["email"] = SmtpChannel()
        unknown = restarted.get_delivery(queued["delivery_id"])
        assert unknown["status"] == "unknown" and restarted.process_due() == [] and len(sink.messages) == 1
        assert restarted.keep_unknown(queued["delivery_id"])["status"] == "unknown_acknowledged"
        # Replaying a no-resend acknowledgement is audit-idempotent.
        assert restarted.keep_unknown(queued["delivery_id"])["status"] == "unknown_acknowledged"
        replacement = restarted.reissue_unknown(queued["delivery_id"], "accepted-crash-reissue-001")
        assert replacement["status"] == "scheduled" and replacement["delivery_id"] != queued["delivery_id"]
        assert restarted.reissue_unknown(queued["delivery_id"], "accepted-crash-reissue-001")["delivery_id"] == replacement["delivery_id"]
        assert restarted.process_due()[0]["status"] == "sent"
        for _ in range(50):
            if len(sink.messages) == 2: break
            threading.Event().wait(0.01)
        assert len(sink.messages) == 2
        with database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action = 'report_delivery_unknown_kept_without_resend'").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action = 'report_delivery_unknown_explicit_reissue'").fetchone()[0] == 1
    finally:
        sink.close(); thread.join(timeout=1)


def test_unknown_reissue_is_transactional_and_respects_revoked_recipient(tmp_path: Path) -> None:
    database = Database(tmp_path / "unknown-reissue-atomic.db"); database.initialize()
    service = ReportDeliveryService(database, b"V" * 32); service.initialize()
    snapshot = service.snapshots.approve(service.snapshots.create_from_daily(_daily_report(database))["snapshot_id"])
    recipient = service.add_recipient("child-junior-3", "email", "atomic@local.test", "监护人", True)
    unknown = service.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET status = 'processing' WHERE delivery_id = ?", (unknown["delivery_id"],)); connection.commit()
    service.initialize()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.reissue_unknown, unknown["delivery_id"], "atomic-reissue-action-001")
        second = pool.submit(service.reissue_unknown, unknown["delivery_id"], "atomic-reissue-action-002")
        outcomes = [future.exception() for future in (first, second)]
    assert sum(error is None for error in outcomes) == 1
    assert any(isinstance(error, DeliveryStateError) for error in outcomes if error is not None)
    assert len(service.list_deliveries(snapshot["snapshot_id"])) == 2
    replacement = next(item for item in service.list_deliveries(snapshot["snapshot_id"]) if item["delivery_id"] != unknown["delivery_id"])
    # A replacement can suffer the same post-submit crash. It becomes a fresh
    # unknown result and may form one more explicit, auditable generation.
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET status = 'processing' WHERE delivery_id = ?", (replacement["delivery_id"],)); connection.commit()
    service.initialize()
    assert service.get_delivery(replacement["delivery_id"])["status"] == "unknown"
    grandchild = service.reissue_unknown(replacement["delivery_id"], "atomic-reissue-action-003")
    chain = service.list_deliveries(snapshot["snapshot_id"])
    assert grandchild["status"] == "scheduled" and len(chain) == 3
    assert service.get_delivery(unknown["delivery_id"])["status"] == "superseded"
    with pytest.raises(DeliveryStateError):
        service.reissue_unknown(unknown["delivery_id"], "atomic-reissue-action-004")

    primary = service.snapshots.approve(service.snapshots.create_from_period(
        PeriodReportService(database).generate('child-primary-4', 'weekly'))['snapshot_id'])
    primary_recipient = service.add_recipient("child-primary-4", "email", "primary@local.test", "另一位监护人", True)
    primary_unknown = service.enqueue(primary["snapshot_id"], primary_recipient["recipient_id"])
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET status = 'processing' WHERE delivery_id = ?", (primary_unknown["delivery_id"],)); connection.commit()
    service.initialize()
    assert service.child_status_summary("child-primary-4")["unknown"] == 1
    assert service.child_status_summary("child-junior-3")["unknown"] == 0
    service.snapshots.revoke(primary["snapshot_id"], "家长撤销旧报告")
    with pytest.raises(DeliveryStateError, match="后续证据更新"):
        service.reissue_unknown(primary_unknown["delivery_id"], "revoked-report-reissue-001")
    with pytest.raises(DeliveryStateError, match="不能跨报告复用"):
        service.reissue_unknown(primary_unknown["delivery_id"], "atomic-reissue-action-003")

    blocked = service.enqueue(snapshot["snapshot_id"], recipient["recipient_id"], "2099-01-01T00:00:00+00:00")
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET status = 'processing' WHERE delivery_id = ?", (blocked["delivery_id"],)); connection.commit()
    service.initialize(); service.revoke_recipient(recipient["recipient_id"])
    with pytest.raises(DeliveryStateError, match="授权已变更"):
        service.reissue_unknown(blocked["delivery_id"], "revoked-recipient-reissue-001")


def test_quiet_end_rolls_over_midnight() -> None:
    now = datetime(2026, 8, 28, 23, 55, tzinfo=FAMILY_ZONE)
    assert _next_quiet_end("07:00", now) == "2026-08-28T23:00:00+00:00"


def test_minimal_disclosure_removes_evidence_details_from_external_message(tmp_path: Path) -> None:
    database = Database(tmp_path / "redaction.db")
    database.initialize()
    report = _daily_report(database)
    snapshot = ParentReportService(database).create_from_daily(report)
    service = ReportDeliveryService(database, b"6" * 32)
    service.initialize()
    saved = service.update_preferences("child-junior-3", "12:00", "12:01", 90, "minimal")
    assert saved["redaction_level"] == "minimal"
    _, minimal_text, minimal_html = render_message(snapshot, "minimal")
    assert "计划20分钟" not in minimal_text
    assert "先说出已经确定的一步" not in minimal_text
    assert "本机家长中心" in minimal_text
    assert "本机家长中心" in minimal_html
    assert "计划20分钟" not in minimal_html


def test_period_snapshot_delivery_includes_its_recorded_days_and_next_step(tmp_path: Path) -> None:
    database = Database(tmp_path / "period-delivery.db")
    database.initialize()
    StudyRepository(database).seed_family()
    snapshot = ParentReportService(database).create_from_period({
        "period_report_id": "period-week-1", "child_id": "child-junior-3", "report_type": "weekly",
        "period_end": "2026-08-30", "source": "local_aggregate_v2",
        "content": {
            "headline": "3天有学习记录", "coverage": {"recorded_days": 3, "recorded_sessions": 4, "evidence_count": 9, "sufficient_for_trend": True},
            "daily_signals": [{"date": "2026-08-28", "headline": "完成订正", "session_count": 1}],
            "interpretation": "只根据本期记录汇总。",
            "next_action": {"what": "复测判别式第一步", "duration_minutes": 10, "steps": ["先复述条件"], "stop_condition": "10分钟后停止"},
            "parent_script": ["我看到你完成了订正，我们只看下一小步。"],
        },
    })
    # Rendering retained wording is not source qualification. With no actual
    # period source, the copy remains readable but is revoked rather than a draft.
    assert snapshot['status'] == 'revoked'
    assert snapshot['correction_status']['source_current'] is False
    with pytest.raises(SnapshotStateError):
        ParentReportService(database).approve(snapshot["snapshot_id"])
    _, text, html = render_message(ParentReportService(database).get(snapshot["snapshot_id"]))
    assert "本期已记录3天、4次学习会话，共9条证据。" in text
    assert "2026-08-28：完成订正" in text
    assert "复测判别式第一步" in text
    assert "我看到你完成了订正" in html


def test_child_delivery_summary_is_isolated_and_parent_api_requires_auth(tmp_path: Path) -> None:
    database = Database(tmp_path / "summary-isolation.db"); database.initialize(); report = _daily_report(database)
    service = ReportDeliveryService(database, b"S" * 32); service.initialize(); snapshot = service.snapshots.approve(service.snapshots.create_from_daily(report)["snapshot_id"])
    recipient = service.add_recipient("child-junior-3", "email", "summary@example.test", "家长", True)
    service.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    assert service.child_status_summary("child-junior-3")["scheduled"] == 1
    assert service.child_status_summary("child-primary-4")["scheduled"] == 0
    settings = Settings(data_dir=tmp_path, database_path=database.path, data_encryption_key=b"S" * 32, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/parent/children/child-junior-3/report-delivery/status").status_code == 401
        assert client.get("/v1/parent/children/child-junior-3/report-delivery/channel-tests").status_code == 401
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        visible = client.get("/v1/parent/children/child-junior-3/report-delivery/channel-tests", headers=headers)
        assert visible.status_code == 200 and {item["channel"] for item in visible.json()["items"]} == {"email", "wechat_work_bot"}


def test_report_facts_distinguish_session_knowledge_evidence_and_open_mistakes() -> None:
    summary, _ = build_daily_report({
        "session": {"recall_declared_empty": False, "homework_declared_empty": False},
        "child": {"grade": "初三"}, "reflection": {"skipped": False, "learned_text": "", "difficult_text": "", "next_action": "", "feeling": ""},
        "tasks": [], "recalls": [], "planning_context": {"energy_level": "medium", "fixed_commitment_minutes": 0}, "coaching_feedback": [],
        "knowledge_evidence": [{"knowledge_title": "根的判别式", "independent": True,
                                "score_status": "verified_local", "hint_level": 0}],
        "mistakes": [{"title": "判别式练习", "status": "remediating"}],
    })
    assert any("1条已核验作答" in item and "根的判别式" in item and "不等同于已经掌握" in item for item in summary["facts"])
    assert not any("知识掌握证据" in item for item in summary["facts"])
    assert any("需继续处理的错题" in item and "判别式练习" in item for item in summary["facts"])
    assert summary["evidence_count"] == 3


def test_daily_report_states_program_progress_without_double_counting_plan_evidence() -> None:
    summary, coaching = build_daily_report({
        "session": {"recall_declared_empty": False, "homework_declared_empty": False},
        "child": {"grade": "四年级"},
        "reflection": {"skipped": True, "learned_text": "", "difficult_text": "", "next_action": "", "feeling": ""},
        "tasks": [{"title": "PET阅读", "subject": "英语", "status": "completed", "expected_minutes": 10, "actual_minutes": 10, "completion_evidence_type": "child_checked"}],
        "recalls": [], "knowledge_evidence": [], "mistakes": [], "coaching_feedback": [],
        "planning_context": {"energy_level": "medium", "fixed_commitment_minutes": 0},
        "goal_context": "四年级阶段目标：PET能力成长本周50分钟/5次，已记录20分钟/2次",
        "goal_cycles": [{"title": "PET能力成长", "target_minutes": 50, "target_sessions": 5, "completed_minutes": 20, "completed_sessions": 2}],
    })
    assert any("PET能力成长" in fact and "20/50分钟" in fact for fact in summary["facts"])
    assert summary["evidence_count"] == 1
    assert "PET能力成长" in coaching["why_this_fits"]


def test_report_keeps_self_reported_state_as_limited_evidence_not_a_label() -> None:
    summary, coaching = build_daily_report({
        "session": {"recall_declared_empty": False, "homework_declared_empty": False},
        "child": {"grade": "四年级"},
        "reflection": {"skipped": False, "learned_text": "", "difficult_text": "应用题数量关系", "feeling": "有点烦", "next_action": "圈出条件"},
        "tasks": [], "recalls": [], "knowledge_evidence": [], "mistakes": [], "coaching_feedback": [],
        "planning_context": {"energy_level": "low", "fixed_commitment_minutes": 30},
    })
    assert "孩子开始前自评精力：有点累；今晚固定安排预留30分钟" in summary["facts"]
    assert "孩子自述困难：应用题数量关系" in summary["facts"]
    assert "孩子自述感受：有点烦" in coaching["evidence_basis"]
    assert "不扩大成能力或性格结论" in coaching["why_this_fits"]


def test_daily_report_excludes_retracted_behavior_hypothesis_from_learning_conclusion() -> None:
    summary, _ = build_daily_report({
        "session": {"recall_declared_empty": False, "homework_declared_empty": False},
        "child": {"grade": "四年级"},
        "reflection": {"skipped": False, "learned_text": "", "difficult_text": "", "next_action": "", "feeling": ""},
        "tasks": [], "recalls": [], "knowledge_evidence": [], "mistakes": [], "coaching_feedback": [],
        "behavior_model_summary": {
            "event_count": 3, "active_hypotheses": [], "retracted_hypothesis_count": 1,
            "withdrawn_intervention_count": 1,
        },
    })

    assert any("已不纳入提醒、基线或学习结论" in fact for fact in summary["facts"])
    assert not any("卡住" in fact or "走神" in fact for fact in summary["facts"])


def test_repository_binds_report_knowledge_and_mistake_facts_to_the_same_session(period_exact_receipts) -> None:
    from home_study_agent.mistake_book import MistakeBookService

    database, repository, learning, clock, session_id = period_exact_receipts
    child_id = "child-primary-4"
    source = learning.submit_diagnostic(session_id, "diag-p4-arithmetic-closed-1", "12", "evidence-knowledge-action")
    assert source["score_status"] == "verified_local" and source["correctness"] == 0
    mistakes = MistakeBookService(database)
    case = mistakes.capture_from_attempt(source)
    assert case is not None
    mistakes.confirm(case["mistake_id"], child_id, "confirm", "evidence-confirm")
    mistakes.diagnose(case["mistake_id"], child_id, "concept_gap", "需要核对运算顺序", .9, "evidence-diagnose")
    now = utc_now()
    with database.connect() as connection:
        connection.execute("""INSERT INTO intervention_record(intervention_id, child_id, session_id, plan_item_id, trigger_state, confidence, stage, message_text, policy_version, child_response, status, created_at, responded_at)
          VALUES ('evidence-intervention', ?, ?, NULL, 'blocked_candidate', .8, 'prompted', '要不要先圈出一处？', 'rules-v1', 'later', 'completed', ?, ?)""", (child_id, session_id, now, now))
        connection.commit()
    repository.transition(session_id, "study", "reflection", "记录作答", "evidence-reflection-stage")
    repository.complete_with_reflection(session_id, "", "运算顺序", "还可以", "核对括号", False, "evidence-reflection")

    clock[0] += timedelta(minutes=1)
    other_session = repository.start_session(child_id, "evidence-other-session")["session_id"]
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other_session,))
        connection.commit()
    other_source = learning.submit_diagnostic(other_session, "diag-p4-arithmetic-closed-2", "12", "evidence-other-answer")
    assert other_source["score_status"] == "verified_local"
    other_case = mistakes.capture_from_attempt(other_source)
    assert other_case is not None and other_case["mistake_id"] != case["mistake_id"]
    repository.transition(other_session, "study", "reflection", "记录作答", "evidence-other-reflection-stage")
    repository.complete_with_reflection(other_session, "", "", "还可以", "", True, "evidence-other-reflection")

    repository = StudyRepository(Database(database.path))
    evidence = repository.report_evidence(session_id)
    assert [row['evidence_id'] for row in evidence['knowledge_evidence']] == [source['evidence_id']]
    assert evidence['knowledge_evidence'][0]['knowledge_title'] == '整数四则运算顺序'
    assert evidence['knowledge_evidence'][0]['score_status'] == 'verified_local'
    assert evidence['knowledge_evidence'][0]['mastery_claim_allowed'] is False
    assert 'mastery_probability' not in evidence['knowledge_evidence'][0]
    assert [row['source_evidence_id'] for row in evidence['mistakes']] == [source['evidence_id']]
    assert evidence['mistakes'][0]['status'] == 'remediating'
    other_evidence = repository.report_evidence(other_session)
    assert [row['evidence_id'] for row in other_evidence['knowledge_evidence']] == [other_source['evidence_id']]
    assert [row['source_evidence_id'] for row in other_evidence['mistakes']] == [other_source['evidence_id']]
    assert evidence['legacy_behavior_intervention_feedback'][0]['kind'] == 'intervention_declined'
    assert evidence['legacy_behavior_intervention_feedback'][0]['legacy_source'] is True
    safe_ref = coaching_evidence_ref(child_id, 'intervention', 'behavior-model-intervention')
    report_safe = repository.report_evidence(session_id, {
        'behavior_intervention_feedback': [{
            'kind': 'intervention_accepted', 'summary': '孩子接受了一次温和提醒',
            'evidence_ref': safe_ref, 'source': '行为模型的孩子确认反馈', 'occurred_at': now,
        }],
        'behavior_model_summary': {'uncertainty': '只记录孩子明确反馈，不判断情绪或动机。'},
    })
    _, coaching = build_daily_report(report_safe)
    assert coaching['evidence_refs'].count(safe_ref) == 1
    assert coaching['evidence_sources'][1]['source'] == '行为模型的孩子确认反馈'
    assert '稍后' not in ' '.join(coaching['evidence_basis'])
    assert '只记录孩子明确反馈' in coaching['uncertainty']
    authoritative_empty = repository.report_evidence(session_id, {
        'behavior_context_authoritative': True, 'behavior_intervention_feedback': [],
        'behavior_model_summary': {'uncertainty': '本次行为上下文不可用，不作推断。'},
    })
    _, empty_coaching = build_daily_report(authoritative_empty)
    assert '稍后' not in ' '.join(empty_coaching['evidence_basis'])
    assert '本次行为上下文不可用' in empty_coaching['uncertainty']


@pytest.fixture
def period_exact_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = [datetime(2026, 8, 20, 9, tzinfo=timezone.utc)]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].astimezone().replace(tzinfo=None)

    for name in ("repository", "learning_state", "mistake_book", "learning_obligations",
                 "learning_feedback", "local_supply", "specialized_supply"):
        monkeypatch.setattr(import_module(f"home_study_agent.{name}"), "datetime", FrozenDateTime)
    database = Database(tmp_path / "period-exact.db")
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    learning = LearningStateService(database)
    learning.seed_catalog()
    session_id = repository.start_session("child-primary-4", "period-exact-session")["session_id"]
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session_id,))
        connection.commit()
    clock[0] = datetime(2026, 8, 27, 9, tzinfo=timezone.utc)
    return database, repository, learning, clock, session_id


def test_period_report_uses_only_child_period_unretracted_evidence_and_freezes_successor_draft(period_exact_receipts) -> None:
    from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService

    database, repository, learning, clock, session_id = period_exact_receipts
    parent_reports = ParentReportService(database)
    parent_reports.initialize()
    reports = PeriodReportService(database)
    reports.initialize()
    clock[0] = datetime(2026, 8, 20, 9, tzinfo=timezone.utc)
    before = learning.submit_diagnostic(session_id, "diag-p4-arithmetic-closed-1", "90", "period-before")
    assert before["score_status"] == "verified_local"
    repository.transition(session_id, "study", "reflection", "结束当天学习", "period-before-reflection")
    repository.complete_with_reflection(session_id, "", "", "okay", "", True, "period-before-finish")
    clock[0] = datetime(2026, 8, 27, 9, tzinfo=timezone.utc)
    session_id = repository.start_session("child-primary-4", "period-current-session")["session_id"]
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session_id,))
        connection.commit()
    current_answer = learning.submit_diagnostic(session_id, "diag-p4-arithmetic-closed-1", "90", "period-current")
    assert current_answer["score_status"] == "verified_local"
    repository.transition(session_id, "study", "reflection", "结束当天学习", "period-current-reflection")
    repository.complete_with_reflection(session_id, "", "", "okay", "", True, "period-current-finish")
    other_session = repository.start_session("child-junior-3", "period-other-session")["session_id"]
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other_session,))
        connection.commit()
    supply = SpecializedDiagnosticSupplyService(database)
    supply.enqueue_local("diag-j3-discriminant-1", "child-junior-3", "period-other-exact", count=1)
    supply.process_local_pending()
    candidate = supply.list("child-junior-3")["items"][0]
    qualified = supply.qualify_local(candidate["candidate_id"], "child-junior-3", "period-other-qualify")
    other_answer = learning.submit_diagnostic(other_session, qualified["diagnostic_item_id"],
        qualified["expected_answer"]["exact_answers"][0], "period-other-answer")
    assert other_answer["score_status"] == "verified_local"
    weekly = reports.generate("child-primary-4", "weekly", date.fromisoformat("2026-08-28"))
    summary = weekly["content"]["learning_summary"]
    assert summary["coverage"]["eligible_evidence_count"] == 1
    assert summary["evidence"][0]["date"] == "2026-08-27"
    assert reports.generate("child-primary-4", "weekly", date.fromisoformat("2026-08-21"))["content"]["learning_summary"]["coverage"]["eligible_evidence_count"] == 1
    assert reports.generate("child-junior-3", "weekly", date.fromisoformat("2026-08-28"))["content"]["learning_summary"]["coverage"]["eligible_evidence_count"] == 1
    assert summary["evidence"][0]["evidence_ref"].startswith("record-")
    assert "evidence_id" not in json.dumps(summary, ensure_ascii=False)
    assert "child-junior-3" not in json.dumps(summary, ensure_ascii=False)
    assert "稳定掌握" not in summary["knowledge_support"][0]["support"]
    old = parent_reports.create_from_period(weekly)
    old = parent_reports.edit_draft(old["snapshot_id"], parent_note="先和孩子核对记录")
    evidence_ref = summary["evidence"][0]["evidence_ref"]
    assert reports.retract_evidence("child-primary-4", evidence_ref, "误记，待核对", "period-retract-001") is True
    assert reports.retract_evidence("child-primary-4", evidence_ref, "误记，待核对", "period-retract-001") is True
    with pytest.raises(PeriodEvidenceActionConflictError):
        reports.retract_evidence("child-junior-3", evidence_ref, "误记，待核对", "period-retract-001")
    after_retract = reports.generate("child-primary-4", "weekly", date.fromisoformat("2026-08-28"))
    assert after_retract["content"]["learning_summary"]["coverage"]["eligible_evidence_count"] == 0
    current = parent_reports.create_from_period(after_retract)
    old_after = parent_reports.get(old["snapshot_id"])
    assert old_after["status"] == "revoked"
    assert old_after["revoke_reason"] == "后续证据已更新；请审核新版草稿"
    assert old_after["content"] == old["content"] and old_after["parent_edit"] == old["parent_edit"]
    assert current["snapshot_id"] != old["snapshot_id"]
    assert current["status"] == "draft"
    restarted = PeriodReportService(Database(database.path))
    assert restarted.restore_evidence("child-primary-4", evidence_ref, "核对后恢复", "period-restore-001") is True
    restored = restarted.generate("child-primary-4", "weekly", date.fromisoformat("2026-08-28"))
    assert restored["content"]["learning_summary"]["coverage"]["eligible_evidence_count"] == 1
    reread = ParentReportService(Database(database.path)).get(old["snapshot_id"])
    assert {key:value for key,value in reread.items() if key!='correction_status'} == {
        key:value for key,value in old_after.items() if key!='correction_status'}
    assert reread['correction_status']['source_current'] is False
    assert reread['correction_status']['latest_replacement_id'] == current['snapshot_id']
    assert reread['status'] == 'revoked'  # Restoring a source never reapproves the old report.
    with database.connect() as connection:
        stale = connection.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (current['snapshot_id'],)).fetchone()
        assert not ParentReportService.snapshot_source_current(connection, stale)
    restored_draft = ParentReportService(Database(database.path)).create_from_period(restored)
    assert restored_draft['status'] == 'draft'
    assert restored_draft['snapshot_id'] not in {old['snapshot_id'], current['snapshot_id']}


def test_period_report_keeps_retested_case_visible_when_current_status_needs_followup(period_exact_receipts) -> None:
    from home_study_agent.mistake_book import MistakeBookService

    database, _, learning, clock, session_id = period_exact_receipts
    reports = PeriodReportService(database); reports.initialize()
    source = learning.submit_diagnostic(session_id, "diag-p4-arithmetic-closed-1", "12", "period-source")
    assert source["score_status"] == "verified_local"
    mistakes = MistakeBookService(database)
    case = mistakes.capture_from_attempt(source)
    assert case is not None
    mistake_id = case["mistake_id"]
    mistakes.confirm(mistake_id, "child-primary-4", "confirm", "period-confirm")
    mistakes.diagnose(mistake_id, "child-primary-4", "concept_gap", "需要核对运算顺序", .9, "period-diagnose")
    mistakes.add_remediation(mistake_id, "child-primary-4", "concept_explanation", "completed", "已比较运算顺序", "period-remediate")
    clock[0] += timedelta(minutes=1)
    retest = mistakes.submit_attempt(mistake_id, "child-primary-4", "near_transfer", "diag-p4-arithmetic-closed-2", "42", 0, "period-retest")
    assert retest["attempt_result"]["correctness"] == 1
    assert retest["status"] == "near_transfer"
    assert retest["mastery_gate"]["satisfied"]["near_transfer"] == 1
    assert retest["mastery_gate"]["requirements"]["near_transfer"] == 2
    report = reports.generate("child-primary-4", "weekly", date.fromisoformat("2026-08-28"))["content"]["learning_summary"]
    assert report["coverage"]["eligible_evidence_count"] == 2
    assert len(report["independent_variant_retests"]) == 1
    assert len(report["new_weaknesses"]) == 1
    assert len(report["pending_confirmation"]) == 1
    assert "未完全正确" in report["knowledge_support"][0]["support"]
    empty = reports.generate("child-junior-3", "monthly", date.fromisoformat("2026-08-28"))["content"]["learning_summary"]
    assert empty["coverage"]["eligible_evidence_count"] == 0
    assert "未作答" in empty["uncertainty"]


def test_period_evidence_retraction_api_requires_parent_and_cannot_cross_children(tmp_path: Path, period_exact_receipts) -> None:
    database, _, learning, _, session_id = period_exact_receipts
    database_path = database.path
    reports = PeriodReportService(database); reports.initialize()
    answer = learning.submit_diagnostic(session_id, "diag-p4-arithmetic-closed-1", "90", "period-api-answer")
    assert answer["score_status"] == "verified_local"
    summary = reports.generate("child-primary-4", "weekly", date.fromisoformat("2026-08-28"))["content"]["learning_summary"]
    ref = summary["evidence"][0]["evidence_ref"]
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"A" * 32, maintenance_enabled=False)
    payload = {"note": "记录需要核对", "client_action_id": "period-api-retract-001"}
    with TestClient(create_app(settings)) as client:
        assert client.post(f"/v1/parent/children/child-primary-4/period-evidence/{ref}/retract", json=payload).status_code == 401
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        assert client.post(f"/v1/parent/children/child-junior-3/period-evidence/{ref}/retract", json=payload, headers={"X-HSC-Parent": token}).status_code == 404
        assert client.post(f"/v1/parent/children/child-primary-4/period-evidence/{ref}/retract", json=payload, headers={"X-HSC-Parent": token}).status_code == 200
    restarted = PeriodReportService(Database(database_path))
    assert restarted.generate("child-primary-4", "weekly", date.fromisoformat("2026-08-28"))["content"]["learning_summary"]["coverage"]["eligible_evidence_count"] == 0


def test_new_period_version_revokes_approved_unsent_and_blocks_retry_and_processing_race(tmp_path: Path) -> None:
    database = Database(tmp_path / "period-superseded-delivery.db"); database.initialize(); StudyRepository(database).seed_family()
    snapshots = ParentReportService(database); delivery = ReportDeliveryService(database, b"S" * 32); delivery.initialize()
    periods = PeriodReportService(database)
    old = snapshots.create_from_period(periods.generate('child-junior-3', 'weekly'))
    snapshots.approve(old["snapshot_id"])
    recipient = delivery.add_recipient("child-junior-3", "email", "parent@example.test", "妈妈", True)
    failed = delivery.enqueue(old["snapshot_id"], recipient["recipient_id"])
    processing = delivery.enqueue(old["snapshot_id"], recipient["recipient_id"], "2099-01-01T00:00:00+00:00")
    with database.connect() as connection:
        connection.execute("UPDATE report_delivery_outbox SET status = 'failed' WHERE delivery_id = ?", (failed["delivery_id"],))
        connection.execute("UPDATE report_delivery_outbox SET status = 'processing' WHERE delivery_id = ?", (processing["delivery_id"],))
        connection.commit()
    _daily_report(database)  # A real new daily source replaces the previously empty period.
    newest = snapshots.create_from_period(periods.generate('child-junior-3', 'weekly'))
    assert newest["status"] == "draft"
    assert snapshots.get(old["snapshot_id"])["status"] == "revoked"
    assert delivery.get_delivery(failed["delivery_id"])["status"] == "cancelled"
    with pytest.raises(DeliveryStateError, match="后续证据更新"):
        delivery.retry(failed["delivery_id"])
    assert delivery._submission_block_reason(processing["delivery_id"]) == "snapshot_not_approved"
    delivery._cancel_before_submission(processing["delivery_id"], "snapshot_not_approved")
    assert delivery.get_delivery(processing["delivery_id"])["status"] == "cancelled"


def test_low_spec_startup_recovers_pending_report_without_loading_a_local_model(tmp_path: Path) -> None:
    database_path = tmp_path / "pending-report.db"
    database = Database(database_path)
    database.initialize()
    _pending_report_job(database)
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"5" * 32, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        report = client.get("/v1/sessions/pending-report-session/report", headers={"X-HSC-Parent": token})
        assert report.status_code == 200
        assert report.json()["source"] == "local_rules_v2"
    with database.connect() as connection:
        job = connection.execute("SELECT status FROM report_job WHERE job_id = 'pending-report-job'").fetchone()
        snapshot = connection.execute("SELECT status FROM parent_report_snapshot WHERE report_id = ?", (report.json()["report_id"],)).fetchone()
    assert job["status"] == "completed"
    assert snapshot["status"] == "draft"


def test_cloud_parent_coaching_is_audited_while_facts_remain_local(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud-report.db"
    database = Database(database_path)
    database.initialize()
    _pending_report_job(database)
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"a" * 32, maintenance_enabled=False, model_api_key="configured")
    with TestClient(create_app(settings, model_gateway=CloudReportGateway())) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        report = client.get("/v1/sessions/pending-report-session/report", headers={"X-HSC-Parent": token}).json()
    assert report["source"] == "cloud_parent_coaching_v1"
    assert report["parent_coaching"]["script"] == ["谢谢你把进度留下来。", "我们只看下一小步。"]
    assert "四年级阶段目标" in report["parent_coaching"]["why_this_fits"]
    assert report["parent_coaching"]["personalization"]["feedback_considered"] == 0
    assert any("孩子开始前自评精力" in fact for fact in report["summary"]["facts"])
    with database.connect() as connection:
        call = connection.execute("SELECT status, verification_status, safety_status FROM model_call WHERE purpose = 'parent_report_coaching'").fetchone()
    assert tuple(call) == ("completed", "partially_verified", "passed")


def test_cloud_parent_coaching_timeout_falls_back_to_explicit_local_rules(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud-report-timeout.db"
    database = Database(database_path)
    database.initialize()
    _pending_report_job(database)
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"b" * 32, maintenance_enabled=False, model_api_key="configured")
    with TestClient(create_app(settings, model_gateway=TimedOutCloudReportGateway())) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        report = client.get("/v1/sessions/pending-report-session/report", headers={"X-HSC-Parent": token}).json()
    assert report["source"] == "local_rules_v2"
    with database.connect() as connection:
        call = connection.execute("SELECT status, error_code FROM model_call WHERE purpose = 'parent_report_coaching'").fetchone()
    assert tuple(call) == ("failed", "MODEL_TIMEOUT")


def test_cloud_parent_coaching_missing_fields_falls_back_and_is_audited(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud-report-malformed.db"
    database = Database(database_path)
    database.initialize()
    _pending_report_job(database)
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"c" * 32, maintenance_enabled=False, model_api_key="configured")
    with TestClient(create_app(settings, model_gateway=MalformedCloudReportGateway())) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        report = client.get("/v1/sessions/pending-report-session/report", headers={"X-HSC-Parent": token}).json()
    assert report["source"] == "local_rules_v2"
    with database.connect() as connection:
        call = connection.execute("SELECT status, error_code FROM model_call WHERE purpose = 'parent_report_coaching'").fetchone()
    assert tuple(call) == ("failed", "PARENT_REPORT_INVALID_ACTION")


def test_report_generation_timeout_keeps_job_pending_without_creating_an_empty_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "timed-out-report.db"
    database = Database(database_path)
    database.initialize()
    _pending_report_job(database)
    settings = Settings(data_dir=tmp_path, database_path=database_path, data_encryption_key=b"9" * 32, maintenance_enabled=False)
    original_builder = app_module.build_daily_report

    def timed_out_builder(_: dict) -> tuple[dict, dict]:
        raise TimeoutError("upstream report generation timed out")

    monkeypatch.setattr(app_module, "build_daily_report", timed_out_builder)
    with TestClient(create_app(settings)):
        pass
    with database.connect() as connection:
        job = connection.execute("SELECT status, attempts, last_error FROM report_job WHERE job_id = 'pending-report-job'").fetchone()
        snapshots = connection.execute("SELECT COUNT(*) FROM parent_report_snapshot").fetchone()[0]
    assert dict(job) == {"status": "pending", "attempts": 1, "last_error": "TimeoutError"}
    assert snapshots == 0

    monkeypatch.setattr(app_module, "build_daily_report", original_builder)
    with TestClient(create_app(settings)):
        pass
    with database.connect() as connection:
        recovered = connection.execute("SELECT status FROM report_job WHERE job_id = 'pending-report-job'").fetchone()
        snapshots = connection.execute("SELECT COUNT(*) FROM parent_report_snapshot WHERE source_kind = 'daily_report'").fetchone()[0]
    assert recovered["status"] == "completed"
    assert snapshots == 1
