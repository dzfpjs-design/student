import json

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_goal_roadmap import flow, CHILD, OTHER, TRACK


def durable_rows(db):
    with db.connect() as c:
        return {table: [tuple(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('goal_program', 'goal_cycle', 'review_task', 'diagnostic_attempt',
                              'learning_evidence', 'agent_objective', 'agent_commitment')}


def test_parent_roadmap_get_does_not_prepare_tasks_and_survives_restart(flow, tmp_path):
    db, _, _ = flow
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    path = f'/v1/parent/children/{CHILD}/goal-programs/{TRACK}/roadmap'
    with TestClient(create_app(settings)) as client:
        assert client.get(path).status_code in {401, 403}
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        before = durable_rows(db)
        first = client.get(path)
        assert first.status_code == 200, first.text
        assert first.headers['cache-control'] == 'no-store'
        body = first.json()
        assert body['child_id'] == CHILD
        assert body['cloud_transmission_allowed'] is False
        assert body['goal_attainment'] == 'not_established'
        assert body['baseline'] and body['prerequisite_path']
        assert client.get(path).json() == body
        assert durable_rows(db) == before
        assert client.get(path.replace(CHILD, OTHER)).status_code == 404
    with TestClient(create_app(settings)) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin': '7391'}).json()['parent_token']
        after = client.get(path)
        assert after.status_code == 200, after.text
        assert after.json()['target_date'] == body['target_date']
        assert after.json()['goal_attainment'] == 'not_established'
        assert durable_rows(db) == before
