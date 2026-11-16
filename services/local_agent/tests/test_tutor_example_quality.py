import json

from home_study_agent.tutor_example_quality import assess


BOARD = {'segments': [
    {'segment_id': 'one', 'narration': '例如，一盒彩笔有三支。', 'blocks': [{'text': '一盒三支'}]},
    {'segment_id': 'two', 'narration': '两盒就是三加三等于六支。', 'blocks': [{'text': '3+3=6'}]},
]}


def test_quality_accepts_only_grounded_steps_in_distinct_segments():
    assessment = {'applicability': 'required', 'assessment': 'present', 'example_quote': '一盒彩笔',
                  'steps': [{'segment_id': 'one', 'narration_quote': '一盒彩笔', 'board_quote': '一盒三支'},
                            {'segment_id': 'two', 'narration_quote': '三加三', 'board_quote': '3+3=6'}]}
    assert assess(json.dumps(assessment, ensure_ascii=False), BOARD)['status'] == 'evidence_present'
    assessment['steps'][1]['board_quote'] = '3+3=7'
    assert assess(json.dumps(assessment, ensure_ascii=False), BOARD)['status'] == 'unavailable'
    assessment['steps'][1] = assessment['steps'][0]
    assert assess(json.dumps(assessment, ensure_ascii=False), BOARD)['status'] == 'unavailable'
    assessment['example_quote'] = '不存在的例子'
    assert assess(json.dumps(assessment, ensure_ascii=False), BOARD)['status'] == 'unavailable'


def test_quality_unknown_and_inapplicable_are_not_a_pass():
    assert assess('{', BOARD)['status'] == 'unavailable'
    assert assess('{"applicability":"uncertain"}', BOARD)['status'] == 'unavailable'
    assert assess('{"applicability":"not_needed","reason":"short_request"}', BOARD)['status'] == 'not_needed'
    assert assess('{"applicability":"required","assessment":"missing"}', BOARD)['status'] == 'missing'
