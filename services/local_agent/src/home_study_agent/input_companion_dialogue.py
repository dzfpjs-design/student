"""Allowlisted companion context derived from existing turns and receipts."""
import json
import hashlib

from .learning_input_revision import _version
from .voice_control_intent import VoiceControlIntent


def _object(value):
    try:
        result = json.loads(value or '{}')
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def context(c, child_id, session_id, *, turn_id=None):
    from .learning_input_return import snapshot
    from .agent_runtime import AgentRuntimeStore
    draft = snapshot(c, child_id, session_id, turn_id=turn_id)
    if not draft:
        return None
    consent = list(VoiceControlIntent.text_consent_version(c) or ())
    rows = c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,s.status AS step_status
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.child_id=? AND t.session_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id
        AND (? IS NULL OR t.agent_turn_id!=?) ORDER BY t.rowid DESC LIMIT 4''',
        (child_id, session_id, turn_id, turn_id)).fetchall()
    accepted = []
    expected = rows[0]['agent_turn_id'] if rows else None
    for row in rows:
        marker = _object(row['context_manifest_json']).get('input_companion_dialogue')
        if not isinstance(marker, dict) or not marker:
            break
        if (row['agent_turn_id'] != expected or row['tool_status'] != 'completed'
                or row['plan_status'] == 'cancelled' or row['step_status'] == 'cancelled'
                or marker.get('root_request_id') != draft['request_id']
                or marker.get('source_version') != draft['source_version'] or marker.get('consent') != consent):
            return None
        journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=? AND child_id=? AND session_id=?',
                            (marker.get('request_key'), child_id, session_id)).fetchone()
        if not journal or journal['status'] != 'completed':
            return None
        decision = _object(journal['decision_json'])
        cache = _object(journal['execution_receipt_json'])
        bound = _object(journal['binding_json'])
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, row['transcript']], ensure_ascii=False).encode()).hexdigest()
        if (decision not in ({'status':'control','control':'companion.chat'}, {'status':'non_control','role':'companion_reply'})
                or journal['fingerprint'] != fingerprint or cache.get('binding') != bound
                or not isinstance(bound.get('input_return'), dict)
                or bound['input_return'].get('request_id') != draft['request_id']
                or bound['input_return'].get('source_version') != draft['source_version']
                or bound.get('input_return_consent') != consent
                or cache.get('kind') != 'input_companion_response_v1' or cache.get('response') != row['response_text']
                or cache.get('source') != _object(row['action_json']).get('source')):
            return None
        action = _object(row['action_json'])
        try:
            if action.get('voice_input_id'):
                AgentRuntimeStore.resolve_captured_voice(c, session_id=session_id, child_id=child_id,
                    voice_input_id=action['voice_input_id'], transcript=row['transcript'])
        except ValueError:
            return None
        accepted.append((row['agent_turn_id'], row['transcript'], row['response_text'], marker, cache))
        expected = marker.get('previous_turn_id')
        if not expected:
            break
    if not accepted:
        return None
    messages = []
    for _, utterance, response, _, _ in reversed(accepted):
        messages.extend([{'role':'user','content':utterance}, {'role':'assistant','content':response}])
    return {'focus': {'last_turn_id': accepted[0][0], 'version': _version(accepted), 'root_request_id':draft['request_id']},
            'messages': tuple(messages)}


def validate(c, child_id, session_id, focus):
    current = context(c, child_id, session_id)
    if not current or current['focus'] != focus:
        raise ValueError('INPUT_COMPANION_CONTEXT_CHANGED')
    return current['messages']
