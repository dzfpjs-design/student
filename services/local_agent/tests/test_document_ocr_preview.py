import base64
import json
from pathlib import Path

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


def image_request(generation):
    return {'expected_generation': generation, 'client_action_id': 'local-preview-synthetic-frame',
            'image_base64': base64.b64encode(frame()).decode()}


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
        body = image_request(generation)
        response = client.post(route, json=body)
        assert response.status_code == 200, response.text
        assert response.headers['Cache-Control'] == 'no-store'
        result = response.json()
        assert result.keys() == {'preview_id', 'created_at', 'text', 'question_count', 'generation'}
        assert result['text'] == '1. Calculate the perimeter.'
        assert result['question_count'] == 1 and result['generation'] == generation
        assert client.post(route, json=body).json() == result
        assert client.post(route, json={**body, 'client_action_id': 'another-preview-same-generation'}).status_code == 409
        assert client.post(route, json={**body, 'image_base64': base64.b64encode(frame() + b'extra').decode()}).status_code == 409
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


def test_preview_needs_current_homework_lease_and_rechecks_after_recognition(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        configure(client)
        root, db, generation = start_homework(client)
        route = root + '/previews'
        body = image_request(generation)
        assert client.post(route, json={**body, 'expected_generation': generation + 1}).status_code == 409
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
