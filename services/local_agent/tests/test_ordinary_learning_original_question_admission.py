import copy
from dataclasses import replace
import pytest
from home_study_agent.agent_runtime import AgentPlanDecision,ObjectiveDraft,PlannedStep,action_from_plan
from test_ordinary_learning_ack_waiting import frame,late

RAW='请给我讲解为什么勾股定理成立'

def question_frame():
    f=frame(transcript='我懂了',lane={'status':'non_control','role':'question','request_key':'public-new-question'})
    f['transcript']=RAW
    cap=f['self'].capability_registry.require('learning.explain','study')
    decision=AgentPlanDecision(ObjectiveDraft('new','learning','Public new learning goal',('Explain independently',),'explain'),
        PlannedStep(cap,{'question':RAW,'subject':'数学','support_level':6},'New original public question',cap.expected_evidence),
        'Public synthetic transition',True,1.0)
    f['decision']=decision
    f['action']=action_from_plan(decision,RAW,{'summary':f['pending_reply']['summary']})
    f['action']['source']='agent_runtime_deterministic'
    return f

def accepts(f):return f['self']._ordinary_learning_new_question_plan(**{k:v for k,v in f.items() if k!='self'})

def test_actual_new_original_question_crosses_late_gate_without_old_playback_authority():
    f=question_frame();before=copy.deepcopy(f['voice_capture'])
    assert accepts(f)
    out=late(f)
    assert out['planned_decision'] is f['decision'] and out['action']==f['action']
    assert out['action']['type']=='ask_tutor' and out['action']['execution_location']=='local_model'
    assert f['voice_capture']==before
    assert 'provenance' not in out['action'] and 'suppress_speech' not in out['action']

@pytest.mark.parametrize('change',[
    'role_answer','role_unknown','unavailable','missing_key','typed_role','scoped_current','echo','stale',
    'foreign_reference','foreign_source','continue_goal','execution_goal','other_capability','wrong_action',
    'wrong_location','planner_source','rewritten_action','rewritten_argument','derived_question','old_summary',
    'copied_old_question','extra_material','bool_level','wrong_subject',
])
def test_new_question_exception_is_not_a_general_question_or_tool_permission(change):
    f=question_frame()
    if change=='role_answer':f['control_lane']['role']='answer'
    elif change=='role_unknown':f['control_lane']['role']='unknown'
    elif change=='unavailable':f['control_lane']['status']='unavailable'
    elif change=='missing_key':f['control_lane'].pop('request_key')
    elif change=='typed_role':f['control_lane']['role_version']='tutor_question_role_v1'
    elif change=='scoped_current':f['control_lane']['question_scope']='current'
    elif change=='echo':f['altered']=True
    elif change=='stale':f['voice_capture']['question_validation']='stale'
    elif change=='foreign_reference':f['voice_capture']=copy.deepcopy(f['voice_capture']);f['voice_capture']['question_reference']['binding']['plan_id']='other'
    elif change=='foreign_source':f['pending_reply']['capability_name']='unregistered.source'
    elif change=='continue_goal':f['decision']=replace(f['decision'],objective=replace(f['decision'].objective,mode='continue'))
    elif change=='execution_goal':f['decision']=replace(f['decision'],objective=replace(f['decision'].objective,objective_type='execution'))
    elif change=='other_capability':f['decision']=replace(f['decision'],step=replace(f['decision'].step,capability=f['self'].capability_registry.require('learning.evaluate_response','study')))
    elif change=='wrong_action':f['action']['type']='erase_learning_data'
    elif change=='wrong_location':f['action']['execution_location']='client_device_bridge'
    elif change=='planner_source':f['action']['source']='agent_runtime_planner'
    elif change=='rewritten_action':f['action']['question']='Model-rewritten unrelated old summary'
    elif change in {'rewritten_argument','extra_material','bool_level'}:
        args=dict(f['decision'].step.arguments);args[{'rewritten_argument':'question','extra_material':'artifact_id','bool_level':'support_level'}[change]]= {'rewritten_argument':'Model-rewritten question','extra_material':'public-test-artifact','bool_level':True}[change]
        f['decision']=replace(f['decision'],step=replace(f['decision'].step,arguments=args))
    elif change=='derived_question':f['transcript']='请给我重新讲解刚才那道题'
    elif change=='old_summary':f['pending_reply']['summary']='给我讲解为什么勾股定理成立'
    elif change=='copied_old_question':f['pending_reply']['arguments']['question']=RAW
    elif change=='wrong_subject':f['action']['subject']='OTHER'
    assert not accepts(f)
