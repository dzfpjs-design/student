"""Local stop authority is narrower than cloud control authority."""
import pytest

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.agent_runtime import CapabilityRegistry, InvalidAgentPlanError, parse_agent_plan_output
from test_cloud_first_agent import Planner
import json


@pytest.mark.parametrize('stage', ['check_in', 'classroom_recall', 'homework_intake', 'planning', 'study', 'break', 'reflection', 'finished'])
def test_direct_document_stop_is_stage_independent_and_not_past_tense(stage):
    intent, response, action = AgentOrchestrator._classify(stage, '暂停高拍仪，我刚才写了什么？', 5)
    assert intent == 'pause_document_monitor'
    assert action['type'] == 'control_document_monitor' and action['command'] == 'pause'
    assert '正在' in response and '已经' not in response
    assert CapabilityRegistry().bind_action(action, stage)['type'] == 'control_document_monitor'
    assert CapabilityRegistry().require('document.monitor_control', stage, arguments={'command': 'pause'}).name == 'document.monitor_control'


@pytest.mark.parametrize('quote', ['不要暂停高拍仪', '老师说“暂停高拍仪”是什么意思？',
    '如果需要，暂停高拍仪', '我在读题：暂停高拍仪', '暂停高拍仪，不对，先别停',
    '这句话是，暂停高拍仪，继续读题', '不要停，暂停高拍仪只是题目里的话'])
def test_negation_quotation_and_retraction_do_not_authorize_local_document_stop(quote):
    _, _, action = AgentOrchestrator._classify('study', quote, 5)
    assert not (action['type'] == 'control_document_monitor' and action['command'] == 'pause')


@pytest.mark.parametrize('command', ['resume', 'inspect', 'review'])
def test_cross_stage_stop_exception_does_not_publish_start_or_read(command):
    registry = CapabilityRegistry()
    assert not any(x['name'] == 'document.monitor_control' for x in registry.catalog('break'))
    with pytest.raises(InvalidAgentPlanError):
        registry.bind_action({'type': 'control_document_monitor', 'command': command}, 'break')
    with pytest.raises(InvalidAgentPlanError):
        parse_agent_plan_output(json.dumps(Planner('document.monitor_control', {'command': command}, 'synthetic').payload), 'break', registry)
