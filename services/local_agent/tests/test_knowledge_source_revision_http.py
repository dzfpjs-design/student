"""Actual create_app + SQLite, no model or outbound messages."""
import json

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from test_knowledge_source_revision import legacy, CHILD


def parent(client, setup=False):
    response = client.post('/v1/parent/' + ('setup' if setup else 'unlock'), json={'pin': '8264'})
    assert response.status_code == 200, response.text
    return {'X-HSC-Parent': response.json()['parent_token']}


def test_actual_parent_repair_review_restart_export_and_delete(tmp_path):
    db, values, text, old = legacy(tmp_path)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    root = f'/v1/parent/children/{CHILD}/knowledge-sources'
    url = root + '/' + old['source_id'] + '/revision'
    with TestClient(create_app(settings), client=('127.0.0.1', 52201)) as client:
        assert client.get(url).status_code == 401
        headers = parent(client, True)
        duplicate = client.post(root, headers=headers, json={**values, 'content_text': text})
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()['details']['source_id'] == old['source_id']
        assert duplicate.headers['cache-control'] == 'no-store'
        preview = client.post(url+'/preview', headers=headers, json={'content_text': text})
        assert preview.status_code == 200, preview.text
        assert preview.json()['status'] == 'incomplete'
        body = dict(content_text=text, expected_version=preview.json()['source_version'],
            client_action_id='http-repair', confirmation='保留旧稿并创建待审核新版本')
        assert client.post(url, headers=headers, json={**body, 'actor': 'spoof'}).status_code == 422
        assert client.post(url.replace(CHILD, 'child-primary-4'), headers=headers, json=body).status_code == 404
        repaired = client.post(url, headers=headers, json=body)
        assert repaired.status_code == 200, repaired.text
        result = repaired.json()
        new_url = root + '/' + result['current']['source_id'] + '/revision'
        assert result['current']['review_status'] == 'quarantined'
    with TestClient(create_app(settings), client=('127.0.0.1', 52201)) as client:
        headers = parent(client)
        retry = client.post(url, headers=headers, json=body)
        assert retry.status_code == 200, retry.text
        assert retry.json()['receipt'] == result['receipt']
        current = client.get(new_url, headers=headers).json()
        assert current['stored_excerpts'][0]['text'] == text
        reviewed = client.post(new_url+'/review', headers=headers, json={
            'expected_version': current['source_version'], 'client_action_id': 'new-review',
            'confirmation': '已核对新版完整内容与来源'})
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()['current']['review_status'] == 'approved'
        assert reviewed.json()['purpose_permission_inherited'] is False
        retry = client.post(url, headers=headers, json=body)
        assert retry.json()['current']['review_status'] == 'approved'
    with Database(db.path).connect() as c:
        assert c.execute('SELECT COUNT(*) FROM knowledge_source_revision_event').fetchone()[0] == 2
        assert c.execute('SELECT content_text FROM knowledge_excerpt WHERE source_id=?', (old['source_id'],)).fetchone()[0] == text[:1000]
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_repair_read_preview_do_not_allow_nonlocal_access(tmp_path):
    db, _, text, old = legacy(tmp_path)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings), client=('192.0.2.1', 2222)) as client:
        headers = parent(client, True)
        url = f'/v1/parent/children/{CHILD}/knowledge-sources/{old["source_id"]}/revision'
        assert client.get(url, headers=headers).status_code == 403
