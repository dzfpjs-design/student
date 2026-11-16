"""Revalidate canonical realtime source in the original dispatch transaction."""
import json

from .agent_runtime import AgentRuntimeStore
from .voice_capture_binding import resolve_voice_capture


def inspect(connection, turn_id, *, page_source_store=None):
    row = connection.execute('''SELECT t.*,d.status AS session_status,c.status AS child_status
        FROM agent_turn t JOIN daily_session d ON d.session_id=t.session_id AND d.child_id=t.child_id
        JOIN child_profile c ON c.child_id=t.child_id WHERE t.agent_turn_id=?''', (turn_id,)).fetchone()
    if not row or not str(row['client_action_id'] or '').startswith('realtime-'):
        return False
    action = json.loads(row['action_json'])
    voice_id = action.get('voice_input_id')
    if (not isinstance(voice_id, str) or row['client_action_id'] != 'realtime-' + voice_id
            or row['session_status'] != 'active' or row['child_status'] != 'active'):
        raise ValueError('CANONICAL_VOICE_EXECUTION_OWNER_CHANGED')
    learning_context = None
    if ((action.get('voice_input_binding') or {}).get('question_reference') or {}).get('kind') == 'learning_reply':
        work = connection.execute('''SELECT s.capability_name FROM agent_plan p
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0 WHERE p.agent_turn_id=?''',(turn_id,)).fetchone()
        if work and work[0] in {'learning.explain','learning.evaluate_response','learning.request_response'}:
            learning_context = turn_id
    capture = resolve_voice_capture(connection, session_id=row['session_id'], child_id=row['child_id'],
        voice_input_id=voice_id, transcript=row['transcript'], page_source_store=page_source_store,
        _execution_turn_id=learning_context)
    protected_control = bool((capture.get('local_recovery_only') or capture.get('pending_evaluation_turn_id'))
        and action.get('local_only') is True and action.get('cloud_transmission_allowed') is False)
    if (capture.get('source_kind') != 'realtime_transcript'
            or AgentRuntimeStore.voice_capture_snapshot(capture) != action.get('voice_input_binding')
            or not protected_control and capture.get('question_validation') != 'valid'):
        raise ValueError('CANONICAL_VOICE_EXECUTION_SOURCE_CHANGED')
    question = AgentRuntimeStore.captured_question(capture)
    # Dedicated recovery/evaluation controls validate their original foreground
    # authority separately; an old exercise must not prevent checking its receipt.
    if question and not protected_control:
        AgentRuntimeStore.validate_captured_question(connection, row['session_id'], row['child_id'], question,
                                                    _execution_turn_id=learning_context)
    return True
