"""Original Parent-only erase authority; never a substitute native permit.

The original App's ParentAuth object and same publication owner issue an opaque
in-process capability. Only current live records persist across restoration.
Tokens/PINs never enter this journal; historical rows alone cannot erase files.
"""
from contextlib import contextmanager,asynccontextmanager,ExitStack
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import asyncio,json,os,re,sqlite3,weakref
from . import restore_publication as pub
from .database import ExternalEffectBusyError

AUTHORITY='restore_asset_parent_authority'
MEMBER='restore_asset_parent_erasure'
CODE='RESTORE_ASSET_PARENT_AUTHORITY_UNCONFIRMED'
PARENT_DELETE_ADMISSION_SECONDS=2.0
PARENT_DELETE_ADMISSION_MAX_ATTEMPTS=201
AUTHORITY_SQL="""CREATE TABLE restore_asset_parent_authority(
 authority_id TEXT PRIMARY KEY CHECK(typeof(authority_id)='text'AND length(authority_id)=32 AND authority_id NOT GLOB '*[^0-9a-f]*'),
 authority_kind TEXT NOT NULL CHECK(authority_kind='PARENT_SESSION_DELETE'),
 deletion_id TEXT NOT NULL UNIQUE CHECK(typeof(deletion_id)='text'AND length(CAST(deletion_id AS BLOB))BETWEEN 1 AND 128),
 client_action_id TEXT NOT NULL UNIQUE CHECK(typeof(client_action_id)='text'AND length(CAST(client_action_id AS BLOB))BETWEEN 1 AND 200),
 child_id TEXT NOT NULL CHECK(typeof(child_id)='text'AND length(CAST(child_id AS BLOB))BETWEEN 1 AND 128),
 target_type TEXT NOT NULL CHECK(target_type IN('child_scope','capture_artifact','capture_artifact_raw_media','knowledge_source')),
 target_id TEXT NOT NULL CHECK(typeof(target_id)='text'AND length(CAST(target_id AS BLOB))BETWEEN 1 AND 200),
 scope TEXT NOT NULL CHECK(scope IN('learning','knowledge','tutor','reports','media','all_history','capture_artifact','raw_media_only')),
 origin_generation TEXT NOT NULL CHECK(typeof(origin_generation)='text'AND length(CAST(origin_generation AS BLOB))BETWEEN 1 AND 128),
 command_sha256 TEXT NOT NULL CHECK(typeof(command_sha256)='text'AND length(command_sha256)=64 AND command_sha256 NOT GLOB '*[^0-9a-f]*'),
 binding_sha256 TEXT NOT NULL CHECK(typeof(binding_sha256)='text'AND length(binding_sha256)=64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),
 selection_sha256 TEXT NOT NULL CHECK(typeof(selection_sha256)='text'AND length(selection_sha256)=64 AND selection_sha256 NOT GLOB '*[^0-9a-f]*'),
 member_count INTEGER NOT NULL CHECK(typeof(member_count)='integer'AND member_count BETWEEN 1 AND 100000),
 state TEXT NOT NULL CHECK(state IN('INTENT','FINISHED'))
)"""
MEMBER_SQL="""CREATE TABLE restore_asset_parent_erasure(
 generation_id TEXT NOT NULL,
 member_id TEXT NOT NULL,
 authority_id TEXT NOT NULL REFERENCES restore_asset_parent_authority(authority_id),
 member_sha256 TEXT NOT NULL CHECK(typeof(member_sha256)='text'AND length(member_sha256)=64 AND member_sha256 NOT GLOB '*[^0-9a-f]*'),
 fingerprint_json TEXT NOT NULL CHECK(typeof(fingerprint_json)='text'AND length(CAST(fingerprint_json AS BLOB))BETWEEN 1 AND 4096),
 state TEXT NOT NULL CHECK(state IN('INTENT','ERASED')),
 PRIMARY KEY(generation_id,member_id),
 FOREIGN KEY(generation_id,member_id)REFERENCES restore_asset_member(generation_id,member_id)
)"""
AUTHORITY_TRIGGER="""CREATE TRIGGER restore_asset_parent_authority_immutable BEFORE UPDATE ON restore_asset_parent_authority
WHEN NEW.authority_id IS NOT OLD.authority_id OR NEW.authority_kind IS NOT OLD.authority_kind
 OR NEW.deletion_id IS NOT OLD.deletion_id OR NEW.client_action_id IS NOT OLD.client_action_id OR NEW.child_id IS NOT OLD.child_id
 OR NEW.target_type IS NOT OLD.target_type OR NEW.target_id IS NOT OLD.target_id OR NEW.scope IS NOT OLD.scope
 OR NEW.origin_generation IS NOT OLD.origin_generation OR NEW.command_sha256 IS NOT OLD.command_sha256
 OR NEW.binding_sha256 IS NOT OLD.binding_sha256 OR NEW.selection_sha256 IS NOT OLD.selection_sha256 OR NEW.member_count IS NOT OLD.member_count
 OR NOT(OLD.state='INTENT'AND NEW.state='FINISHED')
BEGIN SELECT RAISE(ABORT,'parent erase authority is immutable');END"""
MEMBER_TRIGGER="""CREATE TRIGGER restore_asset_parent_erasure_immutable BEFORE UPDATE ON restore_asset_parent_erasure
WHEN NEW.generation_id IS NOT OLD.generation_id OR NEW.member_id IS NOT OLD.member_id OR NEW.authority_id IS NOT OLD.authority_id
 OR NEW.member_sha256 IS NOT OLD.member_sha256 OR NEW.fingerprint_json IS NOT OLD.fingerprint_json
 OR NOT(OLD.state='INTENT'AND NEW.state='ERASED')
BEGIN SELECT RAISE(ABORT,'parent erasure preimage is immutable');END"""
OBJECTS=(('table',AUTHORITY,AUTHORITY_SQL),('table',MEMBER,MEMBER_SQL),
 ('trigger','restore_asset_parent_authority_immutable',AUTHORITY_TRIGGER),
 ('trigger','restore_asset_parent_erasure_immutable',MEMBER_TRIGGER),
 ('trigger','restore_asset_parent_authority_no_delete',"CREATE TRIGGER restore_asset_parent_authority_no_delete BEFORE DELETE ON restore_asset_parent_authority BEGIN SELECT RAISE(ABORT,'parent erase history is required');END"),
 ('trigger','restore_asset_parent_erasure_no_delete',"CREATE TRIGGER restore_asset_parent_erasure_no_delete BEFORE DELETE ON restore_asset_parent_erasure BEGIN SELECT RAISE(ABORT,'parent erasure history is required');END"))

_AUTH=weakref.WeakKeyDictionary();_CAPS=weakref.WeakKeyDictionary();_CURRENT=weakref.WeakKeyDictionary()
def _registered(database):
    binding=_AUTH.get(database);return binding[0]()if binding is not None else None
class _ParentErase:
    __slots__=('__weakref__',)
    def __new__(cls):raise pub.RestorePublicationError(CODE)

def bind_parent_auth(database,parent_auth,*,native_key_present):
    from .parent_auth import ParentAuth
    pub._need(type(parent_auth)is ParentAuth and parent_auth.database is database,CODE)
    pub._need(type(native_key_present)is bool,CODE);binding=_AUTH.get(database)
    pub._need(binding is None or(binding[0]()is parent_auth and binding[1]is native_key_present and binding[2]is True),CODE)
    _AUTH[database]=(weakref.ref(parent_auth),native_key_present,True)

def retire_parent_auth(database,parent_auth):
    binding=_AUTH.get(database);pub._need(binding is not None and binding[0]()is parent_auth,CODE)
    _AUTH[database]=(binding[0],binding[1],False)

def _cap(value):
    pub._need(type(value)is _ParentErase and value in _CAPS,CODE);state=_CAPS[value]
    pub._owner(state.owner);pub._need(_CURRENT.get(state.owner)is value and state.actor==pub._actor()
        and state.database.path==state.owner.path and _registered(state.database)is state.parent_auth
        and _AUTH[state.database][2]is True,CODE)
    return state

def current(database):
    owner=pub._gate(pub._path(database.path)).owner;value=_CURRENT.get(owner)if owner is not None else None
    if value is None:return None
    state=_cap(value);pub._need(state.database is database,CODE);return value

def _shape(method,path,body,c):
    pub._need(type(body)is dict,CODE)
    if body.get('confirmation')!='确认删除':
        from .privacy_lifecycle import DeletionConfirmationError
        raise DeletionConfirmationError()
    patterns=(('DELETE',r'/v1/parent/capture-artifacts/([^/]+)', 'capture_artifact','capture_artifact'),
        ('DELETE',r'/v1/parent/capture-artifacts/([^/]+)/raw-media','capture_artifact_raw_media','raw_media_only'),
        ('POST',r'/v1/parent/children/([^/]+)/delete','child_scope',None),
        ('POST',r'/v1/parent/children/([^/]+)/knowledge-sources/([^/]+)/delete','knowledge_source','knowledge'),
        ('POST',r'/v1/parent/deletions/([^/]+)/retry','retry',None))
    found=[]
    for verb,pattern,kind,scope in patterns:
        match=re.fullmatch(pattern,path)
        if method==verb and match:found.append((kind,scope,match.groups()))
    pub._need(len(found)==1,CODE);kind,scope,ids=found[0];retry=kind=='retry'
    if retry:
        job=c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(ids[0],)).fetchone();pub._need(job is not None,CODE)
        return dict(job),True
    action=body.get('client_action_id');pub._need(type(action)is str and 1<=len(action)<=200,CODE)
    if kind.startswith('capture_artifact'):
        target=ids[0];row=c.execute('SELECT child_id FROM capture_artifact WHERE artifact_id=?',(target,)).fetchone()
        old=c.execute('SELECT *FROM deletion_job WHERE client_action_id=?',(action,)).fetchone()
        if row is None and old is None:
            from .privacy_lifecycle import UnknownCaptureArtifactError
            raise UnknownCaptureArtifactError(target)
        child=row[0]if row is not None else old['child_id']
    elif kind=='child_scope':child=target=ids[0];scope=body.get('scope')
    else:child,target=ids
    pub._need(scope in{'learning','knowledge','tutor','reports','media','all_history','capture_artifact','raw_media_only'}
        and c.execute('SELECT 1 FROM child_profile WHERE child_id=?',(child,)).fetchone()is not None,CODE)
    existing=c.execute('SELECT *FROM deletion_job WHERE client_action_id=?',(action,)).fetchone()
    shape={'child_id':child,'target_type':kind,'target_id':target,'scope':scope,'client_action_id':action}
    if existing is not None:
        if not all(existing[k]==v for k,v in shape.items()):
            from .privacy_lifecycle import InvalidDeletionActionError
            raise InvalidDeletionActionError()
        shape['deletion_id']=existing['deletion_id']
    return shape,False

def _binding(shape,origin,command):
    return pub._sha(pub._canonical({'kind':'PARENT_SESSION_DELETE','job':{k:shape[k]for k in('deletion_id','client_action_id','child_id','target_type','target_id','scope')},
        'origin_generation':origin,'command_sha256':command}))

@asynccontextmanager
async def admitted_parent_delete(*args,**kwargs):
    """Drain transient readers before entering the original opaque factory.

    Only an unsuccessful factory entry may be retried. The original owner and
    capability remain in this same Task throughout the single business body.
    Cancellation, poison and every error other than exact BUSY escape unchanged.
    """
    loop=asyncio.get_running_loop();until=loop.time()+PARENT_DELETE_ADMISSION_SECONDS
    with ExitStack()as stack:
        for attempt in range(PARENT_DELETE_ADMISSION_MAX_ATTEMPTS):
            try:
                value=stack.enter_context(original_parent_delete(*args,**kwargs))
            except ExternalEffectBusyError as error:
                # The factory has not entered or run the deletion body.  Do not
                # infer anything about an earlier request with the same ID.
                from .privacy_lifecycle import DeletionAdmissionBusyError
                raise DeletionAdmissionBusyError() from error
            except pub.RestorePublicationError as error:
                remaining=until-loop.time()
                if(error.args!=('RESTORE_PUBLICATION_BUSY',)or remaining<=0
                    or attempt+1==PARENT_DELETE_ADMISSION_MAX_ATTEMPTS):raise
                await asyncio.sleep(min(0.01,remaining))
            else:break
        yield value

@contextmanager
def original_parent_delete(database,parent_auth,token,*,method,path,body,native_header_present=False):
    pub._need(_registered(database)is parent_auth and _AUTH[database][2]is True,CODE);parent_auth.require(token)
    with pub.publication_owner(database,timeout=0)as owner:
        parent_auth.require(token)
        with database.connect()as c:
            shape,retry=_shape(method,path,body,c);origin=c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()[0]
            dangerous=shape['target_type']=='child_scope'and shape['scope']in{'media','all_history'}
            managed=bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='controlled_launch_epoch'").fetchone()
                and c.execute('SELECT 1 FROM controlled_launch_epoch WHERE child_id=?LIMIT 1',(shape['child_id'],)).fetchone())
            native=dangerous and(bool(native_header_present)or _AUTH[database][1]or managed)
            # A changed configuration never downgrades an original native claim
            # or its persisted preimage into a new Parent-only permission.
            original_native=pub._bounded_rows(c,"SELECT 1 FROM native_privacy_permit WHERE typeof(metadata_json)='text'AND length(CAST(metadata_json AS BLOB))BETWEEN 1 AND 8192 AND json_valid(metadata_json)AND json_extract(metadata_json,'$.request_id')=?LIMIT 1",(shape['client_action_id'],),1,CODE)if c.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()else[]
            if original_native:pub._need(dangerous,CODE);native=True
            command=pub._sha(pub._canonical({'method':method,'route':path,'validated_body':body,'resource':{k:shape[k]for k in('client_action_id','child_id','target_type','target_id','scope')}}))
            saved=c.execute('SELECT *FROM restore_asset_parent_authority WHERE client_action_id=?',(shape['client_action_id'],)).fetchone()
            if saved is not None:
                pub._need(not native and all(saved[k]==shape[k]for k in('client_action_id','child_id','target_type','target_id','scope'))
                    and(saved['state']=='FINISHED'or saved['origin_generation']==origin)and(retry or saved['command_sha256']==command),CODE)
                shape['deletion_id']=saved['deletion_id']
        value=object.__new__(_ParentErase);pub._need(owner not in _CURRENT,CODE)
        state=SimpleNamespace(owner=owner,actor=pub._actor(),database=database,parent_auth=parent_auth,shape=shape,origin=origin,
            command=command,retry=retry,native=native,saved=dict(saved)if saved is not None else None,plan={})
        _CAPS[value]=state;_CURRENT[owner]=value
        try:
            if not native:_capture_selection(value)
            yield value
            if not native:_finish(value)
        finally:_CURRENT.pop(owner,None);_CAPS.pop(value,None)

def _job(state,job):
    pub._need(job is not None and all(job[k]==state.shape[k]for k in('client_action_id','child_id','target_type','target_id','scope')),CODE)
    if 'deletion_id'in state.shape:pub._need(job['deletion_id']==state.shape['deletion_id'],CODE)
    else:state.shape['deletion_id']=job['deletion_id']
    return dict(job)

def _capture_selection(value):
    from . import parent_restore_assets as a
    state=_cap(value)
    with state.database.connect()as c:
        a.validate_schema(c)
        generations=c.execute("SELECT *FROM restore_asset_generation WHERE state='ADOPTED'LIMIT ?",(a.MAX_FILES+1,)).fetchall();pub._need(len(generations)<=a.MAX_FILES,CODE)
        for row in generations:
            g=dict(row);fingerprints={}
            pub._need(a._manifest_original(state.owner,c,g,preimages=fingerprints,pending_parent=value)==g['manifest_sha256'],CODE)
            root=state.owner.path.parent/'media'/g['root_name']
            for member in a._bounded_owned_rows(c,g['generation_id']):
                if member['kind']=='file'and member['purpose']=='managed'and member['child_id']==state.shape['child_id']:
                    record=c.execute('SELECT *FROM restore_asset_parent_erasure WHERE generation_id=?AND member_id=?',(g['generation_id'],member['member_id'])).fetchone()
                    pending=record is not None and state.saved is not None and record['authority_id']==state.saved['authority_id']
                    if member['state']=='READY'and a._original_reference_present(c,root/member['relative_name'])or pending:
                        state.plan[(g['generation_id'],member['member_id'])]={'generation':g,'member':member,'fingerprint':fingerprints[member['relative_name']],
                            'path':root/member['relative_name'],'pending':pending}
        pub._need(len(state.plan)<=a.MAX_FILES,CODE)

def persist_missing_references(database,c,job):
    from . import parent_restore_assets as a
    value=current(database)
    if value is None:return False
    state=_cap(value)
    if state.native:return False
    job=c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(job['deletion_id'],)).fetchone()
    _job(state,job);a._owner_connection(database,c);pub._need(c.in_transaction,CODE)
    selected=[record for record in state.plan.values()if record['pending']or not a._original_reference_present(c,record['path'])]
    if not selected:return False
    pub._fault('before_restore_asset_parent_preimage')
    authority=state.saved
    if authority is None:
        records=sorted([[r['member']['generation_id'],r['member']['member_id'],a._member_preimage_sha(r['member']),pub._canonical(r['fingerprint']).decode()]for r in selected])
        authority={'authority_id':uuid4().hex,'authority_kind':'PARENT_SESSION_DELETE','deletion_id':job['deletion_id'],
            **{k:job[k]for k in('client_action_id','child_id','target_type','target_id','scope')},'origin_generation':state.origin,
            'command_sha256':state.command,'binding_sha256':_binding(state.shape,state.origin,state.command),
            'selection_sha256':pub._sha(pub._canonical(records)),'member_count':len(selected),'state':'INTENT'}
        c.execute('INSERT INTO restore_asset_parent_authority VALUES('+','.join('?'for _ in range(14))+')',tuple(authority.values()))
        for r in selected:
            member=r['member'];pub._need(member['state']=='READY',CODE)
            c.execute("INSERT INTO restore_asset_parent_erasure VALUES(?,?,?,?,?,'INTENT')",(member['generation_id'],member['member_id'],authority['authority_id'],a._member_preimage_sha(member),pub._canonical(r['fingerprint']).decode()))
            c.execute("UPDATE restore_asset_member SET state='REMOVING'WHERE generation_id=?AND member_id=?AND state='READY'",(member['generation_id'],member['member_id']))
        state.saved=authority
    else:pub._need(authority['state']=='INTENT',CODE)
    paths=json.loads(job['residual_paths_json']or'[]');pub._need(type(paths)is list and all(type(p)is str for p in paths),CODE)
    paths=sorted(set(paths)|{str(r['path'])for r in selected})
    c.execute('UPDATE deletion_job SET residual_paths_json=?WHERE deletion_id=?',(json.dumps(paths),job['deletion_id']))
    return True

def _authority_records(c,authority):
    from . import parent_restore_assets as a
    records=[dict(r)for r in c.execute('SELECT *FROM restore_asset_parent_erasure WHERE authority_id=?ORDER BY generation_id,member_id',
        (authority['authority_id'],)).fetchmany(a.MAX_FILES+1)]
    pub._need(len(records)==authority['member_count']<=a.MAX_FILES,CODE)
    selection=[[r['generation_id'],r['member_id'],r['member_sha256'],r['fingerprint_json']]for r in records]
    pub._need(pub._sha(pub._canonical(selection))==authority['selection_sha256'],CODE)
    return records

def _audit_completed(c,authority):
    target=authority['target_type'];ident=authority['deletion_id']if target in{'child_scope','knowledge_source'}else authority['target_id']
    action={'child_scope':'child_data_deleted','knowledge_source':'knowledge_source_deleted',
        'capture_artifact':'capture_artifact_deleted','capture_artifact_raw_media':'capture_artifact_raw_media_deleted'}[target]
    key='deletion_id'if target in{'child_scope','knowledge_source'}else'artifact_id'
    query="SELECT *FROM audit_event WHERE actor='parent'AND action=?AND child_id=?AND typeof(summary_json)='text'AND length(CAST(summary_json AS BLOB))BETWEEN 1 AND 65536 AND json_valid(summary_json)AND json_extract(summary_json,?)=?"
    args=(action,authority['child_id'],'$.'+key,ident)
    rows=pub._bounded_rows(c,query+"AND json_extract(summary_json,'$.deletion_id')=?LIMIT 2",args+(authority['deletion_id'],),2,CODE)
    if not rows and target in{'capture_artifact','capture_artifact_raw_media'}:
        # Original R3 capture events did not include deletion_id. Only their
        # unique exact original resource event retains that prior fact contract.
        rows=pub._bounded_rows(c,query+"AND json_type(summary_json,'$.deletion_id')IS NULL LIMIT 2",args,2,CODE)
    pub._need(len(rows)==1,CODE);row=dict(rows[0]);summary=pub._json_object(row['summary_json'],65536,CODE)
    pub._need(('deletion_id'not in summary or summary['deletion_id']==authority['deletion_id'])
        and('client_action_id'not in summary or summary['client_action_id']==authority['client_action_id']),CODE)
    return row

def _authority_fact(c,authority,*,completed):
    shape={k:authority[k]for k in('deletion_id','client_action_id','child_id','target_type','target_id','scope')}
    pub._need(authority['authority_kind']=='PARENT_SESSION_DELETE'and authority['binding_sha256']==_binding(shape,authority['origin_generation'],authority['command_sha256']),CODE)
    job=c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(authority['deletion_id'],)).fetchone()
    pub._need(job is not None and all(job[k]==v for k,v in shape.items()),CODE)
    if completed:pub._need(authority['state']=='FINISHED'and job['status']=='completed',CODE);_audit_completed(c,authority)
    else:pub._need(authority['state']=='INTENT'and job['status']in{'scanning','deleting','verifying','failed','completed'},CODE)
    return dict(job)

def preimage(c,g,member,owner,*,pending=None,cache=None):
    """Read original proof only; never mint an erase capability from a row."""
    from . import parent_restore_assets as a
    record=c.execute('SELECT *FROM restore_asset_parent_erasure WHERE generation_id=?AND member_id=?',
        (g['generation_id'],member['member_id'])).fetchone()
    pub._need(record is not None and member['purpose']=='managed'and member['kind']=='file'
        and record['member_sha256']==a._member_preimage_sha(member),CODE)
    authority=c.execute('SELECT *FROM restore_asset_parent_authority WHERE authority_id=?',(record['authority_id'],)).fetchone();pub._need(authority is not None,CODE);authority=dict(authority)
    key=(authority['authority_id'],pending is not None)
    if cache is None:cache={}
    if key not in cache:
        allowed=False
        if pending is not None:
            state=_cap(pending)
            allowed=state.owner is owner and state.saved is not None and state.saved['authority_id']==authority['authority_id']and authority['state']=='INTENT'
            if allowed:_job(state,_authority_fact(c,authority,completed=False))
        if not allowed:_authority_fact(c,authority,completed=True)
        records=_authority_records(c,authority)
        if not allowed:pub._need(all(r['state']=='ERASED'for r in records),CODE)
        cache[key]=allowed
    allowed=cache[key]
    pub._need(record['state']=='ERASED'or(allowed and record['state']=='INTENT'),CODE)
    pub._need(member['child_id']==authority['child_id'],CODE)
    root=owner.path.parent/'media'/g['root_name'];a._unreferenced_original(c,root/member['relative_name'])
    fingerprint=json.loads(record['fingerprint_json'])
    from .registered_asset_snapshot import parse_registered_fingerprint
    pub._need(parse_registered_fingerprint(fingerprint)[0]==member['expected_bytes'],CODE)
    return fingerprint,allowed and record['state']=='INTENT'

def completed_preimage(c,g,member,root,*,cache=None):
    """Pure completed fact reader. Never grants pending erase or file rights."""
    from . import parent_restore_assets as a
    record=c.execute('SELECT *FROM restore_asset_parent_erasure WHERE generation_id=?AND member_id=?',
        (g['generation_id'],member['member_id'])).fetchone()
    pub._need(record is not None and member['purpose']=='managed'and member['kind']=='file'
        and record['member_sha256']==a._member_preimage_sha(member),CODE)
    authority=c.execute('SELECT *FROM restore_asset_parent_authority WHERE authority_id=?',(record['authority_id'],)).fetchone()
    pub._need(authority is not None,CODE);authority=dict(authority)
    if cache is None:cache={}
    if authority['authority_id']not in cache:
        _authority_fact(c,authority,completed=True)
        records=_authority_records(c,authority);pub._need(all(r['state']=='ERASED'for r in records),CODE)
        cache[authority['authority_id']]=True
    pub._need(record['state']=='ERASED'and member['child_id']==authority['child_id'],CODE)
    a._unreferenced_original(c,root/member['relative_name'])
    fingerprint=json.loads(record['fingerprint_json'])
    from .registered_asset_snapshot import parse_registered_fingerprint
    pub._need(parse_registered_fingerprint(fingerprint)[0]==member['expected_bytes'],CODE)
    return fingerprint


def validate_export_read(value,database,c):
    """Borrow an already active erase and original read lease; mint nothing."""
    from . import original_connection_read as reads
    state=_cap(value)
    pub._need(current(database)is value and state.database is database
        and type(c)is reads._ReadSQL,CODE)
    _,db,admission=reads._state(c.connection,database)
    lease=pub._original_read_lease(admission,database.path)
    pub._need(db is database and c.resources.admission is admission
        and lease['parent']is state.owner.parent and state.owner.gate.owner is state.owner,CODE)
    generation=c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()
    pub._need(generation is not None and generation[0]==state.origin,CODE)
    return state


def export_erased_preimage(value,database,c,g,member,root):
    """Only the real post-delete export may borrow its current erased facts.

    No pending permission reaches ordinary eligibility or a later backup. The
    read snapshot and original opaque cap stay owned by their existing scopes.
    """
    from . import parent_restore_assets as a
    from . import standard_relocation_provenance as provenance
    provenance.require_original_erase_export(c,database)
    state=validate_export_read(value,database,c)
    pub._need(root==database.path.parent/'media'/g['root_name'],CODE)
    if state.native:
        record=c.execute('SELECT *FROM restore_asset_erasure WHERE generation_id=?AND member_id=?',
            (g['generation_id'],member['member_id'])).fetchone()
        if record is None or record['request_id']!=state.shape['client_action_id']:return None
        job=c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(record['deletion_id'],)).fetchone()
        pub._need(job is not None and all(job[k]==state.shape[k]for k in('client_action_id','child_id','target_type','target_id','scope'))
            and('deletion_id'not in state.shape or job['deletion_id']==state.shape['deletion_id'])
            and job['status']=='verifying'and job['residual_paths_json']=='[]',CODE)
        row,metadata=a._delete_original_facts(c,job,record['native_nonce'],completed=False)
        pub._need(record['state']=='ERASED'and member['state']=='REMOVED'and member['kind']=='file'and member['purpose']=='managed'
            and record['member_sha256']==a._member_preimage_sha(member)
            and record['request_id']==row['request_id']==state.shape['client_action_id']
            and record['child_id']==row['child_id']==member['child_id']and record['scope']==row['scope']
            and record['origin_generation']==row['origin_generation']==state.origin
            and record['request_sha256']==row['request_sha256']
            and record['cleanup_manifest_sha256']==row['cleanup_manifest_sha256']
            and record['native_metadata_sha256']==a._digest(pub._canonical(metadata)),CODE)
        fingerprint=json.loads(record['fingerprint_json'])
    else:
        record=c.execute('SELECT *FROM restore_asset_parent_erasure WHERE generation_id=?AND member_id=?',
            (g['generation_id'],member['member_id'])).fetchone()
        if record is None or state.saved is None or record['authority_id']!=state.saved['authority_id']:return None
        authority=c.execute('SELECT *FROM restore_asset_parent_authority WHERE authority_id=?',(record['authority_id'],)).fetchone()
        pub._need(authority is not None and dict(authority)==state.saved and authority['state']=='INTENT'
            and authority['origin_generation']==state.origin and authority['command_sha256']==state.command
            and record['state']=='ERASED'and member['state']=='REMOVED',CODE)
        job=_authority_fact(c,dict(authority),completed=False)
        pub._need(all(job[k]==state.shape[k]for k in('client_action_id','child_id','target_type','target_id','scope'))
            and job['deletion_id']==state.saved['deletion_id']and job['status']=='verifying'and job['residual_paths_json']=='[]',CODE)
        fingerprint,may_exist=preimage(c,g,member,state.owner,pending=value)
        pub._need(not may_exist,CODE)
    path=root/member['relative_name']
    pub._need(not os.path.lexists(a.io_path(path)),CODE)
    a._unreferenced_original(c,path)
    from .registered_asset_snapshot import parse_registered_fingerprint
    pub._need(parse_registered_fingerprint(fingerprint)[0]==member['expected_bytes'],CODE)
    state.owner.parent.check()
    return fingerprint


def _finish(value):
    from . import parent_restore_assets as a
    state=_cap(value)
    if state.saved is None:return
    with pub._sql(state.owner.path,owner=state.owner,timeout=0)as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
        authority=dict(c.execute('SELECT *FROM restore_asset_parent_authority WHERE authority_id=?',(state.saved['authority_id'],)).fetchone())
        if authority['state']=='FINISHED':_authority_fact(c,authority,completed=True);return
        job=_authority_fact(c,authority,completed=False);_job(state,job);pub._need(job['status']=='completed',CODE)
        records=_authority_records(c,authority);pub._need(all(r['state']=='ERASED'for r in records),CODE)
        for r in records:
            member=c.execute('SELECT *FROM restore_asset_member WHERE generation_id=?AND member_id=?',(r['generation_id'],r['member_id'])).fetchone()
            g=c.execute('SELECT *FROM restore_asset_generation WHERE generation_id=?',(r['generation_id'],)).fetchone()
            path=state.owner.path.parent/'media'/g['root_name']/member['relative_name']
            pub._need(member['state']=='REMOVED'and not os.path.lexists(path),CODE);a._unreferenced_original(c,path)
        _audit_completed(c,authority);pub._fault('before_restore_asset_parent_finish')
        c.execute("UPDATE restore_asset_parent_authority SET state='FINISHED'WHERE authority_id=?AND state='INTENT'",(authority['authority_id'],))
        try:c.commit();state.owner.parent.flush()
        except BaseException:state.owner.gate.poisoned=True;raise
        pub._fault('after_restore_asset_parent_finish')

def erase_member(database,job,path,g,member):
    """Return None only for the separate original native path, never fallback."""
    from . import parent_restore_assets as a
    value=current(database);pub._need(value is not None,CODE);state=_cap(value)
    if state.native:return None
    pub._need(path==state.owner.path.parent/'media'/g['root_name']/member['relative_name'],CODE);_job(state,job)
    with database.connect()as c:
        record=c.execute('SELECT *FROM restore_asset_parent_erasure WHERE generation_id=?AND member_id=?',(g['generation_id'],member['member_id'])).fetchone()
        pub._need(record is not None and state.saved is not None and record['authority_id']==state.saved['authority_id'],CODE)
        fingerprint,pending=preimage(c,g,member,state.owner,pending=value)
        if record['state']=='ERASED':pub._need(member['state']=='REMOVED'and not os.path.lexists(path),CODE);return False
        pub._need(member['state']=='REMOVING'and pending,CODE)
        directories={r['relative_name']:r for r in a._bounded_owned_rows(c,g['generation_id'])if r['kind']=='directory'}
    from .parent_restore_workflow import _SourceHandles
    from .registered_asset_snapshot import HeldFile,Policy,held_registered_fingerprint
    with _SourceHandles(state.owner)as stack:
        stack.enter_context(a._held_media(state.owner,database.path.parent/'media',json.loads(g['media_identity_json'])))
        root=database.path.parent/'media'/g['root_name'];parent=stack.enter_context(a._held_parent(state.owner,root,json.loads(g['root_identity_json'])))
        parts=member['relative_name'].split('/')
        for depth in range(1,len(parts)):
            rel='/'.join(parts[:depth]);directory=directories[rel];pub._need(directory['state']=='READY',CODE)
            parent=stack.enter_context(a._held_parent(state.owner,root/rel,json.loads(directory['identity_json'])))
        identity=json.loads(member['identity_json'])
        pub._fault('after_restore_asset_parent_preimage_committed')
        existed=os.path.lexists(path)
        if existed:
            with HeldFile(root,member['relative_name'],Policy(a.MAX_FILES,64,1024,64*1024*1024,2**53-1,2**53-1))as held:
                original=list(held.before['identity'])if os.name=='nt'else[held.before.st_dev,held.before.st_ino]
                pub._need(original==identity and held.hash()==(member['expected_sha256'],member['expected_bytes'])and held_registered_fingerprint(held)==fingerprint,CODE)
            pub._fault('before_restore_asset_parent_erase');a._erase(SimpleNamespace(owner=state.owner),parent,path,'file',identity)
            pub._fault('after_restore_asset_parent_erased')
        with pub._sql(state.owner.path,owner=state.owner,timeout=0)as c:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE');_job(state,_authority_fact(c,state.saved,completed=False))
            pub._need(not os.path.lexists(path),CODE);a._unreferenced_original(c,path)
            c.execute("UPDATE restore_asset_parent_erasure SET state='ERASED'WHERE generation_id=?AND member_id=?AND state='INTENT'",(g['generation_id'],member['member_id']))
            c.execute("UPDATE restore_asset_member SET state='REMOVED'WHERE generation_id=?AND member_id=?AND state='REMOVING'",(g['generation_id'],member['member_id']))
            try:c.commit();state.owner.parent.flush()
            except BaseException:state.owner.gate.poisoned=True;raise
        pub._fault('after_restore_asset_parent_erasure_recorded')
    return existed

def preserve_original_jobs(live_database,staged_database,live,staged):
    """Keep original completion facts; a recovery snapshot cannot roll them back.

    This is the last copy in the original fresh staging transaction. Its narrow
    authorizer remains installed through the original FK check/commit/close;
    no prior borrowed authorizer is replaced with an unconditional admission.
    """
    from . import parent_restore_assets as a
    from . import parent_restore_ownership as ownership
    owner=a._owner_connection(live_database,live);a._owner_connection(staged_database,staged)
    ticket=a._import_ticket(staged_database)
    pub._need(ticket is not None and ticket['owner']is owner and staged.in_transaction,CODE)
    a.validate_schema(live);a.validate_schema(staged)
    original=ownership._anchor(live)
    pub._need(original is not None and original['state']=='ACTIVE'and ownership._anchor(staged)==original,CODE)
    for table,limit,order in((a.GENERATION,a.MAX_FILES,'generation_id'),(a.MEMBER,a.MAX_MEMBERS,'generation_id,member_id'),
            (a.ERASURE,a.MAX_FILES,'generation_id,member_id'),(AUTHORITY,a.MAX_FILES,'authority_id'),(MEMBER,a.MAX_FILES,'generation_id,member_id')):
        rows=[tuple(r)for r in live.execute('SELECT *FROM '+table+' ORDER BY '+order+' LIMIT ?',(limit+1,))]
        pub._need(len(rows)<=limit and rows==[tuple(r)for r in staged.execute('SELECT *FROM '+table+' ORDER BY '+order+' LIMIT ?',(limit+1,))],CODE)
    jobs={};audits={}
    for row in live.execute('SELECT DISTINCT deletion_id,native_nonce FROM restore_asset_erasure LIMIT ?',(a.MAX_FILES+1,)).fetchall():
        job=live.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(row['deletion_id'],)).fetchone()
        a._delete_original_facts(live,job,row['native_nonce'],completed=True);jobs[row['deletion_id']]=dict(job)
    authorities=live.execute('SELECT *FROM restore_asset_parent_authority LIMIT ?',(a.MAX_FILES+1,)).fetchall()
    pub._need(len(authorities)<=a.MAX_FILES and len(jobs)<=a.MAX_FILES,CODE)
    for authority in authorities:
        job=_authority_fact(live,dict(authority),completed=True);jobs[job['deletion_id']]=job
        audit=_audit_completed(live,dict(authority));previous=audits.setdefault(audit['audit_id'],audit);pub._need(previous==audit,CODE)
    # Only exact original live FINISHED Parent restore results carry their
    # referenced audit. Backup rows never select authority or arbitrary history.
    from . import parent_restore_parent as parent_restore
    if parent_restore.AUTHORITY in pub._tables(live):
        parent_restore.validate_schema(live);parent_restore.validate_schema(staged)
        columns=('authority_id','request_id','kind','origin_generation','request_sha256','command_sha256',
            'cleanup_manifest_sha256','source_sha256','policy','result_generation','result_controlled_sha256','result_native_sha256','state')
        selected=','.join(columns)
        originals=pub._bounded_rows(live,'SELECT '+selected+' FROM restore_parent_authority ORDER BY authority_id LIMIT ?',
            (a.MAX_FILES+1,),a.MAX_FILES,CODE)
        copied=pub._bounded_rows(staged,'SELECT '+selected+' FROM restore_parent_authority ORDER BY authority_id LIMIT ?',
            (a.MAX_FILES+1,),a.MAX_FILES,CODE)
        pub._need([tuple(r)for r in originals]==[tuple(r)for r in copied],CODE)
        for original in originals:
            if original['state']!='FINISHED':continue
            receipt,_,_,_=parent_restore.facts(live,original['request_id'],original['authority_id'])
            audit=parent_restore.finished_audit(live,receipt)
            previous=audits.setdefault(audit['audit_id'],audit);pub._need(previous==audit,CODE)
    if not jobs and not audits:return
    rows_by_table={}
    if jobs:rows_by_table['deletion_job']=list(jobs.values())
    if audits:rows_by_table['audit_event']=list(audits.values())
    columns_by_table={};mutable_by_table={};changes=[];total=0
    for table,records in rows_by_table.items():
        primary={'deletion_job':'deletion_id','audit_event':'audit_id'}[table]
        columns=[tuple(r)for r in live.execute('PRAGMA table_info('+table+')')];names=[r[1]for r in columns]
        pub._need(names and [r[1]for r in columns if r[5]]==[primary]
            and all(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',name)for name in names),CODE)
        for c in(live,staged):
            kind=c.execute('SELECT type FROM sqlite_master WHERE name=?',(table,)).fetchmany(2)
            pub._need(len(kind)==1 and kind[0][0]=='table'
                and not c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'AND lower(tbl_name)=?LIMIT 1",(table,)).fetchone(),CODE)
        pub._need(columns==[tuple(r)for r in staged.execute('PRAGMA table_info('+table+')')]
            and[tuple(r)for r in live.execute('PRAGMA foreign_key_list('+table+')')]==[tuple(r)for r in staged.execute('PRAGMA foreign_key_list('+table+')')],CODE)
        columns_by_table[table]=names;mutable_by_table[table]=tuple(name for name in names if name!=primary)
        for record in records:
            raw=pub._canonical(record);total+=len(raw);pub._need(len(raw)<=65536 and total<=64*1024*1024,CODE)
            if table=='deletion_job':
                collision=staged.execute('SELECT deletion_id FROM deletion_job WHERE client_action_id=?',(record['client_action_id'],)).fetchmany(2)
                pub._need(all(r[0]==record['deletion_id']for r in collision),CODE)
            existing=staged.execute('SELECT *FROM '+table+' WHERE '+primary+'=?',(record[primary],)).fetchone()
            if table=='audit_event'and existing is not None:pub._need(dict(existing)==record,CODE)
            changes.append((table,primary,record,existing is not None))
    def authorize(action,arg1,arg2,database,trigger):
        if trigger is not None:return sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_INSERT:
            return sqlite3.SQLITE_OK if database=='main'and arg1 in rows_by_table else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_UPDATE:
            return sqlite3.SQLITE_OK if database=='main'and arg1 in rows_by_table and arg2 in mutable_by_table[arg1]else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if database=='main'and arg1 in rows_by_table else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if str(arg2).lower()in{'length','typeof','json_valid'}else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_PRAGMA:
            return sqlite3.SQLITE_OK if str(arg1).lower()=='foreign_key_check'and arg2 is None else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_TRANSACTION:
            return sqlite3.SQLITE_OK if arg1 in{'COMMIT','ROLLBACK'}else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action==sqlite3.SQLITE_SELECT else sqlite3.SQLITE_DENY
    staged.set_authorizer(authorize)
    try:
        for table,primary,record,exists in changes:
            names=columns_by_table[table];mutable=mutable_by_table[table]
            if exists:
                staged.execute('UPDATE '+table+' SET '+','.join(name+'=?'for name in mutable)+' WHERE '+primary+'=?',
                    tuple(record[name]for name in mutable)+(record[primary],))
            else:
                staged.execute('INSERT INTO '+table+'('+','.join(names)+')VALUES('+','.join('?'for _ in names)+')',tuple(record[name]for name in names))
    except sqlite3.Error:raise pub.RestorePublicationError(CODE)from None
