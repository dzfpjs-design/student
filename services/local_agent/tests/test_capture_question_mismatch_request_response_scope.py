"""A request-response question has its own speech-notice scope, never scoring permission."""
from home_study_agent.agent_orchestrator import AgentOrchestrator

def test_application_authored_request_response_question_has_closed_notice_scope():
    binding={'objective_id':'synthetic-objective','plan_id':'synthetic-plan','capability_name':'learning.request_response','phase':'check','source_agent_turn_id':'synthetic-request-turn','source_version':'a'*64}
    target={'action':'evaluate_child_response','binding':binding}
    scope=AgentOrchestrator._capture_mismatch_notice_scope('synthetic-session','synthetic-child',target)
    assert scope is not None, 'EXPECTED_CLOSED_REQUEST_RESPONSE_SPEECH_NOTICE_SCOPE'
    assert set(scope)=={'version','reason','scope_sha256'} and scope['version']==1 and scope['reason']=='capture_question_mismatch'
    assert len(scope['scope_sha256'])==64
    assert scope==AgentOrchestrator._capture_mismatch_notice_scope('synthetic-session','synthetic-child',target)
    assert scope!=AgentOrchestrator._capture_mismatch_notice_scope('synthetic-session','synthetic-child',{'action':'evaluate_child_response','binding':{**binding,'source_agent_turn_id':'synthetic-new-request-turn'}})
    assert scope!=AgentOrchestrator._capture_mismatch_notice_scope('synthetic-session','synthetic-other-child',target)
    for changed in ({**binding,'capability_name':'foreign.capability'},{**binding,'source_agent_turn_id':''},{**binding,'unexpected':True},{**binding,'source_version':True}):
        assert AgentOrchestrator._capture_mismatch_notice_scope('synthetic-session','synthetic-child',{'action':'evaluate_child_response','binding':changed}) is None
    for capability in ('learning.explain','learning.evaluate_response'):
        assert AgentOrchestrator._capture_mismatch_notice_scope('synthetic-session','synthetic-child',{'action':'evaluate_child_response','binding':{**binding,'capability_name':capability}}) is not None
