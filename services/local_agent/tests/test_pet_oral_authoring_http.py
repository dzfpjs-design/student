"""Actual local parent descriptor/form import; synthetic text, no model/media."""
import json
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.local_supply import validate_item
from test_pet_oral_supply_http import setup, post, BASE, CHILD, OTHER
from test_pet_reviewed_source_actual_http import NoCloud


def test_parent_form_descriptors_and_exact_import_are_owned_local_and_versioned(tmp_path):
    db, settings = setup(tmp_path)
    with TestClient(create_app(settings, model_gateway=NoCloud()), headers={'X-HSC-Session': settings.session_token},
                    client=('127.0.0.1', 42001)) as c:
        assert c.get(BASE + '/oral-authoring').status_code == 401
        c.headers['X-HSC-Parent'] = post(c, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        r = c.get(BASE + '/oral-authoring')
        assert r.status_code == 200, r.text
        v = r.json()
        assert v['child_id'] == CHILD and v['local_only'] and not v['cloud_transmission_allowed']
        assert len(v['options']) == 7
        assert c.get(BASE.replace(CHILD, 'missing-child') + '/oral-authoring').status_code == 404
        assert c.get(BASE.replace(CHILD, OTHER) + '/oral-authoring').json()['options'] == []
        for o in v['options']:
            with db.connect() as conn:
                item = validate_item(conn, CHILD, o['seed_item_id'])
                assert item['knowledge_node_id'] == o['knowledge_node_id'] and item['answer_type'] == o['answer_type']
            source = dict(schema='pet_speaking_source_v1', subject='英语', grade=v['grade'], knowledge_node_id=o['knowledge_node_id'],
                answer_type=o['answer_type'], assessment_mode='pet_reference_rubric', task_family=o['task_family'],
                prompt='A new garden task: describe what happens and give your reason.', task_points=['Describe the garden', 'Give one reason'],
                stimulus_text='Ada watered the garden before the class arrived.', stimulus_binding=o['stimulus_binding'])
            body = dict(source_type='school_material', title='Parent form ' + o['task_family'], subject='英语', grade_band=v['grade'],
                version_label='Form v1', source_ref='isolated-form:' + o['task_family'], license_status='family_owned_index',
                parent_reviewed=True, content_text=json.dumps(source, ensure_ascii=False))
            saved = post(c, f'/v1/parent/children/{CHILD}/knowledge-sources', body)
            assert post(c, f'/v1/parent/children/{CHILD}/knowledge-sources', body)['source_id'] == saved['source_id']
            detail = c.get(BASE + f"/sources/{saved['source_id']}/options").json()
            assert json.loads(detail['excerpts'][0]['text']) == source and detail['seeds']
        with db.connect() as conn:
            conn.execute("UPDATE diagnostic_item SET stimulus_asset='arbitrary-file' WHERE diagnostic_item_id='diag-pet-speaking-1'")
            conn.commit()
        assert 'picture_description' not in {o['task_family'] for o in c.get(BASE + '/oral-authoring').json()['options']}
