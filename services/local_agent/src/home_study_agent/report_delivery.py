from __future__ import annotations

"""Durable report-delivery outbox with consent, quiet hours and retries."""

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from html import escape
import json
import os
import sqlite3
from typing import Callable
from uuid import uuid4, uuid5, NAMESPACE_URL

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .database import Database
from .family_clock import family_time
from .notification_channels import ChannelDeliveryError, ChannelSubmissionCancelled, EmailAttachment, channels, submission_guard, validate_email_attachments
from .parent_reports import ParentReportService, SnapshotStateError, UnknownReportSnapshotError
from .repository import utc_now


class UnknownRecipientError(KeyError): pass
class DeliveryStateError(ValueError): pass
class ChannelTestActionConflictError(DeliveryStateError): pass


class ReportDeliveryService:
    def __init__(self, database: Database, encryption_key: bytes | None):
        self.database = database
        self.encryption_key = encryption_key
        self.snapshots = ParentReportService(database)
        self.adapters = channels()
        # Passwords/webhooks must never enter SQLite, audit rows, or config
        # exports.  The desktop launcher may provide a value from its system
        # secure store on process start; tests may supply one only in memory.
        self._runtime_secrets: dict[str, str] = {
            key: value for key, value in {
                "email_password": os.environ.get("HSC_REPORT_DELIVERY_EMAIL_PASSWORD"),
                "wechat_work_bot_webhook": os.environ.get("HSC_REPORT_DELIVERY_WECOM_WEBHOOK"),
            }.items() if value
        }

    def freeze_daily_observation(self, child_id: str, day: str, recipient_id: str, source_id: str) -> dict:
        """Freeze a local encrypted observation snapshot; never sends or schedules."""
        from .daily_report_frozen_snapshot import DailyReportFrozenSnapshotService
        return DailyReportFrozenSnapshotService(self.database, self.encryption_key).freeze(child_id, day, recipient_id, source_id)

    def inspect_frozen_daily_observation(self, child_id: str, day: str, recipient_id: str, snapshot_id: str) -> dict:
        from .daily_report_frozen_snapshot import DailyReportFrozenSnapshotService
        return DailyReportFrozenSnapshotService(self.database, self.encryption_key).inspect(child_id, day, recipient_id, snapshot_id)

    def prepare_daily_observation_delivery_locally(self, child_id: str, day: str, recipient_id: str, snapshot_id: str, *, expected_snapshot_id: str | None = None) -> dict:
        """Create a local readiness record; this never submits or schedules mail."""
        from .daily_report_delivery_state import reserve_local
        return reserve_local(self, child_id, day, recipient_id, snapshot_id, expected_snapshot_id=expected_snapshot_id)

    @contextmanager
    def _external_effect_guard(self, timeout: float | None = None):
        with ExitStack() as stack:
            try:
                stack.enter_context(self.database.external_effect_guard() if timeout is None
                                    else self.database.external_effect_guard(timeout=timeout))
            except TimeoutError as error:
                raise DeliveryStateError("REPORT_DELIVERY_BUSY: 报告发送或数据恢复仍在进行，请稍后重试；尚未开始本次发送。") from error
            yield

    def initialize(self, *, recover_processing: bool = True) -> None:
        self.snapshots.initialize()
        with self.database.connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS report_delivery_recipient (
              recipient_id TEXT PRIMARY KEY,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              channel TEXT NOT NULL CHECK(channel IN ('email', 'wechat_work_bot')),
              address TEXT NOT NULL,
              label TEXT NOT NULL,
              consent_status TEXT NOT NULL CHECK(consent_status IN ('pending', 'consented', 'revoked')),
              consented_at TEXT,
              revoked_at TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(child_id, channel, address)
            );
            CREATE TABLE IF NOT EXISTS report_delivery_preference (
              child_id TEXT PRIMARY KEY REFERENCES child_profile(child_id) ON DELETE CASCADE,
              quiet_start TEXT NOT NULL DEFAULT '21:00',
              quiet_end TEXT NOT NULL DEFAULT '07:00',
              timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
              retention_days INTEGER NOT NULL DEFAULT 90 CHECK(retention_days BETWEEN 7 AND 3650),
              redaction_level TEXT NOT NULL DEFAULT 'standard' CHECK(redaction_level IN ('standard', 'minimal')),
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_credential (
              credential_id TEXT PRIMARY KEY,
              channel TEXT NOT NULL UNIQUE,
              encrypted_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS report_delivery_outbox (
              delivery_id TEXT PRIMARY KEY,
              snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE RESTRICT,
              recipient_id TEXT NOT NULL REFERENCES report_delivery_recipient(recipient_id) ON DELETE RESTRICT,
              channel TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL CHECK(status IN ('scheduled', 'processing', 'sent', 'failed', 'cancelled', 'unknown', 'unknown_acknowledged', 'superseded')),
              scheduled_at TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error_code TEXT,
              last_error_message TEXT,
              provider_message_id TEXT,
              sent_at TEXT,
              cancelled_at TEXT,
              cancel_reason TEXT,
              replacement_delivery_id TEXT,
              reissue_action_id TEXT UNIQUE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_report_delivery_due ON report_delivery_outbox(status, scheduled_at);
            CREATE TABLE IF NOT EXISTS report_delivery_attempt (
              attempt_id TEXT PRIMARY KEY,
              delivery_id TEXT NOT NULL REFERENCES report_delivery_outbox(delivery_id) ON DELETE CASCADE,
              attempt_number INTEGER NOT NULL,
              status TEXT NOT NULL,
              error_code TEXT,
              error_message TEXT,
              provider_message_id TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS report_delivery_channel_test (
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              channel TEXT NOT NULL CHECK(channel IN ('email', 'wechat_work_bot')),
              status TEXT NOT NULL CHECK(status IN ('passed', 'failed', 'blocked')),
              message TEXT NOT NULL,
              tested_at TEXT NOT NULL,
              PRIMARY KEY(child_id, channel)
            );
            CREATE TABLE IF NOT EXISTS report_delivery_channel_test_action (
              client_action_id TEXT PRIMARY KEY,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              channel TEXT NOT NULL CHECK(channel IN ('email', 'wechat_work_bot')),
              status TEXT NOT NULL CHECK(status IN ('processing', 'passed', 'failed', 'blocked', 'unknown', 'unknown_acknowledged', 'superseded')),
              message TEXT,
              tested_at TEXT,
              replacement_action_id TEXT,
              created_at TEXT NOT NULL
            );
            """)
            self._migrate_outbox(connection)
            self._migrate_channel_test_actions(connection)
            self._migrate_persisted_secrets(connection)
            preference_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(report_delivery_preference)"
                ).fetchall()
            }
            if "redaction_level" not in preference_columns:
                connection.execute(
                    "ALTER TABLE report_delivery_preference ADD COLUMN redaction_level TEXT NOT NULL DEFAULT 'standard'"
                )
            # Test requests reserve before socket I/O. A process loss after SMTP
            # accepts the message cannot be safely retried, so surface it as
            # unknown rather than silently sending a second test mail.
            uncertain_actions = connection.execute("""
              SELECT client_action_id, child_id, channel FROM report_delivery_channel_test_action
              WHERE status = 'processing' AND ?
            """, (recover_processing,)).fetchall()
            if uncertain_actions:
                for action in uncertain_actions:
                    message = "上次测试结果未知，可能已发出。为避免重复投递，普通重试不会再次发送；可保持不重发，或明确发起新的固定非隐私测试邮件。"
                    connection.execute("""
                      UPDATE report_delivery_channel_test_action
                      SET status = 'unknown', message = ?, tested_at = ? WHERE client_action_id = ?
                    """, (message, utc_now(), action["client_action_id"]))
                    connection.execute("""
                      INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                      VALUES (?, 'system', 'report_delivery_channel_test_unknown_after_restart', ?, ?, ?)
                    """, (str(uuid4()), action["child_id"], json.dumps({"channel": action["channel"]}, ensure_ascii=False), utc_now()))
            # SMTP may have accepted a full report immediately before an abrupt
            # process loss.  Without a provider receipt we cannot call that send
            # idempotent, so never silently put a claimed row back into the queue.
            uncertain = connection.execute("""
              SELECT o.delivery_id, o.attempt_count, s.child_id
              FROM report_delivery_outbox o JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id
              WHERE o.status = 'processing' AND ?
            """, (recover_processing,)).fetchall()
            for item in uncertain:
                now = utc_now()
                connection.execute("""
                  UPDATE report_delivery_outbox
                  SET status = 'unknown', last_error_code = 'UNKNOWN_AFTER_RESTART',
                      last_error_message = '上次发送结果未知，可能已交给邮件服务。为避免重复发送，系统不会自动重试。', updated_at = ?
                  WHERE delivery_id = ? AND status = 'processing'
                """, (now, item["delivery_id"]))
                connection.execute("""
                  INSERT INTO report_delivery_attempt(attempt_id, delivery_id, attempt_number, status, error_code, error_message, created_at)
                  VALUES (?, ?, ?, 'unknown', 'UNKNOWN_AFTER_RESTART', '发送结果未知，可能已交给邮件服务。', ?)
                """, (str(uuid4()), item["delivery_id"], item["attempt_count"], now))
                connection.execute("""
                  INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                  VALUES (?, 'system', 'report_delivery_unknown_after_restart', ?, ?, ?)
                """, (str(uuid4()), item["child_id"], json.dumps({"delivery_id": item["delivery_id"]}, ensure_ascii=False), now))
            connection.commit()

    @staticmethod
    def quarantine_after_restore(connection, restore_id: str) -> dict:
        """Invalidate restored send authority inside the caller's staging transaction.

        No schema initialization, connections, commit, transport, or attempt rows.
        The host must exclude live external effects until staging replaces live.
        """
        if not connection.in_transaction:
            raise DeliveryStateError("REPORT_RESTORE_WRITE_TRANSACTION_REQUIRED")
        if (not isinstance(restore_id, str) or not 1 <= len(restore_id) <= 160
                or not restore_id.isascii() or any(not (char.isalnum() or char in "._:-") for char in restore_id)):
            raise DeliveryStateError("REPORT_RESTORE_ID_INVALID")
        event_id = str(uuid5(NAMESPACE_URL, "home-study-report-restore:" + restore_id))
        existing = connection.execute("SELECT action,summary_json FROM audit_event WHERE audit_id=?", (event_id,)).fetchone()
        if existing:
            if existing[0] != "report_delivery_restore_quarantined":
                raise DeliveryStateError("REPORT_RESTORE_ID_CONFLICT")
            return json.loads(existing[1])
        tables = {row[0]: row[1] or "" for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
        now = utc_now()
        message = "RESTORE_DELIVERY_UNCERTAIN: 已恢复历史记录，无法确认备份后是否已发送；不会自动重发，原审核和外发同意需重新确认。"
        result = {"version": "report_restore_quarantine_v1", "restore_id": restore_id,
                  "outbox_unknown": 0, "snapshot_approvals_invalidated": 0,
                  "recipient_consents_invalidated": 0, "channel_tests_unknown": 0,
                  "channel_verifications_invalidated": 0,
                  "legacy_status_fallbacks": {"outbox": 0, "channel_tests": 0}}

        def supports_unknown(table):
            schema = tables[table].lower()
            return "check" not in schema or "'unknown'" in schema or '"unknown"' in schema

        connection.execute("SAVEPOINT report_restore_quarantine")
        try:
            if "report_delivery_outbox" in tables:
                status = "unknown" if supports_unknown("report_delivery_outbox") else "cancelled"
                count = connection.execute("""UPDATE report_delivery_outbox SET status=?,
                    last_error_code='RESTORE_DELIVERY_UNCERTAIN',last_error_message=?,updated_at=?
                    WHERE status IN ('scheduled','processing','failed')""", (status, message, now)).rowcount
                result["outbox_unknown"] = count
                result["legacy_status_fallbacks"]["outbox"] = count if status == "cancelled" else 0
            if "parent_report_snapshot" in tables:
                approved = connection.execute("SELECT snapshot_id,child_id,approved_at,approved_by FROM parent_report_snapshot WHERE status='approved'").fetchall()
                for row in approved:
                    connection.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                        VALUES (?,'system','parent_report_approval_invalidated_after_restore',?,?,?)""",
                        (str(uuid4()), row[1], json.dumps({"snapshot_id": row[0], "restore_id": restore_id,
                            "previous_approved_at": row[2], "previous_approved_by": row[3]}, ensure_ascii=False), now))
                result["snapshot_approvals_invalidated"] = connection.execute("""UPDATE parent_report_snapshot
                    SET status='draft' WHERE status='approved'""").rowcount
            if "report_delivery_recipient" in tables:
                if "daily_report_email_verification" in tables:
                    connection.execute('DELETE FROM daily_report_email_verification')
                result["recipient_consents_invalidated"] = connection.execute("""UPDATE report_delivery_recipient
                    SET consent_status='pending' WHERE consent_status='consented'""").rowcount
            if "pilot_diagnostic_recipient_consent" in tables:
                connection.execute("UPDATE pilot_diagnostic_recipient_consent SET status='revoked', updated_at=?", (now,))
                if "pilot_test_digest_outbox" in tables:
                    connection.execute("""INSERT OR IGNORE INTO pilot_diagnostic_recipient_consent(recipient,status,updated_at)
                        SELECT DISTINCT lower(trim(recipient)), 'revoked', ? FROM pilot_test_digest_outbox
                        WHERE status='sent' AND recipient IS NOT NULL AND length(trim(recipient)) > 0""", (now,))
            if "report_delivery_channel_test" in tables:
                # A restored verification describes the old environment. Keep
                # immutable successful action receipts, but require a fresh
                # fixed test after recipients renew their consent.
                schema = tables["report_delivery_channel_test"].lower()
                status = ("blocked" if "check" not in schema or "'blocked'" in schema or '"blocked"' in schema
                          else "failed")
                result["channel_verifications_invalidated"] = connection.execute("""UPDATE report_delivery_channel_test
                    SET status=?,message=? WHERE status='passed'""", (
                        status, "学习数据已恢复，请重新发送固定测试邮件验证当前通道；历史投递回执保留，不会自动重发。",
                    )).rowcount
            if "report_delivery_channel_test_action" in tables:
                status = "unknown" if supports_unknown("report_delivery_channel_test_action") else "blocked"
                count = connection.execute("""UPDATE report_delivery_channel_test_action SET status=?,message=?
                    WHERE status='processing'""", (status, message)).rowcount
                result["channel_tests_unknown"] = count
                result["legacy_status_fallbacks"]["channel_tests"] = count if status == "blocked" else 0
            connection.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                VALUES (?,'system','report_delivery_restore_quarantined',NULL,?,?)""",
                (event_id, json.dumps(result, ensure_ascii=False), now))
            connection.execute("RELEASE SAVEPOINT report_restore_quarantine")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK TO SAVEPOINT report_restore_quarantine")
                connection.execute("RELEASE SAVEPOINT report_restore_quarantine")
            raise
        return result

    @staticmethod
    def _migrate_outbox(connection) -> None:
        """Rebuild the SQLite CHECK constraint while preserving old delivery audit rows."""
        table = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'report_delivery_outbox'").fetchone()
        if not table:
            return
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(report_delivery_outbox)").fetchall()}
        if "unknown" in (table["sql"] or "") and {"replacement_delivery_id", "reissue_action_id"}.issubset(columns):
            return
        # SQLite does not change foreign-key enforcement in a transaction. Turn it
        # off before the one atomic rebuild, then re-enable it only after commit.
        # DDL participates in the explicit transaction, so an exception restores
        # the old table/index names instead of stranding a half-upgraded database.
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP INDEX IF EXISTS idx_report_delivery_due")
            connection.execute("ALTER TABLE report_delivery_attempt RENAME TO report_delivery_attempt_legacy")
            connection.execute("ALTER TABLE report_delivery_outbox RENAME TO report_delivery_outbox_legacy")
            connection.execute("""
          CREATE TABLE report_delivery_outbox (
            delivery_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE RESTRICT,
            recipient_id TEXT NOT NULL REFERENCES report_delivery_recipient(recipient_id) ON DELETE RESTRICT,
            channel TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('scheduled', 'processing', 'sent', 'failed', 'cancelled', 'unknown', 'unknown_acknowledged', 'superseded')),
            scheduled_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT,
            last_error_message TEXT,
            provider_message_id TEXT,
            sent_at TEXT,
            cancelled_at TEXT,
            cancel_reason TEXT,
            replacement_delivery_id TEXT,
            reissue_action_id TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
          )
            """)
            connection.execute("""
          INSERT INTO report_delivery_outbox(delivery_id, snapshot_id, recipient_id, channel, idempotency_key, status, scheduled_at, attempt_count, last_error_code, last_error_message, provider_message_id, sent_at, cancelled_at, cancel_reason, created_at, updated_at)
          SELECT delivery_id, snapshot_id, recipient_id, channel, idempotency_key, status, scheduled_at, attempt_count, last_error_code, last_error_message, provider_message_id, sent_at, cancelled_at, cancel_reason, created_at, updated_at
          FROM report_delivery_outbox_legacy
            """)
            connection.execute("""UPDATE report_delivery_outbox SET status='unknown'
                WHERE status='cancelled' AND last_error_code='RESTORE_DELIVERY_UNCERTAIN'""")
            connection.execute("CREATE INDEX idx_report_delivery_due ON report_delivery_outbox(status, scheduled_at)")
            connection.execute("""
          CREATE TABLE report_delivery_attempt (
            attempt_id TEXT PRIMARY KEY,
            delivery_id TEXT NOT NULL REFERENCES report_delivery_outbox(delivery_id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            provider_message_id TEXT,
            created_at TEXT NOT NULL
          )
            """)
            connection.execute("""
          INSERT INTO report_delivery_attempt(attempt_id, delivery_id, attempt_number, status, error_code, error_message, provider_message_id, created_at)
          SELECT attempt_id, delivery_id, attempt_number, status, error_code, error_message, provider_message_id, created_at
          FROM report_delivery_attempt_legacy
            """)
            connection.execute("DROP TABLE report_delivery_attempt_legacy")
            connection.execute("DROP TABLE report_delivery_outbox_legacy")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise DeliveryStateError("送达记录升级校验失败，已恢复旧版本。")
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DeliveryStateError("送达记录升级后无法启用外键校验。")

    @staticmethod
    def _migrate_channel_test_actions(connection) -> None:
        """Upgrade the short-lived test-action ledger without losing its audit trail."""
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute("SAVEPOINT report_channel_test_upgrade")
        released = False
        try:
            legacy = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_delivery_channel_test_action_legacy'").fetchone()
            if legacy:
                # Older upgrades committed the rename before copying. Recover only
                # an empty destination; competing histories need explicit repair.
                if connection.execute("SELECT 1 FROM report_delivery_channel_test_action LIMIT 1").fetchone():
                    raise DeliveryStateError("通道测试记录存在冲突的升级历史，已停止升级；请保留备份并核对，不要重新发送。")
                connection.execute("DROP TABLE report_delivery_channel_test_action")
                connection.execute("ALTER TABLE report_delivery_channel_test_action_legacy RENAME TO report_delivery_channel_test_action")
            ReportDeliveryService._rebuild_channel_test_actions(connection)
            if connection.execute("PRAGMA foreign_key_check(report_delivery_channel_test_action)").fetchall():
                raise DeliveryStateError("通道测试记录升级校验失败，已恢复旧版本。")
            connection.execute("RELEASE SAVEPOINT report_channel_test_upgrade")
            released = True
            if owns_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction and not released:
                connection.execute("ROLLBACK TO SAVEPOINT report_channel_test_upgrade")
                connection.execute("RELEASE SAVEPOINT report_channel_test_upgrade")
            if owns_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _rebuild_channel_test_actions(connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(report_delivery_channel_test_action)").fetchall()}
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'report_delivery_channel_test_action'").fetchone()["sql"]
        if "unknown" in schema and "replacement_action_id" in columns:
            return
        connection.execute("ALTER TABLE report_delivery_channel_test_action RENAME TO report_delivery_channel_test_action_legacy")
        connection.execute("""
          CREATE TABLE report_delivery_channel_test_action (
            client_action_id TEXT PRIMARY KEY,
            child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
            channel TEXT NOT NULL CHECK(channel IN ('email', 'wechat_work_bot')),
            status TEXT NOT NULL CHECK(status IN ('processing', 'passed', 'failed', 'blocked', 'unknown', 'unknown_acknowledged', 'superseded')),
            message TEXT,
            tested_at TEXT,
            replacement_action_id TEXT,
            created_at TEXT NOT NULL
          )
        """)
        replacement = "replacement_action_id" if "replacement_action_id" in columns else "NULL"
        connection.execute(f"""
          INSERT INTO report_delivery_channel_test_action(client_action_id, child_id, channel, status, message, tested_at, created_at, replacement_action_id)
          SELECT client_action_id, child_id, channel, status, message, tested_at, created_at, {replacement}
          FROM report_delivery_channel_test_action_legacy
        """)
        connection.execute("""UPDATE report_delivery_channel_test_action SET status='unknown'
            WHERE status='blocked' AND message LIKE 'RESTORE_DELIVERY_UNCERTAIN:%'""")
        connection.execute("DROP TABLE report_delivery_channel_test_action_legacy")

    def _migrate_persisted_secrets(self, connection) -> None:
        """Remove legacy passwords/webhooks from SQLite without exposing them.

        Older builds encrypted the whole channel config, including provider
        secrets.  Encryption is not a substitute for the desktop system secure
        store boundary, so an upgrade deliberately retains only transport
        metadata.  A parent must re-authorize any external provider afterwards.
        """
        if not self.encryption_key or len(self.encryption_key) != 32:
            return
        rows = connection.execute("SELECT credential_id, encrypted_json FROM notification_credential").fetchall()
        allowed = {"host", "port", "username", "from_address", "use_ssl", "starttls", "allow_external_delivery"}
        for row in rows:
            try:
                raw = base64.b64decode(row["encrypted_json"])
                config = json.loads(AESGCM(self.encryption_key).decrypt(raw[:12], raw[12:], b"hsc-notification-credential-v1"))
            except Exception:
                # Preserve an unreadable record so its normal, non-sensitive
                # repair path can explain the problem without breaking startup.
                continue
            public = {key: value for key, value in config.items() if key in allowed}
            if public == config:
                continue
            nonce = os.urandom(12)
            encrypted = AESGCM(self.encryption_key).encrypt(nonce, json.dumps(public, ensure_ascii=False).encode(), b"hsc-notification-credential-v1")
            connection.execute("UPDATE notification_credential SET encrypted_json = ?, updated_at = ? WHERE credential_id = ?", (base64.b64encode(nonce + encrypted).decode("ascii"), utc_now(), row["credential_id"]))

    def save_channel_config(self, channel: str, config: dict) -> None:
        with self._external_effect_guard():
            self._save_channel_config_guarded(channel, config)

    def _save_channel_config_guarded(self, channel: str, config: dict) -> None:
        if channel not in self.adapters:
            raise ValueError("不支持的送达通道。")
        if not self.encryption_key or len(self.encryption_key) != 32:
            raise ValueError("发送凭证必须使用 HSC_DATA_ENCRYPTION_KEY 的 32 字节密钥加密保存。")
        # Persist only non-secret transport metadata.  A process-local password
        # is intentionally sufficient for test sinks; a production desktop
        # launcher has to restore it from system secure storage on each start.
        # Accept only known non-secret delivery configuration fields; no
        # credential is returned by APIs or written to the database.
        allowed = {"host", "port", "username", "from_address", "use_ssl", "starttls", "allow_external_delivery"}
        safe = {key: value for key, value in config.items() if key in allowed}
        if (channel == "email" and not safe.get("use_ssl", False)
                and safe.get("starttls", True) is False
                and not _is_local_smtp_host(str(safe.get("host") or ""))):
            raise ValueError("为保护孩子数据，外部邮箱必须使用 SSL/TLS 或 STARTTLS；明文 SMTP 只允许本机 sink。")
        nonce = __import__("os").urandom(12)
        encrypted = AESGCM(self.encryption_key).encrypt(nonce, json.dumps(safe, ensure_ascii=False).encode(), b"hsc-notification-credential-v1")
        blob = base64.b64encode(nonce + encrypted).decode("ascii")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("""
              INSERT INTO notification_credential(credential_id, channel, encrypted_json, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?)
              ON CONFLICT(channel) DO UPDATE SET credential_id = excluded.credential_id,
                encrypted_json = excluded.encrypted_json, updated_at = excluded.updated_at
            """, (str(uuid4()), channel, blob, now, now))
            # An explicit save renews the transport/secret configuration. Keep
            # historical action receipts (especially unknown deliveries), but
            # they cannot verify the newly saved configuration.
            connection.execute("""UPDATE report_delivery_channel_test
              SET status='blocked', message='发送配置已重新保存，请重新发送固定测试邮件验证；历史发送结果保留，不会自动重发。'
              WHERE channel=? AND status='passed'""", (channel,))
            connection.commit()

        # Update process-local secrets only after the validated transaction has
        # committed; rejected saves must not alter the working configuration.
        if channel == "email" and isinstance(config.get("password"), str) and config["password"]:
            self._runtime_secrets["email_password"] = config["password"]
        if channel == "wechat_work_bot" and isinstance(config.get("webhook_url"), str) and config["webhook_url"]:
            self._runtime_secrets["wechat_work_bot_webhook"] = config["webhook_url"]

    def channel_status(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT channel, created_at, updated_at FROM notification_credential").fetchall()
        saved = {row["channel"]: dict(row) for row in rows}
        return [{"channel": name, "configured": name in saved, "credential_updated_at": saved.get(name, {}).get("updated_at"), "requires_encryption_key": not bool(self.encryption_key)} for name in self.adapters]

    def submit_diagnostic_email(self, recipient: str, subject: str, body: str, *, attachments: tuple[EmailAttachment, ...] = ()) -> dict:
        """Submit a frozen, locally generated diagnostic digest through real SMTP.

        The caller owns outbox idempotency. This method never falls back to a
        second transport after SMTP submission may have started.
        """
        attachments = validate_email_attachments(attachments)
        if not recipient or "@" not in recipient or len(recipient) > 320:
            raise ChannelDeliveryError("SMTP_RECIPIENT_INVALID", "诊断日报收件地址无效。", False)
        if not subject or not body:
            raise ChannelDeliveryError("SMTP_MESSAGE_INVALID", "诊断日报内容不完整。", False)
        with self._external_effect_guard():
            config = self._config("email")
            receipt = self.adapters["email"].send(
                recipient,
                subject,
                body,
                f"<pre style=\"white-space:pre-wrap;font-family:system-ui,sans-serif\">{escape(body)}</pre>",
                config,
                **({"attachments": attachments} if attachments else {}),
            )
        return {
            "accepted": True,
            "transport": "smtp",
            "client": "configured_smtp",
            "provider_message_id": receipt.provider_message_id,
            "client_message_id": receipt.client_message_id,
        }

    def clear_channel_config(self, channel: str) -> None:
        with self._external_effect_guard():
            self._clear_channel_config_guarded(channel)

    def _clear_channel_config_guarded(self, channel: str) -> None:
        if channel not in self.adapters:
            raise ValueError("不支持的送达通道。")
        runtime_secret_key = {
            "email": "email_password",
            "wechat_work_bot": "wechat_work_bot_webhook",
        }[channel]
        self._runtime_secrets.pop(runtime_secret_key, None)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM notification_credential WHERE channel = ?", (channel,))
            connection.commit()

    def channel_test_status(self, child_id: str) -> list[dict]:
        """Return durable, parent-readable validation results without credentials.

        An SMTP test is deliberately a separate, explicit action.  It sends only a
        fixed non-private message to a consented whitelist recipient; it never
        publishes a report merely because a channel happens to be configured.
        """
        with self.database.connect() as connection:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            connection.execute("""
              UPDATE report_delivery_channel_test_action
              SET status = 'unknown', message = '上次测试结果未知，可能已发出。为避免重复投递，普通重试不会再次发送；可保持不重发，或明确发起新的固定非隐私测试邮件。', tested_at = ?
              WHERE status = 'processing' AND created_at <= ?
            """, (utc_now(), cutoff))
            connection.commit()
            rows = connection.execute(
                "SELECT channel, status, message, tested_at FROM report_delivery_channel_test WHERE child_id = ?",
                (child_id,),
            ).fetchall()
            configurations = {row['channel']: row['updated_at'] for row in connection.execute(
                'SELECT channel, updated_at FROM notification_credential')}
            uncertain = connection.execute("""
              SELECT client_action_id, channel, status, message, tested_at FROM report_delivery_channel_test_action
              WHERE child_id = ? AND status IN ('processing', 'unknown', 'unknown_acknowledged')
              ORDER BY created_at DESC
            """, (child_id,)).fetchall()
            selected_for_action = {}
            for item in uncertain:
                try:
                    selection = self._channel_test_selection(connection, child_id, item['client_action_id'])
                except ChannelTestActionConflictError:
                    selection = None
                selected_for_action[item['client_action_id']] = selection.get('recipient_id') if selection else None
        saved = {row["channel"]: dict(row) for row in rows}
        unknown = {}
        for row in uncertain:
            unknown.setdefault(row["channel"], dict(row))
        result = []
        for channel in self.adapters:
            action = unknown.get(channel)
            row = saved.get(channel)
            if action:
                processing = action["status"] == "processing"
                result.append({
                    "channel": channel, "status": "processing" if processing else "unknown",
                    "message": action["message"] or "测试正在处理中；为避免重复投递，暂时不能再次发送。",
                    "tested_at": action["tested_at"], "client_action_id": action["client_action_id"],
                    "recipient_id": selected_for_action.get(action['client_action_id']),
                    "kept_without_resend": not processing and action["status"] == "unknown_acknowledged",
                })
            elif row:
                current = dict(row)
                if current['status'] == 'passed':
                    try:
                        verified = datetime.fromisoformat(current['tested_at'])
                        configured = datetime.fromisoformat(configurations[channel])
                        fresh = verified.tzinfo is not None and configured.tzinfo is not None and verified >= configured
                    except (KeyError, TypeError, ValueError):
                        fresh = False
                    if not fresh:
                        current.update(status='blocked', message='当前发送配置尚未实发验证；历史测试结果保留，请重新发送固定测试邮件。')
                if channel == 'email':
                    from .daily_report_email_verification import verified
                    with self.database.connect() as connection:
                        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_report_email_verification'").fetchone():
                            try:
                                password = self._config('email').get('password', '')
                                connection.execute('BEGIN')
                                recipients = connection.execute("SELECT recipient_id FROM report_delivery_recipient WHERE child_id=? AND channel='email'", (child_id,)).fetchall()
                                matched = [r['recipient_id'] for r in recipients if verified(connection,child_id,r['recipient_id'],self.encryption_key,password)]
                            except (ChannelDeliveryError, ValueError):
                                matched = []
                            if matched:
                                current['verified_recipient_ids'] = matched
                            elif current['status'] == 'passed':
                                current.update(status='blocked', message='当前邮箱或发送凭据尚未完成固定测试，请重新验证。')
                result.append({"channel": channel, **current})
            elif channel == "wechat_work_bot":
                result.append({
                    "channel": channel,
                    "status": "blocked",
                    "message": "企业微信仅支持企业微信群机器人。未提供企业组织的机器人 Webhook 时无法真实验证；个人微信没有稳定公开 API，系统不会伪造或自动化发送。",
                    "tested_at": None,
                })
            else:
                result.append({"channel": channel, "status": None, "message": "尚未进行发送测试。", "tested_at": None})
        return result

    def test_channel(self, child_id: str, channel: str, client_action_id: str, recipient_id: str | None = None) -> dict:
        """Check a configured delivery path and persist only a safe status.

        Enterprise WeChat is intentionally validation-only here: without an
        organisation-provisioned webhook, claiming delivery would be misleading.
        """
        with self._external_effect_guard():
            return self._test_channel_guarded(child_id, channel, client_action_id, recipient_id)

    def _test_channel_guarded(self, child_id: str, channel: str, client_action_id: str, recipient_id: str | None = None) -> dict:
        if channel not in self.adapters:
            raise ValueError("不支持的送达通道。")
        if recipient_id is not None and (channel != 'email' or not isinstance(recipient_id,str) or not 1 <= len(recipient_id) <= 120):
            raise ValueError('收件人选择无效。')
        replay = self._reserve_channel_test_action(child_id, channel, client_action_id, recipient_id)
        if replay:
            return replay
        return self._run_channel_test(child_id, channel, client_action_id)

    def keep_unknown_channel_test(self, child_id: str, channel: str, client_action_id: str) -> dict:
        message = "家长选择保持不重发。上次测试结果仍未知，可能已发出；系统不会再次发送这封测试邮件。"
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
              SELECT status, message, tested_at FROM report_delivery_channel_test_action
              WHERE client_action_id = ? AND child_id = ? AND channel = ?
            """, (client_action_id, child_id, channel)).fetchone()
            if not row or row["status"] not in {"unknown", "unknown_acknowledged"}:
                connection.rollback()
                raise DeliveryStateError("这次测试不处于可确认的未知状态。")
            selection = self._channel_test_selection(connection,child_id,client_action_id)
            selected_id = selection.get('recipient_id') if selection else None
            if row["status"] == "unknown_acknowledged":
                connection.rollback()
                return {"channel": channel, "status": "unknown", "message": row["message"], "tested_at": row["tested_at"], "client_action_id": client_action_id, "kept_without_resend": True, "recipient_id":selected_id}
            now = utc_now()
            connection.execute("""
              UPDATE report_delivery_channel_test_action SET status = 'unknown_acknowledged', message = ?, tested_at = ?
              WHERE client_action_id = ?
            """, (message, now, client_action_id))
            connection.execute("""
              INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
              VALUES (?, 'parent', 'report_delivery_channel_test_kept_without_resend', ?, ?, ?)
            """, (str(uuid4()), child_id, json.dumps({"channel": channel}, ensure_ascii=False), now))
            connection.commit()
        return {"channel": channel, "status": "unknown", "message": message, "tested_at": now, "client_action_id": client_action_id, "kept_without_resend": True, "recipient_id":selected_id}

    def reissue_unknown_channel_test(self, child_id: str, channel: str, previous_action_id: str, client_action_id: str, recipient_id: str | None = None) -> dict:
        with self._external_effect_guard():
            return self._reissue_channel_test_guarded(child_id, channel, previous_action_id, client_action_id, recipient_id)

    def reissue_reauthorized_email_test(self, child_id: str, previous_action_id: str, client_action_id: str, recipient_id: str, confirmed_address: str) -> dict:
        """Explicitly test the original recipient under newly confirmed authority."""
        if not isinstance(recipient_id, str) or not recipient_id or not isinstance(confirmed_address, str) or not confirmed_address.strip():
            raise DeliveryStateError('请确认当前已授权的原收件邮箱。')
        with self._external_effect_guard():
            return self._reissue_channel_test_guarded(child_id, 'email', previous_action_id, client_action_id, recipient_id, confirmed_address)

    def _reissue_channel_test_guarded(self, child_id: str, channel: str, previous_action_id: str, client_action_id: str, recipient_id: str | None = None, confirmed_address: str | None = None) -> dict:
        if previous_action_id == client_action_id:
            raise ChannelTestActionConflictError("重新测试必须使用新的操作标识。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("""
              SELECT status, replacement_action_id FROM report_delivery_channel_test_action
              WHERE client_action_id = ? AND child_id = ? AND channel = ?
            """, (previous_action_id, child_id, channel)).fetchone()
            if not previous:
                connection.rollback()
                raise DeliveryStateError("只能为结果未知的测试明确发起新的测试。")
            selection = self._channel_test_selection(connection, child_id, previous_action_id)
            if channel == 'email' and (selection is None or not selection.get('recipient_id')):
                raise DeliveryStateError('原测试缺少可靠的收件人身份，不能自动重发；请先核对原测试。')
            if recipient_id is not None and (not selection or selection.get('recipient_id') != recipient_id):
                raise ChannelTestActionConflictError('新的测试必须对应原收件人。')
            reauthorized_recipient = None
            if confirmed_address is not None:
                reauthorized_recipient = self._test_recipient(child_id, channel, recipient_id)
                if (not reauthorized_recipient or not hmac.compare_digest(
                    reauthorized_recipient['address'].strip().casefold().encode('utf-8'),
                    confirmed_address.strip().casefold().encode('utf-8'))):
                    raise DeliveryStateError('当前收件邮箱或授权已变化，请重新核对。')
            existing = connection.execute("""
              SELECT child_id, channel, status, message, tested_at FROM report_delivery_channel_test_action
              WHERE client_action_id = ?
            """, (client_action_id,)).fetchone()
            if previous["replacement_action_id"]:
                connection.rollback()
                if previous["replacement_action_id"] != client_action_id:
                    raise ChannelTestActionConflictError("这次未知测试已绑定另一项新测试，不能再次发送。")
                if not existing or existing["child_id"] != child_id or existing["channel"] != channel:
                    raise ChannelTestActionConflictError("新的测试操作与原测试不匹配。")
                if channel == 'email' and existing['status'] == 'passed':
                    from .daily_report_email_verification import verified
                    try:
                        password = self._config('email').get('password', '')
                        connection.execute('BEGIN')
                        current = bool(selection and verified(connection,child_id,selection['recipient_id'],self.encryption_key,password))
                        connection.rollback()
                    except (ChannelDeliveryError,ValueError,sqlite3.Error):
                        current = False
                    if not current:
                        return {'channel':channel,'status':'passed','message':'原重试当时由服务接受；当前邮箱或发送设置尚未验证，也没有再次发送。',
                                'tested_at':existing['tested_at'],'recipient_id':selection.get('recipient_id') if selection else None,
                                'client_action_id':client_action_id,'historical_only':True,'current_verified':False}
                return {"channel": channel, "status": existing["status"], "message": existing["message"] or "新的测试仍在处理中。", "tested_at": existing["tested_at"], "recipient_id": selection.get('recipient_id') if selection else None, "client_action_id":client_action_id}
            if previous["status"] not in {"unknown", "unknown_acknowledged"}:
                connection.rollback()
                raise DeliveryStateError("只能为结果未知的测试明确发起新的测试。")
            if channel == 'email':
                original_recipient = self._test_recipient(child_id,channel,selection['recipient_id'])
                current_tag = self._channel_test_address_tag(child_id,original_recipient) if original_recipient else None
                if not original_recipient or not current_tag or (reauthorized_recipient is None and selection.get('address_tag') != current_tag):
                    raise DeliveryStateError('原收件人或授权已变化；原测试结果仍未知，不能再次发送。')
                if reauthorized_recipient is not None:
                    selection = {**selection, 'address_tag': current_tag}
            if existing:
                connection.rollback()
                raise ChannelTestActionConflictError("新的测试操作已用于另一位孩子或通道，不能重复使用。")
            now = utc_now()
            connection.execute("""
              INSERT INTO report_delivery_channel_test_action(client_action_id, child_id, channel, status, created_at)
              VALUES (?, ?, ?, 'processing', ?)
            """, (client_action_id, child_id, channel, now))
            if selection is not None:
                if channel == 'email' and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_report_email_verification'").fetchone():
                    connection.execute('DELETE FROM daily_report_email_verification WHERE child_id=? AND recipient_id=?', (child_id,selection['recipient_id']))
                self._record_channel_test_selection(connection, child_id, client_action_id, selection, now)
            connection.execute("""
              UPDATE report_delivery_channel_test_action
              SET status = 'superseded', message = '家长已明确发起新的测试；旧测试结果未知，可能已发出。', tested_at = ?, replacement_action_id = ?
              WHERE client_action_id = ? AND child_id = ? AND channel = ?
            """, (now, client_action_id, previous_action_id, child_id, channel))
            connection.execute("""
              INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
              VALUES (?, 'parent', 'report_delivery_channel_test_reissued', ?, ?, ?)
            """, (str(uuid4()), child_id, json.dumps({"channel": channel}, ensure_ascii=False), now))
            connection.commit()
        return self._run_channel_test(child_id, channel, client_action_id)

    def _run_channel_test(self, child_id: str, channel: str, client_action_id: str) -> dict:
        with self._external_effect_guard():
            return self._run_channel_test_guarded(child_id, channel, client_action_id)

    def _run_channel_test_guarded(self, child_id: str, channel: str, client_action_id: str) -> dict:
        if channel == "wechat_work_bot":
            try:
                config = self._config(channel)
                configured = bool(str(config.get("webhook_url") or "").startswith("https://"))
            except ChannelDeliveryError:
                configured = False
            message = (
                "企业微信配置已保存，但本机只完成配置校验，未发送测试消息。需要企业组织的机器人 Webhook、群成员授权和外部网络后才能真实发送；个人微信不受支持。"
                if configured else
                "尚未保存有效的企业微信机器人配置。个人微信没有稳定公开 API，系统不会伪造或自动化发送。"
            )
            return self._complete_channel_test_action(child_id, channel, client_action_id, "blocked", message)

        with self.database.connect() as connection:
            selection = self._channel_test_selection(connection, child_id, client_action_id)
            recipient = self._test_recipient(child_id, channel, selection['recipient_id']) if selection else None
        if selection and recipient and selection.get('address_tag') != self._channel_test_address_tag(child_id, recipient):
            recipient = None
        if not recipient:
            return self._complete_channel_test_action(
                child_id, channel, client_action_id, "failed", "请先添加并授权一个邮件收件人；系统只会向该孩子的已授权白名单发送测试邮件。"
            )
        try:
            with self.database.connect() as connection:
                revision = connection.execute(
                    'SELECT credential_id, updated_at, encrypted_json FROM notification_credential WHERE channel=?', (channel,)
                ).fetchone()
                expected_revision = tuple(revision) if revision else None
            config = self._config(channel)
            self.adapters[channel].send(
                recipient["address"],
                "家庭学习报告发送测试",
                "这是一封本机发送测试，不含孩子学习内容或个人信息。",
                "<p>这是一封本机发送测试，不含孩子学习内容或个人信息。</p>",
                config,
            )
        except ChannelDeliveryError as error:
            return self._complete_channel_test_action(child_id, channel, client_action_id,
                "unknown" if error.delivery_unknown else "failed",
                "提交结果未知，请核对收件箱；系统不会自动重发。" if error.delivery_unknown else _channel_test_message(error.code))
        accepted_by = "本机 SMTP sink" if _is_local_smtp_host(str(config.get("host") or "")) else "SMTP 服务"
        return self._complete_channel_test_action(
            child_id, channel, client_action_id, "passed", f"{accepted_by}已接受发给已授权“{recipient['label']}”的不含孩子数据的测试邮件；这不代表外部邮箱已阅读或最终送达。",
            expected_revision=expected_revision, expected_recipient=recipient, expected_password=config.get('password', ''),
        )

    def _test_recipient(self, child_id: str, channel: str, recipient_id: str | None = None) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute("""
              SELECT recipient_id, address, label, consented_at FROM report_delivery_recipient
              WHERE child_id = ? AND channel = ? AND consent_status = 'consented' AND revoked_at IS NULL
                AND (? IS NULL OR recipient_id = ?)
              ORDER BY created_at LIMIT 1
            """, (child_id, channel, recipient_id, recipient_id)).fetchone()
        return dict(row) if row else None

    def _channel_test_address_tag(self, child_id: str, recipient: dict) -> str | None:
        if not isinstance(self.encryption_key,bytes) or len(self.encryption_key) != 32:
            return None
        with self.database.connect() as c:
            generation = c.execute('''SELECT audit_id FROM audit_event WHERE child_id=?
                AND action IN ('report_delivery_recipient_authority_changed','report_delivery_recipient_revoked')
                AND json_extract(summary_json,'$.recipient_id')=? ORDER BY rowid DESC LIMIT 1''',
                (child_id,recipient['recipient_id'])).fetchone()
        payload = json.dumps(['channel_test_recipient_v1',child_id,recipient['recipient_id'],
                              recipient['address'].strip().casefold(),recipient['consented_at'],generation[0] if generation else None],
                             ensure_ascii=False,separators=(',', ':')).encode()
        return hmac.new(self.encryption_key,payload,hashlib.sha256).hexdigest()

    @staticmethod
    def _channel_test_selection(connection, child_id: str, client_action_id: str) -> dict | None:
        rows = connection.execute('''SELECT summary_json FROM audit_event
            WHERE child_id=? AND action='report_delivery_test_recipient_selected'
              AND json_extract(summary_json,'$.client_action_id')=?''', (child_id,client_action_id)).fetchall()
        if not rows: return None
        if len(rows) != 1: raise ChannelTestActionConflictError('测试收件人记录不唯一，已阻止重发。')
        try:
            data = json.loads(rows[0]['summary_json'])
            if not isinstance(data,dict) or not isinstance(data.get('recipient_id'),str): raise ValueError()
            return data
        except (TypeError, ValueError):
            raise ChannelTestActionConflictError('测试收件人记录无效，已阻止重发。') from None

    @staticmethod
    def _record_channel_test_selection(connection, child_id, client_action_id, selection, now):
        data = {'client_action_id':client_action_id,'recipient_id':selection['recipient_id'],
                'address_tag':selection.get('address_tag')}
        connection.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
            VALUES(?,'parent','report_delivery_test_recipient_selected',?,?,?)''',
            (str(uuid4()),child_id,json.dumps(data,ensure_ascii=False),now))

    def _reserve_channel_test_action(self, child_id: str, channel: str, client_action_id: str, recipient_id: str | None = None) -> dict | None:
        """Reserve the action before network I/O so a lost response cannot resend it."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("""
              SELECT child_id, channel, status, message, tested_at
              FROM report_delivery_channel_test_action WHERE client_action_id = ?
            """, (client_action_id,)).fetchone()
            if existing:
                connection.rollback()
                if existing["child_id"] != child_id or existing["channel"] != channel:
                    raise ChannelTestActionConflictError("这个发送测试操作已用于另一位孩子或通道，不能重复使用。")
                selection = self._channel_test_selection(connection, child_id, client_action_id)
                if recipient_id is not None and (not selection or selection['recipient_id'] != recipient_id):
                    raise ChannelTestActionConflictError('这个测试操作已绑定其他收件人。')
                if channel == 'email' and existing['status'] == 'passed':
                    from .daily_report_email_verification import verified
                    try:
                        password = self._config('email').get('password', '')
                        connection.execute('BEGIN')
                        current = bool(selection and verified(connection,child_id,selection['recipient_id'],self.encryption_key,password))
                        connection.rollback()
                    except (ChannelDeliveryError,ValueError,sqlite3.Error):
                        current = False
                    if not current:
                        # Preserve the original immutable receipt for older
                        # clients, without treating it as present-day approval.
                        return {'channel':channel,'status':'passed','message':'原测试当时由服务接受；邮箱授权或发送设置已变化，当前尚未验证，也没有再次发送。',
                                'tested_at':existing['tested_at'],'recipient_id':selection.get('recipient_id') if selection else None,
                                'client_action_id':client_action_id,'historical_only':True,'current_verified':False}
                unknown = existing["status"] in {"unknown", "unknown_acknowledged"}
                return {
                    "channel": channel, "status": "unknown" if unknown else existing["status"],
                    "message": existing["message"] or "这次发送测试仍在处理中；为避免重复投递，系统没有再次发送。",
                    "tested_at": existing["tested_at"],
                    "recipient_id": selection.get('recipient_id') if selection else None,
                    "client_action_id":client_action_id,
                    **({"client_action_id": client_action_id, "kept_without_resend": existing["status"] == "unknown_acknowledged"} if unknown else {}),
                }
            selected = self._test_recipient(child_id,channel,recipient_id) if channel == 'email' else None
            if recipient_id is not None and not selected:
                raise DeliveryStateError('该孩子的指定邮箱尚未授权。')
            connection.execute("""
              INSERT INTO report_delivery_channel_test_action(client_action_id, child_id, channel, status, created_at)
              VALUES (?, ?, ?, 'processing', ?)
            """, (client_action_id, child_id, channel, utc_now()))
            if selected:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_report_email_verification'").fetchone():
                    connection.execute('DELETE FROM daily_report_email_verification WHERE child_id=? AND recipient_id=?', (child_id,selected['recipient_id']))
                self._record_channel_test_selection(connection,child_id,client_action_id,
                    {'recipient_id':selected['recipient_id'],'address_tag':self._channel_test_address_tag(child_id,selected)},utc_now())
            connection.commit()
        return None

    def _complete_channel_test_action(self, child_id: str, channel: str, client_action_id: str, status: str, message: str,
                                      *, expected_revision: tuple | None = None, expected_recipient: dict | None = None,
                                      expected_password: str | None = None) -> dict:
        if type(status) is not str or status not in {'passed', 'failed', 'blocked', 'unknown'}:
            raise ValueError('不支持的邮件测试结果状态。')
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            action = connection.execute('''SELECT status,message,tested_at FROM report_delivery_channel_test_action
                WHERE client_action_id=? AND child_id=? AND channel=?''', (client_action_id,child_id,channel)).fetchone()
            selection = self._channel_test_selection(connection,child_id,client_action_id)
            selected_id = selection.get('recipient_id') if selection else None
            if not action or action['status'] != 'processing':
                connection.rollback()
                if not action: raise DeliveryStateError('CHANNEL_TEST_ACTION_MISSING')
                return {'channel':channel, 'status':'unknown' if action['status'] in {'unknown','unknown_acknowledged'} else action['status'],
                        'message':action['message'], 'tested_at':action['tested_at'], 'recipient_id':selected_id,
                        'client_action_id':client_action_id}
            if status == 'passed':
                current = connection.execute(
                    'SELECT credential_id, updated_at, encrypted_json FROM notification_credential WHERE channel=?', (channel,)
                ).fetchone()
                if expected_revision is None or current is None or tuple(current) != expected_revision:
                    status = 'blocked'
                    message = '测试期间发送配置发生变化；这次邮件可能已被原通道接受，但不能验证当前配置，请明确发起新的固定测试。'
                if channel == 'email':
                    from .daily_report_email_verification import secret_fingerprint
                    try:
                        if not isinstance(expected_password, str) or self._config('email').get('password', '') != expected_password:
                            raise ValueError('SECRET_CHANGED')
                        secret_version = secret_fingerprint(self.encryption_key, expected_password)
                    except (ChannelDeliveryError, ValueError, UnicodeError):
                        status = 'blocked'
                        message = '测试期间发送凭据发生变化，不能验证当前配置。'
                    recipient = connection.execute('''SELECT child_id,channel,address,consent_status,consented_at,revoked_at
                        FROM report_delivery_recipient WHERE recipient_id=?''',
                        ((expected_recipient or {}).get('recipient_id'),)).fetchone()
                    if (not recipient or recipient['child_id'] != child_id or recipient['channel'] != 'email'
                            or recipient['consent_status'] != 'consented' or recipient['revoked_at'] is not None
                            or not recipient['consented_at'] or recipient['consented_at'] != expected_recipient.get('consented_at')
                            or recipient['address'] != expected_recipient.get('address')):
                        status = 'blocked'
                        message = '测试期间收件人或授权发生变化，不能验证当前收件人。'
            # The current-verification summary has three states; the original
            # action retains the truthful unknown result and prevents resends.
            summary_status = 'blocked' if status == 'unknown' else status
            connection.execute("""
              INSERT INTO report_delivery_channel_test(child_id, channel, status, message, tested_at)
              VALUES (?, ?, ?, ?, ?)
              ON CONFLICT(child_id, channel) DO UPDATE SET status = excluded.status,
                message = excluded.message, tested_at = excluded.tested_at
            """, (child_id, channel, summary_status, message[:500], now))
            connection.execute("""
              UPDATE report_delivery_channel_test_action
              SET status = ?, message = ?, tested_at = ?
              WHERE client_action_id = ? AND child_id = ? AND channel = ?
            """, (status, message[:500], now, client_action_id, child_id, channel))
            if status == 'passed' and channel == 'email' and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_report_email_verification'").fetchone():
                connection.execute('''INSERT INTO daily_report_email_verification
                    (child_id,recipient_id,address_identity,credential_id,credential_updated_at,credential_ciphertext_sha256,recipient_consented_at,secret_fingerprint,action_id,tested_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(child_id,recipient_id) DO UPDATE SET
                    address_identity=excluded.address_identity,credential_id=excluded.credential_id,
                    credential_updated_at=excluded.credential_updated_at,credential_ciphertext_sha256=excluded.credential_ciphertext_sha256,recipient_consented_at=excluded.recipient_consented_at,secret_fingerprint=excluded.secret_fingerprint,
                    action_id=excluded.action_id,tested_at=excluded.tested_at''',
                    (child_id, expected_recipient['recipient_id'], expected_recipient['address'].strip().casefold(),
                     expected_revision[0], expected_revision[1], hashlib.sha256(expected_revision[2].encode('utf-8')).hexdigest(),
                     expected_recipient['consented_at'], secret_version, client_action_id, now))
            connection.execute("""
              INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
              VALUES (?, 'parent', 'report_delivery_channel_tested', ?, ?, ?)
            """, (str(uuid4()), child_id, json.dumps({"channel": channel, "status": status}, ensure_ascii=False), now))
            connection.commit()
        return {"channel": channel, "status": status, "message": message[:500], "tested_at": now,
                "recipient_id": selected_id, "client_action_id":client_action_id}

    def add_recipient(self, child_id: str, channel: str, address: str, label: str, consented: bool) -> dict:
        # Replacing an existing row with pending consent also revokes sending
        # authority and must serialize with SMTP's final disclosure check.
        with self._external_effect_guard(timeout=10):
            return self._add_recipient_guarded(child_id, channel, address, label, consented)

    def _add_recipient_guarded(self, child_id: str, channel: str, address: str, label: str, consented: bool) -> dict:
        if channel not in self.adapters: raise ValueError("不支持的送达通道。")
        address = address.strip()
        if not address: raise ValueError("收件地址不能为空。")
        now = utc_now()
        with self.database.connect() as connection:
            existing = connection.execute("SELECT recipient_id FROM report_delivery_recipient WHERE child_id = ? AND channel = ? AND address = ?", (child_id, channel, address)).fetchone()
            recipient_id = existing["recipient_id"] if existing else str(uuid4())
            if existing and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_report_email_verification'").fetchone():
                connection.execute('DELETE FROM daily_report_email_verification WHERE child_id=? AND recipient_id=?', (child_id,recipient_id))
            # Any explicit re-save is a new authority decision, even if the
            # clock repeats a timestamp. An old frozen disclosure cannot revive.
            if existing and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_observation_report_snapshot'").fetchone():
                connection.execute('DELETE FROM daily_observation_report_snapshot WHERE child_id=? AND recipient_id=?', (child_id,recipient_id))
            status = "consented" if consented else "pending"
            connection.execute("""
              INSERT INTO report_delivery_recipient(recipient_id, child_id, channel, address, label, consent_status, consented_at, created_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(child_id, channel, address) DO UPDATE SET label = excluded.label, consent_status = excluded.consent_status,
                consented_at = CASE WHEN excluded.consent_status = 'consented' THEN excluded.consented_at ELSE report_delivery_recipient.consented_at END,
                revoked_at = CASE WHEN excluded.consent_status = 'consented' THEN NULL ELSE report_delivery_recipient.revoked_at END
            """, (recipient_id, child_id, channel, address, label.strip()[:100] or address, status, now if consented else None, now))
            connection.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                VALUES(?,'parent','report_delivery_recipient_authority_changed',?,?,?)''',
                (str(uuid4()),child_id,json.dumps({'recipient_id':recipient_id}),now))
            connection.commit()
        return self.get_recipient(recipient_id)

    def get_recipient(self, recipient_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT recipient_id, child_id, channel, address, label, consent_status, consented_at, revoked_at, created_at FROM report_delivery_recipient WHERE recipient_id = ?", (recipient_id,)).fetchone()
        if not row: raise UnknownRecipientError(recipient_id)
        return dict(row)

    def list_recipients(self, child_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT recipient_id FROM report_delivery_recipient WHERE child_id = ? ORDER BY created_at", (child_id,)).fetchall()
        return [self.get_recipient(row["recipient_id"]) for row in rows]

    def revoke_recipient(self, recipient_id: str) -> dict:
        # Serialize the parent's revocation with SMTP's last disclosure check.
        with self._external_effect_guard(timeout=10):
            return self._revoke_recipient_guarded(recipient_id)

    def _revoke_recipient_guarded(self, recipient_id: str) -> dict:
        with self.database.connect() as connection:
            recipient = connection.execute(
                "SELECT child_id FROM report_delivery_recipient WHERE recipient_id = ?", (recipient_id,)
            ).fetchone()
            if not recipient:
                raise UnknownRecipientError(recipient_id)
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_report_email_verification'").fetchone():
                connection.execute('DELETE FROM daily_report_email_verification WHERE child_id=? AND recipient_id=?', (recipient['child_id'],recipient_id))
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_observation_report_snapshot'").fetchone():
                connection.execute('DELETE FROM daily_observation_report_snapshot WHERE child_id=? AND recipient_id=?', (recipient['child_id'],recipient_id))
            now = utc_now()
            connection.execute("UPDATE report_delivery_recipient SET consent_status = 'revoked', revoked_at = ? WHERE recipient_id = ?", (now, recipient_id))
            connection.execute("""
              UPDATE report_delivery_outbox
              SET status = 'cancelled', cancelled_at = ?, cancel_reason = 'recipient_consent_revoked', updated_at = ?
              WHERE recipient_id = ? AND status = 'scheduled'
            """, (now, now, recipient_id))
            connection.execute("""
              INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
              VALUES (?, 'parent', 'report_delivery_recipient_revoked', ?, ?, ?)
            """, (str(uuid4()), recipient["child_id"], json.dumps({"recipient_id": recipient_id}, ensure_ascii=False), now))
            connection.commit()
        return self.get_recipient(recipient_id)

    def update_preferences(self, child_id: str, quiet_start: str, quiet_end: str, retention_days: int, redaction_level: str = "standard") -> dict:
        # A change to minimal disclosure must not race an in-flight email.
        with self._external_effect_guard(timeout=10):
            return self._update_preferences_guarded(child_id, quiet_start, quiet_end, retention_days, redaction_level)

    def _update_preferences_guarded(self, child_id: str, quiet_start: str, quiet_end: str, retention_days: int, redaction_level: str = "standard") -> dict:
        if not _valid_time(quiet_start) or not _valid_time(quiet_end): raise ValueError("安静时段必须为 HH:MM。")
        if redaction_level not in {"standard", "minimal"}: raise ValueError("脱敏级别无效。")
        with self.database.connect() as connection:
            connection.execute("""
              INSERT INTO report_delivery_preference(child_id, quiet_start, quiet_end, retention_days, redaction_level, updated_at)
              VALUES (?, ?, ?, ?, ?, ?)
              ON CONFLICT(child_id) DO UPDATE SET quiet_start = excluded.quiet_start, quiet_end = excluded.quiet_end,
                retention_days = excluded.retention_days, redaction_level = excluded.redaction_level, updated_at = excluded.updated_at
            """, (child_id, quiet_start, quiet_end, max(7, min(retention_days, 3650)), redaction_level, utc_now()))
            connection.commit()
        return self.preferences(child_id)

    def preferences(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT child_id, quiet_start, quiet_end, timezone, retention_days, redaction_level, updated_at FROM report_delivery_preference WHERE child_id = ?", (child_id,)).fetchone()
        return dict(row) if row else {"child_id": child_id, "quiet_start": "21:00", "quiet_end": "07:00", "timezone": "Asia/Shanghai", "retention_days": 90, "redaction_level": "standard", "updated_at": None}

    def _visual_daily_delivery_uncertain(self, connection, snapshot_id: str, recipient_id: str) -> bool:
        """The visual ledger constrains every daily send, including manual sends."""
        from datetime import date
        current = connection.execute('''SELECT s.child_id,s.report_type,d.report_date,r.child_id AS recipient_child,r.address
          FROM parent_report_snapshot s LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
          LEFT JOIN report_delivery_recipient r ON r.recipient_id=? WHERE s.snapshot_id=?''', (recipient_id,snapshot_id)).fetchone()
        if not current or current['report_type'] != 'daily':
            return False
        try:
            day = current['report_date']
            if (not isinstance(day,str) or date.fromisoformat(day).isoformat() != day
                    or current['recipient_child'] != current['child_id'] or not isinstance(current['address'],str)
                    or not current['address'].strip()):
                return True
            address = current['address'].strip().casefold()
            from .daily_report_delivery_state import assert_ledger_key, identity_key
            barrier = connection.execute('SELECT blocked FROM daily_observation_delivery_barrier WHERE singleton_id=1').fetchone()
            if not barrier or barrier['blocked'] != 0:
                return True
            assert_ledger_key(connection,self.encryption_key)
            token = identity_key(self.encryption_key,current['child_id'],day,address)
            if connection.execute("SELECT 1 FROM daily_observation_delivery_ledger WHERE identity=? AND status IN ('processing','unknown','sent')", (token,)).fetchone():
                return True
        except (ValueError,TypeError,sqlite3.Error):
            return True
        return False

    def _uncertain_daily_version(self, connection, snapshot_id: str, recipient_id: str, delivery_id: str = '', ignored_delivery_ids=()) -> bool:
        """Conservatively block any same-child/day/address prior submission."""
        if self._visual_daily_delivery_uncertain(connection, snapshot_id, recipient_id):
            return True
        current = connection.execute('''SELECT s.child_id,s.report_type,d.report_date,r.child_id AS recipient_child,r.address
          FROM parent_report_snapshot s LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
          LEFT JOIN report_delivery_recipient r ON r.recipient_id=? WHERE s.snapshot_id=?''', (recipient_id,snapshot_id)).fetchone()
        if not current or current['report_type'] != 'daily':
            return False
        day = current['report_date']
        address = current['address'].strip().casefold()
        previous = connection.execute('''SELECT o.delivery_id,d.report_date,r.child_id AS recipient_child,r.address
          FROM parent_report_snapshot s JOIN report_delivery_outbox o ON o.snapshot_id=s.snapshot_id
          LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
          LEFT JOIN report_delivery_recipient r ON r.recipient_id=o.recipient_id
          WHERE s.child_id=? AND s.report_type='daily' AND o.delivery_id!=?
            AND (o.status IN ('sent','processing','unknown','unknown_acknowledged','superseded')
                 OR (o.status IN ('failed','cancelled') AND o.attempt_count>0))''',
          (current['child_id'],delivery_id)).fetchall()
        from .daily_report_observation import _is_canonical_day
        return any(row['delivery_id'] not in ignored_delivery_ids
                   and (not _is_canonical_day(row['report_date']) or row['report_date'] == day)
                   and (row['recipient_child'] != current['child_id'] or not isinstance(row['address'],str)
                        or row['address'].strip().casefold() == address) for row in previous)

    def _automatic_email_recipient_verified(self, connection, child_id: str, recipient_id: str) -> bool:
        """Check the exact recipient and current credentials in the caller's transaction."""
        from .daily_report_email_verification import verified
        if not connection.in_transaction:
            raise DeliveryStateError('自动送达验证需要数据库事务。')
        try:
            config = self._config('email')
            return verified(connection,child_id,recipient_id,self.encryption_key,config.get('password') or '')
        except (ChannelDeliveryError, ValueError, sqlite3.Error, TypeError):
            return False

    @staticmethod
    def _explicit_reissue_ancestry(connection, row):
        """Recognize only the exact parent-authorized chain, never a prefix alone."""
        if not row['idempotency_key'].startswith('reissue:'):
            return ()
        current = row
        ancestors = []
        seen = {row['delivery_id']}
        for _ in range(100):
            action = current['reissue_action_id']
            if not isinstance(action,str) or not 8 <= len(action) <= 120:
                return None
            previous = connection.execute('''SELECT * FROM report_delivery_outbox WHERE replacement_delivery_id=?
                AND snapshot_id=? AND recipient_id=? AND channel=? AND status='superseded' ''',
                (current['delivery_id'],row['snapshot_id'],row['recipient_id'],row['channel'])).fetchall()
            if len(previous) != 1:
                return None
            original = previous[0]
            if original['delivery_id'] in seen or current['idempotency_key'] != f"reissue:{original['delivery_id']}:{action}":
                return None
            evidence = connection.execute('''SELECT 1 FROM audit_event WHERE child_id=? AND actor='parent'
                AND action='report_delivery_unknown_explicit_reissue'
                AND json_extract(summary_json,'$.delivery_id')=?
                AND json_extract(summary_json,'$.replacement_delivery_id')=? LIMIT 2''',
                (row['snapshot_child_id'],original['delivery_id'],current['delivery_id'])).fetchall()
            if len(evidence) != 1:
                return None
            ancestors.append(original['delivery_id']); seen.add(original['delivery_id'])
            if not original['idempotency_key'].startswith('reissue:'):
                return tuple(ancestors)
            current = original
        return None

    @staticmethod
    def _requires_email_verification(row, explicit_reissue=False) -> bool:
        # Old queues predate the auto/manual distinction; their origin cannot
        # be inferred safely from an unmarked idempotency key.
        return row['channel'] == 'email' and not explicit_reissue and not row['idempotency_key'].startswith('manual:')

    def enqueue(self, snapshot_id: str, recipient_id: str, scheduled_at: str | None = None, *, automatic: bool = False) -> dict:
        now = utc_now()
        run_at = scheduled_at or now
        legacy_idem = f"{snapshot_id}:{recipient_id}:{run_at[:16]}" if scheduled_at else f"initial:{snapshot_id}:{recipient_id}"
        idem = f"{'auto' if automatic else 'manual'}:{legacy_idem}"
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            snapshot = connection.execute('SELECT child_id,status FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot_id,)).fetchone()
            if not snapshot:
                raise UnknownReportSnapshotError(snapshot_id)
            if snapshot['status'] != 'approved':
                raise SnapshotStateError("只有已批准的报告才能发送。")
            recipient = connection.execute('SELECT * FROM report_delivery_recipient WHERE recipient_id=?',(recipient_id,)).fetchone()
            if not recipient:
                raise UnknownRecipientError(recipient_id)
            if recipient['child_id'] != snapshot['child_id'] or recipient['consent_status'] != 'consented':
                raise DeliveryStateError("收件人不在该孩子的已授权白名单中。")
            from .daily_report_formal_ownership import snapshot_owned
            if snapshot_owned(connection,snapshot_id,recipient_id):
                raise DeliveryStateError('该日报已由综合日报统一处理。')
            if automatic and (recipient['channel'] != 'email' or not self._automatic_email_recipient_verified(connection,snapshot['child_id'],recipient_id)):
                raise DeliveryStateError('该收件邮箱当前尚未完成独立发送测试。')
            existing = connection.execute("SELECT delivery_id,status,cancel_reason,last_error_code FROM report_delivery_outbox WHERE idempotency_key IN (?,?) ORDER BY rowid DESC LIMIT 1", (idem,legacy_idem)).fetchone()
            # A normal click/replay is not a new send, even across minutes,
            # restarts or legacy time-keyed queues. Retry/reissue stay explicit.
            if scheduled_at is None:
                existing = connection.execute('''SELECT delivery_id,status,cancel_reason,last_error_code FROM report_delivery_outbox
                    WHERE snapshot_id=? AND recipient_id=? ORDER BY rowid DESC LIMIT 1''',
                    (snapshot_id,recipient_id)).fetchone()
            if not existing and connection.execute('''SELECT 1 FROM report_delivery_outbox
                    WHERE snapshot_id=? AND recipient_id=? AND status IN ('processing','unknown','unknown_acknowledged')''',
                    (snapshot_id,recipient_id)).fetchone():
                raise DeliveryStateError('已有发送仍在处理或结果未知；请先核对，并使用原记录的明确重发流程。')
            if not existing and automatic and self._uncertain_daily_version(connection, snapshot_id, recipient_id):
                raise DeliveryStateError('同一日报其他版本的发送结果尚未确认。')
            reactivated = False
            if automatic and existing and existing['status'] == 'cancelled' and existing['cancel_reason'] == 'recipient_email_test_required':
                marker = existing['last_error_code']
                proven = isinstance(marker,str) and marker.startswith('AUTO_VERIFY_BLOCKED:') and connection.execute('''
                    SELECT 1 FROM audit_event WHERE child_id=? AND action='report_delivery_auto_verification_blocked'
                      AND json_extract(summary_json,'$.delivery_id')=? AND json_extract(summary_json,'$.marker')=? LIMIT 1''',
                    (snapshot['child_id'],existing['delivery_id'],marker)).fetchone()
                if proven and (connection.execute('''SELECT 1 FROM report_delivery_outbox WHERE snapshot_id=? AND recipient_id=? AND delivery_id!=?
                    AND status IN ('processing','unknown','unknown_acknowledged') LIMIT 1''', (snapshot_id,recipient_id,existing['delivery_id'])).fetchone() or self._uncertain_daily_version(connection,snapshot_id,recipient_id,existing['delivery_id'])):
                    raise DeliveryStateError('已有发送结果尚未确认。')
                if proven:
                    reactivated = bool(connection.execute('''UPDATE report_delivery_outbox SET status='scheduled',scheduled_at=?,
                        cancel_reason=NULL,cancelled_at=NULL,last_error_code=NULL,last_error_message=NULL,updated_at=? WHERE delivery_id=? AND status='cancelled'
                        AND cancel_reason='recipient_email_test_required' AND last_error_code=? ''', (run_at,now,existing['delivery_id'],marker)).rowcount)
                    connection.commit()
            created = not existing
            if created:
                connection.execute("""INSERT INTO report_delivery_outbox(delivery_id, snapshot_id, recipient_id, channel, idempotency_key, status, scheduled_at, created_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)""", (str(uuid4()), snapshot_id, recipient_id, recipient["channel"], idem, run_at, now, now))
                connection.commit()
                delivery_id = connection.execute("SELECT delivery_id FROM report_delivery_outbox WHERE idempotency_key = ?", (idem,)).fetchone()["delivery_id"]
            else: delivery_id = existing["delivery_id"]
        result = self.get_delivery(delivery_id)
        if automatic:
            result['created'] = bool(created or reactivated)
        return result

    def cancel(self, delivery_id: str, reason: str) -> dict:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM report_delivery_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            if not row: raise KeyError(delivery_id)
            if row["status"] not in {"scheduled", "failed", "cancelled"}:
                raise DeliveryStateError("已提交或结果未知的发送不能改为取消；请使用核对或明确重发流程。")
            connection.execute("UPDATE report_delivery_outbox SET status = 'cancelled', cancelled_at = ?, cancel_reason = ?, last_error_code=NULL,last_error_message=NULL,updated_at = ? WHERE delivery_id = ?", (utc_now(), reason.strip()[:300], utc_now(), delivery_id))
            connection.commit()
        return self.get_delivery(delivery_id)

    def retry(self, delivery_id: str) -> dict:
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("""SELECT o.status, s.status AS snapshot_status, s.revoke_reason
              FROM report_delivery_outbox o JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id
              WHERE o.delivery_id = ?""", (delivery_id,)).fetchone()
            if not row: raise KeyError(delivery_id)
            if row["snapshot_status"] != "approved":
                raise DeliveryStateError("这份报告已有后续证据更新，不能重新发送旧版本。")
            if row["status"] not in {"failed", "cancelled"}: raise DeliveryStateError("只有失败或取消的发送可以重新排队。")
            connection.execute("UPDATE report_delivery_outbox SET status = 'scheduled', scheduled_at = ?, last_error_code = NULL, last_error_message = NULL, updated_at = ? WHERE delivery_id = ?", (utc_now(), utc_now(), delivery_id))
            connection.commit()
        return self.get_delivery(delivery_id)

    def keep_unknown(self, delivery_id: str) -> dict:
        """Record the parent's no-resend choice without rewriting the uncertain attempt."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
              SELECT o.status, s.child_id FROM report_delivery_outbox o
              JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id
              WHERE o.delivery_id = ?
            """, (delivery_id,)).fetchone()
            if not row:
                connection.rollback(); raise KeyError(delivery_id)
            if row["status"] == "unknown":
                now = utc_now()
                connection.execute("""
                  UPDATE report_delivery_outbox
                  SET status = 'unknown_acknowledged', cancel_reason = 'parent_kept_unknown_without_resend', updated_at = ?
                  WHERE delivery_id = ? AND status = 'unknown'
                """, (now, delivery_id))
                connection.execute("""
                  INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                  VALUES (?, 'parent', 'report_delivery_unknown_kept_without_resend', ?, ?, ?)
                """, (str(uuid4()), row["child_id"], json.dumps({"delivery_id": delivery_id}, ensure_ascii=False), now))
            elif row["status"] != "unknown_acknowledged":
                connection.rollback(); raise DeliveryStateError("只有结果未知的发送可以选择保持不重发。")
            connection.commit()
        return self.get_delivery(delivery_id)

    def reissue_unknown(self, delivery_id: str, client_action_id: str) -> dict:
        """Create one explicit replacement after an unknown SMTP outcome.

        The new operation is intentionally a distinct delivery row.  One SQLite
        immediate transaction claims the old unknown result and binds the action
        identifier, so a duplicate click/replay returns the same replacement and
        two concurrent different actions cannot both send the report.
        """
        action = client_action_id.strip()
        if len(action) < 8 or len(action) > 120:
            raise DeliveryStateError("重发操作标识无效。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
              SELECT o.*, r.child_id AS recipient_child_id, r.consent_status, s.child_id AS snapshot_child_id,
                     s.status AS snapshot_status
              FROM report_delivery_outbox o
              JOIN report_delivery_recipient r ON r.recipient_id = o.recipient_id
              JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id
              WHERE o.delivery_id = ?
            """, (delivery_id,)).fetchone()
            if not row:
                connection.rollback(); raise KeyError(delivery_id)
            if row["replacement_delivery_id"]:
                replacement = connection.execute("""
                  SELECT delivery_id, reissue_action_id FROM report_delivery_outbox WHERE delivery_id = ?
                """, (row["replacement_delivery_id"],)).fetchone()
                if replacement and replacement["reissue_action_id"] == action:
                    connection.commit(); return self.get_delivery(replacement["delivery_id"])
                connection.rollback(); raise DeliveryStateError("这条结果未知的发送已由另一项明确重发处理，不能再次创建。")
            claimed = connection.execute("SELECT delivery_id FROM report_delivery_outbox WHERE reissue_action_id = ?", (action,)).fetchone()
            if claimed:
                connection.rollback(); raise DeliveryStateError("这项明确重发已用于另一条报告，不能跨报告复用。")
            if row["status"] not in {"unknown", "unknown_acknowledged"}:
                connection.rollback(); raise DeliveryStateError("只有结果未知的发送可以明确创建新重发。")
            if row["recipient_child_id"] != row["snapshot_child_id"] or row["consent_status"] != "consented":
                connection.rollback(); raise DeliveryStateError("收件人授权已变更，不能重新发送。")
            if row["snapshot_status"] != "approved":
                connection.rollback(); raise DeliveryStateError("这份报告已有后续证据更新，不能重新发送旧版本。")
            snapshot = connection.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (row['snapshot_id'],)).fetchone()
            if not self.snapshots.snapshot_source_current(connection, snapshot):
                raise DeliveryStateError('报告来源已变化，请先审核更正版本，不能授权重发旧报告。')
            now = utc_now()
            replacement_id = str(uuid4())
            # The explicit replacement is the sole new authorization. Older
            # pending duplicates must not wake once the unknown row is superseded.
            cancelled_pending = connection.execute("""UPDATE report_delivery_outbox
                SET status='cancelled',cancelled_at=?,cancel_reason='prior_delivery_unresolved',updated_at=?
                WHERE snapshot_id=? AND recipient_id=? AND delivery_id!=? AND status IN ('scheduled','failed')""",
                (now,now,row['snapshot_id'],row['recipient_id'],delivery_id)).rowcount
            connection.execute("""
              INSERT INTO report_delivery_outbox(delivery_id, snapshot_id, recipient_id, channel, idempotency_key, status, scheduled_at, reissue_action_id, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?)
            """, (replacement_id, row["snapshot_id"], row["recipient_id"], row["channel"], f"reissue:{delivery_id}:{action}", now, action, now, now))
            connection.execute("""
              UPDATE report_delivery_outbox
              SET status = 'superseded', replacement_delivery_id = ?, updated_at = ?
              WHERE delivery_id = ? AND status IN ('unknown', 'unknown_acknowledged')
            """, (replacement_id, now, delivery_id))
            connection.execute("""
              INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
              VALUES (?, 'parent', 'report_delivery_unknown_explicit_reissue', ?, ?, ?)
            """, (str(uuid4()), row["snapshot_child_id"], json.dumps({"delivery_id": delivery_id, "replacement_delivery_id": replacement_id,
                "cancelled_pending_delivery_count":cancelled_pending}, ensure_ascii=False), now))
            connection.commit()
        return self.get_delivery(replacement_id)

    def process_due(self, limit: int = 20, max_workers: int = 2) -> list[dict]:
        """Submit a bounded batch without turning a family PC into a mail worker.

        Each worker acquires the database external-effect guard through its final
        receipt. Same-database sends serialize with restore; the dispatching thread
        must not hold that guard while waiting for its workers.
        """
        now = utc_now()
        with self.database.connect() as connection:
            rows = connection.execute("SELECT delivery_id FROM report_delivery_outbox WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at LIMIT ?", (now, max(1, min(limit, 100)))).fetchall()
        delivery_ids = [row["delivery_id"] for row in rows]
        if len(delivery_ids) < 2:
            return [self._process_one(delivery_id) for delivery_id in delivery_ids]
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 2)), thread_name_prefix="hsc-report-send") as executor:
            return list(executor.map(self._process_one, delivery_ids))

    def _process_one(self, delivery_id: str) -> dict:
        with self._external_effect_guard():
            return self._process_one_guarded(delivery_id)

    def _process_one_guarded(self, delivery_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""SELECT o.*, r.child_id, r.address, r.label, r.consent_status,
              r.channel AS recipient_channel, s.child_id AS snapshot_child_id FROM report_delivery_outbox o
              JOIN report_delivery_recipient r ON r.recipient_id = o.recipient_id
              JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id WHERE o.delivery_id = ?""", (delivery_id,)).fetchone()
            if not row or row["status"] != "scheduled":
                connection.rollback(); return self.get_delivery(delivery_id)
            from .daily_report_formal_ownership import snapshot_owned
            if snapshot_owned(connection,row['snapshot_id'],row['recipient_id']):
                connection.rollback(); return self.get_delivery(delivery_id)
            if connection.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (row['child_id'],)).fetchone():
                later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                connection.execute("UPDATE report_delivery_outbox SET scheduled_at=?,last_error_code='WAITING_DELETION',updated_at=? WHERE delivery_id=?", (later,now,delivery_id))
                connection.commit()
                return self.get_delivery(delivery_id)
            scope_reason = self._submission_scope_reason(row)
            if scope_reason:
                connection.execute("""UPDATE report_delivery_outbox SET status='cancelled',
                    cancelled_at=?,cancel_reason=?,updated_at=? WHERE delivery_id=?""",
                    (now, scope_reason, now, delivery_id))
                connection.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                    VALUES (?, 'system', 'report_delivery_cancelled_before_submission', ?, ?, ?)""",
                    (str(uuid4()), row['snapshot_child_id'], json.dumps({'delivery_id': delivery_id, 'reason': scope_reason}), now))
                connection.commit()
                return self.get_delivery(delivery_id)
            ancestry = self._explicit_reissue_ancestry(connection,row)
            if ancestry is None:
                connection.execute("UPDATE report_delivery_outbox SET status='cancelled',cancel_reason='invalid_reissue_provenance',updated_at=? WHERE delivery_id=?", (now,delivery_id))
                connection.commit()
                return self.get_delivery(delivery_id)
            # Historical explicit schedules can predate an uncertain submission.
            # Claiming a different queue row is not permission to bypass it.
            unresolved = connection.execute('''SELECT 1 FROM report_delivery_outbox
                WHERE snapshot_id=? AND recipient_id=? AND delivery_id!=?
                  AND status IN ('processing','unknown','unknown_acknowledged')''',
                (row['snapshot_id'],row['recipient_id'],delivery_id)).fetchone()
            if not unresolved:
                unresolved = (self._uncertain_daily_version(connection, row['snapshot_id'],row['recipient_id'],delivery_id,ancestry)
                    if ancestry or self._requires_email_verification(row)
                    else self._visual_daily_delivery_uncertain(connection,row['snapshot_id'],row['recipient_id']))
            if unresolved:
                connection.execute("""UPDATE report_delivery_outbox SET status='cancelled',
                    cancelled_at=?,cancel_reason='prior_delivery_unresolved',updated_at=? WHERE delivery_id=?""", (now,now,delivery_id))
                connection.commit()
                return self.get_delivery(delivery_id)
            if row["consent_status"] != "consented":
                connection.execute("UPDATE report_delivery_outbox SET status = 'cancelled', cancel_reason = 'recipient_consent_revoked', updated_at = ? WHERE delivery_id = ?", (now, delivery_id)); connection.commit(); return self.get_delivery(delivery_id)
            if self._requires_email_verification(row,bool(ancestry)) and not self._automatic_email_recipient_verified(connection,row['child_id'],row['recipient_id']):
                self._record_verification_blocked(connection,delivery_id,row['child_id'],'scheduled',now)
                connection.commit()
                return self.get_delivery(delivery_id)
            if self._in_quiet_hours(row["child_id"]):
                next_time = _next_quiet_end(self.preferences(row["child_id"])["quiet_end"])
                connection.execute("UPDATE report_delivery_outbox SET scheduled_at = ?, updated_at = ? WHERE delivery_id = ?", (next_time, now, delivery_id)); connection.commit(); return self.get_delivery(delivery_id)
            connection.execute("UPDATE report_delivery_outbox SET status = 'processing', attempt_count = attempt_count + 1, updated_at = ? WHERE delivery_id = ?", (now, delivery_id))
            connection.commit()
        submission_started = False
        try:
            snapshot = self.snapshots.get(row["snapshot_id"])
            if snapshot["status"] != "approved":
                self._cancel_before_submission(delivery_id, "snapshot_not_approved")
                return self.get_delivery(delivery_id)
            if not self.snapshots.validate_current_for_delivery(snapshot["snapshot_id"]):
                self._cancel_before_submission(delivery_id, "snapshot_source_changed")
                return self.get_delivery(delivery_id)
            config = self._config(row["channel"])
            redaction = self.preferences(row["child_id"])["redaction_level"]
            subject, text, html = render_message(snapshot, redaction)

            def submission_check():
                reason = self._submission_block_reason(delivery_id, expected=row)
                if reason:
                    return reason
                try:
                    if self._config(row['channel']) != config:
                        return 'channel_configuration_changed'
                except ChannelDeliveryError:
                    return 'channel_configuration_changed'
                if self.preferences(row['child_id'])['redaction_level'] != redaction:
                    return 'report_disclosure_changed'
                if self._in_quiet_hours(row['child_id']):
                    return 'quiet_hours_started'
                return None

            blocked_reason = submission_check()
            if blocked_reason:
                if blocked_reason in {'quiet_hours_started','deletion_pending'}:
                    self._defer_before_submission(delivery_id,row['child_id'],blocked_reason)
                else:
                    self._cancel_before_submission(delivery_id, blocked_reason)
                return self.get_delivery(delivery_id)
            submission_started = True
            # SMTP connection/TLS/login may be slow. The production adapters run
            # this again immediately before handing the body to their transport.
            with submission_guard(submission_check):
                receipt = self.adapters[row["channel"]].send(row["address"], subject, text, html, config)
            self._finish(delivery_id, "sent", receipt.provider_message_id, None, None)
        except ChannelSubmissionCancelled as error:
            if error.reason in {'quiet_hours_started','deletion_pending'}:
                self._defer_before_submission(delivery_id,row['child_id'],error.reason)
            else:
                self._cancel_before_submission(delivery_id, error.reason)
        except ChannelDeliveryError as error:
            self._fail(delivery_id, error.code, str(error), error.retryable, delivery_unknown=error.delivery_unknown)
        except Exception as error:
            self._fail(delivery_id, type(error).__name__, "发送服务发生未预期错误。", True, delivery_unknown=submission_started)
        return self.get_delivery(delivery_id)

    @staticmethod
    def _submission_scope_reason(row, expected=None) -> str | None:
        if row['child_id'] != row['snapshot_child_id'] or row['channel'] != row['recipient_channel']:
            return 'recipient_snapshot_scope_mismatch'
        if expected is not None and any(row[key] != expected[key] for key in
                ('snapshot_id', 'recipient_id', 'child_id', 'channel', 'address')):
            return 'delivery_envelope_changed'
        return None

    @staticmethod
    def _record_verification_blocked(connection, delivery_id: str, child_id: str, previous_status: str, now: str) -> None:
        marker = 'AUTO_VERIFY_BLOCKED:' + str(uuid4())
        updated = connection.execute('''UPDATE report_delivery_outbox SET status='cancelled',cancelled_at=?,
            cancel_reason='recipient_email_test_required',last_error_code=?,last_error_message=NULL,updated_at=?
            WHERE delivery_id=? AND status=?''', (now,marker,now,delivery_id,previous_status)).rowcount
        if updated:
            connection.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                VALUES(?,'system','report_delivery_auto_verification_blocked',?,?,?)''',
                (str(uuid4()),child_id,json.dumps({'delivery_id':delivery_id,'marker':marker}),now))

    def _defer_before_submission(self, delivery_id: str, child_id: str, reason: str) -> None:
        """Only called when the adapter proves DATA was not submitted."""
        target = (_next_quiet_end(self.preferences(child_id)['quiet_end']) if reason == 'quiet_hours_started'
                  else (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        with self.database.connect() as connection:
            connection.execute('''UPDATE report_delivery_outbox SET status='scheduled',scheduled_at=?,
                attempt_count=MAX(0,attempt_count-1),last_error_code=?,last_error_message=?,updated_at=?
                WHERE delivery_id=? AND status='processing' ''', (target,
                'WAITING_QUIET_HOURS' if reason == 'quiet_hours_started' else 'WAITING_DELETION',
                '静默时段结束后再检查发送。' if reason == 'quiet_hours_started' else '等待当前删除操作结束。',
                utc_now(),delivery_id))
            connection.commit()

    def _submission_block_reason(self, delivery_id: str, expected=None) -> str | None:
        """Recheck durable consent immediately before a network submission.

        A cancellation cannot claw back a provider-accepted message, but this closes
        the normal race where a parent revokes consent while a low-spec worker is
        rendering/configuring a claimed job.
        """
        with self.database.connect() as connection:
            row = connection.execute("""
              SELECT o.*, r.consent_status, r.child_id, r.address, r.channel AS recipient_channel,
                s.child_id AS snapshot_child_id, s.status AS snapshot_status
              FROM report_delivery_outbox o
              JOIN report_delivery_recipient r ON r.recipient_id = o.recipient_id
              JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id
              WHERE o.delivery_id = ?
            """, (delivery_id,)).fetchone()
        if not row or row["status"] != "processing":
            return "delivery_no_longer_processing"
        scope_reason = self._submission_scope_reason(row, expected)
        if scope_reason:
            return scope_reason
        if row["consent_status"] != "consented":
            return "recipient_consent_revoked"
        if row["snapshot_status"] != "approved":
            return "snapshot_not_approved"
        if not self.snapshots.validate_current_for_delivery(row["snapshot_id"]):
            return "snapshot_source_changed"
        # Source refresh may take time; reread approval and consent after it.
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            current = connection.execute("""SELECT o.*,r.consent_status,r.child_id,r.address,
                r.channel AS recipient_channel,s.child_id AS snapshot_child_id,s.status AS snapshot_status
                FROM report_delivery_outbox o JOIN report_delivery_recipient r USING(recipient_id)
                JOIN parent_report_snapshot s USING(snapshot_id) WHERE o.delivery_id=?""", (delivery_id,)).fetchone()
            ancestry = self._explicit_reissue_ancestry(connection,current) if current else ()
            version_unresolved = bool(current and ancestry is not None and (
                self._uncertain_daily_version(connection,current['snapshot_id'],current['recipient_id'],delivery_id,ancestry)
                if ancestry or self._requires_email_verification(current)
                else self._visual_daily_delivery_uncertain(connection,current['snapshot_id'],current['recipient_id'])))
            test_unavailable = bool(current and ancestry is not None and self._requires_email_verification(current,bool(ancestry))
                and not self._automatic_email_recipient_verified(connection,current['child_id'],current['recipient_id']))
            deletion_pending = bool(current and connection.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (current['child_id'],)).fetchone())
        if not current or current["status"] != "processing":
            return "delivery_no_longer_processing"
        scope_reason = self._submission_scope_reason(current, expected if expected is not None else row)
        if scope_reason:
            return scope_reason
        if current["consent_status"] != "consented":
            return "recipient_consent_revoked"
        if current["snapshot_status"] != "approved":
            return "snapshot_not_approved"
        if ancestry is None:
            return 'invalid_reissue_provenance'
        if version_unresolved:
            return 'prior_delivery_unresolved'
        if test_unavailable:
            return 'recipient_email_test_required'
        if deletion_pending:
            return 'deletion_pending'
        return None

    def _cancel_before_submission(self, delivery_id: str, reason: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute("""
              SELECT s.child_id FROM report_delivery_outbox o
              JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id
              WHERE o.delivery_id = ?
            """, (delivery_id,)).fetchone()
            if reason == 'recipient_email_test_required' and row:
                self._record_verification_blocked(connection,delivery_id,row['child_id'],'processing',utc_now())
            else:
                connection.execute("""
              UPDATE report_delivery_outbox
              SET status = 'cancelled', cancelled_at = ?, cancel_reason = ?, updated_at = ?
              WHERE delivery_id = ? AND status = 'processing'
                """, (utc_now(), reason, utc_now(), delivery_id))
            if row:
                connection.execute("""
                  INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                  VALUES (?, 'system', 'report_delivery_cancelled_before_submission', ?, ?, ?)
                """, (str(uuid4()), row["child_id"], json.dumps({"delivery_id": delivery_id, "reason": reason}, ensure_ascii=False), utc_now()))
            connection.commit()

    def _finish(self, delivery_id: str, status: str, provider_id: str | None, code: str | None, message: str | None) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute("""SELECT o.attempt_count, s.child_id FROM report_delivery_outbox o
              JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id WHERE o.delivery_id = ?""", (delivery_id,)).fetchone()
            connection.execute("UPDATE report_delivery_outbox SET status = ?, provider_message_id = ?, sent_at = ?, updated_at = ? WHERE delivery_id = ?", (status, provider_id, now, now, delivery_id))
            connection.execute("INSERT INTO report_delivery_attempt(attempt_id, delivery_id, attempt_number, status, error_code, error_message, provider_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid4()), delivery_id, row["attempt_count"], status, code, message, provider_id, now))
            connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'system', 'report_delivery_sent', ?, ?, ?)", (str(uuid4()), row["child_id"], json.dumps({"delivery_id": delivery_id, "channel_accepted": True}, ensure_ascii=False), now))
            connection.commit()

    def _fail(self, delivery_id: str, code: str, message: str, retryable: bool, *, delivery_unknown: bool = False) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""SELECT o.attempt_count,o.status, s.child_id FROM report_delivery_outbox o
              JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id WHERE o.delivery_id = ?""", (delivery_id,)).fetchone()
            if not row or row['status'] != 'processing':
                return
            attempts = row["attempt_count"]
            status = "unknown" if delivery_unknown else ("scheduled" if retryable and attempts < 5 else "failed")
            scheduled = (datetime.now(timezone.utc) + timedelta(minutes=min(60, 2 ** attempts))).isoformat() if status == "scheduled" else None
            connection.execute("UPDATE report_delivery_outbox SET status = ?, scheduled_at = COALESCE(?, scheduled_at), last_error_code = ?, last_error_message = ?, updated_at = ? WHERE delivery_id = ?", (status, scheduled, code, message[:500], utc_now(), delivery_id))
            connection.execute("INSERT INTO report_delivery_attempt(attempt_id, delivery_id, attempt_number, status, error_code, error_message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid4()), delivery_id, attempts, status, code, message[:500], utc_now()))
            connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'system', 'report_delivery_failed', ?, ?, ?)", (str(uuid4()), row["child_id"], json.dumps({"delivery_id": delivery_id, "error_code": code, "will_retry": status == "scheduled"}, ensure_ascii=False), utc_now()))
            connection.commit()

    def _config(self, channel: str) -> dict:
        if not self.encryption_key: raise ChannelDeliveryError("CREDENTIAL_KEY_UNAVAILABLE", "本地加密密钥不可用。", False)
        with self.database.connect() as connection:
            row = connection.execute("SELECT encrypted_json FROM notification_credential WHERE channel = ?", (channel,)).fetchone()
        if not row: raise ChannelDeliveryError("CHANNEL_NOT_CONFIGURED", "此通道尚未配置。", False)
        try:
            raw = base64.b64decode(row["encrypted_json"])
            config = json.loads(AESGCM(self.encryption_key).decrypt(raw[:12], raw[12:], b"hsc-notification-credential-v1"))
        except Exception as error: raise ChannelDeliveryError("CREDENTIAL_DECRYPT_FAILED", "无法读取加密凭证。", False) from error
        if channel == "email" and self._runtime_secrets.get("email_password"):
            config["password"] = self._runtime_secrets["email_password"]
        if channel == "wechat_work_bot" and self._runtime_secrets.get("wechat_work_bot_webhook"):
            config["webhook_url"] = self._runtime_secrets["wechat_work_bot_webhook"]
        return config

    def _in_quiet_hours(self, child_id: str) -> bool:
        prefs = self.preferences(child_id); current = family_time(datetime.now(timezone.utc)).strftime("%H:%M")
        start, end = prefs["quiet_start"], prefs["quiet_end"]
        return start <= current < end if start < end else current >= start or current < end

    def get_delivery(self, delivery_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM report_delivery_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            attempts = connection.execute("SELECT attempt_number, status, error_code, error_message, provider_message_id, created_at FROM report_delivery_attempt WHERE delivery_id = ? ORDER BY created_at", (delivery_id,)).fetchall()
        if not row: raise KeyError(delivery_id)
        result = dict(row)
        result["attempts"] = [dict(item) for item in attempts]
        result["channel_acceptance"] = "accepted" if row["status"] == "sent" else (
            "unknown" if row["status"] in {"processing", "unknown", "unknown_acknowledged", "superseded"} else "not_confirmed")
        result["human_delivery_verified"] = False
        result["human_read_verified"] = False
        return result

    def list_deliveries(self, snapshot_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT delivery_id FROM report_delivery_outbox WHERE snapshot_id = ? ORDER BY created_at DESC", (snapshot_id,)).fetchall()
        return [self.get_delivery(row["delivery_id"]) for row in rows]

    def child_status_summary(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT o.status, o.last_error_code, COUNT(*) AS total FROM report_delivery_outbox o JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id WHERE s.child_id = ? GROUP BY o.status, o.last_error_code", (child_id,)).fetchall()
        result = {"scheduled": 0, "processing": 0, "failed": 0, "unknown": 0, "recovered": 0}
        for row in rows:
            if row["status"] in {"unknown", "unknown_acknowledged"}: result["unknown"] += row["total"]
            elif row["status"] in result: result[row["status"]] += row["total"]
            if row["last_error_code"] == "RECOVERED_AFTER_RESTART": result["recovered"] += row["total"]
        return result

    def purge_expired(self) -> int:
        """Apply each family's retention window without deleting queued work or audit events."""
        deleted = 0
        with self._external_effect_guard(), self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            preferences = connection.execute("SELECT child_id, retention_days FROM report_delivery_preference").fetchall()
            for preference in preferences:
                rows = connection.execute("""
                  SELECT o.delivery_id,o.status,o.attempt_count,s.report_type,s.child_id,d.report_date,
                         r.child_id AS recipient_child,r.channel,r.address
                  FROM report_delivery_outbox o
                  JOIN parent_report_snapshot s ON s.snapshot_id = o.snapshot_id
                  LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
                  LEFT JOIN report_delivery_recipient r ON r.recipient_id=o.recipient_id
                  WHERE s.child_id = ? AND o.status IN ('sent', 'failed', 'cancelled')
                    AND o.updated_at < datetime('now', ?)
                """, (preference["child_id"], f"-{preference['retention_days']} days")).fetchall()
                for row in rows:
                    if row['report_type'] == 'daily' and (row['status'] == 'sent' or row['attempt_count'] > 0):
                        # Keep only a keyed, non-address receipt for daily
                        # idempotency; if identity cannot be proven, block.
                        from .daily_report_delivery_state import identity_key, assert_ledger_key
                        try:
                            from datetime import date
                            day = row['report_date']
                            if (not isinstance(day,str) or date.fromisoformat(day).isoformat() != day
                                    or row['recipient_child'] != row['child_id'] or row['channel'] != 'email'
                                    or not isinstance(row['address'],str) or not row['address'].strip()):
                                raise ValueError('LEGACY_DELIVERY_IDENTITY_UNKNOWN')
                            assert_ledger_key(connection,self.encryption_key)
                            identity = identity_key(self.encryption_key,row['child_id'],day,row['address'].strip().casefold())
                            stamp = utc_now()
                            retained_status = 'sent' if row['status'] == 'sent' else 'unknown'
                            connection.execute('''INSERT INTO daily_observation_delivery_ledger(identity,child_id,snapshot_id,status,created_at,updated_at)
                              VALUES(?,?,NULL,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                              status=CASE WHEN status IN ('unknown','processing') THEN status ELSE excluded.status END,updated_at=excluded.updated_at''',
                              (identity,row['child_id'],retained_status,stamp,stamp))
                        except (ValueError, TypeError):
                            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_observation_delivery_barrier'").fetchone():
                                continue
                            connection.execute("""INSERT INTO daily_observation_delivery_barrier VALUES(1,1,'DELIVERY_HISTORY_ERASED')
                                ON CONFLICT(singleton_id) DO UPDATE SET blocked=1,reason='DELIVERY_HISTORY_ERASED'""")
                    deleted += connection.execute("DELETE FROM report_delivery_outbox WHERE delivery_id = ?", (row["delivery_id"],)).rowcount
            connection.commit()
        return deleted


def render_message(snapshot: dict, redaction_level: str = "standard") -> tuple[str, str, str]:
    content, edit = snapshot["content"], snapshot["parent_edit"]
    summary, coaching = content.get("summary", {}), content.get("parent_coaching", {})
    subject = f"学习成长报告｜{content.get('report_date') or ''}｜{content.get('headline') or summary.get('headline', '')}".strip("｜")
    if redaction_level == "minimal":
        lines = [
            edit.get("delivery_intro") or "", summary.get("headline", ""), "",
            "为保护孩子隐私，此送达副本不包含学习细节、孩子原话或沟通脚本。",
            "请在本机家长中心查看已批准的完整报告。",
            "", "不确定性：", snapshot["confidence"].get("uncertainty", "仅根据已确认记录生成。"),
        ]
    else:
        facts = list(summary.get("facts") or [])
        if not facts:
            coverage = (snapshot.get("facts") or {}).get("coverage") or {}
            if coverage:
                facts.append(
                    f"本期已记录{coverage.get('recorded_days', 0)}天、"
                    f"{coverage.get('recorded_sessions', 0)}次学习会话，共{coverage.get('evidence_count', 0)}条证据。"
                )
            facts.extend(
                f"{item.get('date', '记录日')}：{item.get('headline', '已记录学习信号')}"
                for item in ((snapshot.get("facts") or {}).get("daily_signals") or [])
            )
        action = coaching.get("action") or (snapshot.get("inferences") or {}).get("next_action") or {}
        scripts = coaching.get("script") or []
        decision = edit.get("coaching_decision", "adopt")
        if decision == "skip":
            action = {"what": "家长本次选择不执行额外行动", "duration_minutes": 0}
            scripts = ["这次先不追加对话或行动；明天根据孩子愿意确认的事实再调整。"]
        elif decision == "custom":
            if edit.get("coaching_action"):
                action = {**action, "what": edit["coaching_action"]}
            if isinstance(edit.get("coaching_duration_minutes"), int) and not isinstance(edit.get("coaching_duration_minutes"), bool):
                action = {**action, "duration_minutes": edit["coaching_duration_minutes"]}
            if edit.get("coaching_script"):
                scripts = [edit["coaching_script"]]
        observation = "" if decision == "skip" else str(edit.get("coaching_observation") or "").strip()
        avoid = coaching.get("avoid") or []
        lines = [
            edit.get("delivery_intro") or "", summary.get("headline", ""), "", "已确认的学习事实：",
            *[f"- {item}" for item in facts], "", "下一步：",
            f"- {action.get('what') or '请查看报告中的下一步'}",
            *([f"- 约{action['duration_minutes']}分钟"] if action.get("duration_minutes") is not None else []),
            "", "建议这样和孩子说：", *[f"- {item}" for item in scripts],
            *(["", "观察与明天调整：", f"- {observation}"] if observation else []),
            "", "不要这样说：", *[f"- 不要说“{item.get('do_not_say', '')}”；可以改说“{item.get('say_instead', '')}”" for item in avoid],
            "", "不确定性：", snapshot["confidence"].get("uncertainty", "仅根据已确认记录生成。"),
        ]
    if edit.get("parent_note"): lines.extend(["", "家长备注：", edit["parent_note"]])
    text = "\n".join(line for line in lines if line is not None).strip()
    html = "<html><body><pre style=\"font-family:system-ui, sans-serif;white-space:pre-wrap\">" + _escape(text) + "</pre></body></html>"
    return subject, text, html


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _channel_test_message(code: str) -> str:
    """Keep low-level SMTP details and credentials out of the parent surface."""
    if code == "CHANNEL_NOT_CONFIGURED":
        return "请先保存邮件发送设置；报告仍只保存在本机家长中心。"
    if code == "EXTERNAL_DELIVERY_NOT_AUTHORIZED":
        return "尚未允许外发测试邮件，系统没有发送。"
    if code == "SMTP_CREDENTIALS_INVALID":
        return "邮箱授权信息无效；更新应用专用密码后可再次测试。"
    if code == "SMTP_RECIPIENT_REFUSED":
        return "邮件服务拒绝该已授权收件人；请核对地址或服务商设置。"
    if code in {"CREDENTIAL_KEY_UNAVAILABLE", "CREDENTIAL_DECRYPT_FAILED"}:
        return "本机无法读取加密发送设置；请由家长重新配置。"
    if code == "SMTP_CONFIG_INVALID":
        return "邮件主机或发件地址不完整；请补全后再次测试。"
    return "测试邮件暂未提交；请检查网络和邮件服务设置后再次测试。"


def _is_local_smtp_host(host: str) -> bool:
    """Use an explicit allow-list instead of trusting DNS for plaintext sink mode."""
    return host.strip().lower().strip("[]") in {"127.0.0.1", "localhost", "::1"}


def _valid_time(value: str) -> bool:
    try: datetime.strptime(value, "%H:%M"); return True
    except ValueError: return False


def _next_quiet_end(end: str, now: datetime | None = None) -> str:
    now = family_time(now if now is not None else datetime.now(timezone.utc))
    target = datetime.strptime(end, "%H:%M").replace(year=now.year, month=now.month, day=now.day, tzinfo=now.tzinfo)
    if target <= now: target += timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()
