from hashlib import sha256
from uuid import uuid4

from .local_ocr.cache_lifecycle import assert_current, request_digest
from .repository import utc_now


_CREATE_PREVIEWS = """CREATE TABLE IF NOT EXISTS document_ocr_preview (
    preview_id TEXT PRIMARY KEY,
    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
    monitor_generation INTEGER NOT NULL,
    lease_id TEXT NOT NULL,
    frame_sha256 TEXT NOT NULL,
    frame_signature TEXT,
    client_action_id TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    question_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
)"""


class DocumentOcrPreviewService:
    """Durable local reading with no capture artifact or page-revision authority."""

    def __init__(self, database, workflow):
        self.database = database
        self.workflow = workflow
        with database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(_CREATE_PREVIEWS)
            columns = {row['name'] for row in connection.execute("PRAGMA table_info(document_ocr_preview)")}
            indexes = connection.execute("PRAGMA index_list(document_ocr_preview)").fetchall()
            legacy_constraint = any(index['unique'] and [
                row['name'] for row in connection.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                                                          (index['name'],))
            ] == ['session_id', 'monitor_generation'] for index in indexes)
            if legacy_constraint:
                # SQLite cannot drop a table-level UNIQUE index. Copy its rows
                # transactionally, retaining the original request identities.
                connection.execute("ALTER TABLE document_ocr_preview RENAME TO document_ocr_preview_previous")
                connection.execute(_CREATE_PREVIEWS)
                signature_column = 'frame_signature' if 'frame_signature' in columns else 'NULL'
                connection.execute(f"""INSERT INTO document_ocr_preview
                    (preview_id,child_id,session_id,lease_id,monitor_generation,frame_sha256,frame_signature,
                     client_action_id,text,question_count,created_at)
                    SELECT preview_id,child_id,session_id,lease_id,monitor_generation,frame_sha256,{signature_column},
                           client_action_id,text,question_count,created_at FROM document_ocr_preview_previous""")
                connection.execute("DROP TABLE document_ocr_preview_previous")
            elif 'frame_signature' not in columns:
                connection.execute("ALTER TABLE document_ocr_preview ADD COLUMN frame_signature TEXT")
            connection.execute("""CREATE INDEX IF NOT EXISTS idx_document_ocr_preview_session
                ON document_ocr_preview(session_id, created_at DESC)""")
            connection.commit()

    @staticmethod
    def _child(connection, session_id):
        row = connection.execute("""SELECT s.child_id FROM daily_session s
            JOIN child_profile c USING(child_id) WHERE s.session_id=? AND s.status='active'
            AND s.state='homework_capture' AND s.ended_at IS NULL AND s.observation_status='enabled'
            AND c.status='active'""", (session_id,)).fetchone()
        if not row:
            raise ValueError('DOCUMENT_PREVIEW_NOT_AVAILABLE')
        return row['child_id']

    @classmethod
    def _capture_scope(cls, connection, session_id, generation, expected_lease_id):
        child_id = cls._child(connection, session_id)
        lease = connection.execute("""SELECT l.lease_id FROM sensor_lease l
            JOIN document_monitor_session m ON m.session_id=l.session_id
            WHERE m.session_id=? AND m.child_id=? AND m.status='active' AND m.generation=?
            AND l.device_type='document_camera' AND l.status='active'
            AND julianday(l.expires_at)>julianday('now')
            ORDER BY l.opened_at DESC, l.lease_id DESC LIMIT 1""",
            (session_id, child_id, generation)).fetchone()
        if not lease or lease['lease_id'] != expected_lease_id:
            raise ValueError('DOCUMENT_PREVIEW_SOURCE_CHANGED')
        return child_id, lease['lease_id']

    @staticmethod
    def _readback(row):
        return {
            **{key: row[key] for key in ('preview_id', 'client_action_id', 'created_at', 'text', 'question_count',
                                        'frame_signature')},
            'generation': row['monitor_generation'],
        }

    def latest(self, session_id):
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            child_id = self._child(connection, session_id)
            rows = connection.execute("""SELECT preview_id,client_action_id,created_at,text,question_count,
                monitor_generation,frame_signature
                FROM document_ocr_preview WHERE session_id=? AND child_id=?
                ORDER BY created_at DESC,preview_id DESC LIMIT 10""", (session_id, child_id))
            return {'items': [self._readback(row) for row in rows]}

    @staticmethod
    def _existing(connection, client_action_id, session_id, child_id, lease_id, generation, frame_sha, frame_signature):
        row = connection.execute("SELECT * FROM document_ocr_preview WHERE client_action_id=?",
                                 (client_action_id,)).fetchone()
        if row and (row['session_id'], row['child_id'], row['lease_id'], row['monitor_generation'],
                    row['frame_sha256'], row['frame_signature']) != (
                    session_id, child_id, lease_id, generation, frame_sha, frame_signature):
            raise ValueError('DOCUMENT_PREVIEW_ACTION_CONFLICT')
        return row

    def create(self, session_id, image, client_action_id, generation, expected_lease_id, frame_signature=None):
        frame_sha = sha256(image).hexdigest()
        if frame_signature is not None:
            if frame_signature != frame_sha:
                raise ValueError('DOCUMENT_PREVIEW_FRAME_SIGNATURE_MISMATCH')
            frame_signature = frame_sha
        request_id = f'document-preview:{client_action_id}'
        # Make the job visible to deletion before recognition starts. Deletion
        # can cancel it without waiting for the recognizer while the frame is in RAM.
        with self.database.external_effect_guard():
            with self.database.connect() as connection:
                connection.execute('BEGIN')
                child_id, lease_id = self._capture_scope(connection, session_id, generation, expected_lease_id)
                saved = self._existing(connection, client_action_id, session_id, child_id, lease_id, generation,
                                       frame_sha, frame_signature)
                if saved:
                    return self._readback(saved)
            scheduled = self.workflow.queue_recognition(
                session_id, image, request_id, generation, preview_lease_id=lease_id, preview_signature=frame_signature
            )
        job = self.workflow.finish_recognition(scheduled, image)
        result = job['result']
        text = '\n'.join(line['text'] for line in result['lines'] if line['text'].strip())
        question_count = sum(block['block_kind'] == 'question' for block in result['question_blocks'])

        # Coordinate with privacy deletion/restore and recheck the original
        # lease in the same transaction as the preview insert.
        with self.database.external_effect_guard(), self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute("""SELECT * FROM ocr_job WHERE job_id=? AND request_id=?
                AND child_id=? AND session_id=? AND monitor_generation=? AND image_hash=?""",
                (job['job_id'], request_id, child_id, session_id, generation, frame_sha)).fetchone()
            if not current or current['status'] != 'completed' or current['publication_started']:
                raise ValueError('DOCUMENT_PREVIEW_SOURCE_CHANGED')
            try:
                if self._capture_scope(connection, session_id, generation, expected_lease_id) != (child_id, lease_id):
                    raise ValueError('DOCUMENT_PREVIEW_SOURCE_CHANGED')
                saved = self._existing(connection, client_action_id, session_id, child_id, lease_id, generation,
                                       frame_sha, frame_signature)
                assert_current(connection, current)
            except ValueError:
                connection.execute("INSERT OR IGNORE INTO ocr_request_tombstone VALUES (?, 'deleted')",
                                   (request_digest(request_id),))
                connection.execute("DELETE FROM ocr_cache WHERE owner_key=? AND image_hash=?",
                                   (current['owner_key'], frame_sha))
                connection.execute("DELETE FROM ocr_job WHERE job_id=?", (job['job_id'],))
                connection.commit()
                raise
            if saved:
                return self._readback(saved)
            preview = dict(preview_id=str(uuid4()), client_action_id=client_action_id, created_at=utc_now(),
                           text=text, question_count=question_count, generation=generation,
                           frame_signature=frame_signature)
            connection.execute("""INSERT INTO document_ocr_preview
                (preview_id,child_id,session_id,lease_id,monitor_generation,frame_sha256,frame_signature,client_action_id,
                 text,question_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (preview['preview_id'], child_id, session_id, lease_id, generation, frame_sha, frame_signature,
                 client_action_id, text, question_count, preview['created_at']))
            connection.commit()
            return preview
