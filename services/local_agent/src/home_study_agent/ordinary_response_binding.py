"""Current, local-only ordinary response authority; never an answer/evaluation bypass."""
import json
from .voice_capture_binding import VoiceCaptureBindingError

VERSION='ordinary_prompt_response_v1'
FIELDS={'version','session_state','source_agent_turn_id','source_tutor_turn_id','source_tutor_voice_playback_id'}


def normalize(value):
    if value is None:return None
    if (not isinstance(value,dict) or not {'version','session_state'}<=set(value)<=FIELDS
            or value.get('version')!=VERSION or any(not isinstance(v,str) or not v.strip() or len(v)>128 for v in value.values())
            or ('source_tutor_voice_playback_id' in value and 'source_tutor_turn_id' not in value)):
        raise VoiceCaptureBindingError('VOICE_RESPONSE_BINDING_INVALID')
    return dict(value)


def stored(row):
    raw=row['ordinary_response_binding_json'] if 'ordinary_response_binding_json' in row.keys() else None
    if raw is None:return None
    try:return normalize(json.loads(raw))
    except (ValueError,TypeError) as exc:raise VoiceCaptureBindingError('VOICE_RESPONSE_BINDING_INVALID') from exc


def validate(c,session_id,child_id,marker,reference,*,page_source_store=None):
    marker=normalize(marker)
    if marker is None:return
    from .diagnostic_prompt import DiagnosticPromptService
    session=DiagnosticPromptService._session(c,session_id)
    if (session['child_id']!=child_id or session['status']!='active' or session['child_status']!='active'
            or session['state']!=marker['session_state'] or session['state'] in {'evolving','completed'}
            or reference.get('local_recovery_only') or reference.get('pending_evaluation_turn_id')):
        raise VoiceCaptureBindingError('VOICE_RESPONSE_CURRENT_CHANGED')
    # Evaluation has its own restrictive control/independent-answer authority.
    if c.execute('''SELECT 1 FROM agent_plan p JOIN agent_step s USING(plan_id)
        WHERE p.session_id=? AND p.child_id=? AND s.capability_name='learning.evaluate_response'
        AND s.status IN ('planned','running') AND p.status NOT IN ('completed','cancelled','failed') LIMIT 1''',
        (session_id,child_id)).fetchone():
        raise VoiceCaptureBindingError('VOICE_RESPONSE_PENDING_EVALUATION_FORBIDDEN')
    aid=marker.get('source_agent_turn_id')
    if aid:
        row=c.execute('SELECT agent_turn_id,tool_status FROM agent_turn WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                      (session_id,child_id)).fetchone()
        if not row or row['agent_turn_id']!=aid or row['tool_status']!='completed':
            raise VoiceCaptureBindingError('VOICE_RESPONSE_AGENT_SOURCE_CHANGED')
    tid=marker.get('source_tutor_turn_id')
    if tid:
        from .tutor_voice_playback import source,validate_finished
        _,board=source(c,session_id,child_id,tid)
        if board:
            if marker.get('source_tutor_voice_playback_id'):
                raise VoiceCaptureBindingError('VOICE_RESPONSE_BOARD_RECEIPT_REQUIRED')
            from .tutor_playback import TutorPlaybackService
            try:
                version,presentation=TutorPlaybackService._source(c,session,tid)
                if not c.execute("SELECT 1 FROM sqlite_master WHERE name='tutor_playback_checkpoint'").fetchone():
                    raise ValueError('missing durable checkpoint')
                row=c.execute('SELECT * FROM tutor_playback_checkpoint WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                              (session_id,child_id)).fetchone()
                if (not row or row['turn_id']!=tid or row['source_version']!=version
                        or row['status']!='playback_finished' or row['reason']!='all_segments_delivered'
                        or row['segment_index']!=len(presentation['segments']) or row['segment_started']):
                    raise ValueError('not delivered')
            except ValueError as exc:raise VoiceCaptureBindingError('VOICE_RESPONSE_TUTOR_DELIVERY_REQUIRED') from exc
        else:
            validate_finished(c,session_id,child_id,tid,marker.get('source_tutor_voice_playback_id'))
    from .voice_capture_binding import normalize_question_reference,normalize_activity_reference,normalize_task_choice_reference
    question=normalize_question_reference(reference.get('question_reference'))
    if question:
        from .agent_runtime import AgentRuntimeStore
        try:
            AgentRuntimeStore.validate_captured_question(c,session_id,child_id,question)
            if question['kind']=='document':
                from .document_voice_source import validate_reference
                validate_reference(c,session_id,child_id,question['binding'],page_source_store=page_source_store)
            if question['kind']=='diagnostic':
                from .pet_skill_gate import pet_skill_gate
                row=c.execute('SELECT diagnostic_item_id FROM diagnostic_prompt WHERE prompt_id=?',
                              (question['binding']['prompt_id'],)).fetchone()
                if row and pet_skill_gate(c,child_id,row[0])['modality']=='speaking':raise ValueError('formal raw scoring')
        except ValueError as exc:raise VoiceCaptureBindingError('VOICE_RESPONSE_QUESTION_CHANGED') from exc
    activity=normalize_activity_reference(reference.get('activity_reference'))
    if activity:
        from .voice_control_intent import VoiceControlIntent
        try:VoiceControlIntent.validate_activity_focus(c,session_id,child_id,activity)
        except ValueError as exc:raise VoiceCaptureBindingError('VOICE_RESPONSE_ACTIVITY_CHANGED') from exc
    choice=normalize_task_choice_reference(reference.get('task_choice_reference'))
    if choice:
        from .task_start_choices import current_offer
        if current_offer(c,session_id,child_id)['reference']!=choice:
            raise VoiceCaptureBindingError('VOICE_RESPONSE_TASK_CHOICE_CHANGED')


def validate_capture(c,capture,marker,*,page_source_store=None):
    from .voice_capture_binding import stored_question_reference,stored_activity_reference,stored_task_choice_reference,stored_local_recovery_only,stored_pending_evaluation_reference
    if (not capture or capture['source_kind']!='local_wav' or capture['purpose']!='agent_command'
            or capture['status']=='cancelled' or stored(capture)!=normalize(marker)):
        raise VoiceCaptureBindingError('VOICE_RESPONSE_CAPTURE_CHANGED')
    validate(c,capture['session_id'],capture['child_id'],marker,{
        'question_reference':stored_question_reference(capture),'activity_reference':stored_activity_reference(capture),
        'task_choice_reference':stored_task_choice_reference(capture),'local_recovery_only':stored_local_recovery_only(capture),
        'pending_evaluation_turn_id':stored_pending_evaluation_reference(capture)},page_source_store=page_source_store)
