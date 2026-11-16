import pytest
from concurrent.futures import ThreadPoolExecutor

from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator, InvalidLearningEvaluationError
from home_study_agent.learning_outcome_coordinator import LearningOutcomeCoordinator
from home_study_agent.learning_outcome_projection import LearningOutcomeProjection
from home_study_agent.learning_rubric import RubricEvidenceService
from test_learning_outcome_mastery_gate import gate, flow, CHILD, SECOND, third_item
from test_learning_rubric_review import review
from test_writing_response_origin import writing


@pytest.fixture
def no_model(gate, monkeypatch):
    async def forbidden(request):
        pytest.fail("Local criterion/review path invoked a model")
    monkeypatch.setattr(gate.gateway, "complete", forbidden)


@pytest.mark.parametrize('item', ['diag-pet-speaking-2', 'diag-pet-read-aloud-1'])
def test_missing_prompt_history_blocks_oral_feedback_without_initializing_it(gate, no_model, item):
    evidence = gate.learning.submit_diagnostic(gate.sid, item,
        'I read a book at home because it is quiet.', 'oral-without-prompt')
    record = {'source_evidence_id': evidence['evidence_id'], 'child_id': CHILD, 'session_id': gate.sid}
    with gate.db.connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone()
        result = gate.evaluator._current_oral_feedback(c, record,
            {'evaluation': {'spoken_feedback': 'Old feedback'}, 'response_text': 'Old feedback'})
        assert result['evaluation']['feedback_delivery_allowed'] is False
        assert result['evaluation']['spoken_feedback'] == ''
        assert result['evaluation']['oral_feedback']['status'] == 'unavailable'
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone()


def test_local_exact_receipt_requires_reviewed_mapping_and_never_rescores(gate, no_model):
    result, evidence, turn = gate.run(local=True)
    service = RubricEvidenceService(gate.db)
    snapshot = result["evaluation"]["rubric_assessment"]
    assert snapshot["source_kind"] == "local_criteria_mapping" and snapshot["dimensions"] == []
    assert result["evaluation"]["certification_status"] == "candidate"
    assert gate.evaluator.prepare_local_evaluation(turn) == result
    coordinator = LearningOutcomeCoordinator(gate.db)
    assert coordinator.diagnostic_evaluation_input(CHILD, gate.objective, evidence["evidence_id"])["status"] == "already_linked"
    review(service, snapshot)
    current = gate.evaluator.prepare_local_evaluation(turn)["evaluation"]
    assert current["outcome_level"] == "independent"
    assert current["mastery_gate"]["score_status"] == "verified_local"
    assert current["mastery_gate"]["criterion_mapping_status"] == "reviewed_human"
    assert current["confidence"] == 0 and current["objective_update_applied"] is False
    assert coordinator.diagnostic_evaluation_input(CHILD, gate.objective, evidence["evidence_id"])["status"] == "already_linked"
    with gate.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0
        assert tuple(c.execute("SELECT status,phase FROM agent_objective").fetchone()) == ("active", "diagnose")
        assert c.execute("SELECT correctness FROM learning_evidence WHERE evidence_id=?", (evidence["evidence_id"],)).fetchone()[0] == 1
    assert LearningOutcomeProjection(gate.db).for_child(CHILD)["level_counts"] == {"independent": 1}


@pytest.mark.parametrize("answer,hint", [("91", 0), ("90", 1)])
def test_human_mapping_cannot_override_the_real_local_score_or_hints(gate, no_model, answer, hint):
    result, _, turn = gate.run(local=True, answer=answer, hint=hint)
    snapshot = result["evaluation"]["rubric_assessment"]
    assert snapshot["source_kind"] == "local_criteria_mapping"
    review(RubricEvidenceService(gate.db), snapshot, hint=0)
    assert gate.evaluator.prepare_local_evaluation(turn)["evaluation"]["outcome_level"] == "understood_now"


def test_local_open_answer_review_is_a_real_no_model_path(gate, no_model):
    answer = "Hi Alex. Would you join me at our library this Saturday at ten? Let's revise English together."
    result, _, turn = gate.run(local=True, item="diag-pet-writing-1", answer=answer)
    snapshot = result["evaluation"]["rubric_assessment"]
    assert snapshot["source_kind"] == "local_open_review"
    assert all(item["score"] is None for item in snapshot["dimensions"])
    service = RubricEvidenceService(gate.db)
    review(service, snapshot)
    # A generic text review without an original manuscript is not writing evidence.
    assert gate.evaluator.prepare_local_evaluation(turn)["evaluation"]["outcome_level"] == "understood_now"
    qualification = service.qualification(CHILD, snapshot["assessment_id"])
    assert qualification["receipt"] is None and not qualification["eligible_for_mastery_gate"]
    with gate.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0


def test_local_original_writing_review_reaches_independent_without_a_model(writing):
    from test_writing_rubric import prepared
    service, snapshot, body = prepared(writing)
    service.review(CHILD, snapshot['assessment_id'], **body)
    with writing[0].connect() as c:
        row = c.execute('''SELECT p.agent_turn_id FROM rubric_assessment r
            JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id)
            WHERE r.assessment_id=?''', (snapshot['assessment_id'],)).fetchone()
    evaluator = LearningOutcomeEvaluator(writing[0], None, None, None, initialize=False)
    assert evaluator.prepare_local_evaluation(row[0])['evaluation']['outcome_level'] == 'independent'
    assert service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    service.review(CHILD, snapshot['assessment_id'], expected_revision=1, client_action_id='retract-original',
        reviewer_id='parent', reviewer_role='parent', operation='retract', reason='Synthetic review withdrawn')
    assert evaluator.prepare_local_evaluation(row[0])['evaluation']['outcome_level'] == 'understood_now'


def test_local_checks_can_reach_retention_and_retract_transitively_across_restart(gate, no_model):
    service = RubricEvidenceService(gate.db)
    result, _, _ = gate.run(local=True)
    first = result["evaluation"]["rubric_assessment"]
    review(service, first)
    result, _, _ = gate.run(local=True, item=SECOND, answer="42", phase="transfer_check", days=1)
    review(service, result["evaluation"]["rubric_assessment"])
    item, answer = third_item(gate)
    result, _, turn = gate.run(local=True, item=item, answer=answer, phase="retention_check", days=3)
    review(service, result["evaluation"]["rubric_assessment"])
    reopened = LearningOutcomeEvaluator(gate.db, None, None, None, initialize=False)
    assert reopened.current_mastery_gate(gate.objective)["outcome_level"] == "retained"
    assert reopened.prepare_local_evaluation(turn)["evaluation"]["objective_update_applied"] is False
    projection = LearningOutcomeProjection(gate.db)
    with gate.db.connect() as c:
        assert projection._has_retained_basis(c, CHILD, gate.objective)
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM agent_commitment").fetchone()[0] == 0
    review(service, first, revision=1, operation="retract")
    assert reopened.current_mastery_gate(gate.objective)["outcome_level"] == "independent"
    with gate.db.connect() as c:
        assert not projection._has_retained_basis(c, CHILD, gate.objective)


def test_local_mapping_cannot_skip_real_delay(gate, no_model):
    service = RubricEvidenceService(gate.db)
    first, _, _ = gate.run(local=True)
    review(service, first["evaluation"]["rubric_assessment"])
    second, _, turn = gate.run(local=True, item=SECOND, answer="42", phase="transfer_check")
    review(service, second["evaluation"]["rubric_assessment"])
    assert gate.evaluator.prepare_local_evaluation(turn)["evaluation"]["outcome_level"] == "independent"


def test_local_review_list_is_read_only_paginated_and_child_scoped(gate, no_model):
    first, _, _ = gate.run(local=True)
    second, _, _ = gate.run(local=True, item=SECOND, answer="42", days=1)
    service = RubricEvidenceService(gate.db)
    page = service.list_for_child(CHILD, limit=1)
    assert page["items"][0]["assessment_id"] == second["evaluation"]["rubric_assessment"]["assessment_id"]
    next_page = service.list_for_child(CHILD, limit=1, before=page["next_cursor"])
    assert next_page["items"][0]["assessment_id"] == first["evaluation"]["rubric_assessment"]["assessment_id"]
    assert next_page["next_cursor"] is None
    assert service.list_for_child("other-child")["items"] == []
    with pytest.raises(ValueError, match="CURSOR_INVALID"):
        service.list_for_child("other-child", before=page["next_cursor"])


def test_deleting_local_snapshot_never_backfills_or_keeps_its_certification(gate, no_model):
    result, _, turn = gate.run(local=True)
    snapshot = result["evaluation"]["rubric_assessment"]
    service = RubricEvidenceService(gate.db)
    review(service, snapshot)
    with gate.db.connect() as c:
        c.execute("DELETE FROM rubric_assessment WHERE assessment_id=?", (snapshot["assessment_id"],))
        c.commit()
    assert gate.evaluator.prepare_local_evaluation(turn)["evaluation"]["certification_status"] == "candidate"
    assert service.list_for_child(CHILD)["items"] == []


def test_concurrent_local_preparation_creates_one_candidate(gate, no_model, monkeypatch):
    prepare = gate.evaluator.prepare_local_evaluation
    monkeypatch.setattr(gate.evaluator, "prepare_local_evaluation", lambda turn: {})
    _, _, turn = gate.run(local=True)
    monkeypatch.setattr(gate.evaluator, "prepare_local_evaluation", prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, [turn, turn]))
    assert results[0] == results[1]
    with gate.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["cancelled", "paused"])
def test_local_preparation_cannot_resume_an_inactive_objective(gate, no_model, monkeypatch, status):
    prepare = gate.evaluator.prepare_local_evaluation
    monkeypatch.setattr(gate.evaluator, "prepare_local_evaluation", lambda turn: {})
    _, _, turn = gate.run(local=True)
    monkeypatch.setattr(gate.evaluator, "prepare_local_evaluation", prepare)
    with gate.db.connect() as c:
        c.execute("UPDATE agent_objective SET status=?", (status,))
        c.commit()
    with pytest.raises(InvalidLearningEvaluationError, match="LOCAL_EVALUATION_NOT_ACTIVE"):
        prepare(turn)
    with gate.db.connect() as c:
        assert c.execute("SELECT status FROM agent_objective").fetchone()[0] == status
        assert c.execute("SELECT COUNT(*) FROM rubric_assessment").fetchone()[0] == 0


def test_local_partial_mapping_can_accumulate_without_pretending_full_coverage(gate, no_model):
    service = RubricEvidenceService(gate.db)
    first, _, turn = gate.run(local=True)
    snapshot = first["evaluation"]["rubric_assessment"]
    assert len(snapshot["criteria"]) > 1
    review(service, {**snapshot, "criteria": snapshot["criteria"][:1]})
    current = gate.evaluator.prepare_local_evaluation(turn)["evaluation"]
    assert current["outcome_level"] == "understood_now"
    assert "success_criteria_not_covered" in current["mastery_gate"]["missing"]
    second, _, turn = gate.run(local=True, item=SECOND, answer="42")
    snapshot = second["evaluation"]["rubric_assessment"]
    review(service, {**snapshot, "criteria": snapshot["criteria"][1:]})
    assert gate.evaluator.prepare_local_evaluation(turn)["evaluation"]["outcome_level"] == "independent"
