"""Typed original source packets are candidates, never self-approving scores."""

import json

import pytest

from home_study_agent.local_supply import LocalSupplyError
from home_study_agent.pet_reviewed_source import parse_listening_source, normalize_pet_exact_text, listening_exact_rubric


def packet(**changes):
    return {"schema": "pet_listening_source_v1", "modality": "listening", "assessment_mode": "objective",
            "prompt": "Where should Mira wait?", "stimulus_text": "Mira, please wait at the red bridge.",
            "instruction": "Listen once and answer the question.", "required_play_count": 1,
            "expected_answer": {"local_rule": "local_exact_v1", "exact_answers": ["at the red bridge"]},
            **changes}


def test_preserves_complete_original_contract_with_no_seed_id_rule():
    source = packet()
    assert parse_listening_source(json.dumps(source)) == source
    different = packet(prompt="Which coat should Jules bring?", stimulus_text="Jules needs the yellow coat.",
                       instruction="Listen three times, then answer.", required_play_count=3,
                       expected_answer={"local_rule": "local_exact_v1", "exact_answers": ["the yellow coat", "yellow coat"]})
    assert parse_listening_source(json.dumps(different)) == different


@pytest.mark.parametrize("changes", [
    {"modality": "speaking"}, {"assessment_mode": "subjective"},
    {"required_play_count": None}, {"required_play_count": True}, {"required_play_count": 0},
    {"required_play_count": 4}, {"stimulus_text": ""}, {"instruction": ""},
    {"expected_answer": {"keywords": ["red", "bridge"]}},
    {"expected_answer": {"local_rule": "local_exact_v1", "exact_answers": []}},
    {"expected_answer": {"local_rule": "local_exact_v1", "exact_answers": [1]}},
    {"expected_answer": {"local_rule": "local_exact_v1", "exact_answers": [" "]}},
    {"audio_path": "C:\\family\\private.wav"}, {"cloud_allowed": True}, {"source_reviewed": True},
])
def test_unknown_subjective_or_untrusted_permissions_are_not_exact(changes):
    with pytest.raises(LocalSupplyError):
        parse_listening_source(json.dumps(packet(**changes)))


@pytest.mark.parametrize("raw", ["Listen twice: where should Mira wait?", "{}", "[]", "null",
                                   '{"schema":"pet_listening_source_v1","schema":"forged"}'])
def test_no_keyword_count_inference_or_legacy_automatic_upgrade(raw):
    with pytest.raises(LocalSupplyError):
        parse_listening_source(raw)


def test_exact_text_preserves_word_boundaries_negation_and_complete_answers():
    assert normalize_pet_exact_text("  AT  THE RED BRIDGE. ") == "at the red bridge"
    assert normalize_pet_exact_text("now here") != normalize_pet_exact_text("nowhere")
    assert normalize_pet_exact_text("not at the red bridge") != "at the red bridge"
    assert listening_exact_rubric(packet()) == {"local_rule": "local_exact_v1", "answer_format": "pet_exact_text_v1",
                                               "exact_answers": ["at the red bridge"]}


@pytest.mark.parametrize("answer,score", [("AT THE RED BRIDGE.", 1), ("red bridge", 0),
    ("not at the red bridge", 0), ("attheredbridge", 0), ("at the red bridge or yellow station", 0)])
def test_existing_numeric_scorer_requires_complete_pet_reference(answer, score):
    from home_study_agent.local_supply import score_local_item
    item = {"expected_answer_json": json.dumps(listening_exact_rubric(packet())), "explanation": "Original reference"}
    assert score_local_item(item, answer)[0] == score


def test_word_boundary_collision_is_not_a_correct_response():
    from home_study_agent.local_supply import score_local_item
    source = packet(expected_answer={"local_rule": "local_exact_v1", "exact_answers": ["now here"]})
    item = {"expected_answer_json": json.dumps(listening_exact_rubric(source)), "explanation": "Original reference"}
    assert score_local_item(item, "nowhere")[0] == 0
