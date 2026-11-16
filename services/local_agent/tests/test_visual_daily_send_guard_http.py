"""The real parent endpoint remains closed until the two reports are combined."""
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings


def test_send_is_closed_with_real_parent_and_local_session(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'app.db',
                        maintenance_enabled=False, session_token='synthetic-local-token')
    with TestClient(create_app(settings), client=('127.0.0.1',4321)) as client:
        local = {'X-HSC-Session':settings.session_token}
        token = client.post('/v1/parent/setup',headers=local,json={'pin':'2468'}).json()['parent_token']
        path = '/api/parent/children/child-primary-4/daily-visual-preview/2026-11-04/send'
        payload = {'recipient_id':'synthetic-recipient','source_id':'synthetic-source'}
        assert client.post(path,json=payload,headers=local).status_code == 401
        assert client.post(path,json=payload,headers={'X-HSC-Parent':token}).status_code == 401
        response = client.post(path,json=payload,headers={**local,'X-HSC-Parent':token})
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'COMBINED_DAILY_REPORT_NOT_READY'
        with client.app.state.repository.database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM daily_observation_delivery_ledger').fetchone()[0] == 0
