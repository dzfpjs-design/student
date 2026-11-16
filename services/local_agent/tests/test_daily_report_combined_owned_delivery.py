"""Owned sender with synthetic database and loopback SMTP only."""
import pytest
from home_study_agent import daily_report_combined_owned_delivery as owned, daily_report_combined_delivery as combined, smtp_owned_process, smtp_owned_journal
from test_combined_report_real_sources_fixture import synthetic_combined_family
from test_daily_report_combined_snapshot import candidate
from test_daily_report_combined_delivery import configured
from test_combined_report_loopback_candidate import sink


def _test_owner(monkeypatch):
    monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
    monkeypatch.setattr(smtp_owned_process,'_process_identity',lambda process:{'pid':process.pid,'windows_creation_time_100ns':123456789})
    monkeypatch.setattr(smtp_owned_process,'_job_members',lambda job,process:[{'pid':process.pid,'windows_creation_time_100ns':123456789,'role':'worker','image_path':'C:\\synthetic\\worker.exe'}],raising=False)
    monkeypatch.setattr(smtp_owned_process,'_job_exited',lambda job:True,raising=False)
    monkeypatch.setattr(smtp_owned_journal,'probe_windows_original',lambda *args:'dead')
    monkeypatch.setattr(smtp_owned_journal,'recover_named_job',lambda *args:True,raising=False)


def test_owned_success_commits_journal_and_all_three_ledgers(candidate,monkeypatch):
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch)
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        assert owned.submit(service,f['telemetry'],snapshot,timeout_seconds=5)=={'status':'sent'}
        assert len(smtp.messages)==1
    with f['db'].connect() as c:
        assert [c.execute('SELECT status FROM '+t).fetchone()[0] for t in ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member')]==['sent']*3
        assert tuple(c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone())==('stopped','accepted')
        assert c.execute('SELECT claim_token FROM combined_daily_delivery').fetchone()[0]==c.execute('SELECT dispatch_id FROM smtp_owned_worker_journal').fetchone()[0]


def test_journal_insert_failure_rolls_back_all_claims_before_go(candidate,monkeypatch):
    from home_study_agent.database import StudyConnection
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch)
    failed_connection=[]; rollbacks=[]
    original_claim=owned._claim; original_rollback=StudyConnection.rollback
    def observe_claim(c,*args,**kwargs):
        try:
            return original_claim(c,*args,**kwargs)
        except BaseException:
            failed_connection.append(id(c))
            raise
    def observe_rollback(c):
        rollbacks.append(id(c))
        return original_rollback(c)
    monkeypatch.setattr(owned,'_claim',observe_claim)
    monkeypatch.setattr(StudyConnection,'rollback',observe_rollback)
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        with f['db'].connect() as c:
            c.execute("CREATE TRIGGER synthetic_fail_journal BEFORE INSERT ON smtp_owned_worker_journal BEGIN SELECT RAISE(ABORT,'synthetic'); END");c.commit()
        assert owned.submit(service,f['telemetry'],snapshot,timeout_seconds=5)=={'status':'blocked','reason':'SMTP_WORKER_ADMISSION_FAILED'}
        assert len(failed_connection)==1 and failed_connection[0] in rollbacks
        assert not smtp.messages and smtp.data_commands==0
    with f['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM combined_daily_delivery').fetchone()[0]==0
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0]=='ready'
        assert c.execute('SELECT status FROM combined_diagnostic_member').fetchone()[0]=='waiting'
        assert c.execute('SELECT count(*) FROM smtp_owned_worker_journal').fetchone()[0]==0


@pytest.mark.parametrize('scenario', ['admission_failure', 'lost_reply', 'accepted'])
def test_cleanup_pending_recovers_from_durable_ledgers_without_duplicate(candidate, monkeypatch, scenario):
    import os
    from threading import Event
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch)
    job_type = smtp_owned_process._WindowsJob if os.name=='nt' else smtp_owned_process._LoopbackPosixJob
    real_active=job_type.active; entered=Event(); release=Event()
    def stalled(job):
        entered.set(); release.wait(15)
        return real_active(job)
    monkeypatch.setattr(job_type,'active',stalled)
    try:
        with sink(lose_reply=scenario=='lost_reply') as smtp:
            service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
            if scenario=='admission_failure':
                with f['db'].connect() as c:
                    c.execute("CREATE TRIGGER synthetic_fail_journal BEFORE INSERT ON smtp_owned_worker_journal BEGIN SELECT RAISE(ABORT,'synthetic'); END");c.commit()
            result=owned.submit(service,f['telemetry'],snapshot,timeout_seconds=5 if os.name=='nt' else 1.5)
            assert entered.is_set() and result=={'status':'cleanup_pending'}
            assert owned.cleanup_status_for_child(service,f['telemetry'],f['child'])=={'status':'cleanup_pending'}
            assert owned.status(service,f['telemetry'],snapshot)=={'status':'cleanup_pending'}
            assert owned.submit(service,f['telemetry'],snapshot,timeout_seconds=1)=={'status':'cleanup_pending'}
            key=owned._pending_key(service,snapshot)
            saved=owned._pending_owners.pop(key)
            # A different process has no registry: the OS effect lock still
            # prevents it from declaring the old owner stopped.
            assert owned.status(service,f['telemetry'],snapshot)=={'status':'cleanup_pending'}
            owned._pending_owners[key]=saved
            assert smtp.data_commands == (0 if scenario=='admission_failure' else 1)
            pending=owned._pending_owners[owned._pending_key(service,snapshot)][0]
            release.set(); pending.thread.join(8)
            assert not pending.thread.is_alive()
            expected={'admission_failure':'not_submitted','lost_reply':'unknown','accepted':'sent'}[scenario]
            assert owned.status(service,f['telemetry'],snapshot)['status']==expected
            # No in-memory owner survives an application restart: same result
            # must follow from the journal, ledger and current frozen source.
            owned._pending_owners.clear()
            assert owned.status(service,f['telemetry'],snapshot)['status']==expected
            if scenario=='admission_failure':
                with f['db'].connect() as c:
                    c.execute("UPDATE daily_observation_delivery_ledger SET status='processing'"); c.commit()
                assert owned.status(service,f['telemetry'],snapshot)['status']=='blocked'
            if scenario!='admission_failure':
                with pytest.raises(ValueError):
                    owned.submit(service,f['telemetry'],snapshot,timeout_seconds=2)
            assert smtp.data_commands == (0 if scenario=='admission_failure' else 1)
    finally:
        release.set()


def test_preflight_revocation_prevents_even_smtp_connection(candidate,monkeypatch):
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch);connections=[]
    with sink(on_connect=lambda:connections.append(True)) as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        original=owned._mime
        def revoked(view):
            result=original(view);service.revoke_recipient(f['recipient']);return result
        monkeypatch.setattr(owned,'_mime',revoked)
        assert owned.submit(service,f['telemetry'],snapshot,timeout_seconds=5)['status']=='blocked'
        assert not connections and not smtp.messages


@pytest.mark.parametrize('recoverable',[True,False])
def test_named_job_recovery_is_atomic_and_fails_closed(candidate,monkeypatch,recoverable):
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch)
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        with f['db'].external_effect_guard(),f['db'].connect() as c:
            c.execute('BEGIN IMMEDIATE');view=owned._view(c,service,f['telemetry'],snapshot)
            from types import SimpleNamespace
            process=SimpleNamespace(pid=123,poll=lambda:None)
            job=smtp_owned_process._LoopbackPosixJob();job.assign(process)
            identity={'pid':123,'windows_creation_time_100ns':123456789}
            members=[{**identity,'role':'worker','image_path':'C:\\synthetic\\worker.exe'}]
            owned._claim(c,service,f['telemetry'],snapshot,view,'a'*32,identity,members,job=job,process=process);c.commit()
        monkeypatch.setattr(smtp_owned_journal,'recover_named_job',lambda *args:recoverable)
        if recoverable:
            with f['db'].external_effect_guard():pass
        else:
            from home_study_agent.database import ExternalEffectBusyError
            with pytest.raises(ExternalEffectBusyError):
                with f['db'].external_effect_guard():pass
        import sqlite3
        with sqlite3.connect(f['db'].path) as c:
            state='unknown' if recoverable else 'processing'
            assert [c.execute('SELECT status FROM '+t).fetchone()[0] for t in ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member')]==[state]*3
            assert tuple(c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone())==(('stopped','unknown') if recoverable else ('pending',None))
        assert not smtp.messages


def test_owner_rejects_call_from_thread_already_holding_effect_lock(candidate,monkeypatch):
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch)
    with f['db'].external_effect_guard():
        with pytest.raises(ValueError,match='SMTP_OWNER_LOCK_ORDER_INVALID'):
            owned.submit(f['delivery'],f['telemetry'],snapshot,timeout_seconds=1)


def test_change_before_ready_prevents_data_and_claim(candidate,monkeypatch):
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch)
    def change():
        with f['db'].connect() as c:
            c.execute("UPDATE parent_report_snapshot SET status='revoked' WHERE snapshot_id=?",(f['formal']['snapshot_id'],));c.commit()
    with sink(on_connect=change) as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        result=owned.submit(service,f['telemetry'],snapshot,timeout_seconds=5)
        assert result['status']=='blocked' and smtp.data_commands==0 and not smtp.messages
        with f['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM combined_daily_delivery').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM smtp_owned_worker_journal').fetchone()[0]==0


def test_real_scheduler_runs_owned_process_without_nested_lock(synthetic_combined_family,monkeypatch):
    from home_study_agent.daily_report_combined_schedule import run_due
    from test_daily_report_combined_schedule import _schedule_fixture
    f=synthetic_combined_family;_test_owner(monkeypatch)
    with sink() as smtp:
        service,when=_schedule_fixture(f,monkeypatch,smtp.server_address[1])
        assert run_due(service,f['telemetry'],now=when)['status']=='sent'
        assert len(smtp.messages)==2
        with f['db'].connect() as c:
            assert c.execute("SELECT count(*) FROM smtp_owned_worker_journal WHERE status='stopped' AND result='accepted'").fetchone()[0]==2
            assert c.execute("SELECT count(*) FROM combined_daily_delivery WHERE status='sent'").fetchone()[0]==2


def test_result_commit_failure_keeps_journal_and_three_claims_atomic(candidate,monkeypatch):
    import sqlite3
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch)
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        with f['db'].connect() as c:
            c.execute("CREATE TRIGGER synthetic_fail_finish BEFORE UPDATE OF status ON smtp_owned_worker_journal WHEN NEW.status='stopped' BEGIN SELECT RAISE(ABORT,'synthetic'); END");c.commit()
        assert owned.submit(service,f['telemetry'],snapshot,timeout_seconds=5)=={'status':'unknown'}
        assert len(smtp.messages)==1
        with sqlite3.connect(f['db'].path) as c:
            assert [c.execute('SELECT status FROM '+t).fetchone()[0] for t in ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member')]==['processing']*3
            assert c.execute('SELECT status FROM smtp_owned_worker_journal').fetchone()[0]=='pending'
            c.execute('DROP TRIGGER synthetic_fail_finish');c.commit()
        with f['db'].external_effect_guard():pass
        with f['db'].connect() as c:
            assert [c.execute('SELECT status FROM '+t).fetchone()[0] for t in ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member')]==['unknown']*3
            assert tuple(c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone())==('stopped','unknown')
        assert len(smtp.messages)==1
