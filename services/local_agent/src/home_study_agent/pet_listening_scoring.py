"""Audited exact-response adapters for original objective listening practice.

Only complete normalized answers match; no substring/keyword rubric. The actual
numeric scorer remains local_supply.score_local_item. No item/snapshot is edited.
"""

import hashlib
import json

from .local_supply import validate_item


EXACT_RESPONSES = {
    "diag-pet-listening-1": ["in front of the library", "in front of library", "the front of the library",
        "leo should meet anna in front of the library", "在图书馆门口", "图书馆门口"],
    "diag-pet-listening-2": ["a raincoat", "raincoat", "a rain coat", "students must bring a raincoat", "雨衣", "一件雨衣"],
}


def listening_scoring_item(connection, child_id, item_id):
    item = validate_item(connection, child_id, item_id)
    if item["source_ref"].startswith("parent_approved_specialized:"):
        from .pet_reviewed_source import published_listening_source
        source = published_listening_source(connection, child_id, item_id)
        if source is None:
            return None
        version = hashlib.sha256(json.dumps({"source": item, "contract": source}, sort_keys=True,
                                            ensure_ascii=True).encode()).hexdigest()
        return {**item, "listening_scoring_version": "pet_reviewed_listening_exact_v1:" + version}
    responses = EXACT_RESPONSES.get(item_id)
    if not responses or item["answer_type"] != "listening_text" or item["assessment_mode"] != "objective":
        return None
    from .learning_state import normalize_spoken_math
    rubric = {"local_rule": "local_exact_v1", "exact_answers": [normalize_spoken_math(value) for value in responses]}
    version = hashlib.sha256(json.dumps({"source": item, "rubric": rubric}, sort_keys=True,
                                         ensure_ascii=True).encode()).hexdigest()
    return {**item, "expected_answer_json": json.dumps(rubric, ensure_ascii=False),
            "listening_scoring_version": "pet_original_listening_exact_v1:" + version}


def qualified_listening_score(connection, child_id, item_id, answer, **pet_scope):
    """Scoped branch for qualified_exact_score; keep stage/mistake kwargs separate."""
    from .pet_skill_gate import pet_skill_gate
    from .local_supply import LocalSupplyError, score_local_item
    try:
        gate = pet_skill_gate(connection, child_id, item_id, answer=answer, **pet_scope)
        if gate["modality"] != "listening" or not gate["skill_scoring_allowed"]:
            return None
        item = listening_scoring_item(connection, child_id, item_id)
        score = score_local_item(item, answer) if item else None
        return (validate_item(connection, child_id, item_id), score[0]) if score is not None else None
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return None
