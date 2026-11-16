"""Original parent Popen, framed stdin, worker main/closed result and owned drain.

Epoch/Windows Job/learned model are explicit test substitutes; no native credit.
"""
import io,wave,hashlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_pet_multipa_reference_worker import ASSETS,REFERENCE_AUTHORITY,REFERENCE_DECLARATION
from home_study_agent.pet_multipa_reference_segments import ADAPTER

@pytest.mark.parametrize('mode',['natural','cancel','crash','private_error','timeout'])
def test_actual_original_worker_long_protocol_and_owned_drain(tmp_path,monkeypatch,mode):
    """Actual Popen/pipe and original worker body; fake epoch/Job/model only.

    This explicitly does not prove a Windows Job/ACL/install or model inference.
    The exact new reference adapter is separately exercised via the original
    upstream function above; this case verifies framing/dispatch/owned drain.
    """
    from home_study_agent import pet_multipa_process as process,pet_multipa_worker as worker
    from home_study_agent.pet_multipa_reference import text_input
    import subprocess,sys
    from contextlib import contextmanager
    from uuid import uuid4
    root=tmp_path/'private-synthetic-worker';root.mkdir();real_worker=Path(worker.__file__).resolve();code=real_worker.parent.parent
    launch=root/'bootstrap.py'
    launch.write_text('''import sys,runpy,hashlib,json,wave,os,time
from pathlib import Path
from contextlib import nullcontext
sys.path.insert(0,CODE)
from home_study_agent import pet_multipa_epoch as epoch,pet_multipa_cpu_engine as engine
from home_study_agent import registered_asset_snapshot,money_r4
from home_study_agent.money_r4 import money_files
class Lease:
 def __init__(self,key,parent_approved):self.approved=parent_approved;self.doc={'epoch':'TEST_ONLY'}
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def path(self,key):
  if key=='worker_script':return Path(WORKER)
  if key=='code_directory':return Path(CODE)
  return Path(STDLIB if key in ('python_stdlib','python_dlls')else PACKAGES)
class Held:
 def __init__(self,d,n,p):self.path=d/n;self.before=self.path.read_bytes()
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def hash(self):return hashlib.sha256(self.before).hexdigest(),len(self.before)
 def check(self):assert self.path.read_bytes()==self.before
class Model:
 def __init__(self,lease):pass
 def predict_original(self,path):raise AssertionError('REFERENCE_MUST_NOT_FALL_BACK')
 def predict_reference_segmented(self,path,reference,*,deadline):
  from home_study_agent.pet_multipa_reference_segments import ADAPTER,RESULT
  assert reference['text']=='Public original reference'
  mode=CONTROL_MODE
  if mode in('cancel','timeout'):
   (path.parent/'reference_entered').write_bytes(b'1')
   time.sleep(10)
  if mode=='crash':os._exit(73)
  if mode=='private_error':
   print(reference['text'],flush=True);print('PRIVATE_MODEL_TRANSCRIPT',file=sys.stderr,flush=True)
   raise ValueError(reference['text']+' PRIVATE_EXCEPTION')
  raw=path.read_bytes()
  with wave.open(str(path),'rb')as w:frames=w.getnframes()
  segments=[]
  for i in range(3):
   segments.append(dict(start_frame=i*160000,end_frame=(i+1)*160000,start_word=i,end_word=i+1,sentence=dict(uttr_acc=.1,uttr_fluency=.2,uttr_prosodic=.3,uttr_total=.4),words=dict(word_acc=[.1],word_stress=[.2],word_total=[.3]),word_frames=[[i*160000,i*160000+1600]]))
  return dict(schema=RESULT,adapter=ADAPTER,audio_sha256=hashlib.sha256(raw).hexdigest(),reference_sha256=reference['text_sha256'],reference_utf8_bytes=reference['text_utf8_bytes'],frames=frames,reference_word_count=3,alignment_frame_count=2998,segments=segments)
epoch.WindowsEpochLease=Lease;money_files.OwnerDirectory=lambda p:nullcontext()
registered_asset_snapshot.HeldFile=Held;engine.WorkerCPUModel=Model
runpy.run_path(WORKER,run_name='__main__')
'''.replace('CONTROL_MODE',repr(mode)).replace('CODE',repr(str(code))).replace('WORKER',repr(str(real_worker))).replace('STDLIB',repr(str(Path(sys.base_prefix)/'lib/python3.9'))).replace('PACKAGES',repr(str(Path(sys.prefix)/'lib/python3.9/site-packages'))))
    approved=dict(asset_hashes=ASSETS,reference_adapter=REFERENCE_DECLARATION,reference_segment_adapter={'version':ADAPTER,'deployment_sha256':REFERENCE_AUTHORITY['deployment.json'],'work_seconds':30,'max_audio_frames':480000},authority_sha256=REFERENCE_AUTHORITY,timeout_seconds=10,memory_bytes=268435456,private_work_root=str(root),actor_sid='TEST_ONLY')
    class ParentLease:
        def __init__(self,*a):self.doc={'epoch':'TEST_ONLY'}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def path(self,key):return Path(sys.executable)if key=='python_executable'else launch
        def check(self):pass
    class Job:
        def __init__(self,m):self.assigned=False;self.process=None
        def assign(self,p):self.assigned=True;self.process=p
        def active(self):return 0
        def terminate(self):
            assert mode in('cancel','timeout') and self.process in created
            if self.process.poll()is None:self.process.kill()  # Exact test-created Python child only.
        def close(self):pass
    @contextmanager
    def scratch(*a):
        p=root/('multipa-'+uuid4().hex);p.mkdir()
        try:yield p
        finally:
            import shutil
            shutil.rmtree(p)
    real_popen=subprocess.Popen;created=[]
    def popen(*a,**kw):
        # POSIX control only: retain real owned process/pipes, omit Win flags.
        kw.pop('creationflags');p=real_popen(*a,**kw);created.append(p);return p
    monkeypatch.setattr(process,'configured',lambda key:approved);monkeypatch.setattr(process,'WindowsEpochLease',ParentLease);monkeypatch.setattr(process,'private_scope',scratch);monkeypatch.setattr(process,'_WindowsWorkerJob',Job);monkeypatch.setattr(process.subprocess,'Popen',popen);monkeypatch.setattr(process.subprocess,'CREATE_NO_WINDOW',0,raising=False)
    out=io.BytesIO()
    with wave.open(out,'wb')as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(b'\0\0'*480000)
    import time as real_time
    original_monotonic=real_time.monotonic;original_sleep=real_time.sleep
    if mode=='timeout':
        # Controlled clock crosses the original derived 30s parent deadline only
        # after this exact child has received the private input and entered work.
        clock=lambda:31.0 if list(root.glob('multipa-*/reference_entered'))else 0.0
        monkeypatch.setattr(process,'time',SimpleNamespace(monotonic=clock,sleep=original_sleep))
    deadlines=[];communicate=process._communicate_owned
    def transport(*a,**kw):
        remaining=kw['deadline']-process.time.monotonic();deadlines.append(remaining)
        assert 29.9<=remaining<=30.0  # Long reviewed budget, never short timeout10.
        return communicate(*a,**kw)
    monkeypatch.setattr(process,'_communicate_owned',transport)
    def cancelled():return mode=='cancel' and bool(list(root.glob('multipa-*/reference_entered')))
    if mode=='natural':
        value=process.predict_in_worker('TEST_ONLY',out.getvalue(),reference=text_input('Public original reference'),segmented=True)
        assert value['reference_sha256']==text_input('Public original reference')['text_sha256'] and value['segments'][0]['word_frames']==[[0,1600]]
    else:
        with pytest.raises(ValueError)as exc:
            process.predict_in_worker('TEST_ONLY',out.getvalue(),reference=text_input('Public original reference'),should_stop=cancelled,segmented=True)
        message=str(exc.value)
        assert message.startswith('PET_MULTIPA_') and 'Public original reference'not in message and 'PRIVATE_'not in message
        if mode=='cancel':assert message=='PET_MULTIPA_WORKER_CANCELLED'
        if mode=='timeout':assert message=='PET_MULTIPA_WORKER_TIMEOUT'
    assert len(created)==1 and created[0].returncode==({'natural':0,'cancel':-9,'crash':73,'private_error':20,'timeout':-9}[mode]) and created[0].stdin.closed and created[0].stdout.closed
    assert not process._QUARANTINE
    assert not list(root.glob('multipa-*')) and not process._WORKER_LOCK.locked()

    assert len(deadlines)==1
