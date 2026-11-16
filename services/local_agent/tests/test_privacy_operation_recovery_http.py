"""Exact privacy receipts survive lost HTTP responses without repeating mutations."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from fastapi.responses import JSONResponse
from home_study_agent.database import Database
from test_app import client_for


def auth(client, setup=False):
    result = client.post('/v1/parent/' + ('setup' if setup else 'unlock'), json={'pin': '4286'})
    assert result.status_code == 200, result.text
    return {'X-HSC-Parent': result.json()['parent_token']}


def identity(client, headers):
    response = client.get('/v1/parent/privacy-operations/generation', headers=headers)
    assert response.status_code == 200, response.text
    return {'request_id': str(uuid4()), 'expected_generation': response.json()['generation'],
            'cleanup_manifest_sha256': hashlib.sha256(b'isolated exact local keys').hexdigest()}


def read(client, headers, operation):
    response = client.get('/v1/parent/privacy-operations/' + operation['request_id'], headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def test_local_restore_lost_response_new_app_reads_original_receipt(tmp_path):
    client = client_for(tmp_path,native_launch_attestation_key=KEY)

    @client.app.middleware('http')
    async def drop_committed_response(request, call_next):
        response = await call_next(request)
        if request.url.path == '/v1/parent/backups/restore' and response.status_code == 200:
            return JSONResponse({'isolated_transport_loss': True}, status_code=503)
        return response

    with client:
        headers = auth(client, True)
        backup = client.post('/v1/parent/backups', headers=headers).json()
        operation = identity(client, headers)
        payload = {'backup_id': backup['backup_id'], 'confirmation': '确认恢复', 'operation': operation}
        assert signed_local_restore_post(client, headers=headers, json=payload).status_code == 503
    with client_for(tmp_path,native_launch_attestation_key=KEY) as restarted:
        headers = auth(restarted)
        receipt = read(restarted, headers, operation)
        assert receipt['status'] == 'committed'
        assert receipt['cleanup_allowed'] is True
        assert receipt['origin_generation'] == operation['expected_generation']
        assert receipt['result_generation'] != operation['expected_generation']
        assert receipt['cleanup_manifest_sha256'] == operation['cleanup_manifest_sha256']
        assert receipt['reference']['type'] == 'audit_event'
        with Database(tmp_path / 'test.db').connect() as c:
            before = c.execute("SELECT COUNT(*) FROM audit_event WHERE action='backup_restored'").fetchone()[0]
        replay = signed_local_restore_post(restarted, headers=headers, json=payload)
        # A consumed native operation cannot be POSTed again after generation changes.
        # The original read-only receipt remains the recovery result.
        assert replay.status_code == 409, replay.text
        assert replay.json()['detail']['code'] == 'NATIVE_PRIVACY_GENERATION_CHANGED'
        assert read(restarted, headers, operation) == receipt
        with Database(tmp_path / 'test.db').connect() as c:
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='backup_restored'").fetchone()[0] == before


def test_restore_rollback_never_authorizes_cleanup_and_legacy_requires_identity(tmp_path, monkeypatch):
    with client_for(tmp_path,native_launch_attestation_key=KEY,raise_server_exceptions=False) as client:
        headers = auth(client, True)
        backup = client.post('/v1/parent/backups', headers=headers).json()
        operation = identity(client, headers)
        original = client.app.state.privacy_lifecycle._audit

        def failing_audit(c, actor, action, child, summary, at):
            if action == 'backup_restored':
                raise RuntimeError('isolated audit rollback')
            return original(c, actor, action, child, summary, at)

        monkeypatch.setattr(client.app.state.privacy_lifecycle, '_audit', failing_audit)
        payload = {'backup_id': backup['backup_id'], 'confirmation': '确认恢复', 'operation': operation}
        proofs=[]
        failed = signed_local_restore_post(client, headers=headers, json=payload,proof_out=proofs)
        assert failed.status_code == 500, failed.text
        assert failed.json()['code']=='RESTORE_OPERATION_INTERRUPTED'
        assert failed.json()['retryable']is False and failed.json()['recovery_action']=='review_original_operation'
        state=client.get('/v1/parent/recovery-status',headers=headers)
        assert state.status_code==200 and state.json()['state']=='UNAPPLIED',state.text
        assert len(proofs)==1
        fenced=client.post('/v1/parent/native-privacy-permits/fence',headers=headers,json=proofs[0])
        assert fenced.status_code==200 and fenced.json()['status']=='failed',fenced.text
    with client_for(tmp_path,native_launch_attestation_key=KEY) as restarted:
        headers = auth(restarted)
        receipt = read(restarted, headers, operation)
        assert receipt['cleanup_allowed'] is False
        assert receipt['status'] in {'failed', 'unknown'}
        legacy = signed_local_restore_post(restarted, headers=headers, operation=operation,
                                json={'backup_id': backup['backup_id'], 'confirmation': '确认恢复'})
        assert legacy.status_code == 409
        assert legacy.json()['code'] == 'PRIVACY_OPERATION_IDENTITY_REQUIRED'


def test_delete_receipt_is_exact_and_old_backup_cannot_reactivate_generation(tmp_path):
    with client_for(tmp_path,native_launch_attestation_key=KEY) as client:
        headers = auth(client, True)
        backup = client.post('/v1/parent/backups', headers=headers).json()
        operation = identity(client, headers)
        payload = {'scope': 'reports', 'confirmation': '确认删除',
                   'client_action_id': operation['request_id'], 'operation': operation}
        deleted = client.post('/v1/parent/children/child-primary-4/delete', headers=headers, json=payload)
        assert deleted.status_code == 200, deleted.text
        receipt = read(client, headers, operation)
        assert receipt['cleanup_allowed'] is True
        assert receipt['child_id'] == 'child-primary-4'
        assert receipt['reference'] == {'type': 'deletion_job', 'id': deleted.json()['deletion_id']}
        conflict = client.post('/v1/parent/children/child-junior-3/delete', headers=headers, json=payload)
        assert conflict.status_code == 409
        assert conflict.json()['code'] == 'DELETION_ACTION_CONFLICT'
        with Database(tmp_path / 'test.db').connect() as c:
            jobs = [dict(r) for r in c.execute('SELECT * FROM deletion_job')]
            assert len(jobs) == 1 and jobs[0]['child_id'] == 'child-primary-4'
            assert jobs[0]['client_action_id'] == operation['request_id']
        # Deletion correctly invalidates every earlier managed backup.
        backup = client.post('/v1/parent/backups', headers=headers).json()
        restore = identity(client, headers)
        assert signed_local_restore_post(client, headers=headers,
                           json={'backup_id': backup['backup_id'], 'confirmation': '确认恢复',
                                 'operation': restore}).status_code == 200
    with client_for(tmp_path,native_launch_attestation_key=KEY) as restarted:
        headers = auth(restarted)
        receipt = read(restarted, headers, operation)
        assert receipt['status'] == 'superseded'
        assert receipt['cleanup_allowed'] is False
        assert restarted.get('/v1/parent/privacy-operations/' + operation['request_id']).status_code in {401, 403}


def test_lost_delete_response_recovers_only_original_job_after_restart(tmp_path):
    client = client_for(tmp_path)

    @client.app.middleware('http')
    async def lose_delete(request, call_next):
        response = await call_next(request)
        if request.url.path.endswith('/child-primary-4/delete') and response.status_code == 200:
            return JSONResponse({'isolated_transport_loss': True}, status_code=503)
        return response

    with client:
        headers = auth(client, True)
        operation = identity(client, headers)
        response = client.post('/v1/parent/children/child-primary-4/delete', headers=headers,
            json={'scope': 'reports', 'confirmation': '确认删除', 'client_action_id': operation['request_id'], 'operation': operation})
        assert response.status_code == 503
    with client_for(tmp_path) as restarted:
        headers = auth(restarted)
        with Database(tmp_path / 'test.db').connect() as c:
            before = c.execute('SELECT COUNT(*) FROM deletion_job').fetchone()[0]
        receipt = read(restarted, headers, operation)
        assert receipt['cleanup_allowed'] is True
        with Database(tmp_path / 'test.db').connect() as c:
            assert c.execute('SELECT COUNT(*) FROM deletion_job').fetchone()[0] == before == 1
            job = c.execute('SELECT * FROM deletion_job WHERE deletion_id=?', (receipt['reference']['id'],)).fetchone()
            assert job['client_action_id'] == operation['request_id']
        assert restarted.get('/v1/parent/privacy-operations/unknown-original-key', headers=headers).status_code == 404


def test_portable_restore_uses_exact_audit_and_a_later_generation_cannot_clean(tmp_path):
    with client_for(tmp_path) as client:
        headers = auth(client, True)
        portable = client.post('/v1/parent/backups/portable', headers=headers,
                              json={'passphrase': 'Synthetic isolated passphrase'}).json()
        operation = identity(client, headers)
        payload = {'path': portable['path'], 'passphrase': 'Synthetic isolated passphrase',
                   'confirmation': '确认迁移恢复', 'operation': operation}
        response = client.post('/v1/parent/backups/portable/restore', headers=headers, json=payload)
        assert response.status_code == 200, response.text
        first = read(client, headers, operation)
        assert first['cleanup_allowed'] is True
        next_operation = identity(client, headers)
        changed = client.post('/v1/parent/backups/portable/restore', headers=headers,
                              json={**payload, 'operation': next_operation})
        assert changed.status_code == 200, changed.text
        prior = read(client, headers, operation)
        assert prior['status'] == 'superseded' and not prior['cleanup_allowed']
        stale = {**next_operation, 'request_id': str(uuid4()), 'expected_generation': first['result_generation']}
        refused = client.post('/v1/parent/backups/portable/restore', headers=headers, json={**payload, 'operation': stale})
        assert refused.status_code == 409
        assert refused.json()['code'] == 'PRIVACY_OPERATION_GENERATION_CHANGED'
        with Database(tmp_path / 'test.db').connect() as c:
            rows = c.execute('SELECT * FROM privacy_operation_intent').fetchall()
            assert len(rows) == 2
            assert all('passphrase' not in str(dict(r)) and portable['path'] not in str(dict(r)) for r in rows)


def test_concurrent_original_request_has_one_deletion_and_legacy_receipt_is_not_upgraded(tmp_path):
    with client_for(tmp_path) as client:
        headers = auth(client, True)
        operation = identity(client, headers)
        payload = {'scope': 'reports', 'confirmation': '确认删除',
                   'client_action_id': operation['request_id'], 'operation': operation}
        def send():
            return client.post('/v1/parent/children/child-primary-4/delete', headers=headers, json=payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: send(), range(2)))
        assert [r.status_code for r in responses] == [200, 200]
        assert all(r.json()['operation_receipt']['request_id'] == operation['request_id'] for r in responses)
        with Database(tmp_path / 'test.db').connect() as c:
            assert c.execute('SELECT COUNT(*) FROM deletion_job').fetchone()[0] == 1
        legacy_key = str(uuid4())
        legacy = client.post('/v1/parent/children/child-primary-4/delete', headers=headers,
                             json={'scope': 'reports', 'confirmation': '确认删除', 'client_action_id': legacy_key})
        assert legacy.status_code == 200 and 'operation_receipt' not in legacy.json()
        fake_upgrade = {**identity(client, headers), 'request_id': legacy_key}
        refused = client.post('/v1/parent/children/child-primary-4/delete', headers=headers,
                              json={**payload, 'client_action_id': legacy_key, 'operation': fake_upgrade})
        assert refused.status_code == 409
        assert refused.json()['code'] == 'PRIVACY_OPERATION_LEGACY_RECEIPT_NOT_UPGRADABLE'


def _isolated_crash_before_restore_audit():
    from pathlib import Path
    args = json.loads(sys.stdin.read())
    with client_for(Path(args['root']),native_launch_attestation_key=KEY) as client:
        headers = auth(client)
        original = client.app.state.privacy_lifecycle._audit
        def crash(c, actor, action, child, summary, at):
            if action == 'backup_restored':
                os._exit(37)
            return original(c, actor, action, child, summary, at)
        client.app.state.privacy_lifecycle._audit = crash
        from test_native_privacy_permit_http import certificate, signed_header
        path = '/v1/parent/backups/restore'
        proof = certificate(path, args['payload'], args['payload']['operation'], kind='restore', child_id=None, scope=None)
        # Synthetic native journal: retain the exact originally issued proof before dispatch.
        Path(args['native_proof_path']).write_text(json.dumps(proof))
        client.post(path, headers={**headers, **signed_header(proof)}, json=args['payload'])


def test_prepublication_audit_crash_keeps_live_data_without_guessing_commit(tmp_path):
    with client_for(tmp_path,native_launch_attestation_key=KEY) as client:
        headers = auth(client, True)
        backup = client.post('/v1/parent/backups', headers=headers).json()
        operation = identity(client, headers)
        with Database(tmp_path / 'test.db').connect() as c:
            old_name = c.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0]
            c.execute("UPDATE child_profile SET nickname='new synthetic nickname' WHERE child_id='child-primary-4'")
            c.commit()
    payload = {'backup_id': backup['backup_id'], 'confirmation': '确认恢复', 'operation': operation}
    crashed = subprocess.run([sys.executable, '-c',
        'from test_privacy_operation_recovery_http import _isolated_crash_before_restore_audit; _isolated_crash_before_restore_audit()'],
        input=json.dumps({'root': str(tmp_path), 'payload': payload, 'native_proof_path': str(tmp_path / 'original-native-proof.json')}), text=True, capture_output=True, timeout=90,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.pathsep.join((
            str(Path(__file__).resolve().parent),
            str(Path(__file__).resolve().parents[1] / 'src'),
            os.environ.get('PYTHONPATH', ''),
        ))})
    assert crashed.returncode == 37, crashed.stderr[-1000:]
    with client_for(tmp_path,native_launch_attestation_key=KEY) as client:
        headers = auth(client)
        receipt = read(client, headers, operation)
        assert receipt['status'] == 'unknown' and receipt['cleanup_allowed'] is False
        assert receipt['reference'] is None
        # Audit is staged before atomic publication: this crash cannot replace live data.
        assert receipt['current_generation'] == operation['expected_generation']
        state = client.get('/v1/parent/recovery-status', headers=headers)
        assert state.status_code == 200 and state.json()['state'] == 'UNAPPLIED', state.text
        proof = json.loads((tmp_path / 'original-native-proof.json').read_text())
        assert proof['request_id'] == operation['request_id'] and proof['kind'] == 'restore'
        resolved = client.post('/v1/parent/native-privacy-permits/fence', headers=headers, json=proof)
        assert resolved.status_code == 200 and resolved.json()['status'] == 'failed', resolved.text
        failed_receipt = read(client, headers, operation)
        assert failed_receipt['status'] == 'failed' and failed_receipt['cleanup_allowed'] is False
        state = client.get('/v1/parent/recovery-status', headers=headers)
        assert state.status_code == 200 and state.json()['state'] == 'RESOLVED_NOT_APPLIED', state.text
    # A real restart follows the parent's original-operation resolution.
    with client_for(tmp_path,native_launch_attestation_key=KEY) as client:
        headers = auth(client)
        with Database(tmp_path / 'test.db').connect() as c:
            assert c.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0] == 'new synthetic nickname'
            assert not c.execute("SELECT 1 FROM audit_event WHERE action='backup_restored'").fetchone()
        retry = signed_local_restore_post(client, headers=headers, json=payload)
        assert retry.status_code == 409
        assert retry.json()['code'] == 'PRIVACY_OPERATION_NOT_APPLIED_AGAIN'


def test_explicit_existing_deletion_job_retry_completes_original_cleanup_receipt(tmp_path, monkeypatch):
    from home_study_agent.privacy_lifecycle import DeletionIncompleteError
    with client_for(tmp_path) as client:
        headers = auth(client, True)
        operation = identity(client, headers)
        service = client.app.state.privacy_lifecycle
        original = service._finish_deletion_job
        def fail_final_files(deletion_id):
            service._mark_deletion_failed(deletion_id, 'DELETION_FILES_PENDING', 'Synthetic unfinished file verification')
            raise DeletionIncompleteError(deletion_id, 'DELETION_FILES_PENDING', 1)
        monkeypatch.setattr(service, '_finish_deletion_job', fail_final_files)
        failed = client.post('/v1/parent/children/child-primary-4/delete', headers=headers,
            json={'scope': 'reports', 'confirmation': '确认删除', 'client_action_id': operation['request_id'], 'operation': operation})
        assert failed.status_code == 500, failed.text
        assert failed.json()['recovery_action'] == 'retry_deletion'
        pending = read(client, headers, operation)
        assert pending['cleanup_allowed'] is False
        deletion_id = pending['reference']['id']
        monkeypatch.setattr(service, '_finish_deletion_job', original)
        # A parent explicitly uses the existing retry route; GET never runs this mutation.
        retried = client.post('/v1/parent/deletions/' + deletion_id + '/retry', headers=headers, json={'confirmation': '确认删除'})
        assert retried.status_code == 200, retried.text
        receipt = read(client, headers, operation)
        assert receipt['cleanup_allowed'] is True
        assert receipt['reference']['id'] == deletion_id
        with Database(tmp_path / 'test.db').connect() as c:
            assert c.execute('SELECT COUNT(*) FROM deletion_job').fetchone()[0] == 1


def test_completed_deletion_finish_failure_is_visible_and_retry_only_finalizes_original_job(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.privacy_operation_receipt import PrivacyOperationReceipts
    app = client_for(tmp_path).app
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = auth(client, True)
        operation = identity(client, headers)
        def broken_finish(*args, **kwargs):
            raise RuntimeError('Synthetic receipt commit failure')
        with monkeypatch.context() as patch:
            patch.setattr(PrivacyOperationReceipts, 'finish', broken_finish)
            result = client.post('/v1/parent/children/child-primary-4/delete', headers=headers,
                json={'scope': 'reports', 'confirmation': '确认删除',
                      'client_action_id': operation['request_id'], 'operation': operation})
            assert result.status_code == 500
        with Database(tmp_path / 'test.db').connect() as c:
            job = c.execute('SELECT * FROM deletion_job WHERE client_action_id=?', (operation['request_id'],)).fetchone()
            assert job['status'] == 'completed'
            original_job = dict(job)
    with client_for(tmp_path) as restarted:
        headers = auth(restarted)
        receipt = read(restarted, headers, operation)
        assert receipt['status'] == 'finalization_required'
        assert receipt['cleanup_allowed'] is False
        assert receipt['reference'] == {'type': 'deletion_job', 'id': original_job['deletion_id']}
        def forbid_redelete(*args, **kwargs):
            raise AssertionError('A completed deletion may not execute again')
        monkeypatch.setattr(restarted.app.state.privacy_lifecycle, '_execute_child_scope_deletion', forbid_redelete)
        monkeypatch.setattr(restarted.app.state.privacy_lifecycle, 'retry_deletion_job', forbid_redelete)
        retried = restarted.post('/v1/parent/privacy-operations/' + operation['request_id'] + '/finalize', headers=headers)
        assert retried.status_code == 200, retried.text
        assert read(restarted, headers, operation)['cleanup_allowed'] is True
        with Database(tmp_path / 'test.db').connect() as c:
            current = dict(c.execute('SELECT * FROM deletion_job WHERE deletion_id=?', (original_job['deletion_id'],)).fetchone())
            assert current == original_job
            assert c.execute('SELECT COUNT(*) FROM deletion_job').fetchone()[0] == 1
        replay = restarted.post('/v1/parent/privacy-operations/' + operation['request_id'] + '/finalize', headers=headers)
        assert replay.status_code == 200 and replay.json() == retried.json()
        assert restarted.post('/v1/parent/privacy-operations/unknown-original/finalize', headers=headers).status_code == 409
