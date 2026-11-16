"""Real cross-process startup admission; all databases and gateways synthetic."""
import asyncio
from contextlib import contextmanager
import hashlib,json,os,queue,subprocess,sys,threading,time
from pathlib import Path
import pytest
import home_study_agent
from home_study_agent.database import Database,LATEST_SCHEMA_VERSION,UnsupportedDatabaseVersionError
from home_study_agent import restore_publication as pub

CHILD=r'''
import asyncio,inspect,json,os,socket,sys,time
from pathlib import Path
source,mode,path,budget=sys.argv[1:];path=Path(path);budget=float(budget)
os.environ.clear();os.environ.update(PATH='/usr/bin:/bin',HOME=str(path.parent/'home'),PYTHONDONTWRITEBYTECODE='1')
def deny(event,args):
 if event in {'socket.connect','socket.getaddrinfo'}:raise RuntimeError('SYNTHETIC_NETWORK_DENIED')
sys.addaudithook(deny);sys.path.insert(0,source)
from home_study_agent.database import Database
from home_study_agent import restore_publication as pub
def emit(kind,**v):print('HSC_WAIT_CONTROL '+json.dumps(dict(kind=kind,pid=os.getpid(),**v)),flush=True)
codes={pub.initialization_preflight.__wrapped__.__code__:'preflight',pub.publication_owner.__wrapped__.__code__:'owner',pub._publication_owner.__wrapped__.__code__:'native_owner',Database.external_effect_guard.__wrapped__.__code__:'external'}
lines,start=inspect.getsourcelines(pub.initialization_preflight.__wrapped__)
waitline=start+next(i for i,s in enumerate(lines)if 'g.condition.wait(min(left,0.01))'in s)
waited=[False];observed_frames=[]
def observe(frame,event,arg):
 if frame.f_code not in codes:return None
 if event=='call'and not any(frame is old for old in observed_frames):
  observed_frames.append(frame)
  if frame.f_locals.get('deadline')is not None:emit('deadline',phase=codes[frame.f_code],value=frame.f_locals['deadline'])
 if frame.f_code is pub.initialization_preflight.__wrapped__.__code__ and event=='line'and frame.f_lineno==waitline and not waited[0]:waited[0]=True;emit('waiting')
 return observe
sys.settrace(observe);db=Database(path);started=time.monotonic()
try:
 if mode=='read':
  with db.connect()as c:c.execute('SELECT 1').fetchone();emit('held');assert sys.stdin.readline().strip()=='release'
 elif mode=='external':
  with db.external_effect_guard():emit('held');assert sys.stdin.readline().strip()=='release'
 elif mode=='app':
  from fastapi.testclient import TestClient
  from home_study_agent.app import create_app
  from home_study_agent.config import Settings
  class NoModel:
   async def complete(self,request):raise AssertionError('NO_MODEL_ALLOWED')
  with TestClient(create_app(Settings(data_dir=path.parent/'app',database_path=path,maintenance_enabled=False,model_provider='disabled'),model_gateway=NoModel())):pass
 elif mode=='initialize':db.initialize(startup_wait_seconds=budget)
 else:raise AssertionError('FIXED_CONTROL_MODE_REQUIRED')
 emit('done',elapsed=time.monotonic()-started);sys.exit(0)
except Exception as error:
 code=str(error)if type(error)is pub.RestorePublicationError else 'EXTERNAL_EFFECT_BUSY'if type(error).__name__=='ExternalEffectBusyError'else'UNKNOWN'
 emit('error',error_type=type(error).__name__,code=code,elapsed=time.monotonic()-started,waiting=pub._gate(path).waiting)
 sys.exit(1)
'''

class Child:
 def __init__(self,mode,path,budget=.2):
  source=Path(home_study_agent.__file__).resolve().parents[1]
  self.process=subprocess.Popen([sys.executable,'-B','-c',CHILD,str(source),mode,str(path),str(budget)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  self.events=[];self.stderr=[];self.lines=queue.Queue();self.mode=mode
  def stdout():
   for line in self.process.stdout:
    if line.startswith('HSC_WAIT_CONTROL '):
     row=json.loads(line.split(' ',1)[1]);self.events.append(row);self.lines.put(row)
  def stderr():self.stderr.append(self.process.stderr.read())
  self.readers=[threading.Thread(target=stdout,daemon=True),threading.Thread(target=stderr,daemon=True)]
  for thread in self.readers:thread.start()
 def until(self,kind):
  deadline=time.monotonic()+15
  while True:
   row=self.lines.get(timeout=max(.01,deadline-time.monotonic()))
   if row['kind']==kind:return row
   assert row['kind']!='error',row
 def release(self):
  if self.process.poll()is None:
   try:self.process.stdin.write('release\n');self.process.stdin.flush()
   except BrokenPipeError:pass
 def finish(self):
  code=self.process.wait(timeout=15)
  for thread in self.readers:thread.join(timeout=2);assert not thread.is_alive()
  for stream in (self.process.stdin,self.process.stdout,self.process.stderr):stream.close()
  evidence=os.environ.get('HSC_STARTUP_WAIT_EVIDENCE')
  if evidence:
   with open(evidence,'a')as f:f.write(json.dumps(dict(mode=self.mode,pid=self.process.pid,native_exit=code,events=self.events,stderr=''.join(self.stderr)))+'\n')
  return code

@pytest.fixture
def database(tmp_path):
 db=Database(tmp_path.resolve()/'study.db');db.initialize();return db

@contextmanager
def holder(database,mode='read'):
 child=Child(mode,database.path);child.until('held')
 try:yield child
 finally:child.release();assert child.finish()==0

def namespace(path):
 return {p.name:(len(p.read_bytes()),hashlib.sha256(p.read_bytes()).hexdigest())for p in path.iterdir()if p.is_file()}

def test_direct_preflight_and_initialize_remain_fail_fast(database):
 with holder(database):
  before=namespace(database.path.parent);started=time.monotonic()
  with pytest.raises(pub.RestorePublicationError,match='RESTORE_PUBLICATION_BUSY'):
   with pub.initialization_preflight(database):pass
  with pytest.raises(pub.RestorePublicationError,match='RESTORE_PUBLICATION_BUSY'):database.initialize()
  assert time.monotonic()-started<1
  assert namespace(database.path.parent)==before

def test_real_App_startup_waits_other_process_short_read_then_naturally_closes(database):
 with holder(database)as reader:
  child=Child('app',database.path)
  try:
   child.until('waiting');assert child.process.poll()is None
   reader.release()
   assert child.finish()==0,child.events
  finally:reader.release()
 values=[x['value']for x in child.events if x['kind']=='deadline']
 assert len(set(values))==1
 assert {'preflight','owner','external','native_owner'}<={x['phase']for x in child.events if x['kind']=='deadline'}

def test_long_reader_timeout_preserves_all_original_files_and_clears_waiting(database):
 with holder(database):
  before=namespace(database.path.parent);child=Child('initialize',database.path,.15)
  child.until('waiting');assert child.finish()==1
  error=next(x for x in child.events if x['kind']=='error')
  assert error['code']=='RESTORE_PUBLICATION_BUSY'and error['waiting']is False
  assert .1<error['elapsed']<2 and namespace(database.path.parent)==before

def test_preflight_and_external_lock_share_one_total_deadline(database):
 with holder(database,'external'):
  with holder(database)as reader:
   before=namespace(database.path.parent);child=Child('initialize',database.path,.3)
   child.until('waiting');time.sleep(.12);reader.release()
   assert child.finish()==1
   error=next(x for x in child.events if x['kind']=='error')
   assert error['error_type']=='ExternalEffectBusyError'and error['waiting']is False
   assert .25<error['elapsed']<2 and namespace(database.path.parent)==before
   values=[x['value']for x in child.events if x['kind']=='deadline']
   assert len(set(values))==1 and {'preflight','owner','external'}<={x['phase']for x in child.events if x['kind']=='deadline'}

@pytest.mark.skipif(os.name=='nt',reason='Windows held handles prevent rename; this native mutation control is POSIX-only')
@pytest.mark.parametrize('changed',['lock','parent'])
def test_original_identity_checks_reject_changes_while_waiting(database,changed):
 with holder(database):
  child=Child('initialize',database.path,1);child.until('waiting')
  original=database.path.with_name(pub._names(database.path)[0])if changed=='lock'else database.path.parent
  moved=original.with_name(original.name+'.held-control');original.rename(moved)
  if changed=='lock':original.write_bytes(b'0')
  else:original.mkdir()
  try:
   assert child.finish()==1
   error=next(x for x in child.events if x['kind']=='error')
   assert error['code']in {'RESTORE_PUBLICATION_LOCK_ALIAS','RESTORE_PUBLICATION_PARENT_CHANGED','RESTORE_PUBLICATION_CLOSE_UNCONFIRMED'}
   assert error['waiting']is False
  finally:
   if changed=='lock':original.unlink()
   else:original.rmdir()
   moved.rename(original)

def test_async_startup_does_not_block_Task_and_remains_cancellable(database):
 with holder(database):
  async def run():
   started=time.monotonic()
   with pytest.raises(pub.RestorePublicationError,match='RESTORE_PUBLICATION_BUSY'):database.initialize(startup_wait_seconds=1)
   assert time.monotonic()-started<.5
   waiting=asyncio.create_task(asyncio.sleep(10));await asyncio.sleep(0);waiting.cancel()
   with pytest.raises(asyncio.CancelledError):await waiting
  asyncio.run(run())
 assert pub._gate(database.path).waiting is False

def test_same_cancel_exception_during_sync_wait_closes_original_entry(database,monkeypatch):
 with holder(database):
  gate=pub._gate(database.path);cancel=asyncio.CancelledError()
  def interrupted(*a,**k):raise cancel
  with monkeypatch.context()as patch:
   patch.setattr(gate.condition,'wait',interrupted)
   with pytest.raises(asyncio.CancelledError)as caught:database.initialize(startup_wait_seconds=1)
   assert caught.value is cancel and gate.waiting is False
 database.initialize()

def test_future_schema_opt_in_still_rejects_without_any_coordination_writes(tmp_path):
 import sqlite3
 path=tmp_path.resolve()/'future.db'
 with sqlite3.connect(path)as c:c.executescript('CREATE TABLE schema_migration(version INTEGER PRIMARY KEY,applied_at TEXT);INSERT INTO schema_migration VALUES('+str(LATEST_SCHEMA_VERSION+1)+',"public");')
 before=namespace(path.parent)
 with pytest.raises(UnsupportedDatabaseVersionError):Database(path).initialize(startup_wait_seconds=1)
 assert namespace(path.parent)==before

def test_active_invalid_journal_remains_rejected_before_schema_writes(database):
 active=database.path.with_name(pub._names(database.path)[1]);active.write_bytes(b'{"schema":1,"unknown":true}')
 before=namespace(database.path.parent)
 with pytest.raises(pub.RestorePublicationError,match='JOURNAL_INVALID'):database.initialize(startup_wait_seconds=1)
 assert namespace(database.path.parent)==before
