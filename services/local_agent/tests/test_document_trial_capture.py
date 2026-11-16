import base64
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import cv2
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import numpy as np
import pytest

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.device_registry import DeviceRegistry
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_trial_capture import DocumentTrialCaptureService, TrialCaptureError, install_trial_capture_routes
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.repository import utc_now


def fixture(tmp_path):
    db = Database(tmp_path / 'study.db'); db.initialize()
    now = utc_now()
    with db.connect() as c:
        for child in ('one', 'two'):
            c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES(?,?,'初三','江苏南京','active',?,?)", (child, child, now, now))
            c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES(?,?,?,?,?,?,?)", (child + '-session', child, 'active' if child == 'one' else 'completed', 'study' if child == 'one' else 'completed', now, now, child))
        c.commit()
    settings = Settings(data_dir=tmp_path, database_path=db.path, data_encryption_key=b'0' * 32)
    media = LocalMediaAI(db, tmp_path, tmp_path / 'models', settings.data_encryption_key)
    monitor = DocumentMonitoringService(db)
    state = monitor.start('one-session', 'start-document-one')
    lease = DeviceRegistry(db).register('one-session', 'document_camera', 'a' * 64, {})
    image = cv2.imencode('.jpg', np.full((480, 640, 3), 220, dtype=np.uint8))[1].tobytes()
    fields = dict(lease_id=lease['lease_id'], generation=state['generation'], reason='periodic',
                  observed_at=utc_now(), client_action_id='trial-action-one')
    return db, DocumentTrialCaptureService(db, media, monitor, settings), image, fields


def test_encrypted_frame_replay_restart_and_child_isolation(tmp_path):
    db, service, image, fields = fixture(tmp_path)
    saved = service.capture('one-session', image, **fields)
    assert service.capture('one-session', image, **fields) == saved
    with db.connect() as c:
        row = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=?', (saved['artifact_id'],)).fetchone()
        assert Path(row['local_path']).read_bytes().startswith(b'HSC1')
        assert image not in Path(row['local_path']).read_bytes()
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='document_trial_frame'").fetchone()[0] == 1
    restarted = DocumentTrialCaptureService(db, service.media, service.monitor, service.settings)
    assert restarted.image('one', saved['artifact_id']) == image
    assert restarted.review('one', 'one-session')['summary']['frame_count'] == 1
    assert restarted.recent_sessions('two') == []
    with pytest.raises(TrialCaptureError, match='UNAVAILABLE'):
        restarted.image('two', saved['artifact_id'])
    with pytest.raises(TrialCaptureError, match='CONFLICT'):
        service.capture('one-session', image, **{**fields, 'reason': 'ocr_capture'})


def test_pause_and_camera_lease_cannot_retain_images(tmp_path):
    db, service, image, fields = fixture(tmp_path)
    camera = DeviceRegistry(db).register('one-session', 'camera', 'b' * 64, {})
    with pytest.raises(TrialCaptureError, match='LEASE_INVALID'):
        service.capture('one-session', image, **{**fields, 'lease_id': camera['lease_id']})
    service.monitor.pause('one-session', 'pause-document-one')
    with pytest.raises(TrialCaptureError, match='NOT_ACTIVE'):
        service.capture('one-session', image, **fields)
    assert not list((tmp_path / 'media').glob('**/*.enc'))


def test_capacity_failure_is_durable_and_does_not_delete_previous_frames(tmp_path):
    db, service, image, fields = fixture(tmp_path)
    saved = service.capture('one-session', image, **fields)
    service.settings = replace(service.settings, document_trial_capture_maximum_bytes=len(image))
    with pytest.raises(TrialCaptureError, match='CAPACITY'):
        service.capture('one-session', image, **{**fields, 'client_action_id': 'trial-action-two'})
    assert service.image('one', saved['artifact_id']) == image
    review = service.review('one', 'one-session')
    assert review['summary']['capture_failure_count'] == 1
    assert review['summary']['frame_count'] == 1


@pytest.mark.parametrize('phase', ['homework_capture', 'plan_review'])
def test_preparation_does_not_expand_study_only_image_retention(tmp_path, phase):
    db, service, image, fields = fixture(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state=? WHERE session_id='one-session'", (phase,))
        c.commit()
    with pytest.raises(TrialCaptureError, match='NOT_ACTIVE'):
        service.capture('one-session',image,**fields)
    assert not list((tmp_path/'media').glob('**/*.enc'))


def test_failed_database_write_removes_unreferenced_image(tmp_path):
    db, service, image, fields = fixture(tmp_path)
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_trial BEFORE INSERT ON capture_artifact WHEN NEW.artifact_type='document_trial_frame' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(TrialCaptureError, match='STORAGE_FAILED'):
        service.capture('one-session', image, **fields)
    assert not list((tmp_path / 'media').glob('**/*.enc'))


def test_ocr_binding_daily_review_and_recovery(tmp_path):
    db, service, image, fields = fixture(tmp_path)
    saved = service.capture('one-session', image, **{**fields, 'reason': 'ocr_capture'})
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES('ocr','one','one-session','document_ocr',NULL,'Question',.9,?,'completed',?,'ocr-action')",
                  (json.dumps({'monitor_frame_sha256': sha256(image).hexdigest()}), utc_now()))
        c.commit()
    service.bind_ocr('one-session', saved['artifact_id'], 'ocr', sha256(image).hexdigest())
    with pytest.raises(TrialCaptureError, match='FRAME_CHANGED'):
        service.bind_ocr('one-session', saved['artifact_id'], 'ocr', '0' * 64)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id='one-session'", (utc_now(),))
        c.commit()
    service.reconcile_completed(); service.reconcile_completed()
    review = service.review('one', 'one-session')
    assert review['frames'][0]['ocr_artifact_id'] == 'ocr'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='document_trial_review'").fetchone()[0] == 1
        c.execute("UPDATE capture_artifact SET local_path=NULL WHERE artifact_id=?", (saved['artifact_id'],)); c.commit()
    assert not service.review('one', 'one-session')['frames'][0]['image_available']


def test_http_capture_and_authenticated_replay(tmp_path):
    db, service, image, fields = fixture(tmp_path)
    app = FastAPI()
    class Auth:
        def require(self, token):
            if token != 'parent':
                raise HTTPException(401)
    install_trial_capture_routes(app, service, Auth())
    with TestClient(app) as client:
        response = client.post('/v1/sessions/one-session/document-monitor/trial-frames',
                               json={**fields, 'image_base64': base64.b64encode(image).decode()})
        assert response.status_code == 200, response.text
        frame_id = response.json()['artifact_id']
        route = '/v1/parent/children/one/document-trial/frames/' + frame_id + '/image'
        assert client.get(route).status_code == 401
        assert client.get(route, headers={'X-HSC-Parent': 'parent'}).content == image
        review = client.get('/v1/parent/children/one/document-trial/sessions/one-session', headers={'X-HSC-Parent': 'parent'})
        assert review.status_code == 200, review.text
        assert review.json()['summary']['frame_count'] == 1


def test_ocr_frame_is_validated_before_processing(tmp_path):
    _, service, image, fields = fixture(tmp_path)
    saved = service.capture('one-session', image, **fields)
    service.validate_ocr_frame('one-session', saved['artifact_id'], sha256(image).hexdigest())
    with pytest.raises(TrialCaptureError, match='BINDING_INVALID'):
        service.validate_ocr_frame('two-session', saved['artifact_id'], sha256(image).hexdigest())
    with pytest.raises(TrialCaptureError, match='FRAME_CHANGED'):
        service.validate_ocr_frame('one-session', saved['artifact_id'], '0' * 64)


def test_frame_question_pagination_is_complete_scoped_and_authenticated(tmp_path):
    db, service, image, fields = fixture(tmp_path)
    saved = service.capture('one-session', image, **fields)
    now = utc_now()
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES('ocr','one','one-session','document_ocr',NULL,'Question',.9,?,'completed',?,'ocr-action')",
                  (json.dumps({'monitor_frame_sha256': sha256(image).hexdigest()}), now))
        for index in range(205):
            c.execute('''INSERT INTO mistake_capture_candidate(candidate_id,artifact_id,child_id,
                question_index,question_text,confidence,status,created_at,updated_at)
                VALUES(?,'ocr','one',?, ?, .95,'pending',?,?)''',
                      (f'candidate-{index:04}', index, 'Question ' + str(index), now, now))
        c.commit()
    service.bind_ocr('one-session', saved['artifact_id'], 'ocr', sha256(image).hexdigest())
    app = FastAPI()
    class Auth:
        def require(self, token):
            if token != 'parent':
                raise HTTPException(401)
    install_trial_capture_routes(app, service, Auth())
    route = '/v1/parent/children/one/document-trial/frames/' + saved['artifact_id'] + '/questions'
    headers = {'X-HSC-Parent': 'parent'}
    with TestClient(app) as client:
        assert client.get(route).status_code == 401
        assert client.get(route.replace('/children/one/', '/children/two/'), headers=headers).status_code == 409
        assert client.get(route + '?offset=-1', headers=headers).status_code == 422
        pages = [client.get(route + '?offset=' + str(offset), headers=headers).json() for offset in (0,100,200)]
    assert [len(page['questions']) for page in pages] == [100,100,5]
    assert all(page['total'] == 205 and page['cloud_transmission_allowed'] is False for page in pages)
    assert [page['has_more'] for page in pages] == [True,True,False]
    assert len({question['candidate_id'] for page in pages for question in page['questions']}) == 205
    assert all(question['correctness'] is None for page in pages for question in page['questions'])
    restarted = DocumentTrialCaptureService(Database(db.path), service.media, service.monitor, service.settings)
    assert restarted.frame_questions('one', saved['artifact_id'], offset=200) == pages[-1]


def test_retained_frame_survives_portable_restore_key_rotation_and_explicit_delete(tmp_path):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.report_delivery import ReportDeliveryService
    source = tmp_path / 'source'; source.mkdir()
    db, service, image, fields = fixture(source)
    ReportDeliveryService(db, service.settings.data_encryption_key).initialize()
    saved = service.capture('one-session', image, **fields)
    backup = PrivacyLifecycleService(db, source, service.settings.data_encryption_key).create_portable_backup('synthetic-trial-passphrase')
    target = tmp_path / 'restored'; target.mkdir()
    restored_db = Database(target / 'study.db'); restored_db.initialize()
    key = b'1' * 32
    ReportDeliveryService(restored_db, key).initialize()
    privacy = PrivacyLifecycleService(restored_db, target, key)
    privacy.restore_portable_backup(backup['path'], 'synthetic-trial-passphrase', '确认迁移恢复')
    settings = replace(service.settings, data_dir=target, database_path=restored_db.path, data_encryption_key=key)
    restored = DocumentTrialCaptureService(restored_db, LocalMediaAI(restored_db, target, target / 'models', key), DocumentMonitoringService(restored_db), settings)
    assert restored.image('one', saved['artifact_id']) == image
    with restored_db.connect() as c:
        raw = c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?', (saved['artifact_id'],)).fetchone()[0]
    assert Path(raw).is_relative_to(target / 'media')
    privacy.delete_capture_artifact_raw_media(saved['artifact_id'], '确认删除', 'delete-trial-frame-one')
    assert not Path(raw).exists()
    assert restored.review('one', 'one-session')['frames'][0]['image_available'] is False
    with pytest.raises(TrialCaptureError, match='UNAVAILABLE'):
        restored.image('one', saved['artifact_id'])
    assert service.image('one', saved['artifact_id']) == image


def test_review_links_correct_wrong_and_withdrawn_scoring(tmp_path):
    db, service, image, fields = fixture(tmp_path)
    saved = service.capture('one-session', image, **fields)
    now = utc_now()
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES('ocr','one','one-session','document_ocr',NULL,'Question',.9,?,'completed',?,'ocr-action')",
                  (json.dumps({'monitor_frame_sha256': sha256(image).hexdigest()}), now))
        for index, correctness in enumerate((1, 0, 1)):
            candidate, request, decision = 'candidate-' + str(index), 'request-' + str(index), 'decision-' + str(index)
            c.execute('''INSERT INTO mistake_capture_candidate(candidate_id,artifact_id,child_id,
                question_index,question_text,confidence,status,created_at,updated_at)
                VALUES(?,'ocr','one',?, ?, .95,'pending',?,?)''', (candidate, index, 'Question ' + str(index), now, now))
            c.execute('''INSERT INTO paper_scoring_request(request_id,candidate_id,child_id,session_id,
                question_text,answer_text,answer_source,question_sha256,answer_sha256,confirmation_source,
                status,created_at,client_action_id)
                VALUES(?,?,'one','one-session',?,'B','child_spoken',?,?,'child_spoken_readback','pending_authority',?,?)''',
                      (request, candidate, 'Question ' + str(index), str(index) * 64, 'a' * 64, now, request))
            c.execute('''INSERT INTO paper_scoring_decision VALUES(?,?,'one','teacher_mark',?,'{}',?,.99,'qualified',?,?)''',
                      (decision, request, 'mark-' + str(index), correctness, now, decision))
        c.execute('''INSERT INTO paper_scoring_decision_status_event(event_id,decision_id,child_id,status,reason,created_at,client_action_id)
            VALUES('revoke','decision-2','one','withdrawn','source removed',?,'revoke-action')''', (now,))
        c.commit()
    service.bind_ocr('one-session', saved['artifact_id'], 'ocr', sha256(image).hexdigest())
    review = service.review('one', 'one-session')
    assert review['summary']['correct_count'] == 1
    assert review['summary']['wrong_count'] == 1
    assert review['summary']['unscored_count'] == 1
    assert [q['correctness'] for q in review['frames'][0]['questions']] == [1, 0, None]
    assert review['frames'][0]['questions'][2]['authority_type'] is None
