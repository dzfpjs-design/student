"""Schema92 typed CPU completion history. No scores, model-call aliases or backfill."""
from datetime import datetime,timezone
import hashlib,json,re,sqlite3
from contextlib import contextmanager,ExitStack
from contextvars import ContextVar
from uuid import uuid4
from threading import Lock

TABLE='pet_multipa_execution_receipt'
EVENT='pet_multipa_execution_receipt_event'
SCOPES={'learning','media','tutor','knowledge','all_history'}
FACTS=frozenset(('schema','invocation_nonce_sha256','job_id_sha256','source_binding_sha256','input_audio_sha256','input_audio_bytes','frames','task_family','reference_sha256','reference_utf8_bytes','raw_result_sha256','deployment_sha256','asset_set_sha256','installation_epoch_sha256','authority_set_sha256','deployment_manifest_sha256','native_exit','job_active','pipe_thread_drain_confirmed','owned_releases_confirmed','score_qualified','mastery_claim_allowed','certification_claim_allowed'))
HASHES=frozenset(('invocation_nonce_sha256','job_id_sha256','source_binding_sha256','input_audio_sha256','raw_result_sha256','deployment_sha256','asset_set_sha256','installation_epoch_sha256','authority_set_sha256','deployment_manifest_sha256'))
def need(ok,code):
    if not ok:raise ValueError('PET_MULTIPA_RECEIPT_'+code)
def canonical(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(v):return hashlib.sha256(canonical(v).encode()).hexdigest()
def hash_value(v):return type(v)is str and re.fullmatch('[a-f0-9]{64}',v)is not None
def identifier(v):need(type(v)is str and 0<len(v)<=200 and not any(ord(x)<32 for x in v),'IDENTITY_REQUIRED');return v
def stamp():return datetime.now(timezone.utc).isoformat()
DDL='CREATE TABLE pet_multipa_execution_receipt (\n receipt_id TEXT PRIMARY KEY,\n child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,\n session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,\n candidate_id TEXT NOT NULL UNIQUE,job_id TEXT NOT NULL UNIQUE,\n candidate_request_sha256 TEXT NOT NULL,candidate_numeric_sha256 TEXT NOT NULL,\n payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,created_at TEXT NOT NULL)'
EVENT_DDL="CREATE TABLE pet_multipa_execution_receipt_event (\n event_id TEXT PRIMARY KEY,receipt_id TEXT NOT NULL,\n child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,\n session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,\n operation TEXT NOT NULL CHECK(operation='restore_quarantine'),restore_id TEXT NOT NULL,\n receipt_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,\n UNIQUE(receipt_id,restore_id,operation))"
OBJECTS=[('table',TABLE,DDL),('table',EVENT,EVENT_DDL)]
for table in(TABLE,EVENT):
    OBJECTS.append(('trigger',table+'_immutable','CREATE TRIGGER '+table+'_immutable BEFORE UPDATE ON '+table+" BEGIN SELECT RAISE(ABORT,'PET_MULTIPA_RECEIPT_IMMUTABLE'); END"))
    OBJECTS.append(('trigger',table+'_privacy_delete','CREATE TRIGGER '+table+'_privacy_delete BEFORE DELETE ON '+table+" WHEN EXISTS(SELECT 1 FROM child_profile WHERE child_id=OLD.child_id) AND EXISTS(SELECT 1 FROM daily_session WHERE session_id=OLD.session_id) AND NOT EXISTS(SELECT 1 FROM deletion_job WHERE child_id=OLD.child_id AND target_type='child_scope' AND target_id=OLD.child_id AND scope IN ('learning','media','tutor','knowledge','all_history') AND status IN ('scanning','deleting','verifying')) BEGIN SELECT RAISE(ABORT,'PET_MULTIPA_RECEIPT_PRIVACY_DELETE_REQUIRED'); END"))
OBJECTS=tuple(OBJECTS)
def _present(c):return bool(c.execute('SELECT 1 FROM sqlite_master WHERE name IN('+','.join('?'for _ in OBJECTS)+')',tuple(v[1]for v in OBJECTS)).fetchone())
def validate_schema(c,*,source_version=None):
    source=(c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0)if source_version is None else source_version
    if source<92:
        need(not _present(c),'OLD_SOURCE_COLLISION');return
    from .parent_restore_assets import _validate_objects
    _validate_objects(c,OBJECTS,(TABLE,EVENT))
    need(not c.execute("SELECT 1 FROM sqlite_master WHERE type='index'AND tbl_name IN(?,?)AND sql IS NOT NULL",(TABLE,EVENT)).fetchone(),'UNKNOWN_INDEX')
def ensure_schema(c):
    source=c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
    if source>=92:validate_schema(c,source_version=source);return
    validate_schema(c,source_version=source)
    for _,_,sql in OBJECTS:c.execute(sql)
    validate_schema(c,source_version=92)
def _facts(v):
    need(type(v)is dict and set(v)==FACTS and v['schema']=='owned_worker_completion_v1'and all(hash_value(v[k])for k in HASHES),'FACTS_INVALID')
    need(v['task_family']in{'read_aloud','spoken_response'}and type(v['frames'])is int and v['frames']>0 and type(v['input_audio_bytes'])is int and v['input_audio_bytes']>=v['frames']*2,'FACTS_INVALID')
    need(type(v['reference_utf8_bytes'])is int and ((v['task_family']=='read_aloud'and hash_value(v['reference_sha256'])and 0<v['reference_utf8_bytes']<=24000)or(v['task_family']=='spoken_response'and v['reference_sha256']is None and v['reference_utf8_bytes']==0)),'FACTS_INVALID')
    need(type(v['native_exit'])is int and v['native_exit']==0 and type(v['job_active'])is int and v['job_active']==0 and v['pipe_thread_drain_confirmed']is True and v['owned_releases_confirmed']is True and all(v[k]is False for k in('score_qualified','mastery_claim_allowed','certification_claim_allowed')),'FACTS_INVALID')
    need(len(canonical(v).encode())<=4096,'FACTS_BOUND');return v
def _row(c,child_id,candidate_id,job_id):
    row=c.execute('SELECT * FROM '+TABLE+' WHERE candidate_id=?AND job_id=?AND child_id=?',(candidate_id,job_id,child_id)).fetchone()
    return row
def record_attested(c,*,candidate_id,child_id,session_id,job_claim,binding,result,numeric,request_hash,attested,invocation):
    need(c.in_transaction,'TRANSACTION_REQUIRED');validate_schema(c)
    from .pet_speaking_candidate_jobs import validate_claim
    from .pet_multipa_process import _consume_worker_completion
    row=validate_claim(c,job_claim,child_id,session_id,binding,json.loads(c.execute('SELECT assets_json FROM pet_speaking_model_candidate WHERE candidate_id=?',(candidate_id,)).fetchone()[0]))
    facts=_facts(_consume_worker_completion(attested,invocation=invocation,job_claim=job_claim,binding=binding))
    need(attested.numeric is result,'RESULT_IDENTITY_REQUIRED')
    need(facts['job_id_sha256']==hashlib.sha256(row['job_id'].encode()).hexdigest()and facts['source_binding_sha256']==digest(binding)and facts['input_audio_sha256']==binding['audio']['sha256']and facts['frames']==binding['audio']['frames']and facts['raw_result_sha256']==digest(result)and facts['asset_set_sha256']==digest(json.loads(row['assets_json']))and facts['deployment_sha256']==row['deployment_sha256'],'BINDING_CHANGED')
    reference=binding.get('reference',{});need(facts['reference_sha256']==reference.get('text_sha256')and facts['reference_utf8_bytes']==reference.get('text_utf8_bytes',0),'REFERENCE_CHANGED')
    candidate=c.execute('SELECT * FROM pet_speaking_model_candidate WHERE candidate_id=?AND child_id=?AND session_id=?',(candidate_id,child_id,session_id)).fetchone()
    event=c.execute("SELECT * FROM pet_speaking_model_candidate_event WHERE candidate_id=?AND operation='recorded'AND revision=1",(candidate_id,)).fetchone()
    need(candidate and event and candidate['binding_json']==canonical(binding)and candidate['request_hash']==event['request_hash']==request_hash and candidate['raw_numeric_json']==canonical(numeric)and event['client_action_id']==row['client_action_id'],'CANDIDATE_CHANGED')
    payload=canonical(facts);receipt_id=uuid4().hex
    c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?)',(receipt_id,child_id,session_id,candidate_id,row['job_id'],request_hash,digest(numeric),payload,hashlib.sha256(payload.encode()).hexdigest(),stamp()))
    return receipt_id

def quarantine_after_restore(c,restore_id):
    need(c.in_transaction,'TRANSACTION_REQUIRED');identifier(restore_id);validate_schema(c)
    for row in c.execute('SELECT * FROM '+TABLE).fetchall():
        if c.execute('SELECT 1 FROM '+EVENT+" WHERE receipt_id=?AND restore_id=?AND operation='restore_quarantine'",(row['receipt_id'],restore_id)).fetchone():continue
        c.execute('INSERT INTO '+EVENT+' VALUES(?,?,?,?,?,?,?,?)',(uuid4().hex,row['receipt_id'],row['child_id'],row['session_id'],'restore_quarantine',restore_id,digest(dict(row)),stamp()))

def delete_child_scope(c,child_id,scope):
    need(c.in_transaction and scope in SCOPES,'PRIVACY_DELETE_REQUIRED')
    return {t:c.execute('DELETE FROM '+t+' WHERE child_id=?',(child_id,)).rowcount for t in(EVENT,TABLE)}

_READ_SCOPE=ContextVar('cpu_receipt_install_read_scope',default=None)
_READ_HOLD_LOCK=Lock()
_READ_HOLDS=[] # Process lifetime strong owners; no retry/reclose/reset API.
def _read_release_blocked():
    with _READ_HOLD_LOCK:return bool(_READ_HOLDS)
def _read_release_confirmed(owner):
    try:
        if getattr(owner,'_completion_unknown',False):return False
        # Registry/policy rejection before any native kernel/handle is not an
        # unknown native release and must not occupy a permanent owner slot.
        if getattr(owner,'_completion_kernel',None)is None and hasattr(owner,'_completion_handles')and not owner._completion_handles:return True
        return owner._completion_release_confirmed()is True
    except BaseException:return False
@contextmanager
def install_read_scope(c):
    prior=_READ_SCOPE.get()
    if prior is not None:
        need(prior['connection']is c,'READ_SCOPE_CHANGED');yield;return
    scope={'connection':c,'stack':ExitStack(),'held':{},'owners':[]}
    token=_READ_SCOPE.set(scope)
    try:yield
    finally:
        try:
            failure=None
            try:scope['stack'].close()
            except BaseException as error:failure=error
            unknown=any(not _read_release_confirmed(owner)for owner in scope['owners'])
            if unknown:
                # Original callbacks may already be popped. Keep original lease
                # objects + SAME-close ledgers, never SQLite/audio/source rows.
                with _READ_HOLD_LOCK:_READ_HOLDS.append({'owners':tuple(scope['owners']),'stack':scope['stack'].pop_all()})
                need(False,'READ_RELEASE_UNCONFIRMED')
            if failure is not None:raise failure
            if scope['owners']:need(not _read_release_blocked(),'READ_RELEASE_UNCONFIRMED')
        finally:_READ_SCOPE.reset(token)

def _held_current(key,facts,assets):
    from .pet_multipa_epoch import _AttestedWindowsEpochLease,AUTHORITIES
    # Reopen the actual approved install under original reader/ACL/file handles.
    # An approved dict, flags or job.owner_epoch cannot substitute this lease.
    from .pet_multipa_epoch import configured
    scope=_READ_SCOPE.get();need(scope is not None,'READ_SCOPE_REQUIRED');need(not _read_release_blocked(),'READ_RELEASE_UNCONFIRMED')
    if key not in scope['held']:
        owner=_AttestedWindowsEpochLease(key);scope['owners'].append(owner)
        scope['held'][key]=scope['stack'].enter_context(owner)
    held=scope['held'][key];held.check()
    hashes={n:hashlib.sha256(held.authority[n]).hexdigest()for n in AUTHORITIES}
    actual=dict(installation_epoch_sha256=hashlib.sha256(held.doc['epoch'].encode()).hexdigest(),authority_set_sha256=digest(hashes),deployment_manifest_sha256=hashes['deployment.json'])
    need(all(facts[k]==v for k,v in actual.items())and held.approved['asset_hashes']==assets and digest(held.approved)==digest(configured(key))==facts['deployment_sha256'],'CURRENT_INSTALL_CHANGED');held.check()

def read_current(c,child_id,*,candidate_id,job_id):
    """Fresh same-connection original source; no exported audio/text/raw vectors."""
    need(c.in_transaction,'TRANSACTION_REQUIRED');validate_schema(c)
    if _READ_SCOPE.get()is None:
        with install_read_scope(c):return read_current(c,child_id,candidate_id=candidate_id,job_id=job_id)
    need(_READ_SCOPE.get()['connection']is c,'READ_SCOPE_CHANGED')
    row=_row(c,child_id,candidate_id,job_id)
    if row is None:return dict(held_execution_verified=False,receipt_sha256=None,blocker='held_execution_attestation_missing')
    try:
        need(not _read_release_blocked(),'READ_RELEASE_UNCONFIRMED')
        facts=_facts(json.loads(row['payload_json']));need(canonical(facts)==row['payload_json']and digest(facts)==row['payload_sha256'],'PAYLOAD_CHANGED')
        events=c.execute('SELECT * FROM '+EVENT+' WHERE receipt_id=?',(row['receipt_id'],)).fetchall()
        need(not events,'RESTORED_QUARANTINED')
        candidate=c.execute('SELECT * FROM pet_speaking_model_candidate WHERE candidate_id=?AND child_id=?AND session_id=?',(candidate_id,child_id,row['session_id'])).fetchone()
        job=c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=?AND child_id=?AND session_id=?',(job_id,child_id,row['session_id'])).fetchone()
        need(candidate and job and job['candidate_id']==candidate_id and job['state']=='completed'and job['owner_token']is None and job['owner_epoch']is None and job['error_code']is None,'JOB_CHANGED')
        from .pet_multipa_candidate import prepare_input
        from .database import local_media_scope
        scope=local_media_scope(c);need(bool(scope),'MEDIA_SCOPE_REQUIRED')
        binding,content=prepare_input(c,row['session_id'],media_id=candidate['media_id'],assessment_id=candidate['assessment_id'],**scope);del content
        need(binding['child_id']==child_id and canonical(binding)==candidate['binding_json']==job['binding_json']and digest(binding)==job['binding_sha256']==facts['source_binding_sha256'],'SOURCE_CHANGED')
        last=c.execute('SELECT * FROM pet_speaking_model_candidate_event WHERE candidate_id=?ORDER BY revision DESC LIMIT 1',(candidate_id,)).fetchone()
        need(last and last['operation']=='recorded'and last['revision']==1 and last['child_id']==child_id and last['request_hash']==candidate['request_hash']==row['candidate_request_sha256']and last['client_action_id']==job['client_action_id'],'SOURCE_WITHDRAWN')
        from .rubric_calibration_evidence import _audio_numeric
        numeric=json.loads(candidate['raw_numeric_json']);assets=json.loads(candidate['assets_json']);_audio_numeric(binding,numeric)
        need(digest(numeric)==row['candidate_numeric_sha256']and canonical(assets)==job['assets_json']and digest(assets)==facts['asset_set_sha256']and facts['deployment_sha256']==job['deployment_sha256']and facts['job_id_sha256']==hashlib.sha256(job_id.encode()).hexdigest(),'METADATA_CHANGED')
        need(facts['input_audio_sha256']==binding['audio']['sha256']and facts['frames']==binding['audio']['frames']and facts['reference_sha256']==binding.get('reference',{}).get('text_sha256'),'INPUT_CHANGED')
        _held_current(job['deployment_key'],facts,assets)
        return dict(held_execution_verified=True,receipt_sha256=digest(dict(row)),blocker=None)
    except (ValueError,KeyError,TypeError,OSError,AttributeError,sqlite3.Error):
        return dict(held_execution_verified=False,receipt_sha256=digest(dict(row)),blocker='held_execution_attestation_not_current')
