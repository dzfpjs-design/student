"""Report source mutations wait for synthetic SMTP without reversing lock order."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from home_study_agent.notification_channels import ChannelReceipt, _check_submission
from home_study_agent.repository import StudyRepository
from home_study_agent.parent_reports import ReportEffectBusyError
from test_report_auto_recipient_verification import setup
from test_period_report_reliable_evidence import flow, answer, report, summary, CHILD
from test_daily_learning_closure_audit import learning_clock


@pytest.mark.parametrize('method',['get','save'])
def test_daily_source_waits_for_actual_fake_smtp_before_any_writer(tmp_path,monkeypatch,method):
    db,child,reports,snapshot,delivery,_,recipient,_=setup(tmp_path)
    reports.approve(snapshot['snapshot_id'])
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _:False)
    repo=StudyRepository(db)
    daily=repo.get_daily_report(snapshot['report_id'])
    queued=delivery.enqueue(snapshot['snapshot_id'],recipient)
    reached, release, starting, finished=Event(),Event(),Event(),Event()
    class SMTP:
        def send(self,*args):
            _check_submission(); reached.set(); assert release.wait(5)
            return ChannelReceipt(provider_message_id='synthetic')
    delivery.adapters['email']=SMTP()
    def mutate():
        starting.set()
        try:
            if method=='get': return repo.get_daily_report(daily['report_id'])
            return repo.save_daily_report(daily['session_id'],daily['summary'],daily['parent_coaching'])
        finally: finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        sender=pool.submit(delivery._process_one,queued['delivery_id'])
        try:
            assert reached.wait(5)
            writer=pool.submit(mutate); assert starting.wait(5)
            assert not finished.wait(.2)
            with db.connect() as c: c.execute('BEGIN IMMEDIATE');c.rollback()
        finally: release.set()
        assert sender.result(timeout=10)['status']=='sent'
        assert writer.result(timeout=30)['report_id']==daily['report_id']


@pytest.mark.parametrize('mode',['retract','restore'])
def test_period_evidence_mutation_waits_for_shared_effect_guard(flow,mode):
    f=flow; answer(f)
    period=report(f)
    evidence=summary(period)['evidence'][0]['evidence_ref']
    if mode=='restore': assert f.reports.retract_evidence(CHILD,evidence,'synthetic','first-action')
    starting,finished=Event(),Event()
    def mutate():
        starting.set()
        try:
            action=f.reports.retract_evidence if mode=='retract' else f.reports.restore_evidence
            return action(CHILD,evidence,'synthetic','second-action')
        finally: finished.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with f.db.external_effect_guard():
            writer=pool.submit(mutate); assert starting.wait(5)
            assert not finished.wait(.2)
            with f.db.connect() as c: c.execute('BEGIN IMMEDIATE');c.rollback()
        assert writer.result(timeout=30) is True


def test_period_retraction_busy_is_retryable_with_same_action(flow, monkeypatch):
    f=flow; answer(f)
    period=report(f)
    evidence=summary(period)['evidence'][0]['evidence_ref']
    original=f.db.external_effect_guard
    monkeypatch.setattr(f.db, 'external_effect_guard', lambda timeout=120: original(timeout=.05))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original():
            blocked=pool.submit(f.reports.retract_evidence,CHILD,evidence,'synthetic','same-action')
            with pytest.raises(ReportEffectBusyError):
                blocked.result(timeout=5)
        assert pool.submit(f.reports.retract_evidence,CHILD,evidence,'synthetic','same-action').result(timeout=10) is True


@pytest.mark.parametrize('mode',['generate','refresh'])
def test_period_report_generation_waits_without_opening_writer(flow,mode):
    f=flow; answer(f); period=report(f)
    starting,finished=Event(),Event()
    def mutate():
        starting.set()
        try:
            if mode=='generate': return f.reports.generate(CHILD,'weekly',f.reference)
            return f.reports.get(period['period_report_id'],persist_refresh=True)
        finally: finished.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with f.db.external_effect_guard():
            writer=pool.submit(mutate); assert starting.wait(5)
            assert not finished.wait(.2)
            with f.db.connect() as c: c.execute('BEGIN IMMEDIATE');c.rollback()
        assert writer.result(timeout=30)['period_report_id']==period['period_report_id']
