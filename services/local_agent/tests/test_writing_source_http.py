"""Existing actual author/review/publish HTTP, not a separate supply endpoint."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_pet_reviewed_source_actual_http import NoCloud
from test_writing_source_publication import prepared, CHILD


def test_actual_host_writing_options_publication_exact_schema_and_restart(tmp_path):
    db, _, spec, original = prepared(tmp_path)
    settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,session_token='writing-supply-host')
    base = f'/api/parent/children/{CHILD}/curriculum-supply'
    local = {'X-HSC-Session':settings.session_token}
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1',41220), headers=local, raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup',json={'pin':'7391'}).json()['parent_token']
        body = {'action_id':'draft','expected_version':0,'expected_scope_version':1,'source':spec}
        assert client.post(base+'/generate',json=body).status_code == 401
        client.headers['X-HSC-Parent'] = token
        source = client.get(base+'/options').json()['sources'][0]
        options = client.get(base+f"/sources/{source['source_id']}/options")
        assert options.status_code == 200, options.text
        assert any(s['seed_item_id']==spec['seed_item_id'] and s['validator']==spec['validator'] for s in options.json()['seeds'])
        response = client.post(base+'/generate',json=body)
        assert response.status_code == 200, response.text
        draft = response.json()
        cid = draft['candidate_id']
        assert draft['payload']['pet_writing'] == original
        assert client.post(base+'/generate',json=body).json()['candidate_id'] == cid
        for version, operation in enumerate(('validate','review','publish'),1):
            payload = {'action_id':operation,'expected_version':version}
            if operation == 'review': payload.update(decision='approve',note='Checked original task and subjective writing criteria')
            response = client.post(base+f'/{cid}/{operation}',json=payload)
            assert response.status_code == 200, response.text
        assert response.json()['eligible']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1',41221), headers=local, raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock',json={'pin':'7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        result = client.get(base+f'/{cid}')
        assert result.status_code == 200 and result.json()['eligible'], result.text
        assert result.json()['review_packet']['pet_writing'] == original
        response = client.post(base+f'/{cid}/withdraw',json={'action_id':'withdraw','expected_version':4,'note':'Source withdrawn'})
        assert response.status_code == 200 and not response.json()['eligible'], response.text
