import json

import pytest

from home_study_agent.tutor_question_projection import project, parse, ROLE_VERSION
from home_study_agent.voice_control_intent import VoiceControlIntent


@pytest.mark.parametrize('text', ['“暂停讲解”', '能解释“缺右引号吗？', '能解释「错配”吗？', '能解释“有「嵌套」内容”吗？'])
def test_ambiguous_or_whole_quoted_text_never_produces_projection(text):
    assert project(text) is None


@pytest.mark.parametrize('text,names,sources', [
    ('小明能解释“私有内容”吗？', ['小明'], []),
    ('能解释“私有内容”吗，联系19900000001。', [], []),
    ('能解释“私有内容”以及其余未经授权的本地原文吗？', [], ['其余未经授权的本地原文']),
])
def test_remaining_text_keeps_all_original_privacy_checks(text, names, sources):
    projected, _ = project(text)
    assert not VoiceControlIntent.text_eligible(projected, names, sources)


def test_multiple_literals_are_fully_removed_and_original_quote_is_not_a_valid_receipt():
    text = '能解释“秘密题文”与「原答案」有什么关系吗？'
    projected, audit = project(text)
    assert projected == '能解释[本地引用]与[本地引用]有什么关系吗？'
    assert len(audit['spans']) == 2
    result = {'role': 'question', 'control': None, 'quote': projected, 'confidence': .95}
    assert parse(json.dumps(result), projected)['role_version'] == ROLE_VERSION
    with pytest.raises(ValueError):
        parse(json.dumps({**result, 'quote': text}), projected)
    assert not VoiceControlIntent.text_eligible(text, [], [])
