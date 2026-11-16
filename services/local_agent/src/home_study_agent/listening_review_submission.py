"""Retain rejected speech references without saving an ungradeable review answer.

Receipts are local annotations on the already-owned answer turn, not attempts.
They follow that turn's existing export/deletion lifecycle and contain no speech
or media copies. New explicit answer requests still use Runtime's original gate.
"""
import hashlib
import json
from datetime import datetime

VERSION = 'listening_review_deferred_v1'
FIELD = 'listening_review_deferred'
DELIVERY_REQUIRED = 'listening_review_delivery_required'


def scheduled_review(c, row):
    if not row['review_task_id'] or not c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='agent_plan'").fetchone():
        return False
    from .pet_task_execution import REVIEW_VERSION
    return bool(c.execute('''SELECT 1 FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(context_manifest_json,'$.goal_prompt_binding.prompt_id')=?
        AND json_extract(context_manifest_json,'$.goal_prompt_binding.pet')=1
        AND json_extract(context_manifest_json,'$.goal_prompt_binding.binding.version')=?
        AND json_extract(context_manifest_json,'$.goal_prompt_binding.binding.review_task_id')=?''',
        (row['child_id'], row['origin_session_id'], row['prompt_id'], REVIEW_VERSION,
         row['review_task_id'])).fetchone())


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':')).encode()).hexdigest()


def _receipts(c, row):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='agent_turn'").fetchone():
        return []
    return [json.loads(r[0]) for r in c.execute('''SELECT j.value FROM agent_turn t,
        json_each(t.action_json, '$.listening_review_deferred') j
        WHERE t.child_id=? AND t.session_id=? AND json_extract(j.value,'$.version')=?
        ORDER BY t.rowid DESC''', (row['child_id'], row['origin_session_id'], VERSION)).fetchall()]


def _request(row, answer, hint_level, authorized_turn_id):
    return _hash({k: row[k] for k in ('child_id', 'origin_session_id', 'prompt_id', 'item_version')} |
        {'answer': answer, 'hint_level': hint_level, 'authorized_turn_id': authorized_turn_id})


def rejected_replay(c, row, *, answer, hint_level, authorized_turn_id, client_action_id):
    from .diagnostic_prompt import DiagnosticPromptError
    for receipt in _receipts(c, row):
        if receipt['client_action_id'] == client_action_id:
            expected = _request(row, answer, hint_level, authorized_turn_id)
            if expected != receipt['request_sha256']:
                raise DiagnosticPromptError('answer_payload_conflict')
            raise DiagnosticPromptError(DELIVERY_REQUIRED)


def _turn_time(c, row, turn_id):
    from .diagnostic_prompt import DiagnosticPromptError
    turn = c.execute('''SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?''',
        (turn_id, row['child_id'], row['origin_session_id'])).fetchone()
    if not turn:
        raise DiagnosticPromptError('listening_review_answer_turn_required')
    action = json.loads(turn['action_json'])
    at = datetime.fromisoformat(turn['created_at'])
    if action.get('voice_input_id'):
        capture = c.execute('''SELECT created_at FROM voice_capture_binding
            WHERE voice_input_id=? AND child_id=? AND session_id=? AND status='completed' ''',
            (action['voice_input_id'], row['child_id'], row['origin_session_id'])).fetchone()
        if not capture:
            raise DiagnosticPromptError('listening_review_answer_turn_required')
        at = min(at, datetime.fromisoformat(capture[0]))
    return dict(turn), at


def _delivered(c, row, at):
    from .database import local_media_scope
    from .pet_listening_requirement import listening_requirement
    from .pet_listening_scope import delivered_before
    scope = local_media_scope(c)
    if not scope or not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_media_binding'").fetchone():
        return False
    requirement = listening_requirement(c, row['child_id'], row['diagnostic_item_id'])
    return bool(delivered_before(c, row, requirement, cutoff=at, **scope))


def admit(c, row, *, answer, hint_level, authorized_turn_id, client_action_id):
    """After Runtime validation, same answer transaction. Commit returned refusal."""
    if not c.in_transaction:
        raise RuntimeError('listening review submission transaction required')
    if not scheduled_review(c, row):
        return None
    from .agent_runtime import AgentRuntimeStore
    from .repository import utc_now
    from .diagnostic_prompt import DiagnosticPromptError
    if not authorized_turn_id:
        raise DiagnosticPromptError('listening_review_answer_turn_required')
    resolved = AgentRuntimeStore.resolve_local_answer_submission(c, authorized_turn_id, 'diagnostic.submit')
    turn, at = _turn_time(c, row, authorized_turn_id)
    if _delivered(c, row, at):
        return None
    source, _ = _turn_time(c, row, resolved['source_turn_id'])
    action = json.loads(turn['action_json'])
    receipts = action.get(FIELD, [])
    receipts.append({'version': VERSION, 'client_action_id': client_action_id,
        'request_sha256': _request(row, answer, hint_level, authorized_turn_id),
        'prompt_id': row['prompt_id'], 'item_version': row['item_version'],
        'source_turn_id': resolved['source_turn_id'],
        'source_voice_input_id': resolved.get('source_voice_input_id'),
        'source_text_sha256': _hash(source['transcript']), 'rejected_at': utc_now()})
    c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.listening_review_deferred',json(?)) WHERE agent_turn_id=?",
        (json.dumps(receipts, ensure_ascii=True), authorized_turn_id))
    return DELIVERY_REQUIRED


def scoring_independence(c, row):
    """Recheck delivery at the new request, never at retry/score wall time."""
    if not scheduled_review(c, row):
        return True
    from .agent_runtime import AgentRuntimeStore
    from .diagnostic_prompt import DiagnosticPromptError
    _, at = _turn_time(c, row, row['authorized_turn_id'])
    if not _delivered(c, row, at):
        raise DiagnosticPromptError(DELIVERY_REQUIRED)
    resolved = AgentRuntimeStore.resolve_local_answer_submission(c, row['authorized_turn_id'], 'diagnostic.submit')
    _, source_at = _turn_time(c, row, resolved['source_turn_id'])
    return _delivered(c, row, source_at) and not any(
        r['prompt_id'] == row['prompt_id'] and r['item_version'] == row['item_version']
        for r in _receipts(c, row))


def deferred_view(c, row):
    """Exact retained local speech only. Never a pending formal answer."""
    for receipt in _receipts(c, row):
        if (receipt['prompt_id'], receipt['item_version']) != (row['prompt_id'], row['item_version']):
            continue
        source = c.execute('''SELECT transcript FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?''',
            (receipt['source_turn_id'], row['child_id'], row['origin_session_id'])).fetchone()
        if not source or _hash(source[0]) != receipt['source_text_sha256']:
            continue
        voice = receipt.get('source_voice_input_id')
        retained = False
        if voice:
            from .voice_capture_binding import resolve_voice_capture, VoiceCaptureBindingError
            try:
                capture = resolve_voice_capture(c, session_id=row['origin_session_id'],
                    child_id=row['child_id'], voice_input_id=voice, transcript=source[0])
                from .agent_runtime import AgentRuntimeStore
                retained = AgentRuntimeStore.capture_matches_answer(capture, 'submit_diagnostic_answer',
                    {'prompt_id': row['prompt_id'], 'item_version': row['item_version']})
            except VoiceCaptureBindingError:
                pass
        waiting = retained and row['status'] == 'waiting_answer'
        from .repository import utc_now
        complete = _delivered(c, row, datetime.fromisoformat(utc_now())) if waiting else None
        return {'version': VERSION, 'source_turn_id': receipt['source_turn_id'],
            'source_voice_input_id': voice if retained else None, 'speech_retained': retained,
            'formal_answer_saved': False, 'automatic_submission_allowed': False,
            'new_confirmation_required': waiting, 'delivery_complete': complete,
            'local_only': True, 'cloud_transmission_allowed': False,
            'message': (('已经听完了；刚才的话还留着，请重新明确确认答案。'
                if complete else '这道题还没听完，我先记住你刚才说的；听完后再确认答案') if waiting else None)}
    return None
