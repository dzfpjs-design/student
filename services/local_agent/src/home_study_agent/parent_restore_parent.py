"""Explicit Parent-session restore authority: isolated schema88 candidate.

Native nonce/permit facts are never fabricated or interpreted as Parent facts.
Parent authority is separate from native permits and delete authority.
"""
import asyncio,re,sqlite3
from . import restore_publication as pub
CODE='RESTORE_PARENT_AUTHORITY_UNCONFIRMED'
NATIVE='NATIVE_PERMIT'
PARENT='PARENT_SESSION_RESTORE'
GENERATION_SQL_88="CREATE TABLE restore_asset_generation(\n generation_id TEXT PRIMARY KEY CHECK(typeof(generation_id)='text'AND length(generation_id)=32 AND generation_id NOT GLOB '*[^0-9a-f]*'),\n request_id TEXT NOT NULL CHECK(typeof(request_id)='text'AND length(CAST(request_id AS BLOB))BETWEEN 1 AND 128),\n binding_sha256 TEXT NOT NULL CHECK(typeof(binding_sha256)='text'AND length(binding_sha256)=64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),\n source_sha256 TEXT NOT NULL CHECK(typeof(source_sha256)='text'AND length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),\n root_name TEXT NOT NULL UNIQUE CHECK(root_name='restore-generation-'||generation_id),\n media_identity_json TEXT NOT NULL CHECK(typeof(media_identity_json)='text'AND length(CAST(media_identity_json AS BLOB))BETWEEN 1 AND 256),\n root_identity_json TEXT CHECK(root_identity_json IS NULL OR(typeof(root_identity_json)='text'AND length(CAST(root_identity_json AS BLOB))BETWEEN 1 AND 256)),\n manifest_sha256 TEXT CHECK(manifest_sha256 IS NULL OR(typeof(manifest_sha256)='text'AND length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*')),\n projection_sha256 TEXT CHECK(projection_sha256 IS NULL OR(typeof(projection_sha256)='text'AND length(projection_sha256)=64 AND projection_sha256 NOT GLOB '*[^0-9a-f]*')),\n expected_files INTEGER NOT NULL CHECK(typeof(expected_files)='integer'AND expected_files BETWEEN 0 AND 100000),\n total_bytes INTEGER NOT NULL CHECK(typeof(total_bytes)='integer'AND total_bytes BETWEEN 0 AND 9007199254740991),\n state TEXT NOT NULL CHECK(state IN('ALLOCATING','BUILDING','SEALED','ADOPTED','RETIRING','CLEANED')), native_nonce TEXT CHECK(native_nonce IS NULL OR(typeof(native_nonce)='text'AND length(CAST(native_nonce AS BLOB))BETWEEN 1 AND 128)), authority_kind TEXT NOT NULL DEFAULT 'NATIVE_PERMIT' CHECK(authority_kind IN('NATIVE_PERMIT','PARENT_SESSION_RESTORE')), parent_authority_id TEXT REFERENCES restore_parent_authority(authority_id)CHECK((authority_kind='NATIVE_PERMIT'AND native_nonce IS NOT NULL AND parent_authority_id IS NULL)OR(authority_kind='PARENT_SESSION_RESTORE'AND native_nonce IS NULL AND typeof(parent_authority_id)='text'AND length(parent_authority_id)=32 AND parent_authority_id NOT GLOB '*[^0-9a-f]*')),\n CHECK(state NOT IN('BUILDING','SEALED','ADOPTED')OR root_identity_json IS NOT NULL),\n CHECK(state NOT IN('SEALED','ADOPTED')OR(manifest_sha256 IS NOT NULL AND projection_sha256 IS NOT NULL))\n)"
AUTHORITY='restore_parent_authority'
AUTHORITY_SQL="""CREATE TABLE restore_parent_authority(
 authority_id TEXT PRIMARY KEY CHECK(typeof(authority_id)='text'AND length(authority_id)=32 AND authority_id NOT GLOB '*[^0-9a-f]*'),
 request_id TEXT NOT NULL UNIQUE REFERENCES privacy_operation_intent(request_id),
 kind TEXT NOT NULL CHECK(kind='portable_restore'),
 origin_generation TEXT NOT NULL CHECK(typeof(origin_generation)='text'AND length(CAST(origin_generation AS BLOB))BETWEEN 1 AND 128),
 request_sha256 TEXT NOT NULL CHECK(typeof(request_sha256)='text'AND length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
 command_sha256 TEXT NOT NULL CHECK(typeof(command_sha256)='text'AND length(command_sha256)=64 AND command_sha256 NOT GLOB '*[^0-9a-f]*'),
 cleanup_manifest_sha256 TEXT NOT NULL CHECK(typeof(cleanup_manifest_sha256)='text'AND length(cleanup_manifest_sha256)=64 AND cleanup_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
 source_sha256 TEXT NOT NULL CHECK(typeof(source_sha256)='text'AND length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
 policy TEXT NOT NULL CHECK(policy='PARENT_NO_NATIVE_NO_MANAGED'),
 result_generation TEXT CHECK(result_generation IS NULL OR(typeof(result_generation)='text'AND length(CAST(result_generation AS BLOB))BETWEEN 1 AND 128)),
 result_controlled_sha256 TEXT CHECK(result_controlled_sha256 IS NULL OR(typeof(result_controlled_sha256)='text'AND length(result_controlled_sha256)=64 AND result_controlled_sha256 NOT GLOB '*[^0-9a-f]*')),
 result_native_sha256 TEXT CHECK(result_native_sha256 IS NULL OR(typeof(result_native_sha256)='text'AND length(result_native_sha256)=64 AND result_native_sha256 NOT GLOB '*[^0-9a-f]*')),
 state TEXT NOT NULL CHECK(state IN('ACTIVE','FINISHED','FAILED')),
 CHECK((state='FINISHED'AND result_generation IS NOT NULL AND result_controlled_sha256 IS NOT NULL AND result_native_sha256 IS NOT NULL)OR(state IN('ACTIVE','FAILED')AND result_generation IS NULL AND result_controlled_sha256 IS NULL AND result_native_sha256 IS NULL))
)"""
AUTHORITY_TRIGGER="""CREATE TRIGGER restore_parent_authority_immutable BEFORE UPDATE ON restore_parent_authority
WHEN NEW.authority_id IS NOT OLD.authority_id OR NEW.request_id IS NOT OLD.request_id OR NEW.kind IS NOT OLD.kind
 OR NEW.origin_generation IS NOT OLD.origin_generation OR NEW.request_sha256 IS NOT OLD.request_sha256 OR NEW.command_sha256 IS NOT OLD.command_sha256
 OR NEW.cleanup_manifest_sha256 IS NOT OLD.cleanup_manifest_sha256 OR NEW.source_sha256 IS NOT OLD.source_sha256 OR NEW.policy IS NOT OLD.policy
 OR OLD.result_controlled_sha256 IS NOT NULL OR OLD.result_native_sha256 IS NOT NULL
 OR NOT(OLD.state='ACTIVE'AND((NEW.state='FINISHED'AND NEW.result_generation IS NOT NULL AND NEW.result_controlled_sha256 IS NOT NULL AND NEW.result_native_sha256 IS NOT NULL)OR(NEW.state='FAILED'AND NEW.result_generation IS NULL AND NEW.result_controlled_sha256 IS NULL AND NEW.result_native_sha256 IS NULL)))
BEGIN SELECT RAISE(ABORT,'parent restore authority is immutable');END"""
AUTHORITY_DELETE="CREATE TRIGGER restore_parent_authority_no_delete BEFORE DELETE ON restore_parent_authority BEGIN SELECT RAISE(ABORT,'parent restore history is required');END"
AUTHORITY_OBJECTS=(('table',AUTHORITY,AUTHORITY_SQL),('trigger','restore_parent_authority_immutable',AUTHORITY_TRIGGER),('trigger','restore_parent_authority_no_delete',AUTHORITY_DELETE))

def migrate_generation_87(database,c):
    """Atomic ALTER only; original PK/FKs/table stay live throughout.

    Caller retains the actual original publication owner/StudyConnection. FK
    checks are never disabled or cleared. A failed step/COMMIT rolls back all
    changes, including the exact original immutable trigger.
    """
    from . import parent_restore_assets as assets
    owner=assets._owner_connection(database,c)
    pub._need(c.in_transaction and c.execute('PRAGMA foreign_keys').fetchone()[0]==1,CODE)
    assets.validate_schema(c)
    names=tuple(row[1]for row in c.execute('PRAGMA table_info(restore_asset_generation)'))
    pub._need(names==('generation_id','request_id','native_nonce','binding_sha256','source_sha256','root_name','media_identity_json','root_identity_json','manifest_sha256','projection_sha256','expected_files','total_bytes','state'),CODE)
    # Unknown external views/triggers or incoming FKs cannot gain schema/write
    # effects through our column migration. The original strict objects remain.
    allowed={name for kind,name,_ in assets.OBJECTS if kind=='trigger'}
    rows=pub._bounded_rows(c,"SELECT type,name,sql FROM sqlite_master WHERE type IN('trigger','view')LIMIT 100001",(),100000,CODE)
    for kind,name,sql in rows:
        pub._need(type(sql)is str and len(sql.encode())<=65536,CODE)
        if re.search(r'\brestore_asset_generation\b',sql,re.I):pub._need(kind=='trigger'and name in allowed,CODE)
    tables=pub._bounded_rows(c,"SELECT name FROM sqlite_master WHERE type='table'LIMIT 100001",(),100000,CODE)
    for row in tables:
        table=row[0];pub._need(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',table)is not None,CODE)
        for fk in c.execute('PRAGMA foreign_key_list('+table+')'):
            if fk[2].lower()=='restore_asset_generation':
                pub._need(table=='restore_asset_member'and tuple(fk)==(0,0,'restore_asset_generation','generation_id','generation_id','NO ACTION','NO ACTION','NONE'),CODE)
    before=[tuple(row)for row in c.execute('SELECT *FROM restore_asset_generation ORDER BY generation_id')]
    pub._need(len(before)<=assets.MAX_FILES,CODE)
    pub._need(not c.execute("SELECT 1 FROM sqlite_master WHERE lower(name)IN('restore_parent_authority','restore_parent_authority_immutable','restore_parent_authority_no_delete')LIMIT 1").fetchone(),CODE)
    for _,_,sql in AUTHORITY_OBJECTS:c.execute(sql)
    c.execute('DROP TRIGGER restore_asset_generation_immutable')
    c.execute("ALTER TABLE restore_asset_generation ADD COLUMN native_nonce_v88 TEXT CHECK(native_nonce_v88 IS NULL OR(typeof(native_nonce_v88)='text'AND length(CAST(native_nonce_v88 AS BLOB))BETWEEN 1 AND 128))")
    c.execute('UPDATE restore_asset_generation SET native_nonce_v88=native_nonce')
    c.execute("ALTER TABLE restore_asset_generation ADD COLUMN authority_kind TEXT NOT NULL DEFAULT 'NATIVE_PERMIT' CHECK(authority_kind IN('NATIVE_PERMIT','PARENT_SESSION_RESTORE'))")
    c.execute("ALTER TABLE restore_asset_generation ADD COLUMN parent_authority_id TEXT REFERENCES restore_parent_authority(authority_id)CHECK((authority_kind='NATIVE_PERMIT'AND native_nonce_v88 IS NOT NULL AND parent_authority_id IS NULL)OR(authority_kind='PARENT_SESSION_RESTORE'AND native_nonce_v88 IS NULL AND typeof(parent_authority_id)='text'AND length(parent_authority_id)=32 AND parent_authority_id NOT GLOB '*[^0-9a-f]*'))")
    c.execute('ALTER TABLE restore_asset_generation DROP COLUMN native_nonce')
    c.execute('ALTER TABLE restore_asset_generation RENAME COLUMN native_nonce_v88 TO native_nonce')
    new_names=tuple(row[1]for row in c.execute('PRAGMA table_info(restore_asset_generation)'))
    pub._need(set(new_names)==set(names)|{'authority_kind','parent_authority_id'},CODE)
    selected=','.join(names)
    pub._need(before==[tuple(row)for row in c.execute('SELECT '+selected+' FROM restore_asset_generation ORDER BY generation_id')],CODE)
    pub._need(not c.execute("SELECT 1 FROM restore_asset_generation WHERE authority_kind!='NATIVE_PERMIT'OR parent_authority_id IS NOT NULL LIMIT 1").fetchone(),CODE)
    sql=assets.GENERATION_TRIGGER.replace('OR NEW.native_nonce IS NOT OLD.native_nonce',
        'OR NEW.native_nonce IS NOT OLD.native_nonce OR NEW.authority_kind IS NOT OLD.authority_kind OR NEW.parent_authority_id IS NOT OLD.parent_authority_id')
    c.execute(sql)
    pub._need(c.execute("SELECT sql FROM sqlite_master WHERE name='restore_asset_generation'").fetchone()[0]==GENERATION_SQL_88,CODE)
    pub._need(c.execute('PRAGMA foreign_key_check').fetchone()is None,CODE)
    owner.parent.check()
    return c.execute("SELECT sql FROM sqlite_master WHERE name='restore_asset_generation'").fetchone()[0]


# All tokens, full command bodies and ParentAuth instances stay in memory.
# Values do not strongly reference weak database keys.
import weakref
from contextlib import ExitStack,asynccontextmanager,contextmanager
from types import SimpleNamespace
from uuid import uuid4
_RECOVERY_AUTH=weakref.WeakKeyDictionary()
_CAPS=weakref.WeakKeyDictionary()
_CURRENT=weakref.WeakKeyDictionary()
class _ParentRestore:
    __slots__=('__weakref__',)
    def __new__(cls):raise pub.RestorePublicationError(CODE)

def bind_recovery_auth(database,auth,*,native_key_present):
    from .parent_auth import ParentAuth
    pub._need(type(auth)is ParentAuth and auth.database is database,CODE)
    old=_RECOVERY_AUTH.get(database)
    pub._need(old is None or(old[0]()is auth and old[1]is bool(native_key_present)and old[2]),CODE)
    _RECOVERY_AUTH[database]=(weakref.ref(auth),bool(native_key_present),True)

def retire_recovery_auth(database,auth):
    old=_RECOVERY_AUTH.get(database)
    if old is not None and old[0]()is auth:_RECOVERY_AUTH[database]=(old[0],old[1],False)

def _auth(database,auth):
    from .parent_restore_privacy import _AUTH
    original=_AUTH.get(database)
    recovery=_RECOVERY_AUTH.get(database)
    valid=[r for r in(original,recovery)if r is not None and r[0]()is auth and r[2]]
    pub._need(len(valid)>=1 and all(r[1]is False for r in valid)and auth.database is database,CODE)

def managed_projection(c):
    if 'controlled_launch_epoch'not in pub._tables(c):return pub._sha(pub._canonical([]))
    rows=pub._bounded_rows(c,'SELECT child_id,epoch FROM controlled_launch_epoch ORDER BY child_id LIMIT 100001',(),100000,CODE)
    pub._need(all(type(v)is str and pub._ID.fullmatch(v)for row in rows for v in row),CODE)
    return pub._sha(pub._canonical([list(row)for row in rows]))

_NATIVE_COLUMNS="CASE WHEN typeof(nonce)='text'AND length(CAST(nonce AS BLOB))BETWEEN 1 AND 128 THEN nonce ELSE NULL END AS nonce,CASE WHEN typeof(metadata_json)='text'AND length(CAST(metadata_json AS BLOB))BETWEEN 1 AND 8192 THEN metadata_json ELSE NULL END AS metadata_json,CASE WHEN status IN('claimed','finished','failed','cancelled')THEN status ELSE NULL END AS status"

def native_projection(c):
    if 'native_privacy_permit'not in pub._tables(c):return pub._sha(pub._canonical([]))
    rows=pub._bounded_rows(c,'SELECT '+_NATIVE_COLUMNS+' FROM native_privacy_permit ORDER BY nonce LIMIT ?',
        (pub.MAX_NATIVE_CANDIDATES+1,),pub.MAX_NATIVE_CANDIDATES,CODE)
    from .native_privacy import FIELDS
    for nonce,raw,status in rows:
        metadata=pub._json_object(raw,8192,CODE)
        pub._need(set(metadata)==FIELDS and metadata['nonce']==nonce and status in{'finished','failed','cancelled'},CODE)
    return pub._sha(pub._canonical([list(row)for row in rows]))

def qualified_policy(c):
    # Only a current original FINISHED Parent result can explain managed epochs.
    # Source backup authority tables have already been discarded before publish.
    native=native_projection(c)
    if 'controlled_launch_epoch'not in pub._tables(c)or not c.execute('SELECT 1 FROM controlled_launch_epoch LIMIT 1').fetchone():return
    validate_schema(c)
    generation=c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()
    pub._need(generation is not None,CODE)
    rows=pub._bounded_rows(c,"SELECT request_id,authority_id FROM restore_parent_authority WHERE state='FINISHED'AND result_generation=?LIMIT 3",(generation[0],),2,CODE)
    pub._need(len(rows)==1,CODE)
    row,_,a,_=facts(c,rows[0][0],rows[0][1])
    pub._need(a['result_controlled_sha256']==managed_projection(c)and a['result_native_sha256']==native,CODE)

def _no_native(c,request_id):
    if 'native_privacy_permit'not in pub._tables(c):return
    from .native_privacy import FIELDS
    rows=pub._bounded_rows(c,'SELECT '+_NATIVE_COLUMNS+' FROM native_privacy_permit LIMIT ?',
        (pub.MAX_NATIVE_CANDIDATES+1,),pub.MAX_NATIVE_CANDIDATES,CODE)
    for row in rows:
        m=pub._json_object(row[1],8192,CODE)
        pub._need(set(m)==FIELDS and m.get('nonce')==row[0] and row[2]in{'claimed','finished','failed','cancelled'}and m.get('request_id')!=request_id,CODE)

def validate_schema(c):
    for kind,name,sql in AUTHORITY_OBJECTS:
        rows=c.execute('SELECT type,name,sql FROM sqlite_master WHERE lower(name)=?',(name,)).fetchmany(2)
        pub._need([tuple(r)for r in rows]==[(kind,name,sql)],CODE)
    names={n for k,n,_ in AUTHORITY_OBJECTS if k=='trigger'}
    actual=c.execute("SELECT name FROM sqlite_master WHERE type='trigger'AND lower(tbl_name)='restore_parent_authority'").fetchall()
    pub._need({r[0]for r in actual}==names,CODE)

def exists(c,request_id):
    if AUTHORITY not in pub._tables(c):return False
    validate_schema(c)
    return c.execute('SELECT 1 FROM restore_parent_authority WHERE request_id=?',(request_id,)).fetchone()is not None

def facts(c,request_id,authority_id=None):
    validate_schema(c);c.row_factory=sqlite3.Row;_no_native(c,request_id)
    row=c.execute('SELECT *FROM privacy_operation_intent WHERE request_id=?',(request_id,)).fetchone()
    a=c.execute('SELECT *FROM restore_parent_authority WHERE request_id=?',(request_id,)).fetchone()
    pub._need(row is not None and a is not None,CODE);row=dict(row);a=dict(a)
    pub._need(authority_id is None or a['authority_id']==authority_id,CODE)
    pub._need(row['kind']==a['kind']=='portable_restore'and row['child_id']is None and row['scope']is None
        and all(row[k]==a[k]for k in('origin_generation','request_sha256','cleanup_manifest_sha256','source_sha256'))
        and a['policy']=='PARENT_NO_NATIVE_NO_MANAGED',CODE)
    if a['state']=='FINISHED':
        pub._need(row['state']=='finished'and row['finalized']==1 and row['result_generation']==a['result_generation'],CODE)
        finished_audit(c,row)
    if a['state']=='FAILED':pub._need(row['state']=='failed'and row['finalized']==0 and row['result_generation']is None,CODE)
    generation=c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()
    pub._need(generation is not None,CODE)
    metadata={k:a[k]for k in('authority_id','request_id','kind','origin_generation','request_sha256','command_sha256','cleanup_manifest_sha256','source_sha256','policy')}
    metadata['authority_kind']=PARENT
    return row,generation[0],a,metadata

def finished_audit(c,row):
    pub._need(row['reference_type']=='audit_event'and type(row['reference_id'])is str,CODE)
    audit=c.execute("SELECT audit_id,actor,action,child_id,CASE WHEN typeof(summary_json)='text'AND length(CAST(summary_json AS BLOB))BETWEEN 1 AND 8192 THEN summary_json ELSE NULL END AS summary_json,CASE WHEN typeof(occurred_at)='text'AND length(CAST(occurred_at AS BLOB))BETWEEN 1 AND 128 THEN occurred_at ELSE NULL END AS occurred_at FROM audit_event WHERE audit_id=?AND actor='parent'AND action='portable_backup_restored'AND child_id IS NULL",(row['reference_id'],)).fetchone()
    pub._need(audit is not None and audit['actor']=='parent'and audit['action']=='portable_backup_restored',CODE)
    summary=pub._json_object(audit['summary_json'],8192,CODE)
    keys={'privacy_operation_request_id','recovery_point_backup_id','portable_backup_id','source_sha256','media_file_count'}
    pub._need(set(summary)<=keys and summary.get('privacy_operation_request_id')==row['request_id'],CODE)
    for key,value in summary.items():
        pub._need((type(value)is int and 0<=value<=2**53-1)if key=='media_file_count'else(type(value)is str and bool((pub._HEX if key=='source_sha256'else pub._ID).fullmatch(value))),CODE)
    if 'source_sha256'in summary:pub._need(summary['source_sha256']==row['source_sha256'],CODE)
    pub._need(type(audit['occurred_at'])is str,CODE)
    return dict(audit)

def is_metadata(metadata):return type(metadata)is dict and metadata.get('authority_kind')==PARENT

def binding(metadata):
    pub._need(is_metadata(metadata),CODE)
    return {'authority_kind':PARENT,'parent_authority_id':metadata['authority_id'],
        'parent_metadata_sha256':pub._sha(pub._canonical(metadata))}

def _cap(database):
    cap=_CURRENT.get(database);state=_CAPS.get(cap)if cap is not None else None
    pub._need(state is not None and state.database is database and state.actor==pub._actor(),CODE)
    pub._owner(state.owner);_auth(database,state.auth);state.auth.require(state.token)
    return state

def current(database):
    if database not in _CURRENT:return None
    return _cap(database)

def original_unapplied_interruption(database,owner,request_id):
    """Read only the current authenticated Parent capability and original work.

    This is separate from the native qualifier. It neither creates authority
    nor converts a native operation, and is usable only before this cap exits.
    """
    if type(request_id)is not str:return False
    state=current(database)
    if state is None or state.owner is not owner or state.recovery:return False
    if owner.path!=pub._path(database.path)or state.body['operation']['request_id']!=request_id:return False
    if state.command_sha256!=pub._sha(pub._canonical(['POST','/v1/parent/backups/portable/restore',state.body])):return False
    result=pub.reconcile_publication(owner)
    if result.get('state')!='UNAPPLIED'or result.get('request_id')!=request_id:return False
    from . import parent_restore_ownership as ownership
    value=ownership.current(owner)
    if value is None or value['state']!='ACTIVE'or value['request_id']!=request_id:return False
    binding=value['binding']
    if not pub._is_parent_binding(binding):return False
    row,generation,authority,metadata=ownership._facts(owner,value)
    return (row['request_id']==request_id and row['kind']==metadata['kind']=='portable_restore'
        and row['state']=='processing'and row['finalized']==0
        and row['reference_type']is None and row['reference_id']is None and row['result_generation']is None
        and row['origin_generation']==generation==binding['origin_generation']
        and row['source_sha256']==binding['source_sha256']
        and row['request_sha256']==binding['request_sha256']
        and row['cleanup_manifest_sha256']==binding['cleanup_manifest_sha256']
        and authority['state']=='ACTIVE'and is_metadata(metadata)
        and metadata['request_id']==request_id and metadata['command_sha256']==state.command_sha256
        and pub._metadata_matches(binding,metadata))

PARENT_RESTORE_ADMISSION_SECONDS=2.0
PARENT_RESTORE_ADMISSION_MAX_ATTEMPTS=201

@asynccontextmanager
async def admitted_parent_restore_owner(database,auth,token,*,native_header_present=False):
    """Yield only between failed original writer entries; never replay a body.

    The Parent factory/capability and all business work remain outside this
    retry. Every attempt rechecks original authentication and native policy.
    """
    loop=asyncio.get_running_loop();until=loop.time()+PARENT_RESTORE_ADMISSION_SECONDS
    for attempt in range(PARENT_RESTORE_ADMISSION_MAX_ATTEMPTS):
        _auth(database,auth);auth.require(token)
        pub._need(native_header_present is False,CODE)
        stack=ExitStack()
        try:
            owner=stack.enter_context(pub.publication_owner(database,timeout=0))
        except pub.RestorePublicationError as error:
            stack.close()
            remaining=until-loop.time()
            if(error.args!=('RESTORE_PUBLICATION_BUSY',)or remaining<=0
                or attempt+1==PARENT_RESTORE_ADMISSION_MAX_ATTEMPTS):raise
            await asyncio.sleep(min(0.01,remaining))
        else:
            # __exit__, cancellation and body exceptions cannot reach the
            # factory-entry retry above, even when their code is exact BUSY.
            with stack:yield owner
            return

@contextmanager
def original_parent_restore(database,auth,token,owner,*,method,route,body,recovery=False):
    pub._owner(owner);pub._need(owner.path==pub._path(database.path)and database not in _CURRENT,CODE)
    _auth(database,auth);auth.require(token)
    pub._need(method=='POST'and type(body)is dict,CODE)
    if recovery:
        pub._need(re.fullmatch(r'/v1/parent/privacy-operations/[^/]+/finalize',route)is not None and body=={},CODE)
    else:
        pub._need(route=='/v1/parent/backups/portable/restore'and set(body)=={'path','passphrase','confirmation','operation'}
            and body['confirmation']=='确认迁移恢复'and type(body['operation'])is dict,CODE)
        with database.connect()as c:
            qualified_policy(c)
    cap=object.__new__(_ParentRestore)
    _CAPS[cap]=SimpleNamespace(database=database,auth=auth,token=token,owner=owner,actor=pub._actor(),
        command_sha256=pub._sha(pub._canonical([method,route,body])),body=body,recovery=recovery)
    _CURRENT[database]=cap
    try:yield cap
    finally:_CURRENT.pop(database,None);_CAPS.pop(cap,None)

def reserve_original(database,operation,path,source_sha):
    from .privacy_operation_receipt import PrivacyOperationReceipts
    state=_cap(database);pub._need(not state.recovery and state.body['path']==path
        and state.body['operation']==operation,CODE)
    with database.connect()as c:
        c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
        _cap(database);_no_native(c,operation['request_id'])
        receipt,fresh=PrivacyOperationReceipts(database)._reserve_in_connection(c,operation,'portable_restore',path,source_sha256=source_sha)
        if fresh:
            validate_schema(c);row=c.execute('SELECT *FROM privacy_operation_intent WHERE request_id=?',(operation['request_id'],)).fetchone()
            c.execute('INSERT INTO restore_parent_authority VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?)',
                (uuid4().hex,row['request_id'],'portable_restore',row['origin_generation'],row['request_sha256'],state.command_sha256,row['cleanup_manifest_sha256'],source_sha,'PARENT_NO_NATIVE_NO_MANAGED','ACTIVE'))
        _,_,a,_=facts(c,operation['request_id'])
        pub._need(a['command_sha256']==state.command_sha256 and a['source_sha256']==source_sha,CODE)
        _cap(database)
        if a['state']=='ACTIVE':state.owner.gate.work_anchor_active=True
        c.commit()
    state.owner.parent.flush()
    return receipt,fresh

def finish(c,metadata,outcome):
    _cap_for_connection(c,metadata)
    row,_,a,original=facts(c,metadata['request_id'],metadata['authority_id'])
    pub._need(original==metadata and outcome in{'FINISHED','FAILED'},CODE)
    if a['state']==outcome:return
    pub._need(a['state']=='ACTIVE',CODE)
    if outcome=='FINISHED':pub._need(row['state']=='finished'and row['finalized']==1 and row['result_generation']is not None,CODE)
    else:pub._need(row['state']=='failed'and row['finalized']==0 and row['result_generation']is None,CODE)
    controlled=None;native=None
    if outcome=='FINISHED':
        owner=_cap_for_connection(c,metadata)
        raw=pub._read(owner.parent,pub._names(owner.path)[1]);j=pub._decode(raw)
        pub._need(j['schema']==3 and pub._metadata_matches(j['binding'],metadata)and j['binding']['result_generation']==row['result_generation'],CODE)
        controlled=managed_projection(c);native=native_projection(c)
        pub._need(controlled==j['binding']['controlled_projection_sha256']and native==j['binding']['native_policy_sha256'],CODE)
    c.execute('UPDATE restore_parent_authority SET state=?,result_generation=?,result_controlled_sha256=?,result_native_sha256=?WHERE authority_id=?',
        (outcome,row['result_generation']if outcome=='FINISHED'else None,controlled,native,a['authority_id']))


def _cap_for_connection(c,metadata):
    # Original registered SQL and currently re-authenticated same-actor cap only.
    matches=[]
    for cap,state in list(_CAPS.items()):
        if state.actor==pub._actor()and state.database._restore_sql_owner()is state.owner and(
                state.database._original_connections.get(id(c))is c or any(sql is c for sql in state.owner.gate.sql_connections)):
            _cap(state.database);matches.append(state.owner)
    pub._need(len(matches)==1,CODE);return matches[0]

def finalize_original(database,auth,token,owner,request_id,*,route,clear=None):
    from . import parent_restore_ownership as ownership,parent_restore_workflow as workflow
    with original_parent_restore(database,auth,token,owner,method='POST',route=route,body={},recovery=True):
        with pub._cold(owner.path,owner=owner)as c:
            _,_,a,metadata=facts(c,request_id)
        value=ownership.current(owner)
        if value is None:
            pub._need(a['state']in{'FINISHED','FAILED'},CODE)
            from .privacy_operation_receipt import PrivacyOperationReceipts
            return PrivacyOperationReceipts(database).read(request_id)
        pub._need(value['request_id']==request_id and pub._metadata_matches(value['binding'],metadata),CODE)
        result=pub.original_publication_metadata(owner,metadata)
        if result['state']in{'UNAPPLIED','ABORTED_NOT_APPLIED'}:
            with pub._cold(owner.path,owner=owner)as c:qualified_policy(c)
            pub.abort_unapplied_publication(owner,metadata)
        else:workflow.finish_original_tails(database,owner,metadata,clear)
        from .privacy_operation_receipt import PrivacyOperationReceipts
        return PrivacyOperationReceipts(database).read(request_id)
