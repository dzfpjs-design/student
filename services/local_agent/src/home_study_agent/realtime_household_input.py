"""Canonical, explicitly local-ASR first utterance; never cloud/audio evidence."""
import json
import re
from datetime import datetime, timezone
from .household_voice_entry import SOURCE, HouseholdEntryConflict, text_hash
from .privacy_operation_receipt import generation

ACTOR = 'household_local_input_v1'
MODEL_PREFIX = 'server_verified_household_local_asr:'


def _action(receipt):
    value = receipt.get('receipt_id') if type(receipt) is dict else None
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value):
        raise HouseholdEntryConflict('HOUSEHOLD_ASR_RECEIPT_INVALID')
    return 'household-entry-' + value


def verify_household_witness(c, voice, *, allow_safety=False):
    from .agent_runtime import AgentRuntimeStore
    from .realtime_capability_tools import _hash, _immutable_capture
    from .realtime_native_safety import native_input_blocked
    if (voice['purpose'] != SOURCE or voice['status'] != 'completed' or voice['local_path'] is not None
            or type(voice['model_version']) is not str or not voice['model_version'].startswith(MODEL_PREFIX)
            or not allow_safety and native_input_blocked(c, voice)):
        return False
    row = c.execute('SELECT actor,summary_json FROM audit_event WHERE audit_id=?',
                    ('household-input-' + voice['voice_input_id'],)).fetchone()
    if not row or row['actor'] != ACTOR:
        return False
    try:
        witness = json.loads(row['summary_json'])
        capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=voice['session_id'],
            child_id=voice['child_id'], voice_input_id=voice['voice_input_id'], transcript=voice['transcript'])
        expected = {'version': 1, 'voice_input_id': voice['voice_input_id'], 'session_id': voice['session_id'],
            'child_id': voice['child_id'], 'client_action_id': voice['client_action_id'],
            'storage_generation': generation(c), 'transcript_sha256': text_hash(voice['transcript']),
            'capture_sha256': _hash(_immutable_capture(capture)), 'receipt': witness['receipt'],
            'raw_audio_available': False, 'cloud_asr': False}
        return (witness == expected and capture.get('source_kind') == SOURCE
            and witness['receipt'].get('origin_scope') == 'unassigned_household'
            and witness['receipt'].get('storage_generation') == generation(c)
            and _action(witness['receipt']) == voice['client_action_id']
            and witness['receipt']['transcript_sha256'] == text_hash(voice['transcript'])
            and voice['model_version'] == MODEL_PREFIX + witness['receipt']['model_version'])
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def record_household_input(captures, media_ai, session_id, value, *, authorize):
    from .realtime_capability_tools import _hash, _immutable_capture
    from .realtime_native_safety import protect_native_input, native_safety_control
    fields = {'version', 'source_kind', 'entry_receipt', 'transcript', 'child_id'}
    if (type(value) is not dict or set(value) != fields or type(value['version']) is not int
            or value['version'] != 1 or value['source_kind'] != SOURCE
            or type(value['transcript']) is not str or not value['transcript'].strip()
            or len(value['transcript']) > 16000 or type(value['child_id']) is not str):
        raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_INVALID')
    action = _action(value['entry_receipt'])
    text = value['transcript'].strip()

    def inspect(c):
        owner = c.execute('''SELECT d.child_id,d.status,p.status AS child_status FROM daily_session d
            JOIN child_profile p ON p.child_id=d.child_id WHERE d.session_id=?''', (session_id,)).fetchone()
        consent = c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
        if (not owner or owner['status'] != 'active' or owner['child_status'] != 'active'
                or owner['child_id'] != value['child_id']):
            raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_OWNER_CHANGED')
        if not consent or any(type(consent[k]) is not int or consent[k] != 1
                for k in ('cloud_audio_allowed', 'cloud_model_text_allowed')):
            raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_CONSENT_REQUIRED')
        if authorize(c, session_id, value['child_id']) is not True:
            raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_HOST_UNCONFIRMED')

    def result(c, voice):
        control = native_safety_control(c, voice['voice_input_id'])
        turns = c.execute("SELECT tool_status FROM agent_turn WHERE child_id=? AND session_id=? AND json_extract(action_json,'$.voice_input_id')=?", (voice['child_id'], session_id, voice['voice_input_id'])).fetchall()
        state = 'pending_read_only' if any(r[0] in ('pending','planned','running','unknown','unconfirmed') for r in turns) else 'processed' if turns else 'unprocessed'
        return {'version': 1, 'entry_state': state, 'status': 'completed', 'voice_input_id': voice['voice_input_id'],
            'client_action_id': action, 'purpose': SOURCE, 'source_kind': SOURCE,
            'raw_audio_available': False, 'cloud_asr': False, 'model_provenance': 'server_local_asr_receipt',
            **({'local_control': control} if control else {})}

    with captures.database.connect() as c:
        inspect(c)
        existing = c.execute('SELECT * FROM voice_input WHERE client_action_id=?', (action,)).fetchone()
        if existing:
            if ((existing['child_id'], existing['session_id'], existing['transcript'])
                    != (value['child_id'], session_id, text)):
                raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_OWNER_CHANGED')
            saved = c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?',
                ('household-input-' + existing['voice_input_id'],)).fetchone()
            if (not saved or _hash(json.loads(saved[0]).get('receipt')) != _hash(value['entry_receipt'])
                    or not verify_household_witness(c, existing, allow_safety=True)):
                raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_WITNESS_CHANGED')
            return result(c, existing)
        original_generation = generation(c)
    receipt = media_ai.household_entries.verify(value['entry_receipt'], text)
    if receipt.get('origin_scope') != 'unassigned_household' or receipt.get('storage_generation') != original_generation:
        raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_GENERATION_CHANGED')
    reference = {'child_id': value['child_id'], 'local_recovery_only': False}
    captures.begin(session_id, action, None, SOURCE, reference, source_kind='realtime_transcript',
                   source_model=MODEL_PREFIX + receipt['model_version'])

    def witness(c, captured):
        inspect(c)
        if generation(c) != original_generation:
            raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_GENERATION_CHANGED')
        summary = {'version': 1, 'voice_input_id': captured['voice_input_id'], 'session_id': session_id,
            'child_id': value['child_id'], 'client_action_id': action, 'storage_generation': original_generation,
            'transcript_sha256': text_hash(text), 'capture_sha256': _hash(_immutable_capture(captured)),
            'receipt': receipt, 'raw_audio_available': False, 'cloud_asr': False}
        audit_id = 'household-input-' + captured['voice_input_id']
        prior = c.execute('SELECT actor,summary_json FROM audit_event WHERE audit_id=?', (audit_id,)).fetchone()
        if prior and (prior['actor'] != ACTOR or json.loads(prior['summary_json']) != summary):
            raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_WITNESS_CHANGED')
        c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?) ON CONFLICT(audit_id) DO NOTHING',
            (audit_id, ACTOR, 'voice_input', value['child_id'],
             json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
             datetime.now(timezone.utc).isoformat()))
        protect_native_input(c, captured['voice_input_id'])
    captured = captures.complete_realtime(session_id, action, text, pre_persist=inspect, post_persist=witness)
    with captures.database.connect() as c:
        voice = c.execute('SELECT * FROM voice_input WHERE voice_input_id=?', (captured['voice_input_id'],)).fetchone()
        return result(c, voice)


def record_native_or_household_input(captures, media_ai, session_id, value, *, authorize):
    if type(value) is dict and value.get('source_kind') == SOURCE:
        return record_household_input(captures, media_ai, session_id, value, authorize=authorize)
    from .realtime_native_input import record_native_input
    return record_native_input(captures, session_id, value, authorize=authorize)
