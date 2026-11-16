"""Fresh original SQL + pure JSON indexing; no authority/snapshot cache."""
from datetime import timedelta
from pathlib import Path
import copy,json,os,types
import pytest
from home_study_agent import rubric_audio_projection as audio,rubric_calibration_authority as authority
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_evidence import digest
from home_study_agent.pet_multipa_execution_receipt import install_read_scope,_READ_SCOPE
from test_pet_oral_supply_http import setup,CHILD,OTHER
from test_calibration_evaluator_host import signer

@pytest.fixture
def signed_rows(tmp_path,monkeypatch):
 db,_=setup(tmp_path);RubricCalibrationService(db).initialize();sign,now=signer(tmp_path,monkeypatch,children=[CHILD,OTHER]);trust=Path(os.environ['HSC_RUBRIC_CALIBRATION_TRUST_FILE']);policy=json.loads(trust.read_text());policy['issuers'][0]['kinds']=['audio_projection'];trust.write_text(json.dumps(policy));profile,rubric_hash=audio.rubric_identity();params={'nested':{'values':[1,2]}}
 payload=dict(schema='audio_rubric_projection_v1',receipt_id='pure-rows',child_id=CHILD,status='current',frozen_at=now.isoformat(),projection_version='NO_ALGORITHM_INSTALLED',deployment_sha256='a'*64,asset_hashes_sha256='b'*64,input_schema='pet_multipa_reference_input_v1',raw_adapter='multipa_reference_short_v1',task_family='read_aloud',length_lane='short',rubric_profile=profile,rubric_version=profile,rubric_hash=rubric_hash,dimensions=['fluency'],adapter_id='UNAVAILABLE',adapter_code_sha256='c'*64,parameters=params,parameters_sha256=digest(params))
 signed=sign('audio_projection',payload)
 with db.connect()as c:
  c.execute('BEGIN IMMEDIATE');receipt=authority.import_receipt(c,child_id=CHILD,**signed);c.commit()
 return db,sign,signed,receipt,now

def read(c,now,child=CHILD):return audio.artifact(c,child,'audio_projection','pure-rows',now)
def spy(monkeypatch):
 sql=[];decoded=[];oldrows=authority.rows;loads=json.loads
 def rows(c,child):
  actual=oldrows(c,child);sql.append((child,tuple(r['audit_id']for r in actual)));return actual
 def parse(value,*a,**kw):
  if isinstance(value,str)and '"client_action_id"'in value and '"envelope"'in value:decoded.append(value)
  return loads(value,*a,**kw)
 monkeypatch.setattr(authority,'rows',rows);monkeypatch.setattr(authority,'json',types.SimpleNamespace(loads=parse));return sql,decoded

def test_repeated_resolve_always_executes_original_SQL_but_parses_once(signed_rows,monkeypatch):
 db,_,_,_,now=signed_rows;sql,decoded=spy(monkeypatch)
 with db.connect()as c:
  c.execute('BEGIN')
  with install_read_scope(c),audio._signature_math_scope(c):
   first=read(c,now);assert read(c,now)==first and read(c,now)==first
   assert len(sql)==3 and len(decoded)==1
   parser=_READ_SCOPE.get()['_audio_authority_rows'];assert parser.snapshot
  assert parser.snapshot is None and parser.connection is None

@pytest.mark.parametrize('mutation',['append','revoke','quarantine','malformed','duplicate'])
def test_changed_rows_never_reuse_old_lookup_or_authority(signed_rows,mutation):
 db,_,signed,receipt,now=signed_rows
 with db.connect()as c:
  c.execute('BEGIN IMMEDIATE')
  with install_read_scope(c),audio._signature_math_scope(c):
   first=read(c,now)
   if mutation=='revoke':authority.revoke(c,child_id=CHILD,kind='audio_projection',receipt_id='pure-rows',expected_hash=receipt['envelope_hash'],client_action_id='pure-revoke')
   elif mutation=='quarantine':authority.quarantine_after_restore(c)
   else:
    value={'envelope':copy.deepcopy(signed['envelope']),'signature':signed['signature'],'client_action_id':'new-row','request_hash':'d'*64}
    if mutation=='append':value['envelope']['receipt_id']='unrelated'
    if mutation=='malformed':value={'envelope':{}}
    authority._append(c,CHILD,authority.IMPORT,value)
   if mutation=='append':assert read(c,now)==first
   else:
    with pytest.raises(ValueError):read(c,now)

def test_rollback_commit_begin_and_complete_raw_metadata_changes_are_fresh(signed_rows,monkeypatch):
 db,_,_,_,now=signed_rows;sql,decoded=spy(monkeypatch)
 with db.connect()as c:
  c.execute('BEGIN IMMEDIATE')
  with install_read_scope(c),audio._signature_math_scope(c):
   first=read(c,now);assert len(decoded)==1
   c.execute('SAVEPOINT original_rows');authority._append(c,CHILD,authority.IMPORT,{'envelope':{}})
   with pytest.raises(ValueError):read(c,now)
   c.execute('ROLLBACK TO original_rows');c.execute('RELEASE original_rows');assert read(c,now)==first
   c.commit();c.execute('BEGIN IMMEDIATE');assert read(c,now)==first
   assert len(sql)==4
   row=authority.rows(c,CHILD)[0];c.execute('UPDATE audit_event SET occurred_at=?WHERE audit_id=?',((now-timedelta(seconds=1)).isoformat(),row['audit_id']))
   prior=len(decoded);assert read(c,now)==first and len(decoded)>prior

def test_nested_result_mutation_never_escapes_cached_private_graph(signed_rows):
 db,_,_,_,now=signed_rows
 with db.connect()as c:
  c.execute('BEGIN')
  with install_read_scope(c),audio._signature_math_scope(c):
   first=read(c,now);first['parameters']['nested']['values'].append(999);first['signed_authority']['key_id']='wrong'
   other=read(c,now);assert other['parameters']['nested']['values']==[1,2]and other['signed_authority']['key_id']=='isolated-signer'

def test_other_child_and_cache_bounds_fallback_without_new_denial(signed_rows,monkeypatch):
 db,sign,signed,_,now=signed_rows;payload=copy.deepcopy(signed['envelope']['payload']);payload['child_id']=OTHER
 with db.connect()as c:
  c.execute('BEGIN IMMEDIATE');authority.import_receipt(c,child_id=OTHER,**sign('audio_projection',payload));c.commit();c.execute('BEGIN')
  with install_read_scope(c),audio._signature_math_scope(c):
   assert read(c,now)['child_id']==CHILD and read(c,now,OTHER)['child_id']==OTHER and read(c,now)['child_id']==CHILD
   monkeypatch.setattr(authority,'_PARSED_ROWS_MAX_BYTES',1)
   assert read(c,now)['child_id']==CHILD and read(c,now)['child_id']==CHILD
   assert _READ_SCOPE.get()['_audio_authority_rows'].snapshot is None


def test_complete_row_resolution_preserves_original_short_circuit_and_bad_json(signed_rows):
 db,_,_,_,now=signed_rows
 values=[({},False),({'envelope':None},False),({'envelope':{}},False),({'envelope':{'kind':'unrelated'}},True),({'envelope':{'kind':[]}},True),({'envelope':{'kind':'audio_projection'}},False),({'envelope':{'kind':'audio_projection','receipt_id':[]}},True),({'envelope':{'kind':'unrelated','receipt_id':[]}},True),('RAW_INVALID_JSON',False)]
 with db.connect()as c:
  c.execute('BEGIN IMMEDIATE')
  with install_read_scope(c),audio._signature_math_scope(c):
   for i,(value,allowed)in enumerate(values):
    c.execute('SAVEPOINT parity_case')
    audit=authority._append(c,CHILD,authority.IMPORT,value)
    if value=='RAW_INVALID_JSON':c.execute('UPDATE audit_event SET summary_json=?WHERE audit_id=?',('{invalid',audit))
    scope=_READ_SCOPE.get();checks=scope['_audio_signature_math'];parser=scope['_audio_authority_rows']
    original=authority.resolve(c,child_id=CHILD,kind='audio_projection',reference_id='pure-rows',_signatures=checks)
    actual=authority.resolve(c,child_id=CHILD,kind='audio_projection',reference_id='pure-rows',_signatures=checks,_row_parser=parser)
    assert bool(original)is allowed and actual==original,(i,value,allowed)
    c.execute('ROLLBACK TO parity_case');c.execute('RELEASE parity_case')
