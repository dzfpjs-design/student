"""D-030: a Runtime-owned speech export, separate from protected input facts.

Only the transport's accepted JSON field may be bound to a turn. This module
has no HTTP writer, renderer permission flags, or model-defined public flag.
"""
import hashlib
import json

from .cloud_privacy import sanitize_cloud_text, sensitive_categories
from .child_safety import classify_child_safety

VERSION = 'public_reply_projection_v1'
REJECTION_ACTOR = 'public_reply_rejection_diagnostic_v1'
REJECTION_REASONS = frozenset({
    'TRANSPORT_PROVENANCE_MISSING', 'CALL_NOT_VERIFIED', 'ARRIVAL_SCOPE_CHANGED',
    'CALL_SAFETY_REJECTED', 'TRANSPORT_FALLBACK_USED', 'TRANSPORT_NOT_NATURAL_STOP',
    'TRANSPORT_IDENTITY_MISSING', 'CONFIGURED_ROUTE_MISMATCH', 'PROVIDER_MODEL_MISMATCH',
    'CONTENT_HASH_MISMATCH', 'MESSAGE_HASH_MISMATCH', 'JSON_FIELD_INVALID',
    'SPOKEN_TRANSITION_CHANGED', 'CONVERSATION_REPLY_CHANGED', 'REPLY_TEXT_POLICY_REJECTED',
    'PROTECTED_REPLY_TEXT', 'PROTECTED_REQUEST_CONTEXT',
})
PROSE = {'question', 'answer', 'response', 'response_text', 'transcript', 'text',
         'narration', 'claim', 'content', 'summary', 'prompt', 'title'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def approved_routes(settings):
    # A changed route revokes existing projections. No keys are inspected here.
    return {(settings.model_provider, name, hashlib.sha256(settings.model_base_url.encode()).hexdigest())
            for name in (settings.model_name, settings.voice_intent_model_name) if name}


def protected_prose(value, protected=False):
    if isinstance(value, dict):
        protected = protected or value.get('local_only') is True or value.get('cloud_transmission_allowed') is False
        for key, item in value.items():
            if protected and key in PROSE and isinstance(item, str) and item.strip():
                yield item.strip()
            elif isinstance(item, (dict, list, tuple)):
                yield from protected_prose(item, protected)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from protected_prose(item, protected)


def _call(c, call_id):
    return c.execute('SELECT * FROM model_call WHERE call_id=?', (call_id,)).fetchone()


def _call_fingerprint(row):
    return digest([row[k] for k in ('call_id', 'child_id', 'session_id', 'purpose', 'provider',
        'model', 'status', 'prompt_version', 'policy_version', 'context_manifest_json',
        'verification_status', 'safety_status', 'completed_at')])


def _read(c, audit_id):
    row = c.execute('SELECT summary_json FROM audit_event WHERE audit_id=? AND actor=?',
                    (audit_id, VERSION)).fetchone()
    return json.loads(row[0]) if row else None


def _append(c, audit_id, child_id, value, now):
    c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
              (audit_id, VERSION, 'public_reply_source', child_id, json.dumps(value, ensure_ascii=False), now))


def _rejected(c, call_id, reason, row=None):
    """Persist only a closed reason enum, under the existing model-call owner.

    This is a diagnostic, not a source receipt. It never changes the completed
    model call, candidate/turn provenance or an output authority decision.
    """
    if reason not in REJECTION_REASONS:
        raise ValueError('PUBLIC_REPLY_DIAGNOSTIC_REASON_INVALID')
    row = row if row is not None else _call(c, call_id)
    if row is not None and row['child_id'] and row['purpose'] in ('agent_plan', 'agent_plan_repair'):
        value = {'version': REJECTION_ACTOR, 'reason': reason, 'export_allowed': False}
        c.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(audit_id) DO NOTHING''',
            ('public-reply-rejection-' + digest(call_id), REJECTION_ACTOR, 'public_reply_rejected',
             row['child_id'], json.dumps(value, sort_keys=True, separators=(',', ':')),
             row['completed_at'] or row['created_at']))
    return None


def _transport_rejection_reason(row, arrival, result, proof, messages, routes):
    # Called only after the original, unchanged transport eligibility predicate
    # rejected. The diagnostic does not replace or authorize that predicate.
    if not row or row['status'] != 'completed' or row['verification_status'] != 'verified':
        return 'CALL_NOT_VERIFIED'
    if not arrival or arrival['child_id'] != row['child_id'] or arrival['session_id'] != row['session_id']:
        return 'ARRIVAL_SCOPE_CHANGED'
    if row['safety_status'] != 'passed':
        return 'CALL_SAFETY_REJECTED'
    if result.fallback_used:
        return 'TRANSPORT_FALLBACK_USED'
    if proof.get('finish_reason') != 'stop':
        return 'TRANSPORT_NOT_NATURAL_STOP'
    if not proof.get('reported_model') or not proof.get('response_id'):
        return 'TRANSPORT_IDENTITY_MISSING'
    if (proof.get('provider'), proof.get('requested_model'), proof.get('endpoint_sha256')) not in routes:
        return 'CONFIGURED_ROUTE_MISMATCH'
    if ((row['provider'], row['model']) != (result.provider, result.model)
            or (result.provider, result.model) != (proof.get('provider'), proof.get('requested_model'))):
        return 'PROVIDER_MODEL_MISMATCH'
    if proof.get('content_sha256') != hashlib.sha256(result.content.encode()).hexdigest():
        return 'CONTENT_HASH_MISMATCH'
    return 'MESSAGE_HASH_MISMATCH'


def record_candidate(c, *, call_id, action_id, result, decision, messages, context, known_child_names, routes):
    """Called only after the real Gateway and deterministic planner validation.

    Return an opaque local reference, never text that a caller may authorize.
    Uncertain provenance, tainted requests and any changed JSON field fail closed.
    """
    proof = getattr(result, 'reply_provenance', None)
    if not isinstance(proof, dict) or proof.get('version') != 'public_reply_transport_v1':
        return _rejected(c, call_id, 'TRANSPORT_PROVENANCE_MISSING')
    row = _call(c, call_id)
    arrival = c.execute('SELECT * FROM learning_input_arrival WHERE client_action_id=?', (action_id,)).fetchone()
    if (not row or row['status'] != 'completed' or row['verification_status'] != 'verified'
            or not arrival or arrival['child_id'] != row['child_id'] or arrival['session_id'] != row['session_id']
            or row['safety_status'] != 'passed' or result.fallback_used
            or proof.get('finish_reason') != 'stop' or not proof.get('reported_model') or not proof.get('response_id')
            or (proof.get('provider'), proof.get('requested_model'), proof.get('endpoint_sha256')) not in routes
            or (row['provider'], row['model']) != (result.provider, result.model)
            or (result.provider, result.model) != (proof.get('provider'), proof.get('requested_model'))
            or proof.get('content_sha256') != hashlib.sha256(result.content.encode()).hexdigest()
            or proof.get('messages_sha256') != digest(messages)):
        return _rejected(c, call_id, _transport_rejection_reason(row, arrival, result, proof, messages, routes), row)
    try:
        body = json.loads(result.content)
        text = body['spoken_transition']
    except (ValueError, KeyError, TypeError):
        return _rejected(c, call_id, 'JSON_FIELD_INVALID', row)
    if not isinstance(text, str) or text != decision.spoken_transition:
        return _rejected(c, call_id, 'SPOKEN_TRANSITION_CHANGED', row)
    texts = [text]
    # conversation.respond executes its actual response field, which can differ
    # from the transition. Neither truncation nor a locally substituted reply is eligible.
    if decision.step.capability.name == 'conversation.respond':
        response = (body.get('next_step') or {}).get('arguments', {}).get('response')
        if response != decision.step.arguments.get('response') or not isinstance(response, str):
            return _rejected(c, call_id, 'CONVERSATION_REPLY_CHANGED', row)
        texts.append(response)
    if any(not item.strip() or len(item) > 1000 or sensitive_categories(item) or classify_child_safety(item)
           or sanitize_cloud_text(item, known_child_names).text != item for item in texts):
        return _rejected(c, call_id, 'REPLY_TEXT_POLICY_REJECTED', row)
    # Refuse local material even if the provider copied it into a fresh reply.
    request_text = json.dumps(messages, ensure_ascii=False)
    if any(any(part in item for item in texts) or part in request_text for part in protected_prose(context)):
        reason = ('PROTECTED_REPLY_TEXT' if any(any(part in item for item in texts)
                  for part in protected_prose(context)) else 'PROTECTED_REQUEST_CONTEXT')
        return _rejected(c, call_id, reason, row)
    value = {'version': VERSION, 'call_id': call_id, 'session_id': row['session_id'],
             'client_action_id': action_id, 'arrival_id': arrival['arrival_id'],
             'transcript_sha256': digest(arrival['utterance']),
             'child_id': row['child_id'], 'reply_hashes': sorted({digest(item) for item in texts}), 'transport': proof,
             'call_fingerprint': _call_fingerprint(row), 'prompt_version': row['prompt_version'],
             'policy_version': row['policy_version'], 'mastery_update_allowed': False}
    _append(c, 'public-reply-model-' + call_id, row['child_id'], value, row['completed_at'])
    return call_id


def bind(c, turn_id, call_id):
    if not call_id:
        return False
    source = _read(c, 'public-reply-model-' + call_id)
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
    if (not source or not turn or source['child_id'] != turn['child_id'] or source['session_id'] != turn['session_id']
            or source['client_action_id'] != turn['client_action_id']
            or source['transcript_sha256'] != digest(turn['transcript'])
            or digest(turn['response_text']) not in source['reply_hashes']):
        return False
    value = {**source, 'agent_turn_id': turn_id, 'client_action_id': turn['client_action_id'],
             'transcript_sha256': digest(turn['transcript'])}
    _append(c, 'public-reply-turn-' + turn_id, turn['child_id'], value, turn['created_at'])
    return True


def inspect(c, turn, routes):
    source = _read(c, 'public-reply-turn-' + turn['agent_turn_id'])
    if not isinstance(source, dict) or source.get('version') != VERSION:
        raise ValueError('PUBLIC_REPLY_SOURCE_MISSING')
    candidate = _read(c, 'public-reply-model-' + source['call_id'])
    row = _call(c, source['call_id'])
    proof = source['transport']
    if (not candidate or not row or {k: source[k] for k in candidate} != candidate
            or source['child_id'] != turn['child_id'] or source['session_id'] != turn['session_id']
            or source['agent_turn_id'] != turn['agent_turn_id'] or source['client_action_id'] != turn['client_action_id']
            or source['transcript_sha256'] != digest(turn['transcript'])
            or digest(turn['response_text']) not in source['reply_hashes'] or source['call_fingerprint'] != _call_fingerprint(row)
            or (proof['provider'], proof['requested_model'], proof['endpoint_sha256']) not in routes):
        raise ValueError('PUBLIC_REPLY_SOURCE_CHANGED')
    return digest(source)
