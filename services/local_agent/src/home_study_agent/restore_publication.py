"""Restore admission and durable DB publication; not a complete parent restore.

Only this module's opaque owner can publish. Nonempty registered/media bindings
remain HOLD here until the parent generation contract is integrated. A committed
DB is never reported as user restore success; original receipt/permit tails rule.
"""
from contextlib import contextmanager, ExitStack, closing
from pathlib import Path
import asyncio
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import weakref
from uuid import uuid4

MAX_JOURNAL=16384
MAX_NATIVE_CANDIDATES=64
MAX_FACT_SQL_STEPS=100000
_PHASES={'PREPARED','PUBLISH_INTENT','DATABASE_COMMITTED','COMPLETE','ABORTED'}
_ID=re.compile(r'^[A-Za-z0-9_-]{1,180}$');_HEX=re.compile(r'^[a-f0-9]{64}$')
_GATES={};_GATES_LOCK=threading.Lock()

class RestorePublicationError(RuntimeError):pass

def _need(value,code):
    if not value:raise RestorePublicationError(code)

def _fault(point):pass  # Internal fault seam; never an admission authority.

def _finite(function):
    from functools import wraps
    @wraps(function)
    def call(*args,**kwargs):
        try:return function(*args,**kwargs)
        except RestorePublicationError:raise
        except Exception:raise RestorePublicationError('RESTORE_PUBLICATION_STATE_UNCONFIRMED')from None
    return call


def _sha(raw):return hashlib.sha256(raw).hexdigest()
def _canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()

def _path(path):
    _need(isinstance(path,(str,Path)),'RESTORE_PUBLICATION_PATH_REQUIRED')
    p=Path(path).absolute()
    _need(str(p)==os.path.abspath(path)and'..'not in p.parts and p.name not in{'','.','..'}and len(str(p).encode())<=4096,'RESTORE_PUBLICATION_PATH_REQUIRED')
    return p

class _Parent:
    def __init__(self,path):self.path=path;self.stack=ExitStack()
    def __enter__(self):
        try:
            if os.name=='nt':
                from .registered_asset_restore import _TargetWin32,_win_target_check
                from .pet_media_files import _windows_parts
                self.kernel=_TargetWin32();parts=_windows_parts(str(self.path));self.chain=[];_need(self.kernel.drive_type(parts[0])in{2,3},'RESTORE_PUBLICATION_LOCAL_VOLUME_REQUIRED')
                for i in range(len(parts)):
                    p=str(Path(*parts[:i+1]));handle=self.kernel.open_target(p)if i==len(parts)-1 else self.kernel.open(p,directory=True)
                    self.stack.callback(self.kernel.close,handle)
                    info=_win_target_check(self.kernel,handle,p);self.chain.append((handle,p,info['identity']))
                self.handle=self.chain[-1][0]
            else:
                fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);self.stack.callback(os.close,fd);self.chain=[]
                for name in self.path.parts[1:]:
                    child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);self.stack.callback(os.close,child)
                    info=os.fstat(child);self.chain.append((fd,name,child,(info.st_dev,info.st_ino)));fd=child
                self.fd=fd
            self.check();return self
        except BaseException:self.stack.close();raise
    def check(self):
        if os.name=='nt':
            from .registered_asset_restore import _win_target_check
            for handle,path,identity in self.chain:_win_target_check(self.kernel,handle,path,identity=identity)
        else:
            for parent,name,fd,identity in self.chain:
                a=os.stat(name,dir_fd=parent,follow_symlinks=False);b=os.fstat(fd)
                _need(stat.S_ISDIR(a.st_mode)and(a.st_dev,a.st_ino)==identity==(b.st_dev,b.st_ino),'RESTORE_PUBLICATION_PARENT_CHANGED')
    def flush(self):
        self.check()
        if os.name!='nt':os.fsync(self.fd)
        else:
            from .registered_asset_restore import _win_target_check
            before=_win_target_check(self.kernel,self.handle,self.path);h=self.kernel.open_target(str(self.path),write=True)
            try:_win_target_check(self.kernel,h,self.path,identity=before['identity']);self.kernel.flush(h);_win_target_check(self.kernel,h,self.path,identity=before['identity'])
            finally:self.kernel.close(h)
        self.check()
    def __exit__(self,*args):self.stack.close()

class _OSLock:
    def __init__(self,parent,name):self.parent=parent;self.name=name;self.locked=False;self.fd=None;self.handle=None
    def open(self,*,create=True):
        try:return self._open(create=create)
        except BaseException:
            try:self.close()
            finally:raise
    def _open(self,*,create=True):
        self.parent.check()
        if os.name!='nt':
            self.fd=os.open(self.name,os.O_RDWR|(os.O_CREAT if create else 0)|os.O_NOFOLLOW,0o600,dir_fd=self.parent.fd)
            a=os.fstat(self.fd);b=os.stat(self.name,dir_fd=self.parent.fd,follow_symlinks=False)
            _need(stat.S_ISREG(a.st_mode)and a.st_nlink==1 and(a.st_dev,a.st_ino)==(b.st_dev,b.st_ino),'RESTORE_PUBLICATION_LOCK_ALIAS')
        else:
            k=self.parent.kernel;ct=k.ct;self.ct=ct
            self.handle=k.dll.CreateFileW('\\\\?\\'+str(self.parent.path/self.name),0xC0000080,3,None,4 if create else 3,0x00200000,None)
            if self.handle in{None,0,ct.c_void_p(-1).value}:
                self.handle=None;raise RestorePublicationError('RESTORE_PUBLICATION_LOCK_OPEN_FAILED')
            from .registered_asset_restore import _win_target_check
            info=_win_target_check(k,self.handle,self.parent.path/self.name,directory=False);_need(info['links']==1,'RESTORE_PUBLICATION_LOCK_ALIAS')
            class Overlapped(ct.Structure):
                _fields_=[('Internal',ct.c_size_t),('InternalHigh',ct.c_size_t),('Offset',ct.c_uint32),('OffsetHigh',ct.c_uint32),('hEvent',ct.c_void_p)]
            self.overlap=Overlapped();self.ct=ct
        return self
    def acquire(self,exclusive):
        self.check()
        if os.name!='nt':
            import fcntl
            try:fcntl.flock(self.fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
            except BlockingIOError:return False
        else:
            ct=self.ct;k=self.parent.kernel;fn=k.dll.LockFileEx
            fn.argtypes=[ct.c_void_p,ct.c_uint32,ct.c_uint32,ct.c_uint32,ct.c_uint32,ct.c_void_p];fn.restype=ct.c_int
            if not fn(self.handle,1|(2 if exclusive else 0),0,1,0,ct.byref(self.overlap)):
                code=ct.get_last_error()
                if code in{33,158}:return False
                raise RestorePublicationError('RESTORE_PUBLICATION_LOCK_FAILED')
        self.locked=True;return True
    def identity(self):
        if os.name!='nt':
            v=os.fstat(self.fd);return(v.st_dev,v.st_ino)
        return self.parent.kernel.info(self.handle)['identity']
    def check(self):
        self.parent.check()
        if os.name!='nt':
            a=os.fstat(self.fd);b=os.stat(self.name,dir_fd=self.parent.fd,follow_symlinks=False)
            _need(stat.S_ISREG(b.st_mode)and a.st_nlink==b.st_nlink==1 and(a.st_dev,a.st_ino)==(b.st_dev,b.st_ino),'RESTORE_PUBLICATION_LOCK_ALIAS')
        else:
            from .registered_asset_restore import _win_target_check
            info=_win_target_check(self.parent.kernel,self.handle,self.parent.path/self.name,directory=False)
            _need(info['links']==1,'RESTORE_PUBLICATION_LOCK_ALIAS')
    def close(self):
        if os.name!='nt':
            if self.fd is not None:
                import fcntl
                if self.locked:
                    self.check();fcntl.flock(self.fd,fcntl.LOCK_UN)
                os.close(self.fd);self.fd=None
        elif self.handle is not None:
            ct=self.ct;k=self.parent.kernel
            if self.locked:
                self.check();fn=k.dll.UnlockFileEx;fn.argtypes=[ct.c_void_p,ct.c_uint32,ct.c_uint32,ct.c_uint32,ct.c_void_p];fn.restype=ct.c_int
                _need(fn(self.handle,0,1,0,ct.byref(self.overlap)),'RESTORE_PUBLICATION_UNLOCK_UNCONFIRMED')
            k.close(self.handle);self.handle=None
        self.locked=False;self.parent.check()

class _Gate:
    def __init__(self):
        self.condition=threading.Condition(threading.RLock());self.active={};self.owner=None;self.waiting=False;self.poisoned=False;self.retained=[];self.database_identity=None;self.lock_identity=None;self.sql_opening=0;self.sql_connections=[]

def _actor():
    try:task=asyncio.current_task()
    except RuntimeError:task=None
    return(threading.get_ident(),task)

def _actor_key(actor):return(actor[0],id(actor[1]))

def _same_actor(owner,actor):return owner is not None and owner.active and owner.actor[0]==actor[0]and owner.actor[1]is actor[1]

def _writer_precheck(g,actor):
    _need(not g.poisoned,'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')
    if _same_actor(g.owner,actor):return
    _need(not g.sql_opening and not g.sql_connections,'RESTORE_PUBLICATION_BUSY')
    _need(not g.active.get(_actor_key(actor)),'RESTORE_PUBLICATION_READER_UPGRADE_FORBIDDEN')
    # An event-loop caller never synchronously drains another Task/process.
    foreign_same_thread=any(key[0]==actor[0]for key in g.active)or(g.owner is not None and g.owner.actor[0]==actor[0])
    _need(not foreign_same_thread and not(actor[1]is not None and(g.active or g.owner is not None or g.waiting)),'RESTORE_PUBLICATION_BUSY')

def _gate(path):
    key=(os.getpid(),str(path).casefold()if os.name=='nt'else str(path))
    with _GATES_LOCK:return _GATES.setdefault(key,_Gate())

def _names(path):return '.'+path.name+'.restore-publication.lock','.'+path.name+'.restore-publication.json','.'+path.name+'.restore-publication.last.json'

def _read(parent,name):
    parent.check()
    if os.name=='nt':
        from .registered_asset_snapshot import HeldFile,Policy
        try:
            with HeldFile(parent.path,name,Policy(100000,64,4096,MAX_JOURNAL,MAX_JOURNAL,MAX_JOURNAL))as held:raw=b''.join(held.chunks())
        except FileNotFoundError:return None
    else:
        try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent.fd)
        except FileNotFoundError:return None
        try:
            a=os.fstat(fd);_need(stat.S_ISREG(a.st_mode)and a.st_nlink==1 and 0<a.st_size<=MAX_JOURNAL,'RESTORE_PUBLICATION_JOURNAL_BOUND')
            raw=os.read(fd,MAX_JOURNAL+1);b=os.fstat(fd)
            _need((a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns)==(b.st_dev,b.st_ino,b.st_size,b.st_mtime_ns)and len(raw)==a.st_size,'RESTORE_PUBLICATION_JOURNAL_CHANGED')
        finally:os.close(fd)
    _need(0<len(raw)<=MAX_JOURNAL,'RESTORE_PUBLICATION_JOURNAL_BOUND');parent.check();return raw

def _unique(items):
    result={}
    for key,value in items:
        _need(key not in result,'RESTORE_PUBLICATION_DUPLICATE_FIELD');result[key]=value
    return result

def _json_object(raw,limit,code):
    _need(type(raw)in{str,bytes}and 0<len(raw.encode('utf-8')if type(raw)is str else raw)<=limit,code)
    try:
        value=json.loads(raw,object_pairs_hook=_unique,parse_constant=lambda _:(_ for _ in()).throw(ValueError()))
    except RestorePublicationError:raise
    except Exception:raise RestorePublicationError(code)from None
    _need(type(value)is dict,code);return value

def _decode(raw):
    v=_json_object(raw,MAX_JOURNAL,'RESTORE_PUBLICATION_JOURNAL_INVALID')
    _need(type(v)is dict and set(v)=={'schema','phase','binding','prepared_sha256','binding_sha256'}and v['schema']in{1,2,3} and type(v['schema'])is int and type(v['phase'])is str and v['phase']in _PHASES,'RESTORE_PUBLICATION_JOURNAL_INVALID')
    b=v['binding'];keys={'request_id','kind','origin_generation','result_generation','source_sha256','request_sha256','cleanup_manifest_sha256','audit_id','native_nonce','native_metadata_sha256','prepared_name'}
    if v['schema']==3:
        keys-={'native_nonce','native_metadata_sha256'}
        keys|={'authority_kind','parent_authority_id','parent_metadata_sha256','asset_mode','controlled_projection_sha256','native_policy_sha256'}
        _need(type(b)is dict and b.get('authority_kind')=='PARENT_SESSION_RESTORE'and b.get('asset_mode')in{'DB_ONLY','OWNED'}and b.get('kind')=='portable_restore'and type(b.get('parent_authority_id'))is str and re.fullmatch('[a-f0-9]{32}',b['parent_authority_id']),'RESTORE_PUBLICATION_JOURNAL_INVALID')
    if _uses_assets(v):keys|={'asset_generation','asset_manifest_sha256','asset_projection_sha256','asset_owner_binding_sha256'}
    _need(type(b)is dict and set(b)==keys,'RESTORE_PUBLICATION_JOURNAL_INVALID')
    for name in keys-{'kind','prepared_name','authority_kind','asset_mode'}:_need(type(b[name])is str and _ID.fullmatch(b[name]),'RESTORE_PUBLICATION_JOURNAL_INVALID')
    for name in['source_sha256','request_sha256','cleanup_manifest_sha256']+(['parent_metadata_sha256','controlled_projection_sha256','native_policy_sha256']if v['schema']==3 else['native_metadata_sha256']):_need(_HEX.fullmatch(b[name]),'RESTORE_PUBLICATION_JOURNAL_INVALID')
    if _uses_assets(v):
        _need(re.fullmatch('[a-f0-9]{32}',b['asset_generation'])is not None and all(_HEX.fullmatch(b[k])for k in('asset_manifest_sha256','asset_projection_sha256','asset_owner_binding_sha256')),'RESTORE_PUBLICATION_JOURNAL_INVALID')
    _need(b['kind']in{'restore','portable_restore'}and type(b['prepared_name'])is str and re.fullmatch(r'\.restore-prepared-[a-f0-9]{32}\.db',b['prepared_name'])and b['origin_generation']!=b['result_generation'],'RESTORE_PUBLICATION_JOURNAL_INVALID')
    _need(type(v['prepared_sha256'])is str and _HEX.fullmatch(v['prepared_sha256'])and v['binding_sha256']==_sha(_canonical(b)),'RESTORE_PUBLICATION_JOURNAL_INVALID')
    return v

def _write(owner,value):
    _owner(owner);raw=_canonical(value);_need(len(raw)<=MAX_JOURNAL,'RESTORE_PUBLICATION_JOURNAL_BOUND');_decode(raw)
    parent=owner.parent;name=_names(owner.path)[1];temporary=name+'.tmp-'+uuid4().hex
    if os.name=='nt':fd=os.open(parent.path/temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_BINARY,0o600)
    else:fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=parent.fd)
    try:
        with os.fdopen(fd,'wb')as out:_need(out.write(raw)==len(raw),'RESTORE_PUBLICATION_WRITE_FAILED');out.flush();os.fsync(out.fileno())
        parent.check()
        if os.name=='nt':os.replace(parent.path/temporary,parent.path/name)
        else:os.replace(temporary,name,src_dir_fd=parent.fd,dst_dir_fd=parent.fd)
        parent.flush()
    except BaseException:
        # A durable/unknown active journal is retained. Only owned temporary is
        # eligible for best-effort removal; failure never means publication passed.
        try:
            if os.name=='nt':os.unlink(parent.path/temporary)
            else:os.unlink(temporary,dir_fd=parent.fd)
        except OSError:pass
        raise

def _database_identity(path,parent,gate):
    if os.name!='nt':
        try:fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent.fd)
        except FileNotFoundError:return None
        try:
            a=os.fstat(fd);b=os.stat(path.name,dir_fd=parent.fd,follow_symlinks=False)
            _need(stat.S_ISREG(a.st_mode)and a.st_nlink==b.st_nlink==1 and(a.st_dev,a.st_ino)==(b.st_dev,b.st_ino),'RESTORE_PUBLICATION_DATABASE_ALIAS')
            return(a.st_dev,a.st_ino)
        finally:os.close(fd)
    return _windows_database_identity(path,parent,gate)


def _windows_database_identity(path,parent,gate):
    # Live DB/external-effect locks can have original concurrent RW handles.
    # Metadata identity is not source snapshot authority: no content/time hash.
    from .registered_asset_restore import _win_target_check
    parent.check();kernel=parent.kernel
    try:handle=kernel.open_target(str(path))  # FILE_READ_ATTRIBUTES/share RW, no DELETE.
    except FileNotFoundError:return None
    try:
        before=_win_target_check(kernel,handle,path,directory=False)
        _need(before['links']==1,'RESTORE_PUBLICATION_DATABASE_ALIAS')
        parent.check()
        after=_win_target_check(kernel,handle,path,directory=False,identity=before['identity'])
        _need(after['links']==1,'RESTORE_PUBLICATION_DATABASE_ALIAS')
        return before['identity']
    finally:
        try:kernel.close(handle)
        except BaseException:
            # Never lose the original unconfirmed HANDLE or admit a later writer.
            gate.poisoned=True;gate.retained.append((None,None,('native_metadata_handle',kernel,handle)))
            raise RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None



_READ_LEASES=weakref.WeakKeyDictionary()

class _ConnectionLease:
    def __init__(self,gate):self.gate=gate;self.unconfirmed=None
    def poison(self,connection):
        self.unconfirmed=connection if self.unconfirmed is None else (self.unconfirmed,connection)
        self.gate.poisoned=True

def _original_read_lease(lease,path):
    """Read identity only. This object is never a publication owner."""
    code='ORIGINAL_CONNECTION_READ_REQUIRED';_need(type(lease)is _ConnectionLease,code)
    state=_READ_LEASES.get(lease)
    _need(state is not None and state['active']
        and state['pid']==os.getpid()and state['actor']==_actor()and state['path']==_path(path)
        and not lease.gate.poisoned and lease.unconfirmed is None
        and lease.gate.active.get(_actor_key(state['actor']),0)>0,code)
    parent=state['parent'];parent.check()
    if state['identity']is not None:
        _need(_database_identity(state['path'],parent,lease.gate)==state['identity'],code)
    parent.check();return state

@contextmanager
def connection_admission(database_path):
    path=_path(database_path);g=_gate(path);actor=_actor();parent=None;lock=None;lease=_ConnectionLease(g);borrowed=False
    with g.condition:
        _need(not g.poisoned,'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')
        borrowed=_same_actor(g.owner,actor)
        if not borrowed:
            _need(not g.waiting and g.owner is None,'RESTORE_PUBLICATION_BUSY')
            parent=_Parent(path.parent).__enter__()
            try:
                lock=_OSLock(parent,_names(path)[0]).open();_need(lock.acquire(False),'RESTORE_PUBLICATION_BUSY')
                lock_identity=lock.identity()
                if g.lock_identity is not None:_need(lock_identity==g.lock_identity,'RESTORE_PUBLICATION_LOCK_ALIAS')
                g.lock_identity=lock_identity
                identity=_database_identity(path,parent,g)
                if g.database_identity is not None:_need(identity==g.database_identity,'RESTORE_PUBLICATION_DATABASE_ALIAS')
                if identity is not None:g.database_identity=identity
                raw=_read(parent,_names(path)[1])
                if raw is not None:_decode(raw);raise RestorePublicationError('RESTORE_PUBLICATION_RECOVERY_REQUIRED')
                from .parent_restore_ownership import active_path
                _need(not active_path(path)and not getattr(g,'work_anchor_active',False),'RESTORE_PUBLICATION_RECOVERY_REQUIRED')
            except BaseException as error:
                if lock:lock.close()
                parent.__exit__(None,None,None)
                if isinstance(error,RestorePublicationError)or not isinstance(error,Exception):raise
                raise RestorePublicationError('RESTORE_PUBLICATION_ADMISSION_UNCONFIRMED')from None
        original_parent=g.owner.parent if borrowed else parent
        original_identity=_database_identity(path,original_parent,g)if borrowed else identity
        g.active[_actor_key(actor)]=g.active.get(_actor_key(actor),0)+1
        _READ_LEASES[lease]={'active':True,'path':path,'parent':original_parent,
            'identity':original_identity,'pid':os.getpid(),'actor':actor}
    try:yield lease
    finally:
        _READ_LEASES[lease]['active']=False
        with g.condition:
            if lease.unconfirmed is not None:
                g.retained.append((parent,lock,lease.unconfirmed));g.poisoned=True
            else:
                try:
                    if lock:lock.close()
                    if parent:parent.__exit__(None,None,None)
                except BaseException:
                    g.retained.append((parent,lock,None));g.poisoned=True;raise RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
                g.active[_actor_key(actor)]-=1
                if not g.active[_actor_key(actor)]:del g.active[_actor_key(actor)]
            g.condition.notify_all()

class _Owner:
    def __init__(self,path,gate,parent,lock):self.path=path;self.gate=gate;self.parent=parent;self.lock=lock;self.actor=_actor();self.pid=os.getpid();self.active=True

def _owner(value):
    _need(type(value)is _Owner and value.active and value.pid==os.getpid()and _same_actor(value,_actor())and value.gate.owner is value and not value.gate.poisoned,'RESTORE_PUBLICATION_OWNER_REQUIRED')
    value.parent.check();return value

@contextmanager
def _publication_owner(database_path,timeout=120,*,deadline=None):
    path=_path(database_path);g=_gate(path);actor=_actor();end=time.monotonic()+max(0,timeout)if deadline is None else deadline
    with g.condition:
        _writer_precheck(g,actor)
        if _same_actor(g.owner,actor):
            nested=g.owner
        else:
            nested=None;_need(not g.waiting,'RESTORE_PUBLICATION_BUSY');g.waiting=True
    if nested:
        yield nested;return
    parent=None;lock=None;owner=None
    try:
        with g.condition:
            while g.active or g.owner is not None or g.sql_opening or g.sql_connections:
                left=end-time.monotonic();_need(left>0,'RESTORE_PUBLICATION_BUSY');g.condition.wait(min(left,0.05))
            parent=_Parent(path.parent).__enter__();lock=_OSLock(parent,_names(path)[0]).open()
            while not lock.acquire(True):
                _need(actor[1]is None and time.monotonic()<end,'RESTORE_PUBLICATION_BUSY');g.condition.wait(min(max(0,end-time.monotonic()),0.01))
            lock_identity=lock.identity()
            if g.lock_identity is not None:_need(lock_identity==g.lock_identity,'RESTORE_PUBLICATION_LOCK_ALIAS')
            g.lock_identity=lock_identity
            identity=_database_identity(path,parent,g)
            if g.database_identity is not None:_need(identity==g.database_identity,'RESTORE_PUBLICATION_DATABASE_ALIAS')
            if identity is not None:g.database_identity=identity
            owner=_Owner(path,g,parent,lock);g.owner=owner;g.waiting=False
        yield owner
    finally:
        with g.condition:
            g.waiting=False
            if owner:
                owner.active=False
                if g.active.get(_actor_key(actor))or g.sql_opening or g.sql_connections or g.poisoned:
                    g.poisoned=True;g.retained.append((parent,lock,None));raise RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')
            try:
                if lock:lock.close()
                if parent:parent.__exit__(None,None,None)
            except BaseException:
                g.poisoned=True;g.retained.append((parent,lock,None));raise RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
            finally:
                if not g.poisoned:g.owner=None
                g.condition.notify_all()


@contextmanager
def publication_owner(database,timeout=120,*,deadline=None):
    """The one public lock order: external effects, writer admission, owner SQL."""
    path=_path(database.path);g=_gate(path);actor=_actor()
    with g.condition:_writer_precheck(g,actor)
    stack=ExitStack()
    try:
        # Reject aliases before the old external guard resolves its configured path.
        validation=stack.enter_context(_Parent(path.parent))
        _database_identity(path,validation,g)
        _database_identity(path.with_name('.'+path.name+'.external-effects.lock'),validation,g)
        if deadline is None:
            stack.enter_context(database.external_effect_guard(timeout=0 if actor[1]is not None else timeout))
            owner=stack.enter_context(_publication_owner(path,timeout))
        else:
            _need(deadline>time.monotonic(),'RESTORE_PUBLICATION_BUSY')
            # Only explicit synchronous startup shares one original total deadline.
            stack.enter_context(database.external_effect_guard(timeout=0 if actor[1]is not None else deadline-time.monotonic(),
                deadline=None if actor[1]is not None else deadline))
            _need(deadline>time.monotonic(),'RESTORE_PUBLICATION_BUSY')
            owner=stack.enter_context(_publication_owner(path,deadline-time.monotonic(),deadline=deadline))
        owner.runtime_mode=database.runtime_mode
    except BaseException as error:
        stack.close()
        from .database import ExternalEffectBusyError
        if isinstance(error,(RestorePublicationError,ExternalEffectBusyError))or not isinstance(error,Exception):raise
        raise RestorePublicationError('RESTORE_PUBLICATION_ADMISSION_UNCONFIRMED')from None
    try:yield owner
    finally:stack.close()


def _close_sql(connection,*,path,owner=None):
    # Preserve the exact original connection; never substitute a PID/handle or
    # assume an exception means SQLite already closed it.
    try:connection.close()
    except BaseException:
        gates=[_gate(_path(path))]
        if owner is not None and owner.gate not in gates:gates.append(owner.gate)
        for gate in gates:
            gate.poisoned=True;gate.retained.append(('original_sql_connection',connection))
        raise RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None

@contextmanager
def _sql(path,*,owner=None,readonly=False,immutable=False,timeout=0,**kwargs):
    path=_path(path)
    if owner is not None:_owner(owner)
    database=path.as_uri()+'?mode=ro'+('&immutable=1'if immutable else'')if readonly else path
    gates=[_gate(path)]
    if owner is not None and owner.gate not in gates:gates.append(owner.gate)
    for gate in gates:
        with gate.condition:gate.sql_opening+=1
    try:c=sqlite3.connect(database,uri=readonly,timeout=timeout,**kwargs)
    except BaseException:
        for gate in gates:
            with gate.condition:gate.sql_opening-=1
        raise
    for gate in gates:
        with gate.condition:gate.sql_connections.append(c);gate.sql_opening-=1
    try:yield c
    finally:
        _close_sql(c,path=path,owner=owner)
        # Only normal return from this exact original close removes the object.
        # A failed close retains it here and in the poisoned gate's quarantine.
        for gate in gates:
            with gate.condition:
                matches=[i for i,value in enumerate(gate.sql_connections)if value is c]
                if len(matches)!=1:
                    gate.poisoned=True;gate.retained.append(('original_sql_registration',c))
                    raise RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')
                del gate.sql_connections[matches[0]]

@contextmanager
def _cold(path,*,owner=None):
    path=_path(path);gate=_gate(path);original=gate.owner
    # A clean WAL-mode database without sidecars cannot necessarily be opened
    # mode=ro by SQLite. Only its actual idle original owner may read a checked
    # zero-frame immutable view before recovery; no RW bootstrap precedes it.
    if(_same_actor(original,_actor())and original.path==path and(owner is None or owner is original)
       and not gate.active and not gate.sql_opening and not gate.sql_connections
       and not os.path.lexists(Path(str(path)+'-shm'))
       and all(not os.path.lexists(Path(str(path)+s))or Path(str(path)+s).lstat().st_size==0 for s in('-wal','-journal'))):
        with _checkpointed_owner_cold(original)as c:yield c
        return
    with _sql(path,owner=owner,readonly=True,timeout=0)as c:
        c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF');yield c

class _PreflightHandles(ExitStack):
    """Original readonly handles; no publication owner or SQL bypass capability."""
    def __init__(self,gate):
        super().__init__();self.gate=gate;self.original=[];self.callbacks=[]
    def enter_context(self,manager):
        from .parent_restore_workflow import _SourceCloseCallbacks
        self.original.append(manager)
        callbacks=_SourceCloseCallbacks(manager);self.callbacks.append(callbacks)
        try:return super().enter_context(manager)
        except BaseException as error:
            if not callbacks.all_confirmed():
                self.gate.poisoned=True;self.gate.retained.append(('preflight_enter',self,manager,callbacks,error))
                raise RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
            raise
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        except BaseException as error:
            self.gate.poisoned=True;self.gate.retained.append(('preflight_original_handles',self,error))
            raise RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None

@contextmanager
def _checkpointed_owner_cold(owner):
    owner=_owner(owner);path=owner.path;gate=owner.gate
    _need(not gate.active and not gate.sql_opening and not gate.sql_connections,'RESTORE_PUBLICATION_BUSY')
    from .registered_asset_snapshot import HeldFile,Policy,AssetSnapshotError
    policy=Policy(1,64,4096,2**63-1,2**63-1,2**63-1)
    try:
        with _PreflightHandles(gate)as stack:
            family={s:os.path.lexists(Path(str(path)+s))for s in('','-wal','-shm','-journal')}
            _need(family['']and not family['-shm'],'RESTORE_PUBLICATION_READONLY_PREFLIGHT_REQUIRED')
            held={s:stack.enter_context(HeldFile(path.parent,path.name+s,policy))for s in('','-wal','-journal')if family[s]}
            def verify():
                _need(not gate.poisoned,'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')
                _owner(owner)
                _need(family=={s:os.path.lexists(Path(str(path)+s))for s in family},'RESTORE_PUBLICATION_PREFLIGHT_CHANGED')
                for original in held.values():original.check()
                b=held[''].before;identity=b['identity']if os.name=='nt'else(b.st_dev,b.st_ino)
                _need(identity==gate.database_identity,'RESTORE_PUBLICATION_DATABASE_ALIAS')
                _need(all(held[s].verified_length()==0 for s in('-wal','-journal')if s in held),'RESTORE_PUBLICATION_READONLY_PREFLIGHT_REQUIRED')
            verify()
            try:
                with _sql(path,owner=owner,readonly=True,immutable=True,timeout=0)as c:
                    c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF');c.execute('BEGIN')
                    yield c
            finally:verify()
    except AssetSnapshotError:
        raise RestorePublicationError('RESTORE_PUBLICATION_PREFLIGHT_CHANGED')from None

@contextmanager
def initialization_preflight(database,*,deadline=None):
    """Reject known incompatible databases before creating coordination files.

    This view is advisory. The exclusive owner still recovers and revalidates
    before initialization. Nonempty WAL is never read as immutable. Missing SHM
    or a possible hot rollback journal needs separate original recovery authority.
    POSIX data FDs do not exclude arbitrary same-uid writers; checked identities,
    signatures and namespace changes reject that uncertainty, not authorize writes.
    """
    from .registered_asset_snapshot import HeldFile,Policy,AssetSnapshotError
    from .parent_restore_ownership import active_path
    path=_path(database.path);g=_gate(path);actor=_actor();reserved=False
    with g.condition:
        _writer_precheck(g,actor)
        borrowed=_same_actor(g.owner,actor)
        if not borrowed:
            _need(not g.active and g.owner is None and not g.waiting,'RESTORE_PUBLICATION_BUSY')
            g.waiting=True;reserved=True
    if borrowed:
        yield None;return  # The original owner already supplies all admission.
    try:
        with _PreflightHandles(g)as stack:
            nearest=path.parent
            while not os.path.lexists(nearest):nearest=nearest.parent
            parent=stack.enter_context(_Parent(nearest))
            if nearest!=path.parent:
                yield None;parent.check();return
            # Existing-only original lock: no auxiliary file is created by this
            # preflight. Closing raw source FDs cannot disturb another admitted
            # SQL connection in this process, or an existing coordinated worker.
            if os.path.lexists(path.with_name(_names(path)[0])):
                lock=_OSLock(parent,_names(path)[0]);parent.stack.callback(lock.close)
                lock._open(create=False)
                while True:
                    if deadline is not None:_need(time.monotonic()<deadline,'RESTORE_PUBLICATION_BUSY')
                    if lock.acquire(True):break
                    _need(deadline is not None and actor[1]is None,'RESTORE_PUBLICATION_BUSY')
                    with g.condition:
                        left=deadline-time.monotonic();_need(left>0,'RESTORE_PUBLICATION_BUSY')
                        g.condition.wait(min(left,0.01))
                if g.lock_identity is not None:_need(lock.identity()==g.lock_identity,'RESTORE_PUBLICATION_LOCK_ALIAS')
            policy=Policy(1,64,4096,2**63-1,2**63-1,2**63-1)
            family={suffix:os.path.lexists(Path(str(path)+suffix))for suffix in('','-wal','-shm','-journal')}
            held={}
            for suffix in('','-wal','-journal'):
                if family[suffix]:held[suffix]=stack.enter_context(HeldFile(path.parent,path.name+suffix,policy))
            # SHM content may carry normal readonly-reader marks. Retain only its
            # original metadata identity with share RW/no DELETE on Windows.
            shm_identity=None
            if family['-shm']:
                shm=Path(str(path)+'-shm')
                if os.name=='nt':
                    from .registered_asset_restore import _win_target_check
                    handle=parent.kernel.open_target(str(shm));parent.stack.callback(parent.kernel.close,handle)
                    info=_win_target_check(parent.kernel,handle,shm,directory=False)
                    _need(info['links']==1,'RESTORE_PUBLICATION_DATABASE_ALIAS');shm_identity=info['identity']
                else:
                    fd=os.open(shm.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent.fd);parent.stack.callback(os.close,fd)
                    info=os.fstat(fd);_need(stat.S_ISREG(info.st_mode)and info.st_nlink==1,'RESTORE_PUBLICATION_DATABASE_ALIAS');shm_identity=(info.st_dev,info.st_ino)
            def verify():
                parent.check()
                _need(family=={s:os.path.lexists(Path(str(path)+s))for s in family},'RESTORE_PUBLICATION_PREFLIGHT_CHANGED')
                for original in held.values():original.check()
                if shm_identity is not None:
                    if os.name=='nt':
                        now=_win_target_check(parent.kernel,handle,shm,directory=False,identity=shm_identity)
                        _need(now['links']==1,'RESTORE_PUBLICATION_DATABASE_ALIAS')
                    else:
                        now=os.stat(shm.name,dir_fd=parent.fd,follow_symlinks=False)
                        _need(stat.S_ISREG(now.st_mode)and now.st_nlink==1 and(now.st_dev,now.st_ino)==shm_identity,'RESTORE_PUBLICATION_PREFLIGHT_CHANGED')
            def length(s):return held[s].verified_length()if s in held else 0
            verify()
            _need(length('-journal')==0 and(length('-wal')==0 or family['-shm']),'RESTORE_PUBLICATION_READONLY_PREFLIGHT_REQUIRED')
            if ''in held and g.database_identity is not None:
                b=held[''].before;identity=b['identity']if os.name=='nt'else(b.st_dev,b.st_ino)
                _need(identity==g.database_identity,'RESTORE_PUBLICATION_DATABASE_ALIAS')
            try:
                if ''not in held or length('')==0 or os.path.lexists(path.with_name(_names(path)[1]))or active_path(path)or getattr(g,'work_anchor_active',False):
                    yield None;return
                # With zero/absent WAL, original data-read handles and all family
                # signatures are checked before and after; no frames are ignored.
                with _sql(path,readonly=True,immutable=length('-wal')==0,timeout=0)as connection:
                    connection.execute('PRAGMA query_only=ON');connection.execute('PRAGMA trusted_schema=OFF');connection.execute('BEGIN')
                    verify();yield connection
            finally:
                _fault('after_initialization_preflight_read');verify()
    except AssetSnapshotError:
        raise RestorePublicationError('RESTORE_PUBLICATION_PREFLIGHT_CHANGED')from None
    except OSError as error:
        # A worker/recovery fence intentionally raises a retryable TimeoutError
        # from inside the yielded initialization body.  It is an OSError too;
        # preserve its meaning rather than reporting a publication failure.
        from .database import ExternalEffectBusyError
        if isinstance(error, ExternalEffectBusyError):
            raise
        raise RestorePublicationError('RESTORE_PUBLICATION_ADMISSION_UNCONFIRMED')from None
    finally:
        if reserved:
            with g.condition:g.waiting=False;g.condition.notify_all()

def _tables(c):return {r[0]for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def _db_only(c):
    tables=_tables(c)
    for table,column in[('voice_input','local_path'),('capture_artifact','local_path'),('kg_parent_textbook_ocr_artifact','raw_image_path')]:
        if table in tables:
            _need(column in{r[1]for r in c.execute('PRAGMA table_info('+table+')')},'RESTORE_PUBLICATION_ASSET_SCHEMA_REQUIRED')
            _need(c.execute('SELECT 1 FROM '+table+' WHERE '+column+' IS NOT NULL LIMIT 1').fetchone()is None,'RESTORE_PUBLICATION_PARENT_ASSET_BINDINGS_REQUIRED')
    for table,columns in[('curriculum_corpus',['source_database_path','transcript_root_path','video_root_path']),('curriculum_standard_source',['local_root_path']),('kg_corpus',['metadata_db_path','pdf_root','image_root'])]:
        if table not in tables:continue
        available={r[1]for r in c.execute('PRAGMA table_info('+table+')')}
        for column in columns:
            if column not in available:
                _need(table=='kg_corpus','RESTORE_PUBLICATION_ASSET_SCHEMA_REQUIRED');continue
            _need(c.execute('SELECT 1 FROM '+table+' WHERE '+column+" IS NOT NULL AND "+column+"!='' LIMIT 1").fetchone()is None,'RESTORE_PUBLICATION_PARENT_ASSET_BINDINGS_REQUIRED')
    if 'kg_corpus'in tables:
        for number,(key,root)in enumerate(c.execute('SELECT corpus_id,transcript_root FROM kg_corpus'),1):
            _need(number<=100000,'RESTORE_PUBLICATION_ASSET_REFERENCE_BOUND');_need(root in{'bundle:'+key,'local://family-textbook-ocr-pages'},'RESTORE_PUBLICATION_PARENT_ASSET_BINDINGS_REQUIRED')

def _bounded_rows(c,sql,args,limit,code):
    steps=0
    def progress():
        nonlocal steps
        steps+=1000;return int(steps>MAX_FACT_SQL_STEPS)
    c.set_progress_handler(progress,1000)
    try:
        rows=c.execute(sql,args).fetchmany(limit+1);_need(len(rows)<=limit,code);return rows
    except sqlite3.Error:raise RestorePublicationError(code)from None
    finally:c.set_progress_handler(None,0)

def _facts(c,request_id,native_nonce=None):
    c.row_factory=sqlite3.Row
    row=c.execute('SELECT *FROM privacy_operation_intent WHERE request_id=?',(request_id,)).fetchone();_need(row is not None,'RESTORE_PUBLICATION_INTENT_REQUIRED');row=dict(row)
    _need(row['kind']in{'restore','portable_restore'}and row['child_id']is None and row['scope']is None and type(row['source_sha256'])is str and _HEX.fullmatch(row['source_sha256']),'RESTORE_PUBLICATION_IDENTITY_REQUIRED')
    generation=c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone();_need(generation is not None,'RESTORE_PUBLICATION_GENERATION_REQUIRED')
    code='RESTORE_PUBLICATION_NATIVE_PERMIT_REQUIRED'
    columns="nonce,CASE WHEN typeof(metadata_json)='text'AND length(CAST(metadata_json AS BLOB))BETWEEN 1 AND 8192 THEN metadata_json ELSE NULL END AS metadata_json,status"
    if native_nonce is None:
        permits=_bounded_rows(c,'SELECT '+columns+" FROM native_privacy_permit WHERE status='claimed' LIMIT ?",(MAX_NATIVE_CANDIDATES+1,),MAX_NATIVE_CANDIDATES,code)
    else:permits=_bounded_rows(c,'SELECT '+columns+' FROM native_privacy_permit WHERE nonce=? LIMIT 2',(native_nonce,),1,code)
    matches=[]
    from .native_privacy import FIELDS
    for permit in permits:
        metadata=_json_object(permit['metadata_json'],8192,code)
        _need(set(metadata)==FIELDS and all(type(metadata[k])is str and 1<=len(metadata[k])<=128 for k in['nonce','request_id','expected_generation'])and all(type(metadata[k])is str and _HEX.fullmatch(metadata[k])for k in['command_sha256','cleanup_manifest_sha256'])and type(metadata['kind'])is str and metadata['kind']in{'delete','restore','portable_restore'}and all(metadata[k]is None or(type(metadata[k])is str and 1<=len(metadata[k])<=128)for k in['child_id','scope']),code)
        if metadata['request_id']==request_id:matches.append((dict(permit),metadata))
    _need(len(matches)==1,code);permit,metadata=matches[0]
    _need(metadata['nonce']==permit['nonce']and metadata['kind']==row['kind']and metadata['child_id']is None and metadata['scope']is None and metadata['expected_generation']==row['origin_generation']and metadata['cleanup_manifest_sha256']==row['cleanup_manifest_sha256'],code)
    return row,generation[0],permit,metadata


def _is_parent_binding(binding):
    from .parent_restore_parent import PARENT
    return type(binding)is dict and binding.get('authority_kind')==PARENT

def _authority_binding(metadata):
    from .parent_restore_parent import is_metadata,binding
    if is_metadata(metadata):return binding(metadata)
    return {'native_nonce':metadata['nonce'],'native_metadata_sha256':_sha(_canonical(metadata))}

def _binding_facts(c,binding):
    if _is_parent_binding(binding):
        from .parent_restore_parent import facts
        result=facts(c,binding['request_id'],binding['parent_authority_id'])
    else:result=_facts(c,binding['request_id'],binding.get('native_nonce'))
    _need(_metadata_matches(binding,result[3]),'RESTORE_PUBLICATION_IDENTITY_REQUIRED')
    return result

def _metadata_facts(c,metadata):
    from .parent_restore_parent import is_metadata,facts
    result=facts(c,metadata['request_id'],metadata['authority_id'])if is_metadata(metadata)else _facts(c,metadata['request_id'],metadata['nonce'])
    _need(result[3]==metadata,'RESTORE_PUBLICATION_IDENTITY_REQUIRED');return result

def _authority_status(authority):
    if 'authority_id'in authority:
        _need(authority.get('policy')=='PARENT_NO_NATIVE_NO_MANAGED','RESTORE_PUBLICATION_IDENTITY_REQUIRED')
        return {'ACTIVE':'claimed','FINISHED':'finished','FAILED':'failed'}[authority['state']]
    return authority['status']

def _metadata_matches(binding,metadata):
    fields=_authority_binding(metadata)
    return all(binding.get(k)==v for k,v in fields.items())and _is_parent_binding(binding)==('authority_kind'in fields)

def _uses_assets(j):return j['schema']==2 or(j['schema']==3 and j['binding']['asset_mode']=='OWNED')

def _generation_matches(g,b):
    if _is_parent_binding(b):
        return g['authority_kind']=='PARENT_SESSION_RESTORE'and g['native_nonce']is None and g['parent_authority_id']==b['parent_authority_id']
    return g['native_nonce']==b['native_nonce']and('authority_kind'not in g.keys()or(g['authority_kind']=='NATIVE_PERMIT'and g['parent_authority_id']is None))

def _successor_facts(c,request_id,code):
    from .parent_restore_parent import exists,facts
    return facts(c,request_id)if exists(c,request_id)else _facts(c,request_id,_successor_nonce(c,request_id,code))


_MARKER_SQL='CREATE TABLE restore_publication_marker(singleton INTEGER PRIMARY KEY CHECK(singleton=1),request_id TEXT NOT NULL,binding_sha256 TEXT NOT NULL)'

def _marker_schema(c):
    rows=c.execute("SELECT type,name,sql FROM sqlite_master WHERE lower(name)='restore_publication_marker'").fetchmany(2)
    _need(len(rows)<=1,'RESTORE_PUBLICATION_MARKER_MISMATCH')
    if not rows:return False
    _need(tuple(rows[0])==('table','restore_publication_marker',_MARKER_SQL),'RESTORE_PUBLICATION_MARKER_MISMATCH')
    _need(not c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'AND lower(tbl_name)='restore_publication_marker' LIMIT 1").fetchone(),'RESTORE_PUBLICATION_TRIGGER_REQUIRED')
    return True

def _marker(c):
    if not _marker_schema(c):return None
    rows=c.execute('SELECT request_id,binding_sha256 FROM restore_publication_marker').fetchmany(2);_need(len(rows)==1,'RESTORE_PUBLICATION_MARKER_MISMATCH');return tuple(rows[0])

def _write_marker(c,request_id,binding_sha256):
    exists=_marker_schema(c);creating=not exists
    def authorize(action,arg1,arg2,database,trigger):
        if trigger is not None:return sqlite3.SQLITE_DENY
        if action in{sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE}:
            return sqlite3.SQLITE_OK if database=='main'and(arg1=='restore_publication_marker'or(creating and arg1=='sqlite_master'))else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_CREATE_TABLE:
            return sqlite3.SQLITE_OK if creating and database=='main'and arg1=='restore_publication_marker'else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in{sqlite3.SQLITE_READ,sqlite3.SQLITE_SELECT,sqlite3.SQLITE_TRANSACTION,sqlite3.SQLITE_FUNCTION}else sqlite3.SQLITE_DENY
    c.set_authorizer(authorize)
    try:
        if creating:c.execute(_MARKER_SQL)
        creating=False
        c.execute('DELETE FROM restore_publication_marker');c.execute('INSERT INTO restore_publication_marker VALUES(1,?,?)',(request_id,binding_sha256));c.commit()
    except sqlite3.Error:
        c.rollback();raise RestorePublicationError('RESTORE_PUBLICATION_MARKER_WRITE_REJECTED')from None
    # Retain the restrictive authorizer until this owned write connection closes.
    _need(_marker_schema(c),'RESTORE_PUBLICATION_MARKER_MISMATCH')


def _completed_generation_successors(c,origin,current):
    """Historical receipt proof only; never an admission/publish capability.

    UUID generations have no ordering. Every hop must be the unique original
    completed restore, with its exact retained native permit and parent audit.
    Unfinished hops, branches, cycles and missing links remain unknown.
    """
    code='RESTORE_PUBLICATION_MARKER_MISMATCH';seen=set();cursor=origin
    for _ in range(MAX_NATIVE_CANDIDATES):
        if cursor==current:return
        _need(type(cursor)is str and _ID.fullmatch(cursor)and cursor not in seen,code);seen.add(cursor)
        rows=_bounded_rows(c,"SELECT request_id FROM privacy_operation_intent WHERE kind IN('restore','portable_restore')AND origin_generation=?AND result_generation IS NOT NULL LIMIT 3",(cursor,),2,code)
        _need(len(rows)==1,code)
        row,live,permit,metadata=_successor_facts(c,rows[0][0],code)
        _need(live==current and row['origin_generation']==cursor and row['result_generation']!=cursor
            and type(row['result_generation'])is str and _ID.fullmatch(row['result_generation'])
            and row['state']=='finished'and row['finalized']==1 and _authority_status(permit)=='finished'
            and row['reference_type']=='audit_event'and type(row['reference_id'])is str
            and type(row['request_sha256'])is str and _HEX.fullmatch(row['request_sha256']),code)
        audit=c.execute("SELECT actor,action,CASE WHEN typeof(summary_json)='text'AND length(CAST(summary_json AS BLOB))BETWEEN 1 AND 8192 THEN summary_json ELSE NULL END AS summary_json FROM audit_event WHERE audit_id=?",(row['reference_id'],)).fetchone()
        _need(audit is not None and audit['actor']=='parent'and audit['action']=={'restore':'backup_restored','portable_restore':'portable_backup_restored'}[row['kind']],code)
        summary=_json_object(audit['summary_json'],8192,code)
        keys={'privacy_operation_request_id','recovery_point_backup_id'}|({'backup_id'}if row['kind']=='restore'else{'portable_backup_id','source_sha256','media_file_count'})
        _need(set(summary)<=keys and summary.get('privacy_operation_request_id')==row['request_id'],code)
        for key,value in summary.items():
            _need((type(value)is int and 0<=value<=2**53-1)if key=='media_file_count'else(type(value)is str and bool((_HEX if key=='source_sha256'else _ID).fullmatch(value))),code)
        if 'source_sha256'in summary:_need(summary['source_sha256']==row['source_sha256'],code)
        cursor=row['result_generation']
    _need(cursor==current,code)


def _successor_nonce(c,request_id,code):
    # Parse only bounded original metadata; no request-ID substring search.
    rows=_bounded_rows(c,"SELECT nonce,metadata_json FROM native_privacy_permit WHERE status='finished'AND typeof(metadata_json)='text'AND length(CAST(metadata_json AS BLOB))BETWEEN 1 AND 8192 AND json_valid(metadata_json)AND json_extract(metadata_json,'$.request_id')=? LIMIT 3",(request_id,),2,code)
    _need(len(rows)==1,code);return rows[0]['nonce']


def _historical_current_work(c,j,owner,generation):
    # History proves a prior result, never today's policy or permission. Bind
    # this exception to the later original work on this same owned connection.
    from .parent_restore_ownership import _anchor,_read_owner,_anchor_matches
    code='RESTORE_PUBLICATION_MARKER_MISMATCH';_owner(owner)
    value=_read_owner(owner);anchor=_anchor(c)
    _need(value is not None and value['state']in{'ACTIVE','CLEANED'}and anchor is not None
        and anchor['state']=='ACTIVE'and value['request_id']!=j['binding']['request_id'],code)
    _anchor_matches(value,anchor)
    _need(_marker(c)==(j['binding']['request_id'],j['binding_sha256']),code)
    b=value['binding'];row,current,permit,metadata=_binding_facts(c,b)
    _need(row['request_id']==value['request_id']==b['request_id']and current==generation==b['origin_generation']==row['origin_generation']
        and row['source_sha256']==b['source_sha256']and row['request_sha256']==b['request_sha256']
        and row['cleanup_manifest_sha256']==b['cleanup_manifest_sha256']and _metadata_matches(b,metadata)
        and row['finalized']==0 and row['reference_type']is None and row['reference_id']is None and row['result_generation']is None
        and((row['state']=='processing'and _authority_status(permit)=='claimed')
            or(row['state']=='failed'and _authority_status(permit)=='failed')),code)


def _committed(c,j,owner=None,*,historical=False):
    b=j['binding'];row,generation,permit,metadata=_binding_facts(c,b)
    if historical:
        _need(j['phase']=='COMPLETE'and row['state']=='finished'and row['finalized']==1
            and _authority_status(permit)=='finished','RESTORE_PUBLICATION_MARKER_MISMATCH')
        _historical_current_work(c,j,owner,generation)
    _need(row['origin_generation']==b['origin_generation']and row['result_generation']==b['result_generation']and row['reference_type']=='audit_event'and row['reference_id']==b['audit_id']and row['request_sha256']==b['request_sha256']and row['source_sha256']==b['source_sha256']and row['cleanup_manifest_sha256']==b['cleanup_manifest_sha256']and _metadata_matches(b,metadata),'RESTORE_PUBLICATION_MARKER_MISMATCH')
    if generation!=b['result_generation']:
        _need(historical and j['phase']=='COMPLETE'and row['state']=='finished'and row['finalized']==1 and _authority_status(permit)=='finished','RESTORE_PUBLICATION_MARKER_MISMATCH')
        _completed_generation_successors(c,b['result_generation'],generation)
    audit=c.execute("SELECT actor,action,CASE WHEN typeof(summary_json)='text'AND length(CAST(summary_json AS BLOB))BETWEEN 1 AND 8192 THEN summary_json ELSE NULL END AS summary_json FROM audit_event WHERE audit_id=?",(b['audit_id'],)).fetchone()
    _need(audit is not None and audit['actor']=='parent'and audit['action']=={'restore':'backup_restored','portable_restore':'portable_backup_restored'}[b['kind']] ,'RESTORE_PUBLICATION_AUDIT_MISMATCH')
    summary=_json_object(audit['summary_json'],8192,'RESTORE_PUBLICATION_AUDIT_MISMATCH')
    keys={'privacy_operation_request_id','recovery_point_backup_id'}|({'backup_id'}if b['kind']=='restore'else{'portable_backup_id','source_sha256','media_file_count'})
    _need(set(summary)<=keys and summary.get('privacy_operation_request_id')==b['request_id'],'RESTORE_PUBLICATION_AUDIT_MISMATCH')
    for key,value in summary.items():
        _need((type(value)is int and 0<=value<=2**53-1)if key=='media_file_count'else(type(value)is str and bool((_HEX if key=='source_sha256'else _ID).fullmatch(value))),'RESTORE_PUBLICATION_AUDIT_MISMATCH')
    _need((row['state']=='processing'and row['finalized']==0 and _authority_status(permit)=='claimed')or(row['state']=='finished'and row['finalized']==1 and _authority_status(permit)in{'claimed','finished'}),'RESTORE_PUBLICATION_RECEIPT_MISMATCH')
    if j['schema']==3 and generation==b['result_generation']and not historical:
        from .parent_restore_parent import managed_projection,native_projection
        _need(managed_projection(c)==b['controlled_projection_sha256']and native_projection(c)==b['native_policy_sha256'],'RESTORE_PARENT_POLICY_CHANGED')
    if _uses_assets(j):
        from .parent_restore_assets import validate_asset_publication
        validate_asset_publication(_owner(owner),c,b,committed=True)
    return row['state']=='finished'and row['finalized']==1 and _authority_status(permit)=='finished'


@_finite
def new_prepared_path(owner):
    _owner(owner);name='.restore-prepared-'+uuid4().hex+'.db'
    if os.name=='nt':fd=os.open(owner.path.parent/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_BINARY,0o600)
    else:fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=owner.parent.fd)
    try:os.fsync(fd)
    finally:os.close(fd)
    owner.parent.flush();return owner.path.parent/name

def _file_hash(path):
    from .registered_asset_snapshot import HeldFile,Policy
    with HeldFile(path.parent,path.name,Policy(100000,64,4096,MAX_JOURNAL,2**53-1,2**53-1))as held:return held.hash()[0]


@_finite
def prepare_publication(owner,prepared_path,request_id,*,asset_seal=None):
    _owner(owner);prepared=_path(prepared_path)
    _need(_ID.fullmatch(request_id)and prepared.parent==owner.path.parent and re.fullmatch(r'\.restore-prepared-[a-f0-9]{32}\.db',prepared.name),'RESTORE_PUBLICATION_PREPARED_REQUIRED')
    _need(_read(owner.parent,_names(owner.path)[1])is None,'RESTORE_PUBLICATION_RECOVERY_REQUIRED')
    with _cold(owner.path)as live:
        from .runtime_data_scope import assert_scope
        assert_scope(live,owner.runtime_mode)
        from .parent_restore_parent import exists
        row,origin,permit,metadata=_successor_facts(live,request_id,'RESTORE_PUBLICATION_IDENTITY_REQUIRED')if exists(live,request_id)else _facts(live,request_id)
        _need(origin==row['origin_generation']and row['state']=='processing'and row['reference_id']is None and row['finalized']==0 and _authority_status(permit)=='claimed','RESTORE_PUBLICATION_IDENTITY_REQUIRED')
    _file_hash(prepared)  # Reject initial symlink/hardlink before any writable SQL.
    with _sql(prepared,owner=owner,timeout=5)as c:
        _need(c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'and c.execute('PRAGMA foreign_key_check').fetchone()is None,'RESTORE_PUBLICATION_PREPARED_REQUIRED')
        if asset_seal is None:_db_only(c)
        from .runtime_data_scope import assert_scope
        assert_scope(c,owner.runtime_mode)
        staged,result,staged_permit,staged_metadata=_binding_facts(c,{'request_id':request_id,**_authority_binding(metadata)})
        _need(staged_permit==permit and staged_metadata==metadata and result!=origin,'RESTORE_PUBLICATION_IDENTITY_REQUIRED')
        binding={'request_id':request_id,'kind':row['kind'],'origin_generation':origin,'result_generation':result,'source_sha256':row['source_sha256'],'request_sha256':row['request_sha256'],'cleanup_manifest_sha256':row['cleanup_manifest_sha256'],'audit_id':staged['reference_id'],'prepared_name':prepared.name,**_authority_binding(metadata)}
        if asset_seal is not None:
            from .parent_restore_assets import publication_asset_binding
            binding.update(publication_asset_binding(asset_seal,owner,c))
        if _is_parent_binding(binding):
            from .parent_restore_parent import managed_projection,native_projection
            binding.update(asset_mode='DB_ONLY'if asset_seal is None else'OWNED',controlled_projection_sha256=managed_projection(c),native_policy_sha256=native_projection(c))
        j={'schema':3 if _is_parent_binding(binding)else 1 if asset_seal is None else 2,'phase':'PREPARED','binding':binding,'binding_sha256':_sha(_canonical(binding)),'prepared_sha256':'0'*64}
        _need(not c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'AND lower(tbl_name)IN ('restore_publication_marker','privacy_operation_intent','audit_event','native_privacy_permit')LIMIT 1").fetchone(),'RESTORE_PUBLICATION_TRIGGER_REQUIRED')
        _marker_schema(c)
        c.execute('PRAGMA journal_mode=DELETE')
        _write_marker(c,request_id,j['binding_sha256'])
        if not _uses_assets(j):_committed(c,j,owner)
    if _uses_assets(j):
        # Keep the original marker authorizer restrictive until its write
        # connection closes. Asset PRAGMAs run only on a new owned RO handle.
        with _cold(prepared,owner=owner)as verified:_committed(verified,j,owner)
    if os.name!='nt':
        with prepared.open('rb')as f:os.fsync(f.fileno())
    else:
        from .registered_asset_restore import _win_target_check
        handle=owner.parent.kernel.open_target(str(prepared),file_attributes=True)
        try:
            info=_win_target_check(owner.parent.kernel,handle,prepared,directory=False);_need(info['links']==1,'RESTORE_PUBLICATION_PREPARED_REQUIRED');owner.parent.kernel.flush(handle)
        finally:owner.parent.kernel.close(handle)
    owner.parent.flush();j['prepared_sha256']=_file_hash(prepared);_fault('before_prepare_journal');_write(owner,j);_fault('after_prepare_journal');return {'state':'PREPARED_DB_ONLY','request_id':request_id}


@_finite
def reconcile_publication(owner):
    _owner(owner);raw=_read(owner.parent,_names(owner.path)[1])
    if raw is None:
        from .parent_restore_ownership import reconcile_without_journal
        return reconcile_without_journal(owner)
    from .parent_restore_ownership import current
    current(owner)  # ACTIVE original anchor requires its bound ownership ledger.
    j=_decode(raw);b=j['binding']
    with _cold(owner.path)as live:
        from .runtime_data_scope import assert_scope
        assert_scope(live,owner.runtime_mode)
        row,generation,permit,metadata=_binding_facts(live,b);marker=_marker(live)
        if marker==(b['request_id'],j['binding_sha256']):
            complete=_committed(live,j,owner);return {'state':'COMPLETE'if complete else'DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS','request_id':b['request_id']}
        # An earlier completed restore may leave its marker. It is neither the
        # current commit nor permission to ignore an unexplained marker.
        if marker is not None:
            previous=_decode(_read(owner.parent,_names(owner.path)[2]))
            _need(previous['phase']=='COMPLETE'and previous['binding']['request_id']!=b['request_id']and marker==(previous['binding']['request_id'],previous['binding_sha256'])and _committed(live,previous,owner,historical=True),'RESTORE_PUBLICATION_MARKER_MISMATCH')
        _need(generation==b['origin_generation']and row['origin_generation']==generation and row['reference_id']is None and row['result_generation']is None and row['source_sha256']==b['source_sha256']and row['request_sha256']==b['request_sha256']and row['cleanup_manifest_sha256']==b['cleanup_manifest_sha256']and _metadata_matches(b,metadata),'RESTORE_PUBLICATION_MARKER_MISMATCH')
        _no_success_audit(live,b['request_id'])
        if row['state']=='failed'and row['finalized']==0 and _authority_status(permit)=='failed':
            return {'state':'ABORTED_NOT_APPLIED','request_id':b['request_id']}
        _need(row['state']=='processing'and row['finalized']==0 and _authority_status(permit)=='claimed','RESTORE_PUBLICATION_RECEIPT_MISMATCH')
        return {'state':'UNAPPLIED','request_id':b['request_id']}

def _no_success_audit(c,request_id):
    base=" FROM audit_event WHERE actor='parent'AND action IN ('backup_restored','portable_backup_restored')"
    invalid="typeof(summary_json)!='text'OR length(CAST(summary_json AS BLOB))NOT BETWEEN 1 AND 8192 OR NOT json_valid(summary_json)"
    _need(not _bounded_rows(c,'SELECT 1'+base+' AND ('+invalid+') LIMIT 1',(),1,'RESTORE_PUBLICATION_AUDIT_MISMATCH'),'RESTORE_PUBLICATION_AUDIT_MISMATCH')
    _need(not _bounded_rows(c,'SELECT 1'+base+" AND json_extract(summary_json,'$.privacy_operation_request_id')=? LIMIT 1",(request_id,),1,'RESTORE_PUBLICATION_AUDIT_MISMATCH'),'RESTORE_PUBLICATION_AUDIT_MISMATCH')

@_finite
def original_publication_metadata(owner,metadata):
    _owner(owner);raw=_read(owner.parent,_names(owner.path)[1])
    if raw is None:
        from .parent_restore_ownership import current,reconcile_without_journal
        _need(current(owner,metadata)is not None,'RESTORE_PUBLICATION_NATIVE_PERMIT_REQUIRED')
        return reconcile_without_journal(owner)
    j=_decode(raw);b=j['binding']
    _need(type(metadata)is dict and _metadata_matches(b,metadata),'RESTORE_PUBLICATION_NATIVE_PERMIT_REQUIRED')
    result=reconcile_publication(owner)
    return result

@_finite
def abort_unapplied_publication(owner,metadata):
    result=original_publication_metadata(owner,metadata)
    _need(result['state']in{'UNAPPLIED','ABORTED_NOT_APPLIED'},'RESTORE_PUBLICATION_ALREADY_APPLIED_OR_UNKNOWN')
    from .parent_restore_ownership import cleanup,finish
    from .parent_restore_assets import abort_original_generations
    abort_original_generations(owner,metadata)
    cleanup(owner,metadata)  # Original native stays claimed until all copies closed/removed.
    if result['state']=='UNAPPLIED':
        with _sql(owner.path,owner=owner,timeout=0)as c:
            c.execute('BEGIN IMMEDIATE')
            from .parent_restore_parent import is_metadata,finish as parent_finish
            if is_metadata(metadata):
                c.execute("UPDATE privacy_operation_intent SET state='failed'WHERE request_id=?AND state='processing'AND finalized=0",(metadata['request_id'],))
                parent_finish(c,metadata,'FAILED')
            else:
                from .native_privacy import fence
                fence(c,metadata)
            c.commit()
    _need(reconcile_publication(owner)['state']=='ABORTED_NOT_APPLIED','RESTORE_PUBLICATION_RECEIPT_MISMATCH')
    _fault('after_abort_original_facts');finish(owner,metadata)
    raw=_read(owner.parent,_names(owner.path)[1])
    if raw is not None:
        j=_decode(raw);j['phase']='ABORTED';_archive(owner,j)
    return {'state':'ABORTED_NOT_APPLIED','request_id':metadata['request_id']}


@_finite
def publish_prepared_database(owner):
    from contextlib import nullcontext
    _owner(owner);j=_decode(_read(owner.parent,_names(owner.path)[1]));_need(reconcile_publication(owner)['state']=='UNAPPLIED','RESTORE_PUBLICATION_ALREADY_APPLIED_OR_UNKNOWN')
    path=owner.path.parent/j['binding']['prepared_name'];_need(_file_hash(path)==j['prepared_sha256'],'RESTORE_PUBLICATION_PREPARED_CHANGED')
    if _uses_assets(j):
        from .parent_restore_assets import validate_asset_publication
        with _cold(owner.path,owner=owner)as live:validate_asset_publication(owner,live,j['binding'],committed=False)
    with _cold(path,owner=owner)as source:
        from .runtime_data_scope import assert_scope
        assert_scope(source,owner.runtime_mode)
        if not _uses_assets(j):_db_only(source)
        _need(_marker(source)==(j['binding']['request_id'],j['binding_sha256']),'RESTORE_PUBLICATION_MARKER_MISMATCH');_committed(source,j,owner)
        from .parent_restore_assets import hold_publication_assets
        with hold_publication_assets(owner,source,j['binding'])if _uses_assets(j)else nullcontext():
            j['phase']='PUBLISH_INTENT';_write(owner,j);_fault('after_publish_intent')
            # SQLite backup owns its target write transaction; never rename live/WAL.
            with _sql(owner.path,owner=owner,timeout=0)as target:
                target.execute('PRAGMA synchronous=FULL')
                def progress(status,remaining,total):
                    _need(status not in (5,6),'RESTORE_PUBLICATION_BUSY')
                source.backup(target,pages=256,progress=progress)
            _fault('after_backup')
            _need(reconcile_publication(owner)['state']=='DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS','RESTORE_PUBLICATION_MARKER_MISMATCH')
            j['phase']='DATABASE_COMMITTED';_write(owner,j);_fault('after_commit_journal')
    return {'state':'DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS','request_id':j['binding']['request_id']}


def _archive(owner,j):
    _write(owner,j);_fault('before_archive');active,last=_names(owner.path)[1:]
    if j['phase']=='ABORTED':last+='.aborted'
    previous=_read(owner.parent,last)
    if previous is not None:_decode(previous)
    if os.name=='nt':os.replace(owner.parent.path/active,owner.parent.path/last)
    else:os.replace(active,last,src_dir_fd=owner.parent.fd,dst_dir_fd=owner.parent.fd)
    owner.parent.flush();_fault('after_archive')

@_finite
def complete_publication(owner):
    _owner(owner);_need(reconcile_publication(owner)['state']=='COMPLETE','RESTORE_PUBLICATION_ORIGINAL_TAILS_PENDING')
    j=_decode(_read(owner.parent,_names(owner.path)[1]));j['phase']='COMPLETE';_archive(owner,j)
    return {'state':'GATE_RELEASED_ORIGINAL_RECEIPT_CONFIRMED','request_id':j['binding']['request_id']}


@_finite
def recover_before_initialize(database):
    path=_path(database.path)
    if not path.parent.exists():return {'state':'NO_ACTIVE_JOURNAL'}
    with publication_owner(database)as owner:
        result=reconcile_publication(owner)
        from .parent_restore_ownership import current
        _need(current(owner)is None,'RESTORE_PUBLICATION_STARTUP_RECOVERY_REQUIRED')
        if result['state']=='COMPLETE':return complete_publication(owner)
        if result['state']=='ABORTED_NOT_APPLIED':
            j=_decode(_read(owner.parent,_names(owner.path)[1]));j['phase']='ABORTED';_archive(owner,j)
            return {'state':'ABORTED_NOT_APPLIED_ARCHIVED','request_id':j['binding']['request_id']}
        _need(result['state']=='NO_ACTIVE_JOURNAL','RESTORE_PUBLICATION_STARTUP_RECOVERY_REQUIRED')
        return result
