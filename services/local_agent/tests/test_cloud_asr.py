from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from normal_provider_owner import normal_provider_owner
from test_cloud_asr_audio_permission import consent_db, permitted, set_permission

from home_study_agent.cloud_asr import CloudAsrError, CloudAsrGateway, CloudAsrResult
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.local_media_ai import ASR_MODEL_NAME, LocalMediaAI, LocalCapabilityNotReadyError


def test_cloud_asr_uses_audio_data_url_and_keeps_language_automatic(tmp_path, monkeypatch, normal_provider_owner) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self): return None
        def json(self): return {"choices": [{"message": {"content": "sign 和 sine 的区别"}}], "usage": {"prompt_tokens": 25, "completion_tokens": 8}}

    class FakeClient:
        def __init__(self, **kwargs): captured["timeout"] = kwargs["timeout"]
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def post(self, url, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "db.sqlite",
        cloud_asr_provider="dashscope",
        cloud_asr_api_key="synthetic-f6343e754c79fcfa6e36",
        cloud_asr_base_url="https://dashscope.example/v1",
        cloud_asr_model="qwen3-asr-flash",
    )
    consent = consent_db(tmp_path, 1)
    result = CloudAsrGateway(settings, cloud_audio_authorized=lambda: permitted(consent)).transcribe(
        b"RIFF-audio",
        context_text="识别参考词：奥数、PET、一次函数。",
    )

    assert result.transcript == "sign 和 sine 的区别"
    assert captured["url"] == "https://dashscope.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer synthetic-f6343e754c79fcfa6e36"
    assert captured["payload"]["messages"][0] == {
        "role": "system",
        "content": [{"text": "识别参考词：奥数、PET、一次函数。"}],
    }
    assert captured["payload"]["messages"][1]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert "language" not in captured["payload"]["asr_options"]
    assert "synthetic-f6343e754c79fcfa6e36" not in json.dumps(captured["payload"])


def test_cloud_asr_obeys_shared_route_health_and_avoids_a_known_blocked_endpoint(tmp_path, normal_provider_owner) -> None:
    class BlockedPolicy:
        def endpoint_available(self, provider, model):
            self.checked = (provider, model)
            return False

    policy = BlockedPolicy()
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "db.sqlite",
        cloud_asr_provider="dashscope", cloud_asr_api_key="synthetic-f6343e754c79fcfa6e36",
        cloud_asr_model="qwen3-asr-flash",
    )

    consent = consent_db(tmp_path, 1)
    with pytest.raises(CloudAsrError, match="CLOUD_ASR_ROUTE_UNAVAILABLE"):
        CloudAsrGateway(settings, runtime_policy=policy, cloud_audio_authorized=lambda: permitted(consent)).transcribe(b"RIFF-audio")

    assert policy.checked == ("qwen", "qwen3-asr-flash")


def speech_fixture() -> bytes:
    path = local_model_root() / ASR_MODEL_NAME / "test_wavs" / "0.wav"
    if not path.is_file():
        pytest.skip("local speech fixture is not downloaded")
    return path.read_bytes()


def local_model_root() -> Path:
    return next(p for p in Path(__file__).resolve().parents if (p / "services/local_agent/models").is_dir()) / "services/local_agent/models"


class FakeCloudAsr:
    configured = True
    context_supported = True
    settings = SimpleNamespace(cloud_asr_model="qwen3-asr-flash")

    def __init__(self, result: CloudAsrResult | Exception):
        self.result = result
        self.calls = 0

    def transcribe(self, _wav_bytes: bytes, context_text: str | None = None) -> CloudAsrResult:
        self.calls += 1
        self.context_text = context_text
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_active_dialogue_prefers_cloud_asr_after_local_human_voice_gate(tmp_path) -> None:
    gateway = FakeCloudAsr(CloudAsrResult("我想复习今天的分数题", "qwen3-asr-flash", 240))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, local_model_root(), cloud_asr_gateway=gateway)  # type: ignore[arg-type]

    transcript, duration_ms, model_version, cloud_used = media._recognize_wav(
        speech_fixture(), prefer_cloud=True, purpose="agent_command",
    )

    assert transcript == "我想复习今天的分数题"
    assert duration_ms > 0
    assert model_version == "cloud:qwen:qwen3-asr-flash"
    assert cloud_used is True
    assert gateway.calls == 1


def test_active_dialogue_builds_sanitized_child_specific_asr_context(tmp_path) -> None:
    database = Database(tmp_path / "context.db")
    database.initialize()
    set_permission(database, 1)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at) VALUES ('child-1', '小明', '四年级', '江苏南京', 'active', datetime('now'), datetime('now'))"
        )
        connection.execute(
            "INSERT INTO daily_session(session_id, child_id, status, state, started_at, last_checkpoint_at, client_action_id) VALUES ('session-1', 'child-1', 'active', 'study', datetime('now'), datetime('now'), 'session-action-1')"
        )
        connection.execute(
            "INSERT INTO child_voice_calibration(child_id, consented_at, profile_json, vocabulary_json, microphone_json, status, updated_at) VALUES ('child-1', datetime('now'), '{}', ?, '{}', 'ready', datetime('now'))",
            (json.dumps(["奥数", "PET", "小明", "一次函数"], ensure_ascii=False),),
        )
        connection.execute(
            "INSERT INTO agent_turn(agent_turn_id, child_id, session_id, transcript, intent, response_text, action_json, orchestrator_version, tool_status, completed_at, created_at, client_action_id) VALUES ('turn-1', 'child-1', 'session-1', '小明想复习一次函数', 'tutor', '我们先看斜率。', '{}', 'test', 'completed', datetime('now'), datetime('now'), 'turn-action-1')"
        )
        connection.execute(
            "INSERT INTO agent_objective(objective_id, child_id, session_id, objective_type, summary, success_criteria_json, phase, status, created_at, updated_at) VALUES ('objective-1', 'child-1', 'session-1', 'learning', '理解一次函数斜率', '[]', 'explanation', 'active', datetime('now'), datetime('now'))"
        )
        connection.commit()

    gateway = FakeCloudAsr(CloudAsrResult("一次函数的斜率", "qwen3-asr-flash", 120))
    media = LocalMediaAI(database, tmp_path, local_model_root(), cloud_asr_gateway=gateway)
    session = {"session_id": "session-1", "child_id": "child-1"}
    media._recognize_wav(speech_fixture(), prefer_cloud=True, session=session, purpose="agent_command")

    assert "奥数" in gateway.context_text
    assert "PET" in gateway.context_text
    assert "一次函数" in gateway.context_text
    assert "小明" not in gateway.context_text
    assert "[孩子称呼已脱敏]" in gateway.context_text


def test_wake_local_recognition_does_not_depend_on_cloud_route_health(tmp_path, monkeypatch) -> None:
    gateway = FakeCloudAsr(CloudAsrError("CLOUD_ASR_TIMEOUT"))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)  # type: ignore[arg-type]
    monkeypatch.setattr(media, "_asr_files", lambda _: {})
    monkeypatch.setattr(media, "_recognize_locally", lambda _samples, _sample_rate: "本地降级识别结果")

    transcript, _, model_version, cloud_used = media._recognize_wav(speech_fixture(), prefer_cloud=True)

    assert transcript == "本地降级识别结果"
    assert model_version == ASR_MODEL_NAME
    assert cloud_used is False
    assert gateway.calls == 0


def test_dialogue_cloud_failure_never_submits_local_guess(tmp_path, monkeypatch) -> None:
    gateway = FakeCloudAsr(CloudAsrError("CLOUD_ASR_TIMEOUT"))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, tmp_path, cloud_asr_gateway=gateway)
    def forbidden(*_args):
        pytest.fail("cloud dialogue must not execute an unreliable local ASR guess")
    monkeypatch.setattr(media, "_recognize_locally", forbidden)
    with pytest.raises(LocalCapabilityNotReadyError, match="云端"):
        media._recognize_wav(speech_fixture(), prefer_cloud=True, purpose="agent_command")


def test_wake_standby_uses_local_without_persisting_audio(tmp_path, monkeypatch) -> None:
    gateway = FakeCloudAsr(CloudAsrResult("小知开始学习", "qwen3-asr-flash", 10))
    media = LocalMediaAI(consent_db(tmp_path, 1), tmp_path, local_model_root(), cloud_asr_gateway=gateway)  # type: ignore[arg-type]

    monkeypatch.setattr(media, "_recognize_locally", lambda *_: "小知开始学习")
    result = media.transcribe_standby(speech_fixture(), "wake-action-1")

    assert result["transcript"] == "小知开始学习"
    assert result["model_version"] == ASR_MODEL_NAME
    assert result["cloud_used"] is False
    assert result["persisted"] is False
    assert gateway.calls == 0
