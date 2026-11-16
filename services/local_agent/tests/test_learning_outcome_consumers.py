from datetime import timedelta
import asyncio
import json
from uuid import uuid4

import pytest

from home_study_agent.learning_outcome_coordinator import LearningOutcomeCoordinator
from home_study_agent.learning_outcome_projection import LearningOutcomeProjection
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, parse_agent_plan_output
from test_learning_outcome_mastery_gate import gate, flow, CHILD, ITEM, SECOND, third_item


def test_unbound_candidate_is_retained_but_not_counted_as_certified(gate):
    result, _, _ = gate.run(receipt=False)
    view = LearningOutcomeProjection(gate.db).for_child(CHILD)
    assert view["candidate_count"] == 1 and view["available_count"] == 0
    assert view["level_counts"] == {} and view["knowledge_evidence"] == []
    item = view["recent_outcomes"][0]
    assert item["summary"] == result["evaluation"]["evidence_summary"]
    assert item["certification_status"] == "candidate"
    assert item["mastery_gate"]["status"] == "unknown"


def test_transitive_revocation_reaches_child_session_and_commitment_consumers(gate):
    first, _, _ = gate.run()
    second, _, _ = gate.run(SECOND, "42", "transfer_check", days=1)
    service = LearningOutcomeProjection(gate.db)
    before = service.for_child(CHILD)
    assert before["level_counts"]["transferred"] == 1
    service.review_outcome(CHILD, first["evaluation"]["outcome_id"], decision="revoke",
                           reason="Basis corrected", client_action_id=str(uuid4()))
    after = service.for_child(CHILD)
    assert "transferred" not in after["level_counts"]
    assert after["fingerprint"] != before["fingerprint"]
    current = next(item for item in service.for_session(CHILD, gate.sid)
                   if item["outcome_id"] == second["evaluation"]["outcome_id"])
    assert current["recorded_outcome_level"] == "transferred"
    assert current["outcome_level"] == "independent"
    with gate.db.connect() as c:
        assert not service.qualified_outcome(c, CHILD, second["evaluation"]["outcome_id"], gate.objective)
        assert service.source_available_outcome(c, CHILD, second["evaluation"]["outcome_id"], gate.objective)
        commitment = c.execute("SELECT * FROM agent_commitment").fetchone()
        assert not service.commitment_available(c, commitment)
        assert commitment["status"] == "cancelled"


def test_projection_without_execution_table_never_initializes_it(gate):
    gate.run(receipt=False)
    with gate.db.connect() as c:
        c.execute("DROP TABLE learning_evaluation_execution")
        c.commit()
        c.execute("PRAGMA query_only=ON")
        view = LearningOutcomeProjection(gate.db).for_child(CHILD, connection=c)
        assert view["candidate_count"] == 1
        assert view["level_counts"] == {}
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='learning_evaluation_execution'").fetchone()


def test_real_scored_receipt_enters_objective_even_when_textual_lookup_is_ambiguous(gate):
    coordinator = LearningOutcomeCoordinator(gate.db)
    gate.learning.submit_diagnostic(gate.sid, ITEM, "90", str(uuid4()))
    gate.clock[0] += timedelta(hours=25)
    plans = []

    def adapter(receipt):
        plan = coordinator.diagnostic_evaluation_input(CHILD, gate.objective, receipt["evidence_id"])
        plans.append(plan)
        assert plan["status"] == "ready"
        return plan

    result, receipt, _ = gate.run(adapter=adapter)
    assert result["evaluation"]["outcome_level"] == "independent"
    assert result["evaluation"]["mastery_gate"]["evidence_id"] == receipt["evidence_id"]
    assert plans[0]["arguments"]["evidence_id"] == receipt["evidence_id"]
    assert coordinator.diagnostic_evaluation_input(CHILD, gate.objective, receipt["evidence_id"])["status"] == "already_linked"
    assert LearningOutcomeProjection(gate.db).for_child(CHILD)["level_counts"] == {"independent": 1}


def test_diagnostic_adapter_rejects_hints_and_foreign_ownership(gate):
    _, receipt, _ = gate.run(hint=1)
    service = LearningOutcomeCoordinator(gate.db)
    assert service.diagnostic_evaluation_input(CHILD, gate.objective, receipt["evidence_id"])["status"] == "candidate"
    with pytest.raises(ValueError, match="OBJECTIVE_NOT_OWNED"):
        service.diagnostic_evaluation_input("other-child", gate.objective, receipt["evidence_id"])
    with pytest.raises(ValueError, match="EVIDENCE_NOT_OWNED"):
        service.diagnostic_evaluation_input(CHILD, gate.objective, "absent")


def test_reconciliation_demotes_completed_history_without_auto_activating_or_resurrecting(gate):
    first, _, _ = gate.run()
    gate.run(SECOND, "42", "transfer_check", days=1)
    result, _, _ = gate.run(*third_item(gate), "retention_check", days=3)
    assert result["evaluation"]["outcome_level"] == "retained"
    projection = LearningOutcomeProjection(gate.db)
    projection.review_outcome(CHILD, first["evaluation"]["outcome_id"], decision="revoke",
                              reason="Withdraw first basis", client_action_id=str(uuid4()))
    service = LearningOutcomeCoordinator(gate.db)
    before = service.inspect_child(CHILD)[0]
    assert before["recorded_status"] == "paused" and before["requires_recheck"]
    after = service.reconcile_child(CHILD)[0]
    assert after["recorded_status"] == "paused" and after["recorded_phase"] == "transfer_check"
    assert after["next_phase"] == "transfer_check" and after["not_before"]
    assert before == after  # The parent's review already reconciled in its transaction.
    assert len(after["exclude_question_identities"]) == 3
    assert LearningOutcomeCoordinator(gate.db).reconcile_child(CHILD)[0] == after
    with gate.db.connect() as c:
        assert c.execute("SELECT completed_at FROM agent_objective").fetchone()[0] is None
        assert c.execute("SELECT outcome_level FROM agent_outcome WHERE outcome_id=?",
                         (result["evaluation"]["outcome_id"],)).fetchone()[0] == "retained"


def test_reconcile_shares_transaction_and_preserves_unrelated_active_work(gate):
    gate.run()
    service = LearningOutcomeCoordinator(gate.db)
    with gate.db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        before = tuple(c.execute("SELECT status,phase,updated_at FROM agent_objective").fetchone())
        result = service.reconcile_child(CHILD, connection=c)
        assert tuple(c.execute("SELECT status,phase,updated_at FROM agent_objective").fetchone()) == before
        assert result[0]["next_phase"] == "transfer_check"
        c.rollback()


def test_deleted_model_source_changes_projection_without_erasing_candidate_history(gate):
    result, _, _ = gate.run()
    service = LearningOutcomeProjection(gate.db)
    before = service.for_child(CHILD)
    with gate.db.connect() as c:
        c.execute("UPDATE model_call SET status='failed'")
        c.commit()
    after = service.for_child(CHILD)
    assert after["level_counts"] == {} and after["available_count"] == 0
    assert after["source_unavailable_count"] == 1 and after["fingerprint"] != before["fingerprint"]
    assert after["recent_outcomes"][0]["summary"] == result["evaluation"]["evidence_summary"]


def test_reliable_mapped_response_improves_dimensions_and_withdrawal_removes_them(gate):
    graph = KnowledgeGraphService(gate.db)
    node = graph.upsert_node(canonical_key="gate:arithmetic", node_type="knowledge_point", title="Arithmetic",
        scope={"grade": "四年级", "subject": "数学"}, source_ref="product_evidence:gate", review_status="approved")
    exercise = graph.upsert_node(canonical_key=f"diagnostic:{ITEM}", node_type="exercise", title="Question",
        source_ref="product_evidence:gate", review_status="approved")
    standard = graph.upsert_node(canonical_key="gate:standard", node_type="standard_capability", title="Standard",
        source_ref="curriculum_standard:gate", review_status="approved")
    graph.link(node, exercise, "assessed_by", rationale="Reviewed assessment", source_ref="product_evidence:gate", review_status="approved")
    graph.link(node, standard, "aligned_to", rationale="Reviewed alignment", source_ref="curriculum_standard:gate", review_status="approved")
    result, _, _ = gate.run(adapter=lambda receipt: LearningOutcomeCoordinator(gate.db).diagnostic_evaluation_input(
        CHILD, gate.objective, receipt["evidence_id"]))
    service = LearningOutcomeProjection(gate.db)
    before = service.for_child(CHILD)
    assert before["knowledge_evidence"][0]["dimensions"] == {
        "understanding": 1, "independence": 1, "transfer": 0, "retention": 0}
    assert before["knowledge_evidence"][0]["mastery_probability"] is None
    service.review_outcome(CHILD, result["evaluation"]["outcome_id"], decision="revoke",
                           reason="Withdraw evidence", client_action_id=str(uuid4()))
    assert service.for_child(CHILD)["knowledge_evidence"] == []


def test_candidate_can_justify_followup_but_never_a_retention_review(gate):
    result, _, _ = gate.run(receipt=False)
    service = LearningOutcomeProjection(gate.db)
    commitment = {"child_id": CHILD, "objective_id": gate.objective, "commitment_type": "follow_up",
                  "payload_json": json.dumps({"source_outcome_id": result["evaluation"]["outcome_id"]})}
    with gate.db.connect() as c:
        assert service.available_outcome(c, CHILD, result["evaluation"]["outcome_id"], gate.objective)
        assert not service.qualified_outcome(c, CHILD, result["evaluation"]["outcome_id"], gate.objective)
        assert service.commitment_available(c, commitment)
        assert not service.commitment_available(c, {**commitment, "commitment_type": "review"})


def test_unbound_candidate_still_reaches_existing_daily_closure_followups(gate):
    result, _, _ = gate.run(receipt=False)
    with gate.db.connect() as c:
        material = gate.learning._closure_material(c, gate.sid)
    followup = next(item for item in material["tutor_followups"] if item["source"]["source_id"] == gate.objective)
    assert followup["outcome_id"] == result["evaluation"]["outcome_id"]
    assert followup["evidence_status"] == "follow_up_not_confirmed_weakness"
    assert any(item["source"]["source_id"] == gate.objective for item in material["needs_verification"])


def test_reconcile_cancelled_objective_never_reactivates_or_overwrites_it(gate):
    gate.run()
    with gate.db.connect() as c:
        c.execute("UPDATE agent_objective SET status='cancelled'")
        c.commit()
    assert LearningOutcomeCoordinator(gate.db).reconcile_child(CHILD) == []
    with gate.db.connect() as c:
        assert c.execute("SELECT status FROM agent_objective").fetchone()[0] == "cancelled"


def test_prepared_diagnostic_input_survives_real_runtime_plan_persistence(gate):
    receipt = gate.learning.submit_diagnostic(gate.sid, ITEM, "90", str(uuid4()))
    prepared = LearningOutcomeCoordinator(gate.db).diagnostic_evaluation_input(CHILD, gate.objective, receipt["evidence_id"])
    decision = parse_agent_plan_output(json.dumps({
        "objective": prepared["objective"],
        "next_step": {"capability": prepared["capability"], "arguments": prepared["arguments"],
                      "reason": "Assess saved diagnostic evidence", "expected_evidence": ["independent_response"]},
        "spoken_transition": "Answer saved", "requires_child_reply": False, "confidence": .95,
    }), "study", CapabilityRegistry())
    turn_id = str(uuid4())
    store = AgentRuntimeStore(gate.db)
    with gate.db.connect() as c:
        c.execute("""INSERT INTO agent_turn (agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,'90','learning.evaluate_response','','{}','test',?,?)""",
            (turn_id, CHILD, gate.sid, gate.clock[0].isoformat(), prepared["client_action_id"]))
        store.persist_plan(c, child_id=CHILD, session_id=gate.sid,
            agent_turn_id=turn_id, client_action_id=prepared["client_action_id"], decision=decision,
            context_manifest={})
        c.commit()
    result = asyncio.run(gate.evaluator.evaluate(turn_id))
    assert result["evaluation"]["outcome_level"] == "independent"
    assert result["evaluation"]["mastery_gate"]["evidence_id"] == receipt["evidence_id"]
    assert result["evaluation"]["mastery_gate"]["covered_criteria"] == sorted(prepared["objective"]["success_criteria"])
    assert asyncio.run(gate.evaluator.evaluate(turn_id)) == result
