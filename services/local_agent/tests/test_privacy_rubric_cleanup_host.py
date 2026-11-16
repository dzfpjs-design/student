"""Actual parent knowledge deletion must not leave either original-audio review copy."""
from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_pet_audio_rubric_review import prepared
from test_pet_media_evidence import media, CHILD
from test_companion_activity_host import NoCloud


@pytest.mark.parametrize('historical_binding_missing', [False, True])
def test_actual_privacy_knowledge_deletion_removes_audio_review_copies(media, historical_binding_missing):
    db, service, snapshot, body, _ = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    if historical_binding_missing:
        with db.connect() as c:
            c.execute('DELETE FROM pet_media_binding WHERE child_id=?', (CHILD,))
            c.commit()
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='cleanup-isolated')
    local = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin': '2468'}).json()['parent_token']
        headers = {**local, 'X-HSC-Parent': token}
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers=headers,
            json={'scope': 'knowledge', 'confirmation': '确认删除', 'client_action_id': 'delete-audio-review-source'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'completed', response.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM pet_media_binding WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock', headers=local, json={'pin': '2468'}).json()['parent_token']
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local, 'X-HSC-Parent': token},
            json={'scope': 'knowledge', 'confirmation': '确认删除', 'client_action_id': 'delete-audio-review-source'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'completed'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE child_id=?', (CHILD,)).fetchone()[0] == 0


@pytest.mark.parametrize('column', ['payload_json', 'request_json'])
def test_actual_privacy_corrupt_copy_rolls_back_and_persists_retryable_failure(media, column):
    db, service, snapshot, body, _ = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='cleanup-isolated')
    local = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin': '2468'}).json()['parent_token']
        with db.connect() as c:
            row = dict(c.execute('SELECT * FROM rubric_review_event').fetchone())
            row.update(event_id='synthetic-damaged-copy', revision=2, client_action_id='damaged-copy')
            intact_row = dict(row)
            row[column] = '{broken'
            c.execute('INSERT INTO rubric_review_event (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
            c.commit()
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local, 'X-HSC-Parent': token},
            json={'scope': 'knowledge', 'confirmation': '确认删除', 'client_action_id': 'delete-corrupt-review-source'})
        assert response.status_code != 200
        assert response.headers.get('content-type', '').startswith('application/json'), response.text
        assert response.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE'
        assert response.json()['retryable']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE child_id=?', (CHILD,)).fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM pet_media_binding WHERE child_id=?', (CHILD,)).fetchone()[0] > 0
        job = c.execute("SELECT status,error_code FROM deletion_job WHERE client_action_id='delete-corrupt-review-source'").fetchone()
        assert tuple(job) == ('failed', 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE')
        # Simulate recovery of the exact synthetic row from its intact copy,
        # without weakening immutable-history triggers or discarding approval.
        c.execute("DELETE FROM rubric_review_event WHERE event_id='synthetic-damaged-copy'")
        c.execute('INSERT INTO rubric_review_event (' + ','.join(intact_row) + ') VALUES (' +
                  ','.join('?' for _ in intact_row) + ')', tuple(intact_row.values()))
        deletion_id = c.execute("SELECT deletion_id FROM deletion_job WHERE client_action_id='delete-corrupt-review-source'").fetchone()[0]
        c.commit()
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock', headers=local, json={'pin': '2468'}).json()['parent_token']
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local, 'X-HSC-Parent': token},
            json={'scope': 'knowledge', 'confirmation': '确认删除', 'client_action_id': 'delete-corrupt-review-source'})
        assert response.status_code == 200, response.text
        assert response.json()['deletion_id'] == deletion_id
        assert response.json()['status'] == 'completed'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
