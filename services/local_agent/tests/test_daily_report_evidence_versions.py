from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.notification_channels import ChannelReceipt
from home_study_agent.parent_reports import ParentReportService, SnapshotStateError
from home_study_agent.report_delivery import DeliveryStateError, ReportDeliveryService
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report


CHILD = "child-primary-4"
ITEM = "diag-p4-arithmetic-closed-1"


@pytest.fixture
def flow(tmp_path):
    db = Database(tmp_path / "daily-versions.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    sid = repo.start_session(CHILD, str(uuid4()))["session_id"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.commit()
    answered = learning.submit_diagnostic(sid, ITEM, "90", "daily-exact")
    assert answered["score_status"] == "verified_local"
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        connection.commit()
    repo.complete_with_reflection(sid, "", "", "okay", "", True, "daily-finish")
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    report = repo.save_daily_report(sid, summary, coaching)
    delivery = ReportDeliveryService(db, b"D" * 32)
    delivery.initialize()
    return db, repo, report, delivery


def retire_source(db, repo, report):
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        connection.commit()
    refreshed = repo.get_daily_report(report["report_id"])
    assert refreshed["evidence"]["reliable_learning_fingerprint"] != report["evidence"]["reliable_learning_fingerprint"]
    assert refreshed["evidence_detail"]["knowledge_evidence"] == []
    return refreshed


@pytest.mark.parametrize("legacy", [False, True])
def test_withdrawal_versions_daily_snapshot_without_overwriting_history_or_resending(flow, legacy):
    db, repo, report, delivery = flow
    snapshots = delivery.snapshots
    old = snapshots.create_from_daily(report)
    snapshots.edit_draft(old["snapshot_id"], "今晚只核对一步", "家长自己的观察")
    old = snapshots.approve(old["snapshot_id"])
    repo.save_parent_report_feedback(report["report_id"], True, 4, 1, "反馈必须保留")
    if legacy:
        with db.connect() as connection:
            connection.execute("UPDATE parent_report_snapshot SET source_id=? WHERE snapshot_id=?", (report["report_id"], old["snapshot_id"]))
            connection.commit()
    recipient = delivery.add_recipient(CHILD, "email", "parent@example.test", "家长", True)
    deliveries = {}
    # These are historical rows predating uncertainty, not new sends authorized
    # after a processing/unknown result. Reserve every schedule first.
    for index, status in enumerate(("scheduled", "failed", "processing", "sent", "unknown")):
        item = delivery.enqueue(old["snapshot_id"], recipient["recipient_id"], f"2099-01-0{index + 1}T00:00:00+00:00")
        deliveries[status] = item["delivery_id"]
    with db.connect() as connection:
        for status, delivery_id in deliveries.items():
            connection.execute("UPDATE report_delivery_outbox SET status=? WHERE delivery_id=?", (status, delivery_id))
        connection.commit()

    refreshed = retire_source(db, repo, report)
    current = snapshots.create_from_daily(refreshed)
    assert current["snapshot_id"] != old["snapshot_id"]
    assert current["evidence_version"] != old["evidence_version"]
    assert current["source_id"].startswith(report["report_id"] + "@")
    assert current["status"] == "draft" and current["approved_at"] is None
    assert current["parent_edit"]["parent_note"] == ""
    assert current["content"]["summary"] == refreshed["summary"]
    assert current["facts"]["evidence_manifest"]["reliable_learning_fingerprint"] == refreshed["evidence"]["reliable_learning_fingerprint"]
    stored_old = snapshots.get(old["snapshot_id"])
    assert stored_old["status"] == "revoked"
    for field in ("facts", "inferences", "content", "confidence", "parent_edit", "approved_at", "approved_by"):
        assert stored_old[field] == old[field]
    for status in ("scheduled", "failed"):
        assert delivery.get_delivery(deliveries[status])["status"] == "cancelled"
        with pytest.raises(DeliveryStateError, match="后续证据更新"):
            delivery.retry(deliveries[status])
    for status in ("sent", "unknown"):
        assert delivery.get_delivery(deliveries[status])["status"] == status
    assert delivery._submission_block_reason(deliveries["processing"]) == "snapshot_not_approved"
    with pytest.raises(SnapshotStateError):
        snapshots.approve(old["snapshot_id"])
    with pytest.raises(SnapshotStateError):
        delivery.enqueue(old["snapshot_id"], recipient["recipient_id"])
    with pytest.raises(SnapshotStateError):
        delivery.enqueue(current["snapshot_id"], recipient["recipient_id"])

    restarted_db = Database(db.path)
    restarted = ParentReportService(restarted_db)
    live = StudyRepository(restarted_db).get_daily_report(report["report_id"])
    assert live["feedback"]["notes_summary"] == "反馈必须保留"
    assert restarted.get(old["snapshot_id"]) == stored_old
    assert restarted.create_from_daily(live)["snapshot_id"] == current["snapshot_id"]
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_outbox WHERE snapshot_id=?", (current["snapshot_id"],)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action='parent_daily_snapshot_superseded'").fetchone()[0] == 1


def test_daily_content_version_changes_even_when_evidence_fingerprint_does_not(flow):
    db, repo, report, delivery = flow
    old = delivery.snapshots.create_from_daily(report)
    summary = {**report["summary"], "headline": "已核验作答，仍需延迟复测"}
    with db.connect() as connection:
        connection.execute("UPDATE daily_report SET summary_json=? WHERE report_id=?", (json.dumps(summary, ensure_ascii=False), report["report_id"]))
        connection.commit()
    refreshed = repo.get_daily_report(report["report_id"])
    assert refreshed["evidence"]["reliable_learning_fingerprint"] == report["evidence"]["reliable_learning_fingerprint"]
    current = delivery.snapshots.create_from_daily(refreshed)
    assert current["snapshot_id"] != old["snapshot_id"]
    assert current["content"]["headline"] == summary["headline"]
    assert delivery.snapshots.get(old["snapshot_id"])["content"]["headline"] == report["summary"]["headline"]
    replay = delivery.snapshots.create_from_daily(report)
    assert replay["status"] == "revoked"
    assert delivery.snapshots.get(current["snapshot_id"])["status"] == "draft"


def test_daily_version_is_idempotent_under_concurrent_creation_and_restart(flow):
    db, _, report, delivery = flow
    def create(_):
        return ParentReportService(Database(db.path)).create_from_daily(report)["snapshot_id"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(create, range(6)))
    assert len(set(ids)) == 1
    assert delivery.snapshots.create_from_daily({**report, "created_at": "ignored-read-metadata"})["snapshot_id"] == ids[0]
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM parent_report_snapshot").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action='parent_report_snapshot_created'").fetchone()[0] == 1


def test_daily_supersession_rolls_back_new_snapshot_and_outbox_together(flow):
    db, repo, report, delivery = flow
    old = delivery.snapshots.approve(delivery.snapshots.create_from_daily(report)["snapshot_id"])
    recipient = delivery.add_recipient(CHILD, "email", "parent@example.test", "家长", True)
    queued = delivery.enqueue(old["snapshot_id"], recipient["recipient_id"])
    refreshed = retire_source(db, repo, report)
    with db.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_daily_supersession BEFORE UPDATE OF status ON parent_report_snapshot
            WHEN NEW.status='revoked' BEGIN SELECT RAISE(ABORT, 'injected write failure'); END""")
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected write failure"):
        delivery.snapshots.create_from_daily(refreshed)
    assert delivery.snapshots.get(old["snapshot_id"])["status"] == "approved"
    assert delivery.get_delivery(queued["delivery_id"])["status"] == "scheduled"
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM parent_report_snapshot").fetchone()[0] == 1
        connection.execute("DROP TRIGGER fail_daily_supersession")
        connection.commit()
    assert delivery.snapshots.create_from_daily(refreshed)["status"] == "draft"
    assert delivery.get_delivery(queued["delivery_id"])["status"] == "cancelled"


def test_parent_list_refreshes_live_daily_version_but_old_snapshot_get_is_historical(flow):
    db, _, report, delivery = flow
    old = delivery.snapshots.create_from_daily(report)
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        connection.commit()
    settings = Settings(data_dir=db.path.parent, database_path=db.path, data_encryption_key=b"D" * 32,
                        maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        path = f"/v1/parent/children/{CHILD}/report-snapshots"
        assert client.get(path).status_code == 401
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        historical = client.get(f"/v1/parent/report-snapshots/{old['snapshot_id']}", headers=headers)
        assert historical.status_code == 200
        assert historical.json()["content"] == old["content"]
        listed = client.get(path, headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        current = next(item for item in items if item["source_kind"] == "daily_report" and item["snapshot_id"] != old["snapshot_id"])
        assert current["status"] == "draft"
        assert current["content"]["evidence_detail"]["knowledge_evidence"] == []
        assert client.get(path, headers=headers).json()["items"] == items
        assert client.get("/v1/parent/children/child-junior-3/report-snapshots", headers=headers).json()["items"] == []
        reread = client.get(f"/v1/parent/report-snapshots/{old['snapshot_id']}", headers=headers).json()
        assert reread["status"] == "revoked" and reread["content"] == old["content"]
        behavior_provider = client.app.state.period_reports.behavior_context_provider
    # A new service must receive the same host dependency as the HTTP service;
    # dropping it deliberately produces different period-coaching context.
    from home_study_agent.period_reports import PeriodReportService
    restarted_db = Database(db.path)
    restarted = ParentReportService(restarted_db)
    restarted.set_period_report_service(PeriodReportService(restarted_db, behavior_provider))
    assert restarted.list(CHILD) == items


def test_stale_unfrozen_payload_cannot_supersede_current_daily_version(flow):
    db, repo, report, delivery = flow
    current = delivery.snapshots.create_from_daily(retire_source(db, repo, report))
    with pytest.raises(SnapshotStateError, match="日报内容已更新"):
        delivery.snapshots.create_from_daily(report)
    assert delivery.snapshots.get(current["snapshot_id"])["status"] == "draft"
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM parent_report_snapshot").fetchone()[0] == 1


def test_source_change_between_daily_read_and_first_freeze_requires_revalidation(flow):
    db, repo, report, delivery = flow
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        connection.commit()
    with pytest.raises(SnapshotStateError, match="日报证据已更新"):
        delivery.snapshots.create_from_daily(report)
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM parent_report_snapshot").fetchone()[0] == 0
    current = delivery.snapshots.create_from_daily(repo.get_daily_report(report["report_id"]))
    assert current["status"] == "draft"


def test_daily_replay_cannot_return_other_child_snapshot(flow):
    _, _, report, delivery = flow
    old = delivery.snapshots.create_from_daily(report)
    with pytest.raises(SnapshotStateError, match="不属于当前孩子"):
        delivery.snapshots.create_from_daily({**report, "child_id": "child-junior-3"})
    assert delivery.snapshots.get(old["snapshot_id"])["status"] == "draft"


class MemoryEmail:
    def __init__(self):
        self.messages = []

    def send(self, recipient, subject, text, html, config):
        self.messages.append(text)
        return ChannelReceipt(provider_message_id="memory-receipt")


@pytest.fixture
def withdrawal_flow(tmp_path, monkeypatch):
    from home_study_agent import learning_state, mistake_book, repository

    db = Database(tmp_path / "withdraw-before-send.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    mistakes = MistakeBookService(db)
    clock = [datetime.now(timezone.utc) - timedelta(days=4)]
    for module in (learning_state, mistake_book, repository):
        monkeypatch.setattr(module, "utc_now", lambda: clock[0].isoformat())
    sid = repo.start_session(CHILD, "withdraw-send-session")["session_id"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.commit()
    mistake_ids = set()
    for index, item_id in enumerate((ITEM, "diag-p4-arithmetic-closed-2", ITEM)):
        if index:
            clock[0] += timedelta(days=2)
        answer = learning.submit_diagnostic(sid, item_id, "12", f"withdraw-answer-{index}")
        assert answer["score_status"] == "verified_local"
        mistake_ids.add(mistakes.capture_from_attempt(answer)["mistake_id"])
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        connection.commit()
    repo.complete_with_reflection(sid, "", "", "okay", "", True, "withdraw-send-finish")
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    report = repo.save_daily_report(sid, summary, coaching)
    assert report["evidence"]["knowledge_evidence_count"] == 3
    delivery = ReportDeliveryService(db, b"D" * 32)
    delivery.initialize()
    hour = datetime.now().hour
    delivery.update_preferences(CHILD, f"{(hour + 1) % 24:02d}:00", f"{(hour + 2) % 24:02d}:00", 90)
    delivery.save_channel_config("email", {"host": "127.0.0.1", "from_address": "report@example.test", "allow_external_delivery": True})
    sink = MemoryEmail()
    delivery.adapters["email"] = sink
    snapshot = delivery.snapshots.create_from_daily(report)
    delivery.snapshots.edit_draft(snapshot["snapshot_id"], "家长开头", "家长观察不可覆盖")
    snapshot = delivery.snapshots.approve(snapshot["snapshot_id"])
    recipient = delivery.add_recipient(CHILD, "email", "parent@example.test", "家长", True)
    queued = delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"])

    def withdraw():
        for mistake_id in mistake_ids:
            mistakes.confirm(mistake_id, CHILD, "exclude", f"exclude-{mistake_id}")

    return db, repo, report, delivery, sink, snapshot, queued, withdraw


@pytest.mark.parametrize("when", ["before_worker", "live_report_already_refreshed", "during_render"])
def test_direct_worker_revalidates_excluded_receipts_without_opening_snapshot_list(withdrawal_flow, monkeypatch, when):
    from home_study_agent import report_delivery

    db, repo, report, delivery, sink, old, queued, withdraw = withdrawal_flow
    if when == "during_render":
        render = report_delivery.render_message

        def withdraw_while_rendering(*args, **kwargs):
            content = render(*args, **kwargs)
            withdraw()
            return content

        monkeypatch.setattr(report_delivery, "render_message", withdraw_while_rendering)
    else:
        withdraw()
        if when == "live_report_already_refreshed":
            assert repo.get_daily_report(report["report_id"])["evidence"]["knowledge_evidence_count"] == 0
    result = delivery.process_due()
    assert sink.messages == []
    assert result[0]["status"] == "cancelled"
    stored = delivery.snapshots.get(old["snapshot_id"])
    assert stored["status"] == "revoked"
    assert stored["content"] == old["content"] and stored["parent_edit"] == old["parent_edit"]
    with db.connect() as connection:
        current_id = connection.execute("SELECT snapshot_id FROM parent_report_snapshot WHERE report_id=? AND status='draft'", (report["report_id"],)).fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_outbox WHERE snapshot_id=?", (current_id,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_attempt WHERE status='sent'").fetchone()[0] == 0
    current = delivery.snapshots.get(current_id)
    assert current["content"]["evidence_detail"]["knowledge_evidence"] == []
    with pytest.raises(DeliveryStateError, match="后续证据更新"):
        delivery.retry(queued["delivery_id"])
    restarted = ReportDeliveryService(Database(db.path), b"D" * 32)
    restarted.adapters["email"] = sink
    assert restarted.process_due() == []
    assert sink.messages == []


def test_current_daily_sends_once_and_later_withdrawal_does_not_resend_sent_history(withdrawal_flow):
    db, _, _, delivery, sink, old, queued, withdraw = withdrawal_flow
    assert delivery.process_due()[0]["status"] == "sent"
    assert len(sink.messages) == 1 and "3条已核验作答" in sink.messages[0]
    withdraw()
    assert delivery.snapshots.validate_current_for_delivery(old["snapshot_id"]) is False
    assert delivery.get_delivery(queued["delivery_id"])["status"] == "sent"
    assert delivery.process_due() == [] and len(sink.messages) == 1
    assert ParentReportService(Database(db.path)).get(old["snapshot_id"])["content"] == old["content"]


@pytest.mark.parametrize("when", ["before_freeze", "before_send"])
def test_prior_report_dependency_change_invalidates_daily_without_changing_own_fingerprint(withdrawal_flow, when):
    db, repo, first, delivery, sink, _, first_queue, withdraw = withdrawal_flow
    delivery.cancel(first_queue["delivery_id"], "本用例验证后续日报")
    sid = repo.start_session(CHILD, "dependent-report-session")["session_id"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        connection.commit()
    repo.complete_with_reflection(sid, "", "", "okay", "", True, "dependent-report-finish")
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    report = repo.save_daily_report(sid, summary, coaching)
    dependencies = report["evidence"]["reliable_learning_dependencies"]
    assert dependencies == [{"session_id": first["session_id"], "evidence_fingerprint": first["evidence"]["reliable_learning_fingerprint"]}]
    if when == "before_send":
        old = delivery.snapshots.approve(delivery.snapshots.create_from_daily(report)["snapshot_id"])
        recipient = delivery.add_recipient(CHILD, "email", "parent@example.test", "家长", True)
        queued = delivery.enqueue(old["snapshot_id"], recipient["recipient_id"])
    withdraw()
    if when == "before_freeze":
        with pytest.raises(SnapshotStateError, match="日报证据已更新"):
            delivery.snapshots.create_from_daily(report)
    else:
        assert delivery.process_due()[0]["status"] == "cancelled"
        assert delivery.get_delivery(queued["delivery_id"])["status"] == "cancelled"
        assert sink.messages == []
        assert delivery.snapshots.get(old["snapshot_id"])["status"] == "revoked"
    refreshed = repo.get_daily_report(report["report_id"])
    assert refreshed["evidence"]["reliable_learning_fingerprint"] == report["evidence"]["reliable_learning_fingerprint"]
    assert refreshed["evidence"]["reliable_learning_dependencies"] == []
    current = delivery.snapshots.create_from_daily(refreshed)
    assert current["status"] == "draft"
    if when == "before_send":
        assert current["source_id"] != old["source_id"]
        assert delivery.snapshots.get(old["snapshot_id"])["content"] == old["content"]
