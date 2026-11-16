"""Local operation identities, not a second deletion or restore result store."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4


class PrivacyOperationError(ValueError):
    pass


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS privacy_storage_generation(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation TEXT NOT NULL)''')
    c.execute('INSERT OR IGNORE INTO privacy_storage_generation VALUES(1,?)', (str(uuid4()),))
    c.execute('''CREATE TABLE IF NOT EXISTS privacy_operation_intent(
        request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, child_id TEXT, scope TEXT,
        request_sha256 TEXT NOT NULL, cleanup_manifest_sha256 TEXT NOT NULL,
        origin_generation TEXT NOT NULL, result_generation TEXT,
        source_sha256 TEXT, reference_type TEXT, reference_id TEXT,
        state TEXT NOT NULL, finalized INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


def generation(c):
    return c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()[0]


def preserve_live_identities(live, staged):
    """Backup receipts never become current authorities. Borrowed transactions stay owned."""
    rows = (live.execute('SELECT * FROM privacy_operation_intent').fetchall()
            if live.execute("SELECT 1 FROM sqlite_master WHERE name='privacy_operation_intent'").fetchone() else [])
    initialize(staged)
    staged.execute('DELETE FROM privacy_operation_intent')
    for row in rows:
        staged.execute('INSERT INTO privacy_operation_intent VALUES(' + ','.join('?' for _ in row) + ')', tuple(row))
    staged.execute('UPDATE privacy_storage_generation SET generation=? WHERE singleton=1', (str(uuid4()),))


def record_restore(c, request_id, audit_id):
    if request_id is None:
        return
    changed = c.execute('''UPDATE privacy_operation_intent SET reference_type='audit_event',
        reference_id=?, result_generation=? WHERE request_id=? AND state='processing'
        AND reference_id IS NULL''', (audit_id, generation(c), request_id)).rowcount
    if changed != 1:
        raise PrivacyOperationError('PRIVACY_OPERATION_REQUEST_CONFLICT')


class PrivacyOperationReceipts:
    def __init__(self, database):
        self.database = database

    def current(self):
        with self.database.connect() as c:
            return {'generation': generation(c)}

    def reserve(self, operation, kind, target, *, child_id=None, scope=None, source_sha256=None):
        if operation is None:
            raise PrivacyOperationError('PRIVACY_OPERATION_IDENTITY_REQUIRED')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            result = self._reserve_in_connection(c, operation, kind, target, child_id=child_id, scope=scope, source_sha256=source_sha256)
            if result[1]:
                c.commit()
            return result

    def _reserve_in_connection(self, c, operation, kind, target, *, child_id=None, scope=None, source_sha256=None):
        if self.database._original_connections.get(id(c)) is not c or not c.in_transaction:
            raise PrivacyOperationError('PRIVACY_OPERATION_TRANSACTION_REQUIRED')
        if operation is None:
            raise PrivacyOperationError('PRIVACY_OPERATION_IDENTITY_REQUIRED')
        digest = hashlib.sha256(json.dumps({'operation': operation, 'kind': kind, 'target': target, 'child_id': child_id, 'scope': scope}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        prior = c.execute('SELECT * FROM privacy_operation_intent WHERE request_id=?', (operation['request_id'],)).fetchone()
        if prior:
            if prior['request_sha256'] != digest:
                raise PrivacyOperationError('PRIVACY_OPERATION_REQUEST_CONFLICT')
            return (self._read(c, prior), False)
        if kind == 'delete' and c.execute('SELECT 1 FROM deletion_job WHERE client_action_id=?', (operation['request_id'],)).fetchone():
            raise PrivacyOperationError('PRIVACY_OPERATION_LEGACY_RECEIPT_NOT_UPGRADABLE')
        if generation(c) != operation['expected_generation']:
            raise PrivacyOperationError('PRIVACY_OPERATION_GENERATION_CHANGED')
        if c.execute("SELECT 1 FROM privacy_operation_intent WHERE state='processing' LIMIT 1").fetchone():
            raise PrivacyOperationError('PRIVACY_OPERATION_UNRESOLVED')
        if child_id and (not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone()):
            raise PrivacyOperationError('PRIVACY_OPERATION_OWNER_CHANGED')
        c.execute("INSERT INTO privacy_operation_intent(request_id,kind,child_id,scope,\n                request_sha256,cleanup_manifest_sha256,origin_generation,source_sha256,state)\n                VALUES(?,?,?,?,?,?,?,?,'processing')", (operation['request_id'], kind, child_id, scope, digest, operation['cleanup_manifest_sha256'], generation(c), source_sha256))
        return (None, True)


    def bind_source(self, request_id, source_sha256):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM privacy_operation_intent WHERE request_id=?', (request_id,)).fetchone()
            if not row or row['state'] != 'processing' or row['source_sha256'] not in (None, source_sha256):
                raise PrivacyOperationError('PRIVACY_OPERATION_SOURCE_CHANGED')
            c.execute('UPDATE privacy_operation_intent SET source_sha256=? WHERE request_id=?',
                      (source_sha256, request_id))
            c.commit()

    def finish(self, request_id, *, deletion_id=None):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if deletion_id:
                job = c.execute('SELECT * FROM deletion_job WHERE deletion_id=?', (deletion_id,)).fetchone()
                row = c.execute('SELECT * FROM privacy_operation_intent WHERE request_id=?', (request_id,)).fetchone()
                if (not job or not row or job['client_action_id'] != request_id
                        or job['child_id'] != row['child_id'] or job['scope'] != row['scope']
                        or job['target_type'] != 'child_scope' or job['status'] != 'completed'
                        or row['origin_generation'] != generation(c)):
                    raise PrivacyOperationError('PRIVACY_OPERATION_RECEIPT_CHANGED')
                c.execute('''UPDATE privacy_operation_intent SET reference_type='deletion_job',
                    reference_id=?, result_generation=?,state='processing' WHERE request_id=? AND state IN ('processing','failed') ''',
                    (deletion_id, generation(c), request_id))
            changed = c.execute('''UPDATE privacy_operation_intent SET state='finished',finalized=1
                WHERE request_id=? AND state='processing' AND reference_id IS NOT NULL''', (request_id,)).rowcount
            if changed != 1:
                raise PrivacyOperationError('PRIVACY_OPERATION_RECEIPT_CHANGED')
            c.commit()
        return self.read(request_id)

    def deletion_retry_identity(self, deletion_id):
        with self.database.connect() as c:
            row = c.execute('''SELECT p.* FROM privacy_operation_intent p JOIN deletion_job d
                ON d.client_action_id=p.request_id AND d.child_id=p.child_id AND d.scope=p.scope
                WHERE d.deletion_id=? AND d.target_type='child_scope' AND p.kind='delete' ''', (deletion_id,)).fetchone()
            if not row or row['origin_generation'] != generation(c):
                return None
            return dict(row)

    def finalization_target(self, request_id):
        with self.database.connect() as c:
            row = c.execute('''SELECT p.*,d.deletion_id FROM privacy_operation_intent p JOIN deletion_job d
                ON d.client_action_id=p.request_id AND d.child_id=p.child_id AND d.scope=p.scope
                WHERE p.request_id=? AND p.kind='delete' AND d.target_type='child_scope'
                AND d.status='completed' ''', (request_id,)).fetchone()
            if not row or row['origin_generation'] != generation(c):
                raise PrivacyOperationError('PRIVACY_OPERATION_FINALIZATION_NOT_VERIFIED')
            return dict(row)

    def failed(self, request_id):
        with self.database.connect() as c:
            # A success audit without completed route tails remains unknown, not rolled back.
            c.execute("UPDATE privacy_operation_intent SET state='failed' WHERE request_id=? AND reference_id IS NULL",
                      (request_id,))
            c.commit()

    def read(self, request_id):
        with self.database.connect() as c:
            row = c.execute('SELECT * FROM privacy_operation_intent WHERE request_id=?', (request_id,)).fetchone()
            if not row:
                raise KeyError(request_id)
            return self._read(c, row)

    @staticmethod
    def _read(c, row):
        proof = False
        reference = None
        if row['reference_type'] == 'audit_event':
            audit = c.execute('SELECT * FROM audit_event WHERE audit_id=?', (row['reference_id'],)).fetchone()
            if audit:
                try:
                    summary = json.loads(audit['summary_json'])
                except (ValueError, TypeError):
                    summary = None
                proof = (isinstance(summary, dict) and summary.get('privacy_operation_request_id') == row['request_id']
                         and audit['action'] == {'restore': 'backup_restored', 'portable_restore': 'portable_backup_restored'}.get(row['kind'])
                         and audit['actor'] == 'parent')
        elif row['reference_type'] == 'deletion_job':
            job = c.execute('SELECT * FROM deletion_job WHERE deletion_id=?', (row['reference_id'],)).fetchone()
            proof = bool(job and job['client_action_id'] == row['request_id'] and job['child_id'] == row['child_id']
                         and job['scope'] == row['scope'] and job['target_type'] == 'child_scope'
                         and job['status'] == 'completed')
        completed_job = None
        if row['kind'] == 'delete':
            completed_job = c.execute('''SELECT deletion_id,status FROM deletion_job WHERE client_action_id=? AND child_id=?
                AND scope=? AND target_type='child_scope' ''', (row['request_id'], row['child_id'], row['scope'])).fetchone()
        current = generation(c)
        status = 'unknown'
        if (completed_job and completed_job['status'] == 'completed' and row['finalized'] == 0
                and row['origin_generation'] == current):
            status = 'finalization_required'
        elif row['state'] == 'failed':
            status = 'failed'
        elif row['finalized'] == 1 and row['result_generation'] != current:
            status = 'superseded'
        elif proof and row['finalized'] == 1:
            status = 'committed'
        if proof:
            reference = {'type': row['reference_type'], 'id': row['reference_id']}
        elif completed_job:
            reference = {'type': 'deletion_job', 'id': completed_job['deletion_id']}
        return {'request_id': row['request_id'], 'kind': row['kind'], 'child_id': row['child_id'], 'scope': row['scope'],
                'origin_generation': row['origin_generation'], 'result_generation': row['result_generation'],
                'current_generation': current, 'cleanup_manifest_sha256': row['cleanup_manifest_sha256'],
                'status': status, 'cleanup_allowed': status == 'committed', 'reference': reference,
                'local_only': True, 'cloud_transmission_allowed': False}
