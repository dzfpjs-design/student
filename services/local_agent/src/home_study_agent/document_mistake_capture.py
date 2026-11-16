"""Publish question crops and teacher-qualified errors from accepted local OCR."""
from __future__ import annotations

import base64
from hashlib import sha256
import json
import os

import cv2
import numpy as np
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .document_monitoring import DocumentMonitoringService
from .local_ocr.text_geometry import validate as validate_geometry
from .mistake_book import utc_now
from .mistake_dialogue import subject_for_question


class DocumentMistakeCapture:
    def __init__(self, database, mistake_capture, paper_scoring, encryption_key):
        self.database = database
        self.capture = mistake_capture
        self.book = mistake_capture.mistake_book
        self.scoring = paper_scoring
        self.cipher = AESGCM(encryption_key) if encryption_key else None
        with database.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS mistake_question_crop (
                candidate_id TEXT PRIMARY KEY REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
                mistake_id TEXT NOT NULL UNIQUE REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                revision_id TEXT NOT NULL REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
                artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                encrypted_jpeg BLOB NOT NULL
            )''')
            connection.execute('''CREATE INDEX IF NOT EXISTS idx_mistake_question_crop_revision
                ON mistake_question_crop(revision_id,child_id)''')
            connection.execute('''CREATE INDEX IF NOT EXISTS idx_mistake_question_crop_artifact
                ON mistake_question_crop(artifact_id)''')
            connection.commit()

    @staticmethod
    def _aad(child_id, mistake_id):
        return json.dumps(['mistake-question-crop-v1', child_id, mistake_id]).encode()

    def on_revision(self, connection, revision, artifact, frame_bytes):
        """The frame and its score remain fenced by the accepting revision transaction."""
        if self.cipher is None or artifact['status'] != 'completed':
            return
        details = json.loads(artifact['details_json'] or '{}')
        pipeline = details.get('local_pipeline') or {}
        orientation = (pipeline.get('quality') or {}).get('orientation_evidence') or {}
        if orientation.get('ambiguous') is not False:
            return
        marks = pipeline.get('correction_mark_candidates') or []
        strict = [mark for mark in marks if isinstance(mark, dict)
                  and isinstance(mark.get('strict_capture_facts'), dict) and isinstance(mark.get('text'), str)]
        if not strict:
            return
        try:
            frame_hash = sha256(frame_bytes).hexdigest()
            geometry = validate_geometry(pipeline.get('text_geometry'), details.get('lines'),
                                         frame_sha256=frame_hash, text=artifact['extracted_text'])
        except (ValueError, cv2.error):
            return
        if frame_hash != details.get('monitor_frame_sha256'):
            return
        image = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or [image.shape[1], image.shape[0]] != geometry['frame_size']:
            return
        for mark in strict:
            self._publish(connection, revision, geometry, image, mark['text'], mark['strict_capture_facts'])

    def _publish(self, connection, revision, geometry, image, source_text, facts):
        if (not isinstance(facts.get('question_text'), str) or not isinstance(facts.get('answer_text'), str)
                or type(facts.get('confidence')) not in (int, float) or not .90 <= facts['confidence'] <= 1):
            return
        question = DocumentMonitoringService._normalized_text(facts['question_text'])
        source_text = DocumentMonitoringService._normalized_text(source_text)
        answer = facts['answer_text'].strip()
        child_id = revision['child_id']
        row = connection.execute('''SELECT candidate.*,evidence.visual_answer_evidence_id,
                   evidence.answer_text,evidence.grading_marks_json
            FROM observed_answer_evidence AS evidence
            JOIN observed_question AS question USING(question_id,child_id)
            JOIN mistake_capture_candidate AS candidate ON candidate.candidate_id=question.mistake_candidate_id
                  AND candidate.child_id=evidence.child_id
            JOIN capture_artifact AS original ON original.artifact_id=candidate.artifact_id
                  AND original.child_id=evidence.child_id AND original.session_id=evidence.session_id
            WHERE evidence.revision_id=? AND evidence.child_id=? AND candidate.question_text IN (?,?)
              AND candidate.status='pending' AND evidence.confidence>=.90
              AND NOT EXISTS(SELECT 1 FROM paper_scoring_request WHERE candidate_id=candidate.candidate_id)''',
            (revision['revision_id'], child_id, question, source_text)).fetchone()
        if not row or not answer or row['answer_text'] != answer or json.loads(row['grading_marks_json']) != ['cross_like']:
            return
        subject_hint = subject_for_question(question, fallback='')
        nodes = self.book.knowledge_candidates(child_id, question, subject_hint or None)['items']
        if not nodes or nodes[0]['match_score'] < .35 or len(nodes[0]['match_terms']) < 2:
            return
        node = nodes[0]
        if len(nodes) > 1 and nodes[1]['match_score'] == node['match_score'] and nodes[1]['subject'] != node['subject']:
            return
        crop = self._crop(image, geometry, facts.get('crop_bbox_xywh'))
        if crop is None:
            return
        raw, width, height = crop
        candidate_id = row['candidate_id']
        action = f'auto-document-mistake:{candidate_id}'
        request = self.scoring.request(
            candidate_id=candidate_id, child_id=child_id, session_id=revision['session_id'],
            question_text=question, answer_text=answer, answer_source='visual_unverified',
            visual_answer_evidence_id=row['visual_answer_evidence_id'], client_action_id=action + ':request',
            fact_source='qualified_visual_observation', fact_confidence=facts['confidence'], _connection=connection,
        )
        decision = self.scoring._resolve(request['request_id'], child_id, action + ':score',
                                         guarded=False, _connection=connection)
        if not decision or decision['authority_type'] != 'teacher_mark' or decision['correctness'] != 0:
            return
        mistake = self.book.capture(
            child_id=child_id, knowledge_node_id=node['knowledge_node_id'], subject=node['subject'],
            title=node['title'], prompt_summary=question, submitted_answer=answer,
            source_type='document_camera', source_ref=f"{row['artifact_id']}#{candidate_id}",
            confidence=facts['confidence'], client_action_id=action + ':case',
            _connection=connection, _defer_commit=True,
        )
        mistake_id = mistake['mistake_id']
        self.book.confirm_from_qualified_scoring(mistake_id, child_id, decision['decision_id'],
                                                 decision['authority_type'], action + ':confirm', _connection=connection)
        now = utc_now()
        connection.execute('''UPDATE mistake_capture_candidate SET status='saved',mistake_id=?,updated_at=?
            WHERE candidate_id=? AND child_id=?''', (mistake_id, now, candidate_id, child_id))
        self.capture._event(connection, candidate_id, child_id, 'captured',
                            {'mistake_id': mistake_id, 'source': 'continuous_document_monitor'}, action + ':candidate', now)
        self.capture._bind_visual_promotion(connection, candidate_id, child_id, mistake_id, answer,
                                            action + ':promotion', 'qualified_scoring_authority')
        nonce = os.urandom(12)
        encrypted = nonce + self.cipher.encrypt(nonce, raw, self._aad(child_id, mistake_id))
        connection.execute('''INSERT INTO mistake_question_crop(
            candidate_id,mistake_id,child_id,revision_id,artifact_id,width,height,sha256,encrypted_jpeg
            ) VALUES(?,?,?,?,?,?,?,?,?)''', (candidate_id, mistake_id, child_id, revision['revision_id'],
                                             revision['artifact_id'], width, height, sha256(raw).hexdigest(), encrypted))

    @staticmethod
    def _crop(image, geometry, box):
        if not isinstance(box, list) or len(box) != 4 or any(type(value) is not int for value in box):
            return None
        x, y, width, height = box
        page_width, page_height = geometry['prepared_size']
        if geometry['orientation_degrees'] in (90, 270):
            page_width, page_height = page_height, page_width
        if (min(x, y) < 0 or min(width, height) < 12 or x + width > page_width or y + height > page_height
                or width * height > min(page_width * page_height * .65, 2_500_000)):
            return None
        crop_transform = np.array([[1,0,-x],[0,1,-y],[0,0,1]], dtype=float) @ np.array(geometry['frame_to_ocr'])
        crop = cv2.warpPerspective(image, crop_transform, (width, height), borderValue=(255,255,255))
        ok, encoded = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok or len(encoded) > 500_000:
            return None
        return encoded.tobytes(), width, height

    def read(self, child_id, mistake_id, source_revision=None):
        if self.cipher is None:
            return None
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            if source_revision is not None:
                from .mistake_learning import _source, _fail
                if _source(connection, child_id, mistake_id)[1] != source_revision:
                    _fail('MISTAKE_LEARNING_SOURCE_CHANGED')
            row = connection.execute('''SELECT crop.* FROM mistake_question_crop AS crop
                JOIN mistake_case AS mistake USING(mistake_id,child_id)
                JOIN child_profile USING(child_id)
                WHERE crop.mistake_id=? AND crop.child_id=? AND mistake.status<>'excluded'
                  AND child_profile.status='active' ''', (mistake_id, child_id)).fetchone()
        if not row:
            return None
        encrypted = row['encrypted_jpeg']
        try:
            raw = self.cipher.decrypt(encrypted[:12], encrypted[12:], self._aad(child_id, mistake_id))
        except (InvalidTag, ValueError):
            return None
        if sha256(raw).hexdigest() != row['sha256']:
            return None
        return {'image_base64': base64.b64encode(raw).decode('ascii'), 'media_type': 'image/jpeg',
                'width': row['width'], 'height': row['height']}
