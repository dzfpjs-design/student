from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from home_study_agent.app import create_app
from home_study_agent.cloud_voice_interaction import (
    CloudVoiceInteractionService, VoiceInteractionConflictError, VoiceInteractionRequest,
)
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway, ModelRequest, ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository, UnknownSessionError
from normal_provider_owner import normal_provider_owner


class Gateway:
    configured = True

    def __init__(self, output=None):
        self.output = output or {"route": "onboarding", "action": "name_candidate", "candidate": "小星", "response_text": "想叫我小星，对吗？"}
        self.requests = []
        self.hook = None

    async def complete(self, request):
        assert isinstance(request, ModelRequest)
        self.requests.append(request)
        if self.hook:
            await self.hook()
        if isinstance(self.output, Exception):
            raise self.output
        return ModelResult(
            content=json.dumps(self.output, ensure_ascii=False), provider="test", model="test-fast",
            latency_ms=8, prompt_tokens=31, completion_tokens=15,
        )


def request(transcript="以后我叫你小星，好不好", phase="naming", action_id="action-1", **context):
    return VoiceInteractionRequest.model_validate({
        "transcript": transcript, "client_action_id": action_id, "context": {"phase": phase, **context},
    })


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "voice.db", maintenance_enabled=False)
    database = Database(settings.database_path)
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    privacy = PrivacyLifecycleService(database, settings.data_dir)
    privacy.initialize()
    with database.connect() as connection:
        for session_id, child in (("session-a", "child-primary-4"), ("session-b", "child-junior-3")):
            connection.execute(
                "INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) "
                "VALUES (?,?,?,'check_in',datetime('now'),datetime('now'),?)",
                (session_id, child, "active" if session_id == "session-a" else "completed", session_id),
            )
        connection.commit()
    gateway = Gateway()
    service = CloudVoiceInteractionService(repository, gateway, settings,
        lambda: privacy.preferences()['cloud_model_text_allowed'])
    service.ensure_schema()
    return service, gateway, database, settings


@pytest.mark.parametrize("phase,transcript,action,context,candidate", [
    ("naming", "以后我叫你小星，好不好", "name_candidate", {}, "小星"),
    ("naming", "小星", "name_candidate", {}, "小星"),
    ("confirm_name", "就这个吧，我挺喜欢的", "confirm_name", {"pending_name": "小星"}, None),
    ("confirm_name", "不太喜欢，重新想想", "reject_name", {"pending_name": "小星"}, None),
    ("confirm_name", "不对，换成小月吧", "name_candidate", {"pending_name": "小星"}, "小月"),
    ("naming", "名字先放一放吧", "skip", {}, None),
    ("tutorial", "刚才走神没听清，再说一下", "repeat", {"step": "wake_word", "wake_word": "小星"}, None),
    ("tutorial", "我明白啦，接着往下吧", "advance", {"step": 2}, None),
    ("tutorial", "这些我会了，介绍就免了吧", "skip", {}, None),
])
def test_cloud_contract_and_natural_expression_fixtures(setup, phase, transcript, action, context, candidate):
    service, gateway, database, _ = setup
    gateway.output = {"route": "onboarding", "action": action, "response_text": "好的，我理解你的意思了。"}
    if candidate:
        gateway.output["candidate"] = candidate
    response = asyncio.run(service.interpret("session-a", request(transcript, phase, **context)))
    assert response.action == action
    assert response.candidate == candidate
    assert response.source == "cloud"
    model_request = gateway.requests[0]
    assert model_request.request_type == "voice_interaction"
    assert model_request.child_id == "child-primary-4"
    assert model_request.knowledge_version is None and model_request.evidence_refs == ()
    sent = json.loads(model_request.messages[1]["content"])
    assert sent == {"transcript": transcript, "context": {"phase": phase, **context}}
    assert "client_action_id" not in sent
    with database.connect() as connection:
        audit = connection.execute("SELECT * FROM model_call WHERE purpose='voice_interaction'").fetchone()
        assert audit["status"] == "completed"
        assert audit["prompt_tokens"] == 31 and audit["completion_tokens"] == 15
        assert transcript not in audit["context_manifest_json"]


@pytest.mark.parametrize("phase", ["naming", "confirm_name", "tutorial"])
def test_agent_route_preserves_original_learning_request_without_executing_it(setup, phase):
    service, gateway, database, _ = setup
    transcript = "  我还是不懂正弦，能换个方法帮我理解吗？  "
    gateway.output = {"route": "agent", "action": "none", "response_text": "我们来看看这个学习问题。"}
    response = asyncio.run(service.interpret("session-a", request(transcript, phase)))
    assert response.route == "agent" and response.action == "none" and response.candidate is None
    assert json.loads(gateway.requests[0].messages[1]["content"])["transcript"] == transcript
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_turn").fetchone()[0] == 0


@pytest.mark.parametrize("transcript,phase,route,action", [
    ("这些介绍我知道了，我们直接开始学习吧", "tutorial", "onboarding", "skip"),
    ("先别介绍了，我想弄懂正弦是什么", "tutorial", "agent", "none"),
    ("名字先不取了，帮我看看这道数学题", "naming", "agent", "none"),
    ("我听懂啦，继续下一个", "tutorial", "onboarding", "advance"),
    ("嗯，就用这个名字", "confirm_name", "onboarding", "confirm_name"),
])
def test_windows_wake_word_equal_to_child_nickname_never_blocks_plain_speech(setup, transcript, phase, route, action):
    service, gateway, database, _ = setup
    gateway.output = {"route": route, "action": action, "response_text": "好的，我理解你的意思了。"}
    payload = request(transcript, phase, wake_word="四年级同学", pending_name="四年级同学")
    response = asyncio.run(service.interpret("session-a", payload))
    assert (response.route, response.action, response.source) == (route, action, "cloud")
    sent = json.loads(gateway.requests[0].messages[1]["content"])
    assert sent["transcript"] == transcript
    assert sent["context"]["wake_word"] == "[当前唤醒词]"
    assert sent["context"]["pending_name"] == "[待确认昵称]"
    assert "四年级同学" not in json.dumps(gateway.requests[0].messages, ensure_ascii=False)
    with database.connect() as connection:
        audit = connection.execute("SELECT context_manifest_json FROM model_call WHERE purpose='voice_interaction'").fetchone()[0]
    assert "四年级同学" not in audit


def test_private_alias_reply_is_validated_then_restored_locally_and_replayed(setup):
    service, gateway, _, _ = setup
    gateway.output = {"route": "onboarding", "action": "confirm_name", "response_text": "你确定要用[待确认昵称]这个称呼，对吗？"}
    payload = request("就它了", "confirm_name", wake_word="四年级同学", pending_name="四年级同学")
    response = asyncio.run(service.interpret("session-a", payload))
    assert response.source == "cloud"
    assert "四年级同学" in response.response_text and "[待确认昵称]" not in response.response_text
    assert asyncio.run(service.interpret("session-a", payload)) == response
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("alias_field", ["wake_word", "pending_name"])
def test_renaming_configured_nickname_alias_redacts_speech_but_grounds_new_name_in_original(setup, alias_field):
    service, gateway, _, _ = setup
    gateway.output = {"route": "onboarding", "action": "name_candidate", "candidate": "小知", "response_text": "想换成小知，对吗？"}
    payload = request("别叫四年级同学了，以后叫你小知", **{alias_field: "四年级同学"})
    response = asyncio.run(service.interpret("session-a", payload))
    assert response.source == "cloud" and response.candidate == "小知"
    sent = json.loads(gateway.requests[0].messages[1]["content"])
    assert "四年级同学" not in sent["transcript"]
    assert sent["context"][alias_field] in sent["transcript"]
    assert "以后叫你小知" in sent["transcript"]
    assert payload.transcript == "别叫四年级同学了，以后叫你小知"


@pytest.mark.parametrize("transcript", [
    "我的名字是四年级同学，以后叫你小知", "我叫四年级同学，以后叫你小知",
    "四年级同学，我家住在幸福街十号，以后叫你小知",
    "别叫四年级同学了，我的手机号是19900000001",
    "四年级同学，初三同学今天让我改个名字", "四年级同学，爸爸打我",
])
def test_configured_alias_in_speech_never_exempts_identity_address_or_other_child(setup, transcript):
    service, gateway, _, _ = setup
    result = asyncio.run(service.interpret("session-a", request(transcript, wake_word="四年级同学")))
    assert result.source == "local_privacy" and result.route == "clarify"
    assert not gateway.requests


@pytest.mark.parametrize("transcript,context", [
    ("我的名字是四年级同学，我家住在幸福街十号", {"wake_word": "四年级同学"}),
    ("继续下一步", {"wake_word": "四年级同学的电话19900000001"}),
    ("继续下一步", {"pending_name": "四年级同学", "step": "四年级同学的家庭住址：幸福街"}),
])
def test_alias_substitution_does_not_exempt_private_speech_or_context_sentences(setup, transcript, context):
    service, gateway, _, _ = setup
    assert asyncio.run(service.interpret("session-a", request(transcript, "tutorial", **context))).source == "local_privacy"
    assert not gateway.requests


@pytest.mark.parametrize("phase,context,output", [
    ("naming", {}, {"route": "onboarding", "action": "advance"}),
    ("tutorial", {}, {"route": "onboarding", "action": "confirm_name"}),
    ("confirm_name", {}, {"route": "onboarding", "action": "confirm_name"}),
    ("confirm_name", {}, {"route": "onboarding", "action": "reject_name"}),
    ("naming", {}, {"route": "agent", "action": "skip"}),
    ("naming", {}, {"route": "onboarding", "action": "none"}),
    ("naming", {}, {"route": "onboarding", "action": "name_candidate", "candidate": "小月"}),
    ("naming", {}, {"route": "onboarding", "action": "name_candidate", "candidate": "数学作业"}),
    ("naming", {}, {"route": "clarify", "action": "none", "candidate": "小星"}),
    ("naming", {}, {"route": "clarify", "action": "none", "extra": "not allowed"}),
    ("naming", {}, {"route": "clarify", "action": "none", "source": "local_privacy"}),
    ("naming", {}, {"route": "onboarding", "action": "skip", "response_text": "告诉我你的住址"}),
    ("naming", {}, {"route": "onboarding", "action": "skip", "response_text": "我会永远陪着你"}),
    ("naming", {}, {"route": "onboarding", "action": "name_candidate", "candidate": "我饿了"}),
])
def test_invalid_or_unsafe_model_decisions_never_advance(setup, phase, context, output):
    service, gateway, _, _ = setup
    gateway.output = {"response_text": "好的。", **output}
    response = asyncio.run(service.interpret("session-a", request("叫你小星吧，我还有数学作业，我饿了", phase, **context)))
    assert (response.route, response.action, response.source) == ("clarify", "none", "unavailable")
    assert response.candidate is None


@pytest.mark.parametrize("transcript,context", [
    ("妈妈电话是19900000001", {}), ("爸爸打我", {}),
    ("我家住在幸福街十号", {}), ("爸爸工资五千", {}),
    ("我叫张三", {}), ("我在人民小学", {}),
    ("叫你小星", {"wake_word": "家庭住址：幸福街十号"}),
    ("叫你小星", {"step": "妈妈电话19900000001"}),
])
def test_private_transcript_and_every_context_text_stay_local(setup, transcript, context):
    service, gateway, database, _ = setup
    response = asyncio.run(service.interpret("session-a", request(transcript, **context)))
    assert response.source == "local_privacy" and response.action == "none"
    assert gateway.requests == []
    with database.connect() as connection:
        audit = connection.execute("SELECT summary_json FROM audit_event WHERE action='voice_interaction_completed'").fetchone()[0]
    assert transcript not in audit
    assert "19900000001" not in audit


@pytest.mark.parametrize("transcript", ["别再监听了", "把麦克风关掉", "停止说话", "stop recording"])
def test_privacy_stop_is_local_handoff_without_claiming_execution(setup, transcript):
    service, gateway, _, _ = setup
    response = asyncio.run(service.interpret("session-a", request(transcript)))
    assert (response.route, response.action, response.source) == ("agent", "none", "local_privacy")
    assert "执行" in response.response_text and "已关闭" not in response.response_text
    assert gateway.requests == []


@pytest.mark.parametrize("mode", ["disabled", "unconfigured", "error", "invalid_json_shape"])
def test_cloud_unavailable_is_honest_and_replayable(setup, mode):
    service, gateway, _, _ = setup
    if mode == "disabled":
        service.cloud_text_allowed = lambda: False
    elif mode == "unconfigured":
        gateway.configured = False
    elif mode == "error":
        gateway.output = RuntimeError("provider exception must never be persisted")
    else:
        gateway.output = ["bad shape"]
    first = asyncio.run(service.interpret("session-a", request()))
    assert (first.route, first.action, first.source) == ("clarify", "none", "unavailable")
    assert asyncio.run(service.interpret("session-a", request())) == first
    assert len(gateway.requests) == (1 if mode in {"error", "invalid_json_shape"} else 0)


@pytest.mark.parametrize("output,expected_code", [
    (["not an object"], "OUTPUT_NOT_OBJECT"),
    ({"route": "onboarding", "action": "advance", "response_text": "继续。"}, "OUTPUT_PHASE_ACTION_INVALID"),
    ({"route": "onboarding", "action": "name_candidate", "candidate": "小月", "response_text": "小月？"}, "OUTPUT_NAME_UNGROUNDED"),
    ({"route": "onboarding", "action": "skip", "response_text": "电话号码是19900000001"}, "OUTPUT_PRIVATE_TEXT"),
    ({"route": "onboarding", "action": "skip", "response_text": "我会永远陪着你"}, "OUTPUT_UNSAFE_REPLY"),
    ({"route": "clarify", "action": "none", "source": "cloud", "response_text": "再说说？"}, "OUTPUT_SOURCE_FORBIDDEN"),
    ({"route": "clarify", "action": "none", "untrusted": "must not be persisted"}, "OUTPUT_SCHEMA_INVALID"),
    (RuntimeError("untrusted provider detail must not be persisted"), "GATEWAY_OR_INTERNAL_FAILED"),
])
def test_diagnostic_codes_distinguish_failures_without_retaining_provider_text(setup, output, expected_code):
    service, gateway, database, _ = setup
    gateway.output = output
    response = asyncio.run(service.interpret("session-a", request()))
    assert response.source == "unavailable"
    with database.connect() as connection:
        audit = connection.execute("SELECT error_code,context_manifest_json FROM model_call WHERE purpose='voice_interaction'").fetchone()
        event = connection.execute("SELECT summary_json FROM audit_event WHERE action='voice_interaction_completed'").fetchone()[0]
        stored = connection.execute("SELECT reason,response_json FROM cloud_voice_interaction").fetchone()
    code = "VOICE_INTERACTION_" + expected_code
    assert audit["error_code"] == stored["reason"] == json.loads(event)["reason"] == code
    persisted = json.dumps([tuple(audit), event, tuple(stored)], ensure_ascii=False)
    assert "19900000001" not in persisted and "must not be persisted" not in persisted


@pytest.mark.parametrize("phase,context,expected_relationship", [
    ("naming", {}, "给助手自己起一个昵称"),
    ("confirm_name", {"pending_name": "小知"}, "等待孩子对这个伙伴昵称确认"),
    ("tutorial", {"step": "wake_practice"}, "不是学科练习题"),
])
def test_prompt_supplies_pending_task_and_correct_naming_roles(setup, phase, context, expected_relationship):
    service, _, _, _ = setup
    messages = service._messages(request(phase=phase, **context))
    system = messages[0]["content"]
    assert expected_relationship in system
    assert "绝不是询问或修改孩子的名字" in system
    assert "助手回应应说‘你叫我’而不是‘我叫你’" in system
    assert "不要返回source或其他字段" in system
    assert "不是要求用户必须说的口令" in system


def test_response_survives_service_and_database_reopen(setup):
    service, gateway, _, settings = setup
    first = asyncio.run(service.interpret("session-a", request()))
    reopened = CloudVoiceInteractionService(StudyRepository(Database(settings.database_path)), gateway, settings, lambda: True)
    assert asyncio.run(reopened.interpret("session-a", request())) == first
    assert len(gateway.requests) == 1


def test_result_text_is_deleted_with_history_and_audits_contain_no_names(setup):
    service, gateway, database, settings = setup
    response = asyncio.run(service.interpret("session-a", request()))
    assert response.source == "cloud"
    with database.connect() as connection:
        row = connection.execute("SELECT response_json FROM cloud_voice_interaction").fetchone()
        assert "小星" in row[0]
        audit_text = " ".join(row[0] for row in connection.execute("SELECT summary_json FROM audit_event"))
        assert "小星" not in audit_text
        assert request().transcript not in audit_text
    privacy = PrivacyLifecycleService(database, settings.data_dir, settings.data_encryption_key)
    privacy.initialize()
    privacy.delete_child_scope("child-primary-4", "all_history", "确认删除", "delete-voice-history")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_voice_interaction").fetchone()[0] == 0
    with pytest.raises(UnknownSessionError):
        asyncio.run(service.interpret("session-a", request()))
    assert len(gateway.requests) == 1


def test_managed_backup_restore_replays_result_without_another_model_call(setup):
    service, gateway, database, settings = setup
    original = asyncio.run(service.interpret("session-a", request()))
    privacy = PrivacyLifecycleService(database, settings.data_dir, settings.data_encryption_key)
    privacy.initialize()
    backup = privacy.create_backup()
    with sqlite3.connect(backup["path"]) as connection:
        saved = json.loads(connection.execute("SELECT response_json FROM cloud_voice_interaction").fetchone()[0])
        assert saved == original.model_dump(exclude_none=True)
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id='session-a'")
        connection.execute("DELETE FROM cloud_voice_interaction")
        connection.commit()
    # Explicit synthetic native issuer at this one legitimate restore site.
    from dataclasses import replace
    from privacy_restore_operation import restore_operation
    from synthetic_native_restore_request import KEY, signed_restore_post
    restored_settings = replace(settings, native_launch_attestation_key=KEY)
    with TestClient(create_app(restored_settings, model_gateway=gateway)) as client:
        token = client.post("/v1/parent/setup", json={"pin": "7391"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        operation = restore_operation(client, headers=headers)
        restored = signed_restore_post(client, headers=headers, json={
            "backup_id": backup["backup_id"], "confirmation": "确认恢复", "operation": operation,
        })
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "restored"
        assert restored.json()["operation_receipt"]["status"] == "committed"
    reopened = CloudVoiceInteractionService(StudyRepository(Database(settings.database_path)), gateway, settings, lambda: True)
    assert asyncio.run(reopened.interpret("session-a", request())) == original
    assert len(gateway.requests) == 1


def test_history_deletion_removes_result_from_rotated_backup(setup):
    service, _, database, settings = setup
    asyncio.run(service.interpret("session-a", request()))
    privacy = PrivacyLifecycleService(database, settings.data_dir, settings.data_encryption_key)
    privacy.initialize()
    old_backup = privacy.create_backup()
    privacy.delete_child_scope("child-primary-4", "all_history", "确认删除", "purge-voice-backup")
    assert not Path(old_backup["path"]).exists()
    for backup in privacy.list_backups():
        with sqlite3.connect(backup["path"]) as connection:
            assert connection.execute("SELECT COUNT(*) FROM cloud_voice_interaction WHERE child_id='child-primary-4'").fetchone()[0] == 0


def test_export_media_deletion_and_tombstone_replay_are_child_isolated(setup):
    service, gateway, database, settings = setup
    first = asyncio.run(service.interpret("session-a", request()))
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id='session-a'")
        connection.execute("UPDATE daily_session SET status='active' WHERE session_id='session-b'")
        connection.commit()
    gateway.output = {"route": "onboarding", "action": "name_candidate", "candidate": "小月", "response_text": "想叫我小月，对吗？"}
    second_payload = request("我想叫你小月", action_id="other-child-action")
    second = asyncio.run(service.interpret("session-b", second_payload))
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id='session-b'")
        connection.execute("UPDATE daily_session SET status='active' WHERE session_id='session-a'")
        connection.commit()
    privacy = PrivacyLifecycleService(database, settings.data_dir, settings.data_encryption_key)
    privacy.initialize()
    for child_id, expected, forbidden in (("child-primary-4", first, "小月"), ("child-junior-3", second, "小星")):
        exported = json.loads(Path(privacy.export_child(child_id)["path"]).read_text())
        rows = exported["cloud_voice_interactions"]
        assert len(rows) == 1 and rows[0]["child_id"] == child_id
        assert json.loads(rows[0]["response_json"]) == expected.model_dump(exclude_none=True)
        assert forbidden not in json.dumps(rows, ensure_ascii=False)
        assert exported["cloud_transmission_allowed"] is False and exported["scope"] == "local_only"
    assert privacy.deletion_preview("child-primary-4")["cloud_voice_interactions"] == 1
    old_backup = privacy.create_backup()
    deleted = privacy.delete_child_scope("child-primary-4", "media", "确认删除", "delete-child-voice")
    assert deleted["status"] == "completed" and deleted["deleted_counts"]["cloud_voice_interaction"] == 1
    assert privacy.delete_child_scope("child-primary-4", "media", "确认删除", "delete-child-voice")["deletion_id"] == deleted["deletion_id"]
    assert privacy.deletion_preview("child-primary-4")["cloud_voice_interactions"] == 0
    assert privacy.deletion_preview("child-junior-3")["cloud_voice_interactions"] == 1
    assert not Path(old_backup["path"]).exists()
    for backup in privacy.list_backups():
        with sqlite3.connect(backup["path"]) as connection:
            rows = connection.execute("SELECT child_id,response_json FROM cloud_voice_interaction").fetchall()
            assert len(rows) == 1 and rows[0][0] == "child-junior-3" and "小星" not in rows[0][1]
    with database.connect() as connection:
        audit = connection.execute("SELECT summary_json FROM audit_event WHERE child_id='child-primary-4' AND action='voice_interaction_completed'").fetchone()
        assert audit is not None and "小星" not in audit[0] and request().transcript not in audit[0]
    reopened = CloudVoiceInteractionService(StudyRepository(Database(settings.database_path)), gateway, settings, lambda: True)
    for payload in (request(), request("换一个叫小木吧"), second_payload):
        with pytest.raises(VoiceInteractionConflictError, match="ACTION_CONFLICT"):
            asyncio.run(reopened.interpret("session-a", payload))
    exported = json.loads(Path(privacy.export_child("child-primary-4")["path"]).read_text())
    assert exported["cloud_voice_interactions"] == []
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id='session-a'")
        connection.execute("UPDATE daily_session SET status='active' WHERE session_id='session-b'")
        connection.commit()
    assert asyncio.run(reopened.interpret("session-b", second_payload)) == second
    with pytest.raises(VoiceInteractionConflictError, match="ACTION_CONFLICT"):
        asyncio.run(reopened.interpret("session-b", request()))
    assert len(gateway.requests) == 2


def test_media_deletion_during_model_call_cannot_resurrect_response(setup):
    service, gateway, database, settings = setup
    privacy = PrivacyLifecycleService(database, settings.data_dir, settings.data_encryption_key)
    privacy.initialize()

    async def delete_pending():
        privacy.delete_child_scope("child-primary-4", "media", "确认删除", "delete-pending-voice")

    gateway.hook = delete_pending
    with pytest.raises(VoiceInteractionConflictError, match="SCOPE_CHANGED"):
        asyncio.run(service.interpret("session-a", request()))
    with pytest.raises(VoiceInteractionConflictError, match="ACTION_CONFLICT"):
        asyncio.run(service.interpret("session-a", request()))
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_voice_interaction").fetchone()[0] == 0
        audit = connection.execute("SELECT summary_json FROM audit_event WHERE action='voice_interaction_started'").fetchone()[0]
        assert "小星" not in audit and request().transcript not in audit
        model_audit = connection.execute("SELECT status,error_code,prompt_tokens FROM model_call WHERE purpose='voice_interaction'").fetchone()
        assert tuple(model_audit) == ("failed", "VOICE_INTERACTION_SCOPE_CHANGED", 31)
    assert len(gateway.requests) == 1


def test_privacy_export_and_media_delete_support_database_without_optional_voice_table(setup):
    _, _, database, settings = setup
    with database.connect() as connection:
        connection.execute("DROP TABLE cloud_voice_interaction")
        connection.commit()
    privacy = PrivacyLifecycleService(database, settings.data_dir, settings.data_encryption_key)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child("child-primary-4")["path"]).read_text())
    assert exported["cloud_voice_interactions"] == []
    assert privacy.deletion_preview("child-primary-4")["cloud_voice_interactions"] == 0
    assert privacy.delete_child_scope("child-primary-4", "media", "确认删除", "delete-old-db")["status"] == "completed"


def test_real_gateway_adapter_uses_low_latency_json_protocol_without_network(setup, normal_provider_owner):
    service, _, database, settings = setup
    from dataclasses import replace

    settings = replace(settings, model_api_key="unit-test-only", voice_intent_model_name="unit-fast-model")
    sent = []

    def provider(http_request):
        body = json.loads(http_request.content)
        sent.append(body)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "route": "onboarding", "action": "name_candidate", "candidate": "小星", "response_text": "想叫我小星，对吗？",
            }, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 22, "completion_tokens": 12},
        })

    async def run():
        gateway = ModelGateway(settings, transport=httpx.MockTransport(provider))
        try:
            service.gateway, service.settings = gateway, settings
            result = await service.interpret("session-a", request())
            assert result.source == "cloud"
        finally:
            await gateway.close()

    asyncio.run(run())
    assert sent[0]["model"] == "unit-fast-model"
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert len(sent) == 1
    with database.connect() as connection:
        audit = connection.execute("SELECT status,model,prompt_tokens FROM model_call WHERE purpose='voice_interaction'").fetchone()
        assert tuple(audit) == ("completed", "unit-fast-model", 22)


def test_ended_session_cannot_replay_an_initialization_action(setup):
    service, gateway, database, _ = setup
    asyncio.run(service.interpret("session-a", request()))
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id='session-a'")
        connection.commit()
    with pytest.raises(VoiceInteractionConflictError, match="SESSION_INACTIVE"):
        asyncio.run(service.interpret("session-a", request()))
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("session_id,payload", [
    ("session-b", request()),
    ("session-a", request("换个名字小月")),
    ("session-a", request(phase="tutorial")),
    ("session-a", request(wake_word="小月")),
])
def test_action_id_scope_and_payload_conflicts(setup, session_id, payload):
    service, gateway, _, _ = setup
    asyncio.run(service.interpret("session-a", request()))
    with pytest.raises(VoiceInteractionConflictError, match="ACTION_CONFLICT"):
        asyncio.run(service.interpret(session_id, payload))
    assert len(gateway.requests) == 1


def test_invalid_session_has_no_cloud_or_audit_side_effects(setup):
    service, gateway, database, _ = setup
    with pytest.raises(UnknownSessionError):
        asyncio.run(service.interpret("missing", request()))
    assert not gateway.requests
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action LIKE 'voice_interaction_%'").fetchone()[0] == 0


def test_concurrent_requests_have_one_durable_claim(setup):
    service, gateway, _, settings = setup

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def hold():
            started.set()
            await release.wait()

        gateway.hook = hold
        first = asyncio.create_task(service.interpret("session-a", request()))
        await started.wait()
        other = CloudVoiceInteractionService(StudyRepository(Database(settings.database_path)), gateway, settings, lambda: True)
        with pytest.raises(VoiceInteractionConflictError, match="PENDING"):
            await other.interpret("session-a", request())
        release.set()
        response = await first
        assert await other.interpret("session-a", request()) == response

    asyncio.run(run())
    assert len(gateway.requests) == 1


def test_abandoned_claim_never_resubmits_after_restart(setup):
    service, gateway, database, settings = setup
    service._claim("session-a", request())
    with database.connect() as connection:
        connection.execute("UPDATE cloud_voice_interaction SET created_at=?", (
            (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        ))
        connection.commit()
    reopened = CloudVoiceInteractionService(StudyRepository(Database(settings.database_path)), gateway, settings, lambda: True)
    with pytest.raises(VoiceInteractionConflictError, match="OUTCOME_UNKNOWN"):
        asyncio.run(reopened.interpret("session-a", request()))
    assert not gateway.requests


def test_cancellation_is_persisted_without_second_cloud_attempt(setup):
    service, gateway, _, _ = setup

    async def run():
        started = asyncio.Event()

        async def hold():
            started.set()
            await asyncio.Event().wait()

        gateway.hook = hold
        task = asyncio.create_task(service.interpret("session-a", request()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await service.interpret("session-a", request())).source == "unavailable"

    asyncio.run(run())
    assert len(gateway.requests) == 1


def test_slow_cloud_call_times_out_and_cannot_be_rebilled(setup, monkeypatch):
    service, gateway, database, _ = setup
    monkeypatch.setattr("home_study_agent.cloud_voice_interaction.CLOUD_TIMEOUT_SECONDS", 0.01)

    async def slow():
        await asyncio.sleep(5)

    gateway.hook = slow
    response = asyncio.run(service.interpret("session-a", request()))
    assert response.source == "unavailable"
    assert asyncio.run(service.interpret("session-a", request())) == response
    assert len(gateway.requests) == 1
    with database.connect() as connection:
        audit = connection.execute("SELECT error_code,latency_ms FROM model_call WHERE purpose='voice_interaction'").fetchone()
        assert audit["error_code"] == "VOICE_INTERACTION_TIMEOUT" and audit["latency_ms"] >= 10


@pytest.mark.parametrize("change", ["child", "consent", "end"])
def test_scope_and_consent_rechecked_after_model_response(setup, change):
    service, gateway, database, _ = setup

    async def mutate():
        if change == "consent":
            service.cloud_text_allowed = lambda: False
        else:
            with database.connect() as connection:
                query = "UPDATE daily_session SET child_id='child-junior-3' WHERE session_id='session-a'" if change == "child" else "UPDATE daily_session SET status='completed' WHERE session_id='session-a'"
                connection.execute(query)
                connection.commit()

    gateway.hook = mutate
    assert asyncio.run(service.interpret("session-a", request())).source == "unavailable"


def test_http_contract_and_persistent_replay(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "http.db", maintenance_enabled=False)
    gateway = Gateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "start-voice"}).json()["session_id"]
        url = f"/v1/sessions/{session_id}/voice-interaction"
        first = client.post(url, json=request().model_dump(exclude_none=True))
        assert first.status_code == 200
        assert first.json() == {**gateway.output, "source": "cloud"}
        assert client.post(url, json=request().model_dump()).json() == first.json()
        conflict = client.post(url, json=request("叫你小月吧").model_dump())
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "VOICE_INTERACTION_ACTION_CONFLICT"
        assert client.post("/v1/sessions/missing/voice-interaction", json=request().model_dump()).status_code == 404
        for extra_context in ({"memory": {"private": "text"}}, {"child_id": "child-junior-3"}, {"phase": "study"}, {"step": "x" * 65}):
            payload = request().model_dump()
            payload["context"].update(extra_context)
            assert client.post(url, json=payload).status_code == 422
        for values in ({"transcript": "   "}, {"client_action_id": ""}, {"transcript": "x" * 2001}, {"child_id": "child-junior-3"}):
            assert client.post(url, json={**request().model_dump(), **values}).status_code == 422
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert client.post(url, json=request().model_dump()).json() == first.json()
    assert len(gateway.requests) == 1
