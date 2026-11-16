from home_study_agent.tutor_first_check import bound_first_check
from home_study_agent.tutor_blackboard import presentation_visible_text


def test_safe_concept_and_single_check_preserve_rich_board():
    board = {'source': 'model_structured', 'segments': [{'narration': '比喻把一个事物比作另一个。你能找出这句中的喻体吗？', 'blocks': [{'kind':'relation','nodes':['事物','喻体']}]}]}
    answer = board['segments'][0]['narration']
    actual, presentation = bound_first_check(answer, board, '语文', 1)
    assert actual == answer and presentation is board


def test_quoted_question_is_content_not_a_child_checkpoint():
    answer = '作者写道“为什么天空是蓝色？”这里先看疑问句。你能指出疑问词吗？很好，我们接着代做下一步。'
    actual, board = bound_first_check(answer, {'source':'model_structured'}, '英语', 1)
    assert actual == answer.split('很好')[0]
    assert '为什么天空' in presentation_visible_text(board)
    assert '代做' not in presentation_visible_text(board)


def test_explicit_full_lesson_keeps_steps_after_questions():
    answer = '判别式是什么？接着代入系数，算出判别式，再求两个根。'
    board = {'source':'model_structured'}
    actual, presentation = bound_first_check(answer, board, '数学', 6)
    assert actual == answer and presentation is board
