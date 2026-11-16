"""Local self-check permits supported revision, never an invented assessment."""
import hashlib
import json
from uuid import uuid4

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.database import Database
from home_study_agent.repository import utc_now
from home_study_agent.voice_control_intent import VoiceControlIntent
from home_study_agent.writing_teaching import WritingTeachingService,bind_reference_revision
from home_study_agent.writing_response_origin import bind_written_attempt
from test_writing_response_origin import writing,capture,answer,bind,CHILD,TEXT
from test_formative_evaluation_projection import objective


@pytest.fixture
def reference_revision(writing):
    db,sid,_,prompt,_=writing
    original=capture(writing)
    scored=answer(writing)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind(c,writing,original)
        c.commit()
    scope=objective(db,sid,prompt,scored['attempt'])
    runtime=AgentRuntimeStore(db)
    teaching=WritingTeachingService(db)
    teaching.initialize()
    VoiceControlIntent(db,None,None,None).initialize()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        original_plan=c.execute('SELECT plan_json FROM agent_plan WHERE objective_id=?',(scope['objective_id'],)).fetchone()
        original_binding=json.loads(original_plan[0])['local_learning_binding']
        reference=runtime.prepare_writing_reference_feedback(c,sid,prompt['prompt_id'],prompt['item_version'],original_binding,'reference')
        # Synthetic completed transport fixture; actual app callback is main-owned.
        c.execute("UPDATE agent_turn SET tool_status='completed',completed_at=? WHERE agent_turn_id=?",(utc_now(),reference))
        c.execute("UPDATE agent_plan SET status='completed' WHERE agent_turn_id=?",(reference,))
        c.execute("UPDATE agent_step SET status='completed' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(reference,))
        source=runtime.writing_reference_source(c,sid,prompt['prompt_id'],prompt['item_version'])
        c.commit()
    request,plan,step=[uuid4().hex for _ in range(3)]
    binding=dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],reference_turn_id=reference,source_version=source['source_version'])
    quote='我想修改这份作文，补上刚才漏掉的内容'
    action=dict(type='revise_writing_original',request_quote=quote,writing_revision_binding=binding,
        writing_revision_semantic_receipt={'request_key':'revise-semantic'},local_only=True,cloud_transmission_allowed=False)
    now=utc_now()
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
            orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,'revise_writing_original','',?,'fixture',?,?)""",
            (request,CHILD,sid,quote,json.dumps(action),now,request))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'fixture','{}','{}','running',?,?)",(plan,scope['objective_id'],CHILD,sid,request,now,now))
        c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?',
            (json.dumps({'writing_voice_binding':{'request_quote':quote,'revision':binding}}),plan))
        c.execute("INSERT INTO agent_step VALUES(?,?,0,'writing.revise_original','1','server','low','{}','[]','running',?,NULL,NULL,?,?)",(step,plan,step,now,now))
        fingerprint=hashlib.sha256(json.dumps([CHILD,sid,quote],ensure_ascii=False).encode()).hexdigest()
        c.execute("INSERT INTO voice_control_intent_request VALUES('revise-semantic',?,?,?,?, 'completed',?,?,?,NULL)",
            (CHILD,sid,fingerprint,json.dumps({'writing_revision':binding}),
             json.dumps({'status':'control','control':'writing.revise_original'}),now,now))
        c.commit()
    return writing,teaching,dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],reference_turn_id=reference,request_turn_id=request)


def resolve(fixture):
    writing,service,scope=fixture
    return service.resolve_reference_revision(writing[1],**scope)


def prepare_revision(fixture,*,key='revise',item=None):
    writing,_,scope=fixture
    ready=resolve(fixture)
    assert ready['status']=='ready',ready
    p=writing[3]
    # Host has connected this hook to the actual prompt creation transaction.
    return writing[2].prepare(writing[1],track='pet',preferred_item_id=item or p['diagnostic_item_id'],client_action_id=key,
        writing_reference_revision=dict(parent_prompt_id=p['prompt_id'],parent_item_version=p['item_version'],
            reference_turn_id=scope['reference_turn_id'],request_turn_id=scope['request_turn_id'],binding_version=ready['binding_version']))


def test_real_reference_revision_prepare_persists_without_assessment_and_retry_keeps_one_draft(reference_revision):
    writing,service,scope=reference_revision
    db,sid,prompts,parent,_=writing
    with db.connect() as c:
        before=c.total_changes
        resolved=service.resolve_reference_revision(sid,connection=c,**scope)
        assert resolved['status']=='ready',resolved
        assert c.total_changes==before
        assert resolved['action']['required_hint_level']==1 and resolved['action']['reference_only']
        assert not resolved['action']['mastery_claim_allowed'] and 'assessment_id' not in resolved['action']
    first=prepare_revision(reference_revision)
    assert prepare_revision(reference_revision)==first
    restarted=WritingTeachingService(Database(db.path))
    view=restarted.read(sid,prompt_id=parent['prompt_id'],item_version=parent['item_version'])
    assert view['assessment_id'] is None and view['feedback'] is None
    assert view['followups'][0]['prompt_id']==first['prompt']['prompt_id']
    assert view['followups'][0]['reference_only']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_reference_revision').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='rubric_assessment'").fetchone():
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0


@pytest.mark.parametrize('fault',['uncompleted_reference','source','semantic_owner','semantic_fingerprint','semantic_binding',
    'semantic_pending','request_quote','unadmitted','cancelled','request_target','request_before_reference','terminal'])
def test_reference_revision_needs_current_sources_and_actual_admitted_child_request(reference_revision,fault):
    writing,_,scope=reference_revision
    db=writing[0]
    with db.connect() as c:
        if fault=='uncompleted_reference':c.execute("UPDATE agent_turn SET tool_status='pending' WHERE agent_turn_id=?",(scope['reference_turn_id'],))
        elif fault=='source':c.execute("UPDATE capture_artifact SET status='withdrawn'")
        elif fault=='semantic_owner':c.execute("UPDATE voice_control_intent_request SET child_id='child-junior-3'")
        elif fault=='semantic_fingerprint':c.execute("UPDATE voice_control_intent_request SET fingerprint='wrong'")
        elif fault=='semantic_binding':c.execute("UPDATE voice_control_intent_request SET binding_json='{}'")
        elif fault=='semantic_pending':c.execute("UPDATE voice_control_intent_request SET status='pending'")
        elif fault=='request_quote':c.execute("UPDATE agent_turn SET transcript='不要修改' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        elif fault=='request_target':c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.writing_revision_binding.prompt_id','other') WHERE agent_turn_id=?",(scope['request_turn_id'],))
        elif fault=='request_before_reference':c.execute("UPDATE agent_turn SET created_at='2000-01-01T00:00:00+00:00' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        elif fault=='terminal':c.execute("UPDATE daily_session SET state='completed'")
        else:c.execute("UPDATE agent_step SET status=? WHERE capability_name='writing.revise_original'",('planned' if fault=='unadmitted' else 'cancelled',))
        c.commit()
    result=resolve(reference_revision)
    assert result['status']=='blocked',result
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_reference_revision').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1


def test_reference_cannot_dispatch_independent_new_item_or_bind_another_child(reference_revision):
    writing,service,scope=reference_revision
    with pytest.raises(ValueError):prepare_revision(reference_revision,item='diag-pet-writing-2')
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1
    result=service.resolve_reference_revision('another-session',**scope)
    assert result['status']=='blocked'


@pytest.mark.parametrize('fault',[None,'step_error','turn_error','cancelled','semantic','quote'])
def test_restart_receipt_is_historical_only_and_preserves_all_request_guards(reference_revision,fault):
    from home_study_agent.writing_teaching import _revision_request,_reference_receipt
    writing,_,scope=reference_revision
    db,sid,_,prompt,_=writing
    prepare_revision(reference_revision)
    with db.connect() as c:
        reference,source=_reference_receipt(c,sid,prompt,scope['reference_turn_id'])
        prior=_revision_request(c,sid,prompt,source,reference,scope['request_turn_id'],live=False)
        c.execute("UPDATE agent_turn SET tool_status='failed',tool_error_code='APP_RESTARTED' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        c.execute("UPDATE agent_plan SET status='failed' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        c.execute("UPDATE agent_step SET status='failed',error_code='APP_RESTARTED' WHERE capability_name='writing.revise_original'")
        if fault=='step_error':c.execute("UPDATE agent_step SET error_code='OTHER' WHERE capability_name='writing.revise_original'")
        elif fault=='turn_error':c.execute("UPDATE agent_turn SET tool_error_code='OTHER' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        elif fault=='cancelled':c.execute("UPDATE agent_step SET status='cancelled' WHERE capability_name='writing.revise_original'")
        elif fault=='semantic':c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
        elif fault=='quote':c.execute("UPDATE agent_turn SET transcript='cancel this' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        if fault:
            with pytest.raises(ValueError):_revision_request(c,sid,prompt,source,reference,scope['request_turn_id'],live=False)
        else:
            assert _revision_request(c,sid,prompt,source,reference,scope['request_turn_id'],live=False)==prior
        with pytest.raises(ValueError):_revision_request(c,sid,prompt,source,reference,scope['request_turn_id'],live=True)
        c.rollback()


def test_actual_reference_revision_answer_records_assistance_and_consumer_rejects_erasure(reference_revision):
    writing,_,_=reference_revision
    db,sid,prompts,_,originals=writing
    target=prepare_revision(reference_revision)['prompt']
    artifact=originals.capture_native_response(sid,prompt_id=target['prompt_id'],item_version=target['item_version'],text=TEXT,client_action_id='revised-text')
    result=prompts.answer(sid,prompt_id=target['prompt_id'],item_version=target['item_version'],answer=TEXT,client_action_id='revised-answer',
        hint_level=0,writing_original_artifact_id=artifact['artifact_id'])
    with db.connect() as c:
        attempt=result['attempt']
        assert c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=?',(attempt['evidence_id'],)).fetchone()[0]>=1
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE learning_evidence SET hint_level=0 WHERE evidence_id=?',(attempt['evidence_id'],))
        with pytest.raises(ValueError,match='REVISION_ASSISTANCE_MUST_BE_RECORDED'):
            bind_written_attempt(c,child_id=CHILD,session_id=sid,prompt_id=target['prompt_id'],item_version=target['item_version'],
                attempt_id=attempt['attempt_id'],original_artifact_id=artifact['artifact_id'])
        c.rollback()


@pytest.mark.parametrize('fault',[None,'no_lineage','step_only','turn_only','different_errors',
    'cancelled','source_deleted','semantic','quote','source','lineage_version','lineage_child',
    'lineage_session','new_prompt_cancelled','new_prompt_version','another_request'])
def test_storage_error_recovers_only_exact_persisted_reference_lineage(reference_revision,fault):
    from home_study_agent.writing_teaching import _revision_request,_reference_receipt
    writing,_,scope=reference_revision
    db,sid,_,prompt,_=writing
    target=prepare_revision(reference_revision)['prompt'] if fault!='no_lineage' else None
    with db.connect() as c:
        reference,source=_reference_receipt(c,sid,prompt,scope['reference_turn_id'])
        prior=_revision_request(c,sid,prompt,source,reference,scope['request_turn_id'],live=False)
        c.execute("UPDATE agent_turn SET tool_status='failed',tool_error_code='OperationalError' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        c.execute("UPDATE agent_plan SET status='failed' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        c.execute("UPDATE agent_step SET status='failed',error_code='OperationalError' WHERE capability_name='writing.revise_original'")
        if fault=='step_only':c.execute("UPDATE agent_turn SET tool_error_code=NULL WHERE agent_turn_id=?",(scope['request_turn_id'],))
        elif fault=='turn_only':c.execute("UPDATE agent_step SET error_code=NULL WHERE capability_name='writing.revise_original'")
        elif fault=='different_errors':c.execute("UPDATE agent_step SET error_code='APP_RESTARTED' WHERE capability_name='writing.revise_original'")
        elif fault=='cancelled':c.execute("UPDATE agent_step SET status='cancelled' WHERE capability_name='writing.revise_original'")
        elif fault=='source_deleted':c.execute("UPDATE agent_step SET error_code='SOURCE_DELETED' WHERE capability_name='writing.revise_original'")
        elif fault=='semantic':c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
        elif fault=='quote':c.execute("UPDATE agent_turn SET transcript='a new request' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        elif fault=='source':source={**source,'source_version':'different-source'}
        elif fault=='lineage_version':c.execute("UPDATE writing_reference_revision SET binding_json=json_set(binding_json,'$.binding_version','other')")
        elif fault=='lineage_child':c.execute("UPDATE writing_reference_revision SET child_id='child-junior-3'")
        elif fault=='lineage_session':c.execute("UPDATE writing_reference_revision SET binding_json=json_set(binding_json,'$.session_id','other')")
        elif fault=='new_prompt_cancelled':c.execute("UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?",(target['prompt_id'],))
        elif fault=='new_prompt_version':c.execute("UPDATE diagnostic_prompt SET item_version='other' WHERE prompt_id=?",(target['prompt_id'],))
        elif fault=='another_request':c.execute('UPDATE writing_reference_revision SET request_turn_id=?',(scope['reference_turn_id'],))
        if fault:
            with pytest.raises(ValueError):_revision_request(c,sid,prompt,source,reference,scope['request_turn_id'],live=False)
        else:
            assert _revision_request(c,sid,prompt,source,reference,scope['request_turn_id'],live=False)==prior
        with pytest.raises(ValueError):_revision_request(c,sid,prompt,source,reference,scope['request_turn_id'],live=True)
        c.rollback()


def test_failed_lineage_insert_rolls_back_actual_prompt_and_no_retry_score(reference_revision):
    import sqlite3
    db=reference_revision[0][0]
    with db.connect() as c:
        c.execute("CREATE TRIGGER reject_revision BEFORE INSERT ON writing_reference_revision BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError):prepare_revision(reference_revision)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM writing_reference_revision').fetchone()[0]==0
        c.execute('DROP TRIGGER reject_revision')
        c.commit()
    assert prepare_revision(reference_revision)['status']=='waiting_answer'


def test_lost_prepare_receipt_replays_original_key_after_request_completion_and_restart(reference_revision):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    writing,_,scope=reference_revision
    db,sid,_,parent,_=writing
    ready=resolve(reference_revision)
    followup=dict(parent_prompt_id=parent['prompt_id'],parent_item_version=parent['item_version'],
        reference_turn_id=scope['reference_turn_id'],request_turn_id=scope['request_turn_id'],binding_version=ready['binding_version'])
    args=dict(track='pet',preferred_item_id=parent['diagnostic_item_id'],client_action_id='lost-receipt',writing_reference_revision=followup)
    first=writing[2].prepare(sid,**args)
    with db.connect() as c:
        c.execute("UPDATE agent_step SET status='completed' WHERE capability_name='writing.revise_original'")
        c.execute("UPDATE agent_plan SET status='completed' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        c.commit()
    restarted=DiagnosticPromptService(Database(db.path))
    assert restarted.prepare(sid,**args)==first
    with pytest.raises(ValueError):
        restarted.prepare(sid,**{**args,'writing_reference_revision':{**followup,'reference_turn_id':'different'}})
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_reference_revision').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==2


@pytest.mark.parametrize('fault',[None,'ancestor_withdrawn','request_changed','lineage_missing','hint_erased','semantic_replaced'])
def test_same_goal_revision_projects_only_current_manuscript_through_real_lineage(reference_revision,fault):
    from home_study_agent.formative_evaluation_projection import build_formative_projection
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    writing,_,scope=reference_revision
    db,sid,prompts,parent,originals=writing
    target=prepare_revision(reference_revision)['prompt']
    revised=TEXT.replace('Dear Alex','Hello Pat').replace('Saturday morning','Sunday afternoon')
    artifact=originals.capture_native_response(sid,prompt_id=target['prompt_id'],item_version=target['item_version'],text=revised,client_action_id='current-original')
    scored=prompts.answer(sid,prompt_id=target['prompt_id'],item_version=target['item_version'],answer=revised,
        client_action_id='current-answer',writing_original_artifact_id=artifact['artifact_id'])
    PrivacyLifecycleService(db,db.path.parent).initialize()
    with db.connect() as c:
        oid=c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(scope['request_turn_id'],)).fetchone()[0]
        origin=c.execute("SELECT plan_json FROM agent_plan WHERE objective_id=? AND json_extract(plan_json,'$.local_learning_binding') IS NOT NULL",(oid,)).fetchone()
        binding={**json.loads(origin[0])['local_learning_binding'], 'prompt_id':target['prompt_id'],'item_version':target['item_version']}
        c.execute("UPDATE agent_plan SET status='completed',plan_json=json_set(plan_json,'$.local_learning_binding',json(?)) WHERE agent_turn_id=?",
            (json.dumps(binding),scope['request_turn_id']))
        c.execute("UPDATE agent_step SET status='completed' WHERE capability_name='writing.revise_original'")
        c.execute("UPDATE agent_turn SET tool_status='completed',completed_at=? WHERE agent_turn_id=?",(utc_now(),scope['request_turn_id']))
        c.commit()
        args=dict(child_id=CHILD,session_id=sid,prompt_id=target['prompt_id'],item_version=target['item_version'],objective_id=oid)
        result=build_formative_projection(c,**args)
        assert result['status']=='ready_for_runtime_checks',result
        assert result['payload']['response']==revised and TEXT not in json.dumps(result['payload'])
        assert not result['automatic_certification']
        if fault is None:return
        if fault=='ancestor_withdrawn':c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id!=?",(artifact['artifact_id'],))
        elif fault=='request_changed':c.execute("UPDATE agent_turn SET transcript='不要修改' WHERE agent_turn_id=?",(scope['request_turn_id'],))
        elif fault=='lineage_missing':c.execute('DELETE FROM writing_reference_revision')
        elif fault=='semantic_replaced':
            c.execute("INSERT INTO voice_control_intent_request SELECT 'replacement',child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at,execution_receipt_json FROM voice_control_intent_request WHERE request_key='revise-semantic'")
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.writing_revision_semantic_receipt.request_key','replacement') WHERE agent_turn_id=?",(scope['request_turn_id'],))
        else:c.execute('UPDATE learning_evidence SET hint_level=0 WHERE evidence_id=?',(scored['attempt']['evidence_id'],))
        assert build_formative_projection(c,**args)['status']=='local_required'
