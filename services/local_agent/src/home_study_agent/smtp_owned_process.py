"""Bounded SMTP process owner, isolated from production delivery for now.

The worker is hard-limited to loopback.  The owner retains the supplied effect
guard through confirmed process cleanup.  Cross-restart process reconciliation
is not implemented here: do not wire this module to production mail.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .smtp_owned_worker import MAX_FRAME


def _packaged_worker_command():
    """Resolve only the installed sibling onedir and verify its complete tree."""
    service=Path(sys.executable)
    if not service.is_absolute():raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
    root=service.parent/'smtp-worker'
    manifest_path=root/'worker-manifest.json'
    executable=root/'hsc-smtp-worker.exe'
    def regular(path,*,directory=False):
        info=path.lstat()
        if path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
        if (directory and not path.is_dir()) or (not directory and not path.is_file()):
            raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
    try:
        regular(service)
        for parent in service.parents:regular(parent,directory=True)
        regular(root,directory=True);regular(root/'_internal',directory=True)
        regular(manifest_path);regular(executable)
        if manifest_path.stat().st_size>1024*1024:raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
        payload=json.loads(manifest_path.read_text(encoding='utf-8'))
        if type(payload) is not dict or set(payload)!={'version','files'} or payload['version']!=1 or type(payload['files']) is not dict:
            raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
        files=payload['files']
        if not files or len(files)>4096 or 'hsc-smtp-worker.exe' not in files or not any(n.startswith('_internal/') for n in files):
            raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
        observed=set(); observed_dirs=set()
        def walk_error(error): raise error
        for parent,dirs,names in os.walk(root,followlinks=False,onerror=walk_error):
            for name in dirs:
                path=Path(parent)/name; regular(path,directory=True)
                observed_dirs.add(path.relative_to(root).as_posix())
            for name in names:
                path=Path(parent)/name;regular(path)
                relative=path.relative_to(root).as_posix()
                if relative!='worker-manifest.json':observed.add(relative)
        expected_dirs=set()
        for name in files:
            parts=name.split('/')
            expected_dirs.update('/'.join(parts[:i]) for i in range(1,len(parts)))
        if observed!=set(files) or observed_dirs!=expected_dirs:raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
        for name,entry in files.items():
            if (not isinstance(name,str) or not name or '\\' in name or ':' in name or name.startswith('/')
                    or any(ord(ch)<32 for ch in name)
                    or any(part in ('','.','..') for part in name.split('/'))
                    or type(entry) is not dict or set(entry)!={'size','sha256'}
                    or type(entry['size']) is not int or not 0<entry['size']<=1024*1024*1024
                    or not isinstance(entry['sha256'],str) or len(entry['sha256'])!=64
                    or any(ch not in '0123456789abcdef' for ch in entry['sha256'])):
                raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
            path=root.joinpath(*name.split('/'))
            if path.stat().st_size!=entry['size']:raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
            if digest.hexdigest()!=entry['sha256']:raise ValueError('SMTP_WORKER_PACKAGE_INVALID')
        return [str(executable)]
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError):
        raise ValueError('SMTP_WORKER_PACKAGE_INVALID') from None


class _WindowsJob:
    def __init__(self):
        import ctypes as c
        self.c = c
        self.k = c.WinDLL('kernel32', use_last_error=True)
        p, d, size = c.c_void_p, c.c_uint32, c.c_size_t
        class Basic(c.Structure):
            _fields_ = [('process_time',c.c_int64),('job_time',c.c_int64),('flags',d),('min_ws',size),('max_ws',size),('active',d),('affinity',size),('priority',d),('scheduling',d)]
        class Io(c.Structure):
            _fields_ = [(n,c.c_uint64) for n in ('readop','writeop','otherop','readbytes','writebytes','otherbytes')]
        class Limits(c.Structure):
            _fields_ = [('basic',Basic),('io',Io),('process_memory',size),('job_memory',size),('peak_process',size),('peak_job',size)]
        class Accounting(c.Structure):
            _fields_ = [('total_user',c.c_int64),('total_kernel',c.c_int64),('period_user',c.c_int64),('period_kernel',c.c_int64),('faults',d),('total',d),('active',d),('terminated',d)]
        self.Accounting = Accounting
        signatures = {'CreateJobObjectW':([p,c.c_wchar_p],p),'SetInformationJobObject':([p,c.c_int,p,d],c.c_int),
            'AssignProcessToJobObject':([p,p],c.c_int),'IsProcessInJob':([p,p,c.POINTER(c.c_int)],c.c_int),
            'QueryInformationJobObject':([p,c.c_int,p,d,c.POINTER(d)],c.c_int),'TerminateJobObject':([p,d],c.c_int),
            'GetHandleInformation':([p,c.POINTER(d)],c.c_int),'CloseHandle':([p],c.c_int)}
        for name, (args, restype) in signatures.items():
            function = getattr(self.k,name); function.argtypes = args; function.restype = restype
        class SecurityAttributes(c.Structure):
            _fields_=[('length',d),('security_descriptor',p),('inherit',c.c_int)]
        advapi=c.WinDLL('advapi32',use_last_error=True)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[c.c_wchar_p,d,c.POINTER(p),c.POINTER(d)]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=c.c_int
        self.k.LocalFree.argtypes=[p]; self.k.LocalFree.restype=p
        descriptor=p()
        # Explicit protected DACL: object owner and LocalSystem only.
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;;GA;;;OW)(A;;GA;;;SY)',1,c.byref(descriptor),None):
            raise RuntimeError('JOB_SECURITY_CONFIGURATION_FAILED')
        attributes=SecurityAttributes(c.sizeof(SecurityAttributes),descriptor,0)
        # Global scope avoids a false not-found after a Windows session change.
        # No existing same-named kernel object may ever be reused.
        self.name = 'Global\\HSC_SMTP_' + uuid4().hex
        c.set_last_error(0)
        try:
            self.handle = self.k.CreateJobObjectW(c.byref(attributes),self.name)
            creation_error=c.get_last_error()
        finally:
            self.k.LocalFree(descriptor)
        if not self.handle: raise RuntimeError('JOB_CREATE_FAILED')
        if creation_error==183:
            self.close(); raise RuntimeError('JOB_NAME_COLLISION')
        limits = Limits(); limits.basic.flags = 0x2000 | 0x8; limits.basic.active = 1
        flags = d()
        if (not self.k.SetInformationJobObject(self.handle,9,c.byref(limits),c.sizeof(limits))
                or not self.k.GetHandleInformation(self.handle,c.byref(flags)) or flags.value & 1):
            self.close(); raise RuntimeError('JOB_CONFIGURATION_FAILED')

    def assign(self, process):
        if not self.k.AssignProcessToJobObject(self.handle,int(process._handle)):
            raise RuntimeError('JOB_ASSIGN_FAILED')
        member = self.c.c_int()
        if not self.k.IsProcessInJob(int(process._handle),self.handle,self.c.byref(member)) or not member.value:
            raise RuntimeError('JOB_MEMBERSHIP_UNCONFIRMED')

    def active(self):
        info = self.Accounting()
        if not self.k.QueryInformationJobObject(self.handle,1,self.c.byref(info),self.c.sizeof(info),None):
            raise RuntimeError('JOB_QUERY_FAILED')
        return info.active

    def terminate(self):
        if not self.k.TerminateJobObject(self.handle,25): raise RuntimeError('JOB_TERMINATE_FAILED')

    def close(self):
        if self.handle:
            if not self.k.CloseHandle(self.handle): raise RuntimeError('JOB_CLOSE_FAILED')
            self.handle = None


class _LoopbackPosixJob:
    # Test isolation only: unlike Windows this does not provide parent-death containment.
    def __init__(self):
        self.process = None
        self.name = 'Global\\HSC_SMTP_' + uuid4().hex
        self.handle = True
    def assign(self, process): self.process = process
    def active(self): return int(self.process is not None and self.process.poll() is None)
    def terminate(self):
        if self.process is not None and self.process.poll() is None: self.process.kill()
    def close(self): self.handle = None


def _process_identity(process):
    """Use the original Windows handle, never a fresh PID lookup."""
    if os.name != 'nt':
        return {'pid':process.pid, 'windows_creation_time_100ns':None}
    import ctypes as c
    class FileTime(c.Structure):
        _fields_ = [('low',c.c_uint32),('high',c.c_uint32)]
    kernel = c.WinDLL('kernel32',use_last_error=True)
    kernel.GetProcessTimes.argtypes = [c.c_void_p,c.POINTER(FileTime),c.POINTER(FileTime),c.POINTER(FileTime),c.POINTER(FileTime)]
    kernel.GetProcessTimes.restype = c.c_int
    created,exited,kernel_time,user_time = FileTime(),FileTime(),FileTime(),FileTime()
    if not kernel.GetProcessTimes(int(process._handle),c.byref(created),c.byref(exited),c.byref(kernel_time),c.byref(user_time)):
        raise RuntimeError('PROCESS_IDENTITY_UNCONFIRMED')
    return {'pid':process.pid,'windows_creation_time_100ns':(created.high<<32)|created.low}


def _job_exited(job):
    """Proof is the still-open original Job handle, never a PID count guess."""
    if os.name!='nt' or type(job) is not _WindowsJob or not job.handle:
        return False
    try: return job.active()==0
    except Exception: return False


def _job_members(job,process):
    """Snapshot and verify every member of the original Windows Job."""
    if os.name!='nt' or type(job) is not _WindowsJob or not job.handle:
        raise ValueError('WINDOWS_JOB_MEMBERS_UNAVAILABLE')
    import ctypes as c
    import struct
    k=job.k
    k.OpenProcess.argtypes=[c.c_uint32,c.c_int,c.c_uint32]; k.OpenProcess.restype=c.c_void_p
    k.GetProcessTimes.argtypes=[c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p]; k.GetProcessTimes.restype=c.c_int
    k.QueryFullProcessImageNameW.argtypes=[c.c_void_p,c.c_uint32,c.c_wchar_p,c.POINTER(c.c_uint32)]; k.QueryFullProcessImageNameW.restype=c.c_int
    k.WaitForSingleObject.argtypes=[c.c_void_p,c.c_uint32]; k.WaitForSingleObject.restype=c.c_uint32
    class FileTime(c.Structure): _fields_=[('low',c.c_uint32),('high',c.c_uint32)]
    def pids():
        raw=c.create_string_buffer(1024); out=c.c_uint32()
        if not k.QueryInformationJobObject(job.handle,3,raw,len(raw),c.byref(out)): raise ValueError('SMTP_JOB_MEMBERS_UNCONFIRMED')
        assigned,count=struct.unpack_from('<II',raw.raw)
        if not 1<=count<=8 or assigned!=count: raise ValueError('SMTP_JOB_MEMBERS_UNCONFIRMED')
        size=c.sizeof(c.c_void_p); fmt='<Q' if size==8 else '<I'
        values=[struct.unpack_from(fmt,raw.raw,8+i*size)[0] for i in range(count)]
        if len(set(values))!=count: raise ValueError('SMTP_JOB_MEMBERS_UNCONFIRMED')
        return sorted(values)
    before=pids(); result=[]
    expected=os.path.normcase(os.path.realpath(str(process.args[0])))
    console=os.path.normcase(os.path.realpath(os.path.join(os.environ.get('SYSTEMROOT',''), 'System32','conhost.exe')))
    if not os.environ.get('SYSTEMROOT'): raise ValueError('SMTP_JOB_MEMBERS_UNCONFIRMED')
    for pid in before:
        handle=k.OpenProcess(0x1000|0x00100000,False,pid)
        if not handle: raise ValueError('SMTP_JOB_MEMBERS_UNCONFIRMED')
        try:
            member=c.c_int(); created,exited,kernel,user=FileTime(),FileTime(),FileTime(),FileTime()
            size=c.c_uint32(32768); image=c.create_unicode_buffer(size.value)
            if (not k.IsProcessInJob(handle,job.handle,c.byref(member)) or not member.value
                    or not k.GetProcessTimes(handle,c.byref(created),c.byref(exited),c.byref(kernel),c.byref(user))
                    or not k.QueryFullProcessImageNameW(handle,0,image,c.byref(size))
                    or k.WaitForSingleObject(handle,0)!=0x102):
                raise ValueError('SMTP_JOB_MEMBERS_UNCONFIRMED')
            path=os.path.normcase(os.path.realpath(image.value))
            role='worker' if pid==process.pid and path==expected else 'console' if path==console else None
            if role is None: raise ValueError('SMTP_JOB_UNKNOWN_MEMBER')
            result.append({'pid':pid,'windows_creation_time_100ns':(created.high<<32)|created.low,'role':role,'image_path':path})
        finally: k.CloseHandle(handle)
    if pids()!=before or sum(x['role']=='worker' for x in result)!=1:
        raise ValueError('SMTP_JOB_MEMBERS_UNCONFIRMED')
    return result


_COMBINED_PUBLIC_TRANSPORT_AUTHORITY = object()


class OwnedSmtpProcess:
    """One non-reusable owned request; callers supply guards and durable intent.

    This API is not a production release gate. A returned cleanup_pending keeps
    the guard in the original background owner until process death is confirmed.
    """
    def __init__(self, guard_factory, preflight, persist_possible_submission, finish_possible_submission=None):
        if not all(callable(x) for x in (guard_factory, preflight, persist_possible_submission)):
            raise ValueError('OWNER_CALLBACKS_REQUIRED')
        self.guard_factory, self.preflight, self.persist = guard_factory, preflight, persist_possible_submission
        if finish_possible_submission is not None and not callable(finish_possible_submission):
            raise ValueError('OWNER_CALLBACKS_REQUIRED')
        self.finish = finish_possible_submission
        self.lock = threading.Lock(); self.process = self.job = self.thread = None
        self.abort = False; self.intent = False; self.confirmed = False; self.cleaned = False
        self.outcome = 'not_submitted'; self.stage = 'new'; self.started = False

    def _stop(self):
        try:
            if self.job is not None: self.job.terminate()
        except Exception: pass
        try:
            if self.process is not None and self.process.poll() is None: self.process.kill()
        except Exception: pass

    def _worker_command(self):
        if getattr(sys, 'frozen', False):
            return _packaged_worker_command()
        # Windows venv python.exe is a launcher that spawns the base Python.
        # Starting it in our one-process Job would block the actual worker.
        executable = getattr(sys, '_base_executable', None) if os.name == 'nt' else sys.executable
        if not executable or not os.path.isabs(executable):
            raise ValueError('SMTP_WORKER_PYTHON_UNAVAILABLE')
        return [executable, '-I', '-S', '-B', str(Path(__file__).with_name('smtp_owned_worker.py').resolve())]

    def _run(self, frame, deadline):
        try:
            with self.guard_factory():
                try:
                    self.stage = 'job'; self.job = _WindowsJob() if os.name == 'nt' else _LoopbackPosixJob()
                    self.stage = 'spawn'
                    env = {k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR'}}
                    self.process = subprocess.Popen(self._worker_command(), stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,close_fds=True,env=env,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name == 'nt' else 0)
                    self.stage = 'assign'; self.job.assign(self.process)
                    with self.lock:
                        if self.abort or time.monotonic() >= deadline: return
                    self.stage = 'input'; self.process.stdin.write(frame); self.process.stdin.flush()
                    self.stage = 'ready'
                    if self.process.stdout.readline(65) != b'READY\n': return
                    with self.lock:
                        if self.abort or time.monotonic() >= deadline: return
                    if self.preflight() is not True: return
                    # Conservatively set before invoking persistence: the callback
                    # may commit and then raise or lose its acknowledgement.
                    identity = _process_identity(self.process)
                    members = _job_members(self.job,self.process)
                    self.intent = True
                    self.persist(identity,members,self.job,self.process)
                    with self.lock:
                        if self.abort or time.monotonic() >= deadline: return
                    self.stage = 'go'; self.process.stdin.write(b'GO\n'); self.process.stdin.flush()
                    reply = self.process.stdout.readline(65)
                    if reply == b'ACCEPTED\n': self.outcome = 'accepted'
                finally:
                    self._stop()
                    # No deadlines here: the caller may return cleanup_pending,
                    # but the original thread keeps its external guard until the
                    # same original process and Job have been observed dead.
                    while self.process is not None and self.process.poll() is None:
                        self._stop(); time.sleep(.05)
                    while self.job is not None:
                        try:
                            if self.job.active() == 0: break
                        except Exception:
                            # An unqueryable Job is not proof of exit.
                            pass
                        self._stop(); time.sleep(.05)
                    self.confirmed = True
                    if self.process is not None:
                        for pipe in (self.process.stdin,self.process.stdout):
                            if pipe is not None and not pipe.closed: pipe.close()
                    if self.job is not None: self.job.close()
                    if self.intent and self.finish is not None:
                        try:
                            self.finish('accepted' if self.outcome == 'accepted' else 'unknown',self.job)
                        except Exception:
                            # A missing durable result cannot be reported as sent.
                            self.outcome = 'unknown'
                    self.cleaned = True
        except BaseException:
            pass
        finally:
            if self.outcome != 'accepted': self.outcome = 'unknown' if self.intent else 'not_submitted'

    def submit(self, *, host, port, username, password, sender, recipient, mime_bytes, timeout_seconds, use_ssl, starttls,
               _public_authority=None):
        import ipaddress
        from .smtp_owned_worker import _valid_host, _public_domain
        if self.started: raise ValueError('OWNER_ALREADY_USED')
        if not _valid_host(host): raise ValueError('SMTP_HOST_INVALID')
        if _public_authority is not None and _public_authority is not _COMBINED_PUBLIC_TRANSPORT_AUTHORITY:
            raise ValueError('SMTP_PUBLIC_AUTHORITY_INVALID')
        public = _public_authority is _COMBINED_PUBLIC_TRANSPORT_AUTHORITY
        if public:
            from .daily_report_combined_delivery import _source_withdrawal_guard_proven
            if not _source_withdrawal_guard_proven(): raise ValueError('COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE')
            if not _public_domain(host): raise ValueError('SMTP_PUBLIC_HOST_INVALID')
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None  # Resolved and restricted within the owned worker.
        if not public and literal is not None and not literal.is_loopback: raise ValueError('LOOPBACK_ONLY')
        if type(use_ssl) is not bool or type(starttls) is not bool or (use_ssl and starttls):
            raise ValueError('SMTP_TLS_MODE_INVALID')
        if public and not (use_ssl or starttls): raise ValueError('SMTP_PUBLIC_TLS_REQUIRED')
        if type(username) is not str or type(password) is not str or bool(username.strip())!=bool(password):
            raise ValueError('SMTP_AUTH_CONFIGURATION_INCOMPLETE')
        if username.strip() and not (use_ssl or starttls):
            raise ValueError('SMTP_PLAINTEXT_AUTH_FORBIDDEN')
        username=username.strip()
        if type(port) is not int or not 1<=port<=65535: raise ValueError('SMTP_PORT_INVALID')
        if type(mime_bytes) is not bytes or not mime_bytes or len(mime_bytes) > 14*1024*1024:
            raise ValueError('MIME_INVALID')
        if type(timeout_seconds) not in (int,float) or not .05 <= timeout_seconds <= 120:
            raise ValueError('TIMEOUT_INVALID')
        # A missing or damaged installed onedir is reported before spawn; do
        # not fall back to the onefile service executable.
        self._worker_command()
        deadline = time.monotonic() + timeout_seconds
        request = dict(host=host,port=port,username=username,password=password,sender=sender,recipient=recipient,
            mime=base64.b64encode(mime_bytes).decode('ascii'),deadline=deadline,ssl=use_ssl,starttls=starttls,public=public)
        payload = json.dumps(request,separators=(',',':')).encode()
        if len(payload) > MAX_FRAME: raise ValueError('FRAME_TOO_LARGE')
        frame = struct.pack('!I',len(payload)) + payload
        self.started = True
        self.thread = threading.Thread(target=self._run,args=(frame,deadline),daemon=True); self.thread.start()
        self.thread.join(max(0,deadline-time.monotonic()))
        if self.thread.is_alive():
            with self.lock: self.abort = True
            threading.Thread(target=self._stop,daemon=True).start()
            return {'status':'cleanup_pending','possible_submission':self.intent,'exit_confirmed':False}
        return {'status':self.outcome if self.confirmed and self.cleaned else 'cleanup_pending',
            'possible_submission':self.intent,'exit_confirmed':self.confirmed and self.cleaned}
