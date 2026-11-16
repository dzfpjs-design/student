"""Current original-writing score authority, separate from stage/mastery claims."""
from datetime import datetime
import json

from .writing_response_origin import require


def written_score_candidates(c, child_id, evidence_id, *, _current=None):
    from .learning_rubric import RubricEvidenceService
    from .rubric_calibration_evidence import digest
    service = RubricEvidenceService(None)
    # The immutable originating step still identifies a corrupt payload whose
    # source field was removed or changed. Neither identity grants a score.
    rows = c.execute('''SELECT a.assessment_id,a.payload_json,a.fingerprint
        FROM rubric_assessment a LEFT JOIN agent_step s ON s.step_id=a.step_id
        LEFT JOIN agent_plan p ON p.plan_id=s.plan_id
        WHERE a.child_id=? AND (
            json_extract(CASE WHEN json_valid(a.payload_json) THEN a.payload_json ELSE '{}' END,
                         '$.source_evidence_id')=? OR
            (p.child_id=? AND json_extract(CASE WHEN json_valid(s.arguments_json)
                THEN s.arguments_json ELSE '{}' END,'$.evidence_id')=?))
        ORDER BY a.assessment_id''', (child_id, evidence_id, child_id, evidence_id)).fetchall()
    currents = []
    for row in rows:
        try:
            payload = json.loads(row['payload_json'])
            require(isinstance(payload, dict) and payload.get('source_evidence_id') == evidence_id
                and digest(payload) == row['fingerprint'], 'SCORE_HISTORY_CORRUPT')
            candidate = (_current if _current is not None and _current['assessment_id'] == row['assessment_id']
                         else service.get(child_id, row['assessment_id'], connection=c))
            require(candidate['source_status'] == 'current' and candidate['source_evidence_id'] == evidence_id,
                'SCORE_HISTORY_CORRUPT')
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            raise ValueError('WRITING_SCORE_HISTORY_CORRUPT') from error
        currents.append(candidate)
    return currents


def latest_human_review(c, child_id, currents):
    """Cross-assessment corrections follow the immutable journal, not UUIDs."""
    reviewed = [candidate for candidate in currents if candidate.get('review')]
    if not reviewed:
        return None
    def order(candidate):
        review = candidate['review']
        row = c.execute('''SELECT rowid FROM rubric_review_event
            WHERE event_id=? AND child_id=? AND assessment_id=? AND revision=?''',
            (review['event_id'],child_id,candidate['assessment_id'],review['review_revision'])).fetchone()
        require(row is not None, 'SCORE_HISTORY_CORRUPT')
        return row[0]
    return max(reviewed, key=order)


def current_written_score(c, child_id, assessment_id, *, now):
    from .learning_rubric import RubricEvidenceService
    service = RubricEvidenceService(None)
    current = service.get(child_id, assessment_id, connection=c)
    evidence_id = current['source_evidence_id']
    require(evidence_id, 'SCORE_BINDING_CHANGED')
    currents = written_score_candidates(c, child_id, evidence_id, _current=current)
    selected = latest_human_review(c, child_id, currents)
    if selected:
        # A correction/retraction applies to the original answer, not only its step.
        # Do not fall back to another assessment when the latest human decision fails.
        require(selected['assessment_id'] == assessment_id, 'SCORE_AUTHORITY_NOT_CURRENT')
        return _assessment_score(c, child_id, selected, now=now)
    qualified = []
    for candidate in currents:
        try:
            score = _assessment_score(c, child_id, candidate, now=now)
        except (ValueError, KeyError, TypeError):
            continue
        qualified.append(score)
    require(len(qualified) == 1, 'SCORE_AUTHORITY_AMBIGUOUS')
    require(qualified[0]['current']['assessment_id'] == assessment_id, 'SCORE_AUTHORITY_NOT_CURRENT')
    return qualified[0]


def _assessment_score(c, child_id, current, *, now):
    from .learning_rubric import RubricEvidenceService
    from .writing_rubric import prepare_writing_review, current_writing_review, writing_dimensions, has_writing_review_evidence
    service = RubricEvidenceService(None)
    assessment_id = current['assessment_id']
    if current.get('review') or current.get('review_revision', 0):
        require(has_writing_review_evidence(current), 'SCORE_HUMAN_DECISION_REQUIRED')
    original = prepare_writing_review(c, child_id, assessment_id)
    review = current.get('review')
    if review or current.get('review_revision', 0):
        require(current_writing_review(c, child_id, current), 'SCORE_HUMAN_DECISION_REQUIRED')
        receipt = service.reviewed_receipt(c, child_id, current['step_id'],
            assessment_id=assessment_id, now=now)
        require(receipt is not None, 'SCORE_RECEIPT_UNAVAILABLE')
        at = datetime.fromisoformat(review['created_at'])
        decision = dict(status='reviewed_human', authority_id=review['event_id'],
            created_at=review['created_at'], passed=review['passed'], review_revision=current['review_revision'])
    else:
        receipt = service.calibrated_receipt(c, child_id, current['step_id'],
            assessment_id=assessment_id, now=now)
        require(isinstance(receipt, dict) and receipt.get('score_status') == 'calibrated_model',
            'SCORE_CALIBRATION_REQUIRED')
        proof = receipt['calibration_receipt']
        require(proof.get('score_qualified') is True and
            proof['assessment_id'] == assessment_id == receipt['assessment_id'] and
            proof['source_evidence_id'] == current['source_evidence_id'] and
            proof['diagnostic_item_id'] == original['diagnostic_item_id'], 'SCORE_BINDING_CHANGED')
        definitions = writing_dimensions(c, child_id, original['diagnostic_item_id'])
        definition_keys = ('dimension_id', 'criterion', 'max_score', 'pass_score', 'score_anchors')
        expected = {d['dimension_id']:{k:d[k] for k in definition_keys} for d in definitions}
        scores = proof['dimensions']
        require(isinstance(scores, list) and len(scores) == len(expected) and
            {d['dimension_id'] for d in scores} == set(expected), 'SCORE_DIMENSIONS_CHANGED')
        for dimension in scores:
            require({k:dimension[k] for k in definition_keys} == expected[dimension['dimension_id']] and
                type(dimension['score']) is int and 0 <= dimension['score'] <= dimension['max_score'],
                'SCORE_DIMENSIONS_CHANGED')
        passed = all(d['score'] >= d['pass_score'] for d in scores)
        require(type(proof['passed']) is bool and proof['passed'] == passed and
            type(receipt['correctness']) is int and receipt['correctness'] == int(passed), 'SCORE_RESULT_CHANGED')
        # The certificate admits the scorer, not a made-up human decision.
        # The original assessment time is stable across plan regeneration.
        at = datetime.fromisoformat(current['created_at'])
        decision = dict(status='calibrated_model', authority_id=receipt['evidence_id'],
            created_at=current['created_at'], passed=passed, review_revision=0)
    require(at.tzinfo is not None and at <= now, 'SCORE_TIME_INVALID')
    return dict(current=current, receipt=receipt, decision=decision,
        original_binding_version=original['binding_version'],
        local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)
