"""Windows-only, offline native Job/parent-death proof for the synthetic worker."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smtp_owned_candidate import _WindowsJob, reap, run

CHILD = Path(__file__).with_name('smtp_owned_candidate_child.py')


def _owner_fixture(phase):
    job = _WindowsJob() if phase == 'after_go' else None
    child = subprocess.Popen([sys.executable,'-I','-S','-B',str(CHILD), 'stall_after_go' if job else 'success'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if job:
        job.assign(child)
        child.stdin.write(b'SYNTHETIC_CREDENTIALS\n'); child.stdin.flush()
        if child.stdout.readline(65) != b'READY\n': raise RuntimeError('SYNTHETIC_NOT_READY')
        child.stdin.write(b'GO\n'); child.stdin.flush()
    print(child.pid,flush=True)
    while True: time.sleep(60)


def _parent_death(phase):
    parent=subprocess.Popen([sys.executable,'-I','-S','-B',str(Path(__file__).resolve()),'--owner',phase],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    captured=[]
    reader=threading.Thread(target=lambda:captured.append(parent.stdout.readline(64)),daemon=True); reader.start(); reader.join(8)
    handle=None
    try:
        if reader.is_alive() or not captured: raise AssertionError('OWNER_NOT_READY')
        pid=int(captured[0].strip())
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32];kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)];kernel.GetExitCodeProcess.restype=ctypes.c_int
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32];kernel.WaitForSingleObject.restype=ctypes.c_uint32
        kernel.CloseHandle.argtypes=[ctypes.c_void_p];kernel.CloseHandle.restype=ctypes.c_int
        handle=kernel.OpenProcess(0x100000|0x1000,False,pid)
        if not handle: raise AssertionError('CHILD_HANDLE_UNAVAILABLE')
        code=ctypes.c_uint32()
        if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value != 259: raise AssertionError('CHILD_NOT_ACTIVE')
        parent.kill();parent.wait(timeout=5)
        if kernel.WaitForSingleObject(handle,5000) != 0: raise AssertionError('CHILD_DID_NOT_EXIT')
        return True
    finally:
        if parent.poll() is None: parent.kill(); parent.wait(timeout=5)
        if handle: kernel.CloseHandle(handle)
        parent.stdout.close()


def main():
    if os.name != 'nt': raise RuntimeError('WINDOWS_ONLY')
    if len(sys.argv)==3 and sys.argv[1]=='--owner': return _owner_fixture(sys.argv[2])
    values={}
    for mode in ('success','stall_ready','stall_after_go','bad_reply'):
        started=time.monotonic()
        outcome=run(mode,timeout_seconds=2 if mode in ('success','bad_reply') else .6)
        print(json.dumps({'mode':mode,'outcome':outcome},sort_keys=True),flush=True)
        assert time.monotonic()-started < 3
        assert outcome['native_windows'] is True
        if mode=='success': assert outcome['state']=='accepted' and outcome['exit_confirmed']
        if mode=='bad_reply': assert outcome['state']=='unknown' and not outcome['retry_permitted']
        if mode.startswith('stall'):
            assert outcome['state'] in ('not_submitted','unknown','cleanup_unconfirmed')
            assert not outcome['retry_permitted'] or outcome['state']=='not_submitted'
            if mode=='stall_after_go': assert outcome['go_maybe_sent'] and not outcome['retry_permitted']
            limit=time.monotonic()+5
            while not reap() and time.monotonic()<limit: time.sleep(.02)
            assert reap(), 'CLEANUP_UNCONFIRMED'
        values[mode]={'state':outcome['state'],'go_maybe_sent':outcome['go_maybe_sent'],'exit_confirmed':outcome['exit_confirmed']}
    values['parent_death_before_assignment_child_exited']=_parent_death('before_assignment')
    values['parent_death_after_go_job_child_exited']=_parent_death('after_go')
    print(json.dumps(values,sort_keys=True),flush=True)


if __name__=='__main__':main()
