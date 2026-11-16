import pytest

from home_study_agent.task_start_semantics import validate


def generic(quote):
    return {'subject': None, 'task_kind': None, 'source_type': None, 'target_quote': quote}


@pytest.mark.parametrize('utterance', ['开始学习', '开始学习。', '现在开始学习吧', '我们继续做题'])
def test_explicit_generic_start_can_select_the_next_qualified_task(utterance):
    assert validate(generic(utterance), utterance) == generic(utterance)


@pytest.mark.parametrize('utterance', ['不要开始学习', '不要开始学习。', '老师说“开始学习”', '开始学习是什么意思', '今天怎么开始学习？'])
def test_generic_start_question_or_quotation_is_not_execution_authority(utterance):
    with pytest.raises(ValueError):
        validate(generic(utterance), utterance)
