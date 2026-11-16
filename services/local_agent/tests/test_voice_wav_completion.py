from __future__ import annotations

import io
import asyncio
import base64
import json
from pathlib import Path
import wave

import httpx
import pytest
from normal_provider_owner import normal_provider_owner

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.voice_synthesis import VoiceSynthesisProtocolError, VoiceSynthesisService


def complete_wav(pcm: bytes = b"\x01\x00\xff\xff\x02\x00\xfe\xff") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(pcm)
    return output.getvalue()


def resize_riff(source: bytes) -> bytes:
    return source[:4] + (len(source) - 8).to_bytes(4, "little") + source[8:]


def test_even_sample_truncation_does_not_become_a_complete_voice() -> None:
    with pytest.raises(VoiceSynthesisProtocolError):
        VoiceSynthesisService._wav_frames(complete_wav()[:-2])


def test_matching_riff_length_does_not_hide_truncated_declared_data() -> None:
    with pytest.raises(VoiceSynthesisProtocolError):
        VoiceSynthesisService._wav_frames(resize_riff(complete_wav()[:-2]))


def test_trailing_metadata_is_not_played_as_pcm() -> None:
    pcm = b"\x01\x00\xff\xff\x02\x00\xfe\xff"
    metadata = b"LIST" + (12).to_bytes(4, "little") + b"INFOmetadata"
    source = resize_riff(complete_wav(pcm) + metadata)
    frames = VoiceSynthesisService._wav_frames(source)
    assert b"".join(frame[8:] for frame in frames) == pcm
    assert all(int.from_bytes(frame[:4], "little") == 24000 for frame in frames)


def test_odd_declared_data_cannot_drop_a_byte_and_claim_complete() -> None:
    source = bytearray(complete_wav())
    source[40:44] = (7).to_bytes(4, "little")
    with pytest.raises(VoiceSynthesisProtocolError):
        VoiceSynthesisService._wav_frames(bytes(source))


def test_complete_audio_retains_exact_samples_and_does_not_change_voice() -> None:
    pcm = b"\x01\x00\xff\xff\x02\x00\xfe\xff"
    assert VoiceSynthesisService._wav_frames(complete_wav(pcm)) == [
        (24000).to_bytes(4, "little") + len(pcm).to_bytes(4, "little") + pcm
    ]


def test_invalid_subchunk_length_is_a_classified_protocol_failure() -> None:
    source = resize_riff(complete_wav()[:12] + b"JUNK" + (0xFFFFFFFF).to_bytes(4, "little") + complete_wav()[12:])
    with pytest.raises(VoiceSynthesisProtocolError):
        VoiceSynthesisService._wav_frames(source)


@pytest.mark.parametrize("channels,width,rate", [(2, 2, 24000), (1, 1, 24000), (1, 2, 8000)])
def test_unsupported_wav_parameters_do_not_enter_the_pcm_lane(channels: int, width: int, rate: int) -> None:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\x01\x00\xff\xff")
    with pytest.raises(VoiceSynthesisProtocolError):
        VoiceSynthesisService._wav_frames(output.getvalue())


@pytest.mark.parametrize("truncated", [False, True])
def test_actual_clone_stream_records_only_generation_result_across_reopen(tmp_path: Path, truncated: bool, normal_provider_owner) -> None:
    async def run() -> None:
        source = complete_wav()
        if truncated:
            source = source[:-2]
        requests = []

        def provider(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"output": {"audio": {"data": base64.b64encode(source).decode()}}})

        settings = Settings(data_dir=tmp_path, database_path=tmp_path / "generation.db", maintenance_enabled=False,
                            tts_provider="qwen_voice_clone", tts_api_key="synthetic-only-key",
                            tts_voice_id="synthetic-reference-voice", tts_model="qwen3-tts-vc-2026-01-22")
        database = Database(settings.database_path)
        database.initialize()
        repository = StudyRepository(database)
        repository.seed_family()
        session_id = repository.start_session("child-primary-4", "synthetic-session")["session_id"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            service = VoiceSynthesisService(database, settings, client)
            frames = []
            if truncated:
                with pytest.raises(VoiceSynthesisProtocolError):
                    async for frame in service.stream(session_id, "一般学习指导。", "synthetic-generation", True):
                        frames.append(frame)
                assert frames == []
            else:
                frames = [frame async for frame in service.stream(session_id, "一般学习指导。", "synthetic-generation", True)]
                assert b"".join(frame[8:] for frame in frames) == source[44:]
            assert len(requests) == 1
            assert json.loads(requests[0].content)["input"]["voice"] == "synthetic-reference-voice"
            assert service._cache_bytes == 0

        reopened = Database(settings.database_path)
        with reopened.connect() as connection:
            rows = connection.execute("SELECT child_id, summary_json FROM audit_event WHERE action='voice_playback'").fetchall()
        assert len(rows) == 1
        assert rows[0]["child_id"] == "child-primary-4"
        summary = json.loads(rows[0]["summary_json"])
        assert summary["status"] == ("failed" if truncated else "completed")
        assert summary["frame_count"] == (0 if truncated else 1)
        assert summary["error_code"] == ("TTS_PROVIDER_PROTOCOL_ERROR" if truncated else None)
        assert "一般学习指导" not in rows[0]["summary_json"]
        # Generation audit is not a delivery or mastery receipt.
        assert "delivery" not in summary and "completion_verified" not in summary

    asyncio.run(run())
