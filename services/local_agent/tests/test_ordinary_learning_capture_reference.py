import copy
import hashlib
import json
from pathlib import Path
import pytest

from home_study_agent.database import Database
from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService, VoiceCaptureBindingError, normalize_question_reference

@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / 'public-synthetic.db')
    db.initialize()
    store = AgentRuntimeStore(db)
    from home_study_agent.pet_vocabulary import PetVocabularyService
    from home_study_agent.exam_simulation import ExamSimulationService
    PetVocabularyService(db).ensure_schema()
    ExamSimulationService(db).ensure_schema()
    with db.connect() as c:
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('c','Synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('s','c','active','study','2026-01-01','2026-01-01','session-action')")
        c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,tool_status,created_at,client_action_id) VALUES('t','c','s','Synthetic','learning_reply','Synthetic question','{}','test','completed','2026-01-01','turn-action')")
        c.execute("INSERT INTO agent_objective(objective_id,child_id,session_id,objective_type,summary,success_criteria_json,phase,status,created_at,updated_at) VALUES('o','c','s','learning','Synthetic objective','[\"independent answer\"]','study','active','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO agent_plan(plan_id,objective_id,child_id,session_id,agent_turn_id,planner_version,context_manifest_json,plan_json,status,created_at,updated_at) VALUES('p','o','c','s','t','test','{}','{}','waiting_child','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO agent_step(step_id,plan_id,step_index,capability_name,capability_version,execution_location,risk_level,arguments_json,expected_evidence_json,status,idempotency_key,result_json,created_at,updated_at) VALUES('step','p',0,'learning.explain','2','local_model','medium','{\"question\":\"Synthetic question\",\"subject\":\"math\"}','[]','completed','step-action','{\"result\":{\"next_check\":\"Synthetic check\"}}','2026-01-01','2026-01-01')")
        c.commit()
    return db, store

def current(db, child='c'):
    with db.connect() as c:
        return AgentRuntimeStore.pending_learning_capture(c,'s',child)

def test_original_canonical_hash_new_reference_and_original_capture_transaction(env):
    db, store = env
    hint = current(db)
    assert hint is not None
    assert store.status('s')['pending_learning_capture'] == hint
    binding = hint['reference']['binding']
    original = {'summary':'Synthetic objective', 'success_criteria':['independent answer'],
        'arguments':{'question':'Synthetic question','subject':'math'},
        'result':{'result':{'next_check':'Synthetic check'}}}
    assert binding['source_version'] == hashlib.sha256(json.dumps(original,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    # SQL source reader must not perform schema/transaction writes, even under capture BEGIN.
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        sql=[]
        c.set_trace_callback(sql.append)
        store.validate_captured_question(c,'s','c',hint['reference'])
        assert sql and all(q.lstrip().upper().startswith('SELECT ') for q in sql)
    row = VoiceCaptureBindingService(db).begin('s','voice-action',b'public-synthetic-wav','agent_command',
        {'child_id':'c','question_reference':hint['reference']})
    assert json.loads(row['question_reference_json']) == hint['reference']
    capture={'question_reference':hint['reference'],'question_validation':'valid'}
    assert store.capture_matches_answer(capture,'evaluate_child_response',binding)
    assert not store.capture_matches_answer(capture,'submit_diagnostic_answer',binding)
    assert not store.capture_matches_answer({'question_reference':None},'evaluate_child_response',binding)
    for isolation in ({'activity_reference':{'activity_id':'a'}},{'pending_evaluation_turn_id':'e'},{'local_recovery_only':True}):
        assert not store.capture_matches_answer({**capture,**isolation},'evaluate_child_response',binding)

@pytest.mark.parametrize('sql',[
    "UPDATE agent_step SET result_json='{\"changed\":true}'",
    "UPDATE agent_step SET status='running'",
    "UPDATE agent_step SET capability_name='conversation.respond'",
    "UPDATE agent_plan SET status='completed'",
    "UPDATE agent_plan SET objective_id=NULL",
    "UPDATE agent_objective SET status='completed'",
    "UPDATE agent_turn SET tool_status='failed'",
    "UPDATE daily_session SET status='completed'",
])
def test_source_or_owner_changes_reject_old_capture(env,sql):
    db,_=env
    old=current(db)['reference']
    with db.connect() as c:
        c.execute(sql); c.commit()
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError, match='LEARNING_SOURCE_CHANGED'):
            AgentRuntimeStore.validate_captured_question(c,'s','c',old)

def test_cross_child_unknown_kind_extra_and_legacy_diagnostic(env):
    db,store=env
    hint=current(db)
    assert current(db,'other') is None
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError):
            store.validate_captured_question(c,'s','other',hint['reference'])
    for changed in ({**hint['reference'],'extra':True},
        {'kind':'learning_reply','binding':{**hint['reference']['binding'],'extra':'x'}},
        {'kind':'learning_reply','binding':{**hint['reference']['binding'],'capability_name':'learning.unsupported_source'}}):
        with pytest.raises(VoiceCaptureBindingError): normalize_question_reference(changed)
    legacy={'kind':'diagnostic','binding':{'prompt_id':'prompt','item_version':'version'}}
    assert normalize_question_reference({'prompt_id':'prompt','item_version':'version'}) == legacy
    assert store.capture_matches_answer({'question_reference':legacy},'submit_diagnostic_answer',legacy['binding'])

def test_explain_to_evaluate_publishes_new_source_old_onset_remains_old(env):
    db,store=env
    frozen=copy.deepcopy(current(db)['reference'])
    with db.connect() as c:
        c.execute("INSERT INTO agent_turn SELECT 't2',child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,tool_status,tool_error_code,completed_at,created_at,'turn2-action' FROM agent_turn WHERE agent_turn_id='t'")
        c.execute("INSERT INTO agent_plan SELECT 'p2',objective_id,child_id,session_id,'t2',planner_version,context_manifest_json,plan_json,'waiting_child','2026-01-02','2026-01-02' FROM agent_plan WHERE plan_id='p'")
        c.execute("INSERT INTO agent_step SELECT 'step2','p2',step_index,'learning.evaluate_response',capability_version,execution_location,risk_level,arguments_json,expected_evidence_json,status,'step2-action','{\"result\":{\"next_check\":\"Synthetic transfer\"}}',error_code,created_at,updated_at FROM agent_step WHERE step_id='step'")
        c.commit()
    fresh=current(db)['reference']
    assert fresh != frozen and fresh['binding']['capability_name']=='learning.evaluate_response'
    assert frozen['binding']['source_agent_turn_id']=='t'
    with db.connect() as c:
        store.validate_captured_question(c,'s','c',fresh)
        with pytest.raises(InvalidAgentPlanError): store.validate_captured_question(c,'s','c',frozen)
    assert store.capture_matches_answer({'question_reference':fresh},'evaluate_child_response',fresh['binding'])
    assert not store.capture_matches_answer({'question_reference':frozen},'evaluate_child_response',fresh['binding'])

    # The original runtime can leave the earlier waiting plan in place. Finishing the new source cannot revive it.
    with db.connect() as c:
        c.execute("UPDATE agent_plan SET status='completed' WHERE plan_id='p2'"); c.commit()
    assert current(db) is None

def test_equal_latest_owners_are_ambiguous(env):
    db,_=env
    with db.connect() as c:
        c.execute("INSERT INTO agent_turn SELECT 'ambiguous',child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,tool_status,tool_error_code,completed_at,created_at,'ambiguous-action' FROM agent_turn WHERE agent_turn_id='t'")
        c.execute("INSERT INTO agent_plan SELECT 'ambiguous',objective_id,child_id,session_id,'ambiguous',planner_version,context_manifest_json,plan_json,status,created_at,updated_at FROM agent_plan WHERE plan_id='p'")
        c.commit()
    assert current(db) is None

def test_unsupported_and_retention_cannot_cancel_old_target_guard(env):
    _,store=env
    for source in ({'objective_id':'o','plan_id':None,'capability_name':'learning.request_response','phase':'study'},
                   {'objective_id':'o','plan_id':'p','source_agent_turn_id':'t','capability_name':'conversation.respond','phase':'study'}):
        assert store.learning_reply_binding(source) is None
        old_binding={key:source.get(key) for key in ('objective_id','plan_id','capability_name','phase')}
        old_binding['source_version']='a'*64
        assert not store.capture_matches_answer({'question_reference':None},'evaluate_child_response',old_binding)
