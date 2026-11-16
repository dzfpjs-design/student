"""Bounded oral authoring controls; none of these checks certify a learner."""
import json

import pytest

from home_study_agent.pet_oral_source import parse, bind_stimulus, materials, check, VALIDATOR
from home_study_agent.knowledge_base import KnowledgeBaseService, InvalidKnowledgeSourceError
from home_study_agent.local_supply import LocalSupplyError, validate_item
from test_pet_oral_supply_http import setup, packet, FAMILIES, CHILD, OTHER
from test_pet_typed_source_import import values


@pytest.mark.parametrize('fault', ['extra_grant', 'exact_answer', 'wrong_type', 'no_text', 'no_picture',
    'foreign_picture', 'family', 'empty_points', 'duplicate_points', 'duplicate_key', 'control_text', 'long'])
def test_invalid_oral_authoring_is_atomic_without_generic_fallback(tmp_path, fault):
    db, _ = setup(tmp_path)
    family = 'picture_description' if fault in {'no_picture', 'foreign_picture'} else 'read_aloud'
    source = packet(db, family)
    if fault == 'extra_grant': source['cloud_transmission_allowed'] = True
    elif fault == 'exact_answer': source['expected_answer'] = {'exact_answers': ['yes']}
    elif fault == 'wrong_type': source['answer_type'] = 'text'
    elif fault == 'no_text': source['stimulus_text'] = None
    elif fault == 'no_picture': source['stimulus_binding'] = None
    elif fault == 'foreign_picture': source['stimulus_binding']['source_version'] = True
    elif fault == 'family': source['task_family'] = 'listening'
    elif fault == 'empty_points': source['task_points'] = []
    elif fault == 'duplicate_points': source['task_points'] = ['Say why', ' Say why ']
    elif fault == 'control_text': source['stimulus_text'] = 'text\x00hidden'
    elif fault == 'long': source['stimulus_text'] = 'x' * 6001
    text = json.dumps(source, ensure_ascii=False)
    if fault == 'duplicate_key': text = text[:-1] + ',"answer_type":"read_aloud"}'
    with pytest.raises(LocalSupplyError): parse(text)
    with pytest.raises(InvalidKnowledgeSourceError): KnowledgeBaseService(db).import_source(CHILD, values(text))
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM knowledge_source').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM knowledge_excerpt').fetchone()[0] == 0


def test_whole_read_aloud_document_not_truncated_and_no_transfer_or_exact_grant(tmp_path):
    db, _ = setup(tmp_path)
    original = packet(db, 'read_aloud')
    original['stimulus_text'] *= 15
    text = json.dumps(original, ensure_ascii=False, indent=2)
    source = KnowledgeBaseService(db).import_source(CHILD, values(text))
    with db.connect() as c:
        assert c.execute('SELECT content_text FROM knowledge_excerpt WHERE source_id=?',
                         (source['source_id'],)).fetchone()[0] == text
        seed = validate_item(c, CHILD, FAMILIES['read_aloud'])
        for stage in ('near_transfer', 'far_transfer', 'delayed_7d'):
            with pytest.raises(LocalSupplyError, match='STAGE_UNAVAILABLE'):
                check(dict(validator=VALIDATOR, stage=stage, prompt=original['prompt'],
                           expected_answer=None, pet_oral=original), seed)


def test_picture_descriptor_is_exact_current_owned_material_not_latest(tmp_path):
    db, _ = setup(tmp_path)
    original = packet(db, 'picture_description')
    with db.connect() as c:
        assert bind_stimulus(c, CHILD, FAMILIES['picture_description']) == original['stimulus_binding']
        with pytest.raises(LocalSupplyError):
            materials(c, OTHER, original)
        altered = json.loads(json.dumps(original))
        altered['stimulus_binding']['stimulus_asset'] = 'some-unrelated-image'
        with pytest.raises(LocalSupplyError, match='PICTURE_CHANGED'):
            materials(c, CHILD, altered)
        c.execute("UPDATE diagnostic_item SET stimulus_asset='changed' WHERE diagnostic_item_id=?",
                  (FAMILIES['picture_description'],))
        with pytest.raises(LocalSupplyError):
            materials(c, CHILD, original)


def test_picture_binding_rejects_generated_alias_before_recursive_validation(tmp_path):
    db, _ = setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET source_ref='parent_approved_specialized:cycle' WHERE diagnostic_item_id=?",
                  (FAMILIES['picture_description'],))
        with pytest.raises(LocalSupplyError, match='PICTURE_ORIGINAL_REQUIRED'):
            bind_stimulus(c, CHILD, FAMILIES['picture_description'])
