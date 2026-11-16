import json,time
from types import SimpleNamespace
import pytest
from test_ordinary_learning_capture_reference import env,current
from home_study_agent.agent_runtime import AgentRuntimeStore,InvalidAgentPlanError
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.canonical_voice_execution import inspect as canonical_inspect

TEXT='PUBLIC SYNTHETIC QUEUED ANSWER'
def completed_capture(env):
    db,store=env
    reference=current(db)['reference']
    service=VoiceCaptureBindingService(db)
    service.begin_realtime('s','queued-capture-source','agent_command',{'child_id':'c','question_reference':reference},
        model_version='TEST_ONLY_SYNTHETIC_TRANSCRIPT',captured_at_ms=int(time.time()*1000))
    capture=service.complete_realtime('s','queued-capture-source',TEXT)
    assert capture['question_validation']=='valid'
    return db,store,capture

def mutate(db,mode):
    with db.connect() as c:
        if mode=='result': c.execute("UPDATE agent_step SET result_json='{\"changed\":true}' WHERE step_id='step'")
        elif mode=='goal': c.execute("UPDATE agent_objective SET status='completed' WHERE objective_id='o'")
        else:
            status='completed' if mode=='completed' else 'failed'
            c.execute("INSERT INTO agent_turn SELECT 'new-source',child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,?,tool_error_code,completed_at,'2026-01-02','new-source-turn-action' FROM agent_turn WHERE agent_turn_id='t'",(status,))
            c.execute("INSERT INTO agent_plan SELECT 'new-source',objective_id,child_id,session_id,'new-source',planner_version,context_manifest_json,plan_json,?,'2026-01-02','2026-01-02' FROM agent_plan WHERE plan_id='p'",(status,))
            c.execute("INSERT INTO agent_step SELECT 'new-source','new-source',step_index,'learning.evaluate_response',capability_version,execution_location,risk_level,arguments_json,expected_evidence_json,?,'new-source-step-action','{\"new_source\":true}',error_code,'2026-01-02','2026-01-02' FROM agent_step WHERE step_id='step'",(status,))
        c.commit()

@pytest.mark.parametrize('mode',['completed','failed','result','goal'])
def test_completed_capture_is_projected_stale_at_delayed_dispatch(env,mode):
    db,store,old=completed_capture(env)
    mutate(db,mode)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        resolved=store.resolve_captured_voice(c,session_id='s',child_id='c',voice_input_id=old['voice_input_id'],transcript=TEXT)
        assert resolved['question_validation']=='stale'
        assert resolved['question_reference']==old['question_reference']
        assert not store.capture_matches_answer(resolved,'evaluate_child_response',old['question_reference']['binding'])
        row=c.execute("SELECT question_validation,question_reference_json FROM voice_capture_binding WHERE voice_input_id=?",(old['voice_input_id'],)).fetchone()
        assert row['question_validation']=='valid' # projected refusal must not rewrite the historical receipt
        assert json.loads(row['question_reference_json'])==old['question_reference']
        with pytest.raises(InvalidAgentPlanError,match='BINDING_CHANGED'):
            AgentOrchestrator._freeze_local_voice(SimpleNamespace(agent_runtime=store,page_source_store=None),c,'s','c',TEXT,old,{}, {})

def test_current_completed_capture_remains_valid_before_interpret(env):
    db,store,old=completed_capture(env)
    action={'type':'evaluate_child_response','voice_input_id':old['voice_input_id'],
        'voice_input_binding':store.voice_capture_snapshot(old),'local_only':True,'cloud_transmission_allowed':False}
    with db.connect() as c:
        c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,tool_status,created_at,client_action_id) VALUES('queued-answer','c','s',?,'learning_reply','Synthetic',?,'test','pending','2026-01-02',?)",(TEXT,json.dumps(action),'realtime-'+old['voice_input_id']))
        c.commit()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        now=store.resolve_captured_voice(c,session_id='s',child_id='c',voice_input_id=old['voice_input_id'],transcript=TEXT)
        assert now['question_validation']=='valid'
        AgentOrchestrator._freeze_local_voice(SimpleNamespace(agent_runtime=store,page_source_store=None),c,'s','c',TEXT,old,{}, {})
