import json
from uuid import uuid4

import pytest

from home_study_agent.learning_outcome_coordinator import LearningOutcomeCoordinator
from home_study_agent.learning_outcome_evaluator import InvalidLearningEvaluationError
from home_study_agent.learning_rubric import RubricEvidenceService
from test_learning_outcome_mastery_gate import gate, flow, CHILD, ITEM, SECOND


@pytest.mark.parametrize("source", ["parent_approved_variant:source", "local_verified_specialized:source",
                                    "textbook:local", "atomic:local", "unknown"])
def test_stripping_adapter_labels_does_not_send_local_diagnostic_to_model(gate, monkeypatch, source):
    with gate.db.connect() as c:
        c.execute("UPDATE diagnostic_item SET source_ref=? WHERE diagnostic_item_id=?", (source, ITEM))
        c.commit()
    async def forbidden(request):
        pytest.fail("Restricted diagnostic reached the model gateway")
    monkeypatch.setattr(gate.gateway, "complete", forbidden)
    with pytest.raises(InvalidLearningEvaluationError, match="LEARNING_EVALUATION_LOCAL_ONLY"):
        gate.run(receipt=False)
    with gate.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM learning_evaluation_execution").fetchone()[0] == 0


@pytest.mark.parametrize("flag", [{"local_only": True}, {"cloud_transmission_allowed": False},
                                 {"source": {"cloud_transmission_allowed": False}}])
def test_continuing_a_local_objective_cannot_launder_its_context(gate, monkeypatch, flag):
    _, _, turn = gate.run()
    with gate.db.connect() as c:
        c.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?", (json.dumps(flag), turn))
        c.commit()
    async def forbidden(request):
        pytest.fail("Local objective context reached the model gateway")
    monkeypatch.setattr(gate.gateway, "complete", forbidden)
    with pytest.raises(InvalidLearningEvaluationError, match="LEARNING_EVALUATION_LOCAL_ONLY"):
        gate.run(item=SECOND, answer="42", days=1)
    with gate.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 1


def test_adapter_receipt_is_local_data_not_a_cloud_criteria_mapping(gate):
    source = gate.learning.submit_diagnostic(gate.sid, ITEM, "90", str(uuid4()))
    result = LearningOutcomeCoordinator(gate.db).diagnostic_evaluation_input(CHILD, gate.objective, source["evidence_id"])
    assert result["status"] == "ready"
    assert result["local_only"] is True and result["cloud_transmission_allowed"] is False
    assert result["evaluation_route"] == "local_criterion_review_required"
    assert result["automatic_criterion_mapping"] is False
    assert result["local_receipt"]["score_status"] == "verified_local"
    assert result["local_receipt"]["independent"] == 1
    assert result["local_receipt"]["hint_level"] == 0
    assert result["criterion_mapping_status"] == "requires_local_review"


def test_open_adapter_preserves_the_same_no_cloud_boundary(gate):
    source = gate.learning.submit_diagnostic(gate.sid, "diag-pet-writing-1", "A short invitation", str(uuid4()))
    result = RubricEvidenceService(gate.db).diagnostic_candidate_input(CHILD, source["evidence_id"])
    assert result["cloud_transmission_allowed"] is False and result["local_only"] is True
    assert result["score_status"] == "candidate"
    assert result["evaluation_route"] == "local_criterion_review_required"
