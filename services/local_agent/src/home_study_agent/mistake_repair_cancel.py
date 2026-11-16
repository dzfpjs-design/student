"""Cancel a version-bound source offer without undoing accepted learning work."""
import json
import re
from datetime import datetime

from .mistake_capture import InvalidMistakeCaptureCandidateError
from .repository import utc_now


def requested(text):
    value = re.sub(r'[，,。！!\s]', '', text)
    return bool(re.fullmatch(r'(?:请)?(?:取消(?:这次)?更正|取消(?:这次)?题干修改|先不改了|保留原题)', value))


def cancel(database, focus, text, action_id, turn_id):
    from .diagnostic_prompt import _TransactionDatabase
    from .mistake_dialogue import MistakeDialogueService
    from .mistake_recapture import original
    pending = focus['state'].get('question_confirmation') or {}
    error = '这次更正已经变化，没有取消新的内容。请重新确认当前要取消的更正。'
    if not turn_id or pending.get('mode') != 'source_repair' or not requested(text):
        raise InvalidMistakeCaptureCandidateError(error)
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND session_id=? AND child_id=?',
                        (turn_id, focus['session_id'], focus['child_id'])).fetchone()
        action = json.loads(row['action_json']) if row else {}
        dialogue = MistakeDialogueService(_TransactionDatabase(c))
        live = dialogue.state(focus['session_id'])
        steps = c.execute('''SELECT s.arguments_json FROM agent_step s JOIN agent_plan p USING(plan_id)
            JOIN daily_session d ON d.session_id=p.session_id AND d.child_id=p.child_id
            WHERE p.agent_turn_id=? AND p.status='running' AND s.status='running'
            AND s.capability_name='mistake.dialogue' AND d.status='active' AND d.state='study' ''', (turn_id,)).fetchall()
        if (not row or row['transcript'] != text or action.get('source') != 'mistake_question_voice_cancellation'
                or action.get('question_confirmation') != pending or not live
                or live['mistake_id'] != focus['mistake_id'] or live['phase'] != 'remediation'
                or live['state'].get('question_confirmation') != pending or live['state'].get('pending_operation')
                or live['state'].get('foundation_hold') or len(steps) != 1
                or json.loads(steps[0][0]).get('utterance') != text):
            raise InvalidMistakeCaptureCandidateError(error)
        if action.get('voice_input_id'):
            from .agent_runtime import AgentRuntimeStore
            captured = AgentRuntimeStore.resolve_captured_voice(c, session_id=focus['session_id'], child_id=focus['child_id'],
                voice_input_id=action['voice_input_id'], transcript=text)
            from .voice_capture_binding import capture_started_at
            recorded = capture_started_at(c, action['voice_input_id'])
            if (AgentRuntimeStore.voice_capture_snapshot(captured) != action.get('voice_input_binding')
                    or not recorded or not pending.get('created_at')
                    or datetime.fromisoformat(recorded) < datetime.fromisoformat(pending['created_at'])):
                raise InvalidMistakeCaptureCandidateError(error)
        source = original(c, live['mistake_id'], live['child_id'])
        state = live['state']
        state['cancelled_source_repair'] = {'offer_id': pending['offer_id'], 'cancelled_at': utc_now(),
            'recapture_turn_id': (pending.get('recapture') or {}).get('turn_id')
                                or pending.get('superseded_recapture_turn_id')}
        for key in ('question_confirmation', 'ocr_confirmation_id', 'teaching_requested', 'teaching_turn_id'):
            state.pop(key, None)
        if source:
            case = source['case']
            artifact_id, _, candidate_id = case['source_ref'].partition('#')
            live['candidate_id'] = candidate_id or None
            state.update(artifact_id=artifact_id, question_text=case['prompt_summary'], submitted_answer=case['submitted_answer'])
            dialogue._sync(live, advance=False)
            message = '已取消这次更正，原错题和原答案没有修改。' + dialogue.prompt(live)
        else:
            live['phase'] = 'closed'
            state.pop('question_text', None)
            message = '已取消这次更正。原题目前无法继续核对，没有恢复旧题或改动学习记录。'
        result = dialogue._save(live, message, action_id, text)
        c.commit()
        return result
