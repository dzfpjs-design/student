"""A new original answer to the same question, never a rewrite of an attempt."""
import json

from .local_supply import LocalSupplyError, validate_item
from .spoken_numeric import FORMAT, parse


def offer(c, row):
    packet = _offer(c, row)
    return packet if packet and not source_reason(c, row) else None


def _offer(c, row, *, live=True):
    from .diagnostic_prompt import DiagnosticPromptService, _version
    if row['status'] != 'answered':
        return None
    session = c.execute('''SELECT ds.status,ds.state,cp.status AS child_status
        FROM daily_session ds JOIN child_profile cp USING(child_id) WHERE session_id=?''',
        (row['origin_session_id'],)).fetchone()
    if not session or (live and (session['status'] != 'active' or session['child_status'] != 'active' or session['state'] in {'evolving','completed'})):
        return None
    receipt = DiagnosticPromptService._receipt(c, row)
    if not receipt or receipt['correctness'] is not None:
        return None
    from .learning_obligations import withdrawn_evidence_ids
    if receipt['evidence_id'] in withdrawn_evidence_ids(c, row['child_id']):
        return None
    try:
        item = validate_item(c, row['child_id'], row['diagnostic_item_id'])
        current = DiagnosticPromptService._item(c, row['child_id'], row['diagnostic_item_id'])
        if not current or _version(current) != row['item_version']:
            return None
        rubric = json.loads(item['expected_answer_json'])
        if rubric.get('answer_format') != FORMAT or parse(receipt['submitted_answer'], rubric.get('numeric_kind')) is not None:
            return None
        if row['authorized_turn_id']:
            from .historical_answer_source import validate_historical_diagnostic_answer
            validate_historical_diagnostic_answer(c, row['authorized_turn_id'], child_id=row['child_id'],
                session_id=row['origin_session_id'], prompt_id=row['prompt_id'], item_version=row['item_version'],
                item_id=row['diagnostic_item_id'], answer=receipt['submitted_answer'])
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return None
    return dict(parent_prompt_id=row['prompt_id'], parent_item_version=row['item_version'],
                parent_attempt_id=receipt['attempt_id'])


def validate(c, session, packet, *, existing, preferred_item_id, track, review_task_id):
    from .diagnostic_prompt import DiagnosticPromptError, DiagnosticPromptService
    parent = DiagnosticPromptService._owned(c, session, packet['parent_prompt_id'])
    if (offer(c, parent) != packet or parent['diagnostic_item_id'] != preferred_item_id
            or parent['review_task_id'] != review_task_id or json.loads(parent['item_json'])['track'] != track):
        raise DiagnosticPromptError('numeric_clarification_source_changed')
    latest = c.execute('SELECT prompt_id FROM diagnostic_prompt WHERE origin_session_id=? ORDER BY rowid DESC LIMIT 1',
        (session['session_id'],)).fetchone()
    if not existing and (not latest or latest[0] != parent['prompt_id']):
        raise DiagnosticPromptError('numeric_clarification_focus_changed')
    return parent


def source_reason(c, row, *, live=True):
    try:
        # Strictly decreasing rowids bound the full ancestry without recursion.
        while True:
            request = json.loads(row['request_json'])
            if not isinstance(request,dict):
                return 'numeric_clarification_source_changed'
            packet = request.get('numeric_clarification')
            if packet is None:
                return None
            parent = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?
                AND origin_session_id=? AND rowid<(SELECT rowid FROM diagnostic_prompt WHERE prompt_id=?)''',
                (packet['parent_prompt_id'],row['child_id'],row['origin_session_id'],row['prompt_id'])).fetchone()
            if (not parent or _offer(c,parent,live=live) != packet or parent['diagnostic_item_id'] != row['diagnostic_item_id']
                    or parent['item_version'] != row['item_version'] or parent['review_task_id'] != row['review_task_id']):
                return 'numeric_clarification_source_changed'
            row = parent
    except (ValueError,KeyError,TypeError):
        return 'numeric_clarification_source_changed'


def history_reason(c, prompt_id):
    row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt_id,)).fetchone()
    try:
        request = json.loads(row['request_json'])
        if not isinstance(request, dict):
            return 'numeric_clarification_source_changed'
        if request.get('numeric_clarification') is None:
            return None
    except (ValueError, KeyError, TypeError):
        return 'numeric_clarification_source_changed'
    # This is historical practice identity, never permission to submit again.
    return source_reason(c, row, live=False) or 'numeric_clarification_same_item'
