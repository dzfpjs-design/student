"""Owned backup containers, not a restore publisher or filesystem authority.

Registry enumeration is exclusively a88 on the caller's one closed SQLite
snapshot. HSC1 collection stays in PrivacyLifecycleService. No key is persisted
here; the caller supplies its existing data key and streaming cipher methods.
"""
from .backup_local_io import io_path, io_resolve, io_walk, io_glob, io_mkdtemp, io_temporary_directory
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tempfile
import zipfile

from .registered_asset_snapshot import (
    Policy, HeldFile, canonical, safe_relative, create_registered_asset_snapshot,
    verify_registered_asset_snapshot, _check_stage_parent,
)

MAGIC = b'HSCR1\0'
FORMAT = 'hsc_registered_capsule_v1'
FULL = 'hsc_backup_full_v2'
M = 64*1024*1024
MAX_FILES = 100000
CHUNK = 1024*1024
SAFE = 2**53-1


def need(value, code):
    if not value:
        raise ValueError(code)


def unique_json(raw):
    def pairs(rows):
        out = {}
        for k,v in rows:
            need(k not in out, 'BACKUP_DUPLICATE_FIELD')
            out[k] = v
        return out
    return json.loads(raw, object_pairs_hook=pairs)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    """Only finite manifests may be materialized; retain original held checks."""
    path=Path(path);raw=bytearray()
    with HeldFile(path.parent,path.name,Policy(MAX_FILES,64,1024,M,M,M)) as held:
        for chunk in held.chunks():
            raw.extend(chunk)
            need(len(raw)<=M,'BACKUP_MANIFEST_BOUND')
    return unique_json(raw)


def capsule_path(snapshot):
    return Path(snapshot).with_suffix('.registered.enc')


def normalize_owned_snapshot(snapshot):
    # Called only for our fresh, closed backup target after acknowledged DELETE.
    # SQLite can leave an orphan SHM; it is accounted here, never an original
    # registry sidecar. Nonempty WAL/journal is never discarded.
    with io_path(Path(snapshot)).open('rb') as source:
        h=source.read(100)
    need(h[:16]==b'SQLite format 3\0' and h[18:20]==b'\x01\x01',
         'BACKUP_SQLITE_SINGLE_FILE_REQUIRED')
    for suffix in ['-wal','-journal','-shm']:
        side=Path(str(snapshot)+suffix)
        if os.path.lexists(io_path(side)):
            value=record(side)
            need(suffix=='-shm' or value['size_bytes']==0,'BACKUP_UNACCOUNTED_SIDECAR')
            io_path(side).unlink()


def stage_policy(parent):
    # A finite caller budget derived from this actual volume, not a family-data
    # allowance. Actual container names are budgeted after inventory is known.
    free = shutil.disk_usage(io_path(parent)).free
    headroom = min(32*1024*1024,max(8*1024*1024,free//32))
    available = free-headroom
    need(available > 0, 'REGISTERED_BACKUP_DISK_REQUIRED')
    manifest = min(M,max(16384,available//16))
    # a88's directly controlled peak is 2*T+F+M. F=T here, so 3*T+M
    # fits the current free budget. This is a caller budget, not an inventory
    # estimate or a statement about SQLite/FS internal allocation.
    total = min(SAFE, (available-manifest)//3)
    need(total > 0, 'REGISTERED_BACKUP_DISK_REQUIRED')
    return Policy(MAX_FILES,64,1024,manifest,total,total)


def space(parent, needed):
    need(type(needed) is int and 0 <= needed <= SAFE, 'BACKUP_SIZE_BOUND')
    free=shutil.disk_usage(io_path(parent)).free
    headroom=min(32*1024*1024,max(8*1024*1024,free//32))
    need(free > needed+headroom,
         'REGISTERED_BACKUP_DISK_REQUIRED')


def record(path):
    path = Path(path)
    size = io_path(path).lstat().st_size
    need(0 <= size <= SAFE, 'BACKUP_SIZE_BOUND')
    p = Policy(MAX_FILES,64,1024,M,max(1,size),max(1,size))
    with HeldFile(path.parent,path.name,p) as held:
        digest,length = held.hash()
    return {'size_bytes':length,'sha256':digest}


@contextmanager
def owned_work(parent):
    parent = Path(parent)
    work = Path(io_mkdtemp(prefix='.registered-working-',dir=parent))
    try:
        os.chmod(io_path(work),0o700)
        _check_stage_parent(work,Policy(MAX_FILES,64,1024,M,SAFE,SAFE))
        yield work
    finally:
        # A failed cleanup is an error, never successful publication evidence.
        shutil.rmtree(io_path(work))
        need(not io_path(work).exists(), 'REGISTERED_BACKUP_CLEANUP_UNCONFIRMED')


def _write_archive(stage, destination, manifest):
    names = ['manifest.json']+[x['logical_name'] for x in manifest['assets']]
    with zipfile.ZipFile(io_path(destination),'x',compression=zipfile.ZIP_STORED) as archive:
        for name in names:
            with HeldFile(stage,name,Policy(**manifest['policy'])) as source:
                with archive.open(name,'w',force_zip64=True) as target:
                    for chunk in source.chunks():
                        target.write(chunk)


@contextmanager
def finite_archive(path,maximum_members=MAX_FILES+1):
    # ZipFile constructs its directory eagerly. Check the constant-size EOCD /
    # ZIP64 envelope first, before it can allocate attacker-chosen entry rows.
    # Our writers emit no member comments and at most the ZIP64 extra fields.
    with io_path(Path(path)).open('rb')as source:
        with _finite_archive_file(source,maximum_members)as archive:yield archive


@contextmanager
def _finite_archive_file(source,maximum_members=MAX_FILES+1):
    """Same original file for the bounded directory and ZipFile reader."""
    end=zipfile._EndRecData(source)
    need(end is not None,'BACKUP_CONTAINER_DIRECTORY')
    count=end[zipfile._ECD_ENTRIES_TOTAL]
    size=end[zipfile._ECD_SIZE]
    need(0<=count<=maximum_members and end[zipfile._ECD_ENTRIES_THIS_DISK]==count
         and end[zipfile._ECD_DISK_NUMBER]==end[zipfile._ECD_DISK_START]==0
         and 0<=size<=maximum_members*(46+1024+32),'BACKUP_CONTAINER_DIRECTORY')
    source.seek(0)
    with zipfile.ZipFile(source)as archive:
        need(len(archive.infolist())==count,'BACKUP_CONTAINER_DIRECTORY')
        yield archive


def create_capsule(snapshot, key, encrypt_file, *, provenance_ticket=None):
    snapshot = Path(snapshot)
    db = record(snapshot)
    with owned_work(snapshot.parent) as work:
        policy = stage_policy(work)
        stage = create_registered_asset_snapshot(snapshot,work,policy,provenance_ticket=provenance_ticket)
        receipt = verify_registered_asset_snapshot(stage,policy)
        manifest_raw = (io_path(stage/'manifest.json')).read_bytes()
        manifest = unique_json(manifest_raw)
        summary = {'coverage':'zero_registered_payload','asset_count':receipt['files'],
                   'total_bytes':receipt['total_bytes'],
                   'manifest_sha256':receipt['manifest_sha256'],
                   'coverage_sha256':sha(canonical(manifest['coverage'])),'capsule':None}
        need(record(snapshot)==db, 'BACKUP_DATABASE_CHANGED')
        if not receipt['files']:
            return summary
        need(type(key) is bytes and len(key)==32,'REGISTERED_BACKUP_KEY_REQUIRED')
        names=['manifest.json']+[x['logical_name']for x in manifest['assets']]
        # ZIP_STORED: actual payload plus actual UTF8 names and a conservative
        # per-member ZIP64/local/central/descriptor envelope; no MAX_FILES tax.
        archive_budget=receipt['total_bytes']+len(manifest_raw)+1024+sum(
            2*len(name.encode('utf-8'))+256 for name in names)
        space(work,archive_budget)
        archive = work/'registered.zip'
        _write_archive(stage,archive,manifest)
        need(archive.stat().st_size<=archive_budget,'BACKUP_CONTAINER_BOUND')
        verify_registered_asset_snapshot(stage,policy)
        shutil.rmtree(io_path(stage))
        need(not io_path(stage).exists(),'REGISTERED_BACKUP_CLEANUP_UNCONFIRMED')
        binding = {'format':FORMAT,'purpose':'registered_backup','snapshot_sha256':db['sha256'],
                   'manifest_sha256':summary['manifest_sha256'],
                   'coverage_sha256':summary['coverage_sha256'],
                   'asset_count':summary['asset_count'],'total_bytes':summary['total_bytes']}
        encrypted = work/'registered.enc'
        nonce = os.urandom(12)
        space(work,archive.stat().st_size)
        tag = encrypt_file(archive,encrypted,key,nonce,aad=canonical(binding))
        archive.unlink()
        header = {**binding,'nonce':base64.b64encode(nonce).decode('ascii'),
                  'tag':base64.b64encode(tag).decode('ascii'),
                  'ciphertext_size':io_path(encrypted).stat().st_size}
        raw = canonical(header)
        need(len(raw)<=16384,'REGISTERED_CAPSULE_HEADER_BOUND')
        output = capsule_path(snapshot)
        space(snapshot.parent,io_path(encrypted).stat().st_size+len(raw)+len(MAGIC)+4)
        with io_path(output).open('xb') as target, io_path(encrypted).open('rb') as source:
            os.chmod(io_path(output),0o600)
            target.write(MAGIC);target.write(struct.pack('>I',len(raw)));target.write(raw)
            shutil.copyfileobj(source,target,length=CHUNK)
            target.flush();os.fsync(target.fileno())
        need(record(snapshot)==db,'BACKUP_DATABASE_CHANGED')
        return {**summary,'coverage':'encrypted_registered_v1','capsule':record(output)}


def header(path):
    path=Path(path);prefix=bytearray()
    with HeldFile(path.parent,path.name,Policy(MAX_FILES,64,1024,M,SAFE,SAFE)) as held:
        size=held.verified_length()
        for chunk in held.chunks():
            if len(prefix)<16384+len(MAGIC)+4:
                prefix.extend(chunk[:16384+len(MAGIC)+4-len(prefix)])
    need(prefix[:len(MAGIC)]==MAGIC,'REGISTERED_CAPSULE_HEADER')
    length=prefix[len(MAGIC):len(MAGIC)+4]
    need(len(length)==4,'REGISTERED_CAPSULE_HEADER')
    n=struct.unpack('>I',length)[0];need(0<n<=16384,'REGISTERED_CAPSULE_HEADER')
    offset=len(MAGIC)+4+n
    need(len(prefix)>=offset,'REGISTERED_CAPSULE_HEADER')
    h=unique_json(prefix[len(MAGIC)+4:offset])
    keys={'format','purpose','snapshot_sha256','manifest_sha256','coverage_sha256',
          'asset_count','total_bytes','nonce','tag','ciphertext_size'}
    need(type(h) is dict and set(h)==keys and h['format']==FORMAT
         and h['purpose']=='registered_backup','REGISTERED_CAPSULE_HEADER')
    for k in ['snapshot_sha256','manifest_sha256','coverage_sha256']:
        need(type(h[k]) is str and len(h[k])==64 and all(c in '0123456789abcdef'for c in h[k]),'REGISTERED_CAPSULE_HEADER')
    need(type(h['asset_count']) is int and 0<h['asset_count']<=MAX_FILES
         and type(h['total_bytes']) is int and 0<=h['total_bytes']<=SAFE
         and type(h['ciphertext_size']) is int and h['ciphertext_size']>0
         and h['ciphertext_size']<=SAFE and size-offset==h['ciphertext_size'],'REGISTERED_CAPSULE_HEADER')
    need(len(base64.b64decode(h['nonce'],validate=True))==12
         and len(base64.b64decode(h['tag'],validate=True))==16,'REGISTERED_CAPSULE_HEADER')
    return h,offset


def _extract_expectations(files):
    policy=Policy(MAX_FILES,64,1024,M,SAFE,SAFE)
    need(type(files)is list and len(files)<=MAX_FILES,'BACKUP_SIZE_BOUND')
    expected={};folded=set();total=0
    for item in files:
        need(type(item)is dict and set(item)=={'archive_path','size_bytes','sha256'},'BACKUP_FILE_CONTRACT')
        name=item['archive_path'];safe_relative(name,policy)
        need(name not in expected and name.casefold() not in folded,'BACKUP_CASE_COLLISION')
        need(type(item['size_bytes']) is int and 0<=item['size_bytes']<=SAFE
             and type(item['sha256']) is str and len(item['sha256'])==64
             and all(c in '0123456789abcdef'for c in item['sha256']),'BACKUP_FILE_CONTRACT')
        expected[name]=item;folded.add(name.casefold());total+=item['size_bytes']
    need(len(expected)<=MAX_FILES and total<=SAFE,'BACKUP_SIZE_BOUND')
    return policy,expected,total


def validate_extract_members(archive,files,manifest_name=None,manifest_limit=M):
    """Original finite extraction predicates; no paths or write authority."""
    policy,expected,total=_extract_expectations(files)
    rows=archive.infolist();names=[x.filename for x in rows]
    need(len(names)==len(set(names)) and len(names)==len({x.casefold()for x in names}),'BACKUP_DUPLICATE_MEMBER')
    allowed=set(expected)|({manifest_name}if manifest_name else set())
    need(set(names)==allowed,'BACKUP_EXTRA_OR_MISSING_MEMBER')
    for row in rows:
        safe_relative(row.filename,policy)
        kind=(row.external_attr>>16)&0o170000
        need(not row.is_dir() and kind in {0,stat.S_IFREG} and not(row.flag_bits&1),'BACKUP_MEMBER_ALIAS')
        if row.filename==manifest_name:
            need(row.file_size<=manifest_limit,'BACKUP_MANIFEST_BOUND');continue
        item=expected[row.filename];need(row.file_size==item['size_bytes'],'BACKUP_MEMBER_SIZE')
    return expected,total


def extract_exact(archive_path, destination, files, manifest_name=None, manifest_limit=M):
    """Stream a finite caller whitelist; archived paths never authorize roots."""
    policy,expected,total=_extract_expectations(files)
    destination=Path(destination);io_path(destination).mkdir(mode=0o700,parents=True,exist_ok=False)
    space(destination,total)
    with finite_archive(archive_path,len(expected)+(1 if manifest_name else 0)) as archive:
        validate_extract_members(archive,files,manifest_name,manifest_limit)
        for row in archive.infolist():
            if row.filename==manifest_name:continue
            item=expected[row.filename]
            target=destination.joinpath(*PurePosixPath(row.filename).parts)
            io_path(target.parent).mkdir(mode=0o700,parents=True,exist_ok=True)
            count=0;digest=hashlib.sha256()
            with archive.open(row) as source,io_path(target).open('xb') as out:
                while chunk:=source.read(CHUNK):
                    count+=len(chunk);need(count<=item['size_bytes'],'BACKUP_MEMBER_SIZE')
                    digest.update(chunk);out.write(chunk)
                out.flush();os.fsync(out.fileno())
            need(count==item['size_bytes'] and digest.hexdigest()==item['sha256'],'BACKUP_MEMBER_CHECKSUM')


def decrypt_capsule(path, key, decrypt_file, owned_parent, expected_snapshot_sha256):
    """Verified private stage for later migration/tests; never publishes live data."""
    path=Path(path);h,offset=header(path)
    need(h['snapshot_sha256']==expected_snapshot_sha256,'REGISTERED_CAPSULE_SNAPSHOT_MISMATCH')
    binding={k:v for k,v in h.items()if k not in {'nonce','tag','ciphertext_size'}}
    with owned_work(owned_parent) as work:
        # GCM ciphertext has the ZIP's exact length. Budget the actual phase;
        # extraction checks its measured whitelist separately while ZIP remains.
        space(work,h['ciphertext_size'])
        archive=work/'registered.zip'
        decrypt_file(path,archive,key,base64.b64decode(h['nonce']),base64.b64decode(h['tag']),offset,aad=canonical(binding))
        with finite_archive(archive) as z:
            info=z.getinfo('manifest.json');need(info.file_size<=M,'BACKUP_MANIFEST_BOUND')
            with z.open(info) as f:raw=f.read(M+1)
        need(len(raw)<=M and sha(raw)==h['manifest_sha256'],'REGISTERED_CAPSULE_MANIFEST')
        m=unique_json(raw);policy=Policy(**m['policy']);policy.validate()
        # MAX_FILES counts payload assets. The authenticated metadata member has
        # its separate finite size/member allowance, just as in the writer.
        files=[{'archive_path':x['logical_name'],'size_bytes':x['bytes'],'sha256':x['sha256']}for x in m['assets']]
        stage=Path(io_mkdtemp(prefix='registered-verified-',dir=owned_parent));io_path(stage).rmdir()
        try:
            extract_exact(archive,stage,files,manifest_name='manifest.json')
            # Write only the manifest bytes already bound to the capsule AAD;
            # extraction still rejects duplicate/extra/alias metadata members.
            space(stage,len(raw))
            with (io_path(stage/'manifest.json')).open('xb') as output:
                output.write(raw);output.flush();os.fsync(output.fileno())
            receipt=verify_registered_asset_snapshot(stage,policy)
            need(receipt['files']==h['asset_count'] and receipt['total_bytes']==h['total_bytes']
                 and sha(canonical(m['coverage']))==h['coverage_sha256'],'REGISTERED_CAPSULE_COVERAGE')
            return stage
        except Exception:
            if io_path(stage).exists():shutil.rmtree(io_path(stage))
            raise


def _full_contract_rows(metadata,record_for,actual,capsule_exists,capsule_header_for,sidecars):
    """Original full-contract data predicates, shared by disk and held ZIP data."""
    need(metadata['format']==FULL,'BACKUP_FULL_CONTRACT')
    need(set(metadata)=={'format','files','missing_media_references','requires_existing_media_key','database','registered'},'BACKUP_FULL_CONTRACT')
    need(type(metadata['files'])is list and len(metadata['files'])<=MAX_FILES
         and type(metadata['missing_media_references'])is int and metadata['missing_media_references']>=0
         and type(metadata['requires_existing_media_key'])is bool,'BACKUP_FULL_CONTRACT')
    need(record_for('database.db')==metadata['database'],'BACKUP_DATABASE_CHANGED')
    rows=[{'archive_path':'database.db',**metadata['database']}]
    expected=set();folded=set()
    for item in metadata['files']:
        need(set(item)=={'relative_path','size_bytes','sha256'},'BACKUP_FILE_CONTRACT')
        n=item['relative_path'];safe_relative(n,Policy(MAX_FILES,64,1024,M,SAFE,SAFE))
        need(n not in expected and n.casefold()not in folded,'BACKUP_CASE_COLLISION');expected.add(n);folded.add(n.casefold())
        value=record_for('database.media/'+n);need(value=={k:item[k]for k in ['size_bytes','sha256']},'BACKUP_MEDIA_CHANGED')
        rows.append({'archive_path':'database.media/'+n,**value})
    need(actual==expected,'BACKUP_EXTRA_OR_MISSING_MEMBER')
    reg=metadata['registered']
    need(type(reg)is dict and set(reg)=={'coverage','asset_count','total_bytes','manifest_sha256','coverage_sha256','capsule'},'BACKUP_FULL_CONTRACT')
    need(type(reg['asset_count'])is int and 0<=reg['asset_count']<=MAX_FILES
         and type(reg['total_bytes'])is int and 0<=reg['total_bytes']<=SAFE,'BACKUP_FULL_CONTRACT')
    for name in ['manifest_sha256','coverage_sha256']:
        value=reg[name]
        need(type(value)is str and len(value)==64 and all(c in '0123456789abcdef'for c in value),'BACKUP_FULL_CONTRACT')
    if reg['coverage']=='zero_registered_payload':
        need(reg['asset_count']==reg['total_bytes']==0 and reg['capsule']is None
             and not capsule_exists,'BACKUP_FULL_CONTRACT')
    else:
        need(reg['coverage']=='encrypted_registered_v1' and record_for('database.registered.enc')==reg['capsule'],'BACKUP_CAPSULE_CHANGED')
        h=capsule_header_for();need(h['snapshot_sha256']==metadata['database']['sha256']
            and all(h[k]==reg[k]for k in ['asset_count','total_bytes','manifest_sha256','coverage_sha256']),'BACKUP_FULL_CONTRACT')
        rows.append({'archive_path':'database.registered.enc',**reg['capsule']})
    for suffix in ['-wal','-shm','-journal']:
        need(suffix not in sidecars,'BACKUP_UNACCOUNTED_SIDECAR')
    need(len(rows)<MAX_FILES and sum(x['size_bytes']for x in rows)<=SAFE,'BACKUP_SIZE_BOUND')
    return rows


def validate_full_records(metadata,records,capsule_header=None):
    """Data validation only; these records cannot issue any filesystem right."""
    need(type(records)is dict and all(name in{'database.db','database.registered.enc'}or name.startswith('database.media/')for name in records),'BACKUP_EXTRA_OR_MISSING_MEMBER')
    actual={name.removeprefix('database.media/')for name in records if name.startswith('database.media/')}
    return _full_contract_rows(metadata,records.get,actual,'database.registered.enc'in records,lambda:capsule_header,set())


def full_files(snapshot,metadata):
    """Original held-file and namespace observations feed the same validator."""
    snapshot=Path(snapshot);media=snapshot.with_suffix('.media')
    actual=set()
    if os.path.lexists(io_path(media)):
        need(not io_path(media).is_symlink() and io_path(media).is_dir(),'BACKUP_MEMBER_ALIAS')
        for base,dirs,files in io_walk(media,followlinks=False):
            for n in dirs:need(not(io_path(Path(base)/n)).is_symlink(),'BACKUP_MEMBER_ALIAS')
            for n in files:actual.add((Path(base)/n).relative_to(media).as_posix())
    def record_for(name):
        if name=='database.db':return record(snapshot)
        if name=='database.registered.enc':return record(capsule_path(snapshot))
        need(name.startswith('database.media/'),'BACKUP_FILE_CONTRACT')
        return record(media/name.removeprefix('database.media/'))
    sidecars={suffix for suffix in ['-wal','-shm','-journal']if os.path.lexists(io_path(Path(str(snapshot)+suffix)))}
    return _full_contract_rows(metadata,record_for,actual,os.path.lexists(io_path(capsule_path(snapshot))),lambda:header(capsule_path(snapshot))[0],sidecars)

_PROVENANCE_CAPSULE_CODE=create_capsule.__code__
