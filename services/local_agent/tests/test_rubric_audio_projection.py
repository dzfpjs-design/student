"""Actual HTTP/SQLite/cap/signatures; synthetic native and adapter only.

External signed imports are NOT App blind-review UI or evidence of human work.
The private adapter below is never registered in production source.
"""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
import hashlib,inspect,json,time
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_evidence import digest,audio_projection_source
from home_study_agent.rubric_calibration_authority import resolve
from home_study_agent import rubric_audio_projection as audio,pet_multipa_epoch as epoch
from home_study_agent.pet_multipa_execution_receipt import install_read_scope
from home_study_agent.pet_speaking_candidate_jobs import register_new_feedback_intent as ORIGINAL_REGISTER
from home_study_agent.pet_speaking_review import prepare_speaking_audio_review,DIMENSIONS,SCHEMA as REVIEW_SCHEMA
from test_pet_multipa_execution_receipt import vertical,saved,public_host
from test_calibration_evaluator_host import signer
from test_pet_oral_supply_http import CHILD,OTHER


def private_synthetic_adapter(numeric,parameters,*,frames,task_family,lane):
    assert set(numeric['sentence'])=={'uttr_acc','uttr_fluency','uttr_prosodic','uttr_total'}
    assert parameters=={'synthetic_only':1}and task_family=='read_aloud'and lane=='short'
    return {name:dict(score=2,start_frame=0,end_frame=min(1600,frames),abstain_reason=None)for name in DIMENSIONS}


def selected(done):
    return dict(modality='original_audio',candidate_id=done['candidate_id'],job_id=done['job_id'],
                assessment_id=done['assessment_id'],media_id=done['media_id'])


def source(db,selector):
    with db.connect()as c,install_read_scope(c):
        c.execute('BEGIN');return audio_projection_source(c,CHILD,selector)


@pytest.fixture
def projected(vertical,tmp_path,monkeypatch):
    v=vertical;done,_,_=saved(v);selector=selected(done)
    service=RubricCalibrationService(v['db']);service.initialize()
    sign,now=signer(tmp_path,monkeypatch,children=[CHILD,OTHER])
    # Test-only issuer policy; importing an artifact cannot grant its own kinds.
    import os
    trust=Path(os.environ['HSC_RUBRIC_CALIBRATION_TRUST_FILE'])
    policy=json.loads(trust.read_text());policy['issuers'][0]['kinds']=sorted(audio.KINDS)
    trust.write_text(json.dumps(policy))
    original=source(v['db'],selector);profile,rubric_hash=audio.rubric_identity()
    code=hashlib.sha256(inspect.getsource(private_synthetic_adapter).encode()).hexdigest()
    params={'synthetic_only':1}
    manifest=dict(receipt_id='public-audio-projection',child_id=CHILD,status='current',schema='audio_rubric_projection_v1',
        frozen_at=(now-timedelta(minutes=10)).isoformat(),projection_version='TEST_ONLY_NOT_AN_ACOUSTIC_ALGORITHM',
        deployment_sha256=original['deployment_sha256'],asset_hashes_sha256=original['asset_hashes_sha256'],
        input_schema='pet_multipa_reference_input_v1',raw_adapter='multipa_reference_short_v1',task_family='read_aloud',length_lane='short',
        rubric_profile=profile,rubric_version=profile,rubric_hash=rubric_hash,dimensions=list(DIMENSIONS),adapter_id='PRIVATE_SYNTHETIC_TEST_ONLY',adapter_code_sha256=code,
        parameters=params,parameters_sha256=digest(params))
    settings=replace(v['settings'],maintenance_enabled=False)
    def client():
        return TestClient(create_app(settings,model_gateway=v['gateway']),headers={'X-HSC-Session':settings.session_token},client=('127.0.0.1',42021))
    def authenticate(c):
        response=c.post('/v1/parent/unlock',json={'pin':'7391'});assert response.status_code==200,response.text
        c.headers['X-HSC-Parent']=response.json()['parent_token']
    root=f'/v1/parent/children/{CHILD}/rubric-calibration'
    def imported(c,kind,payload):
        response=c.post(root+'/authority/import',json=sign(kind,payload));assert response.status_code==200,response.text
        return response.json()
    with client()as c:
        request=sign('audio_projection',manifest)
        assert c.post(root+'/authority/import',json=request).status_code==401
        authenticate(c);imported(c,'audio_projection',manifest)
    return dict(v=v,db=v['db'],selector=selector,original=original,service=service,manifest=manifest,
                sign=sign,now=now,client=client,authenticate=authenticate,imported=imported,root=root,monkeypatch=monkeypatch,code=code)


def registered(value):
    value['monkeypatch'].setitem(audio._REVIEWED_PROJECTION_ADAPTERS,'PRIVATE_SYNTHETIC_TEST_ONLY',(private_synthetic_adapter,value['code']))


def test_signed_projection_original_source_and_true_rubric_consumer_stay_partial(projected):
    x=projected;db=x['db'];selector=x['selector'];phash=digest(['PUBLIC_SYNTHETIC_NO_QUALIFICATION'])
    with db.connect()as c:
        c.execute('BEGIN')
        with pytest.raises(ValueError,match='PROJECTION_UNAVAILABLE'):
            audio.project(c,CHILD,selector,projection_receipt_id=x['manifest']['receipt_id'],protocol_hash=phash,now=x['now'])
    registered(x)
    with db.connect()as c:
        c.execute('BEGIN')
        result=audio.project(c,CHILD,selector,projection_receipt_id=x['manifest']['receipt_id'],protocol_hash=phash,now=x['now'])
        assert set(result['prediction'])==set(DIMENSIONS)and all(d['score']==2 for d in result['prediction'].values())
        assert result['score_qualified']is result['mastery_claim_allowed']is result['certification_claim_allowed']is False
        with pytest.raises(ValueError):audio.project(c,OTHER,selector,projection_receipt_id=x['manifest']['receipt_id'],protocol_hash=phash,now=x['now'])
        wrong={**selector,'job_id':'foreign-job'}
        with pytest.raises(ValueError):audio.project(c,CHILD,wrong,projection_receipt_id=x['manifest']['receipt_id'],protocol_hash=phash,now=x['now'])
        qualification=RubricEvidenceService(db).qualification(CHILD,selector['assessment_id'],connection=c)
        assert qualification['overall_audio_and_content_status']=='partial_unqualified'
        assert qualification['score_qualified']is qualification['eligible_for_mastery_gate']is False
        assert not qualification['original_audio_dimensions']['score_qualified']
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
    store=x['v']['assessor'].store
    store.withdraw(CHILD,selector['candidate_id'],expected_revision=1,client_action_id='public-projection-withdraw')
    reopened=RubricCalibrationService(Database(db.path,media_root=x['v']['settings'].data_dir,data_encryption_key=x['v']['settings'].data_encryption_key))
    with reopened.database.connect()as c:
        c.execute('BEGIN')
        with pytest.raises(ValueError):audio.project(c,CHILD,selector,projection_receipt_id=x['manifest']['receipt_id'],protocol_hash=phash,now=datetime.now(timezone.utc))


def second_original(x,tmp_path,monkeypatch,*,audio_marker=None):
    """Another real HTTP answer to the original saved/published material.

    The Runtime planning prelude is the original public fixture's setup; every
    prompt/capture/confirmation/answer response below is from the actual API.
    This avoids the singleton-count and material-authoring fixture assumptions.
    """
    from uuid import uuid4
    from home_study_agent.agent_runtime import AgentRuntimeStore,CapabilityRegistry,AgentPlanDecision,ObjectiveDraft,PlannedStep
    from home_study_agent import pet_speaking_candidate_jobs as jobs_module
    from test_voice_runtime_binding import capture,interpret,submit
    v=x['v'];db=x['db'];sid=v['sid'];runtime=AgentRuntimeStore(db)
    turn=uuid4().hex;stamp=datetime.now(timezone.utc).isoformat()
    with db.connect()as c:
        # The original child goal is durable and unique; a second genuine
        # answer reuses it instead of manufacturing another active objective.
        assert c.execute("SELECT count(*)FROM agent_objective WHERE child_id=?",(CHILD,)).fetchone()[0]==1
        c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)VALUES(?,?,?,'','start_diagnostic','','{\"type\":\"start_diagnostic\"}','pending','test',?,?)",(turn,CHILD,sid,stamp,turn))
        cap=CapabilityRegistry().require('diagnostic.start','study')
        runtime.persist_plan(c,child_id=CHILD,session_id=sid,agent_turn_id=turn,client_action_id=turn,context_manifest={},decision=AgentPlanDecision(ObjectiveDraft('pause',None,None,(),'execution'),PlannedStep(cap,{'track':'pet'},'Second original read aloud',cap.expected_evidence),'',True,1))
        c.commit()
    registered_jobs=[]
    def registered(c,*args):
        epoch._REVIEWED_DEPLOYMENTS['REFERENCE_TEST_ONLY']=v['host'].approved
        result=ORIGINAL_REGISTER(c,*args)
        if result:registered_jobs.append(result)
        return result
    with monkeypatch.context()as actual:
        # Remove only the old singleton fixture's assertion wrapper. The real
        # production registration still runs inside original completion BEGIN.
        actual.setattr(jobs_module,'register_new_feedback_intent',registered)
        if audio_marker is not None:
            import struct,wave
            import test_voice_runtime_binding as original_voice_fixture
            original_wav=original_voice_fixture.wav_file;sequence=[0]
            def distinct_original(path):
                original_wav(path)
                with wave.open(str(path),'rb')as w:params=w.getparams();pcm=w.readframes(w.getnframes())
                sequence[0]+=1
                with wave.open(str(path),'wb')as w:
                    w.setparams(params);w.writeframes(struct.pack('<h',audio_marker+sequence[0])+pcm[2:])
            actual.setattr(original_voice_fixture,'wav_file',distinct_original)
        assert set(epoch._REVIEWED_DEPLOYMENTS)=={'REFERENCE_TEST_ONLY'}
        assert epoch._REVIEWED_DEPLOYMENTS['REFERENCE_TEST_ONLY']is v['host'].approved
        with x['client']()as client:
            app_jobs=client.app.state.speaking_candidate_jobs
            from home_study_agent.pet_multipa_offline import SpeakingOfflineCandidateAssessor
            assert type(app_jobs.runtime.speaking_candidate_assessor)is SpeakingOfflineCandidateAssessor
            x['authenticate'](client)
            response=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={'track':'pet','preferred_item_id':v['item_id'],'client_action_id':'second-reference-question'})
            assert response.status_code==200,response.text
            prompt=response.json()['prompt']
            with db.connect()as c:
                runtime.complete_turn_execution(c,turn,True,None,{'execution_receipt':{'output':response.json(),'capability':cap.name,'execution_location':cap.execution_location,'action_type':'start_diagnostic','status':'completed','completed_at':stamp}})
                c.execute("UPDATE agent_turn SET tool_status='completed'WHERE agent_turn_id=?",(turn,));c.commit()
            answer='Ada visited a garden and asked her teacher for help.'
            voice=capture(client,v['settings'],actual,sid,answer,prompt,'second-original-answer')
            heard=interpret(client,sid,answer,'second-answer-turn',voice)
            assert heard['intent']=='pending_answer_clarification'
            assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code==200
            confirmation='刚才那句就是我的答案'
            confirmed=capture(client,v['settings'],actual,sid,confirmation,prompt,'second-confirmation')
            heard=interpret(client,sid,confirmation,'second-confirmation-turn',confirmed)
            assert heard['action']['source_voice_input_id']==voice and confirmed!=voice
            response=submit(client,sid,prompt,heard,answer=answer,key='second-original-submit')
            assert response.status_code==200,response.text
            assert response.json()['local_speaking_candidate']['state']=='queued'
            assert len(registered_jobs)==1
            # Keep the original App lifespan/worker alive through its actual
            # postcommit wake, claim, public child, seal and durable record.
            # Shutdown before completion would request cancellation, not prove
            # a second original execution. No second process_one is invoked.
            job_id=registered_jobs[0]['job_id'];end=time.monotonic()+5
            while True:
                view=app_jobs.read(CHILD,job_id)
                if view['state']not in{'queued','claimed'}or time.monotonic()>=end:break
                time.sleep(.02)
            assert view['state']=='completed',view
            with db.connect()as c:done=dict(c.execute('SELECT *FROM pet_speaking_candidate_job WHERE job_id=?AND child_id=?',(job_id,CHILD)).fetchone())
    assert done['assessment_id']!=x['selector']['assessment_id']and done['candidate_id']!=x['selector']['candidate_id']
    return {'prompt':prompt,'voice':voice},selected(done)


def prospective_material_holdout(x):
    projection=resolve_current(x,'audio_projection',x['manifest']['receipt_id'])
    with x['db'].connect()as c:
        grade=c.execute('SELECT grade FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]
        subject=c.execute('SELECT k.subject FROM diagnostic_item d JOIN knowledge_node k USING(knowledge_node_id)WHERE diagnostic_item_id=?',(x['original']['identity']['diagnostic_item_id'],)).fetchone()[0]
        binding=json.loads(c.execute('SELECT binding_json FROM pet_speaking_model_candidate WHERE candidate_id=?',(x['selector']['candidate_id'],)).fetchone()[0])
    scope=dict(subject=subject,grade=grade,task_family='read_aloud',strata=['PUBLIC_SYNTHETIC_NOT_A_HOLDOUT'])
    reference=binding['reference']
    slot=dict(slot_id='public-first-future-original',diagnostic_item_id=reference['diagnostic_item_id'],item_version=reference['item_version'],reference_sha256=digest(reference),source_sha256=digest(reference['source']),group_hash=digest(['PUBLIC_DISTINCT_GROUP']),stratum=scope['strata'][0],ordinal=1)
    holdout=dict(schema='audio_rubric_holdout_v2',receipt_id='public-prospective-audio-holdout',child_id=CHILD,status='current',scope_hash=digest(scope),projection_receipt_id=x['manifest']['receipt_id'],projection_sha256=digest(projection),development_group_hashes=[],slots=[slot])
    return scope,holdout,projection


def test_prospective_material_slots_bind_future_original_and_persistent_gold(projected,tmp_path,monkeypatch):
    x=projected;registered(x);scope,holdout,projection=prospective_material_holdout(x)
    # A real parent imports a fixed published-material rule BEFORE the next
    # capture/answer. No candidate/job UUID or output exists in this artifact.
    with x['client']()as c:x['authenticate'](c);x['imported'](c,'audio_holdout',holdout)
    with x['db'].connect()as c:anchor=audio._import_anchor(c,CHILD,'audio_holdout',holdout['receipt_id'])
    second,selector=second_original(x,tmp_path,monkeypatch,audio_marker=1200);fresh=source(x['db'],selector)
    with x['db'].connect()as c:
        c.execute('BEGIN')
        from home_study_agent.voice_capture_binding import capture_started_at
        assert datetime.fromisoformat(capture_started_at(c,second['voice']))>=datetime.fromisoformat(anchor['occurred_at'])
    spec=dict(schema=audio.SCORER,modality='original_audio',selector=x['selector'],projection_receipt_id=x['manifest']['receipt_id'])
    # Protocol commit must follow the read-scope close boundary. This controlled
    # exception tests transaction order; native BOOL/strong-owner behavior is
    # the existing schema92 resource tests, not reimplemented or waived here.
    from contextlib import contextmanager
    from home_study_agent import pet_multipa_execution_receipt as original_receipt
    real_scope=original_receipt.install_read_scope;depth=[0]
    @contextmanager
    def close_boundary_failure(c):
        outer=depth[0]==0;depth[0]+=1
        try:
            with real_scope(c):yield
        finally:depth[0]-=1
        if outer:raise ValueError('SYNTHETIC_READ_SCOPE_CLOSE_REJECTED')
    with monkeypatch.context()as fault:
        fault.setattr(original_receipt,'install_read_scope',close_boundary_failure)
        with pytest.raises(ValueError,match='READ_SCOPE_CLOSE_REJECTED'):
            x['service'].freeze(CHILD,scorer=spec,scope=scope,holdout_receipt_id=holdout['receipt_id'],client_action_id='public-audio-freeze')
    with x['db'].connect()as c:
        assert c.execute('SELECT count(*)FROM rubric_calibration_protocol').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM rubric_calibration_event').fetchone()[0]==0
    frozen=x['service'].freeze(CHILD,scorer=spec,scope=scope,holdout_receipt_id=holdout['receipt_id'],client_action_id='public-audio-freeze')
    current=frozen['current'];assert not current['score_qualified']and current['status']=='collecting'
    with x['db'].connect()as c:
        c.execute('BEGIN')
        packet=prepare_speaking_audio_review(c,fresh['identity']['session_id'],media_id=selector['media_id'],assessment_id=selector['assessment_id'],media_root=x['v']['settings'].data_dir,data_encryption_key=x['v']['settings'].data_encryption_key)
    review=dict(schema=REVIEW_SCHEMA,binding_version=packet['binding_version'],audio_sha256=packet['audio']['sha256'],observations=[dict(dimension_id=n,start_frame=0,end_frame=1600,heard_text='Public synthetic heard annotation',rationale='Public synthetic independent annotation')for n in DIMENSIONS])
    labels=[]
    for i in range(2):
        stamp=datetime.now(timezone.utc)
        binding=dict(protocol_hash=current['protocol_hash'],selector=selector,source_version=fresh['source_version'],execution_receipt_sha256=fresh['execution_receipt_sha256'],raw_numeric_sha256=fresh['raw_numeric_sha256'],projection_sha256=digest(projection))
        identity=dict(identity_authority='independent_reviewer_registry_v1',identity_receipt_hash=digest(['PUBLIC_IDENTITY',i]),reviewer_principal_id='EXTERNAL_SYNTHETIC_REVIEWER_'+str(i),mask_version='blind_no_predictions_or_peer_labels_v1')
        display=dict(schema='audio_blind_display_v1',receipt_id='public-display-'+str(i),child_id=CHILD,status='current',displayed_at=stamp.isoformat(),expires_at=(stamp+timedelta(hours=1)).isoformat(),**binding,**identity)
        with x['client']()as c:x['authenticate'](c);x['imported'](c,'audio_blind_display',display)
        persisted=resolve_current(x,'audio_blind_display',display['receipt_id'])
        label=dict(schema='audio_blind_label_v1',receipt_id='public-label-'+str(i),child_id=CHILD,status='current',display_receipt_id=display['receipt_id'],display_receipt_hash=digest(persisted),submitted_at=datetime.now(timezone.utc).isoformat(),submission_receipt_hash=digest(['PUBLIC_EXTERNAL_SUBMISSION',i]),review=review,scores={n:2 for n in DIMENSIONS},**binding,**identity)
        # New App/connection: display identity survives, exact replay is safe,
        # and a distinct label cannot consume the same display a second time.
        with x['client']()as c:
            x['authenticate'](c);one=x['imported'](c,'audio_blind_label',label);again=x['imported'](c,'audio_blind_label',label)
            assert one['event_id']==again['event_id']
            forged={**label,'receipt_id':'duplicate-consumption-'+str(i)}
            assert c.post(x['root']+'/authority/import',json=x['sign']('audio_blind_label',forged)).status_code==409
        labels.append(label['receipt_id'])
    recorded=x['service'].record_case(CHILD,current['protocol_id'],assessment_id=selector['assessment_id'],blind_receipt_ids=labels,expected_revision=1,client_action_id='public-audio-record-case')
    assert recorded['current']['status']=='collecting'and not recorded['current']['score_qualified']
    with x['db'].connect()as c:
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
        assert source(x['db'],selector)['source_version']==fresh['source_version']
    evaluation=x['service'].evaluate(CHILD,current['protocol_id'],expected_revision=2,client_action_id='public-too-small-holdout')
    assert evaluation['receipt']['operation']=='evaluate'and not evaluation['current']['score_qualified']
    # The one-hour display window limits SUBMISSION, not continued use of an
    # already timely immutable label under the original thirty-day protocol.
    with monkeypatch.context()as clock:
        clock.setattr(x['service'],'_now',lambda:datetime.now(timezone.utc)+timedelta(hours=2))
        later=x['service'].read(CHILD,current['protocol_id'])
        assert later['status']=='collecting'and later['revision']==3
        assert not later['score_qualified']and 'audio_source_or_authority_not_current'not in later['blockers']
    expired={**display,'receipt_id':'public-expired-display','displayed_at':frozen['receipt']['created_at'],'expires_at':(datetime.now(timezone.utc)-timedelta(microseconds=1)).isoformat()}
    with x['client']()as c:
        x['authenticate'](c)
        assert c.post(x['root']+'/authority/import',json=x['sign']('audio_blind_display',expired)).status_code==409
    assert len(x['v']['host'].created)==2 # No repeat inference for any calibration read/import.
    # Current installation is a fresh authority requirement, not a cached flag.
    approved=epoch._REVIEWED_DEPLOYMENTS.pop('REFERENCE_TEST_ONLY')
    try:
        with x['db'].connect()as c:
            c.execute('BEGIN')
            with pytest.raises(ValueError):
                audio.project(c,CHILD,selector,projection_receipt_id=x['manifest']['receipt_id'],protocol_hash=current['protocol_hash'],now=datetime.now(timezone.utc))
    finally:epoch._REVIEWED_DEPLOYMENTS['REFERENCE_TEST_ONLY']=approved
    proof=x['sign']('audio_blind_label',label)
    with x['client']()as c:
        x['authenticate'](c)
        revoked=c.post(x['root']+'/authority/revoke',json={'kind':'audio_blind_label','receipt_id':label['receipt_id'],'expected_hash':digest(proof['envelope']),'client_action_id':'public-revoke-independent-label'})
        assert revoked.status_code==200,revoked.text
    assert not x['service'].read(CHILD,current['protocol_id'])['score_qualified']
    # Original restore policy appends quarantine; it never re-signs restored
    # projection/display/labels or treats old execution metadata as current gold.
    from home_study_agent.rubric_calibration import quarantine_after_restore
    with x['db'].connect()as c:
        c.execute('BEGIN IMMEDIATE');quarantine_after_restore(c,'public-calibration-restore');c.commit()
    reopened=RubricCalibrationService(x['db'])
    view=reopened.read(CHILD,current['protocol_id'])
    assert view['status']=='quarantined'and not view['score_qualified']
    with x['db'].connect()as c:
        assert resolve(c,child_id=CHILD,kind='audio_projection',reference_id=x['manifest']['receipt_id'])is None
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
    assert len(x['v']['host'].created)==2



def test_prospective_holdout_rejects_legacy_backdating_and_existing_original(projected):
    x=projected;scope,holdout,projection=prospective_material_holdout(x)
    legacy=dict(schema='audio_rubric_holdout_v1',receipt_id='public-legacy-not-upgraded',child_id=CHILD,status='current',frozen_at=(x['now']-timedelta(days=1)).isoformat(),scope_hash=digest(scope),projection_sha256=digest(projection),development_group_hashes=[],cases=[dict(selector=x['selector'],source_version=x['original']['source_version'],execution_receipt_sha256=x['original']['execution_receipt_sha256'],group_hash=digest(['PUBLIC_LEGACY_GROUP']),stratum=scope['strata'][0])])
    spec=dict(schema=audio.SCORER,modality='original_audio',selector=x['selector'],projection_receipt_id=x['manifest']['receipt_id'])
    with x['client']()as c:
        x['authenticate'](c);x['imported'](c,'audio_holdout',legacy)
        forged={**holdout,'receipt_id':'public-forged-time','frozen_at':(x['now']-timedelta(days=1)).isoformat()}
        assert c.post(x['root']+'/authority/import',json=x['sign']('audio_holdout',forged)).status_code==409
        bad={**holdout,'receipt_id':'public-wrong-source','slots':[{**holdout['slots'][0],'source_sha256':'0'*64}]}
        assert c.post(x['root']+'/authority/import',json=x['sign']('audio_holdout',bad)).status_code==409
        cross={**holdout,'receipt_id':'public-other-child-source','child_id':OTHER}
        assert c.post(x['root'].replace(CHILD,OTHER)+'/authority/import',json=x['sign']('audio_holdout',cross)).status_code==409
        x['imported'](c,'audio_holdout',holdout)
    with pytest.raises(ValueError,match='PROSPECTIVE_HOLDOUT_REQUIRED'):
        x['service'].freeze(CHILD,scorer=spec,scope=scope,holdout_receipt_id=legacy['receipt_id'],client_action_id='legacy-never-upgraded')
    # The existing completed original predates the actual import. Backdating a
    # payload cannot recruit it, and no new child/model is run to replace it.
    with pytest.raises(ValueError,match='SLOT_NO_FUTURE_ORIGINAL'):
        x['service'].freeze(CHILD,scorer=spec,scope=scope,holdout_receipt_id=holdout['receipt_id'],client_action_id='existing-not-a-future-case')
    with x['db'].connect()as c:
        assert c.execute('SELECT count(*)FROM rubric_calibration_protocol').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM rubric_calibration_event').fetchone()[0]==0
    assert len(x['v']['host'].created)==1


def test_same_child_reuploaded_original_is_not_a_new_holdout_recording(projected,tmp_path,monkeypatch):
    x=projected;registered(x);scope,holdout,_=prospective_material_holdout(x)
    with x['client']()as c:x['authenticate'](c);x['imported'](c,'audio_holdout',holdout)
    # Original capture helper intentionally uploads the identical tone again;
    # all IDs, original HTTP confirmation, automatic host and receipt are real.
    _,selector=second_original(x,tmp_path,monkeypatch)
    assert source(x['db'],selector)['audio_sha256']==x['original']['audio_sha256']
    spec=dict(schema=audio.SCORER,modality='original_audio',selector=x['selector'],projection_receipt_id=x['manifest']['receipt_id'])
    with pytest.raises(ValueError,match='AUDIO_DUPLICATE_OR_APPLICATION_RECORDING'):
        x['service'].freeze(CHILD,scorer=spec,scope=scope,holdout_receipt_id=holdout['receipt_id'],client_action_id='same-old-recording-not-prospective')
    with x['db'].connect()as c:
        assert c.execute('SELECT count(*)FROM rubric_calibration_protocol').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM rubric_calibration_event').fetchone()[0]==0
    assert len(x['v']['host'].created)==2


def test_same_connection_audio_hash_guard_has_child_scope_and_exact_prior_anchor(projected):
    x=projected;old=x['original']['audio_sha256'];different='f'*64;another='e'*64
    with x['db'].connect()as c:
        c.execute('BEGIN')
        future={'occurred_at':(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()}
        with pytest.raises(ValueError,match='AUDIO_RECORDED_BEFORE_HOLDOUT_ANCHOR'):
            audio._unique_holdout_audio(c,CHILD,{'audio_sha256':different},[{'audio_sha256':old}],future)
        # The same SHA in this child's history is not a cross-child oracle.
        audio._unique_holdout_audio(c,OTHER,{'audio_sha256':different},[{'audio_sha256':old}],future)
        with pytest.raises(ValueError,match='AUDIO_DUPLICATE_OR_APPLICATION_RECORDING'):
            audio._unique_holdout_audio(c,CHILD,{'audio_sha256':different},[{'audio_sha256':another},{'audio_sha256':another}],future)
        with pytest.raises(ValueError,match='AUDIO_DUPLICATE_OR_APPLICATION_RECORDING'):
            audio._unique_holdout_audio(c,CHILD,{'audio_sha256':old},[{'audio_sha256':old}],future)
    assert len(x['v']['host'].created)==1


def test_fresh_distinct_recording_survives_prospective_hash_guards(projected,tmp_path,monkeypatch):
    x=projected;registered(x);scope,holdout,_=prospective_material_holdout(x)
    with x['client']()as c:x['authenticate'](c);x['imported'](c,'audio_holdout',holdout)
    _,selector=second_original(x,tmp_path,monkeypatch,audio_marker=2200)
    fresh=source(x['db'],selector);assert fresh['audio_sha256']!=x['original']['audio_sha256']
    spec=dict(schema=audio.SCORER,modality='original_audio',selector=x['selector'],projection_receipt_id=x['manifest']['receipt_id'])
    view=x['service'].freeze(CHILD,scorer=spec,scope=scope,holdout_receipt_id=holdout['receipt_id'],client_action_id='new-distinct-recording-collecting')['current']
    assert view['status']=='collecting'and not view['score_qualified']
    with x['db'].connect()as c:
        manifest=json.loads(c.execute('SELECT manifest_json FROM rubric_calibration_protocol WHERE protocol_id=?',(view['protocol_id'],)).fetchone()[0])
        assert manifest['cases'][0]['audio_sha256']==fresh['audio_sha256']
    assert len(x['v']['host'].created)==2

def resolve_current(x,kind,rid):
    with x['db'].connect()as c:
        value=resolve(c,child_id=CHILD,kind=kind,reference_id=rid);assert value is not None;return value


def test_closed_partial_audio_shape_never_becomes_whole_feedback_authority():
    assert audio._REVIEWED_PROJECTION_ADAPTERS=={}
    scoped={n:dict(score=None,start_frame=None,end_frame=None,abstain_reason='out_of_scope')for n in DIMENSIONS}
    scoped[DIMENSIONS[0]]=dict(score=2,start_frame=0,end_frame=1600,abstain_reason=None)
    output=audio.projection_output(scoped,1600,[DIMENSIONS[0]])
    assert output[DIMENSIONS[0]]['score']==2 and all(output[n]['score']is None for n in DIMENSIONS[1:])
    for broken in ([],{},True):
        rejected={n:dict(v)for n,v in scoped.items()}
        rejected[DIMENSIONS[1]]['abstain_reason']=broken
        with pytest.raises(ValueError):audio.projection_output(rejected,1600,[DIMENSIONS[0]])
    for kind in ([],{},None):
        with pytest.raises(ValueError):audio.authority_shape(kind,{})
    audio._parameters({'finite_integer':10**500}) # no float conversion overflow
    for value in (float('nan'),float('inf'),'uploaded expression'):
        with pytest.raises(ValueError):audio._parameters({'bad':value})
    service=RubricCalibrationService(None)
    row={'scorer_json':json.dumps({'schema':audio.SCORER})}
    # Even a caller's invented qualified flag cannot turn the dimension lane
    # into original model feedback/whole-score dispatch or a certificate.
    with pytest.raises(ValueError,match='NOT_FULL_SCORE_BINDING'):
        service._score_binding(None,row,'forged',datetime.now(timezone.utc),{'score_qualified':True})
    receipt=service._qualified_score_receipt(None,row,'forged',datetime.now(timezone.utc),{'score_qualified':True})
    assert receipt['score_qualified']is receipt['mastery_claim_allowed']is receipt['certification_claim_allowed']is False
