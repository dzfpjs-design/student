import json
import pytest
from test_ordinary_learning_capture_reference import env,current
from test_ordinary_learning_capture_freshness import completed_capture,mutate,TEXT
from home_study_agent.agent_runtime import AgentRuntimeStore,CapabilityRegistry,ObjectiveDraft,PlannedStep,AgentPlanDecision,InvalidAgentPlanError
from home_study_agent.canonical_voice_execution import inspect as canonical_inspect
from home_study_agent.learning_reply_reservation import KEY,validate,validate_model_work
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator,InvalidLearningEvaluationError
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService,VoiceCaptureBindingError

def reserve(env,capability='learning.evaluate_response',mode='continue'):
    db,store,old=completed_capture(env)
    action={'type':'evaluate_child_response','voice_input_id':old['voice_input_id'],
        'voice_input_binding':store.voice_capture_snapshot(old),'local_only':True,'cloud_transmission_allowed':False}
    decision=AgentPlanDecision(ObjectiveDraft(mode,'learning','POST_CONSUMPTION_GOAL',('POST_CRITERION',),'check'),
        PlannedStep(CapabilityRegistry().require(capability,'study'),
            ({'response':TEXT,'subject':'数学'} if capability=='learning.evaluate_response' else {'prompt':'Explain the key point in your own words','subject':'数学'} if capability=='learning.request_response' else {'question':'New public synthetic topic','subject':'数学'}),'public synthetic current-source test',('independent_response',)),
        'Synthetic evaluation pending',True,1.0)
    manifest={'session_id':'s','current_state':'study',KEY:{'caller_supplied':True}}
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,tool_status,created_at,client_action_id) VALUES('queued-answer','c','s',?,'independent_response','Synthetic',?,'test','pending','2026-10-01',?)",(TEXT,json.dumps(action),'realtime-'+old['voice_input_id']))
        metadata=store.persist_plan(c,child_id='c',session_id='s',agent_turn_id='queued-answer',client_action_id='realtime-'+old['voice_input_id'],decision=decision,context_manifest=manifest)
        c.commit()
    assert KEY not in manifest # private data never escapes through caller's original context object
    return db,store,old,metadata

def test_trusted_consumption_allows_canonical_and_original_answer_metadata_but_no_new_capture(env):
    db,store,old,meta=reserve(env)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert validate(c,'queued-answer')==old['question_reference']
        assert canonical_inspect(c,'queued-answer') is True
        resolved=store.resolve_local_answer_submission(c,'queued-answer','learning.evaluate_response')
        assert resolved['voice_input_id']==old['voice_input_id']
        LearningOutcomeEvaluator._assert_learning_source(c,meta)
        saved=json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?',(meta['plan_id'],)).fetchone()[0])[KEY]
        assert saved['source_goal']['summary']=='Synthetic objective'
        assert saved['objective_after']['summary']=='POST_CONSUMPTION_GOAL'
        public=store.pending_learning_capture(c,'s','c')
        assert public is None
    with pytest.raises(VoiceCaptureBindingError):
        VoiceCaptureBindingService(db).begin_realtime('s','cannot-borrow-reservation','agent_command',{'child_id':'c','question_reference':old['question_reference']},model_version='TEST_ONLY',captured_at_ms=None)
    assert KEY not in json.dumps(store.status('s'))

@pytest.mark.parametrize('change',[
    "UPDATE agent_step SET result_json='{\"changed\":true}' WHERE step_id='step'",
    "UPDATE agent_step SET arguments_json='{\"response\":\"CHANGED\",\"subject\":\"数学\"}' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id='queued-answer')",
    "UPDATE agent_objective SET summary='OTHER_GOAL' WHERE objective_id='o'",
    "UPDATE agent_objective SET status='completed' WHERE objective_id='o'",
    "UPDATE child_profile SET status='archived' WHERE child_id='c'",
    "UPDATE voice_capture_binding SET status='cancelled' WHERE client_action_id='queued-capture-source'",
    "UPDATE agent_plan SET status='completed' WHERE agent_turn_id='queued-answer'",
])
def test_claim_outbound_and_commit_same_private_guard_reject_changed_scope(env,change):
    db,store,old,meta=reserve(env)
    with db.connect() as c:
        c.execute(change);c.commit()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(InvalidAgentPlanError):validate(c,'queued-answer')
        with pytest.raises(InvalidLearningEvaluationError):LearningOutcomeEvaluator._assert_learning_source(c,meta)

def test_forged_private_manifest_and_cross_scope_are_rejected(env):
    db,store,old,meta=reserve(env)
    with db.connect() as c:
        value=json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?',(meta['plan_id'],)).fetchone()[0])
        original=json.loads(json.dumps(value))
        value[KEY]['child_id']='other'
        c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?',(json.dumps(value),meta['plan_id']));c.commit()
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError):validate(c,'queued-answer')
    original[KEY]['source_goal']['summary']='FORGED_SOURCE_GOAL'
    with db.connect() as c:
        c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?',(json.dumps(original),meta['plan_id']));c.commit()
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError):validate(c,'queued-answer')

@pytest.mark.parametrize('mode',['continue','new'])
def test_explain_consumes_only_its_own_same_or_new_goal_without_grading_permission(env,mode):
    db,store,old,meta=reserve(env,'learning.explain',mode)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert canonical_inspect(c,'queued-answer') is True
        item=json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?',(meta['plan_id'],)).fetchone()[0])[KEY]
        assert item['goal_transition']==('same_goal' if mode=='continue' else 'new_explain')
        assert (item['objective_after']['objective_id']==item['work_objective_after']['objective_id'])==(mode=='continue')
        with pytest.raises(InvalidLearningEvaluationError):LearningOutcomeEvaluator._assert_learning_source(c,meta)

def test_evaluation_cannot_move_a_captured_answer_to_another_goal(env):
    with pytest.raises(InvalidAgentPlanError):reserve(env,'learning.evaluate_response','new')

def test_owned_work_child_corruption_cannot_fall_back_to_legacy(env):
    db,store,old,meta=reserve(env)
    with db.connect() as c:
        c.execute("INSERT INTO child_profile SELECT 'other','Other synthetic',grade,curriculum_region,status,created_at,updated_at FROM child_profile WHERE child_id='c'")
        c.execute("UPDATE agent_plan SET child_id='other' WHERE plan_id=?",(meta['plan_id'],));c.commit()
    with db.connect() as c:
        with pytest.raises(InvalidLearningEvaluationError):LearningOutcomeEvaluator._assert_learning_source(c,meta)

def test_plain_candidate_conversation_keeps_normal_fresh_path_without_private_consumption(env):
    db,store,old=completed_capture(env)
    action={'type':'none','source':'pending_answer_clarification','voice_input_id':old['voice_input_id'],
        'voice_input_binding':store.voice_capture_snapshot(old),'local_only':True,'cloud_transmission_allowed':False}
    decision=AgentPlanDecision(ObjectiveDraft('none',None,None,(),'clarify'),
        PlannedStep(CapabilityRegistry().require('conversation.respond','study'),{'response':'Confirm public synthetic answer'},'clarify',()),
        'Confirm public synthetic answer',True,1.0)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,tool_status,created_at,client_action_id) VALUES('candidate','c','s',?,'pending_answer_clarification','Synthetic',?,'test','pending','2026-10-01',?)",(TEXT,json.dumps(action),'realtime-'+old['voice_input_id']))
        meta=store.persist_plan(c,child_id='c',session_id='s',agent_turn_id='candidate',client_action_id='realtime-'+old['voice_input_id'],decision=decision,context_manifest={KEY:{'forged':True}})
        assert KEY not in json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?',(meta['plan_id'],)).fetchone()[0])
        assert canonical_inspect(c,'candidate') is True

@pytest.mark.parametrize('field',['reference','work_objective_after'])
def test_malformed_private_proof_cannot_fall_back_to_valid_capture(env,field):
    db,store,old,meta=reserve(env)
    with db.connect() as c:
        data=json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?',(meta['plan_id'],)).fetchone()[0]);data[KEY][field]=None
        c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?',(json.dumps(data),meta['plan_id']));c.commit()
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError):validate(c,'queued-answer')
        result=store.resolve_captured_voice(c,session_id='s',child_id='c',voice_input_id=old['voice_input_id'],transcript=TEXT,_execution_turn_id='queued-answer')
        assert result['question_validation']=='stale'

def test_completed_claim_uses_original_receipt_without_new_consumption_permission(env,monkeypatch):
    db,store,old,meta=reserve(env)
    evaluator=LearningOutcomeEvaluator(db,None,None,None)
    with db.connect() as c:
        c.execute("UPDATE agent_step SET status='completed' WHERE step_id=?",(meta['step_id'],))
        c.execute("UPDATE agent_plan SET status='waiting_child' WHERE plan_id=?",(meta['plan_id'],))
        c.execute("UPDATE agent_turn SET tool_status='completed' WHERE agent_turn_id='queued-answer'")
        c.execute("INSERT INTO learning_evaluation_execution(step_id,state,owner_token,claimed_at,record_json,result_json) VALUES(?,'completed','TEST_ONLY','2026-01-01','{}','{\"stored_public_synthetic_receipt\":true}')",(meta['step_id'],));c.commit()
    def no_new_admission(*args):raise AssertionError('COMPLETED_RECEIPT_MUST_NOT_READMIT')
    monkeypatch.setattr(evaluator,'_assert_cloud_transmission',no_new_admission)
    claim=evaluator._claim(meta)
    assert claim['state']=='completed'
    assert json.loads(claim['result_json'])=={'stored_public_synthetic_receipt':True}
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError):validate(c,'queued-answer')

def test_private_goals_do_not_escape_into_public_turn_or_model_record(env):
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    db,store,old,meta=reserve(env)
    with db.connect() as c:
        public=AgentOrchestrator._get(c,'queued-answer')
    evaluator=LearningOutcomeEvaluator(db,None,None,None,initialize=False)
    model_record=evaluator._load('queued-answer')
    for value in (public,model_record,store.status('s')):
        encoded=json.dumps(value)
        assert KEY not in encoded and 'source_goal' not in encoded and 'objective_after' not in encoded


def test_consumption_internal_context_cannot_authorize_foreign_scope_or_voice(env):
    db,store,old,meta=reserve(env)
    ref=old['question_reference']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for child,sid,voice in [('other','s',old['voice_input_id']),('c','other',old['voice_input_id']),('c','s','foreign-voice')]:
            with pytest.raises(InvalidAgentPlanError):
                store.validate_captured_question(c,sid,child,ref,_execution_turn_id='queued-answer',_execution_voice_input_id=voice)
        store.validate_captured_question(c,'s','c',ref,_execution_turn_id='queued-answer',_execution_voice_input_id=old['voice_input_id'])


def test_request_response_consumes_same_goal_then_publishes_new_owned_source_not_mastery(env):
    db,store,old,meta=reserve(env,'learning.request_response')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert canonical_inspect(c,'queued-answer') is True
        with pytest.raises(InvalidLearningEvaluationError):LearningOutcomeEvaluator._assert_learning_source(c,meta)
        c.rollback()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        store.complete_turn_execution(c,'queued-answer',True,None,{'prompt':'Explain the key point in your own words'})
        c.execute("UPDATE agent_turn SET tool_status='completed' WHERE agent_turn_id='queued-answer'")
        c.commit()
    status=store.status('s')
    ref=status['pending_learning_capture']['reference']
    assert ref['binding']['capability_name']=='learning.request_response'
    assert ref['binding']['source_agent_turn_id']=='queued-answer'
    assert ref!=old['question_reference']
    service=VoiceCaptureBindingService(db)
    import time
    service.begin_realtime('s','after-own-request','agent_command',{'child_id':'c','question_reference':ref},model_version='TEST_ONLY_SYNTHETIC_TRANSCRIPT',captured_at_ms=int(time.time()*1000))
    assert service.complete_realtime('s','after-own-request','PUBLIC FOLLOWUP')['question_validation']=='valid'
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError):store.validate_captured_question(c,'s','c',old['question_reference'])

def test_request_response_cannot_consume_into_new_goal(env):
    with pytest.raises(InvalidAgentPlanError):reserve(env,'learning.request_response','new')
