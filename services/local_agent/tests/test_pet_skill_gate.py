import pytest

from home_study_agent.pet_skill_gate import pet_skill_gate
from test_pet_media_evidence import media, CHILD, change, complete


@pytest.mark.parametrize("item,modality", [("diag-pet-listening-1", "listening"),
    ("diag-pet-listening-gist-baseline", "listening"), ("diag-pet-speaking-2", "speaking")])
def test_typed_node_modality_cannot_be_bypassed_by_text_answer_type(media, item, modality):
    with media[0].connect() as c:
        before = c.total_changes
        gate = pet_skill_gate(c, CHILD, item)
        assert gate["required"] and gate["modality"] == modality
        assert gate["skill_scoring_allowed"] is False and c.total_changes == before


@pytest.mark.parametrize("item", ["diag-p4-arithmetic-closed-1", "diag-pet-reading-1", "diag-pet-writing-1"])
def test_other_modalities_keep_existing_scorer_authority(media, item):
    with media[0].connect() as c:
        gate = pet_skill_gate(c, CHILD, item)
        assert not gate["required"] and gate["skill_scoring_allowed"]
        assert gate["scope"] == "modality_veto_only_not_a_score_or_mastery_grant"


def test_gate_never_calls_scoring_or_media_projection_recursively(media, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Admission gate recursed into a score-dependent projection")
    monkeypatch.setattr("home_study_agent.learning_obligations.project_child_mastery_evidence", forbidden)
    monkeypatch.setattr("home_study_agent.learning_obligations.qualified_exact_score", forbidden)
    monkeypatch.setattr("home_study_agent.pet_media_evidence.project_pet_media_evidence", forbidden)
    with media[0].connect() as c:
        assert not pet_skill_gate(c, CHILD, "diag-pet-listening-1")["skill_scoring_allowed"]


def test_single_clip_complete_is_not_original_two_play_task_delivery(media):
    row = complete(media)
    q = row["qualification"]
    assert q["listening_delivery_qualified"] is True
    assert q["clip_delivery_qualified"] is True and q["delivery_unit"] == "single_audio_clip"
    assert q["delivery_receipt"]["complete_play_count"] == 1
    assert q["task_delivery_qualified"] is False and q["required_play_count"] == 2
    assert q["skill_scoring_allowed"] is False


def test_withdrawal_and_cross_child_cannot_find_an_ungated_fallback(media):
    with media[0].connect() as c:
        assert not pet_skill_gate(c, "child-junior-3", "diag-pet-listening-1")["skill_scoring_allowed"]
    change(media[0], "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id='diag-pet-listening-1'")
    with media[0].connect() as c:
        assert not pet_skill_gate(c, CHILD, "diag-pet-listening-1")["skill_scoring_allowed"]


def test_non_pet_veto_does_not_replace_existing_grade_or_review_authority(media):
    with media[0].connect() as c:
        for child in (CHILD, "child-junior-3", "not-an-owner"):
            gate = pet_skill_gate(c, child, "diag-p4-arithmetic-closed-1")
            assert gate["status"] == "not_applicable" and not gate["required"]
            assert gate["skill_scoring_allowed"]
            assert gate["scope"] == "modality_veto_only_not_a_score_or_mastery_grant"
