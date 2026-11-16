"""Known local admission is distinct from an uncertain provider submission."""
import pytest

from home_study_agent.money_egress import BudgetHold
from home_study_agent.notification_channels import ChannelDeliveryError, SmtpChannel, WeComBotChannel
from home_study_agent.parent_work_queue import ParentWorkQueueService
from normal_provider_owner import normal_provider_owner
from test_daily_report_evidence_versions import flow, CHILD
from test_report_delivery_recovery_journey import smtp, prepare


def test_smtp_local_budget_rejection_is_not_an_unknown_submission(flow, smtp, normal_provider_owner, monkeypatch):
    db, _, _, delivery = flow
    snapshot, recipient = prepare(flow, smtp, 'daily')
    queued = delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    connections = []
    import socket
    original = socket.create_connection
    def observe(*args, **kwargs):
        connections.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(socket, 'create_connection', observe)
    normal_provider_owner.close()
    result = delivery.process_due()[0]
    assert result['status'] == 'failed'
    assert result['last_error_code'] == 'NOTIFICATION_BUDGET_ADMISSION_BLOCKED'
    assert result['channel_acceptance'] == 'not_confirmed'
    assert connections == [] and smtp.submissions == []
    assert not any(i['object_id'] == queued['delivery_id'] for i in ParentWorkQueueService(db).list(child_id=CHILD)['items'])
    assert delivery.process_due() == [] and smtp.submissions == []


def test_wecom_local_budget_rejection_is_typed_before_http(normal_provider_owner, monkeypatch):
    import httpx
    requests = []
    def forbidden(*args, **kwargs):
        requests.append(True)
        raise AssertionError('HTTP_MUST_NOT_BEGIN')
    monkeypatch.setattr(httpx, 'post', forbidden)
    normal_provider_owner.close()
    with pytest.raises(ChannelDeliveryError) as caught:
        WeComBotChannel().send('public-local-label', 'subject', 'text', '', {})
    assert caught.value.code == 'NOTIFICATION_BUDGET_ADMISSION_BLOCKED'
    assert caught.value.retryable is False and caught.value.delivery_unknown is False
    assert requests == []


def test_budget_hold_after_real_smtp_data_remains_unknown_and_never_resends(flow, smtp, normal_provider_owner):
    db, _, _, delivery = flow
    snapshot, recipient = prepare(flow, smtp, 'daily')
    queued = delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    class AcceptedThenHold:
        channel = 'email'
        def send(self, *args, **kwargs):
            SmtpChannel().send(*args, **kwargs)
            raise BudgetHold('MONEY_OWNER_CLOSED')
    delivery.adapters['email'] = AcceptedThenHold()
    result = delivery.process_due()[0]
    assert result['status'] == 'unknown' and len(smtp.submissions) == 1
    assert result['channel_acceptance'] == 'unknown'
    assert any(i['object_id'] == queued['delivery_id'] for i in ParentWorkQueueService(db).list(child_id=CHILD)['items'])
    assert delivery.process_due() == [] and len(smtp.submissions) == 1
    assert delivery.keep_unknown(queued['delivery_id'])['status'] == 'unknown_acknowledged'
    assert delivery.process_due() == [] and len(smtp.submissions) == 1


def test_unknown_adapter_budget_hold_is_still_conservatively_unknown(flow, smtp, normal_provider_owner):
    _, _, _, delivery = flow
    snapshot, recipient = prepare(flow, smtp, 'daily')
    delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    class UnknownAdapter:
        channel = 'email'
        def send(self, *args, **kwargs):
            raise BudgetHold('MONEY_OWNER_CLOSED')
    delivery.adapters['email'] = UnknownAdapter()
    result = delivery.process_due()[0]
    assert result['status'] == 'unknown' and smtp.submissions == []
    assert delivery.process_due() == []
