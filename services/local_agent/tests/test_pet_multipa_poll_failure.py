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

def test_poll_and_thread_query_exception_is_unknown_never_escape():
    class Broken:
        def poll(self):raise OSError('SYNTHETIC_WIN_GETEXITCODE_FAILURE')
    class Thread:
        def is_alive(self):raise OSError('SYNTHETIC_THREAD_QUERY_FAILURE')
    assert p._drained(Broken(),Job(),[])is False
    assert p._drained(SimpleNamespace(poll=lambda:20),Job(),[Thread()])is False

def test_host_finally_poll_failure_transfers_original_ownership(tmp_path,monkeypatch):
    approved=dict(timeout_seconds=1,memory_bytes=268435456,private_work_root=str(tmp_path),actor_sid='SYNTHETIC_ONLY')
    scratch=tmp_path/('multipa-'+'b'*32);scratch.mkdir();events=[];job=Job()
    class BrokenPoll(Process):
        broken=True
        def poll(self):
            if self.broken:raise OSError('SYNTHETIC_WIN_POLL_FAILURE')
            return self.returncode
    process=BrokenPoll()
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
    monkeypatch.setattr(p,'_WindowsWorkerJob',lambda memory:job);monkeypatch.setattr(p.subprocess,'CREATE_NO_WINDOW',0,raising=False);monkeypatch.setattr(p.subprocess,'Popen',lambda *a,**kw:process)
    monkeypatch.setattr(p,'_communicate_owned',lambda *a,**kw:(_ for _ in ()).throw(p._DrainUnconfirmed([])))
    with pytest.raises(ValueError,match='CLEANUP_UNCONFIRMED'):p.predict_in_worker('test-only',b'wave')
    assert events==['lease-held','scratch-held']and p._QUARANTINE and not job.closed
    assert not p._WORKER_LOCK.acquire(blocking=False)
    process.broken=False;process.returncode=20
    until=time.monotonic()+2
    while p._QUARANTINE and time.monotonic()<until:time.sleep(.02)
    assert p._QUARANTINE==[]and events==['lease-held','scratch-held','scratch-removed','lease-released']
    assert job.closed and p._WORKER_LOCK.acquire(blocking=False);p._WORKER_LOCK.release()
