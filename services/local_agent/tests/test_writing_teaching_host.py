"""Mounted writing services and actual prompt creation, on isolated originals."""
from fastapi.testclient import TestClient
import json
from pathlib import Path
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_companion_activity_host import NoCloud
from test_writing_response_origin import writing, CHILD
from test_writing_teaching import ready


def test_actual_writing_read_resolution_prepare_and_restart(writing):
    ready(writing)
    db, sid, _, original, _ = writing
    settings = Settings(data_dir=db.path.parent, database_path=db.path,
        maintenance_enabled=False, session_token='writing-host-isolated')
    headers = {'X-HSC-Session': settings.session_token}
    params = {'prompt_id': original['prompt_id'], 'item_version': original['item_version']}
    path = f'/v1/sessions/{sid}/writing-teaching'
    saved = None
    for restarted in (False, True):
        with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
            assert client.get(path, params=params).status_code in {401, 403}
            result = client.get(path, params=params, headers=headers)
            assert result.status_code == 200, result.text
            assert result.headers['cache-control'] == 'no-store'
            assert result.json()['status'] == 'feedback_ready'
            assert result.json()['automatic_certification'] is False
            assert result.json()['cloud_transmission_allowed'] is False
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            if restarted:
                assert result.json()['followups'][0]['prompt_id'] == saved['prompt_id']
                continue
            blocked = client.get(path, headers=headers, params={**params, 'item_version': 'stale-version'})
            assert blocked.status_code == 409 and blocked.headers['cache-control'] == 'no-store'
            action = client.post(path + '/next-action', headers=headers, json={**params, 'kind': 'revision'})
            assert action.status_code == 200, action.text
            assert action.json()['status'] == 'ready'
            binding = {'parent_prompt_id': params['prompt_id'], 'parent_item_version': params['item_version'],
                'kind': 'revision', 'binding_version': action.json()['binding_version']}
            body = {'track': 'pet', 'preferred_item_id': original['diagnostic_item_id'],
                'client_action_id': 'writing-host-followup', 'writing_followup': binding}
            prepare_path = f'/v1/sessions/{sid}/diagnostic-prompt/prepare'
            rejected = client.post(prepare_path, headers=headers, json={**body,
                'writing_followup': {**binding, 'binding_version': '0' * 64}})
            assert rejected.status_code == 409, rejected.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            prepared = client.post(prepare_path, headers=headers, json=body)
            assert prepared.status_code == 200, prepared.text
            saved = prepared.json()['prompt']
            assert client.post(prepare_path, headers=headers, json=body).json()['prompt']['prompt_id'] == saved['prompt_id']


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_writing_export_and_scope_erasure_survive_restart(writing, scope):
    service, _, _ = ready(writing)
    db, sid, prompts, parent, _ = writing
    action = service.resolve_next_action(sid, prompt_id=parent['prompt_id'], item_version=parent['item_version'], kind='revision')
    prompts.prepare(sid, track='pet', preferred_item_id=parent['diagnostic_item_id'], client_action_id='privacy-revision',
        writing_followup={'parent_prompt_id':parent['prompt_id'], 'parent_item_version':parent['item_version'],
            'kind':'revision', 'binding_version':action['binding_version']})
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='writing-delete-isolated')
    local = {'X-HSC-Session':settings.session_token}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'}).json()['parent_token']
        exported = client.app.state.privacy_lifecycle.export_child(CHILD)
        content = json.loads(Path(exported['path']).read_text())
        assert len(content['writing_followups']) == 1
        assert any(row['artifact_type'] == 'writing_original' for row in content['capture_artifacts'])
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local,'X-HSC-Parent':token},
            json={'scope':scope,'confirmation':'确认删除','client_action_id':'writing-scope-delete'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'completed'
        assert response.json()['deleted_counts']['writing_followup'] == 1
    with TestClient(create_app(settings, model_gateway=NoCloud())):
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM writing_followup WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE child_id=? AND artifact_type='writing_original'", (CHILD,)).fetchone()[0] == 0
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
