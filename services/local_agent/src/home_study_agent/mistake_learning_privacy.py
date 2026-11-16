"""Local export and erasure of the formative notebook's durable records."""
TABLES = ('mistake_learning_action', 'mistake_learning_turn', 'mistake_learning_item', 'mistake_learning_original_review',
          'mistake_learning_focus', 'mistake_learning_state',
          'mistake_learning_intake_action', 'mistake_learning_card')
TOPIC_TABLES = ('mistake_learning_topic',)


def _exists(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def export_learning(c, child_id):
    result = {t: [dict(r) for r in c.execute('SELECT * FROM '+t+' WHERE child_id=?', (child_id,))]
              for t in TOPIC_TABLES + TABLES if _exists(c, t)}
    if _exists(c, 'mistake_learning_card_revision') and _exists(c, 'mistake_learning_card'):
        result['mistake_learning_card_revision'] = [dict(r) for r in c.execute('''
            SELECT r.* FROM mistake_learning_card_revision r JOIN mistake_learning_card x USING(mistake_id)
            WHERE x.child_id=?''', (child_id,))]
    return result


def purge_learning(c, child_id):
    # Items/turns/actions precede their source rows. Other children's records
    # remain untouched, including identically worded questions.
    counts = {}
    for table in TOPIC_TABLES + TABLES:
        if _exists(c, table):
            counts[table] = c.execute('DELETE FROM '+table+' WHERE child_id=?', (child_id,)).rowcount
    if _exists(c, 'capture_artifact'):
        from .local_ocr.cache_lifecycle import delete_scope
        captures = c.execute('''SELECT artifact_id FROM capture_artifact WHERE child_id=?
            AND (artifact_type='mistake_review_ocr' OR client_action_id LIKE 'mistake-review-ocr:%')''', (child_id,)).fetchall()
        for capture in captures:
            delete_scope(c, child_id, artifact_id=capture['artifact_id'])
            c.execute('DELETE FROM capture_artifact WHERE artifact_id=? AND child_id=?', (capture['artifact_id'], child_id))
        counts['mistake_review_ocr'] = len(captures)
    if _exists(c, 'audit_event'):
        counts['mistake_learning_operational_audits'] = c.execute('''DELETE FROM audit_event
            WHERE child_id=? AND (actor LIKE 'native_mistake_%' OR action='mistake_learning_saved')''',
            (child_id,)).rowcount
    return counts


def validate_renderer_drafts(child_id, entries):
    """Accept only this child's exact local draft keys, preserving raw strings."""
    from urllib.parse import quote
    import json
    if entries is None:
        return []
    if not isinstance(entries, list) or len(entries) > 4096:
        raise ValueError('MISTAKE_DRAFT_EXPORT_LIMIT')
    child = quote(child_id, safe="~()*!.'-")
    prefixes = ('hsc-cloud-mistake-draft-v1:' + child + ':', 'hsc-cloud-mistake-pending-v1:' + child + ':')
    intake = 'hsc-cloud-mistake-intake-v1:' + child
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'key', 'value'} or not all(isinstance(entry[k], str) for k in ('key', 'value')):
            raise ValueError('MISTAKE_DRAFT_EXPORT_INVALID')
        key = entry['key']
        if key in seen or not (key == intake or any(key.startswith(p) for p in prefixes)):
            raise ValueError('MISTAKE_DRAFT_EXPORT_CHILD_SCOPE')
        seen.add(key)
    if len(json.dumps(entries, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 8 * 1024 * 1024:
        raise ValueError('MISTAKE_DRAFT_EXPORT_LIMIT')
    return entries
