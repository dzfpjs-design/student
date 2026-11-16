"""Adapt current original-response reviews to exact task return evidence.

This reader neither scores nor changes a learning fact. Qualification and
independence remain owned by the shared reviewed-receipt projection.
"""
def reviewed_return_evidence(c, child_id, session_id, prompts, now):
    from .pet_audio_rubric import is_speaking
    from .writing_rubric import is_writing
    from .specialized_stage_evidence import reviewed_audio_receipts, reviewed_writing_receipts
    from .specialized_stages import _history, _item_source

    if not all(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
               for table in ('rubric_assessment', 'rubric_review_event')):
        return {}
    groups, exact = {}, {}
    for prompt in prompts:
        if (prompt['child_id'] != child_id or prompt['origin_session_id'] != session_id
                or prompt['status'] != 'answered' or not prompt['attempt_id']):
            continue
        item_id = prompt['diagnostic_item_id']
        written = is_writing(c, item_id)
        if not written and not is_speaking(c, item_id):
            continue
        attempt = c.execute('''SELECT * FROM diagnostic_attempt WHERE attempt_id=?
            AND child_id=? AND session_id=? AND diagnostic_item_id=?''',
            (prompt['attempt_id'], child_id, session_id, item_id)).fetchone()
        item = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (item_id,)).fetchone()
        if not attempt or not item:
            continue
        source, _ = _item_source(c, child_id, item, now)
        if not source:
            continue
        groups.setdefault((item['knowledge_node_id'], written), {})[item_id] = source
        exact[attempt['evidence_id']] = prompt
    if not groups:
        return {}
    first, accepted = _history(c, child_id, now), {}
    for (node_id, written), sources in groups.items():
        reader = reviewed_writing_receipts if written else reviewed_audio_receipts
        receipts, _ = reader(c, child_id, node_id, sources, first, now)
        for receipt in receipts:
            prompt = exact.get(receipt['evidence_id'])
            if prompt and prompt['diagnostic_item_id'] == receipt['diagnostic_item_id']:
                accepted[receipt['evidence_id']] = {
                    **receipt,
                    'current_source_version': sources[receipt['diagnostic_item_id']]['source_version']}
    return accepted
