"""Direct service-unit restores with original synthetic intent/claim/tails.

This mirrors the production wrapper's original objects; it supplies synthetic
claimed metadata below the HTTP HMAC boundary. No native/Windows/UI credit.
It never invents a receipt, resets a journal, or retries an unknown publication.
"""
from uuid import uuid4
from home_study_agent import native_privacy as native
from home_study_agent import restore_publication as publication
from home_study_agent.privacy_operation_receipt import PrivacyOperationReceipts
from home_study_agent.parent_restore_workflow import failed_before_publication, finish_original_tails


def restore_local(service, backup_id, confirmation, *, original_operations=None):
    database = service.database
    service.initialize()  # Original startup schema initialization; no invented generation.
    receipts = PrivacyOperationReceipts(database)
    operation = {'request_id': 'synthetic-restore-' + uuid4().hex,
                 'expected_generation': receipts.current()['generation'],
                 'cleanup_manifest_sha256': 'c' * 64}
    route = '/v1/parent/backups/restore'
    body = {'backup_id': backup_id, 'confirmation': confirmation, 'operation': operation}
    metadata = {'nonce': uuid4().hex, 'request_id': operation['request_id'],
                'expected_generation': operation['expected_generation'],
                'kind': 'restore', 'child_id': None, 'scope': None,
                'command_sha256': native.command_hash(route, body),
                'cleanup_manifest_sha256': operation['cleanup_manifest_sha256']}
    if original_operations is not None:
        original_operations.append(metadata)
    with publication.publication_owner(database, timeout=0) as owner:
        with database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            native.claim(connection, metadata, route, body, 'restore')
            connection.commit()
        receipt, fresh = receipts.reserve(operation, 'restore', backup_id)
        assert fresh and receipt is None
        try:
            try:
                result = service.restore_backup(backup_id, confirmation,
                             operation_request_id=operation['request_id'])
            except Exception:
                failed_before_publication(database, operation['request_id'])
                raise
        except BaseException:
            if publication.reconcile_publication(owner)['state'] == 'NO_ACTIVE_JOURNAL':
                with database.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    native.finish(connection, metadata, 'failed')
                    connection.commit()
            raise
        result['operation_receipt'] = finish_original_tails(database, owner, metadata)
        return result
