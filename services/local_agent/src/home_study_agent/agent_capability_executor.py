from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from time import perf_counter
from typing import Any

from .database import Database
from .agent_continuation import ContinuationStore
from .agent_runtime import AgentRuntimeStore, task_check_reply_arguments
from .mistake_book import MistakeBookService
from .mistake_dialogue import MistakeDialogueService
from .exam_simulation import ExamSimulationError, ExamSimulationService
from .repository import COMPLETION_CHECKS_BY_TASK_TYPE
from .behavior_model import BehaviorModelService
from .execution_trace import ExecutionTraceService, emit_trace_failure


class UnknownAgentStepError(KeyError):
    pass


class UnsupportedServerCapabilityError(ValueError):
    pass


class AgentExecutionConflict(UnsupportedServerCapabilityError):
    pass


class AgentCapabilityExecutor:
    """Executes registered server capabilities behind one durable boundary."""

    @staticmethod
    def safe_error_code(error):
        if (type(error) is ExamSimulationError and len(error.args) == 1
                and type(error.args[0]) is str and error.args[0] == 'EXAM_LISTENING_START_GRANT_STALE'):
            return 'EXAM_LISTENING_START_GRANT_STALE'
        return type(error).__name__[:80]

    def __init__(
        self,
        database: Database,
        repository: Any,
        exam_simulation: ExamSimulationService | None = None,
        writing_ocr_authorization: Any = None,
        page_source_store: Any = None,
    ):
        self.database = database
        self.repository = repository
        self.exam_simulation = exam_simulation or ExamSimulationService(database)
        self.execution_trace = ExecutionTraceService(database)
        self.writing_ocr_authorization = writing_ocr_authorization
        self.page_source_store = page_source_store

    def execute(self, turn_id: str) -> dict[str, Any]:
        trace_started = perf_counter()
        step = self._load(turn_id)
        if step['action'].get('source')=='oral_local_reference' or 'school_oral_reference' in step['action']:
            from .school_oral_reference import validate
            with self.database.connect() as c:
                c.execute('BEGIN')
                validate(c,turn_id)
        if step["execution_location"] != "server":
            raise UnsupportedServerCapabilityError("step requires a device or model bridge")
        if step['capability_name'] in {'task.start', 'task.resume'} and step['status'] in {'failed', 'completed'}:
            review_receipt = self._recorded_mistake_review(step)
            if review_receipt is not None:
                return review_receipt
        if step['capability_name'] == 'task.start' and step['status'] in {'failed', 'completed'}:
            from .task_start_binding import TaskStartBinding
            with self.database.connect() as c:
                c.execute('BEGIN')
                recorded = TaskStartBinding.recorded_start(c, turn_id)
            if recorded is not None:
                return self._task_start_replay(step, recorded)
        if step["status"] == "completed" and step["result_json"]:
            saved = json.loads(step["result_json"])
            if step['action'].get('source')=='oral_local_reference':
                return {**saved,'replayed':True,'recorded_receipt':True,'replay_audio_allowed':False,'evidence':[]}
            if step['capability_name'] == 'learning.review_revisit':
                return self._document_review_replay(step,saved)
            if step['capability_name']=='task.complete':
                with self.database.connect() as c:
                    c.execute('BEGIN')
                    from .writing_daily_followup import inspect_completion
                    if inspect_completion(c,turn_id,replay=True):
                        return {**saved,'replayed':True,'recorded_receipt':True,'replay_audio_allowed':False,'evidence':[]}
            if step['capability_name']=='task.start':
                with self.database.connect() as c:
                    c.execute('BEGIN')
                    from .writing_daily_followup import replay
                    daily=replay(c,turn_id)
                    if daily is not None:
                        return daily
            if step['capability_name'] in {'oral.retry','oral.independent_check'}:
                return self._oral_followup_replay(step)
            if (step['capability_name'] == 'task.resume'
                    and saved.get('result', {}).get('status') != 'awaiting_resources'):
                return self._task_resume_replay(step)
            return self._recorded_canonical_result(step, saved)
        if step["status"] not in {"planned", "waiting_child", "failed"}:
            raise AgentExecutionConflict("AGENT_STEP_ALREADY_EXECUTING" if step["status"] == "running"
                                         else "AGENT_STEP_NOT_EXECUTABLE")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute('''SELECT s.status,s.result_json,p.status AS plan_status
                FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE s.step_id=?''', (step["step_id"],)).fetchone()
            if current and current["status"] == "completed" and current["result_json"]:
                saved = json.loads(current["result_json"])
                if step['action'].get('source')=='oral_local_reference':
                    from .school_oral_reference import validate
                    validate(connection,turn_id)
                    return {**saved,'replayed':True,'recorded_receipt':True,'replay_audio_allowed':False,'evidence':[]}
                if step['capability_name'] == 'learning.review_revisit':
                    return self._document_review_replay(step,saved,connection=connection)
                if step['capability_name']=='task.complete':
                    from .writing_daily_followup import inspect_completion
                    if inspect_completion(connection,turn_id,replay=True):
                        return {**saved,'replayed':True,'recorded_receipt':True,'replay_audio_allowed':False,'evidence':[]}
                if step['capability_name']=='task.start':
                    from .writing_daily_followup import replay
                    daily=replay(connection,turn_id)
                    if daily is not None:
                        return daily
                if step['capability_name'] in {'oral.retry','oral.independent_check'}:
                    return self._oral_followup_replay(step,connection=connection)
                if (step['capability_name'] == 'task.resume'
                        and saved.get('result', {}).get('status') != 'awaiting_resources'):
                    return self._task_resume_replay(step)
                return self._recorded_canonical_result(step, saved)
            if not current or current["status"] not in {"planned", "waiting_child", "failed"}:
                raise AgentExecutionConflict("AGENT_STEP_ALREADY_EXECUTING")
            if current['plan_status'] == 'cancelled':
                raise AgentExecutionConflict('AGENT_EXECUTION_CANCELLED_BEFORE_DISPATCH')
            from .decision_record import validate_current, StaleDecisionContextError
            try:
                validate_current(connection, step["plan_id"])
            except StaleDecisionContextError as error:
                raise AgentExecutionConflict(str(error)) from error
            if step['capability_name']=='task.start' and current['status']=='failed':
                from .writing_daily_followup import recover_request
                recover_request(connection,turn_id)
                from .oral_daily_check import recover_request as recover_oral_check
                recover_oral_check(connection,turn_id)
            if step['capability_name']=='task.complete' and current['status']=='failed':
                from .writing_daily_followup import recover_completion
                recover_completion(connection,turn_id)
            if not ContinuationStore.execution_allowed(connection, turn_id):
                raise AgentExecutionConflict("AGENT_EXECUTION_AUTHORIZATION_CHANGED")
            AgentRuntimeStore.inspect_voice_control_execution(connection, turn_id, allow_planned=True)
            from .canonical_voice_execution import inspect as inspect_canonical_source
            try:
                inspect_canonical_source(connection, turn_id, page_source_store=self.page_source_store)
            except (ValueError, KeyError, TypeError) as error:
                raise AgentExecutionConflict('CANONICAL_VOICE_EXECUTION_SOURCE_CHANGED') from error
            from .foreground_independent_dialogue import validate_turn
            validate_turn(connection, turn_id)
            connection.execute(
                "UPDATE agent_step SET status = 'running', error_code = NULL, updated_at = ? WHERE step_id = ?",
                (now, step["step_id"]),
            )
            connection.execute(
                "UPDATE agent_plan SET status = 'running', updated_at = ? WHERE plan_id = ?",
                (now, step["plan_id"]),
            )
            connection.commit()
            self._trace_boundary(connection, step, 'action_scheduled', trace_started)
        try:
            with self.database.connect() as connection:
                if not ContinuationStore.execution_allowed(connection, turn_id, running=True):
                    raise AgentExecutionConflict("AGENT_EXECUTION_CANCELLED_BEFORE_DISPATCH")
                AgentRuntimeStore.inspect_voice_control_execution(connection, turn_id)
                validate_turn(connection, turn_id)
            result = self._dispatch(step)
        except Exception as error:
            error_code = self.safe_error_code(error)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE agent_step SET status = 'failed', error_code = ?, updated_at = ? WHERE step_id = ? AND status='running'",
                    (error_code, datetime.now(timezone.utc).isoformat(), step["step_id"]),
                )
                connection.execute(
                    "UPDATE agent_plan SET status = 'failed', updated_at = ? WHERE plan_id = ? AND status='running'",
                    (datetime.now(timezone.utc).isoformat(), step["plan_id"]),
                )
                connection.commit()
                self._trace_boundary(connection, step, 'action_result', trace_started)
            raise
        completed_at = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            validate_turn(connection, turn_id)
            if step['capability_name']=='task.complete':
                from .writing_daily_followup import inspect_completion
                from .agent_runtime import InvalidAgentPlanError
                try:
                    inspect_completion(connection,turn_id)
                except InvalidAgentPlanError as error:
                    # A saved task event is an actual fact. A late cancellation
                    # must not be rewritten as a retryable failure by the host.
                    raise AgentExecutionConflict('AGENT_EXECUTION_CANCELLED_BEFORE_RESULT') from error
            if step['capability_name'] in {'oral.retry','oral.independent_check'}:
                current = connection.execute('''SELECT p.status AS plan_status,s.status AS step_status
                    FROM agent_plan p JOIN agent_step s USING(plan_id)
                    WHERE p.agent_turn_id=? AND s.step_id=?''', (turn_id,step['step_id'])).fetchone()
                if (not current or current['plan_status']!='running' or current['step_status']!='running'
                        or not ContinuationStore.execution_allowed(connection,turn_id,running=True)):
                    raise AgentExecutionConflict('AGENT_EXECUTION_CANCELLED_BEFORE_RESULT')
                prompt=(result.get('result') or {}).get('prompt')
                if prompt:
                    row=connection.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
                    AgentRuntimeStore.inspect_oral_followup_execution(connection,turn_id,prepared=row)
            if (step["capability_name"] == "conversation.respond"
                    and step["action"].get("document_stop_followup_root")
                    and not ContinuationStore.execution_allowed(connection, turn_id, running=True)):
                raise AgentExecutionConflict("AGENT_EXECUTION_CANCELLED_BEFORE_RESULT")
            if step["capability_name"] in {"task.start", "task.resume", "companion.activity_feedback"} or step['capability_name'].startswith('learning_inputs.'):
                current = connection.execute("""SELECT s.status AS step_status,p.status AS plan_status
                    FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE s.step_id=?""", (step["step_id"],)).fetchone()
                if not current or current["step_status"] != "running" or current["plan_status"] != "running":
                    # Preserve the actual side-effect receipt without reviving
                    # a cancelled command or admitting its late speech.
                    connection.execute("UPDATE agent_step SET result_json=?,updated_at=? WHERE step_id=?",
                        (json.dumps({**result, "superseded": True}, ensure_ascii=False), completed_at, step["step_id"]))
                    connection.commit()
                    self._trace_boundary(connection, step, 'action_result', trace_started)
                    if step['capability_name'] == 'companion.activity_feedback':
                        from .companion_activity_voice import ActivityVoiceError
                        raise ActivityVoiceError('ACTIVITY_VOICE_SUPERSEDED_AFTER_DISPATCH')
                    if step['capability_name'].startswith('learning_inputs.'):
                        from .learning_input_revision import InputRevisionError
                        raise InputRevisionError('INPUT_REVISION_SUPERSEDED_AFTER_DISPATCH')
                    from .task_start_binding import TaskStartBindingError
                    raise TaskStartBindingError("TASK_START_SUPERSEDED_AFTER_DISPATCH")
            connection.execute(
                "UPDATE agent_step SET status = 'completed', result_json = ?, updated_at = ? WHERE step_id = ?",
                (json.dumps(result, ensure_ascii=False), completed_at, step["step_id"]),
            )
            if step["capability_name"] == "task.complete":
                value = result.get("result", {})
                # Retire the old prompt only with the newly persisted response.
                connection.execute(
                    """UPDATE agent_plan SET status='completed', updated_at=? WHERE status='waiting_child'
                       AND session_id=? AND plan_id!=? AND plan_id IN (
                         SELECT ap.plan_id FROM agent_plan ap JOIN agent_step ast ON ast.plan_id=ap.plan_id
                         JOIN agent_turn at ON at.agent_turn_id=ap.agent_turn_id
                         WHERE ast.capability_name='task.complete'
                           AND at.rowid < (SELECT rowid FROM agent_turn WHERE agent_turn_id=?)
                           AND json_extract(ast.result_json,'$.result.status')='awaiting_completion_check'
                           AND json_extract(ast.result_json,'$.result.plan_item_id')=?
                           AND json_extract(ast.result_json,'$.result.task_started_at') IS ?
                       )""",
                    (completed_at, step["session_id"], step["plan_id"], step["agent_turn_id"],
                     value.get("plan_item_id"), value.get("task_started_at")),
                )
            connection.execute(
                "UPDATE agent_plan SET status = ?, updated_at = ? WHERE plan_id = ?",
                ("waiting_child" if result.get("requires_child_reply") else "completed", completed_at, step["plan_id"]),
            )
            connection.commit()
            self._trace_boundary(connection, step, 'action_result', trace_started)
        return result

    def _document_review_replay(self,step,saved,*,connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self._document_review_replay(step,saved,connection=c)
        from .document_feedback_projection import validate_turn, explain
        from .document_independent_check import replayed
        try:
            from .document_spoken_conversation import validate_receipt
            validate_receipt(connection,step['agent_turn_id'],saved)
            if (saved.get('result') or {}).get('whole_review'):
                from .document_spoken_review import SpokenReview,speech
                request_id=saved['result']['whole_review']['request_turn_id']
                review=SpokenReview(None,None,None,None,None,None).saved(connection,request_id)
                if review!=saved['result']['whole_review'] or saved.get('response_text')!=speech(review['review']):
                    raise ValueError('DOCUMENT_WHOLE_RECEIPT_CHANGED')
            if (saved.get('result') or {}).get('whole_review_followup'):
                from .document_spoken_review import SpokenReview,clarification_for
                followup=saved['result']['whole_review_followup']
                _,projection,_=SpokenReview.guard(connection,followup['request_turn_id'],live=False)
                if (followup!=clarification_for(followup['request_turn_id'],projection)
                        or saved.get('response_text')!=followup['response_text']):
                    raise ValueError('DOCUMENT_WHOLE_FOLLOWUP_CHANGED')
            binding = validate_turn(connection,step['agent_turn_id'])
            if binding:
                turn=connection.execute('SELECT child_id,session_id,transcript,response_text FROM agent_turn WHERE agent_turn_id=?',
                    (step['agent_turn_id'],)).fetchone()
                expected=explain(connection,turn['child_id'],turn['session_id'],turn['transcript'],binding)
                if saved.get('response_text') != expected or turn['response_text'] != expected:
                    raise ValueError('DOCUMENT_FEEDBACK_RECEIPT_CHANGED')
        except (ValueError,TypeError,KeyError) as error:
            raise AgentExecutionConflict('DOCUMENT_FEEDBACK_PROJECTION_CHANGED') from error
        if (saved.get('result') or {}).get('whole_review_requested') or (saved.get('result') or {}).get('whole_review'):
            return {**saved,'replayed':True,'recorded_receipt':True,'replay_audio_allowed':False,'evidence':[]}
        return replayed(saved)

    def _oral_followup_replay(self,step,*,connection=None):
        from .diagnostic_prompt import DiagnosticPromptService
        from .agent_runtime import InvalidAgentPlanError
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self._oral_followup_replay(step,connection=c)
        c=connection
        session=DiagnosticPromptService._session(c,step['session_id'])
        row=c.execute('SELECT * FROM diagnostic_prompt WHERE prepare_action_id=?',(step['idempotency_key'],)).fetchone()
        if (not row or row['child_id']!=step['plan_child_id'] or row['origin_session_id']!=step['session_id']
                or session['status']!='active' or session['child_status']!='active'
                or row['status'] not in {'waiting_answer','answer_saved','answered'}):
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_SOURCE_CHANGED')
        if row['status']!='answered':
            AgentRuntimeStore.inspect_oral_followup_execution(c,step['agent_turn_id'],prepared=row)
            value=DiagnosticPromptService._result(c,row)
        else:
            if not DiagnosticPromptService._receipt(c,row):
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_SOURCE_CHANGED')
            value={'status':'answered','prompt':None,'prompt_id':row['prompt_id'],'item_version':row['item_version']}
        packet=json.loads(row['request_json'])['oral_followup']
        return {'capability':step['capability_name'],'response_text':'原来的记录已保存，这次没有重复出题。',
            'replayed':True,'recorded_receipt':True,'replay_audio_allowed':False,
            'result':{**value,'required_hint_level':packet['action']['required_hint_level'],
                'local_only':True,'cloud_transmission_allowed':False},'evidence':[]}

    def _trace_boundary(self, connection, step, event_type, started):
        # A fatal SQLite trace error must not roll back an actual task receipt.
        try:
            connection.execute('BEGIN IMMEDIATE')
            self.execution_trace.observe_host_boundary(connection=connection,
                turn_id=step['agent_turn_id'],step_id=step['step_id'],event_type=event_type,
                elapsed_ms=min(86400000,max(0,int((perf_counter()-started)*1000))))
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            emit_trace_failure()

    @staticmethod
    def _typed_exam_replay(step, saved):
        if step['capability_name'] not in {'exam.start', 'exam.resume', 'exam.submit'}:
            return None
        value = saved.get('result')
        if not isinstance(value, dict):
            return None
        exam = value.get('exam') or value
        if not isinstance(exam, dict):
            return None
        cue = exam.get('audio_delivery')
        if not isinstance(cue, dict) or cue.get('version') != 'exam_listening_delivery_v1':
            return None
        return {**saved, 'replayed': True, 'recorded_receipt': True,
                'replay_audio_allowed': False, 'evidence': []}

    @staticmethod
    def _recorded_canonical_result(step, saved):
        typed = AgentCapabilityExecutor._typed_exam_replay(step, saved)
        if typed is not None:
            return typed
        if str(step['turn_action_id'] or '').startswith('realtime-'):
            return {**saved, 'replayed': True, 'recorded_receipt': True, 'replay_audio_allowed': False, 'evidence': []}
        return saved

    def _load(self, turn_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT ast.*, ap.session_id, ap.plan_id, at.agent_turn_id, at.transcript, at.response_text, at.action_json,
                       at.orchestrator_version, at.client_action_id AS turn_action_id,
                       ap.child_id AS plan_child_id, at.child_id AS turn_child_id,
                       at.session_id AS turn_session_id, ds.child_id AS session_child_id
                FROM agent_step ast
                JOIN agent_plan ap ON ap.plan_id = ast.plan_id
                JOIN agent_turn at ON at.agent_turn_id = ap.agent_turn_id
                JOIN daily_session ds ON ds.session_id = ap.session_id
                WHERE ap.agent_turn_id = ? AND ast.step_index = 0
                """,
                (turn_id,),
            ).fetchone()
        if not row:
            raise UnknownAgentStepError(turn_id)
        if (row["plan_child_id"] != row["turn_child_id"] or row["turn_child_id"] != row["session_child_id"]
                or row["session_id"] != row["turn_session_id"]):
            raise AgentExecutionConflict("AGENT_STEP_OWNER_MISMATCH")
        result = dict(row)
        result["arguments"] = json.loads(result["arguments_json"])
        result["action"] = json.loads(result["action_json"])
        return result

    def _dispatch(self, step: dict[str, Any]) -> dict[str, Any]:
        capability = step["capability_name"]
        session_id = step["session_id"]
        arguments = step["arguments"]
        action = step["action"]
        key = step["idempotency_key"]
        workspace = self.repository.get_workspace(session_id)
        response = step["response_text"]
        value: Any

        if capability in {'learning.review_schedule', 'learning.review_revisit'}:
            if capability == 'learning.review_schedule':
                from .document_review_schedule import execute
            else:
                from .document_revisit import execute
            from .document_goal_handoff import DocumentGoalError
            try:
                outcome = execute(self.database, step['agent_turn_id'])
            except DocumentGoalError as error:
                raise AgentExecutionConflict(str(error)) from error
            except (ValueError, TypeError, KeyError) as error:
                raise AgentExecutionConflict('DOCUMENT_REVIEW_BINDING_INVALID') from error
            response, value = outcome['response_text'], outcome['result']
            if (capability=='learning.review_revisit' and outcome.get('replayed')
                    and (value.get('spoken_revision_event') or value.get('spoken_undo_event'))):
                return dict(capability=capability,response_text=response,result=value,
                    replayed=True,recorded_receipt=True,replay_audio_allowed=False,evidence=[])
        elif capability == "system.inspect_learning":
            from .learning_system_facts import LearningSystemFacts
            inspected = LearningSystemFacts(self.database).inspect(
                step["plan_child_id"], session_id,
                str(arguments.get("scope") or "all"),
                str(arguments.get("track") or "all"),
                exclude_turn_id=step['agent_turn_id'],
            )
            response = inspected["response_text"]
            value = inspected["facts"]
        elif capability == "conversation.respond":
            from .school_oral_reference import validate
            with self.database.connect() as c:
                c.execute('BEGIN')
                oral_reference=validate(c,step['agent_turn_id'])
            if oral_reference:
                response,value=oral_reference['response_text'],oral_reference['result']
            else:
                response = str(arguments.get("response") or response or "我在听。")[:500]
                value = {"status": "responded"}
        elif capability == "speech.repeat":
            with self.database.connect() as connection:
                previous = connection.execute(
                    """SELECT at.agent_turn_id,at.response_text FROM agent_turn at
                       JOIN daily_session ds ON ds.session_id=at.session_id AND ds.child_id=at.child_id
                       WHERE at.session_id=? AND at.tool_status='completed' AND trim(at.response_text)!=''
                         AND at.intent!='speech_echo_ignored'
                         AND COALESCE(json_extract(at.action_json,'$.suppress_speech'),0)!=1
                         AND COALESCE(json_extract(at.action_json,'$.type'),'') NOT IN ('repeat_response','stop_speech')
                         AND at.rowid < (SELECT rowid FROM agent_turn WHERE agent_turn_id=?)
                       ORDER BY at.rowid DESC LIMIT 1""", (session_id, step["agent_turn_id"]),
                ).fetchone()
                connection.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.local_only',json('true'),'$.cloud_transmission_allowed',json('false')) WHERE agent_turn_id=?",
                                   (step["agent_turn_id"],))
                connection.commit()
            return {"capability": capability,
                    "response_text": previous["response_text"] if previous else "这段会话还没有已完成的回复可以重播。",
                    "result": {"status": "replayed" if previous else "no_previous_response",
                               "replayed_turn_id": previous["agent_turn_id"] if previous else None, "local_only": True},
                    "evidence": ["response_replayed"] if previous else []}
        elif capability == "behavior.feedback":
            feedback = arguments.get("feedback")
            if feedback not in {"accepted", "declined"}:
                raise UnsupportedServerCapabilityError("INVALID_BEHAVIOR_FEEDBACK")
            behavior = BehaviorModelService(self.database)
            child_id = workspace["session"]["child_id"]
            saved = behavior.feedback_for_action(key, child_id, session_id)
            if not saved and step.get("orchestrator_version") == "agent_runtime_deterministic_v1":
                saved = behavior.feedback_for_action("voice:" + step["turn_action_id"], child_id, session_id)
            if saved:
                if saved["feedback"] != feedback:
                    raise UnsupportedServerCapabilityError("BEHAVIOR_FEEDBACK_PAYLOAD_CONFLICT")
                value = {"intervention_id": saved["intervention_id"], "feedback": saved["feedback"]}
            else:
                now = datetime.now(timezone.utc).isoformat()
                pending = behavior.pending_feedback(child_id, session_id, now)
                if pending.get("status") != "pending":
                    raise UnsupportedServerCapabilityError("BEHAVIOR_FEEDBACK_NOT_PENDING")
                value = behavior.record_feedback(pending["intervention"]["intervention_id"], feedback == "accepted", now,
                                                 str(arguments.get("response_kind") or "")[:120] or None,
                                                 key, child_id, session_id)
            response = "你的反馈已经保存。" + ("我会先等你准备好。" if feedback == "declined" else "需要时随时叫我。")
        elif capability == "document.set_occlusion_reminders":
            enabled = arguments.get("enabled")
            if type(enabled) is not bool or set(arguments) != {"enabled"}:
                raise UnsupportedServerCapabilityError("INVALID_DOCUMENT_GUIDANCE_SETTING")
            value = {"occlusion_reminders_enabled": enabled, "settings_saved": True}
            response = ("遮挡语音提醒已开启。" if enabled else "遮挡语音提醒已关闭。") + "页面质量检查仍然保留。"
        elif capability == "learning.request_response":
            prompt = arguments.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 500:
                raise UnsupportedServerCapabilityError("invalid learning response prompt")
            return {
                "capability": capability, "response_text": prompt,
                "result": {"status": "awaiting_child_response", "next_check": prompt, "subject": arguments["subject"]},
                "evidence": ["response_requested"], "requires_child_reply": True,
            }
        elif capability == 'mistake.repair_question':
            from .mistake_source_revision import offer, target
            from .diagnostic_prompt import _TransactionDatabase
            from .mistake_capture import InvalidMistakeCaptureCandidateError
            turn_id = step['agent_turn_id']
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                dialogue = MistakeDialogueService(_TransactionDatabase(c))
                focus = dialogue.state(session_id)
                source = c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=? AND session_id=?',
                                   (turn_id, session_id)).fetchone()
                manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?',
                                                (step['plan_id'],)).fetchone()[0])
                expected = manifest.get('mistake_repair_target')
                fresh = target(c, focus)
                recorded = dialogue._replay(session_id, key, source['transcript']) if source else None
                if (not focus or not source or not arguments or any(v is None for v in arguments.values())
                        or set(arguments) - {'question_quote', 'original_answer_quote'}
                        or (not recorded and (not expected or expected != fresh))):
                    raise InvalidMistakeCaptureCandidateError('刚才的原题、作答或待确认提案已变化，没有套用旧更正。请重新说明要更正的内容。')
                value = recorded or offer(dialogue, focus, arguments.get('question_quote'), key, source['transcript'],
                                          replacement_answer=arguments.get('original_answer_quote'))
                c.commit()
            response = value['response_text']
            return {'capability': capability, 'response_text': response, 'result': value,
                    'requires_child_reply': True, 'evidence': ['mistake_question_repair_offer']}
        elif capability == "mistake.dialogue":
            dialogue = MistakeDialogueService(self.database)
            if action.get('source') in {'mistake_question_voice_confirmation', 'mistake_question_voice_cancellation',
                                       'mistake_question_joint_clarification'}:
                live = dialogue.state(session_id)
                recorded = dialogue._replay(session_id, key, str(arguments['utterance']))
                if not recorded and (not live or not action.get('question_confirmation')
                        or live['state'].get('question_confirmation') != action['question_confirmation']):
                    from .mistake_capture import InvalidMistakeCaptureCandidateError
                    raise InvalidMistakeCaptureCandidateError('待核对的题目提案已经变化，没有执行旧的确认或取消。')
            value = dialogue.respond(session_id, str(arguments["utterance"]), key, authorized_turn_id=step["agent_turn_id"])
            response = value["response_text"]
            listening = self._prepare_active_mistake_listening(step, value)
            if listening:
                value['diagnostic_prompt'] = listening.get('prompt')
                value['listening_resource'] = listening.get('resource')
                if listening['status'] == 'awaiting_resources':
                    value['listening_status'] = 'awaiting_resources'
                    response = ('这道听力题的已审核原音还没准备好。'
                                '我保留当前错题进度，不显示原文、不接收本轮作答。')
            teaching = dialogue.teaching_request(value)
            if teaching:
                value["teaching_request"] = teaching
            elif value.get('state', {}).get('teaching_requested') and value.get('mistake_id'):
                review = dialogue.book.get(value['mistake_id'], value['child_id']).get('question_review')
                if review:
                    value['teaching_status'] = 'question_confirmation_required'
                    response = (value['response_text']
                                if review['status'] == 'confirmed' else
                                '这道公式题的题干还需要对照原题核对，我先不沿用可能识别错的公式讲解。核对记录已保留。')
        elif capability == "mistake.open":
            dialogue = MistakeDialogueService(self.database)
            with self.database.connect() as c:
                manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?', (step['plan_id'],)).fetchone()[0])
            selection = manifest.get('saved_review_selection')
            if selection:
                from .foundation_remediation import review_handoff
                current_focus = dialogue.state(session_id)
                with self.database.connect() as c:
                    handoff = review_handoff(c, current_focus) if current_focus else None
                if (not current_focus or current_focus['mistake_id'] != selection['mistake_id']
                        or handoff != selection['handoff']):
                    from .mistake_capture import InvalidMistakeCaptureCandidateError
                    raise InvalidMistakeCaptureCandidateError('当前复习目标已经变化，未切换到旧复习任务。')
                focus = dialogue.open_review(session_id, key, mistake_id=selection['mistake_id'])
            else:
                focus = dialogue.open_review(session_id, key)
            current = self._current_mistake(session_id)
            value = focus or current or {"status": "no_actionable_mistake"}
            response = focus["response_text"] if focus else self._mistake_activity_prompt(current)
        elif capability == "mistake.review":
            current = self._require_current_mistake(session_id)
            child_id = workspace["session"]["child_id"]
            value = MistakeBookService(self.database).confirm(
                current["mistake_id"], child_id,
                str(arguments.get("decision") or action.get("decision") or "confirm"), key,
            )
            value["next_activity"] = MistakeBookService(self.database).next_activity(
                value["mistake_id"], child_id
            )
            response = self._mistake_activity_prompt(value)
        elif capability == "mistake.diagnose":
            current = self._require_current_mistake(session_id)
            child_id = workspace["session"]["child_id"]
            value = MistakeBookService(self.database).diagnose(
                current["mistake_id"], child_id,
                str(arguments["error_type"]), str(arguments["explanation"]),
                float(arguments.get("confidence", 0.75)), key,
            )
            value["next_activity"] = MistakeBookService(self.database).next_activity(
                value["mistake_id"], child_id
            )
            response = self._mistake_activity_prompt(value)
        elif capability == "mistake.complete_remediation":
            current = self._require_current_mistake(session_id)
            child_id = workspace["session"]["child_id"]
            outcome = str(arguments.get("outcome") or action.get("outcome") or "still_stuck")
            value = MistakeBookService(self.database).add_remediation(
                current["mistake_id"], child_id,
                self._remediation_step_type(current), outcome,
                str(arguments.get("notes") or action.get("notes") or "孩子通过语音反馈补救结果"), key,
            )
            value["next_activity"] = MistakeBookService(self.database).next_activity(
                value["mistake_id"], child_id
            )
            response = self._mistake_activity_prompt(value)
        elif capability == "mistake.submit_response":
            # Persisted legacy commands have no capture-time question authority.
            # New answers use mistake.dialogue; never grade legacy arguments
            # against whatever next_activity happens to be now.
            dialogue = MistakeDialogueService(self.database)
            focus = dialogue.state(session_id) or dialogue.open_review(session_id, key)
            return {
                "capability": capability,
                "response_text": ("先核对这道题，再说你的答案；刚才那句话没有计分。" + dialogue.prompt(focus)
                                  if focus else "目前没有待处理的错题，你可以把需要订正的题放到高拍仪下面。"),
                "result": {**(focus or {}), "status": "requires_new_answer" if focus else "no_actionable_mistake",
                           "legacy_answer_scored": False},
                "evidence": ["mistake_focus_restored"] if focus else [],
                "requires_child_reply": bool(focus),
            }
        elif capability == "mistake.activity_control":
            current = self._current_mistake(session_id)
            value = current or {"status": "no_actionable_mistake"}
            response = self._mistake_activity_prompt(current)
        elif capability == "planning.describe":
            response, value = self._plan_progress(workspace)
        elif capability == "companion.start_activity":
            activity_type = str(arguments["activity_type"])
            opening = None
            if action.get("source") == "agent_runtime_planner":
                from .companion_dialogue import enforce_companion_reply
                opening = enforce_companion_reply(str(arguments.get("opening") or ""))
                if not opening:
                    raise UnsupportedServerCapabilityError("cloud activity opening missing or unsafe")
            elif action.get("source") == "content_free_voice_control":
                from .voice_control_intent import VoiceControlIntent
                with self.database.connect() as c:
                    receipt = VoiceControlIntent.inspect_execution(c, step['agent_turn_id'])
                if not receipt or receipt['control'] != 'companion.start_activity':
                    raise AgentExecutionConflict('ANSWER_CONTROL_SCOPE_CHANGED')
                opening = VoiceControlIntent.activity_arguments(activity_type)['opening']
            topic = str(arguments.get("topic") or "孩子现在感兴趣的主题")
            active_item = next((item for item in workspace["plan_items"] if item["status"] == "active"), None)
            paused_plan_item_id = None
            if active_item and active_item["source_type"] != "planned_break":
                paused_plan_item_id = active_item["plan_item_id"]
            value = AgentRuntimeStore(self.database).start_companion_activity(
                session_id, activity_type, topic, key, paused_plan_item_id, pause_before_create=True
            )
            response = opening or self._companion_activity_starter(activity_type, topic)
        elif capability == "companion.end_activity":
            ended = AgentRuntimeStore(self.database).end_companion_activity(session_id)
            value = ended or {"status": "no_active_activity"}
            response = "这轮活动先停在这里。刚才做了哪些，感觉怎么样？准备好后告诉我，我们再继续刚才的学习。" if ended else "现在没有正在进行的陪伴活动。"
        elif capability == "companion.activity_feedback":
            from .companion_activity_voice import CompanionActivityVoiceService
            service = CompanionActivityVoiceService(self.database)
            value = service.execute(step["agent_turn_id"])
            if arguments['operation'] == 'request_resume':
                value = service.resume_original(step['agent_turn_id'])
            response = value["response_text"]
            with self.database.connect() as connection:
                connection.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.local_only',json('true'),'$.cloud_transmission_allowed',json('false')) WHERE agent_turn_id=?",
                                   (step["agent_turn_id"],))
                connection.commit()
            return {"capability": capability, "response_text": response, "result": value,
                    "evidence": value.get("evidence", []),
                    "requires_child_reply": value['activity']['resume']['status'] != 'confirmed'}
        elif capability == "planning.generate":
            value = self.repository.generate_plan(session_id, key)
            response = self._plan_summary(value)
        elif capability == "planning.reorder":
            value = self.repository.reorder_plan(
                session_id, str(arguments["strategy"]), "孩子通过语音调整计划顺序", key
            )
            response = self._plan_summary(value)
        elif capability == "session.transition":
            value = self.repository.transition(
                session_id, workspace["session"]["state"], str(arguments["target"]),
                "agent_runtime", key,
            )
        elif capability == "checkin.update_context":
            current = workspace["planning_context"]
            value = self.repository.save_planning_context(
                session_id,
                str(arguments.get("energy_level") or current["energy_level"]),
                int(arguments.get("fixed_commitment_minutes", current["fixed_commitment_minutes"])),
                arguments.get("fixed_commitment_note", current["fixed_commitment_note"]),
                arguments.get("tomorrow_event_subject", current["tomorrow_event_subject"]),
                arguments.get("tomorrow_event_title", current["tomorrow_event_title"]),
                int(arguments.get("tomorrow_event_minutes", current["tomorrow_event_minutes"])),
                key,
            )
        elif capability == "recall.add":
            value = self.repository.add_recall(
                session_id, str(arguments["subject"]), str(arguments["summary"]), key
            )
        elif capability == "recall.add_batch":
            saved = []
            for index, item in enumerate(arguments["items"]):
                saved.append(self.repository.add_recall(
                    session_id, str(item["subject"]), str(item["summary"]), f"{key}:item:{index}"
                ))
            value = {"status": "saved", "count": len(saved), "items": saved}
            response = f"已逐项保存{len(saved)}科课堂内容：" + "、".join(
                str(item["subject"]) for item in arguments["items"]
            ) + "。你可以继续补充，说完后告诉我。"
        elif capability == "recall.none":
            value = self.repository.declare_empty_stage(session_id, "no_recall", key)
        elif capability == "homework.add":
            value = self.repository.add_homework(
                session_id, str(arguments["subject"]), str(arguments["title"]),
                int(arguments.get("expected_minutes", 20)), str(arguments.get("task_type", "written")),
                arguments.get("range_text"), arguments.get("due_at"),
                arguments.get("special_requirements"), None, None, key,
            )
        elif capability == "homework.add_batch":
            saved = []
            for index, item in enumerate(arguments["items"]):
                saved.append(self.repository.add_homework(
                    session_id, str(item["subject"]), str(item["title"]),
                    int(item.get("expected_minutes", 20)), str(item.get("task_type", "written")),
                    item.get("range_text"), item.get("due_at"), item.get("special_requirements"),
                    None, None, f"{key}:item:{index}",
                ))
            value = {"status": "saved", "count": len(saved), "items": saved}
            response = f"已逐项保存{len(saved)}项作业：" + "、".join(
                f"{item['subject']}{int(item.get('expected_minutes', 20))}分钟" for item in arguments["items"]
            ) + "。还有的话继续说，全部说完后我再排计划。"
        elif capability in {'learning_inputs.review_unplaced', 'learning_inputs.review_reclaimed_scope'}:
            from .learning_input_carryover import LearningInputCarryoverService, CarryoverConflict
            from .learning_input_revision import InputRevisionError
            service = LearningInputCarryoverService(self.database)
            service.initialize()
            with self.database.connect() as connection:
                owner = connection.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                           (step['agent_turn_id'],)).fetchone()
            binding = json.loads(owner[0]).get('learning_input_unplaced_binding', {}) if owner else {}
            try:
                if capability == 'learning_inputs.review_reclaimed_scope':
                    value = service.review_reclaimed_scope(step['plan_child_id'], binding.get('obligation_id'), session_id,
                        arguments.get('operation'), binding.get('revision'), binding.get('source_version'), key,
                        expected_context_version=binding.get('context_version'), authorized_turn_id=step['agent_turn_id'],
                        request_quote=arguments.get('request_quote'), remaining_minutes=arguments.get('remaining_minutes'),
                        remaining_scope_quote=arguments.get('remaining_scope_quote'))
                else:
                    value = service.review_unplaced(step['plan_child_id'], binding.get('obligation_id'), session_id,
                        arguments.get('decision'), binding.get('revision'), binding.get('source_version'), key,
                        expected_context_version=binding.get('context_version'), authorized_turn_id=step['agent_turn_id'],
                        request_quote=arguments.get('request_quote'), not_before=arguments.get('not_before'))
            except CarryoverConflict as error:
                raise InputRevisionError(str(error)) from error
            if value['status'] == 'scope_confirmed':
                response = f"已保存你说的剩余内容和{value['remaining_minutes']}分钟估时，还没有重新排入或算作完成。今天继续还是留到之后？"
            elif value['status'] == 'scope_withdrawn':
                response = '已撤回刚才的剩余内容估计，原来的学习和用时记录仍然保留。请重新告诉我还剩什么。'
            elif value['status'] == 'accepted_for_planning':
                response = f"已把剩余{value['remaining_minutes']}分钟作业加入今天的待排清单，还没有开始或算作完成。"
            elif value['status'] == 'dropped':
                response = '已取消这项剩余安排，不会把它记成做完或已经掌握。'
            elif value.get('not_before'):
                response = f"已保留到{value['not_before']}再安排，不会提前恢复。"
            else:
                response = f"已实际补排{value['allocated_minutes']}分钟，剩余{value['remaining_minutes']}分钟仍保留待安排。"
            return {'capability': capability, 'response_text': response, 'result': value,
                    'evidence': ['input_remaining_scope_reviewed' if capability == 'learning_inputs.review_reclaimed_scope' else 'input_unplaced_reviewed'],
                    'requires_child_reply': capability == 'learning_inputs.review_reclaimed_scope',
                    'local_only': True, 'cloud_transmission_allowed': False}
        elif capability in {'learning_inputs.propose_revision', 'learning_inputs.confirm_revision',
                            'learning_inputs.select_target', 'learning_inputs.cancel_revision'}:
            from .learning_input_revision import LearningInputRevisionService, InputRevisionError
            from .learning_input_replan import LearningInputReplanService, ReplanConflict
            service = LearningInputRevisionService(self.database)
            service.initialize()
            replanner = LearningInputReplanService(self.database)
            replanner.initialize()
            value = service.execute(step['agent_turn_id'])
            evidence = []
            if value.get('status') == 'applied' and value.get('event_id'):
                evidence = ['input_revision_applied']
                if value.get('impact', {}).get('requires_replan'):
                    try:
                        replanned = replanner.execute_replan(step['plan_child_id'], value['event_id'], f"input-replan:{step['agent_turn_id']}")
                    except ReplanConflict as error:
                        # The child's confirmed correction is already durable.
                        # A later planning conflict must not undo it or claim a plan.
                        raise InputRevisionError(str(error)) from error
                    receipt = replanned['receipt']
                    value = {**value, 'replan': replanned, 'response_text': '修订已保存，原来的执行记录和学习证据没有改动。'}
                    if receipt['status'] == 'deleted':
                        value['response_text'] += '这项作业尚未开始的安排已取消。'
                    else:
                        value['response_text'] += f"本次补排了{receipt['allocated_minutes']}分钟作业。"
                        remaining = sum(row['minutes'] for row in replanned['unplaced'])
                        if remaining:
                            value['response_text'] += f'还有{remaining}分钟保留为待安排，不会挤占休息或自动延长学习。'
                        elif replanned['unplaced']:
                            value['response_text'] += '已执行部分与修订内容还需要核对，已保留待处理记录。'
                    evidence.append('input_revision_replan_recorded')
            elif value.get('status') == 'cancelled':
                evidence = ['input_revision_cancelled']
            elif value.get('request_id') and value.get('readback_version'):
                evidence = ['input_revision_target_selected' if capability.endswith('select_target') else 'input_revision_proposed']
            with self.database.connect() as connection:
                connection.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.local_only',json('true'),'$.cloud_transmission_allowed',json('false')) WHERE agent_turn_id=?",
                                   (step['agent_turn_id'],))
                connection.commit()
            return {'capability': capability, 'response_text': value['response_text'], 'result': value,
                    'evidence': evidence, 'requires_child_reply': value.get('requires_child_reply', False)}
        elif capability == "homework.none":
            value = self.repository.declare_empty_stage(session_id, "no_homework", key)
        elif capability == "exam.start":
            track = str(arguments.get("track") or action.get("track") or "auto")
            subject = str(arguments.get("subject") or action.get("subject") or "")
            if track == "auto":
                child = next(
                    item for item in self.repository.list_children()
                    if item["child_id"] == workspace["session"]["child_id"]
                )
                track = "competition_math" if child["grade"] == "四年级" and subject == "数学" else "pet" if subject == "英语" and child["grade"] == "四年级" else "junior_exam"
            mode = str(arguments.get("mode") or action.get("mode") or "full")
            defaults = {"pet": (17, 60), "competition_math": (10, 45), "junior_exam": (6, 60)}
            count, minutes = defaults.get(track, (6, 45))
            try:
                value = self.exam_simulation.start(
                    session_id, track, subject, mode,
                    int(arguments.get("requested_items") or action.get("requested_items") or count),
                    int(arguments.get("time_limit_minutes") or action.get("time_limit_minutes") or minutes),
                    key,
                    arguments.get("blueprint_id") or action.get("blueprint_id"),
                )
                response = self._exam_prompt(value)
                value["answer_binding"] = self.exam_simulation.arrival_binding(session_id, run_id=value["run_id"])
            except ExamSimulationError as error:
                value = {
                    "status": "not_ready",
                    "track": track,
                    "subject": subject,
                    "mode": mode,
                    "reason": str(error),
                }
                if str(error) == "EXAM_LISTENING_PLAYBACK_SCOPE_UNSUPPORTED":
                    value.update(status="blocked", delivery_status="blocked",
                        delivery_blockers=[str(error)], mastery_update_allowed=False)
                    response = self._exam_prompt(value)
                elif str(error) == 'EXAM_ORDINARY_DRAFT_PENDING':
                    response = '你原来的题目和已保存答案都保留着。请先完成原题，或明确告诉我取消原题，再开始整卷。'
                elif str(error) in {'EXAM_MEDIA_SOURCE_GROUP_INCOMPLETE','EXAM_LISTENING_GROUP_BLUEPRINT_QUOTA_UNAVAILABLE'}:
                    response = '这套卷需要完整的原听力题组，目前审核题源还不能满足整卷要求。请家长核对原题组；现在没有开始计时。'
                elif str(error)=='EXAM_LISTENING_ORIGINAL_INSTRUCTION_REQUIRED':
                    response = '这套卷的原听力播放要求还没有核验。请家长先审核完整原题和原播放要求，再重新开始整卷；现在没有开始计时。'
                else:
                    response = str(error)
        elif capability == "exam.submit":
            bound = action.get("exam_input_binding")
            if not isinstance(bound, dict) or set(bound) != {"run_id", "position", "item_id", "source_version"}:
                raise ExamSimulationError("EXAM_ARRIVAL_BINDING_REQUIRED")
            submitted = self.exam_simulation.submit_bound(
                session_id, **bound, answer=str(arguments.get("answer") or step["transcript"]), client_action_id=key,
                authorized_turn_id=step["agent_turn_id"],
            )
            value = submitted
            response = self._exam_prompt(submitted["exam"], submitted["attempt"])
            value["answer_binding"] = self.exam_simulation.arrival_binding(session_id, run_id=submitted["exam"]["run_id"])
        elif capability in {"exam.pause", "exam.resume", "exam.cancel"}:
            run_id = action.get("exam_run_id")
            current = self.exam_simulation.status(run_id) if run_id else None
            from .voice_control_intent import VoiceControlIntent
            with self.database.connect() as connection:
                recorded_control = VoiceControlIntent.recorded_execution(
                    connection, step['agent_turn_id'], capability, run_id) if run_id else None
            if not current:
                value = {"status": "no_active_exam"}
                response = "现在没有正在进行的模拟卷。"
            elif recorded_control:
                value = current
                response = '刚才的模拟卷控制已经执行并保存，这次没有重复操作。'
            elif current["session_id"] != session_id or current["status"] not in {"active", "paused"}:
                raise ExamSimulationError("EXAM_SLOT_NOT_ACTIVE")
            elif capability == "exam.pause":
                value = self.exam_simulation.pause(current["run_id"], authorized_turn_id=step['agent_turn_id'])
                response = "模拟卷已暂停，已用时间已保存。想继续时直接说“继续模拟卷”。"
            elif capability == "exam.resume":
                value = self.exam_simulation.resume(current["run_id"], authorized_turn_id=step['agent_turn_id'])
                response = self._exam_prompt(value)
                value["answer_binding"] = self.exam_simulation.arrival_binding(session_id, run_id=current["run_id"])
            else:
                value = self.exam_simulation.cancel(current["run_id"], authorized_turn_id=step['agent_turn_id'])
                response = self._exam_prompt(value)
        elif capability in {'oral.retry','oral.independent_check'}:
            from .diagnostic_prompt import DiagnosticPromptService, DiagnosticPromptError
            packet = action.get('oral_followup_binding') or {}
            target = packet.get('action') or {}
            with self.database.connect() as c:
                selected = DiagnosticPromptService._item(c, target.get('child_id'), target.get('diagnostic_item_id'))
            if not selected:
                raise DiagnosticPromptError('oral_followup_source_unavailable')
            value = DiagnosticPromptService(self.database).prepare(session_id,track=selected['track'],
                preferred_item_id=target.get('diagnostic_item_id'),client_action_id=key,
                _oral_authorized_turn_id=step['agent_turn_id'],_oral_followup=packet)
            ready = value['status']=='waiting_answer' and bool(value.get('prompt'))
            return {'capability':capability,'response_text':
                ('请看这道题。你准备好后再说一次。' if capability=='oral.retry' else '这是一道新题，请自己试试。')
                if ready else '原题或请求已变化，没有另开题目。',
                'result':{**value,'required_hint_level':target['required_hint_level'],
                    'local_only':True,'cloud_transmission_allowed':False},
                'evidence':[('oral_retry_prompt_bound' if capability=='oral.retry' else 'oral_independent_prompt_bound')] if ready else []}
        elif capability == 'writing.independent_check' or (
                capability == 'writing.revise_original' and 'writing_followup_binding' in action):
            from .writing_teaching import WritingTeachingService
            from .diagnostic_prompt import DiagnosticPromptService
            packet = action.get('writing_followup_binding')
            kind = 'independent_check' if capability == 'writing.independent_check' else 'revision'
            blocked = {'capability': capability, 'response_text': '原题或评价已变化，先核对原记录，没有另换题目。',
                'result': {'status': 'blocked', 'reason': 'writing_followup_binding_invalid',
                    'local_only': True, 'cloud_transmission_allowed': False}, 'evidence': []}
            if (not isinstance(packet, dict) or set(packet) != {'action', 'binding_version'}
                    or not isinstance(packet['action'], dict) or packet['action'].get('kind') != kind
                    or 'writing_revision_binding' in action):
                return blocked
            resolved = WritingTeachingService(self.database).revalidate_action(session_id,
                action=packet['action'], binding_version=packet['binding_version'])
            if resolved['status'] != 'ready':
                return {**blocked, 'result': resolved}
            target = resolved['action']
            from .writing_response_origin import writing_item
            with self.database.connect() as c:
                _, node = writing_item(c,target['child_id'],target['diagnostic_item_id'])
            value = DiagnosticPromptService(self.database).prepare(session_id, track=node['track'],
                preferred_item_id=target['diagnostic_item_id'], client_action_id=key,
                writing_followup={field: target[field] for field in
                    ('parent_prompt_id', 'parent_item_version', 'kind')} |
                    {'binding_version': resolved['binding_version']},
                _writing_authorized_turn_id=step['agent_turn_id'])
            if value['status'] != 'waiting_answer' or not value.get('prompt'):
                return {**blocked, 'result': {**value, 'status': 'blocked',
                    'local_only': True, 'cloud_transmission_allowed': False}}
            response = ('原稿已保留，请按刚才的反馈修改，写好后告诉我。' if kind == 'revision' else
                '这次换一道没有做过的题，先独立试试。' + value['prompt']['item']['prompt'])
            return {'capability': capability, 'response_text': response,
                'result': {**value, 'local_only': True, 'cloud_transmission_allowed': False},
                'evidence': ['writing_revision_prompt_bound' if kind == 'revision' else 'writing_independent_prompt_bound']}
        elif capability == "writing.revise_original":
            from .writing_teaching import WritingTeachingService
            from .diagnostic_prompt import DiagnosticPromptService
            binding = action.get('writing_revision_binding')
            if not isinstance(binding,dict):
                raise AgentExecutionConflict('WRITING_REVISION_BINDING_REQUIRED')
            resolved = WritingTeachingService(self.database).resolve_reference_revision(session_id,
                prompt_id=binding.get('prompt_id'),item_version=binding.get('item_version'),
                reference_turn_id=binding.get('reference_turn_id'),request_turn_id=step['agent_turn_id'])
            if resolved['status'] != 'ready':
                return {'capability':capability,'response_text':'原稿或修改请求需要重新核对，没有创建另一份作答。',
                    'result':resolved,'evidence':[]}
            lineage = {field:resolved['action'][field] for field in
                ('parent_prompt_id','parent_item_version','reference_turn_id','request_turn_id')}
            from .writing_response_origin import writing_item
            with self.database.connect() as c:
                _, node = writing_item(c,resolved['action']['child_id'],resolved['action']['diagnostic_item_id'])
            value = DiagnosticPromptService(self.database).prepare(session_id,track=node['track'],
                preferred_item_id=resolved['action']['diagnostic_item_id'],client_action_id=key,
                writing_reference_revision={**lineage,'binding_version':resolved['binding_version']})
            if value['status'] != 'waiting_answer' or not value.get('prompt'):
                return {'capability':capability,'response_text':'修改稿还没有准备完成，原稿已保留，先核对当前任务。',
                    'result':{**value,'status':'blocked','local_only':True,'cloud_transmission_allowed':False},'evidence':[]}
            return {'capability':capability,'response_text':'原稿保留好了。请在纸上修改，写好后告诉我，我会重新核对这一稿。',
                'result':{**value,'local_only':True,'cloud_transmission_allowed':False},
                'evidence':['writing_revision_prompt_bound']}
        elif capability == "writing.confirm_original":
            bound = action.get("writing_ocr_binding")
            if not isinstance(bound, dict) or not self.writing_ocr_authorization:
                raise AgentExecutionConflict("WRITING_CONFIRMATION_BINDING_REQUIRED")
            permission = self.writing_ocr_authorization.authorize_use(
                session_id, confirmation_id=bound.get("confirmation_id"),
                binding_version=bound.get("binding_version"),
                authorizing_turn_id=step["agent_turn_id"], client_action_id=key,
            )
            return {
                "capability": capability,
                "response_text": "原稿确认已保存。接下来按这份原稿核对本题要求，这次确认本身不计分。",
                "result": {**permission, "local_only": True, "cloud_transmission_allowed": False},
                "evidence": ["writing_original_use_authorized"],
            }
        elif capability == "break.begin":
            value = self.repository.begin_break(session_id, key, authorized_turn_id=step['agent_turn_id'])
            if value.get('replayed'):
                response = '刚才的休息操作已保存，这次只核对记录，没有再次暂停任务。'
        elif capability == "break.resume":
            value = self.repository.resume_after_break(session_id, key, authorized_turn_id=step['agent_turn_id'])
            if value.get('replayed'):
                response = '刚才的恢复操作已保存，这次只核对记录，没有再次启动计时。'
            if value.get('input_reentry'):
                response = value['input_reentry']['response_text']
        elif capability == "task.start":
            from .task_start_binding import TaskStartBinding
            item = TaskStartBinding(self.database).resolve(step["agent_turn_id"])
            if item and item['source_type'] == 'mistake_review':
                return self._start_mistake_review(step, item, resume=item['_start_operation'] == 'resume')
            with self.database.connect() as c:
                pet_writing = bool(item) and self.repository._is_pet_writing_task(c, item)
                from .pet_task_execution import is_generic_pet_task
                pet_question = bool(item) and is_generic_pet_task(c, item)
                from .school_open_reviews import is_school_review
                school_review = bool(item) and is_school_review(c, item)
                from .closed_review_execution import is_closed_review
                closed_review = bool(item) and is_closed_review(c,item)
            if item and (item["source_type"] in {"agent_retention", "junior_exam_goal", "competition_math", "oral_question_supply", "oral_delivered_continuation", "oral_daily_check"} or pet_writing or pet_question or school_review or closed_review):
                local_task = self._start_local_learning_task(step, item)
                if local_task is not None:
                    return local_task
            if not item:
                response = "今天没有等待开始的任务。"
                value = {"status": "no_task"}
            elif item["source_type"] == "planned_break":
                value = self.repository.begin_break(session_id, key)
            elif item["_start_operation"] == "resume":
                value = self.repository.resume_plan_item(item["plan_item_id"], key, authorized_turn_id=step["agent_turn_id"])
            else:
                value = self.repository.start_plan_item(item["plan_item_id"], key, authorized_turn_id=step["agent_turn_id"])
            if item and item["source_type"] == "agent_retention":
                retention = self._activate_retention_check(session_id, item["source_ref"])
                response = ("我们来继续核对上次的学习重点。" if retention["phase"] != "retention_check" else "我们来做上次学习目标的延迟复测。") + retention["prompt"]
            elif item and item["source_type"] == "planned_break":
                response = f"现在休息{int(item.get('expected_minutes') or 5)}分钟。到时间我会提醒你回来。"
            elif item:
                verb = "继续" if item["status"] == "paused" else "开始"
                response = self._task_start_response(value, verb)
        elif capability == "task.resume":
            with self.database.connect() as connection:
                control = AgentRuntimeStore.inspect_voice_control_execution(connection, step['agent_turn_id'])
            if not control or control['control'] != 'task.resume':
                raise AgentExecutionConflict('TASK_RESUME_BINDING_REQUIRED')
            if control.get('recorded_receipt'):
                return self._task_resume_replay(step)
            target = control['targets']['task_resume']['item']
            item_id = target['plan_item_id']
            if target['source_type'] == 'mistake_review':
                return self._start_mistake_review(step, target, resume=True)
            if target.get('operation') == 'supply':
                from .diagnostic_prompt import DiagnosticPromptService
                from .supply_gap_resume import materialize
                from .task_start_binding import TaskStartBindingError
                DiagnosticPromptService(self.database)
                with self.database.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    item = materialize(c, step['agent_turn_id'], target)
                    value = self._start_local_learning_task(step, {**item, '_start_operation':'start'}, connection=c)
                    if value['result']['status'] not in {'active','awaiting_resources'}:
                        raise TaskStartBindingError('SUPPLY_RESUME_SOURCE_CHANGED')
                    c.commit()
                return {**value, 'capability':capability}
            if target.get('operation') == 'start':
                item = self.repository._get_plan_item_by_id(item_id)
                value = self._start_local_learning_task(step, {**item, '_start_operation': 'start'})
                return {**value, 'capability': capability}
            value = self.repository.resume_plan_item(item_id, key, authorized_turn_id=step['agent_turn_id'])
            prompt = value.get('diagnostic_prompt') or {}
            response = '这项已恢复计时，原来的进度和题目保留。'
            remediation_reply = None
            expected_focus = control['targets']['task_resume'].get('foundation_remediation_focus')
            if expected_focus:
                from .task_start_binding import TaskStartBinding
                with self.database.connect() as c:
                    same_focus = TaskStartBinding.foundation_resume_focus(c, step['session_id'], step['plan_child_id'], target)
                if same_focus == expected_focus:
                    dialogue = MistakeDialogueService(self.database)
                    focus = dialogue.state(step['session_id'])
                    if focus and focus['state'].get('foundation_origin') == same_focus['origin']:
                        remediation_reply = dialogue.prompt(focus)
                        response += remediation_reply
            return {'capability': capability, 'response_text': response,
                    'result': value, 'evidence': ['task_resumed'],
                    'requires_child_reply': bool(remediation_reply) or prompt.get('status') == 'waiting_answer'}
        elif capability == "task.pause":
            with self.database.connect() as connection:
                control = AgentRuntimeStore.inspect_voice_control_execution(connection, step['agent_turn_id'])
            target_id = control['targets']['task']['plan_item_id'] if control else None
            item = next((item for item in workspace["plan_items"]
                         if (item['plan_item_id'] == target_id if control else item['status'] == 'active')), None)
            if not item:
                response = "现在没有正在计时的任务。你可以说开始任务，或者问我接下来做什么。"
                value = {"status": "no_active_task"}
            elif control and control.get('recorded_receipt'):
                value = {**item, 'recorded_receipt': control['recorded_receipt'], 'replayed': True}
                response = '刚才暂停任务的操作已保存；这次只核对记录，没有再次改变计时。'
            else:
                value = self.repository.pause_plan_item(item["plan_item_id"], key,
                    authorized_turn_id=step['agent_turn_id'])
                response = f"已经暂停{item['title']}，进度保留。准备好后说继续任务。"
        elif capability == "task.reschedule":
            with self.database.connect() as connection:
                connection.execute('BEGIN')
                gate = AgentRuntimeStore.inspect_voice_control_execution(connection, step['agent_turn_id'])
            if not gate or gate['control'] != 'task.reschedule':
                raise AgentExecutionConflict('TASK_DEFERRAL_TARGET_REQUIRED')
            item = gate['targets']['task_deferral']
            if not item:
                response = "现在没有可以延期的学习任务。"
                value = {"status": "no_task"}
            else:
                value = self.repository.reschedule_plan_item(
                    item["plan_item_id"], str(arguments.get("reason") or "孩子通过语音决定延期"), key,
                    **({'authorized_turn_id': step['agent_turn_id']} if gate else {})
                )
                refreshed = self.repository.get_workspace(session_id)
                next_item = self._next_unsettled_item(refreshed)
                response = f"已经把{item['title']}延期。{self._next_step_text(next_item)}"
        elif capability == "task.complete":
            item = next((item for item in workspace["plan_items"] if item["status"] == "active"), None)
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                from .writing_daily_followup import inspect_completion
                daily=inspect_completion(c,step['agent_turn_id'])
                if daily:
                    item=daily['item']
                    seconds=int(item.get('active_seconds') or 0)
                    if item['status']=='active':
                        seconds+=self.repository._segment_seconds(item['active_segment_started_at'],datetime.now(timezone.utc).isoformat())
                    value=self.repository.complete_plan_item(item['plan_item_id'],max(1,math.ceil(seconds/60)),
                        key,'voice_confirmed','孩子明确结束本次写作练习；原稿和反馈已保存，不代表独立掌握。',
                        connection=c,authorized_turn_id=step['agent_turn_id'])
                    c.commit()
            if daily:
                response='这次写作练习收好了，第二稿和刚才的反馈都会保留。之后复查时会接着这些记录继续。'
                if daily['event']:
                    return {'capability':'task.complete','result':value,'response_text':response,
                        'evidence':[],'requires_child_reply':False,'replayed':True,
                        'recorded_receipt':True,'replay_audio_allowed':False}
            elif not item:
                response = "现在没有正在计时的任务。先对我说开始任务。"
                value = {"status": "no_active_task"}
            elif item["source_type"] == "agent_retention":
                response = "这项复测需要你先回答刚才的问题，我会根据回答自动记录结果。"
                value = {"status": "waiting_for_retention_answer", "plan_item_id": item["plan_item_id"]}
            else:
                checkpoint = self.repository.checkpoint_plan_item(item["plan_item_id"])
                actual_minutes = max(1, math.ceil(int(checkpoint.get("active_seconds") or 0) / 60))
                evidence_type = str(arguments.get("completion_evidence_type") or action.get("completion_evidence_type") or "voice_confirmed")
                checks = []
                confirmation = None
                pending = AgentRuntimeStore(self.database).pending_task_completion_check(session_id, step["agent_turn_id"])
                if (evidence_type == "child_checked" or pending
                        or "confirmed_checks" in arguments or "revoked_checks" in arguments):
                    state, changed = self._merge_task_completion_checks(item, step, pending)
                    if state["missing_checks"] or not changed:
                        return self._request_task_completion_check(item, state)
                    evidence_type = "child_checked"
                    checks = state["confirmed_checks"]
                    confirmation = {"prompt_step_id": pending["step_id"] if pending else None,
                                    "response_turn_id": step["agent_turn_id"],
                                    "confirmed_checks": checks, "check_confirmations": state["check_confirmations"],
                                    "quote": step["transcript"], "evidence_type": "child_self_report"}
                note = (
                    "孩子通过语音明确确认已完成并检查任务要求"
                    if evidence_type == "child_checked"
                    else "孩子通过语音明确表示已完成但仍需订正"
                    if evidence_type == "needs_correction"
                    else "孩子通过语音确认完成"
                )
                from .repository import PlanInputError
                try:
                    value = self.repository.complete_plan_item(
                        item["plan_item_id"], actual_minutes, key, evidence_type,
                        note, checks, None,
                        authorized_turn_id=step['agent_turn_id'],
                    )
                except PlanInputError as error:
                    # Evidence gates are expected child-facing states, not an
                    # HTTP failure. Keep the task active and explain exactly
                    # what evidence is still needed through the same voice turn.
                    return {
                        "capability": "task.complete",
                        "response_text": str(error),
                        "result": {
                            "status": "blocked",
                            "reason": "task_completion_evidence_required",
                            "plan_item_id": item["plan_item_id"],
                            "local_only": True,
                            "cloud_transmission_allowed": False,
                        },
                        "evidence": [],
                        "requires_child_reply": True,
                    }
                if confirmation:
                    value["completion_confirmation"] = confirmation
                    value["task_started_at"] = item.get("started_at")
                refreshed = self.repository.get_workspace(session_id)
                unsettled = [
                    plan_item for plan_item in refreshed["plan_items"]
                    if plan_item["status"] in {"pending", "active", "paused"}
                ]
                if not unsettled:
                    response = (
                        "这项已经保存，今天安排的任务都处理完了。"
                        "准备好时说“开始复盘”，我们用三句话收好今天的学习。"
                    )
                else:
                    response = f"{item['title']}已经保存完成。{self._next_step_text(self._next_unsettled_item(refreshed))}"
        else:
            raise UnsupportedServerCapabilityError(capability)

        if capability in {"mistake.dialogue", "mistake.open"} and value.get("mistake_id"):
            if value.get('status') in {'blocked', 'conflict'}:
                # The case's learning stage is not the outcome of this command.
                return {'capability': capability, 'response_text': response, 'result': value,
                        'evidence': [], 'requires_child_reply': True}
            book = MistakeBookService(self.database)
            case = book.get(value["mistake_id"], value["child_id"])
            # Keep the existing review API projection while adding the richer
            # conversation state; old review consumers must not lose progress.
            value["status"] = case["status"]
            value["next_activity"] = book.next_activity(value["mistake_id"], value["child_id"])
            if value.get('plan_completion'):
                refreshed = self.repository.get_workspace(session_id)
                response += '本次复习任务已收好。' + self._next_step_text(self._next_unsettled_item(refreshed))
        return {
            "capability": capability,
            "response_text": response,
            "result": value,
            "evidence": list(json.loads(step["expected_evidence_json"])),
        }

    def _task_start_replay(self, step, recorded):
        # Recover only the prompt already bound to this committed start. Never
        # prepare a replacement or turn a historical receipt into timer admission.
        with self.database.connect() as c:
            row = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                            (step['agent_turn_id'],)).fetchone()
            binding = json.loads(row[0]).get('goal_prompt_binding')
            if binding is None:
                return recorded
            if (not isinstance(binding, dict) or binding.get('plan_item_id') != recorded['result']['plan_item_id']
                    or recorded['result']['status'] == 'cancelled'):
                raise AgentExecutionConflict('TASK_START_QUESTION_CHANGED')
            original = c.execute('''SELECT item_version FROM diagnostic_prompt
                WHERE prompt_id=? AND child_id=? AND origin_session_id=?''',
                (binding.get('prompt_id'), step['session_child_id'], step['session_id'])).fetchone()
            if not original or original['item_version'] != binding.get('item_version'):
                raise AgentExecutionConflict('TASK_START_QUESTION_CHANGED')
        from .diagnostic_prompt import DiagnosticPromptService
        prompt = DiagnosticPromptService(self.database).read(step['session_id'], prompt_id=binding['prompt_id'])
        if prompt['status'] in {'no_item', 'invalidated', 'cancelled'}:
            raise AgentExecutionConflict('TASK_START_QUESTION_CHANGED')
        return {**recorded, 'result': {**recorded['result'], 'diagnostic_prompt': prompt}}

    def _task_resume_replay(self, step):
        with self.database.connect() as connection:
            control = AgentRuntimeStore.inspect_voice_control_execution(connection, step['agent_turn_id'], allow_planned=True)
        if not control or control['control'] != 'task.resume' or not control.get('recorded_receipt'):
            raise AgentExecutionConflict('TASK_RESUME_RECEIPT_NOT_FOUND')
        target = control['targets']['task_resume']['item']
        from .legacy_listening_review import VERSION, mapped_target
        if target.get('version') == VERSION:
            with self.database.connect() as c:
                target = mapped_target(c, step['agent_turn_id'], target)
                manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                    (step['agent_turn_id'],)).fetchone()[0])
                binding = manifest.get('goal_prompt_binding') or {}
                if (binding.get('plan_item_id') != target['plan_item_id']
                        or control['recorded_receipt']['plan_item_id'] != target['plan_item_id']):
                    raise AgentExecutionConflict('TASK_RESUME_RECEIPT_NOT_FOUND')
                target = {**target, 'goal_prompt_binding': binding}
        item = self.repository._get_plan_item_by_id(target['plan_item_id'])
        if target.get('operation') in {'start','supply'}:
            from .diagnostic_prompt import DiagnosticPromptService
            binding = target['goal_prompt_binding']
            if target.get('operation') == 'supply':
                with self.database.connect() as c:
                    binding = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                                  (step['agent_turn_id'],)).fetchone()[0])['goal_prompt_binding']
            prompt = DiagnosticPromptService(self.database).read(step['session_id'], prompt_id=binding['prompt_id'])
            return {'capability': 'task.resume', 'response_text': '原任务启动记录已保存，本次没有再次启动计时。',
                    'result': {**item, 'diagnostic_prompt': prompt, 'replayed': True,
                               'recorded_receipt': control['recorded_receipt'], 'local_only': True, 'cloud_transmission_allowed': False},
                    'evidence': ['task_started'], 'requires_child_reply': item['status'] == 'active' and prompt['status'] == 'waiting_answer'}
        return {'capability': 'task.resume', 'response_text': '恢复记录已保存，本次没有重新启动计时。',
                'result': {**item, 'replayed': True, 'recorded_receipt': control['recorded_receipt'],
                           'local_only': True, 'cloud_transmission_allowed': False},
                'evidence': ['task_resumed'], 'requires_child_reply': False}

    def _recorded_mistake_review(self, step):
        with self.database.connect() as c:
            c.execute('BEGIN')
            row = c.execute('''SELECT e.event_id,e.event_type,e.payload_json,pi.*,dp.child_id,dp.session_id
                FROM plan_item_event e JOIN plan_item pi USING(plan_item_id) JOIN daily_plan dp USING(plan_id)
                WHERE e.client_action_id=? AND e.event_type IN ('started','resumed')
                    AND json_type(e.payload_json,'$.mistake_review_entry')='object' ''',
                (step['idempotency_key'],)).fetchone()
            if not row:
                return None
            receipt = json.loads(row['payload_json'])['mistake_review_entry']
            if (row['child_id'] != step['plan_child_id'] or row['session_id'] != step['session_id']
                    or row['source_type'] != 'mistake_review' or row['source_ref'] != receipt.get('mistake_id')
                    or receipt.get('agent_turn_id') != step['agent_turn_id']):
                raise AgentExecutionConflict('MISTAKE_REVIEW_RECEIPT_CHANGED')
            event = c.execute('''SELECT result_json FROM mistake_dialogue_event
                WHERE client_action_id=? AND child_id=? AND session_id=?''',
                (receipt.get('dialogue_action_id'), row['child_id'], row['session_id'])).fetchone()
            if not event or json.loads(event['result_json']).get('mistake_id') != row['source_ref']:
                raise AgentExecutionConflict('MISTAKE_REVIEW_RECEIPT_CHANGED')
        return {'capability': step['capability_name'],
            'response_text': '这次复习的原题和计时记录已保存，没有再次开始或切换题目。',
            'result': {'plan_item_id': row['plan_item_id'], 'status': row['status'],
                       'recorded_receipt': {'event_id': row['event_id'], 'event_type': row['event_type']},
                       'local_only': True, 'cloud_transmission_allowed': False},
            'replayed': True, 'recorded_receipt': True, 'replay_audio_allowed': False,
            'evidence': [], 'requires_child_reply': False}

    def _start_mistake_review(self, step, item, *, resume=False):
        from .diagnostic_prompt import _TransactionDatabase
        from .task_start_binding import TaskStartBinding, TaskStartBindingError
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = c.execute('''SELECT mc.mistake_id FROM mistake_case mc
                JOIN plan_item pi ON pi.source_ref=mc.mistake_id
                JOIN daily_plan dp USING(plan_id)
                WHERE pi.plan_item_id=? AND pi.source_type='mistake_review'
                    AND mc.mistake_id=? AND mc.child_id=? AND dp.child_id=mc.child_id
                    AND dp.session_id=? AND dp.status='active'
                    AND length(trim(mc.prompt_summary))>0
                    AND mc.status NOT IN ('excluded','mastered')''',
                (item['plan_item_id'], item['source_ref'], step['plan_child_id'], step['session_id'])).fetchone()
            if not current:
                raise TaskStartBindingError('MISTAKE_REVIEW_SOURCE_CHANGED')
            if resume:
                interactions = TaskStartBinding.interaction_focus(c, step['session_id'], step['plan_child_id'])
                prior = c.execute('SELECT mistake_id FROM mistake_dialogue WHERE session_id=? AND child_id=?',
                                  (step['session_id'], step['plan_child_id'])).fetchone()
                if interactions and (len(interactions) != 1 or interactions[0]['kind'] != 'mistake'
                                     or not prior or prior['mistake_id'] != current['mistake_id']):
                    raise TaskStartBindingError('MISTAKE_REVIEW_FOCUS_CHANGED')
            activate = self.repository.resume_plan_item if resume else self.repository.start_plan_item
            started = activate(item['plan_item_id'], step['idempotency_key'],
                authorized_turn_id=step['agent_turn_id'], connection=c)
            dialogue = MistakeDialogueService(_TransactionDatabase(c))
            focus = dialogue.open_review(step['session_id'], step['idempotency_key'] + ':review',
                                         mistake_id=current['mistake_id'])
            if not focus or focus['mistake_id'] != item['source_ref']:
                raise TaskStartBindingError('MISTAKE_REVIEW_FOCUS_MISSING')
            if focus['phase'] in {'closed', 'scheduled'}:
                raise TaskStartBindingError('MISTAKE_REVIEW_NOT_READY')
            from .mistake_listening_delivery import prepare as prepare_listening
            listening = prepare_listening(c, focus, item['plan_item_id'],
                step['idempotency_key'] + ':mistake-listening')
            if listening['status'] == 'awaiting_resources':
                c.rollback()
                return {'capability': step['capability_name'],
                    'response_text': '这道听力错题的已审核原音还没准备好。我先保留原任务，不开始计时，也不用文字原文替代听力。',
                    'result': {'status': 'awaiting_resources', 'reason': listening.get('reason'),
                        'plan_item_id': item['plan_item_id'], 'local_only': True,
                        'cloud_transmission_allowed': False, 'mastery_claim_allowed': False},
                    'evidence': [], 'requires_child_reply': False}
            receipt = {'mistake_id': focus['mistake_id'], 'agent_turn_id': step['agent_turn_id'],
                       'dialogue_action_id': step['idempotency_key'] + ':review'}
            receipt_write = c.execute('''UPDATE plan_item_event SET payload_json=json_set(payload_json,'$.mistake_review_entry',json(?))
                WHERE client_action_id=? AND plan_item_id=? AND event_type=?''',
                (json.dumps(receipt), step['idempotency_key'], item['plan_item_id'], 'resumed' if resume else 'started'))
            if receipt_write.rowcount != 1:
                raise TaskStartBindingError('MISTAKE_REVIEW_RECEIPT_MISSING')
            c.commit()
        return {'capability': step['capability_name'], 'response_text': focus['response_text'],
            'result': {**started, 'mistake_dialogue': focus,
                       **({'diagnostic_prompt': listening.get('prompt'),
                           'listening_resource': listening.get('resource')}
                          if listening['status'] == 'ready' else {}),
                       'local_only': True, 'cloud_transmission_allowed': False, 'mastery_claim_allowed': False},
            'evidence': ['plan_item_resumed' if resume else 'plan_item_started', 'original_mistake_bound'],
            'requires_child_reply': focus['phase'] not in {'closed', 'scheduled'}}

    def _prepare_active_mistake_listening(self, step, focus):
        """Bind audio after dialogue transitions without changing answer ownership."""
        activity = (focus.get('state') or {}).get('next_activity') or {}
        if (activity.get('item') or {}).get('answer_type') != 'listening_text':
            return None
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            plan = c.execute('''SELECT pi.plan_item_id FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active'
                  AND pi.status='active' AND pi.source_type='mistake_review' AND pi.source_ref=?''',
                (focus['session_id'], focus['child_id'], focus['mistake_id'])).fetchall()
            if len(plan) != 1:
                c.rollback()
                return None
            from .mistake_listening_delivery import prepare as prepare_listening
            result = prepare_listening(c, focus, plan[0]['plan_item_id'],
                step['idempotency_key'] + ':mistake-listening')
            c.commit()
            return result

    def _start_local_learning_task(self, step, item, *, connection=None):
        from contextlib import nullcontext
        session_id, key = step["session_id"], step["idempotency_key"]
        if connection is None and item['source_type'] in {'oral_question_supply','oral_delivered_continuation','oral_daily_check'}:
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                value=self._start_local_learning_task(step,item,connection=c)
                if value['result']['status']=='active':
                    c.commit()
                else:
                    c.rollback()
                return value
        if connection is None and item['source_type'] == 'due_review':
            from .legacy_listening_review import candidate, materialize
            from .diagnostic_prompt import DiagnosticPromptService
            DiagnosticPromptService(self.database)
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                current = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()
                from .closed_review_execution import is_closed_review
                if current and is_closed_review(c,current):
                    from .school_review_execution import validate_budget
                    from .school_open_reviews import SchoolReviewError
                    try:
                        validate_budget(c,session_id,step['plan_child_id'],item['plan_item_id'])
                    except SchoolReviewError as error:
                        c.rollback()
                        reason='今天剩余的学习时间或计划日期需要重新核对。这道复习题先保留，还没有出题或开始计时。'
                        return {'capability':'task.start','response_text':reason,
                            'result':{'status':'blocked','plan_item_id':item['plan_item_id'],
                                'reason_code':str(error),'reason':reason,'local_only':True,
                                'cloud_transmission_allowed':False,'mastery_claim_allowed':False},
                            'requires_child_reply':True}
                    value=self._start_local_learning_task(step,item,connection=c)
                    if value['result']['status']=='active':
                        c.commit()
                    else:
                        c.rollback()
                    return value
                if current and candidate(c, session_id, step['plan_child_id'], current):
                    replacement = materialize(c, step['agent_turn_id'], dict(current))
                    value = self._start_local_learning_task(step, replacement, connection=c)
                    if value['result']['status'] not in {'active', 'awaiting_resources'}:
                        from .task_start_binding import TaskStartBindingError
                        raise TaskStartBindingError('TASK_START_LEGACY_REVIEW_CHANGED')
                    c.commit()
                    return value
        def awaiting_resource(reason):
            from .task_source_recovery import listening_gap
            with self.database.connect() if connection is None else nullcontext(connection) as c:
                gap = listening_gap(c, step['plan_child_id'], session_id, item['plan_item_id'],
                                    prepared['prompt']['prompt_id'], prepared['prompt']['item_version'])
            return {'capability': step['capability_name'],
                'response_text': '这道题的原音还没有准备好，我先保留原题，不开始计时。请家长补齐并核对录音，再继续原任务。'
                    + ('也可以告诉我把这项延期，保留进度。' if gap else ''),
                'result': {'status': 'awaiting_resources', 'reason': reason,
                    'plan_item_id': item['plan_item_id'], 'diagnostic_prompt': prepared,
                    'task_resource_gap': gap, 'local_only': True, 'cloud_transmission_allowed': False},
                'evidence': [], 'requires_child_reply': False}

        pet = False
        school_review = False
        daily_started = None
        if item["source_type"] == "agent_retention":
            with self.database.connect() if connection is None else nullcontext(connection) as c:
                row = c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=? AND child_id=?',
                                (item['source_ref'], step['plan_child_id'])).fetchone()
                daily = c.execute("SELECT json_type(context_manifest_json,'$.daily_writing_binding') FROM agent_plan WHERE agent_turn_id=?",
                    (step['agent_turn_id'],)).fetchone()
            if row and json.loads(row[0]).get('origin')=='daily_learning_closure':
                if daily and daily[0]=='object':
                    from .writing_daily_followup import prepare_and_start
                    prepared,daily_started=prepare_and_start(self.database,self.repository,step['agent_turn_id'])
                else:
                    return None
            elif not row or json.loads(row[0]).get('origin') != 'local_learning_gate':
                return None
            else:
                prepared = AgentRuntimeStore(self.database).start_learning_recheck(session_id, item['source_ref'], key,
                    authorized_turn_id=step['agent_turn_id'])
        else:
            from .goal_task_prompt import prepare_goal_task_prompt
            with self.database.connect() if connection is None else nullcontext(connection) as c:
                stage = (item['source_type'] == 'competition_math' and item['task_type'] != 'goal_prerequisite') or self.repository._is_pet_writing_task(c, item)
                from .pet_task_execution import is_generic_pet_task
                pet = is_generic_pet_task(c, item)
                from .school_open_reviews import is_school_review
                school_review = is_school_review(c, item)
                from .closed_review_execution import is_closed_review
                closed_review = is_closed_review(c,item)
            prepared = prepare_goal_task_prompt(self.database, step['agent_turn_id'], session_id,
                item['plan_item_id'], item['_start_operation'], key, stage=stage, pet=pet, school_review=school_review, closed_review=closed_review,
                oral_supply=item['source_type'] in {'oral_question_supply','oral_delivered_continuation','oral_daily_check'}, connection=connection)
        if (prepared['status'] not in {'waiting_answer', 'answer_saved', 'answered'}
                and not ((pet or school_review) and prepared['status'] == 'awaiting_task_start')):
            reasons = {
                'not_due': '还没到约定的复测时间，今天不提前重复检查。',
                'distinct_local_question_unavailable': '暂时没有合格的新题，不能用原题重复作答冒充迁移。',
                'pending_prompt_exists': '先完成或取消正在回答的题目，再开始这次复测。',
                'knowledge_prerequisite_unverified': '我们先核对这道挑战需要的基础知识，再继续原来的目标；现在还没有开始计时。',
                'knowledge_prerequisite_binding_changed': '这项计划依据的基础知识记录有了变化，我们先核对安排，没有开始旧题的计时。',
            }
            detail = prepared.get('reason') if prepared.get('reason_code') else None
            fallback = detail if isinstance(detail, str) and detail.strip() else '原学习目标或题目依据已经变化，先核对当前计划，不继续旧复测。'
            reason = reasons.get(prepared.get('reason_code') or prepared.get('reason'), fallback)
            return {'capability': 'task.start', 'response_text': reason,
                    'result': {'status': 'blocked', 'reason': prepared.get('reason_code') or prepared.get('reason'),
                               'plan_item_id': item['plan_item_id'], 'local_only': True, 'cloud_transmission_allowed': False},
                    'evidence': [], 'requires_child_reply': False}
        if pet:
            from .pet_listening_admission import resolve_pet_listening_resource
            with self.database.connect() if connection is None else nullcontext(connection) as c:
                resource = resolve_pet_listening_resource(c, session_id, step['plan_child_id'],
                    prepared['prompt']['prompt_id'], prepared['prompt']['item_version'])
            if resource['status'] == 'awaiting_resources':
                return awaiting_resource(resource['reason'])
        operation = self.repository.resume_plan_item if item['_start_operation'] == 'resume' else self.repository.start_plan_item
        from .repository import PlanInputError
        from .pet_task_execution import PetListeningResourceError
        try:
            started = daily_started if daily_started is not None else operation(
                item['plan_item_id'], key, authorized_turn_id=step['agent_turn_id'],
                **({'connection':connection} if connection is not None else {}))
        except PlanInputError as error:
            if pet and isinstance(error.__cause__, PetListeningResourceError):
                return awaiting_resource(str(error.__cause__))
            raise
        if item['source_type'] in {'oral_question_supply','oral_delivered_continuation','oral_daily_check'}:
            if connection is None:
                raise AgentExecutionConflict('SUPPLY_RESUME_ORAL_TRANSACTION_REQUIRED')
            from .oral_supply_execution import delivered
            delivered(connection,step['agent_turn_id'],item['plan_item_id'],prepared['prompt'])
        if pet:
            from .diagnostic_prompt import DiagnosticPromptService
            if connection is None:
                prepared = DiagnosticPromptService(self.database).read(session_id, prompt_id=prepared['prompt']['prompt_id'])
            else:
                class TransactionPrompt(DiagnosticPromptService):
                    def __init__(self):
                        self.database = None
                    def _transaction(self):
                        return nullcontext(connection)
                prepared = TransactionPrompt().read(session_id, prompt_id=prepared['prompt']['prompt_id'])
        prompt = (prepared.get('prompt') or {}).get('item') or {}
        listening = prompt.get('pet_audio_modality') == 'listening' or prompt.get('answer_type') == 'listening_text'
        stimulus = None if listening else prompt.get('stimulus_text')
        response = ('我们来核对这项学习重点。' + '。'.join(filter(None, [stimulus, prompt.get('prompt')]))) \
            if prepared['status'] == 'waiting_answer' else '这道题的原回答已经保存，先核对原结果，不需要重复作答。'
        if item['source_type']=='oral_delivered_continuation' and prepared['status']=='waiting_answer':
            response='好，我们接着上次还没回答的这道题。'+'。'.join(filter(None,[stimulus,prompt.get('prompt')]))
        if listening and prepared['status'] == 'waiting_answer':
            response = '先播放这道原题的已审核录音，再回答问题；缺少原音时先补齐材料。'
        if daily_started is not None and prepared.get('replayed'):
            return {'capability':'task.start','response_text':'原题和开始记录已经保存，这次只核对记录。',
                'result':{**started,'diagnostic_prompt':prepared,'local_only':True,'cloud_transmission_allowed':False},
                'replayed':True,'recorded_receipt':True,'replay_audio_allowed':False,
                'evidence':[],'requires_child_reply':False}
        return {'capability': 'task.start', 'response_text': response,
                'result': {**started, 'diagnostic_prompt': prepared, 'local_only': True, 'cloud_transmission_allowed': False},
                'evidence': ['plan_item_started', 'diagnostic_prompt_bound'],
                'requires_child_reply': prepared['status'] == 'waiting_answer'}

    @staticmethod
    def _merge_task_completion_checks(item: dict[str, Any], step: dict[str, Any],
                                      pending: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
        required = list(COMPLETION_CHECKS_BY_TASK_TYPE.get(item["task_type"], ()))
        if (pending and (pending["plan_item_id"] != item["plan_item_id"]
                         or pending.get("task_started_at") != item.get("started_at")
                         or pending.get("required_checks") != required)):
            pending = None
        records = dict(pending.get("check_confirmations", {})) if pending else {}
        arguments = task_check_reply_arguments(step["arguments"], step["transcript"],
                                               step.get("orchestrator_version", ""), pending)
        confirmed, revoked = arguments.get("confirmed_checks", []), arguments.get("revoked_checks", [])
        quote = arguments.get("confirmation_quote", "")
        valid = (bool(required) and isinstance(quote, str) and bool(quote.strip())
                 and quote.strip() == step["transcript"].strip()
                 and all(isinstance(values, list) and len(values) <= 20
                         and all(isinstance(check, str) and check in required for check in values)
                         for values in (confirmed, revoked)))
        # A first-turn blanket model claim cannot manufacture a full checklist.
        # Partial reports are bound to the current task and each later reply.
        if valid and not pending and set(confirmed) - set(revoked) == set(required):
            valid = False
        changed = valid and bool(confirmed or revoked)
        if changed:
            for check in set(confirmed) | set(revoked):
                records[check] = {"confirmed": check not in revoked,
                                  "response_turn_id": step["agent_turn_id"], "quote": quote,
                                  "prompt_step_id": pending["step_id"] if pending else None,
                                  "evidence_type": "child_self_report"}
        confirmed_checks = [check for check in required if records.get(check, {}).get("confirmed") is True]
        return {"status": "awaiting_completion_check", "plan_item_id": item["plan_item_id"],
                "task_started_at": item.get("started_at"), "required_checks": required,
                "confirmed_checks": confirmed_checks,
                "missing_checks": [check for check in required if check not in confirmed_checks],
                "check_confirmations": records}, changed

    @staticmethod
    def _request_task_completion_check(item: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        labels = {
            "task_finished": "题目是否都做完", "missing_items_checked": "是否逐项检查过漏题",
            "recall_attempted": "是否尝试独立回忆", "uncertain_parts_marked": "是否标记了不确定的部分",
            "range_completed": "是否完成阅读范围", "main_idea_recalled": "是否回顾主要内容",
            "correction_finished": "是否完成订正", "error_cause_explained": "是否说明错因",
            "review_completed": "是否完成复习", "self_tested": "是否独立自测",
            "preview_completed": "是否完成预习", "question_recorded": "是否记下问题",
            "materials_reviewed": "是否复习指定材料", "uncertainty_marked": "是否标记不确定之处",
            "attempt_completed": "是否完成独立尝试", "reasoning_explained": "是否说明推理依据",
            "spoken_response_recorded": "是否完成口头作答", "task_points_covered": "是否覆盖任务要点",
            "writing_submitted": "是否完成写作并提交",
        }
        details = "；".join(labels.get(check, "是否完成这一项要求") for check in state["missing_checks"])
        prompt = f"关于{item['title']}，还需要确认：{details or '完成和核对情况'}。没完成或需要改正的地方也可以告诉我。"
        return {"capability": "task.complete", "response_text": prompt,
                "result": state,
                "evidence": ["completion_check_requested"], "requires_child_reply": True}

    @staticmethod
    def _exam_prompt(exam: dict[str, Any], attempt: dict[str, Any] | None = None) -> str:
        if exam.get('delivery_status')=='preparing':
            blockers={row.get('reason') for row in exam.get('delivery_blockers',[]) if isinstance(row,dict)}
            if blockers & {'EXAM_MEDIA_SOURCE_CHANGED','EXAM_MEDIA_SOURCE_GROUP_INCOMPLETE'}:
                return '这套卷的原题来源或完整题组已变化，考试计时已暂停，已保存原答保留。请家长核对来源；需要重开时先明确说“取消模拟卷”，再开始新卷。'
            if exam.get('started_at'):
                return '原音需要重新核验，计时已暂停，已用时间和原作答保留。请家长核对原音，核好后直接说“继续模拟卷”。'
            return '完整卷面已保留，正在准备和核验原音，考试还没有开始计时。请家长在原音审核页核对材料；准备好后直接说“继续模拟卷”。'
        if exam.get('delivery_status')=='ready':
            return '原音已核验，完整卷面已保留，考试还没有开始计时。准备开始时直接说“继续模拟卷”。'
        if exam.get('delivery_status')=='paused':
            return '模拟卷已暂停，原作答和已用时间保留。准备继续时直接说“继续模拟卷”。'
        if (exam.get("delivery_status") == "blocked"
                or ((exam.get("current_item") or {}).get("exam_audio_modality") == "listening"
                    and not exam.get("delivery_status"))):
            return "这套模拟卷的原音播放与作答核验还没有接通，暂时不能开始或继续。已保存的作答仍保留，请先安排其他学习任务。"
        if exam["status"] in {"completed", "timed_out", "cancelled"}:
            result = exam.get("result") or {}
            lead = "时间到，已自动收卷。" if exam["status"] == "timed_out" else "这次模拟卷已结束。"
            return f"{lead}你完成了{result.get('answered_items', 0)}/{result.get('total_items', exam['total_items'])}题。我会按能力域把错题和复测排回计划。"
        item = exam.get("current_item") or {}
        feedback = ""
        if attempt:
            feedback = "上一题已保存，本轮不立即透露标准答案。"
        subject = f"{item.get('subject')}学科，" if item.get("subject") and not exam.get("subject") else ""
        current_section = exam.get("current_section") or {}
        section_lead = ""
        if current_section:
            section_lead = (
                f"现在是{current_section.get('title') or current_section.get('section_id')}，"
                f"本卷剩余约{max(1, int(current_section.get('remaining_seconds') or 0) // 60)}分钟。"
            )
        blueprint = exam.get("blueprint") or {}
        blueprint_lead = ""
        if blueprint and exam.get("current_position") == 1 and not attempt:
            if blueprint.get("track") == "pet":
                blueprint_lead = "这套按当前PET四卷结构组织，分数只用于训练参考。"
            elif blueprint.get("track") == "junior_exam":
                blueprint_lead = "这套按南京2026考试框架组织，得分只作训练参考。"
            else:
                blueprint_lead = "这套按家庭数学思维十能力域组织。"
        return (
            f"{blueprint_lead}{feedback}{section_lead}{subject}第{exam['current_position']}题。"
            f"{item.get('stimulus_text') or ''}{item.get('prompt') or ''}"
        )

    def _activate_retention_check(self, session_id: str, commitment_id: str | None) -> dict[str, str]:
        if not commitment_id:
            raise UnsupportedServerCapabilityError("retention task has no commitment")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
            SELECT ac.commitment_id, ac.commitment_type, ac.objective_id, ac.payload_json, ac.status, ac.created_at,
                       ao.child_id
                FROM agent_commitment ac
                JOIN agent_objective ao ON ao.objective_id = ac.objective_id
                JOIN daily_session ds ON ds.child_id = ao.child_id
                WHERE ac.commitment_id = ? AND ds.session_id = ?
                  AND ac.status IN ('pending', 'due') AND ac.child_id=ao.child_id AND ac.due_at<=?
                  AND ao.status IN ('active','paused') AND ds.status='active' AND ds.state='study'
                """,
                (commitment_id, session_id, now),
            ).fetchone()
            if not row:
                connection.rollback()
                raise UnsupportedServerCapabilityError("retention commitment is unavailable")
            from .learning_outcome_projection import LearningOutcomeProjection
            if not LearningOutcomeProjection(self.database).commitment_available(connection, row):
                raise UnsupportedServerCapabilityError("learning follow-up source is unavailable or withdrawn")
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                connection.rollback()
                raise UnsupportedServerCapabilityError("retention commitment payload is invalid") from error
            prompt = str(payload.get("prompt") or "").strip()
            phase = str(payload.get("phase") or "retention_check")
            if phase not in {"check", "transfer_check", "retention_check"}:
                raise UnsupportedServerCapabilityError("unsupported learning follow-up phase")
            if not prompt:
                connection.rollback()
                raise UnsupportedServerCapabilityError("retention commitment has no check prompt")
            connection.execute(
                """
                UPDATE agent_objective SET status = 'paused', updated_at = ?
                WHERE child_id = ? AND status = 'active' AND objective_id != ?
                """,
                (now, row["child_id"], row["objective_id"]),
            )
            connection.execute(
                """
                UPDATE agent_objective SET status = 'active', phase = ?,
                       updated_at = ? WHERE objective_id = ?
                """,
                (phase, now, row["objective_id"]),
            )
            connection.execute(
                "UPDATE agent_commitment SET status = 'due' WHERE commitment_id = ?",
                (commitment_id,),
            )
            connection.commit()
        return {"prompt": prompt, "subject": str(payload.get("subject") or "复习"), "phase": phase}

    def _current_mistake(self, session_id: str) -> dict[str, Any] | None:
        return MistakeBookService(self.database).current_for_session(session_id)

    def _require_current_mistake(self, session_id: str) -> dict[str, Any]:
        current = self._current_mistake(session_id)
        if not current:
            raise UnsupportedServerCapabilityError("no actionable mistake for this session")
        return current

    @staticmethod
    def _remediation_step_type(current: dict[str, Any]) -> str:
        error_type = str((current.get("diagnosis") or {}).get("error_type") or "unknown")
        return {
            "concept_gap": "concept_explanation",
            "prerequisite_gap": "prerequisite_review",
            "reading_gap": "problem_restatement",
            "method_gap": "method_comparison",
            "execution_error": "execution_check",
            "retrieval_failure": "active_recall",
            "transfer_gap": "representation_change",
            "incidental": "execution_check",
            "unknown": "problem_restatement",
        }[error_type]

    @staticmethod
    def _mistake_activity_prompt(current: dict[str, Any] | None) -> str:
        if not current:
            return "现在没有等待处理或到期复测的错题。"
        activity = current.get("next_activity") or {}
        kind = activity.get("kind")
        if kind == "confirmation":
            return (
                f"我找到一条待评分作答，题目是：{current.get('prompt_summary', '')}。"
                f"我读到你当时写的是“{current.get('submitted_answer') or '还没有识别清楚'}”。"
                "请只告诉我题干和实际作答有没有读错；对错会由老师批改、对应版本的标准答案或出题时冻结的评分规则核对。"
            )
        if kind == "diagnosis":
            return "先不急着刷题。你从哪一步开始不确定，当时为什么会写出那个答案？"
        if kind == "remediation":
            return MistakeBookService.remediation_prompt(current)
        if kind == "attempt" and activity.get("item"):
            labels = {
                "repair_check": "关键步骤检查",
                "near_transfer": "近迁移",
                "representation_transfer": "换一种表示",
                "discrimination": "辨析",
                "far_transfer": "远迁移",
                "delayed_48h": "四十八小时复测",
                "delayed_7d": "七天复测",
            }
            label = labels.get(str(activity.get("stage")), "错题复测")
            if activity["item"].get("answer_type") == "listening_text":
                return f"接下来是{label}。我会先播放这道题已审核的原音频；听完后再回答，屏幕上不显示听力原文。"
            return f"接下来是{label}，请独立回答：{activity['item']['prompt']}"

        if kind == "scheduled":
            label = "四十八小时" if activity.get("stage") == "delayed_48h" else "七天"
            return f"眼前的迁移检查已通过，{label}复测还没到时间。我会到期后自动放进计划。"
        if kind == "blocked":
            return str(activity.get("reason") or "当前缺少经过验证的下一道题，我不会用重复题伪装成掌握。")
        if kind in {"preparing_variant", "awaiting_parent_review"}:
            return str(activity.get("reason") or "正在准备并核对新的变式题，完成后会自动进入后续计划。")
        if kind in {"mastered", "complete"} or current.get("status") == "mastered":
            return "这道错题已经通过即时迁移和延迟复测，当前记为稳定掌握。"
        return "这道错题的状态已经保存，我会按真实进度继续。"

    @staticmethod
    def _plan_summary(workspace: dict[str, Any]) -> str:
        items = [
            item for item in workspace.get("plan_items", [])
            if item.get("status") != "rescheduled"
        ]
        learning_items = [item for item in items if item.get("source_type") != "planned_break"]
        breaks = [item for item in items if item.get("source_type") == "planned_break"]
        total_minutes = sum(int(item.get("expected_minutes") or 0) for item in items)
        if not learning_items:
            return "今天暂时没有生成学习任务。你可以继续告诉我作业，或者说今天没有作业。"
        descriptions = [
            f"{item.get('subject') or '学习'}的{item.get('title') or '任务'}，{int(item.get('expected_minutes') or 0)}分钟"
            for item in learning_items[:5]
        ]
        if len(learning_items) > 5:
            descriptions.append(f"另外还有{len(learning_items) - 5}项")
        break_note = f"，中间安排{len(breaks)}次休息" if breaks else ""
        deferred = [
            item for item in workspace.get("constraint_decisions", [])
            if item.get("decision") in {"deferred", "suppressed"}
        ]
        deferred_note = ""
        if deferred:
            labels = [
                str((item.get("metadata") or {}).get("title") or item.get("obligation_type") or "一项任务")
                for item in deferred[:3]
            ]
            deferred_note = f"今天放不下的有：{'、'.join(labels)}；这些没有被取消，我会保留到后续计划。"
        return (
            f"计划已经排好，共{len(learning_items)}项学习任务，预计{total_minutes}分钟{break_note}。"
            f"顺序是：{'；'.join(descriptions)}。"
            f"{deferred_note}合适就说“确认计划”；想调整可以直接说把哪一项提前、缩短或稍后做。"
        )

    @staticmethod
    def _task_start_response(item: dict[str, Any], verb: str) -> str:
        minutes = int(item.get("expected_minutes") or 0)
        standard = str(item.get("completion_standard") or "完成后告诉我结果").strip()
        duration = f"，预计{minutes}分钟" if minutes > 0 else ""
        return f"现在{verb}{item.get('subject') or '学习'}的{item.get('title') or '任务'}{duration}。完成标准是：{standard}。"

    @classmethod
    def _plan_progress(cls, workspace: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        items = [
            item for item in workspace.get("plan_items", [])
            if item.get("source_type") != "planned_break" and item.get("status") != "rescheduled"
        ]
        completed = sum(item.get("status") == "completed" for item in items)
        active = next((item for item in items if item.get("status") == "active"), None)
        paused = next((item for item in items if item.get("status") == "paused"), None)
        pending = next((item for item in items if item.get("status") == "pending"), None)
        current = active or paused or pending
        value = {
            "total": len(items),
            "completed": completed,
            "remaining": max(0, len(items) - completed),
            "current_plan_item_id": current.get("plan_item_id") if current else None,
            "current_status": current.get("status") if current else None,
        }
        if not items:
            return "今天还没有学习计划。先告诉我课堂内容和作业，我会替你排好。", value
        if completed == len(items):
            return f"今天{len(items)}项学习任务都已经完成。现在可以开始复盘。", value
        if active:
            prefix = f"今天共有{len(items)}项，已完成{completed}项。"
            return prefix + cls._task_start_response(active, "正在进行"), value
        if paused:
            return (
                f"今天共有{len(items)}项，已完成{completed}项。"
                f"{paused['title']}现在暂停着，进度已经保留；准备好后说继续任务。"
            ), value
        return (
            f"今天共有{len(items)}项，已完成{completed}项。"
            f"下一项是{pending.get('subject') or '学习'}的{pending.get('title') or '任务'}，"
            f"预计{int(pending.get('expected_minutes') or 0)}分钟。说开始任务就可以开始。"
        ), value

    @staticmethod
    def _next_unsettled_item(workspace: dict[str, Any]) -> dict[str, Any] | None:
        items = workspace.get("plan_items", [])
        return next((item for item in items if item.get("status") == "active"), None) or next(
            (item for item in items if item.get("status") == "paused"), None
        ) or next((item for item in items if item.get("status") == "pending"), None)

    @staticmethod
    def _next_step_text(item: dict[str, Any] | None) -> str:
        if not item:
            return "今天安排的任务都处理完了，准备好时说开始复盘。"
        if item.get("source_type") == "planned_break":
            return f"下一步休息{int(item.get('expected_minutes') or 5)}分钟，说开始任务就会开始计时。"
        if item.get("status") == "paused":
            return f"接下来继续{item.get('title') or '刚才的任务'}，准备好后说继续任务。"
        return f"下一项是{item.get('subject') or '学习'}的{item.get('title') or '任务'}，准备好后说开始任务。"

    @staticmethod
    def _companion_activity_starter(activity_type: str, topic: str) -> str:
        safe_topic = topic.strip()[:40] or "神秘小镇"
        starters = {
            "interactive_story": (
                f"好，我们讲一个你能决定走向的原创故事，主题是{safe_topic}。"
                "第一幕：主角发现一扇只在雨后出现的小门。你想先观察门上的线索，还是先问身边的人？"
            ),
            "word_chain": "好，词语接龙开始，一轮八次。我先说“学习”，你要从“习”字开头接一个词。",
            "riddle": "好，最多猜五轮。第一题：有面没有口，有脚没有手，虽有四只脚，自己不会走。它是什么？",
            "movement": "好，五分钟离屏活动开始。先做二十次轻松跳绳或原地小跳，完成后告诉我身体感觉。注意周围留出空间。",
            "tidy_challenge": "好，五分钟桌面整理挑战开始。先只收一类东西，比如散开的笔或纸，完成后告诉我下一类是什么。",
        }
        if activity_type not in starters:
            raise UnsupportedServerCapabilityError("unsupported companion activity")
        return starters[activity_type]
