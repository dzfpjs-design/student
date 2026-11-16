"""Actual app mounting and parent/session authentication, using synthetic PCM."""
import hashlib

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_pet_media_evidence import media


def test_actual_host_delivers_only_the_reviewed_original_and_recovers_after_restart(media, tmp_path):
    db, _, sid, prompt, path, _ = media
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token='isolated-media-session')
    headers = {'X-HSC-Session': 'isolated-media-session'}
    prefix = f'/v1/sessions/{sid}/pet-media'
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        payload = {'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                   'artifact_id': 'audio', 'client_action_id': 'host-listen'}
        assert client.post(prefix + '/listening/begin', json=payload).status_code == 401
        begin = client.post(prefix + '/listening/begin', headers=headers, json=payload)
        assert begin.status_code == 200, begin.text
        row = begin.json()['media']
        audio_url = prefix + f"/{row['media_id']}/audio?audio_sha256={row['audio']['sha256']}"
        assert client.get(audio_url, headers=headers).status_code == 409
        review_url = prefix + f"/{row['media_id']}/source-review"
        review = {'audio_sha256': row['audio']['sha256'], 'operation': 'approve', 'client_action_id': 'host-review'}
        assert client.post(review_url, headers=headers, json=review).status_code == 401
        setup = client.post('/v1/parent/setup', headers=headers, json={'pin': '2468'})
        assert setup.status_code == 200, setup.text
        parent = {**headers, 'X-HSC-Parent': setup.json()['parent_token']}
        assert client.post(review_url, headers=parent, json=review).status_code == 200
        response = client.get(audio_url, headers=headers)
        assert response.status_code == 200 and response.content == path.read_bytes()
        assert response.headers['cache-control'] == 'no-store'
        assert hashlib.sha256(response.content).hexdigest() == row['audio']['sha256']
        assert client.get(audio_url, headers={**headers, 'Range': 'bytes=0-12'}).status_code == 416
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        current = client.get(prefix + '/' + row['media_id'], headers=headers)
        assert current.status_code == 200, current.text
        assert current.json()['media']['source_reviewed'] is True
        assert current.json()['media']['qualification']['skill_qualified'] is False
        assert client.post(review_url, headers=parent, json={**review, 'operation': 'retract', 'client_action_id': 'expired-parent'}).status_code == 401
