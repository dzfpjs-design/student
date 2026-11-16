"""Exact paper request provenance; no fabricated diagnostic attempts or scores."""
import json
from hashlib import sha256

from .paper_subjective_rubric import digest

VERSION = 'paper_response_origin_v1'
REQUEST_FIELDS = ('request_id', 'candidate_id', 'child_id', 'session_id', 'question_text',
                  'answer_text', 'answer_source', 'visual_answer_evidence_id',
                  'question_sha256', 'answer_sha256', 'fact_source', 'fact_confidence',
                  'created_at', 'client_action_id')


def snapshot(c, child_id, request_id):
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return snapshot(c, child_id, request_id)
        finally:
            c.rollback()
    request = c.execute('''SELECT r.*,ch.grade AS original_grade,
        ch.curriculum_region AS original_curriculum_region FROM paper_scoring_request r
        JOIN daily_session s ON s.session_id=r.session_id AND s.child_id=r.child_id
        JOIN child_profile ch ON ch.child_id=r.child_id AND ch.status='active'
        WHERE r.request_id=? AND r.child_id=?''', (request_id, child_id)).fetchone()
    if not request:
        raise ValueError('SUBJECTIVE_ORIGINAL_SCOPE_INVALID')
    if any(sha256(request[field].encode()).hexdigest() != request[key] for field, key in
           (('question_text', 'question_sha256'), ('answer_text', 'answer_sha256'))):
        raise ValueError('SUBJECTIVE_ORIGINAL_REQUEST_CORRUPT')
    candidate = c.execute('''SELECT mc.*,a.session_id AS original_session_id
        FROM mistake_capture_candidate mc JOIN capture_artifact a
          ON a.artifact_id=mc.artifact_id AND a.child_id=mc.child_id
        WHERE mc.candidate_id=? AND mc.child_id=?''', (request['candidate_id'], child_id)).fetchone()
    if not candidate or candidate['original_session_id'] != request['session_id']:
        raise ValueError('SUBJECTIVE_ORIGINAL_SCOPE_INVALID')
    visual, occurrence, revision = None, None, None
    if request['visual_answer_evidence_id']:
        visual = c.execute('''SELECT e.* FROM observed_answer_evidence e
            JOIN observed_question q ON q.question_id=e.question_id AND q.child_id=e.child_id
            WHERE e.visual_answer_evidence_id=? AND e.child_id=?
              AND q.mistake_candidate_id=?''',
            (request['visual_answer_evidence_id'], child_id, request['candidate_id'])).fetchone()
        if (not visual or visual['session_id'] != request['session_id']
                or visual['confidence'] < .75
                or ''.join(visual['answer_text'].split()).casefold() != ''.join(request['answer_text'].split()).casefold()):
            raise ValueError('SUBJECTIVE_ORIGINAL_ANSWER_CHANGED')
    artifact_id = visual['artifact_id'] if visual else candidate['artifact_id']
    artifact = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?',
                         (artifact_id, child_id, request['session_id'])).fetchone()
    if not artifact or artifact['artifact_type'] != 'document_ocr' or artifact['status'] not in {'completed', 'quality_warning'}:
        raise ValueError('SUBJECTIVE_ORIGINAL_PAGE_UNAVAILABLE')
    try:
        details = json.loads(artifact['details_json'])
    except (ValueError, TypeError) as error:
        raise ValueError('SUBJECTIVE_ORIGINAL_PAGE_INVALID') from error
    if not isinstance(details, dict):
        raise ValueError('SUBJECTIVE_ORIGINAL_PAGE_INVALID')
    # Paths and processing timestamps are not answer identities. OCR content,
    # crop provenance and confidence are, even when the question text is unchanged.
    page = {key: artifact[key] for key in ('artifact_id', 'child_id', 'session_id',
                                           'artifact_type', 'extracted_text', 'confidence')}
    page['details'] = details
    if visual:
        occurrence = c.execute('''SELECT * FROM observed_question_occurrence
            WHERE occurrence_id=? AND child_id=? AND artifact_id=? AND revision_id=? AND question_id=?''',
            (visual['occurrence_id'], child_id, artifact['artifact_id'], visual['revision_id'], visual['question_id'])).fetchone()
        revision = c.execute('''SELECT * FROM document_page_revision
            WHERE revision_id=? AND child_id=? AND session_id=? AND artifact_id=?''',
            (visual['revision_id'], child_id, request['session_id'], artifact['artifact_id'])).fetchone()
        if not occurrence or not revision:
            raise ValueError('SUBJECTIVE_ORIGINAL_REVISION_CHANGED')
        text = '\n'.join(line.strip() for line in (artifact['extracted_text'] or '').splitlines() if line.strip())[:12000]
        if sha256(text.encode()).hexdigest() != revision['content_sha256']:
            raise ValueError('SUBJECTIVE_ORIGINAL_REVISION_CHANGED')
    binding = dict(version=VERSION, child_id=child_id, session_id=request['session_id'],
        grade=request['original_grade'], curriculum_region=request['original_curriculum_region'],
        request_id=request_id, candidate_id=request['candidate_id'], artifact_id=artifact['artifact_id'],
        visual_answer_evidence_id=request['visual_answer_evidence_id'],
        modality='original_paper_text' if visual else 'confirmed_readback_text',
        original_written_answer_available=bool(visual),
        request_sha256=digest({key:request[key] for key in REQUEST_FIELDS}),
        candidate_sha256=digest({key:candidate[key] for key in
                                  ('candidate_id','artifact_id','child_id','question_text','region_json','confidence')}),
        page_sha256=digest(page),
        visual_sha256=digest(dict(visual)) if visual else None,
        occurrence_sha256=digest(dict(occurrence)) if occurrence else None,
        revision_sha256=digest(dict(revision)) if revision else None,
        local_only=True, cloud_transmission_allowed=False)
    return {**binding, 'source_version': digest(binding)}
