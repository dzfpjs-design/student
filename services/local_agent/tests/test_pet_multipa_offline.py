"""Fixed actual worker transport; synthetic learned dependencies, no weights."""
import ast,ctypes,hashlib,io,json,os,struct,subprocess,sys,time,wave
from contextlib import nullcontext,ExitStack
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from home_study_agent import pet_multipa_offline as offline,pet_multipa_epoch as epoch
from home_study_agent import pet_multipa_process as process,pet_multipa_protocol as protocol
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_pet_media_evidence import media,change,CHILD
from test_pet_speaking_review import setup
V=Path(offline.__file__).parent/'_multipa_cpu_vendor'
ASSETS={k:hashlib.sha256(('SYNTHETIC-NOT-A-MODEL-'+k).encode()).hexdigest()for k in epoch.GROUPS}
def api_fixture(on_compute=lambda:None):
    # Execute the exact original upstream predict_one_file function, not a
    # handwritten replacement. Tensor/ASR/learned forward values are synthetic.
    tree=ast.parse((V/'api.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='predict_one_file')
    calls={'audio':0,'asr':0,'forward':0}
    class Tensor:
        def __init__(self,data):self.data=np.asarray(data);self.shape=self.data.shape
        def cpu(self):return self
        def detach(self):return self
        def numpy(self):return self.data
    def load(path):
        calls['audio']+=1
        with wave.open(path,'rb')as w:
            data=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
            return Tensor(data.reshape(1,-1)),w.getframerate()
    def transcript(wav,model):calls['asr']+=1;return 'Synthetic words only'
    def infer(a,b,wav):
        calls['forward']+=1;on_compute()
        return *(Tensor([v])for v in (1.25,2.5,-0.5,1.0)),*(Tensor([[v]])for v in (0.5,0.25,0.75)),[(0,0.1,'SYNTHETIC-NOT-PERSISTED')]
    env=dict(torch=SimpleNamespace(no_grad=nullcontext,reshape=lambda x,s:Tensor(np.reshape(x.data,s))),
             torchaudio=SimpleNamespace(load=load),SAMPLE_RATE=16000,
             get_transcript=transcript,inference_one_seg=infer)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(V/'api.py'),'exec'),env)
    return SimpleNamespace(predict_one_file=env['predict_one_file']),calls


def test_default_blocks_no_loader_import_or_current_process_mutation(monkeypatch):
    original_path=list(sys.path);old_nltk=sys.modules.get('nltk');before=dict(os.environ)
    missing=object();original_engine=sys.modules.get('home_study_agent.pet_multipa_cpu_engine',missing)
    assert epoch._REVIEWED_DEPLOYMENTS=={}
    with pytest.raises(ValueError,match='DEPLOYMENT_UNCONFIGURED'):process.predict_in_worker('unknown',b'wave')
    assert list(sys.path)==original_path and sys.modules.get('nltk')is old_nltk and dict(os.environ)==before
    assert sys.modules.get('home_study_agent.pet_multipa_cpu_engine',missing)is original_engine

def sample():return dict(uttr_acc=.5,uttr_fluency=.2,uttr_prosodic=-.1,uttr_total=.3,word_acc=[.5],word_stress=[.4],word_total=[.1],word_text=['synthetic'],transcript_S='synthetic',transcript_W='synthetic')

def test_protocol_exact_bounded_not_fake_success():
    n='a'*32;b=protocol.encode(n,result=sample());assert protocol.decode(b,n,0)==sample()
    for data,nonce,code in[(b,'b'*32,0),(b,n,20),(b+'PRIVATE'.encode(),n,0),(b'{"unknown":"PRIVATE"}',n,0)]:
        with pytest.raises(ValueError):protocol.decode(data,nonce,code)
    bad=sample();bad['uttr_acc']=float('nan')
    with pytest.raises(ValueError):protocol.encode(n,result=bad)
    bad=sample();bad['word_text']=['s']*513
    with pytest.raises(ValueError):protocol.encode(n,result=bad)
    with pytest.raises(ValueError,match='WORKER_MODEL_REJECTED'):protocol.decode(protocol.encode(n,error='MODEL_REJECTED'),n,20)

@pytest.mark.parametrize('kind',['malformed','unconfigured'])
def test_real_fixed_worker_rejects_without_model_or_private_log(kind):
    script=Path(offline.__file__).with_name('pet_multipa_worker.py');n='a'*32;audio=b'PRIVATE_AUDIO_NOT_A_WAVE'
    h=dict(schema='multipa_worker_input_v2',nonce=n,key='unconfigured',approved={},epoch='not-an-install',audio_bytes=len(audio),audio_sha256=hashlib.sha256(audio).hexdigest())
    b=json.dumps(h).encode()if kind=='unconfigured'else b'PRIVATE_SECRET'
    p=subprocess.Popen([sys.executable,'-I','-S','-B',str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,close_fds=True)
    out,err=p.communicate(struct.pack('!I',len(b))+b+audio,timeout=5)
    assert p.returncode==20 and err==b''and b'PRIVATE'not in out and len(out)<1024
    doc=json.loads(out);assert doc['status']=='failed'and doc['error']in protocol.ERRORS

@pytest.mark.parametrize('reason',['cancel','timeout'])
def test_owned_transport_cancel_timeout_reaps_original_process(reason):
    script=Path(offline.__file__).with_name('pet_multipa_worker.py')
    p=subprocess.Popen([sys.executable,'-I','-S','-B',str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True)
    with pytest.raises(ValueError,match='CANCELLED'if reason=='cancel'else'TIMEOUT'):
        process._communicate_owned(p,b'',deadline=time.monotonic()-1 if reason=='timeout'else time.monotonic()+5,should_stop=lambda:reason=='cancel')
    assert p.poll()is not None and p.stdin.closed and p.stdout.closed

def test_install_acl_rejects_writer_owner_unprotected_broad_inheritance():
    actor=b'ACTOR';valid=[(0,0,0x1200a9,actor),(0,0,0x1f01ff,b'SYSTEM')]
    epoch.readonly_acl(b'SYSTEM',actor,True,valid)
    for owner,protected,aces in[(actor,True,valid),(b'SYSTEM',False,valid),(b'SYSTEM',True,valid+[(0,0,0x1200a9,b'OTHER')]),(b'SYSTEM',True,[(0,0,0x1f01ff,actor)]),(b'SYSTEM',True,[(0,8,0x1200a9,actor)])]:
        with pytest.raises(ValueError,match='EPOCH_ACL_REJECTED'):epoch.readonly_acl(owner,actor,protected,aces)

def test_held_open_denies_write_delete_alias_hardlink():
    calls=[];info=dict(file_type=1,attributes=0,links=1,size=3,identity=(1,2,3))
    def make():
        lease=object.__new__(epoch.WindowsEpochLease);lease.opened={};lease.seen_ids=set();lease.signatures={};lease.stack=ExitStack()
        def create(*args):calls.append(args);return 42
        lease.k=SimpleNamespace(ct=ctypes,dll=SimpleNamespace(CreateFileW=create),close=lambda h:None,info=lambda h:dict(info),final_path=lambda h:'\\\\?\\C:\\model\\asset')
        return lease
    lease=make();lease._open('C:\\model\\asset',False,3)
    assert calls[-1][1]==0x80020000 and calls[-1][2]==1 and calls[-1][5]&0x00200000 and calls[-1][3]is None
    lease.check();info['size']=4
    with pytest.raises(ValueError,match='EPOCH_FILE_CHANGED'):lease.check()
    info['size']=3;info['links']=2
    with pytest.raises(ValueError,match='EPOCH_FILE_CHANGED'):make()._open('C:\\model\\asset',False,3)
    info['links']=1;info['attributes']=0x400
    with pytest.raises(ValueError,match='EPOCH_ALIAS_REJECTED'):make()._open('C:\\model\\asset',False,3)

def test_source_worker_code_only_child_and_complete_fixed_protocol():
    assert not any(k in Path(offline.__file__).read_text()for k in('import nltk','import torch','MultiPAOfflineCPU','sys.path'))
    src=Path(process.__file__).read_text();assert 'job.assign(process)'in src and src.index('job.assign(process)')<src.index('data,exitcode=_communicate_owned')
    assert "'-I','-S','-B'"in src and 'stderr=subprocess.DEVNULL'in src and "cache=scratch/'cache'"in src
    w=Path(offline.__file__).with_name('pet_multipa_worker.py').read_text();assert w.index('os.dup2(null,1)')<w.index('WorkerCPUModel')
    assert "directory=work/('multipa-'+nonce)"in w and "HeldFile(directory,'original.wav',policy)"in w
    assert 'WindowsEpochLease(header' in w and "sys.path[:]="in w

def bridge(media,monkeypatch,on_compute=lambda:None):
    _,args=setup(media);api,calls=api_fixture(on_compute)
    monkeypatch.setattr(offline,'configured',lambda key:dict(asset_hashes=ASSETS))
    def predict(key,content,*,should_stop):
        assert not should_stop()
        # Original upstream predict_one_file + actual original WAV. Learned
        # Tensor/ASR/forward dependencies are synthetic, not a worker score.
        import tempfile
        with tempfile.TemporaryDirectory()as d:
            p=Path(d)/'original.wav';p.write_bytes(content)
            from home_study_agent.pet_multipa_cpu_engine import WorkerCPUModel
            engine=object.__new__(WorkerCPUModel);engine.api=api;engine.whisper_s=None;engine.whisper_w=None;engine.word_model=None;engine.lease=SimpleNamespace(check=lambda:None);engine.torch=SimpleNamespace(inference_mode=nullcontext)
            return engine.predict_original(p)
    monkeypatch.setattr(offline,'predict_in_worker',predict)
    return offline.SpeakingOfflineCandidateAssessor(media[0],media_root=media[4].parent,deployment_key='SYNTHETIC'),dict(media_id=args['media_id'],assessment_id=args['assessment_id'],client_action_id='r2-action'),calls

def test_original_api_real_store_reopen_withdraw_replay_no_recompute(media,monkeypatch):
    assessor,request,calls=bridge(media,monkeypatch);runtime=AgentRuntimeStore(media[0],speaking_candidate_assessor=assessor)
    value=runtime.assess_speaking_model_candidate(CHILD,media[2],**request);current=value['current'];identifier=current['candidate_id']
    assert current['status']=='current'and current['raw_numeric']['sentence']['uttr_prosodic']==-.5
    assert calls=={'audio':1,'asr':2,'forward':1}
    assessor.store.withdraw(CHILD,identifier,expected_revision=1,client_action_id='r2-withdraw')
    again=runtime.assess_speaking_model_candidate(CHILD,media[2],**request)
    assert again['current']['status']=='withdrawn'and calls['forward']==1

def test_original_source_cas_rejects_post_compute_change(media,monkeypatch):
    def changed():
        with media[0].connect()as conn:
            conn.execute('BEGIN IMMEDIATE');conn.execute("UPDATE voice_input SET transcript='changed during compute'");conn.commit()
    assessor,request,calls=bridge(media,monkeypatch,changed)
    with pytest.raises(ValueError):assessor.assess(CHILD,media[2],**request)
    with media[0].connect()as conn:assert conn.execute('SELECT count(*) FROM pet_speaking_model_candidate').fetchone()[0]==0

def test_other_child_cannot_compute(media,monkeypatch):
    assessor,request,calls=bridge(media,monkeypatch)
    with pytest.raises(ValueError,match='CHILD_SCOPE_REQUIRED'):assessor.assess('other-child',media[2],**request)
    assert calls['forward']==0
