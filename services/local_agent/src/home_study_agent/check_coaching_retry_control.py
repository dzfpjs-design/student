"""Explicit local recovery of the canonical saved-check coaching request."""
from datetime import datetime, timezone
import json
import re
from .tutor_check_coaching import action_id, record
from .tutor_check_response import _scope, authorized_runtime_response, TutorCheckError
from .tutor_request_reservation import PENDING_STALE_AFTER
from .voice_scope import text_consent_version, scope_valid

CONTROL = 'learning.retry_check_coaching'
CAPABILITY = 'tutor.retry_check_coaching'
VERSION = 'check_coaching_retry_control_v1'


def explicit(text):
    return bool(re.fullmatch(
        r'(?:(?:请|麻烦你|可以|能不能|能否|能)(?:帮我)?|帮我)?(?:重试|再试(?:一下|一次)?|重新(?:请求|生成))'
        r'(?:一下)?(?:刚才|这次|上次)?(?:的)?(?:检查回答的)?点评(?:吧|一下|吗)?[。！!？?\s]*'
        r'|retry (?:the |that )?feedback[.!\s]*', text, re.I))


def _owned(c, session_id, child_id, feedback_id):
    session = _scope(c, session_id)
    if session['child_id'] != child_id:
        raise TutorCheckError('TUTOR_CHECK_RETRY_SCOPE_CHANGED')
    feedback = c.execute('SELECT * FROM tutor_feedback WHERE feedback_id=? AND session_id=? AND child_id=?',
        (feedback_id, session_id, child_id)).fetchone()
    if not feedback or not (feedback['client_action_id'] or '').startswith('tutor-check:'):
        raise TutorCheckError('TUTOR_CHECK_RETRY_ORIGIN_CHANGED')
    origin_id = feedback['client_action_id'][len('tutor-check:'):]
    origin, args = authorized_runtime_response(c, origin_id)
    if (origin['session_id'] != session_id or origin['child_id'] != child_id
            or args['turn_id'] != feedback['turn_id'] or feedback['response_text'] != args['response']):
        raise TutorCheckError('TUTOR_CHECK_RETRY_ORIGIN_CHANGED')
    parent, _, _, proof = record(c, session_id, child_id, feedback['turn_id'], feedback_id)
    reservation = c.execute('SELECT * FROM tutor_request_reservation WHERE action_id=?',
        (action_id(session_id, feedback_id),)).fetchone()
    if not reservation or reservation['session_id'] != session_id or reservation['child_id'] != child_id or reservation['parent_turn_id'] != parent['turn_id']:
        raise TutorCheckError('TUTOR_CHECK_RETRY_RESERVATION_CHANGED')
    return parent, proof, reservation


def snapshot(c, session_id, child_id):
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tutor_request_reservation'").fetchone():
            return None
        row = c.execute("SELECT feedback_id FROM tutor_feedback WHERE session_id=? AND child_id=? AND evidence_basis='tutor_check_response' ORDER BY rowid DESC LIMIT 1",
            (session_id, child_id)).fetchone()
        if not row:
            return None
        _, proof, reservation = _owned(c, session_id, child_id, row['feedback_id'])
        unknown = reservation['status'] == 'interrupted'
        if reservation['status'] == 'pending':
            updated = datetime.fromisoformat(reservation['updated_at'].replace('Z', '+00:00'))
            unknown = datetime.now(timezone.utc) - updated >= PENDING_STALE_AFTER
            if not unknown:
                return None
        elif reservation['status'] not in {'failed', 'interrupted'}:
            return None
        return {'version': VERSION, 'proof': proof, 'action_id': reservation['action_id'],
            'request_hash': reservation['request_hash'], 'attempts': reservation['attempts'],
            'consent_version': list(text_consent_version(c) or ()),
            'confirm_unknown_outcome_retry': unknown}
    except (ValueError, KeyError, TypeError):
        return None


def target(c, session_id, child_id, frozen):
    if not isinstance(frozen, dict) or frozen.get('version') != VERSION:
        raise TutorCheckError('TUTOR_CHECK_RETRY_SCOPE_CHANGED')
    parent, proof, reservation = _owned(c, session_id, child_id, frozen['proof']['feedback_id'])
    if (frozen.get('consent_version') != list(text_consent_version(c) or ())
            or proof != frozen['proof'] or reservation['action_id'] != frozen['action_id']
            or reservation['request_hash'] != frozen['request_hash']
            or reservation['attempts'] not in {frozen['attempts'], frozen['attempts'] + 1}):
        raise TutorCheckError('TUTOR_CHECK_RETRY_SCOPE_CHANGED')
    # Before the claim the reservation must still be eligible; after it, only
    # the canonical next attempt can dispatch/persist, fenced by the service.
    if reservation['attempts'] == frozen['attempts'] and snapshot(c, session_id, child_id) != frozen:
        raise TutorCheckError('TUTOR_CHECK_RETRY_SCOPE_CHANGED')
    return {'feedback_id': proof['feedback_id'], 'parent_turn_id': parent['turn_id'],
        'subject': parent['subject'], 'question': parent['question'],
        'tutor_request_action_id': frozen['action_id'],
        'confirm_unknown_outcome_retry': frozen['confirm_unknown_outcome_retry']}


def authorized(c, turn_id, *, before_claim=False):
    from .voice_control_intent import VoiceControlIntent
    row = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
    native=bool(row and json.loads(row['action_json']).get('source')=='qwen_realtime_capability')
    if not row or not native and not explicit(row['transcript']):
        raise TutorCheckError('TUTOR_CHECK_RETRY_NOT_REQUESTED')
    validated = VoiceControlIntent.inspect_execution(c, turn_id, allow_planned=True)
    if not validated or validated.get('control') != CONTROL:
        raise TutorCheckError('TUTOR_CHECK_RETRY_SCOPE_CHANGED')
    frozen = validated['targets'].get('check_coaching_retry') or {}
    if before_claim:
        reservation = c.execute('SELECT attempts FROM tutor_request_reservation WHERE action_id=?',
            (frozen.get('action_id'),)).fetchone()
        if not reservation or reservation['attempts'] != frozen.get('attempts'):
            raise TutorCheckError('TUTOR_CHECK_RETRY_ALREADY_ATTEMPTED')
    if not scope_valid(c, row['session_id'], row['child_id'], row['client_action_id']):
        raise TutorCheckError('TUTOR_CHECK_RETRY_SCOPE_CHANGED')
    from .agent_runtime import AgentRuntimeStore
    voice_id = json.loads(row['action_json']).get('voice_input_id')
    if not voice_id:
        raise TutorCheckError('TUTOR_CHECK_RETRY_VOICE_REQUIRED')
    AgentRuntimeStore.resolve_captured_voice(c, session_id=row['session_id'], child_id=row['child_id'],
        voice_input_id=voice_id, transcript=row['transcript'])
    return row, target(c, row['session_id'], row['child_id'], validated['targets'].get('check_coaching_retry'))


def replay(c, turn_id):
    row = c.execute("SELECT t.*,s.result_json,s.status AS step_status,s.capability_name FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0 WHERE t.agent_turn_id=?", (turn_id,)).fetchone()
    if not row or row['capability_name'] != CAPABILITY or row['step_status'] != 'completed':
        return None
    _scope(c, row['session_id'])
    result = json.loads(row['result_json'])
    taught = result.get('tutor_turn')
    if taught:
        from .tutor_check_coaching import validate_saved
        saved = c.execute('SELECT * FROM tutor_turn WHERE turn_id=? AND session_id=? AND child_id=?',
            (taught['turn_id'], row['session_id'], row['child_id'])).fetchone()
        if not saved:
            raise TutorCheckError('TUTOR_CHECK_RETRY_RECEIPT_CHANGED')
        validate_saved(c, saved, json.loads(saved['output_envelope_json']))
    return {**result, 'replayed': True, 'replay_audio_allowed': False}
