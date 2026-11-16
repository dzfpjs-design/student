"""Device commands after naming; opt-in paid probes use synthetic sessions only."""
import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

from home_study_agent.agent_runtime import (
    AgentRuntimeStore, CapabilityRegistry, build_planner_messages, parse_agent_plan_output,
)
from test_agent_executor_evidence_boundary import setup


HISTORY = (
    {"role": "user", "content": "把唤醒词改为小枝小枝"},
    {"role": "assistant", "content": "以后叫我小枝小枝，可以吗？"},
    {"role": "user", "content": "对，就这么叫你"},
    {"role": "assistant", "content": "新的唤醒词已经保存了。"},
)


def seed_naming_objective(database, sid):
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        child = connection.execute("SELECT child_id FROM daily_session WHERE session_id=?", (sid,)).fetchone()[0]
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("""INSERT INTO agent_turn(agent_turn_id,session_id,child_id,transcript,intent,
            response_text,action_json,tool_status,created_at,client_action_id,orchestrator_version)
            VALUES ('synthetic-naming',?,?,'把唤醒词改为小枝小枝','voice.configure_wake_word',
            '新的唤醒词已经保存了。',?,'completed',?,'synthetic-naming','test')""",
            (sid, child, json.dumps({"type": "configure_wake_word", "candidate": "小枝小枝"}), now))
        decision = parse_agent_plan_output(json.dumps({
            "objective": {"mode": "new", "type": "execution", "summary": "修改伙伴唤醒词为小枝小枝",
                          "success_criteria": ["新唤醒词确认并保存"], "phase": "confirm"},
            "next_step": {"capability": "voice.configure_wake_word", "arguments": {"candidate": "小枝小枝"}},
            "spoken_transition": "请确认新名字。", "requires_child_reply": False, "confidence": .99,
        }), "study", CapabilityRegistry())
        saved = store.persist_plan(connection, child_id=child, session_id=sid, agent_turn_id="synthetic-naming",
            client_action_id="synthetic-naming", decision=decision, context_manifest={})
        connection.commit()
    return store, child, saved["objective_id"]


def test_device_planner_contract_does_not_use_status_as_a_mutation_prerequisite(setup):
    database, _, sid = setup
    store, _, _ = seed_naming_objective(database, sid)
    registry = CapabilityRegistry()
    messages = build_planner_messages(store.assemble_context(sid, HISTORY), "你把摄像头也打开", registry)
    system = messages[0]["content"]
    assert "不要先查device.status" in system
    assert "objective.mode=new" in system
    assert "优先继续它；" not in system
    for name in ("observation.resume", "observation.pause"):
        assert "幂等" in registry.require(name, "study").description
        assert "验证" in registry.require(name, "study").description
    assert "不能作为开关命令的前置步骤" in registry.require("device.status", "study").description
    context = json.loads(messages[1]["content"])["context"]
    assert context["active_objective"]["summary"] == "修改伙伴唤醒词为小枝小枝"


def test_device_connect_is_one_multi_device_capability_with_bounded_target(setup):
    database, _, sid = setup
    store, _, _ = seed_naming_objective(database, sid)
    registry = CapabilityRegistry()
    spec = registry.require("device.connect", "check_in", arguments={"device": "all"})
    assert spec.action_type == "connect_devices"
    assert spec.execution_location == "client_device_bridge"
    system = build_planner_messages(store.assemble_context(sid, HISTORY), "连接摄像头和高拍仪", registry)[0]["content"]
    assert "device.connect" in system
    assert "不能拆成只执行一台" in system
    decision = parse_agent_plan_output(json.dumps({
        "objective": {"mode": "new", "type": "execution", "summary": "连接观察摄像头和高拍仪",
                      "success_criteria": ["两台设备均返回真实首帧回执"], "phase": "execute"},
        "next_step": {"capability": "device.connect", "arguments": {"device": "all"}},
        "spoken_transition": "我重新检查两台设备，收到真实画面后再告诉你。",
        "requires_child_reply": False, "confidence": .99,
    }), "check_in", registry)
    assert decision.step.capability.name == "device.connect"
    assert decision.step.arguments == {"device": "all"}


@pytest.mark.parametrize("capability", ["observation.resume", "observation.pause"])
def test_new_device_objective_pauses_unrelated_naming_without_marking_it_complete(setup, capability):
    database, _, sid = setup
    store, child, old_id = seed_naming_objective(database, sid)
    decision = parse_agent_plan_output(json.dumps({
        "objective": {"mode": "new", "type": "execution", "summary": "执行本轮摄像头开关请求",
                      "success_criteria": ["设备桥验证目标状态并返回实际执行回执"], "phase": "execute"},
        "next_step": {"capability": capability, "arguments": {}},
        "spoken_transition": "我来操作摄像头并核对结果。", "requires_child_reply": False, "confidence": .99,
    }), "study", CapabilityRegistry())
    with database.connect() as connection:
        connection.execute("""INSERT INTO agent_turn(agent_turn_id,session_id,child_id,transcript,intent,
            response_text,action_json,tool_status,created_at,client_action_id,orchestrator_version)
            VALUES ('synthetic-device',?,?,'操作摄像头',?,'等待设备验证','{}','pending',?,
                    'synthetic-device','test')""", (sid, child, capability, datetime.now(timezone.utc).isoformat()))
        saved = store.persist_plan(connection, child_id=child, session_id=sid, agent_turn_id="synthetic-device",
            client_action_id="synthetic-device", decision=decision, context_manifest={})
        assert saved["objective_id"] != old_id
        assert connection.execute("SELECT status FROM agent_objective WHERE objective_id=?", (old_id,)).fetchone()[0] == "paused"
        assert connection.execute("SELECT capability_name,status FROM agent_step WHERE plan_id=?", (saved["plan_id"],)).fetchone()[:] == (capability, "planned")


@pytest.mark.skipif(os.environ.get("HSC_RUN_CLOUD_DEVICE_PLANNER") != "1", reason="Explicit paid synthetic cloud probe only")
@pytest.mark.parametrize("utterance,expected", [
    ("你把摄像头也打开", "observation.resume"),
    ("能不能让观察摄像头现在开始工作呀", "observation.resume"),
    ("观察摄像头先关掉吧，我想自己写一会儿", "observation.pause"),
    ("现在摄像头打开了吗，你能看到我吗", "device.status"),
])
def test_real_cloud_device_plan_after_confirmed_name(tmp_path, utterance, expected):
    from voice_dispatch_helpers import capture_dispatch_guard
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings

    credential = subprocess.run(
        ["security", "find-generic-password", "-s", "hsc-deepseek-api-key", "-w"],
        capture_output=True, text=True,
    )
    if credential.returncode or not credential.stdout.strip():
        pytest.fail("Authorized cloud credential unavailable", pytrace=False)
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "synthetic.db", maintenance_enabled=False,
        model_provider="deepseek", model_api_key=credential.stdout.strip(), model_base_url="https://api.deepseek.com",
        model_name="deepseek-v4-pro", voice_intent_model_name="deepseek-v4-flash", agent_runtime_enabled=True,
        model_timeout_seconds=25, data_encryption_key=b"1" * 32)
    del credential
    with TestClient(create_app(settings)) as client:
        sid = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "synthetic-session"}).json()["session_id"]
        orchestrator = client.app.state.agent_orchestrator
        store, child, _ = seed_naming_objective(orchestrator.database, sid)
        planned = client.portal.call(lambda: orchestrator._resolve_agent_plan(
            session_id=sid, child_id=child, transcript=utterance,
            pre_dispatch=capture_dispatch_guard(orchestrator, sid, child, utterance),
            known_child_names=["四年级同学", "初三同学"], recent_dialogue=HISTORY, require_utterance_binding=True,
        ))
        assert planned is not None, "Cloud plan failed; consult synthetic model_call error code"
        decision, _ = planned
        assert decision.step.capability.name == expected
        if expected != "device.status":
            assert decision.objective.mode == "new"
            assert decision.objective.objective_type == "execution"
            assert not decision.requires_child_reply
        with orchestrator.database.connect() as connection:
            call = connection.execute("SELECT model,status,error_code FROM model_call WHERE purpose='agent_plan' ORDER BY rowid DESC LIMIT 1").fetchone()
        assert call and call["status"] == "completed"
