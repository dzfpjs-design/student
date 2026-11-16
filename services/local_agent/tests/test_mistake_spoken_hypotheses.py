import pytest

from home_study_agent.agent_orchestrator import AgentOrchestrator


@pytest.mark.parametrize("explanation", [
    "不是粗心，我不知道为什么错",
    "我可能是粗心了",
    "不是计算错了，我没读懂题",
    "我没有忘公式，可是这次不会用",
    "老师说可能是基础问题，也可能没看清条件",
    "我也不知道",
    "我错在单位",
])
def test_spoken_cause_is_not_a_verified_diagnosis(explanation):
    # A spoken hypothesis is retained as text; assessment determines its cause.
    assert AgentOrchestrator._mistake_error_type(explanation) == "unknown"
