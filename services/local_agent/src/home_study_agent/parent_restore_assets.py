"""Original live asset ownership. Backup rows never confer publish/delete rights.

Schema installation is part of Database's original owned upgrade transaction.
No arbitrary path/root map or public prepared-result object is an authority.
"""
from .backup_local_io import io_path, io_resolve, io_walk, io_glob, io_mkdtemp, io_temporary_directory
from pathlib import Path
from contextlib import contextmanager
import base64,hashlib,json,os,sqlite3,stat,sys,weakref,zipfile
from types import SimpleNamespace
from uuid import uuid4

from . import restore_publication as pub

SCHEMA_VERSION=99
MAX_FILES=100000
MAX_MEMBERS=MAX_FILES*65
GENERATION='restore_asset_generation'
MEMBER='restore_asset_member'
ERASURE='restore_asset_erasure'
_CODE='RESTORE_ASSET_OWNERSHIP_UNCONFIRMED'
_IMPORTED_DATABASES=weakref.WeakKeyDictionary()

GENERATION_SQL="""CREATE TABLE restore_asset_generation(
 generation_id TEXT PRIMARY KEY CHECK(typeof(generation_id)='text'AND length(generation_id)=32 AND generation_id NOT GLOB '*[^0-9a-f]*'),
 request_id TEXT NOT NULL CHECK(typeof(request_id)='text'AND length(CAST(request_id AS BLOB))BETWEEN 1 AND 128),
 native_nonce TEXT NOT NULL CHECK(typeof(native_nonce)='text'AND length(CAST(native_nonce AS BLOB))BETWEEN 1 AND 128),
 binding_sha256 TEXT NOT NULL CHECK(typeof(binding_sha256)='text'AND length(binding_sha256)=64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),
 source_sha256 TEXT NOT NULL CHECK(typeof(source_sha256)='text'AND length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
 root_name TEXT NOT NULL UNIQUE CHECK(root_name='restore-generation-'||generation_id),
 media_identity_json TEXT NOT NULL CHECK(typeof(media_identity_json)='text'AND length(CAST(media_identity_json AS BLOB))BETWEEN 1 AND 256),
 root_identity_json TEXT CHECK(root_identity_json IS NULL OR(typeof(root_identity_json)='text'AND length(CAST(root_identity_json AS BLOB))BETWEEN 1 AND 256)),
 manifest_sha256 TEXT CHECK(manifest_sha256 IS NULL OR(typeof(manifest_sha256)='text'AND length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*')),
 projection_sha256 TEXT CHECK(projection_sha256 IS NULL OR(typeof(projection_sha256)='text'AND length(projection_sha256)=64 AND projection_sha256 NOT GLOB '*[^0-9a-f]*')),
 expected_files INTEGER NOT NULL CHECK(typeof(expected_files)='integer'AND expected_files BETWEEN 0 AND 100000),
 total_bytes INTEGER NOT NULL CHECK(typeof(total_bytes)='integer'AND total_bytes BETWEEN 0 AND 9007199254740991),
 state TEXT NOT NULL CHECK(state IN('ALLOCATING','BUILDING','SEALED','ADOPTED','RETIRING','CLEANED')),
 CHECK(state NOT IN('BUILDING','SEALED','ADOPTED')OR root_identity_json IS NOT NULL),
 CHECK(state NOT IN('SEALED','ADOPTED')OR(manifest_sha256 IS NOT NULL AND projection_sha256 IS NOT NULL))
)"""
MEMBER_SQL="""CREATE TABLE restore_asset_member(
 generation_id TEXT NOT NULL REFERENCES restore_asset_generation(generation_id),
 member_id TEXT NOT NULL CHECK(typeof(member_id)='text'AND length(member_id)=64 AND member_id NOT GLOB '*[^0-9a-f]*'),
 relative_name TEXT NOT NULL CHECK(typeof(relative_name)='text'AND length(CAST(relative_name AS BLOB))BETWEEN 1 AND 1024),
 path_key TEXT NOT NULL CHECK(typeof(path_key)='text'AND length(path_key)=64 AND path_key NOT GLOB '*[^0-9a-f]*'),
 kind TEXT NOT NULL CHECK(kind IN('file','directory')),
 purpose TEXT NOT NULL CHECK(purpose IN('managed','registered','metadata','directory')),
 child_id TEXT CHECK(child_id IS NULL OR(typeof(child_id)='text'AND length(CAST(child_id AS BLOB))BETWEEN 1 AND 128)),
 reference_json TEXT NOT NULL CHECK(typeof(reference_json)='text'AND length(CAST(reference_json AS BLOB))BETWEEN 1 AND 4096),
 identity_json TEXT CHECK(identity_json IS NULL OR(typeof(identity_json)='text'AND length(CAST(identity_json AS BLOB))BETWEEN 1 AND 256)),
 expected_sha256 TEXT CHECK(expected_sha256 IS NULL OR(typeof(expected_sha256)='text'AND length(expected_sha256)=64 AND expected_sha256 NOT GLOB '*[^0-9a-f]*')),
 expected_bytes INTEGER NOT NULL CHECK(typeof(expected_bytes)='integer'AND expected_bytes BETWEEN 0 AND 9007199254740991),
 move_target TEXT CHECK(move_target IS NULL OR(typeof(move_target)='text'AND length(CAST(move_target AS BLOB))BETWEEN 1 AND 1024)),
 state TEXT NOT NULL CHECK(state IN('ALLOCATING','OWNED','WRITING','READY','MOVING','REMOVING','REMOVED')),
 PRIMARY KEY(generation_id,member_id),UNIQUE(generation_id,path_key),
 CHECK(state NOT IN('OWNED','WRITING','READY','MOVING','REMOVING')OR identity_json IS NOT NULL),
 CHECK((kind='directory'AND purpose='directory'AND expected_sha256 IS NULL AND expected_bytes=0)OR(kind='file'AND purpose!='directory'AND expected_sha256 IS NOT NULL)),
 CHECK((state='MOVING'AND move_target IS NOT NULL)OR(state!='MOVING'AND move_target IS NULL))
)"""

GENERATION_TRIGGER="""CREATE TRIGGER restore_asset_generation_immutable BEFORE UPDATE ON restore_asset_generation
WHEN NEW.generation_id IS NOT OLD.generation_id OR NEW.request_id IS NOT OLD.request_id OR NEW.native_nonce IS NOT OLD.native_nonce
 OR NEW.binding_sha256 IS NOT OLD.binding_sha256 OR NEW.source_sha256 IS NOT OLD.source_sha256 OR NEW.root_name IS NOT OLD.root_name
 OR NEW.media_identity_json IS NOT OLD.media_identity_json OR NEW.expected_files IS NOT OLD.expected_files OR NEW.total_bytes IS NOT OLD.total_bytes
 OR(NEW.root_identity_json IS NOT OLD.root_identity_json AND NOT(OLD.state='ALLOCATING'AND OLD.root_identity_json IS NULL AND NEW.state='BUILDING'AND NEW.root_identity_json IS NOT NULL))
 OR(NEW.manifest_sha256 IS NOT OLD.manifest_sha256 AND NOT(OLD.state='BUILDING'AND NEW.state='SEALED'AND OLD.manifest_sha256 IS NULL))
 OR(NEW.projection_sha256 IS NOT OLD.projection_sha256 AND NOT(OLD.state='BUILDING'AND NEW.state='SEALED'AND OLD.projection_sha256 IS NULL))
 OR NOT((OLD.state='ALLOCATING'AND NEW.state IN('BUILDING','CLEANED'))OR(OLD.state='BUILDING'AND NEW.state IN('SEALED','RETIRING'))OR(OLD.state='SEALED'AND NEW.state IN('ADOPTED','RETIRING'))OR(OLD.state='ADOPTED'AND NEW.state='RETIRING')OR(OLD.state='RETIRING'AND NEW.state='CLEANED'))
BEGIN SELECT RAISE(ABORT,'asset ownership is immutable');END"""
MEMBER_TRIGGER="""CREATE TRIGGER restore_asset_member_immutable BEFORE UPDATE ON restore_asset_member
WHEN NEW.generation_id IS NOT OLD.generation_id OR NEW.member_id IS NOT OLD.member_id OR NEW.kind IS NOT OLD.kind OR NEW.purpose IS NOT OLD.purpose
 OR NEW.child_id IS NOT OLD.child_id OR NEW.reference_json IS NOT OLD.reference_json OR NEW.expected_sha256 IS NOT OLD.expected_sha256 OR NEW.expected_bytes IS NOT OLD.expected_bytes
 OR(NEW.identity_json IS NOT OLD.identity_json AND NOT(OLD.state='ALLOCATING'AND OLD.identity_json IS NULL AND NEW.state='OWNED'AND NEW.identity_json IS NOT NULL))
 OR((NEW.relative_name IS NOT OLD.relative_name OR NEW.path_key IS NOT OLD.path_key)AND NOT(OLD.state='MOVING'AND NEW.state='READY'AND NEW.relative_name=OLD.move_target))
 OR NOT((OLD.state='ALLOCATING'AND NEW.state IN('OWNED','REMOVED'))OR(OLD.state='OWNED'AND NEW.state IN('WRITING','REMOVING'))OR(OLD.state='WRITING'AND NEW.state IN('READY','REMOVING'))OR(OLD.state='READY'AND NEW.state IN('MOVING','REMOVING'))OR(OLD.state='MOVING'AND NEW.state IN('READY','REMOVING'))OR(OLD.state='REMOVING'AND NEW.state='REMOVED'))
BEGIN SELECT RAISE(ABORT,'asset member is immutable');END"""
GENERATION_DELETE="CREATE TRIGGER restore_asset_generation_no_delete BEFORE DELETE ON restore_asset_generation BEGIN SELECT RAISE(ABORT,'asset ownership history is required');END"
MEMBER_DELETE="CREATE TRIGGER restore_asset_member_no_delete BEFORE DELETE ON restore_asset_member BEGIN SELECT RAISE(ABORT,'asset member history is required');END"
OBJECTS=(('table',GENERATION,GENERATION_SQL),('table',MEMBER,MEMBER_SQL),
    ('trigger','restore_asset_generation_immutable',GENERATION_TRIGGER),('trigger','restore_asset_member_immutable',MEMBER_TRIGGER),
    ('trigger','restore_asset_generation_no_delete',GENERATION_DELETE),('trigger','restore_asset_member_no_delete',MEMBER_DELETE))
OBJECTS_85=OBJECTS
ERASURE_SQL="""CREATE TABLE restore_asset_erasure(
 generation_id TEXT NOT NULL,
 member_id TEXT NOT NULL,
 request_id TEXT NOT NULL CHECK(typeof(request_id)='text'AND length(CAST(request_id AS BLOB))BETWEEN 1 AND 128),
 native_nonce TEXT NOT NULL CHECK(typeof(native_nonce)='text'AND length(CAST(native_nonce AS BLOB))BETWEEN 1 AND 128),
 deletion_id TEXT NOT NULL CHECK(typeof(deletion_id)='text'AND length(CAST(deletion_id AS BLOB))BETWEEN 1 AND 128),
 child_id TEXT NOT NULL CHECK(typeof(child_id)='text'AND length(CAST(child_id AS BLOB))BETWEEN 1 AND 128),
 scope TEXT NOT NULL CHECK(scope IN('media','all_history')),
 origin_generation TEXT NOT NULL CHECK(typeof(origin_generation)='text'AND length(CAST(origin_generation AS BLOB))BETWEEN 1 AND 128),
 request_sha256 TEXT NOT NULL CHECK(typeof(request_sha256)='text'AND length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
 cleanup_manifest_sha256 TEXT NOT NULL CHECK(typeof(cleanup_manifest_sha256)='text'AND length(cleanup_manifest_sha256)=64 AND cleanup_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
 native_metadata_sha256 TEXT NOT NULL CHECK(typeof(native_metadata_sha256)='text'AND length(native_metadata_sha256)=64 AND native_metadata_sha256 NOT GLOB '*[^0-9a-f]*'),
 member_sha256 TEXT NOT NULL CHECK(typeof(member_sha256)='text'AND length(member_sha256)=64 AND member_sha256 NOT GLOB '*[^0-9a-f]*'),
 fingerprint_json TEXT NOT NULL CHECK(typeof(fingerprint_json)='text'AND length(CAST(fingerprint_json AS BLOB))BETWEEN 1 AND 4096),
 state TEXT NOT NULL CHECK(state IN('INTENT','ERASED')),
 PRIMARY KEY(generation_id,member_id),
 FOREIGN KEY(generation_id,member_id)REFERENCES restore_asset_member(generation_id,member_id)
)"""
ERASURE_TRIGGER="""CREATE TRIGGER restore_asset_erasure_immutable BEFORE UPDATE ON restore_asset_erasure
WHEN NEW.generation_id IS NOT OLD.generation_id OR NEW.member_id IS NOT OLD.member_id
 OR NEW.request_id IS NOT OLD.request_id OR NEW.native_nonce IS NOT OLD.native_nonce OR NEW.deletion_id IS NOT OLD.deletion_id
 OR NEW.child_id IS NOT OLD.child_id OR NEW.scope IS NOT OLD.scope OR NEW.origin_generation IS NOT OLD.origin_generation
 OR NEW.request_sha256 IS NOT OLD.request_sha256 OR NEW.cleanup_manifest_sha256 IS NOT OLD.cleanup_manifest_sha256
 OR NEW.native_metadata_sha256 IS NOT OLD.native_metadata_sha256 OR NEW.member_sha256 IS NOT OLD.member_sha256
 OR NEW.fingerprint_json IS NOT OLD.fingerprint_json OR NOT(OLD.state='INTENT'AND NEW.state='ERASED')
BEGIN SELECT RAISE(ABORT,'asset erasure preimage is immutable');END"""
ERASURE_DELETE="CREATE TRIGGER restore_asset_erasure_no_delete BEFORE DELETE ON restore_asset_erasure BEGIN SELECT RAISE(ABORT,'asset erasure history is required');END"
OBJECTS_86=(('table',ERASURE,ERASURE_SQL),('trigger','restore_asset_erasure_immutable',ERASURE_TRIGGER),
    ('trigger','restore_asset_erasure_no_delete',ERASURE_DELETE))
OBJECTS+=OBJECTS_86
OBJECTS_THROUGH_86=OBJECTS
from .parent_restore_privacy import OBJECTS as OBJECTS_87
OBJECTS+=OBJECTS_87
CONTROL_TABLES=(GENERATION,MEMBER,ERASURE,'restore_asset_parent_authority','restore_asset_parent_erasure')
LEGACY_OBJECTS_87=OBJECTS
LEGACY_CONTROL_TABLES=CONTROL_TABLES
from .parent_restore_parent import GENERATION_SQL_88,AUTHORITY_OBJECTS as PARENT_RESTORE_OBJECTS
GENERATION_TRIGGER_88=GENERATION_TRIGGER.replace('OR NEW.native_nonce IS NOT OLD.native_nonce',
    'OR NEW.native_nonce IS NOT OLD.native_nonce OR NEW.authority_kind IS NOT OLD.authority_kind OR NEW.parent_authority_id IS NOT OLD.parent_authority_id')
OBJECTS_88=PARENT_RESTORE_OBJECTS+tuple((kind,name,GENERATION_SQL_88 if name==GENERATION else GENERATION_TRIGGER_88 if name=='restore_asset_generation_immutable'else sql)for kind,name,sql in OBJECTS)
CONTROL_TABLES=LEGACY_CONTROL_TABLES+('restore_parent_authority',)
OBJECTS=OBJECTS_88

def _owner_connection(database,connection):
    path=pub._path(database.path);owner=pub._owner(pub._gate(path).owner)
    pub._need(owner.path==path and isinstance(connection,sqlite3.Connection),_CODE)
    pub._need(database._original_connections.get(id(connection))is connection
        and owner.gate.active.get(pub._actor_key(pub._actor()),0)>0,_CODE)
    rows=connection.execute('PRAGMA database_list').fetchall()
    main=[r for r in rows if r[1]=='main']
    pub._need(len(main)==1 and pub._path(Path(main[0][2]))==path
        and all(r[1]=='main'or(r[1]=='temp'and not r[2])for r in rows),_CODE)
    return owner

def _validate_objects(connection,objects,tables):
    names=tuple(item[1]for item in objects)
    rows=[tuple(r)for r in connection.execute('SELECT type,name,sql FROM sqlite_master WHERE name IN('+','.join('?'for _ in names)+')',names)]
    pub._need(set(rows)==set(objects)and len(rows)==len(objects),_CODE)
    triggers={r[0]for r in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'AND tbl_name IN("+','.join('?'for _ in tables)+')',tables)}
    pub._need(triggers=={name for kind,name,_ in objects if kind=='trigger'},_CODE)

def validate_schema(connection):
    source=connection.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
    from .pet_multipa_execution_receipt import validate_schema as validate_cpu_execution_receipts
    validate_cpu_execution_receipts(connection,source_version=source)
    if source>=90:
        from .pet_speaking_candidate_jobs import validate_schema as validate_speaking_candidate_jobs
        validate_speaking_candidate_jobs(connection,source_version=source)
    source=connection.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
    if source>=89:
        from .pet_speaking_candidate_store import validate_schema as validate_speaking_model_candidates
        validate_speaking_model_candidates(connection)
    # Exact closed SQL shapes only; this structural distinction confers no
    # factory/work/publication authority and never dispatches from nonce NULL.
    row=connection.execute("SELECT sql FROM sqlite_master WHERE name='restore_asset_generation'").fetchone()
    if row is not None and row[0]==GENERATION_SQL_88:
        _validate_objects(connection,OBJECTS_88,CONTROL_TABLES)
    else:_validate_objects(connection,LEGACY_OBJECTS_87,LEGACY_CONTROL_TABLES)

def ensure_schema(database,connection):
    """Only original Database initialization owns install; never repair loss."""
    owner=_owner_connection(database,connection)
    source=connection.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
    from .pet_multipa_execution_receipt import validate_schema as validate_cpu_execution_receipts
    validate_cpu_execution_receipts(connection,source_version=source)
    if source>=90:
        from .pet_speaking_candidate_jobs import validate_schema as validate_speaking_candidate_jobs
        validate_speaking_candidate_jobs(connection,source_version=source)
    existing=connection.execute('SELECT 1 FROM sqlite_master WHERE name IN(?,?)LIMIT 1',(GENERATION,MEMBER)).fetchone()
    if source<85 and existing is None:
        pub._need(not connection.execute('SELECT 1 FROM sqlite_master WHERE lower(name)IN('+','.join('?'for _ in CONTROL_TABLES)+')LIMIT 1',CONTROL_TABLES).fetchone(),_CODE)
        for _,_,sql in OBJECTS_88:connection.execute(sql)
        _validate_objects(connection,OBJECTS_88,CONTROL_TABLES);return
    if source>=88:
        _validate_objects(connection,OBJECTS_88,CONTROL_TABLES)
        if source>=89:
            from .pet_speaking_candidate_store import validate_schema as validate_speaking_model_candidates
            validate_speaking_model_candidates(connection)
        return
    # Imported original work installs the current empty schema before the
    # ordinary source-version upgrade; the original opaque ticket is required.
    actual=connection.execute("SELECT sql FROM sqlite_master WHERE name='restore_asset_generation'").fetchone()
    if actual is not None and actual[0]==GENERATION_SQL_88:
        pub._need(_import_ticket(database)is not None and not connection.execute('SELECT 1 FROM restore_asset_generation LIMIT 1').fetchone(),_CODE)
        _validate_objects(connection,OBJECTS_88,CONTROL_TABLES);return
    if source<85:
        if existing is None:
            for _,_,sql in OBJECTS_85:connection.execute(sql)
        else:
            ticket=_import_ticket(database)
            pub._need(ticket is not None and ticket['source_version']<=source<85,_CODE)
            pub._need(not connection.execute('SELECT 1 FROM restore_asset_generation LIMIT 1').fetchone()
                and not connection.execute('SELECT 1 FROM restore_asset_member LIMIT 1').fetchone(),_CODE)
    _validate_objects(connection,OBJECTS_85,(GENERATION,MEMBER))
    if source<86:
        present=connection.execute('SELECT 1 FROM sqlite_master WHERE lower(name)IN(?,?,?)LIMIT 1',tuple(x[1]for x in OBJECTS_86)).fetchone()
        if present is None:
            for _,_,sql in OBJECTS_86:connection.execute(sql)
        else:
            pub._need(_import_ticket(database)is not None and not connection.execute('SELECT 1 FROM restore_asset_erasure LIMIT 1').fetchone(),_CODE)
    _validate_objects(connection,OBJECTS_THROUGH_86,(GENERATION,MEMBER,ERASURE))
    if source<87:
        names=tuple(x[1]for x in OBJECTS_87)
        present=connection.execute('SELECT 1 FROM sqlite_master WHERE lower(name)IN('+','.join('?'for _ in names)+')LIMIT 1',names).fetchone()
        if present is None:
            for _,_,sql in OBJECTS_87:connection.execute(sql)
        else:
            pub._need(_import_ticket(database)is not None and not connection.execute('SELECT 1 FROM restore_asset_parent_authority LIMIT 1').fetchone()
                and not connection.execute('SELECT 1 FROM restore_asset_parent_erasure LIMIT 1').fetchone(),_CODE)
    _validate_objects(connection,LEGACY_OBJECTS_87,LEGACY_CONTROL_TABLES)
    if source<=87 and _import_ticket(database)is None:
        connection.row_factory=sqlite3.Row
        rows=connection.execute("SELECT *FROM restore_asset_generation WHERE state!='CLEANED'LIMIT ?",(MAX_FILES+1,)).fetchall()
        pub._need(len(rows)<=MAX_FILES,_CODE)
        for row in rows:
            g=dict(row);pub._need(g['state']=='ADOPTED'and _manifest_original(owner,connection,g)==g['manifest_sha256'],_CODE)
    from .parent_restore_parent import migrate_generation_87
    pub._need(connection.in_transaction,_CODE);migrate_generation_87(database,connection)
    _validate_objects(connection,OBJECTS_88,CONTROL_TABLES)


def _import_ticket(staged):
    ticket=_IMPORTED_DATABASES.get(staged)
    if ticket is None:return None
    work=ticket['work'];pub._owner(ticket['owner'])
    pub._need(staged._restore_work_capability is work and work.owner is ticket['owner']
        and staged.path==work.work/'database.db',_CODE);work._value()
    return ticket


def discard_imported_schema(staged):
    """Drop backup controls only in this actual original R6 staging core.

    No source row/schema/marker is consulted for cleanup or adoption rights.
    The original source version remains intact for the normal upgrade contract.
    Non-R6 legacy imports cannot import any asset authority at all.
    """
    from . import parent_restore_ownership as ownership
    work=ownership.staged_capability(staged)
    if work is None:
        with pub._sql(staged.path,readonly=True,timeout=0)as c:
            present=c.execute('SELECT 1 FROM sqlite_master WHERE lower(name)IN('+','.join('?'for _ in CONTROL_TABLES)+')LIMIT 1',CONTROL_TABLES).fetchone()
            if present:
                version=c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
                if version==85:
                    _validate_objects(c,OBJECTS_85,(GENERATION,MEMBER))
                    pub._need(not c.execute('SELECT 1 FROM sqlite_master WHERE lower(name)IN(?,?,?)LIMIT 1',tuple(x[1]for x in OBJECTS_86)).fetchone(),_CODE)
                elif version==86:
                    _validate_objects(c,OBJECTS_THROUGH_86,(GENERATION,MEMBER,ERASURE))
                    pub._need(not c.execute('SELECT 1 FROM sqlite_master WHERE lower(name)IN(?,?)LIMIT 1',LEGACY_CONTROL_TABLES[-2:]).fetchone(),_CODE)
                else:validate_schema(c)
                pub._need(not c.execute('SELECT 1 FROM restore_asset_generation LIMIT 1').fetchone()
                    and not c.execute('SELECT 1 FROM restore_asset_member LIMIT 1').fetchone()
                    and(version==85 or not c.execute('SELECT 1 FROM restore_asset_erasure LIMIT 1').fetchone())
                    and(version in{85,86}or(not c.execute('SELECT 1 FROM restore_asset_parent_authority LIMIT 1').fetchone()
                        and not c.execute('SELECT 1 FROM restore_asset_parent_erasure LIMIT 1').fetchone()))
                    and('restore_parent_authority'not in pub._tables(c)or not c.execute('SELECT 1 FROM restore_parent_authority LIMIT 1').fetchone()),_CODE)
        return
    owner=pub._owner(work.owner);pub._owner(pub._gate(pub._path(staged.path)).owner)
    pub._need(staged.path==work.work/'database.db',_CODE)
    with pub._sql(staged.path,owner=owner,timeout=0)as c:
        c.execute('PRAGMA foreign_keys=OFF');c.execute('PRAGMA trusted_schema=OFF')
        pub._need(c.execute('PRAGMA foreign_keys').fetchone()[0]==0,_CODE)
        source_version=c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
        pub._need(type(source_version)is int and source_version<=SCHEMA_VERSION,_CODE)
        c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
        # DROP operates only on closed fixed control names. Foreign-key actions
        # are disabled here so a forged source FK cannot mutate family rows.
        for kind,name in c.execute('SELECT type,name FROM sqlite_master WHERE lower(name)IN('+','.join('?'for _ in CONTROL_TABLES)+')',CONTROL_TABLES).fetchall():
            pub._need(kind in{'table','view'},_CODE)
            c.execute('DROP '+kind.upper()+' "'+name.replace('"','""')+'"')
        # Any same-name standalone forged trigger/index must not survive.
        for kind,name,_ in OBJECTS:
            if kind!='table':
                rows=c.execute('SELECT type,name FROM sqlite_master WHERE lower(name)=?',(name,)).fetchall()
                for existing_kind,existing_name in rows:
                    pub._need(existing_kind in{'trigger','index','view','table'},_CODE)
                    c.execute('DROP '+existing_kind.upper()+' "'+existing_name.replace('"','""')+'"')
        for _,_,sql in OBJECTS:c.execute(sql)
        validate_schema(c);c.commit()
    _IMPORTED_DATABASES[staged]={'owner':owner,'work':work,'source_version':source_version}


def preserve_original_live(live_database,staged_database,live,staged):
    """Borrowed transaction: only original live rows cross the DB replacement."""
    owner=_owner_connection(live_database,live);_owner_connection(staged_database,staged)
    ticket=_import_ticket(staged_database);pub._need(ticket is not None and ticket['owner']is owner,_CODE)
    pub._need(staged.in_transaction,_CODE);validate_schema(live);validate_schema(staged)
    pub._need(not staged.execute('SELECT 1 FROM restore_asset_generation LIMIT 1').fetchone()
        and not staged.execute('SELECT 1 FROM restore_asset_member LIMIT 1').fetchone()
        and not staged.execute('SELECT 1 FROM restore_asset_erasure LIMIT 1').fetchone()
        and not staged.execute('SELECT 1 FROM restore_asset_parent_authority LIMIT 1').fetchone()
        and not staged.execute('SELECT 1 FROM restore_asset_parent_erasure LIMIT 1').fetchone(),_CODE)
    from . import parent_restore_ownership as ownership
    original=ownership._anchor(live);target=ownership._anchor(staged)
    pub._need(original is not None and original['state']=='ACTIVE'and target==original,_CODE)
    for table,limit in((GENERATION,MAX_FILES),(MEMBER,MAX_MEMBERS),(ERASURE,MAX_FILES),('restore_asset_parent_authority',MAX_FILES),('restore_asset_parent_erasure',MAX_FILES)):
        columns=[r[1]for r in live.execute('PRAGMA table_info('+table+')')]
        count=0
        for row in live.execute('SELECT *FROM '+table+' ORDER BY '+('authority_id'if table=='restore_asset_parent_authority'else'generation_id'if table==GENERATION else'generation_id,member_id')):
            count+=1;pub._need(count<=limit,_CODE)
            staged.execute('INSERT INTO '+table+'('+','.join(columns)+')VALUES('+','.join('?'for _ in columns)+')',tuple(row))

def assert_original_assets_before_restore(owner,c):
    """Reject unknown prior erasures before creating a new restore owner/work."""
    pub._owner(owner)
    if GENERATION not in pub._tables(c):return
    validate_schema(c);c.row_factory=sqlite3.Row
    pub._need(not c.execute("SELECT 1 FROM restore_asset_parent_authority WHERE state!='FINISHED'LIMIT 1").fetchone(),_CODE)
    rows=c.execute("SELECT *FROM restore_asset_generation WHERE state='ADOPTED'LIMIT ?",(MAX_FILES+1,)).fetchall();pub._need(len(rows)<=MAX_FILES,_CODE)
    for row in rows:
        generation=dict(row);pub._need(_manifest_original(owner,c,generation)==generation['manifest_sha256'],_CODE)


def _generation_query(metadata):
    from .parent_restore_parent import is_metadata
    return ("request_id=?AND authority_kind='PARENT_SESSION_RESTORE'AND parent_authority_id=?AND native_nonce IS NULL",
        (metadata['request_id'],metadata['authority_id']))if is_metadata(metadata)else("request_id=?AND native_nonce=?",(metadata['request_id'],metadata['nonce']))

def _generation_facts(c,g):
    if g.get('authority_kind')=='PARENT_SESSION_RESTORE':
        from .parent_restore_parent import facts
        return facts(c,g['request_id'],g['parent_authority_id'])
    return pub._facts(c,g['request_id'],g['native_nonce'])

def assert_assets_closed_before_native(owner,metadata):
    """Until verified asset publication exists, only cleaned copies may finish."""
    pub._owner(owner)
    with pub._cold(owner.path,owner=owner)as c:
        tables=pub._tables(c)
        if GENERATION not in tables and MEMBER not in tables:
            version=c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
            pub._need(version<89,_CODE);return  # Original asset-closure legacy floor, not latest schema.
        validate_schema(c)
        pub._need(not c.execute("SELECT 1 FROM restore_asset_generation WHERE state NOT IN('CLEANED','ADOPTED')LIMIT 1").fetchone(),_CODE)
        where,args=_generation_query(metadata)
        current=c.execute("SELECT generation_id FROM restore_asset_generation WHERE "+where+" AND state!='CLEANED'",args).fetchmany(2)
        if current:
            pub._need(len(current)==1,_CODE)
            j=pub._decode(pub._read(owner.parent,pub._names(owner.path)[1]))
            pub._need(pub._uses_assets(j)and j['binding']['request_id']==metadata['request_id']and pub._metadata_matches(j['binding'],metadata),_CODE)
            pub._need(pub._marker(c)==(metadata['request_id'],j['binding_sha256']),_CODE)
            validate_asset_publication(owner,c,j['binding'],committed=True)


# The public object contains no owner/path fields to copy or replace. A newly
# constructed/object.__new__ object has no enrollment and therefore no rights.
_CAPABILITIES=weakref.WeakKeyDictionary()

class _AssetCapability:
    __slots__=('__weakref__',)
    def __new__(cls):raise pub.RestorePublicationError(_CODE)
    def copy_held(self,relative,held):return _copy_held(self,relative,held)
    def copy_registered(self,relative,archive,logical):return _copy_registered(self,relative,archive,logical)
    def abort_unapplied(self):return _abort_unapplied(self)


def _digest(value):return hashlib.sha256(value).hexdigest()


def _identity(parent,private=True):
    parent.check()
    if os.name=='nt':return list(parent.chain[-1][2])
    info=os.fstat(parent.fd)
    pub._need(info.st_uid==os.getuid()and info.st_mode&(0o077 if private else 0o022)==0,_CODE)
    return [info.st_dev,info.st_ino]


@contextmanager
def _held_parent(owner,path,expected=None,*,private=True):
    """Retain every original close callback, including failed __enter__."""
    from .parent_restore_workflow import _SourceCloseCallbacks
    manager=pub._Parent(path);callbacks=_SourceCloseCallbacks(manager)
    try:parent=manager.__enter__()
    except BaseException:
        if not callbacks.all_confirmed():
            owner.gate.poisoned=True;owner.gate.retained.append(('restore_asset_parent_enter',manager,callbacks))
        raise
    try:
        identity=_identity(parent,private)
        pub._need(expected is None or identity==expected,_CODE)
        if os.name=='nt':
            # Only the existing pure native ACL reader is reused, never a
            # financial owner/policy/ledger. Safe inherited private DACLs are
            # accepted; all effective allow ACEs still need trusted SIDs.
            from .money_r4.money_files import _windows_kernel
            kernel,security=_windows_kernel();handle=kernel.guarded_open(path,directory=True)
            try:
                from .registered_asset_restore import _win_target_check
                _win_target_check(kernel,handle,path,identity=tuple(identity));security.check(handle)
                _win_target_check(kernel,handle,path,identity=tuple(identity))
            finally:
                try:kernel.close(handle)
                except BaseException:
                    owner.gate.poisoned=True;owner.gate.retained.append(('restore_asset_acl_handle',kernel,handle))
                    raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
        parent.check();yield parent;parent.check()
    finally:
        try:manager.__exit__(None,None,None)
        except BaseException:
            owner.gate.poisoned=True;owner.gate.retained.append(('restore_asset_parent_close',manager,callbacks))
            raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None




def _private_original_ancestor(parent):
    if os.name=='nt':return  # Original final data-root DACL is checked above.
    parent.check();below=[]
    for _,_,fd,identity in reversed(parent.chain):
        info=os.fstat(fd)
        pub._need((info.st_dev,info.st_ino)==identity and info.st_uid==os.getuid()and info.st_mode&0o022==0,_CODE)
        below.append(identity)
        if info.st_mode&0o077==0:return tuple(below)
    raise pub.RestorePublicationError(_CODE)


@contextmanager
def _held_media(owner,media,expected=None,data_expected=None):
    # Only this exact configured media child can use a readable existing
    # directory. The same original private data-root is held for the full
    # operation; every newly owned generation remains strictly private.
    pub._need(media.parent==owner.path.parent and media.name=='media',_CODE)
    with _held_parent(owner,media.parent,data_expected,private=False)as data:
        _private_original_ancestor(data)
        with _held_parent(owner,media,expected,private=False)as parent:
            data.check();yield parent,_identity(data,False);parent.check();data.check();_private_original_ancestor(data)



@contextmanager
def _held_original_media_base(service,work):
    """Only a new empty configured base; never a directory deletion right."""
    from . import parent_restore_ownership as ownership
    pub._need(type(work)is ownership.WorkCapability,_CODE)
    owner=pub._owner(work.owner);value=work._value()
    # An existing original intent is never promoted from a new namespace stat.
    if any(Path(e['relative']).name=='media-base-allocation.json'for e in value['members']['entries']):
        assert_original_media_base_closed(owner,value)
    media=pub._path(service.data_dir/'media')
    pub._need(pub._path(service.database.path)==owner.path and service.database._restore_sql_owner()is owner
        and media.parent==owner.path.parent and media.name=='media',_CODE)
    with _held_parent(owner,media.parent,private=False)as data:
        _private_original_ancestor(data);data_id=_identity(data,False)
        created=not os.path.lexists(io_path(media))
        intent=work.work/'media-base-allocation.json'
        if created:
            work.add_file(intent,family=False)
            fact={'schema':1,'phase':'INTENT','binding_sha256':_digest(pub._canonical(value['binding'])),
                'data_identity':data_id,'media_identity':None}
            work.metadata(intent,fact);data.check()
            if os.name=='nt':os.mkdir(io_path(media),0o700)
            else:os.mkdir('media',0o700,dir_fd=data.fd)
            pub._fault('after_restore_media_base_allocated_before_id')
        with _held_parent(owner,media,private=False)as parent:
            media_id=_identity(parent,False);data.check();parent.check();_private_original_ancestor(data)
            if created:
                parent.flush();data.flush();fact.update(phase='READY',media_identity=media_id);work.metadata(intent,fact)
            yield media_id,data_id
            data.check();parent.check();_private_original_ancestor(data)


def assert_original_media_base_closed(owner,value):
    """A crash before the original base ID/flush acknowledgement stays HOLD."""
    entries=[entry for entry in value['members']['entries']if Path(entry['relative']).name=='media-base-allocation.json']
    pub._need(len(entries)<=1,_CODE)
    if not entries:return
    entry=entries[0]
    if entry['deleting']or entry['removed']:return  # Original cleanup already durably admitted this member.
    from .registered_asset_snapshot import HeldFile,Policy
    from .parent_restore_workflow import _SourceHandles
    path=owner.path.parent/entry['relative']
    with _SourceHandles(owner)as stack:
        source=stack.enter_context(HeldFile(path.parent,path.name,Policy(1,64,1024,4096,4096,4096)))
        pub._need(list(source.before['identity'])==entry['identity']if os.name=='nt'else[source.before.st_dev,source.before.st_ino]==entry['identity'],_CODE)
        raw=b''.join(source.chunks());source.check()
        fact=pub._json_object(raw,4096,_CODE)
    pub._need(set(fact)=={'schema','phase','binding_sha256','data_identity','media_identity'}and type(fact['schema'])is int and fact['schema']==1
        and fact['phase']=='READY'and fact['binding_sha256']==_digest(pub._canonical(value['binding'])),_CODE)
    with _held_media(owner,owner.path.parent/'media',fact['media_identity'],fact['data_identity']):pass


def _plan(records):
    """A bounded content contract is data, never filesystem authority."""
    from .registered_asset_snapshot import Policy,safe_relative
    policy=Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1)
    pub._need(type(records)is list and 0<=len(records)<=MAX_FILES,_CODE)
    result={};paths={};file_count=0;total=0
    references={
        'voice_input':('voice_input_id',{'local_path'}),
        'capture_artifact':('artifact_id',{'local_path'}),
        'kg_parent_textbook_ocr_artifact':('artifact_id',{'raw_image_path'}),
        'curriculum_corpus':('corpus_id',{'source_database_path','transcript_root_path','video_root_path'}),
        'curriculum_standard_source':('source_id',{'local_root_path'}),
        'kg_corpus':('corpus_id',{'metadata_db_path','transcript_root','video_root','pdf_root','image_root'})}
    managed={'voice_input','capture_artifact','kg_parent_textbook_ocr_artifact'}
    def add(relative,kind,purpose,child,refs,sha,size):
        safe_relative(relative,policy);key=relative.casefold()
        pub._need(key not in paths or paths[key]==relative,_CODE);paths[key]=relative
        item={'relative':relative,'member_id':_digest(pub._canonical([relative,kind])),
            'path_key':_digest(key.encode()),'kind':kind,'purpose':purpose,'child_id':child,
            'references':refs,'sha256':sha,'bytes':size}
        if relative in result:pub._need(result[relative]==item,_CODE)
        else:result[relative]=item
    for record in records:
        pub._need(type(record)is dict and set(record)=={'relative','purpose','child_id','references','sha256','bytes'},_CODE)
        relative=record['relative'];parts=safe_relative(relative,policy)
        pub._need(relative not in result,_CODE)
        pub._need(record['purpose']in{'managed','registered','metadata'}and type(record['purpose'])is str,_CODE)
        child=record['child_id'];pub._need(child is None or(type(child)is str and 1<=len(child.encode())<=128),_CODE)
        pub._need(type(record['references'])is list and len(pub._canonical(record['references']))<=4096,_CODE)
        pub._need(record['purpose']!='managed'or(child is not None and bool(record['references'])),_CODE)
        for ref in record['references']:
            pub._need(type(ref)is dict and set(ref)=={'table','id_column','id','column','child_id'},_CODE)
            table=ref['table'];pub._need(type(table)is str and table in references,_CODE)
            id_column,columns=references[table]
            pub._need(ref['id_column']==id_column and type(ref['column'])is str and ref['column']in columns
                and type(ref['id'])is str and 1<=len(ref['id'].encode())<=128 and ref['child_id']==child
                and((table in managed)==(record['purpose']=='managed')),_CODE)
        pub._need(type(record['sha256'])is str and pub._HEX.fullmatch(record['sha256'])and type(record['bytes'])is int and 0<=record['bytes']<=2**53-1,_CODE)
        # Copy input by canonical decoding so later caller mutations cannot
        # change the enrolled content/reference contract.
        refs=json.loads(pub._canonical(record['references']))
        add(relative,'file',record['purpose'],child,refs,record['sha256'],record['bytes'])
        for depth in range(1,len(parts)):add('/'.join(parts[:depth]),'directory','directory',None,[],None,0)
        file_count+=1;total+=record['bytes'];pub._need(total<=2**53-1,_CODE)
    pub._need(len(result)<=MAX_MEMBERS and len(pub._canonical(result))<=64*1024*1024,_CODE)
    return result,file_count,total


def _base(authority,connection):
    from . import parent_restore_ownership as ownership
    pub._owner(authority.owner);pub._need(authority.owner.path==pub._path(authority.database.path),_CODE)
    pub._need(any(value is connection for value in authority.owner.gate.sql_connections),_CODE)
    validate_schema(connection)
    anchor=ownership._anchor(connection)
    pub._need(anchor is not None and anchor['state']=='ACTIVE'and anchor['request_id']==authority.binding['request_id']
        and anchor['initial']['binding']==authority.binding,_CODE)
    row,_,permit,metadata=pub._binding_facts(connection,anchor['initial']['binding'])
    pub._need(row['state']=='processing'and row['finalized']==0 and pub._authority_status(permit)=='claimed'
        and row['source_sha256']==authority.binding['source_sha256']
        and pub._metadata_matches(authority.binding,metadata),_CODE)


@contextmanager
def _transaction(authority):
    owner=authority.owner;pub._owner(owner)
    with pub._sql(owner.path,owner=owner,timeout=0)as connection:
        connection.execute('PRAGMA synchronous=FULL');connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE');_base(authority,connection)
        try:
            yield connection
        except BaseException:
            try:connection.rollback()
            except BaseException:owner.gate.poisoned=True
            raise
        try:connection.commit();owner.parent.flush()
        except BaseException:
            # COMMIT/flush uncertainty cannot grant another send/write/cleanup.
            owner.gate.poisoned=True;raise


def _authority(capability):
    pub._need(type(capability)is _AssetCapability and capability in _CAPABILITIES,_CODE)
    result=_CAPABILITIES[capability];pub._owner(result.owner)
    pub._need(result.actor==pub._actor(),_CODE)
    # Revalidate original external ledger/core IDs in addition to the live
    # immutable anchor checked again inside every original SQL transaction.
    value=result.work._value();pub._need(value['binding']==result.binding,_CODE)
    return result


def _generation(authority,connection):
    connection.row_factory=sqlite3.Row
    row=connection.execute('SELECT *FROM restore_asset_generation WHERE generation_id=?',(authority.generation,)).fetchone()
    pub._need(row is not None,_CODE);row=dict(row)
    pub._need(row['request_id']==authority.binding['request_id']and pub._generation_matches(row,authority.binding)
        and row['binding_sha256']==_digest(pub._canonical(authority.binding))and row['source_sha256']==authority.binding['source_sha256']
        and row['root_name']==authority.root.name and row['media_identity_json']==pub._canonical(authority.media_identity).decode(),_CODE)
    pub._need(row['expected_files']==authority.file_count and row['total_bytes']==authority.total_bytes,_CODE)
    return row


def _member(authority,connection,relative,allow_moving=False):
    pub._need(type(relative)is str and relative in authority.plan,_CODE);item=authority.plan[relative]
    row=connection.execute('SELECT *FROM restore_asset_member WHERE generation_id=?AND member_id=?',(authority.generation,item['member_id'])).fetchone()
    pub._need(row is not None,_CODE);row=dict(row)
    pub._need(row['relative_name']==relative and row['path_key']==item['path_key']and row['kind']==item['kind']
        and row['purpose']==item['purpose']and row['child_id']==item['child_id']and row['reference_json']==pub._canonical(item['references']).decode()
        and row['expected_sha256']==item['sha256']and row['expected_bytes']==item['bytes']
        and(row['move_target']is None or(allow_moving and row['state']=='MOVING')),_CODE)
    return row


@contextmanager
def _target_parent(authority,relative,retiring=False):
    with pub._sql(authority.owner.path,owner=authority.owner,readonly=True,timeout=0)as c:
        _base(authority,c);generation=_generation(authority,c)
        pub._need(generation['state']==('RETIRING'if retiring else'BUILDING'),_CODE);identity=json.loads(generation['root_identity_json'])
        ancestors=[];parts=relative.split('/')
        for depth in range(1,len(parts)):
            row=_member(authority,c,'/'.join(parts[:depth]));pub._need(row['kind']=='directory'and row['state']=='READY',_CODE)
            ancestors.append((authority.root/'/'.join(parts[:depth]),json.loads(row['identity_json'])))
    # All known ancestors remain held through the exact write/create operation.
    from contextlib import ExitStack
    with ExitStack()as stack:
        stack.enter_context(_held_media(authority.owner,authority.media,authority.media_identity,authority.data_identity))
        parent=stack.enter_context(_held_parent(authority.owner,authority.root,identity))
        for path,identity in ancestors:parent=stack.enter_context(_held_parent(authority.owner,path,identity))
        yield parent


def begin_generation(service,work,records):
    """Mint only under this actual R6 request/nonce and original live owner.

    The caller must have authenticated/recomputed the content plan. This
    function grants only bounded writes under a newly created media generation;
    it grants no publication, native terminal or arbitrary cleanup permission.
    """
    from . import parent_restore_ownership as ownership
    pub._need(type(work)is ownership.WorkCapability,_CODE);owner=pub._owner(work.owner)
    db=service.database;pub._need(pub._path(db.path)==owner.path and db._restore_sql_owner()is owner,_CODE)
    media=pub._path(service.data_dir/'media')
    # Main's original local_media_scope records the application data root;
    # standalone media callers record its exact media child. Both resolve to
    # this one service-owned target, never a caller-supplied destination map.
    pub._need(db.local_media_runtime is not None and pub._path(db.local_media_runtime.media_root)
        in{pub._path(service.data_dir),media},_CODE)
    value=work._value();binding=json.loads(pub._canonical(value['binding']))
    plan,count,total=_plan(records);generation=uuid4().hex
    authority=SimpleNamespace(database=db,owner=owner,work=work,actor=pub._actor(),binding=binding,
        media=media,root=media/('restore-generation-'+generation),generation=generation,plan=plan,file_count=count,total_bytes=total,
        registered_payloads={},held_registered={})
    with _held_media(owner,media)as(parent,data_identity):
        authority.media_identity=_identity(parent,False);authority.data_identity=data_identity
        with _transaction(authority)as c:
            pub._need(not c.execute("SELECT 1 FROM restore_asset_generation WHERE state IN('ALLOCATING','BUILDING','SEALED','RETIRING')LIMIT 1").fetchone(),_CODE)
            c.execute('INSERT INTO restore_asset_generation(generation_id,request_id,native_nonce,binding_sha256,source_sha256,root_name,media_identity_json,root_identity_json,manifest_sha256,projection_sha256,expected_files,total_bytes,state,authority_kind,parent_authority_id)VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?)',
                (generation,binding['request_id'],binding.get('native_nonce'),_digest(pub._canonical(binding)),binding['source_sha256'],authority.root.name,pub._canonical(authority.media_identity).decode(),count,total,'ALLOCATING',
                    binding.get('authority_kind','NATIVE_PERMIT'),binding.get('parent_authority_id')))
            for item in plan.values():
                c.execute('INSERT INTO restore_asset_member VALUES(?,?,?,?,?,?,?,?,NULL,?,?,NULL,?)',
                    (generation,item['member_id'],item['relative'],item['path_key'],item['kind'],item['purpose'],item['child_id'],pub._canonical(item['references']).decode(),item['sha256'],item['bytes'],'ALLOCATING'))
        parent.check()
        if os.name=='nt':os.mkdir(io_path(authority.root),0o700)
        else:os.mkdir(authority.root.name,0o700,dir_fd=parent.fd)
        pub._fault('after_restore_asset_root_allocated_before_id')
        with _held_parent(owner,authority.root)as root:
            identity=_identity(root);root.flush();parent.flush()
            with _transaction(authority)as c:
                _generation(authority,c)
                c.execute("UPDATE restore_asset_generation SET root_identity_json=?,state='BUILDING'WHERE generation_id=?AND state='ALLOCATING'",(pub._canonical(identity).decode(),generation))
    capability=object.__new__(_AssetCapability);_CAPABILITIES[capability]=authority
    for relative in sorted((name for name,item in plan.items()if item['kind']=='directory'),key=lambda name:(name.count('/'),name)):_directory(capability,relative)
    return capability


def _directory(capability,relative):
    authority=_authority(capability)
    with _target_parent(authority,relative)as parent:
        with _transaction(authority)as c:
            row=_member(authority,c,relative);pub._need(row['kind']=='directory'and row['state']=='ALLOCATING',_CODE)
        path=authority.root/relative;parent.check()
        if os.name=='nt':os.mkdir(io_path(path),0o700)
        else:os.mkdir(path.name,0o700,dir_fd=parent.fd)
        pub._fault('after_restore_asset_directory_allocated_before_id')
        with _held_parent(authority.owner,path)as child:
            identity=_identity(child);child.flush();parent.flush()
            with _transaction(authority)as c:
                _member(authority,c,relative)
                c.execute("UPDATE restore_asset_member SET identity_json=?,state='OWNED'WHERE generation_id=?AND relative_name=?",(pub._canonical(identity).decode(),authority.generation,relative))
            with _transaction(authority)as c:
                c.execute("UPDATE restore_asset_member SET state='WRITING'WHERE generation_id=?AND relative_name=?",(authority.generation,relative))
                c.execute("UPDATE restore_asset_member SET state='READY'WHERE generation_id=?AND relative_name=?",(authority.generation,relative))


def _copy_held(capability,relative,held):
    from .registered_asset_snapshot import HeldFile
    _authority(capability);pub._need(type(held)is HeldFile,_CODE)
    return _copy_contract(capability,relative,held.hash(),held.chunks,held.hash)


def _copy_contract(capability,relative,before,blocks,verify):
    authority=_authority(capability);pub._need(type(relative)is str and relative in authority.plan,_CODE);item=authority.plan[relative]
    pub._need(before==(item['sha256'],item['bytes']),_CODE)
    with _target_parent(authority,relative)as parent:
        with _transaction(authority)as c:
            row=_member(authority,c,relative);pub._need(row['kind']=='file'and row['state']=='ALLOCATING',_CODE)
        path=authority.root/relative;parent.check();kernel=None;handle=None;fd=None
        try:
            if os.name=='nt':
                from .money_r4.money_files import _windows_kernel
                from .registered_asset_restore import _win_target_check
                kernel,security=_windows_kernel()
                handle=kernel.dll.CreateFileW('\\\\?\\'+str(path),0x40020080,0,None,1,0x00300000,None)
                pub._need(handle not in{None,0,kernel.ct.c_void_p(-1).value},_CODE)
                info=_win_target_check(kernel,handle,path,directory=False);pub._need(info['links']==1,_CODE)
                security.check(handle);identity=list(info['identity'])
            else:
                fd=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent.fd)
                info=os.fstat(fd);pub._need(stat.S_ISREG(info.st_mode)and info.st_nlink==1 and info.st_uid==os.getuid()and info.st_mode&0o077==0,_CODE)
                identity=[info.st_dev,info.st_ino]
            pub._fault('after_restore_asset_file_allocated_before_id')
            with _transaction(authority)as c:
                _member(authority,c,relative)
                c.execute("UPDATE restore_asset_member SET identity_json=?,state='OWNED'WHERE generation_id=?AND relative_name=?",(pub._canonical(identity).decode(),authority.generation,relative))
            with _transaction(authority)as c:c.execute("UPDATE restore_asset_member SET state='WRITING'WHERE generation_id=?AND relative_name=?",(authority.generation,relative))
            pub._fault('before_restore_asset_first_payload_byte')
            total=0;digest=hashlib.sha256()
            for block in blocks():
                pub._need(type(block)is bytes and total+len(block)<=item['bytes'],_CODE);parent.check()
                if os.name=='nt':
                    _win_target_check(kernel,handle,path,directory=False,identity=tuple(identity));kernel.write(handle,block)
                else:
                    current=os.stat(path.name,dir_fd=parent.fd,follow_symlinks=False);original=os.fstat(fd)
                    pub._need((current.st_dev,current.st_ino)==tuple(identity)==(original.st_dev,original.st_ino)and original.st_nlink==current.st_nlink==1,_CODE)
                    view=memoryview(block)
                    while view:
                        written=os.write(fd,view);pub._need(written>0,_CODE);view=view[written:]
                total+=len(block);digest.update(block);pub._fault('during_restore_asset_payload_copy')
            pub._need((digest.hexdigest(),total)==before==verify(),_CODE)
            if os.name=='nt':kernel.flush(handle);_win_target_check(kernel,handle,path,directory=False,identity=tuple(identity))
            else:
                os.fsync(fd);current=os.stat(path.name,dir_fd=parent.fd,follow_symlinks=False);original=os.fstat(fd)
                pub._need((current.st_dev,current.st_ino)==tuple(identity)==(original.st_dev,original.st_ino)
                    and current.st_nlink==original.st_nlink==1 and original.st_size==item['bytes'],_CODE)
        finally:
            try:
                if kernel is not None and handle not in{None,0,kernel.ct.c_void_p(-1).value}:kernel.close(handle)
                if fd is not None:os.close(fd)
            except BaseException:
                authority.owner.gate.poisoned=True;authority.owner.gate.retained.append(('restore_asset_payload_handle',kernel,handle,fd))
                raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
        parent.check();parent.flush()
        with _transaction(authority)as c:
            row=_member(authority,c,relative);pub._need(row['state']=='WRITING'and json.loads(row['identity_json'])==identity,_CODE)
            c.execute("UPDATE restore_asset_member SET state='READY'WHERE generation_id=?AND relative_name=?",(authority.generation,relative))
    return before


def _inventory(authority):
    """Names enumerate only; original held/native identities authorize objects."""
    with pub._sql(authority.owner.path,owner=authority.owner,readonly=True,timeout=0)as c:
        _base(authority,c);generation=_generation(authority,c)
        rows={name:_member(authority,c,name)for name in authority.plan}
    if generation['root_identity_json']is None:
        pub._need(not os.path.lexists(io_path(authority.root)),_CODE)
        pub._need(all(r['state']=='ALLOCATING'and r['identity_json']is None for r in rows.values()),_CODE)
        return
    root_id=json.loads(generation['root_identity_json'])
    if not os.path.lexists(io_path(authority.root)):
        pub._need(generation['state']in{'RETIRING','CLEANED'}and all(r['state']=='REMOVED'for r in rows.values()),_CODE);return
    seen=set()
    def walk(path,relative,identity):
        with _held_parent(authority.owner,path,identity)as parent:
            with os.scandir(io_path(path if os.name=='nt'else parent.fd))as children:
                for child in children:
                    name=relative+'/'+child.name if relative else child.name
                    pub._need(name in rows and name not in seen,_CODE);seen.add(name);row=rows[name]
                    pub._need(row['identity_json']is not None and row['state']!='REMOVED',_CODE)
                    expected=json.loads(row['identity_json'])
                    if row['kind']=='directory':walk(path/child.name,name,expected)
                    else:pub._need(list(pub._database_identity(path/child.name,parent,authority.owner.gate)or())==expected,_CODE)
    with _held_media(authority.owner,authority.media,authority.media_identity,authority.data_identity):walk(authority.root,'',root_id)
    for name,row in rows.items():
        if row['state']in{'ALLOCATING','REMOVED'}:pub._need(name not in seen,_CODE)
        elif row['state']!='REMOVING':pub._need(name in seen,_CODE)


def _erase(authority,parent,path,kind,identity):
    parent.check()
    if kind=='directory':
        with _held_parent(authority.owner,path,identity)as child:
            with os.scandir(io_path(path if os.name=='nt'else child.fd))as entries:pub._need(next(entries,None)is None,_CODE)
    else:pub._need(list(pub._database_identity(path,parent,authority.owner.gate)or())==identity,_CODE)
    if os.name=='nt':
        from .parent_restore_owned_delete import erase_windows
        erase_windows(authority.owner,path,directory=kind=='directory',identity=identity,parent=parent)
    elif kind=='directory':os.rmdir(path.name,dir_fd=parent.fd)
    else:os.unlink(path.name,dir_fd=parent.fd)
    pub._need(not os.path.lexists(io_path(path)),_CODE);parent.flush()


def _abort_unapplied(capability):
    from . import parent_restore_ownership as ownership
    authority=_authority(capability)
    pub._need(ownership.reconcile_without_journal(authority.owner)['state']=='UNAPPLIED',_CODE)
    for relative in list(authority.plan):_resolve_move(authority,relative)
    _inventory(authority)
    with _transaction(authority)as c:
        generation=_generation(authority,c)
        pub._need(generation['state']in{'ALLOCATING','BUILDING','SEALED','RETIRING','CLEANED'},_CODE)
        if generation['state']=='CLEANED':return
        if generation['state']=='ALLOCATING':
            pub._need(not os.path.lexists(io_path(authority.root)),_CODE)
            for relative in authority.plan:
                row=_member(authority,c,relative);pub._need(row['state']=='ALLOCATING',_CODE)
                c.execute("UPDATE restore_asset_member SET state='REMOVED'WHERE generation_id=?AND relative_name=?",(authority.generation,relative))
            c.execute("UPDATE restore_asset_generation SET state='CLEANED'WHERE generation_id=?",(authority.generation,));return
        if generation['state']!='RETIRING':c.execute("UPDATE restore_asset_generation SET state='RETIRING'WHERE generation_id=?",(authority.generation,))
    for relative in sorted(authority.plan,key=lambda name:(-name.count('/'),authority.plan[name]['kind']=='directory',name)):
        with _transaction(authority)as c:
            row=_member(authority,c,relative)
            if row['state']=='REMOVED':continue
            if row['state']=='ALLOCATING':
                pub._need(row['identity_json']is None and not os.path.lexists(io_path(authority.root/relative)),_CODE)
                c.execute("UPDATE restore_asset_member SET state='REMOVED'WHERE generation_id=?AND relative_name=?",(authority.generation,relative));continue
            pub._need(row['state']in{'OWNED','WRITING','READY','REMOVING'},_CODE)
            was_removing=row['state']=='REMOVING'
            if not was_removing:c.execute("UPDATE restore_asset_member SET state='REMOVING'WHERE generation_id=?AND relative_name=?",(authority.generation,relative))
        with _target_parent(authority,relative,retiring=True)as parent:
            path=authority.root/relative
            if os.path.lexists(io_path(path)):_erase(authority,parent,path,row['kind'],json.loads(row['identity_json']))
            else:pub._need(was_removing,_CODE)
        pub._fault('after_restore_asset_member_unlinked')
        with _transaction(authority)as c:c.execute("UPDATE restore_asset_member SET state='REMOVED'WHERE generation_id=?AND relative_name=?",(authority.generation,relative))
    _inventory(authority)
    with _held_media(authority.owner,authority.media,authority.media_identity,authority.data_identity)as(parent,_):
        with pub._sql(authority.owner.path,owner=authority.owner,readonly=True,timeout=0)as c:
            _base(authority,c);generation=_generation(authority,c)
        if os.path.lexists(io_path(authority.root)):_erase(authority,parent,authority.root,'directory',json.loads(generation['root_identity_json']))
    pub._fault('after_restore_asset_root_unlinked')
    with _transaction(authority)as c:c.execute("UPDATE restore_asset_generation SET state='CLEANED'WHERE generation_id=?",(authority.generation,))


_REGISTERED_ARCHIVES=weakref.WeakKeyDictionary()

class _RegisteredArchive:
    __slots__=('__weakref__',)
    def __new__(cls):raise pub.RestorePublicationError(_CODE)


class _OriginalArchiveFile:
    """ZipFile borrows this file. The exact original native close stays owned."""
    def __init__(self,path,identity):self.path=path;self.identity=identity;self.file=None;self.fd=None
    def __enter__(self):
        self.file=io_path(self.path).open('rb');self.fd=self.file.fileno()
        if os.name=='nt':
            import msvcrt
            from .registered_asset_restore import _TargetWin32,_win_target_check
            info=_win_target_check(_TargetWin32(),msvcrt.get_osfhandle(self.fd),self.path,directory=False,identity=tuple(self.identity))
            pub._need(info['links']==1,_CODE)
        else:
            info=os.fstat(self.fd);pub._need(stat.S_ISREG(info.st_mode)and info.st_nlink==1 and [info.st_dev,info.st_ino]==self.identity,_CODE)
        return self.file
    def __exit__(self,*args):self.file.close()


def _registered_archive(value):
    pub._need(type(value)is _RegisteredArchive and value in _REGISTERED_ARCHIVES,_CODE)
    original=_REGISTERED_ARCHIVES[value];pub._owner(original.owner)
    pub._need(original.active and original.source.stack.active and original.work.owner is original.owner,_CODE)
    original.work._value();original.held.check();return original


def registered_archive_contract(value):
    original=_registered_archive(value)
    return json.loads(pub._canonical({'manifest':original.manifest,'receipt':original.receipt,'header':original.header}))


@contextmanager
def registered_archive(service,work,verify_source):
    """Authenticate one original capsule into one pre-registered R6 ZIP core.

    No extraction tree, caller path map, data-class publication, or cleanup
    right is returned. File hashes use the same complete manifest validator as
    the original safe snapshot roundtrip.
    """
    from . import registered_asset_backup as reg
    from .registered_asset_snapshot import HeldFile,Policy,_validate_registered_manifest
    from . import parent_restore_workflow as workflow
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    source=workflow.original_source_bundle(service,work,verify_source)
    owner=pub._owner(work.owner);pub._need(source.capsule_held is not None,_CODE)
    capsule=reg.capsule_path(source.source_path);header,offset=reg.header(capsule)
    contract=source.metadata['registered']
    pub._need(header['snapshot_sha256']==getattr(source,'capsule_database_sha256',source.database_sha256)
        and all(header[k]==contract[k]for k in('asset_count','total_bytes','manifest_sha256','coverage_sha256')),_CODE)
    key=getattr(source,'source_key',service.data_encryption_key);pub._need(type(key)is bytes and len(key)==32,_CODE)
    decryptor=Cipher(algorithms.AES(key),modes.GCM(base64.b64decode(header['nonce'],validate=True),base64.b64decode(header['tag'],validate=True))).decryptor()
    binding={k:v for k,v in header.items()if k not in{'nonce','tag','ciphertext_size'}}
    decryptor.authenticate_additional_data(reg.canonical(binding))
    def plaintext():
        remaining=offset;count=0
        for block in source.capsule_held.chunks():
            if remaining:
                skip=min(remaining,len(block));remaining-=skip;block=block[skip:]
            if block:
                count+=len(block);pub._need(count<=header['ciphertext_size'],_CODE)
                plain=decryptor.update(block)
                if plain:yield plain
        pub._need(remaining==0 and count==header['ciphertext_size'],_CODE)
        tail=decryptor.finalize()  # Tag verified before any ZIP interpretation.
        if tail:yield tail
    reg.space(work.work,header['ciphertext_size'])
    path,expected=work.registered_zip(plaintext(),header['ciphertext_size'])
    verify_source();value=work._value();entry=next(e for e in value['members']['entries']if owner.path.parent/e['relative']==path)
    policy=Policy(reg.MAX_FILES,64,1024,reg.M,reg.SAFE,reg.SAFE)
    with workflow._SourceHandles(owner)as stack:
        held=stack.enter_context(HeldFile(work.work,path.name,policy));pub._need(held.hash()==expected,_CODE)
        original=stack.enter_context(_OriginalArchiveFile(path,entry['identity']))
        archive=stack.enter_context(reg._finite_archive_file(original,header['asset_count']+1))
        infos=archive.infolist();names=[row.filename for row in infos]
        pub._need(len(names)==len(set(names))==len({n.casefold()for n in names})and'manifest.json'in names,_CODE)
        for row in infos:
            reg.safe_relative(row.filename,policy);kind=(row.external_attr>>16)&0o170000
            # The existing capsule writer is ZIP_STORED. Its exact 1:1 ratio
            # is enforced rather than allowing compressed expansion to guess.
            pub._need(not row.is_dir()and kind in{0,stat.S_IFREG}and not row.flag_bits&1
                and row.compress_type==zipfile.ZIP_STORED and row.compress_size==row.file_size,_CODE)
        info=archive.getinfo('manifest.json');pub._need(info.file_size<=reg.M,_CODE)
        with archive.open(info)as member:raw=member.read(reg.M+1)
        pub._need(len(raw)<=reg.M and _digest(raw)==header['manifest_sha256'],_CODE)
        manifest=reg.unique_json(raw);manifest_policy=Policy(**manifest['policy']);manifest_policy.validate()
        def read_member(name):
            row=archive.getinfo(name);pub._need(row.file_size<=manifest_policy.max_file_bytes,_CODE)
            count=0;digest=hashlib.sha256();held.check()
            with archive.open(row)as member:
                while True:
                    block=member.read(reg.CHUNK)
                    if not block:break
                    count+=len(block);pub._need(count<=row.file_size,_CODE);digest.update(block)
            held.check();pub._need(count==row.file_size,_CODE);return digest.hexdigest(),count
        checked,expected_names,total=_validate_registered_manifest(raw,manifest_policy,read_member)
        pub._need(checked==manifest and set(names)==expected_names|{'manifest.json'}and len(expected_names)==header['asset_count']
            and total==header['total_bytes']and _digest(reg.canonical(manifest['coverage']))==header['coverage_sha256'],_CODE)
        capability=object.__new__(_RegisteredArchive)
        state=SimpleNamespace(active=True,owner=owner,work=work,source=source,held=held,archive=archive,
            manifest=manifest,assets={item['logical_name']:item for item in manifest['assets']},header=header,
            receipt={'files':len(expected_names),'total_bytes':total,'manifest_sha256':_digest(raw)},policy=manifest_policy)
        _REGISTERED_ARCHIVES[capability]=state
        try:yield capability
        finally:
            state.active=False
            pub._need(held.hash()==expected,_CODE);verify_source()


def _registered_purpose(item):
    from .standard_relocation_provenance import NAME,ROLE
    from .registered_fingerprint_relocation import NAME as FP_NAME,ROLE as FP_ROLE
    return 'metadata'if(item['storage']=='sqlite_backup'or(item['kind']=='standard'and item['role']==ROLE and item['relative_path']==NAME)or(item['kind']=='kg'and item['role']==FP_ROLE and item['relative_path']==FP_NAME))else'registered'


def _read_registered_provenance(archive,logical):
    from .standard_relocation_provenance import NAME,ROLE,MAX_BYTES
    original=_registered_archive(archive);pub._need(logical in original.assets,_CODE);item=original.assets[logical]
    pub._need(item['kind']=='standard'and item['role']==ROLE and item['relative_path']==NAME
        and item['storage']=='regular'and item['bytes']<=MAX_BYTES,_CODE)
    chunks=[];total=0;digest=hashlib.sha256()
    with original.archive.open(logical)as stream:
        while True:
            original.held.check();block=stream.read(min(1024*1024,MAX_BYTES+1-total))
            if not block:break
            total+=len(block);pub._need(total<=MAX_BYTES,_CODE);chunks.append(block);digest.update(block)
    pub._need((digest.hexdigest(),total)==(item['sha256'],item['bytes']),_CODE)
    original.held.check();return b''.join(chunks)


def _copy_registered(capability,relative,archive,logical):
    authority=_authority(capability);original=_registered_archive(archive)
    pub._need(original.owner is authority.owner and original.work is authority.work
        and original.source.bundle_sha256==authority.binding['source_sha256']and type(logical)is str and logical in original.assets,_CODE)
    pub._need(type(relative)is str and relative in authority.plan and logical not in authority.registered_payloads,_CODE)
    item=original.assets[logical];target=authority.plan[relative]
    pub._need(target['purpose']==_registered_purpose(item),_CODE)
    expected=(item['sha256'],item['bytes'])
    def blocks():
        with original.archive.open(logical)as stream:
            while True:
                original.held.check();block=stream.read(1024*1024)
                if not block:break
                yield block
    def verify():original.held.check();return expected
    result=_copy_contract(capability,relative,expected,blocks,verify)
    authority.registered_payloads[logical]=relative;return result


@contextmanager
def held_registered_payloads(capability):
    from .registered_asset_snapshot import HeldFile,Policy
    from .parent_restore_workflow import _SourceHandles
    authority=_authority(capability);pub._need(not authority.held_registered,_CODE)
    policy=Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1)
    with _SourceHandles(authority.owner)as stack:
        try:
            for logical,relative in authority.registered_payloads.items():
                with pub._sql(authority.owner.path,owner=authority.owner,readonly=True,timeout=0)as c:
                    _base(authority,c);row=_member(authority,c,relative);pub._need(row['state']=='READY',_CODE)
                held=stack.enter_context(HeldFile(authority.root,relative,policy))
                identity=list(held.before['identity'])if os.name=='nt'else[held.before.st_dev,held.before.st_ino]
                pub._need(identity==json.loads(row['identity_json'])and held.hash()==(row['expected_sha256'],row['expected_bytes']),_CODE)
                authority.held_registered[logical]=held
            yield
        finally:
            for held in authority.held_registered.values():held.check()
            authority.held_registered={}


def _registered_payload_path(capability,logical,expected):
    authority=_authority(capability)
    pub._need(type(logical)is str and logical in authority.registered_payloads and logical in authority.held_registered,_CODE)
    relative=authority.registered_payloads[logical];item=authority.plan[relative];held=authority.held_registered[logical]
    pub._need(item['sha256']==expected['sha256']and item['bytes']==expected['bytes'],_CODE)
    held.check();return authority.root/relative


def generation_root(capability):return _authority(capability).root


def _extend_directories(capability,names):
    from .registered_asset_snapshot import Policy,safe_relative
    a=_authority(capability);pub._need(not a.held_registered,_CODE)
    policy=Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1);wanted=set()
    for name in names:
        parts=safe_relative(name,policy)
        wanted.update('/'.join(parts[:n])for n in range(1,len(parts)+1))
    additions={};folded={name.casefold()for name in a.plan}
    for name in wanted:
        if name in a.plan:pub._need(a.plan[name]['kind']=='directory',_CODE);continue
        pub._need(name.casefold()not in folded,_CODE);folded.add(name.casefold())
        additions[name]={'relative':name,'member_id':_digest(pub._canonical([name,'directory'])),'path_key':_digest(name.casefold().encode()),
            'kind':'directory','purpose':'directory','child_id':None,'references':[],'sha256':None,'bytes':0}
    pub._need(len(a.plan)+len(additions)<=MAX_MEMBERS,_CODE)
    with _transaction(a)as c:
        pub._need(_generation(a,c)['state']=='BUILDING',_CODE)
        for item in additions.values():
            c.execute('INSERT INTO restore_asset_member VALUES(?,?,?,?,?,?,?,?,NULL,?,?,NULL,?)',
                (a.generation,item['member_id'],item['relative'],item['path_key'],'directory','directory',None,'[]',None,0,'ALLOCATING'))
    a.plan.update(additions)
    for name in sorted(additions,key=lambda n:(n.count('/'),n)):_directory(capability,name)


def _adopt_moved_name(a,source,target):
    item=a.plan.pop(source);item['relative']=target;item['path_key']=_digest(target.casefold().encode());a.plan[target]=item
    for logical,relative in list(a.registered_payloads.items()):
        if relative==source:a.registered_payloads[logical]=target


def _resolve_move(a,relative):
    with pub._sql(a.owner.path,owner=a.owner,readonly=True,timeout=0)as c:
        _base(a,c);row=_member(a,c,relative,allow_moving=True)
    if row['state']!='MOVING':return
    target=row['move_target']
    from .registered_asset_snapshot import Policy,safe_relative
    safe_relative(target,Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1))
    pub._need(target not in a.plan,_CODE);identity=json.loads(row['identity_json'])
    with _target_parent(a,relative)as origin,_target_parent(a,target)as destination:
        old=pub._database_identity(a.root/relative,origin,a.owner.gate)
        new=pub._database_identity(a.root/target,destination,a.owner.gate)
        at_old=old is not None and list(old)==identity;at_new=new is not None and list(new)==identity
        pub._need((at_old and new is None)or(at_new and old is None),_CODE)
        final=target if at_new else relative
        origin.flush();destination.flush()
        with _transaction(a)as c:
            current=_member(a,c,relative,allow_moving=True)
            pub._need(current['state']=='MOVING'and current['move_target']==target,_CODE)
            c.execute("UPDATE restore_asset_member SET relative_name=?,path_key=?,move_target=NULL,state='READY'WHERE generation_id=?AND member_id=?",
                (final,_digest(final.casefold().encode()),a.generation,row['member_id']))
    if at_new:_adopt_moved_name(a,relative,target)


def _rename_no_replace(a,origin,destination,source,target):
    pub._need(not os.path.lexists(io_path(a.root/target)),_CODE)
    pub._fault('before_restore_asset_native_noreplace')
    if os.name=='nt':
        # Windows os.rename refuses an existing target. These metadata parent
        # holds verify namespace identity; they confer no rename-lock claim.
        os.rename(io_path(a.root/source),io_path(a.root/target))
    elif sys.platform=='darwin':
        # Actual local macOS SDK sys/stdio.h: renameatx_np(..., RENAME_EXCL=4).
        import ctypes as ct
        library=ct.CDLL(None,use_errno=True);function=library.renameatx_np
        function.argtypes=[ct.c_int,ct.c_char_p,ct.c_int,ct.c_char_p,ct.c_uint];function.restype=ct.c_int
        pub._need(function(origin.fd,os.fsencode((a.root/source).name),destination.fd,os.fsencode((a.root/target).name),4)==0,_CODE)
    else:
        # No replacing rename fallback on an unverified platform.
        raise pub.RestorePublicationError(_CODE)


def _move_original(capability,source,target):
    a=_authority(capability);pub._need(not a.held_registered and source!=target and source in a.plan and target not in a.plan,_CODE)
    with _target_parent(a,source)as origin,_target_parent(a,target)as destination:
        with _transaction(a)as c:
            row=_member(a,c,source);pub._need(row['kind']=='file'and row['state']=='READY',_CODE)
            pub._need(list(pub._database_identity(a.root/source,origin,a.owner.gate)or())==json.loads(row['identity_json'])
                and not os.path.lexists(io_path(a.root/target)),_CODE)
            c.execute("UPDATE restore_asset_member SET move_target=?,state='MOVING'WHERE generation_id=?AND member_id=?",(target,a.generation,row['member_id']))
        pub._fault('before_restore_asset_original_move')
        _rename_no_replace(a,origin,destination,source,target)
        pub._need(not os.path.lexists(io_path(a.root/source))and list(pub._database_identity(a.root/target,destination,a.owner.gate)or())==json.loads(row['identity_json']),_CODE)
        origin.flush();destination.flush();pub._fault('after_restore_asset_original_move')
    _resolve_move(a,source)


def _set_original_mtime(capability,relative,mtime):
    a=_authority(capability);pub._need(not a.held_registered and type(mtime)is int and 0<=mtime<2**63,_CODE)
    with _target_parent(a,relative)as parent:
        with pub._sql(a.owner.path,owner=a.owner,readonly=True,timeout=0)as c:
            _base(a,c);row=_member(a,c,relative);pub._need(row['kind']=='file'and row['state']=='READY',_CODE)
        path=a.root/relative;identity=json.loads(row['identity_json']);kernel=None;handle=None;fd=None
        from .registered_asset_snapshot import HeldFile,Policy
        from .parent_restore_workflow import _SourceHandles
        policy=Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1)
        def verify_bytes():
            with _SourceHandles(a.owner)as scope:
                held=scope.enter_context(HeldFile(path.parent,path.name,policy))
                actual=held.before['identity']if os.name=='nt'else(held.before.st_dev,held.before.st_ino)
                pub._need(list(actual)==identity and held.hash()==(row['expected_sha256'],row['expected_bytes']),_CODE)
        verify_bytes()
        try:
            if os.name=='nt':
                from .registered_asset_restore import _TargetWin32,_win_target_check
                kernel=_TargetWin32();handle=kernel.open_target(str(path),file_attributes=True)
                info=_win_target_check(kernel,handle,path,directory=False,identity=tuple(identity));pub._need(info['links']==1,_CODE)
                kernel.set_mtime(handle,mtime);kernel.flush(handle)
                after=_win_target_check(kernel,handle,path,directory=False,identity=tuple(identity))
                pub._need(after['size']==row['expected_bytes']and(after['modified']-116444736000000000)*100==mtime,_CODE)
            else:
                fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent.fd);info=os.fstat(fd)
                pub._need(stat.S_ISREG(info.st_mode)and info.st_nlink==1 and[info.st_dev,info.st_ino]==identity,_CODE)
                os.utime(fd,ns=(mtime,mtime));os.fsync(fd)
                info=os.fstat(fd);pub._need([info.st_dev,info.st_ino]==identity and info.st_mtime_ns==mtime,_CODE)
        finally:
            try:
                if handle is not None:kernel.close(handle)
                if fd is not None:os.close(fd)
            except BaseException:
                a.owner.gate.poisoned=True;a.owner.gate.retained.append(('restore_asset_mtime_handle',kernel,handle,fd))
                raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
        verify_bytes();parent.check();parent.flush()


def _remove_unused_original(capability,relative):
    a=_authority(capability);pub._need(not a.held_registered,_CODE)
    with _target_parent(a,relative)as parent:
        with _transaction(a)as c:
            row=_member(a,c,relative);pub._need(row['state']=='READY'and row['purpose']in{'registered','metadata','directory'},_CODE)
            c.execute("UPDATE restore_asset_member SET state='REMOVING'WHERE generation_id=?AND member_id=?",(a.generation,row['member_id']))
        _erase(a,parent,a.root/relative,row['kind'],json.loads(row['identity_json']))
        pub._fault('after_restore_asset_unused_unlinked')
        with _transaction(a)as c:c.execute("UPDATE restore_asset_member SET state='REMOVED'WHERE generation_id=?AND member_id=?",(a.generation,row['member_id']))


def apply_registered_plan(capability,plan):
    """Original bounded moves only; this result still cannot publish a DB."""
    a=_authority(capability)
    from .registered_asset_restore import _Plan
    pub._need(type(plan)is _Plan and plan.parent_payloads is capability and plan.final==a.root
        and set(plan.claimed)==set(a.registered_payloads),_CODE)
    directories=set(plan.groups)
    for name in plan.targets:
        parts=name.split('/');directories.update('/'.join(parts[:n])for n in range(1,len(parts)))
    _extend_directories(capability,directories)
    for target,(logical,_)in plan.targets.items():
        if target not in plan.derived_targets:_move_original(capability,a.registered_payloads[logical],target)
    for logical,target in plan.claimed.items():
        source=a.registered_payloads[logical]
        if logical in plan.provenance_inputs:
            _remove_unused_original(capability,source)
        elif source!=target:
            _remove_unused_original(capability,source);a.registered_payloads[logical]=target
    if 'payloads'in a.plan:_remove_unused_original(capability,'payloads')
    for relative,mtime in plan.target_mtimes.items():_set_original_mtime(capability,relative,mtime)
    _inventory(a)


_MANAGED_COLUMNS=(('voice_input','voice_input_id','local_path'),
    ('capture_artifact','artifact_id','local_path'),
    ('kg_parent_textbook_ocr_artifact','artifact_id','raw_image_path'))

def managed_source_plan(service,work,verify):
    """Map all original media references using authenticated source data only.

    Old Windows/POSIX roots are lexical data: they are never statted/opened.
    A unique common media root and exact coverage are required, with one child
    per physical encrypted source. The result itself confers no authority.
    """
    from .parent_restore_workflow import original_source_bundle
    from .registered_asset_snapshot import Policy
    from .registered_asset_restore import _source_path
    from pathlib import PureWindowsPath
    bundle=original_source_bundle(service,work,verify);cold=work.work/'authenticated-cold.db'
    pub._need(pub._file_hash(cold)==bundle.database_sha256,_CODE)
    items={i['relative_path']:i for i in(bundle.metadata or{}).get('files',[])}
    policy=Policy(MAX_FILES,128,4096,64*1024*1024,2**53-1,2**53-1)
    references=[];roots=None;count=0
    with pub._cold(cold,owner=work.owner)as c:
        service._validate_restore_scope(c)
        for table,key,column in _MANAGED_COLUMNS:
            if table not in pub._tables(c):continue
            cols={r[1]for r in c.execute('PRAGMA table_info('+table+')')}
            pub._need({key,column,'child_id'}<=cols,_CODE)
            for ident,child,stored in c.execute('SELECT '+key+',child_id,'+column+' FROM '+table+' WHERE '+column+' IS NOT NULL ORDER BY '+key):
                count+=1;pub._need(count<=MAX_FILES and type(ident)is str and type(child)is str and 1<=len(child.encode())<=128,_CODE)
                path=_source_path(stored,policy);matches={}
                for n in range(1,len(path.parts)):
                    relative='/'.join(path.parts[n:])
                    if relative not in items:continue
                    root=path.parents[len(path.parts)-n-1]
                    if root.name.casefold()!='media':continue
                    canonical=str(root).casefold()if isinstance(root,PureWindowsPath)else str(root)
                    pub._need(canonical not in matches,_CODE);matches[canonical]=relative
                roots=set(matches)if roots is None else roots&set(matches)
                pub._need(bool(roots),_CODE)
                references.append((matches,{'table':table,'id_column':key,'id':ident,'column':column,'child_id':child}))
    if not references:pub._need(not items,_CODE);return []
    pub._need(len(roots)==1,_CODE);root=next(iter(roots));grouped={}
    for matches,ref in references:grouped.setdefault(matches[root],[]).append(ref)
    pub._need(set(grouped)==set(items),_CODE)
    result=[]
    for name in sorted(items):
        item=items[name];refs=sorted(grouped[name],key=lambda r:pub._canonical(r));children={r['child_id']for r in refs}
        pub._need(len(children)==1,_CODE)
        result.append({'relative':'managed/'+_digest(name.encode())+'.enc','purpose':'managed',
            'child_id':next(iter(children)),'references':refs,'sha256':item['sha256'],'bytes':item['size_bytes'],'source_relative':name})
    verify();return result


def copy_managed_plan(capability,service,work,verify,plan):
    from .parent_restore_workflow import original_source_bundle
    a=_authority(capability);pub._need(a.work is work and a.database is service.database,_CODE)
    bundle=original_source_bundle(service,work,verify)
    # Recompute rather than accepting a mutable caller's source/remap choice.
    pub._need(plan==managed_source_plan(service,work,verify),_CODE)
    from .registered_asset_snapshot import HeldFile
    for item in plan:
        reader=bundle.media_held[item['source_relative']]
        if type(reader)is HeldFile:capability.copy_held(item['relative'],reader)
        else:
            from .parent_restore_portable import original_member_contract
            before,blocks,check=original_member_contract(reader,service,work,verify)
            _copy_contract(capability,item['relative'],before,blocks,check)
    verify()


def managed_rewrites(capability):
    a=_authority(capability);result=[]
    with pub._sql(a.owner.path,owner=a.owner,readonly=True,timeout=0)as c:
        _base(a,c);_generation(a,c)
        for name,item in a.plan.items():
            if item['purpose']!='managed':continue
            row=_member(a,c,name);pub._need(row['state']=='READY',_CODE)
            for ref in item['references']:
                pub._need(ref['child_id']==item['child_id'],_CODE)
                result.append((ref['table'],ref['id_column'],ref['id'],ref['column'],str(a.root/name)))
    return result

# Only this complete original source factory enrolls publication seals. A data
# result, PreparedRegisteredAssetRestore, mutable Plan or copied token cannot.
_SEALS=weakref.WeakKeyDictionary()
class _AssetSeal:
    __slots__=('__weakref__',)
    def __new__(cls):raise pub.RestorePublicationError(_CODE)


def _asset_projection(c,rewrites=(),root=None,fingerprint_changes=(),fingerprint_scope=None):
    from .registered_asset_restore import _REWRITE_COLUMNS
    from .registered_fingerprint_relocation import NAME as FP_NAME,main_rewrite_tuples
    fp_changes=list(fingerprint_changes);main_rewrite_tuples(fp_changes)
    by_key={}
    for table,key,ident,column,value in rewrites:
        pub._need(table in _REWRITE_COLUMNS and column in _REWRITE_COLUMNS[table],_CODE)
        by_key.setdefault(table,{}).setdefault(ident,{})[column]=value
    result=[];corpora=set()
    for table in sorted(_REWRITE_COLUMNS):
        if table not in pub._tables(c):continue
        key={'curriculum_standard_source':'source_id','voice_input':'voice_input_id','capture_artifact':'artifact_id',
            'kg_parent_textbook_ocr_artifact':'artifact_id'}.get(table,'corpus_id')
        columns={r[1]for r in c.execute('PRAGMA table_info('+table+')')};names=[key]+(['child_id']if table in{r[0]for r in _MANAGED_COLUMNS}else[])
        names+=sorted(_REWRITE_COLUMNS[table]&columns);pub._need(set(names)<=columns,_CODE)
        values=[]
        for number,row in enumerate(c.execute('SELECT '+','.join(names)+' FROM '+table+' ORDER BY '+key),1):
            pub._need(number<=MAX_FILES,_CODE);item=dict(zip(names,row));item.update(by_key.get(table,{}).get(item[key],{}))
            if root is None or any(type(item.get(col))is str and item[col].startswith(str(root)+os.sep)for col in _REWRITE_COLUMNS[table]):
                values.append(item)
                if table=='kg_corpus':corpora.add(item['corpus_id'])
        result.append([table,values])
    raw=pub._canonical(result);pub._need(len(raw)<=64*1024*1024,_CODE);base=_digest(raw)
    if fingerprint_scope is None:
        fingerprint_scope=False
        if root is not None and {GENERATION,MEMBER}<=pub._tables(c):
            rows=c.execute('SELECT generation_id FROM restore_asset_generation WHERE root_name=?',(root.name,)).fetchall()
            pub._need(len(rows)<=1,_CODE)
            if rows:
                for name,purpose,refs in c.execute('SELECT relative_name,purpose,reference_json FROM restore_asset_member WHERE generation_id=?',(rows[0][0],)):
                    if name.endswith('/'+FP_NAME):
                        references=json.loads(refs)
                        # Older generations could legitimately use the same basename
                        # for another registered asset. Only the new closed marker
                        # shape opts into the versioned target-fingerprint seal.
                        if(purpose=='metadata'and type(references)is list and references and all(
                            type(ref)is dict and set(ref)=={'table','id_column','id','column','child_id'}
                            and ref['table']=='kg_corpus'and ref['id_column']=='corpus_id'
                            and ref['column']=='transcript_root'and ref['child_id']is None
                            and type(ref['id'])is str for ref in references)):
                            fingerprint_scope=True
    pub._need(type(fingerprint_scope)is bool and(fingerprint_scope or not fp_changes),_CODE)
    if not fingerprint_scope:return base  # Preserve all previous generations' exact digest contract.
    sha=hashlib.sha256();sha.update(pub._canonical(['fingerprint_target_projection_v1',base]))
    changes={(row['table'],row['id']):row for row in fp_changes};seen=set()
    tables=pub._tables(c)
    for table,key,column,query in (
      ('kg_resource','resource_id','video_fingerprint','SELECT resource_id,corpus_id,video_fingerprint FROM kg_resource ORDER BY resource_id'),
      ('kg_document_page','page_id','image_fingerprint','SELECT p.page_id,p.resource_id,r.corpus_id,p.image_fingerprint FROM kg_document_page p JOIN kg_resource r ON r.resource_id=p.resource_id ORDER BY p.page_id')):
        if table not in tables or column not in {r[1]for r in c.execute('PRAGMA table_info('+table+')')}:continue
        sha.update(pub._canonical(table));count=0
        for row in c.execute(query):
            names=('resource_id','corpus_id',column)if table=='kg_resource'else('page_id','resource_id','corpus_id',column)
            value=dict(zip(names,row))
            if value['corpus_id']not in corpora:continue
            change=changes.get((table,value[key]))
            if change is not None:
                pub._need(value['corpus_id']==change['corpus_id']and value['resource_id']==change['resource_id']and value[column]==change['source_fingerprint'],_CODE)
                value[column]=change['target_fingerprint'];seen.add((table,value[key]))
            sha.update(pub._canonical(value));sha.update(b'\n');count+=1
        sha.update(pub._canonical(['count',count]))
    pub._need(seen==set(changes),_CODE)
    return sha.hexdigest()



def _bounded_owned_rows(c,generation):
    c.row_factory=sqlite3.Row;rows=[];size=0
    for number,row in enumerate(c.execute('SELECT *FROM restore_asset_member WHERE generation_id=? ORDER BY member_id',(generation,)),1):
        value=dict(row);size+=len(pub._canonical(value))
        pub._need(number<=MAX_MEMBERS and size<=64*1024*1024,_CODE);rows.append(value)
    return rows


def _persistent_members(c,generation,pending_parent=None):
    c.row_factory=sqlite3.Row;validate_schema(c)
    rows=_bounded_owned_rows(c,generation['generation_id']);names={};folded=set()
    from .registered_asset_snapshot import safe_relative,Policy
    policy=Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1)
    for row in rows:
        safe_relative(row['relative_name'],policy);name=row['relative_name']
        pub._need(name not in names and name.casefold()not in folded and row['path_key']==_digest(name.casefold().encode())
            and row['state']in({'READY','REMOVED','REMOVING'}if pending_parent is not None else{'READY','REMOVED'})and row['move_target']is None,_CODE)
        names[name]=row;folded.add(name.casefold())
    files=[r for r in rows if r['kind']=='file']
    pub._need(len(files)==generation['expected_files']and sum(r['expected_bytes']for r in files)==generation['total_bytes'],_CODE)
    return rows,names,policy


def _manifest_original(owner,c,generation,held_stack=None,preimages=None,pending_parent=None):
    """No rights are minted here: read exact original IDs and finite namespace."""
    from .parent_restore_workflow import _SourceHandles
    from .registered_asset_snapshot import HeldFile,held_registered_fingerprint
    rows,names,policy=_persistent_members(c,generation,pending_parent);media=owner.path.parent/'media';root=media/generation['root_name']
    root_id=json.loads(generation['root_identity_json']);media_id=json.loads(generation['media_identity_json']);seen=set();fingerprints={}
    parent_pending={};parent_cache={}
    for row in rows:
        if row['purpose']=='managed'and row['state']in{'REMOVED','REMOVING'}:
            record=c.execute('SELECT 1 FROM restore_asset_parent_erasure WHERE generation_id=?AND member_id=?',(generation['generation_id'],row['member_id'])).fetchone()
            if record is not None:
                from .parent_restore_privacy import preimage
                fingerprint,may_exist=preimage(c,generation,row,owner,pending=pending_parent,cache=parent_cache)
                fingerprints[row['relative_name']]=fingerprint;parent_pending[row['relative_name']]=may_exist
            else:pub._need(row['state']=='REMOVED',_CODE)
    def walk(path,relative,identity,stack):
        with _held_parent(owner,path,identity)as parent:
            with os.scandir(io_path(path if os.name=='nt'else parent.fd))as children:
                for child in children:
                    name=relative+'/'+child.name if relative else child.name
                    pub._need(name in names and name not in seen,_CODE);seen.add(name);row=names[name]
                    pub._need((row['state']=='READY'or parent_pending.get(name)is True)and row['identity_json']is not None,_CODE);original=json.loads(row['identity_json'])
                    if row['kind']=='directory':walk(path/child.name,name,original,stack)
                    else:
                        held=stack.enter_context(HeldFile(root,name,policy));held.check()
                        identity=list(held.before['identity'])if os.name=='nt'else[held.before.st_dev,held.before.st_ino]
                        pub._need(identity==original and held.hash()==(row['expected_sha256'],row['expected_bytes']),_CODE)
                        actual_fingerprint=held_registered_fingerprint(held)
                        if name in parent_pending:pub._need(fingerprints[name]==actual_fingerprint,_CODE)
                        fingerprints[name]=actual_fingerprint
            parent.check()
    from contextlib import nullcontext
    with _held_media(owner,media,media_id):
        with _SourceHandles(owner)if held_stack is None else nullcontext(held_stack)as stack:
            if held_stack is not None:
                stack.enter_context(_held_media(owner,media,media_id))
                stack.enter_context(_held_parent(owner,root,root_id))
            walk(root,'',root_id,stack)
            ready={name for name,r in names.items()if r['state']=='READY'}
            pub._need(ready<=seen and seen<=ready|{name for name,allowed in parent_pending.items()if allowed},_CODE)
            for row in rows:
                if row['state']=='REMOVED'and row['purpose']=='managed'and row['relative_name']not in parent_pending:
                    fingerprints[row['relative_name']]=_completed_erasure_preimage(c,generation,row,root)
                if row['state']=='REMOVING':pub._need(parent_pending.get(row['relative_name'])is True,_CODE)
            if preimages is not None:preimages.update(fingerprints)
            return _manifest_digest(generation,rows,fingerprints,root_id,media_id)


def _manifest_digest(generation,rows,fingerprints,root_id,media_id):
    # Pure original encoding, shared by owner verification and the separate
    # read-only original-connection verifier; no filesystem/owner is minted.
    manifest={'generation_id':generation['generation_id'],'root_identity':root_id,'media_identity':media_id,
        'members':[[r['member_id'],r['relative_name'],r['kind'],r['purpose'],r['child_id'],r['reference_json'],
            r['identity_json'],r['expected_sha256'],r['expected_bytes'],'READY'if r['state']in{'REMOVED','REMOVING'}and r['purpose']=='managed'else r['state'],fingerprints.get(r['relative_name'])]for r in rows]}
    raw=pub._canonical(manifest);pub._need(len(raw)<=64*1024*1024,_CODE);return _digest(raw)


@contextmanager
def _fingerprint_metadata_source(archive,work,logical,item):
    """One authenticated input into the fixed, reusable original owned core."""
    from .registered_asset_snapshot import HeldFile
    from .registered_fingerprint_relocation import MAX_METADATA_BYTES
    from .parent_restore_workflow import _SourceHandles
    original=_registered_archive(archive)
    pub._need(original.work is work and logical in original.assets and original.assets[logical]==item
        and item['kind']=='kg'and item['role']=='metadata'and item['storage']=='sqlite_backup'
        and 0<item['bytes']<=min(original.policy.max_file_bytes,MAX_METADATA_BYTES),_CODE)
    def blocks():
        count=0;sha=hashlib.sha256()
        with original.archive.open(logical)as stream:
            while True:
                _registered_archive(archive);block=stream.read(65536)
                if not block:break
                count+=len(block);pub._need(count<=item['bytes'],_CODE);sha.update(block);yield block
        pub._need((sha.hexdigest(),count)==(item['sha256'],item['bytes']),_CODE)
    path,expected=work.stream_fingerprint_source(blocks(),item['bytes'])
    pub._need(expected==(item['sha256'],item['bytes']),_CODE)
    with _SourceHandles(work.owner)as scope:
        held=scope.enter_context(HeldFile(path.parent,path.name,original.policy));pub._need(held.hash()==expected,_CODE)
        with pub._sql(path,owner=work.owner,readonly=True,timeout=0)as c:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF');yield c
        pub._need(held.hash()==expected,_CODE)


def _read_fingerprint_provenance(archive,logical,item):
    from .registered_fingerprint_relocation import NAME,ROLE,MAX_METADATA_BYTES,decode_provenance
    original=_registered_archive(archive)
    pub._need(original.assets.get(logical)==item and item['kind']=='kg'and item['role']==ROLE
        and item['relative_path']==NAME and item['storage']=='regular'
        and item['bytes']<=min(original.policy.max_manifest_bytes,MAX_METADATA_BYTES),_CODE)
    chunks=[];count=0;sha=hashlib.sha256()
    with original.archive.open(logical)as stream:
        while True:
            original.held.check();block=stream.read(65536)
            if not block:break
            count+=len(block);pub._need(count<=item['bytes'],_CODE);sha.update(block);chunks.append(block)
    pub._need((sha.hexdigest(),count)==(item['sha256'],item['bytes']),_CODE)
    raw=b''.join(chunks);decode_provenance(raw,min(original.policy.max_manifest_bytes,MAX_METADATA_BYTES));original.held.check();return raw


def prepare_original_assets(service,work,verify):
    """Authenticate, derive, copy, relocate and seal one generation; no SQL publish."""
    from .parent_restore_workflow import original_source_bundle
    from .registered_asset_restore import _Plan
    from .registered_asset_snapshot import Policy
    from . import registered_fingerprint_relocation as fp
    from contextlib import nullcontext
    bundle=original_source_bundle(service,work,verify);managed=managed_source_plan(service,work,verify)
    bundle.stack.enter_context(_held_original_media_base(service,work))
    context=registered_archive(service,work,verify)if bundle.capsule_held is not None else nullcontext(None)
    with context as archive:
        if archive is not None:
            data=registered_archive_contract(archive);manifest=data['manifest'];policy=_registered_archive(archive).policy
        else:
            manifest={'assets':[],'coverage':None};policy=Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1)
        from .standard_relocation_provenance import generation_plan
        metadata_source=lambda name,item:_fingerprint_metadata_source(archive,work,name,item)
        with pub._cold(work.work/'authenticated-cold.db',owner=work.owner)as c:
            c.row_factory=sqlite3.Row
            derived,incoming=generation_plan(c,manifest,policy,lambda name:_read_registered_provenance(archive,name))
            relocation=fp.collect_plan(c,manifest,policy,fp.platform_precision_ns(),metadata_source)
            incoming_fp=fp.incoming_plan(c,manifest,policy,lambda name,item:_read_fingerprint_provenance(archive,name,item))
        with fp.derivative_spool(work,relocation,manifest,metadata_source,incoming_fp,policy)as spool:
            outputs=fp.derived_contract(spool)
            registered=[{'relative':'payloads/'+_digest(i['logical_name'].encode())+'.bin',
                'purpose':_registered_purpose(i),'child_id':None,'references':[],
                'sha256':i['sha256'],'bytes':i['bytes']}for i in manifest['assets']]
            records=[{k:v for k,v in i.items()if k!='source_relative'}for i in managed]+registered
            records += [{'relative':name,'purpose':'metadata','child_id':None,'references':refs,
                'sha256':_digest(raw),'bytes':len(raw)}for name,(raw,refs)in derived.items()]
            records+=outputs['records']
            cap=begin_generation(service,work,records);a=_authority(cap)
            copy_managed_plan(cap,service,work,verify,managed)
            for item,target in zip(manifest['assets'],registered):cap.copy_registered(target['relative'],archive,item['logical_name'])
            with held_registered_payloads(cap):
                plan=_Plan(work.work/'authenticated-cold.db',a.root/'unused',manifest,policy,a.root,
                    parent_payloads=cap,fingerprint_relocation=relocation,derived_outputs=outputs).build()
            expected_inputs=dict(incoming);expected_inputs.update(outputs['source_inputs'])
            pub._need(plan.provenance_inputs==expected_inputs,_CODE)
            for name,(raw,refs)in derived.items():
                expected=(_digest(raw),len(raw));_copy_contract(cap,name,expected,lambda raw=raw:iter((raw,)),lambda expected=expected:expected)
            for record in outputs['records']:
                name=fp.derived_member_for_relative(spool,record['relative']);expected=(record['sha256'],record['bytes'])
                _copy_contract(cap,record['relative'],expected,lambda name=name:fp.derived_blocks(spool,name),lambda name=name:fp.verify_derived_member(spool,name))
            if bundle.metadata is not None and bundle.metadata.get('format')=='hsc_backup_full_v2':
                pub._need(_digest(pub._canonical(plan.coverage))==bundle.metadata['registered']['coverage_sha256'],_CODE)
            apply_registered_plan(cap,plan);rewrites=managed_rewrites(cap)+plan.rewrites
            fingerprint_scope=any(r['relative'].endswith('/'+fp.NAME)for r in outputs['records'])
            with pub._cold(work.work/'authenticated-cold.db',owner=work.owner)as c:
                projection=_asset_projection(c,rewrites,root=a.root,fingerprint_changes=relocation['main_changes'],fingerprint_scope=fingerprint_scope)
            with _transaction(a)as c:
                row=_generation(a,c);pub._need(row['state']=='BUILDING',_CODE);manifest_sha=_manifest_original(a.owner,c,row)
                c.execute("UPDATE restore_asset_generation SET manifest_sha256=?,projection_sha256=?,state='SEALED'WHERE generation_id=?",
                    (manifest_sha,projection,a.generation))
            verify();seal=object.__new__(_AssetSeal)
            _SEALS[seal]=SimpleNamespace(authority=a,cap=cap,service=service,verify=verify,rewrites=tuple(rewrites),
                fingerprint_changes=relocation['main_changes'],fingerprint_scope=fingerprint_scope,manifest=manifest_sha,projection=projection)
            pub._fault('after_restore_asset_seal');return seal



def _seal(seal,owner):
    pub._need(type(seal)is _AssetSeal and seal in _SEALS,_CODE);s=_SEALS[seal]
    pub._need(s.authority.owner is owner and _authority(s.cap)is s.authority,_CODE)
    from .parent_restore_workflow import original_source_bundle
    original_source_bundle(s.service,s.authority.work,s.verify);return s


def apply_original_asset_projection(seal,staged):
    owner=pub._owner(staged._restore_sql_owner());s=_seal(seal,owner)
    pub._need(_import_ticket(staged)['owner']is owner,_CODE)
    from .registered_asset_restore import _rewrite_fixed_paths,_verify_rewritten_projection
    with staged.connect()as c:protected=_rewrite_fixed_paths(c,s.rewrites,s.fingerprint_changes)
    with staged.connect()as c:
        _verify_rewritten_projection(c,protected,s.rewrites,s.fingerprint_changes);pub._need(_asset_projection(c,root=s.authority.root,fingerprint_scope=s.fingerprint_scope)==s.projection,_CODE)
    pub._fault('after_restore_asset_projection')


def adopt_original_assets(seal,staged,c):
    """Borrow the existing audit/receipt transaction; no independent commit."""
    owner=pub._owner(staged._restore_sql_owner());s=_seal(seal,owner);_owner_connection(staged,c)
    pub._need(c.in_transaction and _import_ticket(staged)['owner']is owner,_CODE)
    with pub._sql(owner.path,owner=owner,readonly=True,timeout=0)as live:
        _base(s.authority,live);original=_generation(s.authority,live)
        target=c.execute('SELECT *FROM restore_asset_generation WHERE generation_id=?',(s.authority.generation,)).fetchone()
        pub._need(original['state']=='SEALED'and target is not None and dict(target)==original,_CODE)
        for table in (MEMBER,):
            left=[tuple(r)for r in live.execute('SELECT *FROM '+table+' WHERE generation_id=? ORDER BY member_id',(s.authority.generation,))]
            right=[tuple(r)for r in c.execute('SELECT *FROM '+table+' WHERE generation_id=? ORDER BY member_id',(s.authority.generation,))]
            pub._need(left==right,_CODE)
        pub._need(_manifest_original(owner,live,original)==s.manifest and _asset_projection(c,root=s.authority.root)==s.projection,_CODE)
    c.execute("UPDATE restore_asset_generation SET state='ADOPTED'WHERE generation_id=?AND state='SEALED'",(s.authority.generation,))


def publication_asset_binding(seal,owner,prepared):
    s=_seal(seal,owner);a=s.authority
    with pub._sql(owner.path,owner=owner,readonly=True,timeout=0)as live:
        _base(a,live);g=_generation(a,live);pub._need(g['state']=='SEALED'and _manifest_original(owner,live,g)==s.manifest,_CODE)
    values={'asset_generation':a.generation,'asset_manifest_sha256':s.manifest,'asset_projection_sha256':s.projection,
        'asset_owner_binding_sha256':_digest(pub._canonical(a.binding))}
    validate_asset_publication(owner,prepared,values,committed=True)
    return values


def validate_asset_publication(owner,c,binding,*,committed):
    pub._owner(owner);validate_schema(c)
    c.row_factory=sqlite3.Row;generation=binding['asset_generation']
    row=c.execute('SELECT *FROM restore_asset_generation WHERE generation_id=?',(generation,)).fetchone()
    pub._need(row is not None,_CODE);g=dict(row)
    pub._need(g['state']==('ADOPTED'if committed else'SEALED')and g['manifest_sha256']==binding['asset_manifest_sha256']
        and g['projection_sha256']==binding['asset_projection_sha256']and g['binding_sha256']==binding['asset_owner_binding_sha256'],_CODE)
    # Internal seal binding calls do not yet have the public journal fields.
    if 'request_id'in binding:
        pub._need(g['request_id']==binding['request_id']and pub._generation_matches(g,binding)and g['source_sha256']==binding['source_sha256'],_CODE)
        from .parent_restore_ownership import _anchor
        anchor=_anchor(c);facts,_,permit,_=_generation_facts(c,g)
        if anchor is not None and anchor['request_id']==g['request_id']:
            pub._need(pub._generation_matches(g,anchor['initial']['binding'])and _digest(pub._canonical(anchor['initial']['binding']))==g['binding_sha256'],_CODE)
        else:
            # A later original restore owns the ACTIVE anchor. A prior durable
            # adoption still requires its exact completed receipt/native fact.
            pub._need(facts['state']=='finished'and facts['finalized']==1 and pub._authority_status(permit)=='finished',_CODE)
    else:permit={'status':'claimed'}
    pub._need(_manifest_original(owner,c,g)==g['manifest_sha256'],_CODE)
    if committed and pub._authority_status(permit)!='finished':pub._need(_asset_projection(c,root=owner.path.parent/'media'/g['root_name'])==g['projection_sha256'],_CODE)


def has_asset_references(c):
    # Advisory only; the opaque full source factory and publisher revalidate.
    for table,_,column in _MANAGED_COLUMNS:
        if table in pub._tables(c)and c.execute('SELECT 1 FROM '+table+' WHERE '+column+' IS NOT NULL LIMIT 1').fetchone():return True
    try:pub._db_only(c)
    except pub.RestorePublicationError as e:
        if str(e)=='RESTORE_PUBLICATION_PARENT_ASSET_BINDINGS_REQUIRED':return True
        raise
    return False


def admit_original_recovery(owner,original_request):
    from . import parent_restore_ownership as ownership
    # Complete original ledger/anchor reads before opening the schema snapshot.
    # Nested cold SQL would leave this snapshot's zero-frame family unconfirmed.
    original=ownership.current(owner)
    if original is not None:pub._need(original['request_id']==original_request,_CODE)
    with pub._cold(owner.path)as c:
        if original is None:
            # The sticky surface still exposes status after resolution. Only the
            # same original DONE anchor, archived original owner and terminal native
            # receipt facts permit that read; normal business remains unavailable.
            anchor=ownership._anchor(c);pub._need(anchor is not None and anchor['state']=='DONE'and anchor['request_id']==original_request,_CODE)
            row,_,permit,_=pub._binding_facts(c,anchor['initial']['binding'])
            pub._need((row['state']=='finished'and row['finalized']==1 and pub._authority_status(permit)=='finished')
                or(row['state']=='failed'and row['finalized']==0 and pub._authority_status(permit)=='failed'),_CODE)
        if GENERATION in pub._tables(c):validate_schema(c)
    # This only admits the existing parent recovery interface. It grants no
    # publication or unowned deletion; original native/body/PIN guards remain.


def abort_original_generations(owner,metadata):
    """Reconstitute only erase permission from original live registered IDs.

    No source backup row is used; no payload copy or publication seal is minted.
    Allocation-without-ID and any foreign namespace remain HOLD.
    """
    from . import parent_restore_ownership as ownership
    pub._owner(owner);value=ownership.current(owner,metadata)
    if value is None:return
    with pub._sql(owner.path,owner=owner,readonly=True,timeout=0)as c:
        if GENERATION not in pub._tables(c):return
        validate_schema(c);c.row_factory=sqlite3.Row
        where,args=_generation_query(metadata)
        generations=[dict(r)for r in c.execute("SELECT *FROM restore_asset_generation WHERE "+where+" AND state!='CLEANED'",args).fetchmany(2)]
        pub._need(len(generations)<=1,_CODE)
        if not generations:return
        g=generations[0];pub._need(g['state']!='ADOPTED',_CODE)
        rows=_bounded_owned_rows(c,g['generation_id'])
    work=ownership.WorkCapability(owner,value);plan={}
    from .registered_asset_snapshot import safe_relative,Policy
    policy=Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1)
    for r in rows:
        safe_relative(r['relative_name'],policy);pub._need(r['relative_name']not in plan,_CODE)
        plan[r['relative_name']]={'relative':r['relative_name'],'member_id':r['member_id'],'path_key':r['path_key'],
            'kind':r['kind'],'purpose':r['purpose'],'child_id':r['child_id'],'references':json.loads(r['reference_json']),
            'sha256':r['expected_sha256'],'bytes':r['expected_bytes']}
    media=owner.path.parent/'media'
    with _held_media(owner,media,json.loads(g['media_identity_json']))as(_,data_identity):pass
    a=SimpleNamespace(database=SimpleNamespace(path=owner.path),owner=owner,work=work,actor=pub._actor(),binding=value['binding'],media=media,
        media_identity=json.loads(g['media_identity_json']),data_identity=data_identity,root=media/g['root_name'],generation=g['generation_id'],
        plan=plan,file_count=g['expected_files'],total_bytes=g['total_bytes'],registered_payloads={},held_registered={})
    cap=object.__new__(_AssetCapability);_CAPABILITIES[cap]=a
    try:_abort_unapplied(cap)
    finally:_CAPABILITIES.pop(cap,None)


@contextmanager
def hold_publication_assets(owner,c,binding):
    from .parent_restore_workflow import _SourceHandles
    pub._owner(owner);validate_asset_publication(owner,c,binding,committed=True)
    row=c.execute('SELECT *FROM restore_asset_generation WHERE generation_id=?',(binding['asset_generation'],)).fetchone();g=dict(row)
    with _SourceHandles(owner)as stack:
        pub._need(_manifest_original(owner,c,g,stack)==binding['asset_manifest_sha256'],_CODE)
        yield
        pub._need(_manifest_original(owner,c,g)==binding['asset_manifest_sha256'],_CODE)
        for held in stack.original:
            if hasattr(held,'check'):held.check()


def _retire_guard(owner,c,metadata,j):
    from .parent_restore_ownership import _anchor
    pub._owner(owner);validate_schema(c);b=j['binding'];anchor=_anchor(c)
    pub._need(pub._uses_assets(j)and anchor is not None and anchor['state']=='ACTIVE'and anchor['request_id']==metadata['request_id']
        and pub._metadata_matches(anchor['initial']['binding'],metadata)and _digest(pub._canonical(anchor['initial']['binding']))==b['asset_owner_binding_sha256'],_CODE)
    row,generation,permit,native=pub._metadata_facts(c,metadata)
    pub._need(row['state']=='finished'and row['finalized']==1 and row['kind']in{'restore','portable_restore'}and row['kind']==b['kind']and pub._authority_status(permit)=='claimed'
        and generation==b['result_generation']and pub._marker(c)==(b['request_id'],j['binding_sha256'])
        and native==metadata and pub._metadata_matches(b,native),_CODE)


def _original_reference_present(c,root):
    from .registered_asset_restore import _REWRITE_COLUMNS,_source_path
    from .registered_asset_snapshot import Policy
    policy=Policy(MAX_FILES,128,4096,64*1024*1024,2**53-1,2**53-1);count=0
    for table,columns in _REWRITE_COLUMNS.items():
        if table not in pub._tables(c):continue
        existing={r[1]for r in c.execute('PRAGMA table_info('+table+')')}
        for col in sorted(columns&existing):
            for row in c.execute('SELECT '+col+' FROM '+table+' WHERE '+col+' IS NOT NULL AND '+col+"!=''"):
                count+=1;pub._need(count<=MAX_MEMBERS,_CODE);value=row[0]
                if type(value)is str and(value.startswith('bundle:')or value=='local://family-textbook-ocr-pages'):continue
                path=_source_path(value,policy)
                # Both directions matter for registered directory roots: a
                # surviving ancestor root might still consume an old child.
                from pathlib import PureWindowsPath
                pub._need(isinstance(path,PureWindowsPath)==(os.name=='nt'),_CODE)
                p=Path(str(path))
                if p==root or root in p.parents or p in root.parents:return True
    return False

def _unreferenced_original(c,root):pub._need(not _original_reference_present(c,root),_CODE)


@contextmanager
def _retirement_transaction(owner,metadata,j):
    with pub._sql(owner.path,owner=owner,timeout=0)as c:
        c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE');_retire_guard(owner,c,metadata,j)
        try:yield c
        except BaseException:
            try:c.rollback()
            except BaseException:owner.gate.poisoned=True
            raise
        try:c.commit();owner.parent.flush()
        except BaseException:owner.gate.poisoned=True;raise


def _retiring_inventory(owner,g,rows):
    media=owner.path.parent/'media';root=media/g['root_name'];known={r['relative_name']:r for r in rows};seen=set()
    if not os.path.lexists(io_path(root)):
        pub._need(g['state']=='RETIRING'and all(r['state']=='REMOVED'for r in rows),_CODE);return
    def walk(path,relative,identity):
        with _held_parent(owner,path,identity)as parent:
            with os.scandir(io_path(path if os.name=='nt'else parent.fd))as children:
                for child in children:
                    name=relative+'/'+child.name if relative else child.name
                    pub._need(name in known and name not in seen,_CODE);seen.add(name);row=known[name]
                    pub._need(row['state']in{'READY','REMOVING'}and row['identity_json']is not None,_CODE);original=json.loads(row['identity_json'])
                    if row['kind']=='directory':walk(path/child.name,name,original)
                    else:pub._need(list(pub._database_identity(path/child.name,parent,owner.gate)or())==original,_CODE)
    with _held_media(owner,media,json.loads(g['media_identity_json'])):walk(root,'',json.loads(g['root_identity_json']))
    pub._need({r['relative_name']for r in rows if r['state']=='READY'}<=seen
        and not any(r['relative_name']in seen for r in rows if r['state']=='REMOVED'),_CODE)


def retire_unreferenced_original_generations(owner,metadata):
    """Original current restore exclusivity spans last reference check to erase.

    A previous finished receipt proves historical binding only. Current ACTIVE
    native restore authority, current applied journal and original registered IDs
    supply erase permission. No imported row, prepared object or recursive tree
    cleanup is accepted.
    """
    from . import parent_restore_ownership as ownership
    pub._owner(owner);value=ownership.current(owner,metadata)
    if value is None:return
    raw=pub._read(owner.parent,pub._names(owner.path)[1])
    if raw is None:return  # Unapplied cleanup is handled by its own path.
    j=pub._decode(raw)
    if not pub._uses_assets(j):return
    state=pub.original_publication_metadata(owner,metadata)['state']
    if state in{'UNAPPLIED','ABORTED_NOT_APPLIED'}:return
    pub._need(state in{'DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS','COMPLETE'},_CODE)
    with pub._sql(owner.path,owner=owner,readonly=True,timeout=0)as c:
        _retire_guard(owner,c,metadata,j)
        old=[dict(r)for r in c.execute("SELECT *FROM restore_asset_generation WHERE generation_id!=?AND state IN('ADOPTED','RETIRING')",
            (j['binding']['asset_generation'],)).fetchmany(2)]
        pub._need(len(old)<=1,_CODE)
        if not old:return
        previous=pub._decode(pub._read(owner.parent,pub._names(owner.path)[2]));b=previous['binding'];g=old[0]
        pub._need(pub._uses_assets(previous)and previous['phase']=='COMPLETE'and b['asset_generation']==g['generation_id']
            and b['asset_owner_binding_sha256']==g['binding_sha256']and b['asset_manifest_sha256']==g['manifest_sha256']
            and b['asset_projection_sha256']==g['projection_sha256']and b['request_id']==g['request_id']and pub._generation_matches(g,b)
            and b['source_sha256']==g['source_sha256'],_CODE)
        prior,_,permit,native=_generation_facts(c,g)
        pub._need(prior['state']=='finished'and prior['finalized']==1 and pub._authority_status(permit)=='finished'
            and prior['origin_generation']==b['origin_generation']and prior['result_generation']==b['result_generation']
            and prior['request_sha256']==b['request_sha256']and prior['cleanup_manifest_sha256']==b['cleanup_manifest_sha256']
            and prior['reference_type']=='audit_event'and prior['reference_id']==b['audit_id']
            and pub._metadata_matches(b,native),_CODE)
        if g['state']=='ADOPTED':pub._need(_manifest_original(owner,c,g)==g['manifest_sha256'],_CODE)
        rows=_bounded_owned_rows(c,g['generation_id'])
    media=owner.path.parent/'media';root=media/g['root_name'];by_name={r['relative_name']:r for r in rows}
    pub._need(len(by_name)==len(rows),_CODE)
    pub._fault('before_restore_asset_retire')
    with _retirement_transaction(owner,metadata,j)as c:
        _unreferenced_original(c,root)
        if g['state']=='ADOPTED':
            c.execute("UPDATE restore_asset_generation SET state='RETIRING'WHERE generation_id=?AND state='ADOPTED'",(g['generation_id'],));g['state']='RETIRING'
    # No await, foreign callback or normal writer admission occurs between this
    # last reference proof and removal; the original exclusive owner stays held.
    pub._fault('after_restore_asset_retire_intent');_retiring_inventory(owner,g,rows)
    from contextlib import ExitStack
    def parent_of(relative):
        stack=ExitStack()
        try:
            stack.enter_context(_held_media(owner,media,json.loads(g['media_identity_json'])))
            parent=stack.enter_context(_held_parent(owner,root,json.loads(g['root_identity_json'])))
            parts=relative.split('/')
            for depth in range(1,len(parts)):
                r=by_name['/'.join(parts[:depth])];pub._need(r['state']=='READY',_CODE)
                parent=stack.enter_context(_held_parent(owner,root/r['relative_name'],json.loads(r['identity_json'])))
            return stack,parent
        except BaseException:stack.close();raise
    a=SimpleNamespace(owner=owner)
    for row in sorted(rows,key=lambda r:(-r['relative_name'].count('/'),r['kind']=='directory',r['relative_name'])):
        if row['state']=='REMOVED':continue
        pub._need(row['state']in{'READY','REMOVING'},_CODE);removing=row['state']=='REMOVING'
        if not removing:
            with _retirement_transaction(owner,metadata,j)as c:
                c.execute("UPDATE restore_asset_member SET state='REMOVING'WHERE generation_id=?AND member_id=?AND state='READY'",(g['generation_id'],row['member_id']))
            row['state']='REMOVING'
        stack,parent=parent_of(row['relative_name'])
        with stack:
            path=root/row['relative_name']
            if os.path.lexists(io_path(path)):_erase(a,parent,path,row['kind'],json.loads(row['identity_json']))
            else:pub._need(removing,_CODE)
        pub._fault('after_restore_asset_retire_member_unlinked')
        with _retirement_transaction(owner,metadata,j)as c:
            c.execute("UPDATE restore_asset_member SET state='REMOVED'WHERE generation_id=?AND member_id=?AND state='REMOVING'",(g['generation_id'],row['member_id']))
        row['state']='REMOVED'
    _retiring_inventory(owner,g,rows)
    with _held_media(owner,media,json.loads(g['media_identity_json']))as(parent,_):
        if os.path.lexists(io_path(root)):_erase(a,parent,root,'directory',json.loads(g['root_identity_json']))
    pub._fault('after_restore_asset_retired_root_unlinked')
    with _retirement_transaction(owner,metadata,j)as c:c.execute("UPDATE restore_asset_generation SET state='CLEANED'WHERE generation_id=?AND state='RETIRING'",(g['generation_id'],))


def _delete_original_facts(c,job,nonce=None,*,completed=False):
    from .native_privacy import FIELDS,read
    pub._need(job is not None and job['target_type']=='child_scope'and job['scope']in{'media','all_history'},_CODE)
    request=job['client_action_id'];row=c.execute('SELECT *FROM privacy_operation_intent WHERE request_id=?',(request,)).fetchone()
    pub._need(row is not None and row['kind']=='delete'and row['child_id']==job['child_id']and row['scope']==job['scope']
        and type(row['request_sha256'])is str and pub._HEX.fullmatch(row['request_sha256'])
        and type(row['cleanup_manifest_sha256'])is str and pub._HEX.fullmatch(row['cleanup_manifest_sha256']),_CODE)
    if nonce is None:
        permits=pub._bounded_rows(c,"SELECT nonce,metadata_json FROM native_privacy_permit WHERE status='claimed'LIMIT ?",(pub.MAX_NATIVE_CANDIDATES+1,),pub.MAX_NATIVE_CANDIDATES,_CODE)
    else:permits=c.execute('SELECT nonce,metadata_json FROM native_privacy_permit WHERE nonce=?',(nonce,)).fetchmany(2)
    matches=[]
    for permit in permits:
        metadata=pub._json_object(permit['metadata_json'],8192,_CODE)
        if metadata.get('request_id')==request:matches.append((permit,metadata))
    pub._need(len(matches)==1,_CODE);permit,metadata=matches[0]
    pub._need(set(metadata)==FIELDS and metadata['nonce']==permit['nonce']and metadata['request_id']==request
        and metadata['kind']=='delete'and metadata['child_id']==row['child_id']and metadata['scope']==row['scope']
        and metadata['expected_generation']==row['origin_generation']and metadata['cleanup_manifest_sha256']==row['cleanup_manifest_sha256']
        and type(metadata['command_sha256'])is str and pub._HEX.fullmatch(metadata['command_sha256']),_CODE)
    status=read(c,metadata)['status']
    if completed:
        pub._need(row['state']=='finished'and row['finalized']==1 and row['reference_type']=='deletion_job'
            and row['reference_id']==job['deletion_id']and row['result_generation']==row['origin_generation']
            and job['status']=='completed'and status=='finished',_CODE)
    else:
        current=c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()
        pub._need(current is not None and current[0]==row['origin_generation']and row['state']=='processing'
            and row['finalized']==0 and row['reference_id']is None and status=='claimed',_CODE)
    return dict(row),metadata


def _member_preimage_sha(member):
    value=dict(member);value['state']='READY';return _digest(pub._canonical(value))


def _completed_erasure_preimage(c,g,member,root):
    record=c.execute('SELECT *FROM restore_asset_erasure WHERE generation_id=?AND member_id=?',
        (g['generation_id'],member['member_id'])).fetchone()
    pub._need(record is not None and record['state']=='ERASED'and member['kind']=='file'and member['purpose']=='managed'
        and record['child_id']==member['child_id']and record['member_sha256']==_member_preimage_sha(member),_CODE)
    job=c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(record['deletion_id'],)).fetchone()
    row,metadata=_delete_original_facts(c,job,record['native_nonce'],completed=True)
    pub._need(record['request_id']==row['request_id']and record['child_id']==row['child_id']and record['scope']==row['scope']
        and record['origin_generation']==row['origin_generation']and record['request_sha256']==row['request_sha256']
        and record['cleanup_manifest_sha256']==row['cleanup_manifest_sha256']
        and record['native_metadata_sha256']==_digest(pub._canonical(metadata)),_CODE)
    # A completed receipt is historical evidence, not deletion authority. The
    # caller still holds the original current owner and checks absence/refs.
    _unreferenced_original(c,root/member['relative_name'])
    fingerprint=json.loads(record['fingerprint_json'])
    from .registered_asset_snapshot import parse_registered_fingerprint
    pub._need(parse_registered_fingerprint(fingerprint)[0]==member['expected_bytes'],_CODE)
    return fingerprint


def prepare_original_privacy_erasures(database,c,job):
    """Borrow original reference-removal transaction before any file erase.

    The complete original manifest is authenticated before recording immutable
    member/fingerprint preimages. A backup row or a later stat cannot supply it.
    """
    from . import parent_restore_privacy as parent_privacy
    parent_cap=parent_privacy.current(database)
    if parent_cap is not None and not parent_privacy._cap(parent_cap).native:return False
    if GENERATION not in pub._tables(c):return False
    validate_schema(c);c.row_factory=sqlite3.Row
    rows=c.execute("SELECT m.*,g.root_name FROM restore_asset_member m JOIN restore_asset_generation g USING(generation_id)WHERE g.state='ADOPTED'AND m.kind='file'AND m.purpose='managed'AND m.child_id=?AND m.state!='REMOVED'LIMIT ?",
        (job['child_id'],MAX_FILES+1)).fetchall();pub._need(len(rows)<=MAX_FILES,_CODE)
    if not rows:return False
    owner=_owner_connection(database,c);pub._need(c.in_transaction,_CODE);facts,metadata=_delete_original_facts(c,job)
    pub._fault('before_restore_asset_privacy_preimage');preimages={}
    for generation in sorted({r['generation_id']for r in rows}):
        value=c.execute('SELECT *FROM restore_asset_generation WHERE generation_id=?',(generation,)).fetchone();g=dict(value)
        fingerprints={};pub._need(_manifest_original(owner,c,g,preimages=fingerprints)==g['manifest_sha256'],_CODE)
        preimages[generation]=fingerprints
    for item in rows:
        member=dict(item);member.pop('root_name');pub._need(member['state']=='READY',_CODE)
        fingerprint=preimages[member['generation_id']][member['relative_name']]
        c.execute('INSERT INTO restore_asset_erasure VALUES('+','.join('?'for _ in range(14))+')',
            (member['generation_id'],member['member_id'],facts['request_id'],metadata['nonce'],job['deletion_id'],facts['child_id'],facts['scope'],
             facts['origin_generation'],facts['request_sha256'],facts['cleanup_manifest_sha256'],_digest(pub._canonical(metadata)),
             _member_preimage_sha(member),pub._canonical(fingerprint).decode(),'INTENT'))
        c.execute("UPDATE restore_asset_member SET state='REMOVING'WHERE generation_id=?AND member_id=?AND state='READY'",
            (member['generation_id'],member['member_id']))
    # This is still the borrowed transaction; caller commits with reference
    # removal and its original deletion job before the first unlink.
    return True


def erase_original_privacy_member(database,job,path):
    """Erase only a recorded original ID under the same exclusive App owner."""
    path=pub._path(path)
    with database.connect()as c:
        if GENERATION not in pub._tables(c):return None
        validate_schema(c);c.row_factory=sqlite3.Row
        generations=c.execute("SELECT *FROM restore_asset_generation WHERE state='ADOPTED'LIMIT ?",(MAX_FILES+1,)).fetchall()
        pub._need(len(generations)<=MAX_FILES,_CODE);found=None
        for row in generations:
            root=database.path.parent/'media'/row['root_name']
            if root==path or root in path.parents:
                pub._need(found is None,_CODE);g=dict(row)
                relative=path.relative_to(root).as_posix();member=c.execute('SELECT *FROM restore_asset_member WHERE generation_id=?AND relative_name=?',(g['generation_id'],relative)).fetchone()
                pub._need(member is not None and member['kind']=='file'and member['purpose']=='managed',_CODE)
                found=(g,dict(member))
        if found is None:return None
        owner=_owner_connection(database,c);g,member=found
        from . import parent_restore_privacy as parent_privacy
        parent_cap=parent_privacy.current(database)
        if parent_cap is not None and not parent_privacy._cap(parent_cap).native:
            return parent_privacy.erase_member(database,job,path,g,member)
        current=c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(job['deletion_id'],)).fetchone()
        facts,metadata=_delete_original_facts(c,current)
        record=c.execute('SELECT *FROM restore_asset_erasure WHERE generation_id=?AND member_id=?',(g['generation_id'],member['member_id'])).fetchone()
        pub._need(record is not None and(record['state'],member['state'])in{('INTENT','REMOVING'),('ERASED','REMOVED')}
            and record['deletion_id']==job['deletion_id']and record['request_id']==facts['request_id']
            and record['native_nonce']==metadata['nonce']and record['child_id']==member['child_id']==job['child_id']
            and record['member_sha256']==_member_preimage_sha(member)
            and record['native_metadata_sha256']==_digest(pub._canonical(metadata)),_CODE)
        _unreferenced_original(c,path);record=dict(record)
        directories={r['relative_name']:r for r in _bounded_owned_rows(c,g['generation_id'])if r['kind']=='directory'}
        if record['state']=='ERASED':pub._need(not os.path.lexists(io_path(path)),_CODE);return False
    try:owner.parent.flush()
    except BaseException:owner.gate.poisoned=True;raise
    pub._fault('after_restore_asset_privacy_preimage_committed')
    root=database.path.parent/'media'/g['root_name'];identity=json.loads(member['identity_json'])
    from .parent_restore_workflow import _SourceHandles
    from .registered_asset_snapshot import HeldFile,Policy,held_registered_fingerprint
    with _SourceHandles(owner)as stack:
        stack.enter_context(_held_media(owner,database.path.parent/'media',json.loads(g['media_identity_json'])))
        parent=stack.enter_context(_held_parent(owner,root,json.loads(g['root_identity_json'])))
        parts=member['relative_name'].split('/')
        for depth in range(1,len(parts)):
            relative='/'.join(parts[:depth]);directory=directories[relative];pub._need(directory['state']=='READY',_CODE)
            parent=stack.enter_context(_held_parent(owner,root/relative,json.loads(directory['identity_json'])))
        pub._need(os.path.lexists(io_path(path)),_CODE)
        with HeldFile(root,member['relative_name'],Policy(MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1))as held:
            original=list(held.before['identity'])if os.name=='nt'else[held.before.st_dev,held.before.st_ino]
            pub._need(original==identity and held.hash()==(member['expected_sha256'],member['expected_bytes'])
                and held_registered_fingerprint(held)==json.loads(record['fingerprint_json']),_CODE)
        pub._fault('before_restore_asset_privacy_erase');_erase(SimpleNamespace(owner=owner),parent,path,'file',identity)
        pub._fault('after_restore_asset_privacy_erased')
        with pub._sql(owner.path,owner=owner,timeout=0)as c:
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(job['deletion_id'],)).fetchone()
            _,same=_delete_original_facts(c,current,metadata['nonce']);pub._need(same==metadata,_CODE);_unreferenced_original(c,path)
            pub._need(not os.path.lexists(io_path(path)),_CODE)
            c.execute("UPDATE restore_asset_erasure SET state='ERASED'WHERE generation_id=?AND member_id=?AND state='INTENT'",(g['generation_id'],member['member_id']))
            c.execute("UPDATE restore_asset_member SET state='REMOVED'WHERE generation_id=?AND member_id=?AND state='REMOVING'",(g['generation_id'],member['member_id']))
            try:c.commit();owner.parent.flush()
            except BaseException:owner.gate.poisoned=True;raise
        pub._fault('after_restore_asset_privacy_erasure_recorded')
    return True
