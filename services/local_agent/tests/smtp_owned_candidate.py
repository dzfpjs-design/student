"""Synthetic-only process ownership experiment. Not an SMTP implementation.

Only the bundled fake worker and fixed public test bytes are allowed. Nothing in
this file accepts a destination, real credentials, report content or adapter.
It is intentionally outside the application package and has no runtime wiring.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

_WORKER = Path(__file__).with_name('smtp_owned_candidate_child.py').resolve()
_LOCK = threading.Lock()
_QUARANTINE = []


class _WindowsJob:
    def __init__(self):
        import ctypes as c
        self.c=c; self.k=c.WinDLL('kernel32',use_last_error=True)
        p=c.c_void_p; d=c.c_uint32; size=c.c_size_t
        class Basic(c.Structure):
            _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',d),('min_ws',size),('max_ws',size),('active',d),('affinity',size),('priority',d),('scheduling',d)]
        class Io(c.Structure):
            _fields_=[(name,c.c_uint64) for name in ('readop','writeop','otherop','readbytes','writebytes','otherbytes')]
        class Limits(c.Structure):
            _fields_=[('basic',Basic),('io',Io),('process_memory',size),('job_memory',size),('peak_process',size),('peak_job',size)]
        class Accounting(c.Structure):
            _fields_=[('total_user',c.c_int64),('total_kernel',c.c_int64),('period_user',c.c_int64),('period_kernel',c.c_int64),('faults',d),('total',d),('active',d),('terminated',d)]
        self.Accounting=Accounting
        signatures={'CreateJobObjectW':([p,c.c_wchar_p],p),'SetInformationJobObject':([p,c.c_int,p,d],c.c_int),
            'AssignProcessToJobObject':([p,p],c.c_int),'IsProcessInJob':([p,p,c.POINTER(c.c_int)],c.c_int),
            'QueryInformationJobObject':([p,c.c_int,p,d,c.POINTER(d)],c.c_int),'TerminateJobObject':([p,d],c.c_int),
            'GetHandleInformation':([p,c.POINTER(d)],c.c_int),'CloseHandle':([p],c.c_int)}
        for name,(args,ret) in signatures.items():
            f=getattr(self.k,name);f.argtypes=args;f.restype=ret
        self.handle=self.k.CreateJobObjectW(None,None)
        if not self.handle: raise RuntimeError('JOB_CREATE_FAILED')
        info=Limits(); info.basic.flags=0x2000|0x8; info.basic.active=1
        flags=d()
        if (not self.k.SetInformationJobObject(self.handle,9,c.byref(info),c.sizeof(info))
                or not self.k.GetHandleInformation(self.handle,c.byref(flags)) or flags.value & 1):
            self.close(); raise RuntimeError('JOB_CONFIGURATION_FAILED')

    def assign(self, process):
        if not self.k.AssignProcessToJobObject(self.handle,int(process._handle)): raise RuntimeError('JOB_ASSIGN_FAILED')
        member=self.c.c_int()
        if not self.k.IsProcessInJob(int(process._handle),self.handle,self.c.byref(member)) or not member.value:
            raise RuntimeError('JOB_MEMBERSHIP_UNCONFIRMED')

    def active(self):
        info=self.Accounting()
        if not self.k.QueryInformationJobObject(self.handle,1,self.c.byref(info),self.c.sizeof(info),None): raise RuntimeError('JOB_QUERY_FAILED')
        return info.active

    def terminate(self):
        if not self.k.TerminateJobObject(self.handle,25): raise RuntimeError('JOB_TERMINATE_FAILED')

    def close(self):
        if self.handle:
            if not self.k.CloseHandle(self.handle): raise RuntimeError('JOB_CLOSE_FAILED')
            self.handle=None


class _SyntheticPosixJob:
    """Mac-only fake ownership; it is not evidence of parent-death containment."""
    def __init__(self): self.process=None
    def assign(self,process): self.process=process
    def active(self): return int(self.process is not None and self.process.poll() is None)
    def terminate(self):
        if self.process and self.process.poll() is None: self.process.kill()
    def close(self): pass


@dataclass
class _Owner:
    deadline: float
    go_maybe_sent: bool = False
    abort: bool = False
    process: object = None
    job: object = None
    thread: object = None
    outcome: str = 'not_submitted'
    confirmed: bool = False
    cleaned: bool = False
    stage: str = 'start'
    error_type: str = ''
    reply_class: str = ''
    exit_code: object = None
    active_count: object = None
    lock: object = None


def _stop(owner):
    try:
        if owner.job is not None: owner.job.terminate()
    except Exception: pass
    try:
        if owner.process is not None and owner.process.poll() is None: owner.process.kill()
    except Exception: pass


def _perform(owner,mode,event_hook):
    try:
        owner.stage='job'
        owner.job=_WindowsJob() if os.name=='nt' else _SyntheticPosixJob()
        owner.stage='spawn'
        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR'}}
        owner.process=subprocess.Popen([sys.executable,'-I','-S','-B',str(_WORKER),mode],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,env=env,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)
        owner.stage='assign'; owner.job.assign(owner.process)
        if event_hook: event_hook('job_assigned',owner.process.pid)
        with owner.lock:
            if owner.abort or time.monotonic() >= owner.deadline: return
        owner.stage='synthetic_input'; owner.process.stdin.write(b'SYNTHETIC_CREDENTIALS\n');owner.process.stdin.flush()
        owner.stage='ready'
        line=owner.process.stdout.readline(65)
        if line != b'READY\n': return
        with owner.lock:
            if owner.abort or time.monotonic() >= owner.deadline: return
            owner.go_maybe_sent=True
        if event_hook: event_hook('go_maybe_sent',owner.process.pid)
        with owner.lock:
            if owner.abort or time.monotonic() >= owner.deadline: return
        owner.stage='go'; owner.process.stdin.write(b'GO\n');owner.process.stdin.flush();owner.process.stdin.close()
        if event_hook: event_hook('go_written',owner.process.pid)
        owner.stage='result'
        reply=owner.process.stdout.readline(65)
        remaining=owner.deadline-time.monotonic()
        if remaining <= 0: return
        owner.process.wait(timeout=remaining)
        owner.reply_class='accepted' if reply==b'ACCEPTED\n' else 'other'
        owner.exit_code=owner.process.returncode
        owner.active_count=owner.job.active()
        # Windows may signal the process handle before Job accounting reaches
        # zero. Recheck only inside the original deadline, never assume exit.
        while owner.active_count and time.monotonic() < owner.deadline and not owner.abort:
            time.sleep(min(.005,max(0,owner.deadline-time.monotonic())))
            owner.active_count=owner.job.active()
        if owner.process.returncode==0 and reply==b'ACCEPTED\n' and owner.active_count==0:
            owner.outcome='accepted'
    except BaseException as error:
        owner.error_type=type(error).__name__
    finally:
        _stop(owner)
        try:
            left=owner.deadline-time.monotonic()
            if owner.process is not None and left>0: owner.process.wait(timeout=left)
            owner.confirmed = bool(owner.process is not None and owner.process.poll() is not None and owner.job is not None and owner.job.active()==0)
            if owner.confirmed:
                if owner.process.stdout: owner.process.stdout.close()
                if owner.process.stdin and not owner.process.stdin.closed: owner.process.stdin.close()
                owner.job.close()
                owner.cleaned=True
        except Exception:
            owner.confirmed=False
        if owner.outcome != 'accepted': owner.outcome='unknown' if owner.go_maybe_sent else 'not_submitted'


def run(mode, timeout_seconds=2, event_hook=None):
    if mode not in {'success','stall_ready','stall_after_go','bad_reply'} or not isinstance(timeout_seconds,(int,float)) or isinstance(timeout_seconds,bool) or not 0.05 <= timeout_seconds <= 10:
        raise ValueError('SYNTHETIC_INPUT_INVALID')
    if not _LOCK.acquire(blocking=False): return {'state':'cleanup_unconfirmed','go_maybe_sent':False,'exit_confirmed':False,'native_windows':os.name=='nt','retry_permitted':False}
    owner=_Owner(time.monotonic()+timeout_seconds,lock=threading.Lock())
    owner.thread=threading.Thread(target=_perform,args=(owner,mode,event_hook),daemon=True);owner.thread.start()
    owner.thread.join(max(0,owner.deadline-time.monotonic()))
    if owner.thread.is_alive():
        with owner.lock: owner.abort=True
        # A native terminate call itself is not assumed bounded. Retain the
        # original handles and admission lock until both termination and exit
        # have been positively checked by reap().
        stopper=threading.Thread(target=_stop,args=(owner,),daemon=True);stopper.start()
        _QUARANTINE.append((owner,stopper))
        return {'state':'cleanup_unconfirmed','go_maybe_sent':owner.go_maybe_sent,'exit_confirmed':False,
                'native_windows':os.name=='nt','retry_permitted':False,'stage':owner.stage,'error_type':owner.error_type}
    if not owner.confirmed:
        _QUARANTINE.append((owner,None))
        return {'state':'cleanup_unconfirmed','go_maybe_sent':owner.go_maybe_sent,'exit_confirmed':False,
                'native_windows':os.name=='nt','retry_permitted':False,'stage':owner.stage,'error_type':owner.error_type}
    _LOCK.release()
    return {'state':owner.outcome,'go_maybe_sent':owner.go_maybe_sent,'exit_confirmed':True,
            'native_windows':os.name=='nt','retry_permitted':owner.outcome=='not_submitted',
            'stage':owner.stage,'error_type':owner.error_type,'reply_class':owner.reply_class,
            'exit_code':owner.exit_code,'active_count':owner.active_count}


def reap():
    """Synthetic test cleanup only; never assumes process termination."""
    if not _QUARANTINE:return True
    owner,stopper=_QUARANTINE[0]
    if owner.thread.is_alive() or (stopper and stopper.is_alive()):return False
    if owner.confirmed and owner.cleaned:
        _QUARANTINE.pop(0);_LOCK.release();return True
    try:
        if owner.process is None or owner.process.poll() is None or owner.job is None or owner.job.active()!=0:return False
        if owner.process.stdout and not owner.process.stdout.closed:owner.process.stdout.close()
        if owner.process.stdin and not owner.process.stdin.closed:owner.process.stdin.close()
        owner.job.close()
    except Exception:return False
    _QUARANTINE.pop(0);_LOCK.release();return True
