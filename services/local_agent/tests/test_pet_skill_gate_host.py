"""Text-score and historical Tutor consumers must enforce the modality veto."""

import json
from types import SimpleNamespace

import pytest

from home_study_agent.learning_obligations import qualified_exact_score
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from home_study_agent.local_supply import validate_item
from home_study_agent.repository import StudyRepository
from test_pet_media_evidence import media, CHILD


@pytest.mark.parametrize("item_id", ["diag-pet-listening-1", "diag-pet-speaking-2"])
def test_exact_text_rubric_does_not_certify_audio_skill(media, monkeypatch, item_id):
    db = media[0]
    with db.connect() as c:
        item = dict(validate_item(c, CHILD, item_id))
        item.update(expected_answer_json=json.dumps({"local_rule": "local_exact_v1", "exact_answers": ["yes"]}),
                    assessment_mode="objective")
        monkeypatch.setattr("home_study_agent.local_supply.validate_item", lambda *a, **kw: item)
        monkeypatch.setattr("home_study_agent.learning_obligations._source_question", lambda *a: item)
        monkeypatch.setattr("home_study_agent.local_supply.score_local_item", lambda *a: (1, "correct"))
        before = c.total_changes
        assert qualified_exact_score(c, CHILD, item_id, "yes") is None
        assert c.total_changes == before


def test_school_exact_scoring_still_works(media):
    with media[0].connect() as c:
        score = qualified_exact_score(c, CHILD, "diag-p4-arithmetic-closed-1", "90")
        assert score and score[1] == 1


@pytest.mark.parametrize("item_id", ["diag-pet-listening-1", "diag-pet-speaking-2"])
def test_historical_reviewed_text_receipt_cannot_advance_audio_mastery(media, item_id):
    db = media[0]
    evaluator = LearningOutcomeEvaluator(db, StudyRepository(db), None, SimpleNamespace())
    receipt = {"evidence_id": "rubric:historical:1", "diagnostic_item_id": item_id,
               "correctness": 1, "independent": True, "hint_level": 0}
    record = {"child_id": CHILD, "evidence_binding": {"evidence_id": receipt["evidence_id"]},
              "reviewed_rubric_receipt": receipt}
    with db.connect() as c:
        gate = evaluator._mastery_gate(c, record, {"understanding": "independent"},
                                     {receipt["evidence_id"]: receipt}, [], ["independent answer"])
    assert gate["status"] == "blocked" and not gate["eligible"]
    assert gate["modality_gate"]["skill_scoring_allowed"] is False
    assert gate["outcome_level"] != "independent"
