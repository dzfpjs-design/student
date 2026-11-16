"""Actual local publisher and encrypted synthetic figure, not pupil outcomes."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import LocalSupplyError, validate_item, score_local_item
from home_study_agent.open_response_materials import bind_material_excerpt
from test_pet_reviewed_source_actual_http import NoCloud
from test_visual_material_host import setup_case, headers, REGION


def supply_case(db, binding):
    LearningStateService(db).seed_catalog()
    kb = KnowledgeBaseService(db)
    caption = kb.import_source('child', dict(source_type='school_material', subject='物理', grade_band='初三',
        title='Synthetic figure caption', version_label='visual-test', source_ref='isolated:caption',
        license_status='family_owned_index', parent_reviewed=True,
        content_text='Observe the closed outline. Explain which relationships are visible and which require further evidence.'))
    with db.connect() as c:
        eid = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (caption['source_id'],)).fetchone()[0]
        material = bind_material_excerpt(c, child_id='child', excerpt_id=eid, material_key='figure')
    material['visual'] = binding
    source = dict(schema='reviewed_open_response_source_v3', subject='物理', grade='初三',
        knowledge_node_id='node-j3-physics-experiment-reasoning', answer_type='open_response',
        assessment_mode='open_response_rubric', task_family='scientific_explanation', response_mode='written',
        prompt='Explain the visible outline and distinguish observations from inferences.',
        task_points=['Identify the visible marks.', 'State the limits of the evidence.'],
        context_requirement='external_material', material_semantics='image_required', material_bindings=[material])
    imported = kb.import_source('child', dict(source_type='school_material', subject='物理', grade_band='初三',
        title='Synthetic original visual task', version_label='visual-test', source_ref='isolated:original',
        license_status='family_owned_index', parent_reviewed=True, content_text=json.dumps(source)))
    quote = 'Explain observations using traceable reasoning and identify the limits of the evidence.'
    standard_root = db.path.parent.resolve(strict=True) / 'visual-standards'
    standard_root.mkdir()
    (standard_root / 'manifest.json').write_text(json.dumps({
        'format': 'hsc_official_curriculum_sources_v1',
        'sources': [{'source_key': 'visual-test', 'pdf_filename': None, 'pages_filename': None}],
    }), encoding='utf-8')
    with db.connect() as c:
        eid = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (imported['source_id'],)).fetchone()[0]
        c.execute("""INSERT INTO curriculum_standard_source(source_id,source_key,jurisdiction,source_type,subject_title,title,
            version_label,official_url,local_root_path,status,imported_at,updated_at)
            VALUES('visual-standard','visual-test','CN','national_standard','物理','Synthetic standard','2022',
            'isolated:standard',?,'structured','2026-09-01','2026-09-01')""", (str(standard_root),))
        c.execute("INSERT INTO curriculum_standard_page VALUES('visual-standard',12,?,'fixture-hash',20)", (quote,))
        c.execute("""INSERT INTO curriculum_standard_requirement(requirement_id,source_id,subject_title,stage_scope,
            requirement_type,title,requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
            VALUES('visual-requirement','visual-standard','物理','第四学段','content_requirement',?,?,'Reasoning',12,12,.9,
            'verified','2026-09-01','2026-09-01')""", (quote, quote))
        c.commit()
    CurriculumSupplyService(db).select_curriculum('child', dict(grade='初三', subject='物理', edition='visual-test',
        semester='上学期', standard_scope='第四学段', textbook_source_id=imported['source_id']),
        actor='parent', action_id='visual-scope', expected_version=0)
    spec = dict(seed_item_id='diag-j3-physics-experiment-1', stage='parallel', excerpt_id=eid,
        requirement_id='visual-requirement', standard_page=12, standard_quote=quote,
        validator='reviewed_open_response_rubric_source_v1', explanation='Reviewed source, not mastery evidence.', difficulty=2)
    return source, imported['source_id'], spec


@pytest.mark.parametrize('fault', ['retract', 'missing'])
def test_visual_original_publish_restart_and_image_invalidation(tmp_path, fault):
    db, settings, prepared, args, region = setup_case(tmp_path)
    base = '/api/parent/children/child/curriculum-supply'
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42180)) as client:
        h = headers(client)
        client.app.state.visual_materials.stage_prepared('child', 'page', 'figure',
            revision_id=args['revision_id'], region_version=args['region_version'], prepared=prepared)
        stored = client.post(REGION+'/storage', json={**args, 'client_action_id':'store',
            'purpose':'local_visual_material_review_v1'}, headers=h)
        assert stored.status_code == 200, stored.text
        image = client.get(REGION+'/image', params={**args, 'sha256':region['safe_png_sha256']}, headers=h)
        assert image.status_code == 200
        displayed = client.post(REGION+'/displayed', json={**args, 'client_action_id':'display',
            'display_token':image.headers['X-HSC-Visual-Display-Token'], 'sha256':region['safe_png_sha256'],
            'width':prepared.width, 'height':prepared.height}, headers=h)
        confirmed = client.post(REGION+'/confirm', json={**args, 'client_action_id':'confirm',
            'display_receipt_id':displayed.json()['display_receipt_id']}, headers=h)
        assert confirmed.status_code == 200, confirmed.text
        binding = dict(artifact_id='page', region_id='figure', revision_id=args['revision_id'],
            region_version=args['region_version'], facts_version=args['facts_version'],
            confirmation_id=confirmed.json()['detail']['binding']['confirmation_id'])
        source, sid, spec = supply_case(db, binding)
        options = client.get(base+'/sources/'+sid+'/options', headers=h)
        assert options.status_code == 200, options.text
        assert any(s['seed_item_id'] == spec['seed_item_id'] for s in options.json()['seeds']), options.text
        result = client.post(base+'/generate', json=dict(action_id='draft', expected_version=0,
            expected_scope_version=1, source=spec), headers=h)
        assert result.status_code == 200, result.text
        cid = result.json()['candidate_id']
        for version, operation in enumerate(('validate', 'review', 'publish'), 1):
            body = dict(action_id=operation, expected_version=version)
            if operation == 'review': body.update(decision='approve', note='Checked visible original and source.')
            result = client.post(base+'/'+cid+'/'+operation, json=body, headers=h)
            assert result.status_code == 200, result.text
        published = result.json()
        assert published['eligible'] and published['review_packet']['open_response'] == source, published
        item_id = published['diagnostic_item_id']
        with client.app.state.repository.database.connect() as c:
            item = validate_item(c, 'child', item_id)
            assert score_local_item(item, ' '.join(source['task_points'])) is None
            proof = json.loads(c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[0])
            assert proof['open_response_materials']['visual_materials'][0]['binding'] == binding
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42181)) as client:
        h = headers(client)
        result = client.get(base+'/'+cid, headers=h)
        assert result.status_code == 200 and result.json()['eligible'], result.text
        if fault == 'retract':
            assert client.post(REGION+'/retract', json={**args, 'client_action_id':'retract'}, headers=h).status_code == 200
        else:
            from pathlib import Path
            with db.connect() as c:
                path = c.execute("SELECT local_path FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0]
            Path(path).unlink()
        result = client.get(base+'/'+cid, headers=h)
        assert result.status_code == 200 and not result.json()['eligible'], result.text
        with client.app.state.repository.database.connect() as c, pytest.raises(LocalSupplyError):
            validate_item(c, 'child', item_id)


def test_core_only_figure_cannot_be_published_without_host_image(tmp_path):
    from test_visual_open_response import source_case
    db, _, _, original = source_case(tmp_path)
    _, _, spec = supply_case(db, original['material_bindings'][0]['visual'])
    service = CurriculumSupplyService(db)
    draft = service.generate_draft('child', spec, author='author', action_id='draft')
    assert draft['state'] == 'blocked' and 'VISUAL_MATERIAL_MANAGED_IMAGE_REQUIRED' in draft['blockers']
    checked = service.validate('child', draft['candidate_id'], actor='validator', action_id='validate', expected_version=1)
    assert checked['state'] == 'blocked' and not checked['eligible']
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM diagnostic_item WHERE source_ref LIKE 'parent_approved_specialized:%'").fetchone()
