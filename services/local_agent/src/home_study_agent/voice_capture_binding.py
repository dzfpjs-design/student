"""Arrival-time voice ownership, separate from transcription and grading."""
from __future__ import annotations

from hashlib import sha256
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

from .diagnostic_prompt import DiagnosticPromptService, DiagnosticPromptError
from .repository import utc_now


class VoiceCaptureBindingError(ValueError):
    pass


def capture_started_at(connection, voice_input_id):
    columns = {row[1] for row in connection.execute('PRAGMA table_info(voice_capture_binding)')}
    if 'created_at' not in columns or 'voice_input_id' not in columns:
        return None
    source = 'source_kind' if 'source_kind' in columns else "'local_wav'"
    onset = 'speech_started_at' if 'speech_started_at' in columns else 'NULL'
    row = connection.execute('SELECT ' + source + ',' + onset + ',created_at FROM voice_capture_binding WHERE voice_input_id=?',
                             (voice_input_id,)).fetchone()
    # A missing realtime onset is unknown, not the later server admission.
    if not row:
        return None
    return row[1] if row[0] == 'realtime_transcript' else row[2] if row[0] == 'local_wav' else None


def normalize_onset(captured_at_ms):
    if captured_at_ms is None:
        return None
    try:
        if type(captured_at_ms) is not int or captured_at_ms <= 0:
            raise ValueError('invalid onset')
        onset = datetime.fromtimestamp(captured_at_ms / 1000, timezone.utc)
        if onset > datetime.now(timezone.utc) + timedelta(seconds=5):
            raise ValueError('future onset')
        return onset.isoformat(timespec='milliseconds')
    except (ValueError, OverflowError, OSError) as error:
        raise VoiceCaptureBindingError('VOICE_CAPTURE_ONSET_INVALID') from error


def normalize_task_choice_reference(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'offer_turn_id', 'source_version'}
            or any(not isinstance(item, str) or not item.strip() or len(item) > 200
                   for item in value.values())):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_TASK_CHOICE_INVALID')
    return dict(value)


def stored_task_choice_reference(row):
    try:
        return normalize_task_choice_reference(json.loads(row['task_choice_reference_json'])) if (
            'task_choice_reference_json' in row.keys() and row['task_choice_reference_json']) else None
    except (ValueError, TypeError) as error:
        raise VoiceCaptureBindingError('VOICE_CAPTURE_TASK_CHOICE_INVALID') from error


def normalize_pending_evaluation_reference(value):
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 128):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_PENDING_EVALUATION_INVALID')
    return value


def stored_pending_evaluation_reference(row):
    return normalize_pending_evaluation_reference(row['pending_evaluation_turn_id']) if (
        'pending_evaluation_turn_id' in row.keys()) else None


def stored_local_recovery_only(row):
    value = row['local_recovery_only'] if 'local_recovery_only' in row.keys() else 0
    if value not in (0, 1):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_RECOVERY_MODE_INVALID')
    return bool(value)


def normalize_activity_reference(reference):
    if reference is None:
        return None
    if (not isinstance(reference, dict) or set(reference) != {'activity_id', 'version', 'last_turn_id'}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 200
                   for value in reference.values())):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTIVITY_INVALID')
    return dict(reference)


def stored_activity_reference(row):
    try:
        return normalize_activity_reference(json.loads(row['activity_reference_json'])) if (
            'activity_reference_json' in row.keys() and row['activity_reference_json']) else None
    except (ValueError, TypeError) as error:
        raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTIVITY_INVALID') from error


def normalize_question_reference(question):
    if question is None:
        return None
    if not isinstance(question, dict):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_QUESTION_INVALID')
    if set(question) == {'prompt_id', 'item_version'}:
        question = {'kind': 'diagnostic', 'binding': question}
    fields = {
        'diagnostic': {'prompt_id', 'item_version'},
        'diagnostic_help': {'prompt_id', 'item_version'},
        'exam': {'run_id', 'position', 'item_id', 'source_version'},
        'vocabulary': {'prompt_session_id', 'card_version'},
        'mistake': {'session_id', 'child_id', 'mistake_id', 'phase', 'item_id', 'source_version'},
        'document': {'revision_id', 'artifact_id', 'content_sha256', 'page_sha256'},
        'learning_reply': {'objective_id', 'plan_id', 'source_agent_turn_id', 'capability_name', 'phase', 'source_version'},
    }
    kind, binding = question.get('kind'), question.get('binding')
    paper_facts = (kind == 'mistake' and isinstance(binding, dict)
        and binding.get('mistake_id') is None
        and isinstance(binding.get('phase'), str)
        and binding.get('phase') in {'candidate', 'answer', 'confirm'}
        and binding.get('item_id') == 'source-dialogue')
    required = fields.get(kind, set()) if isinstance(kind, str) else set()
    if paper_facts:
        required = required | {'candidate_id', 'artifact_id'}
    if set(question) != {'kind', 'binding'} or not isinstance(kind, str) or kind not in fields or not isinstance(binding, dict) or set(binding) != required:
        raise VoiceCaptureBindingError('VOICE_CAPTURE_QUESTION_INVALID')
    for key, value in binding.items():
        if key == 'mistake_id' and paper_facts:
            valid = value is None
        elif key in {'content_sha256', 'page_sha256'}:
            valid = isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
        elif kind == 'learning_reply' and key == 'capability_name':
            valid = value in ('learning.explain', 'learning.evaluate_response', 'learning.request_response')
        elif kind == 'learning_reply' and key == 'source_version':
            valid = isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
        elif key == 'position':
            valid = type(value) is int and value >= 0
        else:
            valid = isinstance(value, str) and bool(value.strip()) and len(value) <= 200
        if not valid:
            raise VoiceCaptureBindingError('VOICE_CAPTURE_QUESTION_INVALID')
    return {'kind': kind, 'binding': dict(binding)}


def stored_question_reference(row):
    try:
        if 'question_reference_json' in row.keys() and row['question_reference_json']:
            return normalize_question_reference(json.loads(row['question_reference_json']))
        return normalize_question_reference({'prompt_id': row['prompt_id'], 'item_version': row['item_version']}) if row['prompt_id'] else None
    except (ValueError, TypeError) as error:
        raise VoiceCaptureBindingError('VOICE_CAPTURE_QUESTION_INVALID') from error


def resolve_voice_capture(connection, *, session_id, child_id, voice_input_id, transcript, page_source_store=None, _execution_turn_id=None):
    row = connection.execute("""SELECT b.*, v.transcript AS original_transcript,
        v.local_path, v.status AS voice_status, v.child_id AS voice_child,
        v.session_id AS voice_session, a.status AS action_status,
        a.child_id AS action_child, a.session_id AS action_session
        FROM voice_capture_binding b JOIN voice_input v USING(voice_input_id)
        LEFT JOIN voice_transcription_action a ON a.client_action_id=b.client_action_id
        WHERE b.voice_input_id=?""", (voice_input_id,)).fetchone()
    if row is None:
        raise VoiceCaptureBindingError('VOICE_CAPTURE_MISSING')
    if (row['child_id'], row['session_id'], row['voice_child'], row['voice_session']) != (child_id, session_id, child_id, session_id):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
    if row['status'] != 'completed' or row['voice_status'] != 'completed' or row['action_status'] != 'completed':
        raise VoiceCaptureBindingError('VOICE_CAPTURE_CANCELLED')
    if (row['action_child'], row['action_session']) != (child_id, session_id):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
    normalize = lambda text: re.sub(r'\s+', ' ', text).strip()
    if normalize(row['original_transcript']) != normalize(transcript):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_TRANSCRIPT_CHANGED')
    from .ordinary_response_binding import stored
    ordinary = stored(row)
    source_kind = row['source_kind'] if 'source_kind' in row.keys() else 'local_wav'
    if source_kind == 'realtime_transcript' and row['local_path'] is not None:
        raise VoiceCaptureBindingError('VOICE_CAPTURE_SOURCE_INVALID')
    # The legacy storage enum distinguishes WAV from text transport only.
    # Household provenance remains explicit and lower trust; no cloud/onset grant.
    if (source_kind == 'realtime_transcript' and row['purpose'] == 'household_local_transcript'
            and (row['source_model'] or '').startswith('server_verified_household_local_asr:')):
        source_kind = 'household_local_transcript'
    activity = stored_activity_reference(row)
    question = stored_question_reference(row)
    validation = row['question_validation'] if 'question_validation' in row.keys() else 'valid'
    if question and question['kind'] == 'learning_reply' and validation == 'valid':
        from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
        try:
            AgentRuntimeStore.validate_captured_question(connection, session_id, child_id, question,
                                                        _execution_turn_id=_execution_turn_id,
                                                        _execution_voice_input_id=voice_input_id)
        except InvalidAgentPlanError:
            validation = 'stale'
    if question and question['kind'] == 'document' and validation == 'valid':
        from .document_voice_source import validate_reference
        try:
            validate_reference(connection, session_id, child_id, question['binding'], page_source_store=page_source_store)
        except ValueError:
            validation = 'stale'
    return {key: row[key] for key in ('voice_input_id', 'child_id', 'session_id', 'prompt_id',
        'item_version', 'status', 'original_transcript')} | {'raw_audio_available': bool(row['local_path']),
        'question_reference': question,
        'question_validation': validation} | (
        {'activity_reference': activity, 'activity_validation': row['activity_validation']} if activity else {}) | (
        {'pending_evaluation_turn_id': stored_pending_evaluation_reference(row)}
        if stored_pending_evaluation_reference(row) else {}) | (
        {'task_choice_reference': stored_task_choice_reference(row)} if stored_task_choice_reference(row) else {}) | (
        {'local_recovery_only': True} if stored_local_recovery_only(row) else {}) | (
        {'ordinary_response_binding': ordinary} if ordinary else {}) | (
        {'source_kind': source_kind, 'speech_started_at': row['speech_started_at'],
         'admitted_at': row['created_at'], 'onset_provenance': 'client_reported' if row['speech_started_at'] else 'unknown'}
        if source_kind in ('realtime_transcript', 'household_local_transcript') else {})


def read_realtime_admission_receipt(database, session_id, client_action_id, *, connection=None):
    """Read canonical delivery facts without speech, planning or replay authority."""
    if not isinstance(client_action_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', client_action_id):
        raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_INVALID')
    from contextlib import nullcontext
    with (database.connect() if connection is None else nullcontext(connection)) as c:
        if connection is None:
            c.execute('BEGIN')
        session = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if session is None:
            raise VoiceCaptureBindingError('VOICE_CAPTURE_SESSION_INVALID')
        child_id = session['child_id']
        capture = c.execute('SELECT * FROM voice_capture_binding WHERE session_id=? AND child_id=? AND client_action_id=?',
                            (session_id, child_id, client_action_id)).fetchone()
        action = c.execute('SELECT status FROM voice_transcription_action WHERE session_id=? AND child_id=? AND client_action_id=?',
                           (session_id, child_id, client_action_id)).fetchone()
        status, voice_id = 'not_found', None
        if (action and action['status'] == 'cancelled') or (capture and capture['status'] == 'cancelled'):
            status = 'cancelled'
        elif capture:
            if capture['source_kind'] != 'realtime_transcript':
                raise VoiceCaptureBindingError('VOICE_CAPTURE_SOURCE_INVALID')
            status = capture['status']
            if status == 'completed':
                if action is None or action['status'] != 'completed':
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                voice = c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?',
                                  (capture['voice_input_id'],)).fetchone()
                if voice is None:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_MISSING')
                resolved = resolve_voice_capture(c, session_id=session_id, child_id=child_id,
                                                 voice_input_id=capture['voice_input_id'], transcript=voice['transcript'])
                voice_id = resolved['voice_input_id']
            elif status != 'prepared' or capture['voice_input_id'] is not None:
                raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
    return {'session_id': session_id, 'child_id': child_id, 'client_action_id': client_action_id,
            'status': status, 'voice_input_id': voice_id, 'local_only': True,
            'cloud_transmission_allowed': False, 'replay_allowed': False, 'mastery_update_allowed': False}


class VoiceCaptureBindingService:
    def __init__(self, database, *, page_source_store=None, current_graph_version=None):
        self.database = database
        self.page_source_store = page_source_store
        self.current_graph_version = current_graph_version
        # App services are constructed before lifespan runs Database.initialize.
        # Schema writes and recovery therefore belong only to the main upgrade.

    def begin_realtime(self, session_id, client_action_id, purpose, reference=None, *, model_version, captured_at_ms=None):
        if not isinstance(model_version, str) or not model_version.strip() or len(model_version) > 256:
            raise VoiceCaptureBindingError('VOICE_CAPTURE_SOURCE_MODEL_INVALID')
        return self.begin(session_id, client_action_id, None, purpose, reference,
                          source_kind='realtime_transcript', source_model=model_version,
                          speech_started_at=normalize_onset(captured_at_ms))

    def begin(self, session_id, client_action_id, wav_bytes, purpose, reference=None, *, source_kind='local_wav', source_model=None, speech_started_at=None, ordinary_response_binding=None):
        if ((source_kind == 'local_wav' and not isinstance(wav_bytes, bytes))
                or (source_kind == 'realtime_transcript' and (wav_bytes is not None or not source_model))
                or source_kind not in ('local_wav', 'realtime_transcript')):
            raise VoiceCaptureBindingError('VOICE_CAPTURE_SOURCE_INVALID')
        if source_kind == 'local_wav' and speech_started_at is not None:
            raise VoiceCaptureBindingError('VOICE_CAPTURE_SOURCE_INVALID')
        from .ordinary_response_binding import normalize,stored,validate
        ordinary = normalize(ordinary_response_binding)
        if ordinary and (purpose != 'agent_command' or source_kind != 'local_wav'):
            raise VoiceCaptureBindingError('VOICE_RESPONSE_BINDING_INVALID')
        reference = reference or {}
        recovery = reference.get('local_recovery_only', False)
        if type(recovery) is not bool:
            raise VoiceCaptureBindingError('VOICE_CAPTURE_RECOVERY_MODE_INVALID')
        if recovery and any(reference.get(k) is not None for k in (
                'question_reference', 'activity_reference', 'pending_evaluation_turn_id', 'task_choice_reference')):
            raise VoiceCaptureBindingError('VOICE_CAPTURE_RECOVERY_REFERENCE_FORBIDDEN')
        typed_question = normalize_question_reference(reference.get('question_reference'))
        activity = normalize_activity_reference(reference.get('activity_reference'))
        pending_evaluation = normalize_pending_evaluation_reference(reference.get('pending_evaluation_turn_id'))
        task_choice = normalize_task_choice_reference(reference.get('task_choice_reference'))
        if task_choice and (typed_question or activity or pending_evaluation):
            raise VoiceCaptureBindingError('VOICE_CAPTURE_TASK_CHOICE_CONFLICT')
        question = typed_question['binding'] if typed_question and typed_question['kind'] == 'diagnostic' else {}
        digest = sha256(wav_bytes).hexdigest() if wav_bytes is not None else None
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                session = DiagnosticPromptService._session(c, session_id)
            except DiagnosticPromptError as error:
                raise VoiceCaptureBindingError('VOICE_CAPTURE_SESSION_INVALID') from error
            if session['status'] != 'active' or session['child_status'] != 'active':
                raise VoiceCaptureBindingError('VOICE_CAPTURE_SESSION_INVALID')
            child_id = session['child_id']
            if ordinary:
                validate(c,session_id,child_id,ordinary,reference,page_source_store=self.page_source_store)
            if reference.get('child_id', child_id) != child_id:
                raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
            transcription = c.execute('SELECT child_id,session_id,status FROM voice_transcription_action WHERE client_action_id=?',
                                      (client_action_id,)).fetchone()
            if transcription:
                if (transcription['child_id'], transcription['session_id']) != (child_id, session_id):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
                if transcription['status'] == 'cancelled':
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_CANCELLED')
            old = c.execute('SELECT * FROM voice_capture_binding WHERE client_action_id=?', (client_action_id,)).fetchone()
            expected = (session_id, child_id, digest, purpose, question.get('prompt_id'), question.get('item_version'))
            if old:
                if stored(old) != ordinary:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if (old['source_kind'], old['source_model']) != (source_kind, source_model):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if old['speech_started_at'] != speech_started_at:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if tuple(old[k] for k in ('session_id','child_id','wav_sha256','purpose','prompt_id','item_version')) != expected:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if stored_question_reference(old) != typed_question:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if stored_activity_reference(old) != activity:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if stored_pending_evaluation_reference(old) != pending_evaluation:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if stored_local_recovery_only(old) != recovery:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if stored_task_choice_reference(old) != task_choice:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if old['status'] == 'cancelled':
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_CANCELLED')
                return dict(old)
            # Old recordings cannot acquire a newly supplied question binding.
            if c.execute('SELECT 1 FROM voice_input WHERE client_action_id=?', (client_action_id,)).fetchone():
                raise VoiceCaptureBindingError('VOICE_CAPTURE_LEGACY_UNBOUND')
            question_validation = 'valid'
            activity_validation = 'valid'
            if task_choice:
                owner = c.execute('SELECT child_id,session_id FROM agent_turn WHERE agent_turn_id=?',
                                  (task_choice['offer_turn_id'],)).fetchone()
                if not owner or (owner['child_id'], owner['session_id']) != (child_id, session_id):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_TASK_CHOICE_OWNER_CHANGED')
            if pending_evaluation:
                from .pending_evaluation_control import validate_pending_evaluation_reference
                try:
                    validate_pending_evaluation_reference(c, session_id, child_id, pending_evaluation,
                                                          question_reference=typed_question)
                except ValueError as error:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_PENDING_EVALUATION_CHANGED') from error
            if activity:
                from .voice_control_intent import VoiceControlIntent
                owner = c.execute('SELECT child_id,session_id FROM companion_activity WHERE activity_id=?',
                                  (activity['activity_id'],)).fetchone()
                if not owner or (owner['child_id'], owner['session_id']) != (child_id, session_id):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTIVITY_OWNER_CHANGED')
                try:
                    VoiceControlIntent.validate_activity_focus(c, session_id, child_id, activity)
                except ValueError as error:
                    if str(error) != 'CONTROL_ACTIVITY_FOCUS_CHANGED':
                        raise
                    # Preserve the supplied old focus for clarification, never
                    # attach a delayed recording to the new activity or question.
                    activity_validation = 'stale'
            if typed_question:
                from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
                try:
                    AgentRuntimeStore.validate_captured_question(c, session_id, child_id, typed_question)
                    if typed_question['kind'] == 'document':
                        from .document_voice_source import validate_reference
                        if self.page_source_store is None:
                            raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_TARGET_CHANGED')
                        try:
                            validate_reference(c, session_id, child_id, typed_question['binding'],
                                               page_source_store=self.page_source_store)
                        except ValueError as error:
                            raise InvalidAgentPlanError(str(error)) from error
                except InvalidAgentPlanError as error:
                    if str(error) != 'ANSWER_VOICE_CAPTURE_TARGET_CHANGED':
                        raise VoiceCaptureBindingError('VOICE_CAPTURE_QUESTION_CHANGED') from error
                    # Preserve an owned, outdated source for control commands;
                    # it can never become a new answer to the current question.
                    question_validation = 'stale'
            now = utc_now()
            c.execute("""INSERT INTO voice_capture_binding
                (client_action_id,session_id,child_id,wav_sha256,purpose,prompt_id,item_version,status,created_at,updated_at,question_reference_json,question_validation,activity_reference_json,activity_validation,pending_evaluation_turn_id,local_recovery_only,task_choice_reference_json,source_kind,source_model,speech_started_at,ordinary_response_binding_json)
                VALUES (?,?,?,?,?,?,?,'prepared',?,?,?,?,?,?,?,?,?,?,?,?,?)""", (client_action_id, *expected, now, now,
                json.dumps(typed_question, sort_keys=True, separators=(',', ':')) if typed_question else None, question_validation,
                json.dumps(activity, sort_keys=True, separators=(',', ':')) if activity else None, activity_validation,
                pending_evaluation, int(recovery), json.dumps(task_choice, sort_keys=True) if task_choice else None, source_kind, source_model, speech_started_at,
                json.dumps(ordinary,sort_keys=True,separators=(',',':')) if ordinary else None))
            c.commit()
            return dict(c.execute('SELECT * FROM voice_capture_binding WHERE client_action_id=?', (client_action_id,)).fetchone())

    def complete_realtime(self, session_id, client_action_id, transcript, *, pre_persist=None, post_persist=None):
        if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 16000:
            raise VoiceCaptureBindingError('VOICE_CAPTURE_TRANSCRIPT_INVALID')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            capture = c.execute('SELECT * FROM voice_capture_binding WHERE client_action_id=?', (client_action_id,)).fetchone()
            if capture is None or capture['status'] == 'cancelled':
                raise VoiceCaptureBindingError('VOICE_CAPTURE_CANCELLED')
            if capture['session_id'] != session_id or capture['source_kind'] != 'realtime_transcript':
                raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
            session = DiagnosticPromptService._session(c, session_id)
            if session['status'] != 'active' or session['child_status'] != 'active' or session['child_id'] != capture['child_id']:
                raise VoiceCaptureBindingError('VOICE_CAPTURE_SESSION_INVALID')
            if pre_persist is not None:
                pre_persist(c)
            action = c.execute('SELECT * FROM voice_transcription_action WHERE client_action_id=?', (client_action_id,)).fetchone()
            if action:
                if (action['session_id'], action['child_id']) != (session_id, capture['child_id']):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
                if action['status'] != 'completed':
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_CANCELLED')
            voice = c.execute('SELECT * FROM voice_input WHERE client_action_id=?', (client_action_id,)).fetchone()
            if voice:
                if (voice['session_id'], voice['child_id']) != (session_id, capture['child_id']):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
                if voice['transcript'] != transcript:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_TRANSCRIPT_CHANGED')
                if (voice['status'] != 'completed' or voice['local_path'] is not None or voice['duration_ms'] is not None
                        or voice['model_version'] != capture['source_model'] or capture['voice_input_id'] != voice['voice_input_id']):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                result = resolve_voice_capture(c, session_id=session_id, child_id=capture['child_id'],
                                               voice_input_id=voice['voice_input_id'], transcript=transcript,
                                               page_source_store=self.page_source_store)
                if post_persist is not None:
                    post_persist(c, result)
                c.commit()
                return result
            if action or capture['status'] != 'prepared' or capture['voice_input_id'] is not None:
                raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
            voice_id, now = str(uuid4()), utc_now()
            c.execute("""INSERT INTO voice_input(voice_input_id,child_id,session_id,purpose,transcript,
                local_path,model_version,duration_ms,status,created_at,client_action_id)
                VALUES(?,?,?,?,?,NULL,?,NULL,'completed',?,?)""",
                (voice_id,capture['child_id'],session_id,capture['purpose'],transcript,capture['source_model'],now,client_action_id))
            c.execute("""INSERT INTO voice_transcription_action(client_action_id,session_id,child_id,status,updated_at)
                VALUES(?,?,?,'completed',?)""", (client_action_id,session_id,capture['child_id'],now))
            c.execute("UPDATE voice_capture_binding SET voice_input_id=?,status='completed',updated_at=? WHERE client_action_id=?",
                      (voice_id,now,client_action_id))
            result = resolve_voice_capture(c, session_id=session_id, child_id=capture['child_id'], voice_input_id=voice_id,
                                           transcript=transcript, page_source_store=self.page_source_store)
            if result['question_validation'] == 'stale':
                c.execute("UPDATE voice_capture_binding SET question_validation='stale' WHERE client_action_id=?", (client_action_id,))
            if post_persist is not None:
                post_persist(c, result)
            c.commit()
            return result

    def complete(self, session_id, client_action_id, voice_input_id):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            capture = c.execute('SELECT * FROM voice_capture_binding WHERE client_action_id=?', (client_action_id,)).fetchone()
            voice = c.execute('SELECT * FROM voice_input WHERE voice_input_id=?', (voice_input_id,)).fetchone()
            if capture is None or capture['status'] == 'cancelled':
                raise VoiceCaptureBindingError('VOICE_CAPTURE_CANCELLED')
            from .ordinary_response_binding import stored,validate_capture
            if stored(capture):
                validate_capture(c,capture,stored(capture),page_source_store=self.page_source_store)
            if capture['source_kind'] != 'local_wav':
                raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
            if (voice is None or voice['status'] != 'completed' or voice['client_action_id'] != client_action_id
                    or (voice['child_id'],voice['session_id']) != (capture['child_id'],session_id)
                    or capture['session_id'] != session_id
                    or capture['voice_input_id'] not in (None, voice_input_id)):
                raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
            c.execute("UPDATE voice_capture_binding SET voice_input_id=?,status='completed',updated_at=? WHERE client_action_id=?",
                      (voice_input_id, utc_now(), client_action_id))
            result = resolve_voice_capture(c, session_id=session_id, child_id=capture['child_id'],
                                           voice_input_id=voice_input_id, transcript=voice['transcript'],
                                           page_source_store=self.page_source_store)
            if result['question_validation'] == 'stale':
                c.execute("UPDATE voice_capture_binding SET question_validation='stale' WHERE client_action_id=?", (client_action_id,))
            c.commit()
            return result

    def recognition_purpose(self, capture):
        from .ordinary_response_binding import stored
        if stored(capture):return 'agent_command'
        if (stored_local_recovery_only(capture) or stored_activity_reference(capture)
                or stored_pending_evaluation_reference(capture) or stored_task_choice_reference(capture)):
            return 'agent_command'
        if not capture['prompt_id']:
            return capture['purpose']
        if capture.get('question_validation') == 'stale':
            return 'agent_command'
        from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
        from .pet_skill_gate import pet_skill_gate
        with self.database.connect() as c:
            c.execute('BEGIN')
            # Admission is historical. Recheck the exact waiting source after
            # cancellation/answer/revision races and on an old capture-key retry.
            try:
                AgentRuntimeStore.validate_captured_question(c, capture['session_id'], capture['child_id'],
                                                            stored_question_reference(capture))
            except InvalidAgentPlanError:
                return 'agent_command'
            prompt = c.execute('SELECT diagnostic_item_id FROM diagnostic_prompt WHERE prompt_id=?', (capture['prompt_id'],)).fetchone()
            if prompt and pet_skill_gate(c, capture['child_id'], prompt['diagnostic_item_id'])['modality'] == 'speaking':
                return 'pet_speaking'
        return capture['purpose']

    def cancel(self, session_id, client_action_id):
        with self.database.connect() as c:
            c.execute("UPDATE voice_capture_binding SET status='cancelled',updated_at=? WHERE session_id=? AND client_action_id=?",
                      (utc_now(),session_id,client_action_id))
            c.commit()
