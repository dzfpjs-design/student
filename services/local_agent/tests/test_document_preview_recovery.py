"""Synthetic frames exercise exact recovery and late-writer deletion fences."""
from hashlib import sha256
import json
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest

from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_ocr_preview import DocumentOcrPreviewService
from home_study_agent.document_preview_recovery import DocumentPreviewRecoveryService
from home_study_agent.local_ocr.cache_lifecycle import OcrSourceInvalid, request_digest
from test_app import client_for
from test_document_ocr_preview import image_request, start_homework
from test_ocr_cache_lifecycle import env
from test_local_ocr_pipeline import image_bytes
from test_visual_capture_producer import configure


@pytest.fixture
def previews(env):
    db, repo, sid, ocr, queue, adapter, workflow = env
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',observation_status='enabled' WHERE session_id=?", (sid,))
        c.execute("INSERT INTO device_profile VALUES ('device','document_camera','synthetic','{}',NULL,'ready')")
        c.execute("""INSERT INTO sensor_lease VALUES
            ('lease',?,'device','document_camera','active',datetime('now'),datetime('now'),
             datetime('now','+10 minutes'),NULL,NULL)""", (sid,))
        c.commit()
    generation = DocumentMonitoringService(db).start(sid, 'start-monitor')['generation']
    preview = DocumentOcrPreviewService(db, workflow)
    recovery = DocumentPreviewRecoveryService(db, workflow)
    return db, sid, generation, preview, recovery, ocr, queue, workflow


def resolve(previews, action='uncertain-action', **overrides):
    db, sid, generation, _, recovery, *_ = previews
    params = dict(session_id=sid, client_action_id=action, generation=generation,
                  expected_lease_id='lease', expected_phase='homework_capture')
    return recovery.resolve(**{**params, **overrides})


def test_not_arrived_request_is_fenced_across_restart_and_scope_replay(previews):
    db, sid, generation, preview, recovery, _, _, workflow = previews
    expected = dict(status='retired', client_action_id='uncertain-action', generation=generation,
                    phase='homework_capture')
    assert resolve(previews) == expected
    assert resolve(previews) == expected
    restarted = DocumentPreviewRecoveryService(db, workflow)
    assert restarted.resolve(sid, 'uncertain-action', generation, 'lease', 'homework_capture') == expected
    with pytest.raises(OcrSourceInvalid, match='DELETED'):
        DocumentOcrPreviewService(db, workflow).create(sid, image_bytes(), 'uncertain-action', generation,
                                                     'lease', expected_phase='homework_capture')
    with pytest.raises(ValueError, match='CONFLICT'):
        resolve(previews, page_token=str(uuid4()))
    with pytest.raises(ValueError, match='CONFLICT'):
        resolve(previews, inspection_turn_id='another-inspection')
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='plan_review'")
        c.commit()
    with pytest.raises(ValueError, match='CONFLICT'):
        resolve(previews, expected_phase='plan_review')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_ocr_preview').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM ocr_job').fetchone()[0] == 0
        row = c.execute('SELECT * FROM document_preview_recovery_fence').fetchone()
        assert all(len(value) == 64 and all(char in '0123456789abcdef' for char in value) for value in row)


def test_saved_action_is_found_after_ten_newer_previews_and_is_never_retired(previews):
    db, sid, generation, preview, _, *_ = previews
    saved = preview.create(sid, image_bytes(), 'uncertain-action', generation, 'lease')
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM document_ocr_preview').fetchone())
        for index in range(11):
            next_row = {**row, 'preview_id': str(uuid4()), 'client_action_id': f'newer-action-{index}',
                        'created_at': f'2099-01-01T00:00:{index:02d}+00:00'}
            c.execute('INSERT INTO document_ocr_preview (' + ','.join(next_row) + ') VALUES (' +
                      ','.join('?' for _ in next_row) + ')', list(next_row.values()))
        c.commit()
    assert saved['preview_id'] not in {item['preview_id'] for item in preview.latest(sid)['items']}
    result = resolve(previews)
    assert result == dict(status='saved', client_action_id='uncertain-action', generation=generation,
                          phase='homework_capture', preview=saved)
    assert 'inspection_turn_id' not in result['preview'] and 'inspection_response_text' not in result['preview']
    with pytest.raises(ValueError, match='CONFLICT'):
        resolve(previews, page_token=str(uuid4()))
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_ocr_preview').fetchone()[0] == 12
        assert c.execute('SELECT count(*) FROM ocr_request_tombstone').fetchone()[0] == 0


@pytest.mark.parametrize('scope_change', ['generation', 'lease', 'phase', 'child'])
@pytest.mark.parametrize('already_saved', [False, True])
def test_current_other_scope_cannot_read_or_retire_an_owned_action(previews, scope_change, already_saved):
    db, sid, generation, preview, recovery, _, _, workflow = previews
    if already_saved:
        preview.create(sid, image_bytes(), 'uncertain-action', generation, 'lease')
    else:
        workflow.queue_recognition(sid, image_bytes(), 'document-preview:uncertain-action', generation,
                                   preview_lease_id='lease')
    lease, phase = 'lease', 'homework_capture'
    with db.connect() as c:
        if scope_change == 'generation':
            generation += 1
            c.execute('UPDATE document_monitor_session SET generation=?', (generation,))
        elif scope_change == 'lease':
            lease = 'replacement-lease'
            c.execute('UPDATE sensor_lease SET lease_id=?', (lease,))
        elif scope_change == 'phase':
            phase = 'plan_review'
            c.execute('UPDATE daily_session SET state=?', (phase,))
        c.commit()
    if scope_change == 'child':
        from home_study_agent.repository import StudyRepository
        sid = StudyRepository(db).switch_active_session_child(sid, 'B', 'switch-owner')['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',observation_status='enabled' WHERE session_id=?", (sid,))
            c.execute("""INSERT INTO sensor_lease SELECT 'other-child-lease',?,device_id,device_type,
                'active',opened_at,last_renewed_at,expires_at,NULL,NULL FROM sensor_lease WHERE lease_id='lease'""", (sid,))
            c.commit()
        lease = 'other-child-lease'
        generation = DocumentMonitoringService(db).start(sid, 'start-B-monitor')['generation']
    with pytest.raises(ValueError, match='CONFLICT'):
        recovery.resolve(sid, 'uncertain-action', generation, lease, phase)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_ocr_preview').fetchone()[0] == int(already_saved)
        assert c.execute('SELECT count(*) FROM ocr_job').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ocr_request_tombstone').fetchone()[0] == 0


@pytest.mark.parametrize('phase,signed,inspection', [
    ('homework_capture', False, None), ('homework_capture', True, None),
    ('plan_review', True, None), ('study', False, 'voice-inspection'),
])
def test_queued_owner_requires_exact_page_phase_lease_and_inspection(previews, phase, signed, inspection):
    db, sid, generation, _, _, _, queue, workflow = previews
    page_token = str(uuid4())
    image = image_bytes()
    signature = sha256(image).hexdigest() if signed else None
    if inspection is not None or phase != 'homework_capture':
        signature = sha256(json.dumps([signature, inspection, phase]).encode()).hexdigest()
    signature = sha256(json.dumps([signature, page_token]).encode()).hexdigest()
    with db.connect() as c:
        c.execute('UPDATE daily_session SET state=?', (phase,))
        c.commit()
    scheduled = workflow.queue_recognition(sid, image, 'document-preview:uncertain-action', generation,
                                           preview_lease_id='lease', preview_signature=signature)
    with pytest.raises(ValueError, match='CONFLICT'):
        resolve(previews, expected_phase=phase, page_token=str(uuid4()), inspection_turn_id=inspection)
    if inspection:
        with pytest.raises(ValueError, match='CONFLICT'):
            resolve(previews, expected_phase=phase, page_token=page_token)
    assert resolve(previews, expected_phase=phase, page_token=page_token,
                   inspection_turn_id=inspection)['status'] == 'retired'
    with queue._connect() as c:
        assert c.execute('SELECT status FROM cpu_task WHERE task_id=?',
                         (scheduled['task']['task_id'],)).fetchone()[0] == 'cancelled'
    with pytest.raises(OcrSourceInvalid, match='DELETED'):
        workflow.queue_recognition(sid, image, 'document-preview:uncertain-action', generation,
                                   preview_lease_id='lease', preview_signature=signature)


@pytest.mark.parametrize('fence_point', ['recognizer_running', 'recognizer_completed'])
def test_real_thread_late_ocr_cannot_commit_after_resolution(previews, monkeypatch, fence_point):
    db, sid, generation, preview, _, ocr, queue, workflow = previews
    entered, release = Event(), Event()
    owner, method = (ocr, '_process') if fence_point == 'recognizer_running' else (workflow, 'finish_recognition')
    original = getattr(owner, method)
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs) if fence_point == 'recognizer_completed' else None
        entered.set()
        assert release.wait(10)
        return result if fence_point == 'recognizer_completed' else original(*args, **kwargs)
    monkeypatch.setattr(owner, method, delayed)
    outcome = []
    def write():
        try:
            outcome.append(preview.create(sid, image_bytes(), 'uncertain-action', generation, 'lease'))
        except Exception as error:
            outcome.append(error)
    worker = Thread(target=write)
    worker.start()
    try:
        assert entered.wait(10)
        assert resolve(previews)['status'] == 'retired'
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and len(outcome) == 1 and isinstance(outcome[0], Exception)
    with db.connect() as c:
        for table in ('ocr_job', 'ocr_cache', 'document_ocr_preview', 'capture_artifact'):
            assert c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        assert c.execute('SELECT reason FROM ocr_request_tombstone WHERE request_digest=?',
                         (request_digest('document-preview:uncertain-action'),)).fetchone()[0] == 'deleted'
    with queue._connect() as c:
        assert c.execute('SELECT status FROM cpu_task').fetchone()[0] == (
            'cancelled' if fence_point == 'recognizer_running' else 'completed')
    with pytest.raises(OcrSourceInvalid, match='DELETED'):
        preview.create(sid, image_bytes(), 'uncertain-action', generation, 'lease')


def test_http_requires_configured_current_token_valid_scope_and_no_store(tmp_path):
    with client_for(tmp_path, session_token='local-proof') as client:
        client.headers.update({'X-HSC-Session': 'local-proof'})
        configure(client)
        root, db, generation = start_homework(client)
        route = root + '/previews/local-preview-synthetic-frame/resolve'
        body = dict(expected_generation=generation, expected_lease_id='preview-lease',
                    expected_phase='homework_capture')
        denied = client.post(route, json=body, headers={'X-HSC-Session': 'wrong'})
        assert denied.status_code == 401 and denied.headers['Cache-Control'] == 'no-store'
        assert client.post(route, json={key: value for key, value in body.items() if key != 'expected_phase'}).status_code == 422
        for overrides in ({'expected_generation': generation + 1}, {'expected_lease_id': 'wrong'},
                          {'expected_phase': 'study'}):
            assert client.post(route, json={**body, **overrides}).status_code == 409
        with db.connect() as c:
            c.execute("UPDATE daily_session SET observation_status='paused'")
            c.commit()
        assert client.post(route, json=body).status_code == 409
        with db.connect() as c:
            c.execute("UPDATE daily_session SET observation_status='enabled'")
            c.commit()
        response = client.post(route, json=body)
        assert response.status_code == 200 and response.json()['status'] == 'retired'
        assert response.headers['Cache-Control'] == 'no-store'
        assert client.post(root + '/previews', json=image_request(generation)).status_code == 409
        # Privacy export carries neither tombstones nor recovery digests. The
        # retained anonymous fence has no child text or identifiers to leak.
        export = client.app.state.privacy_lifecycle.export_child('child-primary-4')
        payload = json.loads(Path(export['path']).read_text(encoding='utf-8'))
        assert 'document_preview_recovery_fence' not in payload and 'ocr_request_tombstone' not in payload
        assert payload['document_ocr_previews'] == []
        client.app.state.privacy_lifecycle.delete_child_scope('child-primary-4', 'media', '确认删除', 'erase-retired-preview')
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM document_preview_recovery_fence').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM ocr_job').fetchone()[0] == 0
    with client_for(tmp_path / 'no-token') as client:
        response = client.post('/v1/sessions/unknown/document-monitor/previews/absent-action/resolve',
                               json=dict(expected_generation=0, expected_lease_id='lease', expected_phase='study'))
        assert response.status_code == 401
