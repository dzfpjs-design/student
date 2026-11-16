"""Foreign keys alone do not prove a queued delivery belongs to its recipient."""
import pytest

from home_study_agent.report_delivery import ReportDeliveryService
from test_daily_report_evidence_versions import flow, CHILD
from test_report_delivery import RecordingEmail


@pytest.mark.parametrize('corruption', ['child', 'channel'])
def test_legacy_queue_scope_mismatch_is_preserved_but_never_sent(flow, monkeypatch, corruption):
    db, _, daily, delivery = flow
    snapshot = delivery.snapshots.approve(delivery.snapshots.create_from_daily(daily)['snapshot_id'])
    recipient = delivery.add_recipient(CHILD, 'email', 'parent@example.test', 'Parent', True)
    queued = delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    delivery.save_channel_config('email', {'host': '127.0.0.1', 'from_address': 'sender@example.test',
        'starttls': False, 'allow_external_delivery': True})
    # An imported historical row can satisfy every FK while violating ownership.
    with db.connect() as c:
        if corruption == 'child':
            c.execute('UPDATE report_delivery_recipient SET child_id=? WHERE recipient_id=?',
                ('child-junior-3', recipient['recipient_id']))
        else:
            c.execute("UPDATE report_delivery_recipient SET channel='wechat_work_bot' WHERE recipient_id=?",
                (recipient['recipient_id'],))
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        c.commit()
    adapter = RecordingEmail()
    delivery.adapters['email'] = adapter
    monkeypatch.setattr(delivery, '_in_quiet_hours', lambda _: False)
    delivery.process_due()
    assert adapter.messages == []
    assert delivery.get_delivery(queued['delivery_id'])['status'] == 'cancelled'
    reopened = ReportDeliveryService(db, b'D' * 32)
    reopened.initialize()
    assert reopened.process_due() == []
    assert reopened.get_delivery(queued['delivery_id'])['status'] == 'cancelled'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM report_delivery_outbox WHERE delivery_id=?',
            (queued['delivery_id'],)).fetchone()[0] == 1
