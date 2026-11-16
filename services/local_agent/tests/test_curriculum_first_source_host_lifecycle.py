"""Actual first-source HTTP publication and host lifecycle, synthetic data only."""
import json
from pathlib import Path

import pytest

from privacy_restore_operation import restore_operation
from synthetic_native_restore_request import KEY, signed_restore_post
from test_curriculum_first_source_http import CHILD, BASE, first_source_fixture, auth, publish
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_pet_reviewed_source_actual_http import NoCloud


def app_client(tmp_path, db, *, native_launch_attestation_key=None):
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token='isolated-first-source', data_encryption_key=b'f' * 32,
                        native_launch_attestation_key=native_launch_attestation_key)
    return TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41200),
                      headers={'X-HSC-Session':settings.session_token})


@pytest.mark.parametrize('scope', ['source', 'learning', 'knowledge', 'all_history', 'reports', 'media'])
def test_first_source_export_delete_restart_preserves_scope(tmp_path, scope):
    db, source, spec, _ = first_source_fixture(tmp_path)
    with app_client(tmp_path, db) as client:
        auth(client)
        packet = publish(client, spec)
        exported = client.post(f'/v1/parent/children/{CHILD}/export')
        assert exported.status_code == 200, exported.text
        data = json.loads(Path(exported.json()['path']).read_text())
        assert data['curriculum_first_sources'][0]['candidate_id'] == packet['candidate_id']
        assert len(data['curriculum_first_source_events']) == 4
        other_before = client.get('/v1/parent/children/child-primary-4/deletion-preview').json()
        body = dict(confirmation='确认删除', client_action_id='delete-first-'+scope)
        route = (f'/v1/parent/children/{CHILD}/knowledge-sources/{source["source_id"]}/delete'
                 if scope == 'source' else f'/v1/parent/children/{CHILD}/delete')
        if scope != 'source':
            body['scope'] = scope
        deleted = client.post(route, json=body)
        assert deleted.status_code == 200, deleted.text
        assert client.get('/v1/parent/children/child-primary-4/deletion-preview').json() == other_before
    with app_client(tmp_path, db) as restarted:
        auth(restarted, restart=True)
        current = restarted.get(BASE+'/'+packet['candidate_id'])
        if scope in {'source', 'learning', 'knowledge', 'all_history'}:
            assert current.status_code == 404, current.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM curriculum_first_source WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
                assert c.execute('SELECT COUNT(*) FROM curriculum_first_source_event WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
                item = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (packet['diagnostic_item_id'],)).fetchone()
                assert item is None or item['status'] != 'active'
        else:
            assert current.status_code == 200 and current.json()['eligible'], current.text


@pytest.mark.parametrize('portable', [False, True])
def test_actual_backup_first_source_quarantine_and_new_review(tmp_path, portable):
    db, _, spec, _ = first_source_fixture(tmp_path)
    with app_client(tmp_path, db, native_launch_attestation_key=KEY if not portable else None) as client:
        auth(client)
        packet = publish(client, spec)
        cid = packet['candidate_id']
        backup = client.post('/v1/parent/backups/portable' if portable else '/v1/parent/backups',
                             json={'passphrase':'synthetic-first-source-backup'} if portable else None)
        assert backup.status_code == 200, backup.text
        body = ({'path':backup.json()['path'], 'passphrase':'synthetic-first-source-backup'} if portable
                else {'backup_id':backup.json()['backup_id']})
        values = {**body, 'confirmation':'确认迁移恢复' if portable else '确认恢复', 'operation':restore_operation(client)}
        if portable:
            response = client.post('/v1/parent/backups/portable/restore', json=values)
        else:
            response = signed_restore_post(client, headers={}, json=values)
        assert response.status_code == 200, response.text
        auth(client, restart=True)
        restored = client.get(BASE+'/'+cid).json()
        assert restored['state'] == 'blocked' and not restored['eligible'], restored
        replay = client.post(BASE+'/'+cid+'/publish', json={'action_id':'first-publish','expected_version':3})
        assert replay.status_code == 200 and replay.json()['state'] == 'blocked', replay.text
        for operation in ('validate', 'review', 'publish'):
            command = {'action_id':'after-restore-'+operation, 'expected_version':restored['version']}
            if operation == 'review':
                command.update(decision='approve', note='恢复后重新核对相同原题与课标')
            response = client.post(BASE+'/'+cid+'/'+operation, json=command)
            assert response.status_code == 200, response.text
            restored = response.json()
        assert restored['eligible'] and restored['diagnostic_item_id'] == packet['diagnostic_item_id']
    with app_client(tmp_path, db) as restarted:
        auth(restarted, restart=True)
        assert restarted.get(BASE+'/'+cid).json()['eligible']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_item WHERE diagnostic_item_id=?', (packet['diagnostic_item_id'],)).fetchone()[0] == 1
