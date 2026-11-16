import pytest

from home_study_agent.voice_controls import (
    CAMERA_STOP_PHRASES, PRIVACY_STOP_PHRASES, SPEECH_STOP_PHRASES, is_direct_voice_control,
)


@pytest.mark.parametrize("text", [
    "不要关闭麦克风，我还想问一道题", "关闭摄像头会影响讲题吗", "别停止讲解",
    "为什么你让我停止监听", "如果关闭麦克风会怎样", "停止监听？",
    "我们不要停止测试", "我们停止测试会怎样", "我们停止测试吗？",
])
def test_mentions_negations_and_questions_are_not_local_controls(text):
    assert not is_direct_voice_control(text, PRIVACY_STOP_PHRASES + CAMERA_STOP_PHRASES + SPEECH_STOP_PHRASES)


@pytest.mark.parametrize("text,phrases", [
    ("请停止监听，谢谢", PRIVACY_STOP_PHRASES), ("停止监听，总结今天的学习", PRIVACY_STOP_PHRASES),
    ("请把麦克风关掉", PRIVACY_STOP_PHRASES), ("别再监听了", PRIVACY_STOP_PHRASES),
    ("关闭摄像头", CAMERA_STOP_PHRASES), ("别讲了，这页哪里有问题", SPEECH_STOP_PHRASES),
    ("好了，我们停止测试吧。我已经发现了一些问题。", SPEECH_STOP_PHRASES),
])
def test_clear_stop_clause_is_available_without_cloud(text, phrases):
    assert is_direct_voice_control(text, phrases)
