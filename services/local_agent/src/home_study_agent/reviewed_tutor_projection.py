"""D015: bounded question-only projection of a currently reviewed source.

This does not grant a public reply outlet or export the source document/rubric.
Runtime text consent and exact source revalidation remain mandatory.
"""
from .cloud_privacy import build_tutor_cloud_context, sanitize_cloud_text, sensitive_categories

POLICY_VERSION = 'reviewed_question_projection_v1'
MAX_QUESTION_CHARS = 520


def eligible_reviewed_source(connection, child_id, item):
    import json
    from .curriculum_first_source import PREFIX
    from .curriculum_objective_source import VALIDATOR, parse
    from .local_supply import validate_item
    source=str(item['source_ref'])
    if source.startswith('parent_approved_specialized:'):
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_supply_package'").fetchone():
            return False
        package=connection.execute('''SELECT * FROM curriculum_supply_package
            WHERE candidate_id=? AND child_id=? AND state='published' ''',
            (source[len('parent_approved_specialized:'):],child_id)).fetchone()
        if not package:
            return False
        payload=json.loads(package['payload_json'])
        if payload.get('validator')!=VALIDATOR or payload.get('stage')!='parallel':
            return False
        original=parse(json.dumps(payload.get('objective_source')))
        if original['prompt']!=item['prompt'] or original['subject']!=item['subject']:
            return False
    elif not source.startswith(PREFIX):
        return False
    # validate_item checks publication/review events, current child scope,
    # source license/effective dates, exact excerpt, answer and standard version.
    validate_item(connection, child_id, item['diagnostic_item_id'])
    consent = connection.execute("""SELECT allowed FROM consent_record
        WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1""").fetchone()
    if not consent or not consent[0]:
        return False
    names = [r[0] for r in connection.execute('SELECT nickname FROM child_profile')]
    question = item['prompt']
    return (0 < len(question) <= MAX_QUESTION_CHARS
            and not item.get('stimulus_text') and not item.get('stimulus_asset')
            and not sensitive_categories(question)
            and sanitize_cloud_text(question, names).text == question)


def is_projected(target):
    return (target.get('status') == 'resolved'
            and target.get('question_content_cloud_reason') == POLICY_VERSION
            and not target.get('question_content_cloud_blocked', True))


def source_receipt(connection, item):
    """Local fingerprint input; never a field in the provider projection."""
    import json
    from .curriculum_first_source import PREFIX
    from .teaching_context import _hash
    source=str(item['source_ref'])
    if source.startswith('parent_approved_specialized:'):
        from .curriculum_objective_source import VALIDATOR
        row=connection.execute('''SELECT version,payload_json,source_snapshot_json FROM curriculum_supply_package
            WHERE candidate_id=? AND state='published' ''', (source[len('parent_approved_specialized:'):],)).fetchone()
        if not row or json.loads(row['payload_json']).get('validator')!=VALIDATOR:
            raise ValueError('REVIEWED_SOURCE_MISSING')
        return {'source_family':VALIDATOR,'review_version':row['version'],
            'source_snapshot_hash':_hash(json.loads(row['source_snapshot_json'])),
            'reviewed_payload_hash':_hash(json.loads(row['payload_json']))}
    row = connection.execute('''SELECT version,source_snapshot_json FROM curriculum_first_source
        WHERE candidate_id=?''', (item['source_ref'][len(PREFIX):],)).fetchone()
    if not row:
        raise ValueError('REVIEWED_SOURCE_MISSING')
    return {'review_version': row['version'], 'source_snapshot_hash': _hash(json.loads(row['source_snapshot_json']))}


def cloud_context(*, subject, question, known_child_names):
    # Do not include task title, recall, region, profile or source metadata.
    context = build_tutor_cloud_context(grade='未传输', curriculum_region='未传输',
        subject=subject, question=question, task=None, recalls=[],
        known_child_names=known_child_names, knowledge_context=[])
    context.manifest['source_projection'] = {
        'policy_version': POLICY_VERSION, 'decision_ref': 'D015',
        'fields': ['subject', 'question'], 'max_question_chars': MAX_QUESTION_CHARS,
        'source_document_included': False, 'answer_key_included': False,
        'child_history_included': False,
    }
    return context
