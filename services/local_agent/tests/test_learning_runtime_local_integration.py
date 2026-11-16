"""Real persisted diagnostic -> local review -> explicit Runtime transitions."""
from datetime import datetime, timedelta
import asyncio
import json
from uuid import uuid4

import pytest

from home_study_agent import agent_runtime, diagnostic_prompt
from home_study_agent.agent_runtime import (
    AgentRuntimeStore, AgentPlanDecision, ObjectiveDraft, PlannedStep,
    CapabilityRegistry, InvalidAgentPlanError,
)
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.learning_rubric import RubricEvidenceService
from test_learning_outcome_mastery_gate import gate, flow, CHILD, ITEM, SECOND
from test_learning_rubric_review import review


@pytest.fixture
def journey(gate, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return gate.clock[0].astimezone(tz) if tz else gate.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(agent_runtime, "datetime", Clock)
    monkeypatch.setattr(diagnostic_prompt, "utc_now", lambda: gate.clock[0].isoformat())
    gate.runtime = AgentRuntimeStore(gate.db)
    gate.prompts = DiagnosticPromptService(gate.db)
    gate.rubrics = RubricEvidenceService(gate.db)
    return gate


def start(j, item=ITEM, objective_draft=None):
    turn = str(uuid4())
    cap = CapabilityRegistry().require("diagnostic.start", "study")
    with j.db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,'','start_diagnostic','', '{"type":"start_diagnostic"}','pending','test',?,?)""",
            (turn, CHILD, j.sid, j.clock[0].isoformat(), turn))
        j.runtime.persist_plan(c, child_id=CHILD, session_id=j.sid, agent_turn_id=turn,
            client_action_id=turn, context_manifest={}, decision=AgentPlanDecision(
                objective_draft or ObjectiveDraft("pause", None, None, (), "execution"),
                PlannedStep(cap, {"track": "school"}, "Diagnostic", cap.expected_evidence), "", True, 1))
        c.commit()
    result = j.prompts.prepare(j.sid, track="school", preferred_item_id=item, client_action_id=turn)
    with j.db.connect() as c:
        j.runtime.complete_turn_execution(c, turn, True, None,
            {"execution_receipt": {"output": result, "capability": cap.name,
                "execution_location": cap.execution_location, "action_type": "start_diagnostic",
                "status": "completed", "completed_at": j.clock[0].isoformat()}})
        c.execute("UPDATE agent_turn SET tool_status='completed' WHERE agent_turn_id=?", (turn,))
        c.commit()
    return result


def answer(j, prepared, text="90"):
    p = prepared["prompt"]
    saved = j.prompts.answer(j.sid, prompt_id=p["prompt_id"], item_version=p["item_version"],
        answer=text, client_action_id=str(uuid4()))
    return j.runtime.finalize_diagnostic_evaluation(j.sid, saved["attempt"]["evidence_id"])


def approve(j, result):
    snapshot = result["evaluation"]["rubric_assessment"]
    review(j.rubrics, snapshot)
    return snapshot


def apply(j):
    view = j.runtime.inspect_learning_gate(CHILD, j.objective)
    return j.runtime.apply_learning_gate(CHILD, j.objective,
        expected_gate_fingerprint=view["gate_fingerprint"],
        expected_objective_updated_at=view["expected_objective_updated_at"])


def objective(j):
    with j.db.connect() as c:
        return tuple(c.execute("SELECT status,phase FROM agent_objective WHERE objective_id=?", (j.objective,)).fetchone())


def test_saved_diagnostic_freezes_original_goal_and_local_candidate(journey):
    j = journey
    result = answer(j, start(j))
    assert result["status"] == "candidate_recorded"
    assert result["objective_id"] == j.objective
    snapshot = result["evaluation"]["rubric_assessment"]
    assert snapshot["response"] == "90" and snapshot["source_kind"] == "local_criteria_mapping"
    assert result["local_only"] and not result["cloud_transmission_allowed"]
    assert objective(j) == ("active", "diagnose")
    approve(j, result)
    assert j.runtime.inspect_learning_gate(CHILD, j.objective)["outcome_level"] == "independent"
    assert objective(j) == ("active", "diagnose")
    assert apply(j)["objective_phase"] == "transfer_check"
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0


def test_explicit_new_learning_target_is_frozen_instead_of_borrowing_previous_goal(journey):
    j = journey
    draft = ObjectiveDraft("new", "learning", "A new arithmetic goal", ("A new criterion",), "diagnose")
    result = answer(j, start(j, objective_draft=draft))
    assert result["status"] == "candidate_recorded" and result["objective_id"] != j.objective
    assert result["evaluation"]["rubric_assessment"]["criteria"] == ["A new criterion"]
    assert objective(j)[0] == "paused"


def test_explicit_due_rechecks_reach_retained_without_cloud(journey):
    j = journey
    approve(j, answer(j, start(j)))
    first = apply(j)
    key = str(uuid4())
    assert j.runtime.start_learning_recheck(j.sid, first["commitment_id"], key)["reason"] == "not_due"
    j.clock[0] += timedelta(days=1)
    transfer = j.runtime.start_learning_recheck(j.sid, first["commitment_id"], key)
    assert transfer["status"] == "waiting_answer"
    assert transfer == j.runtime.start_learning_recheck(j.sid, first["commitment_id"], key)
    assert transfer["prompt"]["diagnostic_item_id"] != ITEM
    approve(j, answer(j, transfer, "42"))
    assert objective(j) == ("active", "transfer_check")
    second = apply(j)
    assert second["outcome_level"] == "transferred"
    j.clock[0] += timedelta(days=3)
    retained = j.runtime.start_learning_recheck(j.sid, second["commitment_id"], str(uuid4()))
    assert retained["prompt"]["diagnostic_item_id"] not in {ITEM, transfer["prompt"]["diagnostic_item_id"]}
    approve(j, answer(j, retained, "42"))
    assert objective(j) == ("active", "retention_check")
    assert apply(j)["outcome_level"] == "retained"
    assert objective(j) == ("completed", "completed")
    reopened = AgentRuntimeStore(Database(j.db.path))
    reopened.recover_learning_outcomes()
    assert objective(j) == ("completed", "completed")
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 3


@pytest.mark.parametrize("change", ["cancelled", "paused", "criteria", "new_goal"])
def test_late_answer_never_borrows_a_new_or_changed_goal(journey, change):
    j = journey
    prepared = start(j)
    with j.db.connect() as c:
        if change == "criteria":
            c.execute("UPDATE agent_objective SET success_criteria_json='[\"New criterion\"]'")
        else:
            c.execute("UPDATE agent_objective SET status=?", ("paused" if change == "new_goal" else change,))
            if change == "new_goal":
                c.execute("""INSERT INTO agent_objective SELECT 'replacement',child_id,session_id,objective_type,
                    summary,success_criteria_json,phase,'active',created_at,updated_at,NULL,NULL FROM agent_objective""")
        c.commit()
    result = answer(j, prepared)
    assert result["status"] == "blocked"
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 0


def test_cross_child_receipt_and_gate_cannot_be_borrowed(journey):
    j = journey
    result = answer(j, start(j))
    from home_study_agent.repository import StudyRepository
    with j.db.connect() as c:
        c.execute("UPDATE daily_session SET status='cancelled' WHERE session_id=?", (j.sid,))
        c.commit()
    other = StudyRepository(j.db).start_session("child-junior-3", str(uuid4()))["session_id"]
    with pytest.raises(InvalidAgentPlanError, match="NOT_OWNED_OR_BOUND"):
        j.runtime.finalize_diagnostic_evaluation(other, result["evidence_id"])
    with pytest.raises(InvalidAgentPlanError, match="NOT_OWNED"):
        j.runtime.inspect_learning_gate("child-junior-3", j.objective)


def test_restart_retries_saved_answer_once_but_never_applies_review(journey):
    j = journey
    p = start(j)["prompt"]
    j.prompts.answer(j.sid, prompt_id=p["prompt_id"], item_version=p["item_version"],
        answer="90", client_action_id=str(uuid4()))
    reopened = AgentRuntimeStore(Database(j.db.path))
    for _ in range(2):
        assert reopened.recover_learning_outcomes()["results"][0]["status"] == "candidate_recorded"
    assert objective(j) == ("active", "diagnose")
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 1


def test_retraction_cancels_local_followup_before_assembly(journey):
    j = journey
    snapshot = approve(j, answer(j, start(j)))
    commitment = apply(j)["commitment_id"]
    review(j.rubrics, snapshot, revision=1, operation="retract")
    j.runtime.assemble_context(j.sid, ())
    with j.db.connect() as c:
        assert c.execute("SELECT status FROM agent_commitment WHERE commitment_id=?", (commitment,)).fetchone()[0] == "cancelled"


def test_candidate_failure_rolls_back_but_original_answer_replays(journey, monkeypatch):
    j = journey
    p = start(j)["prompt"]
    payload = dict(prompt_id=p["prompt_id"], item_version=p["item_version"], answer="90", client_action_id="saved-answer")
    saved = j.prompts.answer(j.sid, **payload)
    from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
    original = LearningOutcomeEvaluator.prepare_local_evaluation

    def crash(self, turn_id):
        original(self, turn_id)
        raise RuntimeError("crash after candidate write")

    monkeypatch.setattr(LearningOutcomeEvaluator, "prepare_local_evaluation", crash)
    with pytest.raises(RuntimeError, match="crash after"):
        j.runtime.finalize_diagnostic_evaluation(j.sid, saved["attempt"]["evidence_id"])
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE intent='local_evaluation'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
    monkeypatch.setattr(LearningOutcomeEvaluator, "prepare_local_evaluation", original)
    replay = j.prompts.answer(j.sid, **payload)
    assert replay["attempt"]["evidence_id"] == saved["attempt"]["evidence_id"]
    result = j.runtime.finalize_diagnostic_evaluation(j.sid, replay["attempt"]["evidence_id"])
    assert result["status"] == "candidate_recorded"
    j.runtime.recover_learning_outcomes()
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 1


def test_changed_criteria_between_adapter_and_atomic_admission_are_rejected(journey, monkeypatch):
    j = journey
    p = start(j)
    original = RubricEvidenceService.diagnostic_candidate_input

    def change(self, child_id, evidence_id):
        result = original(self, child_id, evidence_id)
        with j.db.connect() as c:
            c.execute("UPDATE agent_objective SET success_criteria_json='[\"New target\"]'")
            c.commit()
        return result

    monkeypatch.setattr(RubricEvidenceService, "diagnostic_candidate_input", change)
    assert answer(j, p)["reason"] == "original_learning_objective_changed"
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 0


def test_apply_requires_fresh_gate_and_objective_version_and_preserves_cancel(journey):
    j = journey
    snapshot = approve(j, answer(j, start(j)))
    view = j.runtime.inspect_learning_gate(CHILD, j.objective)
    kwargs = dict(expected_gate_fingerprint=view["gate_fingerprint"],
                  expected_objective_updated_at=view["expected_objective_updated_at"])
    review(j.rubrics, snapshot, revision=1, operation="retract")
    assert j.runtime.apply_learning_gate(CHILD, j.objective, **kwargs)["reason"] == "gate_changed"
    review(j.rubrics, snapshot, revision=2)
    view = j.runtime.inspect_learning_gate(CHILD, j.objective)
    kwargs["expected_gate_fingerprint"] = view["gate_fingerprint"]
    with j.db.connect() as c:
        c.execute("UPDATE agent_objective SET updated_at=?", ((j.clock[0] + timedelta(seconds=1)).isoformat(),))
        c.commit()
    assert j.runtime.apply_learning_gate(CHILD, j.objective, **kwargs)["reason"] == "objective_changed"
    with j.db.connect() as c:
        c.execute("UPDATE agent_objective SET status='cancelled'")
        c.commit()
    assert apply(j)["reason"] == "objective_inactive"
    j.runtime.recover_learning_outcomes()
    assert objective(j)[0] == "cancelled"


def test_apply_original_command_retry_is_idempotent(journey):
    j = journey
    approve(j, answer(j, start(j)))
    view = j.runtime.inspect_learning_gate(CHILD, j.objective)
    kwargs = dict(expected_gate_fingerprint=view["gate_fingerprint"],
        expected_objective_updated_at=view["expected_objective_updated_at"])
    assert j.runtime.apply_learning_gate(CHILD, j.objective, **kwargs)["status"] == "applied"
    assert AgentRuntimeStore(Database(j.db.path)).apply_learning_gate(CHILD, j.objective, **kwargs)["status"] == "already_applied"
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_commitment").fetchone()[0] == 1


def test_review_post_fresh_inspect_retry_does_not_churn_objective(journey):
    j = journey
    snapshot = approve(j, answer(j, start(j)))
    first = apply(j)
    before = j.runtime.inspect_learning_gate(CHILD, j.objective)["expected_objective_updated_at"]
    j.clock[0] += timedelta(seconds=30)
    assert apply(j)["status"] == "already_applied"
    assert j.runtime.inspect_learning_gate(CHILD, j.objective)["expected_objective_updated_at"] == before
    review(j.rubrics, snapshot, revision=1, operation="retract")
    j.runtime.reconcile_learning_state(CHILD)
    review(j.rubrics, snapshot, revision=2)
    reapplied = apply(j)
    assert reapplied["status"] == "applied"
    assert reapplied["commitment_id"] != first["commitment_id"]


def scheduled_recheck(j, commitment_id):
    from home_study_agent.repository import StudyRepository
    workspace = StudyRepository(j.db).get_workspace(j.sid)
    if workspace["plan_items"]:
        return workspace["plan_items"][0]["plan_item_id"]
    pid, item_id = str(uuid4()), str(uuid4())
    with j.db.connect() as c:
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,
            generator,version,created_at,client_action_id) VALUES (?,?,?,?,'active',5,'test',1,?,?)""",
            (pid, CHILD, j.sid, j.clock[0].date().isoformat(), j.clock[0].isoformat(), pid))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
            position,source_type,source_ref) VALUES (?,?,'Math','Local recheck',5,1,'pending',0,'agent_retention',?)""",
            (item_id, pid, commitment_id))
        c.commit()
    return item_id


def task_turn(j, mode="new"):
    with j.db.connect() as c:
        cid = c.execute("SELECT commitment_id FROM agent_commitment WHERE status IN ('pending','due') ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    scheduled_recheck(j, cid)
    turn = str(uuid4())
    cap = CapabilityRegistry().require("task.start", "study")
    with j.db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,'Start next task','start_task','', '{"type":"start_next_task"}', 'pending','test',?,?)""",
            (turn, CHILD, j.sid, j.clock[0].isoformat(), turn))
        j.runtime.persist_plan(c, child_id=CHILD, session_id=j.sid, agent_turn_id=turn, client_action_id=turn,
            context_manifest={}, decision=AgentPlanDecision(
                ObjectiveDraft(mode, "execution", "Start the chosen task", ("Task started",), "execute"),
                PlannedStep(cap, {}, "Explicit task request", cap.expected_evidence), "", False, 1))
        c.commit()
    return turn


def test_task_start_cannot_exchange_its_original_commitment_for_another(journey):
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    turn = task_turn(j)
    with j.db.connect() as c:
        c.execute("""INSERT INTO agent_commitment(commitment_id,objective_id,child_id,commitment_type,
            due_at,status,payload_json,created_at,client_action_id)
            SELECT 'other-commitment',objective_id,child_id,commitment_type,due_at,status,payload_json,
            created_at,'other-commitment' FROM agent_commitment WHERE commitment_id=?""", (cid,))
        step = c.execute("SELECT s.* FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?", (turn,)).fetchone()
        c.execute("UPDATE agent_step SET status='running' WHERE step_id=?", (step["step_id"],))
        c.commit()
    result = j.runtime.start_learning_recheck(j.sid, "other-commitment", step["idempotency_key"])
    assert result["status"] == "blocked"
    assert result["reason"] == "task_start_binding_changed"
    with j.db.connect() as c:
        assert c.execute("SELECT status FROM plan_item").fetchone()[0] == "pending"
        assert c.execute("SELECT status FROM agent_objective WHERE objective_type='execution'").fetchone()[0] == "active"


def test_parent_task_turn_cannot_export_its_local_recheck_question_in_later_history(journey):
    j = journey
    approve(j, answer(j, start(j)))
    apply(j)
    j.clock[0] += timedelta(days=1)
    turn = task_turn(j)
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    with j.db.connect() as c:
        action = c.execute("SELECT action_json FROM agent_turn WHERE agent_turn_id=?", (turn,)).fetchone()[0]
    assert AgentOrchestrator._allows_cloud_history(action) is False


@pytest.mark.parametrize("status", ["blocked", "conflict"])
@pytest.mark.parametrize("nested", ["result", "flat", "outer", "receipt"])
@pytest.mark.parametrize("declared_waiting", [True, False])
@pytest.mark.parametrize("capability", ["task.start", "task.complete", "companion.activity_feedback", "learning.request_response"])
def test_blocked_task_response_is_not_objective_success_or_continuation(
    journey, monkeypatch, status, nested, declared_waiting, capability,
):
    j = journey
    approve(j, answer(j, start(j)))
    apply(j)
    turn = task_turn(j)
    def forbidden(*args):
        pytest.fail("Blocked business result enqueued continuation")
    monkeypatch.setattr(j.runtime.continuations, "enqueue", forbidden)
    with j.db.connect() as c:
        c.execute("UPDATE agent_step SET capability_name=? WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",
                  (capability, turn))
        c.execute("UPDATE agent_plan SET plan_json=json_set(plan_json,'$.requires_child_reply',?) WHERE agent_turn_id=?",
                  (declared_waiting, turn))
        before = [tuple(r) for r in c.execute("SELECT objective_id,status,phase FROM agent_objective ORDER BY objective_id")]
        outcomes = c.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0]
        commitments = [tuple(r) for r in c.execute("SELECT commitment_id,status FROM agent_commitment")]
        business_result = {"status": status, "reason": "not_due"}
        result = {"result": business_result, "evidence": []} if nested == "result" else {**business_result, "evidence": []}
        if nested == "outer":
            result["result"] = {}
        elif nested == "receipt":
            result = {"execution_receipt": {"status": "completed", "output": business_result}, "evidence": []}
        j.runtime.complete_turn_execution(c, turn, True, None, result)
        c.commit()
    restarted = AgentRuntimeStore(j.db)
    monkeypatch.setattr(restarted.continuations, "enqueue", forbidden)
    with j.db.connect() as c:
        restarted.complete_turn_execution(c, turn, True, None, result)
        c.commit()
        assert [tuple(r) for r in c.execute("SELECT objective_id,status,phase FROM agent_objective ORDER BY objective_id")] == before
        assert c.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == outcomes
        assert [tuple(r) for r in c.execute("SELECT commitment_id,status FROM agent_commitment")] == commitments
        assert c.execute("SELECT status FROM agent_plan WHERE agent_turn_id=?", (turn,)).fetchone()[0] == "completed"
        saved = c.execute("SELECT result_json FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?", (turn,)).fetchone()[0]
        assert json.loads(saved) == result


@pytest.mark.parametrize("change", ["plan_cancelled", "step_cancelled", "plan_retired", "source_changed", "failed"])
def test_recheck_prompt_keeps_original_command_source_and_distinguishes_cancel_from_retry(journey, change):
    from home_study_agent.task_start_binding import TaskStartBinding
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    turn = task_turn(j)
    with j.db.connect() as c:
        step = c.execute("SELECT s.* FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?", (turn,)).fetchone()
        c.execute("UPDATE agent_step SET status='running' WHERE step_id=?", (step["step_id"],))
        c.execute("UPDATE agent_plan SET status='running' WHERE plan_id=?", (step["plan_id"],))
        c.commit()
    selected = TaskStartBinding(j.db).resolve(turn)
    result = j.runtime.start_learning_recheck(j.sid, cid, step["idempotency_key"], authorized_turn_id=turn)
    assert result["status"] == "waiting_answer"
    with j.db.connect() as c:
        binding = json.loads(c.execute("SELECT context_manifest_json FROM agent_plan WHERE plan_id=?", (step["plan_id"],)).fetchone()[0])["goal_prompt_binding"]
        assert binding["authorized_turn_id"] == turn
        assert binding["plan_item_id"] == selected["plan_item_id"]
        assert binding["prompt_id"] == result["prompt"]["prompt_id"]
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id=?", (selected["plan_item_id"],)).fetchone()[0] == "pending"
        evidence_count = c.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0]
        if change == "plan_retired":
            c.execute("UPDATE daily_plan SET status='superseded' WHERE plan_id=?", (selected["plan_id"],))
        elif change == "source_changed":
            c.execute("UPDATE plan_item SET source_ref='another-source' WHERE plan_item_id=?", (selected["plan_item_id"],))
        elif change == "step_cancelled":
            c.execute("UPDATE agent_step SET status='cancelled' WHERE step_id=?", (step["step_id"],))
        else:
            c.execute("UPDATE agent_plan SET status=? WHERE plan_id=?", ("failed" if change == "failed" else "cancelled", step["plan_id"]))
        c.commit()
    view = DiagnosticPromptService(Database(j.db.path)).read(j.sid, prompt_id=result["prompt"]["prompt_id"])
    assert view["status"] == ("waiting_answer" if change == "failed" else "invalidated")
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0] == evidence_count


def test_task_plan_cannot_silently_pause_an_unknown_learning_objective(journey):
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    scheduled_recheck(j, cid)
    with j.db.connect() as c:
        c.execute("UPDATE agent_objective SET status='paused'")
        c.execute("""INSERT INTO agent_objective SELECT 'unrelated',child_id,session_id,objective_type,
            summary,success_criteria_json,phase,'active',created_at,updated_at,NULL,NULL FROM agent_objective""")
        c.commit()
    with pytest.raises(InvalidAgentPlanError, match="LOCAL_RECHECK_OTHER_OBJECTIVE_ACTIVE"):
        task_turn(j)
    with j.db.connect() as c:
        assert c.execute("SELECT status FROM agent_objective WHERE objective_id='unrelated'").fetchone()[0] == "active"
        assert c.execute("SELECT COUNT(*) FROM agent_objective WHERE objective_type='execution'").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["missing_binding", "source_changed", "plan_retired", "criteria_changed", "wrong_turn", "pretend_internal"])
def test_frozen_task_start_binding_is_required_at_execution(journey, change):
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    turn = task_turn(j)
    with j.db.connect() as c:
        step = c.execute("SELECT s.* FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?", (turn,)).fetchone()
        c.execute("UPDATE agent_step SET status='running' WHERE step_id=?", (step["step_id"],))
        if change == "missing_binding":
            c.execute("UPDATE agent_plan SET plan_json=json_remove(plan_json,'$.local_recheck_binding') WHERE agent_turn_id=?", (turn,))
        elif change == "source_changed":
            c.execute("UPDATE plan_item SET source_ref='unrelated-commitment'")
        elif change == "plan_retired":
            c.execute("UPDATE daily_plan SET status='superseded'")
        elif change == "criteria_changed":
            c.execute("UPDATE agent_objective SET success_criteria_json='[\"Another target\"]' WHERE objective_id=?", (j.objective,))
        elif change == "pretend_internal":
            c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id)
                VALUES ('unrelated-internal',?,?,'','local_recheck','','{}','test',?,'unrelated-internal')""",
                (CHILD, j.sid, j.clock[0].isoformat()))
        c.commit()
    result = j.runtime.start_learning_recheck(j.sid, cid, step["idempotency_key"],
        authorized_turn_id="wrong-turn" if change == "wrong_turn" else turn)
    assert result["status"] == "blocked"
    with j.db.connect() as c:
        assert c.execute("SELECT status FROM plan_item").fetchone()[0] == ("cancelled" if change == "criteria_changed" else "pending")
        assert c.execute("SELECT status FROM agent_objective WHERE objective_type='execution'").fetchone()[0] == "active"
        assert c.execute("SELECT COUNT(*) FROM diagnostic_prompt WHERE status='waiting_answer'").fetchone()[0] == 0


def test_task_start_can_recover_after_prompt_commit_before_timer_start(journey):
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    turn = task_turn(j)
    with j.db.connect() as c:
        step = c.execute("SELECT s.* FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?", (turn,)).fetchone()
        c.execute("UPDATE agent_step SET status='running' WHERE step_id=?", (step["step_id"],))
        c.commit()
    first = j.runtime.start_learning_recheck(j.sid, cid, step["idempotency_key"], authorized_turn_id=turn)
    with j.db.connect() as c:
        assert c.execute("SELECT status FROM plan_item").fetchone()[0] == "pending"
    reopened = AgentRuntimeStore(Database(j.db.path))
    second = reopened.start_learning_recheck(j.sid, cid, step["idempotency_key"], authorized_turn_id=turn)
    assert second == first and second["status"] == "waiting_answer"
    from home_study_agent.repository import StudyRepository
    repo = StudyRepository(j.db)
    item = repo.get_workspace(j.sid)["plan_items"][0]
    repo.start_plan_item(item["plan_item_id"], step["idempotency_key"])
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_prompt WHERE status='waiting_answer'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1


def test_real_executor_running_task_can_yield_only_its_own_execution_objective(journey, monkeypatch):
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    turn = task_turn(j)
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.repository import StudyRepository
    executor = AgentCapabilityExecutor(j.db, StudyRepository(j.db))
    # Exercise the real executor claim/load boundary; main owns its dispatch wiring.
    def dispatch(step):
        from home_study_agent.task_start_binding import TaskStartBinding
        item = TaskStartBinding(j.db).resolve(step["agent_turn_id"])
        local = j.runtime.start_learning_recheck(step["session_id"], cid, step["idempotency_key"],
            authorized_turn_id=step["agent_turn_id"])
        assert local["authorized_turn_id"] == turn
        if local["status"] == "waiting_answer":
            with j.db.connect() as c:
                assert c.execute("SELECT status FROM agent_plan WHERE agent_turn_id=?", (turn,)).fetchone()[0] == "running"
            executor.repository.start_plan_item(item["plan_item_id"], step["idempotency_key"], authorized_turn_id=turn)
        return {"capability": "task.start", "response_text": "Local check", "result": local, "evidence": []}

    monkeypatch.setattr(executor, "_dispatch", dispatch)
    result = executor.execute(turn)
    assert result["result"]["status"] == "waiting_answer"
    assert objective(j) == ("active", "transfer_check")
    assert executor.execute(turn) == result
    with j.db.connect() as c:
        assert c.execute("SELECT status FROM agent_objective WHERE objective_type='execution'").fetchone()[0] == "paused"
        assert c.execute("SELECT COUNT(*) FROM diagnostic_prompt WHERE status='waiting_answer'").fetchone()[0] == 1
        assert c.execute("SELECT status FROM plan_item").fetchone()[0] == "active"
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        manifest = json.loads(c.execute("SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?",
            (result["result"]["turn_id"],)).fetchone()[0])
        assert manifest["authorized_turn_id"] == turn


@pytest.mark.parametrize("mode", ["none", "continue", "pause"])
def test_task_start_does_not_replace_learning_criteria_with_execution_criteria(journey, mode):
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    with j.db.connect() as c:
        criteria = c.execute("SELECT success_criteria_json FROM agent_objective WHERE objective_id=?", (j.objective,)).fetchone()[0]
    turn = task_turn(j, mode=mode)
    with j.db.connect() as c:
        step = c.execute("SELECT s.* FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?", (turn,)).fetchone()
        c.execute("UPDATE agent_step SET status='running' WHERE step_id=?", (step["step_id"],))
        c.commit()
        assert c.execute("SELECT success_criteria_json FROM agent_objective WHERE objective_id=?", (j.objective,)).fetchone()[0] == criteria
    result = j.runtime.start_learning_recheck(j.sid, cid, step["idempotency_key"], authorized_turn_id=turn)
    assert result["status"] == "waiting_answer"


@pytest.mark.parametrize("invalid", ["wrong_key", "not_running", "newer_turn"])
def test_unrelated_or_stale_task_cannot_displace_active_execution(journey, invalid):
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    turn = task_turn(j)
    with j.db.connect() as c:
        step = c.execute("SELECT s.* FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?", (turn,)).fetchone()
        if invalid != "not_running":
            c.execute("UPDATE agent_step SET status='running' WHERE step_id=?", (step["step_id"],))
        if invalid == "newer_turn":
            c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id)
                VALUES ('interruption',?,?,'Stop','privacy_pause','','{}','test',?,'interruption')""",
                (CHILD, j.sid, j.clock[0].isoformat()))
        c.commit()
    result = j.runtime.start_learning_recheck(j.sid, cid, "wrong" if invalid == "wrong_key" else step["idempotency_key"])
    assert result["reason"] == ("another_objective_active" if invalid == "wrong_key" else "task_start_not_current")
    with j.db.connect() as c:
        assert c.execute("SELECT status FROM agent_objective WHERE objective_type='execution'").fetchone()[0] == "active"
        assert c.execute("SELECT COUNT(*) FROM diagnostic_prompt WHERE status='waiting_answer'").fetchone()[0] == 0


def test_parent_review_cannot_change_the_goal_during_another_pending_answer(journey):
    j = journey
    approve(j, answer(j, start(j)))
    pending = start(j, SECOND)
    assert apply(j)["reason"] == "objective_busy"
    assert objective(j) == ("active", "diagnose")
    assert answer(j, pending, "42")["status"] == "candidate_recorded"


def test_legacy_receipt_remains_unmapped_instead_of_borrowing_active_goal(journey):
    j = journey
    result = j.learning.submit_diagnostic(j.sid, ITEM, "90", "legacy-answer")
    with pytest.raises(InvalidAgentPlanError, match="NOT_OWNED_OR_BOUND"):
        j.runtime.finalize_diagnostic_evaluation(j.sid, result["evidence_id"])
    assert j.runtime.recover_learning_outcomes()["results"] == []
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 0


def test_prompt_created_before_original_turn_cannot_be_rebound(journey):
    j = journey
    p = j.prompts.prepare(j.sid, track="school", preferred_item_id=ITEM, client_action_id="old-prompt")["prompt"]
    j.prompts.cancel(j.sid, prompt_id=p["prompt_id"], item_version=p["item_version"])
    j.clock[0] += timedelta(seconds=1)
    current = start(j, SECOND)
    with j.db.connect() as c:
        turn = c.execute("SELECT agent_turn_id FROM agent_turn ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert not j.runtime.bind_diagnostic_prompt(turn, p["prompt_id"], p["item_version"])
    assert answer(j, current, "42")["status"] == "candidate_recorded"


@pytest.mark.parametrize("entry", ["assemble", "planning", "startup"])
def test_invalid_historical_completion_downgrades_at_each_consumer(journey, entry):
    j = journey
    snapshot = approve(j, answer(j, start(j)))
    first = apply(j)
    with j.db.connect() as c:
        c.execute("UPDATE agent_objective SET status='completed',phase='completed'")
        c.commit()
    review(j.rubrics, snapshot, revision=1, operation="retract")
    if entry == "assemble":
        j.runtime.assemble_context(j.sid, ())
    elif entry == "planning":
        start(j, SECOND)
    else:
        j.runtime.recover_interrupted_executions()
    assert objective(j)[0] != "completed"
    with j.db.connect() as c:
        assert c.execute("SELECT status FROM agent_commitment WHERE commitment_id=?", (first["commitment_id"],)).fetchone()[0] == "cancelled"


def test_unsuccessful_transfer_can_retry_a_fresh_question(journey):
    j = journey
    approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    first = j.runtime.start_learning_recheck(j.sid, cid, "first-check")
    answer(j, first, "wrong")
    second = j.runtime.start_learning_recheck(j.sid, cid, "second-check")
    assert second["status"] == "waiting_answer"
    assert second["prompt"]["diagnostic_item_id"] not in {ITEM, first["prompt"]["diagnostic_item_id"]}


@pytest.mark.parametrize("change", ["retract", "cancel", "no_item", "session_end"])
def test_recheck_is_not_started_when_admission_is_no_longer_valid(journey, change):
    j = journey
    snapshot = approve(j, answer(j, start(j)))
    cid = apply(j)["commitment_id"]
    j.clock[0] += timedelta(days=1)
    if change == "retract":
        review(j.rubrics, snapshot, revision=1, operation="retract")
    else:
        with j.db.connect() as c:
            if change == "cancel":
                c.execute("UPDATE agent_objective SET status='cancelled'")
            elif change == "no_item":
                c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id!=?", (ITEM,))
            else:
                c.execute("UPDATE daily_session SET status='completed'")
            c.commit()
    result = j.runtime.start_learning_recheck(j.sid, cid, str(uuid4()))
    assert result["status"] == "blocked"
    assert result["local_only"] and result["cloud_transmission_allowed"] is False
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE intent='local_recheck'").fetchone()[0] == 0


@pytest.mark.parametrize("utterance,expected", [
    ("90", "submit_diagnostic_answer"), ("停止监听", "privacy_pause"),
    ("总结今天的学习", "none"),
])
def test_pending_real_question_never_sends_utterance_to_cloud(journey, utterance, expected):
    j = journey
    start(j)
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    from home_study_agent.config import Settings
    from home_study_agent.repository import StudyRepository

    class ForbiddenGateway:
        configured = True

        async def complete(self, request):
            pytest.fail("A locally bound diagnostic utterance reached a cloud gateway")

    orchestrator = AgentOrchestrator(j.db, StudyRepository(j.db), ForbiddenGateway(),
        Settings(data_dir=j.db.path.parent, database_path=j.db.path))
    result = asyncio.run(orchestrator.interpret(j.sid, utterance, str(uuid4()), True))
    assert result["action"]["type"] == expected
    assert result["action"]["local_only"] and not result["action"]["cloud_transmission_allowed"]
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0


def test_completed_turn_retries_local_hook_without_replaying_answer_or_exporting_candidate(journey, monkeypatch):
    j = journey
    prepared = start(j)
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    from home_study_agent.config import Settings
    from home_study_agent.repository import StudyRepository
    orchestrator = AgentOrchestrator(j.db, StudyRepository(j.db), None,
        Settings(data_dir=j.db.path.parent, database_path=j.db.path))
    turn = asyncio.run(orchestrator.interpret(j.sid, "90", "spoken-answer", False))
    p = prepared["prompt"]
    saved = j.prompts.answer(j.sid, prompt_id=p["prompt_id"], item_version=p["item_version"],
        answer="90", client_action_id="actual-saved-answer")
    cap = CapabilityRegistry().require("diagnostic.submit", "study")
    output = {"execution_receipt": {"output": saved, "capability": cap.name,
        "execution_location": cap.execution_location, "action_type": "submit_diagnostic_answer",
        "status": "completed", "completed_at": j.clock[0].isoformat()}}
    original = orchestrator.agent_runtime.finalize_diagnostic_turn
    def crash(_turn):
        raise RuntimeError("post-commit hook unavailable")
    monkeypatch.setattr(orchestrator.agent_runtime, "finalize_diagnostic_turn", crash)
    with pytest.raises(RuntimeError, match="post-commit"):
        orchestrator.complete_turn(turn["agent_turn_id"], "Answer saved", "completed", None, output)
    monkeypatch.setattr(orchestrator.agent_runtime, "finalize_diagnostic_turn", original)
    completed = orchestrator.complete_turn(turn["agent_turn_id"], "Answer saved", "completed", None, output)
    metadata = completed["local_evaluation"]
    assert set(metadata) == {"status", "reason", "assessment_id", "local_only", "cloud_transmission_allowed",
                             "feedback_turn_id", "local_feedback_turn_id"}
    assert metadata["feedback_turn_id"] is None and metadata["local_feedback_turn_id"] is None
    assert metadata["status"] == "candidate_recorded" and metadata["assessment_id"]
    assert completed["action"]["cloud_transmission_allowed"] is False
    with j.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 1
