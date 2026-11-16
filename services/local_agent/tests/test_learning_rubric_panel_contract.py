"""Real HTTP contract for the independent parent panel, with isolated fixtures only."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from home_study_agent.database import Database
from home_study_agent.learning_rubric_http import create_learning_rubric_router
from home_study_agent.parent_auth import ParentAuth, ParentSessionRequiredError
from test_learning_outcome_mastery_gate import gate, flow, CHILD


def client_for(db, auth):
    app = FastAPI()
    @app.exception_handler(ParentSessionRequiredError)
    async def parent_session_required(request, error):
        return JSONResponse(status_code=401, content={'code': 'PARENT_SESSION_REQUIRED'})
    app.include_router(create_learning_rubric_router(db, auth))
    client = TestClient(app)
    client.headers['X-HSC-Parent'] = auth.unlock('7391')
    return client


def test_panel_empty_dimensions_mapping_review_restart_retract(gate):
    async def forbidden(request):
        raise AssertionError('panel local mapping must not invoke a model')
    gate.gateway.complete = forbidden
    snapshot = gate.run(local=True)[0]['evaluation']['rubric_assessment']
    auth = ParentAuth(gate.db)
    auth.setup('7391')
    client = client_for(gate.db, auth)
    root = f'/v1/parent/children/{CHILD}/learning-rubrics'
    assert client.get(root, headers={'X-HSC-Parent': 'invalid'}).status_code == 401
    listed = client.get(root, params={'offset': 0, 'limit': 20}).json()
    selected = listed['items'][0]
    assert selected['assessment_id'] == snapshot['assessment_id']
    assert selected['dimensions'] == [] and selected['source_kind'] == 'local_criteria_mapping'
    path = root + '/' + selected['assessment_id']
    detail = client.get(path).json()
    body = {'expected_revision': detail['assessment']['review_revision'], 'client_action_id': 'panel-map-review',
            'operation': 'approve', 'reason': 'Reviewed actual source mapping', 'dimensions': [],
            'criterion_evidence': [{'criterion': c, 'response_quote': selected['response']} for c in selected['criteria']],
            'independent': True, 'hint_level': 0}
    receipt = client.post(path + '/review', json=body)
    assert receipt.status_code == 200, receipt.text[:500]
    assert receipt.json()['event']['dimensions'] == []
    assert receipt.json()['assessment']['review_revision'] == 1
    assert client.post(path + '/review', json=body).json()['event'] == receipt.json()['event']
    assert client.post(path + '/review', json={**body, 'client_action_id': 'stale-map'}).status_code == 409
    assert client.post(path + '/review', json={**body, 'reviewer_id': 'body-parent'}).status_code == 422
    reopened = Database(gate.db.path)
    resumed = client_for(reopened, ParentAuth(reopened))
    restored = resumed.get(path).json()
    assert restored['assessment']['review_revision'] == 1 and len(restored['history']) == 1
    response = resumed.post(path + '/review', json={'expected_revision': 1, 'client_action_id': 'panel-map-retract',
        'operation': 'retract', 'reason': 'Observed assistance later'})
    assert response.status_code == 200, response.text[:500]
    assert response.json()['assessment']['score_status'] == 'withdrawn'
    assert response.json()['qualification']['eligible_for_mastery_gate'] is False
    assert len(response.json()['history']) == 2
    old_key = resumed.post(path + '/review', json=body)
    assert old_key.status_code == 200, old_key.text[:500]
    assert old_key.json()['event'] == receipt.json()['event']
    assert old_key.json()['event']['review_revision'] == 1
    assert old_key.json()['assessment']['review_revision'] == 2
    assert old_key.json()['assessment']['score_status'] == 'withdrawn'
    assert old_key.json()['qualification']['eligible_for_mastery_gate'] is False
    assert len(old_key.json()['history']) == 2
    with reopened.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
