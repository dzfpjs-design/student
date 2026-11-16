from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.app import create_app
from fastapi.testclient import TestClient
from home_study_agent.voice_synthesis import VoiceSynthesisProtocolError, VoiceSynthesisService
from normal_provider_owner import normal_provider_owner
from test_voice_wav_completion import complete_wav, resize_riff


def make_service(root: Path, client: httpx.AsyncClient, provider: str = "qwen_voice_clone"):
    settings = Settings(data_dir=root, database_path=root / "completion.db", maintenance_enabled=False,
                        tts_provider=provider, tts_api_key="synthetic-only-key",
                        tts_voice_id="synthetic-reference-voice", tts_model="qwen3-tts-vc-2026-01-22",
                        tts_relay_url="https://synthetic.example.test/pcm")
    database = Database(settings.database_path)
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    session = repository.start_session("child-primary-4", "synthetic-completion-session")["session_id"]
    return VoiceSynthesisService(database, settings, client), session


def sse_response(source: bytes, terminal: str = "stop") -> httpx.Response:
    # A valid container split before its final sample exercises incremental PCM.
    events = [{"request_id": "synthetic-supplier-request", "output": {"audio": {"data": base64.b64encode(part).decode()}}}
              for part in (source[:-2], source[-2:])]
    if terminal != "missing":
        events.append({"request_id": "foreign-request" if terminal == "foreign" else "synthetic-supplier-request",
                       "output": {"finish_reason": "length" if terminal == "failed" else "stop", "audio": {"data": ""}}})
    if terminal == "after": events.append(events[0])
    if terminal == "duplicate": events.append(events[-1])
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                         content="".join("data: " + json.dumps(event) + "\n\n" for event in events))


@pytest.mark.parametrize("cached", [False, True])
def test_v2_binds_complete_samples_and_cache_to_the_current_request(tmp_path: Path, cached: bool, normal_provider_owner) -> None:
    async def run() -> None:
        calls = []
        source = complete_wav()
        def upstream(request):
            calls.append(request)
            return sse_response(source)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            service, session = make_service(tmp_path, client)
            if cached:
                _ = [part async for part in service.stream(session, "我们继续下一步。", "synthetic-old", True, protocol="pcm16_frame_v2")]
            parts = [part async for part in service.stream(session, "我们继续下一步。", "synthetic-current", True, protocol="pcm16_frame_v2")]
            assert all(int.from_bytes(part[:4], "little") == 24000 for part in parts[:-1])
            assert b"".join(part[8:] for part in parts[:-1]) == source[44:]
            assert int.from_bytes(parts[-1][:4], "little") == 0
            assert json.loads(parts[-1][8:]) == {
                "type": "generation_completed", "request_id": "synthetic-current",
                "frame_count": len(parts) - 1, "pcm_bytes": 8, "sample_count": 4,
            }
            assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["missing", "failed", "foreign", "after", "duplicate"])
def test_complete_container_still_requires_supplier_generation_eos(tmp_path: Path, terminal: str, normal_provider_owner) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: sse_response(complete_wav(), terminal))) as client:
            service, session = make_service(tmp_path, client)
            received = []
            with pytest.raises(VoiceSynthesisProtocolError):
                async for part in service.stream(session, "我们继续下一步。", "synthetic-no-eos", True, protocol="pcm16_frame_v2"):
                    received.append(part)
            assert received and all(int.from_bytes(part[:4], "little") != 0 for part in received)
            assert service._cache_bytes == 0
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["even_truncation", "resized_truncation", "metadata"])
def test_sse_checks_the_full_container_not_just_an_even_prefix(tmp_path: Path, mode: str, normal_provider_owner) -> None:
    async def run() -> None:
        source = complete_wav()
        if mode == "metadata":
            source = resize_riff(source + b"LIST" + (12).to_bytes(4, "little") + b"INFOmetadata")
        else:
            source = source[:-2]
            if mode == "resized_truncation":
                source = resize_riff(source)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: sse_response(source))) as client:
            service, session = make_service(tmp_path, client)
            received = []
            if mode == "metadata":
                received = [part async for part in service._provider_frames("一般指导。")]
                assert b"".join(part[8:] for part in received) == complete_wav()[44:]
            else:
                with pytest.raises(VoiceSynthesisProtocolError):
                    async for part in service.stream(session, "我们继续下一步。", "synthetic-truncated", True):
                        received.append(part)
                assert received and service._cache_bytes == 0
                with Database(service.settings.database_path).connect() as connection:
                    row = connection.execute("SELECT summary_json FROM audit_event WHERE action='voice_playback'").fetchone()
                assert json.loads(row[0])["status"] == "failed"
    asyncio.run(run())


def test_legacy_relay_cannot_issue_v2_completion_from_network_eof(tmp_path: Path, normal_provider_owner) -> None:
    async def run() -> None:
        calls = []
        def upstream(request):
            calls.append(request)
            return httpx.Response(200, content=(24000).to_bytes(4, "little") + (4).to_bytes(4, "little") + b"\1\0\2\0")
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            service, session = make_service(tmp_path, client, "relay_pcm")
            with pytest.raises(VoiceSynthesisProtocolError):
                _ = [part async for part in service.stream(session, "一般指导。", "synthetic-relay", True, protocol="pcm16_frame_v2")]
            assert calls == []
            legacy = [part async for part in service.stream(session, "一般指导。", "synthetic-legacy", True)]
            assert len(legacy) == 1 and int.from_bytes(legacy[0][:4], "little") == 24000
    asyncio.run(run())


@pytest.mark.parametrize("exit_mode", ["close_prefix", "cancel_pending", "close_terminal"])
def test_cancel_closes_the_adapter_without_completion_or_cache(tmp_path: Path, monkeypatch, exit_mode: str) -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            service, session = make_service(tmp_path, client)
            closed, waiting = asyncio.Event(), asyncio.Event()
            async def adapter(_):
                try:
                    yield (24000).to_bytes(4, "little") + (4).to_bytes(4, "little") + b"\1\0\2\0"
                    if exit_mode != "close_terminal":
                        waiting.set()
                        await asyncio.Event().wait()
                finally:
                    closed.set()
            monkeypatch.setattr(service, "_qwen_voice_clone_frames", adapter)
            stream = service.stream(session, "我们继续下一步。", "synthetic-cancel", True, protocol="pcm16_frame_v2")
            await stream.__anext__()
            if exit_mode == "cancel_pending":
                consume = asyncio.create_task(stream.__anext__())
                await waiting.wait()
                consume.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await consume
            elif exit_mode == "close_terminal":
                terminal = await stream.__anext__()
                assert int.from_bytes(terminal[:4], "little") == 0
                await stream.aclose()
            else:
                await stream.aclose()
            assert closed.is_set()
            assert bool(service._cache_bytes) == (exit_mode == "close_terminal")
            with Database(service.settings.database_path).connect() as connection:
                rows = connection.execute("SELECT summary_json FROM audit_event WHERE action='voice_playback'").fetchall()
            assert len(rows) == 1
            assert json.loads(rows[0][0])["status"] == ("completed" if exit_mode == "close_terminal" else "cancelled")
    asyncio.run(run())


def test_http_negotiation_keeps_legacy_and_local_only_boundaries(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "http.db", maintenance_enabled=False,
                        tts_provider="qwen_voice_clone", tts_api_key="synthetic-only-key",
                        tts_voice_id="synthetic-reference-voice", tts_model="qwen3-tts-vc-2026-01-22")
    app = create_app(settings)
    calls = []
    def upstream(request):
        calls.append(request)
        return sse_response(complete_wav())
    app.state.voice_synthesis.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        app.state.privacy_lifecycle.update_preferences({"cloud_model_text_allowed": True})
        session = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "synthetic-http-session"}).json()["session_id"]
        url = f"/v1/sessions/{session}/voice/synthesize/stream"
        body = {"text": "一般指导。", "client_action_id": "synthetic-http-request"}
        legacy = client.post(url, json=body)
        assert legacy.status_code == 200 and legacy.headers["content-type"] == "application/octet-stream"
        v2 = client.post(url, json={**body, "protocol": "pcm16_frame_v2"})
        assert v2.status_code == 200 and v2.headers["content-type"] == "application/x-hsc-pcm16-frame-v2"
        assert v2.headers["cache-control"] == "no-store"
        assert b"generation_completed" in v2.content and b"generation_completed" not in legacy.content
        assert client.post(url, json={**body, "protocol": "pcm16_frame_v2", "local_only": True}).status_code == 409
        assert client.post(url, json={**body, "protocol": "future_v3"}).status_code == 422
        assert len(calls) == 2


@pytest.mark.parametrize("stage", ["task-started", "task-finished"])
@pytest.mark.parametrize("identity", [None, "retired-task"])
def test_supplier_completion_must_belong_to_current_task(tmp_path: Path, monkeypatch, stage: str, identity: str, normal_provider_owner) -> None:
    async def run() -> None:
        sent = []
        class Socket:
            def __init__(self):
                self.index = 0
            async def send(self, value):
                sent.append(json.loads(value))
            async def recv(self):
                event = ("task-started", "samples", "task-finished")[self.index]
                self.index += 1
                if event == "samples":
                    return b"\1\0\2\0"
                task_id = identity if event == stage else sent[0]["header"]["task_id"]
                return json.dumps({"header": {"event": event, "task_id": task_id}})
        class Connection:
            async def __aenter__(self): return Socket()
            async def __aexit__(self, *_): return None
        monkeypatch.setattr("home_study_agent.voice_synthesis.websockets.connect", lambda *_, **__: Connection())
        settings = Settings(data_dir=tmp_path, database_path=tmp_path / "supplier.db", maintenance_enabled=False,
                            tts_provider="aliyun_cosyvoice", tts_api_key="synthetic-only-key", tts_voice_id="longyingtao_v3",
                            tts_workspace_id="synthetic-workspace", tts_model="cosyvoice-v3-flash")
        service = VoiceSynthesisService(Database(settings.database_path), settings)
        with pytest.raises(VoiceSynthesisProtocolError):
            _ = [part async for part in service._provider_frames("一般指导。")]
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["empty", "limit", "supplier_failure"])
def test_failed_generation_never_issues_a_terminal_or_cache(tmp_path: Path, monkeypatch, mode: str) -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            service, session = make_service(tmp_path, client)
            async def adapter(_):
                if mode == "empty": return
                yield (24000).to_bytes(4, "little") + (4).to_bytes(4, "little") + b"\1\0\2\0"
                if mode == "supplier_failure": raise VoiceSynthesisProtocolError("合成资格失败。")
                yield (24000).to_bytes(4, "little") + (4).to_bytes(4, "little") + b"\1\0\2\0"
            monkeypatch.setattr(service, "_qwen_voice_clone_frames", adapter)
            if mode == "limit": monkeypatch.setattr("home_study_agent.voice_synthesis.MAX_RESPONSE_FRAMES", 1)
            received = []
            with pytest.raises(VoiceSynthesisProtocolError):
                async for part in service.stream(session, "我们继续下一步。", "synthetic-failed", True, protocol="pcm16_frame_v2"):
                    received.append(part)
            assert all(int.from_bytes(part[:4], "little") != 0 for part in received)
            assert service._cache_bytes == 0
    asyncio.run(run())
