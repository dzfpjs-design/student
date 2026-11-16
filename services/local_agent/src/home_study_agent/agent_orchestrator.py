from __future__ import annotations

import json
import hashlib
import re
import asyncio
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .cloud_privacy import POLICY_VERSION as CLOUD_PRIVACY_POLICY_VERSION, sanitize_cloud_text, sensitive_categories
from .agent_runtime import (
    PLANNER_POLICY_VERSION,
    PLANNER_PROMPT_VERSION,
    PLANNER_VERSION,
    AgentPlanDecision,
    AgentRuntimeStore,
    CapabilityRegistry,
    CapabilitySpec,
    InvalidAgentPlanError,
    ObjectiveDraft,
    PlannedStep,
    action_from_plan,
    build_planner_messages,
    context_manifest as planner_context_manifest,
    parse_agent_plan_output,
)
from .child_safety import classify_child_safety, persist_safety_event
from .agent_continuation import ContinuationStore, RESULT_CONTINUATION_CAPABILITIES, written_item_identities
from .companion_dialogue import (
    POLICY_VERSION as COMPANION_POLICY_VERSION,
    PROMPT_VERSION as COMPANION_PROMPT_VERSION,
    build_companion_messages,
    enforce_companion_reply,
    safe_companion_fallback,
)
from .config import Settings
from .voice_controls import CAMERA_STOP_PHRASES, PRIVACY_STOP_PHRASES, SPEECH_STOP_PHRASES, is_direct_voice_control
from .voice_playback import VoicePlaybackGuard
from .companion_memory import CompanionMemory
from .learning_state import LearningStateService
from .database import Database
from .knowledge_graph.atomic_companion import AtomicGraphUnavailableError
from .model_gateway import ModelNotConfiguredError, ModelRequest, ModelResponseError
from .repository import UnknownSessionError, utc_now
from .repository import StudyRepository
from .spoken_question_intake import SpokenQuestionIntake
from .voice_intent import (
    CLOUD_INTENT_TIMEOUT_SECONDS,
    MAX_CLARIFICATION_TURNS_PER_STAGE,
    MIN_CLARIFY_CONFIDENCE,
    MIN_EXECUTE_CONFIDENCE,
    POLICY_VERSION as VOICE_POLICY_VERSION,
    PROMPT_VERSION as VOICE_PROMPT_VERSION,
    InvalidVoiceIntentOutputError,
    build_classifier_messages,
    clarification_limit_response,
    clarification_response,
    is_voice_intent_prompt_injection,
    parse_classifier_output,
)


REFLECTION_FINISH_PHRASES = ("结束今天学习", "保存复盘并结束今天学习", "保存复盘并结束", "今天结束")
REFLECTION_SKIP_PHRASES = ("跳过复盘", "不复盘了")

COMPANION_TIMEOUT_SECONDS = 8.0
AGENT_PLANNER_TIMEOUT_SECONDS = 30.0
MIN_AGENT_PLAN_CONFIDENCE = 0.62
AGENT_PLAN_REPAIR_PROMPT_VERSION = f"{PLANNER_PROMPT_VERSION}.repair1"
DOCUMENT_CONTROL_ACTIONS = {
    "request_capture", "retry_capture", "cancel_capture",
    "confirm_ocr_text", "correct_ocr_text",
}


def _repairable_agent_plan_structure(error: InvalidAgentPlanError) -> bool:
    """Allow one model-format repair before any plan is accepted or executed."""
    message = str(error).strip()
    if not message or message.upper() == message:
        return False
    return any(message.startswith(prefix) for prefix in (
        "planner did not return JSON",
        "planner output",
        "invalid objective",
        "active objective requires",
        "success_criteria",
        "new objective requires",
        "unknown capability",
        "capability ",
        "agent action",
        "unowned agent action",
        "unsupported capability argument",
        "missing capability arguments",
        "invalid numeric capability argument",
        "invalid learning response prompt",
        "invalid wake word arguments",
        "invalid device arguments",
        "invalid document guidance setting",
        "invalid completion",
        "invalid after_execution",
        "invalid continuation scope",
        "invalid continuation authorization",
        "spoken transition",
        "expected evidence",
        "input revision requires",
        "batch capability",
        "homework duration",
    ))

CAPTURE_QUESTION_MISMATCH_RESPONSE = '这段语音采集时的题目和当前待答题无法核对，我先不计作答案。请核对原题后重新回答。'
ANSWER_ROLE_CLARIFICATION_RESPONSE = '刚才这句我先不算作答案。你是在回答当前这道题、问题目里的意思，还是要处理另一件事？原题和这句话都留着。'
ANSWER_ROLE_RESTATEMENT_RESPONSE = '当前没有可确认的答语，我不会猜选刚才哪一句。请完整重述，可以说“我的答案是……”。'
ANSWER_ROLE_NOTICE_KEY = '_orchestrator_answer_role_notice_v1'

# These utterances belong to the conversation, even while an activity awaits
# an answer. All activity owners share this priority boundary.
CONVERSATION_CONTROL_INTENTS = {
    "repetition_confirmation",
    "audio_check", "camera_visibility_check", "confirmation_followup",
    "attention_check", "conversation_repair", "prepare_written_question",
    "speech_echo_ignored", "capture_readiness", "memory_command",
    "behavior_intervention_feedback", "habit_experiment_response",
    "urgent_safety_support", "high_risk_child_safety", "emotional_distress_support",
    "ordinary_study_pressure", "tutor_question", "correct_previous",
    "document_observation_question", "document_observation_response",
    "daily_learning_summary",
}


def is_deictic_visual_question_request(text: str) -> bool:
    """Return True when speech points at a question but contains no usable stem."""
    compact = re.sub(r"[\s，。！？!?、；;：:,.]", "", text).lower()
    if not compact:
        return False
    if any(phrase in compact for phrase in ("取消拍", "不拍了", "别拍", "不用拍", "不要拍")):
        return False
    if "高拍仪" in compact or any(
        phrase in compact for phrase in ("拍题", "拍错题", "拍一下错题", "拍一下题", "把题拍下来", "帮我看题")
    ):
        return True
    if "错题" in compact and any(
        phrase in compact for phrase in ("帮我看", "帮我看看", "看一下", "看看", "拍", "检查这", "检查一下")
    ):
        return True
    if "纸上" in compact and "题" in compact and any(
        phrase in compact for phrase in ("帮我解答", "帮我看看", "帮我看", "给我解答", "看一下")
    ):
        return True
    if not any(
        marker in compact
        for marker in (
            "这题", "这个题", "这道题", "这一题", "这一道题",
            "那题", "那个题", "那道题", "那一道题",
        )
    ):
        return False

    residual = compact
    fillers = (
        "可不可以", "能不能", "帮我看看", "帮我看一下", "帮忙看一下",
        "这个题", "这道题", "这一道题", "这一题", "这题",
        "那个题", "那道题", "那一道题", "那题",
        "我不太会", "我不会", "我不懂", "不太会", "不会", "不懂",
        "怎么求解", "应该怎么做", "该怎么做", "怎么做", "咋做",
        "帮我", "帮忙", "给我", "麻烦", "请你", "请",
        "看一下", "看看", "看下", "看", "拍一下", "拍下", "拍",
        "题目", "题", "一下", "可以吗", "行吗", "好吗", "呢", "吗", "我", "你",
    )
    for filler in fillers:
        residual = residual.replace(filler, "")
    # A dictated stem leaves concepts, quantities, or expressions behind.
    return len(residual) <= 2


class AgentActionPayloadConflictError(ValueError):
    """A client action id must represent one session and one normalized utterance."""


@dataclass(frozen=True)
class CloudIntentDecision:
    action: tuple[str, str, dict] | None = None
    companion_eligible: bool = False


@dataclass(frozen=True)
class CompanionReply:
    text: str
    source: str


class AgentOrchestrator:
    def __init__(
        self,
        database: Database,
        repository: StudyRepository,
        model_gateway: Any,
        settings: Settings,
        behavior_model: Any | None = None,
        atomic_companion: Any | None = None,
        pet_vocabulary: Any | None = None,
        exam_simulation: Any | None = None,
        document_learning: Any | None = None,
        page_source_store: Any | None = None,
    ):
        self.database = database
        self.repository = repository
        from .agent_request_ownership import OwnedModelGateway
        self.model_gateway = OwnedModelGateway(model_gateway)
        self.settings = settings
        self.behavior_model = behavior_model
        self.atomic_companion = atomic_companion
        self.local_courses = None
        self.pet_vocabulary = pet_vocabulary
        self.exam_simulation = exam_simulation
        self.document_learning = document_learning
        self.page_source_store = page_source_store
        self.capability_registry = CapabilityRegistry()
        self.agent_runtime = AgentRuntimeStore(database, page_source_store=page_source_store)
        self.companion_memory = CompanionMemory(database)
        self.learning_state = LearningStateService(database)
        self.voice_playback = VoicePlaybackGuard()
        self.spoken_question_intake = SpokenQuestionIntake(database)
        self.spoken_question_intake.initialize()
        from .learning_input_revision import LearningInputRevisionService
        self.learning_input_revisions = LearningInputRevisionService(database)
        self.learning_input_revisions.initialize()
        from .voice_control_intent import VoiceControlIntent
        self.voice_control_intent = VoiceControlIntent(database, repository, self.model_gateway, settings)
        self.voice_control_intent.initialize()

    async def interpret(
        self,
        session_id: str,
        transcript: str,
        client_action_id: str,
        cloud_text_allowed: bool,
        playback_overlap_id: str | None = None,
        voice_input_id: str | None = None,
        pending_evaluation_turn_id: str | None = None,
        local_recovery_only: bool = False,
        require_unsent_resume: bool = False,
    ) -> dict:
        from .pending_evaluation_control import PendingEvaluationControl, assert_request_mode
        from .local_voice_recovery import LocalVoiceRecovery, assert_request_mode as assert_recovery_mode
        if type(local_recovery_only) is not bool or local_recovery_only and pending_evaluation_turn_id is not None:
            raise InvalidAgentPlanError('LOCAL_VOICE_RECOVERY_MODE_CONFLICT')
        with self.database.connect() as c:
            assert_request_mode(c, session_id, client_action_id, pending_evaluation_turn_id)
            assert_recovery_mode(c,session_id,client_action_id,local_recovery_only)
            saved = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (client_action_id,)).fetchone()
            if saved:
                self._assert_action_payload(saved, session_id, transcript.strip())
                self._assert_replayed_voice(saved, voice_input_id)
            if self.behavior_model is not None:
                owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
                if owner:
                    feedback = self.behavior_model.feedback_for_action(f'voice:{client_action_id}', owner[0], session_id)
                    if feedback and feedback.get('payload_hash') != self._feedback_payload_hash(transcript.strip()):
                        raise AgentActionPayloadConflictError('Voice feedback action was reused with a different transcript.')
            if voice_input_id and not local_recovery_only:
                owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
                if not owner:
                    raise UnknownSessionError(session_id)
                saved = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?',(client_action_id,)).fetchone()
                if saved and pending_evaluation_turn_id is None:
                    self._assert_action_payload(saved,session_id,transcript.strip())
                    self._assert_replayed_voice(saved,voice_input_id)
                    capture = json.loads(saved['action_json']).get('voice_input_binding') or {}
                else:
                    capture = self.agent_runtime.resolve_captured_voice(c, session_id=session_id, child_id=owner[0],
                        voice_input_id=voice_input_id, transcript=transcript.strip(), page_source_store=self.page_source_store)
                if capture.get('pending_evaluation_turn_id') != pending_evaluation_turn_id:
                    raise InvalidAgentPlanError('PENDING_EVALUATION_CONTROL_CAPTURE_MISMATCH')
                if capture.get('local_recovery_only'):
                    raise InvalidAgentPlanError('LOCAL_VOICE_RECOVERY_MODE_CONFLICT')
        from .agent_request_ownership import (claim, execution_scope, permit_local_retry, terminal_rejection,
            LocalHandoffDispatchDenied, AgentRequestOwnershipError, TERMINAL_CODES)
        fresh = claim(self.database, session_id, client_action_id, transcript,
              voice_input_id=voice_input_id, playback_overlap_id=playback_overlap_id,
              pending_evaluation_turn_id=pending_evaluation_turn_id, local_recovery_only=local_recovery_only,
              allow_local_resume=True, require_unsent_resume=require_unsent_resume)
        rejected = terminal_rejection(self.database, session_id, client_action_id, voice_input_id)
        if rejected:
            raise InvalidAgentPlanError(rejected)
        with execution_scope(fresh) as authority:
            try:
                return await self._interpret_owned(session_id, transcript, client_action_id, cloud_text_allowed,
                    playback_overlap_id=playback_overlap_id, voice_input_id=voice_input_id,
                    pending_evaluation_turn_id=pending_evaluation_turn_id, local_recovery_only=local_recovery_only)
            except LocalHandoffDispatchDenied:
                raise AgentRequestOwnershipError('AGENT_HANDOFF_UNCONFIRMED') from None
            except Exception as error:
                if isinstance(error, InvalidAgentPlanError) and str(error) in TERMINAL_CODES:
                    terminal_rejection(self.database, session_id, client_action_id, voice_input_id,
                        record_code=str(error))
                    raise
                if authority.allow_external and not authority.external_started:
                    try:
                        permit_local_retry(self.database, session_id, client_action_id, voice_input_id)
                    except Exception:
                        # Failure to persist permission must keep the source fenced.
                        pass
                raise

    async def _interpret_owned(
        self, session_id: str, transcript: str, client_action_id: str, cloud_text_allowed: bool,
        playback_overlap_id: str | None = None, voice_input_id: str | None = None,
        pending_evaluation_turn_id: str | None = None, local_recovery_only: bool = False,
    ) -> dict:
        from .pending_evaluation_control import PendingEvaluationControl
        from .local_voice_recovery import LocalVoiceRecovery
        if local_recovery_only:
            return await LocalVoiceRecovery(self).interpret(session_id,transcript.strip(),client_action_id,
                                                           voice_input_id=voice_input_id)
        if pending_evaluation_turn_id is not None:
            return await PendingEvaluationControl(self).interpret(session_id, transcript.strip(), client_action_id,
                pending_evaluation_turn_id, voice_input_id=voice_input_id, playback_overlap_id=playback_overlap_id,
                cloud_text_allowed=cloud_text_allowed)
        received_transcript = transcript.strip()
        normalized_transcript = received_transcript
        speech_echo_removed = False
        speech_echo_removed_chars = 0
        planned_decision: AgentPlanDecision | None = None
        planned_context_manifest: dict[str, Any] | None = None
        active_exam = None
        active_vocabulary = None
        diagnostic_input_binding = None
        diagnostic_tutor_source = None
        diagnostic_local_source = None
        diagnostic_answer_type = None
        tutor_playback_binding = None
        exam_input_binding = None
        exam_run_binding = None
        companion_feedback_snapshot = None
        input_revision_arrival = None
        pending_input_field = None
        input_return_focus = None
        input_companion_context = None
        semantic_field = None
        field_sources = []
        unplaced_arrival = None
        pet_listening_arrival = None
        writing_followup = None
        oral_followup = None
        voice_capture = None
        activity_speech_pending = False
        current_text_authorized = cloud_text_allowed
        control_lane = None
        independent_document_dialogue = False
        foreground_dialogue_binding = None
        preserves_tutor_question = False
        control_targets = {}
        task_start_arrival = {'status':'blocked','reason':'arrival_unavailable','item':None,'items':[],
                              'local_only':True,'cloud_transmission_allowed':False}
        writing_arrival = None
        writing_reference = None
        writing_reference_sources = []
        cloud_planning_available = bool(
            cloud_text_allowed and self.settings.agent_runtime_enabled
            and getattr(self.model_gateway, "configured", False)
        )
        with self.database.connect() as connection:
            existing = connection.execute("SELECT agent_turn_id, session_id, transcript, action_json FROM agent_turn WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if existing:
                self._assert_action_payload(existing, session_id, received_transcript)
                self._assert_replayed_voice(existing, voice_input_id)
                return self._get_replayed(connection, existing["agent_turn_id"])
            session = connection.execute(
                """
                SELECT ds.child_id, ds.state, ds.status, fr.break_minutes
                FROM daily_session ds JOIN family_rule fr ON fr.child_id = ds.child_id
                WHERE ds.session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session:
                raise UnknownSessionError(session_id)
            if self._direct_controlled_stop(received_transcript) and session['status'] != 'active':
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            if voice_input_id is not None:
                voice_capture = self.agent_runtime.resolve_captured_voice(connection,
                    session_id=session_id, child_id=session['child_id'],
                    voice_input_id=voice_input_id, transcript=received_transcript, page_source_store=self.page_source_store)
                received_transcript = normalized_transcript = voice_capture['original_transcript']
                if self.agent_runtime.captured_question(voice_capture):
                    cloud_planning_available = cloud_text_allowed = False
            local_document_stop = None
            if session["status"] == "active":
                connection.execute('BEGIN IMMEDIATE')
                if self.behavior_model is not None:
                    feedback = self.behavior_model.feedback_for_action(
                        f'voice:{client_action_id}', session['child_id'], session_id)
                    if feedback and feedback.get('payload_hash') != self._feedback_payload_hash(received_transcript):
                        raise AgentActionPayloadConflictError('Voice feedback action was reused with a different transcript.')
                previous_arrival = connection.execute('SELECT 1 FROM learning_input_arrival WHERE client_action_id=?',
                                                      (client_action_id,)).fetchone()
                input_revision_arrival = self.learning_input_revisions.capture_arrival(
                    session_id, client_action_id, received_transcript, connection=connection)
                from .task_start_binding import TaskStartBinding
                task_start_arrival = TaskStartBinding.capture_start(connection,input_revision_arrival['arrival_id'],
                    is_new=previous_arrival is None,transcript=received_transcript,voice_capture=voice_capture)
                consent_version = self.voice_control_intent.text_consent_version(connection)
                unplaced_arrival = self.agent_runtime.capture_unplaced_arrival(connection, input_revision_arrival['arrival_id'])
                pet_listening_arrival = self.agent_runtime.capture_pet_listening_arrival(connection, input_revision_arrival['arrival_id'])
                writing_followup = self.agent_runtime.capture_writing_followup_arrival(connection, input_revision_arrival['arrival_id'], voice_capture)
                oral_followup = self.agent_runtime.capture_oral_followup_arrival(connection, input_revision_arrival['arrival_id'], voice_capture)
                from .writing_daily_followup import capture_arrival as capture_daily_writing
                capture_daily_writing(connection,input_revision_arrival['arrival_id'])
                if (self.voice_control_intent.direct_document_stop(received_transcript)
                        and not is_direct_voice_control(received_transcript, PRIVACY_STOP_PHRASES)):
                    monitor = connection.execute('SELECT monitor_id FROM document_monitor_session WHERE session_id=? AND child_id=?',
                        (session_id, session['child_id'])).fetchone()
                    local_document_stop = self.voice_control_intent.accept_document_stop(session_id, session['child_id'], client_action_id,
                        received_transcript, {'document_stop': {'monitor_id': monitor[0] if monitor else None}}, connection=connection)
                connection.commit()
            pre_dispatch = self._input_dispatch_guard(connection, session_id, session['child_id'],
                client_action_id, received_transcript, voice_capture,
                consent_version if input_revision_arrival else None)
            if local_document_stop:
                return self._persist_voice_control(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], local_document_stop, voice_capture)
            companion_feedback_snapshot = self.agent_runtime.snapshot_companion_activity_outcome(session_id)
            activity_speech_pending = bool((voice_capture or {}).get('activity_reference') or connection.execute(
                "SELECT 1 FROM companion_activity WHERE child_id=? AND session_id=? AND status='active'",
                (session['child_id'], session_id)).fetchone())
            self.agent_runtime.reconcile_learning_state(session["child_id"], connection=connection)
            connection.commit()
            if self.exam_simulation:
                exam_input_binding = self.exam_simulation.arrival_binding(session_id)
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='specialized_exam_run'").fetchone():
                    exam_run = connection.execute("""SELECT run_id FROM specialized_exam_run
                        WHERE session_id=? AND child_id=? AND status IN ('active','paused')
                        ORDER BY rowid DESC LIMIT 1""", (session_id, session["child_id"])).fetchone()
                    exam_run_binding = exam_run[0] if exam_run else None
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tutor_playback_checkpoint'").fetchone():
                playback = connection.execute("""SELECT playback_id,source_version FROM tutor_playback_checkpoint
                    WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1""", (session_id, session["child_id"])).fetchone()
                tutor_playback_binding = dict(playback) if playback else None
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
                bound = connection.execute(
                    """SELECT prompt_id,item_version,item_json,status FROM diagnostic_prompt WHERE origin_session_id=? AND child_id=?
                       AND owner_kind='diagnostic' AND json_extract(request_json,'$.exam_slot') IS NULL
                       AND status IN ('waiting_answer','answer_saved')
                       AND COALESCE(json_extract(request_json,'$.mistake_listening_delivery.version'),'')
                           != 'mistake_listening_delivery_v1'
                       ORDER BY rowid DESC LIMIT 1""",
                    (session_id, session["child_id"]),
                ).fetchone()
                diagnostic_input_binding = {key: bound[key] for key in ("prompt_id", "item_version")} if bound else None
                if bound and bound["status"] == "waiting_answer":
                    from .teaching_question_target import resolve_question_target
                    item = json.loads(bound["item_json"])
                    diagnostic_answer_type = item.get("answer_type")
                    target = resolve_question_target(connection, session_id, subject=item["subject"], question=item["prompt"],
                        prompt_id=bound["prompt_id"], item_version=bound["item_version"])
                    if target["status"] == "resolved":
                        diagnostic_tutor_source = {**diagnostic_input_binding, "question": item["prompt"], "subject": item["subject"]}
                        diagnostic_local_source = item['prompt']
                        from .local_supply import validate_item, LocalSupplyError
                        try:
                            original = validate_item(connection, session['child_id'], item['diagnostic_item_id'])
                        except (LocalSupplyError, ValueError, KeyError, TypeError):
                            original = None
                        # Only exact, immutable application-authored text is public
                        # here. Imported pages, answers and other local sources stay
                        # protected even when they overlap a spoken response.
                        if (original and original['source_ref'].startswith('hsc_original')
                                and original['prompt'] == item['prompt']):
                            diagnostic_local_source = None
            tasks = connection.execute('''SELECT pi.plan_item_id,pi.plan_id,dp.version FROM plan_item pi
                JOIN daily_plan dp USING(plan_id) WHERE dp.session_id=? AND dp.child_id=?
                AND dp.status='active' AND pi.status='active' ''', (session_id, session['child_id'])).fetchall()
            control_targets = {'exam_run_id': exam_run_binding,
                'task': dict(tasks[0]) if len(tasks) == 1 else None,
                'diagnostic': diagnostic_input_binding, 'tutor': tutor_playback_binding,
                'has_waiting_question': bool(diagnostic_input_binding),
                'companion_activity': companion_feedback_snapshot}
            from .tutor_check_response import capture_check_binding
            control_targets['tutor_check'] = capture_check_binding(connection,session_id,session['child_id'],voice_capture)
            document_local_source = None
            document_reference = self.agent_runtime.captured_question(voice_capture or {})
            if document_reference and document_reference['kind'] == 'document':
                diagnostic_tutor_source = None
                if voice_capture.get('question_validation') == 'valid':
                    control_targets['frozen_document'] = document_reference['binding']
                    source = connection.execute('SELECT extracted_text FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?',
                        (document_reference['binding']['artifact_id'],session['child_id'],session_id)).fetchone()
                    document_local_source = source[0] if source else None
                else:
                    current_text_authorized = False
            from .native_writing_control import snapshot as native_writing_snapshot
            native_writing_target=native_writing_snapshot(connection,session_id,session['child_id'])
            if native_writing_target:
                control_targets['native_writing']=native_writing_target
            activity_tasks = []
            for candidate in companion_feedback_snapshot.get('candidates', []):
                source = connection.execute('''SELECT source_snapshot_json FROM companion_activity_outcome
                    WHERE outcome_id=? AND child_id=? AND session_id=?''',
                    (candidate['binding']['outcome_id'], session['child_id'], session_id)).fetchone()
                if source:
                    original_task = json.loads(source[0]).get('paused_plan_item_id')
                    if original_task:
                        activity_tasks.append(original_task)
            control_targets['companion_paused_tasks'] = sorted(set(activity_tasks))
            from .task_start_binding import TaskStartBinding
            control_targets['task_resume'] = TaskStartBinding.resume_snapshot(connection, session_id, session['child_id'],
                self.agent_runtime.captured_question(voice_capture) if voice_capture else None)
            control_targets['task_start'] = task_start_arrival
            from .task_source_recovery import deferral_target as source_deferral_target
            source_deferral = source_deferral_target(connection, session['child_id'], session_id)
            if source_deferral:
                control_targets['task_deferral'] = source_deferral
            writing_arrival = self.agent_runtime.writing_voice_snapshot(
                connection, session_id, session['child_id'], voice_capture)
            writing_reference = self.agent_runtime.writing_reference_arrival(connection, session_id, session['child_id'])
            if writing_reference:
                ref = writing_reference['reference']
                self.agent_runtime.validate_writing_reference_execution(connection, writing_reference['turn_id'])
                writing_reference_sources = [self.agent_runtime.writing_reference_source(connection, session_id,
                    ref['prompt_id'], ref['item_version'])['response']]
            from .break_control import BreakControl
            from .repository import PlanInputError
            try:
                control_targets['break'] = BreakControl.snapshot(connection, session_id, session['child_id'])
            except PlanInputError:
                control_targets['break'] = None
            recent_rows = connection.execute(
                """
                SELECT agent_turn_id, transcript, intent, response_text, action_json, created_at FROM agent_turn
                WHERE session_id = ? AND tool_status = 'completed'
                ORDER BY completed_at DESC, created_at DESC LIMIT 12
                """,
                (session_id,),
            ).fetchall()
            local_memory_turns = set()
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='companion_memory_source'").fetchone():
                local_memory_turns = {row[0] for row in connection.execute(
                    "SELECT turn_id FROM companion_memory_source WHERE child_id=? AND turn_id IS NOT NULL", (session["child_id"],)
                )}
            cloud_history_rows = [row for row in recent_rows
                                  if row["intent"] != "memory_command"
                                  and row["agent_turn_id"] not in local_memory_turns
                                  and self._allows_cloud_history(row["action_json"])]
            recent_dialogue: list[dict[str, str]] = []
            for row in reversed(cloud_history_rows):
                if row["transcript"] and row["intent"] != "speech_echo_ignored":
                    recent_dialogue.append({"role": "user", "content": row["transcript"]})
                if row["response_text"]:
                    recent_dialogue.append({"role": "assistant", "content": row["response_text"]})
            previous_response = next((row["response_text"] for row in cloud_history_rows), None)
            normalized_transcript, speech_echo_removed_chars = self._strip_recent_assistant_echo(
                received_transcript,
                tuple(str(row["response_text"] or "") for row in recent_rows),
            )
            text_overlap_candidate = bool(playback_overlap_id and self.voice_playback.matches(
                session_id, playback_overlap_id, received_transcript
            )) or speech_echo_removed_chars > 0
            # Playback timing and matching text cannot distinguish a child
            # repeating aloud from loudspeaker echo. No acoustic classifier is
            # available here, so never strip or silently discard these words.
            normalized_transcript = received_transcript
            speech_echo_removed = False
            pure_speech_echo = False
            if normalized_transcript and not pure_speech_echo:
                self.agent_runtime.continuations.invalidate(session_id)
            if (pet_listening_arrival and not pure_speech_echo and not speech_echo_removed
                    and not classify_child_safety(received_transcript)):
                pet_choice = self.agent_runtime.resolve_pet_listening_voice(pet_listening_arrival, received_transcript)
                if pet_choice:
                    if (not playback_overlap_id and not text_overlap_candidate) or (pet_choice.get('arguments') or {}).get('command') in {'pause', 'cancel'}:
                        return self._persist_pet_listening_voice(session_id, session['child_id'], session['state'],
                            received_transcript, client_action_id, input_revision_arrival['arrival_id'], pet_choice, voice_capture=voice_capture)
                    text_overlap_candidate = True
            # A child who says "listen while I read the question" starts one
            # durable, multi-turn intake. Once active, each ASR final belongs to
            # that intake until confirmation or cancellation. This boundary runs
            # before generic overlap and cloud planning so a spoken stem can
            # never be reinterpreted as a document-camera command.
            if (input_revision_arrival and not pure_speech_echo
                    and not classify_child_safety(received_transcript)
                    and self.spoken_question_intake.should_route(
                        connection, session['child_id'], session_id, received_transcript
                    )):
                return self._persist_spoken_question_intake(
                    session_id=session_id,
                    child_id=session['child_id'],
                    state=session['state'],
                    transcript=received_transcript,
                    key=client_action_id,
                    arrival_id=input_revision_arrival['arrival_id'],
                    voice_capture=voice_capture,
                )
            if text_overlap_candidate and not classify_child_safety(received_transcript):
                _, _, overlap_action = self._classify(session['state'], received_transcript, session['break_minutes'],
                    previous_response, tuple(recent_dialogue))
                if overlap_action.get('type') not in {'stop_speech', 'pause_observation', 'cancel_capture'}:
                    return self._persist_overlap_clarification(session_id, session['child_id'], session['state'],
                        received_transcript, client_action_id, playback_overlap_id, voice_capture)
            local_stop_kind = self._direct_controlled_stop(received_transcript)
            if local_stop_kind and session['status'] == 'active':
                return self._persist_controlled_stop(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'],
                    local_stop_kind, voice_capture)
            if (unplaced_arrival and not pure_speech_echo and not speech_echo_removed
                    and not input_revision_arrival.get('pending') and not classify_child_safety(received_transcript)):
                unplaced_choice = self.agent_runtime.resolve_unplaced_voice(unplaced_arrival, received_transcript)
                if unplaced_choice:
                    return self._persist_unplaced_voice(session_id, session['child_id'], session['state'],
                        received_transcript, client_action_id, input_revision_arrival['arrival_id'], unplaced_choice, voice_capture=voice_capture)
            if not pure_speech_echo and normalized_transcript == received_transcript and companion_feedback_snapshot.get("selection"):
                selection_match = re.fullmatch(r"(?:我说的是|我是说|就是|是|选|选择)?第([一二两三四五六七八九十百零0-9]+)(?:个|项|次)(?:活动|那个)?[。！!，, ]*", received_transcript)
                if selection_match:
                    from .learning_input_revision import duration_minutes
                    index = duration_minutes(selection_match[1])
                    return self._persist_activity_selection(session_id, session["child_id"], session["state"],
                        received_transcript, client_action_id, companion_feedback_snapshot, index, voice_capture=voice_capture)
            if (not pure_speech_echo and normalized_transcript == received_transcript
                    and input_revision_arrival and input_revision_arrival.get("pending")):
                choice = self.learning_input_revisions.confirmation_choice(received_transcript)
                if choice and (choice == "cancel" or input_revision_arrival["pending"]["state"] == "awaiting_confirmation"):
                    return self._persist_input_revision_control(session_id, session["child_id"], session["state"],
                        received_transcript, client_action_id, input_revision_arrival, choice, voice_capture=voice_capture)
                from .learning_input_field_semantics import snapshot
                pending_input_field = snapshot(connection, input_revision_arrival['pending'])
                if not pending_input_field:
                    from .learning_input_intake import reply_requested
                    if reply_requested(connection, input_revision_arrival['pending'], received_transcript):
                        return self._persist_input_revision_control(session_id, session['child_id'], session['state'],
                            received_transcript, client_action_id, input_revision_arrival, 'supply', voice_capture=voice_capture)
            from .learning_input_return import snapshot as input_return_snapshot
            input_return_focus = input_return_snapshot(connection, session['child_id'], session_id)
            if input_return_focus:
                control_targets['input_return'] = input_return_focus
                from .input_companion_dialogue import context as companion_context
                input_companion_context = companion_context(connection, session['child_id'], session_id)
                if input_companion_context:
                    control_targets['input_companion'] = input_companion_context['focus']
            # A requested device action has not completed yet. Its local control
            # context must not depend on the completed conversational history.
            device_context_rows = connection.execute(
                "SELECT action_json,created_at FROM agent_turn WHERE session_id=? ORDER BY created_at DESC,rowid DESC LIMIT 12",
                (session_id,),
            ).fetchall()
            pending_document_capture = self._has_recent_pending_document_capture(device_context_rows)
            child_profiles = [dict(row) for row in connection.execute(
                "SELECT child_id, nickname, grade FROM child_profile WHERE status = 'active'"
            ).fetchall()]
            profile_switch = self._profile_switch_result(
                session["child_id"], normalized_transcript, child_profiles
            )
            behavior_feedback = None if pure_speech_echo or cloud_planning_available else self._record_pending_behavior_feedback(
                session["child_id"], session_id, normalized_transcript, client_action_id
            )
            if pure_speech_echo:
                intent, response, action = (
                    "speech_echo_ignored",
                    "这段声音与正在播放的内容重叠，来源尚不确定，暂不算作你的独立回答。",
                    {"type": "none", "source": "local_echo_guard", "suppress_speech": True,
                     "local_only": True, "cloud_transmission_allowed": False,
                     "independent_evidence": False, "mastery_update_allowed": False,
                     "playback_overlap_id": playback_overlap_id, "speech_source": "uncertain_playback_overlap"},
                )
            elif behavior_feedback:
                intent, response, action = behavior_feedback
            elif not cloud_planning_available and (habit_response := self._record_habit_experiment_response(
                session["child_id"], normalized_transcript, client_action_id
            )):
                intent, response, action = habit_response
            elif profile_switch:
                intent, response, action = profile_switch
            else:
                intent, response, action = self._classify(
                    session["state"], normalized_transcript, session["break_minutes"],
                    previous_response, tuple(recent_dialogue),
                )
                document_reply = self._continue_document_observation(
                    connection, session_id, session["child_id"], normalized_transcript, intent, action,
                )
                if document_reply:
                    intent, response, action = document_reply
                # A persisted active plan item is the authority for task completion.
                # The session state can briefly lag after diagnostic or break flows;
                # do not send an explicit completion statement to the cloud merely
                # because that coarse state is stale.
                completion = self._active_task_completion(normalized_transcript)
                if len(tasks) == 1 and completion:
                    intent, response, action = completion
                    action.update(
                        source="active_task_explicit_completion",
                        local_only=True,
                        cloud_transmission_allowed=False,
                    )
                heard, _ = self._speech_normalized_with_positions(normalized_transcript)
                if intent not in (CONVERSATION_CONTROL_INTENTS - {"tutor_question", "correct_previous"}) and len(heard) >= 12 and any(
                    len(self._speech_normalized_with_positions(str(row["response_text"] or ""))[0]) >= 12
                    and self._speech_normalized_with_positions(str(row["response_text"] or ""))[0] in heard
                    for row in recent_rows
                ):
                    intent, response, action = (
                        "repetition_confirmation", "你是在复述刚才这段话，还是想让我换一种方式讲？",
                        {"type": "none", "source": "local_repetition_clarification", "independent_evidence": False},
                    )
                if pending_document_capture and self._is_capture_readiness_acknowledgement(
                    normalized_transcript
                ):
                    intent, response, action = (
                        "capture_readiness",
                        "我正在看高拍仪画面，页面稳定后会自动拍摄和识别。",
                        {"type": "request_capture"},
                    )
                active_vocabulary = self.pet_vocabulary.active_prompt(session_id) if self.pet_vocabulary else None
                active_exam = self.exam_simulation.active_for_session(session_id) if self.exam_simulation else None
                if active_exam and active_exam["status"] == "paused":
                    # A paused paper keeps its saved position, not ownership of
                    # unrelated conversation. Explicit resume still routes normally.
                    active_exam = None
                exam_abstention = bool(re.fullmatch(
                    r"(?:这题|这一题)?(?:我)?(?:暂时|现在|还)?(?:不会|不知道|不清楚)(?:做)?[。！？?！ ]*",
                    normalized_transcript.strip(),
                ))
                if active_exam and exam_abstention and intent != "document_observation_response":
                    # The visible exam already owns the question. A declaration
                    # of not knowing is an answer, not a request to photograph it.
                    intent, response, action = "submit_exam_answer", "好，这题先记录为不会，继续下一题。", {
                        "type": "submit_exam_answer", "answer": normalized_transcript,
                    }
                if (
                    active_exam
                    and intent not in CONVERSATION_CONTROL_INTENTS
                    and not self._has_independent_capability(action, session["state"])
                ):
                    intent = "submit_exam_answer"
                    response = "收到，我保存这题的独立作答，再进入下一题。"
                    action = {"type": "submit_exam_answer", "answer": normalized_transcript}
                if (active_vocabulary and not active_exam and intent not in CONVERSATION_CONTROL_INTENTS
                        and not self._has_independent_capability(action, session["state"])):
                    intent = "submit_pet_vocabulary_answer"
                    response = "收到，我核对这张词汇卡并安排下一次复习。"
                    action = {"type": "submit_pet_vocabulary_answer", "answer": normalized_transcript}
            if session["state"] == "study" and not pure_speech_echo and not active_exam and not active_vocabulary and intent not in CONVERSATION_CONTROL_INTENTS:
                active_review = connection.execute("""SELECT rt.review_task_id FROM plan_item pi
                    JOIN daily_plan dp USING(plan_id) JOIN review_task rt ON rt.review_task_id=pi.source_ref
                    WHERE dp.session_id=? AND dp.child_id=? AND rt.child_id=dp.child_id
                      AND pi.source_type='due_review' AND pi.status='active' AND rt.status='pending' LIMIT 1""",
                    (session_id, session["child_id"])).fetchone()
                if active_review and action.get("type") == "none" and self._is_review_answer(normalized_transcript):
                    intent = "submit_diagnostic_answer"
                    response = "我先核对你这次的独立回答。"
                    action = {"type": "submit_diagnostic_answer", "answer": self._spoken_answer(normalized_transcript)}
            child_names = [profile["nickname"] for profile in child_profiles]
            clarification_count = self._clarification_count(
                connection, session_id, session["state"]
            )

        vocabulary_input_binding = None
        if active_vocabulary:
            candidate_binding = {key: active_vocabulary.get(key)
                                 for key in ('prompt_session_id', 'card_version')}
            if all(isinstance(value, str) and value.strip() for value in candidate_binding.values()):
                vocabulary_input_binding = candidate_binding

        if (voice_capture and not pure_speech_echo and not active_exam and not active_vocabulary
                and not classify_child_safety(received_transcript) and intent not in (CONVERSATION_CONTROL_INTENTS - {'tutor_question'})
                and action.get('type') not in {'stop_speech', 'pause_observation', 'cancel_capture',
                                             'confirm_ocr_text', 'correct_ocr_text'}):
            from . import document_review_schedule as review_schedule
            from . import document_revisit
            from .voice_control_intent import STATE_FIELDS
            with self.database.connect() as c:
                c.execute('BEGIN')
                revisit_focus = document_revisit.focus(c, session_id, session['child_id'], client_action_id)
                review_focus = review_schedule.focus(c, session_id, session['child_id'], client_action_id)
                if intent == 'tutor_question':
                    review_focus = None
                review_sources = []
                if review_focus:
                    saved_tutor = c.execute('SELECT question,answer FROM tutor_turn WHERE turn_id=?',
                        (review_focus['tutor_turn_id'],)).fetchone()
                    review_sources = list(saved_tutor) if saved_tutor else []
                if revisit_focus:
                    choices = revisit_focus.get('choices', [revisit_focus])
                    for choice in choices:
                        retained = document_revisit.retained(c, choice['commitment_id'], session['child_id'])
                        review_sources.extend([retained[4]['confirmed_text'], retained[5]['answer']])
                        from .document_check_followup import sources as check_sources
                        review_sources.extend(check_sources(c,retained))
                        hypothesis=retained[1].get('independent_target_hypothesis')
                        options=hypothesis.get('options') if isinstance(hypothesis,dict) else None
                        if isinstance(options,list):
                            review_sources.extend(option['title'] for option in options
                                if isinstance(option,dict) and isinstance(option.get('title'),str))
                    if revisit_focus.get('memory_resume'):
                        recalled = c.execute('SELECT response_text FROM agent_turn WHERE agent_turn_id=?',
                            (revisit_focus['memory_resume']['recall_turn_id'],)).fetchone()
                        if recalled:
                            review_sources.append(recalled['response_text'])
            if review_focus or revisit_focus:
                review_binding = ({'document_revisit':revisit_focus} if revisit_focus else {'document_review':review_focus})
                review_binding['review_clock'] = utc_now()
                review_state = {key:False for key in STATE_FIELDS}
                review_state.update(is_study_phase=session['state']=='study', is_break_phase=session['state']=='break')
                review_reply = await self.voice_control_intent.resolve(session_id=session_id, child_id=session['child_id'],
                    action_id=client_action_id, transcript=received_transcript, state=review_state, local_sources=review_sources,
                    names=child_names, allowed=current_text_authorized, binding=review_binding, voice_capture=voice_capture,
                    pre_dispatch=pre_dispatch)
                if review_reply.get('meaning') != 'other_request':
                    lane = document_revisit if revisit_focus else review_schedule
                    return lane.persist(self, session_id, session['child_id'], session['state'],
                        received_transcript, client_action_id, {**review_reply, 'binding':review_reply.get('binding',review_binding)}, voice_capture)

        from .writing_ocr_review import correction_words
        explicit_writing_correction = bool(writing_arrival and writing_arrival.get('pending_confirmation')
                                           and correction_words(received_transcript))
        if pending_input_field or input_return_focus:
            field_sources = [*writing_reference_sources, diagnostic_local_source,
                             (writing_arrival or {}).get('question'),
                             ((writing_arrival or {}).get('page') or {}).get('extracted_text'),
                             *(writing_arrival or {}).get('local_sources', [])]
            if control_targets.get('native_writing'):
                from .native_writing_control import source
                with self.database.connect() as c:
                    native_source = source(c, session_id, session['child_id'])
                if native_source:
                    field_sources.append(native_source[1])
        if (pending_input_field and not pure_speech_echo and not active_exam and not active_vocabulary
                and not classify_child_safety(received_transcript)
                and intent not in CONVERSATION_CONTROL_INTENTS
                and not explicit_writing_correction and action.get('type') != 'submit_diagnostic_answer'
                and action.get('type') not in {'stop_speech', 'pause_observation', 'cancel_capture'}):
            from .voice_control_intent import STATE_FIELDS
            field_state = {key: False for key in STATE_FIELDS}
            field_state.update(is_study_phase=session['state'] == 'study', is_break_phase=session['state'] == 'break')
            semantic_field = await self.voice_control_intent.resolve(session_id=session_id, child_id=session['child_id'],
                action_id=client_action_id, transcript=received_transcript, state=field_state, local_sources=field_sources, names=child_names,
                allowed=current_text_authorized, binding={'input_slot': pending_input_field}, voice_capture=voice_capture,
                pre_dispatch=pre_dispatch)
            protected_writing_review = bool((writing_arrival or {}).get('confirmation')
                and semantic_field.get('reason') == 'CONTROL_TEXT_LOCAL_ONLY')
            if semantic_field.get('meaning') != 'other_request' and not protected_writing_review:
                return self._persist_input_revision_control(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival, 'supply', voice_capture=voice_capture,
                    semantic_field_request=semantic_field.get('request_key'))

        if intent == "memory_command":
            response = self.companion_memory.handle_command(
                session["child_id"], session_id, normalized_transcript, client_action_id
            ) or "这次没有找到对应的记忆。你可以说得更具体一点。"
            action = {"type": "none", "source": "local_companion_memory", "local_only": True, "cloud_transmission_allowed": False}
            from .document_memory_resume import capture_context
            action['document_memory_resume'] = capture_context(
                self.companion_memory, session['child_id'], session_id, client_action_id, response)

        if intent == "daily_learning_summary":
            try:
                summary = await asyncio.to_thread(self.learning_state.daily_learning_summary, session["child_id"])
                response = summary["child_response"]
                action = {"type": "none", "source": "local_daily_learning_summary", "local_only": True,
                          "cloud_transmission_allowed": False, "mastery_update_allowed": False,
                          "learning_date": summary.get("learning_date")}
            except Exception:
                response = "今天的记录暂时没有整理出来，我不会凭印象猜。你的学习记录会保留，稍后可以再说“总结今天的学习”。"
                action = {"type": "none", "source": "local_daily_learning_summary", "local_only": True,
                          "cloud_transmission_allowed": False, "mastery_update_allowed": False,
                          "summary_status": "unavailable"}

        if intent == "document_observation_question":
            response, action = self._document_observation_reply(
                session_id, session["child_id"], self._document_observation_kind(normalized_transcript)
            )

        if (diagnostic_input_binding and self.settings.agent_runtime_enabled
                and not classify_child_safety(received_transcript)
                and is_direct_voice_control(received_transcript,('取消刚才的请求','这次不答了','这题先不答了'))):
            cancelled=self._persist_oral_cancel(session_id,session['child_id'],session['state'],received_transcript,
                client_action_id,input_revision_arrival['arrival_id'],diagnostic_input_binding,voice_capture)
            if cancelled is not None:
                return cancelled

        if self.settings.agent_runtime_enabled and not active_exam and not active_vocabulary:
            from .learning_question_help import snapshot as help_snapshot
            with self.database.connect() as c:
                c.execute('BEGIN')
                current_help = help_snapshot(c,session_id,session['child_id'])
            captured = self.agent_runtime.captured_question(voice_capture or {})
            if current_help and (not voice_capture or (captured and captured['kind'] in {'diagnostic','diagnostic_help'}
                    and (captured['kind'] != 'diagnostic_help' or voice_capture.get('question_validation') == 'valid')
                    and captured['binding']=={key:current_help[key] for key in ('prompt_id','item_version')})):
                control_targets['learning_help'] = current_help
                from .check_coaching_retry_control import snapshot as retry_snapshot
                with self.database.connect() as c:
                    c.execute('BEGIN')
                    retry = retry_snapshot(c, session_id, session['child_id'])
                if retry:
                    control_targets['check_coaching_retry'] = retry


        if (oral_followup and self.settings.agent_runtime_enabled
                and not classify_child_safety(received_transcript)
                and intent not in {'privacy_pause','switch_child_profile'}
                and action.get('type') not in {'stop_speech','pause_observation','cancel_capture'}):
            from .oral_teaching import project_oral_teaching
            with self.database.connect() as c:
                c.execute('BEGIN')
                view = project_oral_teaching(c,session['child_id'],session_id,**oral_followup['parent'])
            if semantic_field:
                field_sources.extend([view.get('question',''),view.get('response_text','')])
            semantic = await self.voice_control_intent.resolve(session_id=session_id,child_id=session['child_id'],
                action_id=client_action_id,transcript=received_transcript,allowed=current_text_authorized,names=child_names,
                local_sources=[*field_sources,view.get('question',''),view.get('response_text','')],
                binding={**control_targets,'oral_followups':oral_followup['packets']},voice_capture=voice_capture,
                pre_dispatch=pre_dispatch,state={
                    'has_pending_question':False,'has_active_exam':bool(active_exam),
                    'has_paused_exam':bool(exam_run_binding and not active_exam),'has_active_task':bool(control_targets['task']),
                    'has_tutor_playback':bool(tutor_playback_binding),'has_pet_listening':bool(pet_listening_arrival),
                    'is_study_phase':session['state']=='study','is_break_phase':session['state']=='break','has_oral_feedback':True})
            if semantic['status']=='control':
                return self._persist_voice_control(session_id,session['child_id'],session['state'],received_transcript,
                    client_action_id,input_revision_arrival['arrival_id'],semantic,voice_capture)

        if (writing_followup and self.settings.agent_runtime_enabled
                and not classify_child_safety(received_transcript)
                and intent not in {'privacy_pause', 'switch_child_profile'}
                and action.get('type') not in {'stop_speech','pause_observation','cancel_capture'}):
            from .writing_teaching import WritingTeachingService
            with self.database.connect() as c:
                local_view = WritingTeachingService(None).read(session_id, **writing_followup['parent'], connection=c)
            if semantic_field:
                field_sources.extend([local_view.get('question',''),local_view.get('response_text','')])
            semantic = await self.voice_control_intent.resolve(session_id=session_id, child_id=session['child_id'],
                action_id=client_action_id, transcript=received_transcript, allowed=current_text_authorized, names=child_names,
                local_sources=[*field_sources,local_view.get('question',''),local_view.get('response_text','')],
                binding={**control_targets,'writing_followups':writing_followup['packets']}, voice_capture=voice_capture, pre_dispatch=pre_dispatch, state={
                    'has_pending_question': False, 'has_active_exam': bool(active_exam),
                    'has_paused_exam': bool(exam_run_binding and not active_exam), 'has_active_task': bool(control_targets['task']),
                    'has_tutor_playback': bool(tutor_playback_binding), 'has_pet_listening': bool(pet_listening_arrival),
                    'is_study_phase': session['state'] == 'study', 'is_break_phase': session['state'] == 'break',
                    'has_writing_feedback': True})
            if semantic['status'] == 'control' and semantic['control'] in {'writing.revise_original','writing.independent_check'}:
                kind = 'revision' if semantic['control'] == 'writing.revise_original' else 'independent_check'
                packet = writing_followup['packets'][kind]
                snapshot = {'prompt':writing_followup['parent'],'page':None,'pending_confirmation':False,
                    'followup':packet, 'feedback_turn_id':writing_followup['feedback_turn_id']}
                return self._persist_writing_voice(session_id,session['child_id'],session['state'],received_transcript,
                    client_action_id,input_revision_arrival['arrival_id'],snapshot,semantic['control'],voice_capture,semantic=semantic)
            if semantic['status'] == 'control':
                return self._persist_voice_control(session_id,session['child_id'],session['state'],received_transcript,
                    client_action_id,input_revision_arrival['arrival_id'],semantic,voice_capture)

        if (writing_reference and not pure_speech_echo and not speech_echo_removed
                and not classify_child_safety(received_transcript)
                and intent not in {'privacy_pause', 'switch_child_profile'}):
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                current_reference = self.agent_runtime.writing_reference_arrival(c, session_id, session['child_id'])
                if current_reference != writing_reference:
                    raise InvalidAgentPlanError('ANSWER_WRITING_REFERENCE_CHANGED')
                source = writing_reference['reference']
                reference_turn = None if semantic_field else self.agent_runtime.prepare_writing_reference_feedback(c, session_id,
                    source['prompt_id'], source['item_version'], writing_reference['binding'], client_action_id,
                    previous=source, transcript=received_transcript)
                if reference_turn:
                    row = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (reference_turn,)).fetchone()
                    action = json.loads(row[0])
                    manifest = {}
                    self._freeze_local_voice(c, session_id, session['child_id'], received_transcript, voice_capture, action, manifest)
                    c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', (json.dumps(action, ensure_ascii=False), reference_turn))
                    c.commit()
                    return self._get(c, reference_turn)
            if action.get('type') not in {'stop_speech', 'pause_observation', 'cancel_capture'}:
                reference = writing_reference['reference']
                revision = {k:reference[k] for k in ('prompt_id','item_version','source_version')}
                revision['reference_turn_id'] = writing_reference['turn_id']
                control_lane = await self.voice_control_intent.resolve(session_id=session_id, child_id=session['child_id'],
                    action_id=client_action_id, transcript=received_transcript, allowed=current_text_authorized, names=child_names,
                    local_sources=[*field_sources,*writing_reference_sources], binding={**control_targets, 'writing_revision': revision}, voice_capture=voice_capture, pre_dispatch=pre_dispatch, state={
                        'has_pending_question': False, 'has_active_exam': bool(active_exam),
                        'has_paused_exam': bool(exam_run_binding and not active_exam), 'has_active_task': bool(control_targets['task']),
                        'has_tutor_playback': bool(tutor_playback_binding), 'has_pet_listening': bool(pet_listening_arrival),
                        'is_study_phase': session['state'] == 'study', 'is_break_phase': session['state'] == 'break',
                        'has_writing_reference': True})
                if control_lane['status'] == 'control':
                    if control_lane['control'] == 'writing.revise_original':
                        snap = {'prompt': {k:reference[k] for k in ('prompt_id','item_version')}, 'page': None,
                            'pending_confirmation': False, 'revision': revision}
                        return self._persist_writing_voice(session_id, session['child_id'], session['state'], received_transcript,
                            client_action_id, input_revision_arrival['arrival_id'], snap, 'writing.revise_original',
                            voice_capture, semantic=control_lane)
                    return self._persist_voice_control(session_id, session['child_id'], session['state'], received_transcript,
                        client_action_id, input_revision_arrival['arrival_id'], control_lane, voice_capture)

        if (writing_arrival and self.settings.agent_runtime_enabled and not pure_speech_echo
                and not speech_echo_removed and not classify_child_safety(received_transcript)
                and intent not in {'privacy_pause', 'switch_child_profile'}
                and action.get('type') not in {'stop_speech', 'pause_observation', 'cancel_capture'}):
            from .writing_ocr_review import correction_words
            if writing_arrival.get('pending_confirmation') and correction_words(received_transcript):
                return self._persist_writing_voice(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], writing_arrival,
                    'writing.correct_ocr', voice_capture)
            span = self.agent_runtime.writing_response_span(writing_arrival, received_transcript)
            if writing_arrival['scope_pending'] and span and not semantic_field:
                return self._persist_writing_voice(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], writing_arrival,
                    'writing.read_original', voice_capture, span=span)
            writing_lane = await self.voice_control_intent.resolve(
                session_id=session_id, child_id=session['child_id'], action_id=client_action_id,
                transcript=received_transcript, allowed=current_text_authorized, names=child_names,
                local_sources=[*field_sources,writing_arrival['question'], (writing_arrival.get('page') or {}).get('extracted_text'),
                               *writing_arrival['local_sources']],
                binding={**control_targets, 'writing_ocr': writing_arrival['confirmation']}, voice_capture=voice_capture, pre_dispatch=pre_dispatch, state={
                    'has_pending_question': True, 'has_active_exam': bool(active_exam),
                    'has_paused_exam': bool(exam_run_binding and not active_exam),
                    'has_active_task': bool(control_targets['task']), 'has_tutor_playback': bool(tutor_playback_binding),
                    'has_pet_listening': bool(pet_listening_arrival), 'is_study_phase': session['state'] == 'study',
                    'is_break_phase': session['state'] == 'break', 'has_writing_prompt': True,
                    'has_pending_writing_confirmation': writing_arrival['pending_confirmation']})
            if writing_lane['status'] == 'control':
                if writing_lane['control'].startswith('writing.'):
                    return self._persist_writing_voice(session_id, session['child_id'], session['state'],
                        received_transcript, client_action_id, input_revision_arrival['arrival_id'], writing_arrival,
                        writing_lane['control'], voice_capture, semantic=writing_lane)
                return self._persist_voice_control(session_id, session['child_id'], session['state'], received_transcript,
                    client_action_id, input_revision_arrival['arrival_id'], writing_lane, voice_capture)
            if writing_arrival['pending_confirmation'] or writing_arrival['scope_pending']:
                if writing_arrival.get('confirmation'):
                    from .writing_review_intent import resolve as resolve_writing_review
                    correction = await resolve_writing_review(self.voice_control_intent,
                        session_id=session_id,child_id=session['child_id'],action_id=client_action_id,
                        transcript=received_transcript,binding=writing_arrival['confirmation'],voice_capture=voice_capture,
                        names=child_names,local_sources=[*field_sources,writing_arrival['question'],
                            (writing_arrival.get('page') or {}).get('extracted_text'),*writing_arrival['local_sources']],allowed=current_text_authorized,
                        pre_dispatch=pre_dispatch)
                    if correction.get('control')=='writing.correct_ocr':
                        return self._persist_writing_voice(session_id,session['child_id'],session['state'],
                            received_transcript,client_action_id,input_revision_arrival['arrival_id'],writing_arrival,
                            'writing.correct_ocr',voice_capture,semantic=correction)
                if not semantic_field:
                    return self._persist_writing_voice(session_id, session['child_id'], session['state'],
                        received_transcript, client_action_id, input_revision_arrival['arrival_id'], writing_arrival,
                        None, voice_capture)

        if self.settings.agent_runtime_enabled and not pure_speech_echo and not speech_echo_removed:
            local_task_navigation = self.voice_control_intent.local_task_navigation(received_transcript)
            local_named_target = control_targets['task_start'] if local_task_navigation == 'named' else None
            local_named_owned = bool(local_named_target
                and local_named_target.get('reason') != 'requested_task_missing')
            # Content-free task controls keep their exact private arrival owner
            # even while a diagnostic question is waiting for an answer.
            if (session['state'] == 'study' and local_task_navigation in {'pause', 'current_continue', 'replay_current'}
                    and not active_exam and not exam_run_binding and not active_vocabulary
                    and not activity_speech_pending
                    and companion_feedback_snapshot.get('status') not in {'ready', 'ambiguous'}
                    and (local_task_navigation != 'current_continue' or not control_targets.get('task'))):
                navigation = self.voice_control_intent.accept_local_task_navigation(
                    session_id, session['child_id'], client_action_id, received_transcript, control_targets)
                if navigation:
                    return self._persist_voice_control(session_id, session['child_id'], session['state'],
                        received_transcript, client_action_id, input_revision_arrival['arrival_id'], navigation, voice_capture)
            if (session['state'] == 'study'
                    and (control_targets['task_start'].get('selection_offer') or local_named_owned)
                    and not active_exam and not active_vocabulary and not activity_speech_pending):
                choice = self.voice_control_intent.accept_local_task_navigation(
                    session_id, session['child_id'], client_action_id, received_transcript, control_targets)
                if choice:
                    return self._persist_voice_control(session_id, session['child_id'], session['state'],
                        received_transcript, client_action_id, input_revision_arrival['arrival_id'], choice, voice_capture)
            resume_choice = self.voice_control_intent.select_resume_choice(
                session_id, session['child_id'], client_action_id, received_transcript)
            if resume_choice:
                return self._persist_voice_control(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], resume_choice, voice_capture)

        # Read existing interaction ownership before generic offline navigation.
        from .mistake_dialogue import MistakeDialogueService
        mistake_focus = MistakeDialogueService(self.database).state(session_id)
        readonly_mistake_dialogue = bool((mistake_focus or {}).get('dialogue_binding')
            and self.agent_runtime.captured_question(voice_capture or {}) == {
                'kind': 'mistake', 'binding': mistake_focus['dialogue_binding']})
        from .mistake_source_revision import target as mistake_repair_target
        from .mistake_recapture import target as mistake_recapture_target
        with self.database.connect() as connection:
            source_repair_target = mistake_repair_target(connection, mistake_focus)
            source_recapture_target = mistake_recapture_target(connection, mistake_focus)
        explicit_mistake_repair = bool(source_repair_target and session['state'] == 'study'
            and mistake_focus['phase'] in {'attempt', 'diagnosis_probe'}
            and not active_exam and not active_vocabulary and not pure_speech_echo and not speech_echo_removed
            and action.get('type') not in {'stop_speech', 'pause_observation', 'cancel_capture'}
            and intent not in {'privacy_pause', 'switch_child_profile'}
            and re.fullmatch(r'(?:错题题干改成|题干改成|题目应该是)[，,:：\s]*(.{2,2800})', received_transcript.strip(), re.DOTALL))
        explicit_mistake_recapture = bool(source_recapture_target and session['state'] == 'study'
            and not active_exam and not active_vocabulary and not pure_speech_echo and not speech_echo_removed
            and re.fullmatch(r'(?:请|帮我)?(?:重新拍|重拍)(?:一下)?(?:题|这道题|这道错题|原题|原来的题)(?:吧)?[。！!\s]*', received_transcript.strip()))
        # A failed semantic request later never falls back into this route.
        if (self.settings.agent_runtime_enabled and session['state'] == 'study'
                and not (current_text_authorized and getattr(self.model_gateway, 'configured', False))
                and not pure_speech_echo and not speech_echo_removed
                and not active_exam and not active_vocabulary and not activity_speech_pending
                and action.get('type') in {'none','start_next_task','describe_plan'}
                and companion_feedback_snapshot.get('status') not in {'ready','ambiguous'}):
            navigation = self.voice_control_intent.accept_local_task_navigation(
                session_id,session['child_id'],client_action_id,received_transcript,control_targets)
            if navigation:
                return self._persist_voice_control(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], navigation, voice_capture)

        # An actual question awaiting a reply outranks stateless short-utterance
        # classification. Safety, navigation and explicit new tasks still win.
        resume_mistake = bool(re.search(r"继续.*(?:错题|订正|刚才的题|整理)|复习错题|开始复习|下一道错题|记得.*(?:错题|这题)|(?:昨天|上次|刚才).*(?:错题|题目|卡在)", normalized_transcript))
        from .mistake_question_confirmation import relative_teaching_request, relative_diagnosis_reply
        relative_mistake_help = bool(mistake_focus
            and mistake_focus['phase'] in {'diagnosis', 'diagnosis_probe', 'remediation', 'attempt'}
            and (relative_teaching_request(received_transcript)
                 or mistake_focus['phase'] == 'diagnosis' and relative_diagnosis_reply(received_transcript))
            and action.get('type') in {'ask_tutor', 'none'}
            and not pure_speech_echo and not speech_echo_removed)
        if readonly_mistake_dialogue and current_text_authorized and getattr(self.model_gateway, 'configured', False):
            # Resolve current versus independent meaning before borrowing the
            # original dialogue. A read-only reference does not authorize an answer.
            relative_mistake_help = False
        foundation_hold_reply = bool(mistake_focus
            and (mistake_focus.get('state') or {}).get('foundation_hold')
            and self._local_answer_has_shape(received_transcript)
            and not pure_speech_echo and not speech_echo_removed)
        if (session["state"] == "study" and mistake_focus and not active_exam and not active_vocabulary
            and (relative_mistake_help or foundation_hold_reply or intent not in CONVERSATION_CONTROL_INTENTS)
            and (relative_mistake_help or foundation_hold_reply
                 or not self._has_independent_capability(action, session["state"]))
            and (foundation_hold_reply
                 or mistake_focus["phase"] not in {"paused", "closed", "scheduled"}
                 or resume_mistake)):
            intent, response, action = "mistake_dialogue", "我们接着刚才这道题。", {
                "type": "continue_mistake_dialogue", "utterance": normalized_transcript,
            }
        from .mistake_pending_answer import explicit_replacement, explicit_withdraw
        pending_replacement = explicit_replacement(received_transcript)
        if (session['state'] == 'study' and not active_exam and not active_vocabulary
                and not pure_speech_echo and not speech_echo_removed
                and (mistake_focus or {}).get('state', {}).get('pending_operation')
                and pending_replacement is not None):
            intent, response, action = 'mistake_dialogue', '我核对后停用旧答案，按你刚说的新答案继续。', {
                'type': 'continue_mistake_dialogue', 'utterance': received_transcript,
                'pending_answer_control': 'replace', 'replacement_answer': pending_replacement,
                'local_only': True, 'cloud_transmission_allowed': False}
        if (session['state'] == 'study' and not active_exam and not active_vocabulary
                and not pure_speech_echo and not speech_echo_removed
                and not (mistake_focus or {}).get('state', {}).get('pending_operation')
                and (mistake_focus or {}).get('state', {}).get('last_scored_answer')
                and pending_replacement is not None):
            intent, response, action = 'mistake_dialogue', '我核对刚才已评分的回答，并重算相关学习记录。', {
                'type': 'continue_mistake_dialogue', 'utterance': received_transcript,
                'scored_answer_control': 'replace', 'replacement_answer': pending_replacement,
                'local_only': True, 'cloud_transmission_allowed': False}
        from .mistake_scored_correction import explicit_withdrawal as explicit_scored_withdrawal
        if (session['state'] == 'study' and not active_exam and not active_vocabulary
                and not pure_speech_echo and not speech_echo_removed
                and not (mistake_focus or {}).get('state', {}).get('pending_operation')
                and (mistake_focus or {}).get('state', {}).get('last_scored_answer')
                and explicit_scored_withdrawal(received_transcript)):
            intent, response, action = 'mistake_dialogue', '我核对后撤回刚才答案的评分效力，原记录仍会保留。', {
                'type': 'continue_mistake_dialogue', 'utterance': received_transcript,
                'scored_answer_control': 'withdraw',
                'local_only': True, 'cloud_transmission_allowed': False}
        from .mistake_cross_session_control import request as historical_scored_request
        historical_scored = historical_scored_request(received_transcript)
        if (session['state'] == 'study' and historical_scored
                and not active_exam and not active_vocabulary
                and not pure_speech_echo and not speech_echo_removed
                and not (mistake_focus or {}).get('state', {}).get('pending_operation')):
            intent, response, action = 'mistake_dialogue', '我先在本机找到你指的历史题目，读回后再执行。', {
                'type': 'continue_mistake_dialogue', 'utterance': received_transcript,
                'historical_scored_control': historical_scored['control'],
                'local_only': True, 'cloud_transmission_allowed': False}
        if (session['state'] == 'study' and not active_exam and not active_vocabulary
                and not pure_speech_echo and not speech_echo_removed
                and (mistake_focus or {}).get('state', {}).get('pending_operation')
                and explicit_withdraw(received_transcript)):
            intent, response, action = 'mistake_dialogue', '我核对后撤回这份待处理回答，保留原记录。', {
                'type': 'continue_mistake_dialogue', 'utterance': received_transcript,
                'pending_answer_control': 'withdraw', 'local_only': True, 'cloud_transmission_allowed': False}
        if action.get('type') == 'submit_mistake_answer':
            intent, response, action = self._canonical_mistake_submission(
                received_transcript, mistake_focus, voice_capture, transformed=bool(speech_echo_removed))
        if (session['state'] == 'study' and not active_exam and not active_vocabulary
                and not pure_speech_echo and not speech_echo_removed
                and intent not in {'privacy_pause', 'switch_child_profile'}
                and intent not in (CONVERSATION_CONTROL_INTENTS - {'conversation_repair', 'tutor_question', 'correct_previous'})
                and action.get('type') not in {'stop_speech', 'pause_observation', 'cancel_capture'}
                and (mistake_focus or {}).get('state', {}).get('pending_operation')
                and re.search(r'(?:为什么|怎么).*(?:没(?:有)?保存|没存|处理失败|没处理)', received_transcript)):
            intent, response, action = 'mistake_dialogue', '我核对一下已保存回答的处理状态。', {
                'type': 'continue_mistake_dialogue', 'utterance': received_transcript,
                'local_only': True, 'cloud_transmission_allowed': False}
        if explicit_mistake_repair:
            quote = re.fullmatch(r'(?:错题题干改成|题干改成|题目应该是)[，,:：\s]*(.{2,2800})',
                                 received_transcript.strip(), re.DOTALL)[1]
            intent, response, action = 'mistake_source_repair', '我先读回更正内容，请你核对。', {
                'type': 'repair_mistake_question', 'question_quote': quote,
                'local_only': True, 'cloud_transmission_allowed': False}
        if explicit_mistake_recapture:
            intent, response, action = 'retry_capture', '我重新拍这道错题，先保留原答案，拍完再核对题干。', {'type': 'retry_capture'}

        # An explicit choice of a saved prompt is not a new answer or a cloud
        # side effect. Keep its exact target even when intent classification fails.
        focus_request = re.fullmatch(
            r"(?:我(?:想|要)?|我们)?(?:先|接着|继续)?(?:回答|做|复习|继续)(?:刚才那张|那张|这张|当前的)?"
            r"(词汇卡|单词卡|诊断题|诊断|模拟卷)(?:吧)?[。！! ]*", received_transcript.strip())
        selected_prompt = False
        saved_review_selection = None
        if focus_request and not pure_speech_echo and not speech_echo_removed:
            kind = focus_request[1]
            focus_action = ("start_pet_vocabulary_review" if kind in {"词汇卡", "单词卡"} and vocabulary_input_binding
                            else "replay_diagnostic" if kind in {"诊断题", "诊断"} and diagnostic_answer_type
                            else "resume_exam_simulation" if kind == "模拟卷" and exam_run_binding else None)
            if focus_action:
                selected_prompt = True
                intent, response, action = ("select_saved_prompt", "好，先回到你选的这项练习，其他待答内容保留。",
                    {"type": focus_action, "source": "saved_prompt_selection", "local_only": True, "cloud_transmission_allowed": False})
                if focus_action == 'replay_diagnostic':
                    action.update(diagnostic_prompt_id=diagnostic_input_binding['prompt_id'],
                                  diagnostic_item_version=diagnostic_input_binding['item_version'])
                elif focus_action == 'start_pet_vocabulary_review':
                    action.update(vocabulary_prompt_id=vocabulary_input_binding['prompt_session_id'],
                                  vocabulary_card_version=vocabulary_input_binding['card_version'])
                else:
                    action['exam_run_id'] = exam_run_binding
                cloud_planning_available = cloud_text_allowed = False
        if (not selected_prompt and session['state'] == 'study' and mistake_focus
                and not active_exam and not active_vocabulary and not activity_speech_pending
                and not pure_speech_echo and not speech_echo_removed
                and re.fullmatch(r'(?:打开错题本|查看错题本|进入错题本|看看我的错题)[。！! ]*', received_transcript.strip())):
            from .foundation_remediation import review_handoff
            with self.database.connect() as connection:
                handoff = review_handoff(connection, mistake_focus)
            if handoff:
                selected_prompt = True
                saved_review_selection = {'mistake_id': mistake_focus['mistake_id'], 'handoff': handoff}
                intent, response, action = ('select_saved_review', '好，接着今天已经安排的这道错题复习，原基础任务的记录保留。',
                    {'type': 'open_mistake_book', 'source': 'saved_review_selection',
                     'local_only': True, 'cloud_transmission_allowed': False})
                cloud_planning_available = cloud_text_allowed = False
        local_question = bool(self.agent_runtime.captured_question(voice_capture or {})
                              or diagnostic_input_binding or (mistake_focus or {}).get('answer_binding'))
        # An exact new-paper request already has a registered deterministic
        # owner. A retained task-resume context must not send it to a control
        # catalog which only knows pause/resume/cancel for existing papers.
        exam_start_words = re.fullmatch(
            r'(?:请)?(?:开始|做一套|来一套)(?:PET|竞赛数学|奥数|数学|英语|语文|物理|化学|生物|历史|地理|道德与法治)?'
            r'模拟卷(?:吧)?[。！! ]*', received_transcript.strip(), re.IGNORECASE)
        explicit_exam_start = bool(exam_start_words and session['state']=='study'
            and action.get('type')=='start_exam_simulation' and not exam_run_binding
            and not active_vocabulary and not activity_speech_pending
            and not pure_speech_echo and not speech_echo_removed
            and companion_feedback_snapshot.get('status') not in {'ready','ambiguous'})
        if explicit_exam_start:
            cloud_planning_available = cloud_text_allowed = False
            action.update(source='explicit_local_exam_start',local_only=True,cloud_transmission_allowed=False)
            if local_question or pending_input_field or control_targets.get('tutor_check'):
                intent, response, action = ('exam_start_original_pending',
                    '原题和已保存答案都保留着。请先完成原题，或明确告诉我取消原题，再开始整卷。',
                    {'type':'none','source':'pending_question_conflict','local_only':True,'cloud_transmission_allowed':False})
        background_document = bool(
            (self.agent_runtime.captured_question(voice_capture or {}) or {}).get('kind') == 'document'
            and control_targets.get('frozen_document') and not diagnostic_input_binding
            and not active_exam and not active_vocabulary and not mistake_focus
            and not input_return_focus and not activity_speech_pending
            and not control_targets.get('native_writing')
            and not tutor_playback_binding and not control_targets.get('pending_evaluation')
            and not self.agent_runtime.pending_child_reply(session_id))
        foreground_question = bool(diagnostic_input_binding
            and self.agent_runtime.captured_question(voice_capture or {}) ==
                {'kind': 'diagnostic', 'binding': diagnostic_input_binding}
            and not active_exam and not active_vocabulary and not mistake_focus
            and not input_return_focus and not activity_speech_pending
            and not control_targets.get('native_writing') and not tutor_playback_binding
            and not control_targets.get('pending_evaluation'))
        foreground_vocabulary = (vocabulary_input_binding
            if active_vocabulary and not active_exam and not diagnostic_input_binding and not mistake_focus
            and not input_return_focus and not activity_speech_pending and not control_targets.get('native_writing')
            and not tutor_playback_binding and not control_targets.get('pending_evaluation') else None)
        if foreground_vocabulary and self.agent_runtime.captured_question(voice_capture or {}) != {
                'kind': 'vocabulary', 'binding': foreground_vocabulary}:
            foreground_vocabulary = None
        foreground_exam = (exam_input_binding if active_exam and exam_input_binding
            and not active_vocabulary and not diagnostic_input_binding and not mistake_focus
            and not input_return_focus and not activity_speech_pending and not control_targets.get('native_writing')
            and not tutor_playback_binding and not control_targets.get('pending_evaluation') else None)
        if foreground_exam and self.agent_runtime.captured_question(voice_capture or {}) != {
                'kind': 'exam', 'binding': foreground_exam}:
            foreground_exam = None
        foreground_mistake = ((mistake_focus or {}).get('answer_binding') or (mistake_focus or {}).get('dialogue_binding')
            if mistake_focus and mistake_focus.get('phase') in {
                'candidate', 'answer', 'confirm', 'attempt', 'diagnosis_probe', 'diagnosis', 'remediation'}
            and not active_exam and not active_vocabulary and not diagnostic_input_binding
            and not input_return_focus and not activity_speech_pending and not control_targets.get('native_writing')
            and not tutor_playback_binding and not control_targets.get('pending_evaluation') else None)
        if foreground_mistake and self.agent_runtime.captured_question(voice_capture or {}) != {
                'kind': 'mistake', 'binding': foreground_mistake}:
            foreground_mistake = None
        foreground_question = bool(foreground_question or foreground_vocabulary or foreground_exam or foreground_mistake)
        tutor_control_context = bool(local_question and tutor_playback_binding and intent == 'tutor_question')
        bound_diagnostic_hint = bool(diagnostic_tutor_source and not active_exam and not active_vocabulary
            and not pure_speech_echo and not speech_echo_removed
            and self._is_current_diagnostic_hint_request(normalized_transcript, diagnostic_tutor_source['question']))
        from .controlled_launch_cancel import failed_starts
        with self.database.connect() as c:
            c.execute('BEGIN')
            failed_launch_context = any(failed_starts(c, session['child_id'], session_id, kind)
                                        for kind in ('content', 'games'))
        if (self.settings.agent_runtime_enabled
                and not selected_prompt
                and not explicit_exam_start
                # A retained question is context, not the owner of a current
                # reflection self-report or explicit reflection close command.
                # These already have registered, stage-local save capabilities.
                and not (session['state'] == 'reflection'
                    and intent in {'reflection_learned', 'reflection_difficult', 'reflection_next_action',
                                   'reflection_complete', 'reflection_skip'}
                    and action.get('type') in {'update_reflection', 'complete_reflection'})
                and not bound_diagnostic_hint
                and not relative_mistake_help and not foundation_hold_reply
                and not explicit_mistake_repair and not explicit_mistake_recapture
                and action.get('source') != 'active_task_explicit_completion'
                and not (not current_text_authorized and action.get('type') in {'describe_plan', 'open_mistake_book', 'query_device_status'})
                and (input_return_focus or control_targets.get('learning_help') or control_targets.get('task_deferral') or control_targets.get('native_writing') or failed_launch_context or activity_speech_pending or local_question or companion_feedback_snapshot.get('status') in {'ready', 'ambiguous'}
                     or session['state'] == 'break' or action.get('type') in {'begin_break', 'resume_study'}
                     or control_targets['task_resume']['status'] != 'none')
                and not pure_speech_echo and not speech_echo_removed
                and (intent not in CONVERSATION_CONTROL_INTENTS or
                     (activity_speech_pending or local_question) and intent in {'tutor_question', 'correct_previous'}
                     or tutor_control_context)
                and action.get('type') not in {'stop_speech', 'pause_observation', 'cancel_capture'}
                and intent != 'privacy_pause'
                and (activity_speech_pending or (not action.get('pending_answer_candidate')
                    and not self._local_answer_has_shape(received_transcript,
                        'vocabulary' if active_vocabulary else diagnostic_answer_type)
                    and not self._is_answer_role_confirmation(received_transcript)
                    and (not self.agent_runtime.local_answer_requires_clarification(received_transcript)
                         or foreground_question)))):
            control_targets['vocabulary'] = vocabulary_input_binding
            if foreground_exam:
                control_targets['foreground_exam'] = foreground_exam
            if foreground_mistake:
                control_targets['foreground_mistake'] = foreground_mistake
            captured_target = self.agent_runtime.captured_question(voice_capture or {}) or {}
            if captured_target.get('kind') == 'exam':
                control_targets['exam_run_id'] = captured_target['binding']['run_id']
            elif captured_target.get('kind') == 'diagnostic':
                control_targets['diagnostic'] = captured_target['binding']
            elif captured_target.get('kind') == 'vocabulary':
                control_targets['vocabulary'] = captured_target['binding']
            control_targets['pet_listening'] = (pet_listening_arrival or {}).get('binding')
            if (voice_capture or {}).get('activity_reference'):
                control_targets['activity_focus'] = voice_capture['activity_reference']
            # A declined handoff must not weaken an earlier local-source filter.
            local_sources = [*field_sources,diagnostic_local_source,document_local_source,
                (control_targets.get('learning_help') or {}).get('question'),
                (active_vocabulary or {}).get('prompt'),
                ((active_exam or {}).get('current_item') or {}).get('prompt'),
                ((mistake_focus or {}).get('state') or {}).get('question_text'),
                (((mistake_focus or {}).get('state') or {}).get('next_activity') or {}).get('question'),
                ((((mistake_focus or {}).get('state') or {}).get('next_activity') or {}).get('item') or {}).get('prompt'),
                *writing_reference_sources, *[x['label'] for x in companion_feedback_snapshot.get('candidates', [])]]
            control_lane = await self.voice_control_intent.resolve(
                session_id=session_id, child_id=session['child_id'], action_id=client_action_id,
                transcript=received_transcript, allowed=current_text_authorized, names=child_names,
                local_sources=local_sources, binding=control_targets, voice_capture=voice_capture, pre_dispatch=pre_dispatch,
                tutor_question_context=tutor_control_context, state={
                    'has_pending_question': local_question and not background_document, 'has_active_exam': bool(active_exam),
                    **({'has_foreground_independent_dialogue': True} if foreground_question else {}),
                    'has_paused_exam': bool(exam_run_binding and not active_exam),
                    'has_active_task': bool(control_targets['task']),
                    'has_next_task': control_targets['task_start']['status'] == 'ready',
                    **({'has_unavailable_original_task': True} if control_targets.get('task_deferral') else {}),
                    'has_paused_task': any(item.get('operation', 'resume') == 'resume' for item in control_targets['task_resume']['items']),
                    **({'has_pending_task_start': True} if any(item.get('operation') == 'start' for item in control_targets['task_resume']['items']) else {}),
                    **({'has_ready_supply_obligation': True} if any(item.get('operation') == 'supply' for item in control_targets['task_resume']['items']) else {}),
                    'has_companion_activity': companion_feedback_snapshot.get('status') in {'ready', 'ambiguous'},
                    **({'has_pending_input_draft': True} if input_return_focus else {}),
                    **({'has_companion_dialogue': True} if input_companion_context else {}),
                    **({'has_active_activity_focus': True} if control_targets.get('activity_focus') else {}),
                    'has_tutor_playback': bool(tutor_playback_binding),
                    'has_pet_listening': bool(control_targets['pet_listening']),
                    'is_study_phase': session['state'] == 'study', 'is_break_phase': session['state'] == 'break'})
            if (control_lane.get('control') == 'companion.chat'
                    or control_lane.get('status') == 'non_control' and control_lane.get('role') == 'companion_reply'):
                return await self._persist_input_companion_reply(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], control_lane,
                    voice_capture, child_names, local_sources, pre_dispatch)
            if control_lane['status'] == 'control':
                return self._persist_voice_control(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], control_lane, voice_capture)
            if activity_speech_pending:
                return await self._persist_activity_reply(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], control_lane,
                    voice_capture, child_names, local_sources, pre_dispatch)
            if tutor_control_context and control_lane.get('role_version') in {'tutor_question_role_v1', 'tutor_question_projected_role_v1'}:
                with self.database.connect() as c:
                    c.execute('BEGIN')
                    preserves_tutor_question = self.voice_control_intent.tutor_question_current(
                        c, session_id, session['child_id'], client_action_id, received_transcript,
                        control_lane, tutor_playback_binding)
            # A rejected semantic request is not permission to ask a broader
            # planner for another action with local context attached.
            cloud_planning_available = cloud_text_allowed = False
            # A background page is a retained source, not an answer owner.
            # Only independently eligible current speech can enter this lane.
            independent_document_dialogue = bool((background_document or foreground_question
                    and control_lane.get('question_scope') == 'independent'
                    and control_lane.get('role_version') == 'foreground_independent_dialogue_v1')
                and control_lane.get('status') == 'non_control'
                and control_lane.get('role') in {'question', 'greeting', 'companionship', 'topic_mention'}
                and current_text_authorized
                and self.voice_control_intent.text_eligible(received_transcript, child_names, local_sources))
            if independent_document_dialogue:
                if foreground_question:
                    from .foreground_independent_dialogue import VERSION, validate
                    foreground_dialogue_binding = {'version': VERSION, 'request_key': control_lane['request_key'],
                        **({'mistake': foreground_mistake} if foreground_mistake else {'exam': foreground_exam} if foreground_exam else
                           {'vocabulary': foreground_vocabulary} if foreground_vocabulary else {'diagnostic': diagnostic_input_binding}),
                        'role': control_lane['role']}
                    original_dispatch = pre_dispatch
                    def independent_dispatch(connection=None):
                        with (nullcontext(connection) if connection is not None else self.database.connect()) as c:
                            if connection is None:
                                c.execute('BEGIN')
                            original_dispatch(c)
                            validate(c, session_id, session['child_id'], client_action_id,
                                     received_transcript, foreground_dialogue_binding)
                    pre_dispatch = independent_dispatch
                cloud_planning_available = cloud_text_allowed = True
                normalized_transcript = received_transcript
                intent, response, action = 'independent_document_dialogue', '', {'type': 'none'}

        # A field-question handoff can authorize another explicit control, never
        # reinterpret an unclassified reply as the retained learning answer.
        if pending_input_field and semantic_field:
            return self._persist_input_revision_control(session_id, session['child_id'], session['state'],
                received_transcript, client_action_id, input_revision_arrival, 'supply', voice_capture=voice_capture,
                semantic_field_request=semantic_field.get('request_key'))

        orchestrator_version = "local_intent_v3"
        local_intent = intent
        critical_local_intents = CONVERSATION_CONTROL_INTENTS | {
            "privacy_pause", "pause_observation", "resume_observation", "stop_speech",
            "audio_check", "speech_echo_ignored", "conversation_repair",
            "prepare_written_question", "capture_readiness", "attention_check",
            "camera_visibility_check", "confirmation_followup",
            "behavior_intervention_feedback", "habit_experiment_response", "switch_child_profile",
            "start_exam_simulation", "submit_exam_answer", "pause_exam_simulation",
            "resume_exam_simulation", "cancel_exam_simulation", "exam_waiting_resume",
            # A concrete learning question already has a deterministic subject
            # and question reconstruction. A generic cloud planner must not
            # relabel physics as maths or turn a question into answer grading.
            "tutor_question", "correct_previous",
            # Explicit current-stage self-report must be saved, not replanned as chat.
            "reflection_learned", "reflection_difficult", "reflection_next_action",
            "reflection_complete", "reflection_skip",
        }
        planned = None
        if relative_mistake_help or foundation_hold_reply:
            critical_local_intents.add('mistake_dialogue')
        if explicit_mistake_repair:
            critical_local_intents.add('mistake_source_repair')
        if explicit_mistake_recapture:
            critical_local_intents.add('retry_capture')
        runtime_context: dict[str, Any] | None = None
        preserve_legacy_action = False
        pending_reply: dict[str, Any] | None = None
        binds_learning_reply = False
        answer_target = None
        capture_mismatch_notice = None
        spoken_answer_binding = None
        generic_answer_owner = None
        answer_targets = {}
        if active_exam and exam_input_binding:
            answer_targets["exam.start"] = {"action": "submit_exam_answer", "binding": exam_input_binding,
                             "answer_type": (active_exam.get("current_item") or {}).get("answer_type")}
        if vocabulary_input_binding:
            answer_targets["pet_vocabulary.start"] = {"action": "submit_pet_vocabulary_answer", "binding": vocabulary_input_binding,
                "answer_type": "vocabulary"}
        if diagnostic_answer_type and diagnostic_input_binding:
            answer_targets["diagnostic.start"] = {"action": "submit_diagnostic_answer", "binding": diagnostic_input_binding,
                             "answer_type": diagnostic_answer_type}
        if answer_targets:
            # Candidate ownership uses the same receipt-backed focus as answer
            # execution, never an ordering preference between live exercises.
            selected_reply = self.agent_runtime.pending_child_reply(session_id)
            selected_target = answer_targets.get((selected_reply or {}).get("capability_name"))
            if selected_target and selected_target["binding"] == selected_reply.get("result"):
                answer_target = selected_target
        elif self.settings.agent_runtime_enabled:
            generic_answer_owner = self.agent_runtime.pending_child_reply(session_id)
            if (generic_answer_owner and generic_answer_owner.get("objective_id")
                    and self._pending_answer_capability(generic_answer_owner) == "learning.evaluate_response"):
                answer_target = {"action": "evaluate_child_response", "answer_type": "free_response", "binding": {
                    key: generic_answer_owner.get(key) for key in ("objective_id", "plan_id", "capability_name", "phase")}}
                learning_binding = self.agent_runtime.learning_reply_binding(generic_answer_owner)
                if learning_binding is not None:
                    answer_target["binding"] = learning_binding
                else:
                    answer_target["binding"]["source_version"] = hashlib.sha256(json.dumps(
                        {key: generic_answer_owner.get(key) for key in ("summary", "success_criteria", "arguments", "result")},
                        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        if (mistake_focus and mistake_focus['phase'] in {'attempt', 'diagnosis_probe'}
                and not answer_targets and action.get('type') == 'continue_mistake_dialogue'):
            with self.database.connect() as connection:
                answer_target = self.agent_runtime.local_mistake_answer_target(connection, session_id, session['child_id'])
            generic_answer_owner = None
            from .mistake_pending_answer import explicit_resume
            pending_control = bool(mistake_focus['state'].get('pending_operation')
                and not explicit_resume(received_transcript)
                and not self._local_answer_has_shape(received_transcript)
                and not action.get('local_only'))
            if not pending_control:
                cloud_planning_available = cloud_text_allowed = False
                action.update(local_only=True, cloud_transmission_allowed=False)
            if answer_target:
                action['mistake_answer_binding'] = answer_target['binding']
            elif (not mistake_focus['state'].get('pending_operation')
                  and not foundation_hold_reply):
                intent, response, action = self._answer_clarification(normalized_transcript, None, keep_candidate=False)
                response = '当前复测题目或来源已变化，我先不记录作答。请先恢复并核对当前错题。'
        if (answer_target and answer_target['answer_type'] == 'open_response'
                and answer_target['action'] == 'submit_diagnostic_answer'
                and (control_lane or {}).get('role') == 'answer' and voice_capture
                and not pure_speech_echo and not speech_echo_removed
                and not self.agent_runtime.local_answer_requires_clarification(received_transcript)
                and self.agent_runtime.capture_matches_answer(voice_capture, answer_target['action'], answer_target['binding'])):
            with self.database.connect() as connection:
                connection.execute('BEGIN')
                spoken_answer_binding = self.voice_control_intent.spoken_answer_target(connection, session_id,
                    session['child_id'], client_action_id, received_transcript, control_lane.get('request_key'))
            if spoken_answer_binding == answer_target['binding']:
                intent, response, action = ('submit_diagnostic_answer', '我按你刚才的完整口述核对这道题，不把转写当作已经掌握。',
                    {'type':'submit_diagnostic_answer','answer':received_transcript})
            else:
                spoken_answer_binding = None
        answer_confirmation = self._is_answer_role_confirmation(normalized_transcript)
        confirmed_answer = (self._confirmed_local_answer(session_id, session["child_id"], answer_target)
                            if answer_confirmation and not pure_speech_echo else None)
        if confirmed_answer:
            intent = answer_target["action"]
            response = "好，按你刚才确认的原话核对这道题。"
            action = {"type": intent, ("utterance" if intent == 'continue_mistake_dialogue' else "answer"): confirmed_answer["answer"],
                      "answer_source_turn_id": confirmed_answer["turn_id"],
                      "local_only": True, "cloud_transmission_allowed": False}
            if intent == 'continue_mistake_dialogue':
                action['mistake_answer_binding'] = answer_target['binding']
            cloud_planning_available = cloud_text_allowed = False
        if diagnostic_input_binding and not foreground_dialogue_binding:
            # The pending local assessment owns its answer and source material.
            # Intent/planning calls would leak the answer before grading guards.
            cloud_planning_available = False
            cloud_text_allowed = False
            if (diagnostic_tutor_source and not pure_speech_echo and not speech_echo_removed
                    and not active_exam and not active_vocabulary
                    and self._is_current_diagnostic_hint_request(normalized_transcript, diagnostic_tutor_source["question"])):
                intent = local_intent = "tutor_question"
                response = "我先给当前这道题一个提示，不替你作答。"
                action = {"type": "ask_tutor", "question": diagnostic_tutor_source["question"],
                    "subject": diagnostic_tutor_source["subject"], "hint_level": 1}
            if (not active_exam and not active_vocabulary
                    and self._should_bind_learning_reply(intent, normalized_transcript, action)):
                intent, response, action = ("submit_diagnostic_answer", "收到，我先在本机核对这道题。",
                    {"type": "submit_diagnostic_answer", "answer": self._spoken_answer(normalized_transcript)})
            action.update(local_only=True, cloud_transmission_allowed=False)
        conflicting_answer = bool(answer_target and not foreground_dialogue_binding and not pure_speech_echo
            and intent not in CONVERSATION_CONTROL_INTENTS
            and self.agent_runtime.local_answer_requires_clarification(normalized_transcript))
        if conflicting_answer:
            confirmed_answer = None
            cloud_planning_available = cloud_text_allowed = False
            intent, response, action = self._answer_clarification(normalized_transcript, answer_target)
        transformed_answer = bool(
            answer_target and not confirmed_answer and cloud_planning_available
            and (action.get('type') in {'submit_diagnostic_answer', 'submit_exam_answer',
                                       'submit_pet_vocabulary_answer', 'evaluate_child_response', 'continue_mistake_dialogue'}
                 or (action.get('type') == 'none' and not action.get('source'))
                 or re.match(r'^(?:我的答案是|答案是|我选)', normalized_transcript))
            and intent not in CONVERSATION_CONTROL_INTENTS
            and sanitize_cloud_text(normalized_transcript[:1200], child_names).text != normalized_transcript)
        if transformed_answer:
            cloud_planning_available = cloud_text_allowed = False
            intent, response, action = self._answer_clarification(normalized_transcript, answer_target)
        if (answer_target and not confirmed_answer and not cloud_planning_available
                and action.get("type") in {"submit_diagnostic_answer", "submit_exam_answer", "submit_pet_vocabulary_answer"}):
            intent, response, action = (
                answer_target["action"], "我先核对你选定练习的这次回答。",
                {"type": answer_target["action"], "answer": self._spoken_answer(normalized_transcript),
                 "local_only": True, "cloud_transmission_allowed": False})
        if (answer_target and not pure_speech_echo and not cloud_planning_available
                and not spoken_answer_binding
                and not confirmed_answer and intent not in CONVERSATION_CONTROL_INTENTS
                and (action.get("type") in {"submit_diagnostic_answer", "submit_exam_answer", "submit_pet_vocabulary_answer", "evaluate_child_response", "continue_mistake_dialogue"}
                     or (action.get("type") == "none" and not action.get("source")))
                and not (generic_answer_owner and (
                    self._is_non_evidence_acknowledgement(re.sub(r'[，。！？、\s]', '', normalized_transcript))
                    or self._needs_different_explanation(re.sub(r'[，。！？、\s]', '', normalized_transcript), intent)))
                and (transformed_answer or not self._local_answer_has_shape(normalized_transcript, answer_target["answer_type"]))):
            intent, response, action = self._answer_clarification(
                normalized_transcript, answer_target, keep_candidate=not answer_confirmation)
            cloud_text_allowed = False
        if (voice_capture and not pure_speech_echo and answer_target
                and intent not in CONVERSATION_CONTROL_INTENTS
                and (action.get('type') in {'submit_diagnostic_answer', 'submit_exam_answer',
                        'submit_pet_vocabulary_answer', 'evaluate_child_response', 'continue_mistake_dialogue'}
                     or action.get('source') == 'pending_answer_clarification'
                     or (action.get('type') == 'none' and not action.get('source')))
                and (speech_echo_removed or not self.agent_runtime.capture_matches_answer(
                    voice_capture, answer_target['action'], answer_target['binding']))):
            confirmed_answer = None
            cloud_planning_available = cloud_text_allowed = False
            intent, response, action = self._answer_clarification(received_transcript, None, keep_candidate=False)
            response = CAPTURE_QUESTION_MISMATCH_RESPONSE
            capture_mismatch_notice = self._capture_mismatch_notice_scope(
                session_id, session['child_id'], answer_target)
        answer_role_notice_scope = (self._capture_mismatch_notice_scope(
            session_id, session['child_id'], answer_target)
            if response == ANSWER_ROLE_CLARIFICATION_RESPONSE else None)
        if answer_role_notice_scope is not None and answer_confirmation and not confirmed_answer:
            # The latest original turn has no confirmable answer. Never fall back
            # to an older A or silently treat a later, unlabelled B as its answer.
            response = ANSWER_ROLE_RESTATEMENT_RESPONSE
            action.pop('pending_answer_candidate', None)
            answer_role_notice_scope = None
        # Ordinary language is planned by the cloud, even when a legacy phrase
        # recognizer happens to match. Only immediate safety and local-only
        # evidence handling bypass the model; keyword matching is not consent.
        cloud_first = cloud_planning_available and intent not in {
            "privacy_pause", "pause_observation", "stop_speech", "speech_echo_ignored",
            "urgent_safety_support", "high_risk_child_safety", "memory_command",
            "daily_learning_summary", "document_observation_question", "document_observation_response",
            "switch_child_profile", "repetition_confirmation",
            "reflection_learned", "reflection_difficult", "reflection_next_action",
            "reflection_complete", "reflection_skip",
        } and not action.get("local_only") and action.get("cloud_transmission_allowed") is not False
        if cloud_first:
            runtime_context = self.agent_runtime.assemble_context(session_id, tuple(recent_dialogue))
            pending_reply = self.agent_runtime.pending_child_reply(session_id)
            from .learning_system_facts import detect_learning_system_query
            learning_system_query = detect_learning_system_query(
                normalized_transcript, () if independent_document_dialogue else tuple(recent_dialogue)
            )
            planned = await self._resolve_agent_plan(
                session_id=session_id, child_id=session["child_id"],
                transcript=normalized_transcript, known_child_names=child_names,
                recent_dialogue=() if independent_document_dialogue else tuple(recent_dialogue), require_utterance_binding=True,
                independent_document_dialogue=independent_document_dialogue,
                foreground_independent_dialogue=bool(foreground_dialogue_binding),
                foreground_exam_dialogue=bool(foreground_exam and foreground_dialogue_binding),
                independent_document_role=(control_lane or {}).get('role') if independent_document_dialogue else None,
                companion_feedback_snapshot=companion_feedback_snapshot,
                input_revision_arrival=input_revision_arrival,
                pre_dispatch=pre_dispatch,
            )
            if planned:
                planned_decision, planned_context_manifest = planned
                accepted_fact_capabilities = {"system.inspect_learning"}
                if learning_system_query and learning_system_query.scope == "plan":
                    accepted_fact_capabilities.add("planning.describe")
                if (learning_system_query
                        and planned_decision.step.capability.name not in accepted_fact_capabilities):
                    fact_capability = self.capability_registry.require(
                        "system.inspect_learning", session["state"]
                    )
                    planned_decision = AgentPlanDecision(
                        objective=ObjectiveDraft("none", None, None, (), "inspect_local_facts"),
                        step=PlannedStep(
                            fact_capability,
                            {"scope": learning_system_query.scope, "track": learning_system_query.track},
                            "本轮询问产品持久化事实，必须读取本机系统记录，不能采用模型猜测。",
                            fact_capability.expected_evidence,
                        ),
                        spoken_transition="我先核对本机保存的真实记录。",
                        requires_child_reply=False,
                        confidence=1.0,
                    )
                    planned_context_manifest["system_fact_guard"] = {
                        "scope": learning_system_query.scope,
                        "track": learning_system_query.track,
                        "model_answer_discarded": True,
                    }
                intent = planned_decision.step.capability.name
                response = planned_decision.spoken_transition
                action = action_from_plan(planned_decision, normalized_transcript, runtime_context.get("active_objective"))
                action.update(
                    source=("agent_runtime_system_fact_guard" if learning_system_query else "agent_runtime_planner"),
                    confidence=round(planned_decision.confidence, 3),
                )
                orchestrator_version = "agent_runtime_cloud_first_v2"
            else:
                intent = "cloud_understanding_unavailable"
                response = "这次没能确认你的意思。你的话和学习进度已保留，这次没有执行操作。你可以再说一遍，或稍后继续。"
                action = {"type": "none", "source": "cloud_planner_unavailable", "retryable": True}
                orchestrator_version = "agent_runtime_cloud_first_v2"
        if self.settings.agent_runtime_enabled and not cloud_first:
            runtime_context = self.agent_runtime.assemble_context(
                session_id, tuple(recent_dialogue)
            )
            active_companion_activity = runtime_context.get("active_companion_activity")
            if (active_companion_activity and not active_exam and intent not in CONVERSATION_CONTROL_INTENTS
                    and not self._has_independent_capability(action, session["state"])):
                intent = "supportive_chat"
                response = ""
                action = {"type": "none"}
            pending_reply = self.agent_runtime.pending_child_reply(session_id)
            compact_reply = re.sub(r"[，。！？、\s]", "", normalized_transcript)
            if ((pending_reply or {}).get("capability_name") == "input.choose_prompt"
                    and intent not in CONVERSATION_CONTROL_INTENTS and not pure_speech_echo
                    and (action.get("type") in {
                        "submit_diagnostic_answer", "submit_pet_vocabulary_answer", "submit_exam_answer", "evaluate_child_response",
                    } or (action.get("type") == "none" and not action.get("source")))):
                intent, response, action = (
                    "pending_question_clarification",
                    "现在同时留着不止一项待回答的练习。你想先继续哪一项？我先不把这句话算到任何题目上。",
                    {"type": "none", "source": "pending_question_conflict", "requires_child_reply": True,
                     "local_only": True, "cloud_transmission_allowed": False},
                )
            ordinary_capture_answer = bool(
                voice_capture and action.get('type') == 'evaluate_child_response'
                and not pure_speech_echo and not speech_echo_removed and not transformed_answer
                and intent not in CONVERSATION_CONTROL_INTENTS
                and answer_target and answer_target.get('action') == 'evaluate_child_response'
                and self.agent_runtime.learning_reply_binding(pending_reply) == answer_target.get('binding')
                and (pending_reply or {}).get('capability_name') in {'learning.explain','learning.evaluate_response','learning.request_response'}
                and self.agent_runtime.capture_matches_answer(voice_capture, 'evaluate_child_response', answer_target['binding'])
                and self._local_answer_has_shape(normalized_transcript, 'free_response'))
            binds_learning_reply = bool(
                pending_reply and self._should_bind_learning_reply(
                    intent,
                    normalized_transcript,
                    {**action, 'type':'none'} if ordinary_capture_answer else action,
                    pending_reply,
                    previous_response,
                )
            )
            exam_action_types = {
                "start_exam_simulation", "submit_exam_answer", "pause_exam_simulation",
                "resume_exam_simulation", "cancel_exam_simulation",
            }
            if intent in {"pending_answer_clarification", "pending_question_clarification"}:
                planned_decision = AgentPlanDecision(
                    ObjectiveDraft("none", None, None, (), "clarify"),
                    PlannedStep(self.capability_registry.require("conversation.respond", session["state"]),
                                {"response": response}, "Clarify input ownership locally; do not grade", ()),
                    response, True, 1.0,
                )
                preserve_legacy_action = True
            elif confirmed_answer and generic_answer_owner:
                planned_decision = self._deterministic_learning_reply_plan(
                    transcript=confirmed_answer["answer"], intent="independent_response", pending_reply=generic_answer_owner)
            elif confirmed_answer:
                planned_decision = self._deterministic_action_plan(
                    owner=self.capability_registry.owner_for_action_type(action["type"], session["state"]),
                    action=action, response=response)
                preserve_legacy_action = True
            elif intent in {"conversation_repair", "prepare_written_question"}:
                owner = self.capability_registry.owner_for_action_type("none", session["state"])
                planned_decision = self._deterministic_action_plan(
                    owner=owner,
                    action=action,
                    response=response,
                    objective_mode="cancel",
                    objective_phase="child_changed_direction",
                )
                preserve_legacy_action = True
            elif (action.get("type") in exam_action_types or action.get("type") == "continue_mistake_dialogue"
                  or (session["state"] == "reflection" and action.get("type") in {"update_reflection", "complete_reflection"})):
                owner = self.capability_registry.owner_for_action_type(
                    str(action["type"]), session["state"]
                )
                planned_decision = self._deterministic_action_plan(
                    owner=owner,
                    action=action,
                    response=response,
                )
                preserve_legacy_action = True
            elif pending_reply and intent not in {"document_observation_question", "document_observation_response", "daily_learning_summary"} and (
                binds_learning_reply
                or self._needs_different_explanation(compact_reply, intent)
            ):
                planned_decision = self._deterministic_learning_reply_plan(
                    transcript=normalized_transcript,
                    intent=intent,
                    pending_reply=pending_reply,
                )
            elif intent in {"tutor_question", "correct_previous"}:
                planned_decision = self._deterministic_learning_question_plan(
                    intent=intent,
                    response=response,
                    action=action,
                    active_objective=runtime_context.get("active_objective"),
                )
            else:
                owner = self.capability_registry.owner_for_action_type(
                    str(action.get("type") or "none"), session["state"]
                )
                if action.get("type") != "none":
                    planned_decision = self._deterministic_action_plan(
                        owner=owner,
                        action=action,
                        response=response,
                        objective_mode=self._objective_transition_for_action(
                            str(action.get("type") or "none")
                        ),
                        objective_phase=(
                            "capture_new_question"
                            if action.get("type") == "request_capture" else "execute"
                        ),
                    )
                    preserve_legacy_action = True
            if planned_decision:
                planned_context_manifest = planner_context_manifest(runtime_context)
                response = planned_decision.spoken_transition
                if not preserve_legacy_action:
                    intent = planned_decision.step.capability.name
                    action = action_from_plan(
                        planned_decision,
                        normalized_transcript,
                        runtime_context.get("active_objective"),
                    )
                    action["source"] = "agent_runtime_deterministic"
                    action["confidence"] = round(planned_decision.confidence, 3)
                orchestrator_version = "agent_runtime_deterministic_v1"
                planned = (planned_decision, planned_context_manifest)
        if (
            not planned
            and not cloud_first
            and cloud_text_allowed
            and self.settings.agent_runtime_enabled
            and intent not in critical_local_intents
            and action.get("type") == "none"
            and not (runtime_context and runtime_context.get("active_companion_activity"))
        ):
            candidate_plan = await self._resolve_agent_plan(
                session_id=session_id,
                child_id=session["child_id"],
                transcript=normalized_transcript,
                known_child_names=child_names,
                recent_dialogue=tuple(recent_dialogue),
                companion_feedback_snapshot=companion_feedback_snapshot,
                input_revision_arrival=input_revision_arrival,
                pre_dispatch=pre_dispatch,
            )
            explicit_learning_utterance = (
                local_intent in {"tutor_question", "correct_previous"}
                or binds_learning_reply
                or self._is_explicit_learning_request(normalized_transcript)
            )
            if (
                candidate_plan
                and candidate_plan[0].step.capability.name.startswith("learning.")
                and candidate_plan[0].step.capability.name != "learning.explain"
                and not explicit_learning_utterance
            ):
                # A general planner may use the study plan as context, but it
                # may not turn greetings, microphone checks, or casual speech
                # into a lesson without an explicit learning request.
                candidate_plan = None
                intent = "clarify_learning_request"
                response = (
                    f"你提到了“{normalized_transcript[:80]}”。你是想让我讲解它，"
                    "还是只是在说今天的学习内容？"
                )
                action = {"type": "none", "source": "explicit_learning_consent_guard"}
                orchestrator_version = "local_learning_consent_guard_v1"
            if (
                candidate_plan
                and pending_reply
                and not binds_learning_reply
                and candidate_plan[0].step.capability.name == "learning.evaluate_response"
            ):
                candidate_plan = None
            planned = candidate_plan
            if planned:
                planned_decision, planned_context_manifest = planned
                intent = planned_decision.step.capability.name
                response = planned_decision.spoken_transition
                runtime_context = runtime_context or self.agent_runtime.assemble_context(
                    session_id, tuple(recent_dialogue)
                )
                action = action_from_plan(
                    planned_decision,
                    normalized_transcript,
                    runtime_context.get("active_objective"),
                )
                action["source"] = "agent_runtime_planner"
                action["confidence"] = round(planned_decision.confidence, 3)
                orchestrator_version = PLANNER_VERSION

        if not cloud_first and not planned and intent == "supportive_chat" and cloud_text_allowed:
            if runtime_context and runtime_context.get("active_companion_activity"):
                companion_reply = await self._generate_companion_reply(
                    session_id=session_id,
                    child_id=session["child_id"],
                    state=session["state"],
                    transcript=normalized_transcript,
                    previous_response=previous_response,
                    pre_dispatch=pre_dispatch,
                    known_child_names=child_names,
                    recent_dialogue=tuple(recent_dialogue),
                )
                if companion_reply:
                    response = companion_reply.text
                    action = {"type": "none", "source": companion_reply.source}
                    orchestrator_version = "hybrid_companion_v1" if companion_reply.source == "cloud_companion" else "local_policy_v2"
            elif clarification_count >= MAX_CLARIFICATION_TURNS_PER_STAGE:
                intent, response, action = self._clarification_limit_result(session["state"])
                orchestrator_version = "local_policy_v1"
            else:
                cloud_resolution = await self._resolve_cloud_intent(
                    session_id=session_id,
                    child_id=session["child_id"],
                    state=session["state"],
                    transcript=normalized_transcript,
                    break_minutes=session["break_minutes"],
                    pre_dispatch=pre_dispatch,
                    known_child_names=child_names,
                    recent_dialogue=tuple(recent_dialogue),
                )
                if cloud_resolution and cloud_resolution.action:
                    intent, response, action = cloud_resolution.action
                    orchestrator_version = "local_policy_v2" if action.get("source") == "local_prompt_guard" else "hybrid_intent_v1"
                elif cloud_resolution and cloud_resolution.companion_eligible:
                    companion_reply = await self._generate_companion_reply(
                        session_id=session_id,
                        child_id=session["child_id"],
                        state=session["state"],
                        transcript=normalized_transcript,
                        previous_response=previous_response,
                        pre_dispatch=pre_dispatch,
                        known_child_names=child_names,
                        recent_dialogue=tuple(recent_dialogue),
                    )
                    if companion_reply:
                        response = companion_reply.text
                        action = {"type": "none", "source": companion_reply.source}
                        orchestrator_version = "hybrid_companion_v1" if companion_reply.source == "cloud_companion" else "local_policy_v2"

        # A planner is allowed to choose how to teach, but it cannot invent the
        # child's decision to start a lesson. Bare topic names, wake-word echo,
        # and ambiguous ASR fragments must never cross into the tutor model.
        explicit_learning_request = (
            (cloud_first and planned is not None)
            or bool(confirmed_answer and generic_answer_owner)
            or local_intent in {"tutor_question", "correct_previous"}
            or binds_learning_reply
            or self._is_explicit_learning_request(normalized_transcript)
        )
        if action.get("type") == "ask_tutor" and not explicit_learning_request:
            planned = None
            planned_decision = None
            planned_context_manifest = None
            preserve_legacy_action = False
            intent = "clarify_learning_request"
            response = (
                f"你提到了“{normalized_transcript[:80]}”。你是想让我讲解它，"
                "还是只是在说今天的学习内容？"
            )
            action = {"type": "none", "source": "explicit_learning_consent_guard"}
            orchestrator_version = "local_learning_consent_guard_v1"

        if (
            action.get("type") == "ask_tutor"
            and action.get("question")
            and action.get("capability") != "learning.evaluate_response"
        ):
            if not action.get("subject") or action.get("subject") == "其他":
                inferred_subject = self._subject_from_text(str(action["question"]))
                if inferred_subject != "其他":
                    action["subject"] = inferred_subject
            action["local_learning_context"] = await self._local_atomic_learning_context(
                child_id=session["child_id"],
                question=str(action["question"]),
                subject=str(action.get("subject") or ""),
            )
            if self.local_courses is not None:
                try:
                    course_context = await asyncio.to_thread(
                        self.local_courses.context_for_child,
                        child_id=session["child_id"], query=str(action["question"]),
                        subject=str(action.get("subject") or ""), limit=3,
                    )
                    if course_context.get("cloud_transmission_allowed") is False:
                        action["local_learning_context"]["course_context"] = course_context
                except (ValueError, OSError, sqlite3.Error):
                    action["local_learning_context"]["course_context"] = {
                        "status": "unavailable", "cloud_transmission_allowed": False,
                        "mastery_update_allowed": False, "evidence": [],
                    }
            document_context = ({} if independent_document_dialogue else
                                self._local_document_context(session_id, session["child_id"]))
            if document_context.get("status") == "completed":
                action["local_document_context"] = {
                    "revision_id": document_context["latest_revision_id"],
                    "cloud_transmission_allowed": False,
                    "requires_text_confirmation": True,
                    "mastery_update_allowed": False,
                }

        # Deterministic, cloud-resolved and planner-produced actions all cross
        # the same registered capability boundary before they are persisted.
        # An action without an owner is a code defect, not a successful no-op.
        if ((control_lane or {}).get('status')=='non_control' and control_lane.get('role')=='answer'
                and control_targets.get('tutor_check') and not pure_speech_echo and not speech_echo_removed):
            check=control_targets['tutor_check']
            capability=self.capability_registry.require('tutor.capture_check_response',session['state'])
            response='我先把这次回答保存到刚才的检查问题下，保留原题评分；这还不是独立核验。'
            action={'type':'submit_tutor_check_response','capability':capability.name,'execution_location':'server',
                'source':'agent_runtime_tutor_check','local_only':True,'cloud_transmission_allowed':False,
                'turn_id':check['turn_id'],'source_version':check['source_version'],'response':received_transcript}
            intent=capability.name
            planned_decision=AgentPlanDecision(ObjectiveDraft('none',None,None,(),'check'),
                PlannedStep(capability,{k:action[k] for k in ('turn_id','source_version','response')},
                    'Saved content-free answer role owns the displayed Tutor check',capability.expected_evidence),
                response,False,1.0)
            planned_context_manifest={**(planned_context_manifest or {}),'learning_input_arrival_id':input_revision_arrival['arrival_id'],
                'tutor_check_response_binding':{'request_key':control_lane['request_key'],'target':check}}
            preserve_legacy_action=True
        preserves_ordinary_learning_ack = self._ordinary_learning_ack_plan(
            transcript=normalized_transcript, pending_reply=pending_reply,
            decision=planned_decision, action=action, control_lane=control_lane,
            voice_capture=voice_capture, answer_target=answer_target,
            altered=bool(pure_speech_echo or speech_echo_removed or transformed_answer))
        if (preserves_ordinary_learning_ack
                and pending_reply['capability_name'] == 'learning.request_response'):
            # Keep the current question and thinking time. This is a turn receipt,
            # not another question plan, model request, answer candidate or grade.
            intent, response = 'ordinary_learning_waiting_answer', '原问题保留，等你准备好再用自己的话回答。'
            action = {'type': 'none', 'source': 'ordinary_learning_waiting_answer',
                      'suppress_speech': True, 'requires_child_reply': True,
                      'local_only': True, 'cloud_transmission_allowed': False,
                      'independent_evidence': False, 'mastery_update_allowed': False}
            planned = planned_decision = planned_context_manifest = None
            preserve_legacy_action = True
        preserves_ordinary_new_question = self._ordinary_learning_new_question_plan(
            transcript=received_transcript, pending_reply=pending_reply,
            decision=planned_decision, action=action, control_lane=control_lane,
            voice_capture=voice_capture, answer_target=answer_target,
            altered=bool(pure_speech_echo or speech_echo_removed or transformed_answer))
        preserves_answer_candidate = bool(action.get('pending_answer_candidate')
            and (control_lane or {}).get('role') != 'acknowledgement')
        if control_lane and not preserves_ordinary_learning_ack and not preserves_ordinary_new_question and not independent_document_dialogue and not selected_prompt and not (preserves_tutor_question and action.get('type') == 'ask_tutor') and action.get('source') != 'pending_question_conflict' and not preserves_answer_candidate and not (control_lane.get('role') == 'answer' and action.get('type') in {
                'none', 'submit_tutor_check_response', 'submit_diagnostic_answer', 'submit_exam_answer',
                'submit_pet_vocabulary_answer', 'continue_mistake_dialogue'}):
            # A local keyword guess cannot become a fallback side effect when
            # semantic classification found an unknown, mixed or denied target.
            action = {'type': 'none', 'source': 'content_free_control_clarification',
                      'local_only': True, 'cloud_transmission_allowed': False}
            intent, planned = 'clarify_control_request', None
            model_failure = control_lane.get('reason') in {'MODEL_UNAVAILABLE', 'CONTROL_MODEL_FAILED'}
            if model_failure:
                action['understanding_failure_code'] = control_lane['reason']
            response = ('云端文本理解已关闭。这句话我还没有可靠理解，也没有执行操作。原题和进度保留。'
                        if control_lane.get('reason') == 'CLOUD_TEXT_DISABLED' else
                        '语言理解服务还没有配置好，不是你说得不清楚。你的话和学习进度已保留，这次没有执行操作。请家长检查服务配置后再继续。'
                        if control_lane.get('reason') == 'MODEL_UNAVAILABLE' else
                        '语言理解服务暂时不可用，不是你说得不清楚。你的话和学习进度已保留，这次没有执行操作。等服务恢复后再继续。'
                        if control_lane.get('reason') == 'CONTROL_MODEL_FAILED' else
                        '刚才的活动还在等待反馈或恢复确认。你是要结束活动后回到原任务，还是另选一项任务？我先不恢复计时。'
                        if control_lane.get('reason') == 'CONTROL_ACTIVITY_RESUME_SCOPE_REQUIRED' else
                        '我听到了，但还不能确定你是要操作哪一项，还是在回答原题。你再说明一下，原题和进度保留。')
            from .learning_input_voice import clarification_response
            response = clarification_response(control_lane, received_transcript) or response
            planned_decision = replace(self._deterministic_action_plan(
                owner=self.capability_registry.owner_for_action_type('none', session['state']),
                action=action, response=response), requires_child_reply=not model_failure)
            planned_context_manifest = {'learning_input_arrival_id': input_revision_arrival['arrival_id']}
            preserve_legacy_action = False
        if (self.settings.agent_runtime_enabled and action.get('type') in {'begin_break', 'resume_study'}
                and control_lane is None):
            call_id = (planned_context_manifest or {}).get('break_control_model_call_id')
            if not call_id:
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            resolved = self.voice_control_intent.accept_verified_break_plan(
                session_id=session_id, child_id=session['child_id'], action_id=client_action_id,
                transcript=received_transcript, control='break.begin' if action['type'] == 'begin_break' else 'break.resume',
                binding=control_targets, call_id=call_id)
            return self._persist_voice_control(session_id, session['child_id'], session['state'],
                received_transcript, client_action_id, input_revision_arrival['arrival_id'], resolved, voice_capture)
        if action.get('type') == 'submit_mistake_answer':
            # A planner may still return the retained legacy capability. Ignore
            # its answer and freeze only the focus read before the model call.
            intent, response, action = self._canonical_mistake_submission(
                received_transcript, mistake_focus, voice_capture,
                transformed=bool(speech_echo_removed or normalized_transcript != received_transcript))
            owner = self.capability_registry.owner_for_action_type(action['type'], session['state'])
            planned_decision = self._deterministic_action_plan(owner=owner, action=action, response=response)
            planned_decision = replace(planned_decision, requires_child_reply=True)
            planned_context_manifest = planner_context_manifest(
                runtime_context or self.agent_runtime.assemble_context(session_id, tuple(recent_dialogue)))
            planned = (planned_decision, planned_context_manifest)
        if action.get('type') in {'read_writing_original', 'confirm_writing_original'}:
            # The generic planner cannot supply original-page authority.
            action = {'type': 'none', 'source': 'writing_target_clarification', 'local_only': True,
                      'cloud_transmission_allowed': False, 'mastery_update_allowed': False}
            response = '先确认你正在写哪道题，并把自己的作答放到高拍仪下。原题或作答范围还没确定，我不会猜测或提交。'
            intent, planned = 'writing_target_clarification', None
            planned_decision = self._deterministic_action_plan(owner=self.capability_registry.owner_for_action_type('none', session['state']),
                                                               action=action, response=response)
            planned_context_manifest = {'learning_input_arrival_id': (input_revision_arrival or {}).get('arrival_id')}
        if speech_echo_removed:
            action["speech_echo_filter"] = {
                "removed_characters": speech_echo_removed_chars,
                "raw_transcript_sha256": hashlib.sha256(
                    received_transcript.encode("utf-8")
                ).hexdigest(),
            }
        if action.get("type") == "ask_tutor":
            if action.get('question_relation') == 'followup':
                from .tutor_followup_binding import latest_binding
                with self.database.connect() as followup_connection:
                    binding = latest_binding(followup_connection, session_id=session_id, child_id=session['child_id'])
                action['tutor_followup_binding'] = binding
                planned_context_manifest = {**(planned_context_manifest or {}), 'tutor_followup_binding': binding}
            # Only the locally frozen, exact question owns these IDs, never a planner.
            action.pop("diagnostic_prompt_id", None)
            action.pop("diagnostic_item_version", None)
            if (diagnostic_tutor_source and not pure_speech_echo and not speech_echo_removed
                    and action.get("question") == diagnostic_tutor_source["question"]
                    and action.get("subject") == diagnostic_tutor_source["subject"]
                    and self._is_current_diagnostic_hint_request(normalized_transcript, diagnostic_tutor_source["question"])):
                action.update(diagnostic_prompt_id=diagnostic_tutor_source["prompt_id"],
                    diagnostic_item_version=diagnostic_tutor_source["item_version"],
                    local_only=True, cloud_transmission_allowed=False)
        if action.get("type") in {"submit_diagnostic_answer", "replay_diagnostic", "cancel_diagnostic"}:
            action["diagnostic_prompt_id"] = (diagnostic_input_binding or {}).get("prompt_id")
            action["diagnostic_item_version"] = (diagnostic_input_binding or {}).get("item_version")
        if action.get("type") == "control_tutor_playback":
            action["tutor_playback_id"] = (tutor_playback_binding or {}).get("playback_id")
            action["tutor_source_version"] = (tutor_playback_binding or {}).get("source_version")
        if action.get("type") == "submit_exam_answer":
            action["exam_input_binding"] = exam_input_binding
        if action.get("type") in {"pause_exam_simulation", "resume_exam_simulation", "cancel_exam_simulation"}:
            action["exam_run_id"] = exam_run_binding
        if action.get("type") in {"submit_pet_vocabulary_answer", "cancel_pet_vocabulary_review"}:
            action["vocabulary_prompt_id"] = (active_vocabulary or {}).get("prompt_session_id")
            action["vocabulary_card_version"] = (active_vocabulary or {}).get("card_version")
        if action.get("type") in {"start_diagnostic", "submit_diagnostic_answer", "replay_diagnostic",
                                  "start_pet_vocabulary_review", "submit_pet_vocabulary_answer",
                                  "start_exam_simulation", "submit_exam_answer", "resume_exam_simulation"}:
            # The reply may contain local textbook-derived question content.
            # Future planner history must not turn local playback into export.
            action.update(local_only=True, cloud_transmission_allowed=False)
        from .ordinary_learning_text_projection import prepare_input_policy
        ordinary_text_policy = prepare_input_policy(action, planned_decision,
            current_text_authorized=current_text_authorized, voice_capture=voice_capture,
            confirmed_answer=confirmed_answer)
        if confirmed_answer:
            action.update(answer_source_turn_id=confirmed_answer["turn_id"], local_only=True, cloud_transmission_allowed=False)
        if voice_capture:
            action.update(voice_input_id=voice_input_id,
                voice_input_binding=self.agent_runtime.voice_capture_snapshot(voice_capture),
                local_only=True, cloud_transmission_allowed=False)
            if planned_context_manifest is not None:
                # ModelRequest retains its context mapping. Add private arrival
                # metadata to a new local manifest, never mutate that request.
                planned_context_manifest = {**planned_context_manifest,
                    'voice_input_binding': action['voice_input_binding']}
        if foreground_dialogue_binding and planned_context_manifest is not None and planned_decision:
            planned_context_manifest = {**planned_context_manifest,
                'foreground_independent_dialogue_v1': foreground_dialogue_binding}
            if planned_decision.step.capability.name == 'learning.explain':
                action['tutor_request_action_id'] = 'foreground-question:' + hashlib.sha256(client_action_id.encode()).hexdigest()
        if (planned_decision and planned_decision.step.capability.name in {'task.start','task.complete','diagnostic.start'}
                and input_revision_arrival and planned_context_manifest is not None):
            planned_context_manifest = {**planned_context_manifest,
                'learning_input_arrival_id': input_revision_arrival['arrival_id']}
        if action.get('type') == 'start_next_task':
            start_intent = None
            if planned_decision and 'start_selector' in planned_decision.step.arguments:
                resolved = self.voice_control_intent.accept_planned_task_start(
                    session_id=session_id, child_id=session['child_id'], action_id=client_action_id,
                    transcript=received_transcript, binding=control_targets,
                    call_id=(planned_context_manifest or {}).get('task_start_model_call_id'),
                    selector=planned_decision.step.arguments['start_selector'])
                return self._persist_voice_control(session_id, session['child_id'], session['state'],
                    received_transcript, client_action_id, input_revision_arrival['arrival_id'], resolved, voice_capture)
            if (planned_decision and control_lane is None and current_text_authorized
                    and getattr(self.model_gateway, 'configured', False)):
                # Resolve intent slots, never send the private schedule to a
                # second planner or permit it to substitute another capability.
                start_intent = await self.voice_control_intent.resolve(
                    session_id=session_id, child_id=session['child_id'], action_id=client_action_id,
                    transcript=received_transcript, allowed=current_text_authorized, names=child_names,
                    local_sources=[*field_sources, diagnostic_local_source, *writing_reference_sources,
                        ((mistake_focus or {}).get('state') or {}).get('question_text')],
                    binding=control_targets, voice_capture=voice_capture, pre_dispatch=pre_dispatch,
                    state={'has_pending_question': local_question, 'has_active_exam': bool(active_exam),
                        'has_paused_exam': bool(exam_run_binding and not active_exam),
                        'has_active_task': bool(control_targets['task']), 'has_tutor_playback': bool(tutor_playback_binding),
                        'has_pet_listening': bool(pet_listening_arrival), 'is_study_phase': session['state'] == 'study',
                        'is_break_phase': session['state'] == 'break'})
                if start_intent.get('control') == 'task.start' and start_intent.get('status') == 'control':
                    return self._persist_voice_control(session_id, session['child_id'], session['state'],
                        received_transcript, client_action_id, input_revision_arrival['arrival_id'], start_intent, voice_capture)
                # A failed or contradictory selector cannot fall back to queue first.
                control_targets = {**control_targets, 'task_start': {**control_targets['task_start'],
                    'status': 'blocked', 'reason': 'request_not_start', 'item': None}}
            target = control_targets['task_start']
            permitted = self.voice_control_intent.parse(json.dumps({'role':'control','control':'task.start',
                'quote':received_transcript,'confidence':1}), received_transcript).get('status') == 'control'
            source_gap = self._task_source_clarification(session['child_id'], target) if permitted else None
            if target['status'] != 'ready' or not permitted or source_gap:
                response, action = source_gap or self._task_start_clarification(target, target.get('reason') if permitted else 'request_not_start')
                failure = (start_intent or {}).get('reason')
                service_failed = failure in {'MODEL_UNAVAILABLE', 'CONTROL_MODEL_FAILED', 'CLOUD_TEXT_DISABLED'}
                if service_failed:
                    action['understanding_failure_code'] = failure
                    response = '语言理解服务暂时不可用，不是你说得不清楚。你的话和原计划已保留，这次没有开始任务。'
                intent = 'task_navigation_clarification'
                planned_decision = self._deterministic_action_plan(
                    owner=self.capability_registry.owner_for_action_type('none',session['state']),action=action,response=response)
                planned_decision = replace(planned_decision,requires_child_reply=not service_failed,after_execution='stop',continuation_scope=())
                planned_context_manifest = {'learning_input_arrival_id':input_revision_arrival['arrival_id']}
            else:
                item = target['item']
                planned_context_manifest = {**(planned_context_manifest or {}),'task_start_arrival':target,
                    'task_start_binding':{**{k:item[k] for k in ('plan_item_id','source_type','source_ref')},'operation':'start'}}
        if spoken_answer_binding and action.get('type') == 'submit_diagnostic_answer':
            action['voice_answer_request_key'] = control_lane['request_key']
            planned_context_manifest = {**(planned_context_manifest or {}), 'voice_answer_role_binding': {
                'request_key':control_lane['request_key'], 'target':spoken_answer_binding}}
        if (action.get('type') in {'submit_diagnostic_answer', 'submit_exam_answer',
                'submit_pet_vocabulary_answer', 'evaluate_child_response', 'continue_mistake_dialogue'}
                and (voice_capture or (confirmed_answer or {}).get('source_voice_input_id'))):
            action['source_voice_input_id'] = (confirmed_answer.get('source_voice_input_id')
                                             if confirmed_answer else voice_input_id)
        from .mistake_question_confirmation import bind_action as bind_mistake_confirmation
        formula_confirmation = (bind_mistake_confirmation(mistake_focus, action, received_transcript,
            revision_semantics=(planned_context_manifest or {}).get('mistake_revision_semantics'))
            if session['state'] == 'study' and not active_exam and not active_vocabulary and not pure_speech_echo and not speech_echo_removed else None)
        if formula_confirmation:
            action = {**{k: action[k] for k in ('voice_input_id', 'voice_input_binding', 'source_voice_input_id') if k in action},
                      **formula_confirmation}
            intent, response = 'mistake_dialogue', '我核对一下刚才那道题，然后接着讲解。'
            if formula_confirmation['source'] == 'mistake_question_voice_cancellation':
                response = '我先核对这次更正，再取消提案，保留原错题。'
            elif formula_confirmation['source'] == 'mistake_question_joint_clarification':
                response = '这次有题干和原作答两项更正，还需要一起核对，暂不修改原记录。'
            planned_decision = self._deterministic_action_plan(
                owner=self.capability_registry.owner_for_action_type('continue_mistake_dialogue', session['state']),
                action=action, response=response)
            planned_context_manifest = {'mistake_question_confirmation': formula_confirmation['question_confirmation']}
            if formula_confirmation.get('revision_semantics'):
                planned_context_manifest['mistake_revision_semantics'] = formula_confirmation['revision_semantics']
            preserve_legacy_action = False
        if saved_review_selection and action.get('source') == 'saved_review_selection':
            planned_context_manifest = {**(planned_context_manifest or {}), 'saved_review_selection': saved_review_selection}
        if action.get('type') == 'continue_mistake_dialogue':
            from .mistake_pending_answer import binding as pending_answer_binding
            pending_operation = pending_answer_binding(mistake_focus)
            if pending_operation:
                action['pending_answer_operation'] = pending_operation
            if action.get('scored_answer_control') in {'replace', 'withdraw'}:
                scored = (mistake_focus or {}).get('state', {}).get('last_scored_answer')
                if scored:
                    action['scored_answer_operation'] = scored
        if action.get('type') == 'repair_mistake_question':
            ordinary_text_policy = None
            planned_context_manifest = {**(planned_context_manifest or {}), 'mistake_repair_target': source_repair_target}
            if explicit_mistake_repair:
                planned_decision = self._deterministic_action_plan(
                    owner=self.capability_registry.owner_for_action_type('repair_mistake_question', session['state']),
                    action={'question_quote': action['question_quote']}, response=response)
                preserve_legacy_action = False
            action['local_only'] = True
            action['cloud_transmission_allowed'] = False
        if action.get('type') == 'retry_capture' and source_recapture_target and session['state'] == 'study':
            if explicit_mistake_recapture:
                planned_decision = self._deterministic_action_plan(
                    owner=self.capability_registry.owner_for_action_type('retry_capture', session['state']),
                    action={}, response=response)
                planned_context_manifest = {'mistake_recapture_target': source_recapture_target}
                preserve_legacy_action = False
            ordinary_text_policy = None
            action['mistake_recapture_target'] = source_recapture_target
            action['local_only'] = True
            action['cloud_transmission_allowed'] = False
        action = self.capability_registry.bind_action(action, session["state"])

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT agent_turn_id, session_id, transcript, action_json FROM agent_turn WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if existing:
                connection.rollback()
                self._assert_action_payload(existing, session_id, received_transcript)
                self._assert_replayed_voice(existing, voice_input_id)
                return self._get_replayed(connection, existing["agent_turn_id"])
            if foreground_dialogue_binding or action.get('source') in {'agent_runtime_planner', 'cloud_classifier', 'cloud_companion'}:
                pre_dispatch(connection)
            if action.get('mistake_recapture_target'):
                live = connection.execute('SELECT mistake_id,phase,state_json FROM mistake_dialogue WHERE session_id=? AND child_id=?',
                                          (session_id, session['child_id'])).fetchone()
                focus_now = ({'mistake_id': live['mistake_id'], 'phase': live['phase'], 'state': json.loads(live['state_json']),
                              'child_id': session['child_id']} if live else None)
                if mistake_recapture_target(connection, focus_now) != action['mistake_recapture_target']:
                    raise InvalidAgentPlanError('MISTAKE_RECAPTURE_TARGET_CHANGED')
            if preserves_tutor_question and action.get('type') == 'ask_tutor':
                pre_dispatch(connection)
                if not self.voice_control_intent.tutor_question_current(connection, session_id, session['child_id'],
                        client_action_id, received_transcript, control_lane, tutor_playback_binding):
                    raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            if action.get('voice_answer_request_key'):
                pre_dispatch(connection)
                if self.voice_control_intent.spoken_answer_target(connection, session_id, session['child_id'],
                        client_action_id, received_transcript, action['voice_answer_request_key']) != spoken_answer_binding:
                    raise InvalidAgentPlanError('ANSWER_SPOKEN_ROLE_CHANGED')
            if voice_capture:
                current_capture = self.agent_runtime.resolve_captured_voice(connection,
                    session_id=session_id, child_id=session['child_id'],
                    voice_input_id=voice_input_id, transcript=received_transcript, page_source_store=self.page_source_store)
                if self.agent_runtime.voice_capture_snapshot(current_capture) != self.agent_runtime.voice_capture_snapshot(voice_capture):
                    raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_BINDING_CHANGED')
            if capture_mismatch_notice is not None:
                notice_action = {**action, 'capture_mismatch_speech_notice': capture_mismatch_notice}
                if (intent == 'pending_answer_clarification'
                        and self._capture_mismatch_notice_from_action(notice_action, response) == capture_mismatch_notice):
                    # Admission is serialized with the turn, not a playback receipt.
                    # Preserve each input and safety reply; never grant an answer.
                    action = notice_action
                    prior_notice = connection.execute(
                        """SELECT action_json FROM agent_turn
                           WHERE session_id=? AND child_id=? AND intent='pending_answer_clarification'
                             AND response_text=? ORDER BY rowid DESC LIMIT 1""",
                        (session_id, session['child_id'], CAPTURE_QUESTION_MISMATCH_RESPONSE)).fetchone()
                    if prior_notice:
                        try:
                            prior_action = json.loads(prior_notice['action_json'])
                        except (TypeError, ValueError):
                            prior_action = None
                        if self._capture_mismatch_notice_from_action(prior_action, response) == capture_mismatch_notice:
                            action['suppress_speech'] = True
            candidate = action.get('pending_answer_candidate')
            if (answer_role_notice_scope is not None
                    and intent == 'pending_answer_clarification'
                    and response == ANSWER_ROLE_CLARIFICATION_RESPONSE
                    and type(candidate) is dict and set(candidate) == {'target', 'answer'}
                    and candidate['target'] == answer_target and type(candidate['answer']) is str):
                first_hash = hashlib.sha256(received_transcript.encode('utf-8')).hexdigest()
                role_notice = {'version': 1, 'reason': 'answer_role_clarification',
                    'scope_sha256': answer_role_notice_scope['scope_sha256'],
                    'first_candidate_sha256': first_hash}
                role_action = {**action, ANSWER_ROLE_NOTICE_KEY: role_notice}
                if self._answer_role_notice_from_action(role_action, response) == role_notice:
                    prior_role = connection.execute(
                        """SELECT action_json FROM agent_turn
                           WHERE session_id=? AND child_id=? AND intent='pending_answer_clarification'
                             AND response_text=? ORDER BY rowid DESC LIMIT 1""",
                        (session_id, session['child_id'], ANSWER_ROLE_CLARIFICATION_RESPONSE)).fetchone()
                    if prior_role:
                        try:
                            prior_action = json.loads(prior_role['action_json'])
                        except (TypeError, ValueError):
                            prior_action = None
                        previous = self._answer_role_notice_from_action(prior_action, response)
                        if previous and previous['scope_sha256'] == role_notice['scope_sha256']:
                            role_notice = dict(previous)
                            role_action[ANSWER_ROLE_NOTICE_KEY] = role_notice
                            role_action['suppress_speech'] = True
                            if first_hash != role_notice['first_candidate_sha256']:
                                role_action.pop('pending_answer_candidate', None)
                    action = role_action
            if intent == "clarify_intent" and self._clarification_count(
                connection, session_id, session["state"]
            ) >= MAX_CLARIFICATION_TURNS_PER_STAGE:
                intent, response, action = self._clarification_limit_result(session["state"])
                orchestrator_version = "local_policy_v1"
            if diagnostic_input_binding:
                ordinary_text_policy = None
                action.update(local_only=True, cloud_transmission_allowed=False)
            now = datetime.now(timezone.utc)
            if action.get("type") == "snooze":
                snooze_until = now + timedelta(minutes=int(action.get("minutes", 10)))
                action["until"] = snooze_until.isoformat()
                connection.execute(
                    "UPDATE daily_session SET snooze_until = ?, last_checkpoint_at = ? WHERE session_id = ?",
                    (snooze_until.isoformat(), now.isoformat(), session_id),
                )
            turn_id = str(uuid4())
            from .document_goal_handoff import bind_confirmation_turn
            bind_confirmation_turn(connection, session_id, session['child_id'], turn_id,
                client_action_id, received_transcript, action)
            connection.execute(
                """
                INSERT INTO agent_turn(
                  agent_turn_id, child_id, session_id, transcript, intent, response_text,
                  action_json, orchestrator_version, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session["child_id"],
                    session_id,
                    received_transcript if pure_speech_echo or voice_capture else normalized_transcript,
                    intent,
                    response,
                    json.dumps(action, ensure_ascii=False),
                    orchestrator_version,
                    now.isoformat(),
                    client_action_id,
                ),
            )
            if planned_decision and planned_context_manifest:
                runtime_metadata = self.agent_runtime.persist_plan(
                    connection,
                    child_id=session["child_id"],
                    session_id=session_id,
                    agent_turn_id=turn_id,
                    client_action_id=client_action_id,
                    decision=planned_decision,
                    context_manifest=planned_context_manifest,
                )
                if not preserve_legacy_action:
                    action.update(json.loads(connection.execute(
                        "SELECT action_json FROM agent_turn WHERE agent_turn_id=?", (turn_id,)
                    ).fetchone()[0]))
                    action["agent_runtime"] = runtime_metadata
                    connection.execute(
                        "UPDATE agent_turn SET action_json = ? WHERE agent_turn_id = ?",
                        (json.dumps(action, ensure_ascii=False), turn_id),
                    )
            if ordinary_text_policy is not None:
                from .ordinary_learning_text_projection import mint_input_policy
                mint_input_policy(connection, turn_id, ordinary_text_policy)
            persist_safety_event(
                connection, session["child_id"], session_id, turn_id, action, now.isoformat()
            )
            from .public_reply_projection import bind as bind_public_reply
            bind_public_reply(connection, turn_id, (planned_context_manifest or {}).get('public_reply_model_call_id'))
            connection.commit()
            return self._get(connection, turn_id)

    @staticmethod
    def _direct_reflection_command(text: str, phrases: tuple[str, ...]) -> bool:
        # Closing a day is an explicit whole command, never a quoted or negated clause.
        command = re.sub(r"[\s，。！？、,.!?;；]+", "", text)
        choices = "|".join(re.escape(value) for value in phrases)
        return bool(re.fullmatch(r"(?:(?:请你|请|现在|帮我|我们|咱们)){0,3}(?:"
                                 + choices + r")(?:(?:吧|谢谢|谢谢你)){0,2}", command))

    @staticmethod
    def _direct_controlled_stop(transcript):
        # Whole utterances only: quoted, conditional, negated and mixed commands
        # still require disambiguation. Never strip words to manufacture consent.
        from .controlled_local_cancel import direct_kind
        return direct_kind(transcript)

    def _persist_controlled_stop(self, session_id, child_id, state, transcript, key,
                                 arrival_id, kind, voice_capture):
        table, identity = (('controlled_content_session', 'content_session_id') if kind == 'content'
                           else ('controlled_game_session', 'game_session_id'))
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                self._assert_action_payload(old, session_id, transcript)
                self._assert_replayed_voice(old, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, old['agent_turn_id'])
            if not self.voice_control_intent.scope_valid(c, session_id, child_id, key, require_cloud=False):
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            arrival = c.execute('''SELECT utterance FROM learning_input_arrival
                WHERE arrival_id=? AND client_action_id=? AND child_id=? AND session_id=?''',
                (arrival_id, key, child_id, session_id)).fetchone()
            if not arrival or arrival[0] != transcript or self._direct_controlled_stop(transcript) != kind:
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            rows = c.execute(f'''SELECT {identity},status,started_at FROM {table}
                WHERE child_id=? AND session_id=? ORDER BY rowid DESC''', (child_id, session_id)).fetchall()
            active = [row for row in rows if row['status'] == 'active']
            # A single historical resource may be awaiting an exact close/end
            # receipt. Its lifecycle status is not proof that the native closed.
            target = active[0] if len(active) == 1 else rows[0] if not active and len(rows) == 1 else None
            if voice_capture:
                from .voice_capture_binding import capture_started_at
                captured = capture_started_at(c, voice_capture['voice_input_id'])
                # Companion focus is not authority over an external resource.
                # Unfocused audio also cannot bind a resource created after ASR arrival.
                if (voice_capture.get('activity_reference') or not captured or any(
                        datetime.fromisoformat(row['started_at']) > datetime.fromisoformat(captured) for row in rows)):
                    target = None
            action = {'type': 'none', 'source': 'local_controlled_stop_v1', 'local_only': True,
                      'cloud_transmission_allowed': False, 'mastery_update_allowed': False}
            response = '还不能确定要核对哪一个原活动，暂未关闭或结算。原任务保持不变。'
            manifest = {'learning_input_arrival_id': arrival_id}
            from .controlled_launch_cancel import failed_starts, resolve as resolve_cancel
            failures = failed_starts(c, child_id, session_id, kind) if not active else []
            cancel_target = None
            if failures:
                # An unresolved request takes precedence over a sole ended row,
                # but ambiguity/late capture never falls back to that old resource.
                target = None
                try:
                    cancel_target = resolve_cancel(c, child_id, session_id, kind, failures, voice_capture)
                except ValueError:
                    pass
            if cancel_target:
                from .controlled_local_cancel import action_fields
                action.update(type='stop_controlled_content' if kind == 'content' else 'stop_controlled_game',
                              **action_fields(cancel_target, kind))
                manifest['local_controlled_launch_cancel'] = cancel_target
                response = '我先核对并取消原启动请求，尚不认定已经关闭，也不恢复学习计时。'
            elif target:
                binding = {'child_id': child_id, 'session_id': session_id, 'kind': kind,
                           'activity_id': target[identity]}
                action.update(type='stop_controlled_content' if kind == 'content' else 'stop_controlled_game',
                              controlled_stop_binding=binding, **{identity: target[identity]})
                action['controlled_stop_readback'] = {**binding, 'status': target['status'],
                                                     'started_at': target['started_at']}
                manifest['controlled_stop_binding'] = {**binding, 'observed_status': target['status']}
                response = '我先核对并关闭原活动。关闭结果确认前，不算结束，也不恢复学习计时。'
            action = self.capability_registry.bind_action(action, state)
            decision = self._deterministic_action_plan(
                owner=self.capability_registry.owner_for_action_type(action['type'], state),
                action=action, response=response)
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            tid = str(uuid4())
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (tid, child_id, session_id, transcript, action['type'], response,
                 json.dumps(action, ensure_ascii=False), 'local_controlled_stop_v1', utc_now(), key))
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id,
                agent_turn_id=tid, client_action_id=key, decision=decision, context_manifest=manifest)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata), tid))
            c.commit()
            return self._get(c, tid)

    def _freeze_local_voice(self, connection, session_id, child_id, transcript, capture, action, manifest):
        if capture is None:
            return
        current = self.agent_runtime.resolve_captured_voice(connection, session_id=session_id,
            child_id=child_id, voice_input_id=capture['voice_input_id'], transcript=transcript, page_source_store=self.page_source_store)
        snapshot = self.agent_runtime.voice_capture_snapshot(current)
        if snapshot != self.agent_runtime.voice_capture_snapshot(capture):
            raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_BINDING_CHANGED')
        action.update(voice_input_id=capture['voice_input_id'], voice_input_binding=snapshot,
                      local_only=True, cloud_transmission_allowed=False)
        manifest['voice_input_binding'] = snapshot

    def _persist_overlap_clarification(self, session_id, child_id, state, transcript, key, playback_id, voice_capture, *, pending_evaluation=None):
        response = '这句话与你刚才听到的内容重叠，可能是跟读，也可能混入了播放声音。我已保留原话，不会把它直接算作独立作答。你是在跟读、回答，还是想让我做什么？'
        action = {'type': 'none', 'source': 'playback_overlap_clarification', 'local_only': True,
            'cloud_transmission_allowed': False, 'independent_evidence': False, 'mastery_update_allowed': False,
            'playback_overlap_id': playback_id, 'echo_candidate': True,
            'echo_source_verified': False, 'original_utterance_retained': True}
        manifest = {'playback_overlap_policy': 'text_overlap_is_not_acoustic_source_evidence',
                    'local_only': True, 'cloud_transmission_allowed': False}
        if pending_evaluation:
            from .pending_evaluation_control import marker
            action['pending_evaluation_control'] = marker(pending_evaluation)
            manifest.update(pending_evaluation=pending_evaluation, pending_local_control='playback_overlap')
        decision = self._deterministic_action_plan(owner=self.capability_registry.owner_for_action_type('none', state),
                                                   action=action, response=response)
        if pending_evaluation:
            decision = replace(decision, objective=ObjectiveDraft('none',None,None,(),'execute'),
                               after_execution='stop', continuation_scope=())
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if pending_evaluation:
                from .pending_evaluation_control import revalidate
                revalidate(c, pending_evaluation, require_consent=False)
            existing = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if existing:
                self._assert_action_payload(existing, session_id, transcript)
                self._assert_replayed_voice(existing, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, existing['agent_turn_id'])
            turn_id = str(uuid4())
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (turn_id, child_id, session_id, transcript, 'playback_overlap_clarification', response,
                 json.dumps(action, ensure_ascii=False), 'voice_overlap_retention_v1', utc_now(), key))
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id,
                agent_turn_id=turn_id, client_action_id=key, decision=decision, context_manifest=manifest)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata), turn_id))
            c.commit()
            return self._get(c, turn_id)

    def _persist_writing_voice(self, session_id, child_id, state, transcript, key, arrival_id,
                               snapshot, control, voice_capture, *, span=None, semantic=None):
        from .writing_ocr_authorization import WritingOcrAuthorizationService, arrival_binding, _document
        action = {'type': 'none', 'source': 'writing_scope_clarification', 'local_only': True,
                  'cloud_transmission_allowed': False, 'mastery_update_allowed': False}
        response = '请说清作答从哪一句开始、到哪一句结束。如果这页只有你写的答案，也可以直接告诉我；范围分不开可以只拍作答部分。'
        manifest = {'learning_input_arrival_id': arrival_id}
        scope = {'prompt': snapshot['prompt'], 'page': {k:v for k,v in (snapshot.get('page') or {}).items() if k != 'extracted_text'}}
        manifest['writing_scope_binding'] = scope
        if control == 'writing.correct_ocr':
            manifest.pop('writing_scope_binding')
            response = '请先听完本页完整读回。告诉我哪段读错了，纸上实际写的是什么；如果同样的字出现不止一次，请连前后几个字一起说。看不清可以重拍。'
            action['source'] = 'writing_correction_clarification'
        elif control == 'writing.read_original' and span:
            page = snapshot['page']
            with self.database.connect() as c:
                source = c.execute('''SELECT content_sha256 FROM document_page_revision r WHERE revision_id=? AND child_id=? AND session_id=?
                    AND revision_index=(SELECT MAX(r2.revision_index) FROM document_page_revision r2 WHERE r2.monitor_id=r.monitor_id)''',
                    (page['revision_id'], child_id, session_id)).fetchone()
                if not source or source[0] != page['content_sha256']:
                    raise InvalidAgentPlanError('ANSWER_WRITING_SOURCE_CHANGED')
                _document(c, child_id, session_id, page['artifact_id'], page['revision_id'], *span, review=True)
            packet = WritingOcrAuthorizationService(self.database).prepare_confirmation(session_id,
                **snapshot['prompt'], artifact_id=page['artifact_id'], revision_id=page['revision_id'],
                response_start=span[0], response_end=span[1], client_action_id='writing-read:' + key)
            action.update(type='read_writing_original', request_quote=transcript,
                writing_ocr_confirmation={k:packet[k] for k in ('confirmation_id', 'binding_version')})
            response = '我会按原题读回你指定的书面作答，请核对是否准确。'
            manifest.pop('writing_scope_binding')
            manifest['writing_voice_binding'] = {'request_quote': transcript, 'confirmation': action['writing_ocr_confirmation']}
        elif control == 'writing.read_original' and snapshot.get('confirmation'):
            bound = {k:snapshot['confirmation'][k] for k in ('confirmation_id','binding_version')}
            action.update(type='read_writing_original', request_quote=transcript, writing_ocr_confirmation=bound)
            response = '重新读回本页当前待核对的完整稿，原稿和纠正内容都保留。请听完后再确认正文。'
            manifest.pop('writing_scope_binding')
            manifest['writing_voice_binding'] = {'request_quote': transcript, 'confirmation': bound}
        elif snapshot.get('followup') and semantic:
            packet = snapshot['followup']
            manifest.pop('writing_scope_binding')
            if packet['status'] == 'ready':
                target = {k:packet[k] for k in ('action','binding_version')}
                action.update(type='revise_writing_original' if control == 'writing.revise_original' else 'check_writing_independently',
                    source='writing_followup_command',request_quote=transcript,writing_followup_binding=target,
                    writing_followup_semantic_receipt={'request_key':semantic['request_key']})
                manifest['writing_voice_binding'] = {'request_quote':transcript,'followup':target}
                response = ('按这次正式反馈修改原作答，新稿另存，不算独立复测。' if control == 'writing.revise_original'
                            else '准备另一道有可靠题源的写作题，请独立作答。出题本身不算通过。')
            else:
                action.update(source='writing_followup_clarification',reason=packet.get('reason'))
                response = '当前没有符合来源和独立性要求的新写作题，我不会回用旧题或临时编题算复测。可以继续讨论这份反馈。'
        elif control == 'writing.revise_original' and snapshot.get('revision') and semantic:
            action.update(type='revise_writing_original', request_quote=transcript,
                writing_revision_binding=snapshot['revision'], writing_revision_semantic_receipt={'request_key': semantic['request_key']})
            manifest.pop('writing_scope_binding')
            manifest['writing_voice_binding'] = {'request_quote': transcript, 'revision': snapshot['revision']}
            response = '按这份原稿开始修改。原稿会保留，新稿另存；这次修改不算独立复测。'
        elif control == 'writing.confirm_original' and snapshot.get('confirmation') and semantic:
            from .writing_ocr_review import ready_to_confirm
            with self.database.connect() as c:
                ready = ready_to_confirm(c, session_id, snapshot['confirmation'], transcript, semantic)
            if ready:
                action.update(type='confirm_writing_original', request_quote=transcript,
                    writing_ocr_binding=snapshot['confirmation'],
                    writing_ocr_semantic_receipt={'request_key': semantic['request_key']})
            manifest.pop('writing_scope_binding')
            manifest['writing_voice_binding'] = {'request_quote': transcript, 'confirmation': snapshot['confirmation']}
            response = ('我核对这次原稿使用确认；这句话不会作为英语答案评分。' if ready else
                '请先听完本页当前稿的完整读回，再确认这是你自己写的本题答案且识别文字正确。仅确认范围不能提交；可以请我再读一次。')
        elif snapshot['pending_confirmation']:
            action['source'] = 'writing_confirmation_clarification'
            manifest.pop('writing_scope_binding')
            response = '刚读回的是你自己写的本题作答，文字也没有错，对吗？如果读错了，请告诉我是哪段、纸上实际写的是什么。看不清可以重拍；核对的话不会当英语答案。'
        if action['type'] != 'none' and action.get('source') == 'writing_scope_clarification':
            action['source'] = 'writing_voice_command'
        decision = self._deterministic_action_plan(owner=self.capability_registry.owner_for_action_type(action['type'], state),
                                                   action=action, response=response)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                self._assert_action_payload(old, session_id, transcript)
                self._assert_replayed_voice(old, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, old['agent_turn_id'])
            current = c.execute('SELECT client_action_id FROM learning_input_arrival WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                                (session_id, child_id)).fetchone()
            if not current or current[0] != key:
                raise InvalidAgentPlanError('ANSWER_WRITING_ARRIVAL_CHANGED')
            if control == 'writing.correct_ocr' and snapshot.get('confirmation'):
                from .writing_ocr_review import prepare_correction
                c.execute('SAVEPOINT writing_correction')
                try:
                    packet = prepare_correction(WritingOcrAuthorizationService(self.database), c, session_id,
                                                snapshot['confirmation'], transcript, 'writing-correct:' + key, semantic)
                    action.update(type='read_writing_original', source='writing_voice_command', request_quote=transcript,
                        writing_ocr_confirmation={k:packet[k] for k in ('confirmation_id', 'binding_version')})
                    manifest['writing_voice_binding'] = {'request_quote': transcript, 'confirmation': action['writing_ocr_confirmation']}
                    response = '识别更正已保留。现在重新读完整稿，请再次核对正文；刚才的读回不能替这份更正稿确认。'
                except ValueError as error:
                    c.execute('ROLLBACK TO writing_correction')
                    if not str(error).startswith('WRITING_'):
                        raise
                c.execute('RELEASE writing_correction')
                decision = self._deterministic_action_plan(owner=self.capability_registry.owner_for_action_type(action['type'], state),
                                                           action=action, response=response)
            if action['type'] == 'confirm_writing_original':
                b = snapshot['confirmation']
                actual = arrival_binding(c, session_id=session_id, confirmation_id=b['confirmation_id'],
                    binding_version=b['binding_version'], voice_input_id=voice_capture['voice_input_id'])
                if actual != b:
                    raise InvalidAgentPlanError('ANSWER_WRITING_SOURCE_CHANGED')
            turn = str(uuid4())
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (turn, child_id, session_id, transcript, control or 'writing_clarification', response,
                 json.dumps(action, ensure_ascii=False), 'writing_voice_v1', utc_now(), key))
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id,
                agent_turn_id=turn, client_action_id=key, decision=decision, context_manifest=manifest)
            if action['type'] == 'read_writing_original':
                goal = self.agent_runtime.bind_writing_learning_goal(c, turn, action['writing_ocr_confirmation'])
                if goal:
                    metadata['objective_id'] = goal['objective_id']
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata), turn))
            c.commit()
            return self._get(c, turn)

    async def _persist_input_companion_reply(self, session_id, child_id, state, transcript, key, arrival_id,
                                            decision, voice_capture, names, local_sources, input_guard):
        from .voice_control_intent import VoiceControlIntent
        from .learning_input_return import validate as validate_draft
        from .input_companion_dialogue import validate as validate_dialogue, _object
        binding = decision['binding']
        request_key = decision['request_key']
        draft = binding['input_return']
        previous = binding.get('input_companion')
        expected = ({'status': 'control', 'control': 'companion.chat'} if decision.get('control') == 'companion.chat'
                    else {'status': 'non_control', 'role': 'companion_reply'})
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()

        def inspect(c):
            input_guard(c)
            VoiceControlIntent.assert_current_input(c, session_id, child_id, key, transcript, voice_capture)
            if (not VoiceControlIntent.scope_valid(c, session_id, child_id, key)
                    or binding.get('input_return_consent') != list(VoiceControlIntent.text_consent_version(c) or ())):
                raise InvalidAgentPlanError('INPUT_COMPANION_CONSENT_CHANGED')
            validate_draft(c, child_id, session_id, draft)
            history = validate_dialogue(c, child_id, session_id, previous) if previous else ()
            journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=? AND child_id=? AND session_id=?',
                                (request_key, child_id, session_id)).fetchone()
            if (not journal or journal['status'] != 'completed' or journal['fingerprint'] != fingerprint
                    or _object(journal['decision_json']) != expected or _object(journal['binding_json']) != binding):
                raise InvalidAgentPlanError('INPUT_COMPANION_JOURNAL_CHANGED')
            if not all(VoiceControlIntent.text_eligible(text, names, local_sources)
                       for text in [transcript, *[message['content'] for message in history]]):
                raise InvalidAgentPlanError('INPUT_COMPANION_LOCAL_SOURCE')
            cached = _object(journal['execution_receipt_json'])
            if journal['execution_receipt_json'] and not cached:
                raise InvalidAgentPlanError('INPUT_COMPANION_CONTEXT_CHANGED')
            return history, cached

        def verify(c):
            try:
                return inspect(c)
            except ValueError as error:
                raise InvalidAgentPlanError('INPUT_COMPANION_CONTEXT_CHANGED') from error

        def authorize():
            with self.database.connect() as c:
                c.execute('BEGIN')
                verify(c)

        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            history, cached = verify(c)
            if cached.get('kind') == 'input_companion_response_pending_v1':
                raise InvalidAgentPlanError('INPUT_COMPANION_RESPONSE_PENDING')
            if cached and (cached.get('kind') != 'input_companion_response_v1'
                           or cached.get('binding') != binding
                           or not isinstance(cached.get('response'), str) or not isinstance(cached.get('source'), str)):
                raise InvalidAgentPlanError('INPUT_COMPANION_CONTEXT_CHANGED')
            if not cached:
                c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
                          (json.dumps({'kind': 'input_companion_response_pending_v1', 'binding': binding}), request_key))
            c.commit()
        if cached.get('kind') == 'input_companion_response_v1' and cached.get('binding') == binding:
            response, source = cached['response'], cached['source']
        else:
            reply = await self._generate_companion_reply(session_id=session_id, child_id=child_id,
                state=state, transcript=transcript, previous_response=None, known_child_names=names,
                recent_dialogue=history, pre_dispatch=authorize, activity_context={}, auto_advance=False)
            if reply:
                response, source = reply.text, reply.source
            else:
                response, source = ('这次没能连上对话服务，刚才的话和作业草稿都保留了。可以再说一次，或先回到作业。',
                                    'input_companion_unavailable')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                self._assert_action_payload(old, session_id, transcript)
                self._assert_replayed_voice(old, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, old['agent_turn_id'])
            verify(c)
            # Persist the generated response and local focus together; replay must
            # not regenerate text or turn a conversation into a homework answer.
            c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
                      (json.dumps({'kind': 'input_companion_response_v1', 'binding': binding,
                                   'response': response, 'source': source}, ensure_ascii=False), request_key))
            action = {'type': 'none', 'source': source, 'local_only': True,
                      'cloud_transmission_allowed': False, 'independent_evidence': False}
            manifest = {'learning_input_arrival_id': arrival_id, 'input_companion_dialogue': {
                'root_request_id': draft['request_id'], 'source_version': draft['source_version'],
                'consent': binding['input_return_consent'], 'request_key': request_key,
                'previous_turn_id': previous['last_turn_id'] if previous else None}}
            turn, now = str(uuid4()), utc_now()
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id,tool_status,completed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'completed',?)''',
                (turn, child_id, session_id, transcript, 'input_companion_dialogue', response,
                 json.dumps(action, ensure_ascii=False), 'input_companion_dialogue_v1', now, key, now))
            planned = self._deterministic_action_plan(
                owner=self.capability_registry.owner_for_action_type('none', state), action=action, response=response)
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id, agent_turn_id=turn,
                client_action_id=key, decision=replace(planned, requires_child_reply=True, after_execution='stop',
                                                     continuation_scope=()), context_manifest=manifest)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata), turn))
            self.agent_runtime.complete_turn_execution(c, turn, True, None, {'response_text': response})
            result = self._get(c, turn)
            c.commit()
            return result

    async def _persist_activity_reply(self, session_id, child_id, state, transcript, key, arrival_id,
                                      decision, voice_capture, names, local_sources, input_guard, *, pending_evaluation=None):
        from .voice_control_intent import VoiceControlIntent
        binding = (decision.get('binding') or {}).get('activity_focus')
        request_key = decision.get('request_key')
        response = '原题和活动都保留。这句话属于哪一项？我先不记作答，也不推进活动。'
        source = 'activity_focus_clarification'
        activity = None
        consent = None

        def verify(c):
            input_guard(c)
            if VoiceControlIntent.text_consent_version(c) != consent:
                raise ValueError('CONTROL_CONSENT_CHANGED')
            VoiceControlIntent.assert_current_input(c, session_id, child_id, key, transcript, voice_capture)
            if (not voice_capture or voice_capture.get('activity_reference') != binding
                    or voice_capture.get('activity_validation') != 'valid'):
                raise ValueError('CONTROL_ACTIVITY_FOCUS_CHANGED')
            current = VoiceControlIntent.validate_activity_focus(c, session_id, child_id, binding)
            journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (request_key,)).fetchone()
            expected = {'status': 'non_control', 'role': 'activity_reply'}
            fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
            if (not journal or journal['status'] != 'completed' or journal['fingerprint'] != fingerprint
                    or (journal['child_id'], journal['session_id']) != (child_id, session_id)
                    or json.loads(journal['decision_json']) != expected
                    or json.loads(journal['binding_json']) != decision.get('binding')):
                raise ValueError('CONTROL_JOURNAL_CHANGED')
            return current, journal

        def authorize():
            with self.database.connect() as c:
                c.execute('BEGIN')
                verify(c)

        eligible = (decision.get('status') == 'non_control' and decision.get('role') == 'activity_reply'
                    and binding and self.voice_control_intent.text_eligible(transcript, names, local_sources)
                    and not re.search(r'(?:不想|不要|不用|先别|停止|结束|假如|假设)|(?:回|继续).{0,8}(?:题|学习|任务)|[“”「」『』"]', transcript))
        if eligible:
            try:
                with self.database.connect() as c:
                    c.execute('BEGIN')
                    frozen_consent = (decision.get('binding') or {}).get('activity_text_consent')
                    consent = tuple(frozen_consent) if frozen_consent else None
                    activity, journal = verify(c)
                    cached = json.loads(journal['execution_receipt_json'] or '{}')
                    previous = c.execute('SELECT response_text FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                        (binding['last_turn_id'], child_id, session_id)).fetchone()[0]
                    if activity['turn_count'] == 0:
                        stored = c.execute('SELECT result_json FROM agent_step WHERE idempotency_key=?',
                                           (activity['client_action_id'],)).fetchone()
                        previous = json.loads(stored[0])['response_text']
                if cached.get('kind') == 'activity_reply_response_v1' and cached.get('binding') == binding:
                    response, source = cached['response'], cached['source']
                else:
                    reply = await self._generate_companion_reply(session_id=session_id, child_id=child_id,
                        state=state, transcript=transcript, previous_response=previous,
                        known_child_names=names, recent_dialogue=(), pre_dispatch=authorize,
                        activity_context={k: activity[k] for k in ('activity_type', 'status', 'turn_count', 'max_turns')} |
                            {'remaining_turns': activity['max_turns'] - activity['turn_count']}, auto_advance=False)
                    if not reply or reply.source != 'cloud_companion':
                        activity = None
                    else:
                        response, source = reply.text, reply.source
                        with self.database.connect() as c:
                            c.execute('BEGIN IMMEDIATE')
                            verify(c)
                            c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
                                (json.dumps({'kind': 'activity_reply_response_v1', 'binding': binding,
                                    'response': response, 'source': source}, ensure_ascii=False), request_key))
                            c.commit()
            except ValueError:
                activity = None
                response, source = '这轮活动或原话来源已变化。我没有推进活动，也没有提交原题答案。', 'activity_focus_changed'

        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if pending_evaluation:
                input_guard(c)
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                self._assert_action_payload(old, session_id, transcript)
                self._assert_replayed_voice(old, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, old['agent_turn_id'])
            if activity:
                try:
                    activity, _ = verify(c)
                except ValueError as error:
                    raise InvalidAgentPlanError('ANSWER_ACTIVITY_FOCUS_CHANGED') from error
            if not VoiceControlIntent.scope_valid(c, session_id, child_id, key, require_cloud=False):
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            action = {'type': 'none', 'source': source, 'local_only': True,
                      'cloud_transmission_allowed': False, 'independent_evidence': False}
            manifest = {'learning_input_arrival_id': arrival_id}
            if pending_evaluation:
                from .pending_evaluation_control import marker
                manifest['pending_evaluation'] = pending_evaluation
                action['pending_evaluation_control'] = marker(pending_evaluation)
            if activity:
                count = activity['turn_count'] + 1
                done = count >= activity['max_turns']
                now = utc_now()
                updated = c.execute('''UPDATE companion_activity SET turn_count=?,status=?,updated_at=?,ended_at=?,end_reason=?
                    WHERE activity_id=? AND status='active' AND turn_count=?''',
                    (count, 'completed' if done else 'active', now, now if done else None,
                     'turn_limit' if done else None, activity['activity_id'], activity['turn_count']))
                if updated.rowcount != 1:
                    raise InvalidAgentPlanError('ANSWER_ACTIVITY_FOCUS_CHANGED')
                manifest['companion_activity_reply'] = {**binding, 'turn_count': count, 'request_key': request_key}
                if done:
                    response += ' 这轮活动到这里。感觉怎么样？想回到原来的学习，还是先休息？'
            turn = str(uuid4())
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id,tool_status,completed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'completed',?)''',
                (turn, child_id, session_id, transcript, 'companion_activity_reply', response,
                 json.dumps(action, ensure_ascii=False), 'content_free_activity_reply_v1', utc_now(), key, utc_now()))
            planned = self._deterministic_action_plan(
                owner=self.capability_registry.owner_for_action_type('none', state), action=action, response=response)
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id, agent_turn_id=turn,
                client_action_id=key, decision=replace(planned, requires_child_reply=True), context_manifest=manifest)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata), turn))
            self.agent_runtime.complete_turn_execution(c, turn, True, None, {'response_text': response})
            result = self._get(c, turn)
            result['activity_focus'] = VoiceControlIntent.activity_focus(c, session_id, child_id)
            c.commit()
            return result

    def _persist_oral_cancel(self,session_id,child_id,state,transcript,key,arrival_id,target,voice_capture):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM agent_turn WHERE client_action_id=?',(key,)).fetchone()
            if old:
                self._assert_action_payload(old,session_id,transcript)
                self._assert_replayed_voice(old,(voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c,old['agent_turn_id'])
            row=c.execute('''SELECT q.*,p.objective_id FROM diagnostic_prompt q
                JOIN agent_plan p ON p.agent_turn_id=json_extract(q.request_json,'$.oral_authorized_turn_id')
                JOIN agent_objective o ON o.objective_id=p.objective_id AND o.child_id=p.child_id AND o.session_id=p.session_id
                WHERE q.prompt_id=? AND q.item_version=? AND q.child_id=? AND q.origin_session_id=?
                AND p.child_id=q.child_id AND p.session_id=q.origin_session_id AND o.status='active'
                AND q.status IN ('waiting_answer','answer_saved')
                AND q.rowid=(SELECT MAX(rowid) FROM diagnostic_prompt WHERE origin_session_id=?)''',
                (target.get('prompt_id'),target.get('item_version'),child_id,session_id,session_id)).fetchone()
            if not row:
                return None
            current=c.execute("SELECT objective_id FROM agent_objective WHERE child_id=? AND status='active'",(child_id,)).fetchall()
            if len(current)!=1 or current[0][0]!=row['objective_id']:
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_GOAL_CHANGED')
            if not self.voice_control_intent.scope_valid(c,session_id,child_id,key,require_cloud=False):
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_COMMAND_CHANGED')
            arrival=c.execute('SELECT utterance FROM learning_input_arrival WHERE arrival_id=? AND client_action_id=? AND child_id=? AND session_id=?',
                (arrival_id,key,child_id,session_id)).fetchone()
            if not arrival or arrival[0]!=transcript:
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_COMMAND_CHANGED')
            if voice_capture:
                captured=self.agent_runtime.captured_question(voice_capture)
                if not captured or captured['kind']!='diagnostic' or captured['binding']!={'prompt_id':row['prompt_id'],'item_version':row['item_version']}:
                    raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_VOICE_CHANGED')
            action={'type':'none','source':'oral_followup_cancel','local_only':True,'cloud_transmission_allowed':False}
            response='好，这次先不答了。刚才的原回答会保留。'
            decision=self._deterministic_action_plan(owner=self.capability_registry.require('conversation.respond',state),
                action=action,response=response,objective_mode='cancel',objective_phase='child_changed_direction')
            manifest={'learning_input_arrival_id':arrival_id,'oral_cancel_binding':{
                'prompt_id':row['prompt_id'],'item_version':row['item_version'],'objective_id':row['objective_id'],
                'local_only':True,'cloud_transmission_allowed':False}}
            self._freeze_local_voice(c,session_id,child_id,transcript,voice_capture,action,manifest)
            tid=str(uuid4())
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (tid,child_id,session_id,transcript,'oral_followup_cancel',response,json.dumps(action,ensure_ascii=False),
                 'oral_followup_v1',utc_now(),key))
            metadata=self.agent_runtime.persist_plan(c,child_id=child_id,session_id=session_id,agent_turn_id=tid,
                client_action_id=key,decision=decision,context_manifest=manifest)
            original_turn=json.loads(row['request_json'])['oral_authorized_turn_id']
            c.execute("""UPDATE agent_plan SET status='cancelled',updated_at=? WHERE agent_turn_id=?
                AND objective_id=? AND child_id=? AND session_id=? AND status IN ('planned','running','waiting_child','failed')""",
                (utc_now(),original_turn,row['objective_id'],child_id,session_id))
            c.execute("""UPDATE agent_step SET status='cancelled',updated_at=? WHERE plan_id IN (
                SELECT plan_id FROM agent_plan WHERE agent_turn_id=? AND status='cancelled')
                AND status IN ('planned','running','waiting_child','failed')""",(utc_now(),original_turn))
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",(json.dumps(metadata),tid))
            c.commit()
            return self._get(c,tid)

    @staticmethod
    def _task_start_clarification(target, reason):
        from .task_start_choices import offer, offer_text
        value = offer(target) if reason == 'requested_task_ambiguous' else None
        action = {'type': 'none', 'source': 'task_navigation_clarification',
                  'local_only': True, 'cloud_transmission_allowed': False}
        if value:
            action['task_start_offer'] = value
        return (offer_text(value) if value else AgentOrchestrator._task_navigation_response(reason)), action

    def _task_source_clarification(self, child_id, target):
        from .task_source_recovery import source_gap, clarification
        with self.database.connect() as c:
            value = source_gap(c, child_id, target)
        return clarification(value) if value else None

    @staticmethod
    def _task_navigation_response(reason):
        return {
            'active_task':'当前这项还在进行，我不会替你结束或跳到下一项。你是要继续做，还是需要帮助？',
            'paused_task':'还有暂停的原任务。要回到它，还是先说明怎么处理？我没有跳过它。',
            'pending_question':'原题还没有处理完，我先保留它，不另开一道题。你想继续原题还是核对安排？',
            'interaction_focus':'刚才的讲解、听题或订正还在。你想接着哪一部分？我先保留原处，不切换任务或开始计时。',
            'no_task':'今天没有等待开始的任务。我没有开始计时，可以先核对今天的安排。',
            'requested_task_ambiguous':'你提到的任务还不能唯一确定。请告诉我是哪个科目、做作业还是复习错题；原计划保留，我没有开始别的任务。',
            'requested_task_missing':'今天的待办里没有找到你说的那项。我没有改成其他任务，可以先一起核对今天的安排。',
            'planned_break_first':'当前安排先休息。你指定的任务我保留着，休息结束再开始，不会悄悄跳过休息。',
            'task_start_choice_expired':'刚才录音对应的任务列表已经不能确认，我没有开始别的任务。请重新告诉我想做哪个科目或哪类任务。',
            'task_start_choice_out_of_range':'刚才列出的任务里没有这个序号，我没有开始计时。请重新说想做哪个科目或哪类任务。',
            'request_not_start':'我不会据此开始或结束任务。你可以继续说明哪里还没做好，或告诉我想怎样安排。',
        }.get(reason,'我还不能确定要开始哪项，原计划和进度保留，没有开始计时。')

    def _persist_spoken_question_intake(
        self,
        *,
        session_id: str,
        child_id: str,
        state: str,
        transcript: str,
        key: str,
        arrival_id: str,
        voice_capture: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT agent_turn_id,session_id,transcript,action_json FROM agent_turn WHERE client_action_id=?",
                (key,),
            ).fetchone()
            if existing:
                connection.rollback()
                self._assert_action_payload(existing, session_id, transcript)
                self._assert_replayed_voice(existing, (voice_capture or {}).get("voice_input_id"))
                return self._get_replayed(connection, existing["agent_turn_id"])

            result = self.spoken_question_intake.handle(
                connection,
                child_id=child_id,
                session_id=session_id,
                transcript=transcript,
                client_action_id=key,
            )
            if result is None:
                connection.rollback()
                raise InvalidAgentPlanError("SPOKEN_QUESTION_INTAKE_CONTEXT_CHANGED")

            action = self.capability_registry.bind_action(dict(result["action"]), state)
            owner = self.capability_registry.owner_for_action_type(
                action["type"], state, arguments=action,
            )
            action.update(
                capability=owner.name,
                execution_location=owner.execution_location,
                risk_level=owner.risk_level,
            )
            decision = self._deterministic_action_plan(
                owner=owner,
                action=action,
                response=result["response"],
            )
            decision = replace(decision, requires_child_reply=bool(result["requires_child_reply"]))
            if action["type"] == "ask_tutor":
                subject = str(action.get("subject") or "未知学科")
                decision = replace(
                    decision,
                    objective=ObjectiveDraft(
                        mode="new",
                        objective_type="learning",
                        summary=f"理解并独立解决口述的{subject}题",
                        success_criteria=(
                            "能用自己的话说明关键思路",
                            "能独立完成一道近迁移检查",
                        ),
                        phase="teach",
                    ),
                )

            now = datetime.now(timezone.utc).isoformat()
            turn_id = str(uuid4())
            connection.execute(
                """INSERT INTO agent_turn(
                     agent_turn_id,child_id,session_id,transcript,intent,response_text,
                     action_json,orchestrator_version,created_at,client_action_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    turn_id, child_id, session_id, transcript, result["intent"], result["response"],
                    json.dumps(action, ensure_ascii=False), "spoken_question_intake_v1", now, key,
                ),
            )
            self.spoken_question_intake.bind_agent_turn(connection, key, turn_id)
            runtime_metadata = self.agent_runtime.persist_plan(
                connection,
                child_id=child_id,
                session_id=session_id,
                agent_turn_id=turn_id,
                client_action_id=key,
                decision=decision,
                context_manifest={
                    "learning_input_arrival_id": arrival_id,
                    "spoken_question_intake": result["intake"],
                    "voice_input_id": (voice_capture or {}).get("voice_input_id"),
                    "local_only": action["type"] != "ask_tutor",
                },
            )
            persisted_action = json.loads(connection.execute(
                "SELECT action_json FROM agent_turn WHERE agent_turn_id=?", (turn_id,),
            ).fetchone()[0])
            action.update(persisted_action)
            action["agent_runtime"] = runtime_metadata
            connection.execute(
                "UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?",
                (json.dumps(action, ensure_ascii=False), turn_id),
            )
            connection.commit()
            return self._get(connection, turn_id)

    def _persist_voice_control(self, session_id, child_id, state, transcript, key, arrival_id, result, voice_capture):
        from .voice_control_intent import CONTROLS
        control, binding = result['control'], result['binding']
        if control == 'companion.chat':
            raise InvalidAgentPlanError('INPUT_COMPANION_CONTEXT_CHANGED')
        action_type, arguments = CONTROLS[control]
        required = ('exam_run_id' if control.startswith('exam.') else 'task_deferral' if control == 'task.reschedule' else 'task_start' if control == 'task.start' else 'task_resume' if control == 'task.resume' else 'task' if control.startswith('task.')
                    else 'break' if control.startswith('break.')
                    else 'diagnostic' if control.startswith('diagnostic.') else 'vocabulary' if control.startswith('pet_vocabulary.')
                    else 'tutor' if control.startswith('tutor.') else 'pet_listening' if control.startswith('pet_audio.') else None)
        action = {'type': action_type, **arguments, 'source': 'content_free_voice_control',
                  'local_only': True, 'cloud_transmission_allowed': False}
        if control.startswith('native_writing.'):
            action.update(request_quote=transcript,native_writing_target=binding.get('native_writing'))
        pending = binding.get('pending_evaluation')
        if pending:
            from .pending_evaluation_control import ALLOWED_CONTROLS, marker
            if control not in ALLOWED_CONTROLS:
                raise InvalidAgentPlanError('PENDING_EVALUATION_CONTROL_CAPABILITY_FORBIDDEN')
        if result.get('origin') == 'verified_agent_plan':
            action['source'] = 'verified_general_plan_control'
        response = '我按你的请求操作，执行结果确认后再继续。'
        if control == 'learning_inputs.resume_draft':
            action['selection_quote'] = transcript
            response = '我们回到还没说完的作业信息。我先核对草稿，再接着问；原题留着，不开始计时。'
        if control == 'task.reschedule':
            action['reason'] = transcript
            response = '我先保存暂停任务的延期决定，旧进度保留，不会算作完成。'
        if control == 'learning_inputs.propose_revision':
            from .learning_input_voice import arguments as revision_arguments
            action.update(revision_arguments(result['revision'], transcript))
            response = '我先核对你要改的记录并读回，确认前不修改作业，也不恢复或完成旧任务。'
        if control == 'voice.configure_wake_word':
            action.update(self.voice_control_intent.wake_word_arguments(result['wake_word'], transcript))
            response = '我们先确认你想用的名字，确认后才会在这台电脑保存。'
        if control == 'device.status':
            response = '我先查看这台电脑的采集状态，不会开启设备。'
        if control == 'system.inspect_learning':
            response = '我先核对本机保存的请求回执，不重新提交你的原话或答案。'
        if control == 'device.connect':
            action['device'] = result['device']
            response = '我现在重新检查并连接你说的设备，收到真实画面后再告诉你结果。'
        if control == 'learning.help_current':
            target = binding.get('learning_help') or {}
            action.update(question=target.get('question'),subject=target.get('subject'),hint_level=1,
                diagnostic_prompt_id=target.get('prompt_id'),diagnostic_item_version=target.get('item_version'),
                tutor_request_action_id='learning-help:'+result['request_key'])
            response = '我们接着这道题，一次理清一步，再请你自己试试。'
        if control == 'learning.retry_check_coaching':
            from .check_coaching_retry_control import target
            with self.database.connect() as c:
                c.execute('BEGIN')
                action.update(target(c, session_id, child_id, binding.get('check_coaching_retry')))
            response = '你的原回答已经保存。我只重试刚才的点评，不用再回答，也不算作独立掌握。'
        if control == 'learning.independent_check':
            from .independent_question_control import target
            with self.database.connect() as c:
                c.execute('BEGIN')
                action.update(target(c,session_id,child_id,binding.get('learning_help')))
            response = '我先核对一道你没看过、来源和评分依据都合格的题，准备好后请你自己试试。原回答保留。'
        if control == 'parent.request':
            response = '请身边的家长过来一起看看。我还没有联系到家长，也不会代替家长确认。'
        if control == 'task.start':
            target = binding.get('task_start') or {}
            reason = target.get('reason')
            if (result.get('navigation') in {'hold','unfinished','reported'}
                    or binding.get('has_waiting_question') or binding.get('companion_paused_tasks')):
                reason = 'request_not_start' if result.get('navigation') in {'hold','unfinished','reported'} else reason or 'pending_question'
            if reason or target.get('status') != 'ready':
                response, action = self._task_start_clarification(target, reason)
            else:
                response = '我先核对下一项的原计划和题目，开始成功后再告诉你。'
                unavailable = self._task_source_clarification(child_id, target)
                if unavailable:
                    response, action = unavailable
        if control == 'evaluation.status':
            response = '我只核对刚才那次评价的原记录，不重新提交作答或调用评分。'
        if result.get('origin') == 'explicit_local_document_stop':
            response = '我正在停止高拍仪采集，收到设备确认后再继续。你后面的问题先保留，不会提前读取页面。'
        activity = binding.get('companion_activity') or {}
        if control in {'oral.retry','oral.independent_check'}:
            kind = 'retry' if control=='oral.retry' else 'independent_check'
            packet = binding.get('oral_followups',{}).get(kind) or {}
            if packet.get('status')=='ready':
                action.update(request_quote=transcript,oral_followup_binding={k:packet[k] for k in ('action','binding_version')})
                response = '我保留刚才的回答，准备好原题后你可以再说一次。' if kind=='retry' else '我核对一道你没做过的同类题，再请你自己试试。'
            else:
                action={'type':'none','source':'oral_followup_clarification','local_only':True,'cloud_transmission_allowed':False}
                response=('我暂时核对不了之前看过哪些题，先不把下一题算作独立检查。刚才的回答还在，我们可以继续讨论或再练一次。'
                    if packet.get('reason')=='oral_exposure_history_unavailable' else
                    '暂时没有符合要求的新题。原回答会保留，我们可以先讨论刚才的建议。')
                if kind=='independent_check' and packet.get('reason')=='oral_no_unexposed_source':
                    response='我已把补充新题记在刚才的学习目标下，原回答保留。题目准备好后，再请你独立试试；现在也可以继续讨论刚才的建议。'
        if control == 'companion.start_activity':
            action.update(self.voice_control_intent.activity_arguments(result['activity_type']))
        if control.startswith(('controlled_content.', 'controlled_game.')):
            from .controlled_voice import action_fields
            action.update(action_fields(control, binding['controlled']), mastery_update_allowed=False)
            response = ('我先核对家长许可、原资源和剩余时间，成功后再告诉你。' if control.endswith('.play')
                        else '我先核对并关闭原活动，收到设备结果后再确认，不自动恢复学习。')
            if binding['controlled'].get('launch_cancel'):
                response = '我先核对并取消原启动请求，尚不认定已经关闭，也不恢复学习计时。'
        if control == 'companion.activity_feedback':
            if activity.get('binding'):
                action.update(result['activity_feedback'], response_quote=transcript)
                response = '我先记录你对刚才活动的实际反馈。'
                if action['operation'] == 'request_resume':
                    response = '我核对刚才活动和原任务，再按你的请求恢复；尚未确认执行完成。'
            else:
                action = {'type': 'none', 'source': 'companion_activity_clarification',
                          'local_only': True, 'cloud_transmission_allowed': False}
                response = (self._activity_selection_question(activity['candidates']) if activity.get('candidates')
                            else '我没有找到可核对的活动记录。你说的是哪次活动？我不会猜测或恢复任务。')
        if required and (not binding.get(required) or control == 'task.resume' and binding[required].get('status') != 'ready'):
            action = {'type': 'none', 'source': 'content_free_control_missing_target',
                      'local_only': True, 'cloud_transmission_allowed': False}
            response = '我听到了你的操作请求，但还不能确定对应哪项。你想操作哪一项？原题和进度保留。'
            if control == 'task.resume' and (binding.get('task_resume') or {}).get('status') == 'ambiguous':
                choices = binding['task_resume']['items']
                if choices:
                    action.update(source='task_resume_clarification', task_resume_choices=choices)
                    response = '有几项可继续或补齐题目的任务：' + '；'.join(
                        f"第{index + 1}项，{item['title']}" for index, item in enumerate(choices)) + '。你要继续哪一项？'
                else:
                    response = '目前没有可以核对并继续的原任务。原记录保留，需要先核对题源、计划或恢复记录。'
        if action['type'].startswith(('pause_exam', 'resume_exam', 'cancel_exam')):
            action['exam_run_id'] = binding['exam_run_id']
        if control.startswith('diagnostic.') and binding.get('diagnostic'):
            action.update(diagnostic_prompt_id=binding['diagnostic']['prompt_id'],
                          diagnostic_item_version=binding['diagnostic']['item_version'])
        if control.startswith('pet_vocabulary.') and binding.get('vocabulary'):
            action.update(vocabulary_prompt_id=binding['vocabulary']['prompt_session_id'],
                          vocabulary_card_version=binding['vocabulary']['card_version'])
        if control.startswith('tutor.') and binding.get('tutor'):
            action.update(tutor_playback_id=binding['tutor']['playback_id'],
                          tutor_source_version=binding['tutor']['source_version'])
        if control.startswith('pet_audio.') and binding.get('pet_listening'):
            action.update(request_quote=transcript, pet_listening_binding=binding['pet_listening'])
        decision = self._deterministic_action_plan(
            owner=self.capability_registry.owner_for_action_type(action['type'], state, arguments=action), action=action, response=response)
        if control == 'learning.independent_check':
            with self.database.connect() as c:
                active=c.execute("SELECT objective_type FROM agent_objective WHERE child_id=? AND status='active'",(child_id,)).fetchone()
            if not active or active['objective_type']!='learning':
                decision=replace(decision,objective=ObjectiveDraft('new','learning',
                    '独立尝试一份未见过且已审核的来源原题',
                    ('呈现本机未暴露、来源与评分资格仍有效的原题',
                     '保存真实独立作答与可靠核验结果；不把单题表现作为掌握或迁移证明'), 'diagnose'))
        if control.startswith('native_writing.'):
            decision=replace(decision,step=replace(decision.step,arguments={'request_quote':transcript}),
                             after_execution='stop',continuation_scope=())
        if control == 'voice.configure_wake_word':
            decision = replace(decision, step=replace(decision.step,
                arguments=self.voice_control_intent.wake_word_arguments(result['wake_word'], transcript)),
                after_execution='stop', continuation_scope=())
        manifest = {'learning_input_arrival_id': arrival_id}
        if control == 'learning_inputs.resume_draft':
            decision = replace(decision, step=replace(decision.step, arguments={'selection_quote': transcript}),
                               after_execution='stop', continuation_scope=())
        if pending:
            manifest['pending_evaluation'] = pending
            if result.get('origin') == 'explicit_local_document_stop':
                manifest['pending_local_control'] = 'document_stop'
            action['pending_evaluation_control'] = marker(pending)
            if control == 'evaluation.status':
                action['pending_evaluation_query'] = {'turn_id':pending['evaluation_turn_id']}
            decision = replace(decision, after_execution='stop', continuation_scope=())
        if control == 'learning_inputs.propose_revision':
            decision = replace(decision, step=replace(decision.step,
                arguments=revision_arguments(result['revision'], transcript)),
                after_execution='stop', continuation_scope=())
        if control == 'task.reschedule':
            decision = replace(decision, step=replace(decision.step, arguments={'reason': transcript}),
                after_execution='stop', continuation_scope=())
        if control in {'oral.retry','oral.independent_check'} and action['type']!='none':
            decision=replace(decision,step=replace(decision.step,arguments={'request_quote':transcript}),
                             after_execution='stop',continuation_scope=())
        if control == 'companion.start_activity':
            decision = replace(decision, step=replace(decision.step,
                arguments=self.voice_control_intent.activity_arguments(result['activity_type'])))
        followup = binding.get('document_stop_followup') if not pending and result.get('origin') == 'explicit_local_document_stop' else None
        if followup:
            decision = replace(decision, after_execution='replan', continuation_scope=({
                'capability': 'conversation.respond', 'arguments': {}, 'request_quote': followup['request_quote']},))
        if control == 'companion.activity_feedback':
            if action['type'] != 'none':
                decision = replace(decision, step=replace(decision.step,
                    arguments={**result['activity_feedback'], 'response_quote': transcript}))
                manifest['companion_activity_outcome_binding'] = activity['binding']
            else:
                decision = replace(decision, requires_child_reply=True)
                if activity.get('candidates'):
                    manifest['companion_activity_selection'] = {'candidates': activity['candidates'], 'question': response}
        if action.get('source') == 'task_resume_clarification':
            manifest['task_resume_dialogue'] = {'request_key': result['request_key'], 'targets': binding}
            decision = replace(decision, requires_child_reply=True)
        if action.get('source') == 'task_navigation_clarification':
            decision = replace(decision, requires_child_reply=True, after_execution='stop', continuation_scope=())
        if action['type'] != 'none' or control == 'evaluation.status':
            manifest['voice_control_binding'] = {
                'request_key': result['request_key'], 'control': control, 'targets': binding}
            if control == 'task.resume':
                target = binding['task_resume']['item']
                if target.get('goal_prompt_binding'):
                    manifest['goal_prompt_binding'] = target['goal_prompt_binding']
                    if target.get('operation') == 'start':
                        manifest['task_start_binding'] = {key: target[key] for key in ('plan_item_id', 'source_type', 'source_ref', 'operation')}
            if control == 'task.start':
                target = binding['task_start']['item']
                manifest['task_start_arrival'] = binding['task_start']
                manifest['task_start_binding'] = {**{k:target[k] for k in ('plan_item_id','source_type','source_ref')},'operation':'start'}
        if control.startswith('pet_audio.') and binding.get('pet_listening'):
            manifest['pet_listening_binding'] = binding['pet_listening']
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if pending:
                from .pending_evaluation_control import revalidate
                revalidate(c, pending)
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                self._assert_action_payload(old, session_id, transcript)
                self._assert_replayed_voice(old, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, old['agent_turn_id'])
            if not self.voice_control_intent.scope_valid(c, session_id, child_id, key,
                    require_cloud=result.get('origin') not in {'explicit_local_task_navigation','explicit_local_break_resume', 'explicit_local_task_resume_selection', 'explicit_local_document_stop'}):
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            if binding.get('oral_supply_offer_id'):
                from .oral_supply_obligation import acknowledged_offer
                if acknowledged_offer(c, child_id, session_id, datetime.now(timezone.utc)) != binding['oral_supply_offer_id']:
                    raise InvalidAgentPlanError('ANSWER_ORAL_SUPPLY_SOURCE_CHANGED')
            if (binding.get('task_resume') or {}).get('daily_supply_offer'):
                from .oral_daily_reminder import validate
                validate(c, child_id, session_id, binding['task_resume'], datetime.now(timezone.utc))
            turn = str(uuid4())
            if followup and not c.execute("SELECT 1 FROM agent_objective WHERE child_id=? AND status='active'", (child_id,)).fetchone():
                decision = replace(decision, objective=ObjectiveDraft('new', 'execution',
                    '停止采集后处理本次问题', ('停止采集得到真实回执后处理原问题',), 'execute'))
                manifest['document_stop_followup_owned_objective'] = True
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (turn, child_id, session_id, transcript, decision.step.capability.name, response,
                 json.dumps(action, ensure_ascii=False), 'content_free_voice_control_v1', utc_now(), key))
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id,
                agent_turn_id=turn, client_action_id=key, decision=decision, context_manifest=manifest)
            self.agent_runtime.inspect_voice_control_execution(c, turn, allow_planned=True)
            if (control=='oral.independent_check' and action.get('source')=='oral_followup_clarification'
                    and binding.get('oral_followups',{}).get('independent_check',{}).get('reason')=='oral_no_unexposed_source'):
                from .oral_supply_obligation import record
                record(c,turn,result['request_key'])
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata), turn))
            c.commit()
            return self._get(c, turn)

    def _persist_pet_listening_voice(self, session_id, child_id, state, transcript, key, arrival_id, resolved, *, voice_capture=None):
        is_control = 'arguments' in resolved
        cap = self.capability_registry.require('pet.listening_control' if is_control else 'conversation.respond', state)
        decision = AgentPlanDecision(ObjectiveDraft('none', None, None, (), 'execution'),
            PlannedStep(cap, resolved.get('arguments', {'response': resolved['response']}),
                        'Explicit local PET playback request', cap.expected_evidence), resolved['response'], False, 1)
        manifest = {'learning_input_arrival_id': arrival_id}
        manifest['pet_listening_binding' if is_control else 'pet_listening_dialogue'] = resolved.get('binding', True)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                self._assert_action_payload(old, session_id, transcript)
                self._assert_replayed_voice(old, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, old['agent_turn_id'])
            tid = str(uuid4())
            action = {**action_from_plan(decision, transcript, None), 'local_only': True, 'cloud_transmission_allowed': False}
            if is_control:
                action['pet_listening_binding'] = resolved['binding']
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (tid, child_id, session_id, transcript, cap.name if is_control else 'pet_listening_unavailable',
                 resolved['response'], json.dumps(action, ensure_ascii=False), PLANNER_VERSION, utc_now(), key))
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id,
                agent_turn_id=tid, client_action_id=key, decision=decision, context_manifest=manifest)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata, ensure_ascii=False), tid))
            c.commit()
            return self._get(c, tid)

    def _persist_unplaced_voice(self, session_id, child_id, state, transcript, key, arrival_id, resolved, *, voice_capture=None):
        is_write = 'arguments' in resolved
        cap = self.capability_registry.require(resolved.get('capability', 'learning_inputs.review_unplaced') if is_write else 'conversation.respond', state)
        args = resolved['arguments'] if is_write else {'response': resolved['response']}
        manifest = {'learning_input_arrival_id': arrival_id}
        manifest['learning_input_unplaced_binding' if is_write else 'learning_input_unplaced_dialogue'] = resolved['binding' if is_write else 'dialogue']
        if resolved.get('scope_offer'):
            manifest['learning_input_scope_offer'] = resolved['scope_offer']
        decision = AgentPlanDecision(ObjectiveDraft('none', None, None, (), 'execution'),
            PlannedStep(cap, args, 'Explicit local unplaced obligation dialogue', cap.expected_evidence),
            resolved['response'], not is_write, 1)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                self._assert_action_payload(old, session_id, transcript)
                self._assert_replayed_voice(old, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, old['agent_turn_id'])
            tid = str(uuid4())
            action = {**action_from_plan(decision, transcript, None), 'local_only': True, 'cloud_transmission_allowed': False}
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (tid, child_id, session_id, transcript, cap.name if is_write else 'unplaced_voice_dialogue',
                 resolved['response'], json.dumps(action, ensure_ascii=False), PLANNER_VERSION, utc_now(), key))
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id,
                agent_turn_id=tid, client_action_id=key, decision=decision, context_manifest=manifest)
            replacement_binding = resolved.get('binding') or ((resolved.get('dialogue') or {}).get('focus') or {}).get('binding')
            if replacement_binding:
                arrival = c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE arrival_id=?', (arrival_id,)).fetchone()
                snapshot = next(item['record'] for item in json.loads(arrival[0]) if item.get('kind') == 'unplaced_voice_arrival')
                pending = (snapshot.get('dialogue') or {}).get('pending_turn_id')
                old_command = c.execute('''SELECT p.plan_id,p.context_manifest_json,s.step_id,s.idempotency_key FROM agent_plan p
                    JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=? AND p.child_id=? AND p.session_id=?
                    AND p.status IN ('planned','running','failed') AND s.step_index=0
                    AND s.capability_name IN ('learning_inputs.review_unplaced','learning_inputs.review_reclaimed_scope') AND s.status IN ('planned','running','failed')''',
                    (pending, child_id, session_id)).fetchone() if pending else None
                if (old_command and json.loads(old_command['context_manifest_json']).get('learning_input_unplaced_binding') == replacement_binding
                        and not c.execute('SELECT 1 FROM learning_input_carryover_event WHERE child_id=? AND client_action_id=?',
                                          (child_id, old_command['idempotency_key'])).fetchone()):
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE plan_id=?", (old_command['plan_id'],))
                    c.execute("UPDATE agent_step SET status='cancelled' WHERE step_id=?", (old_command['step_id'],))
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata, ensure_ascii=False), tid))
            c.commit()
            return self._get(c, tid)

    def _persist_input_revision_control(self, session_id, child_id, state, transcript, key, arrival, choice, *, voice_capture=None, semantic_field_request=None):
        cap = self.capability_registry.require('learning_inputs.select_target' if choice == 'supply' else f"learning_inputs.{choice}_revision", state)
        decision = AgentPlanDecision(ObjectiveDraft("none", None, None, (), "execution"),
            PlannedStep(cap, {'selection_quote' if choice == 'supply' else 'confirmation_quote': transcript}, "Bound readback response", cap.expected_evidence),
            '我先核对这次补充。' if choice == 'supply' else "我先核对这次确认。" if choice == "confirm" else "我来取消这次提议。", False, 1)
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM agent_turn WHERE client_action_id=?", (key,)).fetchone()
            if existing:
                self._assert_action_payload(existing, session_id, transcript)
                self._assert_replayed_voice(existing, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, existing["agent_turn_id"])
            turn_id = str(uuid4())
            action = action_from_plan(decision, transcript, None)
            manifest = {"learning_input_arrival_id": arrival["arrival_id"]}
            if semantic_field_request:
                manifest['input_field_request'] = semantic_field_request
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (turn_id, child_id, session_id, transcript, cap.name,
                decision.spoken_transition, json.dumps(action, ensure_ascii=False), PLANNER_VERSION, utc_now(), key))
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id,
                agent_turn_id=turn_id, client_action_id=key, decision=decision,
                context_manifest=manifest)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata, ensure_ascii=False), turn_id))
            c.commit()
            return self._get(c, turn_id)

    def _persist_activity_selection(self, session_id, child_id, state, transcript, key, snapshot, index, *, voice_capture=None):
        candidates = snapshot["selection"]["candidates"]
        chosen = candidates[index - 1] if index and index <= len(candidates) else None
        current = snapshot["candidates"]
        valid = chosen and chosen in current
        response = (f"你选的是{chosen['label']}。刚才实际做了哪些，感觉怎么样？要恢复原学习或稍后再做，也可以告诉我。"
                    if valid else self._activity_selection_question(current))
        manifest = ({"companion_activity_focus_binding": chosen["binding"]} if valid else
                    {"companion_activity_selection": {"candidates": current, "question": response}})
        cap = self.capability_registry.require("conversation.respond", state)
        decision = AgentPlanDecision(ObjectiveDraft("none", None, None, (), "execution"),
            PlannedStep(cap, {"response": response}, "Local activity selection", ()), response, True, 1)
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM agent_turn WHERE client_action_id=?", (key,)).fetchone()
            if existing:
                self._assert_action_payload(existing, session_id, transcript)
                self._assert_replayed_voice(existing, (voice_capture or {}).get('voice_input_id'))
                return self._get_replayed(c, existing["agent_turn_id"])
            turn = str(uuid4())
            action = {**action_from_plan(decision, transcript, None), "local_only": True, "cloud_transmission_allowed": False}
            self._freeze_local_voice(c, session_id, child_id, transcript, voice_capture, action, manifest)
            c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
                orchestrator_version,created_at,client_action_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (turn, child_id, session_id, transcript, "companion_activity_selection", response, json.dumps(action, ensure_ascii=False), PLANNER_VERSION, utc_now(), key))
            metadata = self.agent_runtime.persist_plan(c, child_id=child_id, session_id=session_id, agent_turn_id=turn,
                client_action_id=key, decision=decision, context_manifest=manifest)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata, ensure_ascii=False), turn))
            c.commit()
            return self._get(c, turn)

    @staticmethod
    def _activity_selection_question(candidates):
        if not candidates:
            return "原活动已变化，目前没有可绑定的活动。请重新说明你想做什么。"
        choices = "；".join(f"第{index}项，{item['label']}" for index, item in enumerate(candidates, 1))
        return f"你说的是哪次活动？{choices}。请告诉我是第几项，我再记录实际情况。"

    @staticmethod
    def _allows_cloud_history(action_json: str) -> bool:
        try:
            action = json.loads(action_json)
        except (TypeError, ValueError):
            return False
        return (isinstance(action, dict)
                and action.get("local_only") is not True
                and action.get("cloud_transmission_allowed") is not False)

    @staticmethod
    def _document_followup_state(connection: Any, session_id: str, child_id: str, context: Any) -> str:
        if not isinstance(context, dict) or context.get("version") != 1:
            return "unavailable"
        try:
            now = datetime.now(timezone.utc)
            if now >= datetime.fromisoformat(context["expires_at"]):
                return "expired"
            row = connection.execute(
                """SELECT r.revision_id,r.artifact_id,r.created_at,m.generation,m.status AS monitor_status,
                          s.state,s.status,s.ended_at,a.status AS artifact_status
                   FROM document_monitor_session m
                   JOIN daily_session s ON s.session_id=m.session_id AND s.child_id=m.child_id
                   JOIN document_page_revision r ON r.monitor_id=m.monitor_id
                     AND r.session_id=s.session_id AND r.child_id=s.child_id
                   JOIN capture_artifact a ON a.artifact_id=r.artifact_id
                     AND a.session_id=s.session_id AND a.child_id=s.child_id AND a.artifact_type='document_ocr'
                   WHERE m.session_id=? AND m.child_id=? ORDER BY r.revision_index DESC LIMIT 1""",
                (session_id, child_id),
            ).fetchone()
            if not row:
                return "unavailable"
            if row["monitor_status"] != "active" or row["status"] != "active" or row["state"] != "study" or row["ended_at"]:
                return "paused"
            if (row["revision_id"] != context.get("revision_id") or row["artifact_id"] != context.get("artifact_id")
                    or row["generation"] != context.get("monitor_generation")):
                return "superseded"
            if now - datetime.fromisoformat(row["created_at"]) > timedelta(seconds=300):
                return "expired"
            return "valid" if row["artifact_status"] in {"completed", "quality_warning"} else "unavailable"
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            return "unavailable"

    def _continue_document_observation(
        self, connection: Any, session_id: str, child_id: str, transcript: str, intent: str, action: dict,
    ) -> tuple[str, str, dict] | None:
        # This is a one-response local receipt, not an implicit tutoring objective.
        # Any intervening turn, including a pending control, ends its ownership.
        compact = re.sub(r"[\s，。！!、；;：:,]", "", transcript)
        abstention = compact in {"不知道", "我不知道", "我不会", "这一步不会", "我还不会", "还没算出来", "我还没算出来"}
        if (intent in CONVERSATION_CONTROL_INTENTS and not abstention) or (
            action.get("type") not in {"none", "submit_diagnostic_answer"}
            and not (abstention and action.get("type") in {"ask_tutor", "record_tutor_feedback", "request_capture"})
        ):
            return None
        confirmation = compact in {"是", "是的", "对", "没错", "不是", "不是这一行", "这一行", "是这一行", "一致", "不一致"}
        numeric = bool(re.fullmatch(r"(?:我的答案是|答案是)?[负零一二三四五六七八九十百千点\d.]+(?:厘米|米|平方厘米|平方米|个)?", compact))
        method = bool(re.match(r"(?:我(?:刚才|刚刚|先|是)?(?:把|将|用|算)|我写的是|这一步我|纸上写的|这一行是)", compact)
                      and re.search(r"长.{0,3}宽|长度|宽度|边长|面积|周长|单位|条件|主语|谓语|关键词|两个数|这些数|加法|减法|乘法|除法|\d.*[加减乘除=+×÷]|[加减乘除=+×÷].*\d", compact))
        if not (confirmation or numeric or method or abstention) or "？" in compact or "?" in compact:
            return None
        source = connection.execute(
            "SELECT * FROM agent_turn WHERE session_id=? AND child_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (session_id, child_id),
        ).fetchone()
        if not source or source["tool_status"] != "completed" or source["intent"] != "document_observation_question":
            return None
        try:
            source_action = json.loads(source["action_json"])
        except (ValueError, TypeError):
            return None
        if not isinstance(source_action, dict):
            return None
        context = source_action.get("document_followup")
        if (not context or source_action.get("document_context_status") != "completed"
                or source_action.get("local_only") is not True or source_action.get("cloud_transmission_allowed") is not False):
            return None
        state = self._document_followup_state(connection, session_id, child_id, context)
        if state == "valid":
            current = self._local_document_context(session_id, child_id)
            if current.get("status") != "completed" or current.get("latest_revision_id") != context["revision_id"]:
                state = "unconfirmed"
        result = {"type": "none", "source": "local_document_observation_response", "local_only": True,
                  "cloud_transmission_allowed": False, "mastery_update_allowed": False, "independent_evidence": False,
                  "document_reply_status": state}
        if state != "valid":
            return "document_observation_response", "刚才的页面追问已过期、暂停或页面发生变化。这句话没有关联到旧页，也不会交给旧题评分；需要先核对当前页面。", result
        result.update(document_reply_status="recorded", document_evidence_context=context,
                      document_revision_id=context["revision_id"], capture_artifact_id=context["artifact_id"],
                      evidence_refs=[f"agent_turn:{source['agent_turn_id']}", f"document_revision:{context['revision_id']}",
                                     f"capture_artifact:{context['artifact_id']}"],
                      document_response={"source_turn_id": source["agent_turn_id"], "revision_id": context["revision_id"],
                                         "artifact_id": context["artifact_id"], "child_response": transcript,
                                         "status": "unverified"})
        return ("document_observation_response",
                "我已把你的这句说明关联到刚才页面的待核对记录。这只是你的口述，不代表已经判对错或掌握；题干未正式确认前，我不会据此开始讲题。", result)

    @staticmethod
    def _document_observation_kind(transcript: str) -> str | None:
        text = re.sub(r"[\s，。！？!?、；;：:,.]", "", transcript)
        if any(word in text for word in (
            "停止", "暂停", "取消", "关闭", "别听", "别看", "别说", "别讲",
            "不要说", "不要讲", "换话题", "换个话题", "换一个话题", "结束",
            "休息", "找家长", "叫爸爸", "叫妈妈", "先别提醒", "等一下",
        )):
            return None
        if re.search(r"(?:我|这页|这一页|页面).{0,8}(?:刚才|刚刚).{0,6}(?:写了什么|写的什么|写了哪些|写的哪些|增加了什么|新增了什么)", text) or re.search(r"(?:我|你看到我).{0,5}(?:写了什么|写的什么)", text):
            return "recent_writing"
        if re.search(r"(?:这页|这一页|当前页|这张纸|这道题|这题).{0,8}(?:哪里|什么地方|哪一步).{0,8}(?:问题|有错|不对|不清楚)", text):
            return "possible_problem"
        if re.search(r"(?:你|高拍仪).{0,6}(?:看到|看见|看出).{0,6}(?:我在|我现在|我刚才).{0,5}(?:做什么|干什么|写什么)", text):
            return "current_activity"
        return None

    def _local_document_context(self, session_id: str, child_id: str) -> dict[str, Any]:
        if self.document_learning is None:
            return {"status": "unavailable"}
        try:
            # The context port is read-only. Do not enqueue analysis, open a
            # camera, or repair a model job as a side effect of conversation.
            context = self.document_learning.context_for_session(session_id)
            if (not isinstance(context, dict) or context.get("session_id") != session_id
                    or context.get("child_id") != child_id
                    or context.get("cloud_transmission_allowed") is not False):
                return {"status": "unavailable"}
            with self.database.connect() as connection:
                monitor = connection.execute(
                    "SELECT monitor_id,child_id,status,generation FROM document_monitor_session WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if not monitor:
                    return {"status": "empty"}
                if monitor["child_id"] != child_id:
                    return {"status": "unavailable"}
                if monitor["status"] != "active":
                    return {"status": "paused"}
                row = connection.execute(
                    """SELECT r.revision_id,r.artifact_id,r.created_at,r.review_required,r.session_id,r.child_id,
                              r.change_summary_json,r.previous_revision_id,a.extracted_text,
                              a.session_id AS artifact_session_id,a.child_id AS artifact_child_id,
                              a.status AS artifact_status,a.artifact_type,a.details_json,
                              a.client_action_id AS artifact_action_id
                       FROM document_page_revision r LEFT JOIN capture_artifact a ON a.artifact_id=r.artifact_id
                       WHERE r.monitor_id=?
                       ORDER BY r.revision_index DESC LIMIT 1""",
                    (monitor["monitor_id"],),
                ).fetchone()
            if not row:
                return {"status": "empty"}
            revision = dict(row)
            if (revision["session_id"] != session_id or revision["child_id"] != child_id
                    or revision["artifact_session_id"] != session_id or revision["artifact_child_id"] != child_id
                    or revision["artifact_type"] != "document_ocr"
                    or revision["artifact_status"] not in {"completed", "quality_warning"}):
                return {"status": "unavailable"}
            status = context.get("status")
            if status in {"paused", "stale"}:
                return {"status": status}
            if context.get("created_at") != revision["created_at"] or context.get("freshness") != "fresh":
                return {"status": "stale"}
            if context.get("current_revision_id") != revision["revision_id"]:
                return {"status": "stale"}
            if status == "completed" and context.get("latest_revision_id") != revision["revision_id"]:
                return {"status": "stale"}
            if status not in {"completed", "in_progress", "unknown", "blocked", "empty"}:
                return {"status": "unavailable"}
            if not all(isinstance(context.get(key, []), list) for key in ("observations", "hypotheses", "questions")):
                return {"status": "unavailable"}
            return {"status": status, "latest_revision_id": revision["revision_id"], "revision": revision, "context": context,
                    "monitor_generation": monitor["generation"]}
        except Exception:
            # A failed device/analysis dependency must not fall through into a
            # generic cloud lesson using an unrelated task from the study plan.
            return {"status": "unavailable"}

    def _document_observation_reply(self, session_id: str, child_id: str, kind: str | None) -> tuple[str, dict]:
        local = self._local_document_context(session_id, child_id)
        action: dict[str, Any] = {
            "type": "none", "source": "local_document_learning",
            "local_only": True, "cloud_transmission_allowed": False,
            "mastery_update_allowed": False, "independent_evidence": False,
            "evidence_refs": [], "document_context_status": local["status"],
        }
        unavailable = {
            "unavailable": "现在无法核对高拍仪的本地页面记录，我不能假装看到了你正在做什么，也不会猜一道题来讲。",
            "empty": "当前会话还没有可核对的页面记录，我不能判断你刚才写了什么。需要先取得清楚的页面，再核对文字。",
            "paused": "高拍仪持续观察已暂停，我不会使用暂停前的画面来判断你现在在做什么，也不会自动恢复设备。",
            "stale": "最近的页面记录已经过时，或分析还对应旧页面。我不能把旧图当成你现在的页面，也不会继续旧题讲解。",
            "in_progress": "当前页面的本地分析还在处理中，我先不猜测你写了什么；分析结果未确认前不会开始讲题。",
            "unknown": "当前页面分析的结果尚未确认，我不能把它当作观察结论，也不会自动重复分析或讲题。",
            "blocked": "当前页面还不能可靠分析，可能需要核对图像或识别文字。我不会据此判定你写错了或已经掌握。",
        }
        if local["status"] != "completed":
            if kind == "current_activity" and local["status"] in {"empty", "stale"}:
                # An explicit current-page request may acquire new evidence;
                # history queries and a privacy pause remain read-only.
                action.update(type="request_capture", observation_only=True)
                return "现在还没有清楚的当前页面，我先通过高拍仪查看，再告诉你实际读到了什么。", action
            return unavailable[local["status"]], action
        revision = local["revision"]
        action.update(
            document_revision_id=revision["revision_id"],
            capture_artifact_id=revision["artifact_id"],
            evidence_refs=[f"document_revision:{revision['revision_id']}", f"capture_artifact:{revision['artifact_id']}"],
            document_followup={"version": 1, "revision_id": revision["revision_id"], "artifact_id": revision["artifact_id"],
                               "monitor_generation": local["monitor_generation"],
                               "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()},
        )
        text = re.sub(r"\s+", " ", revision["extracted_text"] or "").strip()
        quote = text[:120]
        context = local["context"]
        vision_source = context.get("source") == "cloud_page_ocr"

        def verified_quote(item: Any) -> str | None:
            evidence = item.get("evidence") if isinstance(item, dict) else None
            if (not isinstance(evidence, dict) or evidence.get("revision_id") != revision["revision_id"]
                    or evidence.get("artifact_id") != revision["artifact_id"]):
                return None
            if vision_source:
                if context.get("needs_confirmation") is not True or evidence.get("kind") != "cloud_page_ocr_unconfirmed":
                    return None
                try:
                    vision = json.loads(revision["details_json"]).get("cloud_vision_fallback")
                    if not isinstance(vision, dict) or vision.get("status") != "needs_review":
                        return None
                    request_id, index = evidence.get("cloud_vision_request_id"), evidence.get("block_index")
                    if (request_id != vision.get("request_id") or request_id not in {
                            "monitor:" + revision["artifact_action_id"], "artifact:" + revision["artifact_id"]}
                            or type(index) is not int or not isinstance(vision.get("blocks"), list)
                            or not 0 <= index < len(vision["blocks"])):
                        return None
                    block = vision["blocks"][index]
                    if (not isinstance(block, dict) or not isinstance(block.get("text"), str)
                            or not block["text"].strip() or block["text"] != evidence.get("quote")
                            or block.get("bbox") != evidence.get("bbox")
                            or not isinstance(block.get("bbox"), list) or len(block["bbox"]) != 4
                            or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in block["bbox"])
                            or block.get("kind") not in {"text", "formula"}
                            or type(block.get("confidence")) not in (int, float)
                            or not 0 <= block["confidence"] <= .69
                            or block["confidence"] != evidence.get("confidence")):
                        return None
                    with self.database.connect() as connection:
                        receipt = connection.execute(
                            "SELECT result_json FROM document_vision_request WHERE child_id=? AND request_id=? AND status='succeeded'",
                            (child_id, request_id),
                        ).fetchone()
                    if not receipt or json.loads(receipt["result_json"]) != vision:
                        return None
                    return re.sub(r"\s+", " ", block["text"]).strip()[:120]
                except (TypeError, ValueError, AttributeError, sqlite3.Error):
                    return None
            if evidence.get("kind") != "ocr_text_unconfirmed":
                return None
            start, end, value = evidence.get("start"), evidence.get("end"), evidence.get("quote")
            raw_text = revision["extracted_text"] or ""
            if (type(start) is not int or type(end) is not int or not isinstance(value, str)
                    or not value.strip() or not 0 <= start < end <= len(raw_text)
                    or raw_text[start:end] != value):
                return None
            return re.sub(r"\s+", " ", value).strip()[:120]

        def detail(item: dict, key: str, limit: int) -> str:
            value = item.get(key)
            if (not isinstance(value, str) or not value.strip() or len(value) > limit
                    or is_voice_intent_prompt_injection(value) or sensitive_categories(value)
                    or sanitize_cloud_text(value).text != value
                    or re.search(r"已经掌握|肯定|一定[是对错]|确定[是对错]|你[做写答]错|答案[是为]|正确答案", value)):
                return ""
            if key == "rationale" and not value.startswith(("可能", "或许", "需要核对", "尚需确认", "待确认", "不确定")):
                return ""
            if key == "follow_up" and not value.endswith(("?", "？")):
                return ""
            return value.strip()

        if vision_source:
            action.update(source="cloud_page_ocr", needs_confirmation=True)
            for observation in context.get("observations", []):
                cited = verified_quote(observation)
                if not cited:
                    continue
                evidence = observation["evidence"]
                action["evidence_refs"].append(f"cloud_vision_request:{evidence['cloud_vision_request_id']}:block:{evidence['block_index']}")
                focus = detail(observation, "learning_focus", 120)
                follow_up = "这处文字和纸上的原文一致吗？"
                rationale = ""
                for hypothesis in context.get("hypotheses", []):
                    if (isinstance(hypothesis, dict) and hypothesis.get("status") == "needs_child_confirmation"
                            and verified_quote(hypothesis) and hypothesis["evidence"] == evidence):
                        follow_up = detail(hypothesis, "follow_up", 160) or follow_up
                        rationale = detail(hypothesis, "rationale", 180)
                        break
                focus_text = f"可能相关的学习重点是：{focus}。" if focus else ""
                rationale_text = f"待确认的假设是：{rationale}。" if rationale else ""
                return (f"最新页面的视觉识别候选是“{cited}”，还没有确认，不能据此判断你刚才的动作、对错或掌握。{focus_text}{rationale_text}请先核对识别文字，题干确认前不会开始讲题。{follow_up}", action)
            action["document_context_status"] = "blocked"
            return "最新页面的视觉候选还无法与原始回执核对，我不能把它当作已确认题干，也不会改用不可靠的识别文字讲题。", action
        if not text:
            action["document_context_status"] = "blocked"
            return "最新页面还没有可读的识别文字。我不能判断你写了什么，请先核对画面是否清楚。", action
        if kind == "recent_writing":
            try:
                added = json.loads(revision["change_summary_json"]).get("added_lines", [])
            except (TypeError, ValueError, AttributeError):
                added = []
            added = [re.sub(r"\s+", " ", line).strip() for line in added if isinstance(line, str)] if isinstance(added, list) else []
            verified = [line for line in added if line and line in text]
            if revision["previous_revision_id"] and verified:
                return (f"本次页面比上一版新增的识别文字包括“{verified[0][:120]}”。这是页面变化，不一定是你刚写的，也可能有识别错误。你刚才加的是这一处吗？", action)
            return (f"最新页面识别到了“{quote}”，但还没有足够的相邻版本证据确认哪些是你刚写的。你指的是哪一行？", action)
        if kind == "possible_problem":
            prompts = {
                "question_requirement": "可能需要先核对题目要求。你指的是要找什么条件？",
                "calculation_step": "可能需要核对这一行的计算。你愿意先说说这一行的写法吗？",
                "unit_or_symbol": "可能需要核对单位或符号。这里的字和符号识别对了吗？",
                "copying_or_ocr": "可能只是抄写或识别差异。它和纸上的原文一致吗？",
            }
            for hypothesis in local["context"].get("hypotheses", []):
                if not isinstance(hypothesis, dict) or hypothesis.get("status") != "needs_child_confirmation":
                    continue
                cited = verified_quote(hypothesis)
                prompt = prompts.get(hypothesis.get("kind")) if isinstance(hypothesis.get("kind"), str) else None
                if cited and prompt:
                    rationale = detail(hypothesis, "rationale", 180)
                    follow_up = detail(hypothesis, "follow_up", 160)
                    prompt = (rationale + ("。" if rationale and not rationale.endswith(("。", "？", "?")) else "") + follow_up) or prompt
                    return (f"最新页面里“{cited}”这一处有待核对。{prompt}这只是待确认假设，不代表已经判错或判断掌握。", action)
            return (f"最新页面记录包含“{quote}”。目前只能把这里当作待核对线索，不能确定有错，更不能据此判断掌握。你想先核对识别文字，还是某一步的写法？", action)
        labels = {"arithmetic": "算式", "equation": "方程", "word_problem": "应用题", "reading": "阅读", "language_exercise": "语言练习"}
        for observation in local["context"].get("observations", []):
            if not isinstance(observation, dict):
                continue
            cited = verified_quote(observation)
            label = labels.get(observation.get("kind")) if isinstance(observation.get("kind"), str) else None
            if cited and label:
                focus = detail(observation, "learning_focus", 120)
                focus_text = f"可能相关的学习重点是：{focus}。" if focus else ""
                return (f"本地最新页面里有可能是{label}的文字：“{cited}”。{focus_text}这只是页面文字线索，不等于看到了你的动作，也不能判断掌握。你现在是在处理这一处吗？", action)
        return (f"最新页面记录识别到了“{quote}”。这只说明页面上可能有这些字，不能单凭它确定你正在做什么或是否掌握。你是在写这一步吗？", action)

    @staticmethod
    def _deterministic_action_plan(
        *,
        owner: CapabilitySpec,
        action: dict[str, Any],
        response: str,
        objective_mode: str = "none",
        objective_phase: str = "execute",
    ) -> AgentPlanDecision:
        arguments = {
            key: value for key, value in action.items()
            if key not in {"type", "source", "confidence", "capability", "execution_location", "risk_level"}
        }
        return AgentPlanDecision(
            objective=ObjectiveDraft(
                mode=objective_mode,
                objective_type=None,
                summary=None,
                success_criteria=(),
                phase=objective_phase,
            ),
            step=PlannedStep(
                capability=owner,
                arguments=arguments,
                reason=(
                    "已由本地规则确定的孩子指令，通过统一幂等执行边界完成；"
                    "执行位置由能力注册表决定。"
                ),
                expected_evidence=owner.expected_evidence,
            ),
            spoken_transition=response,
            requires_child_reply=False,
            confidence=1.0,
        )

    @staticmethod
    def _objective_transition_for_action(action_type: str) -> str:
        if action_type in {"stop_speech", "request_capture"}:
            return "cancel"
        if action_type in {
            "start_pet_vocabulary_review", "start_diagnostic", "start_exam_simulation",
            "start_companion_activity", "start_controlled_game", "start_controlled_content",
            "begin_break", "pause_active_task", "request_end", "switch_child_profile",
        }:
            return "pause"
        return "none"

    @staticmethod
    def _speech_normalized_with_positions(value: str) -> tuple[str, list[int]]:
        characters: list[str] = []
        positions: list[int] = []
        for index, character in enumerate(value):
            if re.match(r"[A-Za-z0-9\u3400-\u9fff²³⁴⁵⁶⁷⁸⁹⁰]", character):
                characters.append(character.lower())
                positions.append(index)
        return "".join(characters), positions

    @classmethod
    def _strip_recent_assistant_echo(
        cls,
        transcript: str,
        recent_responses: tuple[str, ...],
    ) -> tuple[str, int]:
        # Text alone cannot distinguish speaker echo from a child's repetition.
        # Acoustic cancellation belongs to capture; never erase child evidence here.
        return transcript, 0

    @staticmethod
    def _is_capture_readiness_acknowledgement(text: str) -> bool:
        compact = re.sub(r"[\s，。！？,.!?]", "", text)
        return bool(re.fullmatch(
            r"(?:好了|放好了|摆好了|准备好了|可以拍了|拍吧|来了|在这里|就是这题|就是这道题|这道题)",
            compact,
        ))

    @staticmethod
    def _has_recent_pending_document_capture(rows: list[Any]) -> bool:
        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                action_type = str(json.loads(row["action_json"] or "{}").get("type") or "none")
                created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if action_type in {
                "cancel_capture", "confirm_ocr_text", "correct_ocr_text",
                "ask_tutor", "start_pet_vocabulary_review", "start_diagnostic",
            }:
                return False
            if action_type in {"request_capture", "retry_capture"}:
                return now - created_at.astimezone(timezone.utc) <= timedelta(minutes=3)
        return False

    def _deterministic_learning_question_plan(
        self,
        *,
        intent: str,
        response: str,
        action: dict[str, Any],
        active_objective: dict[str, Any] | None,
    ) -> AgentPlanDecision:
        question = str(action.get("question") or "").strip()[:2000]
        subject = str(action.get("subject") or "其他").strip()[:40] or "其他"
        existing_learning = bool(
            active_objective and active_objective.get("objective_type") == "learning"
        )
        mode = "continue" if intent == "correct_previous" and existing_learning else "new"
        summary_question = re.sub(r"\s+", "", question)[:140]
        summary = f"理解{subject}问题：{summary_question}"
        criteria = (
            "能用自己的话解释关键概念或方法",
            "能独立完成一道近迁移检查",
        )
        capability = self.capability_registry.require("learning.explain", "")
        return AgentPlanDecision(
            objective=ObjectiveDraft(
                mode=mode,
                objective_type="learning",
                summary=summary,
                success_criteria=criteria,
                phase="explain",
            ),
            step=PlannedStep(
                capability=capability,
                arguments={
                    "question": question,
                    "subject": subject,
                    "support_level": min(6, max(1, int(action.get("hint_level") or 6))),
                },
                reason="孩子提出了明确学习问题，先建立可验证目标再讲解。",
                expected_evidence=capability.expected_evidence,
            ),
            spoken_transition=response,
            requires_child_reply=True,
            confidence=1.0,
        )

    def _deterministic_learning_reply_plan(
        self,
        *,
        transcript: str,
        intent: str,
        pending_reply: dict[str, Any],
    ) -> AgentPlanDecision:
        arguments = pending_reply.get("arguments") or {}
        subject = str(arguments.get("subject") or "其他").strip()[:40] or "其他"
        compact = re.sub(r"[，。！？、\s]", "", transcript)
        criteria = tuple(str(item) for item in pending_reply.get("success_criteria") or ())
        summary = str(pending_reply.get("summary") or "理解当前学习问题")[:300]

        if self._is_non_evidence_acknowledgement(compact):
            capability = self.capability_registry.require("learning.request_response", "")
            prompt = "先别急着说懂了。请不用照着我刚才的话，用自己的话讲一遍关键点。"
            step_arguments = {"prompt": prompt, "subject": subject}
            spoken = prompt
            phase = str(pending_reply.get("phase") or "check")
            reason = "孩子只表达听懂，没有提供可评估的独立证据。"
        elif self._needs_different_explanation(compact, intent):
            capability = self.capability_registry.require("learning.explain", "")
            question = (
                f"当前学习目标是：{summary}。孩子说“{transcript[:240]}”。"
                "请换一种更具体、更短的讲法，先定位卡点，再用一个例子讲解，最后只问一个检查问题。"
            )
            step_arguments = {"question": question, "subject": subject, "support_level": 5}
            spoken = "好，我不重复刚才那套说法。我们先找你卡住的那一步，再换个更具体的例子。"
            phase = "remediate"
            reason = "孩子明确表示仍未理解，需要改变讲解策略。"
        else:
            capability = self.capability_registry.require("learning.evaluate_response", "")
            previous_result = pending_reply.get("result")
            if isinstance(previous_result, dict) and isinstance(previous_result.get("result"), dict):
                previous_result = previous_result["result"]
            question_asked = (
                str(previous_result.get("next_check") or "").strip()[:800]
                if isinstance(previous_result, dict) else ""
            )
            from .ordinary_learning_text_projection import source_question_policy, APP_PROMPT
            with self.database.connect() as source_connection:
                source_policy = source_question_policy(source_connection, pending_reply)
            if source_policy['status'] == 'trusted_lesson_no_question':
                capability = self.capability_registry.require('learning.request_response', '')
                step_arguments = {'prompt': APP_PROMPT, 'subject': subject}
                spoken = APP_PROMPT
                phase = str(pending_reply.get('phase') or 'check')
                reason = '原话保留；这段已授权讲解没有明确检查题，先提出应用原提问，不评价这句。'
            else:
                if source_policy['status'] == 'trusted_question':
                    question_asked = source_policy['question']
                step_arguments = {
                    "response": transcript,
                    "subject": subject,
                    **({"question_asked": question_asked} if question_asked else {}),
                }
                spoken = "我先按我们正在学的目标检查你自己的说法，再决定补哪一点。"
                phase = str(pending_reply.get("phase") or "check")
                reason = "这句话是对当前检查问题的回答，必须绑定原目标评估。"

        return AgentPlanDecision(
            objective=ObjectiveDraft(
                mode="continue",
                objective_type="learning",
                summary=summary,
                success_criteria=criteria,
                phase=phase,
            ),
            step=PlannedStep(
                capability=capability,
                arguments=step_arguments,
                reason=reason,
                expected_evidence=capability.expected_evidence,
            ),
            spoken_transition=spoken,
            requires_child_reply=True,
            confidence=1.0,
        )

    def _ordinary_learning_ack_plan(self, *, transcript, pending_reply, decision, action,
                                    control_lane, voice_capture, answer_target, altered):
        """Only the existing exact acknowledgement and its same-goal app prompt."""
        short_ack = bool(control_lane == {'status': 'non_control', 'role': 'acknowledgement'}
            and re.fullmatch(r'(?:嗯+|嗯哼|哦+|好|好的|行|可以|对)[。！!\s]*', transcript))
        if (altered or not voice_capture or voice_capture.get('question_validation') != 'valid'
                or not pending_reply or decision is None
                or control_lane is not None and not short_ack and (
                    control_lane.get('status') != 'non_control'
                    or control_lane.get('role') != 'acknowledgement'
                    or not isinstance(control_lane.get('request_key'), str)
                    or not control_lane['request_key'].strip())
                or not self._is_non_evidence_acknowledgement(re.sub(r'[，。！？、\s]', '', transcript))):
            return False
        binding = self.agent_runtime.learning_reply_binding(pending_reply)
        if (binding is None or not answer_target or answer_target.get('action') != 'evaluate_child_response'
                or answer_target.get('binding') != binding
                or not self.agent_runtime.capture_matches_answer(voice_capture, 'evaluate_child_response', binding)):
            return False
        prompt = '先别急着说懂了。请不用照着我刚才的话，用自己的话讲一遍关键点。'
        subject = str((pending_reply.get('arguments') or {}).get('subject') or '其他').strip()[:40] or '其他'
        return bool(
            decision.step.capability.name == 'learning.request_response'
            and decision.objective.mode == 'continue' and decision.objective.objective_type == 'learning'
            and decision.objective.summary == str(pending_reply.get('summary') or '理解当前学习问题')[:300]
            and decision.objective.success_criteria == tuple(pending_reply.get('success_criteria') or ())
            and decision.objective.phase == str(pending_reply.get('phase') or 'check')
            and decision.step.arguments == {'prompt': prompt, 'subject': subject}
            and decision.requires_child_reply is True
            and decision.step.capability.action_type == 'none'
            and action.get('type') == 'none'
            and action.get('execution_location') == 'server'
            and action.get('capability') == 'learning.request_response'
            and action.get('source') == 'agent_runtime_deterministic'
            and action.get('prompt') == prompt and action.get('subject') == subject)

    def _ordinary_learning_new_question_plan(self, *, transcript, pending_reply, decision, action,
                                             control_lane, voice_capture, answer_target, altered):
        """A verified new original question does not borrow old playback authority."""
        if (altered or not voice_capture or voice_capture.get('question_validation') != 'valid'
                or not pending_reply or decision is None or not control_lane
                or control_lane.get('status') != 'non_control' or control_lane.get('role') != 'question'
                or control_lane.get('role_version') is not None or control_lane.get('question_scope') is not None
                or not isinstance(control_lane.get('request_key'), str) or not control_lane['request_key'].strip()
                or not isinstance(transcript, str) or len(transcript) < 8
                or not self._is_explicit_learning_request(transcript)
                or re.search(r'刚才|上一|前面|上面|这(?:题|道|个)|原题|原文|重新|再讲|换.{0,3}讲法|继续|纠正|没听懂|不懂|不会|卡住', transcript)):
            return False
        binding = self.agent_runtime.learning_reply_binding(pending_reply)
        if (binding is None or not answer_target or answer_target.get('action') != 'evaluate_child_response'
                or answer_target.get('binding') != binding
                or not self.agent_runtime.capture_matches_answer(voice_capture, 'evaluate_child_response', binding)):
            return False
        prior_args = pending_reply.get('arguments') or {}
        prior_result = pending_reply.get('result') or {}
        if not isinstance(prior_args, dict) or not isinstance(prior_result, dict):
            return False
        prior_texts = [prior_args.get('question'), prior_args.get('prompt'), prior_args.get('question_asked'),
                       prior_result.get('next_check'), pending_reply.get('summary')]
        if isinstance(prior_result.get('result'), dict):
            prior_texts.append(prior_result['result'].get('next_check'))
        if any(isinstance(text, str) and len(text.strip()) >= 8 and text.strip() in transcript for text in prior_texts):
            return False
        args = decision.step.arguments
        if (not isinstance(args, dict) or not {'question','subject'} <= set(args)
                or set(args) - {'question','subject','support_level'}
                or args['question'] != transcript or not isinstance(args['subject'], str)
                or not args['subject'].strip() or len(args['subject']) > 40):
            return False
        level = args.get('support_level', 6)
        return bool(
            decision.step.capability.name == 'learning.explain'
            and decision.step.capability.action_type == 'ask_tutor'
            and decision.step.capability.execution_location == 'local_model'
            and decision.objective.mode == 'new' and decision.objective.objective_type == 'learning'
            and type(level) is int and 1 <= level <= 6
            and action.get('type') == 'ask_tutor' and action.get('capability') == 'learning.explain'
            and action.get('execution_location') == 'local_model'
            and action.get('source') == 'agent_runtime_deterministic'
            and action.get('question') == transcript and action.get('subject') == args['subject']
            and action.get('hint_level') == level)

    @staticmethod
    def _is_non_evidence_acknowledgement(compact: str) -> bool:
        return compact in {
            "嗯", "哦", "好", "好的", "知道了", "明白了", "我明白了",
            "懂了", "我懂了", "会了", "我会了", "听懂了", "我听懂了",
        }

    @staticmethod
    def _is_agent_directed_repair(compact: str) -> bool:
        return any(
            phrase in compact
            for phrase in (
                "你为什么听不明白我说的话", "你为什么听不懂我说的话",
                "为什么不听我说", "你没听懂我", "你没有听懂我",
                "你理解错我的意思", "你又理解错了", "答非所问",
                "我没问这个", "我没有问这个", "我没让你讲这个",
                "我换个话题不行吗", "我非得沿着你去说吗",
            )
        )

    @staticmethod
    def _is_stop_current_explanation(compact: str) -> bool:
        return is_direct_voice_control(compact, SPEECH_STOP_PHRASES)

    @staticmethod
    def _needs_different_explanation(compact: str, intent: str) -> bool:
        return intent in {"tutor_still_stuck", "more_tutor_hint"} or any(
            phrase in compact
            for phrase in ("没懂", "不懂", "听不懂", "没明白", "还是不会", "还是不明白", "卡住了")
        )

    @staticmethod
    def _is_current_diagnostic_hint_request(text: str, question: str) -> bool:
        compact = re.sub(r"[\s，。！？!?]", "", text)
        target = r"(?:当前|现在)?这(?:一)?道题"
        hint = r"(?:一个|一点|一下)?(?:小)?提示"
        if re.fullmatch(rf"(?:请)?(?:{target}(?:请)?给我{hint}|给(?:我)?{target}(?:的)?{hint}|我想要{target}的{hint})", compact):
            return True
        # A quoted original may select itself, but no semantic/subject similarity.
        return any(text.strip() == prefix + question for prefix in ("请给我提示：", "给这道题一个提示："))

    @staticmethod
    def _is_explicit_learning_request(text: str) -> bool:
        if is_deictic_visual_question_request(text):
            return False
        compact = re.sub(r"[，。！、\s]", "", text)
        if not compact:
            return False
        if AgentOrchestrator._is_agent_directed_repair(compact):
            return False
        return any(
            phrase in compact
            for phrase in (
                "为什么", "怎么理解", "如何计算", "如何求", "是什么", "什么意思", "有什么区别",
                "不会", "不懂", "没听懂", "卡住", "提示", "教我", "给我讲",
                "讲一下", "讲讲", "解释", "求解", "怎么算", "怎么做", "弄懂",
                "想学", "想知道",
            )
        )

    def _has_independent_capability(self, action: dict[str, Any], state: str) -> bool:
        """Prevent input-owner fallbacks from rewriting an already routed command.

        This is arbitration, not authorization: persist/execute still validate
        the command's original quote, frozen target and current permissions.
        """
        action_type = str(action.get("type") or "none")
        if action_type == "none":
            return bool(action.get("local_only"))
        if action_type in {
            "submit_diagnostic_answer", "submit_pet_vocabulary_answer", "submit_exam_answer",
            "evaluate_child_response", "record_tutor_feedback",
        }:
            return False
        try:
            self.capability_registry.owner_for_action_type(action_type, state)
        except InvalidAgentPlanError:
            return False
        return True

    @staticmethod
    def _canonical_mistake_submission(transcript, focus, capture, *, transformed=False):
        binding = (focus or {}).get('answer_binding')
        if not binding or (focus or {}).get('phase') not in {'attempt', 'diagnosis_probe'}:
            return ('open_mistake_book', '先看清当前错题，再重新说你的答案；刚才那句话不计分。',
                    {'type': 'open_mistake_book', 'local_only': True, 'cloud_transmission_allowed': False})
        target = {'action': 'continue_mistake_dialogue', 'binding': binding,
                  'answer_type': (((focus.get('state') or {}).get('next_activity') or {}).get('item') or {}).get('answer_type')}
        if transformed or (capture and not AgentRuntimeStore.capture_matches_answer(
                capture, target['action'], binding)):
            return AgentOrchestrator._answer_clarification(transcript, None, keep_candidate=False)
        if (AgentRuntimeStore.local_answer_requires_clarification(transcript)
                or not AgentOrchestrator._local_answer_has_shape(transcript, target['answer_type'])):
            return AgentOrchestrator._answer_clarification(transcript, target)
        return ('mistake_dialogue', '我先按这道题核对你的完整回答。',
                {'type': 'continue_mistake_dialogue', 'utterance': transcript,
                 'mistake_answer_binding': binding, 'local_only': True, 'cloud_transmission_allowed': False})

    @staticmethod
    def _local_answer_has_shape(text: str, answer_type: str | None = None) -> bool:
        """Recognize answer syntax, never correctness or topic-keyword overlap.

        Free-form content has no reliable local syntax. Keep it as a candidate
        until the child identifies its role or an authorized planner does so.
        """
        value = text.strip().rstrip("。！! ")
        if not value or re.search(r"[?？]|[吗么呢]$", value):
            return False
        if re.fullmatch(r"(?:这题|这一题)?(?:我)?(?:暂时|现在|还)?(?:不会|不知道|不清楚)(?:做)?", value):
            return True
        if re.fullmatch(r"(?:我的答案是|错题答案是|这题答案是|答案是|我选).+", value):
            return True
        compact = re.sub(r"\s+", "", value)
        if re.fullmatch(r"[A-Ha-h]", compact):
            return True
        number = r"[负正+-]?(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿点]+)"
        if re.fullmatch(r"(?:[A-Za-z](?:=|等于))?" + number + r"(?:(?:/|／|分之)" + number
                        + r")?(?:%|％|个|米|厘米|平方厘米|平方米|分钟|小时|千克|克|元|度)?", compact):
            return True
        if answer_type in {"boolean", "bool"} and compact in {"是", "否", "对", "错", "true", "false"}:
            return True
        return bool(answer_type == "vocabulary" and re.fullmatch(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", value))

    @staticmethod
    def _is_answer_role_confirmation(text: str) -> bool:
        return AgentRuntimeStore.is_answer_role_confirmation(text)

    def _confirmed_local_answer(self, session_id, child_id, target):
        if not target:
            return None
        with self.database.connect() as c:
            prior = c.execute("""SELECT t.*,p.status AS plan_status FROM agent_turn t
                LEFT JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                WHERE t.session_id=? AND t.child_id=? ORDER BY t.rowid DESC LIMIT 1""",
                (session_id, child_id)).fetchone()
        if not prior or prior["tool_status"] == "cancelled" or prior["plan_status"] == "cancelled":
            return None
        action = json.loads(prior["action_json"])
        candidate = action.get("pending_answer_candidate") or {}
        if (action.get("source") != "pending_answer_clarification" or action.get("local_only") is not True
                or candidate.get("target") != target or candidate.get("answer") != prior["transcript"]
                or self.agent_runtime.local_answer_requires_clarification(candidate['answer'])):
            return None
        return {"answer": candidate["answer"], "turn_id": prior["agent_turn_id"],
                "source_voice_input_id": action.get('voice_input_id')}

    @staticmethod
    def _assert_replayed_voice(existing, voice_input_id):
        if voice_input_id is not None and json.loads(existing['action_json']).get('voice_input_id') != voice_input_id:
            raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_BINDING_CHANGED')

    @staticmethod
    def _capture_mismatch_notice_scope(session_id, child_id, target):
        # Only server-owned question identifiers participate. No transcript,
        # capture UUID, response delivery claim, or unknown-target shared key.
        bindings = {
            'submit_diagnostic_answer': {'prompt_id', 'item_version'},
            'submit_exam_answer': {'run_id', 'position', 'item_id', 'source_version'},
            'submit_pet_vocabulary_answer': {'prompt_session_id', 'card_version'},
            'evaluate_child_response': {'objective_id', 'plan_id', 'capability_name', 'phase', 'source_version'},
            'continue_mistake_dialogue': {'session_id', 'child_id', 'mistake_id', 'phase', 'item_id', 'source_version'},
        }
        if type(target) is not dict or type(target.get('action')) is not str:
            return None
        action, binding = target['action'], target.get('binding')
        if action not in bindings or type(binding) is not dict:
            return None
        if action == 'evaluate_child_response' and set(binding) == bindings[action] | {'source_agent_turn_id'}:
            if (type(binding['capability_name']) is not str
                    or binding['capability_name'] not in {'learning.explain', 'learning.evaluate_response', 'learning.request_response'}):
                return None
        elif set(binding) != bindings[action]:
            return None
        def identifier(value):
            return type(value) is str and 0 < len(value) <= 512 and bool(value.strip())
        if not identifier(session_id) or not identifier(child_id):
            return None
        if any(not (type(value) is int and value >= 0) if key == 'position' else not identifier(value)
               for key, value in binding.items()):
            return None
        if action == 'continue_mistake_dialogue' and (binding['session_id'] != session_id or binding['child_id'] != child_id):
            return None
        encoded = json.dumps({'session_id': session_id, 'child_id': child_id, 'action': action, 'binding': binding},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return {'version': 1, 'reason': 'capture_question_mismatch', 'scope_sha256': hashlib.sha256(encoded).hexdigest()}

    @staticmethod
    def _capture_mismatch_notice_from_action(action, response):
        if (type(action) is not dict or response != CAPTURE_QUESTION_MISMATCH_RESPONSE
                or action.get('type') != 'none' or action.get('source') != 'pending_answer_clarification'
                or action.get('local_only') is not True or action.get('cloud_transmission_allowed') is not False
                or action.get('mastery_update_allowed') is not False or action.get('independent_evidence') is not False
                or action.get('requires_child_reply') is not True or 'pending_answer_candidate' in action):
            return None
        notice = action.get('capture_mismatch_speech_notice')
        if (type(notice) is not dict or set(notice) != {'version', 'reason', 'scope_sha256'}
                or type(notice['version']) is not int or notice['version'] != 1
                or notice['reason'] != 'capture_question_mismatch' or type(notice['scope_sha256']) is not str
                or re.fullmatch('[0-9a-f]{64}', notice['scope_sha256']) is None):
            return None
        return notice

    @staticmethod
    def _answer_role_notice_from_action(action, response):
        if (type(action) is not dict or response != ANSWER_ROLE_CLARIFICATION_RESPONSE
                or action.get('type') != 'none' or action.get('source') != 'pending_answer_clarification'
                or action.get('local_only') is not True or action.get('cloud_transmission_allowed') is not False
                or action.get('mastery_update_allowed') is not False or action.get('independent_evidence') is not False
                or action.get('requires_child_reply') is not True):
            return None
        notice = action.get(ANSWER_ROLE_NOTICE_KEY)
        if (type(notice) is not dict or set(notice) !=
                {'version', 'reason', 'scope_sha256', 'first_candidate_sha256'}
                or type(notice['version']) is not int or notice['version'] != 1
                or notice['reason'] != 'answer_role_clarification'
                or any(type(notice[k]) is not str or re.fullmatch('[0-9a-f]{64}', notice[k]) is None
                       for k in ('scope_sha256', 'first_candidate_sha256'))):
            return None
        return notice

    @staticmethod
    def _answer_clarification(text, target, *, keep_candidate=True):
        action = {"type": "none", "source": "pending_answer_clarification", "local_only": True,
                  "cloud_transmission_allowed": False, "mastery_update_allowed": False,
                  "independent_evidence": False, "requires_child_reply": True}
        if keep_candidate:
            action["pending_answer_candidate"] = {"target": target, "answer": text}
        return ("pending_answer_clarification",
                ANSWER_ROLE_CLARIFICATION_RESPONSE,
                action)

    @staticmethod
    def _should_bind_learning_reply(
        intent: str,
        transcript: str,
        action: dict[str, Any] | None = None,
        pending_reply: dict[str, Any] | None = None,
        previous_response: str | None = None,
    ) -> bool:
        if (pending_reply or {}).get("capability_name") in {
            "diagnostic.start", "diagnostic.replay", "pet_vocabulary.start", "pet_vocabulary.submit", "input.choose_prompt",
        }:
            return False
        compact = re.sub(r"[，。！？、\s]", "", transcript)
        if not compact or intent in {"tutor_question", "correct_previous"}:
            return False
        if AgentOrchestrator._is_agent_directed_repair(compact):
            return False
        if AgentOrchestrator._is_stop_current_explanation(compact):
            return False
        pending_phase = str((pending_reply or {}).get("phase") or "")
        if (
            AgentOrchestrator._is_capture_readiness_acknowledgement(transcript)
            and any(marker in pending_phase.lower() for marker in ("capture", "采集", "拍题"))
        ):
            return False
        action_type = str((action or {}).get("type") or "none")
        if action_type != "none" and intent not in {
            "tutor_understood", "tutor_still_stuck", "more_tutor_hint", "simplify_request",
        }:
            # Device, navigation and task commands are complete utterances in
            # their own right. A waiting learning objective must never swallow
            # them and reinterpret them as an answer.
            return False
        blocking_intents = CONVERSATION_CONTROL_INTENTS | {
            "privacy_pause", "pause_observation", "resume_observation", "stop_speech",
            "audio_check", "speech_echo_ignored", "conversation_repair", "prepare_written_question",
            "capture_readiness", "start_pet_vocabulary", "attention_check",
            "camera_visibility_check", "confirmation_followup", "memory_command",
            "behavior_intervention_feedback", "switch_child_profile", "plan_status",
            "open_mistake_book", "request_break", "pause_task", "defer_task",
            "complete_task", "start_task", "capture_question", "retry_capture",
            "cancel_capture", "start_diagnostic", "replay_diagnostic",
            "resume_study", "reflection_complete", "reflection_skip",
        }
        if intent in blocking_intents:
            return False
        non_answer_patterns = (
            "拜拜", "再见", "喂", "你好", "在吗", "我开始学习",
            "那就这么处理", "唤醒名字", "起一个唤醒", "两到十二个字",
            "直接说一个", "我没问问题", "不是在回答",
        )
        if any(pattern in compact for pattern in non_answer_patterns):
            return False
        if len(compact) == 1 and not re.fullmatch(r"[0-9A-Fa-f零〇一二两三四五六七八九十百千万亿对错是否]", compact):
            return False
        context_switch_patterns = (
            "先不学", "不想学", "换个话题", "先停", "算了", "休息",
            "讲故事", "玩游戏", "打开错题", "今天作业", "老师布置",
            "记录作业", "切换到", "换成四年级", "换成初三",
            "PET学习", "PET复习", "开始今天的PET", "我写一道数学题",
        )
        if any(pattern in compact for pattern in context_switch_patterns):
            return False
        return (intent in {"tutor_understood", "tutor_still_stuck", "more_tutor_hint", "simplify_request"}
                or AgentOrchestrator._is_non_evidence_acknowledgement(compact)
                or AgentOrchestrator._needs_different_explanation(compact, intent)
                or AgentOrchestrator._local_answer_has_shape(transcript))

    async def _local_atomic_learning_context(
        self, *, child_id: str, question: str, subject: str
    ) -> dict[str, Any]:
        """Resolve graph-guided follow-up locally, after all cloud planning has ended.

        The returned object is deliberately an allowlist. Raw nodes, relations,
        source quotes and candidate keys never enter the turn action, model
        context manifests, or a cloud request.
        """
        base = {
            "status": "unavailable",
            "cloud_transmission_allowed": False,
            "mastery_update_allowed": False,
            "audit_event_id": None,
            "matched_node_count": 0,
            "approved_node_count": 0,
            "hypothesis_count": 0,
            "diagnostic_prompt": None,
        }
        if self.atomic_companion is None:
            return base
        normalized_subject = subject if subject and subject != "其他" else None
        try:
            context = await asyncio.to_thread(
                self.atomic_companion.context_for_child,
                child_id=child_id,
                query=question,
                subject=normalized_subject,
                limit=6,
            )
        except (AtomicGraphUnavailableError, ValueError, OSError, sqlite3.Error):
            return base

        truthfulness = context.get("truthfulness") if isinstance(context.get("truthfulness"), dict) else {}
        if (
            truthfulness.get("cloud_transmission_allowed") is not False
            or truthfulness.get("mastery_was_updated") is not False
        ):
            return base

        nodes = context.get("nodes") if isinstance(context.get("nodes"), list) else []
        hypotheses = (
            context.get("weakness_hypotheses")
            if isinstance(context.get("weakness_hypotheses"), list)
            else []
        )
        prompt = self._deterministic_atomic_follow_up(hypotheses)
        return {
            **base,
            "status": "matched" if nodes else "no_match",
            "audit_event_id": context.get("audit_event_id"),
            "matched_node_count": len(nodes),
            "approved_node_count": sum(
                1 for node in nodes
                if isinstance(node, dict) and node.get("review_status") == "approved"
            ),
            "hypothesis_count": len(hypotheses),
            "diagnostic_prompt": prompt,
        }

    @staticmethod
    def _deterministic_atomic_follow_up(hypotheses: list[Any]) -> str | None:
        """Turn one unreviewed hypothesis into a question, never a fact claim."""
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict) or hypothesis.get("status") != "question_required":
                continue
            candidate = str(hypothesis.get("prompt") or "").strip()
            quoted = re.search(r"“([^\u201d]{1,80})”", candidate)
            if quoted:
                concept = quoted.group(1).strip()
                concept_head = re.split(r"[，。；：！？,;:!?]", concept, maxsplit=1)[0].strip()
                if (
                    2 <= len(concept_head) <= 32
                    and not concept_head.startswith(("说明", "运用", "解释步骤", "方程两边"))
                ):
                    return f"讲完后我再确认一个小点：你能用自己的话说说“{concept_head}”吗？"
                continue
            if candidate and len(candidate) <= 72 and not re.search(r"[；;]", candidate):
                statement = candidate[:120].rstrip("。！？!? ")
                return f"讲完后我会用一个小问题再确认：{statement}吗？"
        return None

    def _input_dispatch_guard(self, c, session_id, child_id, action_id, transcript, voice_capture, consent_version):
        # These original proofs stay local, including when the model sees redacted text.
        arrival = c.execute('SELECT * FROM learning_input_arrival WHERE client_action_id=?', (action_id,)).fetchone()
        arrival = tuple(arrival) if arrival else None
        parent_sql = """SELECT agent_turn_id,child_id,transcript,action_json,tool_status,tool_error_code
            FROM agent_turn WHERE session_id=? ORDER BY rowid DESC LIMIT 1"""
        parent = c.execute(parent_sql, (session_id,)).fetchone()
        parent = tuple(parent) if parent else None

        def authorize(connection=None):
            with (nullcontext(connection) if connection is not None else self.database.connect()) as current:
                if connection is None:
                    current.execute('BEGIN')
                if self.voice_control_intent.text_consent_version(current) != consent_version:
                    raise InvalidAgentPlanError('VOICE_DISPATCH_CONSENT_CHANGED')
                self.voice_control_intent.assert_current_input(current, session_id, child_id, action_id, transcript, voice_capture)
                now = current.execute('SELECT * FROM learning_input_arrival WHERE client_action_id=?', (action_id,)).fetchone()
                previous = current.execute(parent_sql, (session_id,)).fetchone()
                if (not now or tuple(now) != arrival or (tuple(previous) if previous else None) != parent
                        or (previous and previous['child_id'] != child_id)):
                    raise InvalidAgentPlanError('VOICE_DISPATCH_SOURCE_CHANGED')
        return authorize

    async def _resolve_agent_plan(
        self,
        *,
        session_id: str,
        child_id: str,
        transcript: str,
        known_child_names: list[str],
        recent_dialogue: tuple[dict[str, str], ...],
        require_utterance_binding: bool = False,
        continuation_event: dict[str, Any] | None = None,
        companion_feedback_snapshot: dict[str, Any] | None = None,
        input_revision_arrival: dict[str, Any] | None = None,
        independent_document_dialogue: bool = False,
        foreground_independent_dialogue: bool = False,
        foreground_exam_dialogue: bool = False,
        pending_evaluation_dialogue: bool = False,
        independent_document_role: str | None = None,
        pre_dispatch=None,
    ) -> tuple[AgentPlanDecision, dict[str, Any]] | None:
        """Plan one outcome-oriented step from the registered capability set.

        The older intent adapter remains a bounded migration fallback.  It is
        no longer the place where new child-facing capabilities are added.
        """
        if pre_dispatch is None or not getattr(self.model_gateway, "configured", False):
            return None
        if is_voice_intent_prompt_injection(transcript):
            return None
        blocked_categories = sensitive_categories(transcript)
        if blocked_categories:
            return None
        context = self.agent_runtime.assemble_context(session_id, recent_dialogue)
        context["companion_activity_feedback"] = {"status": (companion_feedback_snapshot or {}).get("status", "missing")}
        context["input_revision_dialogue"] = {"state": ((input_revision_arrival or {}).get("pending") or {}).get("state", "none")}
        if continuation_event:
            context["continuation_event"] = continuation_event
        pending_reply = self.agent_runtime.pending_child_reply(session_id)
        if independent_document_dialogue and pending_reply and not foreground_independent_dialogue:
            return None
        context["conversation_state"] = {
            "awaiting_learning_reply": bool(pending_reply) and pending_reply.get("capability_name") not in {
                "diagnostic.start", "diagnostic.replay", "pet_vocabulary.start", "pet_vocabulary.submit", "input.choose_prompt",
            },
            "pending_question_conflict": (pending_reply or {}).get("conflicting_owners", []),
            "pending_answer_capability": self._pending_answer_capability(pending_reply),
            "learning_reply_phase": pending_reply.get("phase") if pending_reply else None,
            "exam_active": bool(self.exam_simulation and self.exam_simulation.active_for_session(session_id)),
            "vocabulary_active": bool(self.pet_vocabulary and self.pet_vocabulary.active_prompt(session_id)),
        }
        from .mistake_revision_semantics import pending_pair
        from .mistake_dialogue import MistakeDialogueService
        mistake_state = MistakeDialogueService(self.database).state(session_id)
        revision_pending = pending_pair(mistake_state)
        from .mistake_pending_answer import binding as pending_answer_binding
        pending_operation = pending_answer_binding(mistake_state)
        scored_answer_operation = ((mistake_state or {}).get('state') or {}).get('last_scored_answer')
        context['conversation_state']['pending_saved_mistake_answer'] = bool(pending_operation)
        context['conversation_state']['recent_scored_mistake_answer'] = bool(scored_answer_operation)
        context['conversation_state']['mistake_revision_readback'] = (
            {'status': 'awaiting_confirmation', 'scope': 'question_and_original_answer'} if revision_pending else None)
        sanitized_history, history_redactions = self._sanitize_dialogue_history(
            recent_dialogue, known_child_names
        )
        cloud_context = {
            **context,
            "active_companion_activity": ({key: value for key, value in context["active_companion_activity"].items()
                if key in {"activity_type", "status", "remaining_turns", "turn_count", "max_turns"}}
                if context.get("active_companion_activity") else None),
            "teaching_preferences": self.companion_memory.teaching_preferences(child_id),
            "session": {
                key: value for key, value in context["session"].items()
                if key != "session_id"
            },
            "child": {
                "grade": context["child"]["grade"],
                "curriculum_region": context["child"]["curriculum_region"],
            },
            "active_objective": (
                {
                    key: value for key, value in context["active_objective"].items()
                    if key != "objective_id"
                }
                if context.get("active_objective") else None
            ),
            "active_task": (
                {
                    key: value for key, value in context["active_task"].items()
                    if key != "plan_item_id"
                }
                if context.get("active_task") else None
            ),
            "recent_dialogue": list(sanitized_history),
        }
        sanitized_transcript = sanitize_cloud_text(transcript[:1200], known_child_names)
        context_redactions = 0
        context_redaction_types: set[str] = set()

        def sanitize_context(value: Any) -> Any:
            nonlocal context_redactions
            if isinstance(value, dict):
                if (value.get("cloud_transmission_allowed") is False or value.get("local_only") is True
                        or value.get('kind') in {'unplaced_voice_arrival', 'pet_listening_voice_arrival', 'writing_followup_arrival','oral_followup_arrival'}):
                    return {"cloud_transmission_allowed": False}
                return {key: sanitize_context(item) for key, item in value.items()
                        if not key.endswith("_id") and key not in {"payload_json", "path", "image", "audio",
                            'learning_input_unplaced', 'learning_input_unplaced_binding', 'learning_input_unplaced_dialogue', 'learning_input_scope_offer',
                            'learning_input_arrival', 'unplaced_voice_arrival', 'snapshot_json', 'pending_json',
                            'pet_listening_voice_arrival', 'pet_listening_binding', 'pet_listening_dialogue'}}
            if isinstance(value, (list, tuple)):
                return [sanitize_context(item) for item in value]
            if isinstance(value, str):
                if sensitive_categories(value):
                    context_redactions += 1
                    context_redaction_types.add("sensitive_context_omitted")
                    return "[本地敏感内容未上传]"
                safe = sanitize_cloud_text(value, known_child_names)
                context_redactions += safe.redaction_count
                context_redaction_types.update(safe.redaction_types)
                return safe.text
            return value

        if (pending_operation or scored_answer_operation) and not continuation_event:
            # Recovery/correction needs only a boolean control state, not the
            # saved answer, question, score, or its dialogue history.
            cloud_context = {'session': {'state': context['session']['state']},
                'child': cloud_context['child'], 'conversation_state': context['conversation_state']}
        if independent_document_dialogue:
            cloud_context = {'session': {'state': context['session']['state']},
                'child': {'grade': context['child']['grade']},
                'conversation_state': {'awaiting_learning_reply': False,
                    'pending_answer_capability': None, 'pending_question_conflict': []}}
        messages = build_planner_messages(
            sanitize_context(cloud_context), sanitized_transcript.text, self.capability_registry
        )
        independent_capabilities = {'conversation.respond', 'system.inspect_learning', 'learning.explain'}
        if foreground_exam_dialogue:
            independent_capabilities.discard('learning.explain')
            if independent_document_role == 'question':
                independent_capabilities.discard('conversation.respond')
        if pending_evaluation_dialogue and independent_document_role == 'question':
            independent_capabilities.discard('conversation.respond')
        if independent_document_dialogue:
            payload = json.loads(messages[1]['content'])
            payload['available_capabilities'] = [item for item in payload['available_capabilities']
                if item['name'] in independent_capabilities]
            messages = (messages[0], {**messages[1], 'content': json.dumps(payload, ensure_ascii=False)})
            messages = ({**messages[0], 'content': messages[0]['content'] +
                ('本轮为原评价等待期间的独立用途。原作答已经提交，不提供原题或原答案，也不猜测。'
                 'objective.mode=none且requires_child_reply=false，不能更换、暂停或接续原目标。' if pending_evaluation_dialogue else
                 '本轮为与原待答题无关的独立用途。原练习继续等待，不提供原题或原答案，也不猜测。'
                 'objective.mode=none且requires_child_reply=false，不能更换、暂停或接续原目标。' if foreground_independent_dialogue else
                 '本轮为与背景纸页无关的独立问题。背景纸页不是待答题，不猜测纸面内容。') +
                '仅选择列出的能力；讲解question参数必须为本轮完整原话，不改写成其他题目。'
                '不接续旧目标，不发起评分、设备操作或隐含后续动作；after_execution=stop。'}, messages[1])
            if foreground_exam_dialogue:
                messages = ({**messages[0], 'content': messages[0]['content'] +
                    '当前独立测评仍进行中。本轮只查本机资料状态或进行非教学交流；不讲题、不提示答案、'
                    '不另开检查题，不改变或暂停测评计时。需要学习帮助须先走明确的暂停/求助流程。'}, messages[1])
            if pending_evaluation_dialogue:
                messages = ({**messages[0], 'content': messages[0]['content'] +
                    '原作答正在评价。本轮独立问题可查本机资料事实或只讲解当前完整提问，不猜测或评判原答案；'
                    '问候、感受和单独的知识名词不能隐含开课。资料存在性和计划状态必须读取本机事实。'
                    '不创建新的作答等待，不重复或恢复原评价。'}, messages[1])
        manifest = ({'lane': 'independent_document_dialogue_v1', 'history_included': False,
            'local_sources_included': False} if independent_document_dialogue else planner_context_manifest(context))
        if foreground_independent_dialogue:
            manifest = {**manifest, 'lane': 'foreground_independent_dialogue_v1'}
        if pending_evaluation_dialogue:
            manifest = {**manifest, 'lane': 'pending_evaluation_independent_v1'}
        manifest["privacy_transform"] = {
            "policy_version": CLOUD_PRIVACY_POLICY_VERSION,
            "transmitted": True,
            "blocked_categories": [],
            "redaction_count": sanitized_transcript.redaction_count + history_redactions[0] + context_redactions,
            "redaction_types": sorted(set((
                *sanitized_transcript.redaction_types, *history_redactions[1], *context_redaction_types,
            ))),
        }
        call_id = self.repository.start_model_call(
            session_id,
            child_id,
            "agent_plan",
            self.settings.model_provider,
            getattr(self.settings, "voice_intent_model_name", None) or self.settings.model_name,
            sum(len(message["content"]) for message in messages),
            PLANNER_PROMPT_VERSION,
            manifest,
            "medium",
            self.settings.model_provider,
            PLANNER_POLICY_VERSION,
            None,
        )
        request = ModelRequest(
            request_id=call_id,
            child_id=child_id,
            session_id=session_id,
            request_type="agent_plan",
            pre_dispatch=pre_dispatch,
            risk_level="medium",
            context_manifest=manifest,
            provider_preference=self.settings.model_provider,
            policy_version=PLANNER_POLICY_VERSION,
            prompt_version=PLANNER_PROMPT_VERSION,
            knowledge_version=None,
            output_type="dialogue",
            evidence_refs=(),
            messages=messages,
        )
        result = None
        try:
            pre_dispatch()
            gateway_syntax_error = None
            try:
                try:
                    result = await asyncio.wait_for(
                        self.model_gateway.complete(request), timeout=AGENT_PLANNER_TIMEOUT_SECONDS
                    )
                except ModelResponseError as error:
                    if error.code != "MODEL_INVALID_JSON" or error.response_received is not True:
                        raise
                    gateway_syntax_error = error
                    raise InvalidAgentPlanError("planner did not return JSON") from None
                pre_dispatch()
                decision = parse_agent_plan_output(
                    result.content, context["session"]["state"], self.capability_registry
                )
            except InvalidAgentPlanError as first_error:
                if not _repairable_agent_plan_structure(first_error):
                    raise
                if gateway_syntax_error is not None:
                    self.repository.finish_model_call(
                        call_id, "failed", gateway_syntax_error.latency_ms, None,
                        gateway_syntax_error.prompt_tokens, gateway_syntax_error.completion_tokens,
                        error_code="MODEL_INVALID_JSON",
                        verification_status="rejected", safety_status="degraded",
                    )
                else:
                    self.repository.finish_model_call(
                        call_id, "failed", result.latency_ms, len(result.content),
                        result.prompt_tokens, result.completion_tokens,
                        error_code="MODEL_INVALID_AGENT_PLAN",
                        provider=result.provider, model=result.model,
                        verification_status="rejected", safety_status="degraded",
                        fallback_used=result.fallback_used,
                    )
                original_call_id = call_id
                repair_messages = [
                    *messages,
                    *([{"role": "assistant", "content": result.content[:12000]}]
                      if gateway_syntax_error is None else []),
                    {"role": "user", "content": json.dumps({
                        "repair_request": (
                            "Correct the previous JSON object only. Preserve the child's intent and every grounded quote. Do not add an action that was not requested."
                            if gateway_syntax_error is None else
                            "The previous response was not a JSON object. Return the requested JSON object using the original context only. Preserve the child's intent and every grounded quote. Do not add an action that was not requested."
                        ),
                        "validation_error": str(first_error)[:240],
                        "requirements": [
                            "Return exactly one JSON object matching output_schema.",
                            "Use only an available capability and its declared arguments.",
                            "Do not lower confidence dishonestly or invent child words.",
                        ],
                    }, ensure_ascii=False, separators=(",", ":"))},
                ]
                repair_manifest = {
                    **manifest,
                    "planner_repair": {
                        "source_call_id": original_call_id,
                        "validation_error": str(first_error)[:240],
                        "attempt": 1,
                    },
                }
                call_id = self.repository.start_model_call(
                    session_id, child_id, "agent_plan_repair", self.settings.model_provider,
                    getattr(self.settings, "voice_intent_model_name", None) or self.settings.model_name,
                    sum(len(message["content"]) for message in repair_messages),
                    AGENT_PLAN_REPAIR_PROMPT_VERSION, repair_manifest, "medium",
                    self.settings.model_provider, PLANNER_POLICY_VERSION, None,
                )
                request = ModelRequest(
                    request_id=call_id, child_id=child_id, session_id=session_id,
                    request_type="agent_plan", pre_dispatch=pre_dispatch, risk_level="medium",
                    context_manifest=repair_manifest,
                    provider_preference=self.settings.model_provider,
                    policy_version=PLANNER_POLICY_VERSION,
                    prompt_version=AGENT_PLAN_REPAIR_PROMPT_VERSION,
                    knowledge_version=None, output_type="dialogue", evidence_refs=(),
                    messages=repair_messages,
                )
                pre_dispatch()
                result = await asyncio.wait_for(
                    self.model_gateway.complete(request), timeout=AGENT_PLANNER_TIMEOUT_SECONDS
                )
                pre_dispatch()
                decision = parse_agent_plan_output(
                    result.content, context["session"]["state"], self.capability_registry
                )
                manifest = repair_manifest
            if decision.step.capability.name == 'voice.configure_wake_word':
                self.voice_control_intent.wake_word_arguments(
                    {'candidate': decision.step.arguments.get('candidate')}, transcript)
                decision = replace(decision, objective=ObjectiveDraft('none', None, None, (), 'execution'),
                                   after_execution='stop', continuation_scope=())
            if decision.step.capability.name in {'learning_inputs.review_unplaced', 'learning_inputs.review_reclaimed_scope', 'pet.listening_control'}:
                raise InvalidAgentPlanError('CARRYOVER_LOCAL_DECISION_REQUIRED')
            if decision.step.capability.name.startswith("learning_inputs."):
                if not input_revision_arrival or continuation_event:
                    raise InvalidAgentPlanError("INPUT_REVISION_ARRIVAL_REQUIRED")
                manifest["learning_input_arrival_id"] = input_revision_arrival["arrival_id"]
                decision = replace(decision, objective=ObjectiveDraft("none", None, None, (), "execution"),
                                   after_execution="stop", continuation_scope=())
            if decision.step.capability.name == "companion.activity_feedback":
                from .agent_runtime import _validate_activity_feedback_arguments
                _validate_activity_feedback_arguments(decision.step.arguments, transcript)
                binding = (companion_feedback_snapshot or {}).get("binding")
                if binding is None:
                    clarification = (self._activity_selection_question(companion_feedback_snapshot["candidates"])
                                     if (companion_feedback_snapshot or {}).get("status") == "ambiguous"
                                     else "我还没有找到这次活动的记录。你说的是哪次活动？")
                    if (companion_feedback_snapshot or {}).get("status") == "ambiguous":
                        manifest["companion_activity_selection"] = {"candidates": companion_feedback_snapshot["candidates"], "question": clarification}
                    decision = replace(decision, objective=ObjectiveDraft("none", None, None, (), "execution"),
                        step=PlannedStep(self.capability_registry.require("conversation.respond", context["session"]["state"]),
                                         {"response": clarification}, "Clarify activity target", ()),
                        spoken_transition=clarification, requires_child_reply=True, after_execution="stop", continuation_scope=())
                else:
                    decision = replace(decision, objective=ObjectiveDraft("none", None, None, (), "execution"),
                                       after_execution="stop", continuation_scope=())
                    manifest["companion_activity_outcome_binding"] = dict(binding)
            utterance_role = None
            if (decision.step.capability.name in {'diagnostic.submit', 'pet_vocabulary.submit', 'exam.submit',
                                                 'learning.evaluate_response', 'mistake.dialogue'}
                    and self.agent_runtime.local_answer_requires_clarification(transcript)):
                raise InvalidAgentPlanError('ANSWER_INTENT_CLARIFICATION_REQUIRED')
            if (decision.step.capability.name in {'diagnostic.submit', 'pet_vocabulary.submit', 'exam.submit',
                                                 'learning.evaluate_response', 'mistake.dialogue'}
                    and sanitized_transcript.text != transcript):
                raise InvalidAgentPlanError('ANSWER_FULL_LOCAL_UTTERANCE_REQUIRED')
            if require_utterance_binding:
                utterance_role = self._validate_cloud_utterance_binding(
                    result.content, sanitized_transcript.text, decision, pending_reply, original_transcript=transcript)
            if independent_document_dialogue:
                if (decision.step.capability.name not in independent_capabilities
                        or utterance_role not in {'question', 'greeting', 'companionship', 'topic_mention'}
                        or utterance_role != independent_document_role
                        or decision.after_execution != 'stop'
                        or decision.continuation_scope or decision.objective.mode not in {'none', 'new'}):
                    raise InvalidAgentPlanError('INDEPENDENT_QUESTION_SCOPE_CHANGED')
                if foreground_independent_dialogue and (decision.objective.mode != 'none' or decision.requires_child_reply):
                    raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_PLAN_CHANGED')
                if foreground_independent_dialogue and json.loads(result.content).get('utterance_binding', {}).get('quote') != transcript:
                    raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_QUOTE_CHANGED')
                if utterance_role != 'question' and decision.step.capability.name != 'conversation.respond':
                    raise InvalidAgentPlanError('INDEPENDENT_QUESTION_SCOPE_CHANGED')
                if (decision.step.capability.name == 'learning.explain'
                        and decision.step.arguments.get('question') != transcript):
                    raise InvalidAgentPlanError('INDEPENDENT_QUESTION_SOURCE_CHANGED')
            if decision.step.capability.name == 'task.start' and 'start_selector' in decision.step.arguments:
                from .task_start_semantics import validate
                if not require_utterance_binding or not input_revision_arrival or continuation_event:
                    raise InvalidAgentPlanError('TASK_START_CURRENT_INPUT_REQUIRED')
                validate(decision.step.arguments['start_selector'], transcript)
                # A single planner call must retain the voice control execution threshold.
                if decision.confidence < .82:
                    raise InvalidAgentPlanError('planner confidence below execution threshold')
            if decision.confidence < MIN_AGENT_PLAN_CONFIDENCE:
                raise InvalidAgentPlanError("planner confidence below execution threshold")
            if 'revision_confirmation' in decision.step.arguments:
                from .mistake_revision_semantics import valid_arguments
                if (decision.step.capability.name != 'mistake.dialogue' or not revision_pending
                        or not require_utterance_binding or continuation_event or utterance_role != 'acknowledgement'
                        or decision.confidence < .82 or not valid_arguments(decision.step.arguments, transcript)
                        or decision.after_execution != 'stop'):
                    raise InvalidAgentPlanError('mistake revision requires the current bounded acknowledgement')
            if 'pending_answer_control' in decision.step.arguments:
                from .mistake_pending_answer import permits_control, replacement_grounded
                pending_control_kind = decision.step.arguments['pending_answer_control']
                replacement_answer = decision.step.arguments.get('replacement_answer')
                expected_arguments = {'utterance', 'pending_answer_control'}
                if pending_control_kind == 'replace':
                    expected_arguments.add('replacement_answer')
                if (not pending_operation or continuation_event or not require_utterance_binding
                        or utterance_role not in ({'correction'} if pending_control_kind == 'replace' else {'task_request'})
                        or decision.confidence < .82
                        or not permits_control(transcript, pending_control_kind)
                        or pending_control_kind == 'replace'
                           and not replacement_grounded(transcript, replacement_answer)
                        or decision.step.capability.name != 'mistake.dialogue'
                        or set(decision.step.arguments) != expected_arguments
                        or decision.step.arguments.get('utterance') != transcript
                        or decision.after_execution != 'stop'):
                    raise InvalidAgentPlanError('pending answer recovery requires a bounded current request')
            if 'scored_answer_control' in decision.step.arguments:
                from .mistake_pending_answer import replacement_grounded
                from .mistake_scored_correction import withdrawal_grounded
                control = decision.step.arguments.get('scored_answer_control')
                replacement_answer = decision.step.arguments.get('replacement_answer')
                expected_arguments = {'utterance', 'scored_answer_control'}
                if control == 'replace':
                    expected_arguments.add('replacement_answer')
                if (not scored_answer_operation or pending_operation or continuation_event
                        or control not in {'replace', 'withdraw'}
                        or not require_utterance_binding
                        or utterance_role != ('correction' if control == 'replace' else 'task_request')
                        or decision.confidence < .82
                        or decision.step.capability.name != 'mistake.dialogue'
                        or set(decision.step.arguments) != expected_arguments
                        or decision.step.arguments.get('utterance') != transcript
                        or control == 'replace' and not replacement_grounded(transcript, replacement_answer)
                        or control == 'withdraw' and not withdrawal_grounded(transcript)
                        or decision.after_execution != 'stop'):
                    raise InvalidAgentPlanError('scored answer control requires a bounded current request')
            if any(item["request_quote"] not in sanitized_transcript.text for item in decision.continuation_scope):
                raise InvalidAgentPlanError("continuation quote is not grounded")
            conversation = decision.step.capability.name == "conversation.respond"
            spoken_values = [decision.spoken_transition]
            if conversation:
                spoken_values.append(str(decision.step.arguments.get("response") or ""))
            if require_utterance_binding and decision.step.capability.name == "companion.start_activity":
                spoken_values.append(str(decision.step.arguments.get("opening") or ""))
            tool_discussion = conversation and utterance_role == "question"
            if any(not enforce_companion_reply(text, transcript if conversation else "",
                                              tool_discussion_allowed=tool_discussion) for text in spoken_values):
                raise InvalidAgentPlanError("planner speech violates companion safety policy")
            constrained = self.agent_runtime.constrain_learning_plan(decision, context, self.capability_registry)
            if independent_document_dialogue and constrained.step.capability.name not in independent_capabilities:
                raise InvalidAgentPlanError('INDEPENDENT_QUESTION_SCOPE_CHANGED')
            manifest["local_learning_completion_guard"] = constrained != decision
            decision = constrained
        except asyncio.TimeoutError:
            self.repository.finish_model_call(
                call_id, "failed", round(AGENT_PLANNER_TIMEOUT_SECONDS * 1000), 0,
                error_code="AGENT_PLAN_TIMEOUT", verification_status="rejected",
                safety_status="degraded",
            )
            return None
        except InvalidAgentPlanError as error:
            validation_error = str(error)
            failure_codes = {
                "planner confidence below execution threshold": "MODEL_AGENT_PLAN_LOW_CONFIDENCE",
                "planner speech violates companion safety policy": "MODEL_AGENT_PLAN_UNSAFE_REPLY",
                "missing current utterance binding": "MODEL_AGENT_PLAN_MISSING_BINDING",
                "invalid utterance role": "MODEL_AGENT_PLAN_INVALID_ROLE",
                "utterance quote is not grounded": "MODEL_AGENT_PLAN_UNGROUNDED_QUOTE",
                "planner contradicts child's explicit subject": "MODEL_AGENT_PLAN_SUBJECT_CONTRADICTION",
                "no learning request in current utterance": "MODEL_AGENT_PLAN_UNREQUESTED_LESSON",
                "no request to start a learning objective": "MODEL_AGENT_PLAN_UNREQUESTED_LESSON",
                "no pending learning response": "MODEL_AGENT_PLAN_NO_PENDING_ANSWER",
                "no active learning exchange to continue": "MODEL_AGENT_PLAN_NO_PENDING_ANSWER",
                "ambiguous speech cannot trigger a capability": "MODEL_AGENT_PLAN_AMBIGUOUS_ACTION",
            }
            self.repository.finish_model_call(
                call_id, "failed", result.latency_ms if result else None,
                len(result.content) if result else 0,
                result.prompt_tokens if result else None,
                result.completion_tokens if result else None,
                error_code=failure_codes.get(validation_error, (
                    f"MODEL_{validation_error}"
                    if len(validation_error) <= 80 and re.fullmatch(r"[A-Z][A-Z0-9_]+", validation_error)
                    else "MODEL_INVALID_AGENT_PLAN"
                )),
                provider=result.provider if result else None,
                model=result.model if result else None,
                verification_status="rejected", safety_status="degraded",
                fallback_used=result.fallback_used if result else False,
            )
            return None
        except (ModelNotConfiguredError, ModelResponseError) as error:
            consumed = (
                isinstance(error, ModelResponseError)
                and error.response_received is True
                and type(error.code) is str
                and (
                    error.code in {
                        "MODEL_OUTPUT_TRUNCATED", "MODEL_OUTPUT_TERMINATED", "MODEL_INVALID_JSON",
                        "MODEL_EMPTY_RESPONSE", "MODEL_TIMEOUT", "MODEL_NETWORK_ERROR",
                        "MODEL_INVALID_RESPONSE", "MODEL_REQUEST_FAILED", "MODEL_ROUTE_UNAVAILABLE",
                        "MODEL_ALL_ROUTES_UNAVAILABLE",
                    }
                    or re.fullmatch(r"MODEL_HTTP_[1345][0-9]{2}", error.code) is not None
                )
            )
            self.repository.finish_model_call(
                call_id, "failed", error.latency_ms if consumed else None,
                None if consumed else 0,
                error.prompt_tokens if consumed else None,
                error.completion_tokens if consumed else None,
                error_code=error.code if consumed else "MODEL_AGENT_PLAN_FAILED",
                verification_status="rejected", safety_status="degraded",
            )
            return None
        except Exception:
            self.repository.finish_model_call(
                call_id, "failed", None, 0, error_code="AGENT_PLAN_INTERNAL_ERROR",
                verification_status="rejected", safety_status="degraded",
            )
            return None
        self.repository.finish_model_call(
            call_id, "completed", result.latency_ms, len(result.content),
            result.prompt_tokens, result.completion_tokens,
            provider=result.provider, model=result.model,
            verification_status="verified", safety_status="passed",
            fallback_used=result.fallback_used,
        )
        from .public_reply_projection import record_candidate, approved_routes
        if input_revision_arrival and not continuation_event and getattr(result, 'reply_provenance', None):
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                pre_dispatch(c)
                original_arrival = c.execute('SELECT client_action_id FROM learning_input_arrival WHERE arrival_id=?',
                    (input_revision_arrival['arrival_id'],)).fetchone()
                public_call = record_candidate(c, call_id=call_id,
                    action_id=original_arrival[0] if original_arrival else None, result=result, decision=decision,
                    messages=request.messages, context=context, known_child_names=known_child_names,
                    routes=approved_routes(self.settings))
                c.commit()
            if public_call:
                manifest = {**manifest, 'public_reply_model_call_id': public_call}
        if decision.step.capability.name in {'break.begin', 'break.resume'} and require_utterance_binding:
            manifest = {**manifest, 'break_control_model_call_id': call_id}
        if decision.step.capability.name == 'task.start' and 'start_selector' in decision.step.arguments:
            from .task_start_semantics import record_plan_selection
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                pre_dispatch()
                record_plan_selection(c, session_id, child_id, input_revision_arrival['arrival_id'],
                    call_id, transcript, decision.step.arguments['start_selector'])
                c.commit()
            manifest = {**manifest, 'task_start_model_call_id': call_id}
        if 'revision_confirmation' in decision.step.arguments:
            from .mistake_revision_semantics import record
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                pre_dispatch()
                semantics = record(c, call_id, revision_pending, decision.step.arguments['revision_confirmation'], transcript)
                c.commit()
            manifest = {**manifest, 'mistake_revision_semantics': semantics}
        if 'pending_answer_control' in decision.step.arguments:
            from .mistake_pending_answer import record
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                pre_dispatch()
                record(c, call_id, pending_operation, transcript,
                       decision.step.arguments['pending_answer_control'],
                       decision.step.arguments.get('replacement_answer'))
                c.commit()
            manifest = {**manifest, 'pending_answer_operation': pending_operation,
                        'pending_answer_control_model_call_id': call_id}
        if 'scored_answer_control' in decision.step.arguments:
            from .mistake_scored_correction import record_model_authorization
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                pre_dispatch()
                record_model_authorization(c, call_id, scored_answer_operation, transcript,
                    decision.step.arguments.get('replacement_answer'),
                    decision.step.arguments['scored_answer_control'])
                c.commit()
            manifest = {**manifest, 'scored_answer_operation': scored_answer_operation,
                        'scored_answer_control_model_call_id': call_id}
        # A model's role classification cannot authorize closing the day.
        # Apply this to every planner caller, including continuation paths.
        if (decision.step.capability.name == "reflection.complete"
                and not self._direct_reflection_command(transcript, REFLECTION_FINISH_PHRASES)):
            confirmation = "复盘和已保存的内容会保留。可以继续补充；需要结束时，请说‘保存复盘并结束今天学习’。"
            decision = AgentPlanDecision(
                objective=ObjectiveDraft("none", None, None, (), "clarify_reflection_close"),
                step=PlannedStep(
                    self.capability_registry.require("conversation.respond", context["session"]["state"]),
                    {"response": confirmation},
                    "结束复盘需要孩子明确的当前结束指令，模型分类不能替代授权。", (),
                ),
                spoken_transition=confirmation, requires_child_reply=True, confidence=1.0,
            )
            manifest = {**manifest, "reflection_close_guard": {
                "explicit_completion_confirmed": False, "model_command_discarded": True,
            }}
        return decision, manifest

    @staticmethod
    def _pending_answer_capability(pending_reply: dict[str, Any] | None) -> str | None:
        if not pending_reply:
            return None
        source = pending_reply.get("capability_name")
        if source == "input.choose_prompt":
            return None
        if source in {"diagnostic.start", "diagnostic.replay"}:
            return "diagnostic.submit"
        if source in {"pet_vocabulary.start", "pet_vocabulary.submit"}:
            return "pet_vocabulary.submit"
        if source in {"exam.start", "exam.submit", "exam.resume"}:
            return "exam.submit"
        return "learning.evaluate_response"

    @staticmethod
    def _validate_cloud_utterance_binding(content: str, transcript: str, decision: AgentPlanDecision,
                                        pending_reply: dict[str, Any] | None, *, original_transcript: str | None = None) -> str:
        if (decision.step.capability.name in {'diagnostic.submit', 'pet_vocabulary.submit', 'exam.submit',
                                             'learning.evaluate_response', 'mistake.dialogue'}
                and original_transcript is not None and transcript != original_transcript):
            raise InvalidAgentPlanError('ANSWER_FULL_LOCAL_UTTERANCE_REQUIRED')
        value = content.strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value).strip()
        binding = json.loads(value).get("utterance_binding")
        if not isinstance(binding, dict):
            raise InvalidAgentPlanError("missing current utterance binding")
        role = binding.get("role")
        quote = binding.get("quote")
        if role not in {"question", "task_request", "answer", "acknowledgement", "correction",
                        "greeting", "topic_mention", "companionship", "unknown"}:
            raise InvalidAgentPlanError("invalid utterance role")
        if not isinstance(quote, str) or not quote.strip() or quote not in transcript:
            raise InvalidAgentPlanError("utterance quote is not grounded")
        capability = decision.step.capability.name
        if capability in {'break.begin', 'break.resume'}:
            if role != 'task_request' or quote != transcript or (original_transcript is not None and transcript != original_transcript):
                raise InvalidAgentPlanError('break control requires the complete child request')
            from .voice_control_intent import VoiceControlIntent
            checked = VoiceControlIntent.parse(json.dumps({'role': 'control', 'control': capability,
                'quote': quote, 'confidence': decision.confidence}), transcript)
            if checked['status'] != 'control':
                raise InvalidAgentPlanError('break control requires an unambiguous child request')
        if capability == 'diagnostic.start' and (
            role != 'task_request' or quote != transcript
            or (original_transcript is not None and transcript != original_transcript)
        ):
            raise InvalidAgentPlanError('DIAGNOSTIC_CURRENT_REQUEST_REQUIRED')
        if capability == 'mistake.submit_response' and (role != 'answer' or quote != transcript):
            raise InvalidAgentPlanError('answer must bind the complete child utterance')
        if 'pending_answer_control' in decision.step.arguments and quote != transcript:
            raise InvalidAgentPlanError('recovery control must bind the complete child utterance')
        if capability in {"learning.explain", "learning.evaluate_response"}:
            spoken_subjects = {subject for subject in ("数学", "语文", "英语", "物理", "化学", "生物", "历史", "地理", "道德与法治")
                               if subject in transcript}
            if len(spoken_subjects) == 1 and decision.step.arguments.get("subject") not in spoken_subjects:
                raise InvalidAgentPlanError("planner contradicts child's explicit subject")
        if capability == "learning.explain" and role not in {"question", "correction", "task_request"}:
            raise InvalidAgentPlanError("no learning request in current utterance")
        if capability in {"learning.evaluate_response", "diagnostic.submit", "pet_vocabulary.submit", "exam.submit"} and (
            role != "answer" or capability != AgentOrchestrator._pending_answer_capability(pending_reply)
        ):
            raise InvalidAgentPlanError("no pending learning response")
        if capability in {"learning.evaluate_response", "diagnostic.submit", "pet_vocabulary.submit", "exam.submit"}:
            submitted = decision.step.arguments.get("response" if capability == "learning.evaluate_response" else "answer")
            if quote != transcript or submitted not in (transcript, AgentOrchestrator._spoken_answer(transcript)):
                raise InvalidAgentPlanError("answer must bind the complete child utterance")
        if capability == "learning.request_response" and decision.objective.mode == "new" and role not in {
            "question", "correction", "task_request",
        }:
            raise InvalidAgentPlanError("no request to start a learning objective")
        if capability == "learning.request_response" and role not in {"question", "correction", "task_request"} and not pending_reply:
            raise InvalidAgentPlanError("no active learning exchange to continue")
        if capability != "conversation.respond" and role in {"greeting", "topic_mention", "unknown"}:
            raise InvalidAgentPlanError("ambiguous speech cannot trigger a capability")
        return role

    def _record_pending_behavior_feedback(
        self, child_id: str, session_id: str, transcript: str, client_action_id: str
    ) -> tuple[str, str, dict] | None:
        """Map the three explicit spoken answers to the same durable UI feedback.

        It deliberately recognizes only short, unambiguous phrases while a current
        local prompt exists; other study dialogue keeps its established routing.
        """
        choice = self._behavior_feedback_choice(transcript)
        if self.behavior_model is None:
            return None
        feedback_action_id = f"voice:{client_action_id}"
        payload_hash = self._feedback_payload_hash(transcript)
        recovered = self.behavior_model.feedback_for_action(feedback_action_id, child_id, session_id)
        if recovered:
            if recovered.get("payload_hash") != payload_hash:
                raise AgentActionPayloadConflictError("Voice feedback action was reused with a different transcript.")
            return self._behavior_feedback_result(
                recovered["feedback"], recovered.get("note"),
                recovered.get("candidate_state"), recovered.get("trigger_source"),
            )
        if not choice:
            return None
        pending = self.behavior_model.pending_feedback(
            child_id, session_id, datetime.now(timezone.utc).isoformat()
        )
        if pending.get("status") != "pending":
            return None
        intervention_id = pending["intervention"]["intervention_id"]
        accepted, note, response = choice
        self.behavior_model.record_feedback(
            intervention_id, accepted, datetime.now(timezone.utc).isoformat(), note,
            feedback_action_id, child_id, session_id, payload_hash,
        )
        return self._behavior_feedback_result(
            "accepted" if accepted else "declined", note,
            pending["intervention"].get("candidate_state"),
            pending["intervention"].get("trigger_source"),
        )

    def _record_habit_experiment_response(
        self, child_id: str, transcript: str, client_action_id: str
    ) -> tuple[str, str, dict] | None:
        if self.behavior_model is None:
            return None
        compact = re.sub(r"[，。！？、\s]", "", transcript)
        accepted = compact in {"可以试试", "可以", "试试", "我愿意试试", "开始这个实验"}
        declined = compact in {"不做这个实验", "不想做这个实验", "不要这个实验", "先不做"}
        stop = compact in {"停止这个实验", "结束这个实验", "不想继续实验"}
        if stop:
            active = self.behavior_model.active_habit_experiment(child_id)
            experiment = active.get("experiment")
            if active.get("status") != "active" or not experiment:
                return None
            self.behavior_model.stop_habit_experiment(
                experiment["experiment_id"], child_id,
                datetime.now(timezone.utc).isoformat(), f"voice:{client_action_id}",
            )
            return (
                "habit_experiment_response",
                f"好，“{experiment['title']}”已经停止。我们保留已经发生的记录，但不会再按这个实验提醒你。",
                {"type": "none"},
            )
        if not accepted and not declined:
            return None
        pending = self.behavior_model.pending_habit_experiment(child_id)
        experiment = pending.get("experiment")
        if pending.get("status") != "proposed" or not experiment:
            return None
        saved = self.behavior_model.respond_habit_experiment(
            experiment["experiment_id"], child_id, accepted,
            datetime.now(timezone.utc).isoformat(), f"voice:{client_action_id}",
        )
        if accepted:
            response = (
                f"好，我们从今天开始“{saved['title']}”七天小实验。"
                f"你只需要记住这一件事：{saved['child_action']}。我会少说教，只在合适的时候提醒一次。"
            )
        else:
            response = f"好，不做“{saved['title']}”这个实验。我不会因为你拒绝就增加提醒。"
        return "habit_experiment_response", response, {"type": "none"}

    @staticmethod
    def _feedback_payload_hash(transcript: str) -> str:
        return hashlib.sha256(transcript.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def _behavior_feedback_result(
        feedback: str,
        note: str | None,
        candidate_state: str | None = None,
        trigger_source: str | None = None,
    ) -> tuple[str, str, dict]:
        if feedback == "accepted":
            if candidate_state == "blocked_candidate" and trigger_source == "document_monitor":
                return "behavior_intervention_feedback", "好，请把手移开并保持页面稳定。我先在本机检查当前页，再请你确认是哪道题。", {
                    "type": "control_document_monitor", "command": "inspect",
                    "feedback": "accepted", "response_kind": "accepted",
                }
            return "behavior_intervention_feedback", "好，我记下你愿意试一试；需要时随时叫我。", {
                "type": "behavior_intervention_feedback", "feedback": "accepted", "response_kind": "accepted",
            }
        if note == "less_like_this":
            response = "好，我会让这类提醒更少，并先询问你。"
        else:
            response = "好，我记下你想稍后再说。"
        return "behavior_intervention_feedback", response, {
            "type": "behavior_intervention_feedback", "feedback": "declined", "response_kind": note or "later",
        }

    @staticmethod
    def _assert_action_payload(existing, session_id: str, transcript: str) -> None:
        transcript_matches = existing["transcript"] == transcript
        if not transcript_matches and "action_json" in existing.keys():
            try:
                action = json.loads(existing["action_json"] or "{}")
                expected_hash = (action.get("speech_echo_filter") or {}).get("raw_transcript_sha256")
                transcript_matches = expected_hash == hashlib.sha256(transcript.encode("utf-8")).hexdigest()
                if action.get('voice_input_binding'):
                    transcript_matches = re.sub(r'\s+', ' ', existing['transcript']).strip() == re.sub(r'\s+', ' ', transcript).strip()
            except (TypeError, json.JSONDecodeError):
                transcript_matches = False
        if existing["session_id"] != session_id or not transcript_matches:
            raise AgentActionPayloadConflictError("Agent action id was reused with a different session or transcript.")

    @staticmethod
    def _behavior_feedback_choice(transcript: str) -> tuple[bool, str | None, str] | None:
        compact = re.sub(r"[，。！？、\s]", "", transcript)
        if compact in {"愿意", "我愿意", "好", "好的", "需要", "帮我看看", "好帮我看看", "一起看看"}:
            return True, None, "好，我记下你愿意试一试；需要时随时叫我。"
        if compact in {"稍后", "等会儿", "晚点"}:
            return False, "later", "好，我记下你想稍后再说。"
        if compact in {"这类提醒少一点", "这类提示少一点"}:
            return False, "less_like_this", "好，我会让这类提醒更少，并先询问你。"
        return None

    @staticmethod
    def _clarification_count(connection, session_id: str, state: str) -> int:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM agent_turn
            WHERE session_id = ? AND intent = 'clarify_intent'
              AND created_at >= COALESCE(
                (
                  SELECT MAX(occurred_at) FROM session_transition
                  WHERE session_id = ? AND to_state = ?
                ),
                (
                  SELECT started_at FROM daily_session WHERE session_id = ?
                )
              )
            """,
            (session_id, session_id, state, session_id),
        ).fetchone()
        return int(row["count"] if row else 0)

    @staticmethod
    def _clarification_limit_result(state: str) -> tuple[str, str, dict]:
        return (
            "clarification_limit_reached",
            clarification_limit_response(state),
            {
                "type": "none",
                "source": "local_policy",
                "clarification_limit_reached": True,
                "state": state,
            },
        )

    async def _resolve_cloud_intent(
        self,
        *,
        session_id: str,
        child_id: str,
        state: str,
        transcript: str,
        break_minutes: int,
        known_child_names: list[str],
        recent_dialogue: tuple[dict[str, str], ...] = (),
        pre_dispatch=None,
    ) -> CloudIntentDecision | None:
        if pre_dispatch is None or not getattr(self.model_gateway, "configured", False):
            return None
        if is_voice_intent_prompt_injection(transcript):
            return CloudIntentDecision(action=(
                "supportive_chat",
                "这句话像是在要求我改变内部判断方式，我不会照做。你可以直接告诉我是要继续、完成任务、休息，还是需要帮助。",
                {"type": "none", "source": "local_prompt_guard"},
            ))
        sanitized = sanitize_cloud_text(transcript[:800], known_child_names)
        blocked_categories = sensitive_categories(transcript)
        privacy_transform = {
            "policy_version": CLOUD_PRIVACY_POLICY_VERSION,
            "transmitted": not bool(blocked_categories),
            "blocked_categories": blocked_categories,
            "redaction_count": sanitized.redaction_count,
            "redaction_types": list(sanitized.redaction_types),
        }
        sanitized_history, history_redactions = self._sanitize_dialogue_history(
            recent_dialogue, known_child_names
        )
        privacy_transform["redaction_count"] += history_redactions[0]
        privacy_transform["redaction_types"] = sorted(set((
            *privacy_transform["redaction_types"], *history_redactions[1],
        )))
        messages = build_classifier_messages(state, sanitized.text, sanitized_history)
        context_manifest = {
            "object_types": ["session_state", "voice_turn_text", "recent_dialogue"],
            "session_id": session_id,
            "current_state": state,
            "raw_audio_or_video": False,
            "context_limits": {"transcript_chars": 800, "recent_turns": 4},
            "privacy_transform": privacy_transform,
        }
        call_id = self.repository.start_model_call(
            session_id,
            child_id,
            "voice_agent_intent",
            self.settings.model_provider,
            self.settings.voice_intent_model_name or self.settings.model_name,
            sum(len(message["content"]) for message in messages),
            VOICE_PROMPT_VERSION,
            context_manifest,
            "low" if not blocked_categories else "high",
            self.settings.model_provider,
            VOICE_POLICY_VERSION,
            None,
        )
        if blocked_categories:
            self.repository.finish_model_call(
                call_id,
                "failed",
                0,
                0,
                error_code="SENSITIVE_TEXT_LOCAL_ONLY",
                verification_status="rejected",
                safety_status="blocked",
            )
            return None

        request = ModelRequest(
            request_id=call_id,
            child_id=child_id,
            session_id=session_id,
            request_type="voice_agent_intent",
            pre_dispatch=pre_dispatch,
            risk_level="low",
            context_manifest=context_manifest,
            provider_preference=self.settings.model_provider,
            policy_version=VOICE_POLICY_VERSION,
            prompt_version=VOICE_PROMPT_VERSION,
            knowledge_version=None,
            output_type="dialogue",
            evidence_refs=(),
            messages=messages,
        )
        result = None
        try:
            pre_dispatch()
            result = await asyncio.wait_for(
                self.model_gateway.complete(request),
                timeout=CLOUD_INTENT_TIMEOUT_SECONDS,
            )
            pre_dispatch()
            candidate = parse_classifier_output(result.content, state)
        except InvalidVoiceIntentOutputError:
            self.repository.finish_model_call(
                call_id,
                "failed",
                result.latency_ms if result else None,
                len(result.content) if result else 0,
                result.prompt_tokens if result else None,
                result.completion_tokens if result else None,
                error_code="MODEL_INVALID_INTENT_OUTPUT",
                provider=result.provider if result else None,
                model=result.model if result else None,
                verification_status="rejected",
                safety_status="degraded",
                fallback_used=result.fallback_used if result else False,
            )
            return None
        except asyncio.TimeoutError:
            self.repository.finish_model_call(
                call_id,
                "failed",
                round(CLOUD_INTENT_TIMEOUT_SECONDS * 1000),
                0,
                error_code="VOICE_INTENT_TIMEOUT",
                verification_status="rejected",
                safety_status="degraded",
            )
            return None
        except ModelNotConfiguredError:
            self.repository.finish_model_call(
                call_id,
                "failed",
                0,
                0,
                error_code="MODEL_NOT_CONFIGURED",
                verification_status="rejected",
                safety_status="degraded",
            )
            return None
        except ModelResponseError as error:
            self.repository.finish_model_call(
                call_id,
                "failed",
                None,
                0,
                error_code=error.code,
                verification_status="rejected",
                safety_status="degraded",
            )
            return None
        except Exception:
            self.repository.finish_model_call(
                call_id,
                "failed",
                None,
                0,
                error_code="MODEL_INTENT_FAILED",
                verification_status="rejected",
                safety_status="degraded",
            )
            return None

        accepted = candidate.confidence >= MIN_EXECUTE_CONFIDENCE
        self.repository.finish_model_call(
            call_id,
            "completed",
            result.latency_ms,
            len(result.content),
            result.prompt_tokens,
            result.completion_tokens,
            provider=result.provider,
            model=result.model,
            verification_status="verified" if accepted else "partially_verified",
            safety_status="passed",
            fallback_used=result.fallback_used,
        )
        if candidate.intent == "supportive_chat":
            return CloudIntentDecision(companion_eligible=candidate.confidence >= MIN_CLARIFY_CONFIDENCE)
        if candidate.confidence < MIN_CLARIFY_CONFIDENCE:
            return None
        if not accepted:
            return CloudIntentDecision(action=(
                "clarify_intent", clarification_response(state), {
                    "type": "none", "source": "cloud_classifier",
                    "candidate_intent": candidate.intent, "confidence": round(candidate.confidence, 3),
                },
            ))
        resolved = self._cloud_intent_to_local_action(
            state, transcript, break_minutes, candidate.intent, recent_dialogue
        )
        resolved[2]["source"] = "cloud_classifier"
        resolved[2]["confidence"] = round(candidate.confidence, 3)
        return CloudIntentDecision(action=resolved)

    async def _generate_companion_reply(
        self,
        *,
        session_id: str,
        child_id: str,
        state: str,
        transcript: str,
        previous_response: str | None,
        known_child_names: list[str],
        recent_dialogue: tuple[dict[str, str], ...] = (),
        pre_dispatch=None,
        activity_context=None,
        auto_advance=True,
    ) -> CompanionReply | None:
        if pre_dispatch is None:
            return None
        blocked_categories = sensitive_categories(transcript)
        if blocked_categories:
            return None
        sanitized = sanitize_cloud_text(transcript[:500], known_child_names)
        sanitized_previous = sanitize_cloud_text((previous_response or "")[:180], known_child_names)
        sanitized_history, history_redactions = self._sanitize_dialogue_history(
            recent_dialogue, known_child_names
        )
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone()
        active_activity = activity_context if activity_context is not None else self.agent_runtime.active_companion_activity(session_id)
        if active_activity:
            active_activity = {key: value for key, value in active_activity.items()
                               if key in {"activity_type", "status", "remaining_turns", "turn_count", "max_turns"}}
        messages = build_companion_messages(
            grade=child["grade"] if child else "中小学",
            session_state=state,
            transcript=sanitized.text,
            previous_response=sanitized_previous.text or None,
            recent_dialogue=sanitized_history,
            active_activity=active_activity,
        )
        teaching_preferences = self.companion_memory.teaching_preferences(child_id) if auto_advance else None
        if teaching_preferences:
            messages[0]["content"] += "\n孩子明确选择的讲解方式（不是能力评价）：" + json.dumps(teaching_preferences, ensure_ascii=False)
        context_manifest = {
            "teaching_preferences": teaching_preferences,
            "raw_long_term_memories_transmitted": False,
            "object_types": [
                "child_grade", "session_state", "voice_turn_text", "recent_dialogue",
                *( ["active_companion_activity"] if active_activity else [] ),
            ],
            "session_id": session_id,
            "current_state": state,
            "raw_audio_or_video": False,
            "context_limits": {"transcript_chars": 500, "previous_reply_chars": 180},
            "privacy_transform": {
                "policy_version": CLOUD_PRIVACY_POLICY_VERSION,
                "transmitted": True,
                "blocked_categories": [],
                "redaction_count": sanitized.redaction_count + sanitized_previous.redaction_count + history_redactions[0],
                "redaction_types": sorted(set((*sanitized.redaction_types, *sanitized_previous.redaction_types, *history_redactions[1]))),
            },
        }
        call_id = self.repository.start_model_call(
            session_id, child_id, "companion_dialogue", self.settings.model_provider,
            self.settings.model_name,
            sum(len(message["content"]) for message in messages), COMPANION_PROMPT_VERSION,
            context_manifest, "low", self.settings.model_provider,
            COMPANION_POLICY_VERSION, None,
        )
        request = ModelRequest(
            request_id=call_id,
            child_id=child_id,
            session_id=session_id,
            request_type="companion_dialogue",
            pre_dispatch=pre_dispatch,
            risk_level="low",
            context_manifest=context_manifest,
            provider_preference=self.settings.model_provider,
            policy_version=COMPANION_POLICY_VERSION,
            prompt_version=COMPANION_PROMPT_VERSION,
            knowledge_version=None,
            output_type="dialogue",
            evidence_refs=(),
            messages=messages,
        )
        try:
            pre_dispatch()
            result = await asyncio.wait_for(
                self.model_gateway.complete(request), timeout=COMPANION_TIMEOUT_SECONDS
            )
            pre_dispatch()
        except asyncio.TimeoutError:
            self.repository.finish_model_call(
                call_id, "failed", round(COMPANION_TIMEOUT_SECONDS * 1000), 0,
                error_code="COMPANION_TIMEOUT", verification_status="rejected", safety_status="degraded",
            )
            return None
        except ModelNotConfiguredError:
            self.repository.finish_model_call(
                call_id, "failed", 0, 0, error_code="MODEL_NOT_CONFIGURED",
                verification_status="rejected", safety_status="degraded",
            )
            return None
        except ModelResponseError as error:
            self.repository.finish_model_call(
                call_id, "failed", None, 0, error_code=error.code,
                verification_status="rejected", safety_status="degraded",
            )
            return None
        except Exception:
            self.repository.finish_model_call(
                call_id, "failed", None, 0, error_code="MODEL_COMPANION_FAILED",
                verification_status="rejected", safety_status="degraded",
            )
            return None
        reply = enforce_companion_reply(result.content, transcript)
        if reply is None:
            self.repository.finish_model_call(
                call_id, "failed", result.latency_ms, len(result.content),
                result.prompt_tokens, result.completion_tokens,
                error_code="MODEL_UNSAFE_COMPANION_OUTPUT", provider=result.provider,
                model=result.model, verification_status="rejected", safety_status="blocked",
                fallback_used=result.fallback_used,
            )
            fallback = safe_companion_fallback(transcript)
            if active_activity and auto_advance:
                fallback = self._advance_companion_activity_and_resume(session_id, fallback)
            return CompanionReply(fallback, "local_companion_safety")
        self.repository.finish_model_call(
            call_id, "completed", result.latency_ms, len(reply),
            result.prompt_tokens, result.completion_tokens,
            provider=result.provider, model=result.model,
            verification_status="partially_verified", safety_status="passed",
            fallback_used=result.fallback_used,
        )
        if active_activity and auto_advance:
            reply = self._advance_companion_activity_and_resume(session_id, reply)
        return CompanionReply(reply, "cloud_companion")

    def _advance_companion_activity_and_resume(self, session_id: str, reply: str) -> str:
        activity_result = self.agent_runtime.advance_companion_activity(session_id)
        if not activity_result or not activity_result.get("natural_end"):
            return reply
        return f"{reply} 这轮活动到这里。刚才实际做了哪些，感觉怎么样？之后想回到原来的学习，还是先休息？"

    def _cloud_intent_to_local_action(
        self, state: str, transcript: str, break_minutes: int, intent: str,
        recent_dialogue: tuple[dict[str, str], ...] = (),
    ) -> tuple[str, str, dict]:
        if intent == "plan_status":
            return "plan_status", "我来看看今天的计划和已经完成的任务。", {"type": "describe_plan"}
        if intent == "open_mistake_book":
            return intent, "好，我现在打开你的错题本。", {"type": "open_mistake_book"}
        if intent == "switch_child_profile":
            return intent, "我可以切换，但要先确认独立档案。请明确说“我是初三”或“我是四年级”。", {"type": "none"}
        if intent in {"tutor_question", "correct_previous"}:
            question = self._reconstruct_tutor_question(transcript, recent_dialogue)
            subject = self._subject_from_text(question.replace(" ", ""))
            hint_level = self._tutor_hint_level(question)
            response = "明白了，我按你纠正后的问题重新讲。" if intent == "correct_previous" else "好，我按你的问题来讲，再用一个小问题确认你是否理解。"
            return intent, response, {"type": "ask_tutor", "question": question, "subject": subject, "hint_level": hint_level}
        if state == "check_in" and intent == "check_in_complete":
            return intent, "好，我们先用自己的话回想今天课堂上讲了什么。", {"type": "transition", "target": "classroom_recall"}
        if state == "classroom_recall" and intent == "add_classroom_recall":
            items = AgentOrchestrator._compound_recall_items(transcript)
            if len(items) > 1:
                subjects = "、".join(item["subject"] for item in items)
                return "add_classroom_recall_batch", f"我听到了{len(items)}科：{subjects}。我会逐项记下，不会只保留最后一项。", {"type": "add_recall_batch", "items": items}
            subject = AgentOrchestrator._subject_from_text(transcript.replace(" ", ""))
            return intent, f"我先按{subject}记下来了，你可以继续补充。", {"type": "add_recall", "subject": subject, "summary": transcript}
        if state == "classroom_recall" and intent in {"confirm_recall_candidate", "reject_recall_candidate"}:
            decision = "confirm" if intent == "confirm_recall_candidate" else "reject"
            return intent, "好，我会按你的确认更新刚才的课堂候选。", {"type": "review_pending_recall", "decision": decision}
        if state == "classroom_recall" and intent == "declare_no_recall":
            return intent, "好，我只记录你现在暂时想不起来，不会替你补写课堂内容。", {"type": "declare_no_recall"}
        if state == "classroom_recall" and intent == "recall_complete":
            return intent, "好，接下来把今天的作业列清楚。", {"type": "transition", "target": "homework_capture"}
        if state == "homework_capture" and intent == "add_homework":
            items = AgentOrchestrator._compound_homework_items(transcript)
            if len(items) > 1:
                subjects = "、".join(item["subject"] for item in items)
                return "add_homework_batch", f"我听到了{len(items)}项作业：{subjects}。我会逐项保存各自的时间和要求。", {"type": "add_homework_batch", "items": items}
            action = AgentOrchestrator._homework_action(transcript)
            return intent, AgentOrchestrator._homework_confirmation(action), action
        if state == "homework_capture" and intent == "revise_last_homework_minutes":
            minutes = AgentOrchestrator._minutes_from_text(transcript.replace(" ", ""), 20)
            return intent, f"好，我把刚才那项作业改为预计{minutes}分钟。", {"type": "update_last_homework_minutes", "expected_minutes": minutes}
        if state == "homework_capture" and intent in {"carryover_continue_today", "carryover_completed_elsewhere", "carryover_cancelled"}:
            decision = {"carryover_continue_today": "continue_today", "carryover_completed_elsewhere": "completed_elsewhere", "carryover_cancelled": "cancelled"}[intent]
            response = {"continue_today": "好，这项今天继续安排。", "completed_elsewhere": "好，我记录这项已经在其他时间完成。", "cancelled": "好，我记录老师已经取消或不再要求这项任务。"}[decision]
            return intent, response, {"type": "review_first_carryover", "decision": decision}
        if state == "homework_capture" and intent == "declare_no_homework":
            return intent, "好，我记录今天没有校内作业。", {"type": "declare_no_homework"}
        if state == "homework_capture" and intent == "generate_plan":
            return intent, "好，我会把明日重点、作业、休息和长期目标一起算进时间表。", {"type": "generate_plan"}
        if state == "plan_review" and intent == "confirm_plan":
            return intent, "好，按这个顺序开始。需要调整时直接告诉我。", {"type": "transition", "target": "study"}
        if state == "plan_review" and intent == "short_tasks_first":
            return intent, "好，我把较短的校内任务排在前面，然后重新读给你听。", {"type": "reorder_plan", "strategy": "short_school_first"}
        if state == "plan_review" and intent == "school_order_first":
            return intent, "好，我恢复按校内作业顺序执行。", {"type": "reorder_plan", "strategy": "school_first"}
        if state == "study" and intent == "request_break":
            return intent, f"好，先休息{break_minutes}分钟。时间到我再陪你继续。", {"type": "begin_break"}
        if state == "study" and intent == "pause_task":
            return intent, "好，我先暂停当前任务并停止计时。想继续时直接告诉我。", {"type": "pause_active_task"}
        if state == "study" and intent == "defer_task":
            return intent, "好，我会把当前任务延期并保存这个决定，然后继续看今天剩下的安排。", {"type": "reschedule_current_task", "reason": transcript}
        if state == "study" and intent == "complete_task":
            return intent, "收到。我会保存这项任务的实际用时，然后看看下一项。", {"type": "complete_active_task"}
        if state == "study" and intent == "start_task":
            return intent, "好，计时开始。需要提示就叫我。", {"type": "start_next_task"}
        if state != "completed" and intent == "capture_question":
            return intent, "好，把题目放到高拍仪下并保持不动，我会自动拍摄和本地识别。", {"type": "request_capture"}
        if state != "completed" and intent == "confirm_ocr_text":
            return intent, "好，我按这份题干继续，只先给你一个小提示。", {"type": "confirm_ocr_text"}
        if state != "completed" and intent == "retry_capture":
            return intent, "好，请把题目放完整并保持不动，我重新拍一次。", {"type": "retry_capture"}
        if state != "completed" and intent == "cancel_capture":
            return intent, "好，已经取消拍题，继续当前任务。", {"type": "cancel_capture"}
        if state != "completed" and intent == "correct_ocr_text":
            corrected = self._corrected_ocr_text(transcript)
            return intent, "好，我先保存你口述的修正版，再按修正后的题干提示。", {"type": "correct_ocr_text", "corrected_text": corrected}
        if state == "study" and intent == "start_diagnostic":
            track = self._diagnostic_track(transcript)
            return intent, "好，我准备一道适合当前进度的能力挑战。", {"type": "start_diagnostic", "track": track}
        if state == "study" and intent == "replay_diagnostic":
            return intent, "好，我再读一遍当前题目。", {"type": "replay_diagnostic"}
        if state == "study" and intent == "submit_diagnostic_answer":
            return intent, "收到，我会保存这次作答并告诉你反馈。", {"type": "submit_diagnostic_answer", "answer": self._spoken_answer(transcript)}
        if state == "study" and intent == "tutor_understood":
            return intent, "好，我记下这次提示已经帮你继续做了。", {"type": "record_tutor_feedback", "outcome": "understood"}
        if state == "study" and intent == "tutor_still_stuck":
            return intent, "我记下你还卡着，不会把这次提示算作已经掌握。", {"type": "record_tutor_feedback", "outcome": "still_stuck", "feedback_text": transcript}
        if state == "study" and intent == "more_tutor_hint":
            return intent, "好，我在刚才基础上再给一小步。", {"type": "more_tutor_hint"}
        if state == "study" and intent == "confirm_mistake":
            return "supportive_chat", "你不用判断它是不是错题。我会核对老师批改、这本习题的标准答案，或我出题时保存的评分规则；证据不足就先保持待评分。", {"type": "none"}
        if state == "study" and intent == "exclude_mistake":
            if not self._mistake_fact_exclusion(transcript):
                return "supportive_chat", "我记下你对判定有异议，但不会只凭这句话改写对错。我会重新核对老师批改、对应版本的标准答案或出题时冻结的评分规则；如果是题干、答案或题目归属读错了，请直接告诉我哪一处。", {"type": "none"}
            return intent, "好，我会把这条识别错误的记录从错题强化中排除。", {"type": "review_current_mistake", "decision": "exclude"}
        if state == "study" and intent == "diagnose_mistake":
            explanation = self._strip_spoken_prefix(transcript, ("这次错在", "我错在", "错因是"))
            return intent, "好，错因先作为假设保存，接下来会用新题验证。", {"type": "diagnose_current_mistake", "error_type": self._mistake_error_type(explanation), "explanation": explanation}
        if state == "study" and intent in {"mistake_remediation_complete", "mistake_remediation_stuck"}:
            outcome = "completed" if intent == "mistake_remediation_complete" else "still_stuck"
            return intent, "好，我会按这次真实理解程度安排下一步。", {"type": "complete_mistake_remediation", "outcome": outcome, "notes": transcript}
        if state == "study" and intent == "submit_mistake_answer":
            return intent, "收到，我会保存这次独立作答并更新强化进度。", {"type": "submit_mistake_answer", "answer": self._strip_spoken_prefix(transcript, ("错题答案是", "这题答案是"))}
        if state == "break" and intent == "resume_study":
            return intent, "欢迎回来。我们从刚才的任务继续。", {"type": "resume_study"}
        if state == "reflection" and intent == "reflection_learned":
            return intent, "记下来了。还有哪里不太确定？", {"type": "update_reflection", "field": "learned", "value": self._strip_spoken_prefix(transcript, ("今天学会了", "我学会了"))}
        if state == "reflection" and intent == "reflection_difficult":
            return intent, "好，我不会把它算成已经掌握。明天第一件想先做什么？", {"type": "update_reflection", "field": "difficult", "value": self._strip_spoken_prefix(transcript, ("还不确定", "我还不确定", "没弄懂"))}
        if state == "reflection" and intent == "reflection_next_action":
            return intent, "好，明天第一步已经记下。确认后就可以结束今天学习。", {"type": "update_reflection", "field": "next_action", "value": self._strip_spoken_prefix(transcript, ("明天先", "明天第一件事是"))}
        if state == "reflection" and intent == "reflection_complete":
            return intent, "好，我保存今天的复盘并关闭摄像头和麦克风。", {"type": "complete_reflection", "skipped": False}
        if state == "reflection" and intent == "reflection_skip":
            return intent, "好，这次记为跳过复盘并结束，摄像头和麦克风会关闭。", {"type": "complete_reflection", "skipped": True}
        raise InvalidVoiceIntentOutputError("state_intent_mismatch")

    def _continue_document_stop_question(self, work):
        from .document_monitoring import DocumentMonitoringService
        queue = self.agent_runtime.continuations
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not queue.current(c, work):
                return {'status': 'superseded'}
            followup = queue.document_followup(c, work['root_turn_id'])
            question = followup['request_quote']
            display_question = question[:80] + ('…' if len(question) > 80 else '')
            revision = followup.get('revision')
            text = None
            if revision:
                artifact = c.execute('''SELECT extracted_text FROM capture_artifact WHERE artifact_id=?
                    AND child_id=? AND session_id=?''', (revision['artifact_id'], work['child_id'], work['session_id'])).fetchone()
                text = artifact['extracted_text'] if artifact else None
                if text is None or hashlib.sha256(DocumentMonitoringService._normalized_text(text).encode('utf-8')).hexdigest() != revision['content_sha256']:
                    c.execute("UPDATE agent_continuation SET status='blocked',reason='DOCUMENT_SOURCE_CHANGED' WHERE parent_turn_id=?", (work['parent_turn_id'],))
                    c.commit()
                    return {'status': 'blocked', 'reason': 'DOCUMENT_SOURCE_CHANGED'}
            kind = self._document_observation_kind(question)
            reply_required = not text or kind != 'recent_writing'
            if not text:
                response = '高拍仪已经停止。关于你问的“' + display_question + '”，停止前没有可核对的保存页面，我不能猜测。你可以口述要核对的内容。'
            elif kind in {'recent_writing', 'possible_problem'}:
                response = '高拍仪已经停止。针对你问的“' + display_question + '”，停止前保存的页面识别文字节选是：“' + text[:200] + '”。这是之前的 OCR 记录，可能有识别错误，不能证明你现在的行为或答案是否正确。'
                if kind == 'possible_problem':
                    response += '你希望先核对哪一处？我还不能据此断定错误。'
            else:
                response = '高拍仪已经停止。你后面问的是“' + display_question + '”。我不会继续采集，也不会把本地页面发到云端。请再说明你希望我核对什么；这部分还没有完成。'
            cap = self.capability_registry.require('conversation.respond', c.execute(
                'SELECT state FROM daily_session WHERE session_id=?', (work['session_id'],)).fetchone()[0])
            decision = AgentPlanDecision(ObjectiveDraft('none', None, None, (), 'execute'),
                PlannedStep(cap, {'response': response}, 'Answer the frozen remaining request after the stop receipt', cap.expected_evidence),
                response, reply_required, 1)
            action = {'type': 'none', 'source': 'document_stop_followup', 'local_only': True,
                'cloud_transmission_allowed': False, 'document_stop_followup_root': work['root_turn_id'],
                'root_turn_id': work['root_turn_id'], 'parent_turn_id': work['parent_turn_id'],
                'mastery_claim_allowed': False, 'after_execution': 'stop'}
            tid, key = str(uuid4()), 'continuation:' + work['parent_turn_id']
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id)
                VALUES(?,?,?,'','runtime_continuation',?,?,?,?,?)''',
                (tid, work['child_id'], work['session_id'], response, json.dumps(action, ensure_ascii=False), PLANNER_VERSION, utc_now(), key))
            action['agent_runtime'] = self.agent_runtime.persist_plan(c, child_id=work['child_id'], session_id=work['session_id'],
                agent_turn_id=tid, client_action_id=key, decision=decision,
                context_manifest={'document_stop_followup_root': work['root_turn_id']})
            c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', (json.dumps(action, ensure_ascii=False), tid))
            queue.finish(c, work, tid)
            c.commit()
            return {'status': 'ready', 'turn': self._get(c, tid)}

    async def continue_execution(self, session_id: str, parent_turn_id: str, cloud_text_allowed: bool) -> dict:
        queue = self.agent_runtime.continuations
        with self.database.connect() as c:
            local_followup = queue.document_followup(c, parent_turn_id)
            from .document_goal_handoff import owns
            local_document_goal = owns(c, parent_turn_id)
        if (not cloud_text_allowed and not local_followup and not local_document_goal) or not self.settings.agent_runtime_enabled:
            return {"status": "blocked", "reason": "CONTINUATION_MODEL_DISABLED"}
        work = queue.claim(session_id, parent_turn_id)
        if work["status"] == "ready":
            with self.database.connect() as connection:
                return {"status": "ready", "turn": self._get(connection, work["next_turn_id"])}
        if work["status"] != "planning":
            if local_document_goal:
                return {key: value for key, value in work.items() if key in {
                    "status", "reason", "document_goal", "local_only",
                    "cloud_transmission_allowed", "mastery_update_allowed"}}
            return {key: value for key, value in work.items() if key in {"status", "reason"}}
        if local_followup:
            return self._continue_document_stop_question(work)
        with self.database.connect() as connection:
            names = [row[0] for row in connection.execute("SELECT nickname FROM child_profile WHERE status='active'")]
            consent_version = self.voice_control_intent.text_consent_version(connection)
            proof_sql = """SELECT agent_turn_id,child_id,session_id,transcript,action_json,tool_status,tool_error_code
                FROM agent_turn WHERE agent_turn_id IN (?,?) ORDER BY agent_turn_id"""
            proof_args = (work['root_turn_id'], work['parent_turn_id'])
            proofs = [tuple(row) for row in connection.execute(proof_sql, proof_args)]

        def authorize_continuation(connection=None):
            with (nullcontext(connection) if connection is not None else self.database.connect()) as c:
                if connection is None:
                    c.execute('BEGIN')
                if (self.voice_control_intent.text_consent_version(c) != consent_version
                        or not queue.current(c, work)
                        or [tuple(row) for row in c.execute(proof_sql, proof_args)] != proofs):
                    raise InvalidAgentPlanError('VOICE_CONTINUATION_AUTHORITY_CHANGED')
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='deletion_job'").fetchone() and c.execute(
                        """SELECT 1 FROM deletion_job WHERE child_id=? AND target_type='child_scope'
                        AND scope IN ('learning','knowledge','media','tutor','all_history') AND status!='completed'""",
                        (work['child_id'],)).fetchone():
                    raise InvalidAgentPlanError('VOICE_CONTINUATION_DELETION_PENDING')
                for row in c.execute(proof_sql, proof_args):
                    if (row['child_id'] != work['child_id'] or row['session_id'] != session_id
                            or row['tool_status'] != 'completed' or row['tool_error_code'] == 'SOURCE_DELETED'):
                        raise InvalidAgentPlanError('VOICE_CONTINUATION_PARENT_CHANGED')
                    action = json.loads(row['action_json'])
                    if action.get('voice_input_id'):
                        voice = self.agent_runtime.resolve_captured_voice(c, session_id=session_id, child_id=work['child_id'],
                            voice_input_id=action['voice_input_id'], transcript=row['transcript'], page_source_store=self.page_source_store)
                        if self.agent_runtime.voice_capture_snapshot(voice) != action.get('voice_input_binding'):
                            raise InvalidAgentPlanError('VOICE_CONTINUATION_VOICE_CHANGED')
        resolved = await self._resolve_agent_plan(
            session_id=session_id, child_id=work["child_id"], transcript=work["original_utterance"],
            known_child_names=names, recent_dialogue=(),
            pre_dispatch=authorize_continuation,
            continuation_event={"completed_capability": work["completed_capability"],
                                "execution_status": "completed", "depth": work["depth"],
                                "authorized_actions": work["authorized_actions"]},
        )
        if not resolved:
            with self.database.connect() as connection:
                if not queue.current(connection, work):
                    return {"status": "superseded"}
            queue.block(work, "CONTINUATION_PLAN_UNAVAILABLE")
            return {"status": "blocked", "reason": "CONTINUATION_PLAN_UNAVAILABLE"}
        decision, manifest = resolved
        capability = decision.step.capability.name
        if (decision.objective.mode not in {"continue", "none"}
                or capability not in RESULT_CONTINUATION_CAPABILITIES):
            queue.block(work, "CONTINUATION_REQUIRES_CHILD_INPUT")
            return {"status": "blocked", "reason": "CONTINUATION_REQUIRES_CHILD_INPUT"}
        if capability not in {"conversation.respond", "learning.request_response"} and not any(
            item["capability"] == capability and item["arguments"] == decision.step.arguments
            for item in work["authorized_actions"]
        ):
            queue.block(work, "CONTINUATION_OUTSIDE_REQUEST")
            return {"status": "blocked", "reason": "CONTINUATION_OUTSIDE_REQUEST"}
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not queue.current(connection, work):
                return {"status": "superseded"}
            authorize_continuation(connection)
            objective = connection.execute("SELECT * FROM agent_objective WHERE objective_id=?", (work["objective_id"],)).fetchone()
            if objective["objective_type"] not in decision.step.capability.objective_types:
                connection.execute("UPDATE agent_continuation SET status='blocked',reason='CONTINUATION_OBJECTIVE_MISMATCH' WHERE parent_turn_id=?", (parent_turn_id,))
                connection.commit()
                return {"status": "blocked", "reason": "CONTINUATION_OBJECTIVE_MISMATCH"}
            # Preserve the child's target and criteria; result planning cannot redefine success.
            decision = AgentPlanDecision(
                ObjectiveDraft("continue", objective["objective_type"], None, (), decision.objective.phase),
                decision.step, decision.spoken_transition, decision.requires_child_reply,
                decision.confidence, decision.after_execution, tuple(work["authorized_actions"]),
            )
            prior = connection.execute("""SELECT s.capability_name,s.arguments_json FROM agent_step s
                JOIN agent_plan p ON p.plan_id=s.plan_id WHERE s.step_index=0 AND (
                  p.agent_turn_id=? OR p.agent_turn_id IN (
                    SELECT next_turn_id FROM agent_continuation WHERE root_turn_id=?))""",
                (work["root_turn_id"], work["root_turn_id"])).fetchall()
            identities = written_item_identities(capability, decision.step.arguments)
            if any((row["capability_name"] == capability and json.loads(row["arguments_json"]) == decision.step.arguments)
                   or identities.intersection(written_item_identities(row["capability_name"], json.loads(row["arguments_json"]))) for row in prior):
                connection.execute("UPDATE agent_continuation SET status='blocked',reason='CONTINUATION_NO_PROGRESS' WHERE parent_turn_id=?", (parent_turn_id,))
                connection.commit()
                return {"status": "blocked", "reason": "CONTINUATION_NO_PROGRESS"}
            action = action_from_plan(decision, work["original_utterance"], dict(objective))
            action.update(source="agent_runtime_result", origin="tool_result", parent_turn_id=parent_turn_id,
                          root_turn_id=work["root_turn_id"], after_execution=decision.after_execution)
            now, turn_id = utc_now(), str(uuid4())
            client_action_id = f"continuation:{parent_turn_id}"
            connection.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,
                intent,response_text,action_json,orchestrator_version,created_at,client_action_id)
                VALUES(?,?,?,'','runtime_continuation',?,?,?,?,?)""",
                (turn_id, work["child_id"], session_id, decision.spoken_transition,
                 json.dumps(action, ensure_ascii=False), PLANNER_VERSION, now, client_action_id))
            action["agent_runtime"] = self.agent_runtime.persist_plan(
                connection, child_id=work["child_id"], session_id=session_id,
                agent_turn_id=turn_id, client_action_id=client_action_id, decision=decision, context_manifest=manifest)
            connection.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?",
                               (json.dumps(action, ensure_ascii=False), turn_id))
            queue.finish(connection, work, turn_id)
            connection.commit()
            return {"status": "ready", "turn": self._get(connection, turn_id)}

    def complete_turn(
        self,
        turn_id: str,
        final_response: str,
        tool_status: str,
        tool_error_code: str | None,
        result: dict[str, Any] | None = None,
    ) -> dict:
        if tool_status not in {"completed", "failed"}:
            raise InvalidAgentTurnOutcomeError(tool_status)
        final_response = final_response.strip()
        if not final_response:
            raise InvalidAgentTurnOutcomeError("empty_response")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT agent_turn_id, tool_status FROM agent_turn WHERE agent_turn_id = ?",
                (turn_id,),
            ).fetchone()
            if not existing:
                raise UnknownAgentTurnError(turn_id)
            from .realtime_native_client import check_completion
            check_completion(connection, turn_id)
            self.agent_runtime.assert_turn_source_not_deleted(connection, turn_id)
            self.agent_runtime.validate_writing_reference_execution(connection, turn_id)
            from .school_oral_reference import validate as validate_oral_reference
            validate_oral_reference(connection,turn_id,final_response=final_response)
            server = self.agent_runtime.input_revision_execution_receipt(connection, turn_id)
            if server is not None:
                if server["result"] is not None:
                    result = server["result"]
                    final_response, tool_status, tool_error_code = result["response_text"], "completed", None
                elif tool_status == "completed" or server["status"] == "cancelled" or server["plan_status"] == "cancelled":
                    raise InvalidAgentTurnOutcomeError("INPUT_REVISION_SERVER_RECEIPT_REQUIRED")
            replayed = existing["tool_status"] == "completed"
            client_control = connection.execute('''SELECT s.execution_location,p.context_manifest_json
                FROM agent_step s JOIN agent_plan p USING(plan_id)
                WHERE p.agent_turn_id=? AND s.step_index=0''', (turn_id,)).fetchone()
            control_manifest = json.loads(client_control['context_manifest_json']) if client_control else {}
            local_safety = (control_manifest.get('local_recovery_control')
                            or control_manifest.get('pending_local_control') == 'child_safety')
            if local_safety:
                authority = self.agent_runtime.inspect_voice_control_execution(
                    connection,turn_id,allow_planned=tool_status != 'completed')
                canonical = (authority or {}).get('canonical_response')
                if not canonical or tool_status == 'completed' and final_response != canonical:
                    prefix = 'LOCAL_VOICE_RECOVERY_' if control_manifest.get('local_recovery_control') else 'PENDING_EVALUATION_CONTROL_'
                    raise InvalidAgentPlanError(prefix + 'RESPONSE_CHANGED')
                final_response = canonical
            if not replayed and tool_status == 'completed':
                if (client_control and client_control['execution_location'] == 'client_device_bridge'
                        and not local_safety and control_manifest.get('voice_control_binding')):
                    self.agent_runtime.inspect_voice_control_execution(connection, turn_id)
            if replayed:
                completed = self._get_replayed(connection, turn_id)
            else:
                if tool_status == 'completed' and self.agent_runtime.execution_result_blocked(result):
                    connection.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.execution_blocked',json('true')) WHERE agent_turn_id=?", (turn_id,))
                connection.execute(
                    """
                    UPDATE agent_turn SET response_text = ?, tool_status = ?, tool_error_code = ?,
                      completed_at = ? WHERE agent_turn_id = ?
                    """,
                    (final_response[:4000], tool_status, (tool_error_code or "")[:80] or None,
                     utc_now(), turn_id),
                )
                self.agent_runtime.complete_turn_execution(
                    connection, turn_id, tool_status == "completed", tool_error_code, result
                )
                connection.commit()
                completed = self._get(connection, turn_id)
            saved_result = connection.execute("SELECT s.result_json FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0",
                                              (turn_id,)).fetchone()
            business_blocked = self.agent_runtime.execution_result_blocked(
                json.loads(saved_result[0]) if saved_result and saved_result[0] else result)
            business_blocked = business_blocked or bool(connection.execute(
                "SELECT json_extract(action_json,'$.execution_blocked') FROM agent_turn WHERE agent_turn_id=?", (turn_id,)).fetchone()[0])
        if local_safety:
            return completed
        if tool_status == "completed" and not business_blocked:
            local_evaluation = self.agent_runtime.finalize_diagnostic_turn(turn_id)
            if local_evaluation:
                completed["local_evaluation"] = {
                    "status": local_evaluation["status"], "reason": local_evaluation.get("reason"),
                    "feedback_turn_id": local_evaluation.get("feedback_turn_id"),
                    "local_feedback_turn_id": local_evaluation.get("local_feedback_turn_id"),
                    "assessment_id": (local_evaluation.get("evaluation", {}).get("rubric_assessment") or {}).get("assessment_id"),
                    "local_only": True, "cloud_transmission_allowed": False,
                }
            if replayed:
                return completed
            self._release_previous_activity(completed)
            try:
                if completed["intent"] != "runtime_continuation":
                    self.companion_memory.observe_turn(turn_id)
            except Exception:
                # The completed turn is the durable retry source. Memory failure
                # must not replay an already successful device/learning action.
                pass
        return completed

    def _release_previous_activity(self, turn: dict) -> None:
        action_type = turn["action"].get("type")
        if turn["intent"] != "conversation_repair" and action_type != "stop_speech":
            return
        session_id = turn["session_id"]
        if self.pet_vocabulary and action_type != "start_pet_vocabulary_review":
            self.pet_vocabulary.cancel_active(session_id)
        if self.exam_simulation:
            exam = self.exam_simulation.active_for_session(session_id)
            if exam and exam["status"] == "active":
                self.exam_simulation.pause(exam["run_id"])
        if action_type != "start_companion_activity":
            self.agent_runtime.end_companion_activity(session_id, "child_changed_direction")
        from .mistake_dialogue import MistakeDialogueService
        dialogue = MistakeDialogueService(self.database)
        focus = dialogue.state(session_id)
        if focus and focus["phase"] not in {"paused", "closed", "scheduled"}:
            dialogue.respond(session_id, "先停一下", f"activity-release:{turn['agent_turn_id']}")

    @staticmethod
    def _sanitize_dialogue_history(
        recent_dialogue: tuple[dict[str, str], ...], known_child_names: list[str]
    ) -> tuple[tuple[dict[str, str], ...], tuple[int, tuple[str, ...]]]:
        sanitized_items: list[dict[str, str]] = []
        redaction_count = 0
        redaction_types: set[str] = set()
        for item in recent_dialogue[-24:]:
            content = item.get("content", "")[:800]
            if not content or sensitive_categories(content):
                continue
            sanitized = sanitize_cloud_text(content, known_child_names)
            sanitized_items.append({"role": item.get("role", "user"), "content": sanitized.text})
            redaction_count += sanitized.redaction_count
            redaction_types.update(sanitized.redaction_types)
        return tuple(sanitized_items), (redaction_count, tuple(sorted(redaction_types)))

    @staticmethod
    def _profile_switch_result(
        current_child_id: str, transcript: str, profiles: list[dict[str, str]]
    ) -> tuple[str, str, dict] | None:
        compact = transcript.replace(" ", "")
        grade_pattern = (
            r"(?:我是|我上|我读|切换到|换成)"
            r"(初中三年级|初中二年级|初中一年级|九年级|八年级|七年级|初三|初二|初一|小学六年级|小学五年级|小学四年级|六年级|五年级|四年级)"
        )
        match = re.search(grade_pattern, compact)
        if not match and any(phrase in compact for phrase in ("年级弄错了", "年级错了", "初始化错了", "认错人了")):
            match = re.search(
                r"(初中三年级|初中二年级|初中一年级|九年级|八年级|七年级|初三|初二|初一|小学六年级|小学五年级|小学四年级|六年级|五年级|四年级)",
                compact,
            )
        if not match:
            return None
        requested_grade = AgentOrchestrator._normalize_grade(match.group(1))
        target = next(
            (profile for profile in profiles if AgentOrchestrator._normalize_grade(profile["grade"]) == requested_grade),
            None,
        )
        if not target or target["child_id"] == current_child_id:
            return None
        return (
            "switch_child_profile",
            f"明白，是{target['grade']}的{target['nickname']}。我会保存刚才的记录并切换到你的独立档案。",
            {
                "type": "switch_child_profile",
                "target_child_id": target["child_id"],
                "target_child_name": target["nickname"],
            },
        )

    @staticmethod
    def _normalize_grade(value: str) -> str:
        compact = value.replace("小学", "").replace("初中", "")
        return {
            "一年级": "初一", "七年级": "初一",
            "二年级": "初二", "八年级": "初二",
            "三年级": "初三", "九年级": "初三",
        }.get(compact, compact)

    @staticmethod
    def _reconstruct_tutor_question(
        transcript: str, recent_dialogue: tuple[dict[str, str], ...]
    ) -> str:
        compact = transcript.replace(" ", "")
        is_correction = any(phrase in compact for phrase in (
            "我刚才问", "我问的是", "你理解错", "你听错", "我让你讲", "重新讲",
        ))
        if not is_correction:
            return transcript
        previous_questions = [
            item.get("content", "").strip()
            for item in recent_dialogue
            if item.get("role") == "user" and item.get("content", "").strip()
        ]
        previous = previous_questions[-1] if previous_questions else ""
        if previous and previous != transcript:
            return f"孩子上一轮的问题：{previous}\n孩子现在纠正：{transcript}"[:1200]
        return transcript

    @staticmethod
    def _is_review_answer(text: str) -> bool:
        compact = re.sub(r"[\s，。！？!?]", "", text)
        return bool(re.fullmatch(r"[A-Da-d0-9零〇一二三四五六七八九十百千万两点负正个./+×÷=()（）^%√-]+", compact)
                    or re.match(r"^(?:答案是|我的答案|我选|我算出|我算的是|应该是|结果是|因为|所以|我不知道|不知道|我不会|不会)$|^(?:答案是|我的答案|我选|我算出|我算的是|应该是|结果是|因为|所以).+", compact))

    @staticmethod
    def _active_task_completion(text: str) -> tuple[str, str, dict[str, Any]] | None:
        compact = text.replace(" ", "")
        if any(word in compact for word in ("还需订正", "还要订正", "需要订正", "做完但要订正")):
            return (
                "complete_task",
                "收到。我会把这项记录为已经做完但仍需订正，不会算成完全掌握。",
                {"type": "complete_active_task", "completion_evidence_type": "needs_correction"},
            )
        if any(word in compact for word in ("完成并检查了", "做完并检查了", "检查过了")):
            return (
                "complete_task",
                "收到。我会记录这项已经完成并检查，然后看看下一项。",
                {"type": "complete_active_task", "completion_evidence_type": "child_checked"},
            )
        if any(word in compact for word in ("完成了", "做完了", "这项完成")):
            return (
                "complete_task",
                "收到。我会保存这项任务的实际用时，然后看看下一项。",
                {"type": "complete_active_task", "completion_evidence_type": "voice_confirmed"},
            )
        return None

    @staticmethod
    def _classify(
        state: str, text: str, break_minutes: int, previous_response: str | None = None,
        recent_dialogue: tuple[dict[str, str], ...] = (),
    ) -> tuple[str, str, dict]:
        compact = text.replace(" ", "")
        command_compact = re.sub(r"[^A-Za-z0-9\u3400-\u9fff]", "", text).upper()
        if is_direct_voice_control(text, PRIVACY_STOP_PHRASES):
            return "privacy_pause", "好，我现在停止全部监听并释放摄像头和麦克风。之后需要由家长在设备上恢复。", {"type": "privacy_pause", "scope": "camera_microphone"}
        if AgentOrchestrator._is_stop_current_explanation(compact) or compact in {
            "停", "停止", "别说了", "不要说了",
        }:
            return "stop_speech", "好，刚才的讲解到这里，这道题也不再继续追问。", {"type": "stop_speech"}
        if any(phrase in compact for phrase in (
            "总结今天的学习", "总结今天的作业", "今天的学习总结", "今天有哪些薄弱点",
            "我的薄弱点是什么", "今天哪里还没掌握", "我今天还有什么没掌握",
            "今天学了哪些内容", "今天学了什么", "今天作业完成得怎么样",
            "明天怎么复习", "明天复习什么", "总结一下今天",
        )) and not classify_child_safety(text):
            return "daily_learning_summary", "我来核对今天的学习记录。", {"type": "none"}
        if CompanionMemory.is_command(text) and not classify_child_safety(text):
            return "memory_command", "我正在核对本机保存的记忆。", {"type": "none"}
        if AgentOrchestrator._is_agent_directed_repair(compact):
            return (
                "conversation_repair",
                "可以换话题。刚才是我没有及时听懂并停下来，旧问题已经结束。你直接说现在要做什么。",
                {"type": "none", "source": "local_conversation_repair"},
            )
        if any(phrase in compact for phrase in (
            "换个话题", "换一个话题", "换话题", "不聊这个了", "不说这个了",
            "不想接着说", "不要沿着这个说",
        )):
            return (
                "conversation_repair",
                "可以，刚才的话题已经结束。你直接说现在想做什么，我跟着你的新目标走。",
                {"type": "none", "source": "local_direction_change"},
            )
        if is_direct_voice_control(text, CAMERA_STOP_PHRASES):
            return "pause_observation", "好，我只停止本地摄像头观察，语音陪伴仍然保留。需要时说“恢复观察”。", {"type": "pause_observation"}
        if any(phrase in compact for phrase in ("恢复观察", "重新打开摄像头", "继续观察")):
            return "resume_observation", "好，我重新打开已绑定的观察摄像头，并先在本机确认取景。", {"type": "resume_observation"}
        if any(phrase in compact for phrase in ("重新尝试观察", "重新检测观察", "重试取景确认")):
            return "resume_observation", "好，我重新打开已绑定的观察摄像头，并先在本机确认取景。", {"type": "resume_observation"}
        if any(phrase in compact for phrase in (
            "摄像头能看到我吗", "摄像头看得到我吗", "现在能看到我吗", "你能看到我吗",
        )):
            return (
                "camera_visibility_check",
                "我不能只凭对话假定看到了你。现在检查本机绑定的观察摄像头和取景状态，确认后才使用观察结果。",
                {"type": "resume_observation"},
            )
        if any(phrase in compact for phrase in ("重置提示偏好", "恢复默认提醒", "提醒偏好重置")):
            return "reset_behavior_personalization", "好，我只重置本机的提醒偏好，之后先使用默认温和、先询问的方式。", {"type": "reset_behavior_personalization"}
        from .voice_control_intent import VoiceControlIntent
        if VoiceControlIntent.direct_document_stop(text):
            return "pause_document_monitor", "我正在停止高拍仪采集，收到设备确认后再继续。", {"type": "control_document_monitor", "command": "pause"}
        if state == "study" and any(phrase in compact for phrase in ("恢复高拍仪", "重新连接高拍仪", "继续书写监控")):
            return "resume_document_monitor", "好，我重新连接已绑定的高拍仪，继续在本机观察页面变化。", {"type": "control_document_monitor", "command": "resume"}
        if state == "study" and any(phrase in compact for phrase in ("检查当前页", "检查这一页", "识别当前页")):
            return "inspect_document_page", "好，请移开手并保持页面稳定，我会在本机检查当前页。", {"type": "control_document_monitor", "command": "inspect"}
        if state == "study" and any(phrase in compact for phrase in ("核对这一页", "核对刚才这页", "看看刚才这页")):
            return "review_document_page", "好，我把最近一次本地识别的页面交给你核对，然后继续讲解。", {"type": "control_document_monitor", "command": "review"}
        if state == "study" and any(phrase in compact for phrase in ("重试错题步骤", "重新加载错题", "再试错题")):
            return "retry_mistake_activity", "好，我重新加载当前错题步骤，不会重复提交刚才的作答。", {"type": "control_mistake_activity", "command": "reload"}
        if state == "study" and any(phrase in compact for phrase in ("暂停模拟卷", "模拟卷暂停", "先暂停考试")):
            return "pause_exam_simulation", "好，我暂停计时并保存当前位置。", {"type": "pause_exam_simulation"}
        if state == "study" and any(phrase in compact for phrase in ("继续模拟卷", "恢复模拟卷", "继续考试")):
            return "resume_exam_simulation", "好，从刚才停下的题继续计时。", {"type": "resume_exam_simulation"}
        if state == "study" and any(phrase in compact for phrase in ("提前交卷", "交卷", "结束模拟卷", "结束考试")):
            return "cancel_exam_simulation", "好，我按现有作答收卷，未答题不会被当成已掌握。", {"type": "cancel_exam_simulation"}
        if state == "study" and "模拟卷" in compact and any(word in compact for word in ("开始", "做一套", "来一套")):
            subject = AgentOrchestrator._subject_from_text(compact)
            track = "pet" if "PET" in compact.upper() else "competition_math" if any(word in compact for word in ("奥数", "竞赛")) else "auto"
            if track == "pet":
                subject = "英语"
            elif track == "competition_math":
                subject = "数学"
            elif subject == "其他":
                subject = ""
            return "start_exam_simulation", "好，我先核对能力域覆盖和时间，然后逐题主持，中途不透露答案。", {"type": "start_exam_simulation", "track": track, "subject": subject, "mode": "full"}
        if state == "study" and any(phrase in compact for phrase in ("重试同步进度", "重新同步错题进度", "同步错题进度")):
            return "retry_mistake_progress", "好，我只重试同步已经保存的错题进度，不会重复作答。", {"type": "control_mistake_activity", "command": "sync"}
        if state == "study" and (any(phrase in compact for phrase in ("把这题加入错题本", "整理这道错题", "开始整理错题"))
                or re.fullmatch(r"(?:请)?(?:帮我|给我)?录入(?:一下)?(?:这道|一道)?错题", command_compact)):
            return "open_mistake_capture", "好，我从刚才的本地识别结果开始逐题整理。", {"type": "control_mistake_capture", "command": "open"}
        if state == "study" and compact.startswith(("错题原答案是", "我原来写的是", "当时答案是")):
            answer = AgentOrchestrator._strip_spoken_prefix(text, ("错题原答案是", "我原来写的是", "当时答案是"))
            return "set_mistake_answer", "好，我记下当时的真实答案，保存前还会核对关联知识点。", {"type": "control_mistake_capture", "command": "set_answer", "answer": answer}
        if state == "study" and compact.startswith(("把错题题干改成", "错题题干改成")):
            corrected = AgentOrchestrator._strip_spoken_prefix(text, ("把错题题干改成", "错题题干改成"))
            return "correct_mistake_prompt", "好，我按修正后的题干重新匹配知识点。", {"type": "control_mistake_capture", "command": "correct_prompt", "corrected_text": corrected}
        if state == "study" and any(phrase in compact for phrase in ("保存这道作答", "保存这条作答记录", "保存这道错题", "确认加入错题本", "保存为错题")):
            return "save_mistake_capture", "好，我只保存题干和你当时的真实作答。对错会继续核对老师批改、对应版本的标准答案或出题时冻结的评分规则；证据不足就保持待评分。", {"type": "control_mistake_capture", "command": "save"}
        if state == "study" and AgentOrchestrator._mistake_fact_exclusion(compact):
            return "exclude_mistake", "好，我会按你指出的识别或题目归属错误排除这条记录，不把你的话当作对错评分。", {"type": "review_current_mistake", "decision": "exclude"}
        if state == "study" and any(phrase in compact for phrase in ("结束错题整理", "错题整理完成", "关闭错题整理")):
            return "close_mistake_capture", "好，本页错题整理先到这里，未处理的候选会保留。", {"type": "control_mistake_capture", "command": "close"}
        safety_result = classify_child_safety(text)
        if safety_result:
            return safety_result
        # Current-stage self-reports describe content; Tutor words inside them
        # do not request another lesson. An explicit question still uses Tutor.
        if state == "reflection" and not text.rstrip().endswith(("?", "？")):
            if state == "reflection" and compact.startswith(("今天学会了", "我学会了")):
                return "reflection_learned", "记下来了。还有哪里不太确定？", {"type": "update_reflection", "field": "learned", "value": AgentOrchestrator._strip_spoken_prefix(text, ("今天学会了", "我学会了"))}
            if state == "reflection" and compact.startswith(("还不确定", "我还不确定", "没弄懂")):
                return "reflection_difficult", "好，我不会把它算成已经掌握。明天第一件想先做什么？", {"type": "update_reflection", "field": "difficult", "value": AgentOrchestrator._strip_spoken_prefix(text, ("还不确定", "我还不确定", "没弄懂"))}
            if state == "reflection" and compact.startswith(("明天先", "明天第一件事是")):
                return "reflection_next_action", "好，明天第一步已经记下。确认后就可以结束今天学习。", {"type": "update_reflection", "field": "next_action", "value": AgentOrchestrator._strip_spoken_prefix(text, ("明天先", "明天第一件事是"))}
        if AgentOrchestrator._document_observation_kind(text):
            return "document_observation_question", "我先核对本机会话里的最新页面记录。", {
                "type": "none", "source": "local_document_learning",
                "local_only": True, "cloud_transmission_allowed": False,
            }
        if any(phrase in compact for phrase in ("再说一遍", "重复一遍", "没听清", "你刚才说什么")):
            response = previous_response or "刚才还没有需要重复的内容。"
            return "repeat_response", response, {"type": "repeat_response"}
        if any(phrase in compact for phrase in (
            "现在能听到吗", "能听到我说话吗", "你能听到吗", "听得到吗",
            "能听见吗", "你能听见吗", "麦克风好了吗", "麦克风能用吗",
        )) or (
            any(heard in compact for heard in ("听到", "听见", "听得到", "听得见"))
            and any(source in compact for source in ("我的声音", "我说话", "我讲话", "声音"))
            and any(marker in compact for marker in ("吗", "没", "没有", "能不能"))
        ):
            return "audio_check", "现在能听到。你可以继续说，我不会把这句话当成题目或答案。", {"type": "none"}
        if compact in {"喂", "在吗", "你在吗", "听着吗"}:
            return "attention_check", "我在，也在听。这句话不会被当成题目答案。", {"type": "none"}
        if any(phrase in compact for phrase in ("视频停下", "停止视频", "不看了", "结束播放", "关闭视频")):
            return "stop_controlled_content", "我先核对并关闭这段内容。准备好继续学习时告诉我。", {
                "type": "stop_controlled_content"
            }
        if state in {"study", "break"} and any(phrase in compact for phrase in (
            "播放视频", "看个视频", "看一个视频", "看这节课", "播放这节课", "看讲解视频"
        )):
            query = AgentOrchestrator._strip_spoken_prefix(
                text, ("播放", "我想看", "想看", "看一个", "看个")
            )
            return "start_controlled_content", "我只会从家长许可的本地内容中选择，并按约定时间自动结束。", {
                "type": "start_controlled_content", "query": query or text
            }
        if any(phrase in compact for phrase in ("停止游戏", "关闭游戏", "游戏停下", "不玩电脑了")):
            return "stop_controlled_game", "我先核对并关闭游戏。准备好继续学习时告诉我。", {
                "type": "stop_controlled_game"
            }
        if state in {"study", "break"} and any(phrase in compact for phrase in (
            "打开游戏", "启动游戏", "玩电脑游戏", "玩十分钟游戏", "玩一会游戏"
        )):
            query = AgentOrchestrator._strip_spoken_prefix(
                text, ("打开", "启动", "我想玩", "想玩", "玩十分钟", "玩一会")
            )
            return "start_controlled_game", "我只会启动家长许可的游戏，并在约定时间到后自动关闭。", {
                "type": "start_controlled_game", "query": query or text
            }
        if any(phrase in compact for phrase in ("不玩了", "游戏结束", "结束游戏", "故事到这里", "故事结束", "活动结束")):
            return "end_companion_activity", "好，这轮活动到这里，我们回到刚才的安排。", {
                "type": "end_companion_activity"
            }
        companion_activity_type = None
        if any(phrase in compact for phrase in ("讲个故事", "讲故事", "一起编故事", "继续故事")):
            companion_activity_type = "interactive_story"
        elif any(phrase in compact for phrase in ("词语接龙", "成语接龙", "文字接龙")):
            companion_activity_type = "word_chain"
        elif any(phrase in compact for phrase in ("猜谜语", "猜谜", "脑筋急转弯", "玩游戏")):
            companion_activity_type = "riddle"
        elif any(phrase in compact for phrase in ("跳绳挑战", "运动游戏", "活动一下", "原地跳")):
            companion_activity_type = "movement"
        elif any(phrase in compact for phrase in ("整理桌面", "整理挑战", "收拾桌面")):
            companion_activity_type = "tidy_challenge"
        if companion_activity_type:
            return "start_companion_activity", "好，我们开始一轮有自然结束时间的陪伴活动。", {
                "type": "start_companion_activity",
                "activity_type": companion_activity_type,
                "topic": text,
            }
        if any(phrase in compact for phrase in ("打开错题本", "查看错题本", "进入错题本", "看看我的错题")):
            return "open_mistake_book", "好，我现在打开你的错题本。", {"type": "open_mistake_book"}
        if state != "completed" and is_deictic_visual_question_request(text):
            return "capture_question", "好，把题目放到高拍仪下，我们先确认题干是完整的。", {"type": "request_capture"}
        if state == "study" and "PET" in command_compact and any(
            word in command_compact for word in ("开始", "学习", "复习", "练习", "单词", "词汇")
        ):
            return (
                "start_pet_vocabulary",
                "好，切换到今天的 PET 学习。我先从到期词汇和当前计划开始。",
                {"type": "start_pet_vocabulary_review"},
            )
        if any(phrase in compact for phrase in ("我写一道数学题", "我来写一道数学题", "我要写一道数学题")):
            return (
                "prepare_written_question",
                "好，写完后直接说“帮我看这题”，我会用高拍仪读取，不会接着追问刚才的话题。",
                {"type": "none", "source": "local_direction_change"},
            )
        if command_compact in {"对吧", "是吧", "对不对", "我说得对吧", "我说的对吗"}:
            return (
                "confirmation_followup",
                "我会按你刚才完整说的那句话核对；这句“对吧”不会被当成下一道题的答案。",
                {"type": "none", "source": "local_confirmation_guard"},
            )
        correction = any(phrase in compact for phrase in (
            "我刚才问", "我问的是", "你理解错", "你听错", "我让你讲", "重新讲",
        ))
        tutor_request = any(phrase in compact.lower() for phrase in (
            "不明白", "不懂", "为什么", "怎么理解", "讲解", "讲一下", "解释", "原理", "区别", "什么意思",
        ))
        study_feedback = state == "study" and any(phrase in compact for phrase in (
            "还卡着", "还是不懂", "没听懂", "还不会", "我懂了", "明白了", "会做了",
        ))
        if (
            (correction or tutor_request)
            and not study_feedback
            and not sensitive_categories(text)
            and not any(phrase in compact for phrase in ("拍题", "高拍仪", "错题本"))
        ):
            question = AgentOrchestrator._reconstruct_tutor_question(text, recent_dialogue)
            subject = AgentOrchestrator._subject_from_text(question)
            hint_level = AgentOrchestrator._tutor_hint_level(question)
            intent = "correct_previous" if correction else "tutor_question"
            response = "明白了，我按你纠正后的问题重新讲。" if correction else "好，我按你的问题来讲，再用一个小问题确认你是否理解。"
            return intent, response, {"type": "ask_tutor", "question": question, "subject": subject, "hint_level": hint_level}
        if any(phrase in compact for phrase in ("安静一会儿", "先别提醒", "等一下", "我想静一静")):
            return "snooze", "好，我先安静十分钟。需要我时直接叫我。", {"type": "snooze", "minutes": 10}
        if any(phrase in compact for phrase in ("只有你懂我", "别告诉爸爸妈妈", "你不能离开我", "只和你说")):
            return "relationship_boundary", "我愿意听你说，但我不能取代家人和真实朋友，也不会答应把安全风险保密。我们可以一起想想，现在找哪位可信任的大人最合适。", {"type": "find_parent"}
        if any(phrase in compact for phrase in ("找家长", "叫爸爸", "叫妈妈", "我要找爸爸", "我要找妈妈")):
            return "find_parent", "好，请现在去找家长。我会把当前进度保存在这里，回来后可以继续。", {"type": "find_parent"}
        if any(phrase in compact for phrase in ("简单一点", "说简单点", "没听懂", "换个说法")):
            return "simplify_request", AgentOrchestrator._simplify(previous_response), {"type": "none"}
        if state == "reflection" and AgentOrchestrator._direct_reflection_command(text, REFLECTION_FINISH_PHRASES):
            return "reflection_complete", "好，我保存今天的复盘并关闭摄像头和麦克风。", {"type": "complete_reflection", "skipped": False}
        if state == "study" and any(phrase in compact for phrase in ("开始复盘", "进入复盘", "回顾今天")):
            return "request_end_session", "好，今天的任务都已处理，我们进入三句话复盘。", {"type": "request_end"}
        if state != "reflection" and any(phrase in compact for phrase in ("结束今天学习", "今天不学了", "结束学习")):
            return "request_end_session", "我先检查还有哪些任务没有处理。完成或明确延期后，就进入今天的复盘。", {"type": "request_end"}
        if any(phrase in compact for phrase in ("判断错了", "你看错了", "我在心算", "不是走神")):
            return "observation_correction", "收到，这次只是一个低置信候选，不会写成你的标签。我会降低同类判断的优先级。", {"type": "correct_observation", "correction": text}
        if state == "check_in" and any(word in compact for word in ("有点累", "很累", "没精神", "状态不好")):
            return "set_energy_low", "知道了，今天会把任务切得更短，也会留出休息。", {"type": "update_planning_context", "energy_level": "low"}
        if state == "check_in" and any(word in compact for word in ("状态很好", "精神很好", "精力充沛")):
            return "set_energy_high", "记下了。状态好也不会把时间排满。", {"type": "update_planning_context", "energy_level": "high"}
        if state == "check_in" and any(word in compact for word in ("晚饭", "洗澡", "训练", "上课", "出门")):
            minutes = AgentOrchestrator._minutes_from_text(compact, 30)
            return "set_fixed_commitment", f"好，今晚固定安排预留{minutes}分钟。", {"type": "update_planning_context", "fixed_commitment_minutes": minutes, "fixed_commitment_note": text}
        if state == "check_in" and "明天" in compact and any(word in compact for word in ("考试", "测验", "听写", "检查", "默写")):
            minutes = AgentOrchestrator._minutes_from_text(compact, 10)
            subject = AgentOrchestrator._subject_from_text(compact)
            return "set_tomorrow_event", f"记下了，明天的这项检查会优先安排{minutes}分钟准备。", {"type": "update_planning_context", "tomorrow_event_subject": subject, "tomorrow_event_title": text, "tomorrow_event_minutes": minutes}
        if state == "check_in" and any(word in compact for word in ("准备好了", "开始吧", "可以开始")):
            return "check_in_complete", "好，我们先用自己的话回想今天课堂上讲了什么。", {"type": "transition", "target": "classroom_recall"}
        if state == "classroom_recall" and any(word in compact for word in ("老师讲了", "今天学了", "课上讲了", "我记得")):
            items = AgentOrchestrator._compound_recall_items(text)
            if len(items) > 1:
                subjects = "、".join(item["subject"] for item in items)
                return "add_classroom_recall_batch", f"我听到了{len(items)}科：{subjects}。我会逐项记下，不会只保留最后一项。", {"type": "add_recall_batch", "items": items}
            subject = AgentOrchestrator._subject_from_text(compact)
            return "add_classroom_recall", f"我先按{subject}记下来了，你可以继续补充。", {"type": "add_recall", "subject": subject, "summary": text}
        if state == "classroom_recall" and any(word in compact for word in ("这个对", "确实讲了", "候选正确")):
            return "confirm_recall_candidate", "好，我会按你的确认更新刚才的课堂候选。", {"type": "review_pending_recall", "decision": "confirm"}
        if state == "classroom_recall" and any(word in compact for word in ("这个不对", "不是这个", "没讲这个", "先忽略")):
            return "reject_recall_candidate", "好，我会排除刚才的候选，不把它当成课堂事实。", {"type": "review_pending_recall", "decision": "reject"}
        if state == "classroom_recall" and any(word in compact for word in ("想不起来", "不记得", "记不清", "今天没上课")):
            return "declare_no_recall", "好，我只记录你现在暂时想不起来，不会替你补写课堂内容。看到作业或教材后还能回来补充。", {"type": "declare_no_recall"}
        if state == "classroom_recall" and any(word in compact for word in ("说完了", "就这些", "看作业", "下一步")):
            return "recall_complete", "好，接下来把今天的作业列清楚。", {"type": "transition", "target": "homework_capture"}
        if state == "homework_capture" and any(word in compact for word in ("今天没作业", "没有作业", "今天无作业")):
            return "declare_no_homework", "好，我记录今天没有校内作业，不会生成一项假的作业。仍会检查明日重点、到期复测和长期目标。", {"type": "declare_no_homework"}
        if state == "homework_capture" and any(word in compact for word in ("上一项改成", "刚才那项改成", "刚才的作业改成")) and "分钟" in compact:
            minutes = AgentOrchestrator._minutes_from_text(compact, 20)
            return "revise_last_homework_minutes", f"好，我把刚才那项作业改为预计{minutes}分钟。", {"type": "update_last_homework_minutes", "expected_minutes": minutes}
        if state == "homework_capture" and any(word in compact for word in ("延期任务今天继续", "上次那项今天继续", "这项今天继续")):
            return "carryover_continue_today", "好，这项今天继续安排。", {"type": "review_first_carryover", "decision": "continue_today"}
        if state == "homework_capture" and any(word in compact for word in ("延期任务已经完成", "这项已经完成了", "在其他时间完成了")):
            return "carryover_completed_elsewhere", "好，我记录这项已经在其他时间完成。", {"type": "review_first_carryover", "decision": "completed_elsewhere"}
        if state == "homework_capture" and any(word in compact for word in ("老师取消这项", "这项不用做了", "这项不再要求")):
            return "carryover_cancelled", "好，我记录老师已经取消或不再要求这项任务。", {"type": "review_first_carryover", "decision": "cancelled"}
        if state == "homework_capture" and any(word in compact for word in ("作业", "背诵", "朗读", "订正")) and not any(word in compact for word in ("录完", "都完", "没有了")):
            items = AgentOrchestrator._compound_homework_items(text)
            if len(items) > 1:
                subjects = "、".join(item["subject"] for item in items)
                return "add_homework_batch", f"我听到了{len(items)}项作业：{subjects}。我会逐项保存各自的时间和要求。", {"type": "add_homework_batch", "items": items}
            action = AgentOrchestrator._homework_action(text)
            return "add_homework", AgentOrchestrator._homework_confirmation(action), action
        if state == "homework_capture" and any(word in compact for word in ("作业录完了", "都录完了", "没有了", "制定计划")):
            return "generate_plan", "好，我会把明日重点、作业、休息和长期目标一起算进时间表。", {"type": "generate_plan"}
        if state == "plan_review" and any(word in compact for word in ("确认计划", "按这个开始", "计划可以", "开始学习")):
            return "confirm_plan", "好，按这个顺序开始。需要调整时直接告诉我。", {"type": "transition", "target": "study"}
        if state == "plan_review" and any(word in compact for word in ("先做短任务", "短的先做", "时间短的先做")):
            return "short_tasks_first", "好，我把较短的校内任务排在前面，然后重新读给你听。", {"type": "reorder_plan", "strategy": "short_school_first"}
        if state == "plan_review" and any(word in compact for word in ("按作业顺序", "按原顺序", "校内作业顺序")):
            return "school_order_first", "好，我恢复按校内作业顺序执行。", {"type": "reorder_plan", "strategy": "school_first"}
        if state == "study" and VoiceControlIntent.local_task_navigation(text) == 'pause':
            # The bounded navigation route above must admit the frozen owner.
            # A missing/competing owner cannot become a legacy timer action.
            return "task_navigation_clarification", "我先核对当前任务，原题和进度保留。", {
                "type": "none", "source": "task_navigation_scope_unavailable", "local_only": True,
                "cloud_transmission_allowed": False}
        if state == "study" and any(word in compact for word in ("这项延期", "延期这项", "这项明天再做", "这个以后再做")):
            return "defer_task", "好，我会把当前任务延期并保存这个决定，然后继续看今天剩下的安排。", {"type": "reschedule_current_task", "reason": text}
        if state == "study" and any(word in compact for word in ("休息一下", "我要休息", "暂停学习")):
            return "request_break", f"好，先休息{break_minutes}分钟。喝水、站起来活动一下，时间到我再陪你继续。", {"type": "begin_break"}
        if state == "break" and any(word in compact for word in ("继续", "休息好了", "开始学习")):
            return "resume_study", "欢迎回来。我们从刚才的任务继续。", {"type": "resume_study"}
        if state == "study" and (completion := AgentOrchestrator._active_task_completion(text)):
            return completion
        if state == "study" and VoiceControlIntent.local_task_navigation(text) in {'next','continue'}:
            return "start_task", "我先核对原计划，开始成功后再告诉你。", {"type": "start_next_task"}
        if state != "completed" and any(word in compact for word in ("题干是对的", "题干正确", "识别正确", "题目没错", "题干没错")):
            return "confirm_ocr_text", "好，我按这份题干继续，只先给你一个小提示。", {"type": "confirm_ocr_text"}
        if state != "completed" and any(word in compact for word in ("重新拍", "重拍", "没有拍全", "没拍全", "拍不清楚", "识别错了")):
            return "retry_capture", "好，请把题目放完整并保持不动，我重新拍一次。", {"type": "retry_capture"}
        if state != "completed" and any(word in compact for word in ("取消拍题", "不拍了", "别拍了")):
            return "cancel_capture", "好，已经取消拍题，继续当前任务。", {"type": "cancel_capture"}
        if state != "completed" and any(prefix in compact for prefix in ("把题干改成", "题干改成", "正确题干是")):
            corrected = AgentOrchestrator._corrected_ocr_text(text)
            return "correct_ocr_text", "好，我先保存你口述的修正版，再按修正后的题干提示。", {"type": "correct_ocr_text", "corrected_text": corrected}
        if state == "study" and any(word in compact for word in ("来一道题", "来道题", "来一道校内题", "来一道PET题", "来一道奥数题", "换一道题", "下一道题")):
            track = AgentOrchestrator._diagnostic_track(text)
            return "start_diagnostic", "好，我准备一道适合当前进度的能力挑战。", {"type": "start_diagnostic", "track": track}
        if state == "study" and any(word in compact for word in ("结束背单词", "停止背单词", "单词先到这里", "退出词汇复习")):
            return "cancel_pet_vocabulary_review", "好，词汇复习先到这里，未完成的卡片仍会保留。", {"type": "cancel_pet_vocabulary_review"}
        if state == "study" and any(word in compact for word in ("复习单词", "背单词", "PET词汇", "练习词汇")):
            return "start_pet_vocabulary_review", "好，我们只复习今天到期的教材词汇，一张一张来。", {"type": "start_pet_vocabulary_review"}
        if state == "study" and VoiceControlIntent.local_task_navigation(text) == 'replay_current':
            return "task_navigation_clarification", "我先核对你说的原题，题目和进度保留。", {
                "type": "none", "source": "task_navigation_scope_unavailable", "local_only": True,
                "cloud_transmission_allowed": False}
        if state == "study" and compact.startswith(("答案是", "我的答案是", "我选")):
            return "submit_diagnostic_answer", "收到，我会保存这次作答并告诉你反馈。", {"type": "submit_diagnostic_answer", "answer": AgentOrchestrator._spoken_answer(text)}
        if state == "study" and any(word in compact for word in ("我懂了", "明白了", "会做了")):
            return "tutor_understood", "好，我记下这次提示已经帮你继续做了。", {"type": "record_tutor_feedback", "outcome": "understood"}
        if state == "study" and any(word in compact for word in ("还卡着", "还是不懂", "没听懂", "还不会")) and not any(word in compact for word in ("补了还是不懂", "这一步还是不懂", "错题还是不会")):
            return "tutor_still_stuck", "我记下你还卡着，不会把这次提示算作已经掌握。", {"type": "record_tutor_feedback", "outcome": "still_stuck", "feedback_text": text}
        if state == "study" and any(word in compact for word in ("再给一点提示", "再提示一点", "下一步提示")):
            return "more_tutor_hint", "好，我在刚才基础上再给一小步。", {"type": "more_tutor_hint"}
        if state == "study" and any(word in compact for word in ("确认是错题", "这确实是错题", "这是错题", "这不是错题", "不是错题", "我做对了", "这题没错", "这是对勾")):
            return "supportive_chat", "你不用判断它是不是错题。我会核对老师批改、这本习题的标准答案，或我出题时保存的评分规则；证据不足就先保持待评分。", {"type": "none"}
        if state == "study" and AgentOrchestrator._mistake_fact_exclusion(compact):
            return "exclude_mistake", "好，我会按你指出的识别或题目归属错误排除这条记录，不把你的话当作对错评分。", {"type": "review_current_mistake", "decision": "exclude"}
        if state == "study" and compact.startswith(("这次错在", "我错在", "错因是")):
            explanation = AgentOrchestrator._strip_spoken_prefix(text, ("这次错在", "我错在", "错因是"))
            return "diagnose_mistake", "好，错因先作为假设保存，接下来会用新题验证。", {"type": "diagnose_current_mistake", "error_type": AgentOrchestrator._mistake_error_type(explanation), "explanation": explanation}
        if state == "study" and any(word in compact for word in ("关键一步能说清了", "我能说清了", "补懂了")):
            return "mistake_remediation_complete", "好，我会按这次真实理解程度安排下一步。", {"type": "complete_mistake_remediation", "outcome": "completed", "notes": text}
        if state == "study" and any(word in compact for word in ("补了还是不懂", "这一步还是不懂", "错题还是不会")):
            return "mistake_remediation_stuck", "好，我不会继续追加同级题，会保留还没弄懂的证据。", {"type": "complete_mistake_remediation", "outcome": "still_stuck", "notes": text}
        if state == "study" and compact.startswith(("错题答案是", "这题答案是")):
            return "submit_mistake_answer", "收到，我会保存这次独立作答并更新强化进度。", {"type": "submit_mistake_answer", "answer": AgentOrchestrator._strip_spoken_prefix(text, ("错题答案是", "这题答案是"))}
        if state == "study" and any(word in compact for word in ("不会", "怎么做", "为什么", "提示", "讲一下", "帮我")) and "拍题" not in compact and "高拍仪" not in compact:
            return "tutor_question", "我先给你一个小提示，不直接替你做完。", {"type": "ask_tutor", "question": text}
        if any(word in compact for word in ("还有什么", "今天计划", "做到哪")):
            return "plan_status", "我来看看今天的计划和已经完成的任务。", {"type": "describe_plan"}
        if state == "reflection" and AgentOrchestrator._direct_reflection_command(text, REFLECTION_FINISH_PHRASES):
            return "reflection_complete", "好，我保存今天的复盘并关闭摄像头和麦克风。", {"type": "complete_reflection", "skipped": False}
        if state == "reflection" and AgentOrchestrator._direct_reflection_command(text, REFLECTION_SKIP_PHRASES):
            return "reflection_skip", "好，这次记为跳过复盘并结束，摄像头和麦克风会关闭。", {"type": "complete_reflection", "skipped": True}
        return "supportive_chat", "我听到了。你可以继续说具体哪里卡住，或者告诉我“开始任务”“完成了”“休息一下”。", {"type": "none"}

    @staticmethod
    def _minutes_from_text(text: str, default: int) -> int:
        match = re.search(r"(\d{1,3})分钟", text)
        if match:
            return min(180, max(5, int(match.group(1))))
        chinese = re.search(r"([零一二三四五六七八九十两]{1,4})分钟", text)
        if not chinese:
            return default
        digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        value = chinese.group(1)
        if "十" in value:
            left, right = value.split("十", 1)
            minutes = (digits.get(left, 1) if left else 1) * 10 + (digits.get(right, 0) if right else 0)
        else:
            minutes = digits.get(value, default)
        return min(180, max(5, minutes))

    @staticmethod
    def _homework_action(text: str) -> dict[str, Any]:
        compact = text.replace(" ", "")
        task_type = "written"
        for candidate, keywords in (
            ("correction", ("订正", "改错", "错题重做")),
            ("preview", ("预习",)),
            ("test_prep", ("考试准备", "测验准备", "听写准备", "默写准备")),
            ("memorization", ("背诵", "背会", "默写")),
            ("reading", ("朗读", "阅读")),
            ("review", ("复习",)),
        ):
            if any(keyword in compact for keyword in keywords):
                task_type = candidate
                break

        range_match = re.search(
            r"(第[0-9零一二三四五六七八九十百两]{1,6}页"
            r"(?:(?:到|至|-)第?[0-9零一二三四五六七八九十百两]{1,6}页)?)",
            compact,
        )
        range_text = range_match.group(1) if range_match else None

        due_at = None
        local_today = datetime.now().astimezone().date()
        if "后天" in compact and any(word in compact for word in ("交", "检查", "上课", "听写", "默写")):
            due_at = (local_today + timedelta(days=2)).isoformat()
        elif "明天" in compact and any(word in compact for word in ("交", "检查", "上课", "听写", "默写")):
            due_at = (local_today + timedelta(days=1)).isoformat()
        elif "今天" in compact and any(word in compact for word in ("交", "检查", "完成")):
            due_at = local_today.isoformat()

        requirements_match = re.search(
            r"(?:老师)?要求(.+?)(?:(?:，|,)?(?:大概|预计|约)?[0-9零一二三四五六七八九十百两]{1,5}分钟|$)",
            compact,
        )
        special_requirements = requirements_match.group(1).strip("，,。 ") if requirements_match else None

        return {
            "type": "add_homework",
            "subject": AgentOrchestrator._subject_from_text(compact),
            "title": text.strip(),
            "expected_minutes": AgentOrchestrator._minutes_from_text(compact, 20),
            "task_type": task_type,
            "range_text": range_text,
            "due_at": due_at,
            "special_requirements": special_requirements,
        }

    @staticmethod
    def _homework_confirmation(action: dict[str, Any]) -> str:
        type_label = {
            "written": "书面",
            "memorization": "背诵",
            "reading": "阅读或朗读",
            "correction": "订正",
            "review": "复习",
            "preview": "预习",
            "test_prep": "测验准备",
        }.get(str(action.get("task_type")), "学习")
        details = [f"{action['subject']}{type_label}任务", f"预计{action['expected_minutes']}分钟"]
        if action.get("range_text"):
            details.append(str(action["range_text"]))
        if action.get("due_at"):
            details.append(f"截止日期{action['due_at']}")
        if action.get("special_requirements"):
            details.append(f"老师要求{action['special_requirements']}")
        return "已记下" + "，".join(details) + "。你可以继续说下一项。"

    @staticmethod
    def _compound_subject_segments(text: str) -> list[tuple[str, str]]:
        aliases = (
            ("道德与法治", "道德与法治"), ("道法", "道德与法治"),
            ("语文", "语文"), ("数学", "数学"), ("英语", "英语"),
            ("物理", "物理"), ("化学", "化学"), ("历史", "历史"),
            ("地理", "地理"), ("生物", "生物"), ("科学", "科学"),
        )
        canonical = {alias: subject for alias, subject in aliases}
        pattern = re.compile("|".join(re.escape(alias) for alias, _ in aliases))
        matches = list(pattern.finditer(text))
        if len(matches) < 2:
            return []
        result: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            start = 0 if index == 0 else match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            segment = text[start:end].strip(" ，,;；。、和还有")
            if segment:
                result.append((canonical[match.group(0)], segment))
        return result

    @staticmethod
    def _compound_recall_items(text: str) -> list[dict[str, str]]:
        return [
            {"subject": subject, "summary": segment}
            for subject, segment in AgentOrchestrator._compound_subject_segments(text)
        ]

    @staticmethod
    def _compound_homework_items(text: str) -> list[dict[str, Any]]:
        return [
            AgentOrchestrator._homework_action(segment)
            for _, segment in AgentOrchestrator._compound_subject_segments(text)
        ]

    @staticmethod
    def _corrected_ocr_text(text: str) -> str:
        compact = text.strip()
        for prefix in ("把题干改成", "题干改成", "正确题干是"):
            if compact.replace(" ", "").startswith(prefix):
                index = compact.find(prefix[-1]) + 1
                corrected = compact[index:].strip(" ，,。")
                return corrected or compact
        return compact

    @staticmethod
    def _diagnostic_track(text: str) -> str:
        compact = text.upper().replace(" ", "")
        if "PET" in compact or "英语" in compact:
            return "pet"
        if "奥数" in compact or "竞赛" in compact:
            return "competition_math"
        return "school"

    @staticmethod
    def _spoken_answer(text: str) -> str:
        return AgentRuntimeStore.local_answer_forms(text, 'diagnostic.submit')[-1]

    @staticmethod
    def _strip_spoken_prefix(text: str, prefixes: tuple[str, ...]) -> str:
        compact = text.strip()
        compact_no_space = compact.replace(" ", "")
        for prefix in prefixes:
            if compact_no_space.startswith(prefix):
                index = compact.find(prefix[-1]) + 1
                value = compact[index:].strip(" ，,。")
                return value or compact
        return compact

    @staticmethod
    def _mistake_error_type(explanation: str) -> str:
        # Legacy speech routing preserves the explanation, not a diagnosis.
        # Only discriminating assessment can establish the cause of a mistake.
        return "unknown"

    @staticmethod
    def _mistake_fact_exclusion(transcript: str) -> bool:
        """Accept only observable source corrections, never a correctness opinion."""
        compact = transcript.replace(" ", "")
        return any(phrase in compact for phrase in (
            "题干识别错", "题目识别错", "答案识别错", "作答识别错", "识别有误",
            "不是我做的", "不是我的题", "我没做这题", "我没有做这题", "这题没作答",
            "题目拍错了", "拍错题了", "题目归属错", "排除这条识别记录",
        ))

    @staticmethod
    def _subject_from_text(text: str) -> str:
        if "道德与法治" in text or "道法" in text:
            return "道德与法治"
        for subject in ("语文", "数学", "英语", "物理", "化学", "历史", "地理", "生物", "科学"):
            if subject in text:
                return subject
        lowered = text.lower()
        if (any(token in lowered for token in ("三角函数", "方程", "几何", "函数", "分母"))
                or re.search(r"(?<![a-z])(?:sin|cos|tan|sine|cosine|tangent)(?![a-z])", lowered)):
            return "数学"
        if (re.search(r"(?<![a-z])[a-z]{2,}(?![a-z])", lowered)
                and re.search(r"从句|语法|时态|单词|词汇|词义|短语|发音|读音|代词|动词|介词|形容词|句型", text)):
            return "英语"
        return "其他"

    @staticmethod
    def _tutor_hint_level(question: str) -> int:
        compact = question.replace(" ", "")
        if any(phrase in compact for phrase in (
            "原理", "概念", "定义", "为什么", "什么意思", "有什么区别", "怎么理解", "系统讲", "重新讲",
        )):
            return 6
        return 1

    @staticmethod
    def _simplify(previous_response: str | None) -> str:
        if not previous_response:
            return "刚才还没有可以简化的内容。你把最难懂的那一句告诉我。"
        sentences = [item.strip() for item in re.split(r"[。！？!?]", previous_response) if item.strip()]
        core = sentences[0] if sentences else previous_response.strip()
        if len(core) > 90:
            core = core[:90].rstrip("，,；;：:")
        return f"简单说：{core}。你先告诉我，这里面哪个词还不明白？"

    @staticmethod
    def _get_replayed(connection, turn_id: str) -> dict:
        cached = AgentOrchestrator._get(connection, turn_id)
        if str(cached.get("runtime_capability") or "").startswith("learning_inputs."):
            from .learning_input_revision import LearningInputRevisionService, InputRevisionError
            try:
                replay = LearningInputRevisionService(None).replay_result(connection, turn_id)
            except InputRevisionError:
                replay = {"status": "superseded", "response_text": "这次修订的原始绑定已变化，请重新说明要修改的内容。"}
            if replay and replay["status"] == "superseded":
                cached["response_text"] = replay["response_text"]
                cached["action"] = {"type": "none", "source": "input_revision_replay_guard",
                                    "local_only": True, "cloud_transmission_allowed": False}
                cached["after_execution"] = "stop"
                cached["tool_status"] = "completed"
                return cached
        if cached["intent"] == "memory_command":
            cached["response_text"] = "这次记忆请求已处理。如需了解当前记忆，请再问我一次。"
            cached["action"].update(local_only=True, cloud_transmission_allowed=False)
        elif cached["intent"] == "daily_learning_summary":
            cached["response_text"] = "这次总结请求已处理。记录可能已经更新，请再说一次“总结今天的学习”，我会重新核对。"
            cached["action"].update(local_only=True, cloud_transmission_allowed=False)
        elif cached["intent"] in {"document_observation_question", "document_observation_response"}:
            action = cached["action"]
            context = action.get("document_followup") or action.get("document_evidence_context")
            state = AgentOrchestrator._document_followup_state(connection, cached["session_id"], cached["child_id"], context)
            if state != "valid":
                # Redact this replay projection only. Historical turns are not
                # erased by a memory operation or silently rewritten here.
                cached["response_text"] = "这次页面请求已处理，但原页面已不可用、暂停或过期。我不会重播旧页面内容，也不会声称仍在看那一页。"
                cached["transcript"] = ""
                cached["action"] = {"type": "none", "source": "local_document_replay_guard", "local_only": True,
                                    "cloud_transmission_allowed": False, "mastery_update_allowed": False,
                                    "document_reply_status": state}
        if cached['intent'] == 'pending_answer_clarification' and cached['response_text'] == ANSWER_ROLE_CLARIFICATION_RESPONSE:
            private_row = connection.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
            if private_row:
                private_action = json.loads(private_row['action_json'])
                if AgentOrchestrator._answer_role_notice_from_action(private_action, cached['response_text']) is not None:
                    cached['action']['suppress_speech'] = True
        if (cached['intent'] == 'pending_answer_clarification'
                and AgentOrchestrator._capture_mismatch_notice_from_action(cached['action'], cached['response_text']) is not None):
            # Idempotent transport replay is not a new request to read aloud.
            cached['action']['suppress_speech'] = True
        return cached

    @staticmethod
    def _get(connection, turn_id: str) -> dict:
        row = connection.execute("SELECT agent_turn_id, child_id, session_id, transcript, intent, response_text, action_json, orchestrator_version, tool_status, tool_error_code, completed_at, created_at FROM agent_turn WHERE agent_turn_id = ?", (turn_id,)).fetchone()
        result = dict(row)
        result["action"] = json.loads(result.pop("action_json"))
        result['action'].pop(ANSWER_ROLE_NOTICE_KEY, None)
        from .ordinary_learning_text_projection import INPUT_KEY
        result['action'].pop(INPUT_KEY, None)
        if result['action'].get('document_stop_followup_root'):
            root_id = result['action']['document_stop_followup_root']
            followup = ContinuationStore.document_followup(connection, root_id)
            work = connection.execute('SELECT * FROM agent_continuation WHERE next_turn_id=?', (turn_id,)).fetchone()
            if (not followup or not work or work['status'] != 'completed'
                    or not ContinuationStore._document_source_valid(connection, dict(work), followup)):
                result['response_text'] = '原请求已取消或原页面已变化，我不会重播旧页面内容。'
                result['action'] = {'type': 'none', 'source': 'document_stop_followup_unavailable',
                    'local_only': True, 'cloud_transmission_allowed': False}
        result["requires_control_admission"] = False
        runtime_step = connection.execute(
            """
            SELECT ast.execution_location, ast.capability_name, ap.plan_json, ap.context_manifest_json
            FROM agent_plan ap JOIN agent_step ast ON ast.plan_id = ap.plan_id
            WHERE ap.agent_turn_id = ? AND ast.step_index = 0
            """,
            (turn_id,),
        ).fetchone()
        if runtime_step:
            result["requires_control_admission"] = bool(
                runtime_step["execution_location"] == "client_device_bridge"
                and (json.loads(runtime_step["context_manifest_json"]).get("voice_control_binding")
                     or json.loads(runtime_step["context_manifest_json"]).get("local_controlled_launch_cancel")
                     or json.loads(runtime_step["context_manifest_json"]).get("local_recovery_control")
                     or json.loads(runtime_step["context_manifest_json"]).get("pending_local_control") == 'child_safety')
            )
            result["runtime_execution_location"] = runtime_step["execution_location"]
            result["runtime_capability"] = runtime_step["capability_name"]
            result["after_execution"] = json.loads(runtime_step["plan_json"]).get("after_execution", "stop")
            if runtime_step["capability_name"].startswith("learning_inputs."):
                server = AgentRuntimeStore.input_revision_execution_receipt(connection, turn_id)
                if server and server["result"] is not None:
                    # Shared by same-key replay and recovery readers. No dispatch
                    # or completion checkpoint is performed by this projection.
                    result["response_text"] = server["result"]["response_text"]
        return result


class UnknownAgentTurnError(Exception):
    pass


class InvalidAgentTurnOutcomeError(Exception):
    pass
