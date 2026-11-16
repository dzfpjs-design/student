"""Deployed host must expose true qualification state, not a demo certificate."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_companion_activity_host import NoCloud
from test_rubric_calibration import scorer, scope


def test_host_initializes_and_reads_unqualified_protocol_after_restart(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'host.db', maintenance_enabled=False)
    root = '/v1/parent/children/child-primary-4/rubric-calibration'
    app = create_app(settings, model_gateway=NoCloud())
    with TestClient(app, client=('127.0.0.1', 41000), raise_server_exceptions=False) as client:
        assert client.get(root).status_code == 401
        parent = client.post('/v1/parent/setup', json={'pin': '7352'})
        assert parent.status_code == 200, parent.text
        token = parent.json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        empty = client.get(root)
        assert empty.status_code == 200, empty.text
        assert empty.json()['items'] == []
        assert not empty.json()['requirements']['authority_resolver_configured']
        assert not empty.json()['requirements']['production_authority_available']
        assert empty.headers['Cache-Control'] == 'no-store'
        assert client.post(root, json={'score_qualified': True, 'model_revision': 'caller-invented'}).status_code == 405
        service = app.state.rubric_calibration
        frozen = service.freeze('child-primary-4', scorer=scorer(), scope=scope(),
            holdout_receipt_id='missing-holdout', client_action_id='explicit-local-insufficient-record')
        pid = frozen['current']['protocol_id']
        detail = client.get(root + '/' + pid)
        assert detail.status_code == 200, detail.text
        assert detail.json()['status'] == 'insufficient'
        assert not detail.json()['score_qualified']
        assert client.get(root.replace('child-primary-4', 'child-junior-3')+'/'+pid).status_code == 404
        assert client.get(root+'?score_qualified=true').status_code == 422
    app = create_app(settings, model_gateway=NoCloud())
    with TestClient(app, client=('127.0.0.1', 41000), raise_server_exceptions=False) as client:
        client.headers['X-HSC-Parent'] = token
        assert client.get(root).status_code == 401
        token = client.post('/v1/parent/unlock', json={'pin': '7352'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        current = client.get(root).json()
        assert len(current['items']) == 1 and current['items'][0]['protocol_id'] == pid
        assert current['items'][0]['status'] == 'insufficient'
        assert current['local_only'] and not current['cloud_transmission_allowed']
