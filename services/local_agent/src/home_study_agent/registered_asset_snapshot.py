"""Fixed-registry, bounded asset staging only. Never a restore or live publisher.

The caller supplies its original database path and an owned staging parent.
No archive path/caller root dictionary can authorize another source root.
Dependencies: the existing pet_media_files Windows handle adapter, unchanged.
"""
from __future__ import annotations
from .backup_local_io import io_path, io_resolve, io_walk, io_glob, io_mkdtemp, io_temporary_directory

from contextlib import contextmanager, ExitStack, closing
from dataclasses import dataclass, asdict
import hashlib
import errno
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata

from home_study_agent.pet_media_files import _Win32, _windows_parts


class AssetSnapshotError(ValueError):
    """Finite codes only; never expose source paths or SQL error text."""


def need(value, code):
    if not value:
        raise AssetSnapshotError(code)


@dataclass(frozen=True)
class Policy:
    max_files: int
    max_depth: int
    max_path_utf8: int
    max_manifest_bytes: int
    max_total_bytes: int  # Registered payload only, not a physical staging quota.
    max_file_bytes: int

    def validate(self):
        for field in self.__dataclass_fields__:
            value = getattr(self, field)
            need(type(value) is int and 0 < value <= 2**53 - 1, 'INVALID_POLICY')
        need(self.max_files <= 100000 and self.max_depth <= 64
             and self.max_path_utf8 <= 1024 and self.max_manifest_bytes <= 64*1024*1024
             and self.max_file_bytes <= self.max_total_bytes, 'INVALID_POLICY')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def record_key(kind, identity):
    return digest(canonical([kind, str(identity)]))


def safe_relative(value, policy):
    need(type(value) is str and value and '\\' not in value and ':' not in value
         and not value.startswith('/') and unicodedata.normalize('NFC', value) == value,
         'UNSAFE_ASSET_PATH')
    parts = value.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
    reserved.update(prefix+suffix for prefix in ('COM','LPT')
                    for suffix in (*'123456789', '¹','²','³'))
    need(len(parts) <= policy.max_depth and len(value.encode('utf-8')) <= policy.max_path_utf8,
         'ASSET_PATH_BOUND')
    need(all(p and p not in {'.','..'} and not p.endswith((' ', '.'))
             and not any(ord(c)<32 or c in '<>"|?*' for c in p)
             and p.split('.')[0].upper() not in reserved for p in parts), 'UNSAFE_ASSET_PATH')
    return parts


def local_absolute(value, policy):
    need(type(value) is str and value and '\x00' not in value, 'MISSING_REGISTERED_ROOT')
    if os.name == 'nt':
        parts = _windows_parts(value)
        need(len(parts)>1, 'UNSAFE_SOURCE_ROOT')
        safe_relative('/'.join(parts[1:]), policy)
        return Path(str(PureWindowsPath(*parts)))
    need(value.startswith('/') and not value.startswith('//') and '\\' not in value,
         'UNSAFE_SOURCE_ROOT')
    safe_relative(value[1:], policy)
    path = Path(value)
    need(path != Path('/') and str(path) == value, 'UNSAFE_SOURCE_ROOT')
    return path


def _signature(s):
    return (s.st_dev,s.st_ino,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)


class _CheckedWin32(_Win32):
    def close(self, handle):
        if not self.dll.CloseHandle(handle):
            raise AssetSnapshotError('HANDLE_CLOSE_UNCONFIRMED')


class HeldFile:
    """No follow/reopen fallback. Directory chain is retained and rechecked."""
    def __init__(self, root, relative, policy):
        self.policy=policy;self.root=local_absolute(str(root),policy)
        self.parts=safe_relative(relative,policy);self.stack=ExitStack();self.fd=None

    def __enter__(self):
        try:
            if os.name == 'nt':
                self.kernel=_CheckedWin32();parts=_windows_parts(str(self.root))+self.parts
                need(self.kernel.drive_type(parts[0]) in {2,3},'NONLOCAL_SOURCE_ROOT')
                self.win_handles=[]
                for i in range(len(parts)):
                    path=str(PureWindowsPath(*parts[:i+1]));directory=i<len(parts)-1
                    handle=self.kernel.open(path,directory=directory)
                    self.stack.callback(self.kernel.close,handle);self.win_handles.append((handle,path,directory))
                    self._win_check(handle,path,directory)
                self.handle=self.win_handles[-1][0];self.before=self.kernel.info(self.handle)
                need(self.before['links']==1 and self.before['size']<=self.policy.max_file_bytes,'ASSET_FILE_BOUND_OR_ALIAS')
            else:
                need(os.open in os.supports_dir_fd and hasattr(os,'O_NOFOLLOW'),'SAFE_HANDLES_UNAVAILABLE')
                current=os.open('/',os.O_RDONLY|os.O_DIRECTORY);self.stack.callback(os.close,current)
                self.dirs=[]
                for part in (*self.root.parts[1:],*self.parts[:-1]):
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
                    self.stack.callback(os.close,child);self.dirs.append((current,part,child,os.fstat(child)));current=child
                self.parent=current
                self.fd=os.open(self.parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=current)
                self.stack.callback(os.close,self.fd);self.before=os.fstat(self.fd)
                need(stat.S_ISREG(self.before.st_mode) and self.before.st_nlink==1
                     and self.before.st_size<=self.policy.max_file_bytes,'ASSET_FILE_BOUND_OR_ALIAS')
            return self
        except BaseException as error:
            self.stack.close()
            if isinstance(error,OSError) and error.errno in {errno.ELOOP,errno.ENOTDIR}:
                raise AssetSnapshotError('SOURCE_ALIAS_OR_REPLACED') from None
            raise

    def _win_check(self,handle,path,directory):
        info=self.kernel.info(handle);final=self.kernel.final_path(handle)
        if final.startswith('\\\\?\\'):final=final[4:]
        need(info['file_type']==1 and not info['attributes']&(0x400|0x1000)
             and bool(info['attributes']&0x10)==directory
             and final.rstrip('\\').casefold()==path.rstrip('\\').casefold(),'SOURCE_ALIAS_OR_REPLACED')

    def check(self):
        if os.name == 'nt':
            for args in self.win_handles:self._win_check(*args)
            need(self.kernel.info(self.handle)==self.before,'ASSET_CHANGED')
        else:
            for parent,name,child,before in self.dirs:
                now=os.stat(name,dir_fd=parent,follow_symlinks=False)
                need(stat.S_ISDIR(now.st_mode) and (now.st_dev,now.st_ino)==(before.st_dev,before.st_ino)
                     and (os.fstat(child).st_dev,os.fstat(child).st_ino)==(before.st_dev,before.st_ino),'SOURCE_ALIAS_OR_REPLACED')
            need(_signature(os.fstat(self.fd))==_signature(self.before)
                 and _signature(os.stat(self.parts[-1],dir_fd=self.parent,follow_symlinks=False))==_signature(self.before),
                 'ASSET_CHANGED')

    def chunks(self):
        self.check()
        if os.name == 'nt':
            # Rewind on the held original handle, never a second path open.
            import ctypes
            fn=self.kernel.dll.SetFilePointerEx
            fn.argtypes=[ctypes.c_void_p,ctypes.c_int64,ctypes.c_void_p,ctypes.c_uint32];fn.restype=ctypes.c_int
            need(fn(self.handle,0,None,0),'SOURCE_READ_FAILED')
            while True:
                b=self.kernel.ct.create_string_buffer(65536);n=self.kernel.ct.c_uint32()
                need(self.kernel.dll.ReadFile(self.handle,b,len(b),self.kernel.ct.byref(n),None),'SOURCE_READ_FAILED')
                if not n.value:break
                yield b.raw[:n.value]
        else:
            os.lseek(self.fd,0,os.SEEK_SET)
            while True:
                block=os.read(self.fd,65536)
                if not block:break
                yield block
        self.check()

    def hash(self):
        h=hashlib.sha256();size=0
        for block in self.chunks():
            size+=len(block);need(size<=self.policy.max_file_bytes,'ASSET_FILE_BOUND_OR_ALIAS');h.update(block)
        expected=self.before['size'] if os.name=='nt' else self.before.st_size
        need(size==expected,'ASSET_CHANGED');return h.hexdigest(),size

    def verified_length(self):
        self.check()
        return self.before['size'] if os.name=='nt' else self.before.st_size

    def copy(self,target):
        before=self.hash();h=hashlib.sha256();size=0
        with io_path(target).open('xb') as out:
            for block in self.chunks():out.write(block);h.update(block);size+=len(block)
            out.flush();os.fsync(out.fileno())
        need(before==(h.hexdigest(),size)==self.hash(),'ASSET_CHANGED')
        return before

    def __exit__(self,*args):
        self.stack.close()


@contextmanager
def _open_readonly(path):
    if os.name == 'nt':
        from urllib.parse import quote
        from home_study_agent.backup_local_io import _windows_io_name
        uri = 'file:' + quote(_windows_io_name(path), safe='') + '?mode=ro&vfs=win32-longpath'
    else:
        uri = path.as_uri() + '?mode=ro'
    c = sqlite3.connect(uri, uri=True, timeout=0)
    try:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
        yield c
    finally:c.close()

def _sqlite_snapshot(path, target, scratch, policy, *, payload_remaining=None):
    """Stabilize held DB+WAL/SHM, then sqlite backup from owned RO copy.

    A concurrent source writer/checkpoint makes this fail closed. Source SQLite
    never opens paths outside the held set; only the private copy is interpreted.
    """
    if payload_remaining is not None:
        need(type(payload_remaining) is int and 0<=payload_remaining<=policy.max_total_bytes,'INVALID_PAYLOAD_REMAINING')
        need(payload_remaining>0,'ASSET_TOTAL_BOUND')
    path=local_absolute(str(path),policy)
    with io_temporary_directory(dir=scratch,prefix='sqlite-') as temp, ExitStack() as stack:
        work=Path(temp);held=[];absent=[];total=0
        for suffix in ('','-wal','-shm','-journal'):
            p=Path(str(path)+suffix)
            try:f=stack.enter_context(HeldFile(p.parent,p.name,policy))
            except FileNotFoundError:
                need(bool(suffix),'MISSING_REGISTERED_FILE');absent.append(p);continue
            need(suffix!='-journal','SQLITE_ROLLBACK_JOURNAL_PRESENT')
            held.append((suffix,f))
        need(sum(f.verified_length() for _,f in held)<=policy.max_total_bytes,'ASSET_TOTAL_BOUND')
        initial={suffix:f.hash() for suffix,f in held}
        for suffix,f in held:
            total+=initial[suffix][1];need(total<=policy.max_total_bytes,'ASSET_TOTAL_BOUND')
            f.copy(work/('source.db'+suffix))
        for suffix,f in held:need(initial[suffix]==f.hash(),'ASSET_CHANGED')
        need(all(not os.path.lexists(io_path(p)) for p in absent),'ASSET_CHANGED')
        with _open_readonly(work/'source.db') as source:
            page_size=source.execute('PRAGMA page_size').fetchone()[0]
            page_count=source.execute('PRAGMA page_count').fetchone()[0]
            need(page_count*page_size<=policy.max_file_bytes,'ASSET_FILE_BOUND_OR_ALIAS')
            if payload_remaining is not None:need(page_count*page_size<=payload_remaining,'ASSET_TOTAL_BOUND')
            need(source.execute('PRAGMA integrity_check').fetchone()[0]=='ok','SQLITE_INTEGRITY_REQUIRED')
            io_path(target.parent).mkdir(parents=True,exist_ok=True)
            if os.name == 'nt':
                from urllib.parse import quote
                from .backup_local_io import _windows_io_name
                destination_uri='file:'+quote(_windows_io_name(target),safe='')+'?mode=rwc&vfs=win32-longpath'
                dest=sqlite3.connect(destination_uri,uri=True)
            else:
                dest=sqlite3.connect(target)
            try:
                def progress(status,remaining,pages):
                    need(pages*page_size<=policy.max_file_bytes,'ASSET_FILE_BOUND_OR_ALIAS')
                    if payload_remaining is not None:need(pages*page_size<=payload_remaining,'ASSET_TOTAL_BOUND')
                source.backup(dest,pages=64,progress=progress,sleep=0)
                need(dest.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete','SQLITE_SINGLE_FILE_REQUIRED')
                need(dest.execute('PRAGMA integrity_check').fetchone()[0]=='ok','SQLITE_INTEGRITY_REQUIRED')
            finally:dest.close()
        for suffix,f in held:need(initial[suffix]==f.hash(),'ASSET_CHANGED')
        need(all(not os.path.lexists(io_path(p)) for p in absent),'ASSET_CHANGED')
    # Some SQLite builds leave an orphan SHM after a successful WAL->DELETE
    # transition. Only our owned destination is eligible: both connections are
    # closed, integrity passed, DELETE was acknowledged and header modes are1/1.
    # Never remove an original source sidecar or discard a nonempty WAL/journal.
    with io_path(target).open('rb') as f:header=f.read(100)
    need(header[:16]==b'SQLite format 3\x00' and header[18:20]==b'\x01\x01','SQLITE_SINGLE_FILE_REQUIRED')
    for suffix in ('-wal','-journal','-shm'):
        side=Path(str(target)+suffix)
        if os.path.lexists(io_path(side)):
            with HeldFile(side.parent,side.name,policy) as f:_,size=f.hash()
            need(suffix=='-shm' or size==0,'SQLITE_SINGLE_FILE_REQUIRED');io_path(side).unlink()
    need(io_path(target).stat().st_size<=policy.max_file_bytes,'ASSET_FILE_BOUND_OR_ALIAS')
    if payload_remaining is not None:need(io_path(target).stat().st_size<=payload_remaining,'ASSET_TOTAL_BOUND')


def _rows(c,table,columns):
    # All identifiers originate in constants in this module, never input SQL.
    kind=c.execute('SELECT type FROM sqlite_master WHERE name=?',(table,)).fetchone()
    if kind is None:return []
    need(kind[0]=='table','REGISTRY_SCHEMA_REQUIRED')
    available={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
    need(set(columns)<=available,'REGISTRY_SCHEMA_REQUIRED')
    return c.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY '+columns[0])


def _columns(c,table):
    return {r[1] for r in c.execute('PRAGMA table_info('+table+')')}


FINGERPRINT_WITNESS = 'registered_fingerprint_verified_v1'


def parse_registered_fingerprint(value):
    need(type(value) is str and len(value)<=96,'REGISTERED_SOURCE_FINGERPRINT_REQUIRED')
    match=re.fullmatch(r'size:(0|[1-9][0-9]*):mtime_ns:(-?(?:0|[1-9][0-9]*))',value)
    need(match,'REGISTERED_SOURCE_FINGERPRINT_REQUIRED')
    size,mtime=map(int,match.groups())
    need(value==f'size:{size}:mtime_ns:{mtime}','REGISTERED_SOURCE_FINGERPRINT_REQUIRED')
    return size,mtime


def held_registered_fingerprint(held):
    held.check()
    if os.name=='nt':
        size=held.before['size'];mtime=(held.before['modified']-116444736000000000)*100
    else:size=held.before.st_size;mtime=held.before.st_mtime_ns
    return f'size:{size}:mtime_ns:{mtime}'


def registered_fingerprint_witness(role,fingerprint,sha256):
    need(role in {'pdf','image','video'},'REGISTERED_SOURCE_FINGERPRINT_REQUIRED')
    parse_registered_fingerprint(fingerprint)
    need(type(sha256) is str and re.fullmatch('[a-f0-9]{64}',sha256),'REGISTERED_SOURCE_FINGERPRINT_REQUIRED')
    return [FINGERPRINT_WITNESS,role,fingerprint,sha256]


class _Collector:
    def __init__(self,stage,scratch,policy,*,provenance_ticket=None,provenance_snapshot=None):
        self.stage=stage;self.scratch=scratch;self.policy=policy;self.assets={};self.coverage=[];self.total=0;self.folded={};self.roots={};self.managed_media=set();self.visited=0
        self.provenance_ticket=provenance_ticket;self.provenance_snapshot=provenance_snapshot

    def root(self,value,*,directory_source=False):
        path=local_absolute(value,self.policy)
        need(not directory_source or (path!=self.stage and path not in self.stage.parents and self.stage not in path.parents),
             'REGISTERED_ROOT_STAGE_OVERLAP')
        identity=_check_directory(path,self.policy)
        need(str(path) not in self.roots or self.roots[str(path)]==identity,'SOURCE_ALIAS_OR_REPLACED')
        self.roots[str(path)]=identity;return path

    def rows(self,rows):
        for number,row in enumerate(rows,1):
            need(number<=self.policy.max_files,'ASSET_REFERENCE_BOUND');yield row

    def note(self,kind,key,columns,storage='registered_local'):
        need(len(self.coverage)<self.policy.max_files,'ASSET_REFERENCE_BOUND')
        self.coverage.append({'kind':kind,'record_sha256':record_key(kind,key),'storage':storage,'registry_columns':sorted(columns)})

    def add(self,kind,row,role,root,relative,*,sqlite=False,version=None,expected_sha=None):
        need(kind in {'curriculum','kg','standard'} and re.fullmatch('[a-z_]+',role),'REGISTRY_SCHEMA_REQUIRED')
        root=self.root(root);parts=safe_relative(relative,self.policy)
        need(str(root/relative).casefold() not in self.managed_media,'MANAGED_MEDIA_OWNED_BY_LIFECYCLE')
        name='registered/'+kind+'/'+record_key(kind,row)+'/'+role+'/'+'/'.join(parts)
        safe_relative(name,self.policy);folded=name.casefold()
        need(folded not in self.folded or self.folded[folded]==name,'ASSET_CASE_COLLISION')
        self.folded[folded]=name
        if name in self.assets:
            need(self.assets[name][1]==(str(root),relative,sqlite),'ASSET_LOGICAL_COLLISION')
            versions=self.assets[name][0]['version_sha256s']
            value=digest(canonical(version))
            if value not in versions:versions.append(value);versions.sort()
            need(len(versions)<=self.policy.max_files,'ASSET_REFERENCE_BOUND')
            if expected_sha is not None:need(self.assets[name][0]['sha256']==expected_sha,'REGISTERED_SOURCE_HASH_MISMATCH')
            return self.stage/name
        need(len(self.assets)<self.policy.max_files,'ASSET_COUNT_BOUND')
        out=self.stage/name;remaining=self.policy.max_total_bytes-self.total
        if sqlite:
            _sqlite_snapshot(root/relative,out,self.scratch,self.policy,payload_remaining=remaining)
            with HeldFile(out.parent,out.name,self.policy) as f:h,size=f.hash()
        else:
            with HeldFile(root,relative,self.policy) as f:
                need(f.verified_length()<=remaining,'ASSET_TOTAL_BOUND')
                io_path(out.parent).mkdir(parents=True,exist_ok=True);h,size=f.copy(out)
        if expected_sha is not None:
            need(type(expected_sha) is str and re.fullmatch('[a-f0-9]{64}',expected_sha)
                 and h==expected_sha,'REGISTERED_SOURCE_HASH_MISMATCH')
        self.total+=size;need(self.total<=self.policy.max_total_bytes,'ASSET_TOTAL_BOUND')
        item={'logical_name':name,'kind':kind,'record_sha256':record_key(kind,row),'role':role,
              'relative_path':relative,'bytes':size,'sha256':h,'storage':'sqlite_backup' if sqlite else 'regular',
              'version_sha256s':[digest(canonical(version))]}
        self.assets[name]=(item,(str(root),relative,sqlite));return out

    def database(self,kind,row,role,path,version=None):
        p=local_absolute(path,self.policy)
        return self.add(kind,row,role,str(p.parent),p.name,sqlite=True,version=version)

    def fingerprinted(self,kind,key,role,root,relative,fingerprint,version,expected_sha=None):
        parse_registered_fingerprint(fingerprint)
        with HeldFile(root,relative,self.policy) as held:
            need(held_registered_fingerprint(held)==fingerprint,'REGISTERED_SOURCE_FINGERPRINT_MISMATCH')
            sha,size=held.hash()
            if expected_sha is not None:need(sha==expected_sha,'REGISTERED_SOURCE_HASH_MISMATCH')
            self.add(kind,key,role,root,relative,version=version,expected_sha=sha)
            self.add(kind,key,role,root,relative,
                     version=registered_fingerprint_witness(role,fingerprint,sha),expected_sha=sha)
            held.check()

    def pending(self,kind,key,role,root,relative,version):
        # Existing projections retain their original hash and version contract.
        parts=safe_relative(relative,self.policy)
        name='registered/'+kind+'/'+record_key(kind,key)+'/'+role+'/'+'/'.join(parts)
        if name in self.assets:
            need(self.assets[name][1]==(str(root),relative,False),'ASSET_LOGICAL_COLLISION')
            return
        if str(Path(root)/relative).casefold() in self.managed_media:return
        self.add(kind,key,role,str(root),relative,version=version)

    def tree(self,kind,key,role,root,version,*,hidden_video=False):
        root=self.root(root,directory_source=True)
        with closing(_registered_files(root,self.policy,self)) as files:
            for relative in files:
                parts=relative.split('/');suffix=PurePosixPath(relative).suffix
                selected=(suffix.lower() in {'.jpg','.jpeg','.png','.webp','.heic'} if role=='image'
                          else (suffix.lower() if os.name=='nt' else suffix)==('.txt' if role=='transcript' else '.mp4'))
                if selected and not (hidden_video and any(p.startswith('.') for p in parts)):
                    self.pending(kind,key,role,root,relative,version)

    def inventory(self,c):
        for table,column in [('voice_input','local_path'),('capture_artifact','local_path'),('kg_parent_textbook_ocr_artifact','raw_image_path')]:
            if column in _columns(c,table):
                for row in self.rows(_rows(c,table,[column])):
                    if isinstance(row[column],str):self.managed_media.add(row[column].casefold())
        for row in self.rows(_rows(c,'curriculum_corpus',['corpus_id','source_database_path','transcript_root_path','video_root_path','source_hash'])):
            key=row['corpus_id'];self.root(row['transcript_root_path'],directory_source=True);self.root(row['video_root_path'],directory_source=True)
            self.note('curriculum',key,['source_database_path','transcript_root_path','video_root_path'])
            snap=self.database('curriculum',key,'metadata',row['source_database_path'],row['source_hash'])
            for r in self.rows(c.execute('SELECT transcript_relative_path,video_relative_path,local_video_available FROM curriculum_resource WHERE corpus_id=? ORDER BY row_id',(key,))):
                need(not r['local_video_available'] or r['video_relative_path'],'MISSING_REGISTERED_FILE')
                for column,role,root in [('transcript_relative_path','transcript',row['transcript_root_path']),('video_relative_path','video',row['video_root_path'])]:
                    if r[column]:self.add('curriculum',key,role,root,r[column],version=row['source_hash'])
            # Read only the stabilized original source DB, never the live DB.
            # Legacy source schemas without resources keep their projected refs.
            with _open_readonly(snap) as source:
                for resource in self.rows(_rows(source,'resources',['resource_id'])):
                    for role,base,suffix in [('transcript',row['transcript_root_path'],'.txt'),('video',row['video_root_path'],'.mp4')]:
                        relative=str(resource['resource_id'])+suffix
                        if _optional_registered_file(Path(base),relative,self.policy):
                            self.pending('curriculum',key,role,base,relative,row['source_hash'])
        for row in self.rows(_rows(c,'kg_corpus',['corpus_id','source_kind','transcript_root','video_root','metadata_db_path','version_label'])):
            key=row['corpus_id'];root=row['transcript_root'];version=row['version_label']
            extra={name:None for name in ['pdf_root','image_root']}
            for name in extra:
                if name in _columns(c,'kg_corpus'):extra[name]=c.execute('SELECT '+name+' FROM kg_corpus WHERE corpus_id=?',(key,)).fetchone()[0]
            if root in {'bundle:'+key,'local://family-textbook-ocr-pages'}:
                need(not row['video_root'] and not row['metadata_db_path'] and not any(extra.values()),'VIRTUAL_REGISTRY_CONFLICT')
                for r in self.rows(c.execute('SELECT resource_id,video_relative_path FROM kg_resource WHERE corpus_id=?',(key,))):
                    need(not r['video_relative_path'] and all(not x['image_relative_path'] for x in self.rows(_rows_for_resource(c,r['resource_id']))),'VIRTUAL_REGISTRY_CONFLICT')
                self.note('kg',key,['transcript_root'],'existing_inline_or_bundle');continue
            self.root(root,directory_source=True)
            columns=['transcript_root']
            for name in ['video_root','pdf_root','image_root']:
                value=extra.get(name) if name in extra else row[name]
                if value:self.root(value,directory_source=True);columns.append(name)
            if row['metadata_db_path']:columns.append('metadata_db_path')
            self.note('kg',key,columns)
            video_fp='video_fingerprint' if 'video_fingerprint' in _columns(c,'kg_resource') else 'NULL AS video_fingerprint'
            for r in self.rows(c.execute('SELECT resource_id,transcript_relative_path,video_relative_path,transcript_sha256,video_sha256,'+video_fp+' FROM kg_resource WHERE corpus_id=? ORDER BY resource_id',(key,))):
                for column,role,base in [('transcript_relative_path','transcript',root),('video_relative_path','video',row['video_root'])]:
                    if r[column]:
                        source_sha=r['transcript_sha256'] if role=='transcript' else r['video_sha256']
                        source_version=[version,r['transcript_sha256'],r['video_sha256']]
                        if role=='video' and r['video_fingerprint'] is not None:
                            self.fingerprinted('kg',key,role,base,r[column],r['video_fingerprint'],source_version,source_sha)
                        else:self.add('kg',key,role,base,r[column],version=source_version,expected_sha=source_sha)
                for page in self.rows(_rows_for_resource(c,r['resource_id'])):
                    if page['image_relative_path']:
                        need(extra['image_root'],'MISSING_REGISTERED_IMAGE_ROOT')
                        if page['image_fingerprint'] is not None:
                            self.fingerprinted('kg',key,'image',extra['image_root'],page['image_relative_path'],page['image_fingerprint'],page['image_sha256'],page['image_sha256'])
                        else:self.add('kg',key,'image',extra['image_root'],page['image_relative_path'],version=page['image_sha256'],expected_sha=page['image_sha256'])
            if row['metadata_db_path']:
                snap=self.database('kg',key,'metadata',row['metadata_db_path'],version)
                with _open_readonly(snap) as metadata:
                    need(not _columns(metadata,'textbook_book') or 'fingerprint' in _columns(metadata,'textbook_book'),'REGISTERED_PDF_FINGERPRINT_REQUIRED')
                    books=_rows(metadata,'textbook_book',['book_id','relative_pdf_path','fingerprint'])
                    for book in self.rows(books):
                        need(extra['pdf_root'],'MISSING_REGISTERED_PDF_ROOT')
                        self.fingerprinted('kg',key,'pdf',extra['pdf_root'],book['relative_pdf_path'],book['fingerprint'],version)
                    for page in self.rows(_rows(metadata,'textbook_page',['page_id','ocr_text_relative_path'])):
                        if page['ocr_text_relative_path']:self.add('kg',key,'transcript',root,page['ocr_text_relative_path'],version=version)
            self.tree('kg',key,'transcript',root,version)
            if row['video_root']:self.tree('kg',key,'video',row['video_root'],version,hidden_video=row['source_kind']=='bilibili')
            if extra['image_root']:self.tree('kg',key,'image',extra['image_root'],version)
            # Only the same original admitted export ticket can authorize an
            # immutable restore-generation provenance member. Merely naming a
            # JSON file at a registered root supplies no authority.
            if self.provenance_ticket is not None:
                from .registered_fingerprint_relocation import NAME as FP_NAME,ROLE as FP_ROLE,authorize_export as authorize_fp_export
                authorized=authorize_fp_export(self.provenance_ticket,self.provenance_snapshot,key,root)
                if authorized is not None:
                    sha,size=authorized
                    copied=self.add('kg',key,FP_ROLE,root,FP_NAME,expected_sha=sha)
                    need(io_path(copied).stat().st_size==size,'STAGE_ASSET_MISMATCH')
        for row in self.rows(_rows(c,'curriculum_standard_source',['source_id','source_key','local_root_path','pdf_filename','pages_filename','pdf_sha256','text_sha256'])):
            key=row['source_id'];root=row['local_root_path']
            self.note('standard',key,['local_root_path'])
            manifest_path=self.add('standard',key,'manifest',root,'manifest.json')
            need(io_path(manifest_path).stat().st_size<=self.policy.max_manifest_bytes,'ASSET_MANIFEST_BOUND')
            manifest=json.loads(io_path(manifest_path).read_bytes());matches=[x for x in manifest.get('sources',[]) if x.get('source_key')==row['source_key']]
            need(manifest.get('format')=='hsc_official_curriculum_sources_v1' and len(matches)==1,'STANDARD_MANIFEST_SCOPE_REQUIRED')
            source=matches[0]
            need(source.get('pdf_filename')==row['pdf_filename']
                 and (source.get('ocr_pages_filename') or source.get('pages_filename'))==row['pages_filename'],
                 'STANDARD_MANIFEST_SCOPE_REQUIRED')
            for field,role in [('pdf_filename','pdf'),('pages_filename','ocr_pages')]:
                if row[field]:self.add('standard',key,role,root,row[field],version=[row['pdf_sha256'],row['text_sha256']],
                                       expected_sha=row['pdf_sha256'] if role=='pdf' else None)
            for field,role in [('content_filename','content'),('ocr_text_filename','ocr_text'),('ocr_pages_filename','ocr_pages')]:
                if source.get(field):self.add('standard',key,role,root,source[field],version=[row['pdf_sha256'],row['text_sha256']])
            from .standard_relocation_provenance import NAME,ROLE,reserved_present,authorize_export
            if reserved_present(Path(root),self.policy):
                authorized=authorize_export(self.provenance_ticket,self.provenance_snapshot,key,root)
                if authorized is not None:
                    sha,size=authorized
                    copied=self.add('standard',key,ROLE,root,NAME,expected_sha=sha)
                    need(io_path(copied).stat().st_size==size,'STAGE_ASSET_MISMATCH')


def _rows_for_resource(c,key):
    if not _columns(c,'kg_document_page'):return []
    need(c.execute("SELECT type FROM sqlite_master WHERE name='kg_document_page'").fetchone()[0]=='table','REGISTRY_SCHEMA_REQUIRED')
    image_fp='image_fingerprint' if 'image_fingerprint' in _columns(c,'kg_document_page') else 'NULL AS image_fingerprint'
    return c.execute('SELECT image_relative_path,image_sha256,'+image_fp+' FROM kg_document_page WHERE resource_id=? ORDER BY page_id',(key,))


def create_registered_asset_snapshot(database_path,owned_stage_parent,policy,*,provenance_ticket=None):
    """Return fresh private stage path; failure leaves no completion manifest.

    Caller retains authority/quiescence, disk preflight and full archive handling.
    This function never mutates a source DB, registry, media key or live root.
    """
    policy.validate();parent=local_absolute(str(owned_stage_parent),policy)
    need(io_path(parent).is_dir() and not io_path(parent).is_symlink(),'OWNED_STAGE_PARENT_REQUIRED')
    # The parent is an original caller-owned private directory, never supplied
    # by a backup. Validate its full chain before the first filesystem write.
    _check_stage_parent(parent,policy)
    stage=None
    try:
        stage=Path(io_mkdtemp(prefix='registered-assets-',dir=parent));scratch=stage/'.working';io_path(scratch).mkdir(mode=0o700)
        source=scratch/'registry.db';_sqlite_snapshot(database_path,source,scratch,policy)
        collector=_Collector(stage,scratch,policy,provenance_ticket=provenance_ticket,provenance_snapshot=Path(database_path))
        with _open_readonly(source) as c:collector.inventory(c)
        for path,identity in collector.roots.items():need(_check_directory(Path(path),policy)==identity,'SOURCE_ALIAS_OR_REPLACED')
        shutil.rmtree(io_path(scratch))
        manifest={'schema':1,'format':'registered_asset_snapshot_v1','policy':asdict(policy),
                  'assets':[collector.assets[n][0] for n in sorted(collector.assets)],
                  'coverage':sorted(collector.coverage,key=lambda x:(x['kind'],x['record_sha256']))}
        raw=canonical(manifest);need(len(raw)<=policy.max_manifest_bytes,'ASSET_MANIFEST_BOUND')
        with (io_path(stage/'manifest.json')).open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        verify_registered_asset_snapshot(stage,policy)
        return stage
    except Exception as error:
        if stage is not None:
            try:shutil.rmtree(io_path(stage))
            except Exception:
                failure=AssetSnapshotError('STAGE_CLEANUP_UNCONFIRMED')
                failure.owned_stage=stage  # Caller-private cleanup responsibility, never a source root.
                raise failure from None
        if isinstance(error,AssetSnapshotError):raise
        if isinstance(error,FileNotFoundError):raise AssetSnapshotError('MISSING_REGISTERED_FILE') from None
        raise AssetSnapshotError('REGISTERED_ASSET_SNAPSHOT_FAILED') from None


def _check_stage_parent(parent,policy):
    _check_directory(parent,policy,owned=True)


def _check_directory(parent,policy,owned=False):
    try:return _directory_identity(parent,policy,owned)
    except OSError as error:
        if error.errno in {errno.ELOOP,errno.ENOTDIR}:raise AssetSnapshotError('SOURCE_ALIAS_OR_REPLACED') from None
        raise


def _directory_identity(parent,policy,owned=False):
    with _held_directory(parent,policy,owned) as directory:return directory.identity


class _DirectoryLease:
    def check(self):
        if os.name=='nt':
            for handle,path,before in self.chain:
                info=self.kernel.info(handle)
                need(info['file_type']==1 and info['attributes']&0x10 and not info['attributes']&(0x400|0x1000)
                     and info['identity']==before['identity']
                     and self.kernel.final_path(handle).removeprefix('\\\\?\\').rstrip('\\').casefold()==path.rstrip('\\').casefold(),
                     'SOURCE_ALIAS_OR_REPLACED')
            need(self.kernel.info(self.handle)==self.before,'SOURCE_DIRECTORY_CHANGED')
        else:
            for parent,name,child,before in self.chain:
                now=os.stat(name,dir_fd=parent,follow_symlinks=False);held=os.fstat(child)
                need(stat.S_ISDIR(now.st_mode) and (now.st_dev,now.st_ino)==(before.st_dev,before.st_ino)
                     and (held.st_dev,held.st_ino)==(before.st_dev,before.st_ino),'SOURCE_ALIAS_OR_REPLACED')
            need(_signature(os.fstat(self.fd))==_signature(self.before),'SOURCE_DIRECTORY_CHANGED')


@contextmanager
def _held_directory(parent,policy,owned=False,ancestor=None):
    """Retain nofollow directory handles while streaming entries or testing a file.

    Windows uses the original FILE_SHARE_READ-only handles throughout scandir;
    POSIX enumeration uses the retained FD, not a reopened path.
    """
    with ExitStack() as stack:
        try:
            directory=_DirectoryLease();directory.path=parent;directory.chain=[] if ancestor is None else list(ancestor.chain)
            if ancestor is not None:
                need(parent.parent==ancestor.path,'SOURCE_ALIAS_OR_REPLACED');ancestor.check()
            if os.name=='nt':
                kernel=ancestor.kernel if ancestor is not None else _CheckedWin32();directory.kernel=kernel
                parts=_windows_parts(str(parent));need(kernel.drive_type(parts[0]) in {2,3},'NONLOCAL_SOURCE_ROOT')
                for i in range(len(parts)-1 if ancestor is not None else 0,len(parts)):
                    p=str(PureWindowsPath(*parts[:i+1]));handle=kernel.open(p,directory=True);stack.callback(kernel.close,handle)
                    info=kernel.info(handle)
                    need(info['file_type']==1 and info['attributes']&0x10 and not info['attributes']&(0x400|0x1000),'SOURCE_ALIAS_OR_REPLACED')
                    need(kernel.final_path(handle).removeprefix('\\\\?\\').rstrip('\\').casefold()==p.rstrip('\\').casefold(),'SOURCE_ALIAS_OR_REPLACED')
                    directory.chain.append((handle,p,info))
                directory.handle=handle;directory.before=info;directory.identity=info['identity']
            else:
                need(os.open in os.supports_dir_fd and hasattr(os,'O_NOFOLLOW') and os.scandir in os.supports_fd,'SAFE_HANDLES_UNAVAILABLE')
                if ancestor is None:
                    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);stack.callback(os.close,fd);parts=parent.parts[1:]
                else:fd=ancestor.fd;parts=(parent.name,)
                for part in parts:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);stack.callback(os.close,child)
                    directory.chain.append((fd,part,child,os.fstat(child)));fd=child
                s=os.fstat(fd)
                if owned:need(s.st_uid==os.getuid() and s.st_mode&0o077==0,'OWNED_PRIVATE_STAGE_PARENT_REQUIRED')
                directory.fd=fd;directory.before=s;directory.identity=(s.st_dev,s.st_ino)
            directory.check()
            yield directory
            directory.check()
        except OSError as error:
            if error.errno in {errno.ELOOP,errno.ENOTDIR}:raise AssetSnapshotError('SOURCE_ALIAS_OR_REPLACED') from None
            raise


def _registered_files(root,policy,collector):
    seen=set()
    def walk(directory,parts):
        need(directory.identity not in seen,'SOURCE_ALIAS_OR_REPLACED');seen.add(directory.identity)
        with os.scandir(io_path(directory.path if os.name=='nt' else directory.fd)) as entries:
            for entry in entries:
                collector.visited+=1
                need(collector.visited<=policy.max_files*(policy.max_depth+1),'ASSET_TREE_BOUND')
                relative='/'.join((*parts,entry.name));safe_relative(relative,policy)
                info=entry.stat(follow_symlinks=False)
                need(not stat.S_ISLNK(info.st_mode) and not getattr(info,'st_file_attributes',0)&(0x400|0x1000),'SOURCE_ALIAS_OR_REPLACED')
                directory.check()
                if stat.S_ISDIR(info.st_mode):
                    with _held_directory(directory.path/entry.name,policy,ancestor=directory) as child:
                        if os.name!='nt':need(child.identity==(info.st_dev,info.st_ino),'SOURCE_ALIAS_OR_REPLACED')
                        yield from walk(child,(*parts,entry.name))
                elif stat.S_ISREG(info.st_mode):yield relative
                directory.check()
    with _held_directory(root,policy) as directory:yield from walk(directory,())


def _optional_registered_file(root,relative,policy):
    parts=safe_relative(relative,policy)
    try:
        with _held_directory(root.joinpath(*parts[:-1]),policy) as directory:
            info=os.stat(io_path(directory.path/parts[-1]),follow_symlinks=False) if os.name=='nt' else os.stat(parts[-1],dir_fd=directory.fd,follow_symlinks=False)
            need(not stat.S_ISLNK(info.st_mode) and not getattr(info,'st_file_attributes',0)&(0x400|0x1000),'SOURCE_ALIAS_OR_REPLACED')
            return stat.S_ISREG(info.st_mode)
    except FileNotFoundError:return False


def verify_registered_asset_snapshot(stage,policy):
    try:return _verify_registered_asset_snapshot(stage,policy)
    except AssetSnapshotError:raise
    except Exception:raise AssetSnapshotError('STAGE_MANIFEST_REQUIRED') from None


def _validate_registered_manifest(raw,policy,read_member):
    """Pure bounded content contract; callback grants no filesystem rights."""
    policy.validate();need(type(raw)is bytes and len(raw)<=policy.max_manifest_bytes,'ASSET_MANIFEST_BOUND')
    m=json.loads(raw)
    need(set(m)=={'schema','format','policy','assets','coverage'} and m['schema']==1
         and m['format']=='registered_asset_snapshot_v1' and m['policy']==asdict(policy)
         and type(m['assets']) is list and len(m['assets'])<=policy.max_files
         and type(m['coverage']) is list and len(m['coverage'])<=policy.max_files,'STAGE_MANIFEST_REQUIRED')
    coverage=set()
    columns={'curriculum':{'source_database_path','transcript_root_path','video_root_path'},
             'kg':{'transcript_root','video_root','metadata_db_path','pdf_root','image_root'},'standard':{'local_root_path'}}
    for row in m['coverage']:
        need(set(row)=={'kind','record_sha256','storage','registry_columns'} and row['kind'] in columns
             and re.fullmatch('[a-f0-9]{64}',row['record_sha256']) and row['storage'] in {'registered_local','existing_inline_or_bundle'}
             and type(row['registry_columns']) is list and set(row['registry_columns'])<=columns[row['kind']]
             and row['registry_columns']==sorted(set(row['registry_columns'])),'STAGE_MANIFEST_REQUIRED')
        key=(row['kind'],row['record_sha256']);need(key not in coverage,'STAGE_MANIFEST_REQUIRED');coverage.add(key)
    names=set();folded=set();total=0
    for item in m['assets']:
        need(set(item)=={'logical_name','kind','record_sha256','role','relative_path','bytes','sha256','storage','version_sha256s'},'STAGE_MANIFEST_REQUIRED')
        name=item['logical_name'];safe_relative(name,policy)
        need(item['kind'] in {'curriculum','kg','standard'} and re.fullmatch('[a-f0-9]{64}',item['record_sha256'])
             and re.fullmatch('[a-z_]+',item['role']) and item['storage'] in {'regular','sqlite_backup'}
             and name=='registered/'+item['kind']+'/'+item['record_sha256']+'/'+item['role']+'/'+item['relative_path'], 'STAGE_MANIFEST_REQUIRED')
        need((item['kind'],item['record_sha256']) in coverage and type(item['version_sha256s']) is list
             and 0<len(item['version_sha256s'])<=policy.max_files
             and all(type(x) is str and re.fullmatch('[a-f0-9]{64}',x) for x in item['version_sha256s'])
             and item['version_sha256s']==sorted(set(item['version_sha256s'])),'STAGE_MANIFEST_REQUIRED')
        need(name not in names and name.casefold() not in folded,'ASSET_CASE_COLLISION');names.add(name);folded.add(name.casefold())
        actual,size=read_member(name)
        need(actual==item['sha256'] and type(item['bytes']) is int and size==item['bytes'],'STAGE_ASSET_MISMATCH')
        total+=size;need(total<=policy.max_total_bytes,'ASSET_TOTAL_BOUND')
    return m,names,total


def _verify_registered_asset_snapshot(stage,policy):
    """Local owned-stage roundtrip; does not restore paths or grant authority."""
    policy.validate();stage=local_absolute(str(stage),policy)
    with HeldFile(stage,'manifest.json',policy) as f:
        h,size=f.hash();need(size<=policy.max_manifest_bytes,'ASSET_MANIFEST_BOUND')
        raw=b''.join(f.chunks());need(digest(raw)==h,'ASSET_CHANGED')
    def read_member(name):
        with HeldFile(stage,name,policy)as original:return original.hash()
    m,names,total=_validate_registered_manifest(raw,policy,read_member)
    actual=set();actual_dirs=set();expected_dirs=set()
    for name in names:
        expected_dirs.update(str(p) for p in PurePosixPath(name).parents if str(p)!='.')
    for root,dirs,files in io_walk(stage,followlinks=False):
        for name in dirs+files:
            p=Path(root)/name;need(not io_path(p).is_symlink(),'SOURCE_ALIAS_OR_REPLACED')
            relative=p.relative_to(stage).as_posix();safe_relative(relative,policy)
        for name in files:actual.add((Path(root)/name).relative_to(stage).as_posix())
        for name in dirs:actual_dirs.add((Path(root)/name).relative_to(stage).as_posix())
        need(len(actual)<=policy.max_files+1,'ASSET_COUNT_BOUND')
    need(actual==names|{'manifest.json'} and actual_dirs==expected_dirs,'STAGE_EXTRA_OR_MISSING_FILE')
    return {'files':len(names),'total_bytes':total,'manifest_sha256':digest(raw)}

_PROVENANCE_COLLECTOR_CODE=_Collector.inventory.__code__
_PROVENANCE_SNAPSHOT_CODE=create_registered_asset_snapshot.__code__
