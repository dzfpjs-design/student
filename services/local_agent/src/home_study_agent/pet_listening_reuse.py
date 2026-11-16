"""Reference a still-reviewed instructional recording, never replay its delivery."""
import json
from uuid import uuid4

VERSION = 'pet_listening_source_reuse_v1'


def _latest_review(c, media_id):
    return c.execute("""SELECT * FROM pet_media_event WHERE media_id=? AND operation='review_source'
        ORDER BY rowid DESC LIMIT 1""", (media_id,)).fetchone()


def reused_source(c, row):
    """Resolve the exact original artifact across sessions, within one child."""
    from .pet_media_evidence import PetMediaEvidenceError, _source, _hash, _prompt, _date, _now
    ref = json.loads(row['state_json']).get('source_reuse')
    fields = {'version', 'media_id', 'approval_event_id', 'source_version', 'audio_sha256'}
    if not isinstance(ref, dict) or set(ref) != fields or ref['version'] != VERSION:
        raise PetMediaEvidenceError('listening_reuse_binding_invalid')
    original = c.execute('SELECT * FROM pet_media_binding WHERE media_id=?', (ref['media_id'],)).fetchone()
    if (not original or original['kind'] != 'listening' or row['kind'] != 'listening'
            or any(original[key] != row[key] for key in
                ('child_id', 'diagnostic_item_id', 'item_version', 'artifact_id', 'source_version'))):
        raise PetMediaEvidenceError('listening_reuse_owner_or_source_changed')
    state = json.loads(original['state_json'])
    if state.get('source_reuse') or state['status'] == 'cancelled':
        raise PetMediaEvidenceError('listening_reuse_original_unavailable')
    _prompt(c, original['session_id'], original['prompt_id'], original['item_version'])
    if state.get('delivery_requirement') != json.loads(row['state_json']).get('delivery_requirement'):
        raise PetMediaEvidenceError('listening_reuse_requirement_changed')
    review = _latest_review(c, original['media_id'])
    if (not review or review['event_id'] != ref['approval_event_id']
            or json.loads(review['payload_json']).get('operation') != 'approve'
            or _date(review['created_at']) > _now()):
        raise PetMediaEvidenceError('listening_reuse_approval_changed')
    source = _source(c, original)
    if (_hash(source) != ref['source_version'] or original['source_version'] != ref['source_version']
            or json.loads(original['audio_json']).get('sha256') != ref['audio_sha256']
            or json.loads(row['audio_json']).get('sha256') != ref['audio_sha256']):
        raise PetMediaEvidenceError('listening_reuse_source_changed')
    return source


def listening_source_review(c, row):
    """Keep the parent's original receipt; a new local retraction still vetoes it."""
    local = _latest_review(c, row['media_id'])
    ref = json.loads(row['state_json']).get('source_reuse')
    if not ref:
        return local
    from .pet_media_evidence import PetMediaEvidenceError
    try:
        reused_source(c, row)
    except (PetMediaEvidenceError, ValueError, TypeError, KeyError):
        return None
    return local or _latest_review(c, ref['media_id'])


def bind_reviewed_listening_resource(c, session_id, child_id, prompt_id, item_version):
    """Borrow a write transaction. No audio copy, new approval, or playback event."""
    from .database import local_media_scope
    from .pet_media_evidence import PetMediaEvidenceError, PetMediaEvidenceService, _current, _prompt, _now, _date, _json
    from .pet_listening_requirement import listening_requirement
    if not c.in_transaction:
        raise PetMediaEvidenceError('listening_reuse_transaction_required')
    prompt, item = _prompt(c, session_id, prompt_id, item_version)
    if prompt['child_id'] != child_id or prompt['status'] != 'waiting_answer':
        raise PetMediaEvidenceError('listening_reuse_prompt_unavailable')
    scope = local_media_scope(c)
    missing = {'status': 'missing', 'media_id': None}
    if not scope:
        return missing
    existing = c.execute('''SELECT * FROM pet_media_binding WHERE child_id=? AND session_id=?
        AND prompt_id=? AND kind='listening' ''', (child_id, session_id, prompt_id)).fetchall()
    if existing:
        if len(existing) != 1:
            return {'status': 'ambiguous', 'media_id': None}
        _, _, _, audio, reasons = _current(c, existing[0], **scope)
        review = listening_source_review(c, existing[0])
        valid = (not reasons and json.loads(existing[0]['state_json'])['status'] != 'cancelled'
            and audio['status'] == 'usable_signal' and review
            and json.loads(review['payload_json']).get('operation') == 'approve'
            and _date(review['created_at']) <= _now())
        return {'status': 'ready' if valid else 'unavailable', 'media_id': existing[0]['media_id']}
    requirement = listening_requirement(c, child_id, item['diagnostic_item_id'])
    if requirement['status'] != 'known':
        return missing
    candidates = c.execute('''SELECT * FROM pet_media_binding WHERE child_id=? AND diagnostic_item_id=?
        AND item_version=? AND kind='listening' AND prompt_id!=?''',
        (child_id, item['diagnostic_item_id'], item_version, prompt_id)).fetchall()
    valid = []
    for original in candidates:
        state = json.loads(original['state_json'])
        if state.get('source_reuse') or state['status'] == 'cancelled' or state.get('delivery_requirement') != requirement:
            continue
        _, _, _, audio, reasons = _current(c, original, **scope)
        review = _latest_review(c, original['media_id'])
        if (not reasons and audio['status'] == 'usable_signal' and review
                and json.loads(review['payload_json']).get('operation') == 'approve'
                and _date(review['created_at']) <= _now()):
            valid.append((original, review))
    if len(valid) != 1:
        return {'status': 'ambiguous' if valid else 'missing', 'media_id': None}
    original, review = valid[0]
    ref = {'version': VERSION, 'media_id': original['media_id'], 'approval_event_id': review['event_id'],
        'source_version': original['source_version'], 'audio_sha256': json.loads(original['audio_json'])['sha256']}
    state = dict(status='paused', generation=1, revision=1, runtime_epoch=None, frame_position=0,
        completed_at=None, started=None, delivery_requirement=requirement, source_reuse=ref)
    row = dict(original)
    row.update(media_id=uuid4().hex, session_id=session_id, prompt_id=prompt_id,
        state_json=_json(state), assessment_id=None, created_at=_now().isoformat())
    c.execute(f"INSERT INTO pet_media_binding ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
    request = {'operation': 'reuse_source', 'session_id': session_id, 'prompt_id': prompt_id,
        'item_version': item_version, 'source_reuse': ref}
    PetMediaEvidenceService._event(c, row, 'listening-source-reuse:' + prompt_id, 'reuse_source', request, ref)
    return {'status': 'ready', 'media_id': row['media_id']}
