"""Real Windows named Job + synthetic v99 SQLite; never external SMTP."""
import ctypes
import json
import os
from pathlib import Path
import queue
import sqlite3
import struct
import subprocess
import sys
import threading
import time

import pytest

from home_study_agent.database import ExternalEffectBusyError
from home_study_agent import smtp_owned_journal as journal
from test_combined_report_real_sources_fixture import synthetic_combined_family
from test_daily_report_combined_snapshot import candidate
from test_daily_report_combined_delivery import configured
from test_combined_report_loopback_candidate import sink


pytestmark=pytest.mark.skipif(sys.platform!='win32',reason='requires native Windows Job Object')

_DRIVER=r'''
import json,subprocess,sys,time,traceback
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from home_study_agent.database import Database
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
from home_study_agent import daily_report_combined_owned_delivery as owned,daily_report_visual_submission as visual
from home_study_agent.smtp_owned_process import _WindowsJob,_job_members,_process_identity
db=Database(Path(sys.argv[2])); service=ReportDeliveryService(db,b's'*32)
telemetry=PilotTestTelemetryService(db,Path(sys.argv[3])); snapshot=sys.argv[4]
visual._utc_now=lambda:datetime(2026,11,4,4,0,tzinfo=timezone.utc)
worker_code=r"""
import ctypes,sys,time
sys.stdin.buffer.readline()
ctypes.windll.kernel32.FreeConsole()
print('FREE',flush=True)
sys.stdin.buffer.readline()
ok=ctypes.windll.kernel32.AllocConsole()
print('ALLOC',ok,flush=True)
time.sleep(120)
"""
with db.external_effect_guard(timeout=10):
 for attempt in range(3):
  job=_WindowsJob()
  child=subprocess.Popen([sys._base_executable,'-I','-S','-u','-c',worker_code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
  try:
   job.assign(child); child.stdin.write(b'FREE\n');child.stdin.flush()
   if child.stdout.readline().strip()!=b'FREE':raise ValueError('SYNTHETIC_FREE_CONSOLE_FAILED')
   time.sleep(.25); members=_job_members(job,child);identity=_process_identity(child)
   if len(members)!=1:raise ValueError('SYNTHETIC_INITIAL_CONSOLE_REMAINS')
   with db.connect() as c:
    c.execute('BEGIN IMMEDIATE');view=owned._view(c,service,telemetry,snapshot)
    owned._claim(c,service,telemetry,snapshot,view,'f'*32,identity,members,job=job,process=child);c.commit()
   print(json.dumps({'job_name':job.name,'members':members,'pid':child.pid}),flush=True)
   if sys.stdin.buffer.readline()!=b'ALLOC\n':raise ValueError('SYNTHETIC_ALLOC_REQUEST_MISSING')
   child.stdin.write(b'ALLOC\n');child.stdin.flush()
   if child.stdout.readline().strip()!=b'ALLOC 1':raise ValueError('SYNTHETIC_ALLOC_CONSOLE_FAILED')
   print('ALLOCATED',flush=True)
   while True:time.sleep(1)
  except Exception as error:
   print('attempt',attempt,type(error).__name__,str(error)[:120],file=sys.stderr,flush=True)
   traceback.print_exc(file=sys.stderr)
   try:job.terminate()
   except Exception:pass
   if child.poll() is None:child.kill();child.wait(timeout=5)
   job.close()
 raise RuntimeError('INITIAL_WORKER_SNAPSHOT_UNAVAILABLE')
'''


def _job_api():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenJobObjectW.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_wchar_p];kernel.OpenJobObjectW.restype=ctypes.c_void_p
    kernel.QueryInformationJobObject.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32)];kernel.QueryInformationJobObject.restype=ctypes.c_int
    kernel.TerminateJobObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32];kernel.TerminateJobObject.restype=ctypes.c_int
    kernel.CloseHandle.argtypes=[ctypes.c_void_p];kernel.CloseHandle.restype=ctypes.c_int
    return kernel


def _members(kernel,handle):
    raw=ctypes.create_string_buffer(1024);size=ctypes.c_uint32()
    assert kernel.QueryInformationJobObject(handle,3,raw,len(raw),ctypes.byref(size))
    assigned,count=struct.unpack_from('<II',raw.raw)
    assert assigned==count and count<=8
    step=ctypes.sizeof(ctypes.c_void_p);fmt='<Q' if step==8 else '<I'
    return {struct.unpack_from(fmt,raw.raw,8+i*step)[0] for i in range(count)}


def _state(db):
    with sqlite3.connect(db.path) as c:
        statuses=[c.execute('SELECT status FROM '+table).fetchone()[0] for table in
            ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member')]
        receipt=c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone()
        members=c.execute('SELECT pid,role FROM smtp_owned_worker_member ORDER BY pid').fetchall()
        return statuses,receipt,members


def test_crashed_owner_late_console_and_three_ledgers_recover_atomically(candidate,monkeypatch):
    f,batch,snapshot,tmp_path=candidate
    with sink() as smtp:
        configured(f,snapshot,smtp.server_address[1],monkeypatch)
        src=str(Path(__file__).resolve().parents[1]/'src')
        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA'}}
        parent=subprocess.Popen([sys.executable,'-I','-u','-c',_DRIVER,src,str(f['db'].path),str(tmp_path),snapshot],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,close_fds=True,env=env,
            creationflags=subprocess.CREATE_NO_WINDOW)
        lines=queue.Queue();threading.Thread(target=lambda:lines.put(parent.stdout.readline()),daemon=True).start()
        kernel=_job_api();held=None
        try:
            line=lines.get(timeout=30)
            assert line, parent.stderr.read().decode(errors='replace')[-1000:]
            owner=json.loads(line);initial={x['pid'] for x in owner['members']};assert owner['pid'] in initial
            held=kernel.OpenJobObjectW(0x0004|0x0008,False,owner['job_name']);assert held
            parent.stdin.write(b'ALLOC\n');parent.stdin.flush()
            threading.Thread(target=lambda:lines.put(parent.stdout.readline()),daemon=True).start()
            assert lines.get(timeout=10).strip()==b'ALLOCATED'
            parent.kill();parent.wait(timeout=10)
            # A system conhost may appear only after the durable pre-GO snapshot.
            until=time.monotonic()+3
            while time.monotonic()<until and not (_members(kernel,held)-initial):time.sleep(.02)
            late=_members(kernel,held)-initial
            print('NATIVE_INITIAL_MEMBERS',len(initial),'NATIVE_LATE_JOIN',bool(late),flush=True)
            assert late, 'AllocConsole did not create a real late Job member'
            if late:
                with sqlite3.connect(f['db'].path) as c:
                    c.execute("CREATE TRIGGER fail_late BEFORE INSERT ON smtp_owned_worker_member WHEN NEW.role='console' BEGIN SELECT RAISE(ABORT,'synthetic late failure'); END");c.commit()
                with pytest.raises(ExternalEffectBusyError):
                    with f['db'].external_effect_guard(timeout=0):pass
                statuses,receipt,recorded=_state(f['db'])
                assert statuses==['processing']*3 and receipt==('pending',None) and {x[0] for x in recorded}==initial
                with sqlite3.connect(f['db'].path) as c:c.execute('DROP TRIGGER fail_late');c.commit()
            # Recovery durably records the real console before terminating the
            # job, but the extra held handle must keep the delivery blocked.
            for _ in range(20):
                with pytest.raises(ExternalEffectBusyError):
                    with f['db'].external_effect_guard(timeout=0):pass
                statuses,receipt,recorded=_state(f['db'])
                if (late <= {x[0] for x in recorded}
                        and all(journal.probe_windows_original(x['pid'],x['windows_creation_time_100ns'])=='dead' for x in owner['members'])):break
                time.sleep(.05)
            assert statuses==['processing']*3 and receipt==('pending',None)
            assert late <= {x[0] for x in recorded} and all(role=='console' for pid,role in recorded if pid in late)
            assert all(journal.probe_windows_original(x['pid'],x['windows_creation_time_100ns'])=='dead' for x in owner['members'])
            assert kernel.CloseHandle(held);held=None
            with sqlite3.connect(f['db'].path) as c:
                c.execute("CREATE TRIGGER fail_terminal BEFORE UPDATE OF status ON smtp_owned_worker_journal WHEN NEW.status='stopped' BEGIN SELECT RAISE(ABORT,'synthetic terminal failure'); END");c.commit()
            with pytest.raises(ExternalEffectBusyError):
                with f['db'].external_effect_guard(timeout=0):pass
            statuses,receipt,after=_state(f['db'])
            assert statuses==['processing']*3 and receipt==('pending',None) and after==recorded
            with sqlite3.connect(f['db'].path) as c:c.execute('DROP TRIGGER fail_terminal');c.commit()
            with f['db'].external_effect_guard(timeout=3):pass
            statuses,receipt,after=_state(f['db'])
            assert statuses==['unknown']*3 and receipt==('stopped','unknown') and after==recorded
            assert not smtp.messages and smtp.data_commands==0
        finally:
            if parent.poll() is None:parent.kill();parent.wait(timeout=10)
            if held:
                kernel.TerminateJobObject(held,25);kernel.CloseHandle(held)
            parent.stdin.close();parent.stdout.close();parent.stderr.close()
