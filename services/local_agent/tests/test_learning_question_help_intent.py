"""Adversarial labels cannot override explicit refusal; no positive password list."""
import json

import pytest

from home_study_agent.voice_control_intent import VoiceControlIntent


@pytest.mark.parametrize('quote',[
    '不要解释这道题，我想自己做',
    '先别给我提示',
    '我不需要你讲解',
    '她说“你能带我想明白吗”，不是我在请求',
    '如果我让你讲解，会怎么样',
    "Don't explain this yet",
])
def test_help_refusal_or_quotation_overrides_model_control_label(quote):
    value=VoiceControlIntent.parse(json.dumps(dict(role='control',control='learning.help_current',
        quote=quote,confidence=.99)),quote)
    assert value['status']=='clarify'


@pytest.mark.parametrize('quote',[
    '我刚才其实是猜的，为什么要这样判断，你能带我想明白吗？',
    '这个条件和前面那一步到底有什么联系呀',
    '不是让我再猜，我想知道为什么',
    '不用直接告诉我答案，可以引导我想第一步吗？',
])
def test_contextual_help_does_not_require_a_fixed_command(quote):
    value=VoiceControlIntent.parse(json.dumps(dict(role='control',control='learning.help_current',
        quote=quote,confidence=.99)),quote)
    assert value['status']=='control' and value['control']=='learning.help_current'
