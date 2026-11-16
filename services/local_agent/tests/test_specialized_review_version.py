from fastapi.testclient import TestClient
import pytest
from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment
from test_specialized_closed_contract import ClosedGateway
from test_specialized_closed_http import generated_http, CHILD


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_plain_approval_rejects_unseen_version_and_preserves_original_key(environment, decision):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=ClosedGateway())) as client:
        headers, rows = generated_http(client, db)
        row = next(r for r in rows if r['stage'] == 'parallel')
        context = client.get(f"/v1/parent/children/{CHILD}/specialized-supply/candidates/{row['candidate_id']}/stage-review-context", headers=headers).json()
        assert context['candidate_expected_answer'] == row['expected_answer']
        url = f"/v1/parent/specialized-supply/candidates/{row['candidate_id']}/review"
        body = {'decision': decision, 'note': 'Human checked this complete reference',
                'client_action_id': 'version-bound-review29', 'expected_binding_version': 'old-version'}
        refused = client.post(url, headers=headers, json=body)
        assert refused.status_code == 422, refused.text
        with db.connect() as c:
            saved = c.execute('SELECT status,review_action_id FROM specialized_supply_candidate WHERE candidate_id=?', (row['candidate_id'],)).fetchone()
            assert tuple(saved) == ('needs_review', None)
        body['expected_binding_version'] = context['binding_version']
        saved = client.post(url, headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        assert client.post(url, headers=headers, json=body).json() == saved.json()
        assert client.post(url, headers=headers, json={**body, 'expected_binding_version': 'another-version'}).status_code == 422
    with TestClient(create_app(settings, model_gateway=ClosedGateway())) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '2846'}).json()['parent_token']
        rows = client.get(f'/v1/parent/children/{CHILD}/specialized-supply/candidates?status=', headers={'X-HSC-Parent': token}).json()['items']
        receipt = next(r for r in rows if r['candidate_id'] == row['candidate_id'])
        assert receipt['review_action_id'] == body['client_action_id']
        assert receipt['validation']['review_binding_version'] == body['expected_binding_version']
        assert receipt['review_note'] == body['note']


def test_source_changed_after_read_never_uses_the_stale_human_binding(environment):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=ClosedGateway())) as client:
        headers, rows = generated_http(client, db)
        row = next(r for r in rows if r['stage'] == 'parallel')
        path = f"/v1/parent/children/{CHILD}/specialized-supply/candidates/{row['candidate_id']}/stage-review-context"
        context = client.get(path, headers=headers).json()
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET explanation=explanation || ' Revised original source.' WHERE diagnostic_item_id=?", (row['seed_item_id'],))
            c.commit()
        response = client.post(f"/v1/parent/specialized-supply/candidates/{row['candidate_id']}/review", headers=headers,
            json={'decision': 'approve', 'note': 'Human read the previous reference', 'client_action_id': 'after-source-revision29',
                  'expected_binding_version': context['binding_version']})
        assert response.status_code == 422, response.text
        with db.connect() as c:
            saved = c.execute('SELECT status,review_action_id,diagnostic_item_id FROM specialized_supply_candidate WHERE candidate_id=?', (row['candidate_id'],)).fetchone()
            assert tuple(saved) == ('needs_review', None, None)
