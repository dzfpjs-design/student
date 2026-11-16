"""The actual parent API rejects restore without waiting on socket I/O."""
import time
from privacy_restore_operation import restore_operation

from home_study_agent.database import Database
from test_app import client_for
from synthetic_native_restore_request import KEY
from test_native_privacy_permit_http import certificate, signed_header


def test_restore_http_reports_inflight_submission_without_switching_data(tmp_path):
    with client_for(tmp_path, native_launch_attestation_key=KEY) as client:
        token = client.post('/v1/parent/setup', json={'pin': '4286'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        backup = client.post('/v1/parent/backups', headers=headers).json()
        local_operation = restore_operation(client, headers=headers)
        portable_operation = restore_operation(client, headers=headers)
        def original_signed_post(path, body):
            kind = 'restore' if path == '/v1/parent/backups/restore' else 'portable_restore'
            proof = certificate(path, body, body['operation'], kind=kind, child_id=None, scope=None)
            return client.post(path, headers={**headers, **signed_header(proof)}, json=body)
        database = Database(tmp_path / 'test.db')
        with database.connect() as c:
            c.execute("UPDATE child_profile SET nickname='newer identity' WHERE child_id='child-primary-4'")
            c.commit()
        with database.external_effect_guard():
            started = time.monotonic()
            local = original_signed_post('/v1/parent/backups/restore', {'backup_id': backup['backup_id'], 'confirmation': '确认恢复', 'operation': local_operation})
            portable = original_signed_post('/v1/parent/backups/portable/restore', {'path': str(tmp_path / 'unused.hscbackup'), 'passphrase': 'synthetic safe passphrase',
                      'confirmation': '确认迁移恢复', 'operation': portable_operation})
            assert time.monotonic() - started < 2
        for response in (local, portable):
            assert response.status_code == 409
            assert response.json()['code'] == 'RESTORE_EXTERNAL_EFFECT_BUSY'
            assert '尚未开始恢复' in response.json()['message']
        with database.connect() as c:
            request_ids = (local_operation['request_id'], portable_operation['request_id'])
            assert c.execute('SELECT COUNT(*) FROM privacy_operation_intent WHERE request_id IN (?,?)', request_ids).fetchone()[0] == 0
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone():
                assert c.execute("SELECT COUNT(*) FROM native_privacy_permit WHERE json_extract(metadata_json,'$.request_id') IN (?,?)", request_ids).fetchone()[0] == 0
            assert c.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0] == 'newer identity'
            assert not c.execute("SELECT 1 FROM audit_event WHERE action='backup_restored'").fetchone()
        retried = original_signed_post('/v1/parent/backups/restore', {'backup_id': backup['backup_id'], 'confirmation': '确认恢复', 'operation': local_operation})
        assert retried.status_code == 200
