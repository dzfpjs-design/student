"""One planner decision can interpret a local, version-bound pair readback."""
import json
import re

from .local_supply import digest

CHOICES = {'both', 'question_only', 'answer_only', 'uncertain'}


def pending_pair(focus):
    state = (focus or {}).get('state', {})
    pending = state.get('question_confirmation')
    if (focus and focus.get('phase') == 'remediation' and pending
            and pending.get('mode') == 'source_repair' and 'replacement_answer' in pending
            and not state.get('pending_operation') and not state.get('foundation_hold')):
        return pending
    return None


def valid_arguments(arguments, text):
    return (set(arguments) == {'utterance', 'revision_confirmation'}
            and arguments.get('utterance') == text
            and isinstance(arguments.get('revision_confirmation'), str)
            and arguments['revision_confirmation'] in CHOICES)


def affirmative(text):
    from .mistake_question_confirmation import accepts
    if not accepts(text, semantic=True):
        return False
    question = bool(re.search(r'题干|题目|问题正文', text))
    answer = bool(re.search(r'答案|作答', text))
    # Naming only one field cannot authorize the other, even if a model says both.
    return question == answer


def record(c, call_id, pending, decision, text):
    receipt = {'version': 1, 'decision': decision, 'binding_hash': digest([pending, text, decision])}
    updated = c.execute('''UPDATE model_call SET context_manifest_json=json_set(context_manifest_json,
        '$.mistake_revision_confirmation',json(?)) WHERE call_id=? AND purpose='agent_plan'
        AND status='completed' AND verification_status='verified' AND safety_status='passed'
        AND json_extract(context_manifest_json,'$.mistake_revision_confirmation') IS NULL''',
        (json.dumps(receipt), call_id)).rowcount
    if updated != 1:
        raise ValueError('mistake revision model receipt unavailable')
    return {'call_id': call_id, 'decision': decision, 'quote': text, 'pending': pending}


def matches(evidence, pending, text):
    return (isinstance(evidence, dict) and evidence.get('call_id')
            and evidence.get('pending') == pending and evidence.get('quote') == text
            and isinstance(evidence.get('decision'), str) and evidence['decision'] in CHOICES)


def verifies(c, evidence, focus, pending, text):
    if not matches(evidence, pending, text) or evidence['decision'] != 'both' or not affirmative(text):
        return False
    row = c.execute('''SELECT context_manifest_json FROM model_call WHERE call_id=? AND session_id=? AND child_id=?
        AND purpose='agent_plan' AND status='completed' AND verification_status='verified' AND safety_status='passed' ''',
        (evidence['call_id'], focus['session_id'], focus['child_id'])).fetchone()
    try:
        receipt = json.loads(row[0]).get('mistake_revision_confirmation') if row else None
    except (ValueError, TypeError, AttributeError):
        return False
    return receipt == {'version': 1, 'decision': 'both', 'binding_hash': digest([pending, text, 'both'])}
