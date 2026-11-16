"""Synthetic exclusion cannot race a report's SMTP submission."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.notification_channels import ChannelReceipt, _check_submission
from home_study_agent.parent_reports import ParentReportService, ReportEffectBusyError
from home_study_agent.report_delivery import ReportDeliveryService
from test_daily_learning_closure_audit import learning_clock
from test_period_report_reliable_evidence import flow, answer, case_for, report, CHILD


def test_exclusion_busy_during_fake_smtp_then_same_action_succeeds(flow,monkeypatch):
    f=flow; case=case_for(f,answer(f))
    parents=ParentReportService(f.db)
    saved=parents.approve(parents.create_from_period(report(f))['snapshot_id'])
    delivery=ReportDeliveryService(f.db,b'v'*32); delivery.initialize()
    delivery.save_channel_config('email',{'host':'127.0.0.1','port':25,'from_address':'synthetic@example.test'})
    recipient=delivery.add_recipient(CHILD,'email','parent@example.test','synthetic',True)['recipient_id']
    delivery_id=delivery.enqueue(saved['snapshot_id'],recipient)['delivery_id']
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _:False)
    real_guard=f.db.external_effect_guard
    monkeypatch.setattr(f.db,'external_effect_guard',lambda timeout=120,**kw:real_guard(timeout=.15,**kw))
    reached,release=Event(),Event()
    class SMTP:
        def send(self,*args):
            _check_submission();reached.set();assert release.wait(10)
            return ChannelReceipt(provider_message_id='synthetic')
    delivery.adapters['email']=SMTP()
    book=MistakeBookService(f.db)
    with ThreadPoolExecutor(max_workers=1) as pool:
        sending=pool.submit(delivery._process_one,delivery_id)
        try:
            assert reached.wait(20)
            with pytest.raises(ReportEffectBusyError): book.confirm(case,CHILD,'exclude','same-action')
            with f.db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert c.execute('SELECT status FROM mistake_case WHERE mistake_id=?',(case,)).fetchone()[0]!='excluded'
                c.rollback()
        finally: release.set()
        assert sending.result(timeout=15)['status']=='sent'
    result=book.confirm(case,CHILD,'exclude','same-action')
    assert result['status']=='excluded'
    assert book.confirm(case,CHILD,'exclude','same-action')['status']=='excluded'


def test_new_mistake_confirmation_does_not_wait_for_smtp_guard(flow):
    f=flow; case=case_for(f,answer(f)); book=MistakeBookService(f.db)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with f.db.external_effect_guard():
            result=pool.submit(book.confirm,case,CHILD,'confirm','synthetic-confirm').result(timeout=5)
    assert result['status']=='diagnosing'
