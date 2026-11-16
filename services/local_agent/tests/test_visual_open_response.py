"""Visual core contracts on stored sources; not a published/graded pupil trial."""
import json

import pytest

from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.local_supply import LocalSupplyError
from home_study_agent.open_response_materials import bind_material_excerpt, resolve_source_materials
from home_study_agent.open_response_source import parse, rubric, check
from home_study_agent.open_response_profile import profile_for_item
from test_visual_material import visual_case, registered, confirmed


def source_case(tmp_path, subject='物理', grade='初三'):
    db, monitor, raw, scope = visual_case(tmp_path, grade=grade)
    region = registered(db, raw, scope)
    confirmation = confirmed(db, region, scope)
    source = KnowledgeBaseService(db).import_source('child', dict(source_type='school_material',
        subject=subject, grade_band=grade, title='Synthetic authored figure caption', version_label='figure-1',
        source_ref='isolated:authored-figure', license_status='family_owned_index', parent_reviewed=True,
        content_text='Observe the bounded diagram; the marks are given conditions, not measured pupil results.'))
    with db.connect() as c:
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (source['source_id'],)).fetchone()[0]
        binding = bind_material_excerpt(c, child_id='child', excerpt_id=excerpt, material_key='figure')
    binding['visual'] = confirmation['binding']
    typed = dict(schema='reviewed_open_response_source_v3', subject=subject, grade=grade,
        knowledge_node_id='synthetic-visual-node', answer_type='open_response', assessment_mode='open_response_rubric',
        task_family='mathematical_process' if subject == '数学' else 'scientific_explanation', response_mode='written',
        prompt='Describe the marked relationships in the figure and explain your reasoning.',
        task_points=['Distinguish visible marks from inferred relationships.'], context_requirement='external_material',
        material_semantics='image_required', material_bindings=[binding])
    return db, scope, region, typed


@pytest.mark.parametrize('subject,grade', [('数学', '四年级'), ('科学', '四年级'), ('物理', '初三')])
def test_visual_source_grade_profile_and_material_consumer(tmp_path, subject, grade):
    db, _, _, source = source_case(tmp_path, subject, grade)
    assert parse(json.dumps(source)) == source
    with db.connect() as c:
        material = resolve_source_materials(c, child_id='child', source=source)
        assert material['visual_materials'][0]['facts']['observations']
        assert material['cloud_transmission_allowed'] is False
        assert material['mastery_claim_allowed'] is False
    node = dict(status='active', track='school', grade_band=grade, subject=subject,
                knowledge_node_id=source['knowledge_node_id'])
    item = dict(status='active', knowledge_node_id=node['knowledge_node_id'], answer_type='open_response',
        assessment_mode='open_response_rubric', expected_answer_json=json.dumps(rubric(source)),
        stimulus_text=material['stimulus_text'], stimulus_asset=None)
    profile = profile_for_item(item, node)
    assert profile['grade'] == grade
    assert profile['original_requirement'] == 'written_original'
    assert profile['qualification_status'] == 'needs_review' and not profile['mastery_claim_allowed']
    if grade == '四年级':
        assert not any('判别式' in str(d) or '量纲' in str(d) for d in profile['dimensions'])


def test_legacy_visual_source_not_silently_upgraded_or_published(tmp_path):
    db, _, _, source = source_case(tmp_path)
    legacy = dict(source, schema='reviewed_open_response_source_v2')
    legacy['material_bindings'] = [{k: v for k, v in source['material_bindings'][0].items() if k != 'visual'}]
    with db.connect() as c, pytest.raises(LocalSupplyError, match='IMAGE_SEMANTICS_REQUIRED'):
        resolve_source_materials(c, child_id='child', source=legacy)
    node = dict(status='active', track='school', subject='物理', grade_band='初三', knowledge_node_id=source['knowledge_node_id'])
    seed = dict(knowledge_node_id=source['knowledge_node_id'], answer_type='text', prompt='Different original task.')
    payload = dict(validator='reviewed_open_response_rubric_source_v1', stage='parallel', prompt=source['prompt'],
                   expected_answer=None, open_response=source)
    with pytest.raises(LocalSupplyError, match='LIVE_SOURCE_REQUIRED'):
        check(payload, seed, node=node)


def test_withdrawn_caption_and_confirmed_figure_each_invalidate(tmp_path):
    from home_study_agent.visual_material import confirm_facts
    db, scope, region, source = source_case(tmp_path)
    with db.connect() as c:
        ready = resolve_source_materials(c, child_id='child', source=source)
        c.execute('BEGIN IMMEDIATE')
        binding = source['material_bindings'][0]['visual']
        confirm_facts(c, **scope, region_id='figure', region_version=region['region_version'],
            facts_version=binding['facts_version'], decision='retract', client_action_id='retract')
        with pytest.raises(LocalSupplyError): resolve_source_materials(c, child_id='child', source=source)
        c.rollback()
        assert resolve_source_materials(c, child_id='child', source=source) == ready
        c.execute("UPDATE knowledge_source SET review_status='quarantined'")
        with pytest.raises(LocalSupplyError): resolve_source_materials(c, child_id='child', source=source)


def test_visual_formative_projection_never_borrows_text_grant(tmp_path):
    from home_study_agent.formative_evaluation_projection import visual_material_projection
    db, _, _, source = source_case(tmp_path)
    with db.connect() as c:
        result = visual_material_projection(c, child_id='child', source=source)
        assert result['status'] == 'local_required' and result['payload'] is None
        assert result['required_purpose'] == 'visual_material_formative_v3'
        assert result['reason'] == 'visual_purpose_authorization_and_transport_required'
        assert result['local_audit']['visual_materials'][0]['binding'] == source['material_bindings'][0]['visual']


def test_live_original_source_checker_and_exact_snapshot_are_required(tmp_path):
    """Stored synthetic node validates the core hook, not actual curriculum readiness."""
    from home_study_agent.learning_state import LearningStateService
    db, _, _, source = source_case(tmp_path)
    LearningStateService(db).seed_catalog()
    with db.connect() as c:
        node = dict(c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id='node-j3-physics-experiment-reasoning'").fetchone())
        source['knowledge_node_id'] = node['knowledge_node_id']
        seed = dict(c.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id='diag-j3-physics-experiment-1'").fetchone())
        payload = dict(validator='reviewed_open_response_rubric_source_v1',stage='parallel',prompt=source['prompt'],
            expected_answer=None,open_response=source)
        c.execute('BEGIN')
        result = check(payload,seed,node=node,connection=c,child_id='child')
        assert result['visual_materials']['visual_materials'][0]['fact_status'] == 'confirmed'
        assert result['mastery_claim_allowed'] is False
        altered = json.loads(json.dumps(payload))
        altered['open_response']['material_bindings'][0]['visual']['region_version'] = '0'*64
        with pytest.raises(LocalSupplyError): check(altered,seed,node=node,connection=c,child_id='child')
        with pytest.raises(LocalSupplyError): check(payload,seed,node={**node,'subject':'数学'},connection=c,child_id='child')
        with pytest.raises(LocalSupplyError): check(payload,seed,node=node,connection=c,child_id='other')


def test_item_version_carries_figure_even_when_ocr_and_prompt_are_unchanged(tmp_path):
    from home_study_agent.diagnostic_prompt import _version
    db, _, _, source = source_case(tmp_path)
    with db.connect() as c:
        material = resolve_source_materials(c,child_id='child',source=source)
    # The source body is fixed except the figure identity. The changed reference
    # is intentionally unqualified; this asserts version sensitivity, not approval.
    source2 = json.loads(json.dumps(source))
    source2['material_bindings'][0]['visual']['region_version'] = '0'*64
    common = dict(diagnostic_item_id='same',knowledge_node_id='same',subject='物理',prompt=source['prompt'],
        answer_type='open_response',assessment_mode='open_response_rubric',stimulus_text=material['stimulus_text'],
        stimulus_asset=None,explanation='',difficulty=2,source_ref='same',status='active')
    assert _version({**common,'expected_answer':rubric(source)}) != _version({**common,'expected_answer':rubric(source2)})
