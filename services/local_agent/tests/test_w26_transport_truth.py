import socketserver
import threading
from contextlib import contextmanager

import httpx
import pytest
from normal_provider_owner import normal_provider_owner

from home_study_agent.notification_channels import ChannelDeliveryError, SmtpChannel, WeComBotChannel
from home_study_agent.report_delivery import ReportDeliveryService, DeliveryStateError
from test_daily_report_evidence_versions import CHILD, flow


@contextmanager
def smtp_peer(mode):
    accepted = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'220 localhost\r\n')
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                command = line.upper()
                if command.startswith(b'DATA'):
                    self.wfile.write(b'354 continue\r\n')
                    data = []
                    while True:
                        part = self.rfile.readline()
                        if part in (b'.\r\n', b''):
                            break
                        data.append(part)
                    accepted.append(b''.join(data))
                    if mode == 'lost_data_ack':
                        return
                    self.wfile.write(b'250 queued\r\n')
                elif command.startswith(b'QUIT'):
                    if mode != 'lost_quit_ack':
                        self.wfile.write(b'221 bye\r\n')
                    return
                else:
                    self.wfile.write(b'250 OK\r\n')

    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield dict(host='127.0.0.1', port=server.server_address[1], starttls=False,
                       from_address='sender@example.invalid', allow_external_delivery=True), accepted
        finally:
            server.shutdown()
            thread.join()


def test_real_smtp_lost_data_ack_is_unknown_not_retryable(normal_provider_owner):
    with smtp_peer('lost_data_ack') as (config, accepted):
        with pytest.raises(ChannelDeliveryError) as error:
            SmtpChannel().send('parent@example.invalid', 'Synthetic', 'Synthetic', '<p>Synthetic</p>', config)
        assert len(accepted) == 1
        assert error.value.delivery_unknown is True
        assert error.value.retryable is False


def test_real_smtp_accepted_message_has_id_even_if_quit_disconnects(normal_provider_owner):
    with smtp_peer('lost_quit_ack') as (config, accepted):
        receipt = SmtpChannel().send('parent@example.invalid', 'Synthetic', 'Synthetic', '<p>Synthetic</p>', config)
        assert len(accepted) == 1
        assert receipt.client_message_id
        assert receipt.provider_message_id is None


@pytest.mark.parametrize('body', [{}, [], {'errcode': False}, {'errcode': '0'}])
def test_wecom_missing_or_ill_typed_acceptance_is_unknown(monkeypatch, body, normal_provider_owner):
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: httpx.Response(200, json=body))
    with pytest.raises(ChannelDeliveryError) as error:
        WeComBotChannel().send('local label', 'Synthetic', 'Synthetic', '', {
            'webhook_url': 'https://example.invalid/no-network', 'allow_external_delivery': True})
    assert error.value.delivery_unknown is True
    assert error.value.retryable is False


@pytest.mark.parametrize('lost_receipt', ['smtp_ack', 'database_receipt'])
def test_real_protocol_outbox_never_automatically_reissues_unknown(flow, monkeypatch, lost_receipt, normal_provider_owner):
    db, repo, report, delivery = flow
    monkeypatch.setattr(ReportDeliveryService, '_in_quiet_hours', lambda *a: False)
    snapshot = delivery.snapshots.create_from_daily(report)
    delivery.snapshots.approve(snapshot['snapshot_id'])
    recipient = delivery.add_recipient(CHILD, 'email', 'parent@example.invalid', 'Synthetic parent', True)
    with smtp_peer('lost_data_ack' if lost_receipt == 'smtp_ack' else 'normal') as (config, accepted):
        delivery.save_channel_config('email', config)
        queued = delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
        if lost_receipt == 'database_receipt':
            def fail_receipt(*args):
                raise RuntimeError('Synthetic local receipt failure')
            monkeypatch.setattr(delivery, '_finish', fail_receipt)
        result = delivery.process_due()[0]
        assert result['status'] == result['channel_acceptance'] == 'unknown'
        assert result['human_delivery_verified'] is False
        assert len(accepted) == 1
        assert delivery.process_due() == []
        restarted = ReportDeliveryService(db, b'D' * 32)
        restarted.initialize()
        assert restarted.process_due() == []
        with pytest.raises(DeliveryStateError):
            restarted.retry(queued['delivery_id'])
        with pytest.raises(DeliveryStateError):
            restarted.cancel(queued['delivery_id'], 'Must not launder unknown into retryable cancellation')
        assert len(accepted) == 1


def test_smtp_definite_connection_failure_remains_retryable(normal_provider_owner):
    with socketserver.TCPServer(('127.0.0.1', 0), socketserver.BaseRequestHandler) as server:
        port = server.server_address[1]
    with pytest.raises(ChannelDeliveryError) as error:
        SmtpChannel().send('parent@example.invalid', 'Synthetic', 'Synthetic', '', dict(
            host='127.0.0.1', port=port, starttls=False, from_address='sender@example.invalid', allow_external_delivery=True))
    assert error.value.delivery_unknown is False
    assert error.value.retryable is True
