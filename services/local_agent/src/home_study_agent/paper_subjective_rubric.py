"""Freeze explicit local source criteria without granting score authority."""
from hashlib import sha256
import json
import re
from uuid import uuid4

from .repository import utc_now

VERSION = 'paper_subjective_source_spec_v2'
TYPE = 'paper_subjective_rubric_spec'
POINT = re.compile(r'^(?:\d+[.、]\s*)?(.{4,300}?)[（(](\d+(?:\.\d+)?)分[）)]\s*[。.]?$')


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def extract_spec(question, source):
    text = source.get('text', '')
    if not isinstance(text, str) or not isinstance(question, str) or not 8 <= len(question.strip()) <= 12000:
        raise ValueError('SUBJECTIVE_EXACT_SOURCE_REQUIRED')
    if text.count(question.strip()) != 1 or len(text) > 3000:
        raise ValueError('SUBJECTIVE_SOURCE_BLOCK_AMBIGUOUS')
    confidence = source.get('ocr_confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not .88 <= confidence <= 1:
        raise ValueError('SUBJECTIVE_SOURCE_OCR_LOW')
    # A narrow source block has one exact question followed by explicit printed
    # criteria. Unlabelled solutions and LLM task-point paraphrases abstain.
    tail = text.split(question.strip(), 1)[1].strip()
    match = re.fullmatch(r'评分标准\s*[:：]?\s*\n(.+)', tail, re.DOTALL)
    if not match:
        raise ValueError('SUBJECTIVE_EXPLICIT_CRITERIA_MISSING')
    lines = [line.strip() for line in match[1].splitlines() if line.strip()]
    if not 1 <= len(lines) <= 8:
        raise ValueError('SUBJECTIVE_CRITERIA_COUNT_INVALID')
    points = []
    for line in lines:
        point = POINT.fullmatch(line)
        if not point or not 0 < float(point[2]) <= 20:
            raise ValueError('SUBJECTIVE_CRITERION_POINTS_INVALID')
        points.append(dict(criterion=point[1], maximum_score=float(point[2]), source_quote=line,
                           point_id=digest(line)))
    if len({point['criterion'] for point in points}) != len(points):
        raise ValueError('SUBJECTIVE_DUPLICATE_CRITERION')
    if sum(point['maximum_score'] for point in points) > 100:
        raise ValueError('SUBJECTIVE_TOTAL_SCORE_INVALID')
    return dict(version=VERSION, question_sha256=digest(question), source_version=source['source_version'],
                source_text_sha256=digest(text), source_candidate_id=source['candidate_id'],
                criteria=points, maximum_score=sum(point['maximum_score'] for point in points),
                score_authority_granted=False, independent_calibration_required=True,
                local_only=True, cloud_transmission_allowed=False)


class PaperSubjectiveRubricService:
    def __init__(self, database):
        self.database = database

    def freeze(self, child_id, request_id, source):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            request = c.execute('SELECT * FROM paper_scoring_request WHERE request_id=? AND child_id=?',
                                (request_id, child_id)).fetchone()
            if not request:
                raise ValueError('SUBJECTIVE_REQUEST_SCOPE_INVALID')
            from .paper_scoring_source_match import PaperScoringSourceMatchService
            obligation = c.execute('SELECT obligation_id FROM paper_scoring_supply_obligation WHERE request_id=? AND child_id=?',
                                   (request_id, child_id)).fetchone()
            if not obligation:
                raise ValueError('SUBJECTIVE_SOURCE_SCOPE_INVALID')
            candidates = PaperScoringSourceMatchService(self.database).valid_candidates(obligation[0], connection=c)
            current = next((item for item in candidates if item['candidate_id'] == source.get('candidate_id')), None)
            if current is None or current != source:
                raise ValueError('SUBJECTIVE_SOURCE_CHANGED')
            from .paper_response_origin import snapshot
            spec = {**extract_spec(request['question_text'], current), 'request_id': request_id,
                    'original_response': snapshot(c, child_id, request_id)}
            fingerprint = digest(spec)
            action_id = 'paper-subjective-spec:' + fingerprint
            existing = c.execute('SELECT artifact_id,details_json FROM capture_artifact WHERE client_action_id=?', (action_id,)).fetchone()
            if existing:
                if json.loads(existing['details_json']) != spec:
                    raise ValueError('SUBJECTIVE_SPEC_CORRUPT')
                return dict(artifact_id=existing['artifact_id'], spec_sha256=fingerprint, **spec)
            artifact_id = str(uuid4())
            c.execute("""INSERT INTO capture_artifact VALUES(?,?,?,?,NULL,NULL,NULL,?,'completed',?,?)""",
                      (artifact_id, child_id, request['session_id'], TYPE, json.dumps(spec, ensure_ascii=False), utc_now(), action_id))
            c.commit()
            return dict(artifact_id=artifact_id, spec_sha256=fingerprint, **spec)

    def current(self, child_id, artifact_id, *, connection=None):
        if connection is not None:
            return self._current(connection, child_id, artifact_id)
        with self.database.connect() as c:
            return self._current(c, child_id, artifact_id)

    def _current(self, c, child_id, artifact_id):
        if not c.in_transaction:
            c.execute('BEGIN')
            try:
                return self._current(c, child_id, artifact_id)
            finally:
                c.rollback()
        row = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND artifact_type=?',
                        (artifact_id, child_id, TYPE)).fetchone()
        if not row:
            raise ValueError('SUBJECTIVE_SPEC_UNKNOWN')
        spec = json.loads(row['details_json'])
        if not isinstance(spec, dict) or row['client_action_id'] != 'paper-subjective-spec:' + digest(spec):
            raise ValueError('SUBJECTIVE_SPEC_CORRUPT')
        from .paper_scoring_source_match import PaperScoringSourceMatchService
        obligation = c.execute('SELECT obligation_id FROM paper_scoring_supply_obligation WHERE request_id=? AND child_id=?',
                               (spec['request_id'], child_id)).fetchone()
        candidates = PaperScoringSourceMatchService(self.database).valid_candidates(obligation[0], connection=c) if obligation else []
        request = c.execute('SELECT question_text FROM paper_scoring_request WHERE request_id=? AND child_id=?',
                            (spec['request_id'], child_id)).fetchone()
        source = next((item for item in candidates if item['candidate_id'] == spec['source_candidate_id']), None)
        valid = source is not None and request and digest(request[0]) == spec['question_sha256']
        if valid:
            valid = digest(source['text']) == spec['source_text_sha256'] and source['source_version'] == spec['source_version']
        status = 'calibration_pending' if valid else 'source_withdrawn'
        reason = None if valid else 'SUBJECTIVE_RUBRIC_SOURCE_CHANGED'
        if valid:
            if spec.get('version') != VERSION or not isinstance(spec.get('original_response'), dict):
                status, reason = 'original_binding_required', 'SUBJECTIVE_ORIGINAL_BINDING_REQUIRED'
            else:
                from .paper_response_origin import snapshot
                try:
                    original = snapshot(c, child_id, spec['request_id'])
                    if original != spec['original_response'] or row['session_id'] != original['session_id']:
                        status, reason = 'source_withdrawn', 'SUBJECTIVE_ORIGINAL_RESPONSE_CHANGED'
                except ValueError as error:
                    status, reason = 'source_withdrawn', str(error)
        return dict(artifact_id=artifact_id, status=status, reason_code=reason,
                    spec_sha256=digest(spec), **spec)

    def execution_context(self, child_id, artifact_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.execution_context(child_id, artifact_id, connection=c)
        if not connection.in_transaction:
            connection.execute('BEGIN')
            try:
                return self.execution_context(child_id, artifact_id, connection=connection)
            finally:
                connection.rollback()
        spec = self.current(child_id, artifact_id, connection=connection)
        if spec['status'] != 'calibration_pending':
            raise ValueError(spec['reason_code'])
        request = connection.execute('SELECT * FROM paper_scoring_request WHERE request_id=? AND child_id=?',
                                     (spec['request_id'], child_id)).fetchone()
        return dict(version='paper_subjective_execution_context_v1',
                    artifact_id=artifact_id, spec_sha256=spec['spec_sha256'],
                    original_response=spec['original_response'],
                    question=request['question_text'], response=request['answer_text'],
                    criteria=spec['criteria'], maximum_score=spec['maximum_score'],
                    score_authority_granted=False, independent_calibration_required=True,
                    local_only=True, cloud_transmission_allowed=False)
