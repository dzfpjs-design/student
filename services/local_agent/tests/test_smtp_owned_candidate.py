"""Offline, synthetic process lifecycle checks; no SMTP, credentials or family data."""
from pathlib import Path
import ast
import subprocess
import sys
import time

import pytest

import smtp_owned_candidate as candidate
from smtp_owned_candidate import run

CHILD = Path(__file__).with_name('smtp_owned_candidate_child.py')


@pytest.fixture(autouse=True)
def confirm_owned_cleanup():
    yield
    # A deadline-expired call must return promptly, without claiming the
    # still-running native stop has completed. Reap the same original owner
    # before running the next case; timeout here is a test failure, not proof.
    until=time.monotonic()+3
    while time.monotonic()<until and not candidate.reap():
        time.sleep(.01)
    assert candidate.reap(), 'original process/job cleanup remains unconfirmed'


def test_synthetic_worker_has_no_network_or_process_imports():
    tree = ast.parse(CHILD.read_text(encoding='utf-8'))
    imports = {alias.name.split('.')[0] for node in ast.walk(tree)
               if isinstance(node, ast.Import) for alias in node.names}
    imports.update((node.module or '').split('.')[0] for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom))
    assert imports <= {'sys', 'time'}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in {'eval', 'exec', '__import__'} for node in ast.walk(tree))


@pytest.mark.parametrize('input_bytes,expected', [
    (b'', b''),
    (b'INVALID\nGO\n', b''),
    (b'SYNTHETIC_CREDENTIALS\n', b'READY\n'),
    (b'SYNTHETIC_CREDENTIALS\nNOT_GO\n', b'READY\n'),
    (b'SYNTHETIC_CREDENTIALS\nGO\n', b'READY\nACCEPTED\n'),
])
def test_fixed_worker_requires_complete_credentials_and_go(input_bytes, expected):
    result = subprocess.run([sys.executable, '-I', str(CHILD), 'success'],
                            input=input_bytes, capture_output=True, timeout=5, check=False)
    assert result.stdout == expected
    assert result.stderr == b''
    assert result.returncode == (0 if expected.endswith(b'ACCEPTED\n') else (21 if expected == b'' else 23))


def test_success_requires_confirmed_child_exit_and_reports_platform():
    started=time.monotonic()
    events=[]
    result=run('success',timeout_seconds=2,event_hook=lambda name,pid:events.append((name,pid)))
    assert result['state']=='accepted'
    assert result['go_maybe_sent'] is True
    assert result['exit_confirmed'] is True
    assert result['native_windows'] is (sys.platform=='win32')
    assert [event[0] for event in events] == ['job_assigned','go_maybe_sent','go_written']
    assert len({event[1] for event in events}) == 1
    assert time.monotonic()-started < 5


def test_assignment_failure_never_writes_payload_or_go(monkeypatch):
    writes=[]
    original_popen=candidate.subprocess.Popen
    class TracedInput:
        def __init__(self, stream): self.stream=stream
        def write(self, data): writes.append(bytes(data)); return self.stream.write(data)
        def flush(self): return self.stream.flush()
        def close(self): return self.stream.close()
        @property
        def closed(self): return self.stream.closed
    def start(*args, **kwargs):
        process=original_popen(*args,**kwargs)
        process.stdin=TracedInput(process.stdin)
        return process
    job_class=candidate._WindowsJob if sys.platform=='win32' else candidate._SyntheticPosixJob
    def fail_assignment(_self,_process): raise OSError('synthetic assignment failure')
    monkeypatch.setattr(candidate.subprocess,'Popen',start)
    monkeypatch.setattr(job_class,'assign',fail_assignment)
    events=[]
    result=run('success',timeout_seconds=2,event_hook=lambda name,pid:events.append(name))
    assert result['state'] in {'not_submitted','cleanup_unconfirmed'}
    assert result['go_maybe_sent'] is False and writes==[] and events==[]
    if result['state']=='cleanup_unconfirmed': assert result['retry_permitted'] is False


def test_assignment_stall_fails_closed_until_original_owner_reaped(monkeypatch):
    import threading
    assigned=threading.Event(); release=threading.Event()
    job_class=candidate._WindowsJob if sys.platform=='win32' else candidate._SyntheticPosixJob
    original_assign=job_class.assign
    def blocked_assign(self,process):
        assigned.set()
        release.wait(3)
        return original_assign(self,process)
    monkeypatch.setattr(job_class,'assign',blocked_assign)
    try:
        started=time.monotonic()
        result=run('success',timeout_seconds=.3)
        assert assigned.is_set() and time.monotonic()-started < 2
        assert result['state']=='cleanup_unconfirmed' and result['go_maybe_sent'] is False
        assert result['retry_permitted'] is False and result['exit_confirmed'] is False
        blocked=run('success',timeout_seconds=.3)
        assert blocked['state']=='cleanup_unconfirmed' and blocked['retry_permitted'] is False
    finally:
        release.set()


def test_timeout_after_go_intent_never_allows_a_late_go_write(monkeypatch):
    import threading
    intent=threading.Event(); release_go=threading.Event(); release_stop=threading.Event(); wrote_go=threading.Event()
    original_popen=candidate.subprocess.Popen
    original_stop=candidate._stop
    class TracedInput:
        def __init__(self,stream): self.stream=stream
        def write(self,data):
            if data == b'GO\n': wrote_go.set()
            return self.stream.write(data)
        def flush(self): return self.stream.flush()
        def close(self): return self.stream.close()
        @property
        def closed(self): return self.stream.closed
    def start(*args,**kwargs):
        process=original_popen(*args,**kwargs); process.stdin=TracedInput(process.stdin)
        return process
    def stopped_late(owner):
        release_stop.wait(3)
        return original_stop(owner)
    def hook(name,pid):
        if name=='go_maybe_sent':
            intent.set(); release_go.wait(3)
    monkeypatch.setattr(candidate.subprocess,'Popen',start)
    monkeypatch.setattr(candidate,'_stop',stopped_late)
    try:
        result=run('success',timeout_seconds=.3,event_hook=hook)
        assert intent.is_set() and result['state']=='cleanup_unconfirmed'
        assert result['retry_permitted'] is False
        release_go.set()
        assert not wrote_go.wait(.2), 'GO was written after the deadline expired'
    finally:
        release_go.set(); release_stop.set()


def test_no_ready_times_out_without_go_or_resubmission():
    started=time.monotonic()
    result=run('stall_ready',timeout_seconds=.4)
    assert result['state'] in {'not_submitted','cleanup_unconfirmed'}
    assert result['go_maybe_sent'] is False
    assert result['native_windows'] is (sys.platform=='win32')
    if result['state']=='not_submitted': assert result['exit_confirmed'] is True
    else: assert result['exit_confirmed'] is False and result['retry_permitted'] is False
    assert time.monotonic()-started < 4


def test_no_completion_after_go_is_unknown_and_never_retryable():
    started=time.monotonic()
    result=run('stall_after_go',timeout_seconds=.4)
    assert result['state'] in {'unknown','cleanup_unconfirmed'}
    assert result['go_maybe_sent'] is True
    assert result['retry_permitted'] is False
    if result['state']=='unknown': assert result['exit_confirmed'] is True
    else: assert result['exit_confirmed'] is False
    assert time.monotonic()-started < 4


def test_invalid_reply_after_go_never_means_safe_retry():
    result=run('bad_reply',timeout_seconds=2)
    assert result['state'] == 'unknown'
    assert result['go_maybe_sent'] is True
    assert result['retry_permitted'] is False
    assert result['exit_confirmed'] is True


@pytest.mark.parametrize('bad',['smtp','',None,17])
def test_only_closed_synthetic_modes_are_accepted(bad):
    with pytest.raises((TypeError,ValueError)):
        run(bad,timeout_seconds=1)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Job Object parent-death proof requires native Windows')
@pytest.mark.parametrize('stage', ['job_assigned', 'go_maybe_sent', 'go_written'])
def test_windows_parent_death_terminates_original_job_child(stage):
    """Open the exact child handle before killing its owner, then wait on it.

    The go_maybe_sent milestone precedes the pipe write; go_written only proves
    the pipe write completed. Neither proves real SMTP DATA was accepted.
    """
    import ctypes
    import queue
    import threading
    driver = r'''
import sys,time
sys.path.insert(0,sys.argv[1])
from smtp_owned_candidate import run
stage=sys.argv[2]
def report(event,pid):
    if event==stage:
        print(str(pid),flush=True)
        time.sleep(60)
run('stall_after_go',timeout_seconds=10,event_hook=report)
'''
    owner = subprocess.Popen([sys.executable,'-I','-S','-u','-c',driver,str(CHILD.parent),stage],
                             stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,
                             close_fds=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    messages=queue.Queue()
    reader=threading.Thread(target=lambda:messages.put(owner.stdout.readline()),daemon=True)
    reader.start(); child_handle=None
    try:
        line=messages.get(timeout=8)
        assert line.strip().isdigit(), 'owner never proved the Job milestone'
        pid=int(line)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        kernel.WaitForSingleObject.restype=ctypes.c_uint32
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]; kernel.CloseHandle.restype=ctypes.c_int
        child_handle=kernel.OpenProcess(0x00100000,False,pid)  # SYNCHRONIZE
        assert child_handle, 'could not retain original child identity before owner death'
        owner.kill(); owner.wait(timeout=10)
        assert kernel.WaitForSingleObject(child_handle,5000)==0, 'original child survived owner death'
    finally:
        if owner.poll() is None:
            owner.kill(); owner.wait(timeout=10)
        if child_handle: kernel.CloseHandle(child_handle)
        if owner.stdout: owner.stdout.close()
