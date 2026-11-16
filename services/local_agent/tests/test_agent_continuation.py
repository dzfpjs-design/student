import json
import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, parse_agent_plan_output
from home_study_agent.agent_continuation import ContinuationStore, written_item_identities
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelResult
from test_agent_runtime import seed_session


class Gateway:
    configured = True

    def __init__(self, output):
        self.output, self.requests, self.hook = output, [], None

    async def complete(self, request):
        self.requests.append(request)
        if self.hook:
            await self.hook()
        return ModelResult(content=json.dumps(self.output, ensure_ascii=False), provider="test", model="test",
                           latency_ms=1, prompt_tokens=30, completion_tokens=20)


def plan(*, after="replan", waiting=False):
    return parse_agent_plan_output(json.dumps({
        "objective": {"mode": "new", "type": "execution", "summary": "记录状态后安排学习",
                      "success_criteria": ["状态已保存", "学习安排已确认"], "phase": "prepare"},
        "next_step": {"capability": "checkin.update_context", "arguments": {"energy_level": "high"}},
        "spoken_transition": "先记下你的状态。", "requires_child_reply": waiting,
        "after_execution": after, "confidence": .95,
        "continuation_scope": [{"capability": "session.transition", "arguments": {"target": "classroom_recall"},
                                "request_quote": "安排学习"}],
    }), "check_in", CapabilityRegistry())


def setup(tmp_path, *, waiting=False, after="replan"):
    db = Database(tmp_path / "continuation.db")
    db.initialize()
    session = seed_session(db)
    runtime = AgentRuntimeStore(db)
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
                  response_text,action_json,orchestrator_version,created_at,client_action_id)
                  VALUES('root','child-runtime',?,'精神很好，记下来再安排学习','agent_runtime',
                  '先记录状态','{}','test',datetime('now'),'root-action')""", (session,))
        runtime.persist_plan(c, child_id="child-runtime", session_id=session, agent_turn_id="root",
                             client_action_id="root-action", decision=plan(waiting=waiting, after=after), context_manifest={})
        c.commit()
    return db, session, runtime


def complete(db, runtime, success=True, result=None):
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET tool_status=? WHERE agent_turn_id='root'", ("completed" if success else "failed",))
        runtime.complete_turn_execution(c, "root", success, None if success else "FAILED", result or {"status": "saved"})
        c.commit()


def test_result_creates_one_durable_continuation(tmp_path):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    complete(db, runtime)
    queue = ContinuationStore(Database(db.path))
    claimed = queue.claim(session, "root")
    assert claimed["status"] == "planning"
    assert claimed["original_utterance"] == "精神很好，记下来再安排学习"
    assert queue.claim(session, "root")["status"] == "planning_in_progress"
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM agent_continuation").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == 1


@pytest.mark.parametrize("waiting,after,success", [(True,"replan",True),(False,"stop",True),(False,"replan",False)])
def test_waiting_stop_and_failure_never_auto_continue(tmp_path, waiting, after, success):
    db, session, runtime = setup(tmp_path, waiting=waiting, after=after)
    complete(db, runtime, success)
    assert ContinuationStore(db).claim(session, "root")["status"] == "none"


def test_result_waiting_override_and_privacy_never_enqueue(tmp_path):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime, result={"requires_child_reply": True})
    assert ContinuationStore(db).claim(session, "root")["status"] == "none"


@pytest.mark.parametrize("change", ["new_turn", "cancel", "session_end", "local_only"])
def test_stale_cancelled_and_private_continuation_does_not_run(tmp_path, change):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    with db.connect() as c:
        if change == "new_turn":
            c.execute("""INSERT INTO agent_turn SELECT 'new',child_id,session_id,'换个话题',intent,
                         response_text,action_json,orchestrator_version,tool_status,tool_error_code,
                         completed_at,created_at,'new-action' FROM agent_turn WHERE agent_turn_id='root'""")
        elif change == "cancel":
            c.execute("UPDATE agent_objective SET status='cancelled'")
        elif change == "session_end":
            c.execute("UPDATE daily_session SET status='completed',state='completed'")
        else:
            c.execute("UPDATE agent_turn SET action_json=?", (json.dumps({"cloud_transmission_allowed": False}),))
        c.commit()
    assert ContinuationStore(db).claim(session, "root")["status"] in {"superseded", "blocked"}


def test_lease_and_generation_checked_inside_result_transaction(tmp_path):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    queue = ContinuationStore(db)
    work = queue.claim(session, "root")
    queue.invalidate(session)
    with db.connect() as c:
        assert not queue.current(c, work)
        assert c.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == 1


def test_wrong_session_cannot_claim_work(tmp_path):
    db, _, runtime = setup(tmp_path)
    complete(db, runtime)
    assert ContinuationStore(db).claim("other-session", "root")["status"] == "none"


def orchestrator_for(db, tmp_path, output):
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    from home_study_agent.config import Settings
    from home_study_agent.repository import StudyRepository
    gateway = Gateway(output)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    return AgentOrchestrator(db, StudyRepository(db), gateway, settings), gateway


def next_plan():
    return {
        "objective": {"mode": "continue", "type": "execution", "phase": "execute"},
        "next_step": {"capability": "session.transition", "arguments": {"target": "classroom_recall"}},
        "spoken_transition": "状态记好了，接下来回顾今天课堂学的内容。",
        "requires_child_reply": True, "after_execution": "stop", "confidence": .98,
    }


def test_orchestrator_result_planning_executes_and_persists_without_fake_child_turn(tmp_path):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.repository import StudyRepository
    db, session, runtime = setup(tmp_path)
    orchestrator, gateway = orchestrator_for(db, tmp_path, next_plan())
    executor = AgentCapabilityExecutor(db, StudyRepository(db))
    first = executor.execute("root")
    orchestrator.complete_turn("root", first["response_text"], "completed", None, first)
    result = asyncio.run(orchestrator.continue_execution(session, "root", True))
    assert result["status"] == "ready"
    following = result["turn"]
    assert following["transcript"] == "" and following["intent"] == "runtime_continuation"
    assert following["after_execution"] == "stop"
    payload = json.loads(gateway.requests[0].messages[-1]["content"])
    assert payload["trigger"]["type"] == "tool_result"
    assert "child_utterance" not in payload
    assert "original_child_request" in payload
    replay = asyncio.run(orchestrator.continue_execution(session, "root", True))
    assert replay["turn"]["agent_turn_id"] == following["agent_turn_id"]
    assert len(gateway.requests) == 1
    executed = executor.execute(following["agent_turn_id"])
    orchestrator.complete_turn(following["agent_turn_id"], executed["response_text"], "completed", None, executed)
    with Database(db.path).connect() as c:
        assert c.execute("SELECT state FROM daily_session").fetchone()[0] == "classroom_recall"
        assert c.execute("SELECT energy_level FROM session_planning_context").fetchone()[0] == "high"
        assert c.execute("SELECT count(*) FROM agent_turn WHERE transcript<>''").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM agent_continuation").fetchone()[0] == 1


def test_new_child_input_during_model_call_cannot_persist_a_result_turn(tmp_path):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    orchestrator, gateway = orchestrator_for(db, tmp_path, next_plan())
    async def interrupt():
        runtime.continuations.invalidate(session)
    gateway.hook = interrupt
    result = asyncio.run(orchestrator.continue_execution(session, "root", True))
    assert result["status"] == "superseded"
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == 1


def test_disabled_cloud_cannot_replay_a_prepared_action(tmp_path):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    orchestrator, gateway = orchestrator_for(db, tmp_path, next_plan())
    assert asyncio.run(orchestrator.continue_execution(session, "root", True))["status"] == "ready"
    assert asyncio.run(orchestrator.continue_execution(session, "root", False))["status"] == "blocked"
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("change", ["snooze", "malformed_snooze", "expired", "ended", "private_root", "private_next"])
def test_cached_result_rechecks_context_before_execution(tmp_path, change):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    orchestrator, gateway = orchestrator_for(db, tmp_path, next_plan())
    result = asyncio.run(orchestrator.continue_execution(session, "root", True))
    assert result["status"] == "ready"
    with db.connect() as c:
        if change == "snooze":
            c.execute("UPDATE daily_session SET snooze_until=datetime('now','+1 hour')")
        elif change == "malformed_snooze":
            c.execute("UPDATE daily_session SET snooze_until='bad timestamp'")
        elif change == "ended":
            c.execute("UPDATE daily_session SET state='completed'")
        elif change == "expired":
            c.execute("UPDATE agent_continuation SET created_at=datetime('now','-1 hour')")
        else:
            target = 'root' if change == 'private_root' else result['turn']['agent_turn_id']
            c.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?", (json.dumps({"cloud_transmission_allowed": False}), target))
        c.commit()
    assert asyncio.run(orchestrator.continue_execution(session, "root", True))["status"] == "superseded"
    assert len(gateway.requests) == 1


def test_planner_cannot_use_followup_to_open_an_unrequested_camera(tmp_path):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    payload = next_plan()
    payload["next_step"] = {"capability": "observation.resume", "arguments": {"device": "observation_camera"}}
    orchestrator, _ = orchestrator_for(db, tmp_path, payload)
    result = asyncio.run(orchestrator.continue_execution(session, "root", True))
    assert result["status"] == "blocked"
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == 1


@pytest.mark.parametrize("scope", [[], [{"capability": "session.transition", "arguments": {"target": "classroom_recall"}, "request_quote": "我没有说过的请求"}]])
def test_missing_or_fabricated_scope_never_auto_runs(tmp_path, scope):
    db, session, runtime = setup(tmp_path)
    with db.connect() as c:
        saved = json.loads(c.execute("SELECT plan_json FROM agent_plan").fetchone()[0])
        saved["continuation_scope"] = scope
        c.execute("UPDATE agent_plan SET plan_json=?", (json.dumps(saved),))
        c.commit()
    complete(db, runtime)
    result = ContinuationStore(db).claim(session, "root")
    assert result == {"status": "blocked", "reason": "CONTINUATION_SCOPE_MISSING"}


def test_http_continuation_round_trip_and_backend_restart(tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    application = create_app(settings)
    gateway = Gateway(next_plan())
    application.state.agent_orchestrator.model_gateway = gateway
    with TestClient(application) as client:
        result = client.post(f"/v1/sessions/{session}/agent-turns/root/continue")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "ready", result.json()
        generated_id = result.json()["turn"]["agent_turn_id"]
    reopened = create_app(settings)
    reopened.state.agent_orchestrator.model_gateway = gateway
    with TestClient(reopened) as client:
        result = client.post(f"/v1/sessions/{session}/agent-turns/root/continue")
        assert result.json()["turn"]["agent_turn_id"] == generated_id
        assert len(gateway.requests) == 1
        assert client.post("/v1/sessions/unknown/agent-turns/root/continue").status_code == 404


def test_real_privacy_revocation_during_model_response_is_durable(tmp_path):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    orchestrator, gateway = orchestrator_for(db, tmp_path, next_plan())
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    async def revoke():
        privacy.update_preferences({"cloud_model_text_allowed": False})
        privacy.update_preferences({"cloud_model_text_allowed": True})
    gateway.hook = revoke
    assert asyncio.run(orchestrator.continue_execution(session, "root", True))["status"] == "superseded"
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == 1


def test_cancelled_failed_result_step_cannot_be_retried(tmp_path):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor, UnsupportedServerCapabilityError
    from home_study_agent.repository import StudyRepository
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    orchestrator, _ = orchestrator_for(db, tmp_path, next_plan())
    next_id = asyncio.run(orchestrator.continue_execution(session, "root", True))["turn"]["agent_turn_id"]
    with db.connect() as c:
        c.execute("UPDATE agent_plan SET status='failed' WHERE agent_turn_id=?", (next_id,))
        c.execute("UPDATE agent_step SET status='failed' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (next_id,))
        c.execute("UPDATE agent_turn SET tool_status='failed' WHERE agent_turn_id=?", (next_id,))
        c.commit()
    runtime.continuations.invalidate(session)
    with pytest.raises(UnsupportedServerCapabilityError):
        AgentCapabilityExecutor(db, StudyRepository(db)).execute(next_id)
    with db.connect() as c:
        assert c.execute("SELECT state FROM daily_session").fetchone()[0] == "check_in"


@pytest.mark.parametrize("capability", ["diagnostic.start", "diagnostic.replay", "pet_vocabulary.start"])
def test_question_presenting_capabilities_force_wait_despite_model_flag(capability):
    payload = next_plan()
    payload["objective"]["type"] = "learning"
    payload["next_step"] = {"capability": capability, "arguments": {}}
    payload.update(requires_child_reply=False, after_execution="replan")
    decision = parse_agent_plan_output(json.dumps(payload), "study", CapabilityRegistry())
    assert decision.requires_child_reply


def test_written_item_identity_ignores_duration_and_batch_wrapping():
    first = written_item_identities("homework.add", {"subject": "数学", "title": "练习册第12页", "expected_minutes": 15})
    again = written_item_identities("homework.add_batch", {"items": [
        {"subject": "数学", "title": "练习册 第１２页。", "expected_minutes": 20},
        {"subject": "英语", "title": "背单词"},
    ]})
    assert first.intersection(again)
    assert not first.intersection(written_item_identities("homework.add", {"subject": "数学", "title": "练习册第13页"}))


def test_client_admission_is_once_only_and_revocable(tmp_path):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    orchestrator, _ = orchestrator_for(db, tmp_path, next_plan())
    next_id = asyncio.run(orchestrator.continue_execution(session, "root", True))["turn"]["agent_turn_id"]
    with db.connect() as c:
        c.execute("UPDATE agent_step SET execution_location='client_device_bridge' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (next_id,))
        c.commit()
    assert runtime.continuations.admit_client_execution("another-session", next_id)["status"] == "superseded"
    assert runtime.continuations.admit_client_execution(session, next_id)["status"] == "admitted"
    assert ContinuationStore(Database(db.path)).snapshot(session)["status"] == "awaiting_receipt"
    assert ContinuationStore(Database(db.path)).admit_client_execution(session, next_id)["status"] == "execution_receipt_required"
    runtime.continuations.invalidate(session)
    assert runtime.continuations.admit_client_execution(session, next_id)["status"] == "superseded"


@pytest.mark.parametrize("source", ["root", "objective"])
def test_deleted_source_cascades_derived_internal_turns(tmp_path, source):
    db, session, runtime = setup(tmp_path)
    complete(db, runtime)
    orchestrator, _ = orchestrator_for(db, tmp_path, next_plan())
    next_id = asyncio.run(orchestrator.continue_execution(session, "root", True))["turn"]["agent_turn_id"]
    with db.connect() as c:
        if source == "root":
            c.execute("DELETE FROM agent_turn WHERE agent_turn_id='root'")
        else:
            c.execute("DELETE FROM agent_objective")
        c.commit()
        assert c.execute("SELECT count(*) FROM agent_turn WHERE agent_turn_id=?", (next_id,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM agent_continuation").fetchone()[0] == 0
        assert not ContinuationStore.execution_allowed(c, next_id)
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.skipif(os.environ.get("HSC_REAL_CONTINUATION") != "1", reason="explicit paid synthetic probe only")
def test_official_cloud_result_planning_on_synthetic_request(tmp_path):
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    from home_study_agent.repository import StudyRepository
    key = subprocess.run(["security", "find-generic-password", "-s", "hsc-deepseek-api-key", "-w"], capture_output=True, text=True)
    if key.returncode or not key.stdout.strip():
        pytest.fail("Official credential not available", pytrace=False)
    db, session, runtime = setup(tmp_path)
    original = "精神很好，先记下我的状态，再告诉我摄像头的状态。"
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET transcript=? WHERE agent_turn_id='root'", (original,))
        saved = json.loads(c.execute("SELECT plan_json FROM agent_plan").fetchone()[0])
        saved["continuation_scope"] = [{"capability": "device.status", "arguments": {"device": "camera"}, "request_quote": "告诉我摄像头的状态"}]
        c.execute("UPDATE agent_plan SET plan_json=?", (json.dumps(saved, ensure_ascii=False),))
        c.execute("UPDATE session_planning_context SET energy_level='high'")
        c.commit()
    complete(db, runtime)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        model_provider="deepseek", model_api_key=key.stdout.strip(), model_base_url="https://api.deepseek.com",
                        model_name="deepseek-v4-pro", voice_intent_model_name="deepseek-v4-flash", model_timeout_seconds=25)
    del key
    async def run():
        gateway = ModelGateway(settings)
        try:
            return await AgentOrchestrator(db, StudyRepository(db), gateway, settings).continue_execution(session, "root", True)
        finally:
            await gateway.close()
    result = asyncio.run(run())
    with db.connect() as c:
        call = c.execute("SELECT model,status,error_code,prompt_tokens,completion_tokens FROM model_call ORDER BY rowid DESC LIMIT 1").fetchone()
    assert result["status"] == "ready", {"status": result["status"], "reason": result.get("reason"), "call": dict(call) if call else None}
    assert result["turn"]["runtime_capability"] == "device.status"
    assert result["turn"]["transcript"] == ""
    assert result["turn"]["action"]["device"] == "camera"
    print("Synthetic cloud continuation:", dict(call))


@pytest.mark.skipif(os.environ.get("HSC_REAL_CONTINUATION") != "1", reason="explicit paid synthetic probe only")
def test_official_cloud_parses_and_runs_two_action_request(tmp_path):
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    from home_study_agent.repository import StudyRepository
    key = subprocess.run(["security", "find-generic-password", "-s", "hsc-deepseek-api-key", "-w"], capture_output=True, text=True)
    if key.returncode or not key.stdout.strip():
        pytest.fail("Official credential not available", pytrace=False)
    db = Database(tmp_path / "real-two-actions.db")
    db.initialize()
    session = seed_session(db)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        model_provider="deepseek", model_api_key=key.stdout.strip(), model_base_url="https://api.deepseek.com",
                        model_name="deepseek-v4-pro", voice_intent_model_name="deepseek-v4-flash", model_timeout_seconds=25)
    del key
    repository = StudyRepository(db)
    async def run():
        gateway = ModelGateway(settings)
        orchestrator = AgentOrchestrator(db, repository, gateway, settings)
        try:
            first = await orchestrator.interpret(session, "我今天精力很充足，先记下这个状态，再查询观察摄像头当前的状态。", "synthetic-two-actions", True)
            assert first.get("runtime_capability") == "checkin.update_context", first.get("runtime_capability")
            assert first.get("after_execution") == "replan"
            result = AgentCapabilityExecutor(db, repository).execute(first["agent_turn_id"])
            orchestrator.complete_turn(first["agent_turn_id"], result["response_text"], "completed", None, result)
            following = await orchestrator.continue_execution(session, first["agent_turn_id"], True)
            assert following["status"] == "ready", following.get("reason")
            assert following["turn"]["runtime_capability"] == "device.status"
            assert following["turn"]["action"]["device"] == "camera"
        finally:
            await gateway.close()
    asyncio.run(run())
    with db.connect() as c:
        assert c.execute("SELECT energy_level FROM session_planning_context").fetchone()[0] == "high"
        assert c.execute("SELECT count(*) FROM agent_turn WHERE transcript<>''").fetchone()[0] == 1
        calls = c.execute("SELECT model,status,error_code,prompt_tokens,completion_tokens FROM model_call ORDER BY rowid").fetchall()
    print("Synthetic full request:", [dict(row) for row in calls])
