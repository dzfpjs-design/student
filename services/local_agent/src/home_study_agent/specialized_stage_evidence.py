"""Read current original-response score authorities without creating learning facts."""
from .learning_obligations import diagnostic_media_scope, withdrawn_evidence_ids, _question_catalog, _tutor_exposure_index, _tutor_refs
from .learning_rubric import RubricEvidenceService
from .pet_audio_rubric import is_speaking
from .pet_skill_gate import pet_skill_gate
from .teaching_context import _exists, _time


def reviewed_audio_receipts(c, child_id, node_id, sources, first, now):
    return _reviewed_modality_receipts(c, child_id, node_id, sources, first, now, written=False)


def reviewed_writing_receipts(c, child_id, node_id, sources, first, now):
    return _reviewed_modality_receipts(c, child_id, node_id, sources, first, now, written=True)


def _reviewed_modality_receipts(c, child_id, node_id, sources, first, now, *, written):
    if not all(_exists(c, name) for name in ('diagnostic_prompt', 'rubric_assessment', 'rubric_review_event')):
        return [], []
    from .database import local_media_scope
    media_scope = local_media_scope(c)
    if not written and not media_scope:
        return [], []
    from .writing_rubric import is_writing, writing_review_gate
    rows = c.execute('''SELECT a.*, e.knowledge_node_id AS evidence_node,
        e.evidence_type,e.correctness AS candidate_score,e.hint_level AS evidence_hint,
        e.independent AS evidence_independent,e.occurred_at,e.session_id AS evidence_session
        FROM diagnostic_attempt a JOIN learning_evidence e USING(evidence_id)
        WHERE a.child_id=? AND e.child_id=a.child_id AND e.knowledge_node_id=?
        ORDER BY a.created_at,a.attempt_id''', (child_id, node_id)).fetchall()
    matches = is_writing if written else is_speaking
    rows = [row for row in rows if matches(c, row['diagnostic_item_id'])]
    if not rows:
        return [], []
    # Matching receipts still require the full child's aliases and exposure history.
    withdrawn = withdrawn_evidence_ids(c, child_id)
    catalog, identities = _question_catalog(c)
    tutor_index = _tutor_exposure_index(c, child_id, catalog, identities)
    identity_nodes = {}
    for attempt in c.execute('''SELECT a.diagnostic_item_id,e.knowledge_node_id FROM diagnostic_attempt a
        JOIN learning_evidence e USING(evidence_id) WHERE a.child_id=? AND e.child_id=a.child_id''', (child_id,)):
        identity_nodes.setdefault(identities.get(attempt['diagnostic_item_id']), set()).add(attempt['knowledge_node_id'])
    accepted, rejected = [], []
    for row in rows:
        item_id, evidence_id = row['diagnostic_item_id'], row['evidence_id']
        source = sources.get(item_id)
        at = _time(row['occurred_at'])
        if written:
            gate = writing_review_gate(c, child_id, evidence_id, as_of=now.isoformat())
        else:
            context = diagnostic_media_scope(c, child_id, item_id, evidence_id, as_of=now.isoformat())
            gate = pet_skill_gate(c, child_id, item_id, answer=row['submitted_answer'], **context, **media_scope)
        reason = None
        if not source or not at or at > now or evidence_id in withdrawn:
            reason = 'reviewed_audio_source_unavailable'
        elif (row['evidence_type'] != 'diagnostic_answer' or row['session_id'] != row['evidence_session']
              or at != _time(row['created_at'])
              or c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE evidence_id=?', (evidence_id,)).fetchone()[0] != 1
              or c.execute('SELECT COUNT(*) FROM mistake_attempt WHERE evidence_id=?', (evidence_id,)).fetchone()[0]):
            reason = 'reviewed_audio_original_receipt_mismatch'
        elif gate.get('assessment_scope') not in (('reviewed_human_original_writing', 'calibrated_model_original_writing')
                if written else ('reviewed_human_original_audio',)):
            reason = 'writing_original_review_required' if written else 'speaking_original_audio_review_required'
        elif (row['evidence_independent'] not in (0, 1) or type(row['evidence_hint']) is not int
              or not 0 <= row['evidence_hint'] <= 5 or len(identity_nodes.get(source['question_identity'], set())) != 1):
            reason = 'reviewed_audio_conditions_or_identity_conflict'
        if reason:
            if written:
                reason = reason.replace('reviewed_audio_', 'reviewed_writing_')
            rejected.append({'evidence_id': evidence_id, 'reason': reason})
            continue
        if written:
            from .writing_score import current_written_score
            try:
                authority = current_written_score(c, child_id, gate['assessment_id'], now=now)
            except (ValueError, TypeError, KeyError):
                rejected.append({'evidence_id': evidence_id, 'reason': 'writing_original_review_required'})
                continue
            current, receipt, decision = authority['current'], authority['receipt'], authority['decision']
            review = dict(passed=decision['passed'], independent=receipt['independent'], hint_level=receipt['hint_level'])
            if decision['status'] == 'calibrated_model':
                review_refs = [{'assessment_id': current['assessment_id'], 'review_revision': 0,
                    'score_status': decision['status'], 'score_authority_id': decision['authority_id'],
                    'source_evidence_id': evidence_id}]
                score_authority = 'calibrated_model_original_writing_not_reference_score'
            else:
                review_refs = [{'assessment_id': current['assessment_id'], 'review_revision': current['review_revision'],
                    'event_id': decision['authority_id'], 'source_evidence_id': evidence_id}]
                score_authority = 'versioned_human_original_writing_review_not_reference_score'
        else:
            current = RubricEvidenceService(None).get(child_id, gate['assessment_id'], connection=c)
            review = current['review']
            review_refs = [{'assessment_id': current['assessment_id'], 'review_revision': current['review_revision'],
                'event_id': review['event_id'], 'source_evidence_id': evidence_id}]
            score_authority = 'versioned_human_original_audio_review_not_reference_score'
        identity = source['question_identity']
        review_task = None
        if row['review_task_id']:
            review_task = c.execute('SELECT * FROM review_task WHERE review_task_id=? AND child_id=?',
                                   (row['review_task_id'], child_id)).fetchone()
            if (not review_task or review_task['status'] != 'completed'
                    or review_task['diagnostic_item_id'] != item_id or review_task['knowledge_node_id'] != node_id
                    or review_task['result_evidence_id'] != evidence_id
                    or review_task['source_evidence_id'] in withdrawn
                    or not _time(review_task['due_at']) or at < _time(review_task['due_at'])):
                rejected.append({'evidence_id': evidence_id, 'reason': 'reviewed_audio_review_receipt_unavailable'})
                continue
            original = c.execute('SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=? AND knowledge_node_id=?',
                (review_task['source_evidence_id'], child_id, node_id)).fetchone()
            if not original or not _time(original['occurred_at']) or at <= _time(original['occurred_at']):
                rejected.append({'evidence_id': evidence_id, 'reason': 'reviewed_audio_review_source_unavailable'})
                continue
        # A reused question or prior Tutor exposure is not a new independent check.
        unseen = first.get(identity, (None, None))[1] == evidence_id
        # The original reference-only scorer marks independence false solely
        # because it cannot score speech. Recheck real hint/exposure separately;
        # the explicit later human judgment is not an upgrade of that old score.
        tutor_refs = _tutor_refs(catalog, identities, tutor_index, item_id, at)
        independent = bool(review['independent'] and not tutor_refs)
        hint = max(review['hint_level'], row['evidence_hint'])
        if not unseen and not review_task:
            independent = False
        accepted.append({'evidence_id': evidence_id, 'diagnostic_item_id': item_id,
            'knowledge_node_id': node_id, 'question_identity': identity,
            'occurred_at': row['occurred_at'], 'correctness': int(review['passed']),
            'independent': independent, 'hint_level': hint,
            'review_task_id': row['review_task_id'],
            'review_source_evidence_id': review_task['source_evidence_id'] if review_task else None,
            'review_due_at': review_task['due_at'] if review_task else None,
            'tutor_refs': tutor_refs,
            'review_refs': review_refs, 'score_authority': score_authority})
    return accepted, rejected
