from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4

from .document_monitoring import DOCUMENT_ANALYSIS_STATES
from .document_preview_accumulator import build_page_draft, verified_frame
from .document_visual_registration import LandmarkCache, METHOD, extract_landmarks, register, register_cache
from .local_ocr.cache_lifecycle import assert_current, request_digest
from .repository import utc_now


PARTIAL_INSPECTION_STATUS = "partial_local_preview"
PARTIAL_INSPECTION_NOTICE = "这次已在本机试读，纸面文字仍需核对。"
_INSPECTION_TTL = timedelta(minutes=5)


def is_partial_inspection(result):
    if not isinstance(result, dict):
        return False
    for key in ("execution_receipt", "client_bridge_receipt"):
        receipt = result.get(key)
        output = receipt.get("output") if isinstance(receipt, dict) else None
        if isinstance(output, dict) and output.get("page_status") == PARTIAL_INSPECTION_STATUS:
            return True
    return False


_CREATE_PREVIEWS = """CREATE TABLE IF NOT EXISTS document_ocr_preview (
    preview_id TEXT PRIMARY KEY,
    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
    monitor_generation INTEGER NOT NULL,
    lease_id TEXT NOT NULL,
    frame_sha256 TEXT NOT NULL,
    frame_signature TEXT,
    client_action_id TEXT NOT NULL UNIQUE,
    inspection_turn_id TEXT,
    phase TEXT,
    page_token TEXT,
    geometry_json TEXT,
    page_draft_json TEXT,
    text TEXT NOT NULL,
    question_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
)"""


class DocumentOcrPreviewService:
    """Durable local reading with no capture artifact or page-revision authority."""

    def __init__(self, database, workflow):
        self.database = database
        self.workflow = workflow
        self._visual_landmarks = LandmarkCache()
        register_cache(database, self._visual_landmarks)
        with database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(_CREATE_PREVIEWS)
            columns = {row['name'] for row in connection.execute("PRAGMA table_info(document_ocr_preview)")}
            indexes = connection.execute("PRAGMA index_list(document_ocr_preview)").fetchall()
            legacy_constraint = any(index['unique'] and [
                row['name'] for row in connection.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                                                          (index['name'],))
            ] == ['session_id', 'monitor_generation'] for index in indexes)
            optional_columns = (
                'frame_signature', 'inspection_turn_id', 'phase', 'page_token', 'geometry_json', 'page_draft_json',
            )
            if legacy_constraint:
                # SQLite cannot drop a table-level UNIQUE index. Copy its rows
                # transactionally, retaining the original request identities.
                connection.execute("ALTER TABLE document_ocr_preview RENAME TO document_ocr_preview_previous")
                connection.execute(_CREATE_PREVIEWS)
                optional_names = ','.join(optional_columns)
                values = ','.join(name if name in columns else 'NULL' for name in optional_columns)
                connection.execute(f"""INSERT INTO document_ocr_preview
                    (preview_id,child_id,session_id,lease_id,monitor_generation,frame_sha256,client_action_id,
                     text,question_count,created_at,{optional_names})
                    SELECT preview_id,child_id,session_id,lease_id,monitor_generation,frame_sha256,client_action_id,
                           text,question_count,created_at,{values} FROM document_ocr_preview_previous""")
                connection.execute("DROP TABLE document_ocr_preview_previous")
            else:
                for name in optional_columns:
                    if name not in columns:
                        connection.execute(f"ALTER TABLE document_ocr_preview ADD COLUMN {name} TEXT")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_document_ocr_preview_inspection
                ON document_ocr_preview(inspection_turn_id) WHERE inspection_turn_id IS NOT NULL""")
            connection.execute("""CREATE INDEX IF NOT EXISTS idx_document_ocr_preview_session
                ON document_ocr_preview(session_id, created_at DESC)""")
            connection.execute("""CREATE INDEX IF NOT EXISTS idx_document_ocr_preview_page_token
                ON document_ocr_preview(session_id, child_id, phase, lease_id, monitor_generation, page_token, created_at)
                WHERE page_token IS NOT NULL""")
            connection.commit()

    @staticmethod
    def _active_scope(connection, session_id):
        row = connection.execute("""SELECT s.child_id,s.state FROM daily_session s
            JOIN child_profile c USING(child_id) WHERE s.session_id=? AND s.status='active'
            AND s.ended_at IS NULL AND s.observation_status='enabled'
            AND c.status='active'""", (session_id,)).fetchone()
        if not row or row['state'] not in DOCUMENT_ANALYSIS_STATES:
            raise ValueError('DOCUMENT_PREVIEW_NOT_AVAILABLE')
        return row['child_id'], row['state']

    @classmethod
    def _capture_scope(cls, connection, session_id, generation, expected_lease_id, expected_phase=None):
        child_id, phase = cls._active_scope(connection, session_id)
        if expected_phase is not None and phase != expected_phase:
            raise ValueError('DOCUMENT_PREVIEW_SOURCE_CHANGED')
        lease = connection.execute("""SELECT l.lease_id FROM sensor_lease l
            JOIN document_monitor_session m ON m.session_id=l.session_id
            WHERE m.session_id=? AND m.child_id=? AND m.status='active' AND m.generation=?
            AND l.device_type='document_camera' AND l.status='active'
            AND julianday(l.expires_at)>julianday('now')
            ORDER BY l.opened_at DESC, l.lease_id DESC LIMIT 1""",
            (session_id, child_id, generation)).fetchone()
        if not lease or lease['lease_id'] != expected_lease_id:
            raise ValueError('DOCUMENT_PREVIEW_SOURCE_CHANGED')
        return child_id, lease['lease_id'], phase

    @staticmethod
    def _pending_inspection(connection, turn_id, session_id, child_id, phase, *, completing=False):
        turn = connection.execute("""SELECT t.created_at,t.tool_status FROM agent_turn t
            JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND t.session_id=? AND t.child_id=?
            AND p.session_id=t.session_id AND p.child_id=t.child_id
            AND p.status IN ('planned','running') AND s.status=p.status AND s.step_index=0
            AND s.capability_name='document.monitor_control' AND s.execution_location='client_device_bridge'
            AND json_extract(t.action_json,'$.type')='control_document_monitor'
            AND json_extract(t.action_json,'$.command')='inspect'
            AND json_extract(s.arguments_json,'$.command')='inspect'
            AND NOT EXISTS(SELECT 1 FROM session_transition tr
                WHERE tr.session_id=t.session_id AND tr.to_state=? AND tr.from_state IS NOT tr.to_state
                AND tr.occurred_at>t.created_at)""", (turn_id, session_id, child_id, phase)).fetchone()
        allowed_statuses = {'pending', 'completed'} if completing else {'pending'}
        if not turn or turn['tool_status'] not in allowed_statuses:
            raise ValueError('DOCUMENT_PREVIEW_INSPECTION_NOT_PENDING')
        started_at = datetime.fromisoformat(turn['created_at'])
        if not timedelta(0) <= datetime.now(timezone.utc) - started_at <= _INSPECTION_TTL:
            raise ValueError('DOCUMENT_PREVIEW_INSPECTION_EXPIRED')
        return started_at

    @classmethod
    def validate_inspection_receipt(cls, connection, turn, output, completed_at):
        if (set(output) != {'page_status', 'preview_id'} or not isinstance(output['preview_id'], str)
                or not 0 < len(output['preview_id']) <= 128):
            raise ValueError('DOCUMENT_PREVIEW_INVALID_INSPECTION_RECEIPT')
        session_id, child_id, turn_id = turn['session_id'], turn['child_id'], turn['agent_turn_id']
        # The preview table is initialized by the local preview service, not
        # by the standalone agent runtime.
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='document_ocr_preview'").fetchone():
            raise ValueError('DOCUMENT_PREVIEW_INSPECTION_SOURCE_CHANGED')
        preview = connection.execute("""SELECT monitor_generation,lease_id,phase,created_at FROM document_ocr_preview
            WHERE preview_id=? AND session_id=? AND child_id=? AND inspection_turn_id=?""",
            (output['preview_id'], session_id, child_id, turn_id)).fetchone()
        if not preview or preview['phase'] not in DOCUMENT_ANALYSIS_STATES:
            raise ValueError('DOCUMENT_PREVIEW_INSPECTION_SOURCE_CHANGED')
        cls._capture_scope(connection, session_id, preview['monitor_generation'], preview['lease_id'], preview['phase'])
        started_at = cls._pending_inspection(connection, turn_id, session_id, child_id, preview['phase'], completing=True)
        created_at = datetime.fromisoformat(preview['created_at'])
        now = datetime.now(timezone.utc)
        allow_clock_skew = timedelta(seconds=30)
        if not started_at <= created_at <= now or not (
                created_at - allow_clock_skew <= completed_at <= now + allow_clock_skew):
            raise ValueError('DOCUMENT_PREVIEW_INSPECTION_SOURCE_CHANGED')

    @classmethod
    def _check_inspection_capture(cls, connection, turn_id, session_id, child_id, phase, client_action_id):
        if turn_id is None:
            return
        cls._pending_inspection(connection, turn_id, session_id, child_id, phase)
        if connection.execute("""SELECT 1 FROM document_ocr_preview
            WHERE inspection_turn_id=? AND client_action_id<>?""", (turn_id, client_action_id)).fetchone():
            raise ValueError('DOCUMENT_PREVIEW_INSPECTION_ALREADY_CAPTURED')

    @staticmethod
    def _inspection_response(preview, turn_id):
        if turn_id is not None:
            preview['inspection_turn_id'] = turn_id
            excerpt = ' '.join(preview['text'].split())[:120]
            preview['inspection_response_text'] = (
                f'我从当前画面试读到这段文字：「{excerpt}」。请和纸上的原题核对。'
                if excerpt else '我还没从当前画面读到可核对的文字，请摆正纸张后再试。')
        return preview

    @staticmethod
    def _readback(row):
        return {
            **{key: row[key] for key in ('preview_id', 'client_action_id', 'created_at', 'text', 'question_count',
                                        'frame_signature', 'phase')},
            'generation': row['monitor_generation'],
            **({'page_draft': json.loads(row['page_draft_json'])} if row['page_draft_json'] else {}),
        }

    def latest(self, session_id):
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            child_id, _ = self._active_scope(connection, session_id)
            rows = connection.execute("""SELECT preview_id,client_action_id,created_at,text,question_count,
                monitor_generation,frame_signature,phase,page_draft_json
                FROM document_ocr_preview WHERE session_id=? AND child_id=?
                ORDER BY created_at DESC,preview_id DESC LIMIT 10""", (session_id, child_id))
            return {'items': [self._readback(row) for row in rows]}

    @staticmethod
    def _existing(connection, client_action_id, session_id, child_id, lease_id, generation, frame_sha, frame_signature, inspection_turn_id, phase, page_token):
        row = connection.execute("SELECT * FROM document_ocr_preview WHERE client_action_id=?",
                                 (client_action_id,)).fetchone()
        if row:
            previous_scope = (row['session_id'], row['child_id'], row['lease_id'], row['monitor_generation'],
                              row['frame_sha256'], row['frame_signature'], row['inspection_turn_id'],
                              row['phase'] or 'homework_capture', row['page_token'])
            if previous_scope != (session_id, child_id, lease_id, generation, frame_sha, frame_signature,
                                  inspection_turn_id, phase, page_token):
                raise ValueError('DOCUMENT_PREVIEW_ACTION_CONFLICT')
        return row

    def _page_draft(self, connection, scope, page_token, preview_id, frame, landmarks=None):
        if frame is None:
            return build_page_draft(page_token, preview_id, frame, allow_text_anchor=False)
        where = ("session_id=? AND child_id=? AND phase=? AND lease_id=? AND monitor_generation=? AND page_token=?")
        parameters = (*scope, page_token)
        anchor = connection.execute(f"""SELECT preview_id,geometry_json,page_draft_json FROM document_ocr_preview
            WHERE {where} AND json_extract(page_draft_json,'$.reason') IN ('anchor_started','conflicting_page')
            ORDER BY rowid DESC LIMIT 1""", parameters).fetchone()
        if anchor is None:
            return build_page_draft(page_token, preview_id, frame, visual_anchor=landmarks is not None, allow_text_anchor=False)
        anchor_frame = json.loads(anchor['geometry_json'])
        anchor_draft = json.loads(anchor['page_draft_json'])
        fixed = self._visual_landmarks.get((*scope, page_token), anchor['preview_id'], anchor_frame['frame_sha256'])
        if anchor_draft.get('alignment_method') == METHOD and fixed is None:
            # No raw frame/descriptor is persisted. After restart/expiry the
            # durable old draft stays readable, but a new coordinate anchor is
            # required rather than silently continuing without its evidence.
            return build_page_draft(page_token, preview_id, frame, visual_anchor=landmarks is not None, allow_text_anchor=False)
        latest = connection.execute(f"""SELECT page_draft_json FROM document_ocr_preview
            WHERE {where} AND json_extract(page_draft_json,'$.anchor_preview_id')=?
            AND json_extract(page_draft_json,'$.reason') IN ('anchor_started','conflicting_page','merged')
            ORDER BY rowid DESC LIMIT 1""", (*parameters, anchor['preview_id'])).fetchone()
        duplicate = connection.execute(f"""SELECT 1 FROM document_ocr_preview
            WHERE {where} AND json_extract(page_draft_json,'$.anchor_preview_id')=? AND frame_sha256=?
            AND json_extract(page_draft_json,'$.reason') IN ('anchor_started','conflicting_page','merged') LIMIT 1""",
            (*parameters, anchor['preview_id'], frame['frame_sha256'])).fetchone()
        matrix, conflict, evidence = register(anchor_frame, frame, fixed, landmarks)
        if matrix is None:
            # Live previews require checked image geometry AND independent
            # text agreement. The standalone text-only utility is not a live
            # fallback. A qualified new source starts its own candidate; it
            # never inherits the old draft or claims a physical page turn.
            return build_page_draft(page_token, preview_id, frame, visual_anchor=landmarks is not None,
                                    registration=evidence, allow_text_anchor=False)
        draft = build_page_draft(page_token, preview_id, frame, anchor_id=anchor['preview_id'],
                                 anchor=anchor_frame, previous=json.loads(latest['page_draft_json']),
                                 duplicate=duplicate is not None, visual_matrix=matrix,
                                 visual_conflict=conflict, registration=evidence)
        if (not draft['merged'] and draft['reason'] != 'duplicate_frame'
                and draft['anchor_preview_id'] != preview_id):
            # A checked transform may still leave a newly visible OCR region
            # outside the old reference. Preserve that source as its own
            # candidate instead of keeping an unusable pointer to the old one.
            return build_page_draft(page_token, preview_id, frame, visual_anchor=landmarks is not None,
                                    registration={**(evidence or {}), 'status': 'unconfirmed_alignment',
                                                  'candidate_reason': draft['reason']}, allow_text_anchor=False)
        return draft

    def create(self, session_id, image, client_action_id, generation, expected_lease_id, frame_signature=None,
               inspection_turn_id=None, expected_phase=None, page_token=None):
        if inspection_turn_id is not None and expected_phase is None:
            raise ValueError('DOCUMENT_PREVIEW_INSPECTION_PHASE_REQUIRED')
        frame_sha = sha256(image).hexdigest()
        if frame_signature is not None:
            if frame_signature != frame_sha:
                raise ValueError('DOCUMENT_PREVIEW_FRAME_SIGNATURE_MISMATCH')
            frame_signature = frame_sha
        request_id = f'document-preview:{client_action_id}'
        # The in-flight OCR identity also binds the voice command, so retries
        # cannot turn an earlier automatic frame into a command receipt.
        # Make the job visible to deletion before recognition starts. Deletion
        # can cancel it without waiting for the recognizer while the frame is in RAM.
        with self.database.external_effect_guard():
            with self.database.connect() as connection:
                connection.execute('BEGIN')
                child_id, lease_id, phase = self._capture_scope(
                    connection, session_id, generation, expected_lease_id, expected_phase)
                self._visual_landmarks.activate((session_id, child_id, phase, lease_id, generation, page_token))
                self._check_inspection_capture(connection, inspection_turn_id, session_id, child_id, phase, client_action_id)
                saved = self._existing(connection, client_action_id, session_id, child_id, lease_id, generation,
                                       frame_sha, frame_signature, inspection_turn_id, phase, page_token)
                if saved:
                    return self._inspection_response(self._readback(saved), inspection_turn_id)
            preview_signature = frame_signature
            if inspection_turn_id is not None or phase != 'homework_capture':
                preview_signature = sha256(json.dumps([frame_signature, inspection_turn_id, phase]).encode()).hexdigest()
            if page_token is not None:
                preview_signature = sha256(json.dumps([preview_signature, page_token]).encode()).hexdigest()
            scheduled = self.workflow.queue_recognition(
                session_id, image, request_id, generation, preview_lease_id=lease_id, preview_signature=preview_signature
            )
        job = self.workflow.finish_recognition(scheduled, image)
        result = job['result']
        text = '\n'.join(line['text'] for line in result['lines'] if line['text'].strip())
        question_count = sum(block['block_kind'] == 'question' for block in result['question_blocks'])
        frame = verified_frame(result, frame_sha) if page_token is not None else None
        landmark_diagnostics = {}
        landmarks = extract_landmarks(image, frame, diagnostics=landmark_diagnostics)

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
                if self._capture_scope(connection, session_id, generation, expected_lease_id, phase) != (child_id, lease_id, phase):
                    raise ValueError('DOCUMENT_PREVIEW_SOURCE_CHANGED')
                self._check_inspection_capture(connection, inspection_turn_id, session_id, child_id, phase, client_action_id)
                saved = self._existing(connection, client_action_id, session_id, child_id, lease_id, generation,
                                       frame_sha, frame_signature, inspection_turn_id, phase, page_token)
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
                return self._inspection_response(self._readback(saved), inspection_turn_id)
            preview = dict(preview_id=str(uuid4()), client_action_id=client_action_id, created_at=utc_now(),
                           text=text, question_count=question_count, generation=generation,
                           frame_signature=frame_signature, phase=phase)
            draft = None
            if page_token is not None:
                scope = (session_id, child_id, phase, lease_id, generation)
                draft = self._page_draft(connection, scope, page_token, preview['preview_id'], frame, landmarks)
                if (landmarks is None and draft['reason'] in {'insufficient_anchors', 'geometry_unavailable'}
                        and not draft['merged']):
                    draft['registration'] = landmark_diagnostics
                preview['page_draft'] = draft
            connection.execute("""INSERT INTO document_ocr_preview
                (preview_id,child_id,session_id,lease_id,monitor_generation,frame_sha256,frame_signature,client_action_id,
                 inspection_turn_id,phase,page_token,geometry_json,page_draft_json,text,question_count,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (preview['preview_id'], child_id, session_id, lease_id, generation, frame_sha, frame_signature,
                 client_action_id, inspection_turn_id, phase, page_token, json.dumps(frame) if frame is not None else None,
                 json.dumps(draft, ensure_ascii=False, allow_nan=False) if draft is not None else None,
                 text, question_count, preview['created_at']))
            connection.commit()
            if (landmarks is not None and draft is not None
                    and draft['anchor_preview_id'] == preview['preview_id']
                    and draft['reason'] in {'anchor_started', 'conflicting_page'}):
                # Publish RAM state only after the durable anchor commits and
                # the original child/phase/lease/generation was revalidated.
                self._visual_landmarks.put((*scope, page_token), preview['preview_id'], landmarks)
            elif draft is not None:
                evidence = draft.get('registration') or {}
                if (evidence.get('status') == 'verified' and evidence.get('source_frame_sha256') == frame_sha
                        and draft.get('anchor_preview_id')):
                    # A committed match keeps the same physical reference alive
                    # while the child works. Failed/unknown reads never renew it.
                    self._visual_landmarks.touch((*scope, page_token), draft['anchor_preview_id'],
                                                 evidence.get('anchor_frame_sha256'))
            return self._inspection_response(preview, inspection_turn_id)
