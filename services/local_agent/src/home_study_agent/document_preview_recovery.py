"""Resolve one uncertain preview write, or durably prevent its later arrival."""
from hashlib import sha256
import json

from .document_ocr_preview import DocumentOcrPreviewService
from .local_ocr.cache_lifecycle import reject_deleted, request_digest, resolve_owner


class DocumentPreviewRecoveryService:
    def __init__(self, database, workflow):
        self.database = database
        self.workflow = workflow
        with database.connect() as connection:
            # Only opaque deletion-fence digests survive privacy deletion. They
            # contain no frame, OCR body, raw action ID, or family identifiers.
            connection.execute("""CREATE TABLE IF NOT EXISTS document_preview_recovery_fence(
                request_digest TEXT PRIMARY KEY, scope_digest TEXT NOT NULL)""")
            connection.commit()

    @staticmethod
    def _scope_digest(child_id, session_id, generation, lease_id, phase, page_token, inspection_turn_id):
        return sha256(json.dumps([child_id, session_id, generation, lease_id, phase, page_token, inspection_turn_id],
                                 separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _check_job_owner(connection, job, request_id, child_id, session_id, generation, lease_id, phase, page_token,
                         inspection_turn_id):
        if ((job['child_id'], job['session_id'], job['monitor_generation']) != (child_id, session_id, generation)
                or job['parent_artifact_id'] is not None or job['artifact_id'] is not None
                or job['publication_started']):
            raise ValueError('DOCUMENT_PREVIEW_ACTION_CONFLICT')
        # The preview writer supports signed and unsigned frames.
        # Reconstruct both existing owner identities without accepting a
        # different inspection turn, lease, phase, or page as this operation.
        for signature in (None, job['image_hash']):
            if inspection_turn_id is not None or phase != 'homework_capture':
                signature = sha256(json.dumps([signature, inspection_turn_id, phase]).encode()).hexdigest()
            if page_token is not None:
                signature = sha256(json.dumps([signature, page_token]).encode()).hexdigest()
            expected = resolve_owner(connection, request_id, session_id=session_id,
                                     monitor_generation=generation, preview_lease_id=lease_id,
                                     preview_signature=signature)
            if expected['owner_key'] == job['owner_key'] and expected['owner_epoch'] == job['owner_epoch']:
                return
        raise ValueError('DOCUMENT_PREVIEW_ACTION_CONFLICT')

    def resolve(self, session_id, client_action_id, generation, expected_lease_id, expected_phase, page_token=None,
                inspection_turn_id=None):
        request_id = 'document-preview:' + client_action_id
        digest = request_digest(request_id)
        result = dict(client_action_id=client_action_id, generation=generation, phase=expected_phase)
        # This is the same guard as both preview publication boundaries. The
        # immediate transaction makes absence authoritative only while we also
        # install the permanent request tombstone, never from an empty GET.
        with self.database.external_effect_guard(), self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            child_id, lease_id, phase = DocumentOcrPreviewService._capture_scope(
                connection, session_id, generation, expected_lease_id, expected_phase)
            scope = self._scope_digest(child_id, session_id, generation, lease_id, phase, page_token, inspection_turn_id)
            saved = connection.execute('SELECT * FROM document_ocr_preview WHERE client_action_id=?',
                                       (client_action_id,)).fetchone()
            if saved is not None:
                if ((saved['child_id'], saved['session_id'], saved['monitor_generation'], saved['lease_id'],
                     saved['phase'], saved['page_token'], saved['inspection_turn_id']) !=
                        (child_id, session_id, generation, lease_id, phase, page_token, inspection_turn_id)):
                    raise ValueError('DOCUMENT_PREVIEW_ACTION_CONFLICT')
                return dict(status='saved', **result, preview=DocumentOcrPreviewService._readback(saved))

            fence = connection.execute('SELECT scope_digest FROM document_preview_recovery_fence WHERE request_digest=?',
                                       (digest,)).fetchone()
            if fence is not None:
                if fence['scope_digest'] != scope:
                    raise ValueError('DOCUMENT_PREVIEW_ACTION_CONFLICT')
                tombstone = connection.execute('SELECT reason FROM ocr_request_tombstone WHERE request_digest=?',
                                               (digest,)).fetchone()
                if not tombstone or tombstone['reason'] != 'deleted':
                    raise ValueError('DOCUMENT_PREVIEW_SOURCE_CHANGED')
                return dict(status='retired', **result)

            # An unrelated privacy/restore tombstone does not prove ownership.
            reject_deleted(connection, request_id)
            job = connection.execute('SELECT * FROM ocr_job WHERE request_id=?', (request_id,)).fetchone()
            if job is not None:
                self._check_job_owner(connection, job, request_id, child_id, session_id, generation,
                                      lease_id, phase, page_token, inspection_turn_id)
                connection.execute('DELETE FROM ocr_cache WHERE owner_key=? AND image_hash=?',
                                   (job['owner_key'], job['image_hash']))
                connection.execute('DELETE FROM ocr_job WHERE job_id=?', (job['job_id'],))
                # The CPU queue is metadata-only and uses a separate store.
                # Retire its exact reservation before acknowledging resolution.
                # A late recognizer can no longer update its missing OCR row.
                try:
                    self.workflow.adapter.queue.finish('ocr:' + job['job_id'], 'cancelled',
                                                       reason='DOCUMENT_PREVIEW_RETIRED')
                except KeyError:
                    pass  # No reservation exists; the source fence still wins.
            connection.execute("INSERT INTO ocr_request_tombstone VALUES (?, 'deleted')", (digest,))
            connection.execute('INSERT INTO document_preview_recovery_fence VALUES (?, ?)', (digest, scope))
            connection.commit()
            return dict(status='retired', **result)
