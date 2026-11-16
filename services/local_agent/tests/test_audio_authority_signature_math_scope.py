"""Actual signed authority imports/SQLite/RAM scopes. No acoustic data or model."""
from contextlib import nullcontext
from datetime import datetime,timedelta,timezone
from pathlib import Path
import copy,json,os
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from home_study_agent import rubric_audio_projection as audio,rubric_calibration_authority as authority
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_evidence import digest
from home_study_agent.pet_multipa_execution_receipt import install_read_scope,_READ_SCOPE
from test_pet_oral_supply_http import setup,CHILD
from test_calibration_evaluator_host import signer

@pytest.fixture
def original(tmp_path,monkeypatch):
 db,_=setup(tmp_path);RubricCalibrationService(db).initialize();sign,now=signer(tmp_path,monkeypatch,children=[CHILD])
 trust=Path(os.environ['HSC_RUBRIC_CALIBRATION_TRUST_FILE']);policy=json.loads(trust.read_text());policy['issuers'][0]['kinds']=['audio_projection'];trust.write_text(json.dumps(policy))
 profile,rubric_hash=audio.rubric_identity();params={'synthetic_only':1}
 payload=dict(schema='audio_rubric_projection_v1',receipt_id='public-math-scope',child_id=CHILD,status='current',frozen_at=now.isoformat(),projection_version='NO_ALGORITHM_INSTALLED',deployment_sha256='a'*64,asset_hashes_sha256='b'*64,input_schema='pet_multipa_reference_input_v1',raw_adapter='multipa_reference_short_v1',task_family='read_aloud',length_lane='short',rubric_profile=profile,rubric_version=profile,rubric_hash=rubric_hash,dimensions=['fluency'],adapter_id='UNAVAILABLE',adapter_code_sha256='c'*64,parameters=params,parameters_sha256=digest(params))
 signed=sign('audio_projection',payload)
 with db.connect()as c:
  c.execute('BEGIN IMMEDIATE');receipt=authority.import_receipt(c,child_id=CHILD,**signed);c.commit()
 return db,trust,signed,receipt,now

def context(c):return getattr(audio,'_signature_math_scope',lambda c:nullcontext())(c)
def read(c,now):return audio.artifact(c,CHILD,'audio_projection','public-math-scope',now)

def test_same_operation_exact_signature_math_only_once_and_cleanup(original,monkeypatch):
 db,_,_,_,now=original;calls=[];verify=authority._verify_signature
 def spy(*args):calls.append(args);return verify(*args)
 monkeypatch.setattr(authority,'_verify_signature',spy)
 with db.connect()as c:
  c.execute('BEGIN')
  with install_read_scope(c),context(c):
   first=read(c,now)
   with context(c):assert read(c,now)==first
   assert read(c,now)==first
   assert len(calls)==1
   checks=_READ_SCOPE.get()['_audio_signature_math'];assert checks.checked
  assert checks.checked==set()and checks.size==0
  with install_read_scope(c),context(c):assert read(c,now)==first
  assert len(calls)==2
 assert _READ_SCOPE.get()is None

@pytest.mark.parametrize('mutation',['revoke','quarantine','issuer_expiry','issuer_child','issuer_kind','issuer_key'])
def test_authorization_is_fresh_even_when_exact_math_is_warm(original,mutation):
 db,trust,signed,receipt,now=original
 with db.connect()as c:
  c.execute('BEGIN IMMEDIATE')
  with install_read_scope(c),context(c):
   read(c,now)
   if mutation=='revoke':authority.revoke(c,child_id=CHILD,kind='audio_projection',receipt_id='public-math-scope',expected_hash=receipt['envelope_hash'],client_action_id='withdraw-math')
   elif mutation=='quarantine':authority.quarantine_after_restore(c)
   else:
    policy=json.loads(trust.read_text());issuer=policy['issuers'][0]
    if mutation=='issuer_expiry':issuer['expires_at']=(now-timedelta(seconds=1)).isoformat()
    elif mutation=='issuer_child':issuer['child_ids']=['other-child']
    elif mutation=='issuer_kind':issuer['kinds']=['holdout']
    else:issuer['public_key_pem']=Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    trust.write_text(json.dumps(policy))
   with pytest.raises(ValueError):read(c,now)

def test_exact_message_expiry_fake_cache_and_scope_identity_are_not_waived(original):
 db,_,signed,_,now=original
 with db.connect()as c:
  c.execute('BEGIN')
  with install_read_scope(c),context(c):
   read(c,now);checks=_READ_SCOPE.get()['_audio_signature_math']
   wrong=copy.deepcopy(signed['envelope']);wrong['payload']['projection_version']='changed'
   with pytest.raises(ValueError,match='SIGNATURE_INVALID'):authority.verify(CHILD,wrong,signed['signature'],_signatures=checks)
   with pytest.raises(ValueError,match='AUTHORITY_EXPIRED'):authority.verify(CHILD,signed['envelope'],signed['signature'],now=now+timedelta(days=1,seconds=1),_signatures=checks)
   class Fake:
    def verify(self,*args):return None
   with pytest.raises(ValueError,match='AUTHORITY_SIGNATURE_SCOPE_INVALID'):authority.resolve(c,child_id=CHILD,kind='audio_projection',reference_id='public-math-scope',_signatures=Fake())
   with db.connect()as other:
    other.execute('BEGIN')
    with pytest.raises(ValueError,match='AUDIO_SIGNATURE_SCOPE_CHANGED'):
     with audio._signature_math_scope(other):pass

def test_baseexception_clears_math_and_original_scope(original):
 db,_,_,_,now=original;error=KeyboardInterrupt('synthetic-stop')
 with db.connect()as c:
  c.execute('BEGIN')
  with pytest.raises(KeyboardInterrupt)as observed:
   with install_read_scope(c),context(c):
    read(c,now);checks=_READ_SCOPE.get()['_audio_signature_math'];raise error
  assert observed.value is error and checks.checked==set()and checks.size==0 and _READ_SCOPE.get()is None
