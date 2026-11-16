from local_restore_operation_fixture import restore_local
"""Actual backup restore with synthetic data and loopback-only SMTP."""
import pytest
from normal_provider_owner import normal_provider_owner
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import threading

from home_study_agent.database import Database, ExternalEffectBusyError
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.report_delivery import DeliveryStateError, ReportDeliveryService
from home_study_agent.report_followup import ReportFollowupService
from test_report_delivery_recovery_journey import flow, smtp, prepare


@pytest.mark.parametrize("transport", ["sent", "unknown"])
def test_restoring_pre_send_backup_cannot_automatically_send_a_report_again(flow, smtp, tmp_path, transport, normal_provider_owner):
    db, repository, report, delivery = flow
    draft = delivery.snapshots.create_from_daily(report)
    delivery.snapshots.edit_draft(draft['snapshot_id'], coaching_decision='adopt', parent_note='合成审核：核对后采用')
    snapshot, recipient = prepare(flow, smtp, "daily")
    queued = delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    reviewed = delivery.snapshots.record_feedback(snapshot['snapshot_id'], 4, 2, '合成家长反馈', 'before-restore-feedback')
    assert reviewed['feedback_current_status'] == 'active'
    planning_date = (datetime.now().date() + timedelta(days=1)).isoformat()
    followup = ReportFollowupService(db)
    assert any(item['evidence_kind'] == 'report_experience' for item in followup.project_for_plan(report['child_id'], planning_date)['items'])
    assert repository.report_evidence(report['session_id'])['coaching_decisions']
    with db.connect() as connection:
        session = connection.execute("SELECT session_id,state FROM daily_session WHERE child_id=?", (report["child_id"],)).fetchone()
    assert session["state"] == "completed"
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    backup = privacy.create_backup(prune=False)
    smtp.mode = transport
    assert delivery.process_due()[0]["status"] == transport
    assert len(smtp.submissions) == 1
    restored = restore_local(privacy,backup["backup_id"], "确认恢复")
    assert restored["status"] == "restored"
    # The same running service is the production restore HTTP consumer.
    delivery.process_due()
    assert len(smtp.submissions) == 1
    current = delivery.get_delivery(queued["delivery_id"])
    assert current["status"] == "unknown"
    assert current["last_error_code"] == "RESTORE_DELIVERY_UNCERTAIN"
    assert current["attempts"] == []
    restored_snapshot = delivery.snapshots.get(snapshot["snapshot_id"])
    assert restored_snapshot['status'] == 'draft'
    assert restored_snapshot['approved_at'] == reviewed['approved_at'] is not None
    assert restored_snapshot['feedback'] == reviewed['feedback']
    assert restored_snapshot['feedback_current_status'] == 'source_invalid'
    assert restored_snapshot['feedback_current'] is None
    assert not followup.project_for_plan(report['child_id'], planning_date)['items']
    evidence = repository.report_evidence(report['session_id'])
    assert evidence['report_feedback'] == []
    assert evidence['coaching_decisions'] == []
    assert delivery.get_recipient(recipient["recipient_id"])["consent_status"] == "pending"
    restarted = ReportDeliveryService(Database(db.path), b"D" * 32)
    restarted.initialize()
    assert restarted.process_due() == []
    assert len(smtp.submissions) == 1


def test_restore_hook_preserves_facts_and_only_invalidates_authority_atomically(flow, smtp):
    db, _, _, delivery = flow
    snapshot, recipient = prepare(flow, smtp, "daily")
    statuses = ("scheduled", "processing", "failed", "sent", "unknown", "cancelled")
    queued = [delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"],
        (datetime.now(timezone.utc) + timedelta(days=index)).isoformat()) for index in range(len(statuses))]
    with db.connect() as connection:
        for row, status in zip(queued, statuses):
            connection.execute("UPDATE report_delivery_outbox SET status=? WHERE delivery_id=?", (status, row["delivery_id"]))
        connection.commit()
        frozen = dict(connection.execute("SELECT * FROM parent_report_snapshot WHERE snapshot_id=?", (snapshot["snapshot_id"],)).fetchone())
        connection.execute("""CREATE TRIGGER fail_restore_audit BEFORE INSERT ON audit_event
            WHEN NEW.action='report_delivery_restore_quarantined'
            BEGIN SELECT RAISE(ABORT,'restore audit failed'); END""")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError, match="restore audit failed"):
            ReportDeliveryService.quarantine_after_restore(connection, "atomic-restore")
        assert connection.in_transaction
        assert dict(connection.execute("SELECT * FROM parent_report_snapshot WHERE snapshot_id=?", (snapshot["snapshot_id"],)).fetchone()) == frozen
        connection.execute("DROP TRIGGER fail_restore_audit")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        schemas = list(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        result = ReportDeliveryService.quarantine_after_restore(connection, "atomic-restore")
        assert result["outbox_unknown"] == 3
        assert result["snapshot_approvals_invalidated"] == result["recipient_consents_invalidated"] == 1
        assert connection.in_transaction
        assert list(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name")) == schemas
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_attempt").fetchone()[0] == 0
        restored = dict(connection.execute("SELECT * FROM parent_report_snapshot WHERE snapshot_id=?", (snapshot["snapshot_id"],)).fetchone())
        assert restored == {**frozen, "status": "draft"}
        connection.commit()
    assert [delivery.get_delivery(row["delivery_id"])["status"] for row in queued] == ["unknown", "unknown", "unknown", "sent", "unknown", "cancelled"]
    assert delivery.get_recipient(recipient["recipient_id"])["consent_status"] == "pending"
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert ReportDeliveryService.quarantine_after_restore(connection, "atomic-restore") == result
        connection.commit()
        audit = json.loads(connection.execute("SELECT summary_json FROM audit_event WHERE action='parent_report_approval_invalidated_after_restore'").fetchone()[0])
        assert audit["previous_approved_at"] == frozen["approved_at"]
    # A real new parent review is required; replaying the old quarantine cannot undo it.
    assert delivery.snapshots.approve(snapshot["snapshot_id"])["status"] == "approved"
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        ReportDeliveryService.quarantine_after_restore(connection, "atomic-restore")
        connection.commit()
    assert delivery.snapshots.get(snapshot["snapshot_id"])["status"] == "approved"
    assert not smtp.submissions


@pytest.mark.parametrize("operation", ["report", "channel_test"])
def test_actual_smtp_receipt_finishes_before_restore_replaces_live(flow, smtp, tmp_path, operation, normal_provider_owner):
    db, _, report, delivery = flow
    snapshot, recipient = prepare(flow, smtp, "daily")
    queued = delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    privacy = PrivacyLifecycleService(Database(db.path), tmp_path)
    privacy.initialize()
    backup = privacy.create_backup(prune=False)
    accepted, release, restore_started = threading.Event(), threading.Event(), threading.Event()
    actual_adapter = delivery.adapters["email"]

    class HoldReceipt:
        def send(self, *args, **kwargs):
            receipt = actual_adapter.send(*args, **kwargs)
            accepted.set()
            assert release.wait(10), "Test did not release the accepted SMTP receipt"
            return receipt

    delivery.adapters["email"] = HoldReceipt()

    def restore():
        restore_started.set()
        return restore_local(privacy,backup["backup_id"], "确认恢复")

    with ThreadPoolExecutor(max_workers=2) as workers:
        sending = workers.submit(delivery._process_one, queued["delivery_id"]) if operation == "report" else workers.submit(
            delivery.test_channel, report["child_id"], "email", "concurrent-channel-test")
        try:
            assert accepted.wait(10)
            restoring = workers.submit(restore)
            assert restore_started.wait(2)
            # The current admission reports BUSY while the original accepted
            # receipt is in flight. No restore is allowed to publish at this point.
            with pytest.raises(ExternalEffectBusyError):
                restoring.result(timeout=10)
            assert not sending.done()
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='backup_restored'").fetchone()[0] == 0
                assert c.execute('SELECT COUNT(*) FROM privacy_operation_intent').fetchone()[0] == 0
                assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()
        finally:
            release.set()
        assert sending.result(timeout=10)["status"] == ("sent" if operation == "report" else "passed")
        # Only a new explicit attempt after the original SMTP receipt can restore.
        assert restore_local(privacy, backup["backup_id"], "确认恢复")["status"] == "restored"
    assert len(smtp.submissions) == 1
    assert delivery.process_due() == []
    assert delivery.get_delivery(queued["delivery_id"])["status"] == "unknown"
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_attempt").fetchone()[0] == 0
        assert not connection.execute("SELECT 1 FROM report_delivery_channel_test_action WHERE client_action_id='concurrent-channel-test'").fetchone()


@pytest.mark.parametrize("operation", ["report", "channel_test"])
def test_guard_timeout_is_retryable_conflict_without_reserving_or_sending(flow, smtp, monkeypatch, operation):
    db, _, report, delivery = flow
    snapshot, recipient = prepare(flow, smtp, "daily")
    queued = delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])
    held, release = threading.Event(), threading.Event()
    other = Database(db.path)

    def hold_restore_guard():
        with other.external_effect_guard():
            held.set()
            assert release.wait(5)

    original_guard = db.external_effect_guard
    monkeypatch.setattr(db, "external_effect_guard", lambda: original_guard(timeout=.02))
    with ThreadPoolExecutor(max_workers=1) as workers:
        holding = workers.submit(hold_restore_guard)
        try:
            assert held.wait(2)
            with pytest.raises(DeliveryStateError, match="REPORT_DELIVERY_BUSY"):
                if operation == "report":
                    delivery._process_one(queued["delivery_id"])
                else:
                    delivery.test_channel(report["child_id"], "email", "busy-test")
        finally:
            release.set()
        holding.result(timeout=2)
    assert not smtp.submissions
    assert delivery.get_delivery(queued["delivery_id"])["status"] == "scheduled"
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_attempt").fetchone()[0] == 0
        assert not connection.execute("SELECT 1 FROM report_delivery_channel_test_action WHERE client_action_id='busy-test'").fetchone()
