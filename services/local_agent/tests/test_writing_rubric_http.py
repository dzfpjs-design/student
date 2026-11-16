"""Actual app parent route; synthetic native text, no real media or cloud."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_pet_reviewed_source_actual_http import NoCloud
from test_writing_response_origin import writing, CHILD
from test_writing_rubric import prepared


def test_actual_parent_writing_review_binding_auth_restart_and_source_recheck(writing):
    db = writing[0]
    _, snapshot, body = prepared(writing)
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='writing-isolated')
    local = {'X-HSC-Session':settings.session_token}
    path = f"/v1/parent/children/{CHILD}/learning-rubrics/{snapshot['assessment_id']}"
    payload = {k:v for k,v in body.items() if k not in {'reviewer_id','reviewer_role'}}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'}).json()['parent_token']
        headers = {**local,'X-HSC-Parent':token}
        assert client.get(path, headers=local).status_code == 401
        detail = client.get(path, headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()['writing_review']['binding']['binding_version'] == payload['writing_evidence']['binding_version']
        assert client.post(path+'/review', headers=headers, json={**payload,'reviewer_role':'teacher'}).status_code == 422
        denied = client.post(path+'/review', headers=headers, json={**payload,'writing_evidence':{**payload['writing_evidence'],'binding_version':'0'*64}})
        assert denied.status_code == 409
        result = client.post(path+'/review', headers=headers, json=payload)
        assert result.status_code == 200, result.text
        assert result.json()['qualification']['eligible_for_mastery_gate']
        assert client.post(path+'/review', headers=headers, json=payload).json()['event'] == result.json()['event']
        assert client.get(path.replace(CHILD,'child-junior-3'), headers=headers).status_code in {404,409}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock', headers=local, json={'pin':'2468'}).json()['parent_token']
        headers = {**local,'X-HSC-Parent':token}
        detail = client.get(path, headers=headers).json()
        assert detail['qualification']['eligible_for_mastery_gate']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
            c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_type='writing_original'")
            c.commit()
        changed = client.get(path, headers=headers).json()
        assert not changed['qualification']['eligible_for_mastery_gate']
        assert changed['writing_review']['binding'] is None
        assert changed['writing_review']['blockers']
