"""Read-only delivery recovery; intermediate OCR rows are not completion receipts."""
import hashlib

from .document_monitoring import DocumentMonitoringService
from .repository import utc_now


class DocumentOperationReceipts:
    def __init__(self, database):
        self.database = database
        with database.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS document_operation_receipt (
                client_action_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                operation TEXT NOT NULL CHECK(operation IN ('ocr','revision')),
                artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                revision_id TEXT REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
                content_sha256 TEXT NOT NULL, completed_at TEXT NOT NULL,
                CHECK((operation='ocr' AND revision_id IS NULL) OR (operation='revision' AND revision_id IS NOT NULL)))''')
            c.execute('''CREATE TABLE IF NOT EXISTS document_operation_delivery (
                client_action_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                operation TEXT NOT NULL CHECK(operation IN ('ocr','revision')),
                acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),
                prepared_at TEXT NOT NULL)''')
            c.commit()

    def prepare(self, session_id, action_id, operation):
        self.read(session_id, action_id, operation)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            session = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if not session:
                raise ValueError('DOCUMENT_RECEIPT_SESSION_MISSING')
            identity = (session_id, session['child_id'], operation)
            old = c.execute('SELECT session_id,child_id,operation FROM document_operation_delivery WHERE client_action_id=?', (action_id,)).fetchone()
            if old and tuple(old) != identity:
                raise ValueError('DOCUMENT_RECEIPT_IDENTITY_CONFLICT')
            if not old:
                if c.execute('SELECT count(*) FROM document_operation_delivery WHERE session_id=? AND acknowledged=0', (session_id,)).fetchone()[0] >= 128:
                    raise ValueError('DOCUMENT_RECOVERY_QUEUE_FULL')
                c.execute('INSERT INTO document_operation_delivery VALUES (?,?,?,?,0,?)', (action_id, *identity, utc_now()))
            c.commit()
        return dict(session_id=session_id, client_action_id=action_id, operation=operation, status='prepared')

    def pending(self, session_id):
        with self.database.connect() as c:
            rows = c.execute('''SELECT d.client_action_id,d.operation FROM document_operation_delivery d
                JOIN daily_session s USING(session_id) WHERE d.session_id=? AND d.child_id=s.child_id
                AND d.acknowledged=0 ORDER BY d.prepared_at,d.client_action_id LIMIT 128''', (session_id,)).fetchall()
        return [self.read(session_id, row['client_action_id'], row['operation']) for row in rows]

    def acknowledge(self, session_id, action_id, operation):
        receipt = self.read(session_id, action_id, operation)
        if receipt['status'] != 'saved':
            raise ValueError('DOCUMENT_RECEIPT_NOT_CONFIRMED')
        with self.database.connect() as c:
            c.execute('''UPDATE document_operation_delivery SET acknowledged=1
                WHERE session_id=? AND client_action_id=? AND operation=?''', (session_id, action_id, operation))
            c.commit()
        return receipt

    def record(self, session_id, action_id, operation, artifact_id, revision_id=None):
        if operation not in {'ocr', 'revision'}:
            raise ValueError('DOCUMENT_RECEIPT_OPERATION_INVALID')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('''SELECT a.child_id,a.extracted_text FROM capture_artifact a
                JOIN daily_session s USING(session_id) WHERE a.artifact_id=? AND a.session_id=?
                AND a.child_id=s.child_id AND a.artifact_type='document_ocr'
                AND a.status IN ('completed','quality_warning')''', (artifact_id, session_id)).fetchone()
            if not row:
                raise ValueError('DOCUMENT_RECEIPT_SOURCE_INVALID')
            digest = hashlib.sha256(DocumentMonitoringService._normalized_text(row['extracted_text'] or '').encode()).hexdigest()
            if operation == 'ocr':
                source = c.execute('SELECT 1 FROM capture_artifact WHERE artifact_id=? AND client_action_id=?',
                                   (artifact_id, action_id)).fetchone()
            else:
                source = c.execute('''SELECT 1 FROM document_monitor_request q JOIN document_page_revision r USING(revision_id)
                    WHERE q.client_action_id=? AND q.session_id=? AND q.child_id=? AND q.operation='revision'
                    AND r.revision_id=? AND r.artifact_id=? AND r.session_id=q.session_id AND r.child_id=q.child_id
                    AND r.content_sha256=?''', (action_id, session_id, row['child_id'], revision_id, artifact_id, digest)).fetchone()
            if not source:
                raise ValueError('DOCUMENT_RECEIPT_SOURCE_INVALID')
            identity = (session_id, row['child_id'], operation, artifact_id, revision_id, digest)
            old = c.execute('''SELECT session_id,child_id,operation,artifact_id,revision_id,content_sha256
                FROM document_operation_receipt WHERE client_action_id=?''', (action_id,)).fetchone()
            if old and tuple(old) != identity:
                raise ValueError('DOCUMENT_RECEIPT_IDENTITY_CONFLICT')
            if not old:
                c.execute('INSERT INTO document_operation_receipt VALUES (?,?,?,?,?,?,?,?)',
                          (action_id, *identity, utc_now()))
            c.commit()

    def read(self, session_id, action_id, operation):
        if operation not in {'ocr', 'revision'} or not isinstance(action_id, str) or not 1 <= len(action_id) <= 200:
            raise ValueError('DOCUMENT_RECEIPT_IDENTITY_INVALID')
        result = dict(session_id=session_id, client_action_id=action_id, operation=operation,
                      status='unconfirmed', local_only=True, replay_allowed=False)
        with self.database.connect() as c:
            row = c.execute('''SELECT r.*,a.extracted_text,a.status AS artifact_status,a.artifact_type
                FROM document_operation_receipt r JOIN daily_session s USING(session_id)
                JOIN capture_artifact a ON a.artifact_id=r.artifact_id
                WHERE r.client_action_id=? AND r.session_id=? AND r.operation=?
                AND r.child_id=s.child_id AND a.session_id=r.session_id AND a.child_id=r.child_id''',
                (action_id, session_id, operation)).fetchone()
            if not row or row['artifact_type'] != 'document_ocr' or row['artifact_status'] not in {'completed', 'quality_warning'}:
                return result
            digest = hashlib.sha256(DocumentMonitoringService._normalized_text(row['extracted_text'] or '').encode()).hexdigest()
            if digest != row['content_sha256']:
                return result
            if operation == 'revision' and not c.execute('''SELECT 1 FROM document_page_revision WHERE revision_id=?
                AND artifact_id=? AND child_id=? AND session_id=? AND content_sha256=?''',
                (row['revision_id'], row['artifact_id'], row['child_id'], session_id, digest)).fetchone():
                return result
            return dict(result, status='saved', artifact_id=row['artifact_id'], revision_id=row['revision_id'])
