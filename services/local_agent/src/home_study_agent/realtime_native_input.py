"""A fresh native cloud utterance is distinct from legacy local recovery input.

The purpose is written with the canonical voice row, before any model/tool reply
exists. This permits recovery of an admitted, otherwise orphaned utterance.
"""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from .privacy_operation_receipt import generation
from .realtime_capability_tools import _hash, _immutable_capture
from .realtime_native_safety import protect_native_input, native_safety_control

NATIVE_INPUT_PURPOSE = 'native_cloud_dialogue'
NATIVE_INPUT_MODEL = 'client_reported_realtime:qwen3.8-omni-flash-realtime'


class NativeInputConflict(ValueError):
    pass


def record_native_input(captures, session_id, value, *, authorize):
    def fail(code):
        raise NativeInputConflict(code)
    fields = {'version', 'local_input_id', 'transcript', 'captured_at_ms', 'capture_reference'}
    if (type(value) is not dict or set(value) != fields or type(value['version']) is not int
            or value['version'] != 1 or type(value['local_input_id']) is not str
            or not re.fullmatch('[A-Za-z0-9_-]{1,160}', value['local_input_id'])
            or type(value['transcript']) is not str or not value['transcript'].strip()
            or len(value['transcript']) > 16000 or type(value['capture_reference']) is not dict
            or type(value['captured_at_ms']) not in (int, float)
            or not math.isfinite(value['captured_at_ms']) or value['captured_at_ms'] <= 0):
        fail('REALTIME_NATIVE_INPUT_INVALID')
    reference = dict(value['capture_reference'])
    from .realtime_native_mistake_onset import normalize as normalize_mistake, record as record_mistake
    mistake_onset = normalize_mistake(reference.pop('mistake_learning',None),session_id,reference.get('child_id'))
    allowed = {'child_id', 'question_reference', 'activity_reference', 'pending_evaluation_turn_id',
               'local_recovery_only', 'task_choice_reference'}
    if set(reference) - allowed or reference.get('local_recovery_only', False) is not False:
        fail('REALTIME_NATIVE_INPUT_LOCAL_RECOVERY_FORBIDDEN')
    def inspect(c):
        owner = c.execute('''SELECT d.child_id,d.status,p.status AS child_status FROM daily_session d
            JOIN child_profile p ON d.child_id=p.child_id WHERE d.session_id=?''', (session_id,)).fetchone()
        consent = c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
        if (not owner or owner['status'] != 'active' or owner['child_status'] != 'active'
                or reference.get('child_id') != owner['child_id']):
            fail('REALTIME_NATIVE_INPUT_OWNER_CHANGED')
        if (not consent or any(type(consent[k]) is not int or consent[k] != 1
                for k in ('cloud_audio_allowed', 'cloud_model_text_allowed'))):
            fail('REALTIME_NATIVE_INPUT_CONSENT_REQUIRED')
        if authorize(c, session_id, owner['child_id']) is not True:
            fail('REALTIME_NATIVE_INPUT_HOST_UNCONFIRMED')
    with captures.database.connect() as c:
        inspect(c)
        original_generation = generation(c)
    # A reserved purpose is selected by this verified host route, never by a
    # legacy public request. Existing purpose conflicts are not deleted/rebound.
    action = 'qwen-native-input-' + hashlib.sha256(json.dumps(
        [session_id, value['local_input_id']], separators=(',', ':')).encode()).hexdigest()
    captures.begin_realtime(session_id, action, NATIVE_INPUT_PURPOSE, reference,
        model_version=NATIVE_INPUT_MODEL, captured_at_ms=value['captured_at_ms'])
    def witness(c, captured):
        if generation(c) != original_generation:
            fail('REALTIME_NATIVE_INPUT_STORAGE_CHANGED')
        summary = {'version': 1, 'voice_input_id': captured['voice_input_id'],
            'session_id': session_id, 'child_id': captured['child_id'], 'client_action_id': action,
            'storage_generation': original_generation,
            'transcript_sha256': _hash(value['transcript'].strip()),
            'capture_sha256': _hash(_immutable_capture(captured))}
        audit_id = 'native-input-' + captured['voice_input_id']
        previous = c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?', (audit_id,)).fetchone()
        if previous and json.loads(previous['summary_json']) != summary:
            fail('REALTIME_NATIVE_INPUT_WITNESS_CHANGED')
        c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?) ON CONFLICT(audit_id) DO NOTHING',
            (audit_id, 'native_cloud_input_v1', 'voice_input', captured['child_id'],
             json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
             datetime.now(timezone.utc).isoformat()))
        from .mistake_learning import graph_version_from_reader
        graph_version = graph_version_from_reader(getattr(captures, 'current_graph_version', None), c) if mistake_onset else None
        record_mistake(c,captured,mistake_onset,allow_new=previous is None,current_graph_version=graph_version)
        safety = protect_native_input(c, captured['voice_input_id'])
        if previous is None and safety is None:
            from .realtime_native_curriculum import record_clarification
            record_clarification(c, captured, value['transcript'].strip(), summary)
    captured = captures.complete_realtime(session_id, action, value['transcript'].strip(),
        pre_persist=inspect, post_persist=witness)
    with captures.database.connect() as c:
        row = c.execute('SELECT purpose,client_action_id FROM voice_input WHERE voice_input_id=?',
                        (captured['voice_input_id'],)).fetchone()
        if not row or row['purpose'] != NATIVE_INPUT_PURPOSE or row['client_action_id'] != action:
            fail('REALTIME_NATIVE_INPUT_SOURCE_CONFLICT')
        control = native_safety_control(c, captured['voice_input_id'])
    return {'version': 1, 'voice_input_id': captured['voice_input_id'], 'client_action_id': action,
            'status': 'completed', 'purpose': NATIVE_INPUT_PURPOSE, 'source_kind': 'realtime_transcript',
            'raw_audio_available': False, 'model_provenance': 'client_reported',
            **({'local_control': control} if control else {})}
