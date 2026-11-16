from copy import deepcopy
from datetime import datetime, timedelta, timezone
import ast
import inspect
import json

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, InvalidDeviceReceiptError, DEFAULT_CAPABILITIES
from home_study_agent.device_registry import DeviceRegistry
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_agent_executor_evidence_boundary import setup, turn


def bridge_turn(setup, key, capability, arguments):
    key = turn(setup, key, capability, arguments)
    spec = CapabilityRegistry().require(capability, "check_in")
    with setup[0].connect() as connection:
        connection.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?",
                           (json.dumps({"type": spec.action_type, **arguments}), key))
        connection.commit()
    return key


def receipt(capability, output):
    spec = CapabilityRegistry().require(capability, "check_in")
    return {"execution_receipt": {"capability": capability, "execution_location": spec.execution_location,
            "action_type": spec.action_type, "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(), "output": output}}


@pytest.mark.parametrize("state", ["check_in", "classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection"])
def test_daily_device_controls_are_discoverable_in_every_stage(state):
    registry = CapabilityRegistry()
    for capability in ("voice.configure_wake_word", "device.connect", "device.status", "observation.pause", "observation.resume", "document.set_occlusion_reminders"):
        assert registry.require(capability, state) in registry.available(state)


@pytest.mark.parametrize("state", ["check_in", "classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection"])
@pytest.mark.parametrize("capability", ["tutor.record_feedback", "tutor.more_hint"])
def test_existing_tutor_controls_are_available_without_changing_execution_boundary(state, capability):
    registry = CapabilityRegistry()
    spec = registry.require(capability, state)
    assert spec in registry.available(state)
    assert spec.execution_location == "client_device_bridge"
    assert spec.required_arguments == (("outcome",) if capability == "tutor.record_feedback" else ())


@pytest.mark.parametrize("candidate", [None, "小枝小枝"])
def test_wake_word_configuration_receipt_proves_only_confirmation_flow_started(setup, candidate):
    database, _, _ = setup
    key = bridge_turn(setup, "wake-config", "voice.configure_wake_word", {} if candidate is None else {"candidate": candidate})
    output = {"configuration_started": True, "phase": "naming" if candidate is None else "confirm_name", "wake_word_saved": False}
    value = receipt("voice.configure_wake_word", output)
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        for change in ({"wake_word_saved": True}, {"configuration_started": 1}, {"phase": "saved"}, {"child_id": "child-junior-3"}):
            invalid = deepcopy(value)
            invalid["execution_receipt"]["output"].update(change)
            with pytest.raises(InvalidDeviceReceiptError):
                store.validate_device_receipt(connection, key, invalid)
        store.complete_turn_execution(connection, key, True, None, value)
        connection.commit()
    reopened = Database(database.path)
    with reopened.connect() as connection:
        saved = json.loads(connection.execute("SELECT result_json FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (key,)).fetchone()[0])
    assert saved == value


@pytest.mark.parametrize("candidate", ["", "知", "太长的唤醒词不能超过十二个字符限制", False, {"name": "小知"}])
def test_wake_word_candidate_has_a_strict_bounded_type(setup, candidate):
    with pytest.raises(ValueError):
        turn(setup, "bad-candidate", "voice.configure_wake_word", {"candidate": candidate})


def test_device_status_waits_for_live_bridge_and_history_never_claims_current_visibility(setup):
    database, repository, sid = setup
    key = turn(setup, "device-status", "device.status", {"device": "all"})
    with pytest.raises(ValueError, match="device or model bridge"):
        AgentCapabilityExecutor(database, repository).execute(key)
    reopened = Database(database.path)
    context = AgentRuntimeStore(reopened).assemble_context(sid, [])
    assert context["device_status"]["camera"]["physical_power"] == "unknown"
    assert context["device_status"]["source"] == "last_known"
    assert context["device_status"]["camera"]["last_frame_at"] is None
    assert context["device_status"]["camera"]["current_visibility"] == "unknown"
    assert context["device_status"]["document_camera"]["monitor_active"] is None


def test_device_snapshot_reads_session_scoped_frames_monitor_and_safe_error_codes(setup):
    database, _, sid = setup
    monitor = DocumentMonitoringService(database)
    monitor.start(sid, "monitor-start")
    now = datetime.now(timezone.utc).isoformat()
    with database.connect() as connection:
        connection.execute("""INSERT INTO observation_qualification(session_id,child_id,status,scene_quality,person_baseline,reason,source,updated_at)
            VALUES (?,'child-primary-4','qualified','ready','established','PRIVATE RAW OBSERVATION','engine',?)""", (sid, now))
        connection.execute("""INSERT INTO device_incident(incident_id,child_id,session_id,device_type,error_code,message,status,occurred_at,client_action_id)
            VALUES ('device-fault','child-primary-4',?,'document_camera','CAMERA_UNAVAILABLE','PRIVATE RAW ERROR','open',?,'device-fault')""", (sid, now))
        connection.commit()
    snapshot = AgentRuntimeStore(database).device_status(sid)
    assert snapshot["camera"]["last_frame_at"] == now
    assert snapshot["camera"]["frame_recently_recorded"] is True
    assert snapshot["camera"]["current_visibility"] == "unknown"
    assert snapshot["document_camera"]["monitor_status"] == "active"
    assert snapshot["document_camera"]["last_error_code"] == "CAMERA_UNAVAILABLE"
    assert "PRIVATE RAW" not in json.dumps(snapshot)
    with database.connect() as connection:
        connection.execute("UPDATE observation_qualification SET child_id='child-junior-3' WHERE session_id=?", (sid,))
        connection.execute("UPDATE device_incident SET child_id='child-junior-3' WHERE session_id=?", (sid,))
        connection.execute("UPDATE document_monitor_session SET child_id='child-junior-3' WHERE session_id=?", (sid,))
        connection.commit()
    isolated = AgentRuntimeStore(database).device_status(sid)
    assert isolated["camera"]["last_frame_at"] is None
    assert isolated["document_camera"]["last_error_code"] is None
    assert isolated["document_camera"]["monitor_status"] == "not_started"


def test_camera_success_requires_frame_and_real_owned_lease_then_failure_can_retry(setup):
    database, _, sid = setup
    key = bridge_turn(setup, "camera-open", "observation.resume", {})
    value = receipt("observation.resume", {"device_type": "camera", "device_status": "active",
                    "frame_confirmed": True, "lease_confirmed": True, "observation_status": "enabled"})
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        with pytest.raises(InvalidDeviceReceiptError):
            store.validate_device_receipt(connection, key, value)
        store.complete_turn_execution(connection, key, False, "CAMERA_UNAVAILABLE", None)
        connection.commit()
    DeviceRegistry(database).register(sid, "camera", "test-camera", {})
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET observation_status='enabled' WHERE session_id=?", (sid,))
        connection.commit()
        invalid = deepcopy(value)
        invalid["execution_receipt"]["output"]["frame_confirmed"] = False
        with pytest.raises(InvalidDeviceReceiptError):
            store.validate_device_receipt(connection, key, invalid)
        store.complete_turn_execution(connection, key, True, None, value)
        connection.commit()
        assert connection.execute("SELECT status FROM agent_step").fetchone()[0] == "completed"


def test_connect_both_devices_requires_two_owned_live_receipts_and_saved_states(setup):
    database, _, sid = setup
    key = bridge_turn(setup, "connect-both", "device.connect", {"device": "all"})
    now = datetime.now(timezone.utc).isoformat()
    DeviceRegistry(database).register(sid, "camera", "camera-system-id", {})
    DeviceRegistry(database).register(sid, "document_camera", "document-system-id", {})
    DocumentMonitoringService(database).start(sid, "connect-both-monitor")
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET observation_status='enabled' WHERE session_id=?", (sid,))
        connection.commit()
    output = {
        "requested_device": "all", "connected_at": now,
        "devices": [
            {"status": "connected", "device_type": "camera", "device_status": "active",
             "frame_confirmed": True, "lease_confirmed": True, "observation_status": "enabled"},
            {"status": "connected", "device_type": "document_camera", "device_status": "active",
             "frame_confirmed": True, "lease_confirmed": True, "persisted_status": "active"},
        ],
    }
    value = receipt("device.connect", output)
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        missing = deepcopy(value)
        missing["execution_receipt"]["output"]["devices"][1] = {
            "status": "failed", "device_type": "document_camera", "error_code": "BOUND_DEVICE_NOT_FOUND"
        }
        with pytest.raises(InvalidDeviceReceiptError, match="DEVICE_RECEIPT_CONNECT_NOT_COMPLETED"):
            store.validate_device_receipt(connection, key, missing)
        store.complete_turn_execution(connection, key, True, None, value)
        connection.commit()
        assert connection.execute("SELECT status FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (key,)).fetchone()[0] == "completed"


def test_executor_rejects_a_plan_whose_child_does_not_own_the_turn_session(setup):
    database, repository, _ = setup
    key = turn(setup, "foreign-plan", "learning.request_response", {"subject": "数学", "prompt": "请回答"})
    with database.connect() as connection:
        connection.execute("UPDATE agent_plan SET child_id='child-junior-3' WHERE agent_turn_id=?", (key,))
        connection.commit()
    with pytest.raises(ValueError, match="OWNER_MISMATCH"):
        AgentCapabilityExecutor(database, repository).execute(key)


def test_all_registered_server_capabilities_have_real_dispatch_branches():
    modules = ("home_study_agent.agent_capability_executor", "home_study_agent.app")
    branches = set()
    for module in modules:
        tree = ast.parse(inspect.getsource(__import__(module, fromlist=["__name__"])))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            direct = isinstance(node.left, ast.Name) and node.left.id == "capability"
            host = (isinstance(node.left, ast.Subscript) and isinstance(node.left.value, ast.Name)
                    and node.left.value.id == "step" and isinstance(node.left.slice, ast.Constant)
                    and node.left.slice.value == "capability_name")
            if direct or host:
                for value in node.comparators:
                    branches.update(child.value for child in ast.walk(value) if isinstance(child, ast.Constant) and isinstance(child.value, str))
    assert {spec.name for spec in DEFAULT_CAPABILITIES if spec.execution_location == "server"} <= branches


def test_occlusion_preference_is_durable_reversible_and_isolated_by_child(setup):
    database, repository, sid = setup
    store = AgentRuntimeStore(database)
    assert store.document_guidance_preferences(sid)["source"] == "default"
    executor = AgentCapabilityExecutor(database, repository)
    disable = turn(setup, "quiet-occlusion", "document.set_occlusion_reminders", {"enabled": False})
    result = executor.execute(disable)
    assert result["result"] == {"occlusion_reminders_enabled": False, "settings_saved": True}
    assert executor.execute(disable) == result
    reopened = Database(database.path)
    preferences = AgentRuntimeStore(reopened).document_guidance_preferences(sid)
    assert preferences["occlusion_reminders_enabled"] is False and preferences["source"] == "saved"
    with database.connect() as connection:
        other_session = dict(connection.execute("SELECT * FROM daily_session WHERE session_id=?", (sid,)).fetchone())
        other_session.update(session_id="other-child-session", child_id="child-junior-3", client_action_id="other-child-session", status="completed")
        connection.execute("INSERT INTO daily_session (" + ",".join(other_session) + ") VALUES (" + ",".join("?" for _ in other_session) + ")", tuple(other_session.values()))
        connection.commit()
    assert store.document_guidance_preferences("other-child-session")["occlusion_reminders_enabled"] is True
    enable = turn(setup, "restore-occlusion", "document.set_occlusion_reminders", {"enabled": True})
    executor.execute(enable)
    assert store.document_guidance_preferences(sid)["occlusion_reminders_enabled"] is True
    executor.execute(disable)
    assert store.document_guidance_preferences(sid)["occlusion_reminders_enabled"] is True


@pytest.mark.parametrize("enabled", ["false", 0, None, {}, []])
def test_occlusion_toggle_does_not_coerce_model_values(setup, enabled):
    with pytest.raises(ValueError):
        turn(setup, "bad-toggle", "document.set_occlusion_reminders", {"enabled": enabled})


def test_live_device_query_rejects_unowned_or_stale_frames(setup):
    database, _, sid = setup
    key = bridge_turn(setup, "query-camera", "device.status", {"device": "camera"})
    now = datetime.now(timezone.utc).isoformat()
    device = {"device_type": "camera", "track_live": True, "frame_confirmed": True,
              "last_frame_at": now, "last_error_code": None, "monitor_active": True}
    value = receipt("device.status", {"checked_at": now, "devices": [device]})
    with database.connect() as connection:
        with pytest.raises(InvalidDeviceReceiptError):
            AgentRuntimeStore.validate_device_receipt(connection, key, value)
    DeviceRegistry(database).register(sid, "camera", "query-camera-device", {})
    with database.connect() as connection:
        AgentRuntimeStore.validate_device_receipt(connection, key, value)
        for field, replacement in (("last_frame_at", "2000-01-01T00:00:00+00:00"), ("track_live", False), ("frame_confirmed", False), ("last_error_code", "private raw text"), ("device_type", "document_camera")):
            invalid = deepcopy(value)
            invalid["execution_receipt"]["output"]["devices"][0][field] = replacement
            with pytest.raises(InvalidDeviceReceiptError):
                AgentRuntimeStore.validate_device_receipt(connection, key, invalid)


@pytest.mark.parametrize("completed_offset,checked_offset,frame_offset,allowed", [
    (-7200, -7200, -7200, False),
    (120, 120, 120, False),
    (29, 31, 30, False),
    (-27, -27, -31, False),
    (0, 0, -2, True),
    (-20, -20, -23, True),
    (20, 20, 19, True),
])
def test_device_freshness_binds_server_clock_and_new_turn(
    setup, monkeypatch, completed_offset, checked_offset, frame_offset, allowed,
):
    database, _, sid = setup
    key = bridge_turn(setup, "query-freshness", "device.status", {"device": "camera"})
    now = datetime.now(timezone.utc)

    class ServerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("home_study_agent.agent_runtime.datetime", ServerClock)
    timestamp = lambda offset: (now + timedelta(seconds=offset)).isoformat()
    device = {"device_type": "camera", "track_live": True, "frame_confirmed": True,
              "last_frame_at": timestamp(frame_offset), "last_error_code": None, "monitor_active": True}
    value = receipt("device.status", {"checked_at": timestamp(checked_offset), "devices": [device]})
    value["execution_receipt"]["completed_at"] = timestamp(completed_offset)
    DeviceRegistry(database).register(sid, "camera", "freshness-camera-device", {})
    with database.connect() as connection:
        connection.execute("UPDATE agent_turn SET created_at=? WHERE agent_turn_id=?", (timestamp(0), key))
        if allowed:
            AgentRuntimeStore.validate_device_receipt(connection, key, value)
        else:
            with pytest.raises(InvalidDeviceReceiptError, match="DEVICE_RECEIPT_.*STALE"):
                AgentRuntimeStore.validate_device_receipt(connection, key, value)


def test_completed_device_receipt_replays_after_freshness_window_without_rewriting(setup, monkeypatch):
    from home_study_agent.agent_orchestrator import AgentOrchestrator

    database, _, sid = setup
    key = bridge_turn(setup, "query-freshness-replay", "device.status", {"device": "camera"})
    now = datetime.now(timezone.utc)
    device = {"device_type": "camera", "track_live": True, "frame_confirmed": True,
              "last_frame_at": now.isoformat(), "last_error_code": None, "monitor_active": True}
    value = receipt("device.status", {"checked_at": now.isoformat(), "devices": [device]})
    DeviceRegistry(database).register(sid, "camera", "freshness-replay-camera", {})
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.database = database
    orchestrator.agent_runtime = AgentRuntimeStore(database)
    first = orchestrator.complete_turn(key, "设备状态已核对。", "completed", None, value)
    with database.connect() as connection:
        before = dict(connection.execute("SELECT * FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (key,)).fetchone())

    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(hours=2)

    monkeypatch.setattr("home_study_agent.agent_runtime.datetime", LaterClock)
    replay = orchestrator.complete_turn(key, "不应覆盖已完成结果。", "completed", None, value)
    assert replay == first
    with database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM agent_step WHERE step_id=?", (before["step_id"],)).fetchone()) == before


@pytest.mark.parametrize("capability", ["controlled_content.stop", "controlled_game.stop"])
def test_stop_owned_activities_is_not_blocked_by_current_page_stage(capability):
    assert CapabilityRegistry().require(capability, "check_in").allowed_states == ()


def test_urgent_parent_action_alias_can_acknowledge_prompt_without_claiming_contact(setup):
    database, _, _ = setup
    key = bridge_turn(setup, "urgent-parent", "safety.find_parent", {})
    value = receipt("safety.find_parent", {"parent_contact_prompted": True})
    value["execution_receipt"]["action_type"] = "find_parent_urgent"
    with database.connect() as connection:
        connection.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?", (json.dumps({"type": "find_parent_urgent"}), key))
        connection.commit()
        AgentRuntimeStore.validate_device_receipt(connection, key, value)


@pytest.mark.parametrize("capability", ["parent.unlock", "parent.delete_history", "parent.update_model_policy", "parent.set_pin"])
def test_models_cannot_call_unregistered_parent_authority(setup, capability):
    with pytest.raises(ValueError, match="unknown capability"):
        turn(setup, "parent-escalation", capability, {})


def test_repeat_replays_the_previous_actual_response_not_the_model_transition(setup):
    database, repository, _ = setup
    first = turn(setup, "previous-real-answer", "learning.request_response", {"subject": "数学", "prompt": "请说说两种解法有什么区别。"})
    executor = AgentCapabilityExecutor(database, repository)
    result = executor.execute(first)
    with database.connect() as connection:
        connection.execute("UPDATE agent_turn SET response_text=?,tool_status='completed' WHERE agent_turn_id=?", (result["response_text"], first))
        connection.commit()
    replay = turn(setup, "repeat-actual", "speech.repeat", {})
    repeated = executor.execute(replay)
    assert repeated["response_text"] == result["response_text"]
    assert repeated["result"]["replayed_turn_id"] == first
    assert repeated["result"]["local_only"] is True
    assert executor.execute(replay) == repeated


def test_repeat_without_an_actual_completed_response_is_honest(setup):
    database, repository, _ = setup
    key = turn(setup, "repeat-nothing", "speech.repeat", {})
    result = AgentCapabilityExecutor(database, repository).execute(key)
    assert result["result"]["status"] == "no_previous_response"
    assert result["evidence"] == []


def test_repeat_skips_echo_silent_and_previous_repeat_turns(setup):
    database, repository, _ = setup
    first = turn(setup, "actual-speech", "learning.request_response", {"subject": "数学", "prompt": "实际回复"})
    echo = turn(setup, "echo-silent", "learning.request_response", {"subject": "数学", "prompt": "不应播放的回声"})
    repeated = turn(setup, "old-repeat", "speech.repeat", {})
    with database.connect() as connection:
        connection.execute("UPDATE agent_turn SET tool_status='completed',response_text='实际回复' WHERE agent_turn_id=?", (first,))
        connection.execute("UPDATE agent_turn SET tool_status='completed',intent='speech_echo_ignored',response_text='不应播放的回声',action_json=? WHERE agent_turn_id=?", (json.dumps({"type": "none", "suppress_speech": True}), echo))
        connection.execute("UPDATE agent_turn SET tool_status='completed',response_text='重播的过渡句',action_json=? WHERE agent_turn_id=?", (json.dumps({"type": "repeat_response"}), repeated))
        connection.commit()
    key = turn(setup, "repeat-now", "speech.repeat", {})
    result = AgentCapabilityExecutor(database, repository).execute(key)
    assert result["response_text"] == "实际回复"
    assert result["result"]["replayed_turn_id"] == first


def test_profile_switch_receipt_requires_real_child_session_transition(setup):
    database, repository, sid = setup
    DocumentMonitoringService(database)
    key = bridge_turn(setup, "switch-child", "profile.switch", {"target_child_id": "child-junior-3"})
    result = receipt("profile.switch", {"profile_switched": True, "target_child_id": "child-junior-3",
                     "target_session_id": "invented", "previous_session_id": sid})
    with database.connect() as connection:
        with pytest.raises(InvalidDeviceReceiptError):
            AgentRuntimeStore.validate_device_receipt(connection, key, result)
    switched = repository.switch_active_session_child(sid, "child-junior-3", "real-switch")
    result["execution_receipt"]["output"]["target_session_id"] = switched["session_id"]
    with database.connect() as connection:
        AgentRuntimeStore.validate_device_receipt(connection, key, result)
        for field, wrong in (("target_child_id", "child-primary-4"), ("previous_session_id", switched["session_id"]), ("target_session_id", sid), ("profile_switched", 1)):
            invalid = deepcopy(result)
            invalid["execution_receipt"]["output"][field] = wrong
            with pytest.raises(InvalidDeviceReceiptError):
                AgentRuntimeStore.validate_device_receipt(connection, key, invalid)


def test_cloud_behavior_feedback_really_saves_once_for_current_child(setup):
    from home_study_agent.behavior_model import BehaviorModelService
    database, repository, sid = setup
    behavior = BehaviorModelService(database)
    now = datetime.now(timezone.utc).isoformat()
    with database.connect() as connection:
        connection.execute("""INSERT INTO intervention(intervention_id,child_id,session_id,decided_at,candidate_state,message,status)
            VALUES ('pending-feedback','child-primary-4',? ,?,'work_stall','可以换个方法吗','active')""", (sid, now))
        connection.commit()
    key = turn(setup, "cloud-feedback", "behavior.feedback", {"feedback": "declined", "response_kind": "later"})
    executor = AgentCapabilityExecutor(database, repository)
    result = executor.execute(key)
    assert result["result"]["feedback"] == "declined"
    assert executor.execute(key) == result
    with database.connect() as connection:
        row = connection.execute("SELECT feedback,feedback_action_id FROM intervention WHERE intervention_id='pending-feedback'").fetchone()
        assert row["feedback"] == "declined" and row["feedback_action_id"]
    assert behavior.feedback_for_action(row["feedback_action_id"], "child-junior-3", sid) is None
