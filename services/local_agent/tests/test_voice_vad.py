from pathlib import Path
import wave

import numpy as np

from home_study_agent.local_media_ai import is_low_information_transcript, voice_activity_evidence


def test_aggressive_vad_accepts_official_human_speech_fixture() -> None:
    path = Path(__file__).parents[1] / "models" / "sherpa-onnx-zipformer-zh-en-2023-11-22" / "test_wavs" / "0.wav"
    if not path.is_file():
        import pytest
        pytest.skip("local speech fixture is not downloaded")
    with wave.open(str(path), "rb") as source:
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        evidence = voice_activity_evidence(samples, source.getframerate())
    assert evidence["human_voice_detected"] is True
    assert evidence["longest_speech_run"] >= 6


def test_aggressive_vad_rejects_keyboard_like_impulses_and_silence() -> None:
    rate = 16_000
    impulses = np.zeros(rate * 2, dtype=np.float32)
    for start in range(1_000, len(impulses), 1_600):
        impulses[start:start + 24] = np.linspace(0.9, 0.0, 24, dtype=np.float32)
    assert voice_activity_evidence(impulses, rate)["human_voice_detected"] is False
    assert voice_activity_evidence(np.zeros(rate, dtype=np.float32), rate)["human_voice_detected"] is False


def test_low_information_transcript_filter_preserves_short_meaningful_replies() -> None:
    assert is_low_information_transcript("啊。") is True
    assert is_low_information_transcript("嗯嗯") is True
    assert is_low_information_transcript("好") is False
    assert is_low_information_transcript("对") is False
    assert is_low_information_transcript("我不会") is False
