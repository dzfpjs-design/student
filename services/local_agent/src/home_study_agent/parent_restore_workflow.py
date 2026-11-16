"""Original parent DB-only publication; journal never supplies authority."""
from contextlib import closing,ExitStack
from pathlib import Path
import sqlite3,os,weakref
from types import SimpleNamespace
from . import parent_restore_ownership as ownership
from . import restore_publication as pub
from . import native_privacy as native
from .database import Database
from .privacy_operation_receipt import PrivacyOperationReceipts,record_restore

_SOURCE_CODE='RESTORE_PUBLICATION_PREPARED_CHANGED'
_SOURCE_BUNDLES=weakref.WeakKeyDictionary()

class _SourceCloseCallbacks:
    """Confirm each original close invocation, never probe/reclose its number.

    HeldFile can reject inside __enter__, close its own stack, then leave the
    original numeric fd/HANDLE attributes behind. Only callback return is proof
    of closure; stack emptiness and those stale attributes are not proof.
    """
    def __init__(self,manager):
        self.manager=manager;self.stack=manager.stack;self.records=[]
        self.register=self.stack.callback
        self.stack.callback=self.callback
    def callback(self,function,*args,**kwargs):
        record={'function':function,'args':args,'kwargs':kwargs,'confirmed':False,'error':None}
        self.records.append(record)  # Before registration can itself fail.
        def close_original():
            try:result=function(*args,**kwargs)
            except BaseException as error:record['error']=error;raise
            record['confirmed']=True;return result
        self.register(close_original)
        return function
    def all_confirmed(self):return all(r['confirmed']for r in self.records)

class _SourceHandles(ExitStack):
    def __init__(self,owner):
        super().__init__();self.owner=owner;self.original=[];self.close_callbacks=[];self.enter_failures=[];self.active=True
    def enter_context(self,manager):
        from .registered_asset_snapshot import HeldFile
        self.original.append(manager)  # __enter__ may acquire then fail closing.
        callbacks=None
        try:
            if type(manager)is HeldFile:
                pub._need(type(manager.stack)is ExitStack,'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')
                callbacks=_SourceCloseCallbacks(manager);self.close_callbacks.append(callbacks)
            result=super().enter_context(manager)
        except BaseException as error:
            self.enter_failures.append((manager,error))
            if callbacks is None or not callbacks.all_confirmed():
                self.owner.gate.poisoned=True
                self.owner.gate.retained.append(('restore_source_enter',self,manager,callbacks,error))
            raise
        if result is not manager:self.original.append(result)
        return result
    def __exit__(self,*args):
        self.active=False
        try:return super().__exit__(*args)
        except BaseException:
            self.owner.gate.poisoned=True;self.owner.gate.retained.append(('restore_source_handles',self))
            raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None

def has_active_publication(database):
    # Advisory dispatch only: either subsequent connection admission or a fresh
    # exclusive owner rechecks the original bounded journal before any SQL.
    return os.path.lexists(database.path.with_name(pub._names(database.path)[1]))or ownership.active_path(database.path)or getattr(pub._gate(database.path),'work_anchor_active',False)

def _authenticated_raw_copy(service,source_path,cold,expected_bundle,stack,work=None):
    from .registered_asset_snapshot import HeldFile,Policy
    from . import registered_asset_backup as reg
    import hashlib
    policy=Policy(reg.MAX_FILES,64,4096,reg.M,reg.SAFE,reg.SAFE)
    held=stack.enter_context(HeldFile(source_path.parent,source_path.name,policy))
    db_sha,db_size=held.hash();db_record={'sha256':db_sha,'size_bytes':db_size}
    meta_path=service._backup_metadata_path(source_path);meta_held=None;meta_raw=None
    if os.path.lexists(meta_path):
        meta_held=stack.enter_context(HeldFile(meta_path.parent,meta_path.name,Policy(reg.MAX_FILES,64,4096,reg.M,reg.M,reg.M)))
        meta_raw=b''.join(meta_held.chunks());pub._need(len(meta_raw)<=reg.M,'RESTORE_PUBLICATION_PREPARED_CHANGED')
        metadata=reg.unique_json(meta_raw)
        meta_record={'sha256':hashlib.sha256(meta_raw).hexdigest(),'size_bytes':len(meta_raw)}
    else:metadata=None;meta_record=None
    media_held={};capsule_held=None
    if metadata is not None:
        pub._need(type(metadata)is dict and metadata.get('format')in{reg.FULL,'hsc_backup_media_v1'}
            and type(metadata.get('files'))is list and len(metadata['files'])<=reg.MAX_FILES
            and type(metadata.get('missing_media_references'))is int and metadata['missing_media_references']==0
            and type(metadata.get('requires_existing_media_key'))is bool,_SOURCE_CODE)
        names=set();folded=set()
        for item in metadata['files']:
            pub._need(type(item)is dict and set(item)=={'relative_path','size_bytes','sha256'},_SOURCE_CODE)
            name=item['relative_path'];reg.safe_relative(name,policy)
            pub._need(name not in names and name.casefold()not in folded and type(item['size_bytes'])is int
                and 0<=item['size_bytes']<=reg.SAFE and type(item['sha256'])is str and pub._HEX.fullmatch(item['sha256']),_SOURCE_CODE)
            names.add(name);folded.add(name.casefold())
            original=stack.enter_context(HeldFile(service._backup_media_dir(source_path),name,policy))
            pub._need(original.hash()==(item['sha256'],item['size_bytes']),_SOURCE_CODE);media_held[name]=original
        if metadata['format']==reg.FULL:
            registered=metadata.get('registered');pub._need(type(registered)is dict,_SOURCE_CODE)
            if registered.get('coverage')=='encrypted_registered_v1':
                capsule=reg.capsule_path(source_path);capsule_held=stack.enter_context(HeldFile(capsule.parent,capsule.name,policy))
                before=capsule_held.hash();pub._need(registered.get('capsule')=={'sha256':before[0],'size_bytes':before[1]},_SOURCE_CODE)
        needs_key=bool(media_held)or capsule_held is not None
        pub._need(metadata['requires_existing_media_key']==bool(media_held),_SOURCE_CODE)
        pub._need(not needs_key or(type(service.data_encryption_key)is bytes and len(service.data_encryption_key)==32),_SOURCE_CODE)
    if work is not None:
        original_work=work._value();pub._need(original_work['binding']['source_sha256']==expected_bundle,_SOURCE_CODE)
    def verify_original():
        pub._need(held.hash()==(db_sha,db_size)and not any(os.path.lexists(Path(str(source_path)+suffix))for suffix in('-wal','-shm','-journal')),'RESTORE_PUBLICATION_PREPARED_CHANGED')
        if meta_held is None:pub._need(not os.path.lexists(meta_path),'RESTORE_PUBLICATION_PREPARED_CHANGED')
        else:pub._need(meta_held.hash()==(meta_record['sha256'],meta_record['size_bytes']),'RESTORE_PUBLICATION_PREPARED_CHANGED')
        held.check()
        if meta_held is not None:meta_held.check()
        verified=[]
        for item in metadata['files']if metadata is not None else():
            original=media_held[item['relative_path']]
            pub._need(original.hash()==(item['sha256'],item['size_bytes']),_SOURCE_CODE)
            verified.append({'relative_path':item['relative_path'],'sha256':item['sha256'],'size_bytes':item['size_bytes']})
        if capsule_held is not None:
            before=capsule_held.hash();pub._need(metadata['registered']['capsule']=={'sha256':before[0],'size_bytes':before[1]},_SOURCE_CODE)
        if metadata is None:actual_bundle=db_sha
        elif metadata['format']==reg.FULL:
            # Reused contract/namespace validator performs no SQLite. Its raw
            # records must equal these held originals; copying never reopens a
            # source name. An empty/extra capsule is rejected by this validator.
            files=reg.full_files(source_path,metadata)
            rows=[{'archive_path':'database.db',**db_record}]
            rows.extend({'archive_path':'database.media/'+r['relative_path'],'sha256':r['sha256'],'size_bytes':r['size_bytes']}for r in verified)
            if capsule_held is not None:rows.append({'archive_path':'database.registered.enc',**metadata['registered']['capsule']})
            pub._need(files==rows,_SOURCE_CODE)
            actual_bundle=reg.sha(reg.canonical({'format':reg.FULL,'contract':meta_record,'files':rows}))
        else:
            pub._need(set(metadata)=={'format','files','missing_media_references','requires_existing_media_key'}
                and metadata['requires_existing_media_key']==bool(verified),_SOURCE_CODE)
            actual_bundle=reg.sha(reg.canonical({'database_sha256':db_sha,'files':verified,'missing_media_references':0}))
        pub._need(actual_bundle==expected_bundle,_SOURCE_CODE)
        held.check()
        for original in media_held.values():original.check()
        if capsule_held is not None:capsule_held.check()
        if meta_held is not None:meta_held.check()
    verify_original()
    pub._need((held.copy(cold)if work is None else work.copy_held(held,cold))==(db_sha,db_size),'RESTORE_PUBLICATION_PREPARED_CHANGED')
    verify_original()
    # These are authenticated source data/held readers, never publication or
    # deletion rights. The original DB-only admission remains until a separate
    # original live asset seal is supplied to the publisher.
    verify_original.metadata=metadata;verify_original.media_held=media_held
    verify_original.capsule_held=capsule_held;verify_original.database_sha256=db_sha
    if work is not None:
        pub._need(type(stack)is _SourceHandles and stack.owner is work.owner and stack.active,_SOURCE_CODE)
        _SOURCE_BUNDLES[verify_original]=SimpleNamespace(service=service,work=work,stack=stack,
            source_path=source_path,metadata=metadata,media_held=media_held,capsule_held=capsule_held,database_sha256=db_sha,
            bundle_sha256=expected_bundle)
    return verify_original

def original_source_bundle(service,work,verify):
    pub._need(verify in _SOURCE_BUNDLES,_SOURCE_CODE);original=_SOURCE_BUNDLES[verify]
    pub._need(original.service is service and original.work is work and original.stack.active
        and original.stack.owner is work.owner,_SOURCE_CODE);pub._owner(work.owner)
    pub._need(work._value()['binding']['source_sha256']==original.bundle_sha256,_SOURCE_CODE)
    verify();return original

def publish_local_restore(service,source_path,backup_id,request_id,bundle_sha,runtime_state,current_version):
    pub._need(type(request_id)is str,'RESTORE_PUBLICATION_INTENT_REQUIRED')
    with pub.publication_owner(service.database)as owner:
        work=ownership.begin(owner,request_id)
        with _SourceHandles(owner)as source_stack:
            cold=work.work/'authenticated-cold.db'
            verify_original=_authenticated_raw_copy(service,source_path,cold,bundle_sha,source_stack,work)
            recovery=_publish_verified_bundle(service,owner,work,verify_original,runtime_state,current_version,
                kind='restore',summary={'backup_id':backup_id})
        return {'backup_id':backup_id,'status':'restored','integrity_status':'ok',**_protection_result(recovery)}


def _protection_result(recovery):
    from .restore_incomplete_protection import public_result
    return public_result(recovery)

def _publish_verified_bundle(service,owner,work,verify_original,runtime_state,current_version,*,kind,summary):
    """One original R6 journal/publisher for a validated local or portable source."""
    pub._owner(owner);pub._need(kind in{'restore','portable_restore'}and work.owner is owner,'RESTORE_PUBLICATION_IDENTITY_REQUIRED')
    original_source_bundle(service,work,verify_original)
    from . import parent_restore_assets as assets
    with service.database.connect()as live:
        using_assets=assets.has_asset_references(live)
        row,_,_,_=pub._binding_facts(live,work._value()['binding']);pub._need(row['kind']==kind,'RESTORE_PUBLICATION_IDENTITY_REQUIRED')
    request_id=work.request_id;staged=Database(work.work/'database.db',runtime_mode=service.database.runtime_mode);work.attach(staged)
    cold=work.work/'authenticated-cold.db'
    from .privacy_lifecycle import InvalidBackupError
    with pub._cold(cold,owner=owner)as source:
        service._validate_restore_scope(source);using_assets=using_assets or assets.has_asset_references(source)
        if not using_assets:pub._db_only(source)
        pub._need(source.execute('PRAGMA integrity_check').fetchone()[0]=='ok','RESTORE_PUBLICATION_PREPARED_REQUIRED')
        if source.execute('PRAGMA foreign_key_check').fetchone()is not None:
            raise InvalidBackupError('备份中的数据关联不完整，已拒绝替换当前数据。')
        actual_version=source.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
        pub._need(actual_version<=current_version,'RESTORE_PUBLICATION_PREPARED_REQUIRED')
    if not using_assets:service._require_registered_restore_migration(cold,owner=owner)
    seal=assets.prepare_original_assets(service,work,verify_original)if using_assets else None
    protection=None
    try:recovery=service.create_backup(prune=False,reason='restore_recovery_point')
    except InvalidBackupError as error:
        if str(error)!='MISSING_REGISTERED_FILE':raise
        from .restore_incomplete_protection import Protection,STATUS
        protection=Protection(service,owner,work,verify_original,seal)
        recovery={'backup_id':None,'restore_protection':STATUS}
    prepared=work.prepared
    try:
        with pub.publication_owner(staged):
            service._prepare_database_snapshot(cold,staged,pause_restored_observation=True)
            verify_original()
            if seal is not None:assets.apply_original_asset_projection(seal,staged)
            if protection is not None:protection.capture()
            with staged.connect()as connection:
                connection.execute('BEGIN IMMEDIATE')
                if runtime_state:
                    connection.execute('UPDATE runtime_state SET clean_shutdown=?,run_id=?,started_at=?,ended_at=? WHERE singleton_id=1',tuple(runtime_state))
                if recovery['backup_id'] is not None:connection.execute('''INSERT OR IGNORE INTO backup_manifest(backup_id,path,database_version,size_bytes,sha256,integrity_status,reason,created_at)
                  VALUES(?,?,?,?,?,?,?,datetime('now'))''',(recovery['backup_id'],recovery['path'],current_version,recovery['size_bytes'],recovery['sha256'],recovery['integrity_status'],recovery['reason']))
                from .repository import utc_now
                audit=service._audit(connection,'parent',{'restore':'backup_restored','portable_restore':'portable_backup_restored'}[kind],None,{**summary,**({'recovery_point_backup_id':recovery['backup_id']}if recovery['backup_id'] is not None else{}),'privacy_operation_request_id':request_id},utc_now())
                record_restore(connection,request_id,audit)
                if protection is not None:
                    service._audit(connection,'parent','restore_database_protection',None,{'status':protection.record['kind'],'privacy_operation_request_id':request_id,'database_sha256':protection.record['database']['sha256'],'full_undo_available':False},utc_now())
                if seal is not None:assets.adopt_original_assets(seal,staged,connection)
                connection.commit()
            with staged.connect()as source,pub._sql(prepared,owner=owner,timeout=0)as destination:
                def progress(status,remaining,total):pub._need(status not in(5,6),'RESTORE_PUBLICATION_BUSY')
                source.backup(destination,pages=256,progress=progress)
                pub._need(destination.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete','RESTORE_PUBLICATION_PREPARED_REQUIRED')
        if protection is not None:protection.before_publication()
        pub.prepare_publication(owner,prepared,request_id,asset_seal=seal)
        verify_original()
        pub.publish_prepared_database(owner)
    finally:
        if protection is not None:protection.failed()
    return recovery


def startup_state(database):
    with pub.publication_owner(database)as owner:
        result=pub.reconcile_publication(owner)
        if result['state']in{'COMPLETE','ABORTED_NOT_APPLIED'}and ownership.current(owner)is None:
            pub.recover_before_initialize(database)
            return {'state':'NO_ACTIVE_JOURNAL'}
        return result

def failed_before_publication(database,request_id):
    with pub.publication_owner(database)as owner:
        if pub.reconcile_publication(owner)['state']=='NO_ACTIVE_JOURNAL':
            PrivacyOperationReceipts(database).failed(request_id)

def finish_original_tails(database,owner,metadata,clear=None):
    result=pub.original_publication_metadata(owner,metadata)
    pub._need(result['state']in{'DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS','COMPLETE'},'RESTORE_PUBLICATION_ORIGINAL_TAILS_PENDING')
    if clear is not None:clear()
    receipts=PrivacyOperationReceipts(database)
    receipt=receipts.read(metadata['request_id'])
    if receipt['status']!='committed':receipt=receipts.finish(metadata['request_id'])
    pub._fault('after_original_receipt_finish')
    ownership.cleanup(owner,metadata)
    with database.connect()as connection:
        connection.execute('BEGIN IMMEDIATE')
        from .parent_restore_parent import is_metadata,finish as parent_finish
        if is_metadata(metadata):
            _,_,authority,original=pub._metadata_facts(connection,metadata)
            status=pub._authority_status(authority)
            pub._need(status in{'claimed','finished'},'RESTORE_PARENT_AUTHORITY_UNCONFIRMED')
            if status=='claimed':parent_finish(connection,metadata,'FINISHED')
        else:
            status=native.read(connection,metadata)['status']
            pub._need(status in{'claimed','finished'},'RESTORE_PUBLICATION_NATIVE_PERMIT_REQUIRED')
            if status=='claimed':native.finish(connection,metadata,'finished')
        connection.commit()
    pub._fault('after_original_native_finish')
    ownership.finish(owner,metadata)
    pub.complete_publication(owner)
    return receipt

def recover_original(database,metadata,clear=None):
    pub._need(metadata.get('kind')in{'restore','portable_restore'},'RESTORE_PUBLICATION_NATIVE_PERMIT_REQUIRED')
    with pub.publication_owner(database,timeout=0)as owner:
        pub._need(PrivacyOperationReceipts(database).read(metadata['request_id'])['kind']==metadata['kind'],'RESTORE_PUBLICATION_NATIVE_PERMIT_REQUIRED')
        result=pub.original_publication_metadata(owner,metadata)
        if result['state']in{'UNAPPLIED','ABORTED_NOT_APPLIED'}:pub.abort_unapplied_publication(owner,metadata)
        else:finish_original_tails(database,owner,metadata,clear)
        with database.connect()as connection:return native.read(connection,metadata)


class OriginalRestoreInterrupted(RuntimeError):
    """An unexpected error with an original, still UNAPPLIED native operation."""


def original_unapplied_interruption(owner,request_id):
    """Read original facts only; never acquire/mint an owner or settle an operation."""
    if type(request_id)is not str:return False
    pub._owner(owner)
    state=pub.reconcile_publication(owner)
    if state.get('state')!='UNAPPLIED'or state.get('request_id')!=request_id:return False
    value=ownership.current(owner)
    if value is None or value['state']!='ACTIVE'or value['request_id']!=request_id:return False
    binding=value['binding']
    if pub._is_parent_binding(binding):return False
    row,generation,permit,metadata=ownership._facts(owner,value)
    return (row['request_id']==request_id and row['kind']in{'restore','portable_restore'}
        and row['kind']==metadata['kind']
        and row['state']=='processing'and row['finalized']==0
        and row['reference_id']is None and row['result_generation']is None
        and row['origin_generation']==generation==binding['origin_generation']
        and row['source_sha256']==binding['source_sha256']
        and row['request_sha256']==binding['request_sha256']
        and row['cleanup_manifest_sha256']==binding['cleanup_manifest_sha256']
        and pub._authority_status(permit)=='claimed'
        and metadata['request_id']==request_id and pub._metadata_matches(binding,metadata))
