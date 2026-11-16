from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.parent_auth import ParentSessionRequiredError
from home_study_agent.parent_daily_visual_preview_http import create_parent_daily_visual_preview_router
from home_study_agent.repository import utc_now


def test_parent_local_preview_requires_auth_and_rereads_deleted_sources(tmp_path):
    db = Database(tmp_path / 'preview.db')
    db.initialize()
    now = utc_now()
    with db.connect() as c:
        for child in ('a', 'b'):
            c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES(?,?,'初三','江苏','active',?,?)", (child,child,now,now))
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('s','a','active','study',?,?,?)", (now,now,'session'))
        c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,confidence,details_json,status,created_at,client_action_id)
          VALUES('art','a','s','document_ocr',NULL,'1. 求 2+2',.9,'{}','completed',?,'capture')""", (now,))
        c.commit()
    monitor = DocumentMonitoringService(db)
    monitor.start('s', 'start')
    monitor.record_revision('s', 'art', 'page', 'revision')
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    class Auth:
        def require(self, token):
            if token != 'parent': raise ParentSessionRequiredError()
        def expires_at(self, token):
            from datetime import datetime, timedelta, timezone
            self.require(token)
            return datetime.now(timezone.utc) + timedelta(minutes=30)
    def local(request):
        if request.headers.get('X-HSC-Session') != 'session': raise HTTPException(401)
    app = FastAPI()
    app.include_router(create_parent_daily_visual_preview_router(db, Auth(), local))
    with TestClient(app, client=('127.0.0.1', 4321)) as client:
        url = f'/api/parent/children/a/daily-visual-preview/{day}'
        assert client.get(url).status_code == 401
        assert client.get(url, headers={'X-HSC-Session':'session'}).status_code == 401
        headers = {'X-HSC-Session':'session', 'X-HSC-Parent':'parent'}
        assert client.get(url + '?unexpected=1', headers=headers).status_code == 422
        response = client.get(url, headers=headers)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        body = response.json()
        assert body['document_observation']['pages'][0]['text'] == '1. 求 2+2'
        assert body['page_observation_time']['actual_learning_seconds'] is None
        assert body['camera_and_document_samples']['actual_learning_seconds'] is None
        assert body['cloud_transmission_allowed'] is False and body['is_formal_report'] is False
        other = client.get(url.replace('/a/', '/b/'), headers=headers).json()
        assert other['document_observation']['pages'] == []
        with db.connect() as c:
            c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,created_at,updated_at) VALUES('d','a','capture_artifact','capture_artifact','art','deleting',?,?)", (now,now))
            c.commit()
        assert client.get(url, headers=headers).status_code == 409
        with db.connect() as c:
            c.execute("DELETE FROM capture_artifact WHERE artifact_id='art'")
            c.execute("UPDATE deletion_job SET status='completed' WHERE deletion_id='d'")
            c.commit()
        assert client.get(url, headers=headers).json()['document_observation']['pages'] == []
    with TestClient(app, client=('192.0.2.1', 4321)) as remote:
        assert remote.get(url, headers={'X-HSC-Session':'session', 'X-HSC-Parent':'parent'}).status_code == 403


def test_real_app_requires_local_session_and_parent_unlock(tmp_path):
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'app.db', maintenance_enabled=False, session_token='synthetic-local-token')
    with TestClient(create_app(settings), client=('127.0.0.1', 4321)) as client:
        path = '/api/parent/children/child-primary-4/daily-visual-preview/2026-11-04'
        assert client.get(path).status_code == 401
        local = {'X-HSC-Session':settings.session_token}
        assert client.get(path, headers=local).status_code == 401
        setup = client.post('/v1/parent/setup', json={'pin':'2468'}, headers=local)
        assert setup.status_code == 200
        parent = setup.json()['parent_token']
        assert client.get(path, headers={'X-HSC-Parent':parent}).status_code == 401
        response = client.get(path, headers={**local, 'X-HSC-Parent':parent})
        assert response.status_code == 200
        assert response.json()['child_id'] == 'child-primary-4'


def test_real_app_without_local_session_token_fails_closed(tmp_path):
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'app.db', maintenance_enabled=False, session_token='')
    with TestClient(create_app(settings), client=('127.0.0.1', 4321)) as client:
        setup = client.post('/v1/parent/setup', json={'pin':'2468'})
        assert setup.status_code == 200
        path = '/api/parent/children/child-primary-4/daily-visual-preview/2026-11-04'
        assert client.get(path, headers={'X-HSC-Parent':setup.json()['parent_token']}).status_code == 401


def test_original_capture_deletion_receipt_is_child_and_action_bound(tmp_path):
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'app.db', maintenance_enabled=False, session_token='synthetic-local-token')
    with TestClient(create_app(settings), client=('127.0.0.1', 4321)) as client:
        local = {'X-HSC-Session':settings.session_token}
        token = client.post('/v1/parent/setup', json={'pin':'2468'}, headers=local).json()['parent_token']
        headers = {**local, 'X-HSC-Parent':token}
        now = utc_now()
        with client.app.state.repository.database.connect() as c:
            c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('receipt-s','child-primary-4','active','study',?,?,?)", (now,now,'receipt-s'))
            c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,confidence,details_json,status,created_at,client_action_id)
              VALUES('receipt-art','child-primary-4','receipt-s','document_ocr',NULL,'synthetic',.9,'{}','completed',?,'receipt-capture')""", (now,))
            c.commit()
        action = '18d1a57b-3f39-4e5d-a4af-c810a3950661'
        base = '/api/parent/children/child-primary-4/daily-visual-preview/deletion-receipt/receipt-art/'
        assert client.get(base+action, headers=headers).json()['verified'] is False
        result = client.request('DELETE', '/v1/parent/capture-artifacts/receipt-art', headers=headers,
                                json={'confirmation':'确认删除','client_action_id':action})
        assert result.status_code == 200 and result.json()['status'] == 'completed'
        assert client.get(base+action, headers=headers).json()['verified'] is True
        assert client.get(base.replace('child-primary-4','child-junior-3')+action, headers=headers).json()['verified'] is False
        assert client.get(base.replace('receipt-art','different-artifact')+action, headers=headers).json()['verified'] is False
        assert client.get(base+'different-action', headers=headers).json()['verified'] is False
        import json
        with client.app.state.repository.database.connect() as c:
            original = c.execute('SELECT verification_json FROM deletion_job WHERE client_action_id=?', (action,)).fetchone()[0]
            tampered = json.loads(original)
            tampered['remaining_counts']['plan_reference'] = 1
            c.execute('UPDATE deletion_job SET verification_json=? WHERE client_action_id=?', (json.dumps(tampered),action))
            c.commit()
        assert client.get(base+action, headers=headers).json()['verified'] is False
        with client.app.state.repository.database.connect() as c:
            tampered = json.loads(original); tampered['recovery_rotation']['replacement_backup_id'] = 'nonexistent'
            c.execute('UPDATE deletion_job SET verification_json=? WHERE client_action_id=?', (json.dumps(tampered),action)); c.commit()
        assert client.get(base+action, headers=headers).json()['verified'] is False
        with client.app.state.repository.database.connect() as c:
            c.execute('UPDATE deletion_job SET verification_json=? WHERE client_action_id=?', (original,action)); c.commit()
            backup = c.execute('SELECT path FROM backup_manifest WHERE backup_id=?', (json.loads(original)['recovery_rotation']['replacement_backup_id'],)).fetchone()['path']
        from pathlib import Path
        path = Path(backup); hidden = path.with_suffix('.held')
        path.rename(hidden)
        try:
            assert client.get(base+action, headers=headers).json()['verified'] is False
        finally:
            hidden.rename(path)
        assert client.get(base+action, headers=headers).json()['verified'] is True
        with path.open('ab') as file:
            file.write(b'synthetic-tampering')
        assert client.get(base+action, headers=headers).json()['verified'] is False
