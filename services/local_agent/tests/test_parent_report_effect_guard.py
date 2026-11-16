"""Synthetic report changes serialize with fake SMTP and preserve lock order."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from home_study_agent.notification_channels import ChannelReceipt, _check_submission
from home_study_agent.database import ExternalEffectBusyError
from home_study_agent.parent_reports import ReportEffectBusyError, _report_effect_guard
from test_report_auto_recipient_verification import setup


@pytest.mark.parametrize('change',['revoke','correction','source_issue','reconcile'])
def test_report_mutation_waits_for_transport_without_holding_sqlite_writer(tmp_path,monkeypatch,change):
    db,child,reports,snapshot,delivery,_,recipient,_=setup(tmp_path)
    reports.approve(snapshot['snapshot_id'])
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _:False)
    queued=delivery.enqueue(snapshot['snapshot_id'],recipient)
    reached, release, starting, finished = Event(), Event(), Event(), Event()
    class FakeSMTP:
        def send(self,*args):
            _check_submission()
            reached.set()
            assert release.wait(5)
            return ChannelReceipt(provider_message_id='synthetic')
    delivery.adapters['email']=FakeSMTP()
    with db.connect() as c:
        report_id=c.execute('SELECT report_id FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot['snapshot_id'],)).fetchone()[0]
    def mutate():
        starting.set()
        try:
            if change=='revoke': return reports.revoke(snapshot['snapshot_id'],'synthetic')
            if change=='correction': return reports.request_correction(snapshot['snapshot_id'],'synthetic')
            if change=='source_issue': return reports.record_source_issue(child,'daily_report',report_id,'REPORT_SOURCE_INVALID_JSON')
            return reports.reconcile_daily_sources(child)
        finally:
            finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        sender=pool.submit(delivery._process_one,queued['delivery_id'])
        try:
            assert reached.wait(5)
            writer=pool.submit(mutate)
            assert starting.wait(5)
            assert not finished.wait(.2)
            # If reconcile starts its transaction before the effect guard, this
            # independent write cannot proceed during the paused fake SMTP call.
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE'); c.rollback()
        finally:
            release.set()
        assert sender.result(timeout=10)['status']=='sent'
        writer.result(timeout=10)
        assert finished.is_set()


def test_borrowed_issue_requires_same_database_active_guard_and_transaction(tmp_path):
    db,child,reports,snapshot,_,_,_,_=setup(tmp_path)
    report_id=snapshot['report_id']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError,match='REPORT_BORROWED_TRANSACTION_GUARD_REQUIRED'):
            reports.record_source_issue(child,'daily_report',report_id,'REPORT_SOURCE_INVALID_JSON',_connection=c)
        c.rollback()
    with db.external_effect_guard(),db.connect() as c:
        with pytest.raises(ValueError,match='REPORT_BORROWED_TRANSACTION_GUARD_REQUIRED'):
            reports.record_source_issue(child,'daily_report',report_id,'REPORT_SOURCE_INVALID_JSON',_connection=c)
        c.execute('BEGIN IMMEDIATE')
        result=reports.record_source_issue(child,'daily_report',report_id,'REPORT_SOURCE_INVALID_JSON',_connection=c)
        assert result['status']=='open'
        c.rollback()
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM audit_event WHERE action='parent_report_source_issue'").fetchone()


def test_only_lock_acquisition_busy_is_reported_as_not_started():
    class Database:
        def __init__(self, fail): self.fail=fail
        @contextmanager
        def external_effect_guard(self,timeout):
            if self.fail: raise ExternalEffectBusyError('before entry')
            yield
    with pytest.raises(ReportEffectBusyError):
        with _report_effect_guard(Database(True)): pytest.fail('must not enter')
    with pytest.raises(ExternalEffectBusyError, match='after entry'):
        with _report_effect_guard(Database(False)): raise ExternalEffectBusyError('after entry')
