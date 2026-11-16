import asyncio
from datetime import timedelta
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService, PROFILES, DIMENSION_CRITERIA
from home_study_agent.learning_outcome_projection import LearningOutcomeProjection
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from test_learning_outcome_mastery_gate import gate, flow, CHILD, ITEM, SECOND, CRITERIA, third_item
from test_writing_response_origin import writing


def candidate(gate, **kwargs):
    kwargs.setdefault("answer", "24+6=30; 30*3=90.")
    result, _, turn = gate.run(receipt=False, **kwargs)
    return result["evaluation"]["rubric_assessment"], result, turn


def review(service, snapshot, *, revision=0, scores=None, independent=True, hint=0, operation="approve", key=None):
    return service.review(CHILD, snapshot["assessment_id"], expected_revision=revision,
        client_action_id=key or str(uuid4()), reviewer_id="authenticated-parent", reviewer_role="parent",
        operation=operation, reason="Reviewed the actual answer and observed conditions",
        dimensions=[{"dimension_id": item["dimension_id"], "score": (scores or {}).get(item["dimension_id"], 3),
                     "response_quotes": [snapshot["response"]], "rationale": "Checked against the criterion",
                     **({"source_quotes": [snapshot["material"]["stimulus_text"]]}
                        if item["dimension_id"] == "evidence" and snapshot["material"].get("stimulus_text") else {})}
                    for item in snapshot["dimensions"]] if operation == "approve" else None,
        criterion_evidence=[{"criterion": criterion, "response_quote": snapshot["response"]} for criterion in snapshot["criteria"]]
            if operation == "approve" else None,
        independent=independent if operation == "approve" else None, hint_level=hint if operation == "approve" else None)


def test_model_confidence_never_certifies_an_open_answer(gate):
    snapshot, result, _ = candidate(gate)
    assert snapshot["score_status"] == "candidate"
    assert snapshot["review_revision"] == 0
    assert result["evaluation"]["certification_status"] == "candidate"
    assert snapshot["dimensions"]


def test_reviewed_correct_process_and_actual_conditions_can_qualify(gate):
    snapshot, result, turn = candidate(gate)
    service = RubricEvidenceService(gate.db)
    receipt = review(service, snapshot)
    assert receipt["score_status"] == "reviewed_human"
    assert receipt["passed"] is True
    current = asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]
    assert current["outcome_level"] == "independent"
    assert current["mastery_gate"]["score_status"] == "reviewed_human"
    assert result["evaluation"]["outcome_level"] == "understood_now"


@pytest.mark.parametrize("scores,independent,hint", [({"reasoning": 0}, True, 0), ({}, False, 0), ({}, True, 1)])
def test_correct_final_answer_cannot_hide_bad_process_or_assistance(gate, scores, independent, hint):
    snapshot, _, turn = candidate(gate)
    service = RubricEvidenceService(gate.db)
    review(service, snapshot, scores=scores, independent=independent, hint=hint)
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] not in {"independent", "transferred", "retained"}


def test_revision_retraction_restart_and_exact_replay_preserve_history(gate):
    snapshot, _, turn = candidate(gate)
    service = RubricEvidenceService(gate.db)
    first = review(service, snapshot, key="review-1")
    second = review(service, snapshot, revision=1, scores={"reasoning": 0})
    assert second["review_revision"] == 2 and not second["passed"]
    assert review(service, snapshot, key="review-1") == first
    revoked = review(service, snapshot, revision=2, operation="retract")
    reopened = RubricEvidenceService(gate.db)
    assert reopened.get(CHILD, snapshot["assessment_id"])["review_revision"] == 3
    assert reopened.history(CHILD, snapshot["assessment_id"])[-1] == revoked
    assert len(reopened.history(CHILD, snapshot["assessment_id"])) == 3
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "understood_now"


def test_review_conflicts_and_source_corrections_fail_closed(gate):
    snapshot, _, turn = candidate(gate)
    service = RubricEvidenceService(gate.db)
    review(service, snapshot, key="fixed")
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        review(service, snapshot)
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        review(service, snapshot, key="fixed", scores={"reasoning": 0})
    with gate.db.connect() as c:
        c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','corrected original answer')")
        c.commit()
    assert service.get(CHILD, snapshot["assessment_id"])["score_status"] == "source_changed"
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "understood_now"
    assert review(service, snapshot, revision=1, operation="retract")["score_status"] == "withdrawn"
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        review(service, snapshot, revision=2)


def test_old_evaluation_cannot_be_backfilled_on_read(gate):
    snapshot, _, turn = candidate(gate)
    with gate.db.connect() as c:
        c.execute("DELETE FROM rubric_assessment WHERE assessment_id=?", (snapshot["assessment_id"],))
        c.commit()
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "understood_now"
    with gate.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 0


def test_human_review_does_not_bypass_distinct_questions_and_real_delay(gate):
    service = RubricEvidenceService(gate.db)
    first, _, _ = candidate(gate)
    review(service, first)
    second, _, turn = candidate(gate, item=SECOND, answer="6*3=18; 24+18=42.", phase="transfer_check")
    review(service, second)
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] != "transferred"


def test_three_reviewed_open_checks_can_reach_retention_without_local_exact_scores(gate, monkeypatch):
    service = RubricEvidenceService(gate.db)
    first, _, _ = candidate(gate)
    review(service, first)
    second, _, turn = candidate(gate, item=SECOND, answer="6*3=18; 24+18=42.", phase="transfer_check", days=1)
    review(service, second)
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "transferred"
    item, answer = third_item(gate)
    third, _, turn = candidate(gate, item=item, answer="3+4=7; 6*7=42.", phase="retention_check", days=3)
    review(service, third)
    def forbidden(*args, **kwargs):
        pytest.fail("Human review readback cannot silently complete a runtime task")
    monkeypatch.setattr(gate.evaluator, "_complete_retention_task", forbidden)
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "retained"


def test_privacy_hard_delete_cascades_review_rows(gate):
    snapshot, _, _ = candidate(gate)
    service = RubricEvidenceService(gate.db)
    review(service, snapshot)
    with gate.db.connect() as c:
        c.execute("DELETE FROM rubric_assessment WHERE assessment_id=?", (snapshot["assessment_id"],))
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM rubric_review_event").fetchone()[0] == 0


def test_frozen_rubric_version_does_not_change_when_profile_code_changes(gate, monkeypatch):
    snapshot, _, _ = candidate(gate)
    assert all(item["criterion"] and set(item["score_anchors"]) == {"0", "1", "2", "3"}
               for item in snapshot["dimensions"])
    monkeypatch.setitem(PROFILES, "math_process", ("different_future_dimension",))
    monkeypatch.setitem(DIMENSION_CRITERIA, "math_process", {})
    assert RubricEvidenceService(gate.db).get(CHILD, snapshot["assessment_id"])["dimensions"] == snapshot["dimensions"]
    assert review(RubricEvidenceService(gate.db), snapshot)["passed"]


def test_concurrent_revision_has_one_winner_and_no_lost_review(gate):
    snapshot, _, _ = candidate(gate)
    def submit(index):
        try:
            return review(RubricEvidenceService(gate.db), snapshot, key=f"parallel-{index}")
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "RUBRIC_REVISION_CONFLICT" in results


def test_review_cannot_invent_quotes_or_impersonate_an_automated_reviewer(gate):
    snapshot, _, _ = candidate(gate)
    service = RubricEvidenceService(gate.db)
    bad = {**snapshot, "response": "An invented explanation"}
    with pytest.raises(ValueError, match="DIMENSION_EVIDENCE_INVALID"):
        review(service, bad)
    with pytest.raises(ValueError, match="REVIEW_INVALID"):
        service.review(CHILD, snapshot["assessment_id"], expected_revision=0, client_action_id="model-approval",
            reviewer_id="model", reviewer_role="model", operation="approve", reason="High confidence")


def test_open_diagnostic_hints_cannot_be_overridden_by_human_no_hint_checkbox(gate):
    answer = "Dear Alex, please come to the library on Saturday at ten. We can study English together."
    source = gate.learning.submit_diagnostic(gate.sid, "diag-pet-writing-1", answer, str(uuid4()), hint_level=1)
    service = RubricEvidenceService(gate.db)
    prepared = service.diagnostic_candidate_input(CHILD, source["evidence_id"])
    result, _, turn = gate.run(item="diag-pet-writing-1", answer=answer, receipt=False, adapter=lambda _: prepared)
    snapshot = result["evaluation"]["rubric_assessment"]
    assert snapshot["profile"] == "english_open"
    review(service, snapshot, hint=0)
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "understood_now"


def _written_review_replay(writing):
    from test_writing_rubric import prepared
    from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
    service,snapshot,body=prepared(writing)
    service.review(CHILD,snapshot['assessment_id'],**body)
    with writing[0].connect() as c:
        turn=c.execute('SELECT p.agent_turn_id FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE s.step_id=?',
            (snapshot['step_id'],)).fetchone()[0]
    evaluator=LearningOutcomeEvaluator(writing[0],None,None,None)
    return service,snapshot,evaluator,turn


def test_open_diagnostic_review_has_a_real_non_keyword_path_to_qualification(writing):
    # Bare text/english_open is not a writing original. Preserve the positive
    # review path with exact manuscript, original task points and typed attempt.
    service,snapshot,evaluator,turn=_written_review_replay(writing)
    assert service.qualification(CHILD,snapshot['assessment_id'])['eligible_for_mastery_gate']
    current = asyncio.run(evaluator.evaluate(turn))["evaluation"]
    assert current["outcome_level"] == "independent"
    assert current["mastery_gate"]["score_status"] == "reviewed_human"


def test_diagnostic_source_revision_invalidates_its_old_human_review(writing):
    service,snapshot,evaluator,turn=_written_review_replay(writing)
    assert asyncio.run(evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "independent"
    with writing[0].connect() as c:
        c.execute("UPDATE learning_evidence SET confidence=0.123 WHERE evidence_id=?", (snapshot["source_evidence_id"],))
        c.commit()
    assert service.get(CHILD, snapshot["assessment_id"])["score_status"] == "source_changed"
    assert asyncio.run(evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "understood_now"
    assert len(service.history(CHILD, snapshot["assessment_id"])) == 1
    assert review(service, snapshot, revision=1, operation="retract")["score_status"] == "withdrawn"


def test_keyword_stuffing_cannot_pass_a_failed_task_dimension(gate):
    snapshot, _, turn = candidate(gate, item="diag-pet-writing-1", subject="英语", answer="library Saturday study and English because morning")
    assert snapshot["profile"] == "english_open"
    review(RubricEvidenceService(gate.db), snapshot, scores={"task": 0})
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] not in {"independent", "transferred", "retained"}


@pytest.mark.parametrize("score", [True, -1, 4, 1.5])
def test_invalid_dimension_scores_cannot_enter_review_history(gate, score):
    snapshot, _, _ = candidate(gate)
    service = RubricEvidenceService(gate.db)
    with pytest.raises(ValueError, match="DIMENSION_EVIDENCE_INVALID"):
        review(service, snapshot, scores={"reasoning": score})
    assert service.history(CHILD, snapshot["assessment_id"]) == []


def test_review_can_correct_a_low_confidence_model_without_fabricating_certainty(gate, monkeypatch):
    complete = gate.gateway.complete
    async def uncertain(request):
        result = await complete(request)
        value = json.loads(result.content)
        value.update(understanding="partial", confidence=.2)
        return replace(result, content=json.dumps(value))
    monkeypatch.setattr(gate.gateway, "complete", uncertain)
    snapshot, _, turn = candidate(gate)
    review(RubricEvidenceService(gate.db), snapshot)
    current = asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]
    assert current["outcome_level"] == "independent"
    assert current["confidence"] == .2
    assert current["rubric_assessment"]["review_revision"] == 1


def test_reviewed_candidate_reaches_projection_and_approved_knowledge_dimensions(gate, monkeypatch):
    complete = gate.gateway.complete
    async def uncertain(request):
        result = await complete(request)
        value = json.loads(result.content)
        value.update(understanding="partial", confidence=.2)
        return replace(result, content=json.dumps(value))
    monkeypatch.setattr(gate.gateway, "complete", uncertain)
    graph = KnowledgeGraphService(gate.db)
    node = graph.upsert_node(canonical_key="rubric:arithmetic", node_type="knowledge_point", title="Arithmetic",
        scope={"grade": "四年级", "subject": "数学"}, source_ref="product_evidence:rubric", review_status="approved")
    exercise = graph.upsert_node(canonical_key=f"diagnostic:{ITEM}", node_type="exercise", title="Question",
        source_ref="product_evidence:rubric", review_status="approved")
    standard = graph.upsert_node(canonical_key="rubric:standard", node_type="standard_capability", title="Standard",
        source_ref="curriculum_standard:rubric", review_status="approved")
    graph.link(node, exercise, "assessed_by", rationale="Reviewed assessment", source_ref="product_evidence:rubric", review_status="approved")
    graph.link(node, standard, "aligned_to", rationale="Reviewed alignment", source_ref="curriculum_standard:rubric", review_status="approved")
    snapshot, result, _ = candidate(gate)
    service = RubricEvidenceService(gate.db)
    projection = LearningOutcomeProjection(gate.db)
    before = projection.for_child(CHILD)
    assert before["candidate_count"] == 1 and before["knowledge_evidence"] == []
    review(service, snapshot)
    with gate.db.connect() as c:
        assert projection.qualified_outcome(c, CHILD, result["evaluation"]["outcome_id"], gate.objective)
    after = projection.for_child(CHILD)
    assert after["level_counts"] == {"independent": 1}
    assert after["knowledge_evidence"][0]["dimensions"] == {
        "understanding": 1, "independence": 1, "transfer": 0, "retention": 0}
    assert after["recent_outcomes"][0]["confidence"] == .2
    review(service, snapshot, revision=1, operation="retract")
    assert projection.for_child(CHILD)["knowledge_evidence"] == []


def test_reviewed_transfer_and_retention_are_current_commitment_bases(gate):
    service = RubricEvidenceService(gate.db)
    projection = LearningOutcomeProjection(gate.db)
    first, _, _ = candidate(gate)
    review(service, first)
    second, result, _ = candidate(gate, item=SECOND, answer="6*3=18; 24+18=42.", phase="transfer_check", days=1)
    review(service, second)
    commitment = {"child_id": CHILD, "objective_id": gate.objective, "commitment_type": "review",
                  "payload_json": json.dumps({"source_outcome_id": result["evaluation"]["outcome_id"]})}
    with gate.db.connect() as c:
        assert projection.commitment_available(c, commitment)
    item, _ = third_item(gate)
    third, _, _ = candidate(gate, item=item, answer="3+4=7; 6*7=42.", phase="retention_check", days=3)
    review(service, third)
    with gate.db.connect() as c:
        assert projection._has_retained_basis(c, CHILD, gate.objective)
        assert not projection._has_retained_basis(c, "other-child", gate.objective)
    review(service, first, revision=1, operation="retract")
    with gate.db.connect() as c:
        assert not projection.commitment_available(c, commitment)
        assert not projection._has_retained_basis(c, CHILD, gate.objective)


def test_chinese_review_binds_passage_quotes_and_invalidates_changed_material(gate):
    passage = "雨停后，父亲没有立刻收伞，而是把伞向我这边又倾了倾。"
    answer = "父亲把伞向孩子这边倾，表现出父亲细心照顾孩子。"
    with gate.db.connect() as c:
        c.execute("UPDATE agent_objective SET success_criteria_json=?", (json.dumps(["能定位动作依据", "能解释人物情感"]),))
        c.execute("UPDATE diagnostic_item SET stimulus_text=? WHERE diagnostic_item_id='diag-j3-chinese-reading-1'", (passage,))
        c.commit()
    snapshot, _, turn = candidate(gate, item="diag-j3-chinese-reading-1", subject="语文", answer=answer)
    assert snapshot["profile"] == "chinese_open" and snapshot["material"]["stimulus_text"] == passage
    service = RubricEvidenceService(gate.db)
    fake = {**snapshot, "material": {**snapshot["material"], "stimulus_text": "An invented passage"}}
    with pytest.raises(ValueError, match="SOURCE_QUOTE"):
        review(service, fake)
    review(service, snapshot)
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "independent"
    with gate.db.connect() as c:
        c.execute("UPDATE diagnostic_item SET stimulus_text='Changed reading source' WHERE diagnostic_item_id='diag-j3-chinese-reading-1'")
        c.commit()
    assert service.get(CHILD, snapshot["assessment_id"])["score_status"] == "source_changed"
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "understood_now"


def test_model_source_deletion_blocks_qualification_without_deleting_human_history(gate):
    snapshot, _, _ = candidate(gate)
    service = RubricEvidenceService(gate.db)
    review(service, snapshot)
    with gate.db.connect() as c:
        c.execute("DELETE FROM model_call")
        c.commit()
    assert service.qualification(CHILD, snapshot["assessment_id"])["eligible_for_mastery_gate"] is False
    assert len(service.history(CHILD, snapshot["assessment_id"])) == 1
