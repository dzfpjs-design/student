"""The real SMTP adapter must respect changes made during connection setup."""
import pytest
from normal_provider_owner import normal_provider_owner
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import httpx

from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.notification_channels import ChannelSubmissionCancelled, WeComBotChannel, submission_guard
from test_daily_report_evidence_versions import flow, CHILD, retire_source


@pytest.mark.parametrize('change', ['recipient', 'snapshot', 'learning_source', 'configuration', 'redaction',
    'recipient_child', 'recipient_channel', 'recipient_address'])
def test_smtp_login_then_withdrawal_never_submits_report(flow, monkeypatch, change, normal_provider_owner):
    db, repo, daily, delivery = flow
    snapshot = delivery.snapshots.approve(delivery.snapshots.create_from_daily(daily)['snapshot_id'])
    recipient = delivery.add_recipient(CHILD, 'email', 'parent@example.test', 'Parent', True)
    delivery.save_channel_config('email', {'host': '127.0.0.1', 'port': 2525,
        'username': 'local-test', 'password': 'synthetic-531854cef641648c7817441c', 'from_address': 'sender@example.test',
        'starttls': False, 'allow_external_delivery': True})
    monkeypatch.setattr(delivery, '_in_quiet_hours', lambda _: False)
    sent, closed = [], []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, *args):
            if change == 'recipient':
                delivery.revoke_recipient(recipient['recipient_id'])
            elif change == 'snapshot':
                delivery.snapshots.revoke(snapshot['snapshot_id'], 'Correct the advice first')
            elif change == 'learning_source':
                retire_source(db, repo, daily)
            elif change == 'configuration':
                delivery.clear_channel_config('email')
            elif change.startswith('recipient_'):
                column, value = {
                    'recipient_child': ('child_id', 'child-junior-3'),
                    'recipient_channel': ('channel', 'wechat_work_bot'),
                    'recipient_address': ('address', 'different-parent@example.test'),
                }[change]
                with db.connect() as connection:
                    connection.execute(f'UPDATE report_delivery_recipient SET {column}=? WHERE recipient_id=?',
                        (value, recipient['recipient_id']))
                    connection.commit()
            else:
                delivery.update_preferences(CHILD, '21:00', '07:00', 90, 'minimal')

        def send_message(self, message):
            sent.append(message)
            return {}

        def quit(self):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr('home_study_agent.notification_channels.smtplib.SMTP', SMTP)
    queued = delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    delivery.process_due()
    assert sent == []
    assert closed == [True]
    assert delivery.get_delivery(queued['delivery_id'])['status'] == 'cancelled'
    reopened = ReportDeliveryService(db, b'D' * 32)
    reopened.initialize()
    assert reopened.process_due() == []
    assert reopened.get_delivery(queued['delivery_id'])['status'] == 'cancelled'


def test_guard_is_scoped_to_worker_and_restored_after_cancel(monkeypatch, normal_provider_owner):
    barrier = Barrier(2)
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs['json']['text']['content'])
        return httpx.Response(200, json={'errcode': 0})

    monkeypatch.setattr('home_study_agent.notification_channels.httpx.post', post)
    channel = WeComBotChannel()
    config = {'webhook_url': 'https://example.test/local-only', 'allow_external_delivery': True}

    def run(name, reason):
        def check():
            barrier.wait(timeout=5)
            return reason
        with submission_guard(check):
            try:
                channel.send('', name, 'Synthetic report', '', config)
            except ChannelSubmissionCancelled:
                return 'cancelled'
        return 'sent'

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancelled = executor.submit(run, 'Cancelled', 'recipient_consent_revoked')
        allowed = executor.submit(run, 'Allowed', None)
        assert cancelled.result(timeout=10) == 'cancelled'
        assert allowed.result(timeout=10) == 'sent'
    assert sent == ['Allowed\n\nSynthetic report']
    with submission_guard(lambda: 'snapshot_source_changed'):
        with submission_guard(lambda: None):
            channel.send('', 'Nested allowed', '', '', config)
        with pytest.raises(ChannelSubmissionCancelled):
            channel.send('', 'Outer still cancelled', '', '', config)
    channel.send('', 'Outside guard', '', '', config)
    assert len(sent) == 3
