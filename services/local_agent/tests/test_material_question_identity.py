"""Reviewed source identity includes frozen materials, not mutable item copies."""
import pytest

from home_study_agent.learning_obligations import _question_catalog, question_identity
from home_study_agent.local_supply import validate_item
from test_open_response_materials import material_case, publish_open, CHILD


@pytest.mark.parametrize('material_count', [1, 2])
def test_material_identity_matches_published_content_and_survives_item_tampering(tmp_path, material_count):
    db, supply, spec, _, _, _ = material_case(tmp_path, material_count=material_count)
    item_id = publish_open(supply, spec)['diagnostic_item_id']
    with db.connect() as c:
        original = validate_item(c, CHILD, item_id)
        expected = question_identity(original)
        _, identities = _question_catalog(c)
        assert identities[item_id] == expected
        assert identities[item_id] != question_identity({'prompt': original['prompt']})
        assert identities[item_id] != question_identity({**original, 'stimulus_text': original['stimulus_text']+' Changed conditions.'})
        c.execute("UPDATE diagnostic_item SET prompt='tampered',stimulus_text='tampered material' WHERE diagnostic_item_id=?", (item_id,))
        _, changed = _question_catalog(c)
        assert changed[item_id] == expected
        c.rollback()
