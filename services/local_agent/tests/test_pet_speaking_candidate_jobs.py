"""Real schema90/job/DB/Runtime; learned outputs and installation are synthetic."""
import ast,hashlib,json,sqlite3,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from home_study_agent.database import Database,LATEST_SCHEMA_VERSION
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs,_Claim,quarantine_after_restore,ensure_schema
from home_study_agent import pet_multipa_epoch as epoch,pet_multipa_offline as offline
from home_study_agent.pet_speaking_candidate_store import SpeakingModelCandidateStore
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_pet_media_evidence import media,CHILD
from test_pet_speaking_review import setup
from test_pet_speaking_model_candidates import ASSETS,output
from test_pet_multipa_completion import public_host
from home_study_agent import pet_multipa_process as worker

@pytest.fixture
def owned_worker_host(public_host):
    # Real public Python child + original issuer/decoder/close ledger. Only
    # Windows installation/native resource ports are synthetic; no model claim.
    yield public_host
    epoch._REVIEWED_DEPLOYMENTS.clear()

def drop_empty_schema92_receipts(c):
 from home_study_agent.pet_multipa_execution_receipt import OBJECTS,TABLE,EVENT
 for table in(TABLE,EVENT):assert c.execute('SELECT COUNT(*)FROM '+table).fetchone()[0]==0
 for kind,name,_ in reversed(OBJECTS):c.execute('DROP '+kind.upper()+' '+name)

def _fixture_register(jobs,session_id,turn_id):
    # Synthetic pre-existing evaluator fixture only; production registration is
    # exclusively the shared original Runtime newly-completed transaction.
    from home_study_agent.pet_speaking_candidate_jobs import register_new_feedback_intent
    with jobs.database.connect()as c:
        c.execute('BEGIN IMMEDIATE')
        value=register_new_feedback_intent(c,session_id,turn_id);c.commit();return value

def prepared(media,monkeypatch,configured=True):
    _,args=setup(media);runtime=AgentRuntimeStore(media[0]);calls=[]
    if configured:
        approved=worker.configured('TEST_ONLY') # public_host's actual synthetic native ports
        approved['asset_hashes']=ASSETS
        monkeypatch.setitem(epoch._REVIEWED_DEPLOYMENTS,'TEST_ONLY',approved)
        runtime.speaking_candidate_assessor=offline.SpeakingOfflineCandidateAssessor(media[0],media_root=media[4].parent,deployment_key='TEST_ONLY')
    communicate=worker._communicate_owned
    def actual_child(*a,**kw):
        assert a[0].pid>0
        calls.append(a[0].pid) # A real original-Popen child, never a minted completion.
        return communicate(*a,**kw)
    monkeypatch.setattr(worker,'_communicate_owned',actual_child)
    jobs=SpeakingCandidateJobs(media[0],runtime,media_root=media[4].parent)
    with media[0].connect()as c:
        turn=c.execute('SELECT p.agent_turn_id FROM rubric_assessment r JOIN agent_step s USING(step_id)JOIN agent_plan p USING(plan_id)WHERE r.assessment_id=?',(args['assessment_id'],)).fetchone()[0]
    return jobs,turn,args,calls

def test_new_original_turn_enqueues_but_unknown_turn_or_session_never_does(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch)
    assert _fixture_register(jobs,media[2],'foreign')is None
    assert _fixture_register(jobs,'foreign',turn)is None
    row=_fixture_register(jobs,media[2],turn)
    assert row['state']=='queued'and calls==[]
    assert row['raw_uncalibrated']is True and row['assessor_installed']is False and row['mastery_claim_allowed']is False
    assert _fixture_register(jobs,media[2],turn)==row
    assert jobs.read(CHILD,row['job_id'])==row
    with pytest.raises(ValueError,match='NOT_FOUND'):jobs.read('other-child',row['job_id'])

def test_empty_registry_persists_blocked_and_replay_cannot_auto_activate(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch,False)
    row=_fixture_register(jobs,media[2],turn);assert row['state']=='blocked'and row['error_code']=='UNCONFIGURED'
    monkeypatch.setitem(epoch._REVIEWED_DEPLOYMENTS,'TEST_ONLY',{'asset_hashes':ASSETS})
    assert _fixture_register(jobs,media[2],turn)==row and jobs.process_one()is None and calls==[]

def test_atomic_numeric_candidate_and_job_complete_reopen_no_retry(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    original=jobs.runtime.assess_speaking_model_candidate
    def lost_ack(*a,**kw):original(*a,**kw);raise RuntimeError('SYNTHETIC_AFTER_COMMIT_ACK_LOST')
    monkeypatch.setattr(jobs.runtime,'assess_speaking_model_candidate',lost_ack)
    done=jobs.process_one();assert done['state']=='completed'and done['candidate_id']and len(calls)==1
    db=Database(media[0].path);reopened=SpeakingCandidateJobs(db,AgentRuntimeStore(db),media_root=media[4].parent)
    assert reopened.recover_interrupted()==0 and reopened.read(CHILD,row['job_id'])['state']=='completed'and reopened.process_one()is None
    assert len(calls)==1
    store=SpeakingModelCandidateStore(db,media_root=media[4].parent);store.withdraw(CHILD,done['candidate_id'],expected_revision=1,client_action_id='job-withdraw')
    assert store.get(CHILD,done['candidate_id'])['status']=='withdrawn'and reopened.process_one()is None
    with db.connect()as c:
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==1


def test_concurrent_claim_once_crash_reopen_unknown_no_automatic_compute(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    with ThreadPoolExecutor(2)as pool:claimed=list(pool.map(lambda _:jobs._claim(),range(2)))
    assert sum(x is not None for x in claimed)==1 and calls==[]
    db=Database(media[0].path);new=SpeakingCandidateJobs(db,AgentRuntimeStore(db),media_root=media[4].parent)
    assert new.recover_interrupted()==1 and new.read(CHILD,row['job_id'])['state']=='unknown'
    assert new.process_one()is None and _fixture_register(new,media[2],turn)['state']=='unknown'and calls==[]

def test_cancel_inference_cannot_commit_late_candidate(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    communicate=worker._communicate_owned
    def cancel_after_original_result(*a,**kw):
        result=communicate(*a,**kw)
        jobs.cancel(CHILD,row['job_id'])
        return result
    monkeypatch.setattr(worker,'_communicate_owned',cancel_after_original_result)
    assert jobs.process_one()['state']=='cancelled'
    with media[0].connect()as c:assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==0

def test_original_store_transaction_rechecks_claim_and_rolls_back_both(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn);claim=jobs._claim()
    from home_study_agent.pet_multipa_candidate import prepare_input
    with media[0].connect()as c:binding,_=prepare_input(c,media[2],**args)
    jobs.cancel(CHILD,row['job_id'])
    with pytest.raises(ValueError,match='CLAIM_CHANGED'):
        jobs.runtime.speaking_candidate_assessor.store.record(CHILD,media[2],binding=binding,result=output(),asset_hashes=ASSETS,client_action_id=claim['client_action_id'],job_claim=_Claim(claim))
    with media[0].connect()as c:assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==0

def test_source_and_assets_changed_before_claim_never_compute(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    monkeypatch.setitem(epoch._REVIEWED_DEPLOYMENTS,'TEST_ONLY',{'asset_hashes':ASSETS,'fixture':'CHANGED'})
    assert jobs.process_one()['state']=='blocked'and calls==[]

def test_real_89_upgrade_borrowed_ddl_rollback_and_declared_latest_missing_rejected(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch)
    with media[0].connect()as c:
        drop_empty_schema92_receipts(c)
        c.execute('DROP TABLE pet_speaking_candidate_job');c.execute('DELETE FROM schema_migration WHERE version>=90');c.commit()
        c.execute('BEGIN');ensure_schema(c);c.rollback()
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_speaking_candidate_job'").fetchone()is None
    db=Database(media[0].path);db.initialize();assert db.latest_upgrade_status()['status']=='completed'
    with db.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==LATEST_SCHEMA_VERSION
        c.execute('DROP TABLE pet_speaking_candidate_job');c.commit()
    with pytest.raises(Exception):db.initialize()
    with db.connect()as c:assert c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_speaking_candidate_job'").fetchone()is None

def test_restore_quarantine_and_original_child_delete_cascade(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    with media[0].connect()as c:c.execute('BEGIN IMMEDIATE');quarantine_after_restore(c);c.commit()
    assert jobs.read(CHILD,row['job_id'])['state']=='blocked'and jobs.process_one()is None
    privacy=PrivacyLifecycleService(media[0],media[0].path.parent)
    privacy.initialize();exported=privacy.export_child(CHILD);assert len(json.loads(Path(exported['path']).read_text())['pet_speaking_candidate_jobs'])==1
    privacy.delete_child_scope(CHILD,'learning','确认删除','delete-speaking-job')
    with media[0].connect()as c:assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==0

def test_zero_byte_regular_asset_retains_empty_sha_contract(tmp_path):
    from home_study_agent.registered_asset_snapshot import HeldFile,Policy
    p=tmp_path/'empty.py';p.write_bytes(b'')
    with HeldFile(tmp_path,'empty.py',Policy(1,64,4096,100,10,10))as held:assert held.hash()==(hashlib.sha256(b'').hexdigest(),0)
    source=Path(epoch.__file__).read_text();assert "0<=row['bytes']"in source

def test_owned_maintenance_drains_queued_job_without_creating_child_plan(media,monkeypatch,owned_worker_host):
    import asyncio
    from home_study_agent.maintenance_work import MaintenanceWork,MaintenanceStopped
    jobs,turn,args,calls=prepared(media,monkeypatch)
    row=_fixture_register(jobs,media[2],turn)
    def child_work_rows():
        with jobs.database.connect()as c:
            return {table:[tuple(r)for r in c.execute('SELECT *FROM '+table+' ORDER BY rowid')]
                    for table in('agent_plan','agent_step','agent_turn')}
    before=child_work_rows();owned=MaintenanceWork()
    async def drain():
        result=await owned.run(jobs.process_one)
        assert result['state']=='completed'and len(calls)==1
        assert owned._active==set()
        owned.request_stop()
        with pytest.raises(MaintenanceStopped):await owned.run(jobs.process_one)
        return result
    result=asyncio.run(drain())
    assert child_work_rows()==before
    assert jobs.read(CHILD,row['job_id'])==result
    assert result['raw_uncalibrated']and not result['assessor_installed']and not result['mastery_claim_allowed']
    assert len(calls)==1


from test_diagnostic_prompt_http import environment

def test_original_encrypted_audio_http_backup_restore_quarantines_queued_job(environment,monkeypatch):
    from dataclasses import replace
    from datetime import datetime,timezone
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.agent_runtime import CapabilityRegistry,AgentPlanDecision,ObjectiveDraft,PlannedStep
    from home_study_agent.pet_media_evidence import _read_audio
    from home_study_agent import pet_speaking_candidate_jobs as jobs_module
    from test_agent_continuation import Gateway
    from test_diagnostic_prompt_http import start
    from test_period_rebinding_backup_restore import restore_operation
    from test_voice_runtime_binding import capture,interpret,submit
    from synthetic_native_restore_fixture import KEY,signed_local_restore_post
    settings,_=environment
    settings=replace(settings,data_encryption_key=b'R'*32,native_launch_attestation_key=KEY)
    db=Database(settings.database_path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    gateway=Gateway({});gateway.configured=False
    monkeypatch.setitem(epoch._REVIEWED_DEPLOYMENTS,'TEST_RESTORE_ONLY',{'asset_hashes':ASSETS,'fixture':'SYNTHETIC_NO_MODEL'})
    # Only scheduling is paused; original HTTP and the shared Runtime commit
    # must create the queued intent. No local_review or manual job insertion.
    original_process=SpeakingCandidateJobs.process_one
    monkeypatch.setattr(SpeakingCandidateJobs,'process_one',lambda self,**kw:None)
    monkeypatch.setattr(offline,'predict_in_worker',lambda*a,**kw:pytest.fail('model execution in backup test'))
    registered=[];register=jobs_module.register_new_feedback_intent
    def witness(c,session_id,turn_id):
        assert c.in_transaction
        result=register(c,session_id,turn_id)
        if result:
            assert c.execute("SELECT count(*)FROM learning_evaluation_execution WHERE state='completed'").fetchone()[0]==1
            assert c.execute('SELECT count(*)FROM rubric_assessment').fetchone()[0]==1
            assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==1
            registered.append(result)
        return result
    monkeypatch.setattr(jobs_module,'register_new_feedback_intent',witness)
    with TestClient(create_app(settings,model_gateway=gateway))as client:
        sid=start(client)
        client.headers['X-HSC-Parent']=client.post('/v1/parent/setup',json={'pin':'7391'}).json()['parent_token']
        assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True}).status_code==200
        runtime=AgentRuntimeStore(db);turn=uuid4().hex;now=datetime.now(timezone.utc).isoformat()
        # The original diagnostic plan provides the actual learning goal that
        # Runtime finalization requires. Merely preparing a question does not.
        with db.connect()as c:
            c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','HTTP PET original goal',?,'diagnose','active',?,?,NULL,NULL)",
                (uuid4().hex,CHILD,sid,json.dumps(['Answer the original speaking question']),now,now))
            c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)VALUES(?,?,?,'','start_diagnostic','','{\"type\":\"start_diagnostic\"}','pending','test',?,?)",(turn,CHILD,sid,now,turn))
            cap=CapabilityRegistry().require('diagnostic.start','study')
            runtime.persist_plan(c,child_id=CHILD,session_id=sid,agent_turn_id=turn,client_action_id=turn,
                context_manifest={},decision=AgentPlanDecision(ObjectiveDraft('pause',None,None,(),'execution'),PlannedStep(cap,{'track':'pet'},'Original diagnostic',cap.expected_evidence),'',True,1));c.commit()
        response=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={
            'track':'pet','preferred_item_id':'diag-pet-speaking-2','client_action_id':'job-backup-question'})
        assert response.status_code==200,response.text
        prompt=response.json()['prompt']
        with db.connect()as c:
            runtime.complete_turn_execution(c,turn,True,None,{'execution_receipt':{'output':response.json(),
                'capability':cap.name,'execution_location':cap.execution_location,'action_type':'start_diagnostic','status':'completed','completed_at':now}})
            c.execute("UPDATE agent_turn SET tool_status='completed'WHERE agent_turn_id=?",(turn,));c.commit()
        answer='I like reading in the library because it is quiet. I read story books there after school with my friends.'
        voice=capture(client,settings,monkeypatch,sid,answer,prompt,'job-backup-original')
        public_pcm=(settings.data_dir/'synthetic-runtime.wav').read_bytes()
        heard=interpret(client,sid,answer,'job-backup-answer-turn',voice)
        assert heard['intent']=='pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code==200
        confirmation='刚才那句就是我的答案'
        confirmation_voice=capture(client,settings,monkeypatch,sid,confirmation,prompt,'job-backup-confirmation')
        heard=interpret(client,sid,confirmation,'job-backup-confirm-turn',confirmation_voice)
        assert heard['action']['source_voice_input_id']==voice and voice!=confirmation_voice
        saved=submit(client,sid,prompt,heard,answer=answer,key='job-backup-submit')
        assert saved.status_code==200,saved.text
        assert saved.json()['local_speaking_candidate']['state']=='queued'
        assert len(registered)==1 and registered[0]['state']=='queued'
        with db.connect()as c:
            before=dict(c.execute('SELECT *FROM pet_speaking_candidate_job').fetchone())
            binding=json.loads(before['binding_json'])
            assert binding['voice_input_id']==voice and binding['voice_input_id']!=confirmation_voice
            assert before['job_id']==registered[0]['job_id']and before['assessment_id']==binding['assessment_id']
            source=c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=?',(voice,)).fetchone()
            assert Path(source[0]).read_bytes().startswith(b'HSC1')
            assert _read_audio(source[0],media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)==public_pcm
            from home_study_agent.pet_multipa_candidate import prepare_input
            actual_binding,pcm=prepare_input(c,sid,media_id=before['media_id'],assessment_id=before['assessment_id'],media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
            assert actual_binding==binding and pcm==public_pcm
            assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
            assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==0
        replay=submit(client,sid,prompt,heard,answer=answer,key='job-backup-submit')
        assert replay.status_code==200 and len(registered)==1
        # Close through original session APIs, then use the existing signed
        # native restore fixture and all original publication/safety guards.
        for old,new in(('check_in','classroom_recall'),('classroom_recall','homework_capture'),
                       ('homework_capture','plan_review'),('plan_review','study'),('study','reflection')):
            if old=='classroom_recall':assert client.post(f'/v1/sessions/{sid}/recalls/none',json={'client_action_id':'job-no-recall'}).status_code==200
            if old=='homework_capture':assert client.post(f'/v1/sessions/{sid}/homework/none',json={'client_action_id':'job-no-homework'}).status_code==200
            moved=client.post(f'/v1/sessions/{sid}/transition',json={'expected_state':old,'target_state':new,
                'reason':'public synthetic restore fixture','client_action_id':'job-close-'+new})
            assert moved.status_code==200,moved.text
        closed=client.post(f'/v1/sessions/{sid}/reflection/complete',json={'learned_text':'Synthetic recording persistence only',
            'difficult_text':'','feeling':'okay','next_action':'','skipped':False,'client_action_id':'job-finish'})
        assert closed.status_code==200,closed.text
        backup=client.post('/v1/parent/backups');assert backup.status_code==200,backup.text
        restored=signed_local_restore_post(client,json={'backup_id':backup.json()['backup_id'],
            'confirmation':'确认恢复','operation':restore_operation(client,{})})
        assert restored.status_code==200,restored.text
        assert restored.json()['status']=='restored'
    reopened=Database(settings.database_path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    with TestClient(create_app(settings,model_gateway=gateway))as client:
        unlocked=client.post('/v1/parent/unlock',json={'pin':'7391'});assert unlocked.status_code==200
        client.headers['X-HSC-Parent']=unlocked.json()['parent_token']
        with reopened.connect()as c:
            rows=c.execute('SELECT *FROM pet_speaking_candidate_job').fetchall();assert len(rows)==1
            after=dict(rows[0]);assert after['job_id']==before['job_id']
            assert after['state']=='blocked'and after['error_code']=='RESTORED_NO_AUTORUN'
            assert after['owner_token']is None and after['owner_epoch']is None
            assert all(after[k]==v for k,v in before.items()if k not in{'state','error_code','updated_at'})
            assert not c.execute("SELECT 1 FROM pet_speaking_candidate_job WHERE state IN('queued','claimed')").fetchone()
            raw=c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=?AND child_id=?AND session_id=?',(voice,CHILD,sid)).fetchone()
            assert raw and _read_audio(raw[0],media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)==public_pcm
            capture_row=c.execute('SELECT status,wav_sha256,question_validation FROM voice_capture_binding WHERE voice_input_id=?',(voice,)).fetchone()
            assert tuple(capture_row)==('cancelled',hashlib.sha256(public_pcm).hexdigest(),'valid')
            assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==LATEST_SCHEMA_VERSION
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
            assert c.execute('SELECT count(*)FROM rubric_assessment').fetchone()[0]==1
            assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
            assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==0
            assert c.execute('SELECT count(*)FROM model_call').fetchone()[0]==0
        jobs=SpeakingCandidateJobs(reopened,AgentRuntimeStore(reopened),media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
        assert original_process(jobs)is None
    assert len(registered)==1 and not gateway.requests

def test_release_smoke_exact_current_schema_both_runtime_and_upgrade():
    import re
    # A source overlay may supply the candidate publication script explicitly.
    root=Path(__file__).resolve().parents[1]
    ps=(root/'candidate/scripts/smoke-windows-installer.ps1').read_text()if(root/'candidate').exists()else(Path(__file__).resolve().parents[3]/'scripts/smoke-windows-installer.ps1').read_text()
    assert LATEST_SCHEMA_VERSION==95
    assert re.findall(r'\$(?:runtimeLines\[1\]|upgradeLines\[0\]) -ne "(\d+)"',ps)==[str(LATEST_SCHEMA_VERSION)]*2
    assert f'$upgradeMetadata.target_schema_version -ne {LATEST_SCHEMA_VERSION}' in ps and f'47-to-{LATEST_SCHEMA_VERSION} migration' in ps
    assert '47-to-89 migration'not in ps
    assert ps.count('document_observation_segment') >= 2
    assert ps.count('daily_observation_report_snapshot') >= 2 and '$upgradeLines[10] -ne "1"' in ps
    assert '$runtimeLines[2] -ne "10"' in ps and '$upgradeLines[9] -ne "1"' in ps
    agent_ps=(Path(__file__).resolve().parents[3]/'scripts/smoke-windows-agent.ps1').read_text()
    assert f'$schemaVersion.Trim() -ne "{LATEST_SCHEMA_VERSION}"' in agent_ps
    assert "'document_observation_segment'" in agent_ps
    assert "'daily_observation_report_snapshot'" in agent_ps


def test_registration_metadata_never_decrypts_original_or_accepts_non_speaking(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch)
    import home_study_agent.pet_media_evidence as review
    monkeypatch.setattr(review,'_read_audio',lambda *a,**kw:pytest.fail('raw audio read during registration'))
    assert _fixture_register(jobs,media[2],turn)['state']=='queued'
    with media[0].connect()as c:
        # Real existing assessment, now non-speaking item: the metadata gate
        # must return no job before consulting audio, store or model.
        c.execute("UPDATE diagnostic_item SET answer_type='short_text'WHERE diagnostic_item_id=(SELECT diagnostic_item_id FROM pet_media_binding WHERE media_id=?)",(args['media_id'],));c.commit()
    assert _fixture_register(jobs,media[2],turn)is None and calls==[]


def test_duplicate_same_original_voice_binding_is_ambiguous_not_text_selected(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch)
    with media[0].connect()as c:
        row=dict(c.execute('SELECT * FROM pet_media_binding WHERE media_id=?',(args['media_id'],)).fetchone());row['media_id']='synthetic-duplicate-same-voice'
        c.execute('INSERT INTO pet_media_binding('+','.join(row)+')VALUES('+','.join('?'for _ in row)+')',tuple(row.values()));c.commit()
    with pytest.raises(ValueError,match='AMBIGUOUS_ORIGINAL'):_fixture_register(jobs,media[2],turn)
    assert calls==[]
    with media[0].connect()as c:assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==0


def test_source_revised_before_worker_never_runs(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    with media[0].connect()as c:c.execute("UPDATE voice_input SET transcript='public synthetic revised source'");c.commit()
    assert jobs.process_one()['state']=='blocked'and calls==[]

def test_record_and_job_ack_failure_roll_back_one_transaction(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    import home_study_agent.pet_speaking_candidate_jobs as module
    def failed(*a,**kw):raise ValueError('SYNTHETIC_JOB_ACK_FAIL')
    monkeypatch.setattr(module,'complete_claim',failed)
    assert jobs.process_one()['state']=='blocked'and len(calls)==1
    with media[0].connect()as c:
        assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM pet_speaking_model_candidate_event').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM pet_multipa_execution_receipt').fetchone()[0]==0

def test_unknown_worker_drain_preserves_new_jobs_queued(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    from home_study_agent.pet_multipa_process import _WORKER_LOCK
    assert _WORKER_LOCK.acquire(blocking=False)
    try:assert jobs.process_one()is None and jobs.read(CHILD,row['job_id'])['state']=='queued'and calls==[]
    finally:_WORKER_LOCK.release()


def test_schema90_keeps_original_asset_closure_legacy_floor_89():
    import home_study_agent.parent_restore_assets as assets
    tree=ast.parse(Path(assets.__file__).read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='assert_assets_closed_before_native')
    expressions=[n for n in ast.walk(fn)if isinstance(n,ast.Compare)and isinstance(n.left,ast.Name)and n.left.id=='version']
    assert len(expressions)==1
    condition=compile(ast.Expression(expressions[0]),'<original-fixed-closure-floor>','eval')
    assert [eval(condition,{},dict(version=v))for v in(47,88,89,90)]==[True,True,False,False]


# Fresh original voice capture -> original planner/capability execution. This
# creates no candidate assessment outside original Runtime finalization.
def _atomic_voice_saved(media):
    import asyncio
    from uuid import uuid4
    from datetime import datetime,timezone,timedelta
    from home_study_agent.agent_runtime import CapabilityRegistry,AgentPlanDecision,ObjectiveDraft,PlannedStep
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from test_agent_continuation import orchestrator_for
    from test_pet_media_evidence import speaking,bind_speaking
    db=media[0]
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
    RubricEvidenceService(db).initialize();LearningOutcomeEvaluator(db,None,None,None)
    runtime=AgentRuntimeStore(db);turn=uuid4().hex;now=datetime.now(timezone.utc).isoformat()
    # Existing learning-goal setup uses original persist_plan and completion,
    # not a hand-written local_learning_binding/assessment/execution row.
    with db.connect()as c:
        c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','PET original goal',?,'diagnose','active',?,?,NULL,NULL)",
                  (uuid4().hex,CHILD,media[2],json.dumps(['Answer the original speaking question']),now,now))
        c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)VALUES(?,?,?,'','start_diagnostic','','{\"type\":\"start_diagnostic\"}','pending','test',?,?)",(turn,CHILD,media[2],now,turn))
        cap=CapabilityRegistry().require('diagnostic.start','study')
        runtime.persist_plan(c,child_id=CHILD,session_id=media[2],agent_turn_id=turn,client_action_id=turn,
            context_manifest={},decision=AgentPlanDecision(ObjectiveDraft('pause',None,None,(),'execution'),PlannedStep(cap,{'track':'pet'},'Original diagnostic',cap.expected_evidence),'',True,1));c.commit()
    prompt,answer=speaking(media)
    with db.connect()as c:
        runtime.complete_turn_execution(c,turn,True,None,{'execution_receipt':{'output':{'prompt':prompt},'capability':cap.name,'execution_location':cap.execution_location,'action_type':'start_diagnostic','status':'completed','completed_at':now}})
        c.execute("UPDATE agent_turn SET tool_status='completed'WHERE agent_turn_id=?",(turn,));c.commit()
    bind_speaking(media,prompt)
    agent,gateway=orchestrator_for(db,media[4].parent,{})
    heard=asyncio.run(agent.interpret(media[2],answer,'atomic-original-turn',False,voice_input_id='voice'))
    executor=AgentCapabilityExecutor(db,__import__('home_study_agent.repository',fromlist=['StudyRepository']).StudyRepository(db))
    if heard['intent']=='pending_answer_clarification':
        receipt=executor.execute(heard['agent_turn_id']);agent.complete_turn(heard['agent_turn_id'],receipt['response_text'],'completed',None,receipt)
        heard=asyncio.run(agent.interpret(media[2],'刚才那句就是我的答案','atomic-confirm-turn',False))
    assert heard['action'].get('source_voice_input_id')=='voice'and not gateway.requests
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    saved=DiagnosticPromptService(db).answer(media[2],prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer=answer,client_action_id='atomic-answer',authorized_turn_id=heard['agent_turn_id'])
    receipt={'result':saved,'execution_receipt':{'output':saved,'capability':'diagnostic.submit','execution_location':'client_device_bridge','action_type':heard['action']['type'],'status':'completed','completed_at':datetime.now(timezone.utc).isoformat()}}
    with db.connect()as c:
        runtime.complete_turn_execution(c,heard['agent_turn_id'],True,None,receipt);c.commit()
        attempt=c.execute('SELECT a.evidence_id FROM diagnostic_prompt q JOIN diagnostic_attempt a ON a.attempt_id=q.attempt_id WHERE q.prompt_id=?',(prompt['prompt_id'],)).fetchone()
        assert attempt
        assert c.execute('SELECT count(*)FROM rubric_assessment').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==0
    return runtime,attempt[0],agent,heard,receipt


def _crash_original_completion(media,when):
    import subprocess,sys,os
    runtime,evidence,agent,heard,receipt=_atomic_voice_saved(media)
    paths=list(__import__('home_study_agent').__path__)
    code="""
import sys,os,socket
socket.socket.connect=lambda*a,**k:(_ for _ in()).throw(RuntimeError('TEST_NETWORK_FORBIDDEN'))
import home_study_agent
home_study_agent.__path__[:]=__import__('json').loads(sys.argv[1])
from home_study_agent.database import Database
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent import pet_speaking_candidate_jobs as jobs,pet_multipa_epoch as epoch
ASSET_KEYS=('multipa','charsiu','hubert','roberta','whisper_medium_en','whisper_base_en')
epoch._REVIEWED_DEPLOYMENTS['SYNTHETIC_TEST_ONLY']={'asset_hashes':{k:'a'*64 for k in ASSET_KEYS}}
if sys.argv[5]=='before_commit':
 original=jobs.register_new_feedback_intent
 def crash(c,*args):
  value=original(c,*args)
  assert value and value['state']=='queued' and c.in_transaction
  os._exit(73)
 jobs.register_new_feedback_intent=crash
value=AgentRuntimeStore(Database(sys.argv[2])).finalize_diagnostic_evaluation(sys.argv[3],sys.argv[4])
assert value['local_speaking_candidate']['state']=='queued'
os._exit(74)
"""
    process=subprocess.Popen([sys.executable,'-c',code,json.dumps(paths),str(media[0].path),media[2],evidence,when],env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    out,err=process.communicate(timeout=30)
    assert out==b''and err==b'',err.decode()[:600]
    assert process.returncode==(73 if when=='before_commit'else 74)
    return runtime,evidence


def test_original_completion_crash_before_commit_rolls_back_evaluation_and_intent(media):
    runtime,evidence=_crash_original_completion(media,'before_commit')
    with media[0].connect()as c:
        assert c.execute('SELECT count(*)FROM diagnostic_attempt').fetchone()[0]==1
        assert c.execute('SELECT count(*)FROM rubric_assessment').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM learning_evaluation_execution').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==0
    result=runtime.finalize_diagnostic_evaluation(media[2],evidence)
    assert result['local_speaking_candidate']['state']=='blocked'
    with media[0].connect()as c:
        assert c.execute("SELECT count(*)FROM learning_evaluation_execution WHERE state='completed'").fetchone()[0]==1
        assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==1


def test_original_completion_crash_after_commit_before_wake_keeps_exact_job(media):
    runtime,evidence=_crash_original_completion(media,'after_commit')
    reopened=Database(media[0].path);jobs=SpeakingCandidateJobs(reopened,AgentRuntimeStore(reopened),media_root=media[4].parent)
    assert jobs.recover_interrupted()==0
    with reopened.connect()as c:
        before=dict(c.execute('SELECT *FROM pet_speaking_candidate_job').fetchone())
        assert before['state']=='queued'
        assert c.execute("SELECT count(*)FROM learning_evaluation_execution WHERE state='completed'").fetchone()[0]==1
    result=jobs.runtime.finalize_diagnostic_evaluation(media[2],evidence)
    assert result['local_speaking_candidate']['job_id']==before['job_id']
    with reopened.connect()as c:assert dict(c.execute('SELECT *FROM pet_speaking_candidate_job').fetchone())==before


def test_canonical_voice_finalization_uses_same_atomic_registration_without_app(media,monkeypatch):
    from home_study_agent import pet_speaking_candidate_jobs as jm
    runtime,evidence,agent,heard,receipt=_atomic_voice_saved(media);observed=[];original=jm.register_new_feedback_intent
    def witness(c,*args):
        assert c.in_transaction
        answer=original(c,*args);observed.append(answer);return answer
    monkeypatch.setattr(jm,'register_new_feedback_intent',witness)
    result=runtime.finalize_diagnostic_turn(heard['agent_turn_id'])
    assert result['local_speaking_candidate']['state']=='blocked'
    assert len(observed)==1 and observed[0]['state']=='blocked'and observed[0]['error_code']=='UNCONFIGURED'
    with media[0].connect()as c:
        assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==1
        assert c.execute('SELECT count(*)FROM rubric_assessment').fetchone()[0]==1
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
    runtime.recover_learning_outcomes()
    assert len(observed)==1 # completed replay/startup does not register again


def test_completed_history_is_not_backfilled_when_original_intent_absent(media,monkeypatch):
    from home_study_agent import pet_speaking_candidate_jobs as jm
    runtime,evidence,agent,heard,receipt=_atomic_voice_saved(media)
    # Simulate a pre-upgrade completed assessment: original result commits, but
    # old software had no intent. Installing/recovery must not scan it in.
    original=jm.register_new_feedback_intent
    monkeypatch.setattr(jm,'register_new_feedback_intent',lambda*c:None)
    runtime.finalize_diagnostic_evaluation(media[2],evidence)
    monkeypatch.setattr(jm,'register_new_feedback_intent',lambda*c:(_ for _ in()).throw(AssertionError('HISTORY_BACKFILL')))
    assert 'local_speaking_candidate'not in runtime.finalize_diagnostic_evaluation(media[2],evidence)
    runtime.recover_learning_outcomes()
    with media[0].connect()as c:assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==0


def test_real_http_voice_completion_intent_commits_before_postcommit_wake(environment,monkeypatch):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from datetime import datetime,timezone
    from uuid import uuid4
    from home_study_agent.app import create_app
    from home_study_agent.agent_runtime import CapabilityRegistry,AgentPlanDecision,ObjectiveDraft,PlannedStep
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent import pet_speaking_candidate_jobs as jm
    from test_agent_continuation import Gateway
    from test_diagnostic_prompt_http import start
    from test_voice_runtime_binding import capture,interpret,submit
    settings,_=environment;settings=replace(settings,data_encryption_key=b'R'*32)
    db=Database(settings.database_path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    original=jm.register_new_feedback_intent;seen=[]
    def witness(c,*args):
        assert c.in_transaction
        value=original(c,*args)
        if value:seen.append(value)
        return value
    monkeypatch.setattr(jm,'register_new_feedback_intent',witness)
    monkeypatch.setattr(SpeakingCandidateJobs,'process_one',lambda self,**kw:None)
    gateway=Gateway({});gateway.configured=False
    with TestClient(create_app(settings,model_gateway=gateway))as client:
        sid=start(client)
        client.headers['X-HSC-Parent']=client.post('/v1/parent/setup',json={'pin':'7391'}).json()['parent_token']
        assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True}).status_code==200
        runtime=AgentRuntimeStore(db);turn=uuid4().hex;now=datetime.now(timezone.utc).isoformat()
        with db.connect()as c:
            c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','HTTP PET original goal',?,'diagnose','active',?,?,NULL,NULL)",(uuid4().hex,CHILD,sid,json.dumps(['Answer the original speaking question']),now,now))
            c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)VALUES(?,?,?,'','start_diagnostic','','{\"type\":\"start_diagnostic\"}','pending','test',?,?)",(turn,CHILD,sid,now,turn))
            cap=CapabilityRegistry().require('diagnostic.start','study')
            runtime.persist_plan(c,child_id=CHILD,session_id=sid,agent_turn_id=turn,client_action_id=turn,context_manifest={},decision=AgentPlanDecision(ObjectiveDraft('pause',None,None,(),'execution'),PlannedStep(cap,{'track':'pet'},'Original diagnostic',cap.expected_evidence),'',True,1));c.commit()
        response=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={'track':'pet','preferred_item_id':'diag-pet-speaking-2','client_action_id':'atomic-http-question'})
        assert response.status_code==200,response.text
        prompt=response.json()['prompt']
        with db.connect()as c:
            runtime.complete_turn_execution(c,turn,True,None,{'execution_receipt':{'output':response.json(),'capability':cap.name,'execution_location':cap.execution_location,'action_type':'start_diagnostic','status':'completed','completed_at':now}})
            c.execute("UPDATE agent_turn SET tool_status='completed'WHERE agent_turn_id=?",(turn,));c.commit()
        answer='I like reading in the library because it is quiet. I read story books there after school with my friends.'
        voice=capture(client,settings,monkeypatch,sid,answer,prompt,'atomic-http-original')
        heard=interpret(client,sid,answer,'atomic-http-answer-turn',voice)
        assert heard['intent']=='pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code==200
        # Same words twice still select the original capture through the exact
        # stored source_voice_input_id, never by transcript or array order.
        confirm='刚才那句就是我的答案'
        confirmation=capture(client,settings,monkeypatch,sid,confirm,prompt,'atomic-http-confirmation')
        heard=interpret(client,sid,confirm,'atomic-http-confirm-turn',confirmation)
        assert heard['action']['source_voice_input_id']==voice
        saved=submit(client,sid,prompt,heard,answer=answer,key='atomic-http-submit')
        assert saved.status_code==200,saved.text
        assert 'local_speaking_candidate'in saved.json(),saved.json().get('local_learning_evaluation')
        assert saved.json()['local_speaking_candidate']['state']=='blocked'
        assert len(seen)==1 and seen[0]['error_code']=='UNCONFIGURED'
        with db.connect()as c:
            row=dict(c.execute('SELECT *FROM pet_speaking_candidate_job').fetchone())
            original_binding=json.loads(row['binding_json'])
            assert original_binding['voice_input_id']==voice and original_binding['voice_input_id']!=confirmation
            from home_study_agent.pet_multipa_candidate import prepare_input
            binding,wav=prepare_input(c,sid,media_id=row['media_id'],assessment_id=row['assessment_id'],media_root=settings.data_dir,data_encryption_key=b'R'*32)
            assert binding==original_binding and hashlib.sha256(wav).hexdigest()==binding['audio']['sha256']
            assert c.execute('SELECT count(*)FROM learning_evaluation_execution WHERE state=\'completed\'').fetchone()[0]==1
            assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
        # Original HTTP replay returns this same committed blocked intent.
        replay=submit(client,sid,prompt,heard,answer=answer,key='atomic-http-submit')
        assert replay.status_code==200 and len(seen)==1
    with db.connect()as c:assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==1


def test_original_shared_completion_must_persist_intent_before_return(media):
    runtime,evidence,agent,heard,receipt=_atomic_voice_saved(media)
    result=runtime.finalize_diagnostic_turn(heard['agent_turn_id'])
    assert result['status']=='feedback_ready'
    with media[0].connect()as c:
        assert c.execute("SELECT count(*)FROM learning_evaluation_execution WHERE state='completed'").fetchone()[0]==1
        assert c.execute('SELECT count(*)FROM rubric_assessment').fetchone()[0]==1
        assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==1
