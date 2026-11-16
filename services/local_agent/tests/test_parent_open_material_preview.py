"""Actual parent HTTP read, exact local material and withdrawal, no cloud."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from test_open_response_materials import material_case
from test_open_response_supply_http import CHILD, publish_open
from test_pet_reviewed_source_actual_http import NoCloud


@pytest.mark.parametrize('fault', ['other_child', 'changed_hash', 'image_required'])
def test_parent_projection_never_substitutes_or_leaks_unavailable_material(tmp_path, fault):
    from home_study_agent.open_response_materials import parent_source_materials
    args = {'owner':'child-primary-4'} if fault=='other_child' else {'semantics':'image_required'} if fault=='image_required' else {}
    db, _, _, source, binding, text = material_case(tmp_path, **args)
    with db.connect() as c:
        if fault == 'changed_hash':
            c.execute("UPDATE knowledge_excerpt SET content_hash='invalid' WHERE excerpt_id=?",(binding['excerpt_id'],));c.commit()
        before = c.total_changes
        projection = parent_source_materials(c, child_id=CHILD, source=source)
        assert projection['status']=='blocked' and projection['materials']==[]
        assert text not in json.dumps(projection,ensure_ascii=False)
        assert projection['local_only'] and not projection['cloud_transmission_allowed']
        assert c.total_changes == before


def test_parent_sees_current_full_materials_before_and_after_publication_and_restart(tmp_path):
    db, service, spec, source, binding, text = material_case(tmp_path, material_count=2)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False, session_token='parent-material-preview')
    base = '/api/parent/children/'+CHILD+'/curriculum-supply'
    with db.connect() as c:
        source_id = c.execute('SELECT source_id FROM knowledge_excerpt WHERE excerpt_id=?', (spec['excerpt_id'],)).fetchone()[0]
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41901), headers={'X-HSC-Session':settings.session_token}) as client:
        assert client.get(base+f'/sources/{source_id}/options').status_code == 401
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/setup', json={'pin':'7391'}).json()['parent_token']
        options = client.get(base+f'/sources/{source_id}/options')
        assert options.status_code == 200
        excerpt = next(e for e in options.json()['excerpts'] if e['excerpt_id'] == spec['excerpt_id'])
        preview = excerpt['current_materials']
        assert preview['status'] == 'ready' and preview['materials'][0]['text'] == text
        assert [m['material_key'] for m in preview['materials']] == ['given', 'measurement']
        assert all(m['title'] and m['version_label'] for m in preview['materials'])
        assert preview['local_only'] and not preview['cloud_transmission_allowed'] and not preview['mastery_claim_allowed']
        assert 'source_ref' not in json.dumps(preview) and 'source_id' not in json.dumps(preview)
        draft = client.post(base+'/generate', json=dict(action_id='parent-preview-draft',expected_version=0,expected_scope_version=2,source=spec))
        assert draft.status_code == 200
        cid = draft.json()['candidate_id']
        assert draft.json()['review_packet']['current_materials'] == preview
        version = 1
        for operation in ['validate','review','publish']:
            body = dict(action_id='parent-preview-'+operation,expected_version=version)
            if operation == 'review': body.update(decision='approve',note='核对两份完整合成原材料，非孩子能力证据。')
            response = client.post(base+f'/{cid}/{operation}',json=body)
            assert response.status_code == 200,response.text
            assert response.json()['review_packet']['current_materials'] == preview
            version = response.json()['version']
    reopened = CurriculumSupplyService(Database(db.path)).get(CHILD,cid)
    assert reopened['eligible'] and reopened['review_packet']['current_materials'] == preview
    with db.connect() as c:
        c.execute("UPDATE knowledge_source SET review_status='quarantined' WHERE source_id=?",(binding['source_id'],));c.commit()
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1',41901),headers={'X-HSC-Session':settings.session_token}) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock',json={'pin':'7391'}).json()['parent_token']
        assert client.get(base.replace(CHILD,'child-primary-4')+f'/{cid}').status_code == 404
        current = client.get(base+f'/{cid}').json()
        assert not current['eligible']
        blocked = current['review_packet']['current_materials']
        assert blocked['status'] == 'blocked' and blocked['materials'] == []
        assert text not in json.dumps(current,ensure_ascii=False)
        options = client.get(base+f'/sources/{source_id}/options').json()
        assert next(e for e in options['excerpts'] if e['excerpt_id']==spec['excerpt_id'])['current_materials']['materials'] == []
