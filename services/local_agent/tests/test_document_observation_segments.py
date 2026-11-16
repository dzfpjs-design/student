from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3

import pytest

from home_study_agent.document_observation_segments import DocumentObservationSegments, SEGMENT_SCHEMA_SQL
from home_study_agent.document_observation_segments_http import create_document_observation_router


class SyntheticDatabase:
    def __init__(self, path): self.path = path
    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path); c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try: yield c
        finally: c.close()


def setup(tmp_path):
    db = SyntheticDatabase(tmp_path / 'synthetic.db')
    with db.connect() as c:
        c.executescript('''CREATE TABLE child_profile(child_id TEXT PRIMARY KEY);
          CREATE TABLE daily_session(session_id TEXT PRIMARY KEY,child_id TEXT,status TEXT,state TEXT,ended_at TEXT);
          CREATE TABLE document_monitor_session(monitor_id TEXT PRIMARY KEY,session_id TEXT,child_id TEXT,generation INTEGER,status TEXT,generation_started_at TEXT);
          CREATE TABLE document_page_revision(revision_id TEXT PRIMARY KEY,monitor_id TEXT,child_id TEXT,session_id TEXT,revision_index INTEGER,created_at TEXT);
          CREATE TABLE sensor_lease(lease_id TEXT PRIMARY KEY,session_id TEXT,device_type TEXT,status TEXT,opened_at TEXT,expires_at TEXT,released_at TEXT);
          CREATE TABLE deletion_job(child_id TEXT,status TEXT);
          INSERT INTO child_profile VALUES('a'),('b');
          INSERT INTO daily_session VALUES('s','a','active','study',NULL);
          INSERT INTO document_monitor_session VALUES('m','s','a',1,'active','2026-11-04T00:00:00Z');
          INSERT INTO document_page_revision VALUES('r','m','a','s',1,'2026-11-04T00:00:00Z');
          INSERT INTO sensor_lease VALUES('l','s','document_camera','active','2026-11-04T00:00:00Z','2026-11-04T00:01:00Z',NULL);''')
    with db.connect() as c:
        c.execute(SEGMENT_SCHEMA_SQL); c.commit()
    clock = lambda: datetime(2026,11,4,0,0,15,tzinfo=timezone.utc)
    service = DocumentObservationSegments(db, clock=clock)
    data = dict(child_id='a',revision_id='r',lease_id='l',epoch=service.epoch,generation=1,seq=0,
                segment_id='seg0',started_at='2026-11-04T00:00:01Z',ended_at='2026-11-04T00:00:11Z',visible_ms=9000)
    return db, service, data, clock


def test_record_replay_conflict_and_restart(tmp_path):
    db, service, data, clock = setup(tmp_path)
    assert service.record('s', **data)['replayed'] is False
    assert service.record('s', **data)['replayed'] is True
    with pytest.raises(ValueError, match='ID_CONFLICT'):
        service.record('s', **{**data,'visible_ms':8000})
    with pytest.raises(ValueError, match='EPOCH_STALE'):
        DocumentObservationSegments(db, clock=clock).record('s', **data)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_observation_segment').fetchone()[0] == 1


@pytest.mark.parametrize('changes,reason', [
    ({'child_id':'b'}, 'MONITOR_UNAVAILABLE'), ({'generation':2}, 'MONITOR_UNAVAILABLE'),
    ({'lease_id':'bad'}, 'LEASE_UNAVAILABLE'), ({'revision_id':'bad'}, 'REVISION_UNAVAILABLE'),
    ({'seq':1}, 'SEQUENCE_CONFLICT'), ({'visible_ms':11000}, 'TIME_INVALID'),
    ({'started_at':'2026-11-04T00:00:01'}, 'TIME_INVALID'),
])
def test_reject_invalid_claims(tmp_path, changes, reason):
    _, service, data, _ = setup(tmp_path)
    with pytest.raises(ValueError, match=reason): service.record('s', **{**data, **changes})


def test_pause_sequence_overlap_and_source_deletion(tmp_path):
    db, service, data, _ = setup(tmp_path)
    service.record('s', **data)
    with pytest.raises(ValueError, match='OVERLAP'):
        service.record('s', **{**data,'segment_id':'seg1','seq':1})
    with pytest.raises(ValueError, match='OVERLAP'):
        restarted = DocumentObservationSegments(db, clock=lambda: datetime(2026,11,4,0,0,15,tzinfo=timezone.utc))
        restarted.record('s', **{**data, 'epoch':restarted.epoch, 'segment_id':'replayed-new-epoch'})
    with db.connect() as c:
        c.execute("UPDATE document_monitor_session SET status='paused',generation=2 WHERE monitor_id='m'"); c.commit()
    with pytest.raises(ValueError, match='MONITOR_UNAVAILABLE'):
        service.record('s', **data)
    with db.connect() as c:
        c.execute("DELETE FROM document_page_revision WHERE revision_id='r'"); c.commit()
        assert c.execute('SELECT count(*) FROM document_observation_segment').fetchone()[0] == 0


def test_reject_pre_resume_timestamp_and_pending_deletion(tmp_path):
    db, service, data, _ = setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE document_monitor_session SET generation_started_at='2026-11-04T00:00:05Z'"); c.commit()
    with pytest.raises(ValueError, match='BEFORE_MONITOR_UPDATE'):
        service.record('s', **data)
    with db.connect() as c:
        c.execute("INSERT INTO deletion_job VALUES('a','deleting')"); c.commit()
    with pytest.raises(ValueError, match='DELETION_PENDING'):
        service.record('s', **data)


def test_local_authenticated_http_contract(tmp_path):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    _, service, data, _ = setup(tmp_path)
    def auth(request):
        if request.headers.get('X-HSC-Session') != 'test-token': raise HTTPException(401)
    app = FastAPI()
    app.include_router(create_document_observation_router(service, local_session_dependency=auth))
    client = TestClient(app, client=('127.0.0.1', 1234))
    path = '/v1/sessions/s/document-observation-segments'
    assert client.get(path + '/epoch').status_code == 401
    assert client.get(path + '/epoch', headers={'X-HSC-Session':'test-token'}).json()['epoch'] == service.epoch
    assert client.post(path, headers={'X-HSC-Session':'test-token'}, json=data).json()['recorded'] is True
    nonlocal_client = TestClient(app, client=('192.0.2.1', 1234))
    assert nonlocal_client.get(path + '/epoch', headers={'X-HSC-Session':'test-token'}).status_code == 403


def test_initialization_is_lazy_but_record_requires_schema_without_writing(tmp_path):
    db, _, data, clock = setup(tmp_path)
    with db.connect() as c:
        c.execute('DROP TABLE document_observation_segment'); c.commit()
    service = DocumentObservationSegments(db, clock=clock)
    with pytest.raises(ValueError, match='SCHEMA_UNAVAILABLE'):
        service.record('s', **{**data, 'epoch':service.epoch})
    with db.connect() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='document_observation_segment'").fetchone() is None


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_real_privacy_export_and_scope_deletion(tmp_path, scope):
    import json
    from test_document_monitoring import _service, _artifact
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.repository import utc_now
    db, monitor = _service(tmp_path)
    monitor.start('study', 'start-observation-test')
    _artifact(db, 'art', 'synthetic question')
    revision = monitor.record_revision('study', 'art', 'synthetic', 'revision-observation-test')
    now = utc_now()
    with db.connect() as c:
        c.execute(SEGMENT_SCHEMA_SQL)
        c.execute("INSERT INTO device_profile(device_id,device_type,system_id_hash,capabilities_json,status) VALUES('d','document_camera','synthetic','{}','active')")
        c.execute("INSERT INTO sensor_lease(lease_id,session_id,device_id,device_type,status,opened_at,last_renewed_at,expires_at) VALUES('l','study','d','document_camera','released',?,?,?)", (now,now,now))
        monitor_id = c.execute("SELECT monitor_id FROM document_monitor_session WHERE session_id='study'").fetchone()[0]
        c.execute('INSERT INTO document_observation_segment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('seg','study','child',monitor_id,revision['revision_id'],'l','e',1,0,now,now,1,'h',now))
        c.commit()
    privacy = PrivacyLifecycleService(db, tmp_path); privacy.initialize()
    exported = privacy.export_child('child')
    with open(exported['path'], encoding='utf-8') as f: content = json.load(f)
    assert len(content['document_observation_segments']) == 1
    assert privacy.delete_child_scope('child', scope, '确认删除', 'test-delete-' + scope)['status'] == 'completed'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_observation_segment').fetchone()[0] == 0
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_v92_upgrade_creates_preupgrade_backup_and_preserves_data(tmp_path):
    from home_study_agent.database import Database
    db = Database(tmp_path / 'migration.db'); db.initialize()
    with db.connect() as c:
        c.execute('DROP TABLE document_observation_segment')
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DROP TABLE daily_observation_report_outbox')
        c.execute('DROP TABLE daily_observation_report_source')
        c.execute('DELETE FROM schema_migration WHERE version > 92')
        c.execute("INSERT INTO child_profile VALUES('migration-child','synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        c.commit()
    Database(db.path).initialize()
    with db.connect() as c:
        assert c.execute('SELECT max(version) FROM schema_migration').fetchone()[0] == 95
        assert c.execute("SELECT 1 FROM child_profile WHERE child_id='migration-child'").fetchone()
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='document_observation_segment'").fetchone()
    status = db.latest_upgrade_status()
    assert status['status'] == 'completed' and status['source_schema_version'] == 92 and status['target_schema_version'] == 95
    assert status['snapshot_available'] is True


def test_failed_v92_upgrade_restores_preupgrade_database(tmp_path, monkeypatch):
    from home_study_agent.database import Database
    db = Database(tmp_path / 'migration.db'); db.initialize()
    with db.connect() as c:
        c.execute('DROP TABLE document_observation_segment')
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DROP TABLE daily_observation_report_outbox')
        c.execute('DROP TABLE daily_observation_report_source')
        c.execute('DELETE FROM schema_migration WHERE version > 92')
        c.execute("INSERT INTO child_profile VALUES('migration-child','synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        c.commit()
    original = Database._initialize_schema
    def fail_after_schema(self):
        original(self)
        raise RuntimeError('SYNTHETIC_MIGRATION_FAILURE')
    monkeypatch.setattr(Database, '_initialize_schema', fail_after_schema)
    with pytest.raises(RuntimeError, match='SYNTHETIC_MIGRATION_FAILURE'):
        Database(db.path).initialize()
    with db.connect() as c:
        assert c.execute('SELECT max(version) FROM schema_migration').fetchone()[0] == 92
        assert c.execute("SELECT 1 FROM child_profile WHERE child_id='migration-child'").fetchone()
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='document_observation_segment'").fetchone() is None


def test_existing_v92_monitor_generation_is_unknown_until_restart(tmp_path):
    from test_document_monitoring import _service
    from home_study_agent.database import Database
    db, monitor = _service(tmp_path)
    monitor.start('study', 'start-before-upgrade')
    with db.connect() as c:
        c.execute('ALTER TABLE document_monitor_session DROP COLUMN generation_started_at')
        c.execute('DROP TABLE document_observation_segment')
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DROP TABLE daily_observation_report_outbox')
        c.execute('DROP TABLE daily_observation_report_source')
        c.execute('DELETE FROM schema_migration WHERE version > 92'); c.commit()
    Database(db.path).initialize()
    with db.connect() as c:
        assert c.execute("SELECT generation_started_at FROM document_monitor_session WHERE session_id='study'").fetchone()[0] is None
    monitor.start('study', 'start-after-upgrade')
    with db.connect() as c:
        assert c.execute("SELECT generation_started_at FROM document_monitor_session WHERE session_id='study'").fetchone()[0] is not None


def test_real_backup_restores_segment_with_its_evidence(tmp_path):
    from test_document_monitoring import _service, _artifact
    from local_restore_operation_fixture import restore_local
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.repository import utc_now
    db, monitor = _service(tmp_path)
    monitor.start('study', 'start-backup-test')
    _artifact(db, 'art', 'synthetic question')
    revision = monitor.record_revision('study', 'art', 'synthetic', 'revision-backup-test')
    now = utc_now()
    with db.connect() as c:
        c.execute("INSERT INTO device_profile(device_id,device_type,system_id_hash,capabilities_json,status) VALUES('d','document_camera','synthetic','{}','active')")
        c.execute("INSERT INTO sensor_lease(lease_id,session_id,device_id,device_type,status,opened_at,last_renewed_at,expires_at) VALUES('l','study','d','document_camera','released',?,?,?)", (now,now,now))
        monitor_id = c.execute("SELECT monitor_id FROM document_monitor_session WHERE session_id='study'").fetchone()[0]
        c.execute('INSERT INTO document_observation_segment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('seg','study','child',monitor_id,revision['revision_id'],'l','e',1,0,now,now,1,'h',now))
        c.commit()
    privacy = PrivacyLifecycleService(db, tmp_path); privacy.initialize()
    backup = privacy.create_backup(prune=False)
    with db.connect() as c:
        c.execute('DELETE FROM document_observation_segment')
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id='study'", (now,))
        c.commit()
    restore_local(privacy, backup['backup_id'], '确认恢复')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_observation_segment').fetchone()[0] == 1
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_real_app_v92_migration_lifespan_and_authenticated_segment_http(tmp_path):
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from test_document_monitoring import _service, _artifact
    from home_study_agent.config import Settings
    from home_study_agent.app import create_app
    from home_study_agent.repository import utc_now
    db, monitor = _service(tmp_path)
    monitor.start('study', 'synthetic-v92-start')
    with db.connect() as c:
        c.execute('ALTER TABLE document_monitor_session DROP COLUMN generation_started_at')
        c.execute('DROP TABLE document_observation_segment')
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DROP TABLE daily_observation_report_outbox')
        c.execute('DROP TABLE daily_observation_report_source')
        c.execute('DELETE FROM schema_migration WHERE version > 92'); c.commit()
    settings = Settings(data_dir=tmp_path, database_path=db.path, data_encryption_key=b'0'*32,
                        session_token='synthetic-token', maintenance_enabled=False)
    app = create_app(settings)
    assert db.latest_upgrade_status()['source_schema_version'] == 92
    assert db.latest_upgrade_status()['snapshot_available'] is True
    with TestClient(app, client=('127.0.0.1', 4321)) as client:
        path = '/v1/sessions/study/document-observation-segments'
        assert client.get(path + '/epoch').status_code == 401
        headers = {'X-HSC-Session':'synthetic-token'}
        epoch = client.get(path + '/epoch', headers=headers).json()['epoch']
        result = client.post('/v1/sessions/study/document-monitor/start', headers=headers,
                             json={'client_action_id':'synthetic-start-v93'})
        assert result.status_code == 200
        generation = result.json()['generation']
        _artifact(db, 'synthetic-artifact', '1. Synthetic question')
        result = client.post('/v1/sessions/study/document-monitor/revisions', headers=headers,
            json={'artifact_id':'synthetic-artifact','content_signature':'synthetic-page',
                  'client_action_id':'synthetic-revision-v93'})
        assert result.status_code == 200, result.text
        revision = result.json()['revision_id']
        now = datetime.now(timezone.utc)
        with db.connect() as c:
            c.execute("INSERT INTO device_profile(device_id,device_type,system_id_hash,capabilities_json,status) VALUES('device','document_camera','synthetic','{}','active')")
            c.execute("INSERT INTO sensor_lease(lease_id,session_id,device_id,device_type,status,opened_at,last_renewed_at,expires_at) VALUES('lease','study','device','document_camera','active',?,?,?)",
                      ((now-timedelta(seconds=1)).isoformat(), now.isoformat(), (now+timedelta(seconds=60)).isoformat()))
            c.commit()
        # Synthetic timestamps model qualified frames; no camera or household content is used.
        start = datetime.now(timezone.utc) + timedelta(milliseconds=10)
        end = start + timedelta(milliseconds=100)
        payload = dict(child_id='child',revision_id=revision,lease_id='lease',epoch=epoch,generation=generation,
                       seq=0,segment_id='synthetic-segment',started_at=start.isoformat(),ended_at=end.isoformat(),visible_ms=90)
        response = client.post(path, headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert client.post(path, headers=headers, json=payload).json()['replayed'] is True
        assert client.post(path, headers=headers, json={**payload,'child_id':'other'}).status_code == 409
        from zoneinfo import ZoneInfo
        day = start.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        summary = client.get('/v1/children/child/daily-learning-summary', headers=headers, params={'learning_date':day})
        assert summary.status_code == 200, summary.text
        page = summary.json()['page_observation_time']['pages'][0]
        assert page['revision_id'] == revision and page['estimated_visible_ms'] == 90
        assert summary.json()['page_observation_time']['actual_learning_seconds'] is None
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_observation_segment').fetchone()[0] == 1
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    from home_study_agent.database import Database
    from home_study_agent.learning_state import LearningStateService
    restarted = LearningStateService(Database(db.path))
    assert restarted.daily_learning_summary('child', day)['page_observation_time']['pages'][0]['estimated_visible_ms'] == 90
    tomorrow = (start.astimezone(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=1)).isoformat()
    assert restarted.daily_learning_summary('child', tomorrow)['page_observation_time']['status'] == 'no_segments'
    with db.connect() as c:
        now = utc_now()
        c.execute("INSERT INTO child_profile VALUES('other','synthetic','初三','江苏','active',?,?)", (now,now))
        row = c.execute("SELECT * FROM document_observation_segment WHERE segment_id='synthetic-segment'").fetchone()
        c.execute('INSERT INTO document_observation_segment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('cross-owner','study','other',row['monitor_id'],revision,'lease','other-epoch',generation,0,
                   row['started_at'],row['ended_at'],90,'synthetic-hash',now))
        c.commit()
    assert restarted.daily_learning_summary('other', day)['page_observation_time']['status'] == 'no_segments'
    assert restarted.daily_learning_summary('child', day)['page_observation_time']['pages'][0]['estimated_visible_ms'] == 90
    with db.connect() as c:
        original_text = c.execute("SELECT extracted_text FROM capture_artifact WHERE artifact_id='synthetic-artifact'").fetchone()[0]
        c.execute("UPDATE capture_artifact SET extracted_text='synthetic changed page' WHERE artifact_id='synthetic-artifact'"); c.commit()
    changed = restarted.daily_learning_summary('child', day)['page_observation_time']
    assert changed['status'] == 'no_segments' and changed['invalid_segments_excluded'] == 1
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text=? WHERE artifact_id='synthetic-artifact'", (original_text,)); c.commit()
    with db.connect() as c:
        c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,status,created_at,updated_at) VALUES('delete-synthetic','child','media','child_scope','deleting',?,?)", (now,now)); c.commit()
    assert restarted.daily_learning_summary('child', day)['page_observation_time']['reason'] == 'DELETION_PENDING'
    with db.connect() as c:
        c.execute("DELETE FROM deletion_job WHERE deletion_id='delete-synthetic'")
        c.execute("DELETE FROM capture_artifact WHERE artifact_id='synthetic-artifact'"); c.commit()
    assert restarted.daily_learning_summary('child', day)['page_observation_time']['status'] == 'no_segments'
