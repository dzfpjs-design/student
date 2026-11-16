"""Primary ordinary responses remain source-bound references, never local exact scores."""
import copy
import json

import pytest

from home_study_agent.open_response_profile import profile_for, profile_for_item
from home_study_agent.open_response_source import check, parse
from test_open_response_source import packet, inputs


CASES = [('数学', 'mathematical_process'), ('语文', 'textual_interpretation'),
         ('语文', 'communicative_writing'), ('英语', 'textual_interpretation')]


def test_primary_catalog_has_four_distinct_non_exact_original_seeds(tmp_path):
    from home_study_agent.database import Database
    from home_study_agent.repository import StudyRepository
    from home_study_agent.learning_state import LearningStateService
    from home_study_agent.local_supply import validate_item, score_local_item
    db = Database(tmp_path/'catalog.db')
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    with db.connect() as c:
        for slug in ('math-reasoning', 'chinese-reading', 'chinese-writing', 'english-reading'):
            row = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                            ('diag-p4-open-'+slug+'-1',)).fetchone()
            assert row is not None, slug
            item = validate_item(c, 'child-primary-4', row['diagnostic_item_id'])
            node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
                                  (item['knowledge_node_id'],)).fetchone())
            assert node['grade_band'] == '四年级' and node['track'] == 'school'
            assert profile_for_item(item, node)['version'] == 'school_primary_open_response_profiles_v1'
            assert score_local_item(item, ' '.join(json.loads(item['expected_answer_json'])['task_points'])) is None


@pytest.mark.parametrize('subject,family', CASES)
def test_primary_ordinary_contract_resolves_its_own_persisted_profile(subject, family):
    source = packet(subject, family)
    source['grade'] = '四年级'
    before = copy.deepcopy(source)
    parsed = parse(json.dumps(source))
    payload, seed, node = inputs(parsed)
    node['grade_band'] = '四年级'
    result = check(payload, seed, node=node)
    item = dict(knowledge_node_id=node['knowledge_node_id'], answer_type='open_response',
                assessment_mode='open_response_rubric', status='active',
                expected_answer_json=json.dumps(result['expected_answer']))
    profile = profile_for_item(item, node)
    assert profile['grade'] == '四年级'
    assert profile['version'] == 'school_primary_open_response_profiles_v1'
    assert profile['score_status'] == 'reference'
    assert profile['qualification_status'] == 'needs_review'
    assert not profile['mastery_claim_allowed'] and not profile['automatic_certification']
    assert profile['original_requirement'] == 'written_original'
    assert not {'keywords', 'exact_answers', 'local_rule'} & result['expected_answer'].keys()
    assert source == before
    junior = profile_for(subject, task_family=family, response_mode='written')
    forged = dict(result['expected_answer'], review_profile=junior['profile_id'], profile_version=junior['version'])
    item['expected_answer_json'] = json.dumps(forged)
    with pytest.raises(ValueError, match='OPEN_RESPONSE_ITEM_PROFILE_CHANGED'):
        profile_for_item(item, node)


@pytest.mark.parametrize('fault', ['grade','family','science','keywords','mode','untyped_grade'])
def test_primary_extension_does_not_expand_unreviewed_scope(fault):
    source = packet('英语','textual_interpretation')
    source['grade']='四年级'
    if fault=='grade': source['grade']='三年级'
    elif fault=='family': source['task_family']='communicative_writing'
    elif fault=='science': source.update(subject='科学',task_family='scientific_explanation')
    elif fault=='keywords': source['keywords']=['correct']
    elif fault=='mode': source['response_mode']='chat'
    elif fault=='untyped_grade': source['grade']=['四年级']
    with pytest.raises(ValueError, match='OPEN_RESPONSE_'):
        parse(json.dumps(source))


@pytest.mark.parametrize('subject,family', [('数学','mathematical_process'),('科学','scientific_explanation')])
def test_primary_visual_profile_identity_is_not_reassigned_to_ordinary(subject, family):
    old = profile_for(subject,task_family=family,response_mode='written',grade='四年级')
    visual = profile_for(subject,task_family=family,response_mode='written',grade='四年级',
        source_schema='reviewed_open_response_source_v3')
    assert old==visual
    assert visual['version']=='school_visual_response_profiles_v1'
    if subject=='数学':
        ordinary=profile_for(subject,task_family=family,response_mode='written',grade='四年级',
            source_schema='reviewed_open_response_source_v1')
        assert ordinary['profile_id']!=visual['profile_id']
        assert ordinary['dimensions']!=visual['dimensions']


@pytest.mark.parametrize('subject,grade', [('数学','四年级'),('科学','四年级'),('物理','初三')])
def test_existing_visual_profile_can_roundtrip_its_actual_http_reference(tmp_path, subject, grade):
    from test_visual_open_response import source_case
    from home_study_agent.open_response_source import rubric
    from home_study_agent.curriculum_supply_http import OpenVisualReference
    _, _, _, source = source_case(tmp_path, subject, grade)
    expected=rubric(source)
    assert OpenVisualReference.model_validate(expected).model_dump()==expected
