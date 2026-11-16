"""Offline acceptance harness for parent-report review and delivery.

It deliberately uses an ephemeral SQLite database and a loopback-only SMTP sink.
No child data, mailbox credentials, or WeCom webhook ever leaves the machine.
Exit 0 means the durable outbox/restart path passed; any assertion exits non-zero.
"""
from __future__ import annotations

import argparse
import asyncore
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import smtpd
import tempfile
import threading

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_delivery import ReportDeliveryService, _next_quiet_end, render_message
from home_study_agent.repository import StudyRepository, utc_now


class LoopbackSink(smtpd.SMTPServer):
    def __init__(self) -> None:
        self.socket_map = {}
        super().__init__(("127.0.0.1", 0), None, map=self.socket_map)
        self.messages: list[bytes] = []

    def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
        self.messages.append(data)
        return None


def daily_report(database: Database) -> dict:
    repo = StudyRepository(database); repo.seed_family(); now = utc_now()
    summary = {"headline": "完成 1 项学习计划", "facts": ["计划20分钟，已记录完成20分钟"], "interpretation": "仅根据确认记录。", "evidence_count": 1}
    coaching = {"action": {"what": "复习判别式第一步", "duration_minutes": 10}, "script": ["先说出已经确定的一步。"], "avoid": []}
    with database.connect() as c:
        c.execute("INSERT INTO daily_session(session_id, child_id, status, state, started_at, ended_at, last_checkpoint_at, client_action_id) VALUES ('local-delivery-session','child-junior-3','completed','completed',?,?,?,'local-delivery-session-action')", (now, now, now))
        c.execute("INSERT INTO report_job(job_id, session_id, child_id, report_type, status, created_at, updated_at) VALUES ('local-delivery-job','local-delivery-session','child-junior-3','daily','completed',?,?)", (now, now))
        c.execute("INSERT INTO daily_report(report_id, session_id, child_id, report_date, source, evidence_json, summary_json, parent_coaching_json, created_at) VALUES ('local-delivery-report','local-delivery-session','child-junior-3','2026-08-28','local_rules_v2',?,?,?,?)", (json.dumps({"session_id":"local-delivery-session"}), json.dumps(summary, ensure_ascii=False), json.dumps(coaching, ensure_ascii=False), now))
        c.commit()
    return {"report_id":"local-delivery-report", "session_id":"local-delivery-session", "child_id":"child-junior-3", "report_date":"2026-08-28", "source":"local_rules_v2", "evidence":{"session_id":"local-delivery-session"}, "summary":summary, "parent_coaching":coaching, "evidence_detail":{"recalls":[], "tasks":[], "reflection":None, "declarations":{"no_recall":False,"no_homework":False}}}


def run(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True); database = Database(data_dir / "home-study.db"); database.initialize()
    report = daily_report(database); snapshots = ParentReportService(database); service = ReportDeliveryService(database, b"L" * 32); service.initialize()
    quiet_start = (datetime.now().hour + 1) % 24; quiet_end = (datetime.now().hour + 2) % 24
    service.update_preferences("child-junior-3", f"{quiet_start:02d}:00", f"{quiet_end:02d}:00", 90)
    daily = snapshots.create_from_daily(report)
    snapshots.edit_draft(daily["snapshot_id"], "今晚一起看", "不比较，不追问")
    # Correction is durable, revokes the old snapshot, then a fresh immutable snapshot is approved.
    assert snapshots.request_correction(daily["snapshot_id"], "完成记录待核对")["status"] == "revoked"
    replacement = {**report, "report_id": "local-delivery-report-corrected", "session_id": "local-delivery-session-corrected"}
    with database.connect() as c:
        now = utc_now()
        c.execute("INSERT INTO daily_session(session_id, child_id, status, state, started_at, ended_at, last_checkpoint_at, client_action_id) VALUES (?, 'child-junior-3','completed','completed',?,?,?,'local-delivery-corrected-action')", (replacement["session_id"], now, now, now))
        c.execute("INSERT INTO daily_report(report_id, session_id, child_id, report_date, source, evidence_json, summary_json, parent_coaching_json, created_at) SELECT ?, ?, child_id, report_date, source, evidence_json, summary_json, parent_coaching_json, created_at FROM daily_report WHERE report_id=?", (replacement["report_id"], replacement["session_id"], report["report_id"]))
        c.commit()
    approved = snapshots.approve(snapshots.create_from_daily(replacement)["snapshot_id"])
    recipient = service.add_recipient("child-junior-3", "email", "parent@local.test", "本地家长", True)
    sink = LoopbackSink()
    def serve() -> None:
        try: asyncore.loop(timeout=0.01, map=sink.socket_map)
        except OSError: pass  # Closing the loopback listener wakes select on macOS/Windows.
    thread = threading.Thread(target=serve, daemon=True); thread.start()
    try:
        service.save_channel_config("email", {"host":"127.0.0.1", "port":sink.socket.getsockname()[1], "from_address":"report@local.test", "starttls":False, "allow_external_delivery":True})
        sent = service.enqueue(approved["snapshot_id"], recipient["recipient_id"])
        assert service.process_due()[0]["status"] == "sent"
        assert sink.messages and any(p.get_filename() == "学习成长报告.html" for p in BytesParser(policy=policy.default).parsebytes(sink.messages[0]).iter_attachments())
        # Simulated post-claim crash: next service instance makes it visibly retryable.
        retry = service.enqueue(approved["snapshot_id"], recipient["recipient_id"], "2099-01-01T00:00:00+00:00")
        with database.connect() as c: c.execute("UPDATE report_delivery_outbox SET status='processing' WHERE delivery_id=?", (retry["delivery_id"],)); c.commit()
        restarted = ReportDeliveryService(database, b"L" * 32); restarted.initialize()
        recovered = restarted.get_delivery(retry["delivery_id"])
        assert recovered["status"] == "unknown" and recovered["last_error_code"] == "UNKNOWN_AFTER_RESTART"
        assert restarted.process_due() == []  # Never silently send an uncertain full report again.
        # Weekly/monthly and minimum disclosure are rendered from snapshots, never source tables.
        for kind in ("weekly", "monthly"):
            created = snapshots.create_from_period({"period_report_id":f"local-{kind}", "child_id":"child-junior-3", "report_type":kind, "period_end":"2026-08-28", "source":"local_aggregate_v2", "content":{"headline":f"{kind}本地验收", "coverage":{"recorded_days":1,"recorded_sessions":1,"evidence_count":1}, "next_action":{"what":"确认下一步"}}})
            period = snapshots.approve(created["snapshot_id"])
            _, minimal, _ = render_message(period, "minimal")
            assert "本机家长中心" in minimal and "计划20分钟" not in minimal
        assert _next_quiet_end("07:00", datetime(2026, 8, 28, 23, 55, tzinfo=timezone.utc)).startswith("2026-08-29T07:00")
        print(json.dumps({"status":"passed", "database":str(data_dir / "home-study.db"), "smtp_messages":len(sink.messages), "daily_delivery":sent["delivery_id"], "restart":"UNKNOWN_AFTER_RESTART", "periods":["weekly","monthly"], "external_delivery":False}, ensure_ascii=False))
    finally:
        sink.close(); thread.join(timeout=1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(); target = args.data_dir or Path(tempfile.mkdtemp(prefix="hsc-parent-report-local-")); run(target)
