"""Owned-worker cleanup faults: no weights or Windows native execution."""
import io,subprocess,threading,time,sys
from contextlib import ExitStack,contextmanager
from pathlib import Path
from types import SimpleNamespace
import pytest
from home_study_agent import pet_multipa_process as p

class Process:
    def __init__(self,wait_fails=False):self.stdin=io.BytesIO();self.stdout=io.BytesIO(b'{}');self.returncode=None;self.waits=0;self.wait_fails=wait_fails
    def poll(self):return self.returncode
    def wait(self,timeout=None):
        self.waits+=1
        if self.wait_fails:raise subprocess.TimeoutExpired('fixed-test-only',timeout)
        self.returncode=20;return 20
    def kill(self):pass
class Job:
    def __init__(self,terminate_fails=False,query_fails=False):self.terminate_fails=terminate_fails;self.query_fails=query_fails;self.closed=False;self.assigns=0;self.active_count=0
    def assign(self,process):self.assigns+=1
    def terminate(self):
        if self.terminate_fails:raise OSError('SYNTHETIC_NATIVE_FAILURE_NO_PRIVATE_DATA')
    def active(self):
        if self.query_fails:raise OSError('SYNTHETIC_NATIVE_FAILURE_NO_PRIVATE_DATA')
        return self.active_count
    def close(self):self.closed=True

def test_terminate_failure_does_not_skip_original_wait_join():
    process=Process();job=Job(terminate_fails=True)
    with pytest.raises(ValueError,match='WORKER_CANCELLED'):
        p._communicate_owned(process,b'',deadline=time.monotonic()+2,should_stop=lambda:True,job=job)
    assert process.waits==1 and process.returncode==20 and process.stdout.closed

def test_query_failure_waits_but_retains_unknown_state():
    process=Process();job=Job(query_fails=True,terminate_fails=True)
    with pytest.raises(p._DrainUnconfirmed)as error:
        p._communicate_owned(process,b'',deadline=time.monotonic()+2,should_stop=lambda:True,job=job)
    assert process.waits==1 and all(not t.is_alive()for t in error.value.threads)
    assert not process.stdout.closed and not job.closed

def test_wait_failure_not_mistaken_for_process_gone():
    process=Process(wait_fails=True);job=Job()
    with pytest.raises(p._DrainUnconfirmed):p._communicate_owned(process,b'',deadline=time.monotonic()+2,should_stop=lambda:True,job=job)
    assert process.waits==1 and process.returncode is None and not process.stdout.closed

def test_quarantine_strong_ownership_and_exclusive_lock_until_job_zero():
    process=Process();job=Job();job.active_count=1;closed=[];stack=ExitStack();stack.callback(lambda:closed.append('original-held-epoch'))
    assert p._WORKER_LOCK.acquire(blocking=False)
    p._quarantine(stack.pop_all(),process,job,[],release_lock=True)
    assert p._QUARANTINE and closed==[]and not job.closed and not p._WORKER_LOCK.acquire(blocking=False)
    process.returncode=20;time.sleep(.15)
    assert closed==[]and not job.closed # main process gone alone is insufficient
    job.active_count=0
    until=time.monotonic()+2
    while p._QUARANTINE and time.monotonic()<until:time.sleep(.02)
    assert p._QUARANTINE==[]and closed==['original-held-epoch']and job.closed
    assert p._WORKER_LOCK.acquire(blocking=False);p._WORKER_LOCK.release()

@pytest.mark.parametrize('reason',['cancel','timeout'])
def test_actual_fixed_worker_transport_still_drains_owned_original(reason):
    script=Path(p.__file__).with_name('pet_multipa_worker.py')
    process=subprocess.Popen([sys.executable,'-I','-S','-B',str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True)
    with pytest.raises(ValueError,match='CANCELLED'if reason=='cancel'else'TIMEOUT'):
        p._communicate_owned(process,b'',deadline=time.monotonic()-1 if reason=='timeout'else time.monotonic()+2,should_stop=lambda:reason=='cancel')
    assert process.poll()is not None and process.stdin.closed and process.stdout.closed

def test_real_host_orchestration_transfers_lease_on_unknown_not_success(tmp_path,monkeypatch):
    approved=dict(timeout_seconds=1,memory_bytes=268435456,private_work_root=str(tmp_path),actor_sid='SYNTHETIC_ONLY')
    scratch=tmp_path/('multipa-'+'a'*32);scratch.mkdir();events=[];job=Job(query_fails=True);process=Process()
    @contextmanager
    def lease(key):
        events.append('lease-held')
        try:yield SimpleNamespace(doc={'epoch':'test-only'},path=lambda role:Path('fixed-'+role),check=lambda:None)
        finally:events.append('lease-released')
    @contextmanager
    def scope(root,actor):
        events.append('scratch-held')
        try:yield scratch
        finally:events.append('scratch-removed')
    monkeypatch.setattr(p,'configured',lambda key:approved);monkeypatch.setattr(p,'WindowsEpochLease',lease);monkeypatch.setattr(p,'private_scope',scope)
    monkeypatch.setattr(p,'_WindowsWorkerJob',lambda memory:job);monkeypatch.setattr(p.subprocess,'CREATE_NO_WINDOW',0,raising=False)
    def create(args,**kwargs):
        assert args[0]=='fixed-python_executable'and args[-1]=='fixed-worker_script'and args[1:4]==['-I','-S','-B'];return process
    monkeypatch.setattr(p.subprocess,'Popen',create)
    def communicate(*args,**kwargs):
        assert job.assigns==1;raise p._DrainUnconfirmed([])
    monkeypatch.setattr(p,'_communicate_owned',communicate)
    with pytest.raises(ValueError,match='CLEANUP_UNCONFIRMED'):p.predict_in_worker('test-only',b'wave')
    assert events==['lease-held','scratch-held']and p._QUARANTINE and not job.closed
    process.returncode=20;job.query_fails=False
    until=time.monotonic()+2
    while p._QUARANTINE and time.monotonic()<until:time.sleep(.02)
    assert events==['lease-held','scratch-held','scratch-removed','lease-released']and p._QUARANTINE==[]and job.closed
    assert p._WORKER_LOCK.acquire(blocking=False);p._WORKER_LOCK.release()
