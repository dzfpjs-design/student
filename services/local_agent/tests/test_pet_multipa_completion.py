"""Real public child/pipes, synthetic epoch/Job/clock ports; NOT Windows proof."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import hashlib
import io
import json
import pickle
import subprocess
import sys
import threading
import time
import wave

import pytest

from home_study_agent import pet_multipa_epoch as epoch
from home_study_agent import pet_multipa_process as p
from home_study_agent.pet_speaking_candidate_jobs import _Claim


@pytest.fixture
def public_host(tmp_path, monkeypatch):
    events=[];created=[];fault={'value':None};closes=[];leases=[];jobs=[]
    root=tmp_path/'public-owned-worker';root.mkdir()
    script=root/'public-child.py'
    script.write_text('''import sys,json,struct,hashlib,time,os
n=struct.unpack('!I',sys.stdin.buffer.read(4))[0]
h=json.loads(sys.stdin.buffer.read(n));audio=sys.stdin.buffer.read(h['audio_bytes'])
assert len(audio)==h['audio_bytes'] and hashlib.sha256(audio).hexdigest()==h['audio_sha256']
mode=MODE
if mode=='hang':time.sleep(5)
if mode=='nonzero':sys.exit(73)
if mode=='malformed':sys.stdout.buffer.write(b'PUBLIC-NON-JSON');sys.exit(0)
if mode=='private_error':
 print('PUBLIC_REFERENCE_NOT_TO_PARENT_LOG');print('PUBLIC_TRANSCRIPT_NOT_TO_PARENT_LOG',file=sys.stderr);sys.exit(74)
if 'reference' in h:
 r=h['reference'];frames=(len(audio)-44)//2
 result=dict(schema='pet_multipa_reference_raw_v1',adapter='multipa_reference_short_v1',audio_sha256=h['audio_sha256'],reference_sha256=r['text_sha256'],reference_utf8_bytes=r['text_utf8_bytes'],frames=frames,sentence={k:1.0 for k in ('uttr_acc','uttr_fluency','uttr_prosodic','uttr_total')},words={k:[1.0]for k in ('word_acc','word_stress','word_total')},word_frames=[[0,frames]])
 schema='multipa_reference_worker_v1'
else:
 result={k:1.0 for k in ('uttr_acc','uttr_fluency','uttr_prosodic','uttr_total')}
 result.update({k:[1.0]for k in ('word_acc','word_stress','word_total')})
 result.update(word_text=['Public'],transcript_S='Public',transcript_W='Public');schema='multipa_worker_v2'
print(json.dumps(dict(schema=schema,nonce=h['nonce'],status='completed',result=result)),end='')
'''.replace('mode=MODE',"mode='valid'"))
    authorities={n:('PUBLIC-NOT-A-WINDOWS-AUTHORITY:'+n).encode()for n in epoch.AUTHORITIES}
    approved=dict(timeout_seconds=1,memory_bytes=268435456,private_work_root=str(root),actor_sid='PUBLIC_SYNTHETIC',
        epoch='SYNTHETIC_NOT_WINDOWS',authority_sha256={n:hashlib.sha256(v).hexdigest()for n,v in authorities.items()},
        asset_hashes={n:hashlib.sha256(n.encode()).hexdigest()for n in epoch.GROUPS})
    approved['reference_adapter']=dict(version='multipa_reference_short_v1',deployment_sha256=approved['authority_sha256']['deployment.json'])
    out=io.BytesIO()
    with wave.open(out,'wb')as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(b'\0\0'*1600)
    content=out.getvalue()
    binding=dict(schema='pet_multipa_input_v1',child_id='public-child-A',session_id='public-session-A',
        media_id='public-media-A',assessment_id='public-assessment-A',item_version='public-version-A',
        task_family='spoken_response',audio=dict(sha256=hashlib.sha256(content).hexdigest(),frames=1600,sample_rate=16000))
    claim=_Claim(dict(job_id='public-job-A',owner_token='private-public-fixture-token',owner_epoch='SCHEDULER_NOT_INSTALLATION'))

    class Native:
        def CloseHandle(self,h):
            closes.append(h);events.append('same-close-bool')
            value=getattr(h,'value',h)
            if fault['value']=='token-throw'and value==7:raise OSError('PUBLIC_NATIVE_CLOSE_UNKNOWN')
            return not(fault['value']=='lease-close'and value==9 or fault['value']=='token-close'and value==7)

    class LegacyLease:
        def __init__(self,key):self.key=key;self.approved=approved;self.doc={'epoch':approved['epoch']};leases.append(self)
        def __enter__(self):events.append('lease-enter');return self
        def __exit__(self,*a):self.check();events.append('lease-exit')
        def path(self,role):return Path(sys.executable)if role=='python_executable'else script
        def check(self):
            events.append('lease-check')
            if fault['value']=='lease-check':raise ValueError('PUBLIC_LEASE_CHANGED')

    Base=getattr(epoch,'_AttestedWindowsEpochLease',object)
    class AttestedLease(Base):
        def __init__(self,key):super().__init__(key);leases.append(self)
        def __enter__(self):
            events.append('lease-enter');self.approved=approved;self.doc={'epoch':approved['epoch']};self.authority=authorities
            self.k=SimpleNamespace(dll=Native())
            if fault['value']in {'token-close','token-throw'}:
                # Execute the ORIGINAL actor/finally, with only native Windows
                # ports synthetic. Its token close fails before any lease _open.
                import ctypes
                class C:
                    get_last_error=staticmethod(lambda:122)
                    def __getattr__(self,n):return getattr(ctypes,n)
                class A:
                    def OpenProcessToken(self,process,access,out):ctypes.cast(out,ctypes.POINTER(ctypes.c_void_p))[0]=7;return 1
                    def GetTokenInformation(self,token,kind,buf,size,out):
                        if kind==20:return 1
                        ctypes.cast(out,ctypes.POINTER(ctypes.c_uint32))[0]=8
                        return int(buf is not None)
                    def CheckTokenMembership(self,*args):return 1
                security=object.__new__(epoch._Security);security.c=C();security.k=self.k;security.a=A();security.admin=b'PUBLICSID'
                security.sid=lambda pointer:b'PUBLICSID';self.k.dll.GetCurrentProcess=lambda:1
                epoch._Security.actor(security)
            else:self.k.close(7) # Original security-token port; later handle reuse is legal.
            # A later genuine new owner may reuse that same numeric value.
            for handle in (7,9):
                owner,close=self._completion_closer();self._completion_handles[owner].update(handle=handle,status='OPEN')
                self.stack.callback(close,handle)
            return self
        def __exit__(self,*a):
            try:return super().__exit__(*a)
            finally:events.append('lease-exit')
        def check(self):LegacyLease.check(self)
        def path(self,role):return LegacyLease.path(self,role)

    class Job:
        def __init__(self,memory):
            self.process=None;self.closed=False;jobs.append(self)
            if fault['value']=='job-init-close':
                self.close() # Original constructor can attempt its owned close.
                raise AssertionError('UNREACHABLE')
        def assign(self,process):self.process=process;events.append('job-assigned')
        def active(self):
            if fault['value']=='job-query':raise OSError('PUBLIC_JOB_UNKNOWN')
            return int(self.process.poll()is None)
        def terminate(self):
            if self.process.poll()is None:self.process.kill() # This exact public synthetic child only.
        def close(self):
            events.append('job-close')
            if fault['value']in {'job-close','job-init-close'}:raise OSError('PUBLIC_JOB_CLOSE_UNKNOWN')
            self.closed=True

    @contextmanager
    def scratch(*a,_completion_owners=None):
        # Same two-owner protocol; real OwnerDirectory constructor and checked
        # native BOOL are exercised separately below, not claimed here.
        owners=[SimpleNamespace(closed=False),SimpleNamespace(closed=False)]
        if _completion_owners is not None:_completion_owners.extend(owners)
        path=root/('multipa-'+uuid4().hex);path.mkdir();events.append('scratch-enter')
        try:yield path
        finally:
            for owner in owners:owner.closed=True
            events.append('scratch-exit')
    real_popen=subprocess.Popen
    class popen(real_popen):
        def __init__(self,*a,**kw):
            created.append(self);kw.pop('creationflags');super().__init__(*a,**kw);events.append('popen')
            if fault['value']=='process-init-close':
                # Exact public child naturally exits before the injected
                # unknown-handle-close exception. Never leave a live fixture.
                self.stdin.close();self.wait(timeout=2)
                raise OSError('PUBLIC_PROCESS_CONSTRUCTOR_CLOSE_UNKNOWN')
    monkeypatch.setattr(p,'configured',lambda key:approved)
    monkeypatch.setattr(p,'WindowsEpochLease',LegacyLease)
    monkeypatch.setattr(p,'_AttestedWindowsEpochLease',AttestedLease,raising=False)
    monkeypatch.setattr(p,'private_scope',scratch)
    monkeypatch.setattr(p,'_WindowsWorkerJob',Job)
    monkeypatch.setattr(p.subprocess,'Popen',popen)
    monkeypatch.setattr(p.subprocess,'CREATE_NO_WINDOW',0,raising=False)
    monkeypatch.setattr(p,'_WORKER_LOCK',threading.Lock())
    if hasattr(p,'_COMPLETION_HOLDS'):monkeypatch.setattr(p,'_COMPLETION_HOLDS',[])
    if hasattr(p,'_completion'):monkeypatch.setattr(p._completion,'_PENDING',None)
    value=SimpleNamespace(content=content,binding=binding,claim=claim,events=events,fault=fault,leases=leases,
        jobs=jobs,created=created,approved=approved,script=script,closes=closes)
    yield value
    retained=bool(getattr(p,'_COMPLETION_HOLDS',[]))
    assert all(obj.poll()is not None and (retained or obj.stdin.closed and obj.stdout.closed)for obj in created)
    assert epoch._REVIEWED_DEPLOYMENTS=={}


def invoke(v,**kwargs):
    invocation=p._begin_worker_completion(v.claim,v.binding)
    return invocation,p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content,**kwargs)


def consume(v,invocation,result,**kwargs):
    return p._consume_worker_completion(result,invocation=invocation,job_claim=kwargs.get('claim',v.claim),binding=kwargs.get('binding',v.binding))


def test_original_success_requires_distinct_private_completion(public_host):
    v=public_host
    if not hasattr(p,'_predict_in_worker_attested'):
        value=p.predict_in_worker('PUBLIC_REVIEWED_PORT_ONLY',v.content)
        assert v.created[0].returncode==0 and not p._WORKER_LOCK.locked()
        assert hasattr(value,'completion'),'ORIGINAL_RAW_SUCCESS_HAS_NO_OWNED_COMPLETION_CAPABILITY'
    invocation,result=invoke(v)
    assert result.numeric['uttr_total']==1.0 and v.created[0].returncode==0
    assert v.events[-1]=='lease-exit'and not p._WORKER_LOCK.locked()and v.jobs[0].closed
    receipt=consume(v,invocation,result)
    assert receipt['native_exit']==receipt['job_active']==0 and receipt['owned_releases_confirmed']
    assert not receipt['score_qualified']and not receipt['mastery_claim_allowed']and not receipt['certification_claim_allowed']
    assert receipt['installation_epoch_sha256']==hashlib.sha256(b'SYNTHETIC_NOT_WINDOWS').hexdigest()
    assert receipt['installation_epoch_sha256']!=hashlib.sha256(v.claim.epoch.encode()).hexdigest()
    encoded=json.dumps(receipt);assert len(encoded.encode())<=4096
    assert all(x not in encoded for x in ['Public',str(v.script.parent),v.claim.token,v.claim.epoch,v.claim.job_id])
    assert repr(result)==repr(result.completion)=='<private worker completion>'
    with pytest.raises(TypeError):pickle.dumps(result)
    with pytest.raises(AttributeError):result._numeric={}
    with pytest.raises(ValueError,match='COMPLETION_OWNERSHIP_REQUIRED'):consume(v,invocation,result)


def test_raw_api_preserves_numeric_return_and_never_mints(public_host):
    v=public_host;raw=p.predict_in_worker('PUBLIC_REVIEWED_PORT_ONLY',v.content)
    assert type(raw)is dict and raw['transcript_S']=='Public'and not p._WORKER_LOCK.locked()
    assert not hasattr(p,'_completion')or p._completion._PENDING is None


def test_reference_input_and_result_bind_original_hash(public_host):
    v=public_host
    from home_study_agent.pet_multipa_reference import text_input
    reference=text_input('Public reference')
    v.binding.update(schema='pet_multipa_reference_input_v1',task_family='read_aloud',reference={k:reference[k]for k in ('text_sha256','text_utf8_bytes')})
    invocation,result=invoke(v,reference=reference)
    receipt=consume(v,invocation,result)
    assert receipt['reference_sha256']==reference['text_sha256']and receipt['reference_utf8_bytes']==reference['text_utf8_bytes']
    assert 'Public reference'not in json.dumps(receipt)


@pytest.mark.parametrize('fault',['child','job','claim-copy','claim-mutation','numeric-mutation','numeric-substitution'])
def test_consumption_rejects_transfer_mutation_and_burns_matching_cap(public_host,fault):
    v=public_host;invocation,result=invoke(v);binding=dict(v.binding);claim=v.claim
    if fault=='child':binding['child_id']='public-child-B'
    elif fault=='job':claim=_Claim(dict(job_id='public-job-B',owner_token=v.claim.token,owner_epoch=v.claim.epoch))
    elif fault=='claim-copy':claim=_Claim(dict(job_id=v.claim.job_id,owner_token=v.claim.token,owner_epoch=v.claim.epoch))
    elif fault=='claim-mutation':v.claim.epoch='CHANGED_SCHEDULING_EPOCH'
    elif fault=='numeric-mutation':result.numeric['uttr_total']=2.0
    else:object.__setattr__(result,'_numeric',dict(result.numeric))
    with pytest.raises(ValueError,match='COMPLETION_'):consume(v,invocation,result,binding=binding,claim=claim)
    with pytest.raises(ValueError,match='COMPLETION_OWNERSHIP_REQUIRED'):consume(v,invocation,result)


def test_capacity_discard_fake_json_and_atomic_one_use(public_host):
    v=public_host;invocation=p._begin_worker_completion(v.claim,v.binding)
    with pytest.raises(ValueError,match='COMPLETION_BUSY'):p._begin_worker_completion(v.claim,v.binding)
    assert p._discard_worker_completion(invocation)is True
    invocation,result=invoke(v)
    with pytest.raises(ValueError,match='COMPLETION_CAPABILITY_REQUIRED'):consume(v,invocation,{'completion':True})
    outcomes=[]
    def reader():
        try:consume(v,invocation,result);outcomes.append('consumed')
        except ValueError:outcomes.append('rejected')
    threads=[threading.Thread(target=reader)for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(2)
    assert sorted(outcomes)==['consumed','rejected']


@pytest.mark.parametrize('mode',['nonzero','malformed','private_error','cancel','timeout'])
def test_nonpositive_child_or_original_guard_never_issues(public_host,mode):
    v=public_host
    if mode in {'nonzero','malformed','private_error','timeout'}:
        original=v.script.read_text();target_mode='hang'if mode=='timeout'else mode
        v.script.write_text(original.replace("mode='valid'",'mode='+repr(target_mode)))
    invocation=p._begin_worker_completion(v.claim,v.binding)
    stop=(lambda:bool(v.created))if mode=='cancel'else(lambda:False)
    with pytest.raises(ValueError)as error:p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content,should_stop=stop)
    assert 'PUBLIC_REFERENCE_NOT_TO_PARENT_LOG'not in str(error.value)
    assert 'PUBLIC_TRANSCRIPT_NOT_TO_PARENT_LOG'not in str(error.value)
    assert p._completion._PENDING is None and not p._WORKER_LOCK.locked()and not p._COMPLETION_HOLDS


@pytest.mark.parametrize('fault',['lease-close','job-close','job-query','lease-check'])
def test_unknown_release_retains_original_owner_and_both_slots_without_retry(public_host,fault):
    v=public_host;v.fault['value']=fault;invocation=p._begin_worker_completion(v.claim,v.binding)
    with pytest.raises(ValueError,match='CLEANUP_UNCONFIRMED'):p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content)
    assert p._WORKER_LOCK.locked()and len(p._COMPLETION_HOLDS)==1
    held=p._COMPLETION_HOLDS[0]
    assert held['process']is v.created[0]and held['job']is v.jobs[0]and held['lease']is v.leases[0]and held['worker_lock']is p._WORKER_LOCK
    assert p._discard_worker_completion(invocation)is False
    with pytest.raises(ValueError,match='COMPLETION_BUSY'):p._begin_worker_completion(v.claim,v.binding)
    with pytest.raises(ValueError,match='WORKER_BUSY'):p.predict_in_worker('PUBLIC_REVIEWED_PORT_ONLY',v.content)
    if fault=='lease-close':
        assert v.closes.count(9)==1 and v.leases[0]._completion_unknown
        assert any(row['handle']==9 and row['status']=='UNKNOWN'for row in v.leases[0]._completion_handles.values())


@pytest.mark.parametrize('fault',['token-close','token-throw'])
def test_original_security_token_failure_before_enter_retains_owner(public_host,fault):
    v=public_host;v.fault['value']=fault;invocation=p._begin_worker_completion(v.claim,v.binding)
    with pytest.raises(ValueError,match='CLEANUP_UNCONFIRMED'):
        p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content)
    assert not v.created and not v.jobs and len(v.closes)==1
    lease=v.leases[0];assert lease._completion_unknown and not lease.stack._exit_callbacks
    held=p._COMPLETION_HOLDS[0]
    assert held['lease']is lease and held['worker_lock']is p._WORKER_LOCK and p._WORKER_LOCK.locked()
    assert any(getattr(row['handle'],'value',row['handle'])==7 and row['status']=='UNKNOWN'for row in lease._completion_handles.values())
    assert p._discard_worker_completion(invocation)is False
    with pytest.raises(ValueError,match='COMPLETION_BUSY'):p._begin_worker_completion(v.claim,v.binding)


def test_single_close_callback_is_not_retried_and_numeric_owner_reuse_is_legal(public_host):
    v=public_host;invocation,result=invoke(v)
    lease=v.leases[0]
    assert v.closes.count(7)==2 and v.closes.count(9)==1 # Distinct token/file owners.
    assert lease._completion_release_confirmed() and not lease.stack._exit_callbacks
    consume(v,invocation,result)
    owner,close=lease._completion_closer();close(11)
    before=list(v.closes)
    with pytest.raises(ValueError,match='CLEANUP_UNCONFIRMED'):close(11)
    assert v.closes==before and lease._completion_handles[owner]['status']=='CLOSED'


def test_late_cancel_after_resources_released_never_seals(public_host):
    v=public_host;invocation=p._begin_worker_completion(v.claim,v.binding)
    with pytest.raises(ValueError,match='WORKER_CANCELLED'):
        p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content,
            should_stop=lambda:'lease-exit'in v.events)
    assert v.created[0].returncode==0 and v.jobs[0].closed and not p._WORKER_LOCK.locked()
    assert p._completion._PENDING is None and not p._COMPLETION_HOLDS


def test_discard_during_last_validation_cannot_race_seal(public_host,monkeypatch):
    v=public_host;invocation=p._begin_worker_completion(v.claim,v.binding)
    original=p._completion._digest;discarded=[]
    def tap(value):
        state=p._completion._PENDING
        if state and state['phase']=='RELEASED'and not discarded:
            discarded.append(p._discard_worker_completion(invocation))
        return original(value)
    monkeypatch.setattr(p._completion,'_digest',tap)
    with pytest.raises(ValueError,match='COMPLETION_SOURCE_CHANGED'):
        p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content)
    assert discarded==[False]and p._completion._PENDING is None and not p._WORKER_LOCK.locked()


def test_worker_lock_release_failure_never_issues_and_retains_owner(public_host,monkeypatch):
    v=public_host
    class Lock:
        def __init__(self):self.lock=threading.Lock()
        def acquire(self,**kwargs):return self.lock.acquire(**kwargs)
        def locked(self):return self.lock.locked()
        def release(self):raise RuntimeError('PUBLIC_LOCK_RELEASE_UNKNOWN')
    monkeypatch.setattr(p,'_WORKER_LOCK',Lock())
    invocation=p._begin_worker_completion(v.claim,v.binding)
    with pytest.raises(ValueError,match='CLEANUP_UNCONFIRMED'):
        p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content)
    assert v.created[0].returncode==0 and v.jobs[0].closed and v.leases[0]._completion_release_confirmed()
    assert p._WORKER_LOCK.locked()and p._completion._PENDING['phase']=='HOLD'
    assert p._COMPLETION_HOLDS[0]['worker_lock']is p._WORKER_LOCK
    assert p._discard_worker_completion(invocation)is False


def test_wrong_invocation_does_not_transfer_or_consume_original(public_host):
    v=public_host;invocation,result=invoke(v)
    with pytest.raises(ValueError,match='COMPLETION_OWNERSHIP_REQUIRED'):
        consume(v,object(),result)
    assert consume(v,invocation,result)['owned_releases_confirmed']
    assert p._discard_worker_completion(invocation)is False


def test_no_posix_or_json_route_can_create_held_epoch_completion(public_host,monkeypatch):
    if epoch.os.name!='nt':
        monkeypatch.setattr(epoch,'configured',lambda key:public_host.approved)
        # Original __enter__, not synthetic ports; no registry or Mac fallback.
        with pytest.raises(ValueError,match='WINDOWS_EPOCH_REQUIRED'):
            with epoch._AttestedWindowsEpochLease('PUBLIC_REVIEWED_PORT_ONLY'):pass


@pytest.mark.parametrize('fault',['job-init-close','process-init-close'])
def test_partial_original_constructor_is_held_without_close_retry(public_host,fault):
    v=public_host;v.fault['value']=fault;invocation=p._begin_worker_completion(v.claim,v.binding)
    with pytest.raises(ValueError,match='CLEANUP_UNCONFIRMED'):
        p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content)
    held=p._COMPLETION_HOLDS[0]
    assert held['job']is v.jobs[0]and held['lease']is v.leases[0]
    assert p._WORKER_LOCK.locked()and p._completion._PENDING['phase']=='HOLD'
    if fault=='job-init-close':
        assert not v.created and v.events.count('job-close')==1
    else:
        assert held['process']is v.created[0]and v.created[0].returncode is not None
        assert v.events.count('job-close')==0 # No retry/cleanup of partial owners.
    assert len(held['scope_owners'])==2


@pytest.mark.parametrize('construct,failed_close',[(True,True),(True,False),(False,True)])
def test_original_directory_constructor_and_popped_checked_close_owner(public_host,monkeypatch,tmp_path,construct,failed_close):
    v=public_host
    from home_study_agent.money_r4.money_files import OwnerDirectory
    from home_study_agent.registered_asset_snapshot import _CheckedWin32
    path=tmp_path/'public-original-directory';path.mkdir(mode=0o700)
    owners=[];calls=[]
    class DLL:
        def CloseHandle(self,h):calls.append(h);return not failed_close
    kernel=SimpleNamespace(dll=DLL())
    class Directory(OwnerDirectory):
        def check(self):
            super().check()
            # Only original directory.close/ExitStack controls callback order;
            # a native Windows BOOL is synthetic, no native claim on this host.
            self.stack.callback(_CheckedWin32.close,kernel,91)
            if construct:raise ValueError('PUBLIC_CONSTRUCTOR_CHECK_REJECTED')
    if construct:
        with pytest.raises(Exception):p._completion_directory(Directory,path,owners)
    else:
        owner=p._completion_directory(Directory,path,owners)
        with pytest.raises(Exception):owner.close()
    assert len(owners)==1 and calls==[91]and not owners[0].stack._exit_callbacks
    assert owners[0].closed is (not failed_close)
    assert owners[0].chain # Owner survives even though all callbacks popped.


def test_unknown_directory_close_is_held_after_original_stack_popped(public_host,monkeypatch):
    v=public_host;owners=[]
    @contextmanager
    def scope(*args,_completion_owners=None):
        parent=SimpleNamespace(closed=False);child=SimpleNamespace(closed=False)
        owners.extend([parent,child]);_completion_owners.extend(owners)
        path=Path(v.approved['private_work_root'])/('multipa-'+uuid4().hex);path.mkdir()
        try:yield path
        finally:
            parent.closed=True # child false means its original close failed.
            raise OSError('PUBLIC_SCOPE_NATIVE_CLOSE_UNKNOWN')
    monkeypatch.setattr(p,'private_scope',scope)
    invocation=p._begin_worker_completion(v.claim,v.binding)
    with pytest.raises(ValueError,match='CLEANUP_UNCONFIRMED'):
        p._predict_in_worker_attested(invocation,'PUBLIC_REVIEWED_PORT_ONLY',v.content)
    held=p._COMPLETION_HOLDS[0]
    assert held['scope_owners']==owners and not held['stack']._exit_callbacks
    assert held['scope_owners'][1].closed is False and p._WORKER_LOCK.locked()
    assert p._discard_worker_completion(invocation)is False


def test_real_prepare_input_reference_schema_and_exact_family_pairs(public_host,tmp_path,monkeypatch):
    # Original published source -> original encrypted capture/confirmation HTTP
    # -> completed evaluation/real job claim -> ORIGINAL prepare_input result.
    from test_pet_multipa_reference_worker import make_read_aloud,ASSETS
    from home_study_agent import pet_speaking_candidate_jobs as jobs_module
    from home_study_agent.database import Database
    from home_study_agent.agent_runtime import AgentRuntimeStore
    from home_study_agent.pet_multipa_candidate import prepare_input
    from home_study_agent.pet_multipa_reference import prepare_reference
    v=public_host;v.approved['asset_hashes']=dict(ASSETS)
    original=jobs_module.register_new_feedback_intent
    def register(c,*args):
        epoch._REVIEWED_DEPLOYMENTS['REFERENCE_TEST_ONLY']=v.approved
        return original(c,*args)
    monkeypatch.setattr(jobs_module,'register_new_feedback_intent',register)
    root=tmp_path/'public-original-read-aloud';root.mkdir()
    try:
        source=make_read_aloud(root,monkeypatch,configured=True);settings=source['settings']
        db=Database(source['db'].path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
        jobs=jobs_module.SpeakingCandidateJobs(db,AgentRuntimeStore(db),media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
        row=jobs._claim();assert row and row['state']=='claimed'
        with db.connect()as c:
            binding,content=prepare_input(c,source['sid'],media_id=row['media_id'],assessment_id=row['assessment_id'],media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
            reference=prepare_reference(c,binding)
        assert (binding['task_family'],binding['schema'])==('read_aloud','pet_multipa_reference_input_v1')
        v.binding=binding;v.content=content;v.claim=_Claim(row)
        invocation,result=invoke(v,reference=reference)
        assert consume(v,invocation,result)['source_binding_sha256']==p._completion._digest(binding)
        import copy
        for family,schema in [('read_aloud','pet_multipa_input_v1'),('spoken_response','pet_multipa_reference_input_v1'),('unknown','pet_multipa_input_v1'),('read_aloud','unknown'),('read_aloud',None),([],['schema'])]:
            bad=copy.deepcopy(binding);bad.update(task_family=family,schema=schema)
            with pytest.raises(ValueError,match='COMPLETION_BINDING_REQUIRED'):p._begin_worker_completion(v.claim,bad)
        assert p._completion._PENDING is None
        with db.connect()as c:
            # Host protocol alone creates no candidate/receipt/model score.
            assert not c.execute('SELECT 1 FROM pet_speaking_model_candidate').fetchone()
            assert not c.execute('SELECT 1 FROM model_call').fetchone()
    finally:epoch._REVIEWED_DEPLOYMENTS.clear()
