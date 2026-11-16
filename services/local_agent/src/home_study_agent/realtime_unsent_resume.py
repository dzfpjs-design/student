"""Explicit canonical speech continuation; unknown work is never dispatched again."""
import re

from .agent_request_ownership import read_unsent_authority
from .voice_capture_binding import read_realtime_admission_receipt, resolve_voice_capture, VoiceCaptureBindingError


class RealtimeResumeError(ValueError):
    pass


class RealtimeUnsentResume:
    def __init__(self, database, orchestrator):
        self.database = database
        self.orchestrator = orchestrator

    def _planned_original(self, c, session_id, child_id, action_id, voice_id, transcript, capture, recorded):
        from .agent_runtime import AgentRuntimeStore
        from .agent_continuation import ContinuationStore
        from .decision_record import validate_current
        from .foreground_independent_dialogue import validate_turn
        if len(recorded) != 1:
            return None
        turn_id = recorded[0]['agent_turn_id']
        rows = c.execute('''SELECT t.client_action_id,t.transcript,t.tool_status,
            p.plan_id,p.child_id,p.session_id,p.status AS plan_status,
            s.status AS step_status,s.step_index,s.execution_location,s.capability_name,s.result_json
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=?''', (turn_id,)).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        if (row['client_action_id'] != action_id or row['transcript'] != transcript
                or row['tool_status'] != 'pending' or row['plan_status'] != 'planned'
                or row['step_status'] != 'planned' or row['step_index'] != 0 or row['result_json'] is not None
                or row['execution_location'] != 'server' or row['capability_name'] == 'learning.evaluate_response'
                or (row['child_id'], row['session_id']) != (child_id, session_id)
                or not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()):
            return None
        try:
            turn = self.orchestrator._get(c, turn_id)
            if (turn['action'].get('voice_input_id') != voice_id
                    or turn['action'].get('voice_input_binding') != AgentRuntimeStore.voice_capture_snapshot(capture)
                    or not ContinuationStore.execution_allowed(c, turn_id)):
                return None
            # Same gates as the original executor; preview is not an execution lease.
            validate_current(c, row['plan_id'])
            from .canonical_voice_execution import inspect
            inspect(c, turn_id, page_source_store=self.orchestrator.page_source_store)
            AgentRuntimeStore.inspect_voice_control_execution(c, turn_id, allow_planned=True)
            validate_turn(c, turn_id)
            return turn
        except (ValueError, KeyError, TypeError):
            return None

    def _snapshot(self, session_id, child_id, input_id, expected_voice=None):
        if not isinstance(input_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', input_id):
            raise RealtimeResumeError('REALTIME_RESUME_SOURCE_INVALID')
        result = {'session_id': session_id, 'child_id': child_id, 'input_id': input_id,
                  'voice_input_id': None, 'status': 'blocked', 'resume_allowed': False,
                  'local_only': True, 'cloud_transmission_allowed': False, 'replay_allowed': False,
                  'mastery_update_allowed': False, 'raw_audio_available': False,
                  'response_text': '原话尚不能确认安全续接，已保留原编号，没有重新提交。'}
        with self.database.connect() as c:
            c.execute('BEGIN')
            session = c.execute('SELECT child_id,status FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if not session or session['child_id'] != child_id or session['status'] != 'active':
                raise RealtimeResumeError('REALTIME_RESUME_OWNER_CHANGED')
            receipt = read_realtime_admission_receipt(self.database, session_id, input_id, connection=c)
            if receipt['status'] != 'completed':
                return result, None
            voice_id = receipt['voice_input_id']
            if expected_voice is not None and expected_voice != voice_id:
                raise RealtimeResumeError('REALTIME_RESUME_SOURCE_CHANGED')
            result['voice_input_id'] = voice_id
            voice = c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (voice_id,)).fetchone()
            if not voice or not isinstance(voice['transcript'], str) or not 1 <= len(voice['transcript'].strip()) <= 4000:
                raise RealtimeResumeError('REALTIME_RESUME_SOURCE_INVALID')
            capture = resolve_voice_capture(c, session_id=session_id, child_id=child_id,
                voice_input_id=voice_id, transcript=voice['transcript'],
                page_source_store=self.orchestrator.page_source_store)
            action_id = 'realtime-' + voice_id
            # A turn under another engine key is still the same original source.
            recorded = c.execute('''SELECT agent_turn_id FROM agent_turn WHERE client_action_id=? OR
                (child_id=? AND session_id=? AND
                json_extract(CASE WHEN json_valid(action_json) THEN action_json ELSE '{}' END,'$.voice_input_id')=?)''',
                (action_id, child_id, session_id, voice_id)).fetchall()
            if (capture.get('local_recovery_only') or capture.get('question_validation') != 'valid'
                    or capture.get('activity_reference') and capture.get('activity_validation') != 'valid'):
                result['response_text'] = '原话的学习来源已变化，不能用当前题目替代；原编号仍保留。'
                return result, None
            pending = capture.get('pending_evaluation_turn_id')
            if pending:
                from .pending_evaluation_control import validate_pending_evaluation_reference
                try:
                    validate_pending_evaluation_reference(c, session_id, child_id, pending,
                        question_reference=capture.get('question_reference'))
                except (ValueError, KeyError, TypeError):
                    result['response_text'] = '原话对应的评价来源已变化，不能改用另一份作答；原编号仍保留。'
                    return result, None
                result['pending_evaluation_turn_id'] = pending
            if recorded:
                turn = self._planned_original(c, session_id, child_id, action_id, voice_id, voice['transcript'], capture, recorded)
                if turn is not None:
                    result.update(status='ready', resume_allowed=True, resume_kind='execution',
                        agent_turn_id=turn['agent_turn_id'],
                        response_text='原话已理解，原操作尚未开始，可以接着完成同一操作。')
                    return result, {'turn': turn}
                result['response_text'] = '原话已有处理记录，尚不能确认原操作可续接；请核对原请求，没有重新解释或执行。'
                return result, None
            if not read_unsent_authority(c, session_id, child_id, action_id, voice['transcript'], voice_id, pending):
                return result, None
            result.update(status='ready', resume_allowed=True,
                response_text='已确认原话完整保存且尚未发出，可以按原话和原来源继续。')
            return result, {'transcript': voice['transcript'], 'action_id': action_id, 'voice_input_id': voice_id,
                            'pending_evaluation_turn_id': pending}

    def preview(self, session_id, child_id, input_id):
        try:
            return self._snapshot(session_id, child_id, input_id)[0]
        except VoiceCaptureBindingError as error:
            raise RealtimeResumeError('REALTIME_RESUME_SOURCE_INVALID') from error

    async def resume(self, session_id, child_id, input_id, expected_voice, cloud_text_allowed):
        try:
            preview, source = self._snapshot(session_id, child_id, input_id, expected_voice)
        except VoiceCaptureBindingError as error:
            raise RealtimeResumeError('REALTIME_RESUME_SOURCE_INVALID') from error
        if not preview['resume_allowed'] or source is None:
            raise RealtimeResumeError('REALTIME_RESUME_NOT_UNSENT')
        if 'turn' in source:
            # Return only the original plan. Its existing executor performs the
            # atomic dispatch claim, cancellation/source checks and durable result.
            return source['turn']
        # The preview is not a lease. The same atomic claim fence arbitrates
        # concurrent original delivery, another engine and repeated resumes.
        return await self.orchestrator.interpret(session_id, source['transcript'], source['action_id'],
            cloud_text_allowed, voice_input_id=source['voice_input_id'],
            pending_evaluation_turn_id=source['pending_evaluation_turn_id'], require_unsent_resume=True)
