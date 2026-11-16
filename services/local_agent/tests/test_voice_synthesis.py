from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
import sys
import threading
import time
import types
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from normal_provider_owner import normal_provider_owner
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.voice_synthesis import VoiceSynthesisConsentRequiredError, VoiceSynthesisProtocolError, VoiceSynthesisService


def _settings(tmp_path: Path, **values) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "voice.db",
        maintenance_enabled=False,
        tts_provider="relay_pcm",
        tts_relay_url="https://tts-gateway.example.test/pcm",
        tts_api_key="test-only-key",
        tts_voice_id="original-warm-female-v1",
        **values,
    )


def _frame(samples: tuple[int, ...] = (1, -1, 2, -2)) -> bytes:
    payload = b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)
    return (24000).to_bytes(4, "little") + len(payload).to_bytes(4, "little") + payload


def test_tts_route_is_truthfully_disabled_without_parent_deployment_config(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "test.db", maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "tts-disabled-session"}).json()["session_id"]
        response = client.post(f"/v1/sessions/{session_id}/voice/synthesize/stream", json={"text": "我们继续下一步。", "client_action_id": "tts-disabled-request"})
    assert response.status_code == 503
    assert response.json()["code"] == "TTS_NOT_CONFIGURED"


def test_loopback_development_port_can_call_the_local_tts_route(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "cors.db", maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        response = client.options(
            "/v1/sessions/session/voice/synthesize/stream",
            headers={"Origin": "http://localhost:5174", "Access-Control-Request-Method": "POST"},
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5174"


def test_relay_tts_streams_framed_pcm_caches_without_text_or_audio_audit(tmp_path: Path, normal_provider_owner) -> None:
    async def run() -> None:
        requests: list[httpx.Request] = []

        def relay(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.headers["X-HSC-TTS-Protocol"] == "pcm16-frame-v1"
            assert request.headers["Authorization"] == "Bearer test-only-key"
            assert json.loads(request.content)["text"] == "我们继续下一步。"
            return httpx.Response(200, content=_frame())

        settings = _settings(tmp_path)
        database = Database(settings.database_path)
        database.initialize()
        repository = StudyRepository(database)
        repository.seed_family()
        session_id = repository.start_session("child-primary-4", "tts-stream-session") ["session_id"]
        service = VoiceSynthesisService(database, settings, httpx.AsyncClient(transport=httpx.MockTransport(relay)))
        first = [chunk async for chunk in service.stream(session_id, "我们继续下一步。", "tts-stream-first", True)]
        second = [chunk async for chunk in service.stream(session_id, "我们继续下一步。", "tts-stream-second", True)]
        assert first == [_frame()]
        assert second == [_frame()]
        assert len(requests) == 1
        with database.connect() as connection:
            rows = connection.execute("SELECT summary_json FROM audit_event WHERE action = 'voice_playback' ORDER BY occurred_at").fetchall()
        summaries = [json.loads(row["summary_json"]) for row in rows]
        assert summaries[-1]["cache_hit"] is True
        assert all("我们继续" not in row["summary_json"] for row in rows)
        await service.client.aclose()

    asyncio.run(run())


def test_relay_tts_does_not_cache_a_personalized_reply(tmp_path: Path, normal_provider_owner) -> None:
    async def run() -> None:
        requests: list[httpx.Request] = []

        def relay(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=_frame())

        settings = _settings(tmp_path)
        database = Database(settings.database_path); database.initialize()
        repository = StudyRepository(database); repository.seed_family()
        session_id = repository.start_session("child-primary-4", "tts-personalized-session")["session_id"]
        service = VoiceSynthesisService(database, settings, httpx.AsyncClient(transport=httpx.MockTransport(relay)))
        text = "小明，数学作业我们一起看。"
        _ = [chunk async for chunk in service.stream(session_id, text, "tts-personalized-1", True)]
        _ = [chunk async for chunk in service.stream(session_id, text, "tts-personalized-2", True)]
        assert len(requests) == 2
        await service.client.aclose()

    asyncio.run(run())


def test_relay_tts_requires_text_consent_before_network_request(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    session_id = repository.start_session("child-primary-4", "tts-consent-session")["session_id"]
    service = VoiceSynthesisService(database, settings)
    with pytest.raises(VoiceSynthesisConsentRequiredError):
        service.assert_eligible(session_id, "我们继续下一步。", False)
    asyncio.run(service.close())


def test_cancelled_tts_stream_records_cancellation_and_closes_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        settings = _settings(tmp_path)
        database = Database(settings.database_path); database.initialize()
        repository = StudyRepository(database); repository.seed_family()
        session_id = repository.start_session("child-primary-4", "tts-cancel-session")["session_id"]
        service = VoiceSynthesisService(database, settings)
        provider_waiting = asyncio.Event()
        provider_closed = asyncio.Event()

        async def slow_provider(_: str):
            try:
                yield _frame()
                provider_waiting.set()
                await asyncio.Event().wait()
            finally:
                provider_closed.set()

        monkeypatch.setattr(service, "_provider_frames", slow_provider)

        async def consume() -> None:
            async for _ in service.stream(session_id, "我们继续下一步。", "tts-cancel-request", True):
                pass

        task = asyncio.create_task(consume())
        await provider_waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider_closed.is_set()
        with database.connect() as connection:
            summary = json.loads(connection.execute("SELECT summary_json FROM audit_event WHERE action = 'voice_playback'").fetchone()["summary_json"])
        assert summary["status"] == "cancelled"
        assert summary["error_code"] == "CLIENT_CANCELLED"
        await service.close()

    asyncio.run(run())


def test_aliyun_cosyvoice_adapter_uses_official_duplex_events_and_adult_voice_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, normal_provider_owner) -> None:
    async def run() -> None:
        sent: list[dict] = []

        class Socket:
            def __init__(self) -> None:
                self.events: list[str | bytes] = [json.dumps({"header": {"event": "task-started"}}), b"\x01\x00\xff\xff", json.dumps({"header": {"event": "task-finished"}})]
            async def send(self, value: str) -> None: sent.append(json.loads(value))
            async def recv(self) -> str | bytes:
                event = self.events.pop(0)
                if isinstance(event, str):
                    value = json.loads(event)
                    value["header"]["task_id"] = sent[0]["header"]["task_id"]
                    return json.dumps(value)
                return event

        class Connection:
            async def __aenter__(self) -> Socket: return Socket()
            async def __aexit__(self, *_: object) -> None: return None

        connect_args: dict[str, object] = {}
        def connect(url: str, **kwargs: object) -> Connection:
            connect_args.update({"url": url, **kwargs}); return Connection()

        monkeypatch.setattr("home_study_agent.voice_synthesis.websockets.connect", connect)
        settings = Settings(data_dir=tmp_path, database_path=tmp_path / "aliyun.db", maintenance_enabled=False, tts_provider="aliyun_cosyvoice", tts_api_key="test-key", tts_voice_id="longyingtao_v3", tts_workspace_id="workspace-id", tts_model="cosyvoice-v3-flash")
        service = VoiceSynthesisService(Database(settings.database_path), settings)
        frames = [frame async for frame in service._provider_frames("先完成数学作业。")]
        assert frames == [(24000).to_bytes(4, "little") + (4).to_bytes(4, "little") + b"\x01\x00\xff\xff"]
        assert connect_args["url"] == "wss://workspace-id.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
        assert [item["header"]["action"] for item in sent] == ["run-task", "continue-task", "finish-task"]
        assert sent[0]["payload"]["parameters"]["voice"] == "longyingtao_v3"
        assert sent[0]["payload"]["model"] == "cosyvoice-v3-flash"
        assert sent[1]["payload"]["input"]["text"] == "先完成数学作业。"
        await service.close()

    asyncio.run(run())


def test_qwen_audio_provider_requires_its_own_reviewed_voice_catalog(tmp_path: Path) -> None:
    base = dict(data_dir=tmp_path, database_path=tmp_path / "qwen-audio.db", maintenance_enabled=False, tts_provider="qwen_audio_tts", tts_api_key="test-key", tts_workspace_id="token-plan", tts_model="qwen-audio-3.0-tts-plus")
    accepted = VoiceSynthesisService(Database(base["database_path"]), Settings(**base, tts_voice_id="longanlingxin"))
    rejected = VoiceSynthesisService(Database(base["database_path"]), Settings(**base, tts_voice_id="longyingtao_v3"))
    assert accepted.configured is True
    assert rejected.configured is False


@pytest.mark.parametrize('model,voice,region,accepted', [
    ('cosyvoice-v3-flash','longanrou_v3','cn-beijing',True),
    ('cosyvoice-v3-plus','longanrou_v3','cn-beijing',False),
    ('cosyvoice-v3-plus','longanhuan','cn-beijing',True),
    ('cosyvoice-v3-flash','longyingtao_v3','ap-southeast-1',True),
    ('cosyvoice-v3-flash','longanrou_v3','ap-southeast-1',False),
    ('arbitrary-model','longyingtao_v3','cn-beijing',False),
])
def test_cosyvoice_backend_enforces_model_and_regional_catalog(tmp_path, model, voice, region, accepted):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'voice.db',
        tts_provider='aliyun_cosyvoice', tts_api_key='synthetic-key', tts_workspace_id='ws-synthetic',
        tts_model=model, tts_voice_id=voice, tts_region=region)
    service = VoiceSynthesisService(Database(settings.database_path), settings)
    assert service.configured is accepted
    asyncio.run(service.close())


def test_vendor_failure_classification_never_echoes_untrusted_error_details():
    from home_study_agent.voice_synthesis import VoiceSynthesisProviderRejectedError
    secret = 'synthetic-sensitive-value'
    error = VoiceSynthesisProviderRejectedError({'error_code': 'InvalidParameter', 'error_message': secret})
    assert error.code == 'TTS_PROVIDER_INVALID_PARAMETER' and error.retryable is False
    unknown = VoiceSynthesisProviderRejectedError({'error_code': secret, 'error_message': secret})
    assert unknown.code == 'TTS_PROVIDER_REJECTED'
    assert secret not in str(error) + str(unknown)
    assert VoiceSynthesisProviderRejectedError({'error_code': []}).code == 'TTS_PROVIDER_REJECTED'


def test_qwen_reference_voice_sends_only_text_and_voice_id_then_streams_verified_wav(tmp_path: Path, normal_provider_owner) -> None:
    async def run() -> None:
        pcm = b"\x01\x00\x02\x00"
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000); target.writeframes(pcm)
        requests: list[httpx.Request] = []
        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"output": {"audio": {"data": base64.b64encode(output.getvalue()).decode()}}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        settings = Settings(data_dir=tmp_path, database_path=tmp_path / "clone.db", maintenance_enabled=False, tts_provider="qwen_voice_clone", tts_api_key="test-key", tts_voice_id="qwen3-tts-vc-hsc-reference-1234", tts_model="qwen3-tts-vc-2026-01-22")
        service = VoiceSynthesisService(Database(settings.database_path), settings, client=client)
        assert service.configured is True
        frames = [frame async for frame in service._provider_frames("我们继续下一步。")]
        assert frames == [(24000).to_bytes(4, "little") + (4).to_bytes(4, "little") + pcm]
        payload = json.loads(requests[0].content)
        assert payload["input"] == {"text": "我们继续下一步。", "voice": "qwen3-tts-vc-hsc-reference-1234", "language_type": "Chinese"}
        assert "audio" not in payload["input"]
        await client.aclose()

    asyncio.run(run())


def test_qwen_reference_voice_yields_sse_pcm_before_the_complete_file_url(tmp_path: Path, normal_provider_owner) -> None:
    async def run() -> None:
        pcm = b"\x01\x00\x02\x00"
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000); target.writeframes(pcm)
        wav_bytes = output.getvalue()
        events = [
            {"request_id": "synthetic-clone-request", "output": {"audio": {"data": base64.b64encode(wav_bytes[:-2]).decode(), "url": ""}}},
            {"request_id": "synthetic-clone-request", "output": {"audio": {"data": base64.b64encode(wav_bytes[-2:]).decode(), "url": ""}}},
            {"request_id": "synthetic-clone-request", "output": {"finish_reason": "stop", "audio": {"data": "", "url": "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/complete.wav"}}},
        ]
        body = "".join(f"data:{json.dumps(event)}\n\n" for event in events).encode()
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-dashscope-sse"] == "enable"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        settings = Settings(data_dir=tmp_path, database_path=tmp_path / "clone-sse.db", maintenance_enabled=False, tts_provider="qwen_voice_clone", tts_api_key="test-key", tts_voice_id="qwen-tts-vc-hsc-reference-1234", tts_model="qwen3-tts-vc-2026-01-22")
        service = VoiceSynthesisService(Database(settings.database_path), settings, client=client)
        frames = [frame async for frame in service._provider_frames("继续。")]
        assert b"".join(frame[8:] for frame in frames) == pcm
        assert all(int.from_bytes(frame[:4], "little") == 24000 for frame in frames)
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("event", ["data", "complete", "error"])
def test_qwen_callback_queue_overflow_fails_fast_and_cancels_closes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str, normal_provider_owner) -> None:
    async def run() -> None:
        cancelled = threading.Event()
        calls = {"cancel": 0, "close": 0}

        class ResultCallback: pass
        class AudioFormat: PCM_24000HZ_MONO_16BIT = object()
        class SpeechSynthesizer:
            def __init__(self, *, callback, **_: object) -> None: self.callback = callback
            def streaming_call(self, _: str) -> None:
                for _ in range(33):
                    if event == "data": self.callback.on_data(b"\x01\x00")
                    elif event == "complete": self.callback.on_complete()
                    else: self.callback.on_error("failed")
            def streaming_complete(self, _: int) -> None: cancelled.wait(1)
            def streaming_cancel(self) -> None: calls["cancel"] += 1; cancelled.set()
            def close(self) -> None: calls["close"] += 1

        dashscope = types.ModuleType("dashscope")
        audio = types.ModuleType("dashscope.audio")
        tts = types.ModuleType("dashscope.audio.tts_v2")
        synthesizer_module = types.ModuleType("dashscope.audio.tts_v2.speech_synthesizer")
        tts.AudioFormat = AudioFormat; tts.SpeechSynthesizer = SpeechSynthesizer
        synthesizer_module.ResultCallback = ResultCallback
        dashscope.audio = audio; audio.tts_v2 = tts
        monkeypatch.setitem(sys.modules, "dashscope", dashscope)
        monkeypatch.setitem(sys.modules, "dashscope.audio", audio)
        monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2", tts)
        monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2.speech_synthesizer", synthesizer_module)
        settings = Settings(data_dir=tmp_path, database_path=tmp_path / "qwen-overflow.db", maintenance_enabled=False, tts_provider="qwen_audio_tts", tts_api_key="test-key", tts_voice_id="longanlingxin", tts_workspace_id="workspace", tts_model="qwen-audio-3.0-tts-plus")
        service = VoiceSynthesisService(Database(settings.database_path), settings)
        with pytest.raises(VoiceSynthesisProtocolError, match="队列"):
            _ = [frame async for frame in service._qwen_audio_frames("继续")]
        assert calls == {"cancel": 1, "close": 1}
        await service.close()

    asyncio.run(run())


def test_all_provider_frames_receive_total_response_limits_before_they_are_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        settings = _settings(tmp_path)
        database = Database(settings.database_path); database.initialize()
        repository = StudyRepository(database); repository.seed_family()
        session_id = repository.start_session("child-primary-4", "tts-limit-session")["session_id"]
        service = VoiceSynthesisService(database, settings)

        async def flood(_: str):
            for _ in range(601):
                yield _frame()

        monkeypatch.setattr(service, "_provider_frames", flood)
        with pytest.raises(VoiceSynthesisProtocolError, match="安全时长或大小"):
            _ = [item async for item in service.stream(session_id, "我们继续下一步。", "tts-limit-action", True)]
        assert service._cache == {}
        await service.close()

    asyncio.run(run())


def test_server_pcm_cache_has_entry_and_total_byte_limits(tmp_path: Path) -> None:
    service = VoiceSynthesisService(Database(tmp_path / "cache-bounds.db"), _settings(tmp_path))
    service._cache_put("too-large", (b"x" * 1_000_001,))
    assert not service._cache and service._cache_bytes == 0
    for index in range(12):
        service._cache_put(f"fixed-{index}", (b"x" * 900_000,))
    assert len(service._cache) < 12
    assert service._cache_bytes <= 8_000_000


def test_qwen_cancel_and_hung_close_are_bounded_and_ignore_late_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, normal_provider_owner) -> None:
    async def run() -> None:
        calls = {"cancel": 0, "close": 0}
        class ResultCallback: pass
        class AudioFormat: PCM_24000HZ_MONO_16BIT = object()
        class SpeechSynthesizer:
            def __init__(self, *, callback, **_: object) -> None: self.callback = callback
            def streaming_call(self, _: str) -> None:
                self.callback.on_error("failed")
                threading.Timer(.02, lambda: self.callback.on_data(b"\x01\x00")).start()
            def streaming_complete(self, _: int) -> None: return None
            def streaming_cancel(self) -> None: calls["cancel"] += 1
            def close(self) -> None: calls["close"] += 1; time.sleep(.2)
        dashscope = types.ModuleType("dashscope"); audio = types.ModuleType("dashscope.audio"); tts = types.ModuleType("dashscope.audio.tts_v2"); synthesizer_module = types.ModuleType("dashscope.audio.tts_v2.speech_synthesizer")
        tts.AudioFormat = AudioFormat; tts.SpeechSynthesizer = SpeechSynthesizer; synthesizer_module.ResultCallback = ResultCallback
        dashscope.audio = audio; audio.tts_v2 = tts
        monkeypatch.setitem(sys.modules, "dashscope", dashscope); monkeypatch.setitem(sys.modules, "dashscope.audio", audio); monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2", tts); monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2.speech_synthesizer", synthesizer_module)
        monkeypatch.setattr("home_study_agent.voice_synthesis.QWEN_SHUTDOWN_TIMEOUT_SECONDS", .01)
        settings = Settings(data_dir=tmp_path, database_path=tmp_path / "qwen-hang.db", maintenance_enabled=False, tts_provider="qwen_audio_tts", tts_api_key="test-key", tts_voice_id="longanlingxin", tts_workspace_id="workspace", tts_model="qwen-audio-3.0-tts-plus")
        started = time.monotonic()
        with pytest.raises(Exception):
            _ = [frame async for frame in VoiceSynthesisService(Database(settings.database_path), settings)._qwen_audio_frames("继续")]
        assert time.monotonic() - started < .12
        await asyncio.sleep(.04)
        assert calls == {"cancel": 1, "close": 1}
    asyncio.run(run())
