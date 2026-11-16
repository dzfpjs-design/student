import base64
import sqlite3

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_local_ocr_pipeline import FakeEngine, image_bytes


def app_at(path):
    return create_app(Settings(data_dir=path, database_path=path / 'family.db',
        data_encryption_key=b'O' * 32, maintenance_enabled=False))


def test_actual_http_ocr_restart_and_privacy_delete_never_replays_removed_body(tmp_path):
    payload = {'image_base64': base64.b64encode(image_bytes()).decode(), 'client_action_id': 'http-page'}
    with TestClient(app_at(tmp_path)) as client:
        client.app.state.ocr_workflow.adapter.ocr.engines = [FakeEngine()]
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'start-http'}).json()['session_id']
        absent = client.post('/v1/sessions/absent/ocr', json={**payload, 'client_action_id': 'invalid-session-request'})
        assert absent.status_code == 404
        first = client.post(f'/v1/sessions/{sid}/ocr', json=payload)
        assert first.status_code == 200, first.text
        artifact_id = first.json()['artifact_id']
        assert client.post(f'/v1/sessions/{sid}/ocr', json=payload).json()['artifact_id'] == artifact_id
        changed = client.post(f'/v1/sessions/{sid}/ocr', json={**payload, 'image_base64': base64.b64encode(image_bytes(width=460)).decode()})
        assert changed.status_code in (409, 422)
    with TestClient(app_at(tmp_path)) as client:
        replay = client.post(f'/v1/sessions/{sid}/ocr', json=payload)
        assert replay.status_code == 200 and replay.json()['artifact_id'] == artifact_id
        token = client.post('/v1/parent/setup', json={'pin': '6084'}).json()['parent_token']
        deleted = client.request('DELETE', f'/v1/parent/capture-artifacts/{artifact_id}',
            headers={'X-HSC-Parent': token}, json={'confirmation': '确认删除', 'client_action_id': 'delete-http'})
        assert deleted.status_code == 200 and deleted.json()['status'] == 'completed', deleted.text
        refused = client.post(f'/v1/sessions/{sid}/ocr', json=payload)
        assert refused.status_code in (409, 422)
        with client.app.state.repository.database.connect() as c:
            assert c.execute('SELECT count(*) FROM ocr_job').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM ocr_cache').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM capture_artifact').fetchone()[0] == 0


def test_actual_privacy_unknown_legacy_body_cannot_claim_complete_and_is_retryable(tmp_path):
    with sqlite3.connect(tmp_path / 'local-ocr.db') as c:
        c.execute('CREATE TABLE ocr_job(job_id TEXT PRIMARY KEY,request_id TEXT,image_hash TEXT,result_json TEXT)')
        c.execute('CREATE TABLE ocr_cache(image_hash TEXT PRIMARY KEY,result_json TEXT)')
        c.execute("INSERT INTO ocr_job VALUES('orphan','old-request','old-hash','unowned synthetic text')")
        c.execute("INSERT INTO ocr_cache VALUES('old-hash','unowned synthetic text')")
    with TestClient(app_at(tmp_path)) as client:
        assert client.app.state.legacy_ocr_residuals['legacy_ocr_unattributed_jobs'] == 1
        token = client.post('/v1/parent/setup', json={'pin': '6084'}).json()['parent_token']
        response = client.post('/v1/parent/children/child-primary-4/delete', headers={'X-HSC-Parent': token},
            json={'scope': 'media', 'confirmation': '确认删除', 'client_action_id': 'legacy-delete'})
        assert response.status_code >= 400
        with client.app.state.repository.database.connect() as c:
            job = dict(c.execute("SELECT * FROM deletion_job WHERE client_action_id='legacy-delete'").fetchone())
            assert job['status'] != 'completed' and job['error_code'] == 'OCR_LEGACY_OWNER_UNKNOWN'
        retry = client.post(f"/v1/parent/deletions/{job['deletion_id']}/retry", headers={'X-HSC-Parent': token},
            json={'confirmation': '确认删除'})
        assert retry.status_code >= 400
    with sqlite3.connect(tmp_path / 'local-ocr.db') as c:
        assert c.execute('SELECT result_json FROM ocr_cache').fetchone()[0] == 'unowned synthetic text'
