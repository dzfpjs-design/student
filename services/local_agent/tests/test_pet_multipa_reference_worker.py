"""Public synthetic original material -> recording -> atomic durable blocked intent.

ASR is a controlled transcript boundary. No acoustic model or score is executed.
"""
from datetime import datetime, timezone
from uuid import uuid4
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, AgentPlanDecision, ObjectiveDraft, PlannedStep
from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs
from test_pet_oral_supply_http import setup, authored, post, BASE, CHILD, OTHER
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import start
from test_voice_runtime_binding import capture, interpret, submit


ASSETS={k:hashlib.sha256(('SYNTHETIC-NOT-A-MODEL-'+k).encode()).hexdigest() for k in ('multipa','charsiu','hubert','roberta','whisper_medium_en','whisper_base_en')}
REFERENCE_AUTHORITY={'deployment.json':hashlib.sha256(b'SYNTHETIC-NOT-AN-EPOCH').hexdigest()}
REFERENCE_DECLARATION={'version':'multipa_reference_short_v1','deployment_sha256':REFERENCE_AUTHORITY['deployment.json']}

from test_pet_multipa_completion import public_host as _base_public_host

@pytest.fixture
def public_host(_base_public_host):
    # Reuse the actual public subprocess and original attestation resource ledger.
    try:yield _base_public_host
    finally:
        from home_study_agent import pet_multipa_epoch as epoch
        epoch._REVIEWED_DEPLOYMENTS.clear()

def original_engine_result_port(host,monkeypatch,predict,*,segmented=False):
    """Run the existing acoustic fixture at the controlled child/result boundary.

    The child returns only that actual engine result. Original host input gate,
    decoder, same-close ledger, seal, consumer and store/CAS remain untouched.
    This is synthetic resource/protocol evidence, never installed/model credit.
    """
    import struct
    from home_study_agent import pet_multipa_process as process
    port=host.script.parent/'public-original-engine-result.json'
    child="""import sys,json,struct,hashlib
from pathlib import Path
n=struct.unpack('!I',sys.stdin.buffer.read(4))[0]
h=json.loads(sys.stdin.buffer.read(n));audio=sys.stdin.buffer.read(h['audio_bytes'])
assert len(audio)==h['audio_bytes'] and hashlib.sha256(audio).hexdigest()==h['audio_sha256']
wire=Path(PORT).read_bytes()
if wire==b'MODEL_REJECTED':
 print(json.dumps(dict(schema=SCHEMA,nonce=h['nonce'],status='failed',error='MODEL_REJECTED')),end='');sys.exit(20)
result=json.loads(wire)
print(json.dumps(dict(schema=SCHEMA,nonce=h['nonce'],status='completed',result=result),allow_nan=False),end='')
"""
    schema='multipa_reference_segmented_worker_v1'if segmented else'multipa_reference_worker_v1'
    host.script.write_text(child.replace('PORT',repr(str(port))).replace('SCHEMA',repr(schema)))
    communicate=process._communicate_owned
    def actual_result(process_owner,payload,**kw):
        assert process_owner.pid>0
        size=struct.unpack('!I',payload[:4])[0]
        header=json.loads(payload[4:4+size]);content=payload[4+size:]
        assert len(content)==header['audio_bytes']and hashlib.sha256(content).hexdigest()==header['audio_sha256']
        args=dict(should_stop=kw['should_stop'],reference=header['reference'])
        if segmented:args['segmented']=True
        # Preserve original engine/forward/on_compute; no canned result or cap.
        try:result=predict(header['key'],content,**args)
        except Exception:
            # The original worker converts model exceptions to this closed
            # failed frame. Still execute owned communicate/drain; no private
            # exception string and no success completion can cross this port.
            with port.open('xb')as f:f.write(b'MODEL_REJECTED')
        else:
            with port.open('x')as f:json.dump(result,f,allow_nan=False)
        try:return communicate(process_owner,payload,**kw)
        finally:port.unlink()
    monkeypatch.setattr(process,'_communicate_owned',actual_result)

def make_read_aloud(tmp_path, monkeypatch, configured=False, segmented=False, worker_host=None):
    db, settings = setup(tmp_path)
    gateway = Gateway({}); gateway.configured = False
    from home_study_agent import pet_speaking_candidate_jobs as jobs
    from home_study_agent import pet_multipa_offline as offline
    from home_study_agent import pet_multipa_epoch as epoch
    assert epoch._REVIEWED_DEPLOYMENTS == {}
    if configured:
        approval={'asset_hashes':ASSETS,'reference_adapter':REFERENCE_DECLARATION,'authority_sha256':REFERENCE_AUTHORITY,'fixture':'NOT_A_TRUSTED_INSTALLATION'}
        if worker_host is not None:
            approval=worker_host.approved
            approval['asset_hashes']=ASSETS
            approval['reference_adapter']={'version':'multipa_reference_short_v1','deployment_sha256':approval['authority_sha256']['deployment.json']}
        if segmented:
            from home_study_agent.pet_multipa_reference_segments import ADAPTER
            approval['reference_segment_adapter']={'version':ADAPTER,'deployment_sha256':approval['authority_sha256']['deployment.json'],'work_seconds':60,'max_audio_frames':960000}
        monkeypatch.setitem(epoch._REVIEWED_DEPLOYMENTS,'REFERENCE_TEST_ONLY',approval)
    real_process_one = jobs.SpeakingCandidateJobs.process_one
    monkeypatch.setattr(jobs.SpeakingCandidateJobs,'process_one',lambda *a,**k:None)  # controlled background scheduling only
    monkeypatch.setattr(offline, 'predict_in_worker', lambda *a, **k: pytest.fail('REFERENCE_WORKER_MUST_NOT_RUN'))
    registered = []
    original = jobs.register_new_feedback_intent
    def register(c, *args):
        assert c.in_transaction
        result = original(c, *args)
        if result:
            registered.append(result)
            assert c.execute("SELECT count(*) FROM learning_evaluation_execution WHERE state='completed'").fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM pet_speaking_candidate_job').fetchone()[0] == 1
        return result
    monkeypatch.setattr(jobs, 'register_new_feedback_intent', register)
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}, client=('127.0.0.1', 41910)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True}).status_code == 200
        source_text, source, draft = authored(client, db, 'read_aloud')
        for version, operation in enumerate(('validate', 'review', 'publish'), 1):
            body = dict(action_id=operation, expected_version=version)
            if operation == 'review':
                body.update(decision='approve', note='Public synthetic original passage review, no learner score')
            published = post(client, BASE + '/' + draft['candidate_id'] + '/' + operation, body)
        assert published['eligible']
        item_id = published['diagnostic_item_id']
        sid = start(client)
        runtime = AgentRuntimeStore(db); turn = uuid4().hex; stamp = datetime.now(timezone.utc).isoformat()
        with db.connect() as c:
            c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','Public synthetic original reading goal',?,'diagnose','active',?,?,NULL,NULL)",
                (uuid4().hex, CHILD, sid, json.dumps(['Read the reviewed original passage']), stamp, stamp))
            c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)VALUES(?,?,?,'','start_diagnostic','','{\"type\":\"start_diagnostic\"}','pending','test',?,?)", (turn, CHILD, sid, stamp, turn))
            cap = CapabilityRegistry().require('diagnostic.start', 'study')
            runtime.persist_plan(c, child_id=CHILD, session_id=sid, agent_turn_id=turn, client_action_id=turn, context_manifest={},
                decision=AgentPlanDecision(ObjectiveDraft('pause', None, None, (), 'execution'), PlannedStep(cap, {'track': 'pet'}, 'Original read aloud', cap.expected_evidence), '', True, 1))
            c.commit()
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={'track': 'pet', 'preferred_item_id': item_id, 'client_action_id': 'reference-question'})
        assert response.status_code == 200, response.text
        prompt = response.json()['prompt']
        assert prompt['item']['stimulus_text'] == source_text['stimulus_text']
        assert prompt['item']['prompt'] != source_text['stimulus_text']
        with db.connect() as c:
            runtime.complete_turn_execution(c, turn, True, None, {'execution_receipt': {'output': response.json(), 'capability': cap.name, 'execution_location': cap.execution_location, 'action_type': 'start_diagnostic', 'status': 'completed', 'completed_at': stamp}})
            c.execute("UPDATE agent_turn SET tool_status='completed' WHERE agent_turn_id=?", (turn,)); c.commit()
        # Deliberately different ASR text: it must never become the reference.
        answer = 'Ada visited a garden and asked her teacher for help.'
        voice = capture(client, settings, monkeypatch, sid, answer, prompt, 'reference-original-answer')
        heard = interpret(client, sid, answer, 'reference-answer-turn', voice)
        assert heard['intent'] == 'pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code == 200
        confirmation = '刚才那句就是我的答案'
        confirmation_voice = capture(client, settings, monkeypatch, sid, confirmation, prompt, 'reference-confirmation')
        heard = interpret(client, sid, confirmation, 'reference-confirmation-turn', confirmation_voice)
        assert heard['action']['source_voice_input_id'] == voice and voice != confirmation_voice
        saved = submit(client, sid, prompt, heard, answer=answer, key='reference-submit')
        assert saved.status_code == 200, saved.text
        assert saved.json()['status'] == 'answered'
        assert 'local_speaking_candidate' in saved.json(), 'READ_ALOUD_DURABLE_INTENT_REQUIRED'
        public_job = saved.json()['local_speaking_candidate']
        assert public_job['state'] == ('queued' if configured else 'blocked'), 'REFERENCE_CAPABLE_NEW_INTENT_REQUIRED'
        assert public_job['error_code'] == (None if configured else 'REFERENCE_EXECUTOR_UNAVAILABLE')
        assert len(registered) == 1
        job = registered[0]  # The original response intentionally hides internal job IDs.
        with db.connect() as c:
            row = dict(c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=?', (job['job_id'],)).fetchone())
            binding = json.loads(row['binding_json'])
            assert binding['task_family'] == 'read_aloud' and binding['voice_input_id'] == voice
            assert source_text['stimulus_text'] not in row['binding_json'] and answer not in row['binding_json']
            assert binding['reference']['text_sha256'] == hashlib.sha256(source_text['stimulus_text'].encode()).hexdigest()
            assert binding['reference']['text_utf8_bytes'] == len(source_text['stimulus_text'].encode())
            assert binding['reference']['source']['source_id'] == source['source_id']
            assert binding['reference']['diagnostic_item_id'] == item_id and binding['reference']['item_version'] == prompt['item_version']
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM pet_speaking_model_candidate').fetchone()[0] == 0
        replay = submit(client, sid, prompt, heard, answer=answer, key='reference-submit')
        assert replay.status_code == 200 and replay.json()['local_speaking_candidate']['state'] == ('queued' if configured else 'blocked')
        assert replay.json()['local_speaking_candidate']['error_code'] == (None if configured else 'REFERENCE_EXECUTOR_UNAVAILABLE')
        assert len(registered) == 1
    assert not gateway.requests
    monkeypatch.setattr(jobs.SpeakingCandidateJobs,'process_one',real_process_one)
    return dict(db=db, settings=settings, gateway=gateway, source=source, original=source_text, draft=draft, item_id=item_id,
                sid=sid, prompt=prompt, row=row, binding=binding, voice=voice, job=job, registered=registered)


def test_reference_capable_new_original_completion_queued(tmp_path,monkeypatch):
    v=make_read_aloud(tmp_path,monkeypatch,configured=True)
    assert v['job']['state']=='queued'

# All learned tensors and Windows deployment authority below are synthetic.
# The current original API/worker/protocol/DB implementations are exercised;
# neither this fixture nor its outputs authorize a deployment or PET score.
from contextlib import nullcontext
from types import SimpleNamespace
from pathlib import Path
import ast, io, wave, re, string
import numpy as np

def acoustic_fixture(on_compute=lambda:None):
    from home_study_agent.pet_multipa_cpu_engine import WorkerCPUModel
    from home_study_agent import pet_multipa_cpu_engine
    source=Path(pet_multipa_cpu_engine.__file__).parent/'_multipa_cpu_vendor'
    api_node=next(n for n in ast.parse((source/'api.py').read_text()).body if isinstance(n,ast.FunctionDef)and n.name=='inference_one_seg')
    utility_tree=ast.parse((source/'utils_assessment.py').read_text())
    normalize=next(n for n in utility_tree.body if isinstance(n,ast.FunctionDef)and n.name=='remove_pun_except_apostrophe')
    seen={'audio':0,'asr':0,'features':[],'forward':0}
    class Tensor:
        def __init__(self,value):self.value=np.asarray(value);self.shape=self.value.shape
        def numpy(self):return self.value
        def cpu(self):return self
        def detach(self):return self
        def to(self,*a):return self
        def float(self):return self
        def unsqueeze(self,axis):return Tensor(np.expand_dims(self.value,axis))
    def load(path):
        seen['audio']+=1
        with wave.open(str(path),'rb')as w:return Tensor(np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32).reshape(1,-1)/32768),w.getframerate()
    def asr(*a):seen['asr']+=1;return 'wrong ASR is not original material'
    def features(audio,reference,observed,**kw):
        seen['features'].append((reference,observed))
        return [(0,.1,'PRIVATE_ALIGNED_WORD')],np.zeros((1,1)),np.zeros((1,1)),np.zeros((1,142)),np.zeros((1,768)),np.zeros((1,768)),[[0]]
    def forward(*a):
        seen['forward']+=1;on_compute()
        return *(Tensor([x])for x in (.2,.3,-.1,.4)),*(Tensor([[x]])for x in (.5,.6,.7))
    torch=SimpleNamespace(inference_mode=nullcontext,reshape=lambda a,s:Tensor(np.reshape(a.value,s)),from_numpy=Tensor,cuda=SimpleNamespace(empty_cache=lambda:None))
    env=dict(torch=torch,string=string,convert_num_to_word=lambda x:x,feature_extraction=features,aligment_model=object(),word_model=object(),device='synthetic',SAMPLE_RATE=16000,assessment_model=forward)
    exec(compile(ast.Module(body=[normalize,api_node],type_ignores=[]),str(source/'api.py'),'exec'),env)
    api=SimpleNamespace(torchaudio=SimpleNamespace(load=load),get_transcript=asr,inference_one_seg=env['inference_one_seg'])
    model=object.__new__(WorkerCPUModel);model.api=api;model.torch=torch;model.whisper_w=None;model.lease=SimpleNamespace(check=lambda:None)
    return model,seen

def synthetic_result(content,reference):
    from home_study_agent.pet_multipa_reference import REFERENCE_RESULT,REFERENCE_ADAPTER
    with wave.open(io.BytesIO(content),'rb')as w:frames=w.getnframes()
    return dict(schema=REFERENCE_RESULT,adapter=REFERENCE_ADAPTER,audio_sha256=hashlib.sha256(content).hexdigest(),reference_sha256=reference['text_sha256'],reference_utf8_bytes=reference['text_utf8_bytes'],frames=frames,sentence=dict(uttr_acc=.2,uttr_fluency=.3,uttr_prosodic=-.1,uttr_total=.4),words=dict(word_acc=[.5],word_stress=[.6],word_total=[.7]),word_frames=[[0,1600]])

def install_assessor(v,monkeypatch,on_compute=lambda:None,*,worker_host):
    from home_study_agent import pet_multipa_offline as offline
    from home_study_agent.pet_multipa_offline import SpeakingOfflineCandidateAssessor
    model,seen=acoustic_fixture(on_compute)
    def predict(key,content,*,should_stop,reference):
        assert key=='REFERENCE_TEST_ONLY' and not should_stop()
        assert reference['text']==v['original']['stimulus_text']
        path=v['settings'].data_dir/'public-controlled-reference.wav'
        path.write_bytes(content)
        try:return model.predict_reference_original(path,reference)
        finally:path.unlink()
    original_engine_result_port(worker_host,monkeypatch,predict)
    runtime=AgentRuntimeStore(v['db'],speaking_candidate_assessor=SpeakingOfflineCandidateAssessor(v['db'],media_root=v['settings'].data_dir,deployment_key='REFERENCE_TEST_ONLY',data_encryption_key=v['settings'].data_encryption_key))
    return SpeakingCandidateJobs(v['db'],runtime,media_root=v['settings'].data_dir,data_encryption_key=v['settings'].data_encryption_key),seen

def test_real_reference_job_raw_candidate_reopen_replay_and_withdraw(tmp_path,monkeypatch,public_host):
    v=make_read_aloud(tmp_path,monkeypatch,configured=True,worker_host=public_host);jobs,seen=install_assessor(v,monkeypatch,worker_host=public_host)
    done=jobs.process_one();assert done['state']=='completed'
    store=jobs.runtime.speaking_candidate_assessor.store;current=store.get(CHILD,done['candidate_id'])
    assert current['source_status']=='current' and current['raw_numeric']['alignment_frames']==[[0,1600]]
    assert seen['audio']==seen['asr']==seen['forward']==1
    assert seen['features'][0][0]==v['original']['stimulus_text'].translate(str.maketrans('','',string.punctuation.replace("'",''))).replace('  ',' ').lower()
    assert seen['features'][0][0]!=seen['features'][0][1]
    assert not current['assessor_installed'] and not current['skill_qualified'] and not current['mastery_claim_allowed']
    assert 'score'not in current['raw_numeric'] and 'pass'not in current['raw_numeric']
    with v['db'].connect()as c:
        row=dict(c.execute('SELECT * FROM pet_speaking_model_candidate').fetchone())
        assert v['original']['stimulus_text']not in json.dumps(row) and 'PRIVATE_ALIGNED_WORD'not in json.dumps(row) and 'wrong ASR'not in json.dumps(row)
        assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0]==0
    from home_study_agent.database import Database
    reopened_db=Database(v['db'].path)
    reopened=SpeakingCandidateJobs(reopened_db,AgentRuntimeStore(reopened_db),media_root=v['settings'].data_dir,data_encryption_key=v['settings'].data_encryption_key)
    assert reopened.read(CHILD,done['job_id'])['state']=='completed' and reopened.process_one()is None
    store.withdraw(CHILD,done['candidate_id'],expected_revision=1,client_action_id='reference-withdraw')
    assert store.get(CHILD,done['candidate_id'])['status']=='withdrawn' and jobs.process_one()is None and seen['forward']==1

@pytest.mark.parametrize('fault',['other_child','reference_changed','withdrawn','source_withdrawn','cancelled'])
def test_reference_source_change_or_cancel_during_compute_cannot_commit(tmp_path,monkeypatch,fault,public_host):
    v=make_read_aloud(tmp_path,monkeypatch,configured=True,worker_host=public_host)
    holder={}
    def changed():
        if fault=='cancelled':holder['jobs'].cancel(CHILD,v['job']['job_id']);return
        if fault=='source_withdrawn':
            from home_study_agent.curriculum_supply import CurriculumSupplyService
            result=CurriculumSupplyService(v['db']).withdraw(CHILD,v['draft']['candidate_id'],actor='parent:public-synthetic-withdrawal',note='Public reference withdrawn during compute',action_id='source-withdraw-during-compute',expected_version=4)
            assert not result['eligible']
            return
        if fault=='withdrawn':
            from home_study_agent.learning_evidence_withdrawal import record_withdrawal
            with v['db'].connect()as c:
                evidence=json.loads(c.execute('SELECT payload_json FROM rubric_assessment WHERE assessment_id=?',(v['row']['assessment_id'],)).fetchone()[0])['source_evidence_id']
                c.execute('BEGIN IMMEDIATE')
                record_withdrawal(c,child_id=CHILD,session_id=v['sid'],evidence_id=evidence,client_action_id='reference-parent-withdraw',request={'operation':'withdraw-public-reference-answer'})
                c.commit()
            return
        with v['db'].connect()as c:
            if fault=='other_child':c.execute('UPDATE voice_input SET child_id=? WHERE voice_input_id=?',(OTHER,v['voice']))
            else:c.execute('UPDATE diagnostic_item SET stimulus_text=stimulus_text||? WHERE diagnostic_item_id=?',(' revised',v['item_id']))
            c.commit()
    jobs,seen=install_assessor(v,monkeypatch,changed,worker_host=public_host);holder['jobs']=jobs
    value=jobs.process_one();assert value['state']==('cancelled'if fault=='cancelled'else'blocked')
    assert seen['forward']==1
    with v['db'].connect()as c:
        assert c.execute('SELECT count(*) FROM pet_speaking_model_candidate').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM learning_evidence_withdrawal').fetchone()[0]==(1 if fault=='withdrawn'else 0)
    assert jobs.process_one()is None and seen['forward']==1

def test_registry_empty_blocked_never_auto_activated_and_other_child_never_computes(tmp_path,monkeypatch,public_host):
    v=make_read_aloud(tmp_path,monkeypatch,configured=False)
    from home_study_agent import pet_multipa_epoch as epoch
    from home_study_agent.pet_multipa_offline import SpeakingOfflineCandidateAssessor
    monkeypatch.setitem(epoch._REVIEWED_DEPLOYMENTS,'REFERENCE_TEST_ONLY',{'asset_hashes':ASSETS,'reference_adapter':REFERENCE_DECLARATION,'authority_sha256':REFERENCE_AUTHORITY})
    jobs,seen=install_assessor(v,monkeypatch,worker_host=public_host)
    assert jobs.process_one()is None and jobs.read(CHILD,v['job']['job_id'])['state']=='blocked' and seen['forward']==0
    with pytest.raises(ValueError,match='CHILD_SCOPE'):
        jobs.runtime.speaking_candidate_assessor.assess(OTHER,v['sid'],media_id=v['row']['media_id'],assessment_id=v['row']['assessment_id'],client_action_id='foreign-reference')
    assert seen['forward']==0

def test_reference_protocol_strict_numeric_and_text_shape():
    from home_study_agent import pet_multipa_protocol as protocol
    from home_study_agent.pet_multipa_reference import text_input,validate_text,validate_result
    ref=text_input('Public original passage')
    for bad in [dict(ref,text='changed'),dict(ref,text_utf8_bytes=True),dict(ref,unknown='private')]:
        with pytest.raises(ValueError):validate_text(bad)
    stream=io.BytesIO()
    with wave.open(stream,'wb')as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(b'\0\0'*16000)
    raw=synthetic_result(stream.getvalue(),ref);nonce='b'*32
    packet=protocol.encode(nonce,result=raw,reference=True)
    assert protocol.decode(packet,nonce,0,reference=True)==raw
    for args in [(packet,nonce,0,False),(packet,'c'*32,0,True),(packet,nonce,20,True)]:
        with pytest.raises(ValueError):protocol.decode(*args[:3],reference=args[3])
    for key,value in [('word_frames',[[-1,1600]]),('word_frames',[[0,16001]]),('words',dict(word_acc=[True],word_stress=[.2],word_total=[.3])),('score',100),('reference_utf8_bytes',True),('frames',240000)]:
        with pytest.raises(ValueError):validate_result(dict(raw,**{key:value}))
    with pytest.raises(ValueError):protocol.decode(protocol.encode(nonce,error='CANCELLED',reference=True),nonce,20,reference=True)

def test_reference_capability_is_reviewed_epoch_bound_not_a_caller_flag():
    from home_study_agent.pet_multipa_reference import capable
    assert capable(dict(reference_adapter=REFERENCE_DECLARATION,authority_sha256=REFERENCE_AUTHORITY))
    for approval in [{},{'reference_adapter':'multipa_reference_short_v1'},{'reference_adapter':REFERENCE_DECLARATION},{'reference_adapter':dict(REFERENCE_DECLARATION,deployment_sha256='f'*64),'authority_sha256':REFERENCE_AUTHORITY},{'reference_adapter':dict(REFERENCE_DECLARATION,caller=True),'authority_sha256':REFERENCE_AUTHORITY}]:
        assert not capable(approval)

def test_original_feature_extractor_routes_explicit_reference_and_observed_audio_text():
    from home_study_agent import pet_multipa_cpu_engine
    p=Path(pet_multipa_cpu_engine.__file__).parent/'_multipa_cpu_vendor/utils_assessment.py'
    n=next(n for n in ast.parse(p.read_text()).body if isinstance(n,ast.FunctionDef)and n.name=='feature_extraction')
    calls=[]
    def align(audio,text,model):
        calls.append(text);return [],[(0,.1,text)],[text],np.zeros((1,1)),[0],[[0]]
    env=dict(np=np,get_charsiu_alignment=align,get_phone_alignment_features=lambda *a:np.zeros((1,1)),get_match_index=lambda *a:[0],get_word_alignment_features=lambda *a:([['reference']],[['observed']],np.zeros((1,1)),np.zeros((1,142)),[[0]]),get_roberta_word_embed=lambda *a:np.zeros((1,768)))
    exec(compile(ast.Module(body=[n],type_ignores=[]),str(p),'exec'),env)
    result=env['feature_extraction'](np.zeros(16000),'public frozen original','different observed text',object(),object())
    assert calls==['public frozen original','different observed text'] and len(result)==7

@pytest.mark.parametrize('mode',['valid_reference','tampered_reference'])
def test_actual_unconfigured_worker_never_accepts_authority_or_echoes_reference(mode):
    from home_study_agent import pet_multipa_worker,pet_multipa_protocol
    from home_study_agent.pet_multipa_reference import text_input
    import subprocess,struct,sys
    ref=text_input('PRIVATE_SYNTHETIC_REFERENCE_NOT_FOR_OUTPUT')
    if mode=='tampered_reference':ref['text_sha256']='f'*64
    raw=b'PUBLIC_NOT_WAV';header=dict(schema='multipa_reference_worker_input_v1',nonce='c'*32,key='UNCONFIGURED',approved={'reference_adapter':REFERENCE_DECLARATION,'authority_sha256':REFERENCE_AUTHORITY},epoch='NOT_AN_INSTALL',audio_bytes=len(raw),audio_sha256=hashlib.sha256(raw).hexdigest(),reference=ref)
    wire=json.dumps(header).encode();child=subprocess.Popen([sys.executable,'-I','-S','-B',pet_multipa_worker.__file__],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,close_fds=True)
    output,error=child.communicate(struct.pack('!I',len(wire))+wire+raw,timeout=5)
    assert child.returncode==20 and error==b'' and b'PRIVATE_SYNTHETIC'not in output and len(output)<1024
    packet=json.loads(output);assert packet['status']=='failed'and packet['error']in pet_multipa_protocol.ERRORS

@pytest.mark.parametrize('mode',['natural','cancel','crash','private_error'])
def test_real_owned_process_and_original_worker_reference_dispatch(tmp_path,monkeypatch,mode):
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
 def predict_reference_original(self,path,reference):
  from home_study_agent.pet_multipa_reference import REFERENCE_ADAPTER,REFERENCE_RESULT
  assert reference['text']=='Public original reference'
  mode=CONTROL_MODE
  if mode=='cancel':
   (path.parent/'reference_entered').write_bytes(b'1')
   time.sleep(10)
  if mode=='crash':os._exit(73)
  if mode=='private_error':
   print(reference['text'],flush=True);print('PRIVATE_MODEL_TRANSCRIPT',file=sys.stderr,flush=True)
   raise ValueError(reference['text']+' PRIVATE_EXCEPTION')
  raw=path.read_bytes()
  with wave.open(str(path),'rb')as w:frames=w.getnframes()
  return dict(schema=REFERENCE_RESULT,adapter=REFERENCE_ADAPTER,audio_sha256=hashlib.sha256(raw).hexdigest(),reference_sha256=reference['text_sha256'],reference_utf8_bytes=reference['text_utf8_bytes'],frames=frames,sentence=dict(uttr_acc=.1,uttr_fluency=.2,uttr_prosodic=.3,uttr_total=.4),words=dict(word_acc=[.1],word_stress=[.2],word_total=[.3]),word_frames=[[0,1600]])
epoch.WindowsEpochLease=Lease;money_files.OwnerDirectory=lambda p:nullcontext()
registered_asset_snapshot.HeldFile=Held;engine.WorkerCPUModel=Model
runpy.run_path(WORKER,run_name='__main__')
'''.replace('CONTROL_MODE',repr(mode)).replace('CODE',repr(str(code))).replace('WORKER',repr(str(real_worker))).replace('STDLIB',repr(str(Path(sys.base_prefix)/'lib/python3.9'))).replace('PACKAGES',repr(str(Path(sys.prefix)/'lib/python3.9/site-packages'))))
    approved=dict(asset_hashes=ASSETS,reference_adapter=REFERENCE_DECLARATION,authority_sha256=REFERENCE_AUTHORITY,timeout_seconds=10,memory_bytes=268435456,private_work_root=str(root),actor_sid='TEST_ONLY')
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
            assert mode=='cancel' and self.process in created
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
    with wave.open(out,'wb')as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(b'\0\0'*16000)
    def cancelled():return mode=='cancel' and bool(list(root.glob('multipa-*/reference_entered')))
    if mode=='natural':
        value=process.predict_in_worker('TEST_ONLY',out.getvalue(),reference=text_input('Public original reference'))
        assert value['reference_sha256']==text_input('Public original reference')['text_sha256'] and value['word_frames']==[[0,1600]]
    else:
        with pytest.raises(ValueError)as exc:
            process.predict_in_worker('TEST_ONLY',out.getvalue(),reference=text_input('Public original reference'),should_stop=cancelled)
        message=str(exc.value)
        assert message.startswith('PET_MULTIPA_') and 'Public original reference'not in message and 'PRIVATE_'not in message
        if mode=='cancel':assert message=='PET_MULTIPA_WORKER_CANCELLED'
    assert len(created)==1 and created[0].returncode==({'natural':0,'cancel':-9,'crash':73,'private_error':20}[mode]) and created[0].stdin.closed and created[0].stdout.closed
    assert not process._QUARANTINE
    assert not list(root.glob('multipa-*')) and not process._WORKER_LOCK.locked()


def test_reference_worker_rejection_is_durable_blocked_without_private_message(tmp_path,monkeypatch,public_host):
    v=make_read_aloud(tmp_path,monkeypatch,configured=True,worker_host=public_host)
    def rejected():raise ValueError(v['original']['stimulus_text']+' PRIVATE_WORKER_EXCEPTION')
    jobs,seen=install_assessor(v,monkeypatch,rejected,worker_host=public_host)
    result=jobs.process_one()
    assert result['state']=='blocked' and result['error_code']=='SOURCE_OR_WORKER_BLOCKED'
    assert seen['forward']==1 and jobs.process_one()is None
    with v['db'].connect()as c:
        job=dict(c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=?',(v['job']['job_id'],)).fetchone())
        assert v['original']['stimulus_text']not in json.dumps(job) and 'PRIVATE_WORKER_EXCEPTION'not in json.dumps(job)
        assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
    from home_study_agent.database import Database
    reopened_db=Database(v['db'].path)
    reopened=SpeakingCandidateJobs(reopened_db,AgentRuntimeStore(reopened_db),media_root=v['settings'].data_dir,data_encryption_key=v['settings'].data_encryption_key)
    assert reopened.read(CHILD,v['job']['job_id'])['state']=='blocked' and reopened.process_one()is None
