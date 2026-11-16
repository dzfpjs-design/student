import json

import pytest

from home_study_agent.voice_control_intent import VoiceControlIntent


@pytest.mark.parametrize("quote,device", [
    ("连接摄像头", "camera"),
    ("把高拍仪连上", "document_camera"),
    ("连接摄像头和高拍仪", "all"),
])
def test_device_connect_control_requires_an_explicit_bounded_target(quote, device):
    value = VoiceControlIntent.parse(json.dumps({
        "role": "control", "control": "device.connect", "device": device,
        "quote": quote, "confidence": .99,
    }, ensure_ascii=False), quote)
    assert value == {"status": "control", "control": "device.connect", "device": device}


@pytest.mark.parametrize("value", [None, "both", "microphone", ["camera", "document_camera"]])
def test_device_connect_rejects_unbounded_or_invalid_targets(value):
    with pytest.raises(ValueError):
        VoiceControlIntent.parse(json.dumps({
            "role": "control", "control": "device.connect", "device": value,
            "quote": "连接设备", "confidence": .99,
        }, ensure_ascii=False), "连接设备")


def test_other_controls_cannot_smuggle_a_device_target():
    with pytest.raises(ValueError):
        VoiceControlIntent.parse(json.dumps({
            "role": "control", "control": "observation.resume", "device": "all",
            "quote": "打开摄像头", "confidence": .99,
        }, ensure_ascii=False), "打开摄像头")
