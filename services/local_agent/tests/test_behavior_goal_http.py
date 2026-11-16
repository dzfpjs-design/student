"""Real host, parent authentication and durable goal feedback; synthetic history."""
import hashlib
import json

from fastapi.testclient import TestClient
from fastapi import FastAPI

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.behavior_goal_http import create_behavior_goal_router
from home_study_agent.parent_auth import ParentAuth
from test_behavior_goal_confirmation import CHILD, history
from test_behavior_observation_coverage import managed
from test_companion_activity_host import NoCloud


ROOT = f'/v1/parent/children/{CHILD}/behavior/habit-experiments/goal'


def host(db, tmp_path):
    return TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path,
        maintenance_enabled=False), model_gateway=NoCloud()),
        client=('127.0.0.1', 43000), raise_server_exceptions=False)


def test_goal_feedback_real_host_auth_replay_restart_retract_and_isolation(managed, tmp_path):
    db, service = history(managed)
    with host(db, tmp_path) as client:
        assert client.get(ROOT + '/opportunities').status_code == 401
        parent = client.post('/v1/parent/setup', json={'pin': '8362'})
        assert parent.status_code == 200, parent.text
        token = parent.json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        view = client.get(ROOT + '/opportunities')
        assert view.status_code == 200, view.text
        assert view.headers['Cache-Control'] == 'no-store'
        assert len(view.json()['opportunities']) == 12
        item = view.json()['opportunities'][-1]
        body = dict(opportunity_id=item['opportunity_id'], expected_version=item['binding_version'],
            opportunity_present=True, criteria={k: True for k in item['criteria']},
            initiated_by='child', assistance_level='none', client_action_id='parent-goal-feedback')
        for spoof in ({'actor_id': 'child'}, {'observer': 'parent'}, {'at': '2026-08-28T12:00:00Z'},
                      {'criteria': {k: 1 for k in item['criteria']}}):
            assert client.post(ROOT + '/confirmation', json={**body, **spoof}).status_code == 422
        assert client.get(ROOT + '/opportunities?child_id=another').status_code == 422
        other = ROOT.replace(CHILD, 'child-junior-3')
        assert client.get(other + '/opportunities').status_code == 404
        assert client.post(other + '/confirmation', json=body).status_code == 404
        recorded = client.post(ROOT + '/confirmation', json=body)
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()['local_only'] and not recorded.json()['cloud_transmission_allowed']
        eid = recorded.json()['event_id']
        assert client.post(ROOT + '/confirmation', json=body).json()['event_id'] == eid
        assert client.post(ROOT + '/confirmation', json={**body, 'client_action_id': 'stale'}).status_code == 409
        with db.connect() as c:
            payload = json.loads(c.execute('SELECT payload_json FROM habit_experiment_event WHERE event_id=?', (eid,)).fetchone()[0])
        assert payload['actor_type'] == 'parent' and payload['actor_id'] == 'household_parent'
        assert payload['authorization_ref'] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in json.dumps(payload)
    with host(db, tmp_path) as client:
        client.headers['X-HSC-Parent'] = token
        assert client.get(ROOT + '/opportunities').status_code == 401
        unlocked = client.post('/v1/parent/unlock', json={'pin': '8362'})
        assert unlocked.status_code == 200, unlocked.text
        client.headers['X-HSC-Parent'] = unlocked.json()['parent_token']
        view = client.get(ROOT + '/opportunities').json()
        current = next(i for i in view['opportunities'] if i['opportunity_id'] == body['opportunity_id'])
        assert current['confirmation']['event_id'] == eid
        assert client.post(ROOT + '/confirmation', json=body).json()['event_id'] == eid
        retract = client.post(ROOT + '/confirmation', json={**body,
            'expected_version': current['binding_version'], 'operation': 'retract',
            'client_action_id': 'parent-goal-retract', 'reason': 'The original observation was ambiguous.'})
        assert retract.status_code == 200, retract.text
        items = client.get(ROOT + '/opportunities').json()['opportunities']
        assert next(i for i in items if i['opportunity_id'] == body['opportunity_id'])['confirmation'] is None
        assert client.post(ROOT + '/confirmation', json=body).json()['historical_receipt']
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type IN ('goal_behavior_confirmed','goal_behavior_retracted')").fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_goal_feedback_rejects_network_peer_and_false_proxy_headers(managed):
    db, service = history(managed)
    auth = ParentAuth(db)
    token = auth.setup('8362')
    app = FastAPI()
    app.include_router(create_behavior_goal_router(service, auth))
    with TestClient(app, client=('192.0.2.10', 43000)) as client:
        response = client.get(ROOT + '/opportunities', headers={
            'X-HSC-Parent': token, 'X-Forwarded-For': '127.0.0.1'})
        assert response.status_code == 403
        assert response.json()['detail']['code'] == 'BEHAVIOR_GOAL_LOCAL_ONLY'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type='goal_behavior_confirmed'").fetchone()[0] == 0
