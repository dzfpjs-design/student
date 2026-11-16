"""Local document-camera diagnostics, independent of OCR and scoring authority."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
from uuid import uuid4

import cv2
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import numpy as np

from .pet_media_files import read_managed_media
from .repository import utc_now

FRAME_TYPE = 'document_trial_frame'
REVIEW_TYPE = 'document_trial_review'
MAX_IMAGE_BYTES = 18 * 1024 * 1024


class TrialCaptureError(ValueError):
    pass


class DocumentTrialCaptureService:
    def __init__(self, database, media, monitor, settings):
        self.database, self.media, self.monitor, self.settings = database, media, monitor, settings

    def policy(self):
        return dict(enabled=self.settings.document_trial_capture_enabled,
                    interval_seconds=self.settings.document_trial_capture_interval_seconds,
                    maximum_bytes=self.settings.document_trial_capture_maximum_bytes,
                    local_only=True, cloud_transmission_allowed=False)

    def _audit(self, c, child_id, action, detail):
        c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?)',
                  (str(uuid4()), 'system', action, child_id, json.dumps(detail), utc_now()))

    def capture(self, session_id, image_bytes, **fields):
        try:
            return self._capture(session_id, image_bytes, **fields)
        except Exception as error:
            code = str(error) if isinstance(error, TrialCaptureError) else 'TRIAL_CAPTURE_STORAGE_FAILED'
            try:
                with self.database.connect() as c:
                    row = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
                    if row:
                        self._audit(c, row['child_id'], 'document_trial_capture_failed', dict(session_id=session_id, code=code))
                        c.commit()
            except Exception:
                pass
            if isinstance(error, TrialCaptureError):
                raise
            raise TrialCaptureError(code) from None

    def _capture(self, session_id, image_bytes, *, lease_id, generation, reason, observed_at, client_action_id):
        if not self.policy()['enabled']:
            raise TrialCaptureError('TRIAL_CAPTURE_DISABLED')
        if reason not in {'periodic', 'page_changed', 'ocr_capture'}:
            raise TrialCaptureError('TRIAL_CAPTURE_REASON_INVALID')
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise TrialCaptureError('TRIAL_CAPTURE_IMAGE_INVALID')
        if not image_bytes.startswith(b'\xff\xd8\xff'):
            raise TrialCaptureError('TRIAL_CAPTURE_IMAGE_INVALID')
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[1] < 320 or image.shape[0] < 180 or image.size > 48_000_000:
            raise TrialCaptureError('TRIAL_CAPTURE_IMAGE_INVALID')
        if not isinstance(client_action_id, str) or not 8 <= len(client_action_id) <= 128:
            raise TrialCaptureError('TRIAL_CAPTURE_ACTION_INVALID')
        frame_sha = sha256(image_bytes).hexdigest()
        identity = dict(session_id=session_id, lease_id=lease_id, generation=generation,
                        reason=reason, observed_at=observed_at, sha256=frame_sha)
        request_sha = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT * FROM capture_artifact WHERE client_action_id=?',
                                 (client_action_id,)).fetchone()
            if existing:
                details = json.loads(existing['details_json'])
                if (existing['artifact_type'] != FRAME_TYPE or existing['session_id'] != session_id
                        or details.get('request_sha256') != request_sha):
                    raise TrialCaptureError('TRIAL_CAPTURE_ACTION_CONFLICT')
                return self._frame(existing)
            try:
                observed = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
                if observed.tzinfo is None or abs((datetime.now(timezone.utc) - observed).total_seconds()) > 300:
                    raise ValueError()
            except (ValueError, AttributeError, TypeError):
                raise TrialCaptureError('TRIAL_CAPTURE_TIMESTAMP_INVALID') from None
            try:
                monitor = self.monitor._active_monitor(c, session_id)
            except ValueError:
                raise TrialCaptureError('TRIAL_CAPTURE_NOT_ACTIVE') from None
            # Existing diagnostic-image consent covers the study phase only.
            state = c.execute('SELECT state FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if not state or state['state'] != 'study':
                raise TrialCaptureError('TRIAL_CAPTURE_NOT_ACTIVE')
            if type(generation) is not int or monitor['generation'] != generation:
                raise TrialCaptureError('TRIAL_CAPTURE_GENERATION_CHANGED')
            lease = c.execute('''SELECT l.*,p.system_id_hash FROM sensor_lease l JOIN device_profile p
                ON p.device_id=l.device_id WHERE l.lease_id=? AND l.session_id=?
                AND l.device_type='document_camera' AND l.status='active' ''', (lease_id, session_id)).fetchone()
            if not lease or lease['expires_at'] <= utc_now():
                raise TrialCaptureError('TRIAL_CAPTURE_LEASE_INVALID')
            occupied = c.execute('''SELECT COALESCE(SUM(json_extract(details_json,'$.byte_length')),0)
                FROM capture_artifact WHERE artifact_type=? AND local_path IS NOT NULL''', (FRAME_TYPE,)).fetchone()[0]
            if occupied + len(image_bytes) > self.settings.document_trial_capture_maximum_bytes:
                raise TrialCaptureError('TRIAL_CAPTURE_CAPACITY')
            if shutil.disk_usage(self.media.data_dir).free < len(image_bytes) + 512 * 1024 * 1024:
                raise TrialCaptureError('TRIAL_CAPTURE_DISK_FULL')
            artifact_id = str(uuid4())
            details = dict(**identity, request_sha256=request_sha, byte_length=len(image_bytes),
                           width=image.shape[1], height=image.shape[0], device_identity=lease['system_id_hash'],
                           encrypted=True, local_only=True, cloud_transmission_allowed=False,
                           diagnostic_only=True, mastery_update_allowed=False)
            with self.media._raw_publication(c, monitor['child_id'], session_id, artifact_id,
                                             '.jpg', image_bytes, True) as path:
                c.execute('''INSERT INTO capture_artifact VALUES(?,?,?, ?,?,NULL,NULL,?,'completed',?,?)''',
                          (artifact_id, monitor['child_id'], session_id, FRAME_TYPE, path,
                           json.dumps(details), utc_now(), client_action_id))
                self._audit(c, monitor['child_id'], 'document_trial_frame_retained',
                            dict(session_id=session_id, artifact_id=artifact_id, reason=reason))
            return self._frame(c.execute('SELECT * FROM capture_artifact WHERE artifact_id=?', (artifact_id,)).fetchone())

    def validate_ocr_frame(self, session_id, frame_id, frame_sha):
        if not frame_id:
            return
        with self.database.connect() as c:
            row = c.execute('''SELECT a.* FROM capture_artifact a JOIN daily_session s
                ON s.session_id=a.session_id AND s.child_id=a.child_id
                WHERE a.artifact_id=? AND a.session_id=? AND a.artifact_type=?''',
                            (frame_id, session_id, FRAME_TYPE)).fetchone()
        if not row:
            raise TrialCaptureError('TRIAL_CAPTURE_BINDING_INVALID')
        if json.loads(row['details_json']).get('sha256') != frame_sha:
            raise TrialCaptureError('TRIAL_CAPTURE_FRAME_CHANGED')
        if not self._frame(row)['image_available']:
            raise TrialCaptureError('TRIAL_CAPTURE_IMAGE_UNAVAILABLE')

    def bind_ocr(self, session_id, frame_id, ocr_id, frame_sha):
        if not frame_id:
            return
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            frame = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND session_id=? AND artifact_type=?',
                              (frame_id, session_id, FRAME_TYPE)).fetchone()
            ocr = c.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND session_id=? AND artifact_type='document_ocr'",
                            (ocr_id, session_id)).fetchone()
            if not frame or not ocr or frame['child_id'] != ocr['child_id']:
                raise TrialCaptureError('TRIAL_CAPTURE_BINDING_INVALID')
            details, ocr_details = json.loads(frame['details_json']), json.loads(ocr['details_json'])
            if details['sha256'] != frame_sha or ocr_details.get('monitor_frame_sha256') != frame_sha:
                raise TrialCaptureError('TRIAL_CAPTURE_FRAME_CHANGED')
            if details.get('ocr_artifact_id') not in {None, ocr_id}:
                raise TrialCaptureError('TRIAL_CAPTURE_BINDING_INVALID')
            details['ocr_artifact_id'] = ocr_id
            ocr_details['trial_frame_id'] = frame_id
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), frame_id))
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(ocr_details), ocr_id))
            c.commit()

    @staticmethod
    def _frame(row):
        details = json.loads(row['details_json'])
        return dict(artifact_id=row['artifact_id'], session_id=row['session_id'],
                    captured_at=details['observed_at'], reason=details['reason'], sha256=details['sha256'],
                    width=details['width'], height=details['height'], ocr_artifact_id=details.get('ocr_artifact_id'),
                    image_available=bool(row['local_path'] and Path(row['local_path']).is_file()),
                    local_only=True, cloud_transmission_allowed=False)

    def image(self, child_id, artifact_id):
        with self.database.connect() as c:
            row = c.execute('SELECT * FROM capture_artifact WHERE child_id=? AND artifact_id=? AND artifact_type=?',
                            (child_id, artifact_id, FRAME_TYPE)).fetchone()
        if not row or not row['local_path']:
            raise TrialCaptureError('TRIAL_CAPTURE_IMAGE_UNAVAILABLE')
        try:
            blob = read_managed_media(row['local_path'], media_root=self.media.data_dir / 'media',
                                      max_bytes=MAX_IMAGE_BYTES + 32)
            if not blob.startswith(b'HSC1') or not self.media.data_encryption_key:
                raise ValueError()
            image = AESGCM(self.media.data_encryption_key).decrypt(blob[4:16], blob[16:], b'hsc-raw-media-v1')
            if sha256(image).hexdigest() != json.loads(row['details_json'])['sha256']:
                raise ValueError()
            return image
        except Exception:
            raise TrialCaptureError('TRIAL_CAPTURE_IMAGE_UNAVAILABLE') from None

    def review(self, child_id, session_id, *, offset=0, persist=False):
        with self.database.connect() as c:
            session = c.execute('SELECT * FROM daily_session WHERE child_id=? AND session_id=?', (child_id, session_id)).fetchone()
            if not session:
                raise TrialCaptureError('TRIAL_CAPTURE_SESSION_UNKNOWN')
            frames = c.execute('''SELECT * FROM capture_artifact WHERE child_id=? AND session_id=?
                AND artifact_type=? ORDER BY created_at,artifact_id LIMIT 100 OFFSET ?''',
                               (child_id, session_id, FRAME_TYPE, max(0, offset))).fetchall()
            total = c.execute('SELECT COUNT(*) FROM capture_artifact WHERE session_id=? AND artifact_type=?',
                              (session_id, FRAME_TYPE)).fetchone()[0]
            question_source = '''FROM mistake_capture_candidate q JOIN capture_artifact a ON a.artifact_id=q.artifact_id
                LEFT JOIN paper_scoring_request r ON r.candidate_id=q.candidate_id
                LEFT JOIN paper_scoring_effective_decision d ON d.request_id=r.request_id
                    AND d.effective_status='qualified'
                WHERE q.child_id=? AND a.child_id=? AND a.session_id=?'''
            question_scope = (child_id, child_id, session_id)
            questions = c.execute('''SELECT q.candidate_id,q.artifact_id,q.question_text,q.mistake_id,
                r.request_id,r.answer_text,d.decision_id,d.correctness,d.authority_type,d.authority_ref
                ''' + question_source + ' ORDER BY q.created_at,q.candidate_id LIMIT 500', question_scope).fetchall()
            question_counts = c.execute('''SELECT COUNT(*) total,
                COALESCE(SUM(d.correctness=1),0) correct,
                COALESCE(SUM(d.correctness=0),0) wrong,
                COALESCE(SUM(d.correctness IS NULL),0) unscored ''' + question_source, question_scope).fetchone()
            counts = c.execute('SELECT event_type,COUNT(*) n FROM document_monitor_event WHERE session_id=? GROUP BY event_type',
                               (session_id,)).fetchall()
            event_counts = {r['event_type']: r['n'] for r in counts}
            failed = c.execute("SELECT COUNT(*) FROM audit_event WHERE action='document_trial_capture_failed' AND child_id=? AND json_extract(summary_json,'$.session_id')=?",
                               (child_id, session_id)).fetchone()[0]
            items = [dict(r) for r in questions]
            frame_items = [self._frame(r) for r in frames]
            for item in frame_items:
                linked = c.execute('''SELECT q.candidate_id,q.artifact_id,q.question_text,q.mistake_id,
                    r.request_id,r.answer_text,d.decision_id,d.correctness,d.authority_type,d.authority_ref
                    ''' + question_source + ' AND q.artifact_id=? ORDER BY q.created_at,q.candidate_id LIMIT 101',
                                   (*question_scope, item['ocr_artifact_id'])).fetchall() if item['ocr_artifact_id'] else []
                item['questions'] = [dict(q) for q in linked[:100]]
                item['questions_has_more'] = len(linked) > 100
            summary = dict(frame_count=total, question_count=question_counts['total'],
                           correct_count=question_counts['correct'],
                           wrong_count=question_counts['wrong'],
                           unscored_count=question_counts['unscored'],
                           capture_failure_count=failed, monitor_events=event_counts)
            findings = []
            if failed:
                findings.append('存在诊断图保存失败，需要检查容量与保存回执。')
            if event_counts.get('capture_failed'):
                findings.append('存在页面识别失败，需要对照原图检查 OCR 和恢复。')
            if summary['unscored_count']:
                findings.append('存在尚未合格评分的题目，需要核查题目识别与评分依据。')
            result = dict(session_id=session_id, child_id=child_id, session_status=session['status'],
                          summary=summary, findings=findings, frames=frame_items, offset=offset,
                          has_more=offset + len(frames) < total, questions=items,
                          questions_has_more=len(items) < question_counts['total'],
                          review_scope='program_diagnostics', model_accuracy_verified=False,
                          local_only=True, cloud_transmission_allowed=False)
            if persist and session['status'] != 'active':
                snapshot = dict(summary=summary, findings=findings, session_id=session_id,
                                review_scope='program_diagnostics', model_accuracy_verified=False,
                                local_only=True, cloud_transmission_allowed=False)
                c.execute('''INSERT INTO capture_artifact VALUES(?,?,?, ?,NULL,NULL,NULL,?,'completed',?,?)
                    ON CONFLICT(client_action_id) DO UPDATE SET details_json=excluded.details_json''',
                          (str(uuid4()), child_id, session_id, REVIEW_TYPE, json.dumps(snapshot), utc_now(),
                           'document-trial-review:' + session_id))
                c.commit()
            return result

    def frame_questions(self, child_id, artifact_id, *, offset=0):
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000:
            raise TrialCaptureError('TRIAL_CAPTURE_OFFSET_INVALID')
        with self.database.connect() as c:
            c.execute('BEGIN')
            frame = c.execute('SELECT * FROM capture_artifact WHERE child_id=? AND artifact_id=? AND artifact_type=?',
                              (child_id, artifact_id, FRAME_TYPE)).fetchone()
            if not frame:
                raise TrialCaptureError('TRIAL_CAPTURE_IMAGE_UNAVAILABLE')
            ocr_id = json.loads(frame['details_json']).get('ocr_artifact_id')
            scope = (child_id, child_id, frame['session_id'], ocr_id)
            source = '''FROM mistake_capture_candidate q JOIN capture_artifact a ON a.artifact_id=q.artifact_id
                LEFT JOIN paper_scoring_request r ON r.candidate_id=q.candidate_id
                LEFT JOIN paper_scoring_effective_decision d ON d.request_id=r.request_id
                    AND d.effective_status='qualified'
                WHERE q.child_id=? AND a.child_id=? AND a.session_id=? AND a.artifact_id=?'''
            total = c.execute('SELECT COUNT(*) ' + source, scope).fetchone()[0]
            rows = c.execute('''SELECT q.candidate_id,q.artifact_id,q.question_text,q.mistake_id,
                r.request_id,r.answer_text,d.decision_id,d.correctness,d.authority_type,d.authority_ref
                ''' + source + ' ORDER BY q.created_at,q.candidate_id LIMIT 100 OFFSET ?', (*scope, offset)).fetchall()
            return dict(artifact_id=artifact_id, questions=[dict(row) for row in rows], offset=offset,
                        total=total, has_more=offset + len(rows) < total,
                        local_only=True, cloud_transmission_allowed=False)

    def recent_sessions(self, child_id):
        with self.database.connect() as c:
            rows = c.execute('''SELECT DISTINCT s.session_id,s.started_at,s.status FROM daily_session s
                JOIN capture_artifact a ON a.session_id=s.session_id AND a.child_id=s.child_id
                WHERE s.child_id=? AND a.artifact_type=? ORDER BY s.started_at DESC LIMIT 20''',
                             (child_id, FRAME_TYPE)).fetchall()
        return [dict(r) for r in rows]

    def reconcile_completed(self):
        with self.database.connect() as c:
            rows = c.execute('''SELECT DISTINCT a.child_id,a.session_id FROM capture_artifact a
                JOIN daily_session s ON s.session_id=a.session_id WHERE a.artifact_type=? AND s.status!='active'
                AND NOT EXISTS(SELECT 1 FROM capture_artifact review WHERE review.session_id=a.session_id
                    AND review.artifact_type=?) LIMIT 20''', (FRAME_TYPE, REVIEW_TYPE)).fetchall()
        for row in rows:
            self.review(row['child_id'], row['session_id'], persist=True)


def install_trial_capture_routes(application, service, parent_auth):
    import asyncio
    import base64
    import binascii
    from fastapi import Request, Query
    from fastapi.responses import JSONResponse, Response
    from pydantic import BaseModel, Field, StrictInt

    class FrameRequest(BaseModel):
        image_base64: str = Field(min_length=32, max_length=24_000_000)
        lease_id: str = Field(min_length=1, max_length=120)
        generation: StrictInt = Field(ge=0)
        reason: str = Field(min_length=1, max_length=30)
        observed_at: str = Field(min_length=10, max_length=50)
        client_action_id: str = Field(min_length=8, max_length=128)

    @application.exception_handler(TrialCaptureError)
    async def trial_capture_error(_request, error):
        return JSONResponse(status_code=409, content=dict(code=str(error),
            message='试用诊断图片没有取得保存或读取回执，请查看本机诊断记录。'),
            headers={'Cache-Control': 'no-store'})

    @application.post('/v1/sessions/{session_id}/document-monitor/trial-frames')
    async def retain_frame(session_id: str, payload: FrameRequest):
        try:
            image = base64.b64decode(payload.image_base64, validate=True)
        except (binascii.Error, ValueError):
            raise TrialCaptureError('TRIAL_CAPTURE_IMAGE_INVALID') from None
        fields = payload.model_dump()
        fields.pop('image_base64')
        return await asyncio.to_thread(service.capture, session_id, image, **fields)

    @application.get('/v1/parent/children/{child_id}/document-trial/sessions')
    async def sessions(child_id: str, request: Request):
        parent_auth.require(request.headers.get('X-HSC-Parent'))
        await asyncio.to_thread(service.reconcile_completed)
        return dict(items=service.recent_sessions(child_id))

    @application.get('/v1/parent/children/{child_id}/document-trial/sessions/{session_id}')
    async def review(child_id: str, session_id: str, request: Request, offset: int = 0):
        parent_auth.require(request.headers.get('X-HSC-Parent'))
        return await asyncio.to_thread(service.review, child_id, session_id, offset=max(0, offset), persist=True)

    @application.get('/v1/parent/children/{child_id}/document-trial/frames/{artifact_id}/image')
    async def image(child_id: str, artifact_id: str, request: Request):
        parent_auth.require(request.headers.get('X-HSC-Parent'))
        return Response(await asyncio.to_thread(service.image, child_id, artifact_id), media_type='image/jpeg',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @application.get('/v1/parent/children/{child_id}/document-trial/frames/{artifact_id}/questions')
    async def frame_questions(child_id: str, artifact_id: str, request: Request, offset: int = Query(default=0, ge=0, le=100000)):
        parent_auth.require(request.headers.get('X-HSC-Parent'))
        result = await asyncio.to_thread(service.frame_questions, child_id, artifact_id, offset=offset)
        return JSONResponse(content=result, headers={'Cache-Control': 'no-store'})
