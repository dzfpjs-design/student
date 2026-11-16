"""Borrow reviewed-original receipts for teaching; never upgrade stored scores."""

from .local_supply import digest
from .teaching_context import _exists, _time


def project_reviewed_teaching_evidence(c, child_id, node_id, now):
    from .pet_audio_rubric import is_speaking
    from .writing_rubric import is_writing
    from .specialized_stage_evidence import reviewed_audio_receipts, reviewed_writing_receipts
    from .specialized_stages import _history, _item_source

    result = {'effective_evidence': [], 'excluded_evidence': [], 'source_versions': {},
        'local_only': True, 'cloud_transmission_allowed': False}

    def finish():
        result['evidence_fingerprint'] = digest(result)
        return result

    if not all(_exists(c, name) for name in ('diagnostic_prompt', 'rubric_assessment', 'rubric_review_event')):
        return finish()
    items = c.execute('''SELECT DISTINCT di.* FROM diagnostic_item di
        JOIN diagnostic_attempt a USING(diagnostic_item_id)
        JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id
        WHERE a.child_id=? AND e.knowledge_node_id=? AND di.knowledge_node_id=e.knowledge_node_id
        ORDER BY di.diagnostic_item_id''', (child_id, node_id)).fetchall()
    items = [item for item in items if is_writing(c, item['diagnostic_item_id'])
             or is_speaking(c, item['diagnostic_item_id'])]
    if not items:
        return finish()
    sources = {}
    for item in items:
        source, reason = _item_source(c, child_id, item, now)
        result['source_versions'][item['diagnostic_item_id']] = source or {'reason': reason}
        if source:
            sources[item['diagnostic_item_id']] = source
    # Identity/exposure checks retain the whole child's history, not just this node.
    first = _history(c, child_id, now)
    audio, audio_rejected = reviewed_audio_receipts(c, child_id, node_id, sources, first, now)
    writing, writing_rejected = reviewed_writing_receipts(c, child_id, node_id, sources, first, now)
    result['excluded_evidence'] = audio_rejected + writing_rejected
    for receipt in audio + writing:
        source = sources[receipt['diagnostic_item_id']]
        if (not source['score_qualified'] or (source['reviewed_at']
                and _time(receipt['occurred_at']) < _time(source['reviewed_at']))):
            result['excluded_evidence'].append({'evidence_id': receipt['evidence_id'],
                'reason': 'reviewed_modality_source_not_qualified_at_response'})
            continue
        result['effective_evidence'].append({**receipt, 'receipt_kind': 'diagnostic'})
    return finish()
