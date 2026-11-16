"""Scheduling words alone cannot turn a learning request into a homework command."""
import pytest
from home_study_agent.agent_runtime import AgentRuntimeStore


@pytest.mark.parametrize('candidates',[[],[{'title':'口算作业','binding':{'local':'fixture'}}]])
@pytest.mark.parametrize('quote',['继续今天还没安排好的数学口述复查。','上次没做完的英语听力接着练。'])
def test_unplaced_keywords_without_homework_reference_reach_intent_router(candidates,quote):
    assert AgentRuntimeStore.resolve_unplaced_voice({'candidates':candidates,'dialogue':None},quote) is None


def test_explicit_homework_question_keeps_truthful_empty_response():
    result=AgentRuntimeStore.resolve_unplaced_voice({'candidates':[],'dialogue':None},'昨天没排完的作业有哪些？')
    assert result and '目前没有可确认的未排作业义务' in result['response']
