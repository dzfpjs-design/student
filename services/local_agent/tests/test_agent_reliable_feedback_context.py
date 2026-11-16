import asyncio
from types import SimpleNamespace
import json

import pytest

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.agent_runtime import (
    AgentRuntimeStore, CapabilityRegistry, InvalidAgentPlanError,
    context_manifest, parse_agent_plan_output,
)
from home_study_agent.database import Database
from home_study_agent.learning_feedback import build_learning_feedback
from home_study_agent.learning_state import LearningStateService
from home_study_agent.model_gateway import ModelResult
from home_study_agent.repository import StudyRepository


CHILD = "child-primary-4"
ITEM = "diag-p4-arithmetic-closed-1"
NODE = "node-p4-math-arithmetic"


@pytest.fixture
def runtime(tmp_path):
    database = Database(tmp_path / "reliable-runtime.db")
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    learning = LearningStateService(database)
    learning.seed_catalog()
    sid = repository.start_session(CHILD, "session")["session_id"]
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.execute("""INSERT INTO mastery_state
            (child_id,knowledge_node_id,mastery_probability,evidence_count,hint_dependency,status_confidence,algorithm_version,updated_at)
            VALUES (?, ?, .99, 100, 0, 1, 'legacy', datetime('now'))""", (CHILD, NODE))
        connection.execute("""INSERT INTO agent_objective
            (objective_id,child_id,session_id,objective_type,summary,success_criteria_json,phase,status,created_at,updated_at)
            VALUES ('learning-objective',?,?,'learning','整数运算','["独立解答"]','check','active',datetime('now'),datetime('now'))""",
            (CHILD, sid))
        connection.commit()
    return database, repository, learning, sid, AgentRuntimeStore(database)


def planner_payload(mode="complete", objective_type="learning", phase="completed", *, readonly=False):
    return json.dumps({
        "objective": {"mode": mode, "type": objective_type, "phase": phase,
                      "summary": "整数运算", "success_criteria": ["独立解答"]},
        "next_step": {"capability": 'planning.describe' if readonly else "session.transition" if objective_type == "execution" else "learning.explain",
                      "arguments": {} if readonly else {'target': 'reflection'} if objective_type == "execution" else {"question": "整数运算", "subject": "数学"},
                      "reason": "旧掌握概率为0.99，已经学会", "expected_evidence": []},
        "spoken_transition": "你已经掌握，学习目标完成了。",
        "requires_child_reply": False, "confidence": .99,
    }, ensure_ascii=False)


class PlannerGateway:
    configured = True

    def __init__(self, content):
        self.content = content
        self.requests = []

    async def complete(self, request):
        assert request.request_type == "agent_plan"
        self.requests.append(request)
        return ModelResult(content=self.content, provider="test", model="test", latency_ms=1)


def resolve(database, repository, sid, content):
    from voice_dispatch_helpers import capture_dispatch_guard
    gateway = PlannerGateway(content)
    orchestrator = AgentOrchestrator(database, repository, gateway,
                                    SimpleNamespace(model_provider="test", model_name="test"))
    result = asyncio.run(orchestrator._resolve_agent_plan(
        session_id=sid, child_id=CHILD, transcript="接下来呢", known_child_names=[], recent_dialogue=(),
        pre_dispatch=capture_dispatch_guard(orchestrator, sid, CHILD, '接下来呢'),
    ))
    assert result is not None
    return result, gateway


def persist(database, store, sid, decision, manifest):
    with database.connect() as connection:
        connection.execute("""INSERT INTO agent_turn
            (agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
             orchestrator_version,created_at,client_action_id)
            VALUES ('guarded-turn',?,?,'接下来呢','test',?,'{}','test',datetime('now'),'guarded-turn')""",
            (CHILD, sid, decision.spoken_transition))
        result = store.persist_plan(connection, child_id=CHILD, session_id=sid,
                                    agent_turn_id="guarded-turn", client_action_id="guarded-turn",
                                    decision=decision, context_manifest=manifest)
        connection.commit()
        return result


def test_legacy_high_probability_yields_explicit_local_verification_context(runtime):
    database, _, _, sid, store = runtime
    context = store.assemble_context(sid, ())
    local = context["relevant_learning_state"]
    assert local["cloud_transmission_allowed"] is False
    assert local["status"] == "needs_baseline" and local["nodes"] == []
    assert local["next_action"] == "verify_then_independent_check"
    assert local["requires_independent_check"] is True
    assert local["objective_completion_allowed"] is False
    assert "mastery_probability" not in json.dumps(local)
    assert context["longitudinal_child_model"]["cloud_transmission_allowed"] is False
    assert context_manifest(context)["learning_state_count"] == 0
    assert AgentRuntimeStore(Database(database.path)).assemble_context(sid, ())["relevant_learning_state"] == local


def test_runtime_consumes_current_owned_projection_and_drops_rejected_sources(runtime):
    database, repository, learning, sid, store = runtime
    attempt = learning.submit_diagnostic(sid, ITEM, "12", "wrong-answer")
    context = store.assemble_context(sid, ())
    with database.connect() as connection:
        projection = build_learning_feedback(connection, CHILD)
    local = context["relevant_learning_state"]
    assert local["evidence_fingerprint"] == projection["evidence_fingerprint"]
    assert local["nodes"] == projection["nodes"]
    assert local["nodes"][0]["evidence_refs"] == [attempt["evidence_id"]]
    assert local["next_action"] == "clarify_then_recheck"
    assert context_manifest(context)["learning_state_count"] == 1
    with database.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        connection.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
        connection.commit()
    refreshed = AgentRuntimeStore(Database(database.path)).assemble_context(sid, ())["relevant_learning_state"]
    assert refreshed["nodes"] == [] and refreshed["next_action"] == "verify_then_independent_check"
    assert refreshed["evidence_fingerprint"] != local["evidence_fingerprint"]
    other = repository.start_session("child-junior-3", "other-session")["session_id"]
    assert store.assemble_context(other, ())["relevant_learning_state"]["nodes"] == []


@pytest.mark.parametrize("mode,objective_type,phase", [
    ("complete", "learning", "completed"), ("complete", None, "completed"),
    ("complete", "execution", "completed"), ("continue", "learning", "completed"),
])
def test_real_cloud_path_cannot_complete_learning_from_legacy_probability(runtime, mode, objective_type, phase):
    database, repository, learning, sid, store = runtime
    attempt = learning.submit_diagnostic(sid, ITEM, "12", "private-answer")
    (decision, manifest), gateway = resolve(database, repository, sid, planner_payload(mode, objective_type, phase))
    assert decision.objective.mode == "continue" and decision.objective.phase == "check"
    assert decision.step.capability.name == "learning.request_response"
    assert decision.requires_child_reply is True
    assert manifest["local_learning_completion_guard"] is True
    assert "已经掌握" not in decision.spoken_transition
    assert "独立" in decision.step.arguments["prompt"]
    sent = json.loads(gateway.requests[0].messages[1]["content"])["context"]
    assert "relevant_learning_state" not in sent
    assert "longitudinal_child_model" not in sent
    serialized = json.dumps(sent)
    assert "mastery_probability" not in serialized and attempt["evidence_id"] not in serialized
    assert attempt["evidence_id"] not in json.dumps(gateway.requests[0].context_manifest)
    assert gateway.requests[0].evidence_refs == ()
    persist(database, store, sid, decision, manifest)
    with database.connect() as connection:
        assert tuple(connection.execute("SELECT status,phase,completed_at FROM agent_objective WHERE objective_id='learning-objective'").fetchone()) == ("active", "check", None)
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 0
    pending = AgentRuntimeStore(Database(database.path)).pending_child_reply(sid)
    assert pending and pending["capability_name"] == "learning.request_response"


@pytest.mark.parametrize("mode,objective_type,phase", [
    ("complete", "learning", "completed"), ("complete", "execution", "completed"),
    ("continue", "learning", "completed"), ("new", "learning", "completed"),
])
def test_persistence_rejects_completion_bypassing_cloud_guard(runtime, mode, objective_type, phase):
    database, _, _, sid, store = runtime
    decision = parse_agent_plan_output(planner_payload(mode, objective_type, phase), "study", CapabilityRegistry())
    with pytest.raises(InvalidAgentPlanError, match="learning completion"):
        persist(database, store, sid, decision, context_manifest(store.assemble_context(sid, ())))
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM agent_objective WHERE objective_id='learning-objective'").fetchone()[0] == "active"
        assert connection.execute("SELECT COUNT(*) FROM agent_plan").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["pause", "cancel"])
def test_local_guard_preserves_explicit_pause_and_cancel(runtime, mode):
    database, repository, _, sid, store = runtime
    (decision, manifest), _ = resolve(database, repository, sid, planner_payload(mode, "learning", "paused"))
    assert decision.objective.mode == mode
    persist(database, store, sid, decision, manifest)
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM agent_objective WHERE objective_id='learning-objective'").fetchone()[0] == ("paused" if mode == "pause" else "cancelled")


@pytest.mark.parametrize('objective_type', ['learning', 'execution'])
def test_readonly_query_cannot_complete_or_claim_mastery_of_existing_objective(runtime, objective_type):
    database, repository, _, sid, store = runtime
    with database.connect() as connection:
        connection.execute('UPDATE agent_objective SET objective_type=?', (objective_type,))
        connection.commit()
    (decision, manifest), _ = resolve(database, repository, sid, planner_payload("complete", "execution", "completed", readonly=True))
    assert decision.objective.mode == 'none'
    assert '掌握' not in decision.spoken_transition
    assert decision.step.capability.name == 'planning.describe'
    persist(database, store, sid, decision, manifest)
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM agent_objective").fetchone()[0] == 'active'
        assert connection.execute('SELECT objective_id FROM agent_plan').fetchone()[0] is None


@pytest.mark.parametrize("phase", ["transfer_check", "retention_check"])
def test_completion_guard_keeps_existing_evaluator_check_phase(runtime, phase):
    database, repository, _, sid, store = runtime
    with database.connect() as connection:
        connection.execute("UPDATE agent_objective SET phase=?", (phase,))
        connection.commit()
    (decision, manifest), _ = resolve(database, repository, sid, planner_payload())
    assert decision.objective.phase == phase
    assert decision.step.arguments["subject"] == "数学"
    persist(database, store, sid, decision, manifest)
    assert store.pending_child_reply(sid)["phase"] == phase


def test_no_active_objective_cannot_be_completed_without_a_local_check(runtime):
    database, repository, _, sid, store = runtime
    with database.connect() as connection:
        connection.execute("UPDATE agent_objective SET status='cancelled'")
        connection.commit()
    (decision, manifest), _ = resolve(database, repository, sid, planner_payload())
    assert decision.objective.mode == "new" and decision.objective.phase == "check"
    persist(database, store, sid, decision, manifest)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_objective WHERE status='completed'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_objective WHERE status='active'").fetchone()[0] == 1
