import pytest

from home_study_agent.spoken_question_intake import SpokenQuestionIntake


@pytest.mark.parametrize("text", [
    "我来口述一道题。",
    "你听我口述这道物理题。",
    "不要拍题，你听我说题目。",
])
def test_explicit_spoken_question_dictation_starts_intake(text):
    assert SpokenQuestionIntake.requests_spoken_intake(text) is True


@pytest.mark.parametrize("text", [
    "继续昨天缺的那道数学口述新题，我现在自己试试。",
    "开始今天待补题的口述复查。",
    "口述题库里还有什么题？",
])
def test_spoken_question_task_reference_does_not_start_dictation(text):
    assert SpokenQuestionIntake.requests_spoken_intake(text) is False
