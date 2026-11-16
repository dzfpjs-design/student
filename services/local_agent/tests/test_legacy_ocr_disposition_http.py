import sqlite3

from fastapi.testclient import TestClient
from test_ocr_cache_host import app_at


def test_actual_parent_disposition_authorization_snapshot_and_restart(tmp_path):
    path = tmp_path / 'local-ocr.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE ocr_job(job_id TEXT PRIMARY KEY,request_id TEXT,image_hash TEXT,result_json TEXT)')
        c.execute('CREATE TABLE ocr_cache(image_hash TEXT PRIMARY KEY,result_json TEXT)')
        c.execute("INSERT INTO ocr_job VALUES('orphan','old-request','hash','private fixture body')")
        c.execute("INSERT INTO ocr_cache VALUES('hash','private fixture body')")
    prefix = '/v1/parent/privacy/legacy-ocr'
    with TestClient(app_at(tmp_path)) as client:
        assert client.post(prefix + '/preview').status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '6084'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        preview = client.post(prefix + '/preview', headers=headers)
        assert preview.status_code == 200, preview.text
        assert preview.headers['cache-control'] == 'no-store'
        assert 'private fixture body' not in preview.text and str(path) not in preview.text
        scope = preview.json()
        assert scope['counts']['unattributed_jobs'] == scope['counts']['unattributed_cache'] == 1
        payload = dict(preview_id=scope['preview_id'], expected_fingerprint=scope['snapshot_fingerprint'],
            client_action_id='explicit-disposition-http', confirmation=scope['confirmation_text'])
        assert client.post(prefix + '/dispose', json=payload).status_code == 401
        assert client.post(prefix + '/dispose', headers=headers, json={**payload, 'reviewer_id': 'forged'}).status_code == 422
        assert client.post(prefix + '/dispose', headers=headers, json={**payload, 'expected_fingerprint': 'f'*64}).status_code == 409
        result = client.post(prefix + '/dispose', headers=headers, json=payload)
        assert result.status_code == 200 and result.json()['status'] == 'completed', result.text
        receipt = result.json()
        assert client.post(prefix + '/dispose', headers=headers, json=payload).json() == receipt
        assert client.get(prefix + '/receipts/missing', headers=headers).status_code == 404
        with client.app.state.repository.database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM child_profile').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM ocr_legacy_disposition_receipt').fetchone()[0] == 1
            assert 'household-parent' in c.execute('SELECT request_json FROM ocr_legacy_disposition_receipt').fetchone()[0]
    with TestClient(app_at(tmp_path)) as client:
        assert client.get(prefix + '/receipts/' + receipt['receipt_id']).status_code == 401
        token = client.post('/v1/parent/unlock', json={'pin': '6084'}).json()['parent_token']
        result = client.get(prefix + '/receipts/' + receipt['receipt_id'], headers={'X-HSC-Parent': token})
        assert result.status_code == 200 and result.json() == receipt, result.text
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT COUNT(*) FROM ocr_job').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM ocr_cache').fetchone()[0] == 0
