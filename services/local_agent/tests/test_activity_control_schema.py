import json

import pytest

from home_study_agent.voice_control_intent import VoiceControlIntent


def result(quote, feedback):
    return json.dumps({'role': 'control', 'control': 'companion.activity_feedback',
                       'quote': quote, 'confidence': .99, 'activity_feedback': feedback})


@pytest.mark.parametrize('feedback', [
    {'operation': 'feedback', 'execution': 'partial', 'experience': 'fatigued'},
    {'operation': 'feedback', 'execution': 'not_executed'}, {'operation': 'decline'},
    {'operation': 'defer_resume'}, {'operation': 'request_resume'},
])
def test_activity_control_reuses_service_enums(feedback):
    quote = '现在按我明确说的记录。'
    parsed = VoiceControlIntent.parse(result(quote, feedback), quote)
    assert parsed['control'] == 'companion.activity_feedback'
    assert parsed['activity_feedback'] == feedback


@pytest.mark.parametrize('feedback', [
    {'operation': 'feedback', 'outcome_id': 'invented'},
    {'operation': 'feedback', 'child_id': 'other'},
    {'operation': 'feedback', 'expected_revision': 99},
    {'operation': 'feedback', 'response_quote': 'model replacement'},
    {'operation': 'feedback', 'execution': 'mastered'},
    {'operation': 'request_resume', 'execution': 'completed'},
])
def test_model_cannot_add_activity_authority_or_certification(feedback):
    with pytest.raises(ValueError):
        VoiceControlIntent.parse(result('原话', feedback), '原话')


@pytest.mark.parametrize('quote', ['先别继续学习', '今天不要恢复了', '继续刚才的学习，不对，先别执行'])
def test_negated_resume_is_not_activity_permission(quote):
    assert VoiceControlIntent.parse(result(quote, {'operation': 'request_resume'}), quote)['status'] == 'clarify'


def test_activity_quote_must_match_complete_utterance():
    with pytest.raises(ValueError):
        VoiceControlIntent.parse(result('刚才做了', {'operation': 'feedback'}), '刚才做了，不对，先别记录')


@pytest.mark.parametrize('quote,allowed', [
    ('先继续英语那项，刚才的活动以后再说', True),
    ('继续英语，数学明天再做', True),
    ('继续英语，明天再做', False),
    ('英语那项明天再继续', False),
    ('先别继续英语，活动以后再说', False),
    ('继续英语，不对，先别执行', False),
])
def test_deferral_applies_to_its_named_scope_not_every_task(quote, allowed):
    body = {'role': 'control', 'control': 'task.resume', 'quote': quote, 'confidence': .99,
            'selector': {'subject': 'english', 'task_kind': None, 'relative_order': None}}
    parsed = VoiceControlIntent.parse(json.dumps(body), quote)
    assert (parsed['status'] == 'control') is allowed
