from uuid import uuid4

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_parent_coaching_feedback_semantics import CHILD, report, repository


def test_parent_action_feedback_auth_version_restart_and_retraction(repository, tmp_path):
    saved = report(repository)
    settings = Settings(data_dir=tmp_path, database_path=repository.database.path, maintenance_enabled=False)
    path = f"/v1/parent/reports/{saved['report_id']}"
    snapshot = saved['parent_action']
    payload = {'action_snapshot_id': snapshot['snapshot_id'], 'client_action_id': str(uuid4()),
               'expected_revision': 0, 'executed': True, 'executed_at': snapshot['created_at'],
               'effectiveness': 3, 'stress_rating': None, 'notes_summary': 'We tried the action'}
    with TestClient(create_app(settings)) as client:
        assert client.get(path + '/actions').status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        assert client.get(path + '/actions', headers=headers).json()['items'][0]['snapshot_id'] == snapshot['snapshot_id']
        saved_response = client.put(path + '/feedback', json=payload, headers=headers)
        assert saved_response.status_code == 200, saved_response.text
        assert client.put(path + '/feedback', json=payload, headers=headers).json() == saved_response.json()
        assert client.put(path + '/feedback', json={**payload, 'client_action_id': str(uuid4())}, headers=headers).status_code == 409
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        actions = client.get(path + '/actions', headers=headers).json()['items']
        assert actions[0]['feedback']['client_action_id'] == payload['client_action_id']
        assert actions[0]['feedback']['executed_at'] == payload['executed_at']
        retracted = client.put(path + '/feedback', headers=headers, json={
            'action_snapshot_id': snapshot['snapshot_id'], 'client_action_id': str(uuid4()),
            'expected_revision': 1, 'operation': 'retract', 'reason': 'This was not the action we tried',
        })
        assert retracted.status_code == 200, retracted.text
        assert retracted.json()['revision'] == 2
        assert retracted.json()['operation'] == 'retract'
        invalid = client.put(path + '/feedback', headers=headers, json={
            **payload, 'client_action_id': str(uuid4()), 'expected_revision': 2,
            'executed': False, 'effectiveness': 5,
        })
        assert invalid.status_code == 422, invalid.text
