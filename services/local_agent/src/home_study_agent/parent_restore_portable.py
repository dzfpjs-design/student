"""Original native portable source; data readers confer no publication rights."""
from contextlib import contextmanager,ExitStack
from pathlib import Path
from types import SimpleNamespace
import base64,hashlib,json,os,sqlite3,struct,weakref
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import restore_publication as pub,parent_restore_ownership as ownership

CODE='RESTORE_PORTABLE_SOURCE_UNCONFIRMED'
_READERS=weakref.WeakKeyDictionary()
class _OriginalMember:
    __slots__=('__weakref__',)
    def __new__(cls):raise pub.RestorePublicationError(CODE)

class _CipherCacheFile:
    """Read-only plaintext view; every disk byte is standard AESGCM ciphertext.

    The random cache key and nonce prefix live only in this process. This view
    is not exposed or parsed until the original envelope GCM tag is verified.
    It owns no native handle and grants no file creation or publication right.
    """
    def __init__(self,work,held,original,key,prefix,total,chunk_size,binding):
        self.work=work;self.held=held;self.original=original;self.cipher=AESGCM(key);self.prefix=prefix
        self.total=total;self.chunk_size=chunk_size;self.binding=binding;self.actor=pub._actor()
        self.position=0;self.closed=False;self.last_index=None;self.last_plain=None
    def _check(self):
        pub._need(not self.closed and self.cipher is not None and self.actor==pub._actor(),CODE)
        pub._owner(self.work.owner);self.held.check();pub._need(not self.original.closed,CODE)
        if os.name=='nt':
            import msvcrt
            from .registered_asset_restore import _TargetWin32,_win_target_check
            info=_win_target_check(_TargetWin32(),msvcrt.get_osfhandle(self.original.fileno()),self.held.root.joinpath(*self.held.parts),
                directory=False,identity=tuple(self.held.before['identity']))
            pub._need(info['links']==1 and info['size']==self.held.verified_length(),CODE)
        else:
            info=os.fstat(self.original.fileno());before=self.held.before
            pub._need((info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_nlink)
                ==(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,1),CODE)
    def readable(self):return not self.closed
    def seekable(self):return not self.closed
    def tell(self):self._check();return self.position
    def seek(self,offset,whence=0):
        self._check();pub._need(type(offset)is int and whence in{0,1,2},CODE)
        position=offset+(self.position if whence==1 else self.total if whence==2 else 0)
        pub._need(0<=position<=self.total,CODE);self.position=position;return position
    def read(self,size=-1):
        from . import registered_asset_backup as reg
        self._check();pub._need(type(size)is int and size>=-1,CODE)
        count=self.total-self.position if size==-1 else min(size,self.total-self.position)
        pub._need(count<=max(reg.M,(reg.MAX_FILES+1)*(46+1024+32)),CODE)
        output=bytearray()
        while count:
            index=self.position//self.chunk_size;inside=self.position%self.chunk_size
            length=min(self.chunk_size,self.total-index*self.chunk_size)
            if self.last_index!=index:
                self.original.seek(index*(self.chunk_size+16));ciphertext=self.original.read(length+16)
                pub._need(len(ciphertext)==length+16,CODE)
                nonce=self.prefix+struct.pack('>I',index)
                self.last_plain=self.cipher.decrypt(nonce,ciphertext,_cache_aad(self.binding,index))
                pub._need(len(self.last_plain)==length,CODE);self.last_index=index
            take=min(count,length-inside);pub._need(take>0,CODE)
            output.extend(self.last_plain[inside:inside+take]);self.position+=take;count-=take
        self._check();return bytes(output)
    def close(self):
        self.closed=True;self.cipher=None;self.prefix=None;self.last_plain=None;self.binding=None

def _cache_aad(binding,index):
    return pub._canonical({'domain':'hsc-portable-cache-v1',**binding,'chunk_index':index})

def has_original_native_claim(c,request_id):
    if type(request_id)is not str:return False
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone():return False
    rows=pub._bounded_rows(c,"SELECT metadata_json FROM native_privacy_permit WHERE status='claimed'AND typeof(metadata_json)='text'AND length(CAST(metadata_json AS BLOB))BETWEEN 1 AND 8192 AND json_valid(metadata_json)AND json_extract(metadata_json,'$.request_id')=?LIMIT 2",(request_id,),1,CODE)
    if not rows:return False
    metadata=pub._json_object(rows[0][0],8192,CODE)
    pub._need(metadata.get('kind')=='portable_restore'and metadata.get('child_id')is None and metadata.get('scope')is None,CODE)
    return True  # Dispatch only. begin/_facts still verify the original claim.

def _header(service,held):
    from .privacy_lifecycle import PORTABLE_BACKUP_MAGIC
    raw=bytearray();limit=len(PORTABLE_BACKUP_MAGIC)+4+16384
    for block in held.chunks():
        raw.extend(block[:limit-len(raw)])
        if len(raw)>=limit or len(raw)>=len(PORTABLE_BACKUP_MAGIC)+4:break
    size=len(PORTABLE_BACKUP_MAGIC);pub._need(bytes(raw[:size])==PORTABLE_BACKUP_MAGIC and len(raw)>=size+4,CODE)
    length=struct.unpack('>I',raw[size:size+4])[0];pub._need(0<length<=16384,CODE)
    # Rewind the same original handle, never open a source name again.
    raw=bytearray()
    for block in held.chunks():
        raw.extend(block[:size+4+length-len(raw)])
        if len(raw)==size+4+length:break
    pub._need(len(raw)==size+4+length,CODE);value=pub._json_object(bytes(raw[size+4:]),16384,CODE)
    pub._need(set(value)=={'format','kdf','n','r','p','salt','nonce','tag','ciphertext_size'}
        and value['format']=='hsc_portable_backup_v1'and value['kdf']=='scrypt'
        and all(type(value[k])is int and value[k]==v for k,v in(('n',32768),('r',8),('p',1)))
        and type(value['ciphertext_size'])is int and 0<value['ciphertext_size']<=2**53-1
        and held.verified_length()==len(raw)+value['ciphertext_size'],CODE)
    for name,expected in(('salt',16),('nonce',12),('tag',16)):
        pub._need(type(value[name])is str,CODE);decoded=base64.b64decode(value[name],validate=True);pub._need(len(decoded)==expected,CODE);value[name]=decoded
    return value,len(raw)

def _raw_member(state,name):
    """One exact authenticated member of this held original owned ZIP."""
    from . import registered_asset_backup as reg
    pub._need(state.active and name in state.records,CODE);state.cache_view._check()
    record=state.records[name];count=0;digest=hashlib.sha256()
    with state.archive.open(name)as member:
        while True:
            block=member.read(reg.CHUNK)
            if not block:break
            count+=len(block);pub._need(count<=record['size_bytes'],CODE);digest.update(block);yield block
    pub._need(count==record['size_bytes']and digest.hexdigest()==record['sha256'],CODE)
    state.cache_view._check()

def _encrypted_member(state,name,nonce):
    """Reproduce exactly one destination ciphertext; verify the whole source."""
    size=state.records[name]['size_bytes'];pub._need(32<=size<=64*1024*1024,CODE)
    prefix=bytearray();tail=bytearray();decryptor=None;encryptor=None;count=0
    for block in _raw_member(state,name):
        if decryptor is None:
            take=min(16-len(prefix),len(block));prefix.extend(block[:take]);block=block[take:]
            if len(prefix)<16:continue
            pub._need(prefix[:4]==b'HSC1',CODE)
            decryptor=Cipher(algorithms.AES(state.source_key),modes.GCM(bytes(prefix[4:]))).decryptor();decryptor.authenticate_additional_data(b'hsc-raw-media-v1')
            encryptor=Cipher(algorithms.AES(state.target_key),modes.GCM(nonce)).encryptor();encryptor.authenticate_additional_data(b'hsc-raw-media-v1')
            yield b'HSC1'+nonce
        tail.extend(block)
        if len(tail)>16:
            ciphertext=bytes(tail[:-16]);tail=tail[-16:];plain=decryptor.update(ciphertext);count+=len(plain)
            pub._need(count<=size-32,CODE)
            if plain:yield encryptor.update(plain)
    pub._need(decryptor is not None and encryptor is not None and len(tail)==16,CODE)
    plain=decryptor.finalize_with_tag(bytes(tail));count+=len(plain);pub._need(count==size-32,CODE)
    if plain:yield encryptor.update(plain)
    end=encryptor.finalize()
    if end:yield end
    yield encryptor.tag

def _member_hash(state,name,nonce):
    digest=hashlib.sha256();count=0
    for block in _encrypted_member(state,name,nonce):digest.update(block);count+=len(block)
    pub._need(count==state.records[name]['size_bytes'],CODE);return digest.hexdigest(),count

def original_member_contract(reader,service,work,verify):
    from .parent_restore_workflow import original_source_bundle
    pub._need(type(reader)is _OriginalMember and reader in _READERS,CODE);value=_READERS[reader]
    state=value.state;pub._need(state.active and state.actor==pub._actor()and state.work is work
        and original_source_bundle(service,work,verify)is state,CODE)
    def check():
        pub._owner(work.owner);pub._need(state.active and state.actor==pub._actor(),CODE)
        return _member_hash(state,value.name,value.nonce)
    return value.expected,lambda:_encrypted_member(state,value.name,value.nonce),check

@contextmanager
def _authenticated_portable(service,work,stack,envelope,source_sha,passphrase):
    from . import registered_asset_backup as reg,parent_restore_workflow as workflow
    from .registered_asset_snapshot import HeldFile,Policy
    from .parent_restore_assets import _OriginalArchiveFile
    header,offset=_header(service,envelope);key=service._derive_portable_key(passphrase,header['salt'])
    from .privacy_lifecycle import PORTABLE_BACKUP_AAD
    decryptor=Cipher(algorithms.AES(key),modes.GCM(header['nonce'],header['tag'])).decryptor();decryptor.authenticate_additional_data(PORTABLE_BACKUP_AAD)
    def plaintext():
        skip=offset;total=0
        for block in envelope.chunks():
            if skip:part=min(skip,len(block));skip-=part;block=block[part:]
            if block:
                total+=len(block);pub._need(total<=header['ciphertext_size'],CODE);value=decryptor.update(block)
                if value:yield value
        pub._need(skip==0 and total==header['ciphertext_size'],CODE);value=decryptor.finalize()
        if value:yield value
    total=header['ciphertext_size'];chunks=(total+reg.CHUNK-1)//reg.CHUNK
    pub._need(0<chunks<=2**32 and total+16*chunks<=reg.SAFE,CODE)
    cache_key=os.urandom(32);nonce_prefix=os.urandom(8);cache_cipher=AESGCM(cache_key)
    cache_binding={'source_sha256':source_sha,'work_binding_sha256':pub._sha(pub._canonical(work._value()['binding'])),
        'plain_total':total,'chunk_size':reg.CHUNK,'chunk_count':chunks}
    def sealed_cache():
        pending=bytearray();index=0;count=0
        for block in plaintext():
            pending.extend(block)
            while len(pending)>=reg.CHUNK:
                value=bytes(pending[:reg.CHUNK]);del pending[:reg.CHUNK]
                yield cache_cipher.encrypt(nonce_prefix+struct.pack('>I',index),value,_cache_aad(cache_binding,index))
                index+=1;count+=len(value)
        if pending:
            yield cache_cipher.encrypt(nonce_prefix+struct.pack('>I',index),bytes(pending),_cache_aad(cache_binding,index))
            index+=1;count+=len(pending)
        pub._need(index==chunks and count==total,CODE)
    # The original envelope tag is checked by plaintext() before this returns.
    # On failure only registered opaque ciphertext remains; never a plaintext ZIP.
    reg.space(work.work,total+16*chunks)
    zip_path,zip_hash=work.portable_core('cache',sealed_cache(),total+16*chunks)
    pub._need(envelope.hash()[0]==source_sha,CODE)
    entry=next(e for e in work._value()['members']['entries']if work.owner.path.parent/e['relative']==zip_path)
    held=stack.enter_context(HeldFile(work.work,zip_path.name,Policy(reg.MAX_FILES,64,1024,reg.M,reg.SAFE,reg.SAFE)))
    pub._need(held.hash()==zip_hash,CODE)
    original=stack.enter_context(_OriginalArchiveFile(zip_path,entry['identity']))
    cache_view=_CipherCacheFile(work,held,original,cache_key,nonce_prefix,total,reg.CHUNK,cache_binding)
    stack.callback(cache_view.close)
    pub._need(type(cache_view)is _CipherCacheFile and not hasattr(cache_view,'fileno')and not hasattr(cache_view,'write'),CODE)
    # This one fixed ZipFile manager owns no native FD: the original data-read
    # file and HeldFile remain in strict _SourceHandles, including failed close.
    archive=ExitStack.enter_context(stack,reg._finite_archive_file(cache_view,reg.MAX_FILES+1))
    info=archive.getinfo('portable-manifest.json');pub._need(info.file_size<=reg.M,CODE)
    with archive.open(info)as member:manifest_raw=member.read(reg.M+1)
    pub._need(len(manifest_raw)<=reg.M,CODE);manifest=reg.unique_json(manifest_raw)
    pub._need(type(manifest)is dict and set(manifest)=={'format','files','media_files','portable_backup_id','created_at','source_data_key'}
        and manifest['format']=='hsc_portable_archive_v1'and type(manifest['portable_backup_id'])is str
        and pub._ID.fullmatch(manifest['portable_backup_id'])is not None,CODE)
    records,_=reg.validate_extract_members(archive,manifest['files'],'portable-manifest.json')
    records={name:{key:item[key]for key in('sha256','size_bytes')}for name,item in records.items()}
    pub._need('database.db'in records and 'database.media.json'in records
        and all(name in{'database.db','database.media.json','database.registered.enc'}or name.startswith('database.media/')for name in records),CODE)
    for info in archive.infolist():pub._need(info.compress_type==0 and info.compress_size==info.file_size,CODE)
    pub._need(type(manifest['source_data_key'])is str,CODE);source_key=base64.b64decode(manifest['source_data_key'],validate=True)
    pub._need(len(source_key)==32 and type(service.data_encryption_key)is bytes and len(service.data_encryption_key)==32,CODE)
    state=SimpleNamespace(active=True,actor=pub._actor(),service=service,work=work,stack=stack,source_path=work.work/'authenticated-cold.db',
        records=records,archive=archive,original=original,zip_held=held,cache_view=cache_view,source_key=source_key,target_key=service.data_encryption_key,
        media_held={},capsule_held=None,bundle_sha256=source_sha,portable_id=manifest['portable_backup_id'])
    readers=[];verify=None
    try:
        # Authenticate every member/hash before minting any source reader.
        for name in records:
            for _ in _raw_member(state,name):pass
        pub._need(records['database.media.json']['size_bytes']<=reg.M,CODE)
        metadata=reg.unique_json(b''.join(_raw_member(state,'database.media.json')))
        pub._need(type(metadata)is dict and type(metadata.get('files'))is list and len(metadata['files'])<=reg.MAX_FILES
            and metadata.get('files')==manifest['media_files']and type(metadata.get('missing_media_references'))is int and metadata['missing_media_references']==0
            and type(metadata.get('requires_existing_media_key'))is bool and metadata['requires_existing_media_key']==bool(metadata['files']),CODE)
        pub._need(work.portable_core('database',_raw_member(state,'database.db'),records['database.db']['size_bytes'])[1]
            ==(records['database.db']['sha256'],records['database.db']['size_bytes']),CODE)
        state.capsule_database_sha256=records['database.db']['sha256'];capsule_header=None
        if 'database.registered.enc'in records:
            capsule,copied=work.portable_core('capsule',_raw_member(state,'database.registered.enc'),records['database.registered.enc']['size_bytes'])
            pub._need(copied==(records['database.registered.enc']['sha256'],records['database.registered.enc']['size_bytes']),CODE)
            state.capsule_held=stack.enter_context(HeldFile(work.work,capsule.name,Policy(reg.MAX_FILES,64,1024,reg.M,reg.SAFE,reg.SAFE)))
            capsule_header,_=reg.header(capsule)
        if metadata.get('format')==reg.FULL:
            rows=reg.validate_full_records(metadata,{name:item for name,item in records.items()if name!='database.media.json'},capsule_header)
            pub._need(sorted(rows,key=lambda r:r['archive_path'])==sorted([{'archive_path':name,**item}for name,item in records.items()if name!='database.media.json'],key=lambda r:r['archive_path']),CODE)
        else:
            pub._need(set(metadata)=={'format','files','missing_media_references','requires_existing_media_key'}and metadata['format']=='hsc_backup_media_v1'
                and 'database.registered.enc'not in records,CODE)
            pub._need(set(records)=={'database.db','database.media.json'}|{'database.media/'+r['relative_path']for r in metadata['files']},CODE)
        derived=[]
        for item in metadata['files']:
            pub._need(type(item)is dict and set(item)=={'relative_path','sha256','size_bytes'},CODE)
            reg.safe_relative(item['relative_path'],Policy(reg.MAX_FILES,64,1024,reg.M,reg.SAFE,reg.SAFE))
            name='database.media/'+item['relative_path'];pub._need(records[name]=={k:item[k]for k in('sha256','size_bytes')},CODE)
            nonce=os.urandom(12);expected=_member_hash(state,name,nonce);reader=object.__new__(_OriginalMember)
            _READERS[reader]=SimpleNamespace(state=state,name=name,nonce=nonce,expected=expected);readers.append(reader)
            state.media_held[item['relative_path']]=reader;derived.append({'relative_path':item['relative_path'],'sha256':expected[0],'size_bytes':expected[1]})
        metadata['files']=derived;state.metadata=metadata
        cold=work.work/'authenticated-cold.db'
        with pub._sql(cold,owner=work.owner,timeout=0)as c:
            c.row_factory=sqlite3.Row
            service._validate_restore_scope(c);pub._need(c.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete',CODE)
            c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE');service._rekey_portable_credentials(c,source_key,state.target_key);c.commit()
        state.database_sha256=pub._file_hash(cold);metadata_sha=pub._sha(pub._canonical(metadata))
        def verify_original():
            pub._owner(work.owner);pub._need(state.active and state.actor==pub._actor()and stack.active and stack.owner is work.owner,CODE)
            pub._need(work._value()['binding']['source_sha256']==source_sha and envelope.hash()[0]==source_sha and held.hash()==zip_hash
                and pub._file_hash(cold)==state.database_sha256 and pub._sha(pub._canonical(state.metadata))==metadata_sha,CODE)
            cache_view._check()
            if state.capsule_held is not None:pub._need(state.capsule_held.hash()==(records['database.registered.enc']['sha256'],records['database.registered.enc']['size_bytes']),CODE)
        verify=verify_original;workflow._SOURCE_BUNDLES[verify]=state;verify();yield verify,state
    finally:
        if verify is not None:workflow._SOURCE_BUNDLES.pop(verify,None)
        for reader in readers:_READERS.pop(reader,None)
        state.active=False;state.source_key=None;state.target_key=None;state.media_held.clear()

def publish_native_portable(service,path,passphrase,request_id):
    from .privacy_lifecycle import InvalidBackupError,RestoreWhileSessionActiveError
    try:return _publish_native_portable(service,path,passphrase,request_id)
    except (pub.RestorePublicationError,InvalidBackupError,RestoreWhileSessionActiveError):raise
    except Exception:raise InvalidBackupError('迁移备份认证或受控准备失败。')from None

def _publish_native_portable(service,path,passphrase,request_id):
    from .registered_asset_snapshot import HeldFile,Policy
    from . import registered_asset_backup as reg,parent_restore_workflow as workflow
    from .privacy_operation_receipt import PrivacyOperationReceipts
    from .privacy_lifecycle import RestoreWhileSessionActiveError
    pub._need(type(request_id)is str,CODE)
    with pub.publication_owner(service.database)as owner:
        with service.database.connect()as c:
            pub._need(has_original_native_claim(c,request_id),CODE)
            if c.execute("SELECT 1 FROM daily_session WHERE status='active'LIMIT 1").fetchone():raise RestoreWhileSessionActiveError()
            runtime=c.execute('SELECT clean_shutdown,run_id,started_at,ended_at FROM runtime_state WHERE singleton_id=1').fetchone()
            version=c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
        source=pub._path(Path(path).expanduser())
        with workflow._SourceHandles(owner)as stack:
            original=stack.enter_context(HeldFile(source.parent,source.name,Policy(reg.MAX_FILES,64,4096,reg.M,reg.SAFE,reg.SAFE)))
            source_sha,_=original.hash();PrivacyOperationReceipts(service.database).bind_source(request_id,source_sha)
            work=ownership.begin(owner,request_id)
            with _authenticated_portable(service,work,stack,original,source_sha,passphrase)as(verify,bundle):
                recovery=workflow._publish_verified_bundle(service,owner,work,verify,runtime,version,kind='portable_restore',
                    summary={'portable_backup_id':bundle.portable_id,'source_sha256':source_sha,'media_file_count':len(bundle.media_held)})
                result={'portable_backup_id':bundle.portable_id,'status':'restored','integrity_status':'ok',
                    **workflow._protection_result(recovery),'media_file_count':len(bundle.media_held)}
        return result


def publish_parent_portable(service,path,passphrase,operation):
    """Only the original App's live opaque Parent capability can reach here."""
    from .registered_asset_snapshot import HeldFile,Policy
    from . import registered_asset_backup as reg,parent_restore_workflow as workflow,parent_restore_parent as parent
    from .privacy_lifecycle import RestoreWhileSessionActiveError,InvalidBackupError
    state=parent._cap(service.database);owner=state.owner
    pub._need(not state.recovery,CODE)
    with service.database.connect()as c:
        if c.execute("SELECT 1 FROM daily_session WHERE status='active'LIMIT 1").fetchone():raise RestoreWhileSessionActiveError()
        runtime=c.execute('SELECT clean_shutdown,run_id,started_at,ended_at FROM runtime_state WHERE singleton_id=1').fetchone()
        version=c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
    source=pub._path(Path(path).expanduser())
    with workflow._SourceHandles(owner)as stack:
        original=stack.enter_context(HeldFile(source.parent,source.name,Policy(reg.MAX_FILES,64,4096,reg.M,reg.SAFE,reg.SAFE)))
        source_sha,_=original.hash();receipt,fresh=parent.reserve_original(service.database,operation,path,source_sha)
        if not fresh:return {'operation_receipt':receipt,'replayed':True}
        work=ownership.begin(owner,operation['request_id'],parent_database=service.database)
        with _authenticated_portable(service,work,stack,original,source_sha,passphrase)as(verify,bundle):
            recovery=workflow._publish_verified_bundle(service,owner,work,verify,runtime,version,kind='portable_restore',
                summary={'portable_backup_id':bundle.portable_id,'source_sha256':source_sha,'media_file_count':len(bundle.media_held)})
            return {'portable_backup_id':bundle.portable_id,'status':'restored','integrity_status':'ok',
                **workflow._protection_result(recovery),'media_file_count':len(bundle.media_held)}
