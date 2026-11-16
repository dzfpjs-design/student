"""An answer mentioning a concept is not a request to teach that concept."""
import pytest

from home_study_agent.agent_orchestrator import AgentOrchestrator


@pytest.mark.parametrize('utterance', [
    'One person is reading a book and another person is writing. They are doing different activities in the study room.',
    'My cousin enjoys singing and drawing.',
    'The costume costs ten pounds.',
    'The distance is quite long.',
    'sin',
    'cosine',
])
def test_ordinary_english_answer_does_not_request_a_lesson(utterance):
    _, _, action = AgentOrchestrator._classify('study', utterance, 5)
    assert action['type'] != 'ask_tutor'


@pytest.mark.parametrize('utterance', [
    'One person is reading a book.', 'My cousin enjoys singing.',
    'The costume costs ten pounds.', 'The distance is quite long.',
])
def test_subject_inference_does_not_match_inside_english_words(utterance):
    assert AgentOrchestrator._subject_from_text(utterance) == '其他'


@pytest.mark.parametrize('utterance', [
    '解释 sin 和 cos 的区别', 'sin和cos有什么区别', '讲一下 tan(A)',
    '我不懂三角函数',
])
def test_explicit_math_question_still_routes_to_teaching(utterance):
    _, _, action = AgentOrchestrator._classify('study', utterance, 5)
    assert action['type'] == 'ask_tutor'
    assert action['subject'] == '数学'
    assert action['question'] == utterance
