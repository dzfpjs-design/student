"""Restored in-progress backup copies are history, never new deletion authority."""
import pytest
from home_study_agent.database import Database
from home_study_agent.privacy_operation_receipt import PrivacyOperationError
from test_native_privacy_durability_http import client_for
from test_native_privacy_permit_http import certificate, signed_header
from test_privacy_operation_recovery_http import auth, identity
from test_learning_runtime_http import NoCloud


@pytest.mark.parametrize('scope', ['all_history', 'reports'])
def test_post_deletion_backup_history_cannot_retry_current_generation(tmp_path, scope):
    db = Database(tmp_path / 'test.db')
    child = 'child-primary-4'
    with client_for(tmp_path, NoCloud()) as client:
        headers = auth(client, True)
        operation = identity(client, headers)
        path = '/v1/parent/children/' + child + '/delete'
        body = {'scope': scope, 'confirmation': '确认删除',
                'client_action_id': operation['request_id'], 'operation': operation}
        proof = certificate(path, body, operation, child_id=child, scope=scope)
        response = client.post(path, headers={**headers, **signed_header(proof)}, json=body)
        assert response.status_code == 200, response.text
        deletion_id = response.json()['deletion_id']
        receipt_path = '/v1/parent/privacy-operations/' + operation['request_id']
        assert client.get(receipt_path, headers=headers).json()['status'] == 'committed'
        backups = client.get('/v1/parent/backups', headers=headers).json()['items']
        backup = next(b for b in backups if b['reason'] == 'post_deletion')
        # Use the actual rotation backup, not a later hand-made completed snapshot.
        restore = identity(client, headers)
        restore_path = '/v1/parent/backups/restore'
        values = {'backup_id': backup['backup_id'], 'confirmation': '确认恢复', 'operation': restore}
        permit = certificate(restore_path, values, restore, kind='restore', child_id=None, scope=None)
        result = client.post(restore_path, headers={**headers, **signed_header(permit)}, json=values)
        assert result.status_code == 200, result.text
    with client_for(tmp_path, NoCloud()) as client:
        headers = auth(client)
        receipt = client.get(receipt_path, headers=headers)
        assert receipt.status_code == 200 and receipt.headers['cache-control'] == 'no-store'
        assert receipt.json()['status'] == 'superseded'
        assert receipt.json()['cleanup_allowed'] is False
        assert client.get(receipt_path).status_code in {401, 403}
        health = client.get('/v1/parent/system-health', headers=headers).json()
        row = next(r for r in health['recent_deletion_failures'] if r['deletion_id'] == deletion_id)
        assert row.get('retry_allowed') is False, row
        assert row.get('historical') is True and '请重试' not in row['error_message']
        with db.connect() as c:
            before = dict(c.execute('SELECT * FROM deletion_job WHERE deletion_id=?', (deletion_id,)).fetchone())
            audits = c.execute("SELECT COUNT(*) FROM audit_event WHERE action='child_data_deleted'").fetchone()[0]
            operations = [tuple(r) for r in c.execute('SELECT * FROM privacy_operation_intent ORDER BY request_id')]
        # Service callers cannot bypass the generation guard, even without native scope.
        with pytest.raises(PrivacyOperationError, match='PRIVACY_OPERATION_GENERATION_CHANGED'):
            client.app.state.privacy_lifecycle.retry_deletion_job(deletion_id, '确认删除')
        retry = client.post('/v1/parent/deletions/' + deletion_id + '/retry', headers=headers,
                            json={'confirmation': '确认删除'})
        assert retry.status_code == 409 and retry.headers['cache-control'] == 'no-store', retry.text
        if scope == 'all_history':
            context = client.get('/v1/parent/deletions/' + deletion_id + '/native-context', headers=headers)
            assert context.status_code == 409, context.text
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM deletion_job WHERE deletion_id=?', (deletion_id,)).fetchone()) == before
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='child_data_deleted'").fetchone()[0] == audits
            assert [tuple(r) for r in c.execute('SELECT * FROM privacy_operation_intent ORDER BY request_id')] == operations
