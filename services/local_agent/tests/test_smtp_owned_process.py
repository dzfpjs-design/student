"""Synthetic loopback SMTP exercises owned worker, never a real mailbox."""
from contextlib import contextmanager
import threading
import time
import subprocess
import sys
from pathlib import Path

import pytest

from home_study_agent.smtp_owned_process import OwnedSmtpProcess
from test_combined_report_loopback_candidate import sink


MIME = b'From: sender@example.test\r\nTo: parent@example.test\r\nSubject: synthetic\r\n\r\nSynthetic message.\r\n'


@pytest.fixture(autouse=True)
def synthetic_posix_members(monkeypatch):
    if sys.platform!='win32':
        from home_study_agent import smtp_owned_process as module
        monkeypatch.setattr(module,'_job_members',lambda job,process:[{'pid':process.pid,'windows_creation_time_100ns':1,'role':'worker','image_path':'synthetic'}])


def owner(guard, *, preflight=lambda:True, persist=lambda *args:None):
    @contextmanager
    def hold():
        with guard: yield
    return OwnedSmtpProcess(hold,preflight,persist)


def send(process, port, timeout=4):
    return process.submit(host='127.0.0.1',port=port,username='',password='',sender='sender@example.test',
        recipient='parent@example.test',mime_bytes=MIME,timeout_seconds=timeout,use_ssl=False,starttls=False)


def test_loopback_acceptance_follows_ready_guard_and_persist():
    guard=threading.Lock(); intents=[]
    with sink() as smtp:
        process=owner(guard,preflight=lambda:guard.locked(),persist=lambda identity,*args:intents.append(identity))
        result=send(process,smtp.server_address[1])
        assert result == {'status':'accepted','possible_submission':True,'exit_confirmed':True}
        assert len(intents)==1 and len(smtp.messages)==1 and b'Synthetic message.' in smtp.messages[0]
        assert intents[0]['pid'] > 0
        assert (intents[0]['windows_creation_time_100ns'] > 0) if sys.platform == 'win32' else (intents[0]['windows_creation_time_100ns'] is None)
        assert not guard.locked()


def test_missing_250_is_unknown_and_not_repeated():
    with sink(lose_reply=True) as smtp:
        process=owner(threading.Lock())
        result=send(process,smtp.server_address[1])
        assert result == {'status':'unknown','possible_submission':True,'exit_confirmed':True}
        assert len(smtp.messages)==1
        with pytest.raises(ValueError,match='OWNER_ALREADY_USED'): send(process,smtp.server_address[1])


def test_preflight_rejection_never_sends_data():
    with sink() as smtp:
        process=owner(threading.Lock(),preflight=lambda:False)
        result=send(process,smtp.server_address[1])
        assert result['status']=='not_submitted' and result['exit_confirmed']
        assert smtp.data_commands==0 and not smtp.messages


def test_ambiguous_intent_persistence_never_sends_data():
    def committed_then_lost(*_args): raise RuntimeError('lost acknowledgement')
    with sink() as smtp:
        process=owner(threading.Lock(),persist=committed_then_lost)
        result=send(process,smtp.server_address[1])
        assert result['status']=='unknown' and result['exit_confirmed']
        assert smtp.data_commands==0 and not smtp.messages


def test_timeout_releases_guard_only_after_original_worker_exits():
    entered,release=threading.Event(),threading.Event(); guard=threading.Lock()
    def stall_data(): entered.set(); release.wait(5)
    try:
        with sink(on_data=stall_data) as smtp:
            process=owner(guard)
            result=send(process,smtp.server_address[1],timeout=.7)
            assert entered.is_set() and result['status']=='cleanup_pending' and result['possible_submission']
            process.thread.join(3)
            assert not process.thread.is_alive() and process.confirmed and process.cleaned
            assert not guard.locked() and process.outcome=='unknown'
    finally:
        release.set()


def test_non_loopback_hard_block():
    with pytest.raises(ValueError,match='LOOPBACK_ONLY'):
        owner(threading.Lock()).submit(host='192.0.2.1',port=25,username='',password='',sender='a@example.test',
            recipient='b@example.test',mime_bytes=MIME,timeout_seconds=1,use_ssl=False,starttls=False)


def test_timeout_between_last_check_and_go_holds_guard_until_owner_cleans(monkeypatch):
    from home_study_agent import smtp_owned_process as module
    original=subprocess.Popen; entered=threading.Event(); release=threading.Event(); guard=threading.Lock()
    class BlockWrite:
        def __init__(self, stream): self.stream=stream
        def write(self, data):
            if data == b'GO\n': entered.set(); release.wait(5)
            return self.stream.write(data)
        def flush(self): return self.stream.flush()
        def close(self): return self.stream.close()
        @property
        def closed(self): return self.stream.closed
    def start(*args,**kwargs):
        process=original(*args,**kwargs); process.stdin=BlockWrite(process.stdin); return process
    monkeypatch.setattr(module.subprocess,'Popen',start)
    try:
        with sink() as smtp:
            process=owner(guard)
            result=send(process,smtp.server_address[1],timeout=.7)
            assert entered.is_set() and result['status']=='cleanup_pending' and result['possible_submission']
            assert guard.locked(), 'the same effect guard must remain owned until cleanup'
            release.set(); process.thread.join(4)
            assert not process.thread.is_alive() and process.confirmed and not guard.locked()
            assert process.outcome=='unknown' and smtp.data_commands==0
    finally:
        release.set()


@pytest.mark.skipif(sys.platform!='win32',reason='Job Object parent death requires native Windows')
@pytest.mark.parametrize('after_go',[False,True])
def test_windows_owner_death_kills_original_smtp_worker(after_go):
    import ctypes
    import queue
    child_ready=threading.Event(); release=threading.Event()
    def data_wait(): child_ready.set(); release.wait(10)
    driver = '''
import sys,time,threading
from contextlib import nullcontext
sys.path.insert(0,sys.argv[1])
from home_study_agent.smtp_owned_process import OwnedSmtpProcess
def persisted(identity,members,job,process):
    print(identity['pid'],flush=True)
    if sys.argv[3]=='before': time.sleep(60)
p=OwnedSmtpProcess(nullcontext,lambda:True,persisted)
p.submit(host='127.0.0.1',port=int(sys.argv[2]),username='',password='',sender='a@example.test',recipient='b@example.test',mime_bytes=b'Subject: synthetic\\r\\n\\r\\nhello\\r\\n',timeout_seconds=30,use_ssl=False,starttls=False)
'''
    root=str(Path(__file__).resolve().parents[1]/'src')
    with sink(on_data=data_wait) as smtp:
        process=subprocess.Popen([sys.executable,'-I','-S','-u','-c',driver,root,str(smtp.server_address[1]),'after' if after_go else 'before'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,close_fds=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        lines=queue.Queue(); threading.Thread(target=lambda:lines.put(process.stdout.readline()),daemon=True).start()
        handle=None
        try:
            line=lines.get(timeout=10); assert line.strip().isdigit()
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]; kernel.OpenProcess.restype=ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]; kernel.WaitForSingleObject.restype=ctypes.c_uint32
            kernel.CloseHandle.argtypes=[ctypes.c_void_p]; kernel.CloseHandle.restype=ctypes.c_int
            handle=kernel.OpenProcess(0x00100000,False,int(line)); assert handle
            if after_go: assert child_ready.wait(10)
            process.kill(); process.wait(timeout=10)
            assert kernel.WaitForSingleObject(handle,5000)==0
        finally:
            release.set()
            if process.poll() is None: process.kill(); process.wait(timeout=10)
            if handle: kernel.CloseHandle(handle)
            process.stdout.close()
