"""Typed original-audio projection contracts. No production algorithm is installed.

Signed external calibration artifacts are distinct from LLM calls and from the
application's human review. Only source-reviewed adapter code may run; importing
JSON cannot install a callable, coefficients, an identity or a qualification.
"""
from datetime import datetime,timezone
from contextlib import contextmanager
import hashlib,inspect,json,math,types
from .rubric_calibration_evidence import require,digest,encoded,is_hash,timestamp,audio_selector
from .pet_speaking_review import DIMENSIONS

SCORER='typed_cpu_original_audio_scorer_v2'
PROTOCOL='original_audio_protocol_v2'
KINDS=frozenset(('audio_projection','audio_holdout','audio_blind_display','audio_blind_label'))
COMMON=frozenset(('receipt_id','child_id','status'))
BINDING=frozenset(('protocol_hash','selector','source_version','execution_receipt_sha256','raw_numeric_sha256','projection_sha256'))
IDENTITY=frozenset(('identity_authority','identity_receipt_hash','reviewer_principal_id','mask_version'))
FIELDS={
 'audio_projection':COMMON|{'schema','frozen_at','projection_version','deployment_sha256','asset_hashes_sha256','input_schema','raw_adapter','task_family','length_lane','rubric_profile','rubric_version','rubric_hash','dimensions','adapter_id','adapter_code_sha256','parameters','parameters_sha256'},
 'audio_holdout':COMMON|{'schema','scope_hash','projection_receipt_id','projection_sha256','slots','development_group_hashes'},
 'audio_blind_display':COMMON|BINDING|IDENTITY|{'schema','displayed_at','expires_at'},
 'audio_blind_label':COMMON|BINDING|IDENTITY|{'schema','display_receipt_id','display_receipt_hash','submitted_at','submission_receipt_hash','review','scores'},
}
# Deliberately empty. No registration/import/HTTP API exists. An actual adapter
# needs an independently reviewed algorithm, lawful resources and held deployment.
_REVIEWED_PROJECTION_ADAPTERS={}

def _id(v):
 require(type(v)is str and 0<len(v)<=200 and not any(ord(x)<32 for x in v),'AUDIO_PROJECTION_ID');return v

def _parameters(value,depth=0):
 require(depth<=8,'AUDIO_PROJECTION_PARAMETERS')
 if type(value)is dict:
  require(len(value)<=128,'AUDIO_PROJECTION_PARAMETERS')
  for key,v in value.items():_id(key);_parameters(v,depth+1)
 elif type(value)is list:
  require(len(value)<=4096,'AUDIO_PROJECTION_PARAMETERS')
  for v in value:_parameters(v,depth+1)
 else:require(value is None or type(value)in(bool,int)or type(value)is float and math.isfinite(value),'AUDIO_PROJECTION_PARAMETERS')

def scorer(value):
 require(type(value)is dict and set(value)=={'schema','modality','selector','projection_receipt_id'}and value['schema']==SCORER and value['modality']=='original_audio','AUDIO_CPU_SCORER_FIELDS')
 audio_selector(value['selector']);_id(value['projection_receipt_id']);return json.loads(encoded(value))

def _score_map(value):
 require(type(value)is dict and set(value)==set(DIMENSIONS)and all(type(x)is int and 0<=x<=3 for x in value.values()),'AUDIO_LABEL_SCORES');return dict(value)

def authority_shape(kind,payload):
 require(type(kind)is str and kind in KINDS and type(payload)is dict,'AUDIO_AUTHORITY_FIELDS')
 legacy=kind=='audio_holdout'and payload.get('schema')=='audio_rubric_holdout_v1'
 fields=COMMON|{'schema','frozen_at','scope_hash','projection_sha256','cases','development_group_hashes'}if legacy else FIELDS[kind]
 require(set(payload)==fields,'AUDIO_AUTHORITY_FIELDS')
 for key in COMMON:_id(payload[key])
 require(payload['status']=='current','AUDIO_AUTHORITY_STATUS')
 expected={'audio_projection':'audio_rubric_projection_v1','audio_holdout':'audio_rubric_holdout_v1'if legacy else'audio_rubric_holdout_v2','audio_blind_display':'audio_blind_display_v1','audio_blind_label':'audio_blind_label_v1'}
 require(payload['schema']==expected[kind],'AUDIO_AUTHORITY_SCHEMA')
 if kind=='audio_projection':
  for key in('deployment_sha256','asset_hashes_sha256','rubric_hash','adapter_code_sha256','parameters_sha256'):require(is_hash(payload[key]),'AUDIO_PROJECTION_HASH')
  for key in('projection_version','input_schema','raw_adapter','rubric_profile','rubric_version','adapter_id'):_id(payload[key])
  require(type(payload['task_family'])is str and payload['task_family']in{'read_aloud','spoken_response'}and type(payload['length_lane'])is str and payload['length_lane']in{'short','segmented'},'AUDIO_PROJECTION_SCOPE')
  require(type(payload['dimensions'])is list and 1<=len(payload['dimensions'])<=len(DIMENSIONS)and all(type(x)is str and x in DIMENSIONS for x in payload['dimensions'])and len(set(payload['dimensions']))==len(payload['dimensions']),'AUDIO_DIMENSION_SCOPE')
  _parameters(payload['parameters']);require(type(payload['parameters'])is dict and len(encoded(payload['parameters']).encode())<=65536 and digest(payload['parameters'])==payload['parameters_sha256'],'AUDIO_PROJECTION_PARAMETERS');timestamp(payload['frozen_at']);return
 if kind=='audio_holdout':
  require(is_hash(payload['scope_hash'])and is_hash(payload['projection_sha256']),'AUDIO_HOLDOUT_HASH')
  require(type(payload['development_group_hashes'])is list and len(payload['development_group_hashes'])<=10000 and all(is_hash(x)for x in payload['development_group_hashes']),'AUDIO_HOLDOUT_CASES')
  if legacy:
   timestamp(payload['frozen_at'])
   require(type(payload['cases'])is list and 1<=len(payload['cases'])<=10000,'AUDIO_HOLDOUT_CASES')
   for case in payload['cases']:
    require(type(case)is dict and set(case)=={'selector','source_version','execution_receipt_sha256','group_hash','stratum'},'AUDIO_HOLDOUT_CASE_FIELDS');audio_selector(case['selector']);_id(case['stratum'])
    require(all(is_hash(case[k])for k in('source_version','execution_receipt_sha256','group_hash')),'AUDIO_HOLDOUT_HASH')
   return # Imported historical format can never enter the v2 protocol below.
  _id(payload['projection_receipt_id']);require(type(payload['slots'])is list and 1<=len(payload['slots'])<=10000,'AUDIO_HOLDOUT_SLOTS')
  identities=set();groups=set();slot_ids=set()
  for slot in payload['slots']:
   require(type(slot)is dict and set(slot)=={'slot_id','diagnostic_item_id','item_version','reference_sha256','source_sha256','group_hash','stratum','ordinal'},'AUDIO_SLOT_FIELDS')
   for k in('slot_id','diagnostic_item_id','item_version','stratum'):_id(slot[k])
   require(type(slot['ordinal'])is int and slot['ordinal']==1 and all(is_hash(slot[k])for k in('reference_sha256','source_sha256','group_hash')),'AUDIO_SLOT_IDENTITY')
   identity=(slot['diagnostic_item_id'],slot['item_version'])
   require(identity not in identities and slot['slot_id']not in slot_ids and slot['group_hash']not in groups and slot['group_hash']not in payload['development_group_hashes'],'AUDIO_HOLDOUT_LEAKAGE')
   identities.add(identity);groups.add(slot['group_hash']);slot_ids.add(slot['slot_id'])
  return
 audio_selector(payload['selector'])
 for key in BINDING-{'selector'}:require(is_hash(payload[key]),'AUDIO_BLIND_BINDING')
 require(payload['identity_authority']=='independent_reviewer_registry_v1'and is_hash(payload['identity_receipt_hash'])and payload['mask_version']=='blind_no_predictions_or_peer_labels_v1','AUDIO_BLIND_IDENTITY');_id(payload['reviewer_principal_id'])
 if kind=='audio_blind_display':require(timestamp(payload['displayed_at'])<timestamp(payload['expires_at']),'AUDIO_BLIND_TIME');return
 _id(payload['display_receipt_id']);require(is_hash(payload['display_receipt_hash'])and is_hash(payload['submission_receipt_hash']),'AUDIO_BLIND_BINDING');timestamp(payload['submitted_at']);_score_map(payload['scores'])
 require(type(payload['review'])is dict,'AUDIO_BLIND_ORIGINAL_OBSERVATION')

@contextmanager
def _signature_math_scope(c):
 from .pet_multipa_execution_receipt import _READ_SCOPE
 from .rubric_calibration_authority import SignatureChecks,_ParsedAuthorityRows
 scope=_READ_SCOPE.get()
 require(c.in_transaction and scope is not None and scope['connection']is c,'AUDIO_SIGNATURE_SCOPE_CHANGED')
 existing=scope.get('_audio_signature_math')
 if existing is not None:
  require(type(existing)is SignatureChecks and type(scope.get('_audio_authority_rows'))is _ParsedAuthorityRows and scope['_audio_authority_rows'].connection is c,'AUDIO_SIGNATURE_SCOPE_CHANGED');yield;return
 checks=SignatureChecks();parser=_ParsedAuthorityRows(c);scope['_audio_signature_math']=checks;scope['_audio_authority_rows']=parser
 try:yield
 finally:
  scope.pop('_audio_signature_math',None);scope.pop('_audio_authority_rows',None);checks.clear();parser.clear()

def artifact(c,child_id,kind,reference_id,now):
 from .rubric_calibration_authority import resolve
 from .pet_multipa_execution_receipt import _READ_SCOPE
 scope=_READ_SCOPE.get();checks=None;parser=None
 if scope is not None:
  require(scope['connection']is c,'AUDIO_SIGNATURE_SCOPE_CHANGED');checks=scope.get('_audio_signature_math');parser=scope.get('_audio_authority_rows')
 _id(reference_id);proof=resolve(c,child_id=child_id,kind=kind,reference_id=reference_id,_signatures=checks,_row_parser=parser)
 require(proof is not None and proof['status']=='current'and proof['child_id']==child_id,'AUDIO_AUTHORITY_UNAVAILABLE')
 authority_shape(kind,{k:v for k,v in proof.items()if k!='signed_authority'})
 require(type(proof.get('signed_authority'))is dict and is_hash(proof['signed_authority'].get('envelope_hash')),'AUDIO_SIGNED_AUTHORITY_REQUIRED')
 return proof


def _import_anchor(c,child_id,kind,reference_id):
 from .rubric_calibration_authority import rows,IMPORT
 proof=artifact(c,child_id,kind,reference_id,datetime.now(timezone.utc));matches=[]
 for row in rows(c,child_id):
  if row['action']!=IMPORT:continue
  envelope=json.loads(row['summary_json']).get('envelope',{})
  if envelope.get('kind')==kind and envelope.get('receipt_id')==reference_id:
   require(digest(envelope)==proof['signed_authority']['envelope_hash'],'AUDIO_AUTHORITY_ANCHOR_CHANGED');matches.append(row)
 require(len(matches)==1,'AUDIO_AUTHORITY_ANCHOR_REQUIRED');row=matches[0];timestamp(row['occurred_at'])
 return {'audit_id':row['audit_id'],'occurred_at':row['occurred_at'],'envelope_hash':proof['signed_authority']['envelope_hash']}


def _slot_reference(c,child_id,slot):
 from .pet_oral_source import resolve_read_aloud_reference
 reference,_=resolve_read_aloud_reference(c,child_id,slot['diagnostic_item_id'],slot['item_version'])
 require(digest(reference)==slot['reference_sha256']and digest(reference['source'])==slot['source_sha256'],'AUDIO_SLOT_MATERIAL_CHANGED')
 return reference


def validate_holdout_import(c,child_id,envelope):
 require(c.in_transaction,'AUDIO_TRANSACTION_REQUIRED');proof=envelope['payload']
 if proof['schema']!='audio_rubric_holdout_v2':return # Legacy cannot qualify.
 projection=artifact(c,child_id,'audio_projection',proof['projection_receipt_id'],datetime.now(timezone.utc))
 anchor=_import_anchor(c,child_id,'audio_projection',proof['projection_receipt_id'])
 require(projection['task_family']=='read_aloud'and digest(projection)==proof['projection_sha256']and timestamp(projection['frozen_at'])<=timestamp(anchor['occurred_at'])<=datetime.now(timezone.utc),'AUDIO_PROSPECTIVE_PROJECTION_REQUIRED')
 for slot in proof['slots']:_slot_reference(c,child_id,slot)


def _resolve_slot(c,child_id,slot,anchor):
 from .rubric_calibration_evidence import audio_projection_source
 from .voice_capture_binding import capture_started_at
 reference=_slot_reference(c,child_id,slot)
 # The first original job is fixed irrespective of its state. Never skip a
 # blocked/withdrawn/missing first attempt in favor of a more favorable result.
 row=c.execute("SELECT *FROM pet_speaking_candidate_job WHERE child_id=?AND json_extract(binding_json,'$.reference.diagnostic_item_id')=?AND julianday(created_at)>=julianday(?)ORDER BY julianday(created_at),job_id LIMIT 1",(child_id,slot['diagnostic_item_id'],anchor['occurred_at'])).fetchone()
 require(row is not None,'AUDIO_SLOT_NO_FUTURE_ORIGINAL')
 require(row['state']=='completed'and row['candidate_id']is not None,'AUDIO_SLOT_FIRST_ORIGINAL_NOT_COMPLETED')
 binding=json.loads(row['binding_json'])
 require(binding['item_version']==slot['item_version']and binding.get('reference')==reference,'AUDIO_SLOT_MATERIAL_CHANGED')
 capture=c.execute('SELECT *FROM voice_capture_binding WHERE child_id=?AND session_id=?AND voice_input_id=?',(child_id,row['session_id'],binding['voice_input_id'])).fetchone()
 voice=c.execute('SELECT created_at FROM voice_input WHERE child_id=?AND session_id=?AND voice_input_id=?',(child_id,row['session_id'],binding['voice_input_id'])).fetchone()
 start=capture_started_at(c,binding['voice_input_id'])
 # Only the original server-owned local-WAV admission is a supported clock.
 # Client-reported realtime onset is never promoted to trusted chronology.
 require(capture is not None and capture['source_kind']=='local_wav'and voice is not None and start==capture['created_at']and timestamp(anchor['occurred_at'])<=timestamp(start)<=timestamp(voice['created_at'])<=timestamp(row['created_at']),'AUDIO_SLOT_CAPTURE_BEFORE_ANCHOR')
 selector={'modality':'original_audio',**{k:row[k]for k in('candidate_id','job_id','assessment_id','media_id')}}
 source=audio_projection_source(c,child_id,selector)
 created=c.execute('SELECT created_at FROM pet_speaking_model_candidate WHERE child_id=?AND candidate_id=?',(child_id,row['candidate_id'])).fetchone()[0]
 require(source['held_execution_verified']and capture['wav_sha256']==source['audio_sha256']and timestamp(row['created_at'])<=timestamp(created)<=datetime.now(timezone.utc),'AUDIO_SLOT_EXECUTION_REQUIRED')
 return {'slot_id':slot['slot_id'],'selector':selector,'source_version':source['source_version'],'execution_receipt_sha256':source['execution_receipt_sha256'],'input_binding_sha256':row['binding_sha256'],'audio_sha256':source['audio_sha256'],'capture_started_at':start,'voice_admitted_at':voice['created_at'],'group_hash':slot['group_hash'],'stratum':slot['stratum'],'source_ids':_linked_source_ids(c,child_id,source)},source

def rubric_identity():
 from .pet_audio_rubric import PROFILE,dimensions
 definitions=[{k:d[k]for k in('dimension_id','criterion','max_score','pass_score','score_anchors')}for d in dimensions()if d['dimension_id']in DIMENSIONS]
 return PROFILE,digest(definitions)

def projection_output(value,frames,declared):
 require(type(value)is dict and set(value)==set(DIMENSIONS),'AUDIO_PROJECTION_OUTPUT')
 result={}
 for name in DIMENSIONS:
  row=value[name];require(type(row)is dict and set(row)=={'score','start_frame','end_frame','abstain_reason'},'AUDIO_PROJECTION_OUTPUT')
  if row['score']is None:
   require(row['start_frame']is row['end_frame']is None and type(row['abstain_reason'])is str and row['abstain_reason']in{'unsupported_feature','out_of_scope','insufficient_evidence'},'AUDIO_PROJECTION_ABSTENTION')
  else:
   require(name in declared and type(row['score'])is int and 0<=row['score']<=3 and type(row['start_frame'])is int and type(row['end_frame'])is int and 0<=row['start_frame']<row['end_frame']<=frames and row['abstain_reason']is None,'AUDIO_PROJECTION_FRAME')
  result[name]=dict(row)
 return result

def project(c,child_id,selector,*,projection_receipt_id,protocol_hash,now):
 require(c.in_transaction and is_hash(protocol_hash),'AUDIO_TRANSACTION_REQUIRED');audio_selector(selector)
 from .rubric_calibration_evidence import audio_projection_source
 from .pet_multipa_execution_receipt import install_read_scope
 with install_read_scope(c),_signature_math_scope(c):
  source=audio_projection_source(c,child_id,selector)
  require(source['held_execution_verified'],'AUDIO_EXECUTION_NOT_CURRENT')
  proof=artifact(c,child_id,'audio_projection',projection_receipt_id,now);profile,rubric_hash=rubric_identity()
  require(proof['rubric_profile']==profile and proof['rubric_version']==profile and proof['rubric_hash']==rubric_hash and proof['task_family']==source['task_family']and proof['deployment_sha256']==source['deployment_sha256']and proof['asset_hashes_sha256']==source['asset_hashes_sha256'],'AUDIO_PROJECTION_SCOPE_CHANGED')
  row=c.execute('SELECT binding_json,raw_numeric_json FROM pet_speaking_model_candidate WHERE child_id=?AND candidate_id=?',(child_id,selector['candidate_id'])).fetchone();binding=json.loads(row['binding_json']);numeric=json.loads(row['raw_numeric_json'])
  lane='segmented'if binding['task_family']=='read_aloud'and binding['audio']['frames']>=240000 else'short';adapter=numeric.get('adapter','multipa_original_numeric_v1')
  require(proof['input_schema']==binding['schema']and proof['raw_adapter']==adapter and proof['length_lane']==lane,'AUDIO_PROJECTION_ADAPTER_SCOPE')
  registered=_REVIEWED_PROJECTION_ADAPTERS.get(proof['adapter_id']);require(type(registered)is tuple and len(registered)==2 and type(registered[0])is types.FunctionType and registered[1]==proof['adapter_code_sha256'],'AUDIO_PROJECTION_UNAVAILABLE')
  try:code_hash=hashlib.sha256(inspect.getsource(registered[0]).encode()).hexdigest()
  except(OSError,TypeError):require(False,'AUDIO_PROJECTION_UNAVAILABLE')
  require(code_hash==proof['adapter_code_sha256'],'AUDIO_PROJECTION_CODE_CHANGED')
  # Only a reviewed local implementation executes; no uploaded code/expressions.
  try:value=registered[0](numeric,dict(proof['parameters']),frames=source['frames'],task_family=source['task_family'],lane=lane)
  except Exception:raise ValueError('RUBRIC_CALIBRATION_AUDIO_PROJECTION_REJECTED')from None
  predicted=projection_output(value,source['frames'],proof['dimensions'])
  require(audio_projection_source(c,child_id,selector)==source,'AUDIO_SOURCE_CHANGED_DURING_PROJECTION')
  provenance={'schema':'typed_cpu_score_provenance_v1','child_id':child_id,'selector':dict(selector),'source_version':source['source_version'],'execution_receipt_sha256':source['execution_receipt_sha256'],'raw_numeric_sha256':source['raw_numeric_sha256'],'deployment_sha256':source['deployment_sha256'],'projection_sha256':digest(proof),'parameters_sha256':proof['parameters_sha256'],'rubric_hash':rubric_hash,'prediction_sha256':digest(predicted),'protocol_hash':protocol_hash,'dimensions':list(proof['dimensions'])}
  return {'source':source,'projection':proof,'prediction':predicted,'provenance':provenance,'provenance_hash':digest(provenance),'score_qualified':False,'mastery_claim_allowed':False,'certification_claim_allowed':False}


def validate_blind_import(c,child_id,envelope,prior):
 require(c.in_transaction,'AUDIO_TRANSACTION_REQUIRED');kind=envelope['kind'];proof=envelope['payload'];now=datetime.now(timezone.utc)
 source=None
 from .rubric_calibration_evidence import audio_projection_source,audio_label_scores
 from .pet_multipa_execution_receipt import install_read_scope
 with install_read_scope(c),_signature_math_scope(c):
  source=audio_projection_source(c,child_id,proof['selector']);require(source['held_execution_verified'],'AUDIO_EXECUTION_NOT_CURRENT')
  require(all(proof[k]==source[k]for k in('source_version','execution_receipt_sha256','raw_numeric_sha256')),'AUDIO_BLIND_BINDING_CHANGED')
  protocol=c.execute('SELECT *FROM rubric_calibration_protocol WHERE child_id=?AND protocol_hash=?',(child_id,proof['protocol_hash'])).fetchall()
  require(len(protocol)==1 and json.loads(protocol[0]['manifest_json']).get('schema')==PROTOCOL,'AUDIO_PROTOCOL_REQUIRED')
  manifest=json.loads(protocol[0]['manifest_json'])
  cases=[x for x in manifest['cases']if x['selector']==proof['selector']]
  require(len(cases)==1 and cases[0]['source_version']==source['source_version']and cases[0]['execution_receipt_sha256']==source['execution_receipt_sha256'],'AUDIO_CASE_NOT_IN_HOLDOUT')
  pscorer=scorer(json.loads(protocol[0]['scorer_json']));projection=artifact(c,child_id,'audio_projection',pscorer['projection_receipt_id'],now)
  require(digest(projection)==proof['projection_sha256'],'AUDIO_PROJECTION_CHANGED')
  require(timestamp(protocol[0]['created_at'])<=timestamp(proof['displayed_at']if kind=='audio_blind_display'else proof['submitted_at'])<=now,'AUDIO_BLIND_TIME')
  require(not c.execute("SELECT 1 FROM rubric_calibration_event WHERE protocol_id=?AND operation IN('revoke','restore_quarantine','source_invalidated')",(protocol[0]['protocol_id'],)).fetchone(),'AUDIO_PROTOCOL_INACTIVE')
  if kind=='audio_blind_display':
   require(now<timestamp(proof['expires_at']),'AUDIO_DISPLAY_EXPIRED');return
  display=artifact(c,child_id,'audio_blind_display',proof['display_receipt_id'],now)
  require(digest(display)==proof['display_receipt_hash']and all(proof[k]==display[k]for k in BINDING|IDENTITY),'AUDIO_DISPLAY_BINDING_CHANGED')
  require(timestamp(display['displayed_at'])<=timestamp(proof['submitted_at'])<timestamp(display['expires_at'])and timestamp(proof['submitted_at'])<=now<timestamp(display['expires_at']),'AUDIO_DISPLAY_EXPIRED')
  for row in prior:
   if row['action']!='rubric_authority_import_v1':continue
   old=json.loads(row['summary_json']).get('envelope',{})
   require(not(old.get('kind')=='audio_blind_label'and old.get('payload',{}).get('display_receipt_id')==proof['display_receipt_id']),'AUDIO_DISPLAY_ALREADY_CONSUMED')
  audio_label_scores(c,child_id,source,proof)


def _scope(value):
 require(type(value)is dict and set(value)=={'subject','grade','task_family','strata'}and type(value['strata'])is list and 1<=len(value['strata'])<=20 and all(type(x)is str for x in value['strata'])and len(set(value['strata']))==len(value['strata']),'AUDIO_SCOPE_FIELDS')
 for v in(value['subject'],value['grade'],value['task_family'],*value['strata']):_id(v)
 require(value['task_family']in{'read_aloud','spoken_response'},'AUDIO_TASK_SCOPE');return dict(value)

def _source_scope(c,child,source,scope):
 row=c.execute('SELECT kn.subject,ch.grade FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id) JOIN child_profile ch ON ch.child_id=?WHERE di.diagnostic_item_id=?',(child,source['identity']['diagnostic_item_id'])).fetchone()
 require(row and row['subject']==scope['subject']and row['grade']==scope['grade']and source['task_family']==scope['task_family'],'AUDIO_CASE_SCOPE')

def _linked_source_ids(c,child,source):
 # The original source has already passed original prepare/validate_item. Keep
 # its persisted source identity for the existing privacy invalidation owner.
 row=c.execute('SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?',(source['identity']['diagnostic_item_id'],)).fetchone()
 require(row is not None,'AUDIO_EXACT_SOURCE_REQUIRED');prefix='parent_approved_specialized:'
 if not row[0].startswith(prefix):return []
 package=c.execute("SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?AND child_id=?AND state='published'",(row[0][len(prefix):],child)).fetchone()
 require(package is not None,'AUDIO_EXACT_SOURCE_REQUIRED')
 identity=json.loads(package[0])['source']['source_id'];_id(identity)
 return [identity]


def _unique_holdout_audio(c,child_id,application,cases,anchor):
 """Exact same-child bytes cannot become independent prospective samples.

 This is a deterministic replay/leakage guard, not speaker identity, an acoustic
 quality judgment, or proof that changed bytes contain independently new speech.
 """
 require(c.in_transaction,'AUDIO_TRANSACTION_REQUIRED');seen={application['audio_sha256']};start=timestamp(anchor['occurred_at'])
 for case in cases:
  audio=case['audio_sha256'];require(is_hash(audio)and audio not in seen,'AUDIO_DUPLICATE_OR_APPLICATION_RECORDING');seen.add(audio)
  prior=c.execute('SELECT created_at FROM voice_capture_binding WHERE child_id=?AND wav_sha256=?',(child_id,audio)).fetchall()
  require(not any(timestamp(row[0])<start for row in prior),'AUDIO_RECORDED_BEFORE_HOLDOUT_ANCHOR')

def freeze_v2(service,child_id,*,scorer_value,scope,holdout_receipt_id,client_action_id):
 from datetime import timedelta
 from uuid import uuid4
 from .rubric_calibration import VERSION,_event
 from .rubric_calibration_evidence import POLICY,audio_projection_source
 from .rubric_calibration_transaction import calibration_connection
 from .pet_multipa_execution_receipt import install_read_scope
 value=scorer(scorer_value);scope=_scope(scope);_id(client_action_id);_id(holdout_receipt_id)
 request_hash=digest(['freeze_original_audio_v2',value,scope,holdout_receipt_id])
 with calibration_connection(service.database)as c:
  c.execute('BEGIN IMMEDIATE')
  with install_read_scope(c),_signature_math_scope(c):
   replay=service._replay(c,child_id,client_action_id,request_hash)
   if replay:return replay
   now=service._now();source=audio_projection_source(c,child_id,value['selector']);require(source['held_execution_verified'],'AUDIO_EXECUTION_NOT_CURRENT');_source_scope(c,child_id,source,scope)
   projection=artifact(c,child_id,'audio_projection',value['projection_receipt_id'],now)
   holdout=artifact(c,child_id,'audio_holdout',holdout_receipt_id,now)
   require(holdout['schema']=='audio_rubric_holdout_v2'and scope['task_family']=='read_aloud','AUDIO_PROSPECTIVE_HOLDOUT_REQUIRED')
   projection_anchor=_import_anchor(c,child_id,'audio_projection',value['projection_receipt_id']);holdout_anchor=_import_anchor(c,child_id,'audio_holdout',holdout_receipt_id)
   require(holdout['projection_receipt_id']==value['projection_receipt_id']and holdout['scope_hash']==digest(scope)and holdout['projection_sha256']==digest(projection)and timestamp(projection_anchor['occurred_at'])<=timestamp(holdout_anchor['occurred_at'])<=now,'AUDIO_HOLDOUT_SCOPE')
   cases=[];assessments=set();evidence=set()
   for slot in holdout['slots']:
    require(slot['stratum']in scope['strata'],'AUDIO_CASE_SCOPE')
    case,original=_resolve_slot(c,child_id,slot,holdout_anchor);_source_scope(c,child_id,original,scope)
    aid=original['identity']['assessment_id'];eid=original['identity']['evidence_id']
    require(aid not in assessments and eid not in evidence and aid!=value['selector']['assessment_id']and eid!=source['identity']['evidence_id'],'AUDIO_HOLDOUT_LEAKAGE');assessments.add(aid);evidence.add(eid);cases.append(case)
   _unique_holdout_audio(c,child_id,source,cases,holdout_anchor)
   manifest={'schema':PROTOCOL,'application_source':source,'application_source_ids':_linked_source_ids(c,child_id,source),'holdout_receipt_id':holdout_receipt_id,'holdout_hash':digest(holdout),'projection_hash':digest(projection),'projection_anchor':projection_anchor,'holdout_anchor':holdout_anchor,'cases':cases,'policy_hash':digest(POLICY)}
   pid=str(uuid4());expires=(now+timedelta(days=POLICY['max_valid_days'])).isoformat();phash=digest([VERSION,child_id,value,scope,manifest,now.isoformat(),expires])
   c.execute('INSERT INTO rubric_calibration_protocol VALUES(?,?,?,?,?,?,?,?,?)',(pid,child_id,VERSION,encoded(value),encoded(scope),encoded(manifest),phash,now.isoformat(),expires))
   row=service._owned(c,child_id,pid);receipt=_event(c,row,'freeze',{},client_action_id,request_hash,now.isoformat());result={'receipt':receipt,'current':read_v2(service,c,row,now)}
  c.commit();return result


def case_v2(service,c,row,reference,now):
 from .rubric_calibration_host import actual_audio_scorer_receipt
 from .rubric_calibration_evidence import audio_label_scores
 manifest=json.loads(row['manifest_json']);value=scorer(json.loads(row['scorer_json']));matches=[x for x in manifest['cases']if x['selector']['assessment_id']==reference['assessment_id']]
 require(len(matches)==1,'AUDIO_CASE_NOT_IN_HOLDOUT');case=matches[0]
 actual=actual_audio_scorer_receipt(c,row['child_id'],case['selector'],projection_receipt_id=value['projection_receipt_id'],protocol_hash=row['protocol_hash'],as_of=now)
 source=actual['source'];require(source['source_version']==case['source_version']and source['execution_receipt_sha256']==case['execution_receipt_sha256']and digest(actual['projection'])==manifest['projection_hash'],'AUDIO_CASE_SOURCE_CHANGED')
 require(type(reference['blind_receipt_ids'])is list and len(reference['blind_receipt_ids'])==2 and all(type(x)is str for x in reference['blind_receipt_ids'])and len(set(reference['blind_receipt_ids']))==2,'TWO_BLIND_LABELS_REQUIRED')
 labels=[];principals=set();proofs=[]
 for rid in reference['blind_receipt_ids']:
  proof=artifact(c,row['child_id'],'audio_blind_label',rid,now);display=artifact(c,row['child_id'],'audio_blind_display',proof['display_receipt_id'],now)
  require(proof['protocol_hash']==row['protocol_hash']and proof['selector']==case['selector']and proof['projection_sha256']==manifest['projection_hash']and digest(display)==proof['display_receipt_hash']and all(proof[k]==display[k]for k in BINDING|IDENTITY),'AUDIO_BLIND_BINDING_CHANGED')
  require(timestamp(row['created_at'])<=timestamp(display['displayed_at'])<=timestamp(proof['submitted_at'])<timestamp(display['expires_at'])and timestamp(proof['submitted_at'])<=now,'AUDIO_BLIND_TIME')
  require(proof['reviewer_principal_id']not in principals,'INDEPENDENT_REVIEWERS_REQUIRED');principals.add(proof['reviewer_principal_id'])
  labels.append(audio_label_scores(c,row['child_id'],source,proof));proofs.append([rid,digest(proof),proof['display_receipt_id'],digest(display)])
 require(labels[0]==labels[1],'LABEL_DISAGREEMENT')
 predicted={k:v['score']for k,v in actual['prediction'].items()}
 return {'evidence_id':source['identity']['evidence_id'],'stratum':case['stratum'],'group_hash':case['group_hash'],'gold':labels[0],'predicted':predicted,'fingerprint':digest([source,actual['provenance_hash'],proofs])}

def read_v2(service,c,row,now,*,require_live=False):
 from .rubric_calibration import VERSION
 from .rubric_calibration_evidence import POLICY,summarize_cases,audio_projection_source
 from .pet_multipa_execution_receipt import install_read_scope
 if not c.in_transaction:
  c.execute('BEGIN')
  try:return read_v2(service,c,row,now,require_live=require_live)
  finally:c.rollback()
 with install_read_scope(c),_signature_math_scope(c):
  value=scorer(json.loads(row['scorer_json']));scope=_scope(json.loads(row['scope_json']));manifest=json.loads(row['manifest_json'])
  require(manifest['schema']==PROTOCOL and row['version']==VERSION and row['protocol_hash']==digest([VERSION,row['child_id'],value,scope,manifest,row['created_at'],row['expires_at']]),'AUDIO_PROTOCOL_INVALID')
  events=c.execute('SELECT *FROM rubric_calibration_event WHERE protocol_id=?ORDER BY revision',(row['protocol_id'],)).fetchall();ops={x['operation']for x in events};blockers=[];status='collecting';cases=[];metrics={}
  if 'restore_quarantine'in ops:status='quarantined';blockers.append('restore_revalidation_required')
  elif 'revoke'in ops or'source_invalidated'in ops:status='revoked';blockers.append('qualification_revoked')
  if status not in{'quarantined','revoked'}and(now<timestamp(row['created_at'])or now>=timestamp(row['expires_at'])):status='invalidated';blockers.append('protocol_expired')
  if status not in{'quarantined','revoked'}and manifest['policy_hash']!=digest(POLICY):status='invalidated';blockers.append('policy_changed')
  try:
   projection=artifact(c,row['child_id'],'audio_projection',value['projection_receipt_id'],now);holdout=artifact(c,row['child_id'],'audio_holdout',manifest['holdout_receipt_id'],now)
   require(holdout['schema']=='audio_rubric_holdout_v2'and digest(projection)==manifest['projection_hash']and digest(holdout)==manifest['holdout_hash']and holdout['scope_hash']==digest(scope)and _import_anchor(c,row['child_id'],'audio_projection',value['projection_receipt_id'])==manifest['projection_anchor']and _import_anchor(c,row['child_id'],'audio_holdout',manifest['holdout_receipt_id'])==manifest['holdout_anchor'],'AUDIO_FROZEN_AUTHORITY_CHANGED')
   require(audio_projection_source(c,row['child_id'],value['selector'])==manifest['application_source'],'AUDIO_APPLICATION_SOURCE_CHANGED')
   require(_linked_source_ids(c,row['child_id'],manifest['application_source'])==manifest['application_source_ids'],'AUDIO_APPLICATION_SOURCE_CHANGED')
   require(len(holdout['slots'])==len(manifest['cases']),'AUDIO_FROZEN_AUTHORITY_CHANGED')
   for slot,case in zip(holdout['slots'],manifest['cases']):
    resolved,_=_resolve_slot(c,row['child_id'],slot,manifest['holdout_anchor']);require(resolved==case,'AUDIO_HOLDOUT_SOURCE_CHANGED')
   _unique_holdout_audio(c,row['child_id'],manifest['application_source'],manifest['cases'],manifest['holdout_anchor'])
   for event in events:
    if event['operation']=='record_case':
     reference=json.loads(event['reference_json']);case=case_v2(service,c,row,reference,now);require(case['fingerprint']==reference['case_fingerprint']and case['evidence_id']==reference['evidence_id'],'AUDIO_CASE_CHANGED');cases.append(case)
   metrics,statistics=summarize_cases(cases,projection['dimensions'],scope['strata']);blockers.extend(statistics)
   if len(cases)!=len(manifest['cases']):blockers.append('holdout_cases_incomplete')
  except (ValueError,KeyError,TypeError,OSError):
   if status not in{'quarantined','revoked'}:status='invalidated'
   blockers.append('audio_source_or_authority_not_current')
  if not blockers:status='qualified'if'qualify'in ops else'ready_for_evaluation'
  qualified=status=='qualified';require(not require_live or qualified,'PROTOCOL_NOT_QUALIFIED')
  return {'protocol_id':row['protocol_id'],'protocol_hash':row['protocol_hash'],'revision':events[-1]['revision']if events else 0,'status':status,'blockers':sorted(set(blockers)),'score_qualified':qualified,'score_scope':'original_audio_dimensions_only','mastery_claim_allowed':False,'certification_claim_allowed':False,'metrics':metrics,'scorer':value,'scope':scope,'expires_at':row['expires_at'],'requirements':service.requirements(),'local_only':True,'cloud_transmission_allowed':False}

def qualified_v2(service,c,row,assessment_id,now,view):
 from .rubric_calibration_host import actual_audio_scorer_receipt
 value=scorer(json.loads(row['scorer_json']));selector=value['selector'];require(selector['assessment_id']==assessment_id,'AUDIO_EXACT_ASSESSMENT_REQUIRED')
 if not view['score_qualified']:return {'status':'reference','score_qualified':False,'blockers':view['blockers'],'score_scope':'original_audio_dimensions_only','mastery_claim_allowed':False,'certification_claim_allowed':False}
 actual=actual_audio_scorer_receipt(c,row['child_id'],selector,projection_receipt_id=value['projection_receipt_id'],protocol_hash=row['protocol_hash'],as_of=now)
 require(actual['source']==json.loads(row['manifest_json'])['application_source'],'AUDIO_APPLICATION_SOURCE_CHANGED')
 certificate=c.execute("SELECT *FROM rubric_calibration_event WHERE protocol_id=?AND operation='qualify'ORDER BY revision",(row['protocol_id'],)).fetchall();require(len(certificate)==1,'AUDIO_CERTIFICATE_REQUIRED')
 scores=actual['prediction'];covered=actual['projection']['dimensions'];abstained=any(scores[k]['score']is None for k in covered)
 return {'status':'reference'if abstained else'calibrated_audio_dimensions','score_qualified':not abstained,'score_authority':'typed_cpu_original_audio_projection_v1','score_scope':'original_audio_dimensions_only','assessment_id':assessment_id,'protocol_id':row['protocol_id'],'protocol_hash':row['protocol_hash'],'qualification_event_id':certificate[0]['event_id'],'source_version':actual['source']['source_version'],'execution_receipt_sha256':actual['source']['execution_receipt_sha256'],'provenance':actual['provenance'],'covered_dimensions':covered,'dimensions':scores,'mastery_claim_allowed':False,'certification_claim_allowed':False,'local_only':True,'cloud_transmission_allowed':False}


def application_receipt(service,c,child_id,assessment_id,now):
 require(c.in_transaction,'AUDIO_TRANSACTION_REQUIRED')
 from .pet_multipa_execution_receipt import install_read_scope
 with install_read_scope(c),_signature_math_scope(c):
  return _application_receipt(service,c,child_id,assessment_id,now)


def _application_receipt(service,c,child_id,assessment_id,now):
 # Selection is an exact persisted assessment binding, not a latest candidate,
 # timestamp heuristic or caller-supplied approved result. Ambiguity is closed.
 require(c.in_transaction,'AUDIO_TRANSACTION_REQUIRED');_id(assessment_id)
 from .rubric_calibration import _exists
 if not _exists(c):return {'status':'unavailable','score_qualified':False,'score_scope':'original_audio_dimensions_only','blockers':['audio_qualified_protocol_missing_or_ambiguous'],'mastery_claim_allowed':False,'certification_claim_allowed':False}
 rows=c.execute("SELECT p.*FROM rubric_calibration_protocol p WHERE child_id=?AND json_extract(scorer_json,'$.schema')=?AND json_extract(scorer_json,'$.selector.assessment_id')=?AND julianday(created_at)<=julianday(?)AND julianday(expires_at)>julianday(?)AND EXISTS(SELECT 1 FROM rubric_calibration_event e WHERE e.protocol_id=p.protocol_id AND e.operation='qualify')AND NOT EXISTS(SELECT 1 FROM rubric_calibration_event e WHERE e.protocol_id=p.protocol_id AND e.operation IN('revoke','restore_quarantine','source_invalidated'))LIMIT 2",(child_id,SCORER,assessment_id,now.isoformat(),now.isoformat())).fetchall()
 if len(rows)!=1:return {'status':'unavailable','score_qualified':False,'score_scope':'original_audio_dimensions_only','blockers':['audio_qualified_protocol_missing_or_ambiguous'],'mastery_claim_allowed':False,'certification_claim_allowed':False}
 try:
  view=read_v2(service,c,rows[0],now);return qualified_v2(service,c,rows[0],assessment_id,now,view)
 except (ValueError,KeyError,TypeError,OSError):return {'status':'unavailable','score_qualified':False,'score_scope':'original_audio_dimensions_only','blockers':['audio_source_or_authority_not_current'],'mastery_claim_allowed':False,'certification_claim_allowed':False}


def command_v2(service,c,row,operation,reference,expected_revision,key,request_hash):
 """Original command/CAS semantics, one read lease, release before commit.

 This exact modality branch never changes the original text command body.
 A release failure rolls back the event instead of acknowledging a committed
 result while the native read owner is unknown.
 """
 from .rubric_calibration import _event
 from .rubric_calibration_evidence import POLICY
 from .pet_multipa_execution_receipt import install_read_scope
 require(c.in_transaction and operation in{'record_case','evaluate','revoke'},'AUDIO_COMMAND_REQUIRED')
 with install_read_scope(c),_signature_math_scope(c):
  replay=service._replay(c,row['child_id'],key,request_hash)
  if replay:return replay
  now=service._now();before=read_v2(service,c,row,now)
  require(before['revision']==expected_revision,'REVISION_CONFLICT')
  require(before['status']not in{'revoked','quarantined'}and(operation=='revoke'or before['status']!='invalidated'),'PROTOCOL_INACTIVE')
  if operation=='record_case':
   require(not c.execute("SELECT 1 FROM rubric_calibration_event WHERE protocol_id=?AND operation='qualify'",(row['protocol_id'],)).fetchone(),'PROTOCOL_SEALED')
   case=case_v2(service,c,row,reference,now)
   previous=c.execute("SELECT reference_json FROM rubric_calibration_event WHERE protocol_id=?AND operation='record_case'",(row['protocol_id'],)).fetchall()
   require(not any(json.loads(x[0])['evidence_id']==case['evidence_id']for x in previous),'CASE_ALREADY_REGISTERED')
   reference={**reference,'case_fingerprint':case['fingerprint'],'evidence_id':case['evidence_id']}
  elif operation=='evaluate':
   if before['status']in{'ready_for_evaluation','qualified'}:
    refs=c.execute("SELECT reference_json FROM rubric_calibration_event WHERE protocol_id=?AND operation='record_case'ORDER BY revision",(row['protocol_id'],)).fetchall()
    reference={'case_digest':digest([json.loads(x[0])['case_fingerprint']for x in refs]),'policy_hash':digest(POLICY)};operation='qualify'
   else:reference={'blockers':before['blockers']}
  receipt=_event(c,row,operation,reference,key,request_hash,now.isoformat())
  result={'receipt':receipt,'current':read_v2(service,c,row,now)}
 c.commit();return result
