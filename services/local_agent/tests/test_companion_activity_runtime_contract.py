"""Local Runtime -> real activity journal, with an in-process planner stub."""
import asyncio
import json

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, InvalidAgentPlanError, parse_agent_plan_output
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService, ActivityConflict
from home_study_agent.companion_activity_voice import CompanionActivityVoiceService
from home_study_agent.database import Database
from test_agent_runtime import seed_session, seed_paused_companion_task
from test_agent_continuation import orchestrator_for as bare_orchestrator


QUOTE = "刚才只做了一部分，感觉有点累。"


def orchestrator_for(db, tmp_path, output):
    from home_study_agent.model_gateway import ModelResult
    agent, gateway = bare_orchestrator(db, tmp_path, output)
    complete = gateway.complete
    async def respond(request):
        if request.request_type != 'voice_interaction':
            return await complete(request)
        gateway.requests.append(request)
        args = output['next_step']['arguments']
        return ModelResult(content=json.dumps({'role': 'control', 'control': 'companion.activity_feedback',
            'quote': args['response_quote'], 'confidence': .98,
            'activity_feedback': {k: v for k, v in args.items() if k != 'response_quote'}}),
            provider='test', model='controlled-intent', latency_ms=1)
    gateway.complete = respond
    return agent, gateway


def payload(**arguments):
    return dict(objective={"mode": "none"}, next_step={"capability": "companion.activity_feedback",
        "arguments": {"operation": "feedback", "response_quote": QUOTE, "execution": "partial",
                      "experience": "fatigued", **arguments}},
        spoken_transition="我来记录你刚才实际参与的情况。", requires_child_reply=False,
        after_execution="stop", confidence=.98, utterance_binding={"role": "answer", "quote": QUOTE})


@pytest.fixture
def activity_runtime(tmp_path):
    db = Database(tmp_path / "activity-runtime.db")
    db.initialize()
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    PrivacyLifecycleService(db, tmp_path).initialize()
    sid = seed_session(db)
    runtime = AgentRuntimeStore(db)
    item = seed_paused_companion_task(db, sid)
    source = runtime.start_companion_activity(sid, "movement", "PRIVATE_SOURCE_DO_NOT_UPLOAD", "start", item)
    runtime.end_companion_activity(sid)
    return db, runtime, sid, source, item


def claim(db, turn):
    with db.connect() as c:
        c.execute("UPDATE agent_plan SET status='running' WHERE agent_turn_id=?", (turn,))
        c.execute("UPDATE agent_step SET status='running' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        c.commit()


def test_real_interpret_freezes_local_binding_then_executes_feedback(activity_runtime, tmp_path):
    db, runtime, sid, source, item = activity_runtime
    orchestrator, gateway = orchestrator_for(db, tmp_path, payload())
    turn = asyncio.run(orchestrator.interpret(sid, QUOTE, "voice-feedback", True))
    with db.connect() as c:
        saved = c.execute("SELECT * FROM agent_turn WHERE client_action_id='voice-feedback'").fetchone()
        plan = c.execute("SELECT * FROM agent_plan WHERE agent_turn_id=?", (saved["agent_turn_id"],)).fetchone()
        assert json.loads(saved["action_json"])["cloud_transmission_allowed"] is False
        manifest = json.loads(plan["context_manifest_json"])
        assert manifest["companion_activity_outcome_binding"] == runtime.snapshot_companion_activity_outcome(sid)["binding"]
        assert json.loads(plan["plan_json"])["objective"]["mode"] == "none"
    for request in gateway.requests:
        assert "PRIVATE_SOURCE_DO_NOT_UPLOAD" not in json.dumps(request.messages)
        assert "companion_activity_outcome_binding" not in json.dumps(request.messages)
    claim(db, saved["agent_turn_id"])
    result = CompanionActivityVoiceService(db).execute(saved["agent_turn_id"])
    assert result["activity"]["execution_status"] == "partial"
    assert result["activity"]["feedback"]["payload"]["response_quote"] == QUOTE
    with db.connect() as c:
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id=?", (item,)).fetchone()[0] == "paused"


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_multiple_targets_clarify_without_model_choosing(activity_runtime, tmp_path, count):
    db, runtime, sid, _, item = activity_runtime
    if count == 0:
        with db.connect() as c:
            c.execute("DELETE FROM companion_activity_outcome_event")
            c.execute("DELETE FROM companion_activity_outcome")
            c.commit()
    else:
        runtime.start_companion_activity(sid, "riddle", "Another private activity", "second", item)
        runtime.end_companion_activity(sid)
    orchestrator, _ = orchestrator_for(db, tmp_path, payload())
    asyncio.run(orchestrator.interpret(sid, QUOTE, "ambiguous", True))
    with db.connect() as c:
        row = c.execute("SELECT s.capability_name,p.context_manifest_json FROM agent_step s JOIN agent_plan p USING(plan_id)").fetchone()
        assert row["capability_name"] == "conversation.respond"
        assert "companion_activity_outcome_binding" not in json.loads(row["context_manifest_json"])


@pytest.mark.parametrize("field,value", [("outcome_id", "invented"), ("child_id", "other"),
    ("session_id", "other"), ("revision", 9), ("actor", "parent"), ("execution", "verified"),
    ("experience", "great"), ("operation", "retract")])
def test_model_cannot_supply_authority_or_invent_feedback_enum(field, value):
    with pytest.raises(InvalidAgentPlanError):
        parse_agent_plan_output(json.dumps(payload(**{field: value})), "study", CapabilityRegistry())


def test_non_feedback_cannot_classify_execution():
    with pytest.raises(InvalidAgentPlanError):
        parse_agent_plan_output(json.dumps(payload(operation="request_resume")), "study", CapabilityRegistry())


def test_start_and_outcome_are_atomic_idempotent_after_restart(activity_runtime, monkeypatch):
    db, runtime, sid, source, item = activity_runtime
    restored = AgentRuntimeStore(Database(db.path))
    replay = restored.start_companion_activity(sid, "movement", "PRIVATE_SOURCE_DO_NOT_UPLOAD", "start", item)
    assert replay["activity_id"] == source["activity_id"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event").fetchone()[0] == 1
    def fail(*args, **kwargs):
        raise RuntimeError("journal unavailable")
    monkeypatch.setattr(CompanionActivityOutcomeService, "open", fail)
    with pytest.raises(RuntimeError, match="journal unavailable"):
        runtime.start_companion_activity(sid, "riddle", "New", "failed-open", item)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM companion_activity").fetchone()[0] == 1


def test_revision_change_during_planning_keeps_original_version(activity_runtime, tmp_path):
    db, runtime, sid, _, _ = activity_runtime
    original = runtime.snapshot_companion_activity_outcome(sid)["binding"]
    orchestrator, gateway = orchestrator_for(db, tmp_path, payload())
    complete = gateway.complete
    async def changed(request):
        CompanionActivityOutcomeService(db).feedback(original["child_id"], original["outcome_id"],
            expected_revision=1, client_action_id="concurrent-feedback", execution="unknown", experience="unknown",
            note="Earlier report", evidence_basis="child_report", actor="child")
        return await complete(request)
    gateway.complete = changed
    asyncio.run(orchestrator.interpret(sid, QUOTE, "stale-feedback", True))
    with db.connect() as c:
        row = c.execute("SELECT * FROM agent_plan").fetchone()
        assert json.loads(row["context_manifest_json"])["companion_activity_outcome_binding"] == original
    claim(db, row["agent_turn_id"])
    with pytest.raises(ActivityConflict):
        CompanionActivityVoiceService(db).execute(row["agent_turn_id"])


def test_active_activity_source_never_enters_cloud_planner(activity_runtime, tmp_path):
    from voice_dispatch_helpers import capture_dispatch_guard
    db, runtime, sid, _, item = activity_runtime
    runtime.start_companion_activity(sid, "riddle", "PRIVATE_ACTIVE_SOURCE", "active-second", item)
    orchestrator, gateway = orchestrator_for(db, tmp_path, payload())
    asyncio.run(orchestrator._resolve_agent_plan(session_id=sid, child_id="child-runtime", transcript=QUOTE,
        pre_dispatch=capture_dispatch_guard(orchestrator, sid, 'child-runtime', QUOTE),
        known_child_names=[], recent_dialogue=(), companion_feedback_snapshot=runtime.snapshot_companion_activity_outcome(sid)))
    assert gateway.requests
    for request in gateway.requests:
        assert "PRIVATE_ACTIVE_SOURCE" not in json.dumps(request.messages)


def test_turn_limit_only_ends_activity_and_asks_for_report(activity_runtime, tmp_path):
    db, runtime, sid, _, item = activity_runtime
    source = runtime.start_companion_activity(sid, "movement", "Movement", "turn-limit", item)
    orchestrator, _ = orchestrator_for(db, tmp_path, payload())
    with db.connect() as c:
        c.execute("UPDATE companion_activity SET turn_count=max_turns-1 WHERE activity_id=?", (source["activity_id"],))
        c.commit()
    response = orchestrator._advance_companion_activity_and_resume(sid, "到这里。")
    assert "实际做了哪些" in response
    with db.connect() as c:
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id=?", (item,)).fetchone()[0] == "paused"
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
    states = CompanionActivityOutcomeService(db).list("child-runtime")["items"]
    assert all(state["execution_status"] == "unknown" for state in states)


def test_blocked_completion_and_replay_do_not_run_post_success_hooks(activity_runtime, tmp_path, monkeypatch):
    db, _, sid, _, _ = activity_runtime
    orchestrator, _ = orchestrator_for(db, tmp_path, payload())
    asyncio.run(orchestrator.interpret(sid, QUOTE, "blocked-turn", True))
    with db.connect() as c:
        turn = c.execute("SELECT agent_turn_id FROM agent_turn WHERE client_action_id='blocked-turn'").fetchone()[0]
    def forbidden(*args, **kwargs):
        pytest.fail("Blocked command reached post-success behavior")
    monkeypatch.setattr(orchestrator, "_release_previous_activity", forbidden)
    monkeypatch.setattr(orchestrator.agent_runtime, "finalize_diagnostic_turn", forbidden)
    monkeypatch.setattr(orchestrator.companion_memory, "observe_turn", forbidden)
    result = {"status": "blocked", "reason": "authorization_changed", "result": {}, "evidence": []}
    orchestrator.complete_turn(turn, "这次操作没有执行。", "completed", None, result)
    restored, _ = orchestrator_for(db, tmp_path, payload())
    monkeypatch.setattr(restored.agent_runtime, "finalize_diagnostic_turn", forbidden)
    restored.complete_turn(turn, "不应覆盖原结果", "completed", None, {"status": "completed"})
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 0


def finish_real_command(db, orchestrator, key):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.repository import StudyRepository
    with db.connect() as c:
        turn = c.execute("SELECT agent_turn_id FROM agent_turn WHERE client_action_id=?", (key,)).fetchone()[0]
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn)
    orchestrator.complete_turn(turn, result["response_text"], "completed", None, result)
    return result


def test_second_active_activity_is_current_not_permanent_history_ambiguity(activity_runtime):
    db, runtime, sid, _, item = activity_runtime
    second = runtime.start_companion_activity(sid, "riddle", "Second", "second", item)
    snapshot = runtime.snapshot_companion_activity_outcome(sid)
    assert snapshot["status"] == "ready"
    current = CompanionActivityOutcomeService(db).get("child-runtime", snapshot["binding"]["outcome_id"])
    assert current["activity"]["source_id"] == second["activity_id"]


def test_deferred_old_activity_does_not_block_new_ended_activity(activity_runtime):
    db, runtime, sid, _, item = activity_runtime
    first = runtime.snapshot_companion_activity_outcome(sid)["binding"]
    CompanionActivityOutcomeService(db).defer_resume("child-runtime", first["outcome_id"], note="Later", actor="child",
        expected_revision=first["expected_revision"], client_action_id="defer")
    runtime.start_companion_activity(sid, "riddle", "Second", "second", item)
    runtime.end_companion_activity(sid)
    snapshot = runtime.snapshot_companion_activity_outcome(sid)
    assert snapshot["status"] == "ready" and snapshot["binding"]["outcome_id"] != first["outcome_id"]


def test_multiple_unresolved_activities_select_locally_after_restart_then_record_feedback(activity_runtime, tmp_path):
    db, runtime, sid, _, item = activity_runtime
    runtime.start_companion_activity(sid, "riddle", "Second", "second", item)
    runtime.end_companion_activity(sid)
    orchestrator, _ = orchestrator_for(db, tmp_path, payload())
    asyncio.run(orchestrator.interpret(sid, QUOTE, "choose-question", True))
    question = finish_real_command(db, orchestrator, "choose-question")
    assert "第1项" in question["response_text"] and "第2项" in question["response_text"]
    original = runtime.snapshot_companion_activity_outcome(sid)["selection"]["candidates"][1]["binding"]
    restored, unused = orchestrator_for(db, tmp_path, payload())
    asyncio.run(restored.interpret(sid, "我说的是第二个", "choose-second", False))
    selected = finish_real_command(db, restored, "choose-second")
    assert "刚才实际做了哪些" in selected["response_text"]
    assert not unused.requests
    assert runtime.snapshot_companion_activity_outcome(sid)["binding"] == original
    asyncio.run(restored.interpret(sid, QUOTE, "feedback-after-choice", True))
    result = finish_real_command(db, restored, "feedback-after-choice")
    assert result["result"]["activity"]["outcome_id"] == original["outcome_id"]
    assert result["result"]["activity"]["feedback"]["payload"]["response_quote"] == QUOTE
    assert all("PRIVATE_SOURCE_DO_NOT_UPLOAD" not in json.dumps(request.messages) for request in unused.requests)


def test_old_selection_version_is_not_rebound_to_changed_activity(activity_runtime, tmp_path):
    db, runtime, sid, _, item = activity_runtime
    runtime.start_companion_activity(sid, "riddle", "Second", "second", item)
    runtime.end_companion_activity(sid)
    orchestrator, _ = orchestrator_for(db, tmp_path, payload())
    asyncio.run(orchestrator.interpret(sid, QUOTE, "choose-question", True))
    finish_real_command(db, orchestrator, "choose-question")
    first = runtime.snapshot_companion_activity_outcome(sid)["selection"]["candidates"][0]["binding"]
    CompanionActivityOutcomeService(db).defer_resume("child-runtime", first["outcome_id"], note="Later", actor="child",
        expected_revision=first["expected_revision"], client_action_id="defer-after-question")
    asyncio.run(orchestrator.interpret(sid, "第一个", "stale-choice", False))
    finish_real_command(db, orchestrator, "stale-choice")
    with db.connect() as c:
        manifest = json.loads(c.execute("SELECT context_manifest_json FROM agent_plan p JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id WHERE t.client_action_id='stale-choice'").fetchone()[0])
        assert "companion_activity_focus_binding" not in manifest
