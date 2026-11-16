"""Actual original published reference/capture/Runtime/job/CAS, synthetic model only."""
import hashlib,json,math,wave
import pytest
from home_study_agent.pet_multipa_offline import SpeakingOfflineCandidateAssessor
from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_pet_multipa_reference_worker import make_read_aloud,CHILD,public_host,_base_public_host,original_engine_result_port
from test_pet_multipa_reference_segments_cpu import model_fixture


def setup_long(tmp_path,monkeypatch,on_compute=lambda:None,*,worker_host):
    import test_voice_runtime_binding as voice
    def long_public_pcm(path):
        samples=bytes().join(int(math.sin(i/16000*2*math.pi*220)*5000).to_bytes(2,'little',signed=True)for i in range(16000))
        with wave.open(str(path),'wb')as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(samples*30)
    monkeypatch.setattr(voice,'wav_file',long_public_pcm)
    v=make_read_aloud(tmp_path,monkeypatch,configured=True,segmented=True,worker_host=worker_host)
    from home_study_agent import pet_multipa_offline as offline
    model,seen=model_fixture(on_compute,dynamic_alignment=True)
    def predict(key,content,*,should_stop,reference,segmented):
        import time
        assert segmented is True and not should_stop()and reference['text']==v['original']['stimulus_text']
        path=v['settings'].data_dir/'public-owned-test.wav';path.write_bytes(content)
        try:
            value=model.predict_reference_segmented(path,reference,deadline=time.monotonic()+10);seen['numeric_result']=value;return value
        finally:path.unlink()
    original_engine_result_port(worker_host,monkeypatch,predict,segmented=True)
    runtime=AgentRuntimeStore(v['db'],speaking_candidate_assessor=SpeakingOfflineCandidateAssessor(v['db'],media_root=v['settings'].data_dir,deployment_key='REFERENCE_TEST_ONLY',data_encryption_key=v['settings'].data_encryption_key))
    jobs=SpeakingCandidateJobs(v['db'],runtime,media_root=v['settings'].data_dir,data_encryption_key=v['settings'].data_encryption_key)
    return v,jobs,seen


def test_long_original_reference_raw_segments_persist_reopen_replay_withdraw(tmp_path,monkeypatch,public_host):
    v,jobs,seen=setup_long(tmp_path,monkeypatch,worker_host=public_host)
    done=jobs.process_one();assert done['state']=='completed'
    value=jobs.runtime.speaking_candidate_assessor.store.get(CHILD,done['candidate_id']);numeric=value['raw_numeric']
    assert numeric['adapter']=='multipa_reference_aligned_segments_v1' and len(numeric['segments'])>=2
    assert 'sentence'not in numeric and 'score'not in numeric and 'pass'not in numeric
    assert not value['assessor_installed']and not value['skill_qualified']and not value['mastery_claim_allowed']
    # The complete long numeric result digest remains part of idempotency.
    with v['db'].connect()as c:action=c.execute('SELECT client_action_id FROM pet_speaking_candidate_job WHERE job_id=?',(done['job_id'],)).fetchone()[0]
    store=jobs.runtime.speaking_candidate_assessor.store
    repeat=store.record(CHILD,v['sid'],binding=value['binding'],result=seen['numeric_result'],asset_hashes=value['asset_hashes'],client_action_id=action)
    assert repeat['current']['candidate_id']==done['candidate_id']
    changed=json.loads(json.dumps(seen['numeric_result']));changed['segments'][0]['sentence']['uttr_acc']+=.01
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        store.record(CHILD,v['sid'],binding=value['binding'],result=changed,asset_hashes=value['asset_hashes'],client_action_id=action)
    count=seen['forward'];assert count==seen['ASR']and count>=2
    assert jobs.process_one()is None and seen['forward']==count
    with v['db'].connect()as c:
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
        for table in('pet_speaking_model_candidate','pet_speaking_candidate_job'):
            assert v['original']['stimulus_text']not in json.dumps([dict(r)for r in c.execute('SELECT * FROM '+table)])
    from home_study_agent.database import Database
    db=Database(v['db'].path);reopened=SpeakingCandidateJobs(db,AgentRuntimeStore(db),media_root=v['settings'].data_dir,data_encryption_key=v['settings'].data_encryption_key)
    assert reopened.read(CHILD,done['job_id'])['state']=='completed'and reopened.process_one()is None
    store=jobs.runtime.speaking_candidate_assessor.store;store.withdraw(CHILD,done['candidate_id'],expected_revision=1,client_action_id='long-reference-withdraw')
    assert store.get(CHILD,done['candidate_id'])['status']=='withdrawn'and jobs.process_one()is None


def test_changed_reference_during_segment_inference_cannot_commit(tmp_path,monkeypatch,public_host):
    holder={}
    def changed():
        if holder.get('changed'):return
        holder['changed']=True;v=holder['v']
        with v['db'].connect()as c:c.execute('UPDATE diagnostic_item SET stimulus_text=stimulus_text||? WHERE diagnostic_item_id=?',(' changed',v['item_id']));c.commit()
    v,jobs,seen=setup_long(tmp_path,monkeypatch,changed,worker_host=public_host);holder['v']=v
    result=jobs.process_one();assert result['state']=='blocked'and result['error_code']=='SOURCE_OR_WORKER_BLOCKED'
    with v['db'].connect()as c:assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==0
    assert jobs.process_one()is None


def test_cancelled_job_during_segment_compute_is_durable_and_never_commits(tmp_path,monkeypatch,public_host):
    holder={}
    def cancelled():
        if holder.get('once'):return
        holder['once']=True;holder['jobs'].cancel(CHILD,holder['v']['job']['job_id'])
    v,jobs,seen=setup_long(tmp_path,monkeypatch,cancelled,worker_host=public_host);holder.update(v=v,jobs=jobs)
    result=jobs.process_one();assert result['state']=='cancelled'
    with v['db'].connect()as c:
        assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
    count=seen['forward'];assert count>=1 and jobs.process_one()is None and seen['forward']==count
