"""Synthetic original HTTP/SQLite + original private host issuer; no model/Windows qualification."""
from dataclasses import replace
from pathlib import Path
import hashlib,json,sqlite3
import pytest
from fastapi.testclient import TestClient
from home_study_agent.database import Database
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs,_Claim
from home_study_agent.pet_speaking_candidate_store import SpeakingModelCandidateStore
from home_study_agent.pet_multipa_offline import SpeakingOfflineCandidateAssessor
from home_study_agent import pet_multipa_epoch as epoch,pet_multipa_process as process,pet_multipa_execution_receipt as receipt
from home_study_agent.app import create_app
from test_pet_multipa_completion import public_host
from test_pet_multipa_reference_worker import make_read_aloud,ASSETS
from test_rubric_calibration_original_audio import original_cpu_candidate
from test_pet_oral_supply_http import CHILD,OTHER

@pytest.fixture
def vertical(tmp_path,monkeypatch,public_host):
    from home_study_agent import pet_speaking_candidate_jobs as jobs_module
    approved=public_host.approved;approved['asset_hashes']=dict(ASSETS)
    original=jobs_module.register_new_feedback_intent
    def register(c,*a):
        epoch._REVIEWED_DEPLOYMENTS['REFERENCE_TEST_ONLY']=approved
        return original(c,*a)
    monkeypatch.setattr(jobs_module,'register_new_feedback_intent',register)
    v=make_read_aloud(tmp_path,monkeypatch,configured=True);settings=v['settings']
    db=Database(settings.database_path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    assessor=SpeakingOfflineCandidateAssessor(db,media_root=settings.data_dir,deployment_key='REFERENCE_TEST_ONLY',data_encryption_key=settings.data_encryption_key)
    runtime=AgentRuntimeStore(db,speaking_candidate_assessor=assessor)
    jobs=SpeakingCandidateJobs(db,runtime,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    opens=[]
    class ReaderLease:
        def __init__(self,key):assert key=='REFERENCE_TEST_ONLY';self.approved=epoch.configured(key);assert self.approved is approved;self.doc={'epoch':approved['epoch']}
        def __enter__(self):
            self.closed=False;opens.append('open');self.authority=public_host.leases[-1].authority;return self
        def __exit__(self,*a):self.closed=True;opens.append('close')
        def _completion_release_confirmed(self):return self.closed
        def check(self):assert epoch._REVIEWED_DEPLOYMENTS.get('REFERENCE_TEST_ONLY')is approved
    monkeypatch.setattr(epoch,'_AttestedWindowsEpochLease',ReaderLease)
    v.update(db=db,assessor=assessor,jobs=jobs,host=public_host,opens=opens)
    try:yield v
    finally:epoch._REVIEWED_DEPLOYMENTS.clear()

def saved(v):
    value=v['jobs'].process_one();assert value['state']=='completed',value
    with v['db'].connect()as c:
        row=dict(c.execute('SELECT *FROM '+receipt.TABLE).fetchone());candidate=dict(c.execute('SELECT *FROM pet_speaking_model_candidate').fetchone());value=dict(c.execute('SELECT *FROM pet_speaking_candidate_job WHERE job_id=?',(value['job_id'],)).fetchone())
    return value,row,candidate

def current(v,row,child=CHILD):
    with v['db'].connect()as c:
        c.execute('BEGIN');return receipt.read_current(c,child,candidate_id=row['candidate_id'],job_id=row['job_id'])

@pytest.mark.parametrize('mode',['success','rollback'])
def test_actual_private_host_record_reader_cas_rollback_and_prior_no_backfill(vertical,monkeypatch,mode):
    v=vertical
    if mode=='rollback':
        from home_study_agent import pet_speaking_candidate_jobs as jobs_module
        def fail(*a):raise ValueError('PUBLIC_COMMIT_FAULT')
        monkeypatch.setattr(jobs_module,'complete_claim',fail)
        result=v['jobs'].process_one();assert result['state']=='blocked'
        with v['db'].connect()as c:
            for table in('pet_speaking_model_candidate','pet_speaking_model_candidate_event',receipt.TABLE):assert c.execute('SELECT COUNT(*)FROM '+table).fetchone()[0]==0
        assert process._completion._PENDING is None and len(v['host'].created)==1
        assert v['jobs'].process_one()is None and len(v['host'].created)==1
        return
    done,row,candidate=saved(v)
    assert len(v['host'].created)==1 and v['host'].created[0].returncode==0
    assert process._completion._PENDING is None
    assert current(v,row)['held_execution_verified']is True
    assert current(v,row,OTHER)['held_execution_verified']is False
    assert current(v,{**row,'job_id':'wrong-job'})['held_execution_verified']is False
    payload=json.loads(row['payload_json']);assert set(payload)==receipt.FACTS
    assert payload['score_qualified']is payload['mastery_claim_allowed']is payload['certification_claim_allowed']is False
    text=json.dumps(row,ensure_ascii=False);assert v['original']['stimulus_text']not in text and 'Public'not in text and str(v['settings'].data_dir)not in text
    opens_before=len(v['opens'])
    with v['db'].connect()as c:
        c.execute('BEGIN')
        with receipt.install_read_scope(c):
            assert receipt.read_current(c,CHILD,candidate_id=row['candidate_id'],job_id=row['job_id'])['held_execution_verified']
            assert receipt.read_current(c,CHILD,candidate_id=row['candidate_id'],job_id=row['job_id'])['held_execution_verified']
    assert v['opens'][opens_before:]==['open','close']
    # A saved numeric mutation cannot inherit the historical full-result attestation.
    with v['db'].connect()as c:
        c.execute('BEGIN');c.execute('DROP TRIGGER pet_speaking_model_candidate_no_update')
        c.execute("UPDATE pet_speaking_model_candidate SET raw_numeric_json='{}'WHERE candidate_id=?",(row['candidate_id'],))
        assert not receipt.read_current(c,CHILD,candidate_id=row['candidate_id'],job_id=row['job_id'])['held_execution_verified'];c.rollback()
    assert current(v,row)['held_execution_verified']is True
    approved=epoch._REVIEWED_DEPLOYMENTS.pop('REFERENCE_TEST_ONLY')
    assert not current(v,row)['held_execution_verified']
    epoch._REVIEWED_DEPLOYMENTS['REFERENCE_TEST_ONLY']=approved
    # Prior completion returns the original lifecycle and performs no new child work.
    assert v['assessor'].assess(CHILD,v['sid'],media_id=done['media_id'],assessment_id=done['assessment_id'],client_action_id=candidate and done['client_action_id'])['current']['candidate_id']==row['candidate_id']
    assert len(v['host'].created)==1
    with v['db'].connect()as c:
        with pytest.raises(sqlite3.IntegrityError):c.execute('UPDATE '+receipt.TABLE+" SET payload_json='{}'")
    # Raw legacy recorded() remains legal and cannot accept caller completion JSON.
    with v['db'].connect()as c:binding=json.loads(candidate['binding_json'])
    with pytest.raises((ValueError,AttributeError)):
        v['assessor'].store.record(CHILD,v['sid'],binding=binding,result={},asset_hashes=ASSETS,client_action_id='fake',job_claim=_Claim({'job_id':done['job_id'],'owner_token':'fake','owner_epoch':'fake'}),attested={},invocation={})
    # Withdraw destroys current authority; the immutable historical receipt remains.
    v['assessor'].store.withdraw(CHILD,row['candidate_id'],expected_revision=1,client_action_id='withdraw-receipt')
    assert current(v,row)['held_execution_verified']is False
    with v['db'].connect()as c:assert c.execute('SELECT COUNT(*)FROM '+receipt.TABLE).fetchone()[0]==1

def test_real91_upgrade_strict92_missing_unknown_and_snapshot_rollback(original_cpu_candidate,monkeypatch):
    f=original_cpu_candidate;db=f['db']
    with db.connect()as c:
        assert c.execute('SELECT COUNT(*)FROM '+receipt.TABLE).fetchone()[0]==0
        before={t:[tuple(x)for x in c.execute('SELECT *FROM '+t)]for t in('pet_speaking_candidate_job','pet_speaking_model_candidate','pet_speaking_model_candidate_event')}
        for kind,name,_ in reversed(receipt.OBJECTS):c.execute('DROP '+kind.upper()+' '+name)
        c.execute('DELETE FROM schema_migration WHERE version>=92');c.commit();assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==91
    original=db._initialize_schema
    def fail_after_original():original();raise RuntimeError('PUBLIC_POST92_ROLLBACK')
    monkeypatch.setattr(db,'_initialize_schema',fail_after_original)
    with pytest.raises(RuntimeError,match='PUBLIC_POST92_ROLLBACK'):db.initialize()
    with db.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==91
        assert {t:[tuple(x)for x in c.execute('SELECT *FROM '+t)]for t in before}==before
        assert not c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(receipt.TABLE,)).fetchone()
        c.execute('CREATE TABLE '+receipt.TABLE+'(fake TEXT)');c.commit()
    monkeypatch.setattr(db,'_initialize_schema',original)
    with pytest.raises(ValueError,match='OLD_SOURCE_COLLISION'):db.initialize()
    with db.connect()as c:c.execute('DROP TABLE '+receipt.TABLE);c.commit()
    db.initialize()
    with db.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==92
        assert {t:[tuple(x)for x in c.execute('SELECT *FROM '+t)]for t in before}==before
        assert not c.execute('SELECT 1 FROM '+receipt.TABLE).fetchone() # no historical backfill
        c.execute('DROP TRIGGER '+receipt.TABLE+'_immutable');c.commit()
    from home_study_agent.restore_publication import RestorePublicationError
    with pytest.raises(RestorePublicationError,match='RESTORE_ASSET_OWNERSHIP_UNCONFIRMED'):db.initialize()
    with db.connect()as c:assert not c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(receipt.TABLE+'_immutable',)).fetchone()

def test_signed_restore_quarantine_reopen_and_child_privacy_order(vertical):
    from synthetic_native_restore_fixture import KEY,signed_local_restore_post
    from test_period_rebinding_backup_restore import restore_operation
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    v=vertical;done,row,candidate=saved(v);settings=replace(v['settings'],native_launch_attestation_key=KEY)
    with TestClient(create_app(settings,model_gateway=v['gateway']),headers={'X-HSC-Session':settings.session_token})as client:
        client.headers['X-HSC-Parent']=client.post('/v1/parent/unlock',json={'pin':'7391'}).json()['parent_token']
        for old,new in(('check_in','classroom_recall'),('classroom_recall','homework_capture'),('homework_capture','plan_review'),('plan_review','study'),('study','reflection')):
            if old=='classroom_recall':assert client.post(f"/v1/sessions/{v['sid']}/recalls/none",json={'client_action_id':'receipt-no-recall'}).status_code==200
            if old=='homework_capture':assert client.post(f"/v1/sessions/{v['sid']}/homework/none",json={'client_action_id':'receipt-no-homework'}).status_code==200
            result=client.post(f"/v1/sessions/{v['sid']}/transition",json={'expected_state':old,'target_state':new,'reason':'Public synthetic restore fixture','client_action_id':'receipt-close-'+new});assert result.status_code==200,result.text
        result=client.post(f"/v1/sessions/{v['sid']}/reflection/complete",json={'learned_text':'Synthetic receipt persistence only','difficult_text':'','feeling':'okay','next_action':'','skipped':False,'client_action_id':'receipt-close'});assert result.status_code==200,result.text
        backup=client.post('/v1/parent/backups');assert backup.status_code==200,backup.text
        result=signed_local_restore_post(client,json={'backup_id':backup.json()['backup_id'],'confirmation':'确认恢复','operation':restore_operation(client,{})});assert result.status_code==200,result.text
    reopened=Database(settings.database_path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key);reopened.initialize()
    with reopened.connect()as c:
        c.execute('BEGIN');after=dict(c.execute('SELECT *FROM '+receipt.TABLE).fetchone());assert after==row
        events=c.execute('SELECT *FROM '+receipt.EVENT).fetchall();assert len(events)==1 and events[0]['operation']=='restore_quarantine'and events[0]['receipt_sha256']==receipt.digest(row)
        receipt.quarantine_after_restore(c,events[0]['restore_id']);assert c.execute('SELECT COUNT(*)FROM '+receipt.EVENT).fetchone()[0]==1
        assert not receipt.read_current(c,CHILD,candidate_id=row['candidate_id'],job_id=row['job_id'])['held_execution_verified']
        assert not c.execute("SELECT 1 FROM pet_speaking_candidate_job WHERE state IN('queued','claimed')").fetchone();c.commit()
    assert len(v['host'].created)==1
    service=PrivacyLifecycleService(reopened,settings.data_dir,settings.data_encryption_key);service.initialize()
    with reopened.connect()as c:other=[tuple(x)for x in c.execute('SELECT *FROM child_profile WHERE child_id=?',(OTHER,))]
    service.delete_child_scope(CHILD,'learning','确认删除','receipt-child-privacy')
    with reopened.connect()as c:
        assert not c.execute('SELECT 1 FROM '+receipt.TABLE+' WHERE child_id=?',(CHILD,)).fetchone()
        assert not c.execute('SELECT 1 FROM '+receipt.EVENT+' WHERE child_id=?',(CHILD,)).fetchone()
        assert [tuple(x)for x in c.execute('SELECT *FROM child_profile WHERE child_id=?',(OTHER,))]==other
    assert receipt.SCOPES=={'learning','media','tutor','knowledge','all_history'}


def test_original_audio_protocol_uses_current_receipt_and_stays_unqualified(vertical):
    from home_study_agent.rubric_calibration import RubricCalibrationService
    v=vertical;done,row,candidate=saved(v)
    with v['db'].connect()as c:
        subject=c.execute('SELECT k.subject FROM diagnostic_item i JOIN knowledge_node k USING(knowledge_node_id)WHERE i.diagnostic_item_id=?',(v['item_id'],)).fetchone()[0]
        grade=c.execute('SELECT grade FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]
    service=RubricCalibrationService(v['db']);service.initialize();before=len(v['opens'])
    selector=dict(modality='original_audio',assessment_id=done['assessment_id'],media_id=done['media_id'],candidate_id=row['candidate_id'],job_id=row['job_id'])
    answer=service.freeze(CHILD,scorer=selector,scope=dict(subject=subject,grade=grade,task_family='read_aloud',strata=['public']),holdout_receipt_id='public-unverified',client_action_id='public-cpu-protocol')
    assert answer['current']['source']['held_execution_verified']is True
    assert answer['current']['status']=='pending_unqualified'and answer['current']['score_qualified']is False
    assert 'original_audio_projection_missing'in answer['current']['blockers']and 'held_execution_attestation_missing'not in answer['current']['blockers']
    assert answer['current']['mastery_claim_allowed']is answer['current']['certification_claim_allowed']is False
    assert v['opens'][before:]==['open','close']
    encoded=json.dumps(answer,ensure_ascii=False);assert v['original']['stimulus_text']not in encoded and 'raw_numeric'not in answer['current']['source']


@pytest.fixture
def read_close_ports(monkeypatch):
    # Original WindowsEpochLease __exit__ and original _Win32.close; native BOOL
    # only is synthetic. Attested variant reuses the exact existing ledger.
    from types import SimpleNamespace,MethodType
    from home_study_agent.pet_media_files import _Win32
    from home_study_agent import pet_multipa_epoch as e
    calls=[];created=[];mode={'value':'false'}
    authority={n:('public-'+n).encode()for n in e.AUTHORITIES};approved={'asset_hashes':{},'epoch':'public-read-epoch'}
    facts={'installation_epoch_sha256':hashlib.sha256(approved['epoch'].encode()).hexdigest(),'authority_set_sha256':receipt.digest({n:hashlib.sha256(v).hexdigest()for n,v in authority.items()}),'deployment_manifest_sha256':hashlib.sha256(authority['deployment.json']).hexdigest(),'deployment_sha256':receipt.digest(approved)}
    class DLL:
        def CloseHandle(self,h):
            calls.append(h)
            if mode['value']=='throw':raise OSError('PUBLIC_NATIVE_CLOSE_UNKNOWN')
            return mode['value']=='healthy'
    class Legacy(e.WindowsEpochLease):
        def __enter__(self):
            self.approved=approved;self.doc={'epoch':approved['epoch']};self.authority=authority
            self.k=SimpleNamespace(dll=DLL());self.k.close=MethodType(_Win32.close,self.k)
            self.stack.callback(self.k.close,41);created.append(self);return self
    class Attested(e._AttestedWindowsEpochLease):
        def __enter__(self):
            self.approved=approved;self.doc={'epoch':approved['epoch']};self.authority=authority
            self.k=SimpleNamespace(dll=DLL());self.stack.callback(self.k.close,41);created.append(self);return self
    monkeypatch.setattr(e,'configured',lambda key:approved)
    monkeypatch.setattr(e,'WindowsEpochLease',Legacy);monkeypatch.setattr(e,'_AttestedWindowsEpochLease',Attested)
    if hasattr(receipt,'_READ_HOLDS'):monkeypatch.setattr(receipt,'_READ_HOLDS',[],raising=False) # synthetic test isolation only; no production reset API
    return SimpleNamespace(calls=calls,created=created,mode=mode,approved=approved,facts=facts)

@pytest.mark.parametrize('fault',['false','throw'])
def test_read_scope_unknown_native_close_keeps_popped_owner_and_future_reads_closed(read_close_ports,fault):
    import gc,weakref
    v=read_close_ports;v.mode['value']=fault
    with pytest.raises(ValueError,match='READ_RELEASE_UNCONFIRMED'):
        with receipt.install_read_scope(object()):
            receipt._held_current('PUBLIC',v.facts,{})
            receipt._held_current('PUBLIC',v.facts,{})
    assert v.calls==[41]and len(v.created)==1
    owner=v.created.pop();w=weakref.ref(owner);assert not owner.stack._exit_callbacks and owner._completion_unknown
    del owner;gc.collect();assert w()is not None and receipt._READ_HOLDS[0]['owners']==(w(),)
    assert set(receipt._READ_HOLDS[0])=={'owners','stack'} # no SQLite c/audio/source/rows kept
    assert receipt._READ_SCOPE.get()is None
    for _ in range(2):
        with receipt.install_read_scope(object()):
            with pytest.raises(ValueError,match='READ_RELEASE_UNCONFIRMED'):receipt._held_current('PUBLIC',v.facts,{})
    assert v.calls==[41]and not v.created # no reclose/reopen/retry of recycled numeric handle

def test_read_scope_healthy_one_scope_one_lease_and_same_close_confirmed(read_close_ports):
    v=read_close_ports;v.mode['value']='healthy'
    with receipt.install_read_scope(object()):
        receipt._held_current('PUBLIC',v.facts,{})
        receipt._held_current('PUBLIC',v.facts,{})
    assert v.calls==[41]and len(v.created)==1 and v.created[0]._completion_release_confirmed()
    assert not receipt._READ_HOLDS and receipt._READ_SCOPE.get()is None

@pytest.mark.parametrize('closed',[False,True])
def test_read_scope_original_enter_failure_keeps_unknown_ledger_not_empty_callbacks(monkeypatch,closed):
    import ctypes
    from types import SimpleNamespace,MethodType
    from home_study_agent import pet_multipa_epoch as e,pet_media_files as media
    calls=[];created=[];Original=e._AttestedWindowsEpochLease;original_close=media._Win32.close
    class DLL:
        def CreateFileW(self,*a):return 51
        def CloseHandle(self,h):calls.append(h);return h==7 or closed
    class Kernel:
        def __init__(self):self.ct=ctypes;self.dll=DLL();self.close=MethodType(original_close,self)
        def drive_type(self,*a):return 3
        def info(self,*a):return {'identity':(1,1),'file_type':1,'attributes':16,'links':1,'size':0}
        def final_path(self,*a):return '\\\\?\\C:\\'
    class Security:
        current='public-actor'
        def __init__(self,k):k.close(7)
        def known(self,*a):return self.current
        def check(self,*a):raise ValueError('PUBLIC_ORIGINAL_SOURCE_CHECK_REQUIRED')
    class Tracked(Original):
        def __init__(self,key):super().__init__(key);created.append(self)
    monkeypatch.setattr(e,'os',SimpleNamespace(name='nt'));monkeypatch.setattr(e,'_Security',Security)
    monkeypatch.setattr(e,'configured',lambda key:{'root':'C:\\','actor_sid':'public','root_identity':[1,1],'root_security_sha256':'public'})
    monkeypatch.setattr(media,'_windows_parts',lambda root:['C:\\']);monkeypatch.setattr(media,'_Win32',Kernel)
    monkeypatch.setattr(e,'_AttestedWindowsEpochLease',Tracked);monkeypatch.setattr(receipt,'_READ_HOLDS',[],raising=False)
    with pytest.raises(ValueError,match='PUBLIC_ORIGINAL_SOURCE_CHECK_REQUIRED'if closed else 'READ_RELEASE_UNCONFIRMED'):
        with receipt.install_read_scope(object()):receipt._held_current('PUBLIC',{}, {})
    assert calls==[7,51]and len(created)==1 and not created[0].stack._exit_callbacks and receipt._READ_SCOPE.get()is None
    assert bool(receipt._READ_HOLDS)is (not closed)
    if not closed:assert receipt._READ_HOLDS[0]['owners']==(created[0],)and created[0]._completion_unknown
