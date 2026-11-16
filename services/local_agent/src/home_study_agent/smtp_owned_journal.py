"""Crash-recovery fence for owned SMTP workers. No production sender is wired.

All operations execute under the database's original cross-process effect lock.
A stopped worker clears only the resource fence; it does not prove delivery.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
import sqlite3
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS smtp_owned_worker_journal (
 dispatch_id TEXT PRIMARY KEY,
 pid INTEGER NOT NULL CHECK(pid > 0),
 creation_time_100ns INTEGER NOT NULL CHECK(creation_time_100ns > 0),
 status TEXT NOT NULL CHECK(status IN ('pending','stopped')),
 result TEXT CHECK(result IS NULL OR result IN ('unknown','accepted')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 CHECK((status='pending' AND result IS NULL) OR (status='stopped' AND result IS NOT NULL))
)"""

BINDING_SCHEMA = """CREATE TABLE IF NOT EXISTS smtp_owned_worker_binding (
 dispatch_id TEXT PRIMARY KEY REFERENCES smtp_owned_worker_journal(dispatch_id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('standalone','combined')),
 delivery_identity TEXT, batch_id TEXT, child_id TEXT, snapshot_id TEXT, visual_snapshot_id TEXT, claim_token TEXT,
 job_name TEXT NOT NULL UNIQUE,
 member_count INTEGER NOT NULL CHECK(member_count BETWEEN 1 AND 8),
 members_sha256 TEXT NOT NULL,
 CHECK((kind='standalone' AND delivery_identity IS NULL AND batch_id IS NULL AND child_id IS NULL AND snapshot_id IS NULL AND visual_snapshot_id IS NULL AND claim_token IS NULL)
    OR (kind='combined' AND delivery_identity IS NOT NULL AND batch_id IS NOT NULL AND child_id IS NOT NULL AND snapshot_id IS NOT NULL AND visual_snapshot_id IS NOT NULL AND claim_token=dispatch_id))
)"""
MEMBER_SCHEMA = """CREATE TABLE IF NOT EXISTS smtp_owned_worker_member (
 dispatch_id TEXT NOT NULL REFERENCES smtp_owned_worker_journal(dispatch_id) ON DELETE CASCADE,
 pid INTEGER NOT NULL CHECK(pid > 0), creation_time_100ns INTEGER NOT NULL CHECK(creation_time_100ns > 0),
 role TEXT NOT NULL CHECK(role IN ('worker','console')), image_path TEXT NOT NULL,
 PRIMARY KEY(dispatch_id,pid)
)"""


def schema_valid(c):
    row=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='smtp_owned_worker_journal'").fetchone()
    normalize=lambda value:' '.join(value.replace('IF NOT EXISTS ','').split())
    return bool(row and normalize(row[0])==normalize(SCHEMA))


def schema_valid_v99(c):
    if not schema_valid(c): return False
    normalize=lambda value:' '.join(value.replace('IF NOT EXISTS ','').split())
    for name,sql in (('smtp_owned_worker_binding',BINDING_SCHEMA),('smtp_owned_worker_member',MEMBER_SCHEMA)):
        row=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()
        if not row or normalize(row[0])!=normalize(sql): return False
    return True


def _checked_members(identity,members):
    if (type(identity) is not dict or type(identity.get('pid')) is not int or not 0<identity['pid']<=0xffffffff
            or type(identity.get('windows_creation_time_100ns')) is not int or identity['windows_creation_time_100ns']<=0
            or type(members) is not list or not 1<=len(members)<=8):
        raise ValueError('SMTP_WORKER_IDENTITY_INVALID')
    values=[]
    for item in members:
        if (type(item) is not dict or set(item)!={'pid','windows_creation_time_100ns','role','image_path'}
                or type(item['pid']) is not int or not 0<item['pid']<=0xffffffff
                or type(item['windows_creation_time_100ns']) is not int or item['windows_creation_time_100ns']<=0
                or item['role'] not in ('worker','console') or type(item['image_path']) is not str
                or not 1<=len(item['image_path'])<=32767 or '\x00' in item['image_path']):
            raise ValueError('SMTP_WORKER_MEMBERS_INVALID')
        values.append([item['pid'],item['windows_creation_time_100ns'],item['role'],item['image_path']])
    values.sort()
    if len({item[0] for item in values})!=len(values): raise ValueError('SMTP_WORKER_MEMBERS_INVALID')
    workers=[x for x in values if x[2]=='worker']
    if len(workers)!=1 or workers[0][:2]!=[identity['pid'],identity['windows_creation_time_100ns']]:
        raise ValueError('SMTP_WORKER_MEMBERS_INVALID')
    return values,sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _binding_values(dispatch_id,binding):
    if type(binding) is not dict: raise ValueError('SMTP_WORKER_BINDING_INVALID')
    kind=binding.get('kind')
    keys=('delivery_identity','batch_id','child_id','snapshot_id','visual_snapshot_id','claim_token')
    if kind=='standalone' and set(binding)=={'kind'}: return (kind,None,None,None,None,None,None)
    if (kind!='combined' or set(binding)!={'kind',*keys} or binding['claim_token']!=dispatch_id
            or any(type(binding[k]) is not str or not 1<=len(binding[k])<=256 for k in keys)):
        raise ValueError('SMTP_WORKER_BINDING_INVALID')
    return (kind,*(binding[k] for k in keys))


def _now(): return datetime.now(timezone.utc).isoformat()


def probe_windows_original(pid, creation_time):
    """Return dead/alive/unknown; a reused PID with a different epoch is dead."""
    if os.name != 'nt': return 'unknown'
    import ctypes as c
    class FileTime(c.Structure):
        _fields_=[('low',c.c_uint32),('high',c.c_uint32)]
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[c.c_uint32,c.c_int,c.c_uint32]; kernel.OpenProcess.restype=c.c_void_p
    kernel.GetProcessTimes.argtypes=[c.c_void_p,c.POINTER(FileTime),c.POINTER(FileTime),c.POINTER(FileTime),c.POINTER(FileTime)]; kernel.GetProcessTimes.restype=c.c_int
    kernel.WaitForSingleObject.argtypes=[c.c_void_p,c.c_uint32]; kernel.WaitForSingleObject.restype=c.c_uint32
    kernel.CloseHandle.argtypes=[c.c_void_p]; kernel.CloseHandle.restype=c.c_int
    handle=kernel.OpenProcess(0x1000 | 0x00100000,False,pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not handle:
        # ERROR_INVALID_PARAMETER is the documented absent-process result;
        # access denied and every other error leave the fence in place.
        return 'dead' if c.get_last_error()==87 else 'unknown'
    try:
        created,exited,kernel_time,user_time=FileTime(),FileTime(),FileTime(),FileTime()
        if not kernel.GetProcessTimes(handle,c.byref(created),c.byref(exited),c.byref(kernel_time),c.byref(user_time)):
            return 'unknown'
        actual=(created.high<<32)|created.low
        if actual != creation_time: return 'dead'
        result=kernel.WaitForSingleObject(handle,0)
        return 'dead' if result==0 else 'alive' if result==0x102 else 'unknown'
    finally:
        kernel.CloseHandle(handle)


def recover_named_job(name,members,record_late=None):
    """Stop the original named Job; unknown state never authorizes recovery."""
    if os.name!='nt' or not re.fullmatch(r'Global\\HSC_SMTP_[a-f0-9]{32}',name): return False
    import ctypes as c
    import struct
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.OpenJobObjectW.argtypes=[c.c_uint32,c.c_int,c.c_wchar_p]; kernel.OpenJobObjectW.restype=c.c_void_p
    kernel.QueryInformationJobObject.argtypes=[c.c_void_p,c.c_int,c.c_void_p,c.c_uint32,c.POINTER(c.c_uint32)]; kernel.QueryInformationJobObject.restype=c.c_int
    kernel.TerminateJobObject.argtypes=[c.c_void_p,c.c_uint32]; kernel.TerminateJobObject.restype=c.c_int
    kernel.CloseHandle.argtypes=[c.c_void_p]; kernel.CloseHandle.restype=c.c_int
    kernel.OpenProcess.argtypes=[c.c_uint32,c.c_int,c.c_uint32]; kernel.OpenProcess.restype=c.c_void_p
    kernel.IsProcessInJob.argtypes=[c.c_void_p,c.c_void_p,c.POINTER(c.c_int)]; kernel.IsProcessInJob.restype=c.c_int
    kernel.GetProcessTimes.argtypes=[c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p]; kernel.GetProcessTimes.restype=c.c_int
    kernel.QueryFullProcessImageNameW.argtypes=[c.c_void_p,c.c_uint32,c.c_wchar_p,c.POINTER(c.c_uint32)]; kernel.QueryFullProcessImageNameW.restype=c.c_int
    kernel.WaitForSingleObject.argtypes=[c.c_void_p,c.c_uint32]; kernel.WaitForSingleObject.restype=c.c_uint32
    class FileTime(c.Structure): _fields_=[('low',c.c_uint32),('high',c.c_uint32)]
    c.set_last_error(0)
    handle=kernel.OpenJobObjectW(0x0004|0x0008,False,name)  # QUERY | TERMINATE
    if not handle:
        if c.get_last_error()!=2: return False  # ERROR_FILE_NOT_FOUND only
        return all(probe_windows_original(x[0],x[1])=='dead' for x in members)
    safe=False
    try:
        known={x[0]:x[1] for x in members}
        def current():
            raw=c.create_string_buffer(1024); out=c.c_uint32()
            if not kernel.QueryInformationJobObject(handle,3,raw,len(raw),c.byref(out)): return None
            assigned,count=struct.unpack_from('<II',raw.raw)
            if count>8 or assigned!=count: return None
            size=c.sizeof(c.c_void_p); fmt='<Q' if size==8 else '<I'
            values=[struct.unpack_from(fmt,raw.raw,8+i*size)[0] for i in range(count)]
            if len(set(values))!=count: return None
            new=[]
            for pid in values:
                if pid in known:
                    if probe_windows_original(pid,known[pid])!='alive': return None
                    continue
                # A console host may be attached after the pre-GO snapshot.
                # Only a live, kernel-verified System32 conhost is admissible.
                process=kernel.OpenProcess(0x1000|0x00100000,False,pid)
                if not process: return None
                try:
                    in_job=c.c_int();created,exited,kt,ut=FileTime(),FileTime(),FileTime(),FileTime()
                    size=c.c_uint32(32768);image=c.create_unicode_buffer(size.value)
                    if (not kernel.IsProcessInJob(process,handle,c.byref(in_job)) or not in_job.value
                            or not kernel.GetProcessTimes(process,c.byref(created),c.byref(exited),c.byref(kt),c.byref(ut))
                            or not kernel.QueryFullProcessImageNameW(process,0,image,c.byref(size))
                            or kernel.WaitForSingleObject(process,0)!=0x102): return None
                    systemroot=os.environ.get('SYSTEMROOT')
                    path=os.path.normcase(os.path.realpath(image.value))
                    if not systemroot or path!=os.path.normcase(os.path.realpath(os.path.join(systemroot,'System32','conhost.exe'))): return None
                    stamp=(created.high<<32)|created.low
                    if stamp<=0: return None
                    new.append([pid,stamp,'console',path])
                finally: kernel.CloseHandle(process)
            if new:
                if len(members)+len(new)>8 or record_late is None or record_late(new) is not True: return None
                members.extend(new)
                known.update((x[0],x[1]) for x in new)
            return values
        values=current()
        if values is None: return False
        # Opening before the owner dies itself prevents kill-on-last-close.
        if values and not kernel.TerminateJobObject(handle,25): return False
        deadline=time.monotonic()+.5
        while time.monotonic()<deadline:
            raw=c.create_string_buffer(48); out=c.c_uint32()
            if not kernel.QueryInformationJobObject(handle,1,raw,len(raw),c.byref(out)): return False
            active=struct.unpack_from('<I',raw.raw,40)[0]
            if active==0:
                safe=all(probe_windows_original(x[0],x[1])=='dead' for x in members)
                break
            time.sleep(.01)
    except Exception:
        return False
    finally:
        if not kernel.CloseHandle(handle): safe=False
    if not safe: return False
    # An additional handle keeps the named Job alive and could permit another
    # assignment. Only destruction (ERROR_FILE_NOT_FOUND) closes this fence.
    c.set_last_error(0)
    extra=kernel.OpenJobObjectW(0x0004|0x0008,False,name)
    if extra:
        kernel.CloseHandle(extra)
        return False
    return c.get_last_error()==2


def _assert_combined(c,binding,status):
    identity,batch,child,snapshot,visual,token=(binding[k] for k in ('delivery_identity','batch_id','child_id','snapshot_id','visual_snapshot_id','claim_token'))
    delivery=c.execute('''SELECT 1 FROM combined_daily_delivery WHERE identity=? AND batch_id=? AND child_id=?
        AND snapshot_id=? AND claim_token=? AND status=?''',(identity,batch,child,snapshot,token,status)).fetchall()
    ledger=c.execute('''SELECT 1 FROM daily_observation_delivery_ledger l
        JOIN daily_observation_report_snapshot v ON v.snapshot_id=l.snapshot_id AND v.child_id=l.child_id
        JOIN combined_daily_snapshot s ON s.snapshot_id=? AND s.child_id=v.child_id AND s.day=v.day AND s.batch_id=?
        WHERE l.identity=? AND l.child_id=? AND l.snapshot_id=? AND l.status=?''',
                     (snapshot,batch,identity,child,visual,status)).fetchall()
    member=c.execute('SELECT 1 FROM combined_diagnostic_member WHERE batch_id=? AND child_id=? AND claim_token=? AND status=?',
                     (batch,child,token,status)).fetchall()
    if len(delivery)!=1 or len(ledger)!=1 or len(member)!=1: raise ValueError('SMTP_COMBINED_CLAIM_UNCONFIRMED')


def reserve_in_transaction(database,c,dispatch_id,identity,members,binding,*,job,process):
    """Reserve exact Job membership and existing combined claims before GO."""
    if not database._external_effect_guard_owned(): raise ValueError('SMTP_EFFECT_GUARD_REQUIRED')
    if not c.in_transaction: raise ValueError('SMTP_TRANSACTION_REQUIRED')
    if not isinstance(dispatch_id,str) or not re.fullmatch('[a-f0-9]{32}',dispatch_id):
        raise ValueError('SMTP_WORKER_IDENTITY_INVALID')
    if not schema_valid_v99(c): raise ValueError('SMTP_WORKER_JOURNAL_INVALID')
    values,digest=_checked_members(identity,members); fields=_binding_values(dispatch_id,binding)
    from .smtp_owned_process import _job_members
    if (not isinstance(getattr(job,'name',None),str)
            or not re.fullmatch(r'Global\\HSC_SMTP_[a-f0-9]{32}',job.name)
            or _checked_members(identity,_job_members(job,process))[0]!=values):
        raise ValueError('SMTP_JOB_MEMBERS_UNCONFIRMED')
    if binding['kind']=='combined': _assert_combined(c,binding,'processing')
    if c.execute("SELECT 1 FROM smtp_owned_worker_journal WHERE status='pending' LIMIT 1").fetchone():
        raise ValueError('SMTP_WORKER_RECOVERY_PENDING')
    now=_now()
    c.execute("INSERT INTO smtp_owned_worker_journal VALUES(?,?,?,'pending',NULL,?,?)",
              (dispatch_id,identity['pid'],identity['windows_creation_time_100ns'],now,now))
    c.execute('INSERT INTO smtp_owned_worker_binding VALUES(?,?,?,?,?,?,?,?,?,?,?)',(dispatch_id,*fields,job.name,len(values),digest))
    c.executemany('INSERT INTO smtp_owned_worker_member VALUES(?,?,?,?,?)',[(dispatch_id,*item) for item in values])


def reserve(database,dispatch_id,identity,members,binding,*,job,process):
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE'); reserve_in_transaction(database,c,dispatch_id,identity,members,binding,job=job,process=process); c.commit()


def _validate_pending(c,dispatch_id,*,require_dead=True):
    c.row_factory=sqlite3.Row
    row=c.execute("SELECT * FROM smtp_owned_worker_journal WHERE dispatch_id=? AND status='pending'",(dispatch_id,)).fetchone()
    binding=c.execute('SELECT * FROM smtp_owned_worker_binding WHERE dispatch_id=?',(dispatch_id,)).fetchone()
    if not row or not binding: raise ValueError('SMTP_WORKER_BINDING_UNCONFIRMED')
    raw=c.execute('SELECT pid,creation_time_100ns,role,image_path FROM smtp_owned_worker_member WHERE dispatch_id=? ORDER BY pid',(dispatch_id,)).fetchall()
    members=[{'pid':x['pid'],'windows_creation_time_100ns':x['creation_time_100ns'],'role':x['role'],'image_path':x['image_path']} for x in raw]
    identity={'pid':row['pid'],'windows_creation_time_100ns':row['creation_time_100ns']}
    values,digest=_checked_members(identity,members)
    if len(values)!=binding['member_count'] or digest!=binding['members_sha256']:
        raise ValueError('SMTP_WORKER_MEMBERS_UNCONFIRMED')
    fields={k:binding[k] for k in ('kind','delivery_identity','batch_id','child_id','snapshot_id','visual_snapshot_id','claim_token')}
    _binding_values(dispatch_id,{'kind':'standalone'} if fields['kind']=='standalone' and all(fields[x] is None for x in fields if x!='kind') else fields)
    if not re.fullmatch(r'Global\\HSC_SMTP_[a-f0-9]{32}',binding['job_name']):
        raise ValueError('SMTP_WORKER_BINDING_UNCONFIRMED')
    if require_dead and any(probe_windows_original(x[0],x[1])!='dead' for x in values):
        raise ValueError('SMTP_WORKER_EXIT_UNCONFIRMED')
    return fields,values,binding['job_name']


def finish_in_transaction(database,c,dispatch_id,result,*,job):
    if not database._external_effect_guard_owned(): raise ValueError('SMTP_EFFECT_GUARD_REQUIRED')
    if not c.in_transaction: raise ValueError('SMTP_TRANSACTION_REQUIRED')
    if result not in {'accepted','unknown'}: raise ValueError('SMTP_WORKER_RESULT_INVALID')
    if getattr(job,'handle',None) is not None: raise ValueError('SMTP_WORKER_EXIT_UNCONFIRMED')
    binding,members,name=_validate_pending(c,dispatch_id)
    saved=c.execute('SELECT job_name FROM smtp_owned_worker_binding WHERE dispatch_id=?',(dispatch_id,)).fetchone()
    if not saved or saved[0]!=getattr(job,'name',None): raise ValueError('SMTP_WORKER_BINDING_UNCONFIRMED')
    if not recover_named_job(name,members): raise ValueError('SMTP_WORKER_EXIT_UNCONFIRMED')
    if binding['kind']=='combined': _assert_combined(c,binding,'sent' if result=='accepted' else 'unknown')
    if c.execute("UPDATE smtp_owned_worker_journal SET status='stopped',result=?,updated_at=? WHERE dispatch_id=? AND status='pending'",
                 (result,_now(),dispatch_id)).rowcount!=1: raise ValueError('SMTP_WORKER_BINDING_UNCONFIRMED')


def finish(database,dispatch_id,result,*,job):
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE'); finish_in_transaction(database,c,dispatch_id,result,job=job); c.commit()


def check_and_reconcile(path):
    """Called only after acquiring the OS effect lock, including before restore."""
    if not path.exists() or path.stat().st_size == 0: return
    try:
        # sqlite3.Connection's context manager commits/rolls back, but does
        # not close the handle. Close after its transaction outcome is known.
        with closing(sqlite3.connect(path,timeout=.2)) as c, c:
            migrations=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'").fetchone()
            journal=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='smtp_owned_worker_journal'").fetchone()
            if not migrations:
                if journal or c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
                    raise ValueError('SMTP_WORKER_RECOVERY_UNAVAILABLE')
                return
            row=c.execute('SELECT max(version) FROM schema_migration').fetchone()
            version=row[0] if row else None
            if type(version) is not int: raise ValueError('SMTP_WORKER_RECOVERY_UNAVAILABLE')
            if version < 98 and not journal: return
            if not schema_valid(c): raise ValueError('SMTP_WORKER_JOURNAL_INVALID')
            if c.execute("""SELECT 1 FROM smtp_owned_worker_journal WHERE
                typeof(dispatch_id)!='text' OR length(dispatch_id)!=32 OR dispatch_id GLOB '*[^a-f0-9]*'
                OR typeof(pid)!='integer' OR pid<=0 OR pid>4294967295
                OR typeof(creation_time_100ns)!='integer' OR creation_time_100ns<=0
                OR status IS NULL OR status NOT IN ('pending','stopped')
                OR (status='pending' AND result IS NOT NULL)
                OR (status='stopped' AND (result IS NULL OR result NOT IN ('unknown','accepted'))) LIMIT 1""").fetchone():
                raise ValueError('SMTP_WORKER_JOURNAL_INVALID')
            rows=c.execute("SELECT dispatch_id FROM smtp_owned_worker_journal WHERE status='pending' LIMIT 101").fetchall()
            if len(rows)>100: raise ValueError('SMTP_WORKER_RECOVERY_PENDING')
            if version<99:
                if rows: raise ValueError('SMTP_LEGACY_MEMBER_SET_UNCONFIRMED')
                return
            if not schema_valid_v99(c): raise ValueError('SMTP_WORKER_JOURNAL_INVALID')
            if rows:
                validated=[]
                for entry in rows:
                    dispatch_id=entry[0]
                    binding,members,job_name=_validate_pending(c,dispatch_id,require_dead=False)
                    if binding['kind']=='combined': _assert_combined(c,binding,'processing')
                    def record_late(extra):
                        combined=members+extra
                        worker=next((x for x in combined if x[2]=='worker'),None)
                        if not worker: return False
                        identity={'pid':worker[0],'windows_creation_time_100ns':worker[1]}
                        items=[{'pid':x[0],'windows_creation_time_100ns':x[1],'role':x[2],'image_path':x[3]} for x in combined]
                        checked,digest=_checked_members(identity,items)
                        c.execute('BEGIN IMMEDIATE')
                        c.executemany('INSERT INTO smtp_owned_worker_member VALUES(?,?,?,?,?)',[(dispatch_id,*x) for x in extra])
                        if c.execute('UPDATE smtp_owned_worker_binding SET member_count=?,members_sha256=? WHERE dispatch_id=?',
                                     (len(checked),digest,dispatch_id)).rowcount!=1: raise ValueError('SMTP_WORKER_BINDING_UNCONFIRMED')
                        c.commit()
                        return True
                    if not recover_named_job(job_name,members,record_late): raise ValueError('SMTP_WORKER_RECOVERY_PENDING')
                    validated.append((dispatch_id,binding))
                c.execute('BEGIN IMMEDIATE')
                for dispatch_id,binding in validated:
                    if binding['kind']=='combined':
                        from .daily_report_combined_delivery import _finish
                        _finish(c,binding['delivery_identity'],binding['batch_id'],binding['child_id'],binding['snapshot_id'],dispatch_id,'unknown')
                c.executemany("UPDATE smtp_owned_worker_journal SET status='stopped',result='unknown',updated_at=? WHERE dispatch_id=? AND status='pending'",
                              [(_now(),dispatch_id) for dispatch_id,_ in validated])
                c.commit()
    except (sqlite3.Error,OSError) as error:
        raise ValueError('SMTP_WORKER_RECOVERY_UNAVAILABLE') from error
