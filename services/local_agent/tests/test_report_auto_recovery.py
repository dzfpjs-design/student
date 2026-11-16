from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_auto_delivery import recover_saved_daily_reports, verify_and_schedule_reports
from home_study_agent.report_delivery import DeliveryStateError, ReportDeliveryService
from home_study_agent.repository import utc_now
from test_report_delivery import _daily_report, RecordingEmail
import pytest
import threading


def test_saved_daily_report_recovers_after_setup_restart_without_replaying_unknown(tmp_path):
    database = Database(tmp_path / 'recovery.db'); database.initialize()
    report = _daily_report(database)
    snapshots = ParentReportService(database)
    snapshot = snapshots.create_from_daily(report)
    delivery = ReportDeliveryService(database, b'R' * 32); delivery.initialize()
    child = report['child_id']
    recipient = delivery.add_recipient(child, 'email', 'parent@example.test', '家长', True)
    assert recover_saved_daily_reports(snapshots, delivery, child)['blocked'] == ['EMAIL_INITIALIZATION_INCOMPLETE']
    assert snapshots.get(snapshot['snapshot_id'])['status'] == 'draft'
    delivery.save_channel_config('email', {'host':'127.0.0.1','port':2525,'from_address':'report@example.test','use_ssl':False,'starttls':False})
    delivery.adapters['email'] = RecordingEmail()
    assert delivery.test_channel(child,'email','synthetic-verified-recipient',recipient['recipient_id'])['status'] == 'passed'
    restarted_db = Database(database.path)
    restarted_delivery = ReportDeliveryService(restarted_db, b'R' * 32)
    restarted_snapshots = ParentReportService(restarted_db)
    recovered = recover_saved_daily_reports(restarted_snapshots, restarted_delivery, child)
    assert recovered['scheduled'] == 1
    with restarted_db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET status='unknown' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
        c.commit()
    recover_saved_daily_reports(restarted_snapshots, restarted_delivery, child)
    with restarted_db.connect() as c:
        rows = c.execute('SELECT status FROM report_delivery_outbox WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchall()
        assert [row['status'] for row in rows] == ['unknown']
    with restarted_db.connect() as c:
        columns = [row['name'] for row in c.execute('PRAGMA table_info(parent_report_snapshot)')]
        values = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
        values['snapshot_id'], values['source_id'] = 'new-snapshot-version', 'new-source-version'
        c.execute('INSERT INTO parent_report_snapshot (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', [values[key] for key in columns])
        c.commit()
    with pytest.raises(DeliveryStateError):
        restarted_delivery.enqueue('new-snapshot-version', recipient['recipient_id'], automatic=True)
    # Hold a real SQLite write transaction while another thread attempts enqueue.
    # Once the uncertain status commits, enqueue must observe it in its own
    # BEGIN IMMEDIATE transaction, rather than act on a stale earlier check.
    writing, release, attempted = threading.Event(), threading.Event(), threading.Event()
    outcomes = []
    def commit_uncertain():
        with restarted_db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE report_delivery_outbox SET status='unknown' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
            writing.set()
            assert release.wait(5)
            c.commit()
    def attempt_enqueue():
        attempted.set()
        try:
            restarted_delivery.enqueue('new-snapshot-version', recipient['recipient_id'], automatic=True)
            outcomes.append('queued')
        except DeliveryStateError:
            outcomes.append('blocked')
    with restarted_db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET status='scheduled' WHERE snapshot_id=?", (snapshot['snapshot_id'],)); c.commit()
    writer = threading.Thread(target=commit_uncertain)
    contender = threading.Thread(target=attempt_enqueue)
    writer.start(); assert writing.wait(5)
    contender.start(); assert attempted.wait(5)
    release.set(); writer.join(5); contender.join(5)
    assert not writer.is_alive() and not contender.is_alive() and outcomes == ['blocked']
    with restarted_db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET status='scheduled' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
        c.commit()
    pending = restarted_delivery.enqueue('new-snapshot-version', recipient['recipient_id'], automatic=True)
    # Simulate the earlier submission becoming uncertain after the later row was queued.
    with restarted_db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET status='unknown' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
        c.commit()
    cancelled = restarted_delivery._process_one(pending['delivery_id'])
    assert cancelled['status'] == 'cancelled' and cancelled['cancel_reason'] == 'prior_delivery_unresolved'
    restarted_delivery.revoke_recipient(recipient['recipient_id'])
    recover_saved_daily_reports(restarted_snapshots, restarted_delivery, child)
    with restarted_db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM report_delivery_outbox WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()[0] == 1
