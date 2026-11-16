import json

import pytest

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.agent_runtime import CapabilityRegistry, InvalidAgentPlanError, parse_agent_plan_output


def decision(capability):
    return parse_agent_plan_output(json.dumps({
        'objective': {'mode': 'continue', 'type': 'learning', 'phase': 'check'},
        'next_step': {'capability': capability, 'arguments':
                      {'response': '42', 'subject': '数学'} if capability == 'learning.evaluate_response' else {'answer': '42'}},
        'spoken_transition': '核对这次回答。', 'requires_child_reply': False, 'confidence': .95,
    }), 'study', CapabilityRegistry())


@pytest.mark.parametrize('pending_cap,submit_cap', [
    ('diagnostic.start', 'diagnostic.submit'),
    ('diagnostic.replay', 'diagnostic.submit'),
    ('pet_vocabulary.start', 'pet_vocabulary.submit'),
])
def test_typed_answer_cannot_enter_generic_learning_grader(pending_cap, submit_cap):
    pending = {'capability_name': pending_cap, 'phase': 'check'}
    content = json.dumps({'utterance_binding': {'role': 'answer', 'quote': '42'}})
    generic = decision('learning.evaluate_response')
    with pytest.raises(InvalidAgentPlanError):
        AgentOrchestrator._validate_cloud_utterance_binding(content, '42', generic, pending)
    assert AgentOrchestrator._validate_cloud_utterance_binding(content, '42', decision(submit_cap), pending) == 'answer'
    assert not AgentOrchestrator._should_bind_learning_reply('supportive_chat', '42', {'type': 'none'}, pending)


@pytest.mark.parametrize('submit_cap', ['diagnostic.submit', 'pet_vocabulary.submit'])
def test_typed_submission_needs_matching_pending_question(submit_cap):
    content = json.dumps({'utterance_binding': {'role': 'answer', 'quote': '42'}})
    for pending in (None, {'capability_name': 'learning.request_response', 'phase': 'check'}):
        with pytest.raises(InvalidAgentPlanError):
            AgentOrchestrator._validate_cloud_utterance_binding(content, '42', decision(submit_cap), pending)
