"""Original operation ownership for DB-only scratch cleanup, never commit authority.

The durable original directory/core IDs authorize only the exact recorded file
families. SQLite aux files do not claim creation-time per-leaf identity. Unknown
members, aliases or replacement core IDs stop cleanup and keep native claimed.
"""
from pathlib import Path
from contextlib import closing,contextmanager
import hashlib,os,re,sqlite3,stat
from uuid import uuid4
from . import restore_publication as pub

MAX_MEMBERS=32
FINGERPRINT_CORES={'source':'fingerprint-source.db','target':'fingerprint-target.db','derived':'fingerprint-derived.zip'}
MAX_FINGERPRINT_SOURCE_BYTES=64*1024*1024


def _fingerprint_relative(relative,work):
    # Only these exact work-relative leaves receive the three typed slots.
    return relative in{work+'/'+name for name in FINGERPRINT_CORES.values()}


def _fingerprint_idle(path):
    gate=pub._gate(path)
    with gate.condition:pub._need(not gate.sql_opening and not gate.sql_connections,_CODE)


def _member_room(value,relative):
    entries=value['members']['entries'];work=value['binding']['work_name']
    pub._need(all(e['relative']!=relative for e in entries),_CODE)
    typed=_fingerprint_relative(relative,work)
    count=sum(_fingerprint_relative(e['relative'],work)==typed for e in entries)
    pub._need(count<(len(FINGERPRINT_CORES)if typed else MAX_MEMBERS),_CODE)
_CODE='RESTORE_PUBLICATION_OWNERSHIP_UNCONFIRMED'
_ANCHOR_SQL="CREATE TABLE restore_work_anchor(singleton INTEGER PRIMARY KEY CHECK(singleton=1),request_id TEXT NOT NULL UNIQUE CHECK(length(request_id)BETWEEN 1 AND 128),native_nonce TEXT NOT NULL CHECK(length(native_nonce)BETWEEN 1 AND 128),initial_json TEXT NOT NULL CHECK(length(CAST(initial_json AS BLOB))BETWEEN 1 AND 8192),state TEXT NOT NULL CHECK(state IN ('ACTIVE','DONE')))"

def _native_anchor(connection,create=False):
    rows=connection.execute("SELECT type,name,sql FROM sqlite_master WHERE lower(name)='restore_work_anchor'").fetchmany(2)
    if not rows:
        if create:connection.execute(_ANCHOR_SQL)
        return None
    pub._need([tuple(r)for r in rows]==[('table','restore_work_anchor',_ANCHOR_SQL)],_CODE)
    pub._need(not connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'AND lower(tbl_name)='restore_work_anchor'LIMIT 1").fetchone(),_CODE)
    connection.row_factory=sqlite3.Row;rows=connection.execute('SELECT *FROM restore_work_anchor LIMIT 2').fetchmany(2);pub._need(len(rows)<=1,_CODE)
    if not rows:return None
    value=dict(rows[0]);initial=pub._json_object(value['initial_json'],8192,_CODE)
    pub._need(value['singleton']==1 and value['state']in{'ACTIVE','DONE'}and set(initial)=={'binding','members'},_CODE)
    probe={'schema':1,'revision':1,'request_id':value['request_id'],'native_nonce':value['native_nonce'],'binding':initial['binding'],'members':initial['members'],'state':'ACTIVE'};_decode(probe)
    pub._need(all(not e['removed']and not e['deleting']for e in initial['members']['entries']),_CODE)
    value['initial']=initial;return value

_PARENT_ANCHOR_SQL="CREATE TABLE restore_parent_work_anchor(singleton INTEGER PRIMARY KEY CHECK(singleton=1),request_id TEXT NOT NULL UNIQUE CHECK(length(request_id)BETWEEN 1 AND 128),parent_authority_id TEXT NOT NULL REFERENCES restore_parent_authority(authority_id),initial_json TEXT NOT NULL CHECK(length(CAST(initial_json AS BLOB))BETWEEN 1 AND 8192),state TEXT NOT NULL CHECK(state IN ('ACTIVE','DONE')))"
def _parent_anchor(c,create=False):
    rows=c.execute("SELECT type,name,sql FROM sqlite_master WHERE lower(name)='restore_parent_work_anchor'").fetchmany(2)
    if not rows:
        if create:c.execute(_PARENT_ANCHOR_SQL)
        return None
    pub._need([tuple(r)for r in rows]==[('table','restore_parent_work_anchor',_PARENT_ANCHOR_SQL)],_CODE)
    pub._need(not c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'AND lower(tbl_name)='restore_parent_work_anchor'LIMIT 1").fetchone(),_CODE)
    c.row_factory=sqlite3.Row;rows=c.execute('SELECT *FROM restore_parent_work_anchor LIMIT 2').fetchmany(2);pub._need(len(rows)<=1,_CODE)
    if not rows:return None
    value=dict(rows[0]);initial=pub._json_object(value['initial_json'],8192,_CODE)
    pub._need(value['singleton']==1 and value['state']in{'ACTIVE','DONE'}and set(initial)=={'binding','members'},_CODE)
    _decode({'schema':2,'revision':1,'request_id':value['request_id'],'authority_kind':'PARENT_SESSION_RESTORE','parent_authority_id':value['parent_authority_id'],'binding':initial['binding'],'members':initial['members'],'state':'ACTIVE'})
    pub._need(all(not e['removed']and not e['deleting']for e in initial['members']['entries']),_CODE)
    value['initial']=initial;value['authority_kind']='PARENT_SESSION_RESTORE';return value

def _anchor(c,create=False):
    native=_native_anchor(c,create=create);parent=_parent_anchor(c)
    pub._need(not(native is not None and native['state']=='ACTIVE'and parent is not None and parent['state']=='ACTIVE'),_CODE)
    if parent is not None and(parent['state']=='ACTIVE'or native is None or native['state']=='DONE'):
        # DONE cross-kind ambiguity is resolved only by the exact current ledger
        # or original archive. A newer begin removes its prior DONE opposite.
        pub._need(native is None,_CODE);return parent
    return native

def _anchor_owner(owner):
    if not owner.path.exists():return None
    with pub._cold(owner.path)as c:return _anchor(c)

def _anchor_matches(value,anchor):
    pub._need(value['binding']==anchor['initial']['binding']and value['request_id']==anchor['request_id']and pub._is_parent_binding(value['binding'])==pub._is_parent_binding(anchor.get('initial',{}).get('binding')),_CODE)
    actual={e['relative']:e for e in value['members']['entries']}
    for original in anchor['initial']['members']['entries']:
        current=actual.get(original['relative']);pub._need(current is not None and all(current[k]==original[k]for k in['relative','kind','identity','family']),_CODE)

def _anchor_commit(owner,value,initial=False):
    pub._owner(owner)
    if initial:owner.gate.work_anchor_active=True
    parent=pub._is_parent_binding(value['binding']);table='restore_parent_work_anchor'if parent else'restore_work_anchor'
    with pub._sql(owner.path,owner=owner,timeout=0)as c:
        c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE');old=_anchor(c)
        if initial:
            pub._need(old is None or old['state']=='DONE',_CODE)
            # An old DONE anchor carries history, never active authority. Its
            # original archived ledger remains. Drop only its exact fixed table.
            if old is not None and pub._is_parent_binding(old['initial']['binding'])!=parent:
                c.execute('DROP TABLE '+('restore_parent_work_anchor'if not parent else'restore_work_anchor'))
            (_parent_anchor if parent else _native_anchor)(c,create=True)
            raw=pub._canonical({'binding':value['binding'],'members':value['members']}).decode();pub._need(len(raw.encode())<=8192,_CODE)
            identity=value['parent_authority_id']if parent else value['native_nonce']
            c.execute("INSERT OR REPLACE INTO "+table+" VALUES(1,?,?,?,'ACTIVE')",(value['request_id'],identity,raw))
        else:
            pub._need(old is not None,_CODE);_anchor_matches(value,old)
            c.execute("UPDATE "+table+" SET state='DONE'WHERE singleton=1")
        c.commit()
    owner.parent.flush();owner.gate.work_anchor_active=initial

def _close_fd(owner,fd):
    try:os.close(fd)
    except BaseException:
        owner.gate.poisoned=True;owner.gate.retained.append(('restore_work_fd',fd))
        raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None

def _name(owner):return '.'+owner.path.name+'.restore-work.json'

def _ledger_identity(owner,path):return pub._database_identity(path,owner.parent,owner.gate)

def _read_owner(owner):
    from .registered_asset_snapshot import HeldFile,Policy
    pub._owner(owner);path=owner.path.parent/_name(owner)
    identity=_ledger_identity(owner,path);expected=getattr(owner.gate,'work_ledger_identity',None)
    if expected is not None:pub._need(identity==expected,_CODE)
    if identity is None:return None
    try:
        with HeldFile(path.parent,path.name,Policy(1,64,4096,pub.MAX_JOURNAL,pub.MAX_JOURNAL,pub.MAX_JOURNAL))as held:
            raw=b''.join(held.chunks());held.check()
        pub._need(_ledger_identity(owner,path)==identity,_CODE)
    except BaseException:
        owner.gate.poisoned=True;raise
    owner.gate.work_ledger_identity=identity
    value=pub._json_object(raw,pub.MAX_JOURNAL,_CODE)
    return _decode(value)

def _decode(row):
    if row is None:return None
    value=dict(row);parent=value.get('schema')==2
    top={'schema','revision','request_id','binding','members','state'}|({'authority_kind','parent_authority_id'}if parent else{'native_nonce'})
    pub._need(set(value)==top and type(value['schema'])is int and value['schema']in{1,2} and type(value['revision'])is int and 1<=value['revision']<=1000 and value['state']in{'ACTIVE','CLEANED','DONE'},_CODE);b=value['binding'];m=value['members']
    keys={'request_id','origin_generation','source_sha256','request_sha256','cleanup_manifest_sha256','work_name','prepared_name'}|({'authority_kind','parent_authority_id','parent_metadata_sha256'}if parent else{'native_nonce','native_metadata_sha256'})
    pub._need(type(b)is dict and type(m)is dict and set(b)==keys and b['request_id']==value['request_id']and ((b['authority_kind']==value['authority_kind']=='PARENT_SESSION_RESTORE'and b['parent_authority_id']==value['parent_authority_id']and re.fullmatch('[a-f0-9]{32}',b['parent_authority_id']))if parent else b['native_nonce']==value['native_nonce'])and set(m)=={'entries'}and type(m['entries'])is list and 1<=len(m['entries'])<=MAX_MEMBERS+len(FINGERPRINT_CORES),_CODE)
    for key in keys-{'work_name','prepared_name'}:pub._need(type(b[key])is str and pub._ID.fullmatch(b[key]),_CODE)
    for key in(['parent_metadata_sha256']if parent else['native_metadata_sha256'])+['source_sha256','request_sha256','cleanup_manifest_sha256']:pub._need(pub._HEX.fullmatch(b[key]),_CODE)
    pub._need(type(b['work_name'])is str and re.fullmatch(r'\.restore-work-[a-f0-9]{32}',b['work_name'])and type(b['prepared_name'])is str and re.fullmatch(r'\.restore-prepared-[a-f0-9]{32}\.db',b['prepared_name']),_CODE)
    names=set();ordinary=0
    for entry in m['entries']:
        pub._need(type(entry)is dict and set(entry)=={'relative','kind','identity','deleting','removed','family'},_CODE)
        name=entry['relative'];pub._need(type(name)is str and name not in names and len(name)<=240 and '\\'not in name and all(part not in{'','.','..'}for part in name.split('/')),_CODE);names.add(name)
        work=b['work_name'];relative=name[len(work)+1:]if name.startswith(work+'/')else None
        ordinary+=not _fingerprint_relative(name,work);pub._need(ordinary<=MAX_MEMBERS,_CODE)
        directory=name==work or relative=='upgrade-backups'
        fixed=relative in FINGERPRINT_CORES.values()or relative in{'authenticated-cold.db','authenticated-registered.zip','authenticated-portable.cache','authenticated-cold.registered.enc','media-base-allocation.json','rollback-current.db','rollback-current.json','database.db','.database.db.external-effects.lock','.database.db.restore-publication.lock'}
        upgrade=relative is not None and re.fullmatch(r'upgrade-backups/pre-upgrade-v[0-9]{1,3}-to-v[0-9]{1,3}-[0-9]{8}T[0-9]{12}Z-[a-f0-9]{8}\.(?:db|json)',relative)is not None
        rollback=relative is not None and re.fullmatch(r'\.database\.db\.upgrade-restore-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.tmp',relative)is not None
        pub._need(name==b['prepared_name']or directory or fixed or upgrade or rollback,_CODE)
        pub._need(entry['kind']==('directory'if directory else'file'),_CODE)
        expected_family=name==b['prepared_name']or relative in{'authenticated-cold.db','database.db','rollback-current.db','fingerprint-source.db','fingerprint-target.db'}or bool(upgrade and relative.endswith('.db'))or rollback
        pub._need(entry['family']is expected_family,_CODE)
        pub._need(entry['kind']in{'file','directory'}and type(entry['identity'])is list and 1<=len(entry['identity'])<=4 and all(type(v)is int and v>=0 for v in entry['identity'])and all(type(entry[k])is bool for k in['deleting','removed','family'])and(not entry['removed']or entry['deleting']),_CODE)
        pub._need(not entry['family']or entry['kind']=='file',_CODE)
    pub._need({b['prepared_name'],b['work_name'],b['work_name']+'/authenticated-cold.db',b['work_name']+'/database.db'}<=names,_CODE)
    pub._need(value['state']=='ACTIVE'or all(e['removed']for e in m['entries']),_CODE)
    value.update(binding=b,members=m);return value

def active_path(path):
    # Existence is only a fail-closed dispatch gate. It supplies no SQL authority.
    return os.path.lexists(path.with_name('.'+path.name+'.restore-work.json'))

def _identity_unchecked(owner,path,directory=False):
    pub._owner(owner)
    with pub._Parent(path.parent)as parent:
        if not directory:
            result=pub._database_identity(path,parent,owner.gate);pub._need(result is not None,_CODE);return list(result)
        with pub._Parent(path)as held:
            if os.name=='nt':result=held.chain[-1][2]
            else:
                v=os.fstat(held.fd);pub._need(v.st_uid==os.getuid()and v.st_mode&0o077==0,_CODE);result=(v.st_dev,v.st_ino)
            held.check();return list(result)

def _identity(owner,path,directory=False):
    try:return _identity_unchecked(owner,path,directory)
    except BaseException:
        owner.gate.poisoned=True;owner.gate.retained.append(('restore_work_identity',str(path)));raise

def _entry(owner,path,directory=False,family=False):
    return {'relative':path.relative_to(owner.path.parent).as_posix(),'kind':'directory'if directory else'file','identity':_identity(owner,path,directory),'deleting':False,'removed':False,'family':family}

def _check_entry(owner,entry,allow_missing=False):
    path=owner.path.parent/entry['relative']
    if not os.path.lexists(path):pub._need(allow_missing,_CODE);return False
    pub._need(_identity(owner,path,entry['kind']=='directory')==entry['identity'],_CODE);return True

def _save(owner,value,initial=False):
    pub._owner(owner);path=owner.path.parent/_name(owner)
    old=_read_owner(owner)
    pub._need(old is None if initial else old is not None and old['request_id']==value['request_id']and old['binding']==value['binding']and old['revision']==value['revision'],_CODE)
    value['revision']+=1;raw=pub._canonical(value);pub._need(len(raw)<=pub.MAX_JOURNAL,_CODE);_decode(value)
    temporary=path.with_name(path.name+'.tmp-'+uuid4().hex)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False)as out:pub._need(out.write(raw)==len(raw),_CODE);out.flush();os.fsync(fd)
    finally:_close_fd(owner,fd)
    identity=_ledger_identity(owner,temporary);pub._need(identity is not None,_CODE);owner.parent.check()
    # Metadata-only temporary. A failed close/fsync/replace poisons this admission.
    try:
        if old is not None:pub._need(_ledger_identity(owner,path)==owner.gate.work_ledger_identity,_CODE)
        os.replace(temporary,path);owner.parent.flush();pub._need(_ledger_identity(owner,path)==identity,_CODE)
        owner.gate.work_ledger_identity=identity
    except BaseException:owner.gate.poisoned=True;raise

def _facts(owner,value):
    with pub._cold(owner.path)as c:
        row,generation,permit,metadata=pub._binding_facts(c,value['binding']);b=value['binding']
        pub._need(row['kind']in{'restore','portable_restore'}and row['origin_generation']==b['origin_generation']and row['source_sha256']==b['source_sha256']and row['request_sha256']==b['request_sha256']and row['cleanup_manifest_sha256']==b['cleanup_manifest_sha256'],_CODE)
        return row,generation,permit,metadata

def current(owner,metadata=None):
    pub._owner(owner);anchor=_anchor_owner(owner);value=_read_owner(owner)
    if anchor is None:
        pub._need(value is None,_CODE)
        if owner.path.exists():
            with pub._cold(owner.path,owner=owner)as c:
                from .parent_restore_parent import AUTHORITY,validate_schema as parent_schema
                if AUTHORITY in pub._tables(c):
                    parent_schema(c);pub._need(not c.execute("SELECT 1 FROM restore_parent_authority WHERE state='ACTIVE'LIMIT 1").fetchone(),_CODE)
        owner.gate.work_anchor_active=False;return None
    owner.gate.work_anchor_active=True
    if value is None:
        # Missing ACTIVE ledger is never NO_ACTIVE. DONE also needs its exact
        # metadata archive and original terminal facts; no phase self-authorizes.
        pub._need(anchor['state']=='DONE',_CODE)
        from .registered_asset_snapshot import HeldFile,Policy
        last=owner.path.parent/(_name(owner)+'.last')
        with HeldFile(last.parent,last.name,Policy(1,64,4096,pub.MAX_JOURNAL,pub.MAX_JOURNAL,pub.MAX_JOURNAL))as held:
            value=_decode(pub._json_object(b''.join(held.chunks()),pub.MAX_JOURNAL,_CODE))
        _anchor_matches(value,anchor);pub._need(value['state']=='DONE',_CODE);_inventory(owner,value)
        row,generation,permit,original=_facts(owner,value)
        pub._need((row['state']=='finished'and row['finalized']==1 and pub._authority_status(permit)=='finished')or(row['state']=='failed'and row['finalized']==0 and pub._authority_status(permit)=='failed'),_CODE)
        owner.gate.work_anchor_active=False;return None
    _anchor_matches(value,anchor);_facts(owner,value)
    if metadata is not None:pub._need(type(metadata)is dict and pub._metadata_matches(value['binding'],metadata),_CODE)
    return value

def reconcile_without_journal(owner):
    value=current(owner)
    if value is None:return {'state':'NO_ACTIVE_JOURNAL'}
    row,generation,permit,metadata=_facts(owner,value);b=value['binding']
    with pub._cold(owner.path)as c:
        marker=pub._marker(c)
        if marker is not None:
            previous=pub._decode(pub._read(owner.parent,pub._names(owner.path)[2]));pub._need(previous['phase']=='COMPLETE'and previous['binding']['request_id']!=value['request_id']and marker==(previous['binding']['request_id'],previous['binding_sha256'])and pub._committed(c,previous,owner,historical=True),_CODE)
        pub._need(generation==b['origin_generation']and row['reference_id']is None and row['result_generation']is None and row['finalized']==0,_CODE);pub._no_success_audit(c,value['request_id'])
    pub._need((row['state']=='processing'and pub._authority_status(permit)=='claimed')or(row['state']=='failed'and pub._authority_status(permit)=='failed'),_CODE)
    return {'state':'UNAPPLIED'if pub._authority_status(permit)=='claimed'else'ABORTED_NOT_APPLIED','request_id':value['request_id']}

class _FingerprintSpoolWriter:
    """Seekable ZIP writer with no exposed truncate/fileno or unbounded seek."""
    def __init__(self,fd,max_bytes,check):
        self._fd=fd;self._max=max_bytes;self._check=check;self._size=0;self._closed=False
    @property
    def closed(self):return self._closed
    def _live(self):pub._need(not self._closed,_CODE)
    def writable(self):self._live();return True
    def seekable(self):self._live();return True
    def tell(self):self._live();return os.lseek(self._fd,0,os.SEEK_CUR)
    def seek(self,offset,whence=os.SEEK_SET):
        self._live();pub._need(type(offset)is int and type(whence)is int and whence in{os.SEEK_SET,os.SEEK_CUR,os.SEEK_END},_CODE)
        base=0 if whence==os.SEEK_SET else(self.tell()if whence==os.SEEK_CUR else self._size)
        target=base+offset;pub._need(0<=target<=self._max,_CODE);self._check()
        return os.lseek(self._fd,target,os.SEEK_SET)
    def write(self,block):
        self._live();view=memoryview(block).cast('B');size=len(view)
        position=self.tell();pub._need(position+size<=self._max,_CODE);self._check()
        while view:
            count=os.write(self._fd,view);pub._need(count>0,_CODE);view=view[count:]
        self._size=max(self._size,position+size);return size
    def flush(self):self._live();self._check()


class WorkCapability:
    def __init__(self,owner,value):self.owner=owner;self.request_id=value['request_id'];self.work=owner.path.parent/value['binding']['work_name'];self.prepared=owner.path.parent/value['binding']['prepared_name']
    def _value(self):
        pub._owner(self.owner);value=current(self.owner);pub._need(value is not None and value['request_id']==self.request_id and value['state']=='ACTIVE',_CODE)
        for e in value['members']['entries']:_check_entry(self.owner,e)
        return value
    def directory(self,path):
        value=self._value();pub._need(path==self.work/'upgrade-backups'and not os.path.lexists(path),_CODE)
        _member_room(value,path.relative_to(self.owner.path.parent).as_posix())
        path.mkdir(mode=0o700);value['members']['entries'].append(_entry(self.owner,path,True));_save(self.owner,value)
    def add_file(self,path,family=False):
        value=self._value();pub._need(path.parent==self.work or path.parent==self.work/'upgrade-backups',_CODE)
        relative=path.relative_to(self.owner.path.parent).as_posix();_member_room(value,relative)
        if _fingerprint_relative(relative,value['binding']['work_name']):
            pub._need(family is(path.name!='fingerprint-derived.zip'),_CODE)
        pub._need(not os.path.lexists(path),_CODE)
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
        try:os.fsync(fd)
        finally:_close_fd(self.owner,fd)
        value['members']['entries'].append(_entry(self.owner,path,False,family));_save(self.owner,value)
    def fingerprint_core(self,kind):
        """Fixed scratch DBs may be reused; the derived spool is CreateNew once."""
        pub._need(type(kind)is str and kind in FINGERPRINT_CORES,_CODE)
        path=self.work/FINGERPRINT_CORES[kind];_fingerprint_idle(path);value=self._value()
        entry=next((e for e in value['members']['entries']if self.owner.path.parent/e['relative']==path),None)
        if entry is None:self.add_file(path,family=kind!='derived')
        else:
            pub._need(kind!='derived'and not entry['deleting']and not entry['removed'],_CODE)
            _check_entry(self.owner,entry)
        return path
    @contextmanager
    def _fingerprint_fd(self,kind,*,empty=False):
        """Retain the original identity; never truncate before exclusive open/check.

        Windows open_osfhandle transfers the single native HANDLE to the fd.
        POSIX uses the existing synchronous publication owner and original dirfd;
        this does not claim to exclude arbitrary foreign POSIX SQLite handles.
        """
        pub._need(kind in FINGERPRINT_CORES,_CODE)
        path=self.work/FINGERPRINT_CORES[kind];_fingerprint_idle(path);value=self._value()
        entry=next((e for e in value['members']['entries']if self.owner.path.parent/e['relative']==path),None)
        pub._need(entry is not None and entry['family']is(kind!='derived')and not entry['deleting']and not entry['removed'],_CODE)
        fd=None;kernel=None;handle=None;native=None
        with _owned_parent(self.owner,value,self.work)as parent:
            def check():
                pub._owner(self.owner);parent.check()
                _fingerprint_idle(path)
                pub._need(not any(os.path.lexists(Path(str(path)+suffix))for suffix in('-wal','-shm','-journal')),_CODE)
                if os.name=='nt':
                    from .registered_asset_restore import _win_target_check
                    info=_win_target_check(kernel,native,path,directory=False,identity=tuple(entry['identity']))
                    pub._need(info['links']==1,_CODE);return info['size']
                info=os.fstat(fd);current=os.stat(path.name,dir_fd=parent.fd,follow_symlinks=False)
                pub._need(stat.S_ISREG(info.st_mode)and stat.S_ISREG(current.st_mode)
                    and info.st_nlink==current.st_nlink==1
                    and (info.st_dev,info.st_ino)==(current.st_dev,current.st_ino)==tuple(entry['identity']),_CODE)
                return info.st_size
            try:
                _fingerprint_idle(path)
                pub._need(not any(os.path.lexists(Path(str(path)+suffix))for suffix in('-wal','-shm','-journal')),_CODE)
                if os.name=='nt':
                    import msvcrt
                    from .registered_asset_restore import _TargetWin32,_win_target_check
                    kernel=_TargetWin32();handle=kernel.dll.CreateFileW('\\\\?\\'+str(path),0xC0000080,0,None,3,0x00300000,None)
                    pub._need(handle not in{None,0,kernel.ct.c_void_p(-1).value},_CODE)
                    _win_target_check(kernel,handle,path,directory=False,identity=tuple(entry['identity']))
                    native=handle;fd=msvcrt.open_osfhandle(handle,os.O_RDWR|os.O_BINARY);handle=None
                else:fd=os.open(path.name,os.O_RDWR|os.O_NOFOLLOW,dir_fd=parent.fd)
                pub._need(not empty or check()==0,_CODE)
                check();yield path,fd,check
                check()
            finally:
                if fd is not None:_close_fd(self.owner,fd)
                elif kernel is not None and handle not in{None,0,kernel.ct.c_void_p(-1).value}:
                    try:kernel.close(handle)
                    except BaseException:
                        self.owner.gate.poisoned=True;self.owner.gate.retained.append(('fingerprint_native_handle',kernel,handle))
                        raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
            parent.flush()
        _check_entry(self.owner,entry)
    def reset_fingerprint_core(self,kind):
        pub._need(type(kind)is str and kind in{'source','target'},_CODE)
        path=self.fingerprint_core(kind)
        with self._fingerprint_fd(kind)as(_,fd,check):
            check();os.ftruncate(fd,0);os.fsync(fd);pub._need(check()==0,_CODE)
        return path
    def stream_fingerprint_source(self,blocks,expected_bytes):
        pub._need(type(expected_bytes)is int and 0<expected_bytes<=MAX_FINGERPRINT_SOURCE_BYTES,_CODE)
        path=self.reset_fingerprint_core('source');digest=hashlib.sha256();total=0
        with self._fingerprint_fd('source',empty=True)as(_,fd,check):
            for block in blocks:
                pub._need(type(block)is bytes and total+len(block)<=expected_bytes,_CODE);check()
                view=memoryview(block)
                while view:
                    count=os.write(fd,view);pub._need(count>0,_CODE);view=view[count:]
                total+=len(block);digest.update(block)
            pub._need(total==expected_bytes,_CODE);os.fsync(fd);pub._need(check()==expected_bytes,_CODE)
        return path,(digest.hexdigest(),total)
    @contextmanager
    def open_fingerprint_spool_writer(self,max_bytes):
        pub._need(type(max_bytes)is int and 0<max_bytes<=2**53-1,_CODE)
        self.fingerprint_core('derived')
        with self._fingerprint_fd('derived',empty=True)as(_,fd,check):
            out=_FingerprintSpoolWriter(fd,max_bytes,check)
            try:
                yield out
                out.flush();os.fsync(fd);pub._need(check()==out._size<=max_bytes,_CODE)
            finally:out._closed=True
    def copy_held(self,held,path):
        value=self._value();entry=next((e for e in value['members']['entries']if self.owner.path.parent/e['relative']==path),None);pub._need(entry is not None and not path.stat().st_size,_CODE)
        before=held.hash();h=hashlib.sha256();size=0;_check_entry(self.owner,entry)
        fd=os.open(path,os.O_WRONLY|getattr(os,'O_NOFOLLOW',0))
        try:
            pub._need(_identity(self.owner,path)==entry['identity'],_CODE)
            with os.fdopen(fd,'wb',closefd=False)as out:
                for block in held.chunks():
                    out.write(block);h.update(block);size+=len(block);pub._fault('during_owned_cold_copy')
                out.flush();os.fsync(fd)
        finally:_close_fd(self.owner,fd)
        pub._need(before==(h.hexdigest(),size)==held.hash(),_CODE);_check_entry(self.owner,entry);return before
    def attach(self,database):self._value();pub._need(database.path==self.work/'database.db',_CODE);database._restore_work_capability=self
    def registered_zip(self,blocks,expected_bytes):
        """Only this fixed original core; ID is durable before any plaintext."""
        return self._stream_original_core('authenticated-registered.zip',blocks,expected_bytes,create=True)
    def portable_core(self,kind,blocks,expected_bytes):
        names={'cache':'authenticated-portable.cache','capsule':'authenticated-cold.registered.enc','database':'authenticated-cold.db'}
        pub._need(type(kind)is str and kind in names,_CODE)
        return self._stream_original_core(names[kind],blocks,expected_bytes,create=kind!='database')
    def _stream_original_core(self,name,blocks,expected_bytes,*,create):
        pub._need(name in{'authenticated-registered.zip','authenticated-portable.cache','authenticated-cold.registered.enc','authenticated-cold.db'},_CODE)
        pub._need(type(expected_bytes)is int and 0<expected_bytes<=2**53-1,_CODE)
        path=self.work/name
        if create:self.add_file(path,family=False)
        value=self._value();entry=next(e for e in value['members']['entries']if self.owner.path.parent/e['relative']==path)
        digest=hashlib.sha256();total=0;fd=None;kernel=None;handle=None
        with _owned_parent(self.owner,value,self.work)as parent:
            try:
                if os.name=='nt':
                    from .registered_asset_restore import _TargetWin32,_win_target_check
                    kernel=_TargetWin32();handle=kernel.dll.CreateFileW('\\\\?\\'+str(path),0x40000080,0,None,3,0x00300000,None)
                    pub._need(handle not in{None,0,kernel.ct.c_void_p(-1).value},_CODE)
                    info=_win_target_check(kernel,handle,path,directory=False,identity=tuple(entry['identity']))
                    pub._need(info['links']==1 and info['size']==0,_CODE)
                else:
                    fd=os.open(path.name,os.O_WRONLY|os.O_NOFOLLOW,dir_fd=parent.fd);info=os.fstat(fd)
                    pub._need(stat.S_ISREG(info.st_mode)and info.st_nlink==1 and info.st_size==0
                        and [info.st_dev,info.st_ino]==entry['identity'],_CODE)
                pub._fault('before_owned_registered_zip_first_byte')
                for block in blocks:
                    pub._need(type(block)is bytes and total+len(block)<=expected_bytes,_CODE);parent.check()
                    if os.name=='nt':
                        _win_target_check(kernel,handle,path,directory=False,identity=tuple(entry['identity']));kernel.write(handle,block)
                    else:
                        current=os.stat(path.name,dir_fd=parent.fd,follow_symlinks=False);held=os.fstat(fd)
                        pub._need((current.st_dev,current.st_ino)==tuple(entry['identity'])==(held.st_dev,held.st_ino)and current.st_nlink==held.st_nlink==1,_CODE)
                        view=memoryview(block)
                        while view:
                            count=os.write(fd,view);pub._need(count>0,_CODE);view=view[count:]
                    total+=len(block);digest.update(block);pub._fault('during_owned_registered_zip_copy')
                pub._need(total==expected_bytes,_CODE)
                if os.name=='nt':kernel.flush(handle);_win_target_check(kernel,handle,path,directory=False,identity=tuple(entry['identity']))
                else:
                    os.fsync(fd);current=os.stat(path.name,dir_fd=parent.fd,follow_symlinks=False);held=os.fstat(fd)
                    pub._need((current.st_dev,current.st_ino)==tuple(entry['identity'])==(held.st_dev,held.st_ino)and current.st_nlink==held.st_nlink==1 and held.st_size==expected_bytes,_CODE)
            finally:
                try:
                    if kernel is not None and handle not in{None,0,kernel.ct.c_void_p(-1).value}:kernel.close(handle)
                    if fd is not None:os.close(fd)
                except BaseException:
                    self.owner.gate.poisoned=True;self.owner.gate.retained.append(('owned_registered_zip_handle',kernel,handle,fd))
                    raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
            parent.flush()
        _check_entry(self.owner,entry);return path,(digest.hexdigest(),total)
    def upgrade(self,paths):
        self._value()
        for path in paths:self.add_file(path,family=path.suffix in{'.db','.tmp'})
    def metadata(self,path,value):
        record=self._value();entry=next((e for e in record['members']['entries']if self.owner.path.parent/e['relative']==path),None);pub._need(entry is not None,_CODE);raw=pub._canonical(value);pub._need(len(raw)<=8192,_CODE)
        _check_entry(self.owner,entry)
        out=path.open('r+b')
        try:out.seek(0);out.write(raw);out.truncate();out.flush();os.fsync(out.fileno())
        finally:
            try:out.close()
            except BaseException:
                self.owner.gate.poisoned=True;self.owner.gate.retained.append(('restore_work_metadata',out));raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
        _check_entry(self.owner,entry)

def begin(owner,request_id,*,parent_database=None):
    pub._owner(owner)
    with pub._cold(owner.path)as c:
        pub._need(_read_owner(owner)is None,_CODE)
        if parent_database is not None:
            from .parent_restore_parent import _cap,facts
            cap=_cap(parent_database);pub._need(cap.owner is owner and not cap.recovery,_CODE)
            row,generation,permit,metadata=facts(c,request_id)
        else:row,generation,permit,metadata=pub._facts(c,request_id)
        pub._need(row['kind']in{'restore','portable_restore'}and row['state']=='processing'and row['reference_id']is None and row['result_generation']is None and row['finalized']==0 and generation==row['origin_generation']and pub._authority_status(permit)=='claimed',_CODE)
        from .parent_restore_assets import assert_original_assets_before_restore
        assert_original_assets_before_restore(owner,c)
    work=owner.path.parent/('.restore-work-'+uuid4().hex);work.mkdir(mode=0o700);prepared=pub.new_prepared_path(owner)
    # A crash here can leave only empty objects. No family byte precedes commit.
    paths=[work/'authenticated-cold.db',work/'database.db',work/'.database.db.external-effects.lock',work/'.database.db.restore-publication.lock']
    for path in paths:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
        try:os.fsync(fd)
        finally:_close_fd(owner,fd)
    entries=[_entry(owner,work,True),_entry(owner,prepared,False,True)]+[_entry(owner,path,False,path.suffix=='.db')for path in paths]
    b={'request_id':request_id,**pub._authority_binding(metadata),'origin_generation':generation,'source_sha256':row['source_sha256'],'request_sha256':row['request_sha256'],'cleanup_manifest_sha256':row['cleanup_manifest_sha256'],'work_name':work.name,'prepared_name':prepared.name}
    value={'schema':2 if parent_database is not None else 1,'revision':0,'request_id':request_id,'binding':b,'members':{'entries':entries},'state':'ACTIVE'}
    value.update({'authority_kind':'PARENT_SESSION_RESTORE','parent_authority_id':metadata['authority_id']}if parent_database is not None else{'native_nonce':metadata['nonce']})
    _anchor_commit(owner,value,initial=True)
    pub._fault('after_owned_anchor_before_ledger')
    _save(owner,value,initial=True)
    with pub._Parent(work)as parent:parent.flush()
    owner.parent.flush();value=current(owner,metadata);pub._fault('after_owned_registration');return WorkCapability(owner,value)

def discard_imported(staged):
    # Only the already authenticated isolated staging copy is transformed.
    # Its backup anchor is never consulted for path cleanup or live admission.
    with pub._sql(staged.path,owner=staged._restore_sql_owner(),timeout=0)as c:
        _anchor(c)
        for table in('restore_work_anchor','restore_parent_work_anchor'):
            if table in pub._tables(c):c.execute('DROP TABLE '+table)
        c.commit()
    from .parent_restore_assets import discard_imported_schema
    discard_imported_schema(staged)

def preserve_live(live,staged,live_database=None,staged_database=None):
    # The original external ledger is never imported. Only this live anchor is
    # preserved; even a valid backup anchor cannot confer new cleanup authority.
    pub._need('restore_work_ownership'not in pub._tables(staged),_CODE)
    original=_anchor(live)
    _anchor(staged)
    for table in('restore_work_anchor','restore_parent_work_anchor'):
        if table in pub._tables(staged):staged.execute('DROP TABLE '+table)
    # Parent authority must be copied before its FK anchor/generations. Only
    # original current live rows, not any backup control facts, are admitted.
    from .parent_restore_parent import AUTHORITY,validate_schema as parent_schema
    if AUTHORITY in pub._tables(live):
        parent_schema(live);parent_schema(staged)
        pub._need(not staged.execute('SELECT 1 FROM '+AUTHORITY+' LIMIT 1').fetchone(),_CODE)
        rows=pub._bounded_rows(live,'SELECT *FROM '+AUTHORITY+' ORDER BY authority_id LIMIT 100001',(),100000,_CODE)
        columns=[r[1]for r in live.execute('PRAGMA table_info('+AUTHORITY+')')]
        staged.executemany('INSERT INTO '+AUTHORITY+'('+','.join(columns)+')VALUES('+','.join('?'for _ in columns)+')',[tuple(r)for r in rows])
    if original is not None:
        parent=pub._is_parent_binding(original['initial']['binding']);table='restore_parent_work_anchor'if parent else'restore_work_anchor'
        (_parent_anchor if parent else _native_anchor)(staged,create=True)
        staged.execute('INSERT INTO '+table+' VALUES(1,?,?,?,?)',(original['request_id'],original['parent_authority_id']if parent else original['native_nonce'],original['initial_json'],original['state']))
    from .parent_restore_assets import preserve_original_live,GENERATION,MEMBER
    if GENERATION in pub._tables(live)or MEMBER in pub._tables(live):
        if live_database is None or staged_database is None or getattr(staged_database,'_restore_work_capability',None)is None:
            pub._need(not live.execute('SELECT 1 FROM restore_asset_generation LIMIT 1').fetchone()
                and not staged.execute('SELECT 1 FROM restore_asset_generation LIMIT 1').fetchone(),_CODE)
        else:preserve_original_live(live_database,staged_database,live,staged)

@contextmanager
def _owned_parent(owner,value,path):
    """Hold the original registered directory through enumeration/deletion.

    DirEntry is a name enumerator only. Its Windows cached link/ID fields are
    not native authority. POSIX unlink uses this original dirfd; Windows keeps
    each original parent HANDLE without delete sharing through disposition.
    """
    pub._owner(owner);owner.parent.check()
    if path==owner.path.parent:
        yield owner.parent
        owner.parent.check();return
    expected={owner.path.parent/e['relative']:e for e in value['members']['entries']if e['kind']=='directory'and not e['removed']}
    pub._need(path in expected,_CODE)
    manager=pub._Parent(path)
    try:parent=manager.__enter__()
    except BaseException:
        owner.gate.poisoned=True;owner.gate.retained.append(('restore_work_parent',manager));raise
    try:
        if os.name=='nt':
            chain={Path(name):list(identity)for handle,name,identity in parent.chain}
        else:
            chain={};prefix=Path('/')
            for previous,name,fd,identity in parent.chain:
                prefix=prefix/name;chain[prefix]=list(identity)
                if prefix in expected:
                    info=os.fstat(fd);pub._need(info.st_uid==os.getuid()and info.st_mode&0o077==0,_CODE)
        for directory,entry in expected.items():
            if directory==path or directory in path.parents:
                pub._need(chain.get(directory)==entry['identity'],_CODE)
        parent.check();yield parent;parent.check()
    finally:
        try:manager.__exit__(None,None,None)
        except BaseException:
            owner.gate.poisoned=True;owner.gate.retained.append(('restore_work_parent',manager))
            raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None

def _inventory_file(owner,parent,path,expected=None):
    parent.check()
    identity=pub._database_identity(path,parent,owner.gate)
    pub._need(identity is not None and(expected is None or list(identity)==expected),_CODE)
    parent.check()

def _inventory(owner,value):
    entries=value['members']['entries'];known={e['relative']:e for e in entries};allowed=set(known)
    for entry in entries:
        if entry['family']:allowed.update(entry['relative']+suffix for suffix in('-wal','-shm','-journal'))
    root=owner.path.parent/value['binding']['work_name'];seen=[]
    def walk(path):
        with _owned_parent(owner,value,path)as parent:
            # No DirEntry.stat/is_dir/is_symlink supplies type/link authority.
            with os.scandir(path if os.name=='nt'else parent.fd)as children:
                for child in children:
                    child_path=path/child.name;relative=child_path.relative_to(owner.path.parent).as_posix()
                    pub._need(relative in allowed,_CODE);entry=known.get(relative)
                    seen.append(relative);pub._need(len(seen)<=MAX_MEMBERS*4+2*4+1,_CODE)
                    if entry is not None and entry['kind']=='directory':walk(child_path)
                    else:_inventory_file(owner,parent,child_path,entry['identity']if entry is not None else None)
    for entry in entries:
        if entry['removed']:pub._need(not os.path.lexists(owner.path.parent/entry['relative']),_CODE)
        else:_check_entry(owner,entry,entry['deleting'])
    if os.path.lexists(root):walk(root)
    prepared=value['binding']['prepared_name']
    for suffix in('-wal','-shm','-journal'):
        path=owner.path.parent/(prepared+suffix)
        if os.path.lexists(path):
            _inventory_file(owner,owner.parent,path);seen.append(prepared+suffix)
    return [name for name in seen if name not in known]

def cleanup(owner,metadata):
    value=current(owner,metadata)
    if value is None:return
    from .parent_restore_assets import assert_assets_closed_before_native,retire_unreferenced_original_generations,assert_original_media_base_closed
    assert_original_media_base_closed(owner,value)
    retire_unreferenced_original_generations(owner,metadata)
    assert_assets_closed_before_native(owner,metadata)
    aux=_inventory(owner,value)
    # Every original SQL/source/native handle has already left its scope here.
    # Aux deletion authority is the exact original family, not a freshly read ID.
    for relative in aux:
        _inventory(owner,value)
        path=owner.path.parent/relative
        with _owned_parent(owner,value,path.parent)as parent:
            _inventory_file(owner,parent,path)
            if os.name=='nt':
                from .parent_restore_owned_delete import erase_windows
                erase_windows(owner,path,directory=False,parent=parent)
            else:os.unlink(path.name,dir_fd=parent.fd)
            pub._need(not os.path.lexists(path),_CODE);parent.flush()
        pub._fault('during_owned_cleanup')
    ordered=sorted(value['members']['entries'],key=lambda e:(e['kind']=='directory',-len(e['relative'].split('/'))))
    for entry in ordered:
        if entry['removed']:continue
        _inventory(owner,value);exists=_check_entry(owner,entry,entry['deleting'])
        if not entry['deleting']:entry['deleting']=True;_save(owner,value)
        if exists:
            path=owner.path.parent/entry['relative']
            with _owned_parent(owner,value,path.parent)as parent:
                _check_entry(owner,entry)
                directory=entry['kind']=='directory'
                if directory:pub._need(not list(os.scandir(path)),_CODE)
                if os.name=='nt':
                    from .parent_restore_owned_delete import erase_windows
                    erase_windows(owner,path,directory=directory,identity=entry['identity'],parent=parent)
                elif directory:os.rmdir(path.name,dir_fd=parent.fd)
                else:os.unlink(path.name,dir_fd=parent.fd)
                pub._need(not os.path.lexists(path),_CODE);parent.flush()
        pub._fault('after_owned_object_unlink');entry['removed']=True;_save(owner,value);pub._fault('during_owned_cleanup')
    value['state']='CLEANED';_save(owner,value);pub._fault('after_owned_cleanup')

def finish(owner,metadata):
    value=current(owner,metadata)
    if value is None:return
    pub._need(value['state']=='CLEANED'and all(e['removed']for e in value['members']['entries']),_CODE);_inventory(owner,value)
    row,generation,permit,original=_facts(owner,value)
    pub._need((row['state']=='finished'and row['finalized']==1 and pub._authority_status(permit)=='finished')or(row['state']=='failed'and row['finalized']==0 and pub._authority_status(permit)=='failed'),_CODE)
    value['state']='DONE';_save(owner,value);_anchor_commit(owner,value)
    # Native is already terminal and every original object absent. An interrupted
    # archive resumes from DONE plus those same facts; it never republishes.
    active=owner.path.parent/_name(owner);last=active.with_name(active.name+'.last')
    if os.path.lexists(last):
        from .registered_asset_snapshot import HeldFile,Policy
        with HeldFile(last.parent,last.name,Policy(1,64,4096,pub.MAX_JOURNAL,pub.MAX_JOURNAL,pub.MAX_JOURNAL))as held:
            previous=_decode(pub._json_object(b''.join(held.chunks()),pub.MAX_JOURNAL,_CODE));pub._need(previous['state']=='DONE',_CODE)
    identity=_ledger_identity(owner,active);pub._need(identity==owner.gate.work_ledger_identity,_CODE)
    os.replace(active,last);owner.parent.flush();pub._need(_ledger_identity(owner,last)==identity,_CODE);owner.gate.work_ledger_identity=None;owner.gate.work_anchor_active=False
    pub._fault('after_owned_ledger_archive')

def staged_capability(database):
    value=getattr(database,'_restore_work_capability',None)
    if value is not None:pub._need(type(value)is WorkCapability,_CODE);value._value()
    return value
