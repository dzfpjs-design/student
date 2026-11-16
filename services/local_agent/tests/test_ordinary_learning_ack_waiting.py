import copy,inspect,re,textwrap
from dataclasses import replace
import pytest
from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.agent_runtime import AgentRuntimeStore,CapabilityRegistry,action_from_plan


def frame(source='learning.explain',transcript='我懂了',lane=None):
    host=AgentOrchestrator.__new__(AgentOrchestrator)
    host.agent_runtime=AgentRuntimeStore
    host.capability_registry=CapabilityRegistry()
    pending={'objective_id':'public-goal','plan_id':'public-source-plan','source_agent_turn_id':'public-source-turn',
        'capability_name':source,'phase':'study','summary':'Public synthetic concept',
        'success_criteria':['Explain the key point independently'],
        'arguments':{'subject':'数学'},'result':{'execution_receipt':'synthetic source identity'}}
    decision=host._deterministic_learning_reply_plan(transcript=transcript,intent='continue_study',pending_reply=pending)
    action=action_from_plan(decision,transcript,{'summary':pending['summary']})
    action['source']='agent_runtime_deterministic'
    binding=host.agent_runtime.learning_reply_binding(pending)
    capture={'question_validation':'valid','question_reference':{'kind':'learning_reply','binding':binding}}
    return {'self':host,'transcript':transcript,'pending_reply':pending,'decision':decision,'action':action,
        'control_lane':({'status':'non_control','role':'acknowledgement','request_key':'public-role-request'} if lane is None else lane),
        'voice_capture':capture,'answer_target':{'action':'evaluate_child_response','binding':binding},'altered':False}


def eligible(f):
    return f['self']._ordinary_learning_ack_plan(**{k:v for k,v in f.items() if k!='self'})


def late(f):
    fn=inspect.getsource(AgentOrchestrator._interpret_owned)
    start=fn.index('        preserves_ordinary_learning_ack =')
    stop=fn.index('        if (self.settings.agent_runtime_enabled',start)
    block=textwrap.dedent(fn[start:stop])
    scope={**f,'normalized_transcript':f['transcript'],'received_transcript':f['transcript'],
        'planned_decision':f['decision'],'planned_context_manifest':{'session_id':'public-session'},
        'planned':(f['decision'],{}),'pure_speech_echo':False,'speech_echo_removed':False,'transformed_answer':False,
        'preserve_legacy_action':False,'intent':'learning.request_response','response':f['decision'].spoken_transition,
        'independent_document_dialogue':False,'selected_prompt':False,'preserves_tutor_question':False,
        'session':{'state':'study'},'input_revision_arrival':{'arrival_id':'public-arrival'}}
    exec(compile(block,'<actual_ack_late_gate>','exec'),AgentOrchestrator._interpret_owned.__globals__,scope)
    return scope


def test_actual_fixed_ack_plan_keeps_original_request_at_late_guard():
    f=frame();assert eligible(f)
    out=late(f)
    assert out['planned_decision'] is f['decision']
    assert out['action']==f['action']
    assert out['planned_decision'].step.capability.name=='learning.request_response'
    assert out['action']['type']=='none' and out['action']['execution_location']=='server'


def test_actual_short_ack_fastpath_is_exact_and_does_not_need_a_model_key():
    f=frame(transcript='好的',lane={'status':'non_control','role':'acknowledgement'})
    assert eligible(f) and late(f)['planned_decision'] is f['decision']
    f['control_lane']['foreign']=True
    assert not eligible(f)
    f=frame(lane={'status':'non_control','role':'acknowledgement'})
    assert not eligible(f) # a long utterance cannot impersonate the short fastpath


@pytest.mark.parametrize('transcript,lane',[
    ('我懂了',{'status':'non_control','role':'acknowledgement','request_key':'public-request'}),
    ('好的',{'status':'non_control','role':'acknowledgement'}),
])
def test_second_ack_is_a_neutral_receipt_without_a_new_plan_or_candidate(transcript,lane):
    f=frame('learning.request_response',transcript,lane);before=copy.deepcopy(f['pending_reply'])
    out=late(f)
    assert out['planned'] is out['planned_decision'] is out['planned_context_manifest'] is None
    assert out['action']['type']=='none' and out['action']['suppress_speech'] is True
    assert out['action']['source']=='ordinary_learning_waiting_answer'
    assert out['action']['local_only'] is True and out['action']['cloud_transmission_allowed'] is False
    assert out['action']['mastery_update_allowed'] is out['action']['independent_evidence'] is False
    assert 'pending_answer_candidate' not in out['action']
    assert f['pending_reply']==before


@pytest.mark.parametrize('change',[
    'echo','stale','scope','foreign_cap','new_goal','other_summary','other_criteria','other_phase',
    'other_prompt','other_subject','extra_args','wrong_action','wrong_location','unknown_role','no_model_key',
])
def test_ack_exception_rejects_any_unsafe_or_unrelated_plan(change):
    f=frame()
    if change=='echo':f['altered']=True
    elif change=='stale':f['voice_capture']['question_validation']='stale'
    elif change=='scope':f['voice_capture']=copy.deepcopy(f['voice_capture']);f['voice_capture']['question_reference']['binding']['source_version']='0'*64
    elif change=='foreign_cap':f['pending_reply']['capability_name']='unregistered.source'
    elif change=='new_goal':f['decision']=replace(f['decision'],objective=replace(f['decision'].objective,mode='new'))
    elif change=='other_summary':f['decision']=replace(f['decision'],objective=replace(f['decision'].objective,summary='OTHER'))
    elif change=='other_criteria':f['decision']=replace(f['decision'],objective=replace(f['decision'].objective,success_criteria=('OTHER',)))
    elif change=='other_phase':f['decision']=replace(f['decision'],objective=replace(f['decision'].objective,phase='OTHER'))
    elif change in {'other_prompt','other_subject','extra_args'}:
        args=dict(f['decision'].step.arguments);args[{'other_prompt':'prompt','other_subject':'subject','extra_args':'foreign'}[change]]='OTHER'
        f['decision']=replace(f['decision'],step=replace(f['decision'].step,arguments=args))
    elif change=='wrong_action':f['action']['type']='erase_learning_data'
    elif change=='wrong_location':f['action']['execution_location']='client_device_bridge'
    elif change=='unknown_role':f['control_lane']['role']='unknown'
    elif change=='no_model_key':f['control_lane'].pop('request_key')
    assert not eligible(f)


def test_explicit_speech_repeat_keeps_its_original_registered_route():
    f=frame('learning.request_response','我懂了')
    f['transcript']='再说一遍'
    f['decision']=replace(f['decision'],step=replace(f['decision'].step,capability=f['self'].capability_registry.require('speech.repeat','study')))
    f['action']={'type':'repeat_last_response','capability':'speech.repeat','source':'agent_runtime_deterministic'}
    assert not eligible(f)
