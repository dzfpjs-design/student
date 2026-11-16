"""Material-purpose decisions use actual parent authentication and durable receipts."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_material_question_authorization import published_material, grant_arguments, CHILD
from test_writing_question_authorization_http import parent


def test_material_rights_http_grant_restart_and_revoked_replay(tmp_path):
    db, service, item, _, text, _ = published_material(tmp_path)
    _, args = grant_arguments(service, item)
    body = {key: value for key, value in args.items() if key not in {'actor_id', 'diagnostic_item_id'}}
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        data_encryption_key=b'M' * 32)
    url = f'/v1/parent/children/{CHILD}/writing-question-authorizations/{item}'
    with TestClient(create_app(settings)) as client:
        assert client.get(url).status_code == 401
        headers = parent(client, True)
        view = client.get(url, headers=headers)
        assert view.status_code == 200 and view.headers['cache-control'] == 'no-store'
        assert text in view.json()['preview']['question']
        spoofed = client.post(url+'/grant', headers=headers, json={**body, 'actor_id': 'system'})
        assert spoofed.status_code == 422
        question_only = {key: value for key, value in body.items() if key != 'material_license_bases'}
        refused = client.post(url+'/grant', headers=headers, json=question_only)
        assert refused.status_code == 409
        granted = client.post(url+'/grant', headers=headers, json=body)
        assert granted.status_code == 200, granted.text
        assert granted.json()['current']['status'] == 'authorized'
        receipt = granted.json()['receipt']
    with TestClient(create_app(settings)) as client:
        headers = parent(client)
        assert client.get(url, headers=headers).json()['status'] == 'authorized'
        revoked = client.post(url+'/revoke', headers=headers,
            json={'expected_revision': receipt['revision'], 'client_action_id': 'revoke-material-http'})
        assert revoked.status_code == 200 and revoked.json()['current']['status'] == 'revoked'
        retry = client.post(url+'/grant', headers=headers, json=body)
        assert retry.status_code == 200 and retry.json()['receipt'] == receipt
        assert retry.json()['current']['status'] == 'revoked'
