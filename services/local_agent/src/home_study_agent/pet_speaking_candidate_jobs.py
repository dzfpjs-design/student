"""One-purpose durable raw-model work, separate from rubric/interactive plans.

Only a newly completed original local-speaking feedback turn can register work.
No history scanner, generic command, retry, cloud gateway or mastery update.
"""
from datetime import datetime,timezone
import hashlib,json,time
from uuid import uuid4
from .pet_multipa_candidate import prepare_input
from .pet_multipa_epoch import _REVIEWED_DEPLOYMENTS
from .pet_speaking_candidate_store import canonical

TABLE='pet_speaking_candidate_job'
DDL_90="""CREATE TABLE pet_speaking_candidate_job (
 job_id TEXT PRIMARY KEY,
 child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
 session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
 media_id TEXT NOT NULL REFERENCES pet_media_binding(media_id) ON DELETE CASCADE,
 assessment_id TEXT NOT NULL REFERENCES rubric_assessment(assessment_id) ON DELETE CASCADE,
 binding_json TEXT NOT NULL,binding_sha256 TEXT NOT NULL,
 deployment_key TEXT,assets_json TEXT NOT NULL,deployment_sha256 TEXT NOT NULL,
 client_action_id TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('queued','claimed','completed','blocked','cancelled','unknown')),
 owner_token TEXT,owner_epoch TEXT,
 candidate_id TEXT REFERENCES pet_speaking_model_candidate(candidate_id) ON DELETE SET NULL,
 error_code TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(child_id,media_id,assessment_id),
 CHECK((state='claimed' AND owner_token IS NOT NULL AND owner_epoch IS NOT NULL)
 OR(state!='claimed' AND owner_token IS NULL AND owner_epoch IS NULL)))"""
# Schema91 follows the schema89 candidate contract: optional service identities
# are logical references and are revalidated before claim/CPU/commit. Core
# child/session/candidate foreign keys remain authoritative.
DDL=DDL_90.replace('media_id TEXT NOT NULL REFERENCES pet_media_binding(media_id) ON DELETE CASCADE,',
                  'media_id TEXT NOT NULL,').replace(
                  'assessment_id TEXT NOT NULL REFERENCES rubric_assessment(assessment_id) ON DELETE CASCADE,',
                  'assessment_id TEXT NOT NULL,')
IMMUTABLE="""CREATE TRIGGER pet_speaking_job_identity BEFORE UPDATE ON pet_speaking_candidate_job
 WHEN NEW.job_id IS NOT OLD.job_id OR NEW.child_id IS NOT OLD.child_id
 OR NEW.session_id IS NOT OLD.session_id OR NEW.media_id IS NOT OLD.media_id
 OR NEW.assessment_id IS NOT OLD.assessment_id OR NEW.binding_json IS NOT OLD.binding_json
 OR NEW.binding_sha256 IS NOT OLD.binding_sha256 OR NEW.deployment_key IS NOT OLD.deployment_key
 OR NEW.assets_json IS NOT OLD.assets_json OR NEW.deployment_sha256 IS NOT OLD.deployment_sha256
 OR NEW.client_action_id IS NOT OLD.client_action_id OR NEW.created_at IS NOT OLD.created_at
 OR(NEW.state IS NOT OLD.state AND NOT(
 (OLD.state='queued' AND NEW.state IN ('claimed','blocked','cancelled'))
 OR(OLD.state='claimed' AND NEW.state IN ('completed','blocked','cancelled','unknown'))
 OR(OLD.state='unknown' AND NEW.state IN ('completed','blocked'))))
 BEGIN SELECT RAISE(ABORT,'PET_SPEAKING_JOB_IDENTITY_OR_STATE'); END"""
OBJECTS=(('table',TABLE,DDL),('index','pet_speaking_job_queue',"CREATE INDEX pet_speaking_job_queue ON pet_speaking_candidate_job(state,created_at,job_id)"),('trigger','pet_speaking_job_identity',IMMUTABLE))

def need(ok,code):
    if not ok:raise ValueError('PET_SPEAKING_JOB_'+code)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
OBJECTS_90=tuple((kind,name,DDL_90 if kind=='table'else sql)for kind,name,sql in OBJECTS)

def validate_schema(c,*,source_version=None):
    from .parent_restore_assets import _validate_objects
    version=(c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0)if source_version is None else source_version
    _validate_objects(c,OBJECTS_90 if version==90 else OBJECTS,(TABLE,))

def _migrate_90(c):
    # Original caller owns this transaction. Never disable foreign keys, edit
    # sqlite_master, repair an unknown shape, commit, or publish intermediate data.
    need(c.in_transaction,'MIGRATION_TRANSACTION_REQUIRED')
    stage=TABLE+'_schema91_stage'
    c.execute('SAVEPOINT speaking_job_schema91')
    try:
        # Acquire the writer lock before rechecking the fixed old contract.
        c.execute('UPDATE schema_migration SET applied_at=applied_at WHERE version=90')
        need(c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==90,'MIGRATION_SOURCE_CHANGED')
        validate_schema(c,source_version=90)
        indexes={(r[0],r[1])for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='index'AND tbl_name=?AND sql IS NOT NULL",(TABLE,))}
        need(indexes=={(name,sql)for kind,name,sql in OBJECTS_90 if kind=='index'},'MIGRATION_UNKNOWN_INDEX')
        need(not c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(stage,)).fetchone(),'MIGRATION_STAGE_COLLISION')
        # This is a leaf table. Unknown incoming foreign keys must not acquire
        # cascading side effects when the original table is rebuilt.
        for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'AND substr(name,1,7) COLLATE NOCASE<>'sqlite_'").fetchall():
            quoted='"'+row[0].replace('"','""')+'"'
            need(not any(fk[2].casefold()==TABLE for fk in c.execute('PRAGMA foreign_key_list('+quoted+')')),'MIGRATION_INCOMING_REFERENCE')
        c.execute(DDL.replace('CREATE TABLE '+TABLE+' (','CREATE TABLE '+stage+' (',1))
        c.execute('INSERT INTO '+stage+' SELECT * FROM '+TABLE)
        def identical(left,right):
            return (c.execute('SELECT COUNT(*)FROM '+left).fetchone()[0]==c.execute('SELECT COUNT(*)FROM '+right).fetchone()[0]
                    and not c.execute('SELECT * FROM '+left+' EXCEPT SELECT * FROM '+right+' LIMIT 1').fetchone()
                    and not c.execute('SELECT * FROM '+right+' EXCEPT SELECT * FROM '+left+' LIMIT 1').fetchone())
        need(identical(TABLE,stage),'MIGRATION_ROWS_CHANGED')
        c.execute('DROP TABLE '+TABLE)
        c.execute(DDL)
        c.execute('INSERT INTO '+TABLE+' SELECT * FROM '+stage)
        need(identical(TABLE,stage),'MIGRATION_ROWS_CHANGED')
        c.execute('DROP TABLE '+stage)
        for kind,_,sql in OBJECTS:
            if kind!='table':c.execute(sql)
        validate_schema(c,source_version=91)
        need(not c.execute('PRAGMA foreign_key_check('+TABLE+')').fetchall(),'MIGRATION_CORE_REFERENCE_INVALID')
        c.execute('RELEASE speaking_job_schema91')
    except BaseException:
        c.execute('ROLLBACK TO speaking_job_schema91')
        c.execute('RELEASE speaking_job_schema91')
        raise

def ensure_schema(c):
    version=c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]or 0
    if version>=91:validate_schema(c,source_version=version);return
    if version==90:_migrate_90(c);return
    need(not c.execute("SELECT 1 FROM sqlite_master WHERE name IN('pet_speaking_candidate_job','pet_speaking_job_queue','pet_speaking_job_identity')").fetchone(),'OLD_SOURCE_COLLISION')
    for _,_,sql in OBJECTS:c.execute(sql)
    validate_schema(c,source_version=91)
def quarantine_after_restore(c):
    if c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone():
        validate_schema(c)
        c.execute("UPDATE pet_speaking_candidate_job SET state='blocked',owner_token=NULL,owner_epoch=NULL,error_code='RESTORED_NO_AUTORUN',updated_at=? WHERE state IN('queued','claimed','unknown')",(now(),))

def _metadata_binding(c,session_id,media_id,assessment_id):
    """SQL/capture metadata only. Never read/decrypt audio during registration.

    This grants no model/result eligibility. The original full prepare_input
    reads the real WAV after claim and the store repeats it before commit.
    """
    from .pet_media_evidence import _owned,_source,_hash
    from .pet_speaking_origin import original_answer_transcript
    from .learning_obligations import withdrawn_evidence_ids
    m=_owned(c,session_id,media_id)
    need(m['kind']=='speaking'and json.loads(m['state_json']).get('status')!='cancelled','SOURCE_CHANGED')
    r=c.execute('SELECT * FROM rubric_assessment WHERE child_id=? AND assessment_id=?',(m['child_id'],assessment_id)).fetchone();need(r,'SOURCE_CHANGED')
    payload=json.loads(r['payload_json'])
    q=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',(m['prompt_id'],m['child_id'],session_id)).fetchone()
    need(q and q['status']=='answered'and q['item_version']==m['item_version'],'SOURCE_CHANGED')
    item=c.execute("SELECT i.* FROM diagnostic_item i JOIN knowledge_node k USING(knowledge_node_id)WHERE i.diagnostic_item_id=? AND i.status='active'AND k.status='active'AND k.track='pet'AND i.answer_type IN('speaking','read_aloud')",(q['diagnostic_item_id'],)).fetchone()
    need(item and item['diagnostic_item_id']==m['diagnostic_item_id']and payload.get('session_id')==session_id and payload.get('material',{}).get('diagnostic_item_id')==item['diagnostic_item_id'],'SOURCE_CHANGED')
    attempt=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',(q['attempt_id'],m['child_id'],session_id)).fetchone()
    need(attempt and attempt['diagnostic_item_id']==item['diagnostic_item_id']and attempt['evidence_id']==payload.get('source_evidence_id')and attempt['evidence_id']not in withdrawn_evidence_ids(c,m['child_id']),'SOURCE_CHANGED')
    source=_source(c,m);need(_hash(source)==m['source_version'],'SOURCE_CHANGED')
    audio=json.loads(m['audio_json'])
    upper=16000*600+1 if item['answer_type']=='read_aloud'else 240000
    need(audio.get('sample_rate')==16000 and type(audio.get('frames'))is int and 0<audio['frames']<upper and type(audio.get('sha256'))is str and len(audio['sha256'])==64,'SOURCE_CHANGED')
    origin=original_answer_transcript(c,q,attempt,m,audio['sha256'])
    review=c.execute('SELECT revision FROM rubric_review_event WHERE assessment_id=? ORDER BY revision DESC LIMIT 1',(assessment_id,)).fetchone()
    reference={key:m[key]for key in('child_id','session_id','media_id','prompt_id','item_version','diagnostic_item_id','voice_input_id','source_version')}
    reference.update(assessment_id=assessment_id,assessment_fingerprint=r['fingerprint'],attempt_id=attempt['attempt_id'],evidence_id=attempt['evidence_id'],audio_sha256=audio['sha256'],answer_voice_origin=origin)
    from .pet_multipa_candidate import FIELDS
    result={k:reference[k]for k in FIELDS if k not in{'binding_version','review_revision'}}
    result.update(binding_version='pet_speaking_review_v2:'+digest(reference),review_revision=review[0]if review else 0,schema='pet_multipa_input_v1',audio={k:audio[k]for k in('sha256','frames','sample_rate')},task_family='spoken_response')
    if item['answer_type']=='read_aloud':
        from .pet_oral_source import resolve_read_aloud_reference
        reference,_=resolve_read_aloud_reference(c,m['child_id'],item['diagnostic_item_id'],m['item_version'])
        result.update(schema='pet_multipa_reference_input_v1',task_family='read_aloud',reference=reference)
    return result

def read_feedback_intent(c,session_id,turn_id):
    if not turn_id:return None
    rows=c.execute("""SELECT j.* FROM pet_speaking_candidate_job j
 JOIN rubric_assessment r ON r.assessment_id=j.assessment_id AND r.child_id=j.child_id
 JOIN agent_step s ON s.step_id=r.step_id JOIN agent_plan p ON p.plan_id=s.plan_id
 WHERE p.agent_turn_id=? AND p.child_id=j.child_id AND p.session_id=j.session_id AND j.session_id=?""",(turn_id,session_id)).fetchmany(2)
    need(len(rows)<=1,'AMBIGUOUS_ORIGINAL')
    return _job_view(rows[0])if rows else None

def _job_view(row):
    return {**{k:row[k]for k in('job_id','state','error_code','candidate_id','created_at','updated_at')},'raw_uncalibrated':True,'assessor_installed':False,'mastery_claim_allowed':False}

def register_new_feedback_intent(c,session_id,turn_id):
    """Original Runtime calls only on its new completion, before SAME commit.

    No connection, transaction, audio read, model execution or historical scan
    is opened here. An exception rolls back the original completion and intent.
    """
    from .original_connection_read import original_connection
    need(original_connection(c).in_transaction,'REGISTRATION_TRANSACTION_REQUIRED')
    if not turn_id:return None
    rows=c.execute("""SELECT DISTINCT r.assessment_id,r.child_id,m.media_id FROM rubric_assessment r
 JOIN agent_step s ON s.step_id=r.step_id JOIN agent_plan p ON p.plan_id=s.plan_id
 JOIN learning_evaluation_execution e ON e.step_id=s.step_id
 JOIN diagnostic_item i ON i.diagnostic_item_id=json_extract(r.payload_json,'$.material.diagnostic_item_id')
 JOIN knowledge_node k ON k.knowledge_node_id=i.knowledge_node_id
 JOIN diagnostic_attempt a ON a.evidence_id=json_extract(r.payload_json,'$.source_evidence_id') AND a.child_id=r.child_id AND a.session_id=p.session_id AND a.diagnostic_item_id=i.diagnostic_item_id
 JOIN diagnostic_prompt q ON q.attempt_id=a.attempt_id AND q.origin_session_id=p.session_id AND q.child_id=r.child_id AND q.diagnostic_item_id=i.diagnostic_item_id
 JOIN pet_media_binding m ON m.prompt_id=q.prompt_id AND m.child_id=r.child_id AND m.session_id=p.session_id AND m.diagnostic_item_id=i.diagnostic_item_id AND m.item_version=q.item_version
 WHERE p.agent_turn_id=? AND p.child_id=r.child_id AND p.session_id=? AND json_extract(r.payload_json,'$.session_id')=?
 AND e.state='completed'AND json_extract(e.record_json,'$.evaluation_origin')='local_diagnostic'
 AND k.track='pet'AND i.answer_type IN('speaking','read_aloud')AND m.kind='speaking'""",(turn_id,session_id,session_id)).fetchmany(65)
    need(len(rows)<=64,'AMBIGUOUS_ORIGINAL')
    valid=[]
    for row in rows:
        try:binding=_metadata_binding(c,session_id,row['media_id'],row['assessment_id'])
        except ValueError:continue # confirmation audio is not the original answer
        valid.append((row,binding))
    if not valid:return None
    need(len(valid)==1,'AMBIGUOUS_ORIGINAL')
    row,binding=valid[0]
    key=next(iter(_REVIEWED_DEPLOYMENTS))if len(_REVIEWED_DEPLOYMENTS)==1 else None
    approved=_REVIEWED_DEPLOYMENTS[key]if key is not None else None
    need(binding['child_id']==row['child_id'],'CHILD_CHANGED')
    prior=c.execute('SELECT * FROM pet_speaking_candidate_job WHERE child_id=? AND media_id=? AND assessment_id=?',(row['child_id'],row['media_id'],row['assessment_id'])).fetchone()
    if prior:return _job_view(prior) # terminal blocked/unknown never requeued
    identifier=uuid4().hex;stamp=now();assets=approved['asset_hashes']if approved else{}
    from .pet_multipa_reference import capable
    if binding['task_family']=='read_aloud' and binding['audio']['frames']>=240000:
        from .pet_multipa_reference_segments import capable as long_capable
        blocked='REFERENCE_EXECUTOR_UNAVAILABLE'if not long_capable(approved,frames=binding['audio']['frames'])else None
    else:
        blocked=('REFERENCE_EXECUTOR_UNAVAILABLE'if not capable(approved)else None)if binding['task_family']=='read_aloud'else None if approved else'UNCONFIGURED' 
    values=(identifier,row['child_id'],session_id,row['media_id'],row['assessment_id'],canonical(binding),digest(binding),key,canonical(assets),digest(approved)if approved else'UNCONFIGURED','multipa-job:'+identifier,'blocked'if blocked else'queued',None,None,None,blocked,stamp,stamp)
    c.execute('INSERT INTO pet_speaking_candidate_job VALUES('+','.join('?'for _ in values)+')',values)
    return _job_view(c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=?',(identifier,)).fetchone())

class _Claim:
    def __init__(self,row):self.job_id=row['job_id'];self.token=row['owner_token'];self.epoch=row['owner_epoch']
def validate_claim(c,claim,child_id,session_id,binding,assets):
    need(type(claim)is _Claim,'CLAIM_REQUIRED')
    row=c.execute("SELECT * FROM pet_speaking_candidate_job WHERE job_id=?",(claim.job_id,)).fetchone()
    need(row and row['state']=='claimed'and row['owner_token']==claim.token and row['owner_epoch']==claim.epoch and row['child_id']==child_id and row['session_id']==session_id and row['binding_json']==canonical(binding)and row['assets_json']==canonical(assets),'CLAIM_CHANGED')
    return row

def complete_claim(c,claim,candidate_id):
    changed=c.execute("UPDATE pet_speaking_candidate_job SET state='completed',candidate_id=?,owner_token=NULL,owner_epoch=NULL,error_code=NULL,updated_at=? WHERE job_id=? AND state='claimed'AND owner_token=? AND owner_epoch=?",(candidate_id,now(),claim.job_id,claim.token,claim.epoch))
    need(changed.rowcount==1,'CLAIM_CHANGED')

class SpeakingCandidateJobs:
    def __init__(self,database,runtime,*,media_root,data_encryption_key=None):
        need(runtime.database is database,'DATABASE_REQUIRED');self.database=database;self.runtime=runtime;self.media_root=media_root;self.dek=data_encryption_key;self.epoch=uuid4().hex
    def _deployment(self):
        if len(_REVIEWED_DEPLOYMENTS)!=1:return None,None
        key=next(iter(_REVIEWED_DEPLOYMENTS));return key,_REVIEWED_DEPLOYMENTS[key]
    def _view(self,row):return _job_view(row)
    def read(self,child_id,job_id):
        with self.database.connect()as c:row=c.execute('SELECT * FROM pet_speaking_candidate_job WHERE child_id=? AND job_id=?',(child_id,job_id)).fetchone()
        need(row,'NOT_FOUND');return self._view(row)
    def read_for_feedback_turn(self,session_id,turn_id):
        with self.database.connect()as c:return read_feedback_intent(c,session_id,turn_id)
    def recover_interrupted(self):
        # Called only at actual original runtime startup, never by __init__ or
        # parallel clients. No audio/model access and no claimed auto-retry.
        with self.database.connect()as c:
            c.execute('BEGIN IMMEDIATE');n=c.execute("UPDATE pet_speaking_candidate_job SET state='unknown',owner_token=NULL,owner_epoch=NULL,error_code='PROCESS_RESTARTED_NO_RETRY',updated_at=? WHERE state='claimed'AND owner_epoch!=?",(now(),self.epoch)).rowcount;c.commit();return n
    def _claim(self):
        with self.database.connect()as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute("SELECT * FROM pet_speaking_candidate_job WHERE state='queued'ORDER BY created_at,job_id LIMIT 1").fetchone()
            if not row:return None
            from .pet_multipa_reference import capable
            queued_binding=json.loads(row['binding_json'])
            deployment=self._deployment()[1]
            allowed=capable(deployment)
            if queued_binding.get('task_family')=='read_aloud'and queued_binding['audio']['frames']>=240000:
                from .pet_multipa_reference_segments import capable as long_capable
                allowed=long_capable(deployment,frames=queued_binding['audio']['frames'])
            if queued_binding.get('task_family')=='read_aloud'and not allowed:
                try:
                    current=_metadata_binding(c,row['session_id'],row['media_id'],row['assessment_id'])
                    need(canonical(current)==row['binding_json'],'SOURCE_CHANGED')
                    code='REFERENCE_EXECUTOR_UNAVAILABLE'
                except ValueError:
                    code='SOURCE_CHANGED'
                c.execute("UPDATE pet_speaking_candidate_job SET state='blocked',error_code=?,updated_at=? WHERE job_id=? AND state='queued'",(code,now(),row['job_id']))
                c.commit();return None
            token=uuid4().hex;c.execute("UPDATE pet_speaking_candidate_job SET state='claimed',owner_token=?,owner_epoch=?,updated_at=? WHERE job_id=? AND state='queued'",(token,self.epoch,now(),row['job_id']))
            value=dict(c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=?',(row['job_id'],)).fetchone());c.commit();return value
    def cancel(self,child_id,job_id):
        with self.database.connect()as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=? AND child_id=?',(job_id,child_id)).fetchone();need(row,'NOT_FOUND')
            if row['state']in{'queued','claimed'}:c.execute("UPDATE pet_speaking_candidate_job SET state='cancelled',owner_token=NULL,owner_epoch=NULL,error_code='CANCELLED',updated_at=? WHERE job_id=?",(now(),job_id))
            result=self._view(c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=?',(job_id,)).fetchone());c.commit();return result
    def _finish(self,row,state,code):
        with self.database.connect()as c:
            c.execute('BEGIN IMMEDIATE');c.execute("UPDATE pet_speaking_candidate_job SET state=?,owner_token=NULL,owner_epoch=NULL,error_code=?,updated_at=? WHERE job_id=? AND state='claimed'AND owner_token=? AND owner_epoch=?",(state,code,now(),row['job_id'],row['owner_token'],row['owner_epoch']));c.commit()
    def process_one(self,*,should_stop=lambda:False):
        from .pet_multipa_process import _WORKER_LOCK
        if should_stop()or _WORKER_LOCK.locked():return None
        row=self._claim()
        if not row:return None
        try:
            key,approved=self._deployment();need(approved and key==row['deployment_key']and digest(approved)==row['deployment_sha256'],'DEPLOYMENT_CHANGED')
            with self.database.connect()as c:
                binding,_=prepare_input(c,row['session_id'],media_id=row['media_id'],assessment_id=row['assessment_id'],media_root=self.media_root,data_encryption_key=self.dek)
            need(canonical(binding)==row['binding_json'],'SOURCE_CHANGED')
            last=[0.0,False]
            def stopped():
                if should_stop():return True
                if time.monotonic()-last[0]>.25:
                    with self.database.connect()as c:live=c.execute('SELECT state,owner_token,owner_epoch FROM pet_speaking_candidate_job WHERE job_id=?',(row['job_id'],)).fetchone()
                    last[:]=[time.monotonic(),not live or tuple(live)!=('claimed',row['owner_token'],self.epoch)]
                return last[1]
            self.runtime.assess_speaking_model_candidate(row['child_id'],row['session_id'],media_id=row['media_id'],assessment_id=row['assessment_id'],client_action_id=row['client_action_id'],should_stop=stopped,job_claim=_Claim(row))
        except Exception as error:
            # Source paths/transcripts/model exceptions never become job fields.
            code='CLEANUP_UNCONFIRMED'if 'CLEANUP_UNCONFIRMED'in str(error)else'SOURCE_OR_WORKER_BLOCKED'
            self._finish(row,'unknown'if code=='CLEANUP_UNCONFIRMED'else'blocked',code)
        try:return self.read(row['child_id'],row['job_id'])
        except ValueError:
            # Child/media deletion can legitimately cascade a claimed job.
            # It must stop inference, not terminate the background dispatcher.
            with self.database.connect()as c:
                if not c.execute('SELECT 1 FROM pet_speaking_candidate_job WHERE job_id=?',(row['job_id'],)).fetchone():
                    return {'state':'cancelled','error_code':'SOURCE_DELETED'}
            raise
