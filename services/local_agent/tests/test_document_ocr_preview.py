import base64
import json
from hashlib import sha256
from pathlib import Path

from home_study_agent.document_ocr_preview import DocumentOcrPreviewService
from home_study_agent.local_ocr.workflow import OcrWorkflowDeferredError
from test_app import client_for
from test_visual_capture_producer import configure
from test_visual_material import frame


def start_homework(client):
    response = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'open-ocr-preview'})
    assert response.status_code == 201, response.text
    session_id = response.json()['session_id']
    root = f'/v1/sessions/{session_id}'
    for expected, target in [('check_in', 'classroom_recall'), ('classroom_recall', 'homework_capture')]:
        if expected == 'classroom_recall':
            response = client.post(root + '/recalls', json={
                'subject': '数学', 'summary': '合成课堂记录', 'client_action_id': 'preview-recall-example'
            })
            assert response.status_code in {200, 201}, response.text
        response = client.post(root + '/transition', json={
            'expected_state': expected, 'target_state': target, 'reason': 'synthetic-test',
            'client_action_id': f'preview-transition-{target}',
        })
        assert response.status_code == 200, response.text
    response = client.post(root + '/document-monitor/start', json={'client_action_id': 'start-ocr-preview'})
    assert response.status_code == 200, response.text
    database = client.app.state.document_monitoring.database
    with database.connect() as connection:
        connection.execute("""INSERT INTO device_profile VALUES
            ('preview-device','document_camera','synthetic-preview-hash','{}',NULL,'ready')""")
        connection.execute("""INSERT INTO sensor_lease VALUES
            ('preview-lease',?,'preview-device','document_camera','active',datetime('now'),datetime('now'),
             datetime('now','+10 minutes'),NULL,NULL)""", (session_id,))
        connection.commit()
    return root + '/document-monitor', database, response.json()['generation']


def image_request(generation, *, signed=False, diagonal=False):
    raw = frame(diagonal=diagonal)
    body = {'expected_generation': generation, 'client_action_id': 'local-preview-synthetic-frame',
            'expected_lease_id': 'preview-lease', 'image_base64': base64.b64encode(raw).decode()}
    if signed:
        body['frame_signature'] = sha256(raw).hexdigest()
    return body


def test_preview_persists_locally_without_formal_artifact_and_obeys_erasure(tmp_path, monkeypatch):
    with client_for(tmp_path, session_token='synthetic-session-token') as client:
        client.headers.update({'X-HSC-Session': 'synthetic-session-token'})
        configure(client)
        root, db, generation = start_homework(client)
        route = root + '/previews'
        def forbidden(*args, **kwargs):
            raise AssertionError('preview crossed a formal or cloud OCR boundary')

        monkeypatch.setattr(client.app.state.media_ai, 'ocr', forbidden)
        monkeypatch.setattr(client.app.state.document_vision, 'analyze_page', forbidden)
        denied = client.get(route, headers={'X-HSC-Session': 'wrong'})
        assert denied.status_code == 401 and denied.headers['Cache-Control'] == 'no-store'
        assert client.get(route).json() == {'items': []}
        body = image_request(generation, signed=True)
        mismatch = client.post(route, json={**body, 'frame_signature': 'b' * 64})
        assert mismatch.status_code == 409
        assert mismatch.json()['detail']['code'] == 'DOCUMENT_PREVIEW_FRAME_SIGNATURE_MISMATCH'
        with db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM ocr_job WHERE request_id LIKE 'document-preview:%'").fetchone()[0] == 0
        assert client.get(route).json() == {'items': []}
        response = client.post(route, json=body)
        assert response.status_code == 200, response.text
        assert response.headers['Cache-Control'] == 'no-store'
        result = response.json()
        assert result.keys() == {'preview_id', 'created_at', 'text', 'question_count', 'generation',
                                 'client_action_id', 'frame_signature'}
        assert result['text'] == '1. Calculate the perimeter.'
        assert result['question_count'] == 1 and result['generation'] == generation
        assert result['client_action_id'] == body['client_action_id'] and result['frame_signature'] == body['frame_signature']
        assert client.post(route, json=body).json() == result
        assert client.post(route, json={**body, 'frame_signature': 'a' * 63}).status_code == 422
        replay_conflict = client.post(route, json=image_request(generation, signed=True, diagonal=True))
        assert replay_conflict.status_code == 409 and replay_conflict.json()['detail']['code'] == 'DOCUMENT_PREVIEW_ACTION_CONFLICT'
        assert client.post(root + '/revisions', json={
            'artifact_id': result['preview_id'], 'content_signature': 'preview-is-not-a-revision',
            'client_action_id': 'preview-revision-denied',
        }).status_code == 409
        assert client.post('/v1/capture-artifacts/' + result['preview_id'] + '/mistake-candidates/prepare', json={
            'child_id': 'child-primary-4', 'client_action_id': 'preview-mistake-denied',
        }).status_code == 404
        with db.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM document_page_revision').fetchone()[0] == 0
            assert tuple(connection.execute("SELECT status,artifact_id,publication_started FROM ocr_job WHERE request_id LIKE 'document-preview:%'").fetchone()) == ('completed', None, 0)

    with client_for(tmp_path, session_token='synthetic-session-token') as restarted:
        restarted.headers.update({'X-HSC-Session': 'synthetic-session-token'})
        reading = restarted.get(route)
        assert reading.json() == {'items': [result]}
        assert reading.headers['Cache-Control'] == 'no-store'
        privacy = restarted.app.state.privacy_lifecycle
        export = privacy.export_child('child-primary-4')
        payload = json.loads(Path(export['path']).read_text(encoding='utf-8'))
        assert len(payload['document_ocr_previews']) == 1
        assert privacy.deletion_preview('child-primary-4')['document_ocr_previews'] == 1
        assert privacy.deletion_preview('child-primary-4')['document_ocr_preview_jobs'] == 1
        privacy.delete_child_scope('child-primary-4', 'media', '确认删除', 'erase-local-preview-test')
        with restarted.app.state.document_monitoring.database.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM document_ocr_preview').fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM ocr_job WHERE request_id LIKE 'document-preview:%'").fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM ocr_request_tombstone').fetchone()[0] == 1


def test_new_writing_saves_separate_previews_in_the_same_monitor_generation(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        configure(client)
        root, db, generation = start_homework(client)
        route = root + '/previews'
        first = image_request(generation)
        before = client.post(route, json=first).json()

        engine = client.app.state.local_ocr.engines[0]
        read = engine.recognize

        def recognize_with_new_answer(image, route):
            return [{**line, 'text': '1. A new answer is written.'} for line in read(image, route)]

        monkeypatch.setattr(engine, 'recognize', recognize_with_new_answer)
        second = {
            **image_request(generation, signed=True, diagonal=True),
            'client_action_id': 'next-preview-same-active-lease',
        }
        response = client.post(route, json=second)
        assert response.status_code == 200, response.text
        after = response.json()
        assert after['generation'] == before['generation'] == generation
        assert after['text'] == '1. A new answer is written.'
        assert after['preview_id'] != before['preview_id']
        assert client.post(route, json=second).json() == after
        assert client.get(route).json() == {'items': [after, before]}
        with db.connect() as connection:
            rows = connection.execute("SELECT lease_id,COUNT(*) FROM document_ocr_preview GROUP BY lease_id").fetchall()
            assert [tuple(row) for row in rows] == [('preview-lease', 2)]


def test_legacy_unique_generation_migrates_without_losing_the_saved_action(tmp_path):
    with client_for(tmp_path) as client:
        configure(client)
        root, db, generation = start_homework(client)
        session_id = root.split('/')[3]
        with db.connect() as connection:
            connection.execute("DROP TABLE document_ocr_preview")
            connection.execute("""CREATE TABLE document_ocr_preview (
                preview_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                monitor_generation INTEGER NOT NULL, lease_id TEXT NOT NULL, frame_sha256 TEXT NOT NULL,
                client_action_id TEXT NOT NULL UNIQUE, text TEXT NOT NULL, question_count INTEGER NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(session_id, monitor_generation))""")
            connection.execute("""CREATE INDEX idx_document_ocr_preview_session
                ON document_ocr_preview(session_id, created_at DESC)""")
            connection.execute("""INSERT INTO document_ocr_preview VALUES (
                'legacy-preview','child-primary-4',?,?,'preview-lease',?,'local-preview-synthetic-frame',
                '1. Legacy reading',1,'2026-11-20T12:00:00+00:00')""", (session_id, generation, sha256(frame()).hexdigest()))
            connection.commit()

        DocumentOcrPreviewService(db, client.app.state.ocr_workflow)
        route = root + '/previews'
        legacy = client.get(route).json()['items'][0]
        assert legacy == {
            'preview_id': 'legacy-preview', 'client_action_id': 'local-preview-synthetic-frame',
            'generation': generation, 'frame_signature': None, 'text': '1. Legacy reading',
            'question_count': 1, 'created_at': '2026-11-20T12:00:00+00:00',
        }
        assert client.post(route, json=image_request(generation)).json() == legacy
        for number in range(11):
            response = client.post(route, json={**image_request(generation), 'client_action_id': f'followup-{number}'})
            assert response.status_code == 200, response.text
        history = client.get(route).json()['items']
        assert len(history) == 10
        assert history[0]['client_action_id'] == 'followup-10'
        assert history[-1]['client_action_id'] == 'followup-1'
        assert client.post(route, json=image_request(generation)).json() == legacy
        with db.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM document_ocr_preview').fetchone()[0] == 12
            assert connection.execute('PRAGMA foreign_key_check(document_ocr_preview)').fetchall() == []


def test_deferred_preview_recovery_cannot_replace_the_original_frame_signature(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        configure(client)
        root, _, generation = start_homework(client)
        route = root + '/previews'
        body = image_request(generation, signed=True)

        def defer(*args):
            raise OcrWorkflowDeferredError('preview worker busy')

        with monkeypatch.context() as scoped:
            scoped.setattr(client.app.state.ocr_workflow, 'finish_recognition', defer)
            assert client.post(route, json=body).json()['detail']['code'] == 'DOCUMENT_OCR_BUSY'
        conflict = client.post(route, json=image_request(generation, signed=True, diagonal=True))
        assert conflict.status_code == 409 and conflict.json()['code'] == 'OCR_ACTION_CONFLICT'
        resumed = client.post(route, json=body)
        assert resumed.status_code == 200, resumed.text
        assert client.get(route).json() == {'items': [resumed.json()]}


def test_preview_needs_current_homework_lease_and_rechecks_after_recognition(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        configure(client)
        root, db, generation = start_homework(client)
        route = root + '/previews'
        body = image_request(generation)
        assert client.post(route, json={**body, 'expected_generation': generation + 1}).status_code == 409
        assert client.post(route, json={key: value for key, value in body.items() if key != 'expected_lease_id'}).status_code == 422
        assert client.get('/v1/sessions/other-session/document-monitor/previews').status_code == 409

        def update(sql):
            with db.connect() as connection:
                connection.execute(sql)
                connection.commit()

        update("UPDATE daily_session SET state='study'")
        assert client.post(route, json=body).status_code == 409
        update("UPDATE daily_session SET state='homework_capture', observation_status='paused'")
        assert client.post(route, json=body).status_code == 409
        assert client.get(route).status_code == 409
        update("UPDATE daily_session SET observation_status='enabled'")
        update("UPDATE sensor_lease SET status='released'")
        assert client.post(route, json=body).status_code == 409
        update("""INSERT INTO sensor_lease SELECT 'replacement-lease',session_id,device_id,device_type,
            'active',opened_at,last_renewed_at,expires_at,NULL,NULL
            FROM sensor_lease WHERE lease_id='preview-lease'""")
        assert client.post(route, json=body).json()['detail']['code'] == 'DOCUMENT_PREVIEW_SOURCE_CHANGED'
        with db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM ocr_job WHERE request_id LIKE 'document-preview:%'").fetchone()[0] == 0
        update("DELETE FROM sensor_lease WHERE lease_id='replacement-lease'")
        update("UPDATE sensor_lease SET status='active'")

        workflow = client.app.state.ocr_workflow
        finish = workflow.finish_recognition

        def release_lease(scheduled, image):
            result = finish(scheduled, image)
            update("UPDATE sensor_lease SET status='released'")
            return result

        monkeypatch.setattr(workflow, 'finish_recognition', release_lease)
        assert client.post(route, json=body).json()['detail']['code'] == 'DOCUMENT_PREVIEW_SOURCE_CHANGED'
        assert client.get(route).json() == {'items': []}
        with db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM ocr_job WHERE request_id LIKE 'document-preview:%'").fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM ocr_cache').fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM ocr_request_tombstone').fetchone()[0] == 1
        update("UPDATE sensor_lease SET status='active'")
        assert client.post(route, json=body).json()['code'] == 'OCR_SOURCE_INVALIDATED'
