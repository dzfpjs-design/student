import pytest

from home_study_agent.agent_runtime import DEFAULT_CAPABILITIES


@pytest.mark.parametrize('name', ['controlled_content.stop', 'controlled_game.stop'])
def test_media_stop_contract_does_not_promise_task_resume(name):
    spec = next(spec for spec in DEFAULT_CAPABILITIES if spec.name == name)
    assert '不自动恢复' in spec.description
    assert 'previous_task_resumed' not in spec.expected_evidence
