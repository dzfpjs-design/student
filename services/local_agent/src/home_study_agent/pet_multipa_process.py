"""One fixed CPU worker, owned pipes, mandatory Windows Job and finite output."""
from contextlib import contextmanager,ExitStack
import ctypes,hashlib,json,os,shutil,struct,subprocess,threading,time
from pathlib import Path
from uuid import uuid4
from .pet_multipa_epoch import WindowsEpochLease,configured,need
from .pet_multipa_protocol import MAX_RESULT,decode
from .pet_multipa_epoch import _AttestedWindowsEpochLease
from . import pet_multipa_completion as _completion

def _completion_directory(cls,path,owners):
    if owners is None:return cls(path) # Original raw constructor/default route.
    owner=cls.__new__(cls);owners.append(owner)
    cls.__init__(owner,path) # Retain even if original constructor close fails.
    return owner


@contextmanager
def private_scope(root,actor_sid,*,_completion_owners=None):
    """Create only a fresh protected scratch child, never repair an old ACL."""
    from .money_r4.money_files import OwnerDirectory
    need(os.name=='nt','WINDOWS_EPOCH_REQUIRED')
    with _completion_directory(OwnerDirectory,Path(root),_completion_owners):
        c=ctypes;k=c.WinDLL('kernel32',use_last_error=True);a=c.WinDLL('advapi32',use_last_error=True);P=c.c_void_p
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[c.c_wchar_p,c.c_uint32,c.POINTER(P),c.POINTER(c.c_uint32)];a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=c.c_int
        k.LocalFree.argtypes=[P];k.LocalFree.restype=P
        class SA(c.Structure):_fields_=[('length',c.c_uint32),('descriptor',P),('inherit',c.c_int)]
        k.CreateDirectoryW.argtypes=[c.c_wchar_p,c.POINTER(SA)];k.CreateDirectoryW.restype=c.c_int
        sd=P();path=Path(root)/('multipa-'+uuid4().hex);created=False
        try:
            sddl='O:'+actor_sid+'G:'+actor_sid+'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;'+actor_sid+')'
            need(a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,c.byref(sd),None),'PRIVATE_SCOPE_REJECTED')
            sa=SA(c.sizeof(SA),sd,False);need(k.CreateDirectoryW(str(path),c.byref(sa)),'PRIVATE_SCOPE_REJECTED');created=True
            with _completion_directory(OwnerDirectory,path,_completion_owners):yield path
        finally:
            if sd:k.LocalFree(sd)
            if created:
                try:shutil.rmtree(path) # Only this CreateNew child, after held work drains.
                except OSError:raise ValueError('PET_MULTIPA_WORKER_CLEANUP_UNCONFIRMED')from None

class _WindowsWorkerJob:
    """No fallback: assignment/memory/exit proof must succeed before input gate."""
    def __init__(self,memory_bytes):
        c=ctypes;self.c=c;self.k=c.WinDLL('kernel32',use_last_error=True);P=c.c_void_p;D=c.c_uint32;S=c.c_size_t
        class Basic(c.Structure):_fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',D),('min_ws',S),('max_ws',S),('active',D),('affinity',S),('priority',D),('scheduling',D)]
        class IO(c.Structure):_fields_=[(n,c.c_uint64)for n in('readop','writeop','otherop','readbytes','writebytes','otherbytes')]
        class Limits(c.Structure):_fields_=[('basic',Basic),('io',IO),('process_memory',S),('job_memory',S),('peak_process',S),('peak_job',S)]
        self.Accounting=type('Accounting',(c.Structure,),{'_fields_':[('total_user',c.c_int64),('total_kernel',c.c_int64),('period_user',c.c_int64),('period_kernel',c.c_int64),('faults',D),('total',D),('active',D),('terminated',D)]})
        signatures={'CreateJobObjectW':([P,c.c_wchar_p],P),'SetInformationJobObject':([P,c.c_int,P,D],c.c_int),'AssignProcessToJobObject':([P,P],c.c_int),'QueryInformationJobObject':([P,c.c_int,P,D,c.POINTER(D)],c.c_int),'TerminateJobObject':([P,D],c.c_int),'CloseHandle':([P],c.c_int)}
        for name,(args,result)in signatures.items():f=getattr(self.k,name);f.argtypes=args;f.restype=result
        self.handle=self.k.CreateJobObjectW(None,None);need(self.handle,'WORKER_JOB_UNAVAILABLE')
        info=Limits();info.basic.flags=0x2000|0x200|8;info.basic.active=1;info.job_memory=memory_bytes
        if not self.k.SetInformationJobObject(self.handle,9,c.byref(info),c.sizeof(info)):
            self.close();raise ValueError('PET_MULTIPA_WORKER_JOB_UNAVAILABLE')
    def assign(self,process):need(self.k.AssignProcessToJobObject(self.handle,int(process._handle)),'WORKER_JOB_ASSIGNMENT_REJECTED')
    def active(self):
        info=self.Accounting();need(self.k.QueryInformationJobObject(self.handle,1,self.c.byref(info),self.c.sizeof(info),None),'WORKER_JOB_QUERY_REJECTED');return info.active
    def terminate(self):need(self.k.TerminateJobObject(self.handle,20),'WORKER_CLEANUP_UNCONFIRMED')
    def close(self):
        if self.handle:
            value=self.handle;need(self.k.CloseHandle(value),'WORKER_CLEANUP_UNCONFIRMED');self.handle=None


class _DrainUnconfirmed(ValueError):
    def __init__(self,threads):
        super().__init__('PET_MULTIPA_WORKER_CLEANUP_UNCONFIRMED');self.threads=threads

_WORKER_LOCK=threading.Lock()
_QUARANTINE=[] # Strong ownership, never merely a weak reference/finalizer.
_COMPLETION_HOLDS=[] # A failed attested close retains its real owner and both slots.


def _retain_completion_hold(invocation,stack,process,job,threads,lease,scope_owners):
    _completion._hold(invocation,_completion._OWNER)
    _COMPLETION_HOLDS.append(dict(invocation=invocation,stack=stack,process=process,
        job=job,threads=threads,lease=lease,scope_owners=scope_owners,worker_lock=_WORKER_LOCK))
    # No reaper, retry of a numeric handle, empty-stack quarantine or unlock.


def _drained(process,job,threads):
    try:
        if process.poll()is None or any(t.is_alive()for t in threads):return False
        return job is None or job.active()==0
    except Exception:return False


def _close_process(process):
    for stream in(process.stdin,process.stdout):
        if stream is not None and not stream.closed:stream.close()
    handle=getattr(process,'_handle',None)
    if handle is not None:handle.Close()


def _quarantine(stack,process,job,threads,*,release_lock):
    # Ownership transfer happens before returning failure. No later model may
    # start while this guard is retained. The reaper only checks/drains this
    # exact original process; it never respawns or retries inference.
    owner=dict(stack=stack,process=process,job=job,threads=threads,release_lock=release_lock)
    _QUARANTINE.append(owner)
    def reap():
        while True:
            try:
                if _drained(process,job,threads):
                    if job is not None:job.close()
                    _close_process(process)
                    stack.close()
                    _QUARANTINE.remove(owner)
                    if release_lock:_WORKER_LOCK.release()
                    return
            except BaseException:
                # Unknown native/cleanup state never authorizes releasing the
                # remaining resource lease or running another model.
                return
            time.sleep(.1)
    threading.Thread(target=reap,name='multipa-owned-drain',daemon=True).start()


def _communicate_owned(process,payload,*,deadline,should_stop,job=None,_completion_invocation=None):
    """Bounded actual transport; unconfirmed drain transfers original handles."""
    output=bytearray();read_done=threading.Event();write_done=threading.Event();overflow=threading.Event();pipe_failed=threading.Event()
    def read():
        try:
            while True:
                block=process.stdout.read(4096)
                if not block:break
                if len(output)+len(block)>MAX_RESULT:overflow.set();break
                output.extend(block)
        except(OSError,ValueError):pipe_failed.set()
        finally:read_done.set()
    def write():
        try:process.stdin.write(payload);process.stdin.flush()
        except(BrokenPipeError,OSError,ValueError):pipe_failed.set()
        finally:
            try:process.stdin.close()
            except OSError:pipe_failed.set()
            finally:write_done.set()
    threads=[threading.Thread(target=read,daemon=True),threading.Thread(target=write,daemon=True)]
    for t in threads:t.start()
    reason=None;natural=False
    try:
        while True:
            if overflow.is_set():reason='OUTPUT_BOUND';break
            if should_stop():reason='CANCELLED';break
            if time.monotonic()>=deadline:reason='TIMEOUT';break
            if process.poll()is not None and read_done.is_set()and write_done.is_set()and(job is None or job.active()==0):natural=True;break
            time.sleep(.02)
    except Exception:reason='PROCESS_REJECTED'
    finally:
        if not natural:
            try:
                if job is not None:job.terminate()
                elif process.poll()is None:process.kill() # POSIX transport controls only.
            except Exception:pass # Still perform original wait/join/query below.
        try:process.wait(timeout=5)
        except(Exception,):pass
        for t in threads:t.join(5)
    if not _drained(process,job,threads):raise _DrainUnconfirmed(threads)
    process.stdout.close()
    if reason:raise ValueError('PET_MULTIPA_WORKER_'+reason)
    need(not pipe_failed.is_set(),'WORKER_PROTOCOL_REJECTED')
    if _completion_invocation is not None:
        _completion._transport_drained(_completion_invocation,_completion._OWNER,process,job,threads)
    return bytes(output),process.returncode


def _run_owned_worker(key,content,*,should_stop,reference,segmented,_completion_invocation=None):
    approved=configured(key)
    need(type(segmented)is bool and (not segmented or reference is not None),'WORKER_PROTOCOL_REJECTED')
    if reference is not None:
        from .pet_multipa_reference import validate_text,capable
        if segmented:
            from .pet_multipa_reference_segments import capable as long_capable
            need(long_capable(approved),'REFERENCE_EXECUTOR_UNAVAILABLE')
        else:need(capable(approved),'REFERENCE_EXECUTOR_UNAVAILABLE')
        reference=validate_text(reference)
    need(type(content)is bytes and 0<len(content)<=(24*1024*1024 if segmented else 4194304),'WORKER_INPUT_REJECTED')
    if segmented:
        import io,wave
        from .pet_multipa_reference_segments import budget
        with wave.open(io.BytesIO(content),'rb')as wav:
            need(wav.getcomptype()=='NONE'and wav.getnchannels()==1 and wav.getsampwidth()==2 and wav.getframerate()==16000,'REFERENCE_LONG_PCM_REQUIRED')
            timeout=budget(approved,wav.getnframes())
    else:timeout=approved.get('timeout_seconds')
    memory=approved.get('memory_bytes')
    need(type(timeout)is int and 1<=timeout<=600 and type(memory)is int and 268435456<=memory<=8589934592,'WORKER_BUDGET_REQUIRED')
    need(not should_stop(),'WORKER_CANCELLED')
    if _completion_invocation is not None:
        _completion._start(_completion_invocation,_completion._OWNER,content,reference,segmented,approved)
    need(_WORKER_LOCK.acquire(blocking=False),'WORKER_BUSY_OR_DRAIN_UNCONFIRMED')
    stack=ExitStack();process=None;job=None;threads=[];retained=False;lease_owner=None;attested_ready=False;result=None
    scope_owners=[];job_initialized=False;process_initialized=False
    try:
        if _completion_invocation is None:
            lease=stack.enter_context(WindowsEpochLease(key))
        else:
            # Keep the object even when its __enter__ fails after an unknown close.
            lease_owner=_AttestedWindowsEpochLease(key)
            lease=stack.enter_context(lease_owner)
        if _completion_invocation is None:
            scratch=stack.enter_context(private_scope(approved['private_work_root'],approved['actor_sid']))
        else:
            scratch=stack.enter_context(private_scope(approved['private_work_root'],approved['actor_sid'],_completion_owners=scope_owners))
        nonce=scratch.name.removeprefix('multipa-')
        header={'schema':'multipa_worker_input_v2','nonce':nonce,'key':key,'approved':approved,'epoch':lease.doc['epoch'],'audio_bytes':len(content),'audio_sha256':hashlib.sha256(content).hexdigest()}
        if reference is not None:header.update(schema='multipa_reference_segmented_worker_input_v1'if segmented else'multipa_reference_worker_input_v1',reference=reference)
        b=json.dumps(header,sort_keys=True,separators=(',',':'),allow_nan=False).encode();need(len(b)<=1048576,'WORKER_PROTOCOL_REJECTED')
        cache=scratch/'cache';cache.mkdir()
        args=[str(lease.path('python_executable')),'-I','-S','-B','-X','pycache_prefix='+str(cache),str(lease.path('worker_script'))]
        env={k:v for k,v in os.environ.items()if k.upper()in{'SYSTEMROOT','WINDIR'}}
        env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_DATASETS_OFFLINE='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',TOKENIZERS_PARALLELISM='false',TEMP=str(scratch),TMP=str(scratch))
        if _completion_invocation is None:
            job=_WindowsWorkerJob(memory)
            process=subprocess.Popen(args,cwd=scratch,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            job=_WindowsWorkerJob.__new__(_WindowsWorkerJob)
            _WindowsWorkerJob.__init__(job,memory);job_initialized=True
            process=subprocess.Popen.__new__(subprocess.Popen)
            subprocess.Popen.__init__(process,args,cwd=scratch,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
            process_initialized=True
        try:
            job.assign(process)
        except BaseException:
            # No model/input gate has been sent. Still retain all identities if
            # exact created-bootstrap termination cannot be proven.
            try:process.kill()
            except Exception:pass
            try:process.wait(timeout=5)
            except Exception:pass
            raise ValueError('PET_MULTIPA_WORKER_JOB_ASSIGNMENT_REJECTED')from None
        try:
            if _completion_invocation is None:
                data,exitcode=_communicate_owned(process,struct.pack('!I',len(b))+b+content,deadline=time.monotonic()+timeout,should_stop=should_stop,job=job)
            else:
                data,exitcode=_communicate_owned(process,struct.pack('!I',len(b))+b+content,deadline=time.monotonic()+timeout,should_stop=should_stop,job=job,_completion_invocation=_completion_invocation)
        except _DrainUnconfirmed as error:
            threads=error.threads;raise
        lease.check()
        if segmented:result=decode(data,nonce,exitcode,reference=True,segmented=True)
        else:result=decode(data,nonce,exitcode,reference=True)if reference is not None else decode(data,nonce,exitcode)
        if _completion_invocation is not None:
            _completion._decoded(_completion_invocation,_completion._OWNER,result,nonce,lease_owner._completion_held_evidence(),lease_owner.approved)
            attested_ready=True
    finally:
        if _completion_invocation is not None and (job is not None and not job_initialized or process is not None and not process_initialized):
            # Original __init__ may already have tried the single native close.
            # Do not poll/reclose partial objects or rely on their finalizers.
            _retain_completion_hold(_completion_invocation,stack,process,job,threads,lease_owner,scope_owners)
            retained=True
            raise ValueError('PET_MULTIPA_WORKER_CLEANUP_UNCONFIRMED')from None
        if process is not None and not _drained(process,job,threads):
            # pop_all transfers original callbacks, not a reopened path/hash.
            if _completion_invocation is None:
                _quarantine(stack.pop_all(),process,job,threads,release_lock=True)
            else:_retain_completion_hold(_completion_invocation,stack,process,job,threads,lease_owner,scope_owners)
            retained=True
            raise ValueError('PET_MULTIPA_WORKER_CLEANUP_UNCONFIRMED')from None
        if not retained:
            try:
                if job is not None:job.close()
                if process is not None:_close_process(process)
                stack.close()
                if lease_owner is not None:
                    need(not lease_owner._completion_unknown,'WORKER_CLEANUP_UNCONFIRMED')
                if _completion_invocation is not None:
                    need(all(getattr(owner,'closed',False)is True for owner in scope_owners),'WORKER_CLEANUP_UNCONFIRMED')
                if attested_ready:
                    need(len(scope_owners)==2 and lease_owner._completion_release_confirmed(),'WORKER_CLEANUP_UNCONFIRMED')
            except BaseException:
                # A native close failure is unknown too. Keep the remaining
                # stack and single-worker lock; never silently drop ownership.
                if _completion_invocation is not None:
                    _retain_completion_hold(_completion_invocation,stack,process,job,threads,lease_owner,scope_owners);retained=True
                elif process is not None:_quarantine(stack.pop_all(),process,job,threads,release_lock=True);retained=True
                else:stack.close()
                raise ValueError('PET_MULTIPA_WORKER_CLEANUP_UNCONFIRMED')from None
            finally:
                if not retained:
                    if _completion_invocation is None:_WORKER_LOCK.release()
                    else:
                        try:
                            _WORKER_LOCK.release()
                            if attested_ready:_completion._released(_completion_invocation,_completion._OWNER)
                        except BaseException:
                            _retain_completion_hold(_completion_invocation,stack,process,job,threads,lease_owner,scope_owners)
                            raise ValueError('PET_MULTIPA_WORKER_CLEANUP_UNCONFIRMED')from None
    if _completion_invocation is not None:
        need(not should_stop(),'WORKER_CANCELLED')
        return _completion._seal(_completion_invocation,_completion._OWNER)
    return result


def predict_in_worker(key,content,*,should_stop=lambda:False,reference=None,segmented=False):
    """Original numeric-only API. It never creates a completion invocation/capability."""
    return _run_owned_worker(key,content,should_stop=should_stop,reference=reference,segmented=segmented)


_begin_worker_completion=_completion._begin_worker_completion
_consume_worker_completion=_completion._consume_worker_completion
_discard_worker_completion=_completion._discard_worker_completion


def _predict_in_worker_attested(invocation,key,content,*,should_stop=lambda:False,reference=None,segmented=False):
    """Private separate return type; no flag changes the legacy numeric API."""
    try:
        return _run_owned_worker(key,content,should_stop=should_stop,reference=reference,
                                 segmented=segmented,_completion_invocation=invocation)
    except BaseException:
        _completion._abort(invocation,_completion._OWNER)
        raise
