from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from .database import Database, SESSION_TRANSITION_TARGETS
from .child_model import ChildModelService
from .child_state_consumption import ChildStateConsumptionService
from .agent_continuation import ContinuationStore, RESULT_CONTINUATION_CAPABILITIES


PLANNER_VERSION = "agent_runtime_planner_v1"


class _RuntimeTransactionDatabase:
    """Let existing local services share Runtime's atomic admission/write boundary."""

    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        from .original_connection_read import original_connection_view
        view = _RuntimeTransactionDatabase(self.connection)
        with original_connection_view(view, self.connection):
            yield view

    def execute(self, sql, parameters=()):
        if sql.strip().upper() in {"BEGIN", "BEGIN IMMEDIATE"}:
            if not self.connection.in_transaction:
                raise RuntimeError("Runtime transaction missing")
            return None
        return self.connection.execute(sql, parameters)

    def commit(self):
        pass

    def rollback(self):
        # Readback services release their own read transaction; Runtime owns it.
        pass

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _valid_recent_time(value: str | None, now: datetime, future: bool = False) -> bool:
    if not value:
        return False
    try:
        timestamp = _aware_datetime(value.replace("Z", "+00:00"))
        return timestamp > now if future else 0 <= (now - timestamp).total_seconds() <= 15
    except (ValueError, TypeError, AttributeError):
        return False
PLANNER_PROMPT_VERSION = "agent_runtime_plan_prompt_v11_tutor_followup_relation"
PLANNER_POLICY_VERSION = "agent_runtime_policy_v1"

# Parameter vocabulary belongs to the tool contract, never to the child's speech.
CAPABILITY_ENUM_ARGUMENTS = {
    "learning.explain": {"question_relation": ("new", "followup")},
    "system.inspect_learning": {
        "scope": ("resources", "plan", "memory", "progress", "requests", "all"),
        "track": ("pet", "competition_math", "school", "all"),
    },
    "tutor.playback_control": {"command": ("pause", "resume", "previous", "restart", "cancel")},
    "pet.listening_control": {"command": ("pause", "resume", "restart", "cancel")},
    "session.transition": {"target": ("classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection", "evolving", "completed")},
    "companion.start_activity": {"activity_type": ("interactive_story", "word_chain", "riddle", "movement", "tidy_challenge")},
    "planning.reorder": {"strategy": ("school_first", "short_school_first")},
    "document.monitor_control": {"command": ("pause", "resume", "inspect", "review")},
    "mistake.activity_control": {"command": ("reload", "sync")},
    "mistake.capture_control": {"command": ("open", "set_answer", "correct_prompt", "save", "skip", "close")},
    "checkin.update_context": {"energy_level": ("low", "medium", "high")},
    "recall.review_pending": {"decision": ("confirm", "reject")},
    "homework.review_carryover": {"decision": ("continue_today", "completed_elsewhere", "cancelled")},
    "learning_inputs.propose_revision": {"kind": ("homework", "recall"), "operation": ("update", "delete", "undo", "add")},
    "learning_inputs.review_unplaced": {"decision": ("continue_today", "drop", "keep")},
    "learning_inputs.review_reclaimed_scope": {"operation": ("confirm_scope", "withdraw_scope")},
    "mistake.complete_remediation": {"outcome": ("completed", "still_stuck", "skipped")},
    "task.complete": {"completion_evidence_type": ("voice_confirmed", "child_confirmed", "child_checked", "needs_correction")},
    "device.connect": {"device": ("camera", "document_camera", "all")},
    "device.status": {"device": ("camera", "document_camera", "all")},
    "workspace.navigate": {"destination": ("learning", "mistake_book")},
    "behavior.feedback": {"feedback": ("accepted", "declined")},
}
OBJECTIVE_TYPES = frozenset({"learning", "execution", "habit", "companion"})
OBJECTIVE_MODES = frozenset({"new", "continue", "pause", "complete", "cancel", "none"})
# These capability plans never transition an unrelated objective. Their own
# software, sensor or retained-source effects still require the original authority.
OBJECTIVE_NEUTRAL_CAPABILITIES = frozenset({
    "mistake.learn", "planning.describe", "device.status", "system.inspect_learning", "workspace.navigate",
    "device.connect", "observation.pause", "observation.resume", "observation.correct",
    "voice.configure_wake_word", "speech.stop", "speech.repeat", "profile.switch", "privacy.pause_all",
    "tutor.playback_control", "pet.listening_control",
    "native_writing.submission_status", "native_writing.submit_retained",
})


class InvalidAgentPlanError(ValueError):
    pass


class InvalidDeviceReceiptError(ValueError):
    """Map to HTTP 409 outside the transaction; codes contain no child data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    version: str
    description: str
    objective_types: tuple[str, ...]
    allowed_states: tuple[str, ...]
    execution_location: str
    risk_level: str
    required_arguments: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    action_type: str

    def prompt_projection(self, state: str | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "objective_types": self.objective_types,
            "allowed_states": self.allowed_states or ("any",),
            "required_arguments": self.required_arguments,
            "argument_choices": (
                {"target": SESSION_TRANSITION_TARGETS.get(state, ())}
                if self.name == "session.transition" and state is not None
                else CAPABILITY_ENUM_ARGUMENTS.get(self.name, {})
            ),
            "expected_evidence": self.expected_evidence,
        }


class CapabilityRegistry:
    """The single discoverable contract between planning and execution.

    The existing renderer action format remains a temporary device-bridge
    adapter.  New capabilities must be registered here and must not add a new
    utterance classifier branch.
    """

    def __init__(self, capabilities: tuple[CapabilitySpec, ...] | None = None):
        specs = capabilities or DEFAULT_CAPABILITIES
        self._items = {item.name: item for item in specs}
        if len(self._items) != len(specs):
            raise ValueError("duplicate capability name")

    @staticmethod
    def _available_in(item: CapabilitySpec, state: str) -> bool:
        if item.name.startswith(("diagnostic.", "pet_vocabulary.")) and state in {"evolving", "completed"}:
            return False
        return not item.allowed_states or state in item.allowed_states

    def available(self, state: str) -> tuple[CapabilitySpec, ...]:
        return tuple(
            item for item in self._items.values()
            if self._available_in(item, state)
        )

    def require(self, name: str, state: str, *, arguments: dict | None = None) -> CapabilitySpec:
        item = self._items.get(name)
        if not item:
            raise InvalidAgentPlanError(f"unknown capability: {name}")
        stop_document = name == 'document.monitor_control' and (arguments or {}).get('command') == 'pause'
        if not self._available_in(item, state) and not stop_document:
            raise InvalidAgentPlanError(f"capability {name} is unavailable in {state}")
        return item

    def catalog(self, state: str | None = None) -> list[dict[str, Any]]:
        items = self.available(state) if state else tuple(self._items.values())
        catalog = [
            {
                **item.prompt_projection(state),
                "version": item.version,
                "execution_location": item.execution_location,
                "risk_level": item.risk_level,
                "action_type": item.action_type,
            }
            for item in items
        ]
        return catalog

    def action_types(self) -> frozenset[str]:
        return frozenset(item.action_type for item in self._items.values())

    def owner_for_action_type(self, action_type: str, state: str | None = None, *, arguments: dict | None = None) -> CapabilitySpec:
        capability_name = DEFAULT_ACTION_CAPABILITY_MAP.get(action_type, "")
        if not capability_name:
            raise InvalidAgentPlanError(f"unowned agent action: {action_type}")
        capability = (
            self.require(capability_name, state, arguments=arguments)
            if state is not None
            else self._items.get(capability_name)
        )
        if capability is None:
            raise InvalidAgentPlanError(f"unknown capability: {capability_name}")
        return capability

    def bind_action(self, action: dict[str, Any], state: str | None = None) -> dict[str, Any]:
        """Attach one registered execution owner to every emitted action.

        Legacy deterministic interpretation still emits renderer action types.
        This adapter keeps those actions inside the same capability contract as
        model-planned actions while the executors are migrated behind one
        runtime boundary.
        """
        action_type = str(action.get("type") or "").strip()
        if not action_type:
            raise InvalidAgentPlanError("agent action is missing type")
        capability_name = str(action.get("capability") or "").strip()
        if capability_name:
            capability = (
                self.require(capability_name, state, arguments=action)
                if state is not None
                else self._items.get(capability_name)
            )
            if capability is None:
                raise InvalidAgentPlanError(f"unknown capability: {capability_name}")
            if capability.action_type != action_type and DEFAULT_ACTION_CAPABILITY_MAP.get(action_type) != capability.name:
                raise InvalidAgentPlanError(
                    f"capability {capability_name} cannot own action {action_type}"
                )
        else:
            capability = self.owner_for_action_type(action_type, state, arguments=action)
            # Keep the stable renderer payload intact. Capability ownership and
            # execution metadata are persisted separately in agent_step.
            return dict(action)
        return {
            **action,
            "capability": capability.name,
            "execution_location": capability.execution_location,
            "risk_level": capability.risk_level,
        }


@dataclass(frozen=True)
class ObjectiveDraft:
    mode: str
    objective_type: str | None
    summary: str | None
    success_criteria: tuple[str, ...]
    phase: str


@dataclass(frozen=True)
class PlannedStep:
    capability: CapabilitySpec
    arguments: dict[str, Any]
    reason: str
    expected_evidence: tuple[str, ...]


@dataclass(frozen=True)
class AgentPlanDecision:
    objective: ObjectiveDraft
    step: PlannedStep
    spoken_transition: str
    requires_child_reply: bool
    confidence: float
    after_execution: str = "stop"
    continuation_scope: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": {
                "mode": self.objective.mode,
                "type": self.objective.objective_type,
                "summary": self.objective.summary,
                "success_criteria": list(self.objective.success_criteria),
                "phase": self.objective.phase,
            },
            "next_step": {
                "capability": self.step.capability.name,
                "capability_version": self.step.capability.version,
                "arguments": self.step.arguments,
                "reason": self.step.reason,
                "expected_evidence": list(self.step.expected_evidence),
                "execution_location": self.step.capability.execution_location,
                "risk_level": self.step.capability.risk_level,
            },
            "spoken_transition": self.spoken_transition,
            "requires_child_reply": self.requires_child_reply,
            "confidence": self.confidence,
            "after_execution": self.after_execution,
            "continuation_scope": self.continuation_scope,
        }


DEFAULT_CAPABILITIES = (
    CapabilitySpec(
        'native_writing.submission_status','1','只读核对本机书面原稿的原提交回执，不重新采集、提交、评分或认定掌握。',
        ('execution',),('study',),'client_device_bridge','low',('request_quote',),
        ('native_writing_receipt_checked',),'check_native_writing_submission',
    ),
    CapabilitySpec(
        'native_writing.submit_retained','1','明确提交本机保留且与冻结题目及文字哈希一致的原稿。未知结果只读核对，控制话不得作答。',
        ('execution',),('study',),'client_device_bridge','medium',('request_quote',),
        ('native_writing_submission_checked',),'submit_retained_native_writing',
    ),
    CapabilitySpec(
        'oral.retry', '1', '根据本题口语反馈明确重新作答。保留原答，新提示与新录音分开绑定，不作为独立复测。',
        ('execution',), ('study',), 'server', 'medium', ('request_quote',),
        ('oral_retry_prompt_bound',), 'retry_oral_answer',
    ),
    CapabilitySpec(
        'oral.independent_check', '1', '明确尝试另一道未暴露的同类口语题，只使用本地冻结的可靠题源。出题不表示通过。',
        ('execution',), ('study',), 'server', 'medium', ('request_quote',),
        ('oral_independent_prompt_bound',), 'check_oral_independently',
    ),
    CapabilitySpec(
        "writing.read_original", "1", "请求核对本题书面原稿。原题、页面版本和作答范围只由本地冻结；范围不明先询问起止句或单页重拍。朗读须真实开始结束回执，不评分。",
        ("execution",), ("study",), "client_device_bridge", "medium", ("request_quote",),
        ("writing_original_readback",), "read_writing_original",
    ),
    CapabilitySpec(
        "writing.confirm_original", "1", "明确确认刚刚完整读回的是本人本题作答且识别准确。必须本地冻结原声、原题、页面及语义回执；不是提交英语答案，不授权云上传。",
        ("execution",), ("study",), "server", "medium", ("request_quote",),
        ("writing_original_use_authorized",), "confirm_writing_original",
    ),
    CapabilitySpec(
        "writing.revise_original", "1", "按已完成的本机原稿自检开始修改，原稿和新稿分开保存。仅本地冻结原题与参考反馈来源，新提示辅助等级至少1，不认证独立掌握。",
        ("execution",), ("study",), "server", "medium", ("request_quote",),
        ("writing_revision_prompt_bound",), "revise_writing_original",
    ),
    CapabilitySpec(
        "writing.independent_check", "1", "根据已有正式写作反馈，明确请求另一道可靠且未暴露的同目标写作题。只用本地冻结的新题来源，不由模型编造；出题不等于独立掌握。",
        ("execution",), ("study",), "server", "medium", ("request_quote",),
        ("writing_independent_prompt_bound",), "check_writing_independently",
    ),
    CapabilitySpec(
        "conversation.respond", "1", "承接情绪、自由聊天或简短说明，不伪造工具执行。",
        ("companion",), (), "server", "low", ("response",), (), "none",
    ),
    CapabilitySpec(
        "system.inspect_learning", "1",
        "只读查询本机系统记录后回答题库、知识库、学习资料、学习计划、长期记忆、学习进度或最近请求的实际保存/处理状态。"
        "任何关于产品自身数据的事实问题都必须先用本能力，禁止由模型凭上下文猜有或没有。",
        ("execution", "learning"), (), "server", "low", ("scope", "track"),
        ("learning_system_facts_read",), "inspect_learning_system",
    ),
    CapabilitySpec(
        "companion.start_activity", "1", "开始有轮次和时长边界的原创故事、语言游戏或离屏活动。",
        ("companion",), (), "server", "low", ("activity_type",),
        ("activity_started", "natural_end_budget"), "start_companion_activity",
    ),
    CapabilitySpec(
        "companion.end_activity", "1", "结束陪伴活动，再询问实际参与、感受和恢复意愿；不自动恢复原任务。",
        ("companion", "execution"), (), "server", "low", (),
        ("activity_ended",), "end_companion_activity",
    ),
    CapabilitySpec(
        "companion.activity_feedback", "1", "记录本轮孩子对活动的实际参与或感受自述，或明确拒绝、请求恢复、延后恢复；不是活动成功认证。response_quote须逐字等于本轮完整原话。目标由本地冻结，缺失或歧义先澄清，不猜最近活动。仅feedback可带execution、experience，未知用unknown。",
        ("companion", "execution"), (), "server", "medium", ("operation", "response_quote"),
        ("activity_self_report_recorded", "activity_resume_intent_recorded"), "record_companion_activity_feedback",
    ),
    CapabilitySpec(
        "controlled_content.play", "1", "只从家长许可的本地教育内容目录选择并限时播放，暂停原任务。",
        ("companion", "learning", "execution"), ("study", "break"), "client_device_bridge", "medium", ("query",),
        ("parent_approval", "content_session_started", "time_limit", "previous_task_paused"),
        "start_controlled_content",
    ),
    CapabilitySpec(
        "controlled_content.stop", "2", "确认原视频窗口关闭后保存结束记录，再询问恢复意愿；不自动恢复原学习任务。",
        ("companion", "execution"), (), "client_device_bridge", "low", (),
        ("content_session_ended",), "stop_controlled_content",
    ),
    CapabilitySpec(
        "controlled_game.play", "1", "只启动家长许可的本地游戏，按日和单次预算强制结束并暂停原任务。",
        ("companion", "execution"), ("study", "break"), "client_device_bridge", "high", ("query",),
        ("parent_approval", "game_session_started", "os_process_bound", "time_limit", "previous_task_paused"),
        "start_controlled_game",
    ),
    CapabilitySpec(
        "controlled_game.stop", "2", "确认原游戏进程退出后保存结束记录，再询问恢复意愿；不自动恢复原学习任务。",
        ("companion", "execution"), (), "client_device_bridge", "medium", (),
        ("game_session_ended",), "stop_controlled_game",
    ),
    CapabilitySpec(
        "learning.explain", "2", "围绕一个明确知识目标生成同步语音与结构化黑板讲解，并留下立即检查问题。",
        ("learning",), (), "local_model", "medium", ("question", "subject"),
        ("child_check_response",), "ask_tutor",
    ),
    CapabilitySpec(
        "learning.evaluate_response", "1", "评价孩子对当前学习目标的回答，选择纠正、近迁移或下一步。",
        ("learning",), (), "local_model", "medium", ("response", "subject"),
        ("independent_response", "transfer_response"), "ask_tutor",
    ),
    CapabilitySpec(
        "tutor.capture_check_response", "1", "把实际语音回答绑定到已呈现检查问题，持久化提示后作答；不评分或授予独立掌握。",
        ("learning",), (), "server", "low", ("turn_id", "source_version", "response"),
        ("child_check_response",), "submit_tutor_check_response",
    ),
    CapabilitySpec(
        "tutor.retry_check_coaching", "1", "只按明确新语音重试已保存检查回答的同一点评请求；不再作答、评分或更新掌握。",
        ("learning",), (), "server", "low", ("feedback_id", "parent_turn_id"),
        ("saved_check_coaching",), "retry_tutor_check_coaching",
    ),
    CapabilitySpec(
        "learning.request_response", "1", "孩子只说听懂时，要求用自己的话作答，不把自我确认当作掌握证据。",
        ("learning",), (), "server", "low", ("prompt", "subject"),
        ("child_check_response",), "none",
    ),
    CapabilitySpec(
        "learning.capture_question", "3", "仅当孩子明确要求用高拍仪、拍照、看纸上或摆好的实物题目时，启动高拍仪采集和本地OCR。“听我说/读/念题”是语音口述，严禁调用本能力。题干确认属于后续独立步骤。",
        ("learning", "execution"), (), "client_device_bridge", "low", (),
        ("capture_artifact_id", "ocr"), "request_capture",
    ),
    CapabilitySpec(
        "mistake.learn", "1", "围绕当前错题或孩子刚说的新题进行形成性学习。capture保存本轮口述新题并分析；analyze解释错因、知识点和改正方法；practice给1至3道类似题；coach按孩子当前困难讲解或苏格拉底引导；answer提交本轮孩子对当前练习的原话；review_today打开到期复习；select按自然标题或序号选题。只给operation、selector、count、style，不得给内部ID、题目答案或改写孩子原话。本机绑定真实当前题、冻结练习并保存。讲解和模型反馈不代表正式掌握。",
        ("learning", "execution"), (), "client_device_bridge", "low", ("operation",),
        ("mistake_learning_saved",), "learn_mistake",
    ),
    CapabilitySpec(
        "mistake.open", "2", "查看错题本或恢复独立错题整理。不是开始日计划任务：孩子明确要做某科错题复习时用task.start，由本机选择计划任务并接原错题；不能只打开错题本代替任务执行。",
        ("learning", "execution"), (), "server", "low", (),
        ("mistake_activity_loaded",), "open_mistake_book",
    ),
    CapabilitySpec(
        "planning.describe", "1", "回答孩子询问今日计划、进度或下一项任务的提问；孩子明确要现在开始学习时应使用task.start，而不是只读计划。",
        ("execution",), (), "server", "low", (),
        ("plan_status_read",), "describe_plan",
    ),
    CapabilitySpec(
        "planning.generate", "1", "根据作业、长期目标、精力和硬约束生成今日计划。",
        ("execution",), ("homework_capture",), "server", "medium", (),
        ("plan_revision",), "generate_plan",
    ),
    CapabilitySpec(
        "task.start", "1", "孩子明确要现在启动已有日计划待办、开始学习或下一项任务时选择本能力，不要求固定口令；泛指开始由本机选择待办，可指定科目、作业、错题或活动类型，不限队首。请求给一道题、考考我或开始独立诊断属于diagnostic.start，不是启动或切换日计划任务，不能因为说到做题就选task.start。本机核验目标并调度原题，无唯一目标会追问。开始英语错题复习也是本能力，不是mistake.open；不代表掌握或完成。恢复暂停原任务用task.resume。"
        "arguments给start_selector，包含subject(学科英文枚举或null)、task_kind(review|written|oral|reading|listening|writing或null)、source_type(school_homework|mistake_review或null)、target_quote(本轮现在要执行的逐字肯定片段)。不得给任务ID。",
        ("execution",), ("study",), "server", "low", (),
        ("task_started",), "start_next_task",
    ),
    CapabilitySpec(
        "task.pause", "1", "暂停当前任务并保存计时。",
        ("execution",), ("study",), "server", "low", (),
        ("task_paused",), "pause_active_task",
    ),
    CapabilitySpec(
        "task.resume", "1", "继续本轮原话所指的已暂停任务或已保留原题但未启动的原任务，按真实操作保存用时；不能另选下一项。",
        ("execution",), ("study",), "server", "low", (),
        ("task_resumed",), "resume_paused_task",
    ),
    CapabilitySpec(
        "task.complete", "1", "保存当前任务完成状态；检查记录只是孩子自述，不是独立正确性或掌握证据。"
        "孩子分轮确认、否定或纠正任务检查时均调用本能力：confirmed_checks只列本轮明确确认的项，"
        "revoked_checks列本轮否定、尚未完成、不确定或要求撤销的项，confirmation_quote逐字引用本轮完整回答。"
        "例如先说题做完了但还没检查，确认task_finished并撤销missing_items_checked；"
        "后说漏题也检查过了，只确认missing_items_checked。说刚才说错了还没做完，应撤销task_finished，不能继续累计。"
        "pending_task_completion_check包含服务端已有confirmed_checks和missing_checks；不要复制历史项到本轮参数，"
        "服务端会合并并只追问缺项。首次只有笼统的全部确认时仍需核对，不能自己补全检查项。"
        "存在检查待办时不能用voice_confirmed绕过缺项；所有项确认才记录child_checked，不要求孩子重复已确认项。",
        ("execution",), ("study",), "server", "medium", (),
        ("task_completion",), "complete_active_task",
    ),
    CapabilitySpec(
        "session.transition", "1", "按孩子本轮明确意愿在当前每日学习阶段之间前进，包括签到结束进入课堂回想；不生成计划，不跳过阶段前置条件。",
        ("execution",), (), "server", "medium", ("target",),
        ("session_transition",), "transition",
    ),
    CapabilitySpec(
        "profile.switch", "1", "切换到另一个已存在孩子的独立档案。",
        ("execution",), (), "client_device_bridge", "medium", ("target_child_id",),
        ("profile_switched",), "switch_child_profile",
    ),
    CapabilitySpec(
        "observation.pause", "1", "直接幂等关闭或暂停当前会话的观察摄像头：设备桥执行停止读取、释放租约并验证持久状态。"
        "即使状态未知或已关闭也直接调用，不必先查询；只在真实回执后报告停止成功。",
        ("execution",), (), "client_device_bridge", "low", (),
        ("observation_paused",), "pause_observation",
    ),
    CapabilitySpec(
        "observation.resume", "1", "直接幂等打开或恢复当前会话的观察摄像头：设备桥获取或复用视频轨道，验证首帧、租约及持久状态。"
        "这是执行开启动作，不是仅导航；即使状态未知或看似已打开也直接调用，不必先查询。失败由设备桥报告，不能猜已看见孩子。",
        ("execution",), (), "client_device_bridge", "low", (),
        ("observation_resumed",), "resume_observation",
    ),
    CapabilitySpec(
        "observation.correct", "1", "记录孩子对行为观察结果的纠正。",
        ("execution", "habit"), (), "client_device_bridge", "low", ("correction",),
        ("observation_correction_saved",), "correct_observation",
    ),
    CapabilitySpec(
        "checkin.update_context", "1", "持久化签到时孩子本人明确提供的当前精力、固定安排和明日事项；精力使用energy_level的low、medium或high，不猜测未提供字段。",
        ("execution",), ("check_in",), "server", "low", (),
        ("planning_context_saved",), "update_planning_context",
    ),
    CapabilitySpec(
        "recall.add", "1", "在采集阶段先保存孩子口述的一条课堂学习内容，不虚构空回忆，不自动跳阶段。",
        ("execution", "learning"), ("check_in", "classroom_recall", "homework_capture"), "server", "low", ("subject", "summary"),
        ("recall_saved",), "add_recall",
    ),
    CapabilitySpec(
        "recall.add_batch", "1", "在采集阶段先将一次自然口述中的多学科课堂内容逐项保存，不丢失中间项，不虚构空回忆，不自动跳阶段。",
        ("execution", "learning"), ("check_in", "classroom_recall", "homework_capture"), "server", "low", ("items",),
        ("recalls_saved",), "add_recall_batch",
    ),
    CapabilitySpec(
        "recall.none", "1", "确认孩子暂时想不起课堂内容。",
        ("execution",), ("classroom_recall",), "server", "low", (),
        ("empty_recall_confirmed",), "declare_no_recall",
    ),
    CapabilitySpec(
        "homework.add", "1", "在采集阶段先保存一项孩子确认的学校作业，不虚构空回忆，不自动跳阶段。",
        ("execution",), ("check_in", "classroom_recall", "homework_capture"), "server", "low", ("subject", "title"),
        ("homework_saved",), "add_homework",
    ),
    CapabilitySpec(
        "homework.add_batch", "1", "在采集阶段先将一次自然口述中的多项作业拆分后逐项幂等保存，不虚构空回忆，不自动跳阶段。",
        ("execution",), ("check_in", "classroom_recall", "homework_capture"), "server", "low", ("items",),
        ("homework_items_saved",), "add_homework_batch",
    ),
    CapabilitySpec(
        "homework.none", "1", "确认今天没有需要录入的学校作业。",
        ("execution",), ("homework_capture",), "server", "low", (),
        ("empty_homework_confirmed",), "declare_no_homework",
    ),
    CapabilitySpec(
        "planning.reorder", "1", "按孩子的明确要求调整今日任务顺序。",
        ("execution",), ("plan_review",), "server", "medium", ("strategy",),
        ("plan_revision",), "reorder_plan",
    ),
    CapabilitySpec(
        "break.begin", "1", "暂停任务计时并开始计划内休息。",
        ("execution",), ("study",), "server", "low", (),
        ("break_started",), "begin_break",
    ),
    CapabilitySpec(
        "break.resume", "1", "结束休息并回到学习阶段。",
        ("execution",), ("break",), "server", "low", (),
        ("study_resumed",), "resume_study",
    ),
    CapabilitySpec(
        "task.reschedule", "1", "保存当前任务延期及原因。",
        ("execution",), ("study",), "server", "medium", ("reason",),
        ("task_rescheduled",), "reschedule_current_task",
    ),
    CapabilitySpec(
        "diagnostic.start", "1", "孩子要求给一道题、出题考考我、一道校内题或开始独立诊断，或明确说做这张卷、开始这份材料、下一题时，使用本能力。从已审核的本地题源呈现一道可靠原题等待孩子独立作答；这张卷由本机核对家长为当前孩子本次学习明确选择且仍有效的材料，模型不得传材料或题目ID，也不得把仅发布题目当作开始学习；不由模型编题或猜测题目ID。校内题arguments.track=school，PET题=pet，竞赛数学题=competition_math，未指定时由本机选择。当前日计划任务正在进行也可以请求一道题，本能力不结束、替换或另开日计划任务。objective.type为learning，成功标准包含实际独立作答或复测，不表示已经掌握。",
        ("learning",), (), "client_device_bridge", "medium", (),
        ("diagnostic_item_presented",), "start_diagnostic",
    ),
    CapabilitySpec(
        "diagnostic.submit", "1", "保存孩子独立作答并按量表评估。",
        ("learning",), (), "client_device_bridge", "medium", ("answer",),
        ("diagnostic_attempt",), "submit_diagnostic_answer",
    ),
    CapabilitySpec(
        "pet_vocabulary.start", "1", "从教材证据绑定的个人词库开始到期词汇复习。",
        ("learning",), (), "client_device_bridge", "low", (),
        ("vocabulary_card_presented",), "start_pet_vocabulary_review",
    ),
    CapabilitySpec(
        "pet_vocabulary.submit", "1", "保存当前词汇回答并更新间隔复习计划。",
        ("learning",), (), "client_device_bridge", "medium", ("answer",),
        ("vocabulary_attempt", "next_due_at"), "submit_pet_vocabulary_answer",
    ),
    CapabilitySpec(
        "pet_vocabulary.cancel", "1", "结束当前词汇回合，保留尚未复习的到期卡。",
        ("learning",), (), "client_device_bridge", "low", (),
        ("vocabulary_round_cancelled",), "cancel_pet_vocabulary_review",
    ),
    CapabilitySpec(
        "exam.start", "1", "开始按能力域覆盖组卷的限时专项模拟。",
        ("learning", "execution"), ("study",), "server", "medium", ("track", "subject"),
        ("exam_started", "domain_coverage", "time_limit"), "start_exam_simulation",
    ),
    CapabilitySpec(
        "exam.submit", "1", "保存当前模拟卷的独立作答并进入下一题。",
        ("learning",), ("study",), "server", "medium", ("answer",),
        ("exam_answer", "next_exam_item"), "submit_exam_answer",
    ),
    CapabilitySpec(
        "exam.pause", "1", "暂停模拟卷并持久化已用时间。",
        ("learning", "execution"), ("study",), "server", "low", (),
        ("exam_paused",), "pause_exam_simulation",
    ),
    CapabilitySpec(
        "exam.resume", "1", "从持久化时间点恢复模拟卷。",
        ("learning", "execution"), ("study",), "server", "low", (),
        ("exam_resumed",), "resume_exam_simulation",
    ),
    CapabilitySpec(
        "exam.cancel", "1", "提前交卷，保留已作答证据并结束未答部分。",
        ("learning", "execution"), ("study",), "server", "medium", (),
        ("exam_finished", "domain_results"), "cancel_exam_simulation",
    ),
    CapabilitySpec(
        "mistake.review", "3", "排除题干、实际作答或归属识别错误的记录；不得让孩子确认正误，正式错题由评分权威自动建立。",
        ("learning",), ("study",), "server", "low", ("decision",),
        ("mistake_review_started",), "review_current_mistake",
    ),
    CapabilitySpec(
        "mistake.diagnose", "2", "记录当前错题的错因假设并启动针对性补救。",
        ("learning",), ("study",), "server", "medium", ("error_type", "explanation"),
        ("mistake_diagnosis",), "diagnose_current_mistake",
    ),
    CapabilitySpec(
        "mistake.submit_response", "2", "兼容旧命令：仅恢复错题焦点并要求重新回答，不评分旧参数；新回答使用 mistake.dialogue。",
        ("learning",), ("study",), "server", "medium", ("answer",),
        ("mistake_focus_restored",), "submit_mistake_answer",
    ),
    CapabilitySpec(
        "reflection.update", "1", "保存孩子对今天学习的复盘。",
        ("execution", "habit"), ("reflection",), "client_device_bridge", "low", ("field", "value"),
        ("reflection_saved",), "update_reflection",
    ),
    CapabilitySpec(
        "reflection.complete", "1", "完成今日复盘并生成本地日报证据。",
        ("execution",), ("reflection",), "client_device_bridge", "medium", (),
        ("reflection_completed", "daily_report_ready"), "complete_reflection",
    ),
    CapabilitySpec(
        "document.capture_question", "1", "请高拍仪稳定采集当前题目。",
        ("learning", "execution"), (), "client_device_bridge", "low", (),
        ("document_artifact",), "request_capture",
    ),
    CapabilitySpec(
        "document.monitor_control", "1", "控制高拍仪持续监控、查看或核对当前页面。",
        ("learning", "execution"), ("study",), "client_device_bridge", "low", ("command",),
        ("document_monitor_state",), "control_document_monitor",
    ),
    CapabilitySpec(
        "behavior.feedback", "2", "把孩子对当前待确认提醒的接受、拒绝或稍后再说真实保存，不是只发送界面通知；只能处理本孩子本会话的待反馈提醒。",
        ("habit", "execution"), (), "server", "low", ("feedback",),
        ("intervention_feedback",), "behavior_intervention_feedback",
    ),
    CapabilitySpec(
        "privacy.pause_all", "1", "立即停止语音监听与本地视觉观察并释放设备。",
        ("execution",), (), "client_device_bridge", "low", (),
        ("camera_released", "microphone_released"), "privacy_pause",
    ),
    CapabilitySpec(
        "behavior.reset_personalization", "1", "重置本机行为提醒个性化参数。",
        ("habit", "execution"), (), "client_device_bridge", "medium", (),
        ("behavior_preferences_reset",), "reset_behavior_personalization",
    ),
    CapabilitySpec(
        "recall.review_pending", "1", "确认或否定一条待核对的课堂回忆。",
        ("execution", "learning"), ("classroom_recall",), "client_device_bridge", "medium", ("decision",),
        ("recall_review_saved",), "review_pending_recall",
    ),
    CapabilitySpec(
        "learning_inputs.review_unplaced", "1", "仅处理本地冻结焦点的未排作业义务。decision为continue_today/drop/keep，request_quote逐字等于本轮原话；keep另需原话支持的未来日期not_before。候选不明确先澄清；模型不得传义务ID或版本，自动续行不得代替孩子决定。",
        ("execution",), ("homework_capture", "plan_review", "study", "break"),
        "server", "medium", ("decision", "request_quote"), ("input_unplaced_reviewed",), "review_learning_input_unplaced",
    ),
    CapabilitySpec(
        "learning_inputs.review_reclaimed_scope", "1", "仅本地冻结已延期实配项的剩余范围。confirm_scope需remaining_scope_quote和remaining_minutes逐字据本轮request_quote，withdraw_scope只撤回范围确认。不推算完成比例，不自动补排，不接模型目标ID或自动续行。",
        ("execution",), ("homework_capture", "plan_review", "study", "break"), "server", "medium",
        ("operation", "request_quote"), ("input_reclaimed_scope_reviewed",), "review_learning_input_reclaimed_scope",
    ),
    CapabilitySpec(
        "learning_inputs.propose_revision", "1", "按当前原话提出作业或课堂项的修改、删除、撤销提议。target_quote引用所指项，changes每个新值须有change_quotes逐字来源；歧义先澄清，读回后确认才写入，不把unknown补成课堂事实。",
        ("execution", "learning"), ("check_in", "classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection"),
        "server", "medium", ("kind", "operation", "target_quote", "request_quote"), ("input_revision_proposed",), "propose_learning_input_revision",
    ),
    CapabilitySpec(
        "learning_inputs.select_target", "1", "仅回答当前未确认修订的目标或缺字段问题，selection_quote逐字引用本轮回答；字段须绑定原提问和来源，补全后读回，不直接写入作业事实。",
        ("execution", "learning"), ("check_in", "classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection"),
        "server", "medium", ("selection_quote",), ("input_revision_target_selected",), "select_learning_input_target",
    ),
    CapabilitySpec(
        "learning_inputs.confirm_revision", "1", "只确认已读回且版本未变的输入修订。confirmation_quote必须是本轮完整确认原话，不能拿提议话语当确认。",
        ("execution", "learning"), ("check_in", "classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection"),
        "server", "medium", ("confirmation_quote",), ("input_revision_applied",), "confirm_learning_input_revision",
    ),
    CapabilitySpec(
        "learning_inputs.cancel_revision", "1", "取消当前未执行的输入修订提议，不修改原记录。",
        ("execution", "learning"), ("check_in", "classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection"),
        "server", "low", ("confirmation_quote",), ("input_revision_cancelled",), "cancel_learning_input_revision",
    ),
    CapabilitySpec(
        "homework.update_duration", "1", "修正最近一项作业的预计时长。",
        ("execution",), ("homework_capture",), "client_device_bridge", "low", ("expected_minutes",),
        ("homework_duration_saved",), "update_last_homework_minutes",
    ),
    CapabilitySpec(
        "homework.review_carryover", "1", "处理一项从上次学习延续的任务。",
        ("execution",), ("homework_capture",), "client_device_bridge", "medium", ("decision",),
        ("carryover_decision_saved",), "review_first_carryover",
    ),
    CapabilitySpec(
        "document.confirm_ocr", "1", "确认本地OCR识别的当前题干。",
        ("learning", "execution"), (), "client_device_bridge", "low", (),
        ("confirmed_question_text",), "confirm_ocr_text",
    ),
    CapabilitySpec(
        "learning.review_schedule", "1", "按本轮语音和原题绑定提出、确认、修改、查询或取消复习时间，不认证掌握。",
        ("learning",), (), "server", "low", (),
        ("review_schedule_receipt",), "control_document_review",
    ),
    CapabilitySpec(
        "learning.review_revisit", "1", "按当前孩子的语音回到已确认原题，保存独立尝试或求助，不直接更新掌握度。",
        ("learning",), (), "server", "low", (),
        ("original_question_practice",), "control_document_revisit",
    ),
    CapabilitySpec(
        "document.correct_ocr", "1", "用孩子的语音纠正当前OCR题干。",
        ("learning", "execution"), (), "client_device_bridge", "low", ("corrected_text",),
        ("corrected_question_text",), "correct_ocr_text",
    ),
    CapabilitySpec(
        "document.retry_capture", "1", "重新采集当前题目。",
        ("learning", "execution"), (), "client_device_bridge", "low", (),
        ("document_artifact",), "retry_capture",
    ),
    CapabilitySpec(
        "document.cancel_capture", "1", "取消当前拍题流程且不丢失会话。",
        ("learning", "execution"), (), "client_device_bridge", "low", (),
        ("capture_cancelled",), "cancel_capture",
    ),
    CapabilitySpec(
        "pet.listening_control", "1", "仅按本轮原话控制本地冻结的PET原音。pause/resume/restart/cancel，不生成音频，不把播放当听懂；resume当前从原音起点重播。目标与版本不得来自模型。",
        ("learning", "execution"), (), "client_device_bridge", "low", ("command", "request_quote"),
        ("pet_listening_control_recorded",), "control_pet_listening",
    ),
    CapabilitySpec(
        "diagnostic.replay", "1", "重复当前诊断题且不暴露答案。",
        ("learning",), (), "client_device_bridge", "low", (),
        ("diagnostic_item_replayed",), "replay_diagnostic",
    ),
    CapabilitySpec(
        "diagnostic.cancel", "1", "取消当前待答诊断题，不删除已经保存的作答或评分。",
        ("learning",), (), "client_device_bridge", "low", (),
        ("diagnostic_prompt_cancelled",), "cancel_diagnostic",
    ),
    CapabilitySpec(
        "tutor.record_feedback", "1", "记录孩子对刚才讲解的真实反馈。",
        ("learning",), (), "client_device_bridge", "low", ("outcome",),
        ("tutor_feedback_saved",), "record_tutor_feedback",
    ),
    CapabilitySpec(
        "tutor.more_hint", "1", "对当前问题增加一层提示而不直接给答案。",
        ("learning",), (), "client_device_bridge", "low", (),
        ("next_hint_presented",), "more_tutor_hint",
    ),
    CapabilitySpec(
        "tutor.playback_control", "1", "控制已有讲解的实际播放位置：继续刚才讲解用resume，退回上一步用previous，"
        "从头再讲用restart，暂停或取消用pause/cancel。不生成新讲解、不增加提示、不把播放完等同掌握。"
        "previous和restart只定位，随后可继续播放；没有已有讲解时应说明，不能编造题目。",
        ("learning", "execution"), (), "client_device_bridge", "low", ("command",),
        ("tutor_playback_checkpoint",), "control_tutor_playback",
    ),
    CapabilitySpec(
        "mistake.complete_remediation", "2", "保存当前错题补救结果并返回可验证的下一步。",
        ("learning",), ("study",), "server", "medium", ("outcome",),
        ("remediation_outcome", "next_review_due"), "complete_mistake_remediation",
    ),
    CapabilitySpec(
        "mistake.activity_control", "2", "从服务端持久状态恢复或同步当前错题强化活动。",
        ("learning", "execution"), ("study",), "server", "low", ("command",),
        ("mistake_activity_state",), "control_mistake_activity",
    ),
    CapabilitySpec(
        "mistake.repair_question", "3", "孩子更正当前已保存错题的题干、原作答或两者，可继续修改尚未确认的口述提案。question_quote仅在本轮更正题干时逐字摘取完整新题干；original_answer_quote仅在更正原作答记录时逐字摘取本轮所说答案。至少提供一项，未改的一项省略，由本地保留，不补写题干、不猜标准答案，不当成新做题。分别读回题干和原作答，等待下一轮两项确认；保持同一错题，确认前不改变记录，不等于评分、掌握或完成。",
        ("learning", "execution"), ("study",), "server", "medium", (),
        ("mistake_question_repair_offer",), "repair_mistake_question",
    ),
    CapabilitySpec(
        "mistake.dialogue", "2", "按持久题目焦点核对、记录原答案、追问错因并推进复测。conversation_state.mistake_revision_readback.status为awaiting_confirmation时，正在共同核对题干和原作答；孩子对此回应时arguments必须仅含utterance（本轮完整原话）和revision_confirmation（both确认两项、question_only只确认题干、answer_only只确认原作答、uncertain犹豫或不确认）。结合上下文理解自然回应，不要求固定口令，不能把引用别人的话、否定、提问或只确认一项当both；需要修改内容时用mistake.repair_question。没有此待核对状态时不得给revision_confirmation。",
        ("learning", "execution"), ("study",), "server", "medium", ("utterance",),
        ("mistake_dialogue_state",), "continue_mistake_dialogue",
    ),
    CapabilitySpec(
        "mistake.capture_control", "1", "用语音控制错题采集、纠正、保存或放弃。",
        ("learning", "execution"), ("study",), "client_device_bridge", "medium", ("command",),
        ("mistake_capture_state",), "control_mistake_capture",
    ),
    CapabilitySpec(
        "speech.stop", "1", "立即停止当前播放并继续接收孩子说话。",
        ("execution", "companion"), (), "client_device_bridge", "low", (),
        ("speech_stopped",), "stop_speech",
    ),
    CapabilitySpec(
        "speech.repeat", "2", "重播当前孩子本会话上一次实际完成的回复，不编造重讲内容；没有可重播回复时如实说明。",
        ("execution", "companion"), (), "server", "low", (),
        ("response_replayed",), "repeat_response",
    ),
    CapabilitySpec(
        "onboarding.resume", "1", "按孩子要求重新开始或恢复语音初始化教程；等待桌面保存重启状态后确认。",
        ("execution", "companion"), (), "client_device_bridge", "low", (),
        ("onboarding_restarted",), "resume_voice_tutorial",
    ),
    CapabilitySpec(
        "voice.configure_wake_word", "1", "首次设置或日常修改学习伙伴的唤醒词，无需家长代办。candidate可选，必须从本轮明确名字逐字提取，2到12字；"
        "本轮已经给出新名字时必须填candidate，不能丢掉名字再让孩子重复。"
        "例如我想换个叫你的名字，以后喊你小葵行吗，应传candidate=小葵；把唤醒词改为小枝小枝，应传candidate=小枝小枝。"
        "只有我想改个名字但未说新名称时才传空对象，不编造候选。疑问语气也可以是在提出候选，前端仍会请孩子确认。"
        "工具仅开启命名或确认流程，不代表新名字已保存；孩子随后确认且本机保存成功后才能宣布改名。",
        ("execution", "companion"), (), "client_device_bridge", "low", (),
        ("wake_word_configuration_started",), "configure_wake_word",
    ),
    CapabilitySpec(
        "device.connect", "1", "按孩子要求连接观察摄像头、高拍仪或两者。设备桥必须重新枚举本机设备，"
        "逐台验证原绑定、真实视频轨道、首帧、租约和持久状态；只在Windows未发现原绑定设备时提示重新插拔。"
        "device=all表示同时连接观察摄像头和高拍仪；这是执行请求，不得先用device.status替代，也不得在回执前声称成功。",
        ("execution", "companion", "learning"), (), "client_device_bridge", "low", ("device",),
        ("device_connection_checked",), "connect_devices",
    ),
    CapabilitySpec(
        "device.status", "1", "查询观察摄像头或高拍仪当前真实视频轨道、帧和监控状态；等待设备桥回执，不启动拍题。"
        "仅用于孩子确实询问设备现状，不能作为开关命令的前置步骤，也不会查询后自动执行打开或关闭。"
        "礼貌疑问形式也可能是操作请求，应按希望改变状态还是了解现状区分，不能仅凭问号选择查询。"
        "历史记录仅是last_known，不能据此声称现在看见孩子、页面或推断物理电源。",
        ("execution", "companion", "learning"), (), "client_device_bridge", "low", ("device",),
        ("device_status_checked",), "query_device_status",
    ),
    CapabilitySpec(
        "document.set_occlusion_reminders", "1", "开启或关闭当前孩子的高拍仪遮挡语音提醒并保存偏好；"
        "轻微遮挡不要提示等自然要求也应调用本能力，enabled=false。仅控制提醒，绝不关闭隐私、遮挡检测或采集质量门。",
        ("execution", "companion", "habit"), (), "server", "low", ("enabled",),
        ("document_guidance_preferences_saved",), "set_document_occlusion_reminders",
    ),
    CapabilitySpec(
        "session.snooze", "1", "暂缓非紧急主动提醒一段时间。",
        ("execution", "habit"), (), "client_device_bridge", "low", ("minutes",),
        ("snooze_saved",), "snooze",
    ),
    CapabilitySpec(
        "session.request_end", "1", "核对未完成项后进入学习收尾。",
        ("execution",), (), "client_device_bridge", "medium", (),
        ("end_request_resolved",), "request_end",
    ),
    CapabilitySpec(
        "workspace.navigate", "1", "在当前孩子学习工作区切换学习或错题本浏览界面，保持语音会话；等待真实加载显示回执，不挑题或评分。",
        ("execution",), (), "client_device_bridge", "low", ("destination",),
        ("workspace_rendered",), "navigate_workspace",
    ),
    CapabilitySpec(
        "safety.find_parent", "1", "引导孩子在需要成人协助时联系家长。",
        ("execution", "companion"), (), "client_device_bridge", "medium", (),
        ("parent_contact_prompted",), "find_parent",
    ),
)


# Exactly one default capability owns every action emitted by the deterministic
# interpreter. Model-planned actions may choose a more specific registered
# capability with the same action type (for example learning.evaluate_response).
DEFAULT_ACTION_CAPABILITY_MAP = {
    "learn_mistake": "mistake.learn",
    "navigate_workspace": "workspace.navigate",
    "inspect_learning_system": "system.inspect_learning",
    'retry_oral_answer':'oral.retry',
    'check_oral_independently':'oral.independent_check',
    "check_writing_independently": "writing.independent_check",
    "read_writing_original": "writing.read_original",
    'check_native_writing_submission':'native_writing.submission_status',
    'submit_retained_native_writing':'native_writing.submit_retained',
    "confirm_writing_original": "writing.confirm_original",
    "revise_writing_original": "writing.revise_original",
    "record_companion_activity_feedback": "companion.activity_feedback",
    "control_tutor_playback": "tutor.playback_control",
    "control_pet_listening": "pet.listening_control",
    "configure_wake_word": "voice.configure_wake_word",
    "connect_devices": "device.connect",
    "query_device_status": "device.status",
    "set_document_occlusion_reminders": "document.set_occlusion_reminders",
    "resume_voice_tutorial": "onboarding.resume",
    "none": "conversation.respond",
    "start_companion_activity": "companion.start_activity",
    "end_companion_activity": "companion.end_activity",
    "start_controlled_content": "controlled_content.play",
    "stop_controlled_content": "controlled_content.stop",
    "start_controlled_game": "controlled_game.play",
    "stop_controlled_game": "controlled_game.stop",
    "ask_tutor": "learning.explain",
    "request_capture": "learning.capture_question",
    "open_mistake_book": "mistake.open",
    "describe_plan": "planning.describe",
    "generate_plan": "planning.generate",
    "start_next_task": "task.start",
    "pause_active_task": "task.pause",
    "resume_paused_task": "task.resume",
    "complete_active_task": "task.complete",
    "transition": "session.transition",
    "switch_child_profile": "profile.switch",
    "pause_observation": "observation.pause",
    "resume_observation": "observation.resume",
    "correct_observation": "observation.correct",
    "update_planning_context": "checkin.update_context",
    "add_recall": "recall.add",
    "add_recall_batch": "recall.add_batch",
    "declare_no_recall": "recall.none",
    "add_homework": "homework.add",
    "add_homework_batch": "homework.add_batch",
    "declare_no_homework": "homework.none",
    "reorder_plan": "planning.reorder",
    "begin_break": "break.begin",
    "resume_study": "break.resume",
    "reschedule_current_task": "task.reschedule",
    "start_diagnostic": "diagnostic.start",
    "submit_diagnostic_answer": "diagnostic.submit",
    "start_pet_vocabulary_review": "pet_vocabulary.start",
    "submit_pet_vocabulary_answer": "pet_vocabulary.submit",
    "cancel_pet_vocabulary_review": "pet_vocabulary.cancel",
    "start_exam_simulation": "exam.start",
    "submit_exam_answer": "exam.submit",
    "pause_exam_simulation": "exam.pause",
    "resume_exam_simulation": "exam.resume",
    "cancel_exam_simulation": "exam.cancel",
    "review_current_mistake": "mistake.review",
    "diagnose_current_mistake": "mistake.diagnose",
    "submit_mistake_answer": "mistake.submit_response",
    "update_reflection": "reflection.update",
    "complete_reflection": "reflection.complete",
    "control_document_monitor": "document.monitor_control",
    "behavior_intervention_feedback": "behavior.feedback",
    "privacy_pause": "privacy.pause_all",
    "reset_behavior_personalization": "behavior.reset_personalization",
    "review_pending_recall": "recall.review_pending",
    "update_last_homework_minutes": "homework.update_duration",
    "review_learning_input_unplaced": "learning_inputs.review_unplaced",
    "review_learning_input_reclaimed_scope": "learning_inputs.review_reclaimed_scope",
    "propose_learning_input_revision": "learning_inputs.propose_revision",
    "select_learning_input_target": "learning_inputs.select_target",
    "confirm_learning_input_revision": "learning_inputs.confirm_revision",
    "cancel_learning_input_revision": "learning_inputs.cancel_revision",
    "review_first_carryover": "homework.review_carryover",
    "confirm_ocr_text": "document.confirm_ocr",
    "control_document_review": "learning.review_schedule",
    "control_document_revisit": "learning.review_revisit",
    "correct_ocr_text": "document.correct_ocr",
    "retry_capture": "document.retry_capture",
    "cancel_capture": "document.cancel_capture",
    "replay_diagnostic": "diagnostic.replay",
    "cancel_diagnostic": "diagnostic.cancel",
    "record_tutor_feedback": "tutor.record_feedback",
    "submit_tutor_check_response": "tutor.capture_check_response",
    "retry_tutor_check_coaching": "tutor.retry_check_coaching",
    "more_tutor_hint": "tutor.more_hint",
    "complete_mistake_remediation": "mistake.complete_remediation",
    "control_mistake_activity": "mistake.activity_control",
    "control_mistake_capture": "mistake.capture_control",
    "continue_mistake_dialogue": "mistake.dialogue",
    "repair_mistake_question": "mistake.repair_question",
    "stop_speech": "speech.stop",
    "repeat_response": "speech.repeat",
    "snooze": "session.snooze",
    "request_end": "session.request_end",
    "find_parent": "safety.find_parent",
    "find_parent_urgent": "safety.find_parent",
}


def _without_local_only(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("cloud_transmission_allowed") is False:
            return None
        return {key: _without_local_only(item) for key, item in value.items()
                if not (isinstance(item, dict) and item.get("cloud_transmission_allowed") is False)}
    if isinstance(value, (list, tuple)):
        return [_without_local_only(item) for item in value
                if not (isinstance(item, dict) and item.get("cloud_transmission_allowed") is False)]
    return value


# These actions are assembled only from saved local authority, never provider IDs.
LOCAL_COMPOSED_CAPABILITIES = frozenset({'tutor.capture_check_response', 'tutor.retry_check_coaching'})


def build_planner_messages(context: dict[str, Any], transcript: str, registry: CapabilityRegistry) -> tuple[dict[str, str], ...]:
    state = context["session"]["state"]
    continuation = context.get("continuation_event")
    capabilities = [item.prompt_projection(state) for item in registry.available(state)
                    if item.name not in LOCAL_COMPOSED_CAPABILITIES
                    and (not continuation or item.name in RESULT_CONTINUATION_CAPABILITIES)]
    system = (
        "你是孩子端唯一的学习伙伴运行时。你不是意图分类器。你的任务是围绕孩子可验证的结果，"
        "选择下一项已注册能力。不要假装执行能力，不要把讲解完成等同于掌握。当前话语决定本轮目标；"
        "只有本轮仍围绕同一结果、确实继续或补充时才沿用活动目标。孩子提出无关的新明确目标时必须objective.mode=new，"
        "summary和success_criteria绑定新请求的实际结果，不能因为旧目标仍active就继续它。"
        "例如确认修改唤醒词后要求操作摄像头，设备操作是新execution目标，不属于命名确认；旧目标是否完成不可凭模型猜测。"
        "只询问无关现状可以mode=none或另建查询目标，不要把查询记成旧命名目标的进展。"
        "孩子纠正你时优先修正。学习目标必须把独立回答或迁移检查写进成功标准。"
        "先按本轮请求选择能力，再从该能力声明的objective_types选择目标类型，不能为了沿用旧目标而改选能力。"
        "请求给一道题、自测或考考我用diagnostic.start及learning目标；要求启动已有日计划待办才用task.start及execution目标。"
        "使用learning.explain时，明确追问刚讲过的同一道题或例题，arguments.question_relation设为followup；"
        "明确提出新题或新主题设为new。拿不准关系时先澄清，不得猜测沿用旧题；不要输出讲解记录ID。"
        "例如正在做作业时说来一道校内题，应diagnostic.start(arguments.track=school)，不是task.start，也不暂停或完成当前任务。"
        "若conversation_state.mistake_revision_readback.status为awaiting_confirmation，回应正在核对的两项记录必须先走mistake.dialogue的revision_confirmation。"
        "即使肯定回应附带继续讲解的请求，也不能跳过确认去tutor.playback_control或learning.explain；确认落库后本地会接续原题讲解。"
        "每轮只选择一个下一能力，等待执行结果或孩子回答后再计划。严格输出一个JSON对象，不要Markdown。"
        "学习目标不能由规划器标记complete或completed；只有本地学习结果评估流程能够结束学习目标。"
        "缺少有效证据时先核实并请求独立作答，不得依据历史概率、自述听懂或讲解完成宣布掌握。"
        "先理解孩子本轮话语的语义和最近对话，不要求任何固定口令；utterance_binding.role标明话语用途，quote逐字引用本轮原话。"
        "单独的知识名词、打招呼、试麦克风、复述你说过的话不是讲课许可：用conversation.respond澄清，禁止擅自讲题。"
        "孩子改变主意、纠正、提问或聊天时，不能因存在待答题就当成答案。只有conversation_state.awaiting_learning_reply为真且本轮确实作答才可learning.evaluate_response。"
        "作答必须使用conversation_state.pending_answer_capability指定的评分能力；diagnostic.submit、pet_vocabulary.submit和exam.submit不能被通用learning.evaluate_response替代。没有对应待答题不得提交答案。"
        "pending_question_conflict非空时先澄清要继续哪项任务，不能把同一句话分别判给多个题目；只在明确取消请求时取消指定任务。"
        "看这页、看看摆好的题等明确指向实物而未提供完整题干的请求应调用learning.capture_question，不能靠猜测讲题。"
        "孩子说“听我说题”、“我来读题”或“不要拍题”时绝对不得调用learning.capture_question；口述收题由本地持久会话状态机承接。"
        "不因当前处于签到而拒绝拍题、讲解或聊天；学习流程阶段仅约束必须保存的流程动作。"
        "conversation.respond的spoken_transition必须直接、有内容地回答或澄清，不要只说我听到了或我来处理。"
        "询问题库、知识库、学习资料、学习计划、长期记忆或学习进度是否存在、还有多少、当前状态时，"
        "必须选择system.inspect_learning；其中只询问今天日计划时也可选择planning.describe。"
        "询问刚才说的话有没有收到、断线/切换语音后请求处理到哪、是否完成时，选择system.inspect_learning的scope=requests。"
        "只能由本地回执回答，不要自动重做原请求，不要把已保存说成已执行，也不要把缺回执说成未执行或还在运行。"
        "system.inspect_learning的scope按resources、plan、memory、progress、requests或all选择，"
        "track按pet、competition_math、school或all选择。模型绝对不能自行声称本机有或没有这些数据。"
        "凡请求改变设备、任务、作业记录或活动状态，必须选择对应工具，不能用conversation.respond口头承诺已经执行。"
        "孩子说连接摄像头、连接高拍仪或连接两台设备时选择device.connect，并用device=camera、document_camera或all；"
        "它会重新枚举和验证真实画面，不能拆成只执行一台，不能在结果前建议无条件插拔。"
        "仅开关观察摄像头的请求也可直接选择observation.resume或observation.pause，不要先查device.status；"
        "这些幂等执行能力本身会核对真实视频轨道、首帧、租约和保存结果，状态未知或已达目标均不是先查询的理由。"
        "只有孩子要了解当前设备状态、没有要求改变状态时才选device.status；能否帮忙开启等礼貌问法仍是执行请求，不是状态问句。"
        "只有原请求还有明确未完成动作且无需孩子新回答时，after_execution=replan；单一动作或需要孩子回答则stop。"
        "replan还必须在continuation_scope列出原话明确要求的剩余能力、已知参数及逐字request_quote。"
        "它是授权边界而非必须执行的长计划，最多4项；不列隐含扩展任务，缺参数先询问并stop。"
        "无缺失参数的设备执行应requires_child_reply=false，等待真实设备回执，而非再次请孩子确认已经明确提出的操作。"
        "例如孩子要求暂时别看本子，应选择document.monitor_control(command=pause)；以后继续看则command=resume。"
        "工具可以幂等执行，所以即使认为设备已停止也要发出停止动作确认。spoken_transition只能说明将要做什么，执行成功后系统才会报告已完成。"
        "tutor_playback只表示讲解播放进度，不表示孩子掌握。孩子要求暂停、继续、上一段或从头讲时使用tutor.playback_control；"
        "previous和restart只移动位置并暂停，resume才播放。不得因有未完成讲解就自行恢复，也不要重新生成题目替代原讲解。"
        "companion.start_activity还需在arguments.opening给出根据本轮主题原创的第一轮内容和一个参与问题；不能只说现在开始，也不要复用固定故事。"
        "作业或课堂记录修订用learning_inputs.propose_revision：kind为homework或recall，operation为update/delete/undo；"
        "target_quote引用所指原项，request_quote引用本轮修改请求，changes给新值，change_quotes逐字段引用新值原话，中文分钟数可转数字但quote保留原话。"
        "不能推测旧记录、目标ID或版本；本地负责冻结和澄清。input_revision_dialogue.state为clarification_required且本轮在选项时用select_target，selection_quote引用所指项。"
        "只有当前awaiting_confirmation读回后的明确确认才用confirm_revision，取消当前提议用cancel_revision；confirmation_quote为本轮完整原话。所有修订stop，不修改学习目标。"
    )
    if state == "check_in" and not continuation:
        system += (
            "当前session.state为check_in。本轮若是孩子本人对现在或今天精力的明确自述，"
            "即使没有说记录或保存，也必须先选择checkin.update_context持久化，不能仅用conversation.respond承接而遗漏记录。"
            "按本轮含义填写arguments.energy_level：状态很好或精力充沛为high，疲累或没精神为low，明确说一般或普通为medium。"
            "例如孩子本人说‘我今天状态很好。’是当前精力high的签到自述，应先保存，工具结果确认后再自然回应。"
            "只提供孩子明确表达的字段，不编造固定安排、明日事项或其他参数；不能从未表达精力推断medium。"
            "本轮仅明确自述精力时选择execution目标，以planning_context_saved为成功证据，after_execution=stop、requires_child_reply=false。"
            "spoken_transition只能说明将要保存，不得在工具结果之前声称已记录。"
            "只引用故事、转述他人、描述过去、假设、疑问或含糊且相互矛盾而未澄清的精力表达，都不是当前本人精力事实；"
            "不更新精力，用conversation.respond正常回应或具体澄清。"
            "仅否认某个等级但未给出可确认的当前等级时应澄清，例如‘我不是状态很好’不能自动推断low；"
            "否定措辞本身不排除明确低精力自述，‘我没精神’或‘我不太有精神’仍为本人当前low。"
            "聊天、夸赞伙伴以及与精力无关的状态评价仍可正常聊天。"
            "拍题、讲解或其他明确请求继续按其语义选择原有能力，不能因签到阶段就改成精力记录；不得靠关键词或旧精力状态替孩子作判断。"
            "孩子在当前签到阶段明确表示本人现在已准备好结束签到、进入下一学习阶段时，例如‘准备好了。’或‘我这边都好了，往下继续吧’，"
            "应选择session.transition，arguments.target=classroom_recall，以session_transition作为执行证据；不是conversation.respond闲聊。"
            "仅表达这一结束签到意愿时，objective.type=execution、after_execution=stop、requires_child_reply=false；"
            "spoken_transition只说明即将进入课堂回想，不得在真实执行前声称已经切换，也不能改成planning.generate、跳过课堂回想或直接开始任务。"
            "按当前本人完整语义判断，不能靠关键词或从旧对话补同意，不能从已有high精力推断准备完成。"
            "未准备好、先别开始、取消、转述他人、故事、过去、条件假设和询问你是否准备好都不是本人当前结束签到的许可；"
            "例如‘准备好了但先别开始’仍需尊重暂缓，不转换阶段；模糊或相互矛盾且未澄清时应正常澄清。"
            "本轮同时提供新的精力或固定安排时，仍先选择checkin.update_context保存，不能先转换阶段而丢掉明确的新信息。"
            "只有本轮还明确授权随后结束签到时，才可按原continuation_scope逐字引用该授权并列session.transition到classroom_recall；"
            "否则保存后stop，不推断孩子已准备好，不扩大到生成计划或启动任务。"
        )
    if state == "classroom_recall" and not continuation:
        system += (
            "当前session.state为classroom_recall，正在采集孩子本人今天真实课堂学过的内容。"
            "否定和不确定性优先于当前采集阶段：本轮明确说不要保存、暂不记录或只想聊时，优先尊重，不写课堂记录，不用recall.none制造空回忆。"
            "裸知识名词可以是对采集问句的简答，但若缺明确学科或本人课堂事实，即使刚问过课堂所学，也应先自然澄清，不补造学科或本人课堂事实。"
            "例如只说‘分式。’可以问‘这是今天哪门课学到的内容，还是你想聊这个？’，不能自行补成数学课已学了分式。"
            "上述澄清和不保存选择conversation.respond；不必新建目标时可objective.mode=none，不能用空success_criteria建立new目标。"
            "孩子明确自述本人今天课堂所学，即使没有说记录或保存，也是对课堂采集问题的回答；"
            "utterance_binding.role=answer，quote逐字引用本轮完整原话，不是仅提及知识主题的topic_mention。"
            "例如‘我们今天数学课练习了分式。’是在回答课堂回想，应先用recall.add保存孩子明确说出的学科和内容。"
            "只含一项时用recall.add，arguments.subject和summary只来自本轮明确内容；一次说了多学科或多项内容时用recall.add_batch，"
            "arguments.items逐项保留原话中的学科和内容，不丢项、不改成空回忆，不补造老师、知识点、掌握程度或学习结果。"
            "这类采集是objective.type=execution，不是learning讲课目标。next_step.expected_evidence使用已注册能力的回执ID：单项recall_saved，多项recalls_saved。"
            "objective.success_criteria用自然语言描述可验证的保存结果，例如本轮明确的学科和课堂内容已保存且能在当前孩子记录中查到；"
            "不要求技术回执ID重复出现在自然语言目标条件中，也不能把这些目标条件写成保存已经发生的口头承诺。"
            "after_execution=stop、requires_child_reply=false；spoken_transition只说明将要保存，真实工具结果之前不得声称已记下。"
            "保存后不自动跳阶段，也不生成计划、出题、判掌握或开始讲课，继续由孩子决定补充或结束课堂回想。"
            "孩子明确说课堂回想已说完、进入作业采集时，才选择session.transition到homework_capture；不能由一次保存推断已说完。"
            "必须按本人完整语义判断，不能靠关键词、旧回忆或当前阶段补出事实和授权。"
            "裸知识名词、转述他人、故事、条件假设、询问别人学了什么，以及明确不保存或暂不记录，都不是本人本轮课堂事实保存许可；"
            "用conversation.respond正常回应、尊重不保存或具体澄清，不能因为出现学科或知识词就写入。"
            "孩子不确定或想不起来时不能编造课堂内容，明确想不起可用原recall.none语义。"
            "孩子本轮明确请求讲解、提问或做题时仍按原请求选择学习能力，不因课堂采集阶段强改成保存；明确讲解请求的role仍为question或task_request。"
            "孩子本人自报学校作业时使用原homework.add或homework.add_batch保存作业，不把作业当课堂回忆；不擅自切阶段或生成计划。"
        )
    if state == "homework_capture" and not continuation:
        system += (
            "当前session.state为homework_capture，本轮可能是对当前作业采集问题的回答。"
            "孩子在这个对话中明确给出自己的学科和待做任务时，应先用homework.add保存一项，"
            "多项用homework.add_batch；不要求孩子重复“作业”或“请保存”，也不能只聊天而漏掉保存。"
            "例如回答数学练习要做哪几题、英语练习册几页时，按本轮明确学科和任务保存；"
            "utterance_binding.role=answer，quote逐字引用本轮完整原话，objective.type=execution。"
            "只填写孩子明确提供的字段，不编造时长、页码、截止日期或老师要求；"
            "没有明确学科或任务、仅说一个知识名词时先用conversation.respond具体澄清。"
            "单项expected_evidence用homework_saved，多项用homework_items_saved；保存后stop，不自动生成计划或开始学习。"
            "转述他人、故事、过去、假设、举例以及本轮明确不保存都不是当前孩子的待做作业，不保存、不假定无作业，不能靠关键词执行。"
            "孩子明确说作业说完并要求现在安排计划时选择planning.generate，arguments={}，"
            "objective.type=execution，expected_evidence=[plan_revision]，after_execution=stop。"
            "daily_planning_state来自本机保存事实；若没有已保存作业且孩子也未明确无作业，应先澄清遗漏，不能虚构任务或假称计划已生成。"
            "成功生成计划才会进入plan_review并读回计划；不能用session.transition代替生成计划。"
            "确认计划进入study只适用于真实plan_review阶段；还在homework_capture时不能接受一个不存在的计划。"
            "spoken_transition只说明将执行什么，不能把将要保存、生成或确认说成已经完成。"
        )
    payload = {
        "context": _without_local_only(context),
        "child_utterance": transcript[:1200],
        "available_capabilities": capabilities,
        "output_schema": {
            "utterance_binding": {
                "role": "question|task_request|answer|acknowledgement|correction|greeting|topic_mention|companionship|unknown",
                "quote": "本轮child_utterance中支持判断的逐字原话，不得编造",
            },
            "objective": {
                "mode": "new|continue|pause|complete|cancel|none",
                "type": "learning|execution|habit|companion|null",
                "summary": "string|null",
                "success_criteria": ["string"],
                "phase": "short_string",
            },
            "next_step": {
                "capability": "registered capability name",
                "arguments": {},
                "reason": "short reason",
                "expected_evidence": ["string"],
            },
            "spoken_transition": "给孩子的一至三句自然口语",
            "requires_child_reply": True,
            "after_execution": "stop|replan",
            "continuation_scope": [{"capability": "原请求剩余能力", "arguments": {}, "request_quote": "支持该动作的逐字原话"}],
            "confidence": 0.0,
        },
    }
    if not continuation and state == 'study':
        from .task_start_semantics import PLAN_PROMPT
        system += PLAN_PROMPT
        system += ('conversation_state.pending_saved_mistake_answer为真时，已经保存一份原回答但处理未完成。'
            '明确要求继续处理那份已保存回答时用mistake.dialogue，arguments只含utterance本轮完整原话和pending_answer_control=resume，'
            '明确要求撤回或撤销这份未处理回答时用同一能力、pending_answer_control=withdraw；role=task_request。'
            '孩子在同一句话中明确改口时，用pending_answer_control=replace和replacement_answer；replacement_answer必须是本轮原话中'
            '改成、换成、替换成或更正为之后的逐字新答案，不得猜测、改写或带入历史答案，role=correction。'
            '三种控制均after_execution=stop，不删除原话。询问、转述、假设、否定控制和含糊改口都不执行，保留原话并澄清。')
        system += ('conversation_state.recent_scored_mistake_answer为真且孩子明确纠正刚才已评分的答案时，'
            '使用mistake.dialogue，arguments只含utterance本轮完整原话、scored_answer_control=replace和replacement_answer。'
            'replacement_answer必须是本轮原话中“改成、改为、换成、替换成、更正为、应该是”之后的逐字内容，'
            'role=correction、after_execution=stop。孩子明确要求撤回、撤销或作废刚才已评分的答案且不提供新答案时，'
            'arguments只含utterance和scored_answer_control=withdraw，role=task_request、after_execution=stop。'
            '不得把历史答案、评分或题目带入参数。询问、转述、假设、否定或含糊表达不得执行。')
    if continuation:
        system += (
            "本次触发是已经落库的工具结果，不是孩子又说了一句话。original_child_request仅是原始授权的范围。"
            "根据最新状态推进原请求中仍未完成的部分，禁止重做已完成动作、编造孩子回答/确认或另开无关目标。"
            "objective.mode只能continue或none，保留原目标及成功标准；需要新答案/授权就提出具体问题并停止续行。"
            "如果原请求已经办完，直接简短总结实际结果并stop，不能为了续行凭空增加任务。"
            "除澄清/总结外，下一动作及参数必须完全来自trigger.authorized_actions；不得增改授权范围。"
        )
        payload["trigger"] = {"type": "tool_result", **continuation}
        payload["original_child_request"] = payload.pop("child_utterance")
    return (
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    )


def parse_agent_plan_output(content: str, state: str, registry: CapabilityRegistry) -> AgentPlanDecision:
    value = content.strip()
    if value.startswith("```"):
        value = value.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidAgentPlanError("planner did not return JSON") from error
    if not isinstance(payload, dict):
        raise InvalidAgentPlanError("planner output must be an object")
    objective_value = payload.get("objective")
    step_value = payload.get("next_step")
    if not isinstance(objective_value, dict) or not isinstance(step_value, dict):
        raise InvalidAgentPlanError("planner output is missing objective or next_step")
    mode = str(objective_value.get("mode", "")).strip()
    objective_type = objective_value.get("type")
    if mode not in OBJECTIVE_MODES:
        raise InvalidAgentPlanError("invalid objective mode")
    if objective_type is not None and objective_type not in OBJECTIVE_TYPES:
        raise InvalidAgentPlanError("invalid objective type")
    if mode in {"new", "continue"} and objective_type not in OBJECTIVE_TYPES:
        raise InvalidAgentPlanError("active objective requires a type")
    summary = objective_value.get("summary")
    if summary is not None:
        summary = str(summary).strip()[:300] or None
    criteria_value = objective_value.get("success_criteria") or []
    if not isinstance(criteria_value, list):
        raise InvalidAgentPlanError("success_criteria must be a list")
    criteria = tuple(str(item).strip()[:240] for item in criteria_value if str(item).strip())[:6]
    if mode == "new" and (not summary or not criteria):
        raise InvalidAgentPlanError("new objective requires summary and success criteria")
    capability = registry.require(str(step_value.get("capability", "")).strip(), state,
                                  arguments=step_value.get('arguments') if isinstance(step_value.get('arguments'), dict) else None)
    if capability.name in LOCAL_COMPOSED_CAPABILITIES:
        raise InvalidAgentPlanError("CAPABILITY_REQUIRES_LOCAL_COMPOSITION")
    if objective_type and objective_type not in capability.objective_types:
        raise InvalidAgentPlanError("capability does not match objective type")
    arguments = step_value.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise InvalidAgentPlanError("capability arguments must be an object")
    if set(arguments) & {
        "type", "capability", "execution_location", "agent_runtime", "source", "confidence",
        "local_only", "cloud_transmission_allowed", "child_id", "session_id", "client_action_id",
        "source_turn_id", "tutor_followup_binding",
        "local_learning_context", "local_document_context", "mastery_update_allowed",
    }:
        raise InvalidAgentPlanError("capability arguments contain reserved execution fields")
    for name, choices in CAPABILITY_ENUM_ARGUMENTS.get(capability.name, {}).items():
        if name in arguments and arguments[name] not in choices:
            raise InvalidAgentPlanError(f"unsupported capability argument: {name}")
    for name, lower, upper in (("support_level", 1, 6), ("expected_minutes", 5, 180), ("minutes", 1, 120)):
        if name in arguments:
            number = arguments[name]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or int(number) != number or not lower <= number <= upper:
                raise InvalidAgentPlanError(f"invalid numeric capability argument: {name}")
    missing = [name for name in capability.required_arguments if not str(arguments.get(name, "")).strip()]
    if missing:
        raise InvalidAgentPlanError(f"missing capability arguments: {', '.join(missing)}")
    if (capability.name == "session.transition"
            and arguments.get("target") not in SESSION_TRANSITION_TARGETS.get(state, ())):
        raise InvalidAgentPlanError("SESSION_TRANSITION_TARGET_NOT_AVAILABLE")
    if capability.name == 'task.start' and 'start_selector' in arguments:
        from .task_start_semantics import validate
        utterance = payload.get('utterance_binding')
        if set(arguments) != {'start_selector'} or not isinstance(utterance, dict) or utterance.get('role') != 'task_request':
            raise InvalidAgentPlanError('TASK_START_PLANNER_SELECTOR_SHAPE_INVALID')
        try:
            validate(arguments['start_selector'], utterance.get('quote', ''))
        except (ValueError, TypeError) as error:
            reason = str(error)
            if reason not in {
                'CONTROL_START_SELECTOR_INVALID', 'CONTROL_START_NOT_REQUESTED',
                'CONTROL_START_MULTIPLE_TARGETS', 'CONTROL_START_NEGATION_SCOPE_UNCLEAR',
                'CONTROL_START_SELECTOR_CONFLICT', 'CONTROL_START_TARGET_REQUIRED',
            }:
                reason = 'CONTROL_START_SELECTOR_INVALID'
            raise InvalidAgentPlanError(f'TASK_START_PLANNER_{reason}') from None
    if capability.name == "companion.activity_feedback":
        _validate_activity_feedback_arguments(arguments)
    if capability.name.startswith(('writing.','native_writing.')):
        if (set(arguments) != {'request_quote'} or not isinstance(arguments['request_quote'], str)
                or payload.get('after_execution', 'stop') != 'stop'):
            raise InvalidAgentPlanError('ANSWER_WRITING_LOCAL_BINDING_REQUIRED')
    if capability.name == 'pet.listening_control':
        if (set(arguments) != {'command', 'request_quote'} or not isinstance(arguments.get('request_quote'), str)
                or not arguments['request_quote'].strip() or payload.get('after_execution', 'stop') != 'stop'):
            raise InvalidAgentPlanError('PET_LISTENING_BOUNDED_COMMAND_REQUIRED')
    if capability.name.startswith("learning_inputs."):
        allowed = ({"operation", "remaining_minutes", "remaining_scope_quote", "request_quote"} if capability.name == 'learning_inputs.review_reclaimed_scope' else
                   {"decision", "request_quote", "not_before"} if capability.name == "learning_inputs.review_unplaced" else
                   {"kind", "operation", "target_quote", "request_quote", "changes", "change_quotes"}
                   if capability.name.endswith("propose_revision") else {"selection_quote"}
                   if capability.name.endswith("select_target") else {"confirmation_quote"})
        if set(arguments) - allowed or payload.get("after_execution", "stop") != "stop":
            raise InvalidAgentPlanError("input revision requires a bounded versioned dialogue")
        if capability.name == "learning_inputs.review_unplaced":
            if not isinstance(arguments.get("request_quote"), str) or not arguments['request_quote'].strip():
                raise InvalidAgentPlanError("CARRYOVER_QUOTE_REQUIRED")
            if arguments.get('decision') == 'keep':
                try:
                    datetime.strptime(arguments['not_before'], '%Y-%m-%d')
                except (KeyError, TypeError, ValueError):
                    raise InvalidAgentPlanError("CARRYOVER_KEEP_DATE_REQUIRED") from None
            elif arguments.get('not_before') is not None:
                raise InvalidAgentPlanError("CARRYOVER_UNEXPECTED_KEEP_DATE")
        if capability.name == 'learning_inputs.review_reclaimed_scope':
            if arguments['operation'] == 'confirm_scope' and (type(arguments.get('remaining_minutes')) is not int or arguments['remaining_minutes'] < 1
                    or not isinstance(arguments.get('remaining_scope_quote'), str) or not arguments['remaining_scope_quote'].strip()):
                raise InvalidAgentPlanError('CARRYOVER_REMAINING_SCOPE_REQUIRED')
            if arguments['operation'] == 'withdraw_scope' and any(arguments.get(key) is not None for key in ('remaining_minutes', 'remaining_scope_quote')):
                raise InvalidAgentPlanError('CARRYOVER_WITHDRAW_HAS_NO_REPLACEMENT')
    if capability.name == "learning.request_response":
        if any(not isinstance(arguments[name], str) or not arguments[name].strip()
               or len(arguments[name]) > limit for name, limit in (("prompt", 500), ("subject", 40))):
            raise InvalidAgentPlanError("invalid learning response prompt")
    if capability.name == "voice.configure_wake_word":
        if set(arguments) - {"candidate"}:
            raise InvalidAgentPlanError("invalid wake word arguments")
        if "candidate" in arguments:
            candidate = arguments["candidate"]
            if not isinstance(candidate, str) or not 2 <= len(candidate.strip()) <= 12 or candidate != candidate.strip():
                raise InvalidAgentPlanError("invalid wake word candidate")
    if capability.name in {"device.connect", "device.status"} and set(arguments) != {"device"}:
        raise InvalidAgentPlanError("invalid device arguments")
    if capability.name == "document.set_occlusion_reminders":
        if set(arguments) != {"enabled"} or type(arguments["enabled"]) is not bool:
            raise InvalidAgentPlanError("invalid document guidance setting")
    if capability.name == "task.complete":
        for field, label in (("confirmed_checks", "confirmed"), ("revoked_checks", "revoked")):
            checks = arguments.get(field, [])
            if not isinstance(checks, list) or len(checks) > 20 or any(not isinstance(item, str) for item in checks):
                raise InvalidAgentPlanError(f"invalid {label} checks")
        quote = arguments.get("confirmation_quote", "")
        if not isinstance(quote, str) or len(quote) > 2000:
            raise InvalidAgentPlanError("invalid completion confirmation quote")
    if capability.name in {"recall.add_batch", "homework.add_batch"}:
        items = arguments.get("items")
        if not isinstance(items, list) or not 2 <= len(items) <= 12:
            raise InvalidAgentPlanError("batch capability requires 2 to 12 items")
        for item in items:
            if not isinstance(item, dict):
                raise InvalidAgentPlanError("batch capability item must be an object")
            required = ("subject", "summary") if capability.name == "recall.add_batch" else ("subject", "title")
            if any(not str(item.get(name, "")).strip() for name in required):
                raise InvalidAgentPlanError("batch capability item is missing required fields")
            if capability.name == "homework.add_batch":
                try:
                    minutes = int(item.get("expected_minutes", 20))
                except (TypeError, ValueError) as error:
                    raise InvalidAgentPlanError("homework duration must be an integer") from error
                if not 5 <= minutes <= 180:
                    raise InvalidAgentPlanError("homework duration is outside the supported range")
    spoken = str(payload.get("spoken_transition", "")).strip()
    if not spoken or len(spoken) > 500:
        raise InvalidAgentPlanError("spoken transition is missing or too long")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise InvalidAgentPlanError("invalid planner confidence")
    expected_value = step_value.get("expected_evidence") or capability.expected_evidence
    if not isinstance(expected_value, (list, tuple)):
        raise InvalidAgentPlanError("expected evidence must be a list")
    expected = tuple(str(item).strip()[:120] for item in expected_value if str(item).strip())[:8]
    after_execution = payload.get("after_execution", "stop")
    if after_execution not in {"stop", "replan"}:
        raise InvalidAgentPlanError("invalid after_execution policy")
    if capability.name in {"conversation.respond", "learning.request_response"}:
        after_execution = "stop"
    scope = payload.get("continuation_scope", [])
    if not isinstance(scope, list) or len(scope) > 4:
        raise InvalidAgentPlanError("invalid continuation scope")
    for item in scope:
        if (not isinstance(item, dict) or set(item) != {"capability", "arguments", "request_quote"}
                or item["capability"] not in RESULT_CONTINUATION_CAPABILITIES
                or not isinstance(item["arguments"], dict)
                or not isinstance(item["request_quote"], str) or not 2 <= len(item["request_quote"].strip()) <= 1200):
            raise InvalidAgentPlanError("invalid continuation authorization")
    return AgentPlanDecision(
        objective=ObjectiveDraft(
            mode=mode,
            objective_type=objective_type,
            summary=summary,
            success_criteria=criteria,
            phase=str(objective_value.get("phase", "engage")).strip()[:80] or "engage",
        ),
        step=PlannedStep(
            capability=capability,
            arguments={str(key): item for key, item in arguments.items()},
            reason=str(step_value.get("reason", "")).strip()[:500],
            expected_evidence=expected,
        ),
        spoken_transition=spoken,
        requires_child_reply=capability.name in {"learning.request_response", "diagnostic.start", "diagnostic.replay", "pet_vocabulary.start"}
                             or bool(payload.get("requires_child_reply", False)),
        confidence=float(confidence),
        after_execution=after_execution,
        continuation_scope=tuple(scope),
    )


def task_check_reply_arguments(arguments: dict[str, Any], transcript: str,
                               orchestrator_version: str, pending: dict[str, Any] | None) -> dict[str, Any]:
    """Bind a whole-answer legacy reply to its saved question, never to model claims."""
    if (orchestrator_version != "agent_runtime_deterministic_v1" or not pending
            or set(arguments) & {"confirmed_checks", "revoked_checks", "confirmation_quote"}):
        return arguments
    compact = re.sub(r"[\s，,。.!！]", "", transcript)
    # This is a bounded offline compatibility response, not a substitute for
    # cloud semantic parsing. Questions, negation and extra clauses fail closed.
    if not re.fullmatch(r"(?:刚才问的|你刚才问的|你问的)(?:每一项|这些项|这几项)(?:我)?都(?:完成并检查了|做完并检查了|做完了并且检查过了)", compact):
        return arguments
    missing = pending.get("missing_checks", pending.get("required_checks", []))
    return {**arguments, "confirmed_checks": list(missing), "revoked_checks": [],
            "confirmation_quote": transcript}


def _requests_learning_completion(decision: AgentPlanDecision, active: Any) -> bool:
    objective_type = (
        active["objective_type"] if active and decision.objective.mode in {"continue", "complete"}
        else decision.objective.objective_type
    )
    return objective_type == "learning" and (
        decision.objective.mode == "complete"
        or (decision.objective.mode in {"new", "continue"} and decision.objective.phase in {"complete", "completed"})
    )


def _validate_activity_feedback_arguments(arguments, transcript=None):
    from .companion_activity_voice import ActivityVoiceArguments
    try:
        parsed = ActivityVoiceArguments.model_validate(arguments)
    except ValueError as error:
        raise InvalidAgentPlanError("ACTIVITY_FEEDBACK_ARGUMENTS_INVALID") from error
    if parsed.operation != "feedback" and set(arguments) != {"operation", "response_quote"}:
        raise InvalidAgentPlanError("ACTIVITY_FEEDBACK_UNRELATED_CLASSIFICATION")
    if transcript is not None and (parsed.response_quote != transcript or not transcript.strip()):
        raise InvalidAgentPlanError("ACTIVITY_FEEDBACK_QUOTE_MISMATCH")


class AgentRuntimeStore:
    def __init__(self, database: Database, *, page_source_store=None, speaking_candidate_assessor=None):
        self.database = database
        self.page_source_store = page_source_store
        self.speaking_candidate_assessor = speaking_candidate_assessor
        self.continuations = ContinuationStore(database)
        self.child_state_consumption = ChildStateConsumptionService(database)
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS companion_activity (
                  activity_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  activity_type TEXT NOT NULL CHECK(activity_type IN (
                    'interactive_story','word_chain','riddle','movement','tidy_challenge'
                  )),
                  topic TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active','completed','cancelled')),
                  turn_count INTEGER NOT NULL DEFAULT 0,
                  max_turns INTEGER NOT NULL,
                  duration_minutes INTEGER NOT NULL,
                  paused_plan_item_id TEXT REFERENCES plan_item(plan_item_id),
                  started_at TEXT NOT NULL,
                  ends_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  ended_at TEXT,
                  end_reason TEXT,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_activity_active_session
                  ON companion_activity(session_id) WHERE status = 'active';
                CREATE TABLE IF NOT EXISTS agent_proactive_prompt (
                  prompt_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  source_type TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  message_text TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('delivered', 'expired')),
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  UNIQUE(session_id, source_type, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_proactive_session_created
                  ON agent_proactive_prompt(session_id, created_at DESC);
                """
            )
            prompt_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(agent_proactive_prompt)"
                ).fetchall()
            }
            for column, definition in (
                ("acknowledged_at", "TEXT"),
                ("delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("last_attempt_at", "TEXT"),
            ):
                if column not in prompt_columns:
                    connection.execute(
                        f"ALTER TABLE agent_proactive_prompt ADD COLUMN {column} {definition}"
                    )
            connection.commit()

    def assess_speaking_model_candidate(self, child_id, session_id, *, media_id,
                                       assessment_id, client_action_id, should_stop=lambda:False, job_claim=None):
        """Local uncalibrated background result; never a planner scoring action.

        The original postcommit learning hook registers a durable job. Only its
        maintenance claim calls this method, outside any DB/voice transaction.
        The host supplies the concrete source-reviewed assessor and same Database.
        """
        from .pet_multipa_offline import SpeakingOfflineCandidateAssessor
        assessor = self.speaking_candidate_assessor
        if (type(assessor) is not SpeakingOfflineCandidateAssessor
                or assessor.database is not self.database):
            raise ValueError('PET_MULTIPA_OFFLINE_REVIEWED_DEPLOYMENT_NOT_AVAILABLE')
        return assessor.assess(child_id, session_id, media_id=media_id,
                              assessment_id=assessment_id, client_action_id=client_action_id,
                              should_stop=should_stop,job_claim=job_claim)

    def _bind_proactive_prompt_state(
        self, child_id: str, prompt: dict[str, Any], *, recovery: bool = False,
    ) -> dict[str, Any]:
        binding = self.child_state_consumption.bind_current(
            child_id=child_id,
            consumer_type="proactive_prompt",
            source_type="agent_proactive_prompt",
            source_id=prompt["prompt_id"],
            capture_phase="legacy_recovery" if recovery else "result_state",
            purpose="validate_and_deliver_evidence_based_proactive_prompt",
        )
        return {**prompt, "state_consumption": binding}

    def _ensure_companion_time_limits(self, session_id: str | None = None) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        ended = []
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM companion_activity WHERE status = 'active'"
                + (" AND session_id = ?" if session_id is not None else ""),
                (session_id,) if session_id is not None else (),
            ).fetchall()
            for row in rows:
                if _aware_datetime(row["ends_at"]) > now:
                    continue
                timestamp = now.isoformat()
                connection.execute(
                    """UPDATE companion_activity SET status = 'completed', ended_at = ?,
                       updated_at = ?, end_reason = 'time_limit'
                       WHERE activity_id = ? AND status = 'active'""",
                    (timestamp, timestamp, row["activity_id"]),
                )
                ended.append({
                    **dict(row), "status": "completed", "ended_at": timestamp,
                    "updated_at": timestamp, "end_reason": "time_limit", "natural_end": True,
                })
            connection.commit()
        return ended

    def recover_interrupted_executions(self) -> int:
        """Make an interrupted capability explicitly retryable after restart.

        Capability side effects use stable idempotency keys, so a later child
        retry can safely replay the same logical action.  Leaving rows in
        ``running`` would instead strand the objective forever.
        """
        self._ensure_companion_time_limits()
        self.recover_learning_outcomes()
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT ast.step_id, ap.plan_id, ap.agent_turn_id
                FROM agent_step ast JOIN agent_plan ap ON ap.plan_id = ast.plan_id
                WHERE ast.status = 'running' OR ap.status = 'running'
                """
            ).fetchall()
            if not rows:
                return 0
            step_ids = {row["step_id"] for row in rows}
            plan_ids = {row["plan_id"] for row in rows}
            turn_ids = {row["agent_turn_id"] for row in rows}
            connection.executemany(
                "UPDATE agent_step SET status = 'failed', error_code = 'APP_RESTARTED', updated_at = ? WHERE step_id = ?",
                ((now, step_id) for step_id in step_ids),
            )
            connection.executemany(
                "UPDATE agent_plan SET status = 'failed', updated_at = ? WHERE plan_id = ?",
                ((now, plan_id) for plan_id in plan_ids),
            )
            connection.executemany(
                """
                UPDATE agent_turn SET tool_status = 'failed', tool_error_code = 'APP_RESTARTED',
                  completed_at = ? WHERE agent_turn_id = ? AND tool_status = 'pending'
                """,
                ((now, turn_id) for turn_id in turn_ids),
            )
            connection.commit()
        return len(step_ids)

    def recover_learning_outcomes(self) -> dict[str, Any]:
        """Startup hook: no model calls, no positive mastery transitions."""
        with self.database.connect() as connection:
            children = [row[0] for row in connection.execute("SELECT DISTINCT child_id FROM agent_objective WHERE objective_type='learning'")]
            ready = all(connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
                        for name in ("diagnostic_prompt", "rubric_assessment", "learning_evaluation_execution"))
            answers = connection.execute("""SELECT DISTINCT p.origin_session_id,a.evidence_id FROM diagnostic_prompt p
                JOIN diagnostic_attempt a ON a.attempt_id=p.attempt_id AND a.child_id=p.child_id AND a.session_id=p.origin_session_id
                JOIN agent_plan plan ON plan.child_id=p.child_id AND plan.session_id=p.origin_session_id
                  AND (json_extract(CASE WHEN json_valid(plan.plan_json) THEN plan.plan_json ELSE '{}' END,
                                    '$.local_learning_binding.prompt_id')=p.prompt_id
                    OR (json_extract(CASE WHEN json_valid(plan.context_manifest_json)
                                         THEN plan.context_manifest_json ELSE '{}' END,
                                     '$.goal_prompt_binding.prompt_id')=p.prompt_id
                        AND json_extract(CASE WHEN json_valid(p.item_json) THEN p.item_json ELSE '{}' END,
                                         '$.answer_type') IN ('writing','open_response')))
                WHERE p.status='answered'""").fetchall() if ready else []
        for child_id in children:
            self.reconcile_learning_state(child_id)
        results = [self.finalize_diagnostic_evaluation(row[0], row[1]) for row in answers]
        return {"local_only": True, "cloud_transmission_allowed": False, "results": results}

    @staticmethod
    def _bind_learning_diagnostic(connection, turn_id: str, prompt_id: str, item_version: str) -> bool:
        row = connection.execute("""SELECT p.plan_id,p.plan_json,p.child_id,p.session_id,t.created_at
            FROM agent_plan p JOIN agent_step s USING(plan_id) JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
            WHERE p.agent_turn_id=? AND s.capability_name='diagnostic.start'""", (turn_id,)).fetchone()
        if not row:
            return False
        plan = json.loads(row["plan_json"])
        binding = plan.get("local_learning_binding")
        if not isinstance(binding, dict):
            return False
        prompt = connection.execute("""SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND item_version=?
            AND child_id=? AND origin_session_id=? AND status IN ('waiting_answer','answer_saved','answered')
            AND owner_kind='diagnostic' AND json_extract(request_json,'$.exam_slot') IS NULL
            AND julianday(created_at)>=julianday(?)""",
            (prompt_id, item_version, row["child_id"], row["session_id"], row["created_at"])).fetchone()
        if not prompt or binding.get("prompt_id") not in {None, prompt_id}:
            return False
        binding.update(prompt_id=prompt_id, item_version=item_version, diagnostic_item_id=prompt["diagnostic_item_id"])
        connection.execute("UPDATE agent_plan SET plan_json=? WHERE plan_id=?", (json.dumps(plan, ensure_ascii=False), row["plan_id"]))
        return True

    def bind_diagnostic_prompt(self, turn_id: str, prompt_id: str, item_version: str) -> bool:
        """Optional server hook when a bridge completes after the answer arrived."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = self._bind_learning_diagnostic(connection, turn_id, prompt_id, item_version)
            connection.commit()
            return result

    @staticmethod
    def bind_writing_learning_goal(c, turn_id, confirmation):
        """A real read-original request anchors criteria before any answer exists."""
        from .writing_ocr_authorization import _confirmation
        from .writing_response_origin import _prompt
        turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
        packet, _, _ = _confirmation(c, turn['session_id'], confirmation['confirmation_id'], confirmation['binding_version'], live=True)
        prompt, item = _prompt(c, turn['session_id'], packet['prompt_id'], packet['item_version'], live=True)
        origin = c.execute('''SELECT p.plan_json,o.* FROM agent_plan p JOIN agent_objective o USING(objective_id)
            WHERE p.child_id=? AND p.session_id=? AND o.objective_type='learning' AND o.status='active'
            AND json_extract(p.plan_json,'$.local_learning_binding.prompt_id')=?
            AND json_extract(p.plan_json,'$.local_learning_binding.item_version')=? ORDER BY p.rowid LIMIT 1''',
            (turn['child_id'], turn['session_id'], packet['prompt_id'], packet['item_version'])).fetchone()
        if origin:
            bound = json.loads(origin['plan_json'])['local_learning_binding']
        else:
            active = c.execute("SELECT * FROM agent_objective WHERE child_id=? AND status='active'", (turn['child_id'],)).fetchone()
            if active:
                from .school_review_execution import reading_execution_owner
                if not reading_execution_owner(c, turn, prompt, active):
                    return None
                c.execute("UPDATE agent_objective SET status='paused',updated_at=? WHERE objective_id=? AND status='active'",
                    (datetime.now(timezone.utc).isoformat(),active['objective_id']))
            criteria = json.loads(item['expected_answer_json'])['task_points']
            oid, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
            c.execute('''INSERT INTO agent_objective(objective_id,child_id,session_id,objective_type,summary,
                success_criteria_json,phase,status,created_at,updated_at,last_agent_turn_id)
                VALUES(?,?,?,'learning',?,?,'check','active',?,?,?)''',
                (oid, turn['child_id'], turn['session_id'], '按本题要求核对自己的书面作答', json.dumps(criteria, ensure_ascii=False), now, now, turn_id))
            bound = dict(version='writing_objective_binding_v1', objective_id=oid, child_id=turn['child_id'],
                session_id=turn['session_id'], prompt_id=packet['prompt_id'], item_version=packet['item_version'],
                diagnostic_item_id=prompt['diagnostic_item_id'], phase='check', success_criteria=criteria,
                created_at=now, local_only=True, cloud_transmission_allowed=False)
        c.execute("UPDATE agent_plan SET objective_id=?,plan_json=json_set(plan_json,'$.local_learning_binding',json(?)) WHERE agent_turn_id=?",
            (bound['objective_id'], json.dumps(bound, ensure_ascii=False), turn_id))
        return bound

    @staticmethod
    def bind_writing_followup_prompt(c, turn_id, output):
        prompt = output.get('prompt') if isinstance(output, dict) else None
        if not isinstance(prompt, dict) or not c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_followup'").fetchone():
            return False
        row = c.execute('''SELECT t.child_id,t.session_id,t.action_json,p.objective_id,p.context_manifest_json,s.idempotency_key
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND s.step_index=0''', (turn_id,)).fetchone()
        if not row:
            return False
        packet = json.loads(row['action_json']).get('writing_followup_binding') or {}
        target = packet.get('action') or {}
        origin = json.loads(row['context_manifest_json']).get('writing_followup_origin') or {}
        actual = c.execute('''SELECT f.*,d.item_version,d.status FROM writing_followup f
            JOIN diagnostic_prompt d USING(prompt_id) WHERE f.prompt_id=? AND f.child_id=? AND f.session_id=?
            AND d.child_id=f.child_id AND d.origin_session_id=f.session_id''',
            (prompt.get('prompt_id'),row['child_id'],row['session_id'])).fetchone()
        if (not actual or actual['status'] != 'waiting_answer' or actual['item_version'] != prompt.get('item_version')
                or actual['parent_prompt_id'] != target.get('parent_prompt_id') or actual['assessment_id'] != target.get('assessment_id')
                or actual['kind'] != target.get('kind')
                or json.loads(actual['binding_json']).get('binding_version') != packet.get('binding_version')):
            return False
        objective = c.execute('''SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?
            AND objective_type='learning' AND status='active' ''',
            (row['objective_id'],row['child_id'],row['session_id'])).fetchone()
        if not objective:
            raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_GOAL_CHANGED')
        bound = {'objective_id':objective['objective_id'],'child_id':row['child_id'],'session_id':row['session_id'],
            'prompt_id':actual['prompt_id'],'item_version':actual['item_version'],'diagnostic_item_id':target['diagnostic_item_id'],
            'phase':objective['phase'],'success_criteria':json.loads(objective['success_criteria_json']),
            'created_at':actual['created_at'],'parent_prompt_id':actual['parent_prompt_id'],
            'assessment_id':actual['assessment_id'],'source_objective_id':origin.get('objective_id'),
            'required_hint_level':target['required_hint_level'],
            'local_only':True,'cloud_transmission_allowed':False}
        c.execute("UPDATE agent_plan SET objective_id=?,plan_json=json_set(plan_json,'$.local_learning_binding',json(?)) WHERE agent_turn_id=?",
                  (objective['objective_id'],json.dumps(bound,ensure_ascii=False),turn_id))
        return True

    @staticmethod
    def bind_writing_revision_prompt(c, turn_id, output):
        prompt = output.get('prompt') if isinstance(output, dict) else None
        if not isinstance(prompt, dict) or not c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_reference_revision'").fetchone():
            return False
        row = c.execute('''SELECT r.*,p.item_version,a.objective_id,ref.context_manifest_json
            FROM writing_reference_revision r JOIN diagnostic_prompt p ON p.prompt_id=r.prompt_id
            JOIN agent_plan a ON a.agent_turn_id=r.request_turn_id
            JOIN agent_plan ref ON ref.agent_turn_id=r.reference_turn_id
            WHERE r.request_turn_id=? AND r.prompt_id=? AND p.status='waiting_answer'
            AND p.child_id=r.child_id AND p.origin_session_id=r.session_id
            AND a.child_id=r.child_id AND a.session_id=r.session_id''', (turn_id, prompt.get('prompt_id'))).fetchone()
        if not row or row['item_version'] != prompt.get('item_version'):
            return False
        bound = json.loads(row['context_manifest_json']).get('original_learning_binding')
        objective = c.execute("SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND status='active' AND objective_type='learning'",
            ((bound or {}).get('objective_id'), row['child_id'])).fetchone()
        recovery = isinstance(bound,dict) and bound.get('recovery_only') is True
        if recovery:
            from .scheduled_writing_goal import activate_recovery_revision
            from .local_supply import LocalSupplyError
            try:
                objective = activate_recovery_revision(c,row,turn_id,bound)
            except LocalSupplyError:
                raise
            except ValueError as error:
                raise InvalidAgentPlanError('ANSWER_WRITING_RECOVERY_AUTHORIZATION_CHANGED') from error
        if objective and (recovery or row['objective_id'] == objective['objective_id']):
            bound = {**bound, 'prompt_id': row['prompt_id'], 'item_version': row['item_version'],
                'parent_prompt_id': row['parent_prompt_id'], 'reference_turn_id': row['reference_turn_id'],
                'required_hint_level': 1, 'phase': objective['phase']}
            c.execute("UPDATE agent_plan SET plan_json=json_set(plan_json,'$.local_learning_binding',json(?)) WHERE agent_turn_id=?",
                (json.dumps(bound, ensure_ascii=False), turn_id))
        return True

    def finalize_diagnostic_evaluation(self, session_id: str, evidence_id: str) -> dict[str, Any]:
        """Post-commit hook: bind the saved answer to its original local goal."""
        from .diagnostic_prompt import DiagnosticPromptService
        from .learning_outcome_evaluator import LearningOutcomeEvaluator, InvalidLearningEvaluationError
        from .learning_rubric import RubricEvidenceService
        base = {"local_only": True, "cloud_transmission_allowed": False, "evidence_id": evidence_id}
        with self.database.connect() as connection:
            session = connection.execute("SELECT * FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
            if not session:
                raise KeyError(session_id)
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
                return {**base, "status": "blocked", "reason": "diagnostic_prompt_binding_missing"}
            prompt = connection.execute("""SELECT p.* FROM diagnostic_prompt p JOIN diagnostic_attempt a ON a.attempt_id=p.attempt_id
                JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id AND e.session_id=a.session_id
                WHERE e.evidence_id=? AND e.child_id=? AND e.session_id=? AND p.child_id=e.child_id
                  AND p.origin_session_id=e.session_id AND p.diagnostic_item_id=a.diagnostic_item_id""",
                (evidence_id, session["child_id"], session_id)).fetchone()
            if not prompt:
                raise InvalidAgentPlanError("LOCAL_DIAGNOSTIC_RECEIPT_NOT_OWNED_OR_BOUND")
        prompts = DiagnosticPromptService(self.database)
        current = prompts.read(session_id, prompt_id=prompt["prompt_id"])
        if current["status"] != "answered" or (current.get("attempt") or {}).get("evidence_id") != evidence_id:
            return {**base, "status": "blocked", "reason": "diagnostic_source_not_current"}
        prepared = RubricEvidenceService(self.database).diagnostic_candidate_input(session["child_id"], evidence_id)
        writing = json.loads(prompt['item_json']).get('answer_type') in {'writing','open_response'}
        school_oral = (json.loads(prompt['item_json']).get('answer_type')=='open_response'
            and current['prompt']['item'].get('response_mode')=='spoken_explanation')
        oral = json.loads(prompt['item_json']).get('answer_type') in {'speaking','read_aloud'}
        from .rubric_calibration_transaction import calibrated_write
        with calibrated_write(self.database, session['child_id']) as connection:
            live_session = DiagnosticPromptService._session(connection, session_id)
            live_prompt = prompts._validate(connection, live_session,
                prompts._owned(connection, live_session, prompt["prompt_id"]))
            if live_prompt["status"] != "answered":
                connection.commit()
                return {**base, "status": "blocked", "reason": "diagnostic_source_not_current"}
            origin = connection.execute("""SELECT p.* FROM agent_plan p JOIN agent_step s USING(plan_id)
                WHERE p.child_id=? AND p.session_id=? AND (s.capability_name IN ('diagnostic.start','writing.read_original','writing.revise_original','writing.independent_check','oral.retry','oral.independent_check')
                    OR (s.capability_name='learning.review_revisit' AND CASE WHEN json_valid(p.context_manifest_json)
                        THEN json_type(p.context_manifest_json,'$.document_open_check') END='object')
                    OR (s.capability_name='task.start' AND CASE WHEN json_valid(p.context_manifest_json)
                        THEN json_type(p.context_manifest_json,'$.daily_writing_binding') END='object')
                    OR (s.capability_name='task.start' AND CASE WHEN json_valid(p.context_manifest_json)
                        THEN json_type(p.context_manifest_json,'$.scheduled_writing_goal') END='object')
                    OR (s.capability_name='task.resume' AND CASE WHEN json_valid(p.context_manifest_json)
                        THEN json_type(p.context_manifest_json,'$.supply_resume_receipt') END='object'
                        AND json_type(p.context_manifest_json,'$.scheduled_writing_goal')='object'))
                  AND CASE WHEN json_valid(p.plan_json)
                      THEN json_extract(p.plan_json,'$.local_learning_binding.prompt_id') END=?
                  AND CASE WHEN json_valid(p.plan_json)
                      THEN json_extract(p.plan_json,'$.local_learning_binding.item_version') END=? ORDER BY p.rowid LIMIT 1""",
                (session["child_id"], session_id, prompt["prompt_id"], prompt["item_version"])).fetchone()
            if not origin:
                if writing:
                    from .scheduled_writing_goal import recover_original
                    try:
                        recovery = recover_original(self, connection, live_prompt)
                    except ValueError as error:
                        connection.rollback()
                        return {**base, 'status':'blocked', 'reason':str(error)}
                    if recovery:
                        connection.commit()
                        return {**base, **recovery}
                return {**base, "status": "blocked", "reason": "original_learning_objective_missing"}
            if json.loads(origin['context_manifest_json']).get('document_open_check'):
                from .document_open_check import validate_origin
                try:
                    validate_origin(connection, origin, live_prompt)
                except ValueError as error:
                    connection.rollback()
                    return {**base, 'status':'blocked', 'reason':str(error)}
            if json.loads(origin['context_manifest_json']).get('scheduled_writing_goal'):
                from .scheduled_writing_goal import validate_origin
                try:
                    validate_origin(connection, origin, live_prompt)
                except ValueError as error:
                    connection.rollback()
                    return {**base, 'status':'blocked', 'reason':str(error)}
            binding = json.loads(origin["plan_json"]).get("local_learning_binding")
            if (not isinstance(binding, dict)
                    or any(not isinstance(binding.get(k), str) or not binding[k].strip()
                           for k in ('objective_id','phase','child_id','session_id','prompt_id','item_version'))
                    or not isinstance(binding.get('success_criteria'), list) or not binding['success_criteria']
                    or any(not isinstance(v, str) or not v.strip() for v in binding['success_criteria'])
                    or binding['child_id'] != session['child_id'] or binding['session_id'] != session_id
                    or binding['prompt_id'] != prompt['prompt_id'] or binding['item_version'] != prompt['item_version']):
                return {**base, 'status': 'blocked', 'reason': 'original_learning_binding_invalid'}
            objective = connection.execute("SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND objective_type='learning'",
                (binding["objective_id"], session["child_id"])).fetchone()
            key = f"{'formative-evaluation' if writing else 'local-evaluation'}:{binding['objective_id']}:{evidence_id}"
            existing = connection.execute("SELECT agent_turn_id FROM agent_turn WHERE client_action_id=? AND child_id=? AND session_id=?",
                (key, session["child_id"], session_id)).fetchone()
            completed = existing and connection.execute("""SELECT 1 FROM learning_evaluation_execution e
                JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id)
                WHERE p.agent_turn_id=? AND e.state='completed'""", (existing[0],)).fetchone()
            if not completed:
                other_active = connection.execute("SELECT 1 FROM agent_objective WHERE child_id=? AND status='active' AND objective_id!=?",
                    (session["child_id"], binding["objective_id"])).fetchone()
                if (not objective or objective["status"] != "active" or live_session["status"] != "active"
                        or live_session["child_status"] != "active" or other_active
                        or objective["phase"] != binding["phase"]
                        or json.loads(objective["success_criteria_json"]) != binding["success_criteria"]
                        or _aware_datetime(current["attempt"]["created_at"]) < _aware_datetime(objective["created_at"])):
                    return {**base, "status": "blocked", "reason": "original_learning_objective_changed"}
            if existing:
                turn_id = existing[0]
            else:
                now, turn_id = datetime.now(timezone.utc).isoformat(), str(uuid4())
                action = {"type": "none", "source": "local_evaluation", "original_objective_id": objective["objective_id"],
                          "source_prompt_id": prompt["prompt_id"], **base}
                connection.execute("""INSERT INTO agent_turn
                    (agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,
                     orchestrator_version,created_at,client_action_id)
                    VALUES (?,?,?,'','local_evaluation','',?,'pending',?,?,?)""",
                    (turn_id, session["child_id"], session_id, json.dumps(action), PLANNER_VERSION, now, key))
                capability = CapabilityRegistry().require("learning.evaluate_response", "study")
                decision = AgentPlanDecision(ObjectiveDraft("none", None, None, (), objective["phase"]),
                    PlannedStep(capability, prepared["arguments"], "Local diagnostic receipt review", capability.expected_evidence),
                    "", False, 0)
                self.persist_plan(connection, child_id=session["child_id"], session_id=session_id, agent_turn_id=turn_id,
                    client_action_id=key, decision=decision,
                    context_manifest={**base, "original_learning_binding": binding,
                        "execution_method": "formative_evaluation" if writing else "prepare_local_evaluation"})
            if writing:
                from .formative_evaluation_projection import build_formative_projection
                projection = build_formative_projection(connection, child_id=session['child_id'], session_id=session_id,
                    prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], objective_id=binding['objective_id'])
                reference_turn = None
                if not completed and projection['status'] != 'ready_for_runtime_checks':
                    try:
                        if school_oral:
                            from .school_oral_reference import prepare
                            reference_turn = prepare(self,connection,session_id,prompt['prompt_id'],prompt['item_version'],binding,key+':oral-reference')
                        else:
                            reference_turn = self.prepare_writing_reference_feedback(connection, session_id, prompt['prompt_id'],
                                prompt['item_version'], binding, key + ':reference')
                    except ValueError as error:
                        if school_oral:
                            connection.rollback()
                            return {**base,'status':'blocked','reason':str(error)}
                        if not str(error).startswith(('WRITING_', 'ANSWER_WRITING_REFERENCE_')):
                            raise
                        # The answer is already saved; an invalid original must
                        # neither create a feedback plan nor break startup recovery.
                        connection.rollback()
                        return {**base, 'status': 'blocked', 'reason': str(error)}
                connection.commit()
                return {**base, 'status': 'feedback_ready' if completed else 'feedback_required'
                            if projection['status'] == 'ready_for_runtime_checks' else 'local_feedback_required',
                        'reason': projection.get('reason'), 'turn_id': turn_id, 'feedback_turn_id': turn_id,
                        'local_feedback_turn_id': reference_turn,
                        'objective_id': binding['objective_id']}
            try:
                result = LearningOutcomeEvaluator(_RuntimeTransactionDatabase(connection), None, None, None,
                    initialize=False).prepare_local_evaluation(turn_id)
            except InvalidLearningEvaluationError as error:
                connection.rollback()
                return {**base, "status": "blocked", "reason": str(error)}
            connection.execute("UPDATE agent_turn SET tool_status='completed',completed_at=? WHERE agent_turn_id=? AND tool_status='pending'",
                (datetime.now(timezone.utc).isoformat(), turn_id))
            # Only this original newly completed evaluation registers work.
            # Existing completed replay/recovery reads an intent, never backfills.
            from .pet_speaking_candidate_jobs import register_new_feedback_intent, read_feedback_intent
            local_job = (register_new_feedback_intent(connection, session_id, turn_id)
                         if not completed else read_feedback_intent(connection, session_id, turn_id)) if oral else None
            connection.commit()
        if oral:
            return {**base, 'status': 'feedback_ready', 'turn_id': turn_id,
                    'feedback_turn_id': turn_id, 'objective_id': binding['objective_id'],
                    **({'local_speaking_candidate': local_job} if local_job else {})}
        return {**base, "status": "candidate_recorded", "turn_id": turn_id,
                "objective_id": binding["objective_id"], "evaluation": result["evaluation"]}

    @staticmethod
    def writing_reference_source(c, session_id, prompt_id, item_version):
        from .writing_response_origin import _prompt, original_written_response
        from .local_supply import digest
        from .learning_obligations import withdrawn_evidence_ids
        prompt, item = _prompt(c, session_id, prompt_id, item_version, live=False)
        attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
            (prompt['attempt_id'], prompt['child_id'], session_id)).fetchone()
        if not attempt or attempt['evidence_id'] in withdrawn_evidence_ids(c, prompt['child_id']):
            raise InvalidAgentPlanError('ANSWER_WRITING_REFERENCE_WITHDRAWN')
        origin = original_written_response(c, prompt, attempt)
        rubric = json.loads(item['expected_answer_json'])
        return {'child_id': prompt['child_id'], 'session_id': session_id, 'prompt_id': prompt_id, 'item_version': item_version,
            'authorized_turn_id': prompt['authorized_turn_id'],
            'evidence_id': attempt['evidence_id'], 'attempt_id': attempt['attempt_id'], 'response': attempt['submitted_answer'],
            'task_points': rubric['task_points'], 'min_words': rubric.get('min_words'), 'max_words': rubric.get('max_words'),
            'source_version': digest([origin, dict(attempt), item])}

    def prepare_writing_reference_feedback(self, c, session_id, prompt_id, item_version, binding, key,
                                          *, previous=None, transcript=''):
        """Measurable facts and child-led source references, never a rubric score."""
        source = self.writing_reference_source(c, session_id, prompt_id, item_version)
        existing = c.execute('SELECT agent_turn_id FROM agent_turn WHERE child_id=? AND session_id=? AND client_action_id=?',
            (source['child_id'], session_id, key)).fetchone()
        if existing:
            if previous is None:
                current = c.execute('''SELECT t.agent_turn_id FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
                    WHERE t.child_id=? AND t.session_id=? AND t.intent='writing_local_reference'
                    AND p.status NOT IN ('cancelled','failed')
                    AND json_extract(p.context_manifest_json,'$.writing_reference.evidence_id')=?
                    AND json_extract(p.context_manifest_json,'$.writing_reference.source_version')=?
                    ORDER BY t.rowid DESC LIMIT 1''', (source['child_id'], session_id, source['evidence_id'], source['source_version'])).fetchone()
                if current:
                    return current[0]
            return existing[0]
        count = len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", source['response']))
        points = source['task_points']
        index, entries = 0, []
        if previous is not None:
            if previous['source_version'] != source['source_version']:
                raise InvalidAgentPlanError('ANSWER_WRITING_REFERENCE_CHANGED')
            index, entries = previous['point_index'], list(previous['entries'])
            quote = transcript.strip().strip('“”"')
            quoted = re.fullmatch(r'(?:我写的是|我写了|这句是)[“「"](.+?)[”」"][。！!\s]*', transcript)
            quote = quoted[1] if quoted else quote
            missing = bool(re.fullmatch(r'(?:这(?:一)?项|这个|这点)?(?:我)?(?:还没写|没有写|暂时没写|漏写了)[。！!\s]*', transcript))
            if index >= len(points) or (not missing and (len(quote) < 8 or source['response'].count(quote) != 1)):
                return None
            entries.append({'task_point': points[index], 'status': 'child_reported_missing' if missing else 'child_linked_quote',
                            'response_quote': None if missing else quote, 'request_quote': transcript,
                            'semantic_coverage_verified': False})
            index += 1
        facts = f'按英文词的本机计数，这份原稿有{count}词。'
        lo, hi = source['min_words'], source['max_words']
        if type(lo) is int and type(hi) is int:
            facts += f'题目要求约{lo}到{hi}词。'
            facts += f'目前比下限少{lo-count}词。' if count < lo else f'目前比上限多{count-hi}词。' if count > hi else '字数在这个区间。'
        response = (facts + '这是本机参考核对，不是完整语法或任务覆盖评分。' if previous is None
                    else '已保存你对这一项的自检和原句出处，还没有认证这句话是否充分表达要求。')
        if previous is None and binding.get('recovery_only'):
            response = ('你的原稿已经保存。当时没有建立对应的学习目标，这份旧稿不能补记为掌握证据。'
                '我们先保留原稿做本机自检；你可以说“我想改一遍”，再提交一份新稿。' + response)
        if index < len(points):
            response += f'接着核对第{index+1}项：{points[index]}。请读出你原稿表达这一点的原句；还没写也可以直接说。'
        else:
            response += '各项自检已保存；标出的漏项可以继续修改。引用记录不等于写作达标，完整表达和语法仍需进一步反馈。'
        local = {k:source[k] for k in ('child_id','session_id','prompt_id','item_version','evidence_id','attempt_id','source_version')}
        local.update(point_index=index, entries=entries, word_count=count, task_points=points,
            reference_only=True, automatic_certification=False, local_only=True, cloud_transmission_allowed=False)
        tid = str(uuid4())
        action = {'type': 'none', 'source': 'writing_local_reference', 'local_only': True, 'cloud_transmission_allowed': False,
                  'mastery_update_allowed': False, 'writing_reference': local}
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (tid, source['child_id'], session_id, transcript, 'writing_local_reference', response, json.dumps(action, ensure_ascii=False),
             'writing_reference_v1', datetime.now(timezone.utc).isoformat(), key))
        cap = CapabilityRegistry().require('conversation.respond', 'study')
        decision = AgentPlanDecision(ObjectiveDraft('none', None, None, (), 'check'),
            PlannedStep(cap, {'response': response}, 'Local writing self-check, not assessment', ()), response, False, 1)
        metadata = self.persist_plan(c, child_id=source['child_id'], session_id=session_id, agent_turn_id=tid,
            client_action_id=key, decision=decision, context_manifest={'writing_reference': local,
                'writing_reference_authorizing_turn_id': tid if previous else source['authorized_turn_id'],
                'original_learning_binding': binding, 'local_only': True, 'cloud_transmission_allowed': False})
        c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?", (json.dumps(metadata), tid))
        return tid

    @staticmethod
    def writing_reference_arrival(c, session_id, child_id):
        row = c.execute('''SELECT t.agent_turn_id,p.context_manifest_json,p.status FROM agent_turn t
            JOIN agent_plan p USING(agent_turn_id) WHERE t.session_id=? AND t.child_id=? ORDER BY t.rowid DESC LIMIT 1''',
            (session_id, child_id)).fetchone()
        if not row or row['status'] in {'cancelled','failed'}:
            return None
        manifest = json.loads(row['context_manifest_json'])
        reference = manifest.get('writing_reference')
        if not reference:
            return None
        return {'reference': reference, 'binding': manifest['original_learning_binding'], 'turn_id': row['agent_turn_id']}

    @staticmethod
    def validate_writing_reference_execution(c, turn_id):
        from .school_oral_reference import validate
        validate(c,turn_id)
        row = c.execute('''SELECT t.child_id,t.session_id,t.tool_status,p.context_manifest_json FROM agent_turn t
            JOIN agent_plan p USING(agent_turn_id) WHERE t.agent_turn_id=?''', (turn_id,)).fetchone()
        reference = json.loads(row['context_manifest_json']).get('writing_reference') if row else None
        if not reference:
            return
        if row['tool_status'] != 'completed':
            authorizer = json.loads(row['context_manifest_json']).get('writing_reference_authorizing_turn_id')
            if authorizer:
                current = c.execute('''SELECT 1 FROM learning_input_arrival a JOIN agent_turn t
                    ON t.client_action_id=a.client_action_id AND t.session_id=a.session_id AND t.child_id=a.child_id
                    WHERE t.agent_turn_id=? AND a.session_id=? AND a.child_id=?
                    AND a.rowid=(SELECT MAX(rowid) FROM learning_input_arrival WHERE session_id=?)''',
                    (authorizer, row['session_id'], row['child_id'], row['session_id'])).fetchone()
                if not current:
                    raise InvalidAgentPlanError('ANSWER_WRITING_REFERENCE_SUPERSEDED')
        try:
            current = AgentRuntimeStore.writing_reference_source(c, row['session_id'], reference['prompt_id'], reference['item_version'])
            if current['child_id'] != row['child_id'] or current['source_version'] != reference['source_version']:
                raise ValueError('source changed')
        except (ValueError, KeyError):
            raise InvalidAgentPlanError('ANSWER_WRITING_REFERENCE_CHANGED') from None

    @staticmethod
    def confirmed_writing_submission(c, turn_id, *, for_write=False):
        from .writing_ocr_authorization import resolve_written_document
        row = c.execute('''SELECT t.*,s.capability_name,s.status AS step_status,s.idempotency_key,
            p.status AS plan_status FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
            JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0
            AND p.child_id=t.child_id AND p.session_id=t.session_id''', (turn_id,)).fetchone()
        if not row or row['capability_name'] != 'writing.confirm_original':
            return None
        AgentRuntimeStore.assert_turn_source_not_deleted(c, turn_id)
        action = json.loads(row['action_json'])
        binding = action.get('writing_ocr_binding') or {}
        event = c.execute('''SELECT * FROM writing_ocr_event WHERE client_action_id=? AND child_id=?
            AND confirmation_id=? AND kind='authorize' ''',
            (row['idempotency_key'], row['child_id'], binding.get('confirmation_id'))).fetchone()
        if (not event or row['tool_status'] != 'completed' or row['step_status'] != 'completed'
                or row['plan_status'] != 'completed'
                or json.loads(event['request_json']).get('authorizing_turn_id') != turn_id):
            raise InvalidAgentPlanError('ANSWER_WRITING_AUTHORIZATION_RECEIPT_REQUIRED')
        if for_write:
            latest = c.execute('SELECT client_action_id FROM learning_input_arrival WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                (row['session_id'], row['child_id'])).fetchone()
            if not latest or latest[0] != row['client_action_id']:
                raise InvalidAgentPlanError('ANSWER_WRITING_AUTHORIZATION_SUPERSEDED')
        source = dict(child_id=row['child_id'], session_id=row['session_id'],
            **{k:binding.get(k) for k in ('prompt_id','item_version','artifact_id','revision_id','confirmation_id')},
            use_authorization_id=event['event_id'])
        try:
            document = resolve_written_document(c, **source)
        except ValueError as error:
            raise InvalidAgentPlanError('ANSWER_WRITING_SOURCE_CHANGED') from error
        return {**source, 'answer': document['text']}

    def finalize_confirmed_writing(self, turn_id):
        """Post-commit recovery uses the authorized page, never confirmation ASR."""
        from .writing_response_origin import WritingOriginalService
        from .diagnostic_prompt import DiagnosticPromptService
        with self.database.connect() as c:
            c.execute('BEGIN')
            source = self.confirmed_writing_submission(c, turn_id)
            if source is None:
                return None
        origin = WritingOriginalService(self.database).bind_confirmed_document_response(source['session_id'],
            **{k:source[k] for k in ('prompt_id','item_version','artifact_id','revision_id','confirmation_id','use_authorization_id')},
            client_action_id='confirmed-turn:' + turn_id)
        result = DiagnosticPromptService(self.database).answer(source['session_id'], prompt_id=source['prompt_id'],
            item_version=source['item_version'], answer=source['answer'], client_action_id='writing-submit:' + turn_id,
            authorized_turn_id=turn_id, writing_original_artifact_id=origin['artifact_id'])
        attempt = result.get('attempt') or {}
        if result.get('status') != 'answered' or not attempt.get('evidence_id'):
            return {'status': 'blocked', 'reason': 'original_writing_submission_not_current',
                    'local_only': True, 'cloud_transmission_allowed': False}
        return self.finalize_diagnostic_evaluation(source['session_id'], attempt['evidence_id'])

    def finalize_diagnostic_turn(self, turn_id: str) -> dict[str, Any] | None:
        writing = self.finalize_confirmed_writing(turn_id)
        if writing is not None:
            return writing
        with self.database.connect() as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
                return None
            row = connection.execute("""SELECT p.session_id,a.evidence_id FROM agent_plan p JOIN agent_step s USING(plan_id)
                JOIN diagnostic_prompt prompt ON prompt.prompt_id=json_extract(p.plan_json,'$.local_learning_binding.prompt_id')
                  AND prompt.item_version=json_extract(p.plan_json,'$.local_learning_binding.item_version')
                  AND prompt.child_id=p.child_id AND prompt.origin_session_id=p.session_id
                JOIN diagnostic_attempt a ON a.attempt_id=prompt.attempt_id AND a.child_id=p.child_id AND a.session_id=p.session_id
                WHERE p.agent_turn_id=? AND s.capability_name='diagnostic.submit' AND s.status='completed' AND prompt.status='answered'""",
                (turn_id,)).fetchone()
        return self.finalize_diagnostic_evaluation(row[0], row[1]) if row else None

    def reconcile_learning_state(self, child_id: str, *, connection=None) -> list[dict[str, Any]]:
        from .learning_outcome_coordinator import LearningOutcomeCoordinator
        from .learning_obligations import cancel_unstarted_plans
        if connection is None:
            from .rubric_calibration_transaction import calibrated_write
            with calibrated_write(self.database, child_id) as opened:
                result = self.reconcile_learning_state(child_id, connection=opened)
                opened.commit()
                return result
        items = LearningOutcomeCoordinator(self.database).reconcile_child(child_id, connection=connection)
        objectives = {item["objective_id"]: item for item in items}
        rank = {"independent": 1, "transferred": 2, "retained": 3}
        rows = connection.execute("""SELECT * FROM agent_commitment WHERE child_id=? AND status IN ('pending','due')
            AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.origin') END='local_learning_gate'""", (child_id,)).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            source = next((gate for gate in objectives.get(row["objective_id"], {}).get("mastery_gate", {}).get("outcomes", [])
                           if gate["outcome_id"] == payload.get("source_outcome_id")), None)
            if (not source or not source["eligible"] or rank.get(source["outcome_level"], 0)
                    < (2 if payload.get("phase") == "retention_check" else 1)):
                connection.execute("UPDATE agent_commitment SET status='cancelled' WHERE commitment_id=?", (row["commitment_id"],))
                cancel_unstarted_plans(connection, child_id, "agent_retention", row["commitment_id"], datetime.now(timezone.utc).isoformat())
        from .oral_supply_obligation import reconcile
        reconcile(connection,child_id)
        return items

    def inspect_learning_gate(self, child_id: str, objective_id: str, *, connection=None) -> dict[str, Any]:
        from .learning_outcome_coordinator import LearningOutcomeCoordinator
        if connection is None:
            with self.database.connect() as opened:
                opened.execute("BEGIN")
                return self.inspect_learning_gate(child_id, objective_id, connection=opened)
        row = connection.execute("SELECT * FROM agent_objective WHERE child_id=? AND objective_id=? AND objective_type='learning'",
                                 (child_id, objective_id)).fetchone()
        if not row:
            raise InvalidAgentPlanError("LEARNING_OBJECTIVE_NOT_OWNED")
        return {**LearningOutcomeCoordinator(self.database)._recommendation(connection, row),
                "expected_objective_updated_at": row["updated_at"]}

    @staticmethod
    def _next_scheduled_task(connection, child_id: str, session_id: str):
        plan = connection.execute("SELECT plan_id FROM daily_plan WHERE child_id=? AND session_id=? AND status='active' LIMIT 1",
                                  (child_id, session_id)).fetchone()
        return connection.execute("""SELECT * FROM plan_item WHERE plan_id=? AND status IN ('paused','pending')
            ORDER BY CASE status WHEN 'paused' THEN 0 ELSE 1 END,position,plan_item_id LIMIT 1""", (plan[0],)).fetchone() if plan else None

    def start_learning_recheck(self, session_id: str, commitment_id: str, client_action_id: str, *,
                               authorized_turn_id: str | None = None) -> dict[str, Any]:
        """Explicit task-start hook. Prepare an owned local question, never a cloud prompt."""
        from .diagnostic_prompt import DiagnosticPromptService
        from .learning_obligations import _question_catalog

        if not isinstance(client_action_id, str) or not client_action_id.strip():
            raise InvalidAgentPlanError("LOCAL_RECHECK_ACTION_REQUIRED")
        # Initialize outside the owning transaction: the prompt constructor uses DDL.
        DiagnosticPromptService(self.database)
        base = {"local_only": True, "cloud_transmission_allowed": False, "commitment_id": commitment_id}
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            session = DiagnosticPromptService._session(c, session_id)
            self.reconcile_learning_state(session["child_id"], connection=c)
            row = c.execute("""SELECT ac.*,ao.status AS objective_status,ao.phase AS objective_phase,
                ao.success_criteria_json FROM agent_commitment ac
                JOIN agent_objective ao ON ao.objective_id=ac.objective_id AND ao.child_id=ac.child_id
                WHERE ac.commitment_id=? AND ac.child_id=? AND ao.objective_type='learning'""",
                (commitment_id, session["child_id"])).fetchone()
            if (not row or row["status"] not in {"pending", "due"}
                    or row["objective_status"] not in {"active", "paused"}):
                c.commit()
                return {**base, "status": "blocked", "reason": "commitment_unavailable"}
            payload = json.loads(row["payload_json"])
            if payload.get("origin") != "local_learning_gate":
                return {**base, "status": "blocked", "reason": "not_local_learning_gate"}
            if (session["status"] != "active" or session["child_status"] != "active" or session["state"] != "study"):
                return {**base, "status": "blocked", "reason": "session_inactive"}
            now = datetime.now(timezone.utc).isoformat()
            if not row["due_at"] or _aware_datetime(row["due_at"]) > _aware_datetime(now):
                return {**base, "status": "blocked", "reason": "not_due", "not_before": row["due_at"]}
            current = self.inspect_learning_gate(session["child_id"], row["objective_id"], connection=c)
            # Reconcile replays this exact source's eligibility. New unsuccessful
            # candidates must not permanently prevent a later authorized retry.
            key = f"local-recheck:{client_action_id}"
            existing = c.execute("""SELECT t.child_id,t.session_id,p.plan_json,p.context_manifest_json,t.agent_turn_id
                FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                WHERE t.client_action_id=?""", (key,)).fetchone()
            command = c.execute("""SELECT p.plan_json,p.plan_id,p.objective_id,t.agent_turn_id,s.status,
                p.child_id,p.session_id,t.child_id AS turn_child,t.session_id AS turn_session,
                EXISTS(SELECT 1 FROM agent_turn newer WHERE newer.session_id=t.session_id AND newer.rowid>t.rowid
                    AND NOT (newer.intent='local_recheck'
                        AND COALESCE(json_extract(newer.action_json,'$.authorized_turn_id'),'')=t.agent_turn_id
                        AND EXISTS (SELECT 1 FROM agent_plan derived WHERE derived.agent_turn_id=newer.agent_turn_id
                            AND derived.child_id=t.child_id AND derived.session_id=t.session_id
                            AND json_extract(derived.context_manifest_json,'$.authorized_plan_id')=p.plan_id))) AS superseded
                FROM agent_step s JOIN agent_plan p USING(plan_id) JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
                WHERE s.capability_name='task.start' AND s.idempotency_key=?""", (client_action_id,)).fetchone()
            if authorized_turn_id is not None and (not command or command["agent_turn_id"] != authorized_turn_id):
                return {**base, "status": "blocked", "reason": "task_start_not_current"}
            if command:
                binding = json.loads(command["plan_json"]).get("local_recheck_binding")
                if not isinstance(binding, dict):
                    return {**base, "status": "blocked", "reason": "task_start_binding_missing"}
                owned = (session["child_id"], session_id)
                if ((command["child_id"], command["session_id"]) != owned
                        or (command["turn_child"], command["turn_session"]) != owned
                        or command["status"] != "running" or command["superseded"]
                        or not ContinuationStore.execution_allowed(c, command["agent_turn_id"], running=True)):
                    return {**base, "status": "blocked", "reason": "task_start_not_current"}
                item = c.execute("""SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                    WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=? AND dp.status='active'""",
                    (binding.get("plan_item_id"), session["child_id"], session_id)).fetchone()
                selected = self._next_scheduled_task(c, session["child_id"], session_id)
                if (binding.get("version") != "local_recheck_task_binding_v1"
                        or binding.get("child_id") != session["child_id"] or binding.get("session_id") != session_id
                        or binding.get("commitment_id") != commitment_id or binding.get("learning_objective_id") != row["objective_id"]
                        or binding.get("authorized_turn_id") != command["agent_turn_id"]
                        or binding.get("source_outcome_id") != payload.get("source_outcome_id")
                        or binding.get("phase") != payload.get("phase")
                        or binding.get("success_criteria") != json.loads(row["success_criteria_json"])
                        or row["objective_phase"] != (payload["phase"] if existing else binding.get("objective_phase"))
                        or not item or item["plan_id"] != binding.get("daily_plan_id")
                        or item["source_type"] != "agent_retention" or item["source_ref"] != commitment_id
                        or item["status"] not in {"pending", "paused", "active"}
                        or (not existing and (not selected or selected["plan_item_id"] != item["plan_item_id"]
                            or item["started_at"] != binding.get("started_at")))):
                    return {**base, "status": "blocked", "reason": "task_start_binding_changed"}
                base.update(authorized_turn_id=command["agent_turn_id"], authorized_plan_id=command["plan_id"])
            other_active = c.execute("SELECT objective_id FROM agent_objective WHERE child_id=? AND status='active' AND objective_id!=?",
                (session["child_id"], row["objective_id"])).fetchall()
            wrapper = None
            if other_active:
                wrapper = c.execute("""SELECT ao.objective_id,t.agent_turn_id FROM agent_step s
                    JOIN agent_plan p USING(plan_id) JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
                    JOIN agent_objective ao ON ao.objective_id=p.objective_id AND ao.child_id=p.child_id
                    WHERE s.capability_name='task.start' AND s.idempotency_key=? AND s.status='running'
                      AND p.session_id=? AND p.child_id=? AND t.session_id=p.session_id AND t.child_id=p.child_id
                      AND ao.objective_type='execution' AND ao.status='active' AND ao.last_agent_turn_id=t.agent_turn_id
                      AND json_extract(p.plan_json,'$.objective.mode')='new'
                      """,
                    (client_action_id, session_id, session["child_id"])).fetchone()
                if (not command or not wrapper or len(other_active) != 1 or other_active[0][0] != wrapper["objective_id"]
                        or not ContinuationStore.execution_allowed(c, wrapper["agent_turn_id"], running=True)):
                    return {**base, "status": "blocked", "reason": "another_objective_active"}
            class TransactionPrompt(DiagnosticPromptService):
                # Borrow this transaction without constructor DDL or inner commits.
                def __init__(self):
                    self.database = None

                def _transaction(self):
                    return nullcontext(c)

            prompts = TransactionPrompt()
            if existing:
                manifest = json.loads(existing["context_manifest_json"])
                if ((existing["child_id"], existing["session_id"]) != (session["child_id"], session_id)
                        or manifest.get("commitment_id") != commitment_id):
                    raise InvalidAgentPlanError("LOCAL_RECHECK_ACTION_CONFLICT")
                binding = json.loads(existing["plan_json"])["local_learning_binding"]
                result = prompts.read(session_id, prompt_id=binding["prompt_id"])
                if wrapper and result["status"] == "waiting_answer":
                    c.execute("UPDATE agent_objective SET status='paused',updated_at=? WHERE objective_id=?", (now, wrapper["objective_id"]))
                    c.execute("UPDATE agent_objective SET status='active',phase=?,updated_at=? WHERE objective_id=?",
                        (binding["phase"], now, row["objective_id"]))
                c.commit()
                return {**result, **base, "turn_id": existing["agent_turn_id"], "objective_id": row["objective_id"]}
            if c.execute("SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=? AND status IN ('waiting_answer','answer_saved')",
                         (session_id,)).fetchone():
                return {**base, "status": "blocked", "reason": "pending_prompt_exists"}
            source = next(g for g in current["mastery_gate"]["outcomes"] if g["outcome_id"] == payload["source_outcome_id"])
            evidence_id = source.get("source_evidence_id") or source.get("evidence_id")
            node = c.execute("""SELECT i.knowledge_node_id,n.track FROM diagnostic_attempt a
                JOIN diagnostic_item i USING(diagnostic_item_id) JOIN knowledge_node n USING(knowledge_node_id)
                WHERE a.evidence_id=? AND a.child_id=?""", (evidence_id, session["child_id"])).fetchone()
            if not node:
                return {**base, "status": "blocked", "reason": "diagnostic_source_missing"}
            catalog, identities = _question_catalog(c)
            excluded = set(current["exclude_question_identities"])
            shown = c.execute("""SELECT DISTINCT json_extract(plan_json,'$.local_learning_binding.diagnostic_item_id')
                FROM agent_plan WHERE child_id=? AND objective_id=?""", (session["child_id"], row["objective_id"]))
            excluded.update(identities[item[0]] for item in shown if item[0] in identities)
            selected = next((item_id for item_id, item in sorted(catalog.items())
                if item["knowledge_node_id"] == node[0] and identities.get(item_id)
                and identities[item_id] not in excluded
                and prompts._item(c, session["child_id"], item_id)), None)
            if not selected:
                return {**base, "status": "blocked", "reason": "distinct_local_question_unavailable"}
            turn_id = str(uuid4())
            action = {"type": "start_diagnostic", "track": node[1], "source": "local_learning_gate", **base}
            if wrapper:
                c.execute("UPDATE agent_objective SET status='paused',updated_at=? WHERE objective_id=?", (now, wrapper["objective_id"]))
            c.execute("""UPDATE agent_objective SET status='active',phase=?,updated_at=?
                WHERE objective_id=? AND child_id=?""", (payload["phase"], now, row["objective_id"], session["child_id"]))
            c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,tool_status,orchestrator_version,created_at,client_action_id)
                VALUES (?,?,?,'','local_recheck','',?,'pending',?,?,?)""",
                (turn_id, session["child_id"], session_id, json.dumps(action), PLANNER_VERSION, now, key))
            cap = CapabilityRegistry().require("diagnostic.start", "study")
            self.persist_plan(c, child_id=session["child_id"], session_id=session_id, agent_turn_id=turn_id,
                client_action_id=key, context_manifest=base, decision=AgentPlanDecision(
                    ObjectiveDraft("continue", "learning", None, (), payload["phase"]),
                    PlannedStep(cap, {"track": node[1]}, "Local due recheck", cap.expected_evidence), "", True, 1))
            result = prompts.prepare(session_id, track=node[1], preferred_item_id=selected, client_action_id=key)
            if result["status"] != "waiting_answer":
                c.rollback()
                return {**base, "status": "blocked", "reason": result.get("reason") or "question_unavailable"}
            if command:
                prompt = result["prompt"]
                source_binding = json.loads(command["plan_json"])["local_recheck_binding"]
                goal_prompt_binding = {
                    "prompt_id": prompt["prompt_id"], "item_version": prompt["item_version"],
                    "authorized_turn_id": command["agent_turn_id"], "authorized_plan_id": command["plan_id"],
                    "child_id": session["child_id"], "session_id": session_id,
                    "plan_item_id": source_binding["plan_item_id"], "daily_plan_id": source_binding["daily_plan_id"],
                    "source_type": "agent_retention", "source_ref": commitment_id,
                }
                parent_manifest = json.loads(c.execute("SELECT context_manifest_json FROM agent_plan WHERE plan_id=?",
                                                       (command["plan_id"],)).fetchone()[0])
                task_binding = parent_manifest.get("task_start_binding")
                if task_binding and any(task_binding.get(key) != goal_prompt_binding[key]
                                        for key in ("plan_item_id", "source_type", "source_ref")):
                    c.rollback()
                    return {**base, "status": "blocked", "reason": "task_start_binding_changed"}
                parent_manifest.setdefault("task_start_binding", {key: goal_prompt_binding[key]
                    for key in ("plan_item_id", "source_type", "source_ref")})
                parent_manifest["task_start_binding"].setdefault("operation", "resume" if item["status"] == "paused" else "start")
                parent_manifest["goal_prompt_binding"] = goal_prompt_binding
                c.execute("UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?",
                          (json.dumps(parent_manifest), command["plan_id"]))
            self.complete_turn_execution(c, turn_id, True, None, {"execution_receipt": {
                "capability": cap.name, "execution_location": cap.execution_location, "action_type": "start_diagnostic",
                "status": "completed", "completed_at": now, "output": result}})
            c.execute("UPDATE agent_turn SET tool_status='completed',completed_at=? WHERE agent_turn_id=?", (now, turn_id))
            c.execute("UPDATE agent_commitment SET status='due' WHERE commitment_id=?", (commitment_id,))
            c.commit()
            return {**result, **base, "turn_id": turn_id, "objective_id": row["objective_id"]}

    def apply_learning_gate(self, child_id: str, objective_id: str, *, expected_gate_fingerprint: str,
                            expected_objective_updated_at: str) -> dict[str, Any]:
        """Explicit authorized write hook; never called by status/GET readers."""
        from .rubric_calibration_transaction import calibrated_write
        with calibrated_write(self.database, child_id) as c:
            current = self.inspect_learning_gate(child_id, objective_id, connection=c)
            base = {"local_only": True, "cloud_transmission_allowed": False, "objective_id": objective_id}
            if current["recorded_status"] in {"cancelled", "failed"}:
                return {**base, "status": "blocked", "reason": "objective_inactive"}
            if current["gate_fingerprint"] != expected_gate_fingerprint:
                return {**base, "status": "conflict", "reason": "gate_changed"}
            level = current["outcome_level"]
            basis = next((gate for gate in reversed(current["mastery_gate"]["outcomes"])
                          if gate["eligible"] and gate["outcome_level"] == level), None)
            if not basis or level not in {"independent", "transferred", "retained"}:
                self.reconcile_learning_state(child_id, connection=c)
                c.commit()
                return {**base, "status": "requires_evidence"}
            plan = c.execute("""SELECT p.* FROM agent_outcome o JOIN agent_step s
                ON s.step_id=json_extract(o.evidence_refs_json,'$[0].agent_step_id') JOIN agent_plan p USING(plan_id)
                WHERE o.outcome_id=? AND o.child_id=? AND p.child_id=o.child_id AND p.objective_id=o.objective_id""",
                (basis["outcome_id"], child_id)).fetchone()
            if not plan:
                return {**base, "status": "blocked", "reason": "basis_plan_missing"}
            manifest = json.loads(plan["context_manifest_json"])
            previous = manifest.get("learning_gate_application") or {}
            if (previous.get("gate_fingerprint") == expected_gate_fingerprint
                    and previous.get("objective_status") == current["recorded_status"]
                    and previous.get("objective_phase") == current["recorded_phase"]):
                return {**base, "status": "already_applied", "outcome_level": level,
                        "commitment_id": previous.get("commitment_id"),
                        "objective_status": current["recorded_status"], "objective_phase": current["recorded_phase"]}
            if current["expected_objective_updated_at"] != expected_objective_updated_at:
                return {**base, "status": "conflict", "reason": "objective_changed"}
            busy = c.execute("SELECT 1 FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.objective_id=? AND s.status='running' LIMIT 1",
                             (objective_id,)).fetchone()
            if not busy and c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
                busy = c.execute("""SELECT 1 FROM diagnostic_prompt d JOIN agent_plan p
                    ON json_extract(p.plan_json,'$.local_learning_binding.prompt_id')=d.prompt_id
                    AND p.child_id=d.child_id AND p.session_id=d.origin_session_id
                    WHERE p.objective_id=? AND p.child_id=? AND d.status IN ('waiting_answer','answer_saved') LIMIT 1""",
                    (objective_id, child_id)).fetchone()
            if busy:
                return {**base, "status": "blocked", "reason": "objective_busy"}
            phase = {"independent": "transfer_check", "transferred": "retention_due", "retained": "completed"}[level]
            status = ("completed" if level == "retained" else "paused" if level == "transferred"
                      or current["recorded_status"] != "active" else "active")
            now = datetime.now(timezone.utc).isoformat()
            c.execute("UPDATE agent_objective SET status=?,phase=?,updated_at=?,completed_at=? WHERE objective_id=? AND child_id=?",
                      (status, phase, now, now if status == "completed" else None, objective_id, child_id))
            self.reconcile_learning_state(child_id, connection=c)
            commitment_id = None
            if level in {"independent", "transferred"}:
                check_phase = "transfer_check" if level == "independent" else "retention_check"
                existing = c.execute("""SELECT commitment_id FROM agent_commitment WHERE child_id=? AND objective_id=?
                    AND status IN ('pending','due') AND json_extract(payload_json,'$.origin')='local_learning_gate'
                    AND json_extract(payload_json,'$.source_outcome_id')=? AND json_extract(payload_json,'$.phase')=? LIMIT 1""",
                    (child_id, objective_id, basis["outcome_id"], check_phase)).fetchone()
                commitment_id = existing[0] if existing else str(uuid4())
                if not existing:
                    payload = {"origin": "local_learning_gate", "phase": check_phase, "source_outcome_id": basis["outcome_id"],
                        "gate_fingerprint": current["gate_fingerprint"], "exclude_question_identities": current["exclude_question_identities"],
                        "missing_criteria": current["missing_criteria"], "local_only": True, "cloud_transmission_allowed": False}
                    c.execute("""INSERT INTO agent_commitment
                        (commitment_id,objective_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
                        VALUES (?,?,?,?,?,'pending',?,?,?)""", (commitment_id, objective_id, child_id,
                        "follow_up" if level == "independent" else "review", current["not_before"], json.dumps(payload), now,
                        f"local-gate:{objective_id}:{current['gate_fingerprint']}"))
            if level in {"transferred", "retained"}:
                previous_phase = "transfer_check" if level == "transferred" else "retention_check"
                c.execute("""UPDATE agent_commitment SET status='completed' WHERE child_id=? AND objective_id=?
                    AND status IN ('pending','due') AND json_extract(payload_json,'$.origin')='local_learning_gate'
                    AND json_extract(payload_json,'$.phase')=?""", (child_id, objective_id, previous_phase))
            manifest["learning_gate_application"] = {"gate_fingerprint": current["gate_fingerprint"],
                "from_updated_at": expected_objective_updated_at, "applied_at": now, "outcome_level": level,
                "objective_status": status, "objective_phase": phase, "commitment_id": commitment_id,
                "local_only": True, "cloud_transmission_allowed": False}
            c.execute("UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?", (json.dumps(manifest, ensure_ascii=False), plan["plan_id"]))
            c.commit()
            return {**base, "status": "applied", "outcome_level": level, "objective_status": status,
                    "objective_phase": phase, "commitment_id": commitment_id}

    @staticmethod
    def _behavior_reminder_current(connection, row, now) -> bool:
        if not row or row['status'] != 'active' or row['feedback'] is not None:
            return False
        # A durably detected stop suppresses every reminder type for the
        # affected child/state even before the guarded final stop can commit.
        from .behavior_model.habit_formation import auto_stop_pending_for_state
        if auto_stop_pending_for_state(connection, row['child_id'], row['candidate_state']):
            return False
        from .behavior_model.intervention_withdrawal import pending_for_state
        if pending_for_state(connection, row['child_id'], row['candidate_state']):
            return False
        if row['intervention_type'] != 'contextual_habit_ask_first':
            return True
        from .behavior_model.outcome_evidence import accepted_habit_support
        try:
            basis = json.loads(row['support_basis_json'] or '{}')
        except (TypeError, ValueError):
            return False
        current = accepted_habit_support(connection, row['child_id'], row['session_id'],
                                         row['candidate_state'], row['decided_at'], now)
        return bool(isinstance(basis, dict) and current and all(
            current[key] == basis.get(key)
            for key in ('goal_version', 'acceptance_ref', 'context_version', 'evidence_version')))

    @classmethod
    def _proactive_source_current(cls, connection, row, now) -> bool:
        if row['status'] != 'delivered' or _aware_datetime(row['expires_at']) <= now:
            return False
        session = connection.execute('SELECT child_id,status FROM daily_session WHERE session_id=?',
                                     (row['session_id'],)).fetchone()
        if not session or session['child_id'] != row['child_id'] or session['status'] != 'active':
            return False
        if row['source_type'] == 'document_revisit':
            from .document_revisit import offer_current
            return offer_current(connection, row['child_id'], row['source_id'], now.isoformat())
        if row['source_type'] == 'oral_supply_ready':
            from .oral_supply_obligation import ready_offer
            return bool(ready_offer(connection, row['child_id'], row['session_id'], now, row['source_id']))
        if row['source_type'] == 'oral_daily_supply_ready':
            from .oral_daily_reminder import ready
            return bool(ready(connection, row['child_id'], row['session_id'], now, row['source_id']))
        if row['source_type'] == 'due_commitment':
            from .oral_daily_reminder import due_current
            return due_current(connection, row['child_id'], row['session_id'], row['source_id'], now)
        if row['source_type'] == 'task_ready':
            from .oral_daily_reminder import task_current
            return task_current(connection, row['child_id'], row['session_id'], row['source_id'])
        if row['source_type'] == 'habit_experiment':
            from .behavior_model.habit_formation import auto_stop_pending
            experiment = connection.execute('''SELECT status FROM habit_experiment
                WHERE experiment_id=? AND child_id=?''', (row['source_id'], row['child_id'])).fetchone()
            return bool(experiment and experiment['status'] == 'proposed'
                        and not auto_stop_pending(connection, row['child_id'], row['source_id']))
        if row['source_type'] != 'behavior_intervention':
            return True
        source = connection.execute('''SELECT * FROM intervention
            WHERE intervention_id=? AND child_id=? AND session_id=?''',
            (row['source_id'], row['child_id'], row['session_id'])).fetchone()
        return cls._behavior_reminder_current(connection, source, now)

    def proactive_prompt_current(self, session_id: str, prompt_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT * FROM agent_proactive_prompt WHERE prompt_id=? AND session_id=?',
                                     (prompt_id, session_id)).fetchone()
            if not row:
                raise KeyError(prompt_id)
            current = not row['acknowledged_at'] and self._proactive_source_current(
                connection, row, datetime.now(timezone.utc))
        return {'prompt_id': prompt_id, 'current': bool(current)}

    def next_proactive_prompt(self, session_id: str) -> dict[str, Any]:
        """Persist and return at most one non-repeating, evidence-based prompt."""
        # Expiry must persist even when prompt delivery is suppressed below.
        self._ensure_companion_time_limits(session_id)
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT child_id, state, status, snooze_until FROM daily_session
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session or session["status"] != "active":
                connection.rollback()
                return {"status": "inactive", "prompt": None}
            if session["snooze_until"] and _aware_datetime(session["snooze_until"]) > now:
                connection.rollback()
                return {"status": "snoozed", "prompt": None, "until": session["snooze_until"]}
            if connection.execute(
                "SELECT 1 FROM companion_activity WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone():
                connection.rollback()
                return {"status": "companion_active", "prompt": None}
            latest_turn = connection.execute(
                "SELECT MAX(created_at) AS latest FROM agent_turn WHERE session_id = ?", (session_id,)
            ).fetchone()["latest"]
            if latest_turn and _aware_datetime(latest_turn) > now - timedelta(seconds=45):
                connection.rollback()
                return {"status": "dialogue_active", "prompt": None}
            waiting_child = connection.execute(
                """
                SELECT 1 FROM agent_plan ap
                JOIN agent_objective ao ON ao.objective_id = ap.objective_id
                WHERE ap.session_id = ? AND ap.status = 'waiting_child'
                  AND ao.status = 'active'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if waiting_child:
                connection.rollback()
                return {"status": "waiting_child", "prompt": None}
            pending_delivery = connection.execute(
                """
                SELECT * FROM agent_proactive_prompt
                WHERE session_id = ? AND acknowledged_at IS NULL AND status = 'delivered'
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if pending_delivery:
                if not self._proactive_source_current(connection, pending_delivery, now):
                    connection.execute(
                        "UPDATE agent_proactive_prompt SET status = 'expired' WHERE prompt_id = ?",
                        (pending_delivery["prompt_id"],),
                    )
                    connection.commit()
                    return {"status": "expired", "prompt": None}
                elif pending_delivery["last_attempt_at"] and _aware_datetime(
                    pending_delivery["last_attempt_at"]
                ) > now - timedelta(seconds=60):
                    connection.rollback()
                    return {"status": "awaiting_ack", "prompt": None}
                else:
                    connection.execute(
                        """
                        UPDATE agent_proactive_prompt
                        SET delivery_attempts = delivery_attempts + 1, last_attempt_at = ?
                        WHERE prompt_id = ?
                        """,
                        (now.isoformat(), pending_delivery["prompt_id"]),
                    )
                    connection.commit()
                    return {
                        "status": "retry_delivery",
                        "prompt": self._bind_proactive_prompt_state(
                            pending_delivery["child_id"], {
                            "prompt_id": pending_delivery["prompt_id"],
                            "source_type": pending_delivery["source_type"],
                            "message": pending_delivery["message_text"],
                            "expires_at": pending_delivery["expires_at"],
                            }, recovery=True,
                        ),
                    }
            last_prompt = connection.execute(
                """
                SELECT created_at FROM agent_proactive_prompt
                WHERE session_id = ? AND acknowledged_at IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if last_prompt and _aware_datetime(last_prompt["created_at"]) > now - timedelta(minutes=5):
                connection.rollback()
                return {"status": "cooldown", "prompt": None}

            candidate: tuple[str, str, str, int] | None = None
            intervention = connection.execute(
                """
                SELECT * FROM intervention
                WHERE session_id = ? AND child_id = ? AND status = 'active' AND feedback IS NULL
                  AND decided_at >= ?
                ORDER BY decided_at DESC LIMIT 1
                """,
                (session_id, session['child_id'], (now - timedelta(minutes=15)).isoformat()),
            ).fetchone()
            if intervention and self._behavior_reminder_current(connection, intervention, now):
                candidate = (
                    "behavior_intervention", intervention["intervention_id"],
                    intervention["message"], 10,
                )
            if not candidate:
                habit = connection.execute(
                    """SELECT experiment_id,title,child_action,duration_days
                       FROM habit_experiment
                       WHERE child_id=? AND status='proposed'
                       ORDER BY proposed_at LIMIT 1""",
                    (session["child_id"],),
                ).fetchone()
                if habit:
                    candidate = (
                        "habit_experiment", habit["experiment_id"],
                        f"我发现这个情况在不同日子出现过几次。我们要不要试一个{int(habit['duration_days'])}天的小实验，叫“{habit['title']}”？做法是：{habit['child_action']}。愿意就说“可以试试”，不想做就说“不做这个实验”。",
                        30,
                    )
            if not candidate and session["state"] == "break":
                break_state = connection.execute(
                    """
                    SELECT st.transition_id, st.occurred_at,
                           COALESCE(active_break.expected_minutes, fr.break_minutes, 5) AS break_minutes,
                           paused.title AS paused_title
                    FROM session_transition st
                    JOIN daily_session ds ON ds.session_id = st.session_id
                    LEFT JOIN family_rule fr ON fr.child_id = ds.child_id
                    LEFT JOIN (
                      SELECT dp.session_id, pi.expected_minutes
                      FROM daily_plan dp JOIN plan_item pi ON pi.plan_id = dp.plan_id
                      WHERE pi.status = 'active' AND pi.source_type = 'planned_break'
                    ) active_break ON active_break.session_id = st.session_id
                    LEFT JOIN (
                      SELECT dp.session_id, pi.title
                      FROM daily_plan dp JOIN plan_item pi ON pi.plan_id = dp.plan_id
                      WHERE pi.status = 'paused'
                      ORDER BY pi.position LIMIT 1
                    ) paused ON paused.session_id = st.session_id
                    WHERE st.session_id = ? AND st.to_state = 'break'
                    ORDER BY st.occurred_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if break_state:
                    due_at = _aware_datetime(break_state["occurred_at"]) + timedelta(
                        minutes=max(1, int(break_state["break_minutes"] or 5))
                    )
                    if now >= due_at:
                        return_to = (
                            f"，接着继续“{break_state['paused_title']}”"
                            if break_state["paused_title"] else ""
                        )
                        candidate = (
                            "break_due", break_state["transition_id"],
                            f"休息时间到了{return_to}。准备好就说“继续任务”；还需要一点时间也可以直接告诉我。",
                            10,
                        )
                    else:
                        connection.rollback()
                        return {
                            "status": "break_running",
                            "prompt": None,
                            "remaining_seconds": max(1, int((due_at - now).total_seconds())),
                        }
            if not candidate and session["state"] == "study":
                active_task = connection.execute(
                    """
                    SELECT 1 FROM plan_item pi
                    JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                    WHERE dp.session_id = ? AND pi.status = 'active'
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                from .oral_daily_reminder import due_current, ready as daily_supply_ready
                commitments = connection.execute(
                    """
                    SELECT commitment_id, payload_json FROM agent_commitment
                    WHERE child_id = ? AND status IN ('pending', 'due') AND due_at <= ?
                      AND (CASE WHEN json_valid(payload_json)
                        THEN json_extract(payload_json,'$.origin') END) IS NOT 'document_revisit_v1'
                      AND (CASE WHEN json_valid(payload_json)
                        THEN json_extract(payload_json,'$.origin') END) IS NOT 'oral_question_supply_v1'
                    ORDER BY due_at, commitment_id
                    """,
                    (session["child_id"], now.isoformat()),
                )
                commitment = next((r for r in commitments if due_current(
                    connection, session['child_id'], session_id, r['commitment_id'], now)), None)
                from .oral_supply_obligation import ready_offer
                oral_supply = ready_offer(connection, session['child_id'], session_id, now)
                if oral_supply:
                    candidate = ('oral_supply_ready', oral_supply,
                        '刚才想换的新题现在有了。要不要继续原来的练习，自己试一道？现在不方便也可以晚点再练。', 10)
                if not candidate:
                    supplied = daily_supply_ready(connection, session['child_id'], session_id, now)
                    if supplied:
                        candidate = ('oral_daily_supply_ready', supplied['source_id'],
                            f"今天{supplied['item']['subject']}口述复查需要的新题补齐了。现在要自己试一道吗？也可以晚点再练。", 10)
                if not candidate and commitment and not active_task:
                    # A due review may wake the child, but it must not start a
                    # lesson or read a question without an explicit reply. The
                    # actual prompt stays in the durable commitment and is only
                    # shown after the child chooses to begin the review task.
                    candidate = (
                        "due_commitment", commitment["commitment_id"],
                        "上次约好的复测时间到了。准备好时说“开始复测”；现在不方便也可以说“稍后提醒”。",
                        15,
                    )
                if not candidate and not active_task:
                    from .document_revisit import due
                    original = due(connection, session['child_id'], now.isoformat())
                    if original:
                        candidate = ('document_revisit', original,
                            '之前约好再试的那道原题到时间了。你想现在自己试试，还是另约时间？', 15)
            if not candidate and session["state"] == "study":
                active = connection.execute(
                    """
                    SELECT pi.plan_item_id, pi.title, pi.expected_minutes, pi.active_seconds,
                           pi.active_segment_started_at
                    FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                    WHERE dp.session_id = ? AND pi.status = 'active' LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if active and active["active_segment_started_at"]:
                    elapsed = int(active["active_seconds"] or 0) + max(
                        0, int((now - _aware_datetime(active["active_segment_started_at"])).total_seconds())
                    )
                    if elapsed >= int(active["expected_minutes"]) * 60 + 180:
                        candidate = (
                            "task_overtime", active["plan_item_id"],
                            f"“{active['title']}”比预计时间多了几分钟。你想继续、需要提示，还是先暂停？", 10,
                        )
                if not active:
                    from .oral_daily_reminder import task_current
                    pending_rows = connection.execute(
                        """
                        SELECT pi.plan_item_id, pi.title FROM plan_item pi
                        JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                        WHERE dp.session_id = ? AND dp.status='active' AND pi.status = 'pending'
                        ORDER BY pi.position, pi.plan_item_id
                        """,
                        (session_id,),
                    )
                    pending = next((r for r in pending_rows if task_current(
                        connection, session['child_id'], session_id, r['plan_item_id'])), None)
                    if pending:
                        candidate = (
                            "task_ready", pending["plan_item_id"],
                            f"下一项是“{pending['title']}”。准备好就直接说开始；需要调整也可以告诉我。", 10,
                        )
                    else:
                        settled_plan = connection.execute(
                            """
                            SELECT dp.plan_id,
                                   SUM(CASE WHEN pi.status IN ('pending', 'active', 'paused') THEN 1 ELSE 0 END) AS unsettled,
                                   COUNT(pi.plan_item_id) AS total
                            FROM daily_plan dp
                            JOIN plan_item pi ON pi.plan_id = dp.plan_id
                            WHERE dp.session_id = ? AND dp.status = 'active'
                            GROUP BY dp.plan_id LIMIT 1
                            """,
                            (session_id,),
                        ).fetchone()
                        if settled_plan and int(settled_plan["total"] or 0) > 0 and int(settled_plan["unsettled"] or 0) == 0:
                            candidate = (
                                "study_wrap_up", settled_plan["plan_id"],
                                "今天安排的任务都已经处理完了。准备好时说“开始复盘”，我们用三句话收好今天的学习。",
                                15,
                            )
            if not candidate and session["state"] == "plan_review":
                plan = connection.execute(
                    "SELECT plan_id FROM daily_plan WHERE session_id = ? AND status = 'active'",
                    (session_id,),
                ).fetchone()
                if plan:
                    candidate = (
                        "plan_review", plan["plan_id"],
                        "今天的计划已经排好。你可以直接说开始，也可以说先做哪一项。", 10,
                    )
            if not candidate:
                connection.rollback()
                return {"status": "none", "prompt": None}
            source_type, source_id, message, expiry_minutes = candidate
            previous = connection.execute(
                """
                SELECT * FROM agent_proactive_prompt
                WHERE session_id = ? AND source_type = ? AND source_id = ?
                """,
                (session_id, source_type, source_id),
            ).fetchone()
            if (previous and source_type in {'oral_supply_ready', 'oral_daily_supply_ready'} and previous['status'] == 'expired'
                    and not previous['acknowledged_at']):
                expires_at = (now + timedelta(minutes=expiry_minutes)).isoformat()
                connection.execute('''UPDATE agent_proactive_prompt SET status='delivered',expires_at=?,
                    last_attempt_at=?,delivery_attempts=delivery_attempts+1 WHERE prompt_id=?''',
                    (expires_at, now.isoformat(), previous['prompt_id']))
                connection.commit()
                return {'status':'retry_delivery','prompt':self._bind_proactive_prompt_state(
                    session['child_id'], {'prompt_id':previous['prompt_id'],
                    'source_type':source_type,'message':previous['message_text'],'expires_at':expires_at},
                    recovery=True)}
            if previous:
                connection.rollback()
                return {"status": "already_delivered", "prompt": None}
            prompt_id = str(uuid4())
            expires_at = (now + timedelta(minutes=expiry_minutes)).isoformat()
            connection.execute(
                """
                INSERT INTO agent_proactive_prompt(
                  prompt_id, child_id, session_id, source_type, source_id,
                  message_text, status, created_at, expires_at,
                  delivery_attempts, last_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'delivered', ?, ?, 1, ?)
                """,
                (
                    prompt_id, session["child_id"], session_id, source_type,
                    source_id, message, now.isoformat(), expires_at, now.isoformat(),
                ),
            )
            connection.commit()
            return {
                "status": "delivered",
                "prompt": self._bind_proactive_prompt_state(session["child_id"], {
                    "prompt_id": prompt_id, "source_type": source_type,
                    "message": message, "expires_at": expires_at,
                }),
            }

    def acknowledge_proactive_prompt(self, session_id: str, prompt_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                """
                SELECT * FROM agent_proactive_prompt
                WHERE prompt_id = ? AND session_id = ?
                """,
                (prompt_id, session_id),
            ).fetchone()
            if not row:
                raise KeyError(prompt_id)
            current = self._proactive_source_current(connection, row, now)
            if not current:
                connection.execute("UPDATE agent_proactive_prompt SET status='expired' WHERE prompt_id=?", (prompt_id,))
            if not row["acknowledged_at"]:
                # Preserve late playback receipts without reviving their source.
                connection.execute(
                    "UPDATE agent_proactive_prompt SET acknowledged_at = ? WHERE prompt_id = ?",
                    (now.isoformat(), prompt_id),
                )
            connection.commit()
        return {"prompt_id": prompt_id, "status": "acknowledged" if current else "expired"}

    def active_companion_activity(self, session_id: str) -> dict[str, Any] | None:
        self._ensure_companion_time_limits(session_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM companion_activity WHERE session_id = ? AND status = 'active' LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["remaining_turns"] = max(0, int(value["max_turns"]) - int(value["turn_count"]))
        return value

    def start_companion_activity(
        self,
        session_id: str,
        activity_type: str,
        topic: str,
        client_action_id: str,
        paused_plan_item_id: str | None = None,
        *, pause_before_create: bool = False, pause_active_task: bool = False,
    ) -> dict[str, Any]:
        if pause_before_create and pause_active_task:
            raise ValueError("COMPANION_START_PAUSE_MODE_CONFLICT")
        limits = {
            "interactive_story": (6, 10),
            "word_chain": (8, 8),
            "riddle": (5, 8),
            "movement": (2, 5),
            "tidy_challenge": (2, 5),
        }
        if activity_type not in limits:
            raise ValueError("unsupported companion activity")
        from .companion_activity_outcome import CompanionActivityOutcomeService
        outcomes = CompanionActivityOutcomeService(self.database)
        self._ensure_companion_time_limits(session_id)
        now = datetime.now(timezone.utc)
        max_turns, duration_minutes = limits[activity_type]
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def open_outcome(value):
                outcomes.open(value["child_id"], source_kind="companion", source_id=value["activity_id"],
                    intent="bounded_companion_activity",
                    success_description="记录孩子实际参与和感受；只有明确恢复意愿及原任务执行回执才能确认恢复，计时结束不代表成功。",
                    client_action_id=f"runtime-companion-open:{value['activity_id']}", connection=connection)

            replay = connection.execute(
                "SELECT activity_id FROM companion_activity WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if replay:
                value = dict(connection.execute(
                    "SELECT * FROM companion_activity WHERE activity_id = ?", (replay["activity_id"],)
                ).fetchone())
                if (value["session_id"] != session_id or value["activity_type"] != activity_type
                        or value["topic"] != topic[:200]
                        or (not (pause_before_create or pause_active_task)
                            and value["paused_plan_item_id"] != paused_plan_item_id)):
                    raise ValueError("COMPANION_START_IDEMPOTENCY_CONFLICT")
                open_outcome(value)
                connection.commit()
                value["remaining_turns"] = max(
                    0, int(value["max_turns"]) - int(value["turn_count"])
                )
                return value
            if pause_before_create:
                from .repository import StudyRepository
                binding = outcomes.inspect_companion_start(connection, session_id=session_id,
                    client_action_id=client_action_id, activity_type=activity_type, topic=topic,
                    expected_plan_item_id=paused_plan_item_id)
                paused_plan_item_id = binding['plan_item_id']
                if binding['requires_pause']:
                    StudyRepository(self.database).pause_plan_item(binding['plan_item_id'],
                        f'{client_action_id}:pause', 'companion_activity', connection=connection)
            elif pause_active_task:
                # The legacy HTTP entry has no voice provenance. Pause atomically,
                # without treating its task selection as a verified child request.
                from .repository import StudyRepository
                active = connection.execute('''SELECT pi.plan_item_id FROM plan_item pi
                    JOIN daily_plan dp USING(plan_id) JOIN daily_session ds ON ds.session_id=dp.session_id
                    WHERE dp.session_id=? AND dp.child_id=ds.child_id AND ds.status='active'
                      AND dp.status='active' AND pi.status='active' AND pi.source_type!='planned_break' ''',
                    (session_id,)).fetchall()
                if len(active) > 1:
                    raise ValueError('COMPANION_START_ACTIVE_TASK_AMBIGUOUS')
                paused_plan_item_id = active[0]['plan_item_id'] if active else None
                if paused_plan_item_id:
                    StudyRepository(self.database).pause_plan_item(paused_plan_item_id,
                        f'{client_action_id}:pause', 'companion_activity', connection=connection)
            connection.execute(
                """
                UPDATE companion_activity SET status = 'cancelled', ended_at = ?,
                       end_reason = 'switched_activity', updated_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (now.isoformat(), now.isoformat(), session_id),
            )
            session = connection.execute(
                "SELECT child_id FROM daily_session WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if not session:
                raise KeyError(session_id)
            activity_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO companion_activity(
                  activity_id, child_id, session_id, activity_type, topic, status,
                  turn_count, max_turns, duration_minutes, paused_plan_item_id,
                  started_at, ends_at, updated_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, 'active', 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activity_id, session["child_id"], session_id, activity_type, topic[:200],
                    max_turns, duration_minutes, paused_plan_item_id, now.isoformat(),
                    (now + timedelta(minutes=duration_minutes)).isoformat(), now.isoformat(),
                    client_action_id,
                ),
            )
            value = dict(connection.execute("SELECT * FROM companion_activity WHERE activity_id=?", (activity_id,)).fetchone())
            open_outcome(value)
            connection.commit()
        return self.active_companion_activity(session_id) or {}

    def snapshot_companion_activity_outcome(self, session_id: str) -> dict[str, Any]:
        """Freeze local candidates before planning; never choose by recency."""
        from .companion_activity_outcome import CompanionActivityOutcomeService
        self._ensure_companion_time_limits(session_id)
        service = CompanionActivityOutcomeService(self.database)
        with self.database.connect() as c:
            c.execute("BEGIN")
            session = c.execute("SELECT child_id FROM daily_session WHERE session_id=? AND status='active'", (session_id,)).fetchone()
            candidates = []
            active = []
            selection = None
            focus = None
            if session:
                for row in c.execute("SELECT outcome_id FROM companion_activity_outcome WHERE child_id=? AND session_id=?",
                                     (session["child_id"], session_id)).fetchall():
                    activity = service._detail(c, session["child_id"], row[0])["activity"]
                    if activity["source_valid"] and activity["resume"]["status"] not in {"confirmed", "deferred"}:
                        binding = {"outcome_id": row[0], "expected_revision": activity["revision"],
                                   "child_id": session["child_id"], "session_id": session_id}
                        source = c.execute("SELECT source_snapshot_json FROM companion_activity_outcome WHERE outcome_id=?", (row[0],)).fetchone()
                        saved = json.loads(source[0])
                        label = str(saved.get("topic") or saved.get("title") or activity["source_kind"])[:120]
                        candidates.append({"binding": binding, "label": label})
                        if activity["lifecycle"]["status"] == "active":
                            active.append(binding)
                prior = c.execute("""SELECT t.*,p.context_manifest_json,s.result_json,s.capability_name,
                    p.status AS plan_status,s.status AS step_status FROM agent_turn t
                    LEFT JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                    LEFT JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
                    WHERE t.session_id=? ORDER BY t.rowid DESC LIMIT 1""", (session_id,)).fetchone()
                if prior and prior["tool_status"] == "completed" and prior["plan_status"] != "cancelled" and prior["step_status"] != "cancelled":
                    manifest = json.loads(prior["context_manifest_json"] or "{}")
                    dialogue = manifest.get("companion_activity_selection")
                    if dialogue and dialogue.get("question") == prior["response_text"]:
                        selection = dialogue
                    desired = manifest.get("companion_activity_focus_binding")
                    if prior["capability_name"] == "companion.activity_feedback":
                        receipt = json.loads(prior["result_json"] or "{}").get("result") or {}
                        result_activity = receipt.get("activity") or {}
                        desired = {"outcome_id": result_activity.get("outcome_id"), "expected_revision": result_activity.get("revision"),
                                   "child_id": session["child_id"], "session_id": session_id}
                    focus = next((item["binding"] for item in candidates if item["binding"] == desired), None)
            chosen = focus or (active[0] if len(active) == 1 else None) or (candidates[0]["binding"] if len(candidates) == 1 else None)
        return {"status": "ready" if chosen else "ambiguous" if candidates else "missing",
                "binding": chosen, "candidates": candidates, "selection": selection,
                "local_only": True, "cloud_transmission_allowed": False}

    def advance_companion_activity(self, session_id: str) -> dict[str, Any] | None:
        ended = self._ensure_companion_time_limits(session_id)
        if ended:
            return ended[0]
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM companion_activity WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            turns = int(row["turn_count"]) + 1
            completed = turns >= int(row["max_turns"])
            connection.execute(
                """
                UPDATE companion_activity SET turn_count = ?, status = ?, updated_at = ?,
                       ended_at = ?, end_reason = ? WHERE activity_id = ?
                """,
                (
                    turns, "completed" if completed else "active", now.isoformat(),
                    now.isoformat() if completed else None,
                    "turn_limit" if completed else None,
                    row["activity_id"],
                ),
            )
            connection.commit()
        result = dict(row)
        result.update({"turn_count": turns, "status": "completed" if completed else "active", "natural_end": completed})
        return result

    def end_companion_activity(self, session_id: str, reason: str = "child_requested") -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM companion_activity WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """
                UPDATE companion_activity SET status = 'completed', ended_at = ?,
                       end_reason = ?, updated_at = ? WHERE activity_id = ?
                """,
                (now, reason[:80], now, row["activity_id"]),
            )
            connection.commit()
        return {**dict(row), "status": "completed", "ended_at": now, "end_reason": reason[:80]}

    def assemble_context(
        self,
        session_id: str,
        recent_dialogue: tuple[dict[str, str], ...],
    ) -> dict[str, Any]:
        self._ensure_companion_time_limits(session_id)
        with self.database.connect() as connection:
            session = connection.execute(
                """
                SELECT ds.session_id, ds.child_id, ds.state, ds.status, ds.observation_status,
                       ds.homework_declared_empty,
                       cp.nickname, cp.grade, cp.curriculum_region,
                       spc.energy_level, spc.fixed_commitment_minutes,
                       spc.tomorrow_event_subject, spc.tomorrow_event_title
                FROM daily_session ds
                JOIN child_profile cp ON cp.child_id = ds.child_id
                LEFT JOIN session_planning_context spc ON spc.session_id = ds.session_id
                WHERE ds.session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session:
                raise KeyError(session_id)
            daily_planning_state = {
                "saved_homework_count": connection.execute(
                    "SELECT COUNT(*) FROM homework_item WHERE session_id=? AND child_id=? AND status='pending'",
                    (session_id, session["child_id"]),
                ).fetchone()[0],
                "homework_declared_empty": bool(session["homework_declared_empty"]),
                "active_plan_count": connection.execute(
                    "SELECT COUNT(*) FROM daily_plan WHERE session_id=? AND child_id=? AND status='active'",
                    (session_id, session["child_id"]),
                ).fetchone()[0],
            }
            learning_outcomes = self.reconcile_learning_state(session["child_id"], connection=connection)
            connection.commit()
            tutor_playback = None
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='tutor_playback_checkpoint'").fetchone():
                tutor_playback = connection.execute("""SELECT status,segment_index,segment_started,
                    json_array_length(presentation_json,'$.segments') AS segment_count
                    FROM tutor_playback_checkpoint WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1""",
                    (session_id, session["child_id"])).fetchone()
            active_objective = connection.execute(
                """
                SELECT objective_id, objective_type, summary, success_criteria_json, phase,
                       status, updated_at
                FROM agent_objective
                WHERE child_id = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (session["child_id"],),
            ).fetchone()
            active_task = connection.execute(
                """
                SELECT item.plan_item_id, item.subject, item.title, item.task_type,
                       item.expected_minutes, item.status
                FROM plan_item item
                JOIN daily_plan plan ON plan.plan_id = item.plan_id
                WHERE plan.session_id = ? AND item.status IN ('active', 'paused', 'pending')
                ORDER BY CASE item.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                         item.position LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            plan_counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN item.status = 'completed' THEN 1 ELSE 0 END) AS completed
                FROM plan_item item JOIN daily_plan plan ON plan.plan_id = item.plan_id
                WHERE plan.session_id = ? AND item.source_type != 'planned_break'
                """,
                (session_id,),
            ).fetchone()
            pending_intervention = connection.execute(
                """
                SELECT stage, message_text, status FROM intervention_record
                WHERE child_id = ? AND session_id = ? AND status IN ('proposed', 'delivered', 'pending')
                ORDER BY created_at DESC LIMIT 1
                """,
                (session["child_id"], session_id),
            ).fetchone()
            latest_camera_event = connection.execute(
                """
                SELECT event_type, confidence, occurred_at
                FROM observable_event
                WHERE session_id = ? AND source = 'local_vision'
                ORDER BY occurred_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            document_tables_ready = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'document_monitor_session'"
            ).fetchone()
            document_event_table_ready = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'document_monitor_event'"
            ).fetchone()
            document_monitor = (
                connection.execute(
                    """
                    SELECT status, current_state FROM document_monitor_session
                    WHERE session_id = ? LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if document_tables_ready else None
            )
            latest_document_event = (
                connection.execute(
                    """
                    SELECT event_type, confidence, observed_at
                    FROM document_monitor_event
                    WHERE session_id = ?
                    ORDER BY observed_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if document_event_table_ready else None
            )
            due_commitments = connection.execute(
                """
                SELECT commitment_id, commitment_type, due_at, payload_json
                FROM agent_commitment
                WHERE child_id = ? AND status IN ('pending', 'due')
                ORDER BY due_at LIMIT 5
                """,
                (session["child_id"],),
            ).fetchall()
            active_companion_activity = connection.execute(
                """
                SELECT activity_id, activity_type, topic, turn_count, max_turns,
                       duration_minutes, started_at, ends_at
                FROM companion_activity
                WHERE session_id = ? AND status = 'active' LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            habit_experiment = connection.execute(
                """SELECT experiment_id,title,child_action,success_signal,duration_days,
                          status,started_at,ends_at,outcome_json
                   FROM habit_experiment
                   WHERE child_id=? AND status IN ('proposed','active')
                   ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, proposed_at DESC LIMIT 1""",
                (session["child_id"],),
            ).fetchone()
        longitudinal_model = ChildModelService(self.database).persisted_compact_for_agent(
            session["child_id"]
        )
        # Keep the entire derived model local, including legacy aggregate labels.
        # Planning consumes the same qualified projection as the current child model.
        longitudinal_model["cloud_transmission_allowed"] = False
        feedback = longitudinal_model["learning_feedback"]
        learning_context = {
            **feedback,
            "cloud_transmission_allowed": False,
            "scope": "local_only",
            "requires_independent_check": True,
            "objective_completion_allowed": False,
            "next_action": feedback["nodes"][0]["next_action"] if feedback["nodes"] else "verify_then_independent_check",
            "decision_boundary": "核实有效证据并独立检查；规划器不能推定掌握或完成学习目标。",
        }
        objective = dict(active_objective) if active_objective else None
        if objective:
            objective["success_criteria"] = json.loads(objective.pop("success_criteria_json"))
            from .learning_rubric import evaluation_source_boundary
            with self.database.connect() as connection:
                boundary = evaluation_source_boundary(connection, session["child_id"], objective_id=objective["objective_id"])
            if boundary["cloud_evaluator_blocked"]:
                objective.update(local_only=True, cloud_transmission_allowed=False)
        pending_checks = self.pending_task_completion_check(session_id)
        commitment_context = []
        for row in due_commitments:
            value = dict(row)
            try:
                payload = json.loads(value['payload_json'])
                if not isinstance(payload, dict):
                    raise ValueError('commitment payload must be an object')
                value['payload'] = payload
            except (ValueError, TypeError):
                value.pop('payload_json', None)
                value.update(payload=None, source_status='unavailable', local_only=True,
                             cloud_transmission_allowed=False)
            commitment_context.append(value)
        return {
            "learning_outcomes": {"items": learning_outcomes, "local_only": True, "cloud_transmission_allowed": False},
            "session": {
                "session_id": session["session_id"],
                "state": session["state"],
                "status": session["status"],
                "observation_status": session["observation_status"],
            },
            "child": {
                "child_id": session["child_id"],
                "nickname": session["nickname"],
                "grade": session["grade"],
                "curriculum_region": session["curriculum_region"],
            },
            "planning_constraints": {
                "energy_level": session["energy_level"],
                "fixed_commitment_minutes": session["fixed_commitment_minutes"],
                "tomorrow_event_subject": session["tomorrow_event_subject"],
                "tomorrow_event_title": session["tomorrow_event_title"],
            },
            "daily_planning_state": daily_planning_state,
            "active_objective": objective,
            "tutor_playback": dict(tutor_playback) if tutor_playback else None,
            "longitudinal_child_model": longitudinal_model,
            "active_task": dict(active_task) if active_task else None,
            "plan_progress": {
                "total": int(plan_counts["total"] or 0),
                "completed": int(plan_counts["completed"] or 0),
            },
            "relevant_learning_state": learning_context,
            "pending_intervention": dict(pending_intervention) if pending_intervention else None,
            "pending_task_completion_check": (
                {key: pending_checks[key] for key in ("status", "required_checks", "confirmed_checks", "missing_checks")
                 if key in pending_checks} if pending_checks else None
            ),
            "device_status": self.device_status(session_id),
            "document_guidance_preferences": self.document_guidance_preferences(session_id),
            # Only compact local inferences enter orchestration. Frames, OCR
            # text, page hashes and device identifiers stay outside the model
            # context and remain available to their dedicated local workflows.
            "sensor_state": {
                "camera": {
                    "status": session["observation_status"],
                    "latest_event_type": latest_camera_event["event_type"] if latest_camera_event else None,
                    "confidence": float(latest_camera_event["confidence"]) if latest_camera_event else None,
                    "observed_at": latest_camera_event["occurred_at"] if latest_camera_event else None,
                },
                "document_camera": {
                    "status": document_monitor["status"] if document_monitor else "inactive",
                    "current_state": document_monitor["current_state"] if document_monitor else None,
                    "latest_event_type": latest_document_event["event_type"] if latest_document_event else None,
                    "confidence": float(latest_document_event["confidence"]) if latest_document_event else None,
                    "observed_at": latest_document_event["observed_at"] if latest_document_event else None,
                },
            },
            "due_commitments": commitment_context,
            "active_companion_activity": (
                {
                    **dict(active_companion_activity),
                    "remaining_turns": max(
                        0,
                        int(active_companion_activity["max_turns"]) - int(active_companion_activity["turn_count"]),
                    ),
                }
                if active_companion_activity else None
            ),
            "habit_experiment": (
                {
                    **{key: value for key, value in dict(habit_experiment).items() if key != "outcome_json"},
                    "outcome": json.loads(habit_experiment["outcome_json"])
                    if habit_experiment["outcome_json"] else None,
                }
                if habit_experiment else None
            ),
            "recent_dialogue": list(recent_dialogue[-8:]),
        }

    def document_guidance_preferences(self, session_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            session = connection.execute("SELECT child_id FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
            if not session:
                raise ValueError("SESSION_NOT_FOUND")
            row = connection.execute(
                """SELECT ast.result_json FROM agent_step ast JOIN agent_plan ap ON ap.plan_id=ast.plan_id
                   JOIN agent_turn at ON at.agent_turn_id=ap.agent_turn_id
                   JOIN daily_session ds ON ds.session_id=ap.session_id AND ds.child_id=ap.child_id
                   WHERE ap.child_id=? AND at.child_id=ap.child_id AND at.session_id=ap.session_id
                     AND ast.capability_name='document.set_occlusion_reminders' AND ast.status='completed'
                     AND json_extract(ast.result_json,'$.result.settings_saved')=1
                     AND json_type(ast.result_json,'$.result.occlusion_reminders_enabled') IN ('true','false')
                   ORDER BY at.rowid DESC LIMIT 1""", (session["child_id"],),
            ).fetchone()
        value = json.loads(row["result_json"])["result"] if row else {}
        return {"child_id": session["child_id"], "occlusion_reminders_enabled": value.get("occlusion_reminders_enabled", True),
                "settings_saved": bool(row), "source": "saved" if row else "default"}

    def device_status(self, session_id: str) -> dict[str, Any]:
        """Frame-free, last-known records only; live visibility belongs to the device bridge."""
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            session = connection.execute("SELECT child_id,observation_status FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
            if not session:
                raise ValueError("SESSION_NOT_FOUND")
            owner = (session_id, session["child_id"])
            qualification = connection.execute(
                "SELECT source,status,scene_quality,updated_at FROM observation_qualification WHERE session_id=? AND child_id=?", owner,
            ).fetchone()
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            monitor = connection.execute(
                "SELECT status,current_state FROM document_monitor_session WHERE session_id=? AND child_id=?", owner,
            ).fetchone() if "document_monitor_session" in tables else None
            event = connection.execute(
                """SELECT observed_at FROM document_monitor_event WHERE session_id=? AND child_id=?
                   AND event_type!='capture_failed' ORDER BY observed_at DESC LIMIT 1""", owner,
            ).fetchone() if "document_monitor_event" in tables else None
            result = {"source": "last_known"}
            for device in ("camera", "document_camera"):
                leases = connection.execute(
                    "SELECT expires_at FROM sensor_lease WHERE session_id=? AND device_type=? AND status='active'",
                    (session_id, device),
                ).fetchall()
                lease_active = any(_valid_recent_time(row["expires_at"], now, future=True) for row in leases)
                incident = connection.execute(
                    """SELECT error_code FROM device_incident WHERE session_id=? AND child_id=? AND device_type=?
                       AND status='open' ORDER BY occurred_at DESC LIMIT 1""", (*owner, device),
                ).fetchone()
                last_frame = (qualification["updated_at"] if qualification and qualification["source"] == "engine"
                              and qualification["status"] in {"qualified", "unqualified"}
                              and qualification["scene_quality"] != "not_evaluated" else None) if device == "camera" else (
                                  event["observed_at"] if event else None)
                code = incident["error_code"] if incident else None
                allowed_codes = {"CAMERA_UNAVAILABLE", "DEVICE_NOT_ACTIVE", "DEVICE_DISCONNECTED", "DEVICE_BUSY",
                                 "CAMERA_PERMISSION_DENIED", "DEVICE_PERMISSION_DENIED", "FRAME_TIMEOUT", "STREAM_ENDED"}
                result[device] = {
                    "current_visibility": "unknown", "physical_power": "unknown", "lease_active": lease_active,
                    "requested_state": session["observation_status"] if device == "camera" else None,
                    "monitor_status": (session["observation_status"] if device == "camera" else monitor["status"] if monitor else "not_started"),
                    "monitor_active": None,
                    "last_frame_at": last_frame,
                    "frame_recently_recorded": _valid_recent_time(last_frame, now),
                    "last_error_code": code if code in allowed_codes else "DEVICE_ERROR_RECORDED" if code else None,
                }
        return result

    def pending_task_completion_check(self, session_id: str, before_turn_id: str | None = None) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT ap.plan_id, ast.step_id, ast.result_json
                   FROM agent_step ast JOIN agent_plan ap ON ap.plan_id=ast.plan_id
                   JOIN agent_turn asked ON asked.agent_turn_id=ap.agent_turn_id
                   JOIN daily_session ds ON ds.session_id=ap.session_id AND ds.child_id=ap.child_id
                   JOIN plan_item pi ON pi.plan_item_id=json_extract(ast.result_json,'$.result.plan_item_id')
                   JOIN daily_plan dp ON dp.plan_id=pi.plan_id AND dp.session_id=ds.session_id AND dp.child_id=ds.child_id
                   WHERE ap.session_id=? AND ds.status='active' AND pi.status='active'
                     AND pi.started_at IS json_extract(ast.result_json,'$.result.task_started_at')
                     AND ast.capability_name='task.complete' AND ast.status='completed' AND ap.status='waiting_child'
                     AND json_extract(ast.result_json,'$.result.status')='awaiting_completion_check'
                     AND (? IS NULL OR asked.rowid < (SELECT rowid FROM agent_turn WHERE agent_turn_id=?))
                   ORDER BY asked.rowid DESC LIMIT 1""", (session_id, before_turn_id, before_turn_id),
            ).fetchone()
        if not row:
            return None
        value = json.loads(row["result_json"])["result"]
        return {**value, "plan_id": row["plan_id"], "step_id": row["step_id"]}

    def pending_child_reply(self, session_id: str) -> dict[str, Any] | None:
        """Return the latest durable learning turn that is waiting for the child.

        This is local orchestration state, not cloud context. It binds a child's
        next utterance to the goal that asked for it, even after an app or backend
        restart, instead of asking a stateless intent classifier to rediscover the
        conversation phase from a short text window.
        """
        with self.database.connect() as connection:
            has_prompts = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone()
            has_vocabulary = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_vocabulary_prompt_session'").fetchone()
            has_exams = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='specialized_exam_run'").fetchone()
        typed = []
        saved_answer = False
        if has_prompts:
            from .diagnostic_prompt import DiagnosticPromptService
            prompt_result = DiagnosticPromptService(self.database).read(session_id)
            if prompt_result["status"] == "waiting_answer":
                prompt = prompt_result["prompt"]
                typed.append({"objective_id": None, "summary": "当前独立诊断", "success_criteria": [],
                        "phase": "check", "plan_id": None, "plan_status": "waiting_child",
                        "capability_name": "diagnostic.start", "arguments": {"track": prompt["item"]["track"]},
                        "expected_evidence": ["diagnostic_attempt"], "step_status": "completed",
                        "result": {"prompt_id": prompt["prompt_id"], "item_version": prompt["item_version"]}})
            if prompt_result["status"] == "answer_saved":
                saved_answer = True
        if has_vocabulary:
            from .pet_vocabulary import PetVocabularyService
            vocabulary = PetVocabularyService(self.database).active_prompt(session_id)
            if vocabulary:
                typed.append({"objective_id": None, "summary": "当前词汇复习", "success_criteria": [],
                              "phase": "check", "plan_id": None, "plan_status": "waiting_child",
                              "capability_name": "pet_vocabulary.start", "arguments": {},
                              "expected_evidence": ["vocabulary_attempt"], "step_status": "completed",
                              "result": {"prompt_session_id": vocabulary["prompt_session_id"], "card_version": vocabulary["card_version"]}})
        if has_exams:
            from .exam_simulation import ExamSimulationService
            exam = ExamSimulationService(self.database).arrival_binding(session_id)
            if exam:
                typed.append({"objective_id": None, "summary": "当前模拟卷", "success_criteria": [],
                              "phase": "check", "plan_id": None, "plan_status": "waiting_child",
                              "capability_name": "exam.start", "arguments": {},
                              "expected_evidence": ["diagnostic_attempt"], "step_status": "completed", "result": exam})
        if len(typed) > 1:
            with self.database.connect() as connection:
                selected = connection.execute(
                    """SELECT json_extract(ap.plan_json,'$.pending_input_binding') AS binding
                       FROM agent_plan ap JOIN agent_turn at ON at.agent_turn_id=ap.agent_turn_id
                       JOIN agent_step ast ON ast.plan_id=ap.plan_id AND ast.step_index=0
                       WHERE ap.session_id=? AND ast.status='completed'
                         AND json_type(ap.plan_json,'$.pending_input_binding')='object'
                       ORDER BY at.rowid DESC LIMIT 1""", (session_id,),
                ).fetchone()
            if selected:
                binding = json.loads(selected["binding"])
                focused = next((item for item in typed if item["capability_name"] == binding.get("capability_name")
                                and item["result"] == binding.get("result")), None)
                if focused:
                    return focused
            return {"capability_name": "input.choose_prompt", "phase": "clarify", "conflicting_owners": [item["capability_name"] for item in typed]}
        if typed:
            return typed[0]
        if saved_answer:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT ao.objective_id, ao.summary, ao.success_criteria_json, ao.phase,
                       ap.plan_id, ap.agent_turn_id AS source_agent_turn_id, ap.status AS plan_status, ast.capability_name,
                       ast.arguments_json, ast.expected_evidence_json, ast.result_json,
                       ast.status AS step_status
                FROM agent_plan ap
                JOIN agent_step ast ON ast.plan_id = ap.plan_id AND ast.step_index = 0
                JOIN agent_objective ao ON ao.objective_id = ap.objective_id
                WHERE ap.session_id = ? AND ap.status = 'waiting_child'
                  AND ao.status = 'active' AND ao.objective_type = 'learning'
                ORDER BY ap.updated_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not row:
                row = connection.execute(
                    """
                    SELECT ao.objective_id, ao.summary, ao.success_criteria_json, ao.phase,
                           NULL AS plan_id, 'waiting_child' AS plan_status,
                           'learning.request_response' AS capability_name,
                           json_object(
                             'subject', COALESCE(json_extract(ac.payload_json, '$.subject'), '其他')
                           ) AS arguments_json,
                           '[]' AS expected_evidence_json,
                           json_object(
                             'result', json_object(
                               'next_check', COALESCE(json_extract(ac.payload_json, '$.prompt'), '')
                             )
                           ) AS result_json,
                           'completed' AS step_status
                    FROM agent_objective ao
                    JOIN agent_commitment ac ON ac.objective_id = ao.objective_id
                    JOIN plan_item pi ON pi.source_type = 'agent_retention'
                                     AND pi.source_ref = ac.commitment_id
                    JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                    WHERE dp.session_id = ? AND pi.status = 'active'
                      AND ao.status = 'active' AND ao.objective_type = 'learning'
                      AND ao.phase IN ('check','transfer_check','retention_check') AND ac.status = 'due'
                      AND ac.child_id=ao.child_id AND dp.child_id=ao.child_id
                    ORDER BY pi.started_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
        if not row:
            return None
        value = dict(row)
        if value["capability_name"] in {"diagnostic.start", "diagnostic.replay", "diagnostic.submit", "pet_vocabulary.start", "pet_vocabulary.submit"}:
            return None
        value["success_criteria"] = json.loads(value.pop("success_criteria_json"))
        value["arguments"] = json.loads(value.pop("arguments_json"))
        value["expected_evidence"] = json.loads(value.pop("expected_evidence_json"))
        value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
        return value

    @staticmethod
    def _neutral_query_plan(decision: AgentPlanDecision) -> AgentPlanDecision:
        spoken = '我查看一下当前安排。' if decision.step.capability.name == 'planning.describe' else '我查看一下设备状态。'
        return replace(decision, objective=ObjectiveDraft('none', None, None, (), 'execute'),
                       spoken_transition=spoken)

    def constrain_learning_plan(
        self, decision: AgentPlanDecision, context: dict[str, Any], registry: CapabilityRegistry,
    ) -> AgentPlanDecision:
        if decision.step.capability.name in OBJECTIVE_NEUTRAL_CAPABILITIES:
            return self._neutral_query_plan(decision)
        active = context.get("active_objective")
        if not _requests_learning_completion(decision, active):
            return decision
        continuing = bool(active and decision.objective.mode != "new")
        summary = (active["summary"] if continuing else decision.objective.summary) or "当前学习问题"
        criteria = tuple(active["success_criteria"]) if continuing else decision.objective.success_criteria
        phase = active["phase"] if continuing and active["phase"] in {"check", "transfer_check", "retention_check"} else "check"
        prompt = f"先独立核实一下：关于“{summary}”，请用自己的话说明关键步骤和依据。"
        return AgentPlanDecision(
            objective=ObjectiveDraft("continue" if continuing else "new", "learning", summary,
                                     criteria or ("独立回答核实问题",), phase),
            step=PlannedStep(
                capability=registry.require("learning.request_response", context["session"]["state"]),
                arguments={"prompt": prompt, "subject": str(decision.step.arguments.get("subject")
                           or (context.get("active_task") or {}).get("subject") or "其他")},
                reason="学习目标完成必须由本地结果评估确认，不能采用规划器的完成判断。",
                expected_evidence=("child_check_response",),
            ),
            spoken_transition=prompt, requires_child_reply=True, confidence=decision.confidence,
        )

    @staticmethod
    def resolve_oral_followup(c, *, child_id, session_id, prompt_id, item_version, kind,
                              _diagnostic_item_id=None, _prepared_prompt_id=None, _history_before=None):
        """Read a source-bound practice candidate, never authorize or create it."""
        base = {'status':'blocked','reason':None,'local_only':True,'cloud_transmission_allowed':False,
                'requires_runtime_admission':True,'requires_new_prompt_and_capture':True}
        if not c.in_transaction:
            raise InvalidAgentPlanError('ANSWER_ORAL_READ_TRANSACTION_REQUIRED')
        if kind not in {'retry','independent_check'}:
            return {**base,'reason':'oral_followup_kind_invalid'}
        session = c.execute('''SELECT ds.status,ds.state,cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp USING(child_id) WHERE ds.session_id=? AND ds.child_id=?''',
            (session_id,child_id)).fetchone()
        focus = c.execute('SELECT prompt_id,item_version FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=? ORDER BY rowid DESC LIMIT 1',
            (child_id,session_id)).fetchone()
        if (not session or session['status']!='active' or session['child_status']!='active'
                or session['state'] in {'evolving','completed'} or not focus
                or (_history_before is None and focus['prompt_id'] != _prepared_prompt_id
                    and (focus['prompt_id'],focus['item_version'])!=(prompt_id,item_version))):
            return {**base,'reason':'oral_current_focus_changed'}
        return AgentRuntimeStore._resolve_oral_followup_source(c, child_id=child_id,
            session_id=session_id, prompt_id=prompt_id, item_version=item_version, kind=kind,
            _diagnostic_item_id=_diagnostic_item_id, _prepared_prompt_id=_prepared_prompt_id,
            _history_before=_history_before)

    @staticmethod
    def _resolve_oral_followup_source(c, *, child_id, session_id, prompt_id, item_version, kind,
                                      _diagnostic_item_id=None, _prepared_prompt_id=None, _history_before=None):
        """Source evidence only; live callers must separately admit the current session."""
        from .diagnostic_prompt import DiagnosticPromptService, _version
        from .local_supply import digest
        from .oral_teaching import project_oral_teaching
        from .specialized_stages import _item_source
        from .learning_obligations import question_identity, project_item_exposure
        base = {'status':'blocked','reason':None,'local_only':True,'cloud_transmission_allowed':False,
                'requires_runtime_admission':True,'requires_new_prompt_and_capture':True}
        if not c.in_transaction:
            raise InvalidAgentPlanError('ANSWER_ORAL_READ_TRANSACTION_REQUIRED')
        if kind not in {'retry','independent_check'}:
            return {**base,'reason':'oral_followup_kind_invalid'}
        try:
            original = project_oral_teaching(c,child_id,session_id,prompt_id,item_version)
        except ValueError:
            return {**base,'reason':'oral_original_scope_unavailable'}
        if original['status'] not in {'content_self_check','reviewed_audio_feedback','formative_audio_feedback'}:
            return {**base,'reason':original['reason'] or 'oral_original_answer_required'}
        roots = c.execute('''SELECT DISTINCT root.* FROM agent_plan origin JOIN agent_plan root
            ON root.objective_id=origin.objective_id AND root.child_id=origin.child_id AND root.session_id=origin.session_id
            WHERE origin.child_id=? AND origin.session_id=?
            AND json_extract(origin.plan_json,'$.local_learning_binding.prompt_id')=?
            AND json_type(root.context_manifest_json,'$.scheduled_writing_goal')='object' ''',
            (child_id,session_id,prompt_id)).fetchall()
        for root in roots:
            from .scheduled_writing_goal import validate_origin
            root_id = json.loads(root['plan_json']).get('local_learning_binding',{}).get('prompt_id')
            root_prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
                (root_id,child_id,session_id)).fetchone()
            try:
                if not root_prompt:
                    raise ValueError('oral_scheduled_original_missing')
                validate_origin(c,root,root_prompt)
            except ValueError as error:
                return {**base,'reason':str(error)}
        parent = DiagnosticPromptService._item(c,child_id,original['diagnostic_item_id'])
        if not parent:
            return {**base,'reason':'oral_source_unavailable'}
        family = json.loads(parent['expected_answer_json']).get('task_family')
        if kind == 'retry':
            ids = [original['diagnostic_item_id']]
            if _diagnostic_item_id not in (None,ids[0]):
                return {**base,'reason':'oral_retry_requires_original_question'}
        elif not family:
            return {**base,'reason':'oral_task_family_unknown'}
        elif _diagnostic_item_id is not None:
            ids = [_diagnostic_item_id]
        else:
            ids = [row[0] for row in c.execute('''SELECT diagnostic_item_id FROM diagnostic_item
                WHERE knowledge_node_id=? AND answer_type=? AND status='active' ORDER BY diagnostic_item_id''',
                (parent['knowledge_node_id'],parent['answer_type']))]
        now = datetime.now(timezone.utc)
        parent_identity = None
        shown_identities = set()
        if kind == 'independent_check':
            from .diagnostic_history_identity import historical_identity, HistoryIdentityUnavailable
            parent_identity = question_identity(parent, connection=c)
            # Freeze all previously displayed material once, independent of its
            # present source permission. A damaged public copy may still have a
            # verified seal; unprovable history must not become new-question proof.
            shown = c.execute('''SELECT * FROM diagnostic_prompt WHERE child_id=?
                AND (? IS NULL OR prompt_id!=?) AND (? IS NULL OR created_at<?)''',
                (child_id,_prepared_prompt_id,_prepared_prompt_id,_history_before,_history_before))
            try:
                for row in shown:
                    shown_identities.add(historical_identity(c,child_id,row,None))
            except HistoryIdentityUnavailable:
                return {**base,'reason':'oral_exposure_history_unavailable'}
        for item_id in ids:
            item = DiagnosticPromptService._item(c,child_id,item_id)
            if (not item or item['answer_type']!=parent['answer_type']
                    or item['assessment_mode']!=parent['assessment_mode']
                    or item['knowledge_node_id']!=parent['knowledge_node_id']):
                continue
            source, _ = _item_source(c,child_id,item,now)
            if not source or not source['score_qualified']:
                continue
            if kind == 'independent_check':
                if json.loads(item['expected_answer_json']).get('task_family') != family:
                    continue
                identity = question_identity(item, connection=c)
                if identity==parent_identity or identity in shown_identities:
                    continue
                exposure = project_item_exposure(c,child_id,item_id,as_of=_history_before or now.isoformat())
                if exposure['answer_repeated'] or exposure['tutor_assisted']:
                    continue
            action = {'version':'oral_followup_v1','kind':kind,'child_id':child_id,'session_id':session_id,
                'parent_prompt_id':prompt_id,'parent_item_version':item_version,
                'parent_source_version':original['source_version'],'original':original['original'],
                'feedback_version':original['feedback_version'],'review_ref':original['review_ref'],
                'audio_work_ref':original.get('audio_work_ref'),
                'diagnostic_item_id':item_id,'item_version':_version(item),'source_version':source['source_version'],
                'knowledge_node_id':item['knowledge_node_id'],'task_points':json.loads(item['expected_answer_json'])['task_points'],
                'required_hint_level':1 if kind=='retry' else 0,'independent_check':kind=='independent_check',
                'mastery_claim_allowed':False}
            return {**base,'status':'ready','action':action,'binding_version':digest(action)}
        return {**base,'reason':'oral_no_unexposed_source' if kind=='independent_check' else 'oral_source_unavailable'}

    @staticmethod
    def revalidate_oral_followup(c, *, action, binding_version, _prepared_prompt_id=None, _history_before=None):
        from .local_supply import digest
        blocked = {'status':'blocked','reason':'oral_followup_binding_changed',
                   'local_only':True,'cloud_transmission_allowed':False}
        if not isinstance(action,dict) or action.get('version')!='oral_followup_v1' or digest(action)!=binding_version:
            return blocked
        try:
            current = AgentRuntimeStore.resolve_oral_followup(c,child_id=action['child_id'],session_id=action['session_id'],
                prompt_id=action['parent_prompt_id'],item_version=action['parent_item_version'],kind=action['kind'],
                _diagnostic_item_id=action['diagnostic_item_id'], _prepared_prompt_id=_prepared_prompt_id,
                _history_before=_history_before)
        except (KeyError,ValueError):
            return blocked
        return current if current.get('action')==action and current.get('binding_version')==binding_version else blocked

    @staticmethod
    def capture_oral_followup_arrival(c, arrival_id, voice_capture):
        from .voice_control_intent import VoiceControlIntent
        arrival = c.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=?',(arrival_id,)).fetchone()
        items = json.loads(arrival['snapshot_json'])
        old = next((x for x in items if x.get('kind')=='oral_followup_arrival'),None)
        if old is not None:
            return old['record']
        snapshot = None
        parent = c.execute('''SELECT * FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=?
            ORDER BY rowid DESC LIMIT 1''',(arrival['child_id'],arrival['session_id'])).fetchone() if c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt' AND type='table'").fetchone() else None
        captured = AgentRuntimeStore.captured_question(voice_capture or {})
        if (parent and parent['status']=='answered' and json.loads(parent['item_json']).get('answer_type') in {'speaking','read_aloud','open_response'}
                and (not voice_capture or (captured and captured['kind']=='diagnostic' and captured['binding']==
                    {'prompt_id':parent['prompt_id'],'item_version':parent['item_version']}))):
            origins = c.execute('''SELECT DISTINCT p.objective_id FROM agent_plan p
                JOIN agent_objective o USING(objective_id) WHERE p.child_id=? AND p.session_id=?
                AND o.child_id=p.child_id AND o.session_id=p.session_id AND o.status='active' AND o.objective_type='learning'
                AND json_extract(p.plan_json,'$.local_learning_binding.prompt_id')=?
                AND json_extract(p.plan_json,'$.local_learning_binding.item_version')=?''',
                (arrival['child_id'],arrival['session_id'],parent['prompt_id'],parent['item_version'])).fetchall()
            if len(origins)==1:
                packets = {kind:AgentRuntimeStore.resolve_oral_followup(c,child_id=arrival['child_id'],session_id=arrival['session_id'],
                    prompt_id=parent['prompt_id'],item_version=parent['item_version'],kind=kind) for kind in ('retry','independent_check')}
                if packets['retry']['status']=='ready':
                    snapshot = {'objective_id':origins[0][0],'parent':{'prompt_id':parent['prompt_id'],'item_version':parent['item_version']},
                        'packets':packets,'consent_version':VoiceControlIntent.text_consent_version(c),
                        'local_only':True,'cloud_transmission_allowed':False}
        items.append({'kind':'oral_followup_arrival','record':snapshot,'local_only':True,'cloud_transmission_allowed':False})
        c.execute('UPDATE learning_input_arrival SET snapshot_json=? WHERE arrival_id=?',(json.dumps(items,ensure_ascii=False),arrival_id))
        return snapshot

    @staticmethod
    def _validate_oral_followup_authority(c, turn, action, manifest, *, prepared=None, historical=False):
        from .voice_control_intent import VoiceControlIntent
        frozen = manifest.get('voice_control_binding') or {}
        control = {'retry_oral_answer':'oral.retry','check_oral_independently':'oral.independent_check'}.get(action.get('type'))
        kind = 'retry' if control=='oral.retry' else 'independent_check'
        from .realtime_native_special_sources import source_witness
        native = source_witness(c, turn, action, manifest, control, current=not historical)
        packet = action.get('oral_followup_binding') or {}
        arrival = c.execute('''SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=? AND client_action_id=?''',
            (manifest.get('learning_input_arrival_id'),turn['child_id'],turn['session_id'],turn['client_action_id'])).fetchone()
        snapshot = next((x.get('record') for x in json.loads(arrival['snapshot_json']) if x.get('kind')=='oral_followup_arrival'),None) if arrival else None
        expected = ((snapshot or {}).get('packets') or {}).get(kind) or {}
        receipt = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',(frozen.get('request_key'),)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([turn['child_id'],turn['session_id'],turn['transcript']],ensure_ascii=False).encode()).hexdigest()
        if (not c.in_transaction or not control or turn['intent']=='runtime_continuation' or not arrival or not snapshot
                or arrival['utterance']!=turn['transcript'] or action.get('request_quote')!=turn['transcript']
                or action.get('local_only') is not True or action.get('cloud_transmission_allowed') is not False
                or set(packet)!={'action','binding_version'} or expected.get('status')!='ready'
                or packet!={k:expected[k] for k in ('action','binding_version')}
                or (native is None and (not receipt or receipt['status']!='completed' or receipt['fingerprint']!=fingerprint
                    or (receipt['child_id'],receipt['session_id'])!=(turn['child_id'],turn['session_id'])
                    or frozen.get('control')!=control or json.loads(receipt['decision_json'])!={'status':'control','control':control}
                    or json.loads(receipt['binding_json'])!=frozen.get('targets')
                    or frozen['targets'].get('oral_followups')!=snapshot['packets']))):
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_AUTHORITY_CHANGED')
        if native is None and VoiceControlIntent.parse(json.dumps({'role':'control','control':control,'quote':turn['transcript'],
                'confidence':1}),turn['transcript'])!={'status':'control','control':control}:
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_COMMAND_CHANGED')
        goal = c.execute('''SELECT o.* FROM agent_objective o JOIN daily_session ds ON ds.session_id=o.session_id AND ds.child_id=o.child_id
            JOIN child_profile cp ON cp.child_id=o.child_id WHERE o.objective_id=? AND o.child_id=? AND o.session_id=?
            AND o.status='active' AND o.objective_type='learning' AND ds.status='active' AND ds.state='study' AND cp.status='active' ''',
            (snapshot['objective_id'],turn['child_id'],turn['session_id'])).fetchone()
        other = c.execute("SELECT 1 FROM agent_objective WHERE child_id=? AND status='active' AND objective_id!=?",
            (turn['child_id'],snapshot['objective_id'])).fetchone()
        if not goal or other:
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_GOAL_CHANGED')
        if prepared is None:
            VoiceControlIntent.assert_current_input(c,turn['session_id'],turn['child_id'],turn['client_action_id'],turn['transcript'])
            if list(VoiceControlIntent.text_consent_version(c) or ())!=list(snapshot.get('consent_version') or ()):
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_CONSENT_CHANGED')
        else:
            request = json.loads(prepared['request_json'])
            if (request.get('oral_authorized_turn_id')!=turn['agent_turn_id'] or request.get('oral_followup')!=packet
                    or prepared['child_id']!=turn['child_id'] or prepared['origin_session_id']!=turn['session_id']
                    or prepared['diagnostic_item_id']!=packet['action']['diagnostic_item_id']
                    or prepared['item_version']!=packet['action']['item_version']):
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_PROMPT_CHANGED')
        if action.get('voice_input_id'):
            voice = AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
                voice_input_id=action['voice_input_id'],transcript=turn['transcript'])
            if AgentRuntimeStore.voice_capture_snapshot(voice)!=action.get('voice_input_binding'):
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_VOICE_CHANGED')
        current = AgentRuntimeStore.revalidate_oral_followup(c,**packet,
            _prepared_prompt_id=prepared['prompt_id'] if prepared is not None else None,
            _history_before=prepared['created_at'] if historical else None)
        if current['status']!='ready':
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_SOURCE_CHANGED')
        return snapshot

    @staticmethod
    def inspect_oral_followup_execution(c, agent_turn_id, *, prepared=None, historical=False):
        # Readback may validate an answered ancestor. It cannot authorize a new
        # prompt or erase exposures that preceded the original preparation.
        if historical and (prepared is None or prepared['status']!='answered' or not prepared['attempt_id']):
            raise InvalidAgentPlanError('ANSWER_ORAL_HISTORY_REQUIRES_SAVED_ANSWER')
        row = c.execute('''SELECT t.*,p.context_manifest_json,p.objective_id,p.status AS plan_status,
            s.status AS step_status,s.capability_name,s.arguments_json,s.error_code,s.idempotency_key
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id AND s.step_index=0''',(agent_turn_id,)).fetchone()
        if (not row or row['plan_status']=='cancelled' or row['step_status']=='cancelled'
                or row['tool_error_code']=='SOURCE_DELETED' or row['error_code']=='SOURCE_DELETED'):
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_CANCELLED')
        if historical:
            saved = c.execute('SELECT created_at FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
                (prepared['attempt_id'],row['child_id'],row['session_id'])).fetchone()
            if (row['plan_status'] not in {'waiting_child','awaiting_child','completed'}
                    or row['step_status']!='completed' or row['tool_status']!='completed'
                    or not saved or not (_aware_datetime(row['created_at']) <= _aware_datetime(prepared['created_at'])
                        <= _aware_datetime(saved[0]) <= datetime.now(timezone.utc))):
                raise InvalidAgentPlanError('ANSWER_ORAL_HISTORY_RECEIPT_CHANGED')
        if prepared is None:
            from .realtime_native_special_sources import validate as validate_native_special
            if validate_native_special(c, agent_turn_id) is not True:
                from .voice_control_intent import VoiceControlIntent
                VoiceControlIntent.inspect_execution(c,agent_turn_id)
        elif prepared['prepare_action_id']!=row['idempotency_key']:
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_COMMAND_CHANGED')
        elif (row['plan_status'] not in {'running','waiting_child','awaiting_child','completed'}
                or row['step_status'] not in {'running','completed'}):
            storage_recovery = (row['plan_status']==row['step_status']=='failed'
                and row['tool_error_code']==row['error_code'] and row['error_code'] in {'OperationalError','IntegrityError','APP_RESTARTED'})
            if not storage_recovery:
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_FAILED_COMMAND')
        action = json.loads(row['action_json'])
        expected = {'retry_oral_answer':'oral.retry','check_oral_independently':'oral.independent_check'}
        if (expected.get(action.get('type'))!=row['capability_name']
                or json.loads(row['arguments_json'])!={'request_quote':row['transcript']}):
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_COMMAND_CHANGED')
        snapshot = AgentRuntimeStore._validate_oral_followup_authority(c,row,action,json.loads(row['context_manifest_json']),
            prepared=prepared,historical=historical)
        if row['objective_id']!=snapshot['objective_id']:
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_GOAL_CHANGED')
        return {'binding':action['oral_followup_binding'],'objective_id':snapshot['objective_id']}

    @staticmethod
    def oral_followup_prompt_source_reason(c, prompt):
        if prompt['status'] not in {'waiting_answer','answer_saved'}:
            return None
        request = json.loads(prompt['request_json'])
        turn_id = request.get('oral_authorized_turn_id')
        managed = c.execute('''SELECT 1 FROM agent_step WHERE idempotency_key=?
            AND capability_name IN ('oral.retry','oral.independent_check')''',(prompt['prepare_action_id'],)).fetchone()
        if not turn_id:
            return 'oral_followup_command_missing' if managed or request.get('oral_followup') else None
        try:
            AgentRuntimeStore.inspect_oral_followup_execution(c,turn_id,prepared=prompt)
        except (ValueError,KeyError,TypeError):
            return 'oral_followup_source_changed'
        return None

    @staticmethod
    def bind_oral_followup_prompt(c, turn_id, output):
        prompt = output.get('prompt') if isinstance(output,dict) else None
        if not prompt:
            return False
        row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
        admitted = AgentRuntimeStore.inspect_oral_followup_execution(c,turn_id,prepared=row)
        target = admitted['binding']['action']
        goal = c.execute('SELECT * FROM agent_objective WHERE objective_id=?',(admitted['objective_id'],)).fetchone()
        bound = {'version':'oral_learning_binding_v1','objective_id':goal['objective_id'],'child_id':row['child_id'],
            'session_id':row['origin_session_id'],'prompt_id':row['prompt_id'],'item_version':row['item_version'],
            'diagnostic_item_id':row['diagnostic_item_id'],'phase':goal['phase'],
            'success_criteria':json.loads(goal['success_criteria_json']),'required_hint_level':target['required_hint_level'],
            'parent_prompt_id':target['parent_prompt_id'],'created_at':row['created_at'],
            'local_only':True,'cloud_transmission_allowed':False}
        c.execute("UPDATE agent_plan SET plan_json=json_set(plan_json,'$.local_learning_binding',json(?)) WHERE agent_turn_id=?",
            (json.dumps(bound,ensure_ascii=False),turn_id))
        from .oral_supply_obligation import prepared
        prepared(c,turn_id,target,goal['objective_id'],row)
        return row['status']=='waiting_answer'

    @staticmethod
    def capture_writing_followup_arrival(c, arrival_id, voice_capture):
        """Freeze a real feedback focus before semantic planning; never reselect on retry."""
        from .writing_teaching import WritingTeachingService
        row = c.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=?', (arrival_id,)).fetchone()
        items = json.loads(row['snapshot_json'])
        old = next((x for x in items if x.get('kind') == 'writing_followup_arrival'), None)
        if old is not None:
            return old['record']
        snapshot = None
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='learning_evaluation_execution'").fetchone():
            # Match DiagnosticPromptService's persistent current focus. Dialogue
            # chronology is not evidence of which manuscript the child means.
            parent = c.execute('''SELECT prompt_id,item_version FROM diagnostic_prompt
                WHERE origin_session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1''',
                (row['session_id'], row['child_id'])).fetchone()
            captured = AgentRuntimeStore.captured_question(voice_capture or {})
            if parent and (not voice_capture or (captured and captured['kind'] == 'diagnostic'
                    and captured['binding'] == dict(parent))):
                service = WritingTeachingService(None)
                try:
                    view = service.read(row['session_id'], **dict(parent), connection=c)
                    if view['status'] == 'feedback_ready':
                        matches = c.execute('''SELECT t.agent_turn_id,p.objective_id FROM agent_turn t
                            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                                AND p.child_id=t.child_id AND p.session_id=t.session_id
                            JOIN agent_step s USING(plan_id)
                            JOIN learning_evaluation_execution e USING(step_id)
                            WHERE t.session_id=? AND t.child_id=? AND t.tool_status='completed'
                                AND s.capability_name='learning.evaluate_response' AND s.status='completed'
                                AND p.status='completed' AND e.state='completed'
                                AND json_extract(e.record_json,'$.source_evidence_id')=?
                                AND json_extract(e.record_json,'$.child_id')=t.child_id
                                AND json_extract(e.record_json,'$.session_id')=t.session_id
                                AND json_extract(e.record_json,'$.objective_id')=p.objective_id
                                AND json_extract(e.result_json,'$.evaluation.rubric_assessment.assessment_id')=?''',
                            (row['session_id'],row['child_id'],view['evidence_id'],view['assessment_id'])).fetchall()
                        if len(matches) == 1:
                            focus = matches[0]
                            goal = c.execute('SELECT status FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?',
                                (focus['objective_id'],row['child_id'],row['session_id'])).fetchone()
                            other_goal = c.execute('''SELECT 1 FROM agent_objective WHERE child_id=? AND session_id=?
                                AND status='active' AND objective_id!=?''',
                                (row['child_id'],row['session_id'],focus['objective_id'])).fetchone()
                            if goal and goal[0] in {'active','completed'} and not other_goal:
                                snapshot = {'parent': dict(parent), 'feedback_turn_id': focus['agent_turn_id'],
                                    'objective_id': focus['objective_id'], 'goal_status': goal[0],
                                    'task_points': view['success_criteria'], 'packets': {
                                        'revision': service.resolve_next_action(row['session_id'], **dict(parent), kind='revision', connection=c),
                                        'independent_check': service.select_independent_action(row['session_id'], **dict(parent), connection=c)},
                                    'local_only': True, 'cloud_transmission_allowed': False}
                except ValueError:
                    snapshot = None
        items.append({'kind': 'writing_followup_arrival', 'record': snapshot})
        c.execute('UPDATE learning_input_arrival SET snapshot_json=? WHERE arrival_id=?',
                  (json.dumps(items, ensure_ascii=False),arrival_id))
        return snapshot

    @staticmethod
    def _validate_writing_followup_authority(c, turn, action, manifest):
        from .writing_teaching import WritingTeachingService
        packet = action.get('writing_followup_binding')
        from .realtime_native_special_sources import source_witness
        native = source_witness(c, turn, action, manifest)
        control = {'revise_writing_original':'writing.revise_original',
                   'check_writing_independently':'writing.independent_check'}.get(action.get('type'))
        kind = 'revision' if control == 'writing.revise_original' else 'independent_check'
        if (not control or not isinstance(packet, dict) or set(packet) != {'action','binding_version'}
                or action.get('writing_revision_binding') or action.get('request_quote') != turn['transcript']
                or turn['intent'] == 'runtime_continuation'
                or (manifest.get('writing_voice_binding') or {}).get('followup') != packet):
            raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_BINDING_REQUIRED')
        arrival = c.execute('''SELECT * FROM learning_input_arrival WHERE arrival_id=?
            AND session_id=? AND child_id=? AND client_action_id=?''',
            (manifest.get('learning_input_arrival_id'),turn['session_id'],turn['child_id'],turn['client_action_id'])).fetchone()
        latest = c.execute('SELECT arrival_id FROM learning_input_arrival WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                           (turn['session_id'],turn['child_id'])).fetchone()
        frozen = next((x['record'] for x in json.loads(arrival['snapshot_json']) if x.get('kind') == 'writing_followup_arrival'), None) if arrival else None
        expected = ((frozen or {}).get('packets') or {}).get(kind) or {}
        if (not arrival or not latest or latest[0] != arrival['arrival_id'] or arrival['utterance'] != turn['transcript']
                or expected.get('status') != 'ready' or {k:expected.get(k) for k in packet} != packet):
            raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_ARRIVAL_CHANGED')
        original = c.execute("SELECT status FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=? AND objective_type='learning'",
                             (frozen['objective_id'],turn['child_id'],turn['session_id'])).fetchone()
        active = c.execute("SELECT objective_id FROM agent_objective WHERE child_id=? AND status='active'", (turn['child_id'],)).fetchall()
        practice = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
        terminal_allowed = (original and original[0] == frozen.get('goal_status') == 'completed'
            and (not active or (len(active) == 1 and practice and active[0][0] == practice[0]
                                and practice[0] != frozen['objective_id'])))
        active_allowed = (original and original[0] == frozen.get('goal_status') == 'active'
                          and len(active) == 1 and active[0][0] == frozen['objective_id'])
        if not terminal_allowed and not active_allowed:
            raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_GOAL_CHANGED')
        receipt = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
            ((action.get('writing_followup_semantic_receipt') or {}).get('request_key'),)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([turn['child_id'],turn['session_id'],turn['transcript']], ensure_ascii=False).encode()).hexdigest()
        if native is None and (not receipt or receipt['status'] != 'completed' or receipt['fingerprint'] != fingerprint
                or receipt['child_id'] != turn['child_id'] or receipt['session_id'] != turn['session_id']
                or json.loads(receipt['binding_json']).get('writing_followups', {}).get(kind) != expected
                or json.loads(receipt['decision_json']) != {'status':'control','control':control}):
            raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_SEMANTIC_RECEIPT_REQUIRED')
        if action.get('voice_input_id'):
            voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=turn['session_id'],child_id=turn['child_id'],
                voice_input_id=action['voice_input_id'],transcript=turn['transcript'])
            if AgentRuntimeStore.voice_capture_snapshot(voice) != action.get('voice_input_binding'):
                raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_VOICE_CHANGED')
        current = WritingTeachingService(None).revalidate_action(turn['session_id'], **packet, connection=c)
        if current['status'] != 'ready' or current['action'].get('kind') != kind:
            raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_SOURCE_CHANGED')
        return frozen

    @staticmethod
    def writing_followup_prompt_source_reason(c, prompt):
        """A saved prompt keeps its original command; new speech cannot revive it."""
        if prompt['status'] not in {'waiting_answer','answer_saved'}:
            return None
        try:
            return AgentRuntimeStore._writing_followup_prompt_source_reason(c, prompt)
        except (ValueError, TypeError, KeyError, AttributeError):
            return 'writing_followup_binding_invalid'

    @staticmethod
    def _writing_followup_prompt_source_reason(c, prompt):
        request = json.loads(prompt['request_json'])
        revision = request.get('writing_reference_revision')
        turn_id = request.get('writing_authorized_turn_id') or (revision or {}).get('request_turn_id')
        if not turn_id:
            # Direct formal preparation has its own source gate. Only a saved
            # runtime command key (or reference lineage) makes this its child.
            managed = c.execute('''SELECT 1 FROM agent_step s JOIN agent_plan p USING(plan_id)
                WHERE s.idempotency_key=? AND p.child_id=? AND p.session_id=?
                AND s.capability_name IN ('writing.revise_original','writing.independent_check')''',
                (prompt['prepare_action_id'],prompt['child_id'],prompt['origin_session_id'])).fetchone()
            reference_lineage = (c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='writing_reference_revision'").fetchone()
                and c.execute('SELECT 1 FROM writing_reference_revision WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone())
            if managed or reference_lineage:
                return 'writing_followup_command_missing'
            return None
        source = c.execute('''SELECT t.action_json,t.transcript,t.tool_error_code,p.objective_id,p.status AS plan_status,
            p.context_manifest_json,p.plan_json,s.status AS step_status,s.error_code,s.capability_name,o.status AS goal_status
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            LEFT JOIN agent_objective o ON o.objective_id=p.objective_id AND o.child_id=p.child_id
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND p.child_id=t.child_id
            AND p.session_id=t.session_id AND s.step_index=0''',
            (turn_id,prompt['child_id'],prompt['origin_session_id'])).fetchone()
        if not source:
            return 'writing_followup_command_missing'
        if json.loads(source['context_manifest_json']).get('daily_writing_binding'):
            from .writing_daily_followup import inspect_execution
            recoverable = (source['plan_status'] == source['step_status'] == 'failed'
                and source['error_code'] == source['tool_error_code']
                and source['error_code'] in {'OperationalError','IntegrityError','APP_RESTARTED'})
            inspect_execution(c,turn_id,prompt=prompt,recovering=recoverable)
            return None
        if (source['plan_status'] == 'cancelled' or source['step_status'] == 'cancelled'
                or source['goal_status'] == 'cancelled'
                or 'SOURCE_DELETED' in (source['tool_error_code'],source['error_code'])):
            return 'writing_followup_command_cancelled'
        action = json.loads(source['action_json'])
        manifest = json.loads(source['context_manifest_json'])
        active = c.execute("SELECT objective_id FROM agent_objective WHERE child_id=? AND status='active'", (prompt['child_id'],)).fetchall()
        if source['goal_status'] != 'active' or len(active) != 1 or active[0][0] != source['objective_id']:
            return 'writing_followup_goal_changed'
        bound = json.loads(source['plan_json']).get('local_learning_binding')
        if bound and any(bound.get(key) != value for key,value in {
                'objective_id':source['objective_id'],'prompt_id':prompt['prompt_id'],
                'item_version':prompt['item_version'],'child_id':prompt['child_id'],
                'session_id':prompt['origin_session_id']}.items()):
            return 'writing_followup_binding_changed'
        if action.get('voice_input_id'):
            voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=prompt['origin_session_id'],child_id=prompt['child_id'],
                voice_input_id=action['voice_input_id'],transcript=source['transcript'])
            if AgentRuntimeStore.voice_capture_snapshot(voice) != action.get('voice_input_binding'):
                return 'writing_followup_voice_changed'
        from .realtime_native_special_sources import semantic_version
        native = semantic_version(c, turn_id, completion=True)
        semantic = action.get('writing_revision_semantic_receipt' if revision else 'writing_followup_semantic_receipt') or {}
        receipt = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (semantic.get('request_key'),)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([prompt['child_id'],prompt['origin_session_id'],source['transcript']],ensure_ascii=False).encode()).hexdigest()
        if (action.get('request_quote') != source['transcript'] or (native is None and (not receipt or receipt['status'] != 'completed' or receipt['fingerprint'] != fingerprint
                or receipt['child_id'] != prompt['child_id'] or receipt['session_id'] != prompt['origin_session_id']))):
            return 'writing_followup_request_changed'
        if revision:
            if request.get('writing_authorized_turn_id') or action.get('writing_followup_binding'):
                return 'writing_followup_binding_changed'
            target = action.get('writing_revision_binding') or {}
            if (action.get('type') != 'revise_writing_original' or action.get('request_quote') != source['transcript']
                    or source['capability_name'] != 'writing.revise_original'
                    or manifest.get('writing_voice_binding') != {'request_quote':source['transcript'],'revision':target}
                    or target.get('prompt_id') != revision.get('parent_prompt_id')
                    or target.get('item_version') != revision.get('parent_item_version')
                    or target.get('reference_turn_id') != revision.get('reference_turn_id')):
                return 'writing_followup_binding_changed'
            from .writing_teaching import _reference_receipt, _revision_request, _reference_revision_action, _reference_revision_version
            from .writing_response_origin import _prompt
            try:
                parent, _ = _prompt(c,prompt['origin_session_id'],revision['parent_prompt_id'],revision['parent_item_version'],live=False)
                reference, original = _reference_receipt(c,prompt['origin_session_id'],parent,revision['reference_turn_id'])
                proof = _revision_request(c,prompt['origin_session_id'],parent,original,reference,turn_id,live=False)
                expected = _reference_revision_action(parent,revision['reference_turn_id'],turn_id)
                if (expected['diagnostic_item_id'] != prompt['diagnostic_item_id'] or expected['item_version'] != prompt['item_version']
                        or _reference_revision_version(expected,original,reference,proof) != revision['binding_version']):
                    return 'writing_followup_binding_changed'
            except ValueError:
                return 'writing_followup_source_changed'
            return None
        packet = action.get('writing_followup_binding') or {}
        target = packet.get('action') or {}
        reference = request.get('writing_followup') or {}
        capability = {'revision':'writing.revise_original','independent_check':'writing.independent_check'}.get(target.get('kind'))
        action_type = {'revision':'revise_writing_original','independent_check':'check_writing_independently'}.get(target.get('kind'))
        if (not capability or source['capability_name'] != capability or action.get('type') != action_type
                or set(packet) != {'action','binding_version'} or action.get('writing_revision_binding')
                or target.get('child_id') != prompt['child_id'] or target.get('session_id') != prompt['origin_session_id']
                or target.get('diagnostic_item_id') != prompt['diagnostic_item_id']
                or target.get('item_version') != prompt['item_version']
                or reference != {k:target.get(k) for k in ('parent_prompt_id','parent_item_version','kind')} | {'binding_version':packet.get('binding_version')}
                or manifest.get('writing_voice_binding') != {'request_quote':source['transcript'],'followup':packet}):
            return 'writing_followup_binding_changed'
        if native is not None:
            arrival = c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE arrival_id=?', (manifest.get('learning_input_arrival_id'),)).fetchone()
            frozen = next((v.get('record') for v in json.loads(arrival[0]) if v.get('kind')=='writing_followup_arrival'), None) if arrival else None
            expected = ((frozen or {}).get('packets') or {}).get(target.get('kind'), {})
        else:
            expected = json.loads(receipt['binding_json']).get('writing_followups',{}).get(target.get('kind'),{})
        control = 'writing.revise_original' if target.get('kind') == 'revision' else 'writing.independent_check'
        if (expected.get('status') != 'ready' or {k:expected.get(k) for k in packet} != packet
                or (native is None and json.loads(receipt['decision_json']) != {'status':'control','control':control})):
            return 'writing_followup_request_changed'
        from .writing_teaching import WritingTeachingService
        current = WritingTeachingService(None).revalidate_action(prompt['origin_session_id'], **packet, connection=c)
        return None if current['status'] == 'ready' else 'writing_followup_source_changed'

    @staticmethod
    def inspect_writing_followup_execution(c, agent_turn_id, *, allow_planned=False):
        daily=c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',(agent_turn_id,)).fetchone()
        if daily and json.loads(daily[0]).get('daily_writing_binding'):
            from .writing_daily_followup import inspect_execution
            return inspect_execution(c,agent_turn_id)
        turn = c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,s.status AS step_status,
            s.capability_name,s.error_code,s.idempotency_key,d.status AS session_status,d.state AS session_state
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            JOIN daily_session d ON d.session_id=t.session_id AND d.child_id=t.child_id
            WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id AND s.step_index=0''',
            (agent_turn_id,)).fetchone()
        allowed = {'running','planned'} if allow_planned else {'running'}
        if (not turn or turn['plan_status'] not in allowed or turn['step_status'] not in allowed
                or turn['tool_status'] not in {'pending','failed'} or turn['error_code']
                or turn['session_status'] != 'active' or turn['session_state'] != 'study'):
            raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_NOT_AUTHORIZED')
        AgentRuntimeStore.assert_turn_source_not_deleted(c, agent_turn_id)
        action = json.loads(turn['action_json'])
        expected = {'revise_writing_original':'writing.revise_original','check_writing_independently':'writing.independent_check'}
        if turn['capability_name'] != expected.get(action.get('type')):
            raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_CAPABILITY_CHANGED')
        from .realtime_native_special_sources import validate as validate_native_special
        validate_native_special(c, agent_turn_id)
        AgentRuntimeStore._validate_writing_followup_authority(c, turn, action,json.loads(turn['context_manifest_json']))
        return {'binding':action['writing_followup_binding'],'request_quote':turn['transcript'],'capability':turn['capability_name']}

    @staticmethod
    def writing_voice_snapshot(c, session_id, child_id, voice_capture):
        """Local-only source focus. Never place this projection in model context."""
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
            return None
        from .writing_response_origin import _prompt
        from .writing_ocr_authorization import _confirmation, arrival_binding
        captured = AgentRuntimeStore.captured_question(voice_capture or {})
        if captured and captured.get('kind') != 'diagnostic':
            return None
        params = [session_id, child_id]
        query = """SELECT prompt_id,item_version FROM diagnostic_prompt
            WHERE origin_session_id=? AND child_id=? AND owner_kind='diagnostic' AND status='waiting_answer'"""
        if captured:
            query += ' AND prompt_id=? AND item_version=?'
            params.extend([captured['binding']['prompt_id'], captured['binding']['item_version']])
        prompts = c.execute(query, params).fetchall()
        if len(prompts) != 1:
            return None
        p = dict(prompts[0])
        try:
            _, item = _prompt(c, session_id, p['prompt_id'], p['item_version'], live=True)
        except ValueError:
            return None
        result = {'prompt': p, 'question': item['prompt'], 'page': None, 'confirmation': None,
                  'pending_confirmation': False, 'scope_pending': False,
                  'local_sources': [],
                  'local_only': True, 'cloud_transmission_allowed': False}
        prior = c.execute('''SELECT p.context_manifest_json,p.status FROM agent_turn t
            JOIN agent_plan p USING(agent_turn_id) WHERE t.session_id=? AND t.child_id=?
            ORDER BY t.rowid DESC LIMIT 1''', (session_id, child_id)).fetchone()
        scope = json.loads(prior['context_manifest_json']).get('writing_scope_binding') if prior and prior['status'] != 'cancelled' else None
        if scope and scope.get('prompt') == p:
            result.update(scope_pending=True, page=scope.get('page'))
            frozen = result['page'] or {}
            source = c.execute('''SELECT a.extracted_text FROM document_page_revision r
                JOIN capture_artifact a ON a.artifact_id=r.artifact_id
                WHERE r.revision_id=? AND r.artifact_id=? AND r.content_sha256=?
                AND r.session_id=? AND r.child_id=? AND a.status IN ('completed','quality_warning')
                AND r.revision_index=(SELECT MAX(r2.revision_index) FROM document_page_revision r2 WHERE r2.monitor_id=r.monitor_id)''',
                (frozen.get('revision_id'), frozen.get('artifact_id'), frozen.get('content_sha256'), session_id, child_id)).fetchone()
            result['page'] = {**frozen, 'extracted_text': source[0]} if source else None
        elif c.execute("SELECT 1 FROM sqlite_master WHERE name='document_page_revision'").fetchone():
            page = c.execute('''SELECT r.revision_id,r.artifact_id,r.content_sha256,a.extracted_text
                FROM document_page_revision r JOIN document_monitor_session m USING(monitor_id)
                JOIN capture_artifact a ON a.artifact_id=r.artifact_id
                WHERE r.session_id=? AND r.child_id=? AND m.status='active'
                AND a.status IN ('completed','quality_warning')
                AND r.revision_index=(SELECT MAX(r2.revision_index) FROM document_page_revision r2 WHERE r2.monitor_id=r.monitor_id)''',
                (session_id, child_id)).fetchall()
            if len(page) == 1:
                result['page'] = dict(page[0])
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_ocr_confirmation'").fetchone():
            rows = c.execute('''SELECT * FROM writing_ocr_confirmation w WHERE session_id=? AND child_id=?
                AND prompt_id=? AND item_version=? AND NOT EXISTS
                (SELECT 1 FROM writing_ocr_event e WHERE e.confirmation_id=w.confirmation_id AND e.kind IN ('authorize','retract'))''',
                (session_id, child_id, p['prompt_id'], p['item_version'])).fetchall()
            candidates = []
            for row in rows:
                try:
                    _, source, _ = _confirmation(c, session_id, row['confirmation_id'], row['binding_version'], live=True)
                except ValueError:
                    continue
                candidates.append(row)
                result['local_sources'].append(source['text'])
            result['pending_confirmation'] = bool(candidates)
            if len(candidates) > 1 and voice_capture:
                from .voice_capture_binding import capture_started_at
                captured_at = capture_started_at(c, voice_capture['voice_input_id'])
                # A persisted completed readback is a focus, unlike merely
                # preparing a page or guessing the newest historical source.
                focus = c.execute('''SELECT e.confirmation_id FROM writing_ocr_event e
                    JOIN writing_ocr_confirmation w USING(confirmation_id) WHERE w.session_id=? AND w.child_id=?
                    AND w.prompt_id=? AND w.item_version=? AND e.kind='ended' AND julianday(e.created_at)<=julianday(?)
                    ORDER BY e.rowid DESC LIMIT 1''', (session_id, child_id, p['prompt_id'], p['item_version'],
                        captured_at)).fetchone()
                if focus:
                    candidates = [row for row in candidates if row['confirmation_id'] == focus[0]]
            if len(candidates) == 1 and voice_capture:
                row = candidates[0]
                try:
                    result['confirmation'] = arrival_binding(c, session_id=session_id,
                        confirmation_id=row['confirmation_id'], binding_version=row['binding_version'],
                        voice_input_id=voice_capture['voice_input_id'])
                except ValueError:
                    pass
        return result

    @staticmethod
    def writing_response_span(snapshot, transcript):
        """Only explicit page scope or unique verbatim anchors select OCR text."""
        page = (snapshot or {}).get('page')
        if not page or not page.get('extracted_text'):
            return None
        text = page['extracted_text']
        if re.fullmatch(r'(?:这|这一|整)页(?:上)?(?:只有|全是|都是)(?:我|我自己)(?:写的)?(?:答案|作答|作文)[。！!\s]*', transcript):
            return (0, len(text))
        anchors = re.fullmatch(r'从[“「"](.+?)[”」"](?:开始)?[，,\s]*到[“「"](.+?)[”」"](?:结束)?[。！!\s]*', transcript)
        if anchors and text.count(anchors[1]) == text.count(anchors[2]) == 1:
            start, end = text.index(anchors[1]), text.index(anchors[2]) + len(anchors[2])
            if start < end:
                return (start, end)
        return None

    def capture_unplaced_arrival(self, connection, arrival_id):
        from .learning_input_carryover import LearningInputCarryoverService, CarryoverConflict
        row = connection.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=?', (arrival_id,)).fetchone()
        if not row:
            raise InvalidAgentPlanError('CARRYOVER_ARRIVAL_REQUIRED')
        items = json.loads(row['snapshot_json'])
        existing = next((item['record'] for item in items if item.get('kind') == 'unplaced_voice_arrival'), None)
        if existing is not None:
            return existing
        try:
            view = LearningInputCarryoverService(self.database).list_unplaced(row['child_id'], row['session_id'], connection=connection)
        except CarryoverConflict:
            view = {'items': [], 'planning_date': None}
        candidates = [item for item in view['items'] if item['allowed_actions'] and (item['remaining_minutes'] > 0 or item.get('scope_review_required'))]
        prior = connection.execute("""SELECT t.agent_turn_id,t.intent,t.response_text,p.context_manifest_json,p.status,
            s.status AS step_status,s.idempotency_key
            FROM agent_turn t LEFT JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            LEFT JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            WHERE t.child_id=? AND t.session_id=? ORDER BY t.rowid DESC LIMIT 1""", (row['child_id'], row['session_id'])).fetchone()
        dialogue = None
        if prior and prior['intent'] == 'unplaced_voice_dialogue' and prior['status'] != 'cancelled':
            saved = json.loads(prior['context_manifest_json'] or '{}').get('learning_input_unplaced_dialogue')
            if saved and saved.get('response') == prior['response_text']:
                dialogue = dict(saved)
                if saved.get('scope_offer_ready') and saved.get('scope_draft', {}).get('scope') and saved.get('scope_draft', {}).get('estimate'):
                    dialogue['offer_turn_id'] = prior['agent_turn_id']
        elif prior and prior['intent'] in {'learning_inputs.review_unplaced', 'learning_inputs.review_reclaimed_scope'} and prior['status'] in {'planned', 'running', 'failed'}:
            binding = json.loads(prior['context_manifest_json'] or '{}').get('learning_input_unplaced_binding')
            candidate = next((item for item in candidates if binding and item['obligation_id'] == binding['obligation_id']), None)
            if candidate:
                dialogue = {'candidates': candidates, 'focus': {**candidate, 'binding': binding},
                            'pending_turn_id': prior['agent_turn_id']}
        elif (prior and prior['intent'] == 'learning_inputs.review_reclaimed_scope'
                and prior['status'] in {'completed', 'waiting_child'} and prior['step_status'] == 'completed'):
            event = connection.execute("SELECT obligation_id,revision FROM learning_input_carryover_event WHERE child_id=? AND operation='confirm_scope' AND client_action_id=? AND json_extract(request_json,'$.authorized_turn_id')=?",
                (row['child_id'], prior['idempotency_key'], prior['agent_turn_id'])).fetchone()
            original = json.loads(prior['context_manifest_json'] or '{}').get('learning_input_unplaced_binding') or {}
            candidate = next((item for item in candidates if event and item['source_current'] and item['scope_confirmed']
                and item['obligation_id'] == event['obligation_id'] == original.get('obligation_id')
                and item['revision'] == event['revision'] == original.get('revision', -2) + 1), None)
            if candidate:
                dialogue = {'candidates': candidates, 'focus': candidate}
        snapshot = {'arrival_id': arrival_id, 'candidates': candidates, 'dialogue': dialogue, 'planning_date': view['planning_date'],
                    'context_version': view.get('context_version')}
        # Extend the existing local arrival record, once, before any planning/model work.
        items.append({'kind': 'unplaced_voice_arrival', 'record': snapshot})
        connection.execute('UPDATE learning_input_arrival SET snapshot_json=? WHERE arrival_id=?',
                           (json.dumps(items, ensure_ascii=False), arrival_id))
        return snapshot

    @staticmethod
    def resolve_unplaced_voice(snapshot, transcript):
        text = re.sub(r'[\s，,。！!？?；;]', '', transcript)
        candidates = snapshot.get('candidates', [])
        dialogue = snapshot.get('dialogue')
        deictic = candidates and re.fullmatch(r'(?:昨天|上次)(?:的)?(?:那|这)?(?:几道|几项|几份|这些)?(?:没做完|没写完|没完成)(?:的)?(?:题目|题)?(?:今天|今日)?(?:继续|接着|补上|补做)(?:做|安排|排上)?(?:吧)?', text)
        # A generic scheduling phrase is not evidence that the child means homework.
        # Without a live homework dialogue, leave ungrounded requests to intent routing.
        if not dialogue and not deictic and '作业' not in text and not any(item.get('title') and item['title'] in text for item in candidates):
            return None
        explicit = bool(re.search(r'(?:没|未)(?:排完|安排|排上)|(?:昨天|上次|剩下|剩余|延期|待安排).*作业|(?:昨天|上次).*(?:没做完|没写完|没完成)|(?:剩下|剩余).*(?:不做|继续|补上|补做|安排|再做|留)', text))
        if not dialogue and not explicit:
            return None
        selected = None
        named_selection = False
        offered = dialogue.get('candidates', []) if dialogue else candidates
        focus = dialogue.get('focus') if dialogue else None
        ordinal = re.fullmatch(r'(?:我说的是|我选|就是|就|选择|选)?第?([一二两三四五六七八九十0-9]+)(?:个|项)(?:作业)?(?:吧)?', text)
        if ordinal and dialogue:
            from .learning_input_revision import duration_minutes
            index = duration_minutes(ordinal[1])
            selected = offered[index - 1] if index and 0 < index <= len(offered) else None
        else:
            named = [item for item in offered if item['title'] and item['title'] in text]
            from .learning_input_scope_dialogue import scope_slots
            describing_focused_scope = (focus and focus.get('scope_review_required') and scope_slots(transcript)
                and not any(text in {item['title'], '我说的是' + item['title'], '换成' + item['title'], '选' + item['title']} for item in offered))
            if describing_focused_scope:
                selected = focus
            elif len(named) == 1:
                selected = named[0]
                named_selection = True
            elif not named and focus:
                selected = focus
            elif not dialogue and explicit and len(candidates) == 1:
                selected = candidates[0]
        valid = selected and any(item['binding'] == selected['binding'] for item in candidates)
        if valid and dialogue and not ordinal:
            if selected.get('scope_review_required'):
                match = re.fullmatch(r'(?:还剩|剩下|剩余)?(.+?)(?:[，, ]*)(?:预计|大约|大概|还要|需要)(?:还要|需要)?([零〇一二两三四五六七八九十百0-9]+)分钟[。！! ]*', transcript.strip())
                if match:
                    from .learning_input_revision import duration_minutes
                    minutes = duration_minutes(match[2])
                    if (minutes and 1 <= minutes <= 1440 and re.search(r'题|页|段|行|面|单词|口算|作文|练习', match[1])
                            and (re.match(r'还剩|剩下|剩余', transcript.strip()) or re.search(r'没写|没做|没完成|未写|未做|还没', match[1]))
                            and not re.search(r'不是|不要|他说|她说|假如|如果', match[1])):
                        return {'capability': 'learning_inputs.review_reclaimed_scope', 'binding': selected['binding'],
                            'arguments': {'operation': 'confirm_scope', 'remaining_scope_quote': match[1].strip('，, '), 'remaining_minutes': minutes, 'request_quote': transcript},
                            'response': '我先核对并保存你确认的剩余范围和预计时长，暂不开始补排。'}
            if text in {'撤回刚才确认的剩余范围', '撤回剩余范围确认'} and (selected.get('scope_confirmed') or selected.get('scope_review_required')):
                return {'capability': 'learning_inputs.review_reclaimed_scope', 'binding': selected['binding'],
                    'arguments': {'operation': 'withdraw_scope', 'request_quote': transcript}, 'response': '我来撤回这次范围确认，不把撤回当作完成。'}
            if selected.get('scope_review_required') and snapshot.get('arrival_id'):
                from .learning_input_scope_dialogue import advance_scope_dialogue
                scope_reply = advance_scope_dialogue(snapshot, selected, transcript)
                if scope_reply:
                    return scope_reply
        # Only whole grounded decision clauses authorize writes; selection alone never does.
        clause = text
        if selected:
            clause = clause.replace(selected['title'], '', 1)
        clause = re.sub(r'^(?:昨天|上次)(?:的)?(?:那)?(?:几道|几项|几份|这些)?(?:没做完|没写完|没完成)(?:的)?(?:题目|题|作业)?', '', clause)
        clause = re.sub(r'^(?:请|麻烦)?(?:把|将)?(?:昨天|上次|剩下|剩余|延期|待安排)?(?:的)?(?:没排完的|没安排的|没排上的)?(?:作业)?(?:的)?', '', clause)
        clause = re.sub(r'^(?:这项|这一项|这个|这份)(?:作业)?', '', clause)
        clause = re.sub(r'(?:吧|好了|就行)$', '', clause)
        decision, not_before = None, None
        if re.fullmatch(r'(?:那|就)?(?:今天|今日)(?:继续|接着|补上|补做)(?:做|安排|排上)?', clause):
            decision = 'continue_today'
        elif re.fullmatch(r'(?:那|就)?(?:不要了|不做了|取消(?:安排)?|放弃(?:剩余安排)?)', clause):
            decision = 'drop'
        elif match := re.fullmatch(r'(?:那|就|先)?(?:留着|留到|改到)?(明天|后天|\d{4}-\d{2}-\d{2})(?:再做|再安排|再说|做|安排)', clause):
            try:
                day = datetime.strptime(snapshot['planning_date'], '%Y-%m-%d').date()
                target = day + timedelta(days=1 if match[1] == '明天' else 2) if match[1] in {'明天', '后天'} else datetime.strptime(match[1], '%Y-%m-%d').date()
                if target > day:
                    decision, not_before = 'keep', target.isoformat()
            except (TypeError, ValueError):
                pass
        if (dialogue or named_selection) and valid and decision and not ordinal and decision in selected['allowed_actions']:
            args = {'decision': decision, 'request_quote': transcript}
            if not_before:
                args['not_before'] = not_before
            return {'arguments': args, 'binding': selected['binding'], 'response': '我先核对这项剩余安排和今天的时间。'}
        uncertain_decision = dialogue and re.search(r'(?:今天|明天|后天).*(?:继续|补上|再做|再说|安排)|不要了|不做了|取消安排|(?:不|别).*(?:继续|取消|再做)|改天|^(?:还剩|剩下|剩余).+分钟', text)
        if not explicit and not ordinal and not decision and not uncertain_decision and not (selected and text == selected['title']):
            return None
        if valid:
            response = (f"{selected['title']}已做过一部分，需要核对具体剩哪些内容、预计还需几分钟；不会把原分段全额返还，也不会根据耗时判完成。"
                if selected.get('scope_review_required') else f"你说的是{selected['title']}，剩余{selected['remaining_minutes']}分钟。今天继续安排、不要这项剩余安排，还是改天再做？")
            return {'dialogue': {'candidates': candidates, 'focus': selected, 'response': response}, 'response': response}
        choices = '；'.join(f"第{index}项，{item['subject']}，{item['title']}，" + ('剩余范围待核实' if item.get('scope_review_required') else f"剩余{item['remaining_minutes']}分钟") for index, item in enumerate(candidates, 1))
        response = ('原焦点或安排版本已变化，请重新选择。' if selected else '') + (
            f'你说的是哪项未排作业？{choices}。请先说第几项。' if candidates else '目前没有可确认的未排作业义务；不会恢复已失效的原作业。')
        return {'dialogue': {'candidates': candidates, 'focus': None, 'response': response}, 'response': response}

    def capture_pet_listening_arrival(self, connection, arrival_id):
        """Freeze identifiers only; actual source/audio eligibility stays with PET media."""
        row = connection.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=?', (arrival_id,)).fetchone()
        items = json.loads(row['snapshot_json'])
        saved = next((item['record'] for item in items if item.get('kind') == 'pet_listening_voice_arrival'), None)
        if saved is not None:
            return saved
        snapshot = {'status': 'missing', 'binding': None, 'local_only': True, 'cloud_transmission_allowed': False}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
            prompts = connection.execute("SELECT * FROM diagnostic_prompt WHERE origin_session_id=? AND child_id=? AND status='waiting_answer'",
                                         (row['session_id'], row['child_id'])).fetchall()
            from .exam_listening_delivery import exam_owner,control_snapshot
            current_exam = None
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='specialized_exam_run'").fetchone():
                current_exam = connection.execute("SELECT * FROM specialized_exam_run WHERE session_id=? AND child_id=? AND status IN ('active','paused')",
                    (row['session_id'],row['child_id'])).fetchone()
            if current_exam and current_exam['delivery_phase'] != 'ordinary':
                items.append({'kind':'exam_listening_control','record':control_snapshot(connection,current_exam)})
                unit = connection.execute("""SELECT u.prompt_id FROM exam_listening_delivery_slot d
                    JOIN exam_listening_delivery_unit u USING(run_id,unit_key) WHERE d.run_id=? AND d.position=?""",
                    (current_exam['run_id'],current_exam['current_position'])).fetchone()
                prompts = [connection.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(unit['prompt_id'],)).fetchone()] if unit and current_exam['status']=='active' else []
            else:
                prompts = [p for p in prompts if not exam_owner(p)]
            if len(prompts) == 1:
                from .pet_media_evidence import _prompt, PetMediaEvidenceError
                prompt = prompts[0]
                try:
                    _, item = _prompt(connection, row['session_id'], prompt['prompt_id'], prompt['item_version'])
                    if item['answer_type'] == 'listening_text' and connection.execute("SELECT 1 FROM sqlite_master WHERE name='pet_media_binding'").fetchone():
                        media = connection.execute("SELECT * FROM pet_media_binding WHERE prompt_id=? AND item_version=? AND child_id=? AND session_id=? AND kind='listening'",
                            (prompt['prompt_id'], prompt['item_version'], row['child_id'], row['session_id'])).fetchall()
                        if len(media) == 1:
                            m = media[0]
                            state = json.loads(m['state_json'])
                            snapshot.update(status=state['status'], binding={
                                'version': 'pet_listening_voice_v1', 'child_id': row['child_id'], 'session_id': row['session_id'],
                                'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                                'media_id': m['media_id'], 'generation': state['generation'],
                                'expected_revision': state['revision'], 'source_version': m['source_version']})
                        elif len(media) > 1:
                            snapshot['status'] = 'ambiguous'
                except PetMediaEvidenceError:
                    snapshot['status'] = 'unavailable'
            elif len(prompts) > 1:
                snapshot['status'] = 'ambiguous'
        if snapshot['binding']:
            snapshot['short_pause_focus'] = ('ambiguous' if self._pet_has_competing_focus(connection, row['session_id'], row['child_id'], exam_run_id=current_exam['run_id'] if current_exam else None)
                else 'ready' if snapshot['status'] == 'playing' else 'inactive')
        items.append({'kind': 'pet_listening_voice_arrival', 'record': snapshot})
        connection.execute('UPDATE learning_input_arrival SET snapshot_json=? WHERE arrival_id=?',
                           (json.dumps(items, ensure_ascii=False), arrival_id))
        return snapshot

    @staticmethod
    def _pet_short_pause(transcript):
        text = re.sub(r'[\s，。！？、,!.?]', '', transcript)
        return bool(re.fullmatch(r'(?:请|帮我)?(?:先)?(?:暂停(?:一下)?|停一下|停一停|等我一下|别放了)(?:我想一下|我想一想|等一下)?(?:吧)?', text))

    @staticmethod
    def _pet_has_competing_focus(connection, session_id, child_id, *, exam_run_id=None):
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('tutor_playback_checkpoint','companion_activity','specialized_exam_run')")}
        for table, state in (('tutor_playback_checkpoint', 'playing'), ('companion_activity', 'active'), ('specialized_exam_run', 'active')):
            if table == 'specialized_exam_run' and exam_run_id:
                if table in tables and connection.execute('SELECT 1 FROM specialized_exam_run WHERE session_id=? AND child_id=? AND status=? AND run_id!=? LIMIT 1',
                        (session_id,child_id,state,exam_run_id)).fetchone():
                    return True
            elif table in tables and connection.execute(f'SELECT 1 FROM {table} WHERE session_id=? AND child_id=? AND status=? LIMIT 1', (session_id, child_id, state)).fetchone():
                return True
        return False

    @staticmethod
    def resolve_pet_listening_voice(snapshot, transcript):
        # Whole utterances only: quoted instructions, negations and mixed task
        # requests must not become playback authorization by substring matching.
        text = re.sub(r'[\s，。！？、,!.?]', '', transcript)
        command = None
        if AgentRuntimeStore._pet_short_pause(transcript) and (snapshot or {}).get('binding'):
            focus = snapshot.get('short_pause_focus')
            if focus == 'ambiguous':
                return {'response': '当前还留着其他活动。你想暂停哪一项？我先不把这句话记作答案，也不改变其他活动。'}
            if focus != 'ready':
                return {'response': '当前原音没有可确认的播放中状态。我不会把暂停请求记成答案，也不会自动重新播放。'}
            command = 'pause'
        elif re.fullmatch(r'(?:请|帮我)?(?:先)?(?:暂停|停一下|暂停一下)(?:这道题的|这道|当前)?(?:听力|听题|原音|音频)(?:播放)?(?:我想一下)?', text):
            command = 'pause'
        elif re.fullmatch(r'(?:请|帮我)?(?:取消|停止|不听了)(?:这道|当前)?(?:听力|听题|原音|音频)(?:播放)?', text):
            command = 'cancel'
        elif re.fullmatch(r'(?:请|帮我)?(?:(?:这道|当前)(?:听力|原音))?(?:再放一遍|重放|从头播放|再听一遍)(?:这道题的|这道|当前)?(?:听力|听题|原音|音频)?', text):
            command = 'restart'
        elif re.fullmatch(r'(?:请|帮我|我想)?(?:继续|播放|开始听|听一下)(?:这道题的|这道|当前)?(?:听力|听题|原音|音频)(?:播放)?', text):
            command = 'resume'
        if command is None:
            return None
        status = snapshot.get('status') if snapshot else 'missing'
        binding = (snapshot or {}).get('binding')
        if not binding or status in {'cancelled', 'invalidated', 'pending_receipt'}:
            response = ('上次播放回执还待核对，先不重复播放。' if status == 'pending_receipt' else
                        '当前题目有多份原音或待答目标，需要先核对对应材料，我还没有播放。' if status == 'ambiguous' else
                        '当前没有可绑定的听力原音，或原题已经变化、取消。我不会用朗读题干代替听力。')
            return {'response': response}
        response = {'pause': '我来暂停这道听力，未完整播放不记作听完。',
                    'cancel': '我来取消这次听力播放，保留已有记录，不算完成。',
                    'restart': '我来从头重播当前原音，不把播放当作听懂。',
                    'resume': '我来继续听当前原音；当前播放器会从这一段起点重播。'}[command]
        return {'binding': binding, 'arguments': {'command': command, 'request_quote': transcript}, 'response': response}

    @staticmethod
    def inspect_pet_listening_control(connection, turn_id, *, allow_planned=False):
        """Read-only admission; host must call in the media control write transaction."""
        row = connection.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,
            s.arguments_json,s.status AS step_status,ds.status AS session_status,ds.state AS session_state,
            (SELECT status FROM child_profile WHERE child_id=t.child_id) AS child_status
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            JOIN agent_step s USING(plan_id) JOIN daily_session ds ON ds.session_id=t.session_id AND ds.child_id=t.child_id
            WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id
              AND s.step_index=0 AND s.capability_name='pet.listening_control' ''', (turn_id,)).fetchone()
        states = {'planned', 'running'} if allow_planned else {'running'}
        if (not row or row['intent'] == 'runtime_continuation' or row['tool_status'] != 'pending'
                or row['plan_status'] not in states or row['step_status'] != row['plan_status'] or row['session_status'] != 'active'
                or row['session_state'] in {'evolving', 'completed'} or row['child_status'] != 'active'):
            raise InvalidAgentPlanError('PET_LISTENING_COMMAND_NOT_EXECUTABLE')
        latest = connection.execute('SELECT agent_turn_id FROM agent_turn WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (row['session_id'],)).fetchone()
        if not latest or latest[0] != turn_id:
            raise InvalidAgentPlanError('PET_LISTENING_COMMAND_SUPERSEDED')
        manifest = json.loads(row['context_manifest_json'])
        from .realtime_native_special_sources import validate as validate_native_special
        validate_native_special(connection, turn_id)
        binding = manifest.get('pet_listening_binding')
        arrival = connection.execute('SELECT * FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (row['session_id'],)).fetchone()
        if (not arrival or arrival['arrival_id'] != manifest.get('learning_input_arrival_id')
                or arrival['child_id'] != row['child_id'] or arrival['utterance'] != row['transcript']
                or arrival['client_action_id'] != row['client_action_id']):
            raise InvalidAgentPlanError('PET_LISTENING_ARRIVAL_SUPERSEDED')
        args = json.loads(row['arguments_json'])
        action = json.loads(row['action_json'])
        if (not binding or action.get('pet_listening_binding') != binding or args.get('request_quote') != row['transcript']
                or any(action.get(field) != args.get(field) for field in ('command', 'request_quote'))
                or binding['child_id'] != row['child_id'] or binding['session_id'] != row['session_id']):
            raise InvalidAgentPlanError('PET_LISTENING_COMMAND_BINDING_CHANGED')
        from .pet_media_evidence import _prompt, PetMediaEvidenceError
        try:
            prompt, _ = _prompt(connection, row['session_id'], binding['prompt_id'], binding['item_version'])
        except PetMediaEvidenceError as exc:
            raise InvalidAgentPlanError('PET_LISTENING_PROMPT_CHANGED') from exc
        media = connection.execute("SELECT * FROM pet_media_binding WHERE media_id=? AND child_id=? AND session_id=? AND prompt_id=? AND kind='listening'",
            (binding['media_id'], row['child_id'], row['session_id'], binding['prompt_id'])).fetchone()
        state = json.loads(media['state_json']) if media else {}
        if (prompt['status'] != 'waiting_answer' or not media or media['source_version'] != binding['source_version']
                or state.get('generation') != binding['generation'] or state.get('revision') != binding['expected_revision']
                or state.get('status') in {'cancelled', 'pending_receipt'}):
            raise InvalidAgentPlanError('PET_LISTENING_MEDIA_CHANGED')
        if AgentRuntimeStore._pet_short_pause(row['transcript']) and (
                state.get('status') != 'playing' or AgentRuntimeStore._pet_has_competing_focus(connection, row['session_id'], row['child_id'])):
            raise InvalidAgentPlanError('PET_LISTENING_FOCUS_CHANGED')
        return {'session_id': row['session_id'], 'media_id': binding['media_id'],
                'generation': binding['generation'], 'expected_revision': binding['expected_revision'],
                'command': args['command'], 'client_action_id': f'{turn_id}:pet:control'}

    @staticmethod
    def admit_pet_listening_control(connection, turn_id):
        """CAS a real ordinary voice action inside the host's control write transaction.

        Recorded means receipt recovery only, never permission to replay audio.
        The caller owns rollback when source checks or the media write fail.
        """
        if not connection.in_transaction:
            raise InvalidAgentPlanError('PET_LISTENING_WRITE_TRANSACTION_REQUIRED')
        row = connection.execute('''SELECT t.*,p.plan_id,p.context_manifest_json,s.arguments_json,s.step_id
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND t.child_id=p.child_id AND t.session_id=p.session_id
            AND s.step_index=0 AND s.capability_name='pet.listening_control' AND s.capability_version='1'
            AND s.execution_location='client_device_bridge' ''', (turn_id,)).fetchone()
        if not row or row['intent'] == 'runtime_continuation':
            raise InvalidAgentPlanError('PET_LISTENING_CHILD_COMMAND_REQUIRED')
        action = json.loads(row['action_json'])
        args = json.loads(row['arguments_json'])
        binding = json.loads(row['context_manifest_json']).get('pet_listening_binding') or {}
        if (args.get('request_quote') != row['transcript'] or action.get('pet_listening_binding') != binding
                or binding.get('child_id') != row['child_id'] or binding.get('session_id') != row['session_id']):
            raise InvalidAgentPlanError('PET_LISTENING_COMMAND_BINDING_CHANGED')
        key = f'{turn_id}:pet:control'
        event = connection.execute('''SELECT e.* FROM pet_media_event e JOIN pet_media_binding m USING(media_id)
            WHERE e.client_action_id=? AND e.operation='control' AND m.media_id=? AND m.child_id=? AND m.session_id=?
            AND m.prompt_id=? AND m.item_version=?''', (key, binding.get('media_id'), row['child_id'], row['session_id'],
            binding.get('prompt_id'), binding.get('item_version'))).fetchone()
        expected = {'command': args.get('command'), 'generation': binding.get('generation'), 'expected_revision': binding.get('expected_revision')}
        if event:
            if json.loads(event['payload_json']) != expected or event['generation'] != binding.get('generation', 0) + 1:
                raise InvalidAgentPlanError('PET_LISTENING_RECEIPT_CONFLICT')
            return {'status': 'recorded', 'control_action_id': key, 'media_id': binding['media_id'],
                    'event_id': event['event_id'], 'replay_audio_allowed': False}
        control = AgentRuntimeStore.inspect_pet_listening_control(connection, turn_id, allow_planned=True)
        from .decision_record import validate_current, StaleDecisionContextError
        try:
            validate_current(connection, row['plan_id'])
        except StaleDecisionContextError as error:
            raise InvalidAgentPlanError(str(error)) from error
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("UPDATE agent_plan SET status='running',updated_at=? WHERE plan_id=? AND status='planned'", (now, row['plan_id']))
        connection.execute("UPDATE agent_step SET status='running',updated_at=? WHERE step_id=? AND status='planned'", (now, row['step_id']))
        return {'status': 'admitted', 'control': control}

    def persist_plan(
        self,
        connection,
        *,
        child_id: str,
        session_id: str,
        agent_turn_id: str,
        client_action_id: str,
        decision: AgentPlanDecision,
        context_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        from .learning_reply_reservation import KEY as learning_source_key, admit as admit_learning_source
        # Neither a model nor a caller can supply this server-only consumption record.
        context_manifest.pop(learning_source_key, None)
        learning_source = admit_learning_source(connection, agent_turn_id, child_id, session_id, decision.step.capability.name, decision.step.arguments)
        local_recovery = 'local_recovery_control' in context_manifest
        pending_independent = context_manifest.get('pending_evaluation_independent_v1')
        if pending_independent:
            from .pending_evaluation_control import validate_independent
            turn = connection.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                                      (agent_turn_id, child_id, session_id)).fetchone()
            if not turn:
                raise InvalidAgentPlanError('PENDING_EVALUATION_CONTROL_INDEPENDENT_BINDING_CHANGED')
            validate_independent(connection, session_id, child_id, client_action_id, turn['transcript'],
                                 context_manifest, decision=decision)
        foreground_independent = context_manifest.get('foreground_independent_dialogue_v1')
        if foreground_independent:
            from .foreground_independent_dialogue import capabilities, validate
            turn = connection.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                                      (agent_turn_id, child_id, session_id)).fetchone()
            if (not turn or decision.step.capability.name not in capabilities(foreground_independent)
                    or decision.objective.mode != 'none' or decision.requires_child_reply
                    or decision.after_execution != 'stop' or decision.continuation_scope):
                raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_PLAN_CHANGED')
            validate(connection, session_id, child_id, client_action_id, turn['transcript'], foreground_independent)
        if decision.step.capability.name in {'oral.retry','oral.independent_check'}:
            turn = connection.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                (agent_turn_id,child_id,session_id)).fetchone()
            if not turn or decision.after_execution!='stop' or decision.continuation_scope:
                raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_COMMAND_REQUIRED')
            frozen = self._validate_oral_followup_authority(connection,turn,json.loads(turn['action_json']),context_manifest)
            decision = replace(decision,objective=ObjectiveDraft('none',None,None,(),'execution'),
                               after_execution='stop',continuation_scope=())
            context_manifest['oral_followup_origin']={'objective_id':frozen['objective_id'],'parent':frozen['parent']}
        if local_recovery:
            from .local_voice_recovery import validate_plan
            validate_plan(connection, child_id=child_id, session_id=session_id,
                agent_turn_id=agent_turn_id, decision=decision, context_manifest=context_manifest)
            decision = replace(decision, objective=ObjectiveDraft('none', None, None, (), 'execute'),
                               after_execution='stop', continuation_scope=())
        if decision.step.capability.name.startswith('writing.'):
            from .writing_ocr_authorization import _confirmation, arrival_binding
            turn = connection.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                (agent_turn_id, child_id, session_id)).fetchone()
            source = context_manifest.get('writing_voice_binding')
            if not turn or not source or turn['intent'] == 'runtime_continuation' or decision.after_execution != 'stop':
                raise InvalidAgentPlanError('ANSWER_WRITING_LOCAL_BINDING_REQUIRED')
            action = json.loads(turn['action_json'])
            from .realtime_native_special_sources import source_witness
            native = source_witness(connection, turn, action, context_manifest, decision.step.capability.name)
            if action.get('request_quote') != turn['transcript'] or source.get('request_quote') != turn['transcript']:
                raise InvalidAgentPlanError('ANSWER_WRITING_FULL_QUOTE_REQUIRED')
            if decision.step.capability.name == 'writing.read_original':
                packet = action.get('writing_ocr_confirmation') or {}
                if packet != source.get('confirmation'):
                    raise InvalidAgentPlanError('ANSWER_WRITING_LOCAL_BINDING_REQUIRED')
                _confirmation(connection, session_id, packet.get('confirmation_id'), packet.get('binding_version'), live=True)
            elif decision.step.capability.name == 'writing.confirm_original':
                b = action.get('writing_ocr_binding') or {}
                actual = arrival_binding(connection, session_id=session_id, confirmation_id=b.get('confirmation_id'),
                    binding_version=b.get('binding_version'), voice_input_id=action.get('voice_input_id'))
                receipt = connection.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
                    ((action.get('writing_ocr_semantic_receipt') or {}).get('request_key'),)).fetchone()
                fingerprint = hashlib.sha256(json.dumps([child_id, session_id, turn['transcript']], ensure_ascii=False).encode()).hexdigest()
                if (actual != b or source.get('confirmation') != b or (native is None and (not receipt or receipt['status'] != 'completed'
                        or receipt['fingerprint'] != fingerprint or receipt['child_id'] != child_id or receipt['session_id'] != session_id
                        or json.loads(receipt['binding_json']).get('writing_ocr') != b
                        or json.loads(receipt['decision_json']) != {'status': 'control', 'control': 'writing.confirm_original'}))):
                    raise InvalidAgentPlanError('ANSWER_WRITING_SEMANTIC_RECEIPT_REQUIRED')
            elif action.get('writing_followup_binding'):
                frozen = self._validate_writing_followup_authority(connection, turn, action, context_manifest)
                context_manifest['writing_followup_origin'] = {k:frozen[k] for k in ('parent','feedback_turn_id','objective_id')}
            else:
                target = action.get('writing_revision_binding') or {}
                self.validate_writing_reference_execution(connection, target.get('reference_turn_id'))
                reference = connection.execute('''SELECT t.child_id,t.session_id,p.context_manifest_json,s.status
                    FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
                    WHERE t.agent_turn_id=? AND s.step_index=0''', (target.get('reference_turn_id'),)).fetchone()
                local = json.loads(reference['context_manifest_json']).get('writing_reference') if reference else None
                expected = ({k:local[k] for k in ('prompt_id','item_version','source_version')}
                            if local else {})
                expected['reference_turn_id'] = target.get('reference_turn_id')
                receipt = connection.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
                    ((action.get('writing_revision_semantic_receipt') or {}).get('request_key'),)).fetchone()
                fingerprint = hashlib.sha256(json.dumps([child_id, session_id, turn['transcript']], ensure_ascii=False).encode()).hexdigest()
                if (not reference or reference['status'] != 'completed' or reference['child_id'] != child_id
                        or reference['session_id'] != session_id or not local or target != expected or source.get('revision') != target
                        or (native is None and (not receipt or receipt['status'] != 'completed' or receipt['fingerprint'] != fingerprint
                        or json.loads(receipt['binding_json']).get('writing_revision') != target
                        or json.loads(receipt['decision_json']) != {'status': 'control', 'control': 'writing.revise_original'}))):
                    raise InvalidAgentPlanError('ANSWER_WRITING_REVISION_SCOPE_CHANGED')
            if action.get('writing_followup_binding') and frozen.get('goal_status') == 'completed':
                decision = replace(decision, objective=ObjectiveDraft('new','learning','根据已有反馈独立练习写作',
                    tuple(frozen['task_points']),'check'))
            else:
                decision = replace(decision, objective=ObjectiveDraft('none', None, None, (), 'execution'))
        if decision.step.capability.name == 'pet.listening_control' or 'pet_listening_dialogue' in context_manifest:
            turn = connection.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                                      (agent_turn_id, child_id, session_id)).fetchone()
            arrival = connection.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=? AND client_action_id=?',
                (context_manifest.get('learning_input_arrival_id'), child_id, session_id, client_action_id)).fetchone()
            latest = connection.execute('SELECT arrival_id FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (session_id,)).fetchone()
            if (not turn or not arrival or not latest or latest[0] != arrival['arrival_id']
                    or turn['transcript'] != arrival['utterance'] or turn['intent'] == 'runtime_continuation'):
                raise InvalidAgentPlanError('PET_LISTENING_EXPLICIT_ARRIVAL_REQUIRED')
            snapshot = next((item['record'] for item in json.loads(arrival['snapshot_json']) if item.get('kind') == 'pet_listening_voice_arrival'), None)
            from .realtime_native_special_sources import source_witness
            native = source_witness(connection, turn, json.loads(turn['action_json']), context_manifest, decision.step.capability.name)
            resolved = ({'arguments': decision.step.arguments, 'binding': (snapshot or {}).get('binding')} if native is not None
                        else self.resolve_pet_listening_voice(snapshot, turn['transcript']))
            if (not resolved or decision.after_execution != 'stop' or decision.continuation_scope
                    or ('arguments' in resolved and (decision.step.capability.name != 'pet.listening_control'
                        or decision.step.arguments != resolved['arguments'] or context_manifest.get('pet_listening_binding') != resolved['binding']))
                    or ('arguments' not in resolved and decision.step.capability.name != 'conversation.respond')):
                raise InvalidAgentPlanError('PET_LISTENING_FROZEN_COMMAND_REQUIRED')
            decision = replace(decision, objective=ObjectiveDraft('none', None, None, (), 'execution'))
        if any(key in context_manifest for key in ("companion_activity_selection", "companion_activity_focus_binding")):
            connection.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.local_only',json('true'),'$.cloud_transmission_allowed',json('false')) WHERE agent_turn_id=? AND child_id=? AND session_id=?",
                               (agent_turn_id, child_id, session_id))
        if decision.step.capability.name in {'learning_inputs.review_unplaced', 'learning_inputs.review_reclaimed_scope'} or 'learning_input_unplaced_dialogue' in context_manifest:
            turn = connection.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                                      (agent_turn_id, child_id, session_id)).fetchone()
            arrival = connection.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=? AND client_action_id=?',
                (context_manifest.get('learning_input_arrival_id'), child_id, session_id, client_action_id)).fetchone()
            latest = connection.execute('SELECT arrival_id FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (session_id,)).fetchone()
            if not turn or not arrival or not latest or latest[0] != arrival['arrival_id'] or turn['transcript'] != arrival['utterance'] or turn['intent'] == 'runtime_continuation':
                raise InvalidAgentPlanError('CARRYOVER_EXPLICIT_ARRIVAL_REQUIRED')
            snapshot = next((item['record'] for item in json.loads(arrival['snapshot_json']) if item.get('kind') == 'unplaced_voice_arrival'), None)
            from .realtime_native_sources import resolve_unplaced_native
            resolved = resolve_unplaced_native(connection,turn,context_manifest,decision.step.arguments,snapshot) if snapshot else None
            if resolved is None:resolved = self.resolve_unplaced_voice(snapshot, turn['transcript']) if snapshot else None
            if not resolved:
                raise InvalidAgentPlanError('CARRYOVER_LOCAL_FOCUS_REQUIRED')
            if decision.step.capability.name in {'learning_inputs.review_unplaced', 'learning_inputs.review_reclaimed_scope'}:
                if (decision.step.capability.name != resolved.get('capability', 'learning_inputs.review_unplaced') or decision.step.arguments != resolved.get('arguments') or context_manifest.get('learning_input_unplaced_binding') != resolved.get('binding')):
                    raise InvalidAgentPlanError('CARRYOVER_FROZEN_DECISION_REQUIRED')
                if context_manifest.get('learning_input_scope_offer') != resolved.get('scope_offer'):
                    raise InvalidAgentPlanError('CARRYOVER_SCOPE_OFFER_CHANGED')
            elif (decision.step.capability.name != 'conversation.respond' or context_manifest.get('learning_input_unplaced_dialogue') != resolved.get('dialogue')):
                raise InvalidAgentPlanError('CARRYOVER_DIALOGUE_BINDING_INVALID')
            if decision.after_execution != 'stop' or decision.continuation_scope:
                raise InvalidAgentPlanError('CARRYOVER_AUTOMATIC_DECISION_FORBIDDEN')
            decision = replace(decision, objective=ObjectiveDraft('none', None, None, (), 'execution'))
            connection.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.local_only',json('true'),'$.cloud_transmission_allowed',json('false')) WHERE agent_turn_id=?", (agent_turn_id,))
        elif decision.step.capability.name.startswith("learning_inputs."):
            from .learning_input_revision import validate_arguments, InputRevisionError
            try:
                validate_arguments(decision.step.capability.name, decision.step.arguments)
            except InputRevisionError as error:
                raise InvalidAgentPlanError(str(error)) from error
            turn = connection.execute("SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?",
                                      (agent_turn_id, child_id, session_id)).fetchone()
            arrival = connection.execute("SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=? AND client_action_id=?",
                (context_manifest.get("learning_input_arrival_id"), child_id, session_id, client_action_id)).fetchone()
            if not turn or not arrival or turn["transcript"] != arrival["utterance"] or turn["intent"] == "runtime_continuation":
                raise InvalidAgentPlanError("INPUT_REVISION_ARRIVAL_REQUIRED")
            decision = replace(decision, objective=ObjectiveDraft("none", None, None, (), "execution"),
                               after_execution="stop", continuation_scope=())
            action = json.loads(turn["action_json"])
            action.update(local_only=True, cloud_transmission_allowed=False)
            connection.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?", (json.dumps(action, ensure_ascii=False), agent_turn_id))
        if decision.step.capability.name == "companion.activity_feedback":
            turn = connection.execute("SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?",
                                      (agent_turn_id, child_id, session_id)).fetchone()
            if not turn or turn["intent"] == "runtime_continuation":
                raise InvalidAgentPlanError("ACTIVITY_FEEDBACK_CHILD_TURN_REQUIRED")
            _validate_activity_feedback_arguments(decision.step.arguments, turn["transcript"])
            binding = context_manifest.get("companion_activity_outcome_binding")
            if (not isinstance(binding, dict) or set(binding) != {"outcome_id", "expected_revision", "child_id", "session_id"}
                    or binding["child_id"] != child_id or binding["session_id"] != session_id
                    or not isinstance(binding["outcome_id"], str) or not binding["outcome_id"]
                    or type(binding["expected_revision"]) is not int or binding["expected_revision"] < 1):
                raise InvalidAgentPlanError("ACTIVITY_FEEDBACK_BINDING_REQUIRED")
            if not connection.execute("SELECT 1 FROM companion_activity_outcome WHERE outcome_id=? AND child_id=? AND session_id=?",
                                      (binding["outcome_id"], child_id, session_id)).fetchone():
                raise InvalidAgentPlanError("ACTIVITY_FEEDBACK_OWNER_MISMATCH")
            decision = replace(decision, objective=ObjectiveDraft("none", None, None, (), "execution"),
                               after_execution="stop", continuation_scope=())
            action = json.loads(turn["action_json"])
            action.update(local_only=True, cloud_transmission_allowed=False)
            connection.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?", (json.dumps(action, ensure_ascii=False), agent_turn_id))
        if not local_recovery and not foreground_independent and not pending_independent:
            self.reconcile_learning_state(child_id, connection=connection)
        now = datetime.now(timezone.utc).isoformat()
        active = None if local_recovery else connection.execute(
            "SELECT * FROM agent_objective WHERE child_id = ? AND status = 'active'",
            (child_id,),
        ).fetchone()
        objective_id = active["objective_id"] if active else None
        turn_action = connection.execute("SELECT action_json FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?",
                                         (agent_turn_id, child_id, session_id)).fetchone()
        clarifying_answer = bool(turn_action and json.loads(turn_action[0]).get("source") in {
            "pending_answer_clarification", "playback_overlap_clarification", "writing_scope_clarification", "task_navigation_clarification",
            "writing_target_clarification", "writing_confirmation_clarification"})
        if foreground_independent or pending_independent:
            objective_id = None
        elif decision.step.capability.name in OBJECTIVE_NEUTRAL_CAPABILITIES:
            decision = self._neutral_query_plan(decision)
            objective_id = None
        elif clarifying_answer:
            # Enforce again in the write boundary, including deterministic and
            # direct callers. Do not attach a query receipt to an unrelated goal.
            decision = replace(decision, objective=ObjectiveDraft("none", None, None, (), "execute"))
            objective_id = None
        elif (active and active['objective_type'] == 'learning'
                and decision.objective.mode == 'none'
                and decision.step.capability.name == 'conversation.respond'
                and not context_manifest.get('writing_reference')
                and not context_manifest.get('local_learning_binding')):
            # An explicitly neutral reply is not a new task in the manuscript's
            # learning lineage. Bound local teaching replies keep their goal.
            current_prompt = connection.execute('''SELECT q.prompt_id FROM diagnostic_prompt q
                JOIN diagnostic_item i USING(diagnostic_item_id)
                JOIN agent_plan p ON p.child_id=q.child_id AND p.session_id=q.origin_session_id
                  AND json_extract(p.plan_json,'$.local_learning_binding.prompt_id')=q.prompt_id
                  AND json_extract(p.plan_json,'$.local_learning_binding.item_version')=q.item_version
                WHERE q.child_id=? AND q.origin_session_id=? AND q.status='answered'
                  AND i.answer_type IN ('writing','open_response','speaking','read_aloud') AND p.objective_id=?
                  AND q.rowid=(SELECT MAX(rowid) FROM diagnostic_prompt WHERE origin_session_id=?)
                LIMIT 1''', (child_id,session_id,active['objective_id'],session_id)).fetchone()
            if current_prompt:
                objective_id = None
        learning_binding = None
        recheck_binding = None
        if decision.step.capability.name == 'task.complete':
            from .writing_daily_followup import admit_completion
            if admit_completion(connection,agent_turn_id,context_manifest):
                decision=replace(decision,objective=ObjectiveDraft('none',None,None,(),'execution'),
                    after_execution='stop',continuation_scope=(),requires_child_reply=False)
                objective_id=None
        if decision.step.capability.name == "task.start":
            from .writing_daily_followup import admit_plan
            daily=admit_plan(connection,agent_turn_id,context_manifest)
            if daily:
                decision=replace(decision,objective=ObjectiveDraft('new','learning',
                    '继续原稿，逐项补充本题要求',tuple(daily['criteria']),'remediate'),
                    requires_child_reply=False,after_execution='stop',continuation_scope=())
            selected = self._next_scheduled_task(connection, child_id, session_id)
            from .closed_review_execution import is_closed_review
            if selected and is_closed_review(connection, selected):
                # This review already owns its source evidence. Inheriting an
                # unrelated active goal can attach today's prompt to yesterday.
                decision = replace(decision, objective=ObjectiveDraft('none', None, None, (), 'execution'),
                    after_execution='stop', continuation_scope=())
                objective_id = None
            commitment = connection.execute("""SELECT ac.*,ao.phase,ao.success_criteria_json FROM agent_commitment ac
                JOIN agent_objective ao ON ao.objective_id=ac.objective_id AND ao.child_id=ac.child_id
                WHERE ac.commitment_id=? AND ac.child_id=? AND ao.objective_type='learning'
                  AND json_extract(ac.payload_json,'$.origin')='local_learning_gate'""",
                (selected["source_ref"], child_id)).fetchone() if selected and selected["source_type"] == "agent_retention" else None
            if commitment:
                if active and active["objective_id"] != commitment["objective_id"]:
                    raise InvalidAgentPlanError("LOCAL_RECHECK_OTHER_OBJECTIVE_ACTIVE")
                if decision.objective.mode == "new" and decision.objective.objective_type != "execution":
                    raise InvalidAgentPlanError("LOCAL_RECHECK_EXECUTION_OBJECTIVE_REQUIRED")
                if decision.objective.mode != "new":
                    # Starting a scheduled check does not revise its learning
                    # target or adopt a planner's generic execution criteria.
                    decision = replace(decision, objective=ObjectiveDraft("none", None, None, (), commitment["phase"]))
                payload = json.loads(commitment["payload_json"])
                recheck_binding = {"version": "local_recheck_task_binding_v1", "child_id": child_id,
                    "authorized_turn_id": agent_turn_id,
                    "session_id": session_id, "daily_plan_id": selected["plan_id"], "plan_item_id": selected["plan_item_id"],
                    "started_at": selected["started_at"], "commitment_id": commitment["commitment_id"],
                    "learning_objective_id": commitment["objective_id"], "objective_phase": commitment["phase"],
                    "phase": payload["phase"], "source_outcome_id": payload["source_outcome_id"],
                    "success_criteria": json.loads(commitment["success_criteria_json"]),
                    "local_only": True, "cloud_transmission_allowed": False}
        if (decision.step.capability.name == "diagnostic.start" and active and active["objective_type"] == "learning"
                and decision.objective.mode in {"none", "continue", "pause"}):
            learning_binding = {"version": "diagnostic_objective_binding_v1", "objective_id": objective_id,
                "child_id": child_id, "session_id": session_id, "phase": active["phase"],
                "success_criteria": json.loads(active["success_criteria_json"]), "created_at": now,
                "local_only": True, "cloud_transmission_allowed": False}
            # A diagnostic is work on this learning goal, not an unrelated goal
            # switch. Model-generated criteria cannot replace its frozen target.
            decision = replace(decision, objective=ObjectiveDraft("continue", "learning", None, (), active["phase"]))
        if decision.step.capability.name in {"diagnostic.submit", "diagnostic.replay"}:
            # Reply routing never borrows a newly active objective.
            decision = replace(decision, objective=ObjectiveDraft("none", None, None, (), decision.objective.phase))
            objective_id = None
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
                prompt_id = decision.step.arguments.get("prompt_id") or decision.step.arguments.get("diagnostic_prompt_id")
                prompt = connection.execute("""SELECT prompt_id,item_version FROM diagnostic_prompt
                    WHERE child_id=? AND origin_session_id=? AND owner_kind='diagnostic'
                      AND json_extract(request_json,'$.exam_slot') IS NULL AND
                      ((? IS NOT NULL AND prompt_id=?) OR (? IS NULL AND status IN ('waiting_answer','answer_saved')))
                    ORDER BY rowid DESC LIMIT 1""", (child_id, session_id, prompt_id, prompt_id, prompt_id)).fetchone()
                origin = connection.execute("""SELECT plan_json FROM agent_plan p JOIN agent_step s USING(plan_id)
                    WHERE p.child_id=? AND p.session_id=? AND s.capability_name='diagnostic.start'
                      AND json_extract(plan_json,'$.local_learning_binding.prompt_id')=?
                      AND json_extract(plan_json,'$.local_learning_binding.item_version')=? ORDER BY p.rowid LIMIT 1""",
                    (child_id, session_id, prompt[0], prompt[1])).fetchone() if prompt else None
                if origin:
                    learning_binding = json.loads(origin[0])["local_learning_binding"]
                    objective_id = learning_binding["objective_id"]
        mode = decision.objective.mode
        # Recheck the persisted target, not the model's claimed type or an old context.
        if _requests_learning_completion(decision, active):
            raise InvalidAgentPlanError("learning completion requires the local outcome evaluator")
        if mode == "new":
            if active:
                connection.execute(
                    "UPDATE agent_objective SET status = 'paused', updated_at = ? WHERE objective_id = ?",
                    (now, objective_id),
                )
            objective_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO agent_objective(
                  objective_id, child_id, session_id, objective_type, summary,
                  success_criteria_json, phase, status, created_at, updated_at,
                  last_agent_turn_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    objective_id, child_id, session_id, decision.objective.objective_type,
                    decision.objective.summary,
                    json.dumps(decision.objective.success_criteria, ensure_ascii=False),
                    decision.objective.phase, now, now, agent_turn_id,
                ),
            )
        elif mode == "continue":
            if not active:
                if not decision.objective.summary or not decision.objective.objective_type:
                    raise InvalidAgentPlanError("cannot continue a missing objective")
                objective_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO agent_objective(
                      objective_id, child_id, session_id, objective_type, summary,
                      success_criteria_json, phase, status, created_at, updated_at,
                      last_agent_turn_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        objective_id, child_id, session_id, decision.objective.objective_type,
                        decision.objective.summary,
                        json.dumps(decision.objective.success_criteria, ensure_ascii=False),
                        decision.objective.phase, now, now, agent_turn_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE agent_objective SET phase = ?,
                      summary = COALESCE(?, summary),
                      success_criteria_json = CASE WHEN ? != '[]' THEN ? ELSE success_criteria_json END,
                      updated_at = ?, last_agent_turn_id = ?
                    WHERE objective_id = ?
                    """,
                    (
                        decision.objective.phase,
                        decision.objective.summary,
                        json.dumps(decision.objective.success_criteria, ensure_ascii=False),
                        json.dumps(decision.objective.success_criteria, ensure_ascii=False),
                        now,
                        agent_turn_id,
                        objective_id,
                    ),
                )
        elif mode in {"pause", "complete", "cancel"} and active:
            status = {"pause": "paused", "complete": "completed", "cancel": "cancelled"}[mode]
            connection.execute(
                """
                UPDATE agent_objective SET status = ?, phase = ?, updated_at = ?,
                       completed_at = CASE WHEN ? IN ('completed', 'cancelled') THEN ? ELSE completed_at END,
                       last_agent_turn_id = ? WHERE objective_id = ?
                """,
                (status, decision.objective.phase, now, status, now, agent_turn_id, objective_id),
            )
        plan_id = str(uuid4())
        step_id = str(uuid4())
        plan = decision.to_dict()
        if decision.step.capability.name == "diagnostic.start" and not learning_binding and objective_id:
            target = connection.execute("SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND objective_type='learning' AND status='active'",
                                        (objective_id, child_id)).fetchone()
            if target:
                learning_binding = {"version": "diagnostic_objective_binding_v1", "objective_id": objective_id,
                    "child_id": child_id, "session_id": session_id, "phase": target["phase"],
                    "success_criteria": json.loads(target["success_criteria_json"]), "created_at": now,
                    "local_only": True, "cloud_transmission_allowed": False}
        if learning_binding:
            plan["local_learning_binding"] = learning_binding
        if recheck_binding:
            plan["local_recheck_binding"] = recheck_binding
            connection.execute("""UPDATE agent_turn SET action_json=json_set(action_json,
                '$.local_only',json('true'),'$.cloud_transmission_allowed',json('false'))
                WHERE agent_turn_id=? AND child_id=? AND session_id=?""", (agent_turn_id, child_id, session_id))
        plan_status = "waiting_child" if decision.requires_child_reply else "planned"
        stored_context_manifest = context_manifest
        if learning_source is not None:
            from .learning_reply_reservation import seal as seal_learning_source
            stored_context_manifest = {**context_manifest, learning_source_key: seal_learning_source(connection, learning_source, plan_id, objective_id)}
        connection.execute(
            """
            INSERT INTO agent_plan(
              plan_id, objective_id, child_id, session_id, agent_turn_id,
              planner_version, context_manifest_json, plan_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id, objective_id, child_id, session_id, agent_turn_id,
                PLANNER_VERSION, json.dumps(stored_context_manifest, ensure_ascii=False),
                json.dumps(plan, ensure_ascii=False), plan_status, now, now,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_step(
              step_id, plan_id, step_index, capability_name, capability_version,
              execution_location, risk_level, arguments_json, expected_evidence_json,
              status, idempotency_key, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step_id, plan_id, decision.step.capability.name,
                decision.step.capability.version, decision.step.capability.execution_location,
                decision.step.capability.risk_level,
                json.dumps(decision.step.arguments, ensure_ascii=False),
                json.dumps(decision.step.expected_evidence, ensure_ascii=False),
                plan_status, f"agent-step:{client_action_id}:0", now, now,
            ),
        )
        from .decision_record import persist as persist_decision_record, StaleDecisionContextError
        try:
            decision_record_id = persist_decision_record(
                connection, plan_id=plan_id, child_id=child_id, session_id=session_id,
                agent_turn_id=agent_turn_id, decision=decision,
                context_manifest=context_manifest, planner_version=PLANNER_VERSION,
                created_at=now,
            )
        except StaleDecisionContextError as error:
            raise InvalidAgentPlanError(str(error)) from error
        return {
            "plan_id": plan_id,
            "step_id": step_id,
            "decision_record_id": decision_record_id,
            "objective_id": objective_id,
            "objective": plan["objective"],
            "capability": plan["next_step"],
            "requires_child_reply": decision.requires_child_reply,
            "after_execution": decision.after_execution,
        }

    @staticmethod
    def execution_result_blocked(result: dict[str, Any] | None) -> bool:
        envelope = result if isinstance(result, dict) else {}
        values = [envelope, envelope.get("result")]
        for key in ("execution_receipt", "client_bridge_receipt"):
            receipt = envelope.get(key)
            if isinstance(receipt, dict):
                values.extend((receipt, receipt.get("output")))
        return any(isinstance(value, dict) and value.get("status") in {"blocked", "conflict"}
                   for value in values)

    @staticmethod
    def is_answer_role_confirmation(text: str) -> bool:
        value = re.sub(r"[\s，。！!、,]", "", text)
        return bool(re.fullmatch(
            r"(?:对|是的|没错)?(?:刚才(?:那句|那句话|说的)?|那句|那句话|这句|我刚才|我)?"
            r"(?:就是|是|是在|说的就是)(?:我(?:的)?(?:在)?)?(?:答案|回答|回答这题|回答这道题|在回答这题|在回答这道题)", value))

    @staticmethod
    def local_mistake_answer_target(connection, session_id: str, child_id: str, *, authorized_turn_id=None) -> dict[str, Any] | None:
        """Fingerprint the displayed source, not its score or qualification."""
        row = connection.execute("""SELECT d.*,m.fingerprint AS case_fingerprint,m.source_ref AS case_source FROM mistake_dialogue d
            JOIN mistake_case m ON m.mistake_id=d.mistake_id AND m.child_id=d.child_id
            JOIN daily_session ds ON ds.session_id=d.session_id AND ds.child_id=d.child_id
            WHERE d.session_id=? AND d.child_id=? AND d.phase IN ('attempt','diagnosis_probe')
              AND m.status!='excluded' AND ds.status='active' AND ds.state='study'""",
            (session_id, child_id)).fetchone()
        if not row:
            return None
        state = json.loads(row['state_json'])
        reserved = state.get('pending_operation')
        if reserved and (authorized_turn_id is None or reserved.get('authorized_turn_id') != authorized_turn_id):
            return None
        shown = ((state.get('next_activity') or {}).get('item') or {}).get('diagnostic_item_id')
        if row['phase'] == 'diagnosis_probe':
            from .diagnostic_prompt import _TransactionDatabase
            from .mistake_diagnosis import MistakeDiagnosisService
            diagnosis = MistakeDiagnosisService(_TransactionDatabase(connection)).project(
                connection, row['mistake_id'], child_id)
            probe = next((p for p in diagnosis['probes'] if p['event_id'] == state.get('diagnosis_probe_id')), None)
            if (not probe or probe['binding_status']
                    or probe['binding']['item']['diagnostic_item_id'] != shown
                    or any(not h['original_current'] for h in diagnosis['hypotheses'])):
                return None
        item = connection.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=? AND status='active'", (shown,)).fetchone()
        if not item:
            return None
        source_state = {key: state.get(key) for key in ('next_activity', 'diagnosis_probe_id', 'diagnosis_hinted', 'attempt_hinted')}
        if state.get('last_pending_withdrawal'):
            # Same question, new answer generation: pre-withdrawal voice captures
            # must not resurrect a withdrawn answer under a new request ID.
            source_state['pending_answer_withdrawal'] = state['last_pending_withdrawal']
        from .mistake_plan_completion import active_binding
        source_state['daily_task'] = active_binding(connection, dict(row))
        from .paper_formula_review import for_mistake
        formula_review = for_mistake(connection, row['mistake_id'], child_id)
        source_state['formula_review_version'] = formula_review['version'] if formula_review else None
        version = hashlib.sha256(json.dumps({'state': source_state, 'item': dict(item),
                                            'case_fingerprint': row['case_fingerprint'], 'case_source': row['case_source']},
                                            sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        return {'action': 'continue_mistake_dialogue', 'answer_type': item['answer_type'], 'binding': {
            'session_id': session_id, 'child_id': child_id, 'mistake_id': row['mistake_id'],
            'phase': row['phase'], 'item_id': shown, 'source_version': version}}

    @staticmethod
    def learning_reply_binding(pending):
        """A source reference only; never a speech or independent-evidence grant."""
        if (not pending or pending.get('capability_name') not in
                {'learning.explain', 'learning.evaluate_response', 'learning.request_response'}
                or any(not isinstance(pending.get(key), str) or not pending[key].strip()
                       for key in ('objective_id', 'plan_id', 'source_agent_turn_id', 'phase'))):
            return None
        binding = {key: pending.get(key) for key in
                   ('objective_id', 'plan_id', 'capability_name', 'phase', 'source_agent_turn_id')}
        # Preserve the original answer-target encoding, including default separators.
        binding['source_version'] = hashlib.sha256(json.dumps(
            {key: pending.get(key) for key in ('summary', 'success_criteria', 'arguments', 'result')},
            sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        return binding

    @staticmethod
    def pending_learning_capture(connection, session_id, child_id=None):
        """All source/owner/priority facts are read in one original-connection SELECT.

        Never instantiate typed services here: some of their readers ensure schema.
        This also works through the existing SELECT-only trusted read facade.
        """
        required = {'diagnostic_prompt', 'pet_vocabulary_prompt_session', 'specialized_exam_run',
                    'companion_activity', 'mistake_dialogue'}
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            return None
        row = connection.execute("""SELECT ao.objective_id, ao.summary, ao.success_criteria_json,
            ao.phase, ap.plan_id, ap.agent_turn_id AS source_agent_turn_id, ast.capability_name,
            ast.arguments_json, ast.result_json, d.child_id
            FROM daily_session d JOIN child_profile cp ON cp.child_id=d.child_id
            JOIN agent_plan ap ON ap.session_id=d.session_id AND ap.child_id=d.child_id
            JOIN agent_objective ao ON ao.objective_id=ap.objective_id
                AND ao.session_id=d.session_id AND ao.child_id=d.child_id
            JOIN agent_step ast ON ast.plan_id=ap.plan_id AND ast.step_index=0
            JOIN agent_turn t ON t.agent_turn_id=ap.agent_turn_id
                AND t.session_id=d.session_id AND t.child_id=d.child_id
            WHERE d.session_id=? AND (? IS NULL OR d.child_id=?)
                AND d.status='active' AND cp.status='active'
                AND ap.status='waiting_child' AND ao.status='active' AND ao.objective_type='learning'
                AND ast.status='completed' AND t.tool_status='completed'
                AND ast.capability_name IN ('learning.explain','learning.evaluate_response','learning.request_response')
                AND ap.updated_at=(SELECT MAX(p.updated_at) FROM agent_plan p JOIN agent_objective o USING(objective_id)
                    WHERE p.session_id=d.session_id AND p.status='waiting_child'
                      AND o.status='active' AND o.objective_type='learning')
                AND 1=(SELECT COUNT(*) FROM agent_plan p JOIN agent_objective o USING(objective_id)
                    WHERE p.session_id=d.session_id AND p.status='waiting_child'
                      AND o.status='active' AND o.objective_type='learning' AND p.updated_at=ap.updated_at)
                AND NOT EXISTS(SELECT 1 FROM agent_plan newer JOIN agent_step ns ON ns.plan_id=newer.plan_id
                    AND ns.step_index=0 WHERE newer.objective_id=ap.objective_id
                    AND newer.child_id=d.child_id AND newer.session_id=d.session_id
                    AND ns.capability_name IN ('learning.explain','learning.evaluate_response','learning.request_response')
                    AND newer.plan_id<>ap.plan_id AND newer.created_at>=ap.created_at)
                AND NOT EXISTS(SELECT 1 FROM diagnostic_prompt q WHERE q.origin_session_id=d.session_id
                    AND q.status='waiting_answer')
                AND NOT EXISTS(SELECT 1 FROM pet_vocabulary_prompt_session v WHERE v.session_id=d.session_id
                    AND v.status='active')
                AND NOT EXISTS(SELECT 1 FROM specialized_exam_run e WHERE e.session_id=d.session_id
                    AND e.status IN ('active','paused'))
                AND NOT EXISTS(SELECT 1 FROM companion_activity a WHERE a.session_id=d.session_id
                    AND a.status='active')
                AND NOT EXISTS(SELECT 1 FROM mistake_dialogue m WHERE m.session_id=d.session_id
                    AND m.phase IN ('attempt','diagnosis_probe','answer'))
            """, (session_id, child_id, child_id)).fetchone()
        if row is None:
            return None
        pending = dict(row)
        pending['success_criteria'] = json.loads(pending.pop('success_criteria_json'))
        pending['arguments'] = json.loads(pending.pop('arguments_json'))
        pending['result'] = json.loads(pending.pop('result_json') or 'null')
        binding = AgentRuntimeStore.learning_reply_binding(pending)
        if binding is None:
            return None
        return {'version': 'ordinary_learning_capture_v1', 'session_id': session_id,
                'child_id': row['child_id'], 'reference': {'kind': 'learning_reply', 'binding': binding}}

    @staticmethod
    def validate_captured_question(connection, session_id, child_id, reference, *, _execution_turn_id=None, _execution_voice_input_id=None):
        """Read only the supplied source within the capture host's transaction."""
        from .diagnostic_prompt import DiagnosticPromptService, DiagnosticPromptError, _version
        from .exam_simulation import ExamSimulationService, ExamSimulationError
        from .pet_vocabulary import PetVocabularyService, PetVocabularyError
        if (not isinstance(reference, dict) or set(reference) != {'kind', 'binding'}
                or not isinstance(reference['binding'], dict)):
            raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_REFERENCE_INVALID')
        kind, binding = reference['kind'], reference['binding']
        if kind == 'learning_reply':
            if _execution_turn_id is not None:
                from .learning_reply_reservation import validate as validate_learning_source
                validate_learning_source(connection, _execution_turn_id, reference=reference,
                                         voice_input_id=_execution_voice_input_id, child_id=child_id, session_id=session_id)
                return
            current = AgentRuntimeStore.pending_learning_capture(connection, session_id, child_id)
            if current is None or current['reference'] != reference:
                raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_LEARNING_SOURCE_CHANGED')
            return
        if kind == 'document':
            from .document_voice_source import validate_reference
            try:
                return validate_reference(connection, session_id, child_id, binding)
            except ValueError as error:
                raise InvalidAgentPlanError(str(error)) from error
        owner = None
        if kind in {'diagnostic', 'diagnostic_help'}:
            owner = connection.execute('SELECT child_id,origin_session_id AS session_id FROM diagnostic_prompt WHERE prompt_id=?',
                                       (binding.get('prompt_id'),)).fetchone()
        elif kind == 'exam':
            owner = connection.execute('SELECT child_id,session_id FROM specialized_exam_run WHERE run_id=?',
                                       (binding.get('run_id'),)).fetchone()
        elif kind == 'vocabulary':
            owner = connection.execute('SELECT child_id,session_id FROM pet_vocabulary_prompt_session WHERE prompt_session_id=?',
                                       (binding.get('prompt_session_id'),)).fetchone()
        elif kind == 'mistake':
            if binding.get('child_id') != child_id or binding.get('session_id') != session_id:
                raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_OWNER_CHANGED')
            case = connection.execute('SELECT child_id FROM mistake_case WHERE mistake_id=?', (binding.get('mistake_id'),)).fetchone()
            if case and case['child_id'] != child_id:
                raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_OWNER_CHANGED')
            owner = connection.execute('SELECT child_id,session_id FROM mistake_dialogue WHERE session_id=? AND mistake_id=?',
                                       (session_id, binding.get('mistake_id'))).fetchone()
            if binding.get('mistake_id') is None and binding.get('candidate_id'):
                owner = connection.execute('''SELECT child_id,session_id FROM mistake_dialogue
                    WHERE session_id=? AND candidate_id=? AND mistake_id IS NULL
                    AND phase IN ('candidate','answer','confirm')''',
                    (session_id, binding['candidate_id'])).fetchone()
            if owner is None and case:
                owner = connection.execute("""SELECT child_id,session_id FROM mistake_dialogue_event WHERE session_id=?
                    AND json_extract(result_json,'$.mistake_id')=? LIMIT 1""", (session_id, binding.get('mistake_id'))).fetchone()
        else:
            raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_REFERENCE_INVALID')
        if owner is None:
            raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_REFERENCE_UNAVAILABLE')
        if owner['child_id'] != child_id or owner['session_id'] != session_id:
            raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_OWNER_CHANGED')
        try:
            if kind == 'diagnostic_help':
                from .learning_question_help import snapshot
                target = snapshot(connection, session_id, child_id)
                prompt = connection.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',
                    (binding.get('prompt_id'),)).fetchone()
                if (set(binding) != {'prompt_id', 'item_version'} or not target or not prompt
                        or prompt['status'] != 'answered'
                        or binding != {key: target[key] for key in ('prompt_id', 'item_version')}):
                    raise DiagnosticPromptError('capture_source_changed')
            elif kind == 'diagnostic':
                if set(binding) != {'prompt_id', 'item_version'}:
                    raise DiagnosticPromptError('capture_reference_invalid')
                session = DiagnosticPromptService._session(connection, session_id)
                row = DiagnosticPromptService._owned(connection, session, binding['prompt_id'])
                item = DiagnosticPromptService._item(connection, child_id, row['diagnostic_item_id'])
                if (session['child_id'] != child_id or session['status'] != 'active'
                        or session['child_status'] != 'active' or session['state'] in {'evolving', 'completed'}
                        or row['status'] != 'waiting_answer' or not item
                        or row['item_version'] != binding['item_version'] or _version(item) != binding['item_version']
                        or DiagnosticPromptService._scheduled_source_reason(connection, row)):
                    raise DiagnosticPromptError('capture_source_changed')
                if row['review_task_id']:
                    review = DiagnosticPromptService._review(connection, child_id, row['review_task_id'])
                    if not review or _version(review) != row['review_version']:
                        raise DiagnosticPromptError('capture_review_changed')
            elif kind == 'exam':
                ExamSimulationService.validate_capture_reference(connection,
                    session_id=session_id, child_id=child_id, binding=binding)
            elif kind == 'vocabulary':
                PetVocabularyService.validate_capture_reference(connection,
                    session_id=session_id, child_id=child_id, binding=binding)
            elif kind == 'mistake':
                target = AgentRuntimeStore.local_mistake_answer_target(connection, session_id, child_id)
                from .mistake_dialogue_reference import current_binding
                dialogue_binding = current_binding(connection, session_id, child_id) if not target else None
                if ((target or {}).get('binding') or dialogue_binding) != binding:
                    raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_TARGET_CHANGED')
            else:
                raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_REFERENCE_INVALID')
        except (DiagnosticPromptError, ExamSimulationError, PetVocabularyError) as error:
            raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_TARGET_CHANGED') from error
        return {'kind': kind, 'binding': dict(binding)}

    @staticmethod
    def captured_question(capture):
        if 'question_reference' in capture:
            return capture['question_reference']
        if capture.get('prompt_id') and capture.get('item_version'):
            return {'kind': 'diagnostic', 'binding': {key: capture[key] for key in ('prompt_id', 'item_version')}}
        return None

    @staticmethod
    def capture_matches_answer(capture, action_type, binding):
        kind = {'submit_diagnostic_answer': 'diagnostic', 'submit_exam_answer': 'exam',
                'submit_pet_vocabulary_answer': 'vocabulary', 'continue_mistake_dialogue': 'mistake', 'evaluate_child_response': 'learning_reply'}.get(action_type)
        return bool(not capture.get('activity_reference') and not capture.get('pending_evaluation_turn_id')
                    and not capture.get('local_recovery_only')
                    and capture.get('question_validation', 'valid') == 'valid' and kind
                    and AgentRuntimeStore.captured_question(capture) == {'kind': kind, 'binding': binding})

    @staticmethod
    def _inspect_original_client_execution(c,session_id,turn_id,*,running=False,page_source_store=None):
        """Recheck original authority in the host's actual write transaction."""
        if not c.in_transaction:
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_WRITE_TRANSACTION_REQUIRED')
        row = c.execute('''SELECT t.*,p.plan_id,p.status AS plan_status,p.context_manifest_json,p.plan_json,
            s.step_id,s.status AS step_status,s.capability_name,s.capability_version,
            s.execution_location,s.arguments_json,s.result_json,d.state AS session_state,d.status AS session_status,
            cp.status AS child_status FROM agent_turn t
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
            JOIN agent_step s USING(plan_id) JOIN daily_session d ON d.session_id=t.session_id AND d.child_id=t.child_id
            JOIN child_profile cp ON cp.child_id=t.child_id
            WHERE t.agent_turn_id=? AND t.session_id=? AND s.step_index=0''', (turn_id, session_id)).fetchone()
        if not row or row['intent']=='runtime_continuation':
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_COMMAND_REQUIRED')
        action=json.loads(row['action_json']);manifest=json.loads(row['context_manifest_json'])
        args=json.loads(row['arguments_json']);planned=json.loads(row['plan_json']).get('next_step',{})
        if (row['capability_name']!='diagnostic.start' or row['execution_location']!='client_device_bridge'
                or action.get('type')!='start_diagnostic' or any(manifest.get(k) for k in (
                    'voice_control_binding','local_controlled_launch_cancel','local_recovery_control','pending_local_control'))):
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_CAPABILITY_CHANGED')
        spec=CapabilityRegistry().require(row['capability_name'],row['session_state'],arguments=args)
        if (spec.version!=row['capability_version'] or spec.execution_location!=row['execution_location']
                or spec.action_type!=action['type'] or planned.get('capability')!=spec.name
                or planned.get('capability_version')!=spec.version or planned.get('execution_location')!=spec.execution_location
                or planned.get('arguments')!=args or any(action.get(k)!=v for k,v in args.items())):
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_CAPABILITY_CHANGED')
        latest=c.execute('SELECT agent_turn_id FROM agent_turn WHERE session_id=? ORDER BY rowid DESC LIMIT 1',(session_id,)).fetchone()
        if (row['session_status']!='active' or row['child_status']!='active'
                or row['session_state'] in {'evolving','completed'} or not latest or latest[0]!=turn_id):
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_SCOPE_CHANGED')
        states={'running'} if running else {'planned','waiting_child'}
        if row['tool_status']!='pending' or row['plan_status']!=row['step_status'] or row['plan_status'] not in states:
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_RECEIPT_REQUIRED')
        AgentRuntimeStore.assert_turn_source_not_deleted(c,turn_id)
        from .voice_control_intent import VoiceControlIntent
        if not VoiceControlIntent.scope_valid(c,session_id,row['child_id'],row['client_action_id'],require_cloud=False):
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_SCOPE_CHANGED')
        arrival=c.execute('SELECT * FROM learning_input_arrival WHERE client_action_id=?',(row['client_action_id'],)).fetchone()
        if (not arrival or arrival['arrival_id']!=manifest.get('learning_input_arrival_id')
                or (arrival['session_id'],arrival['child_id'],arrival['utterance'])!=(session_id,row['child_id'],row['transcript'])):
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_ARRIVAL_CHANGED')
        from .decision_record import validate_current, StaleDecisionContextError
        try:
            validate_current(c,row['plan_id'])
        except StaleDecisionContextError as error:
            raise InvalidAgentPlanError(str(error)) from error
        from .agent_continuation import ContinuationStore
        if not ContinuationStore.execution_allowed(c,turn_id,running=running):
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_AUTHORIZATION_CHANGED')
        if action.get('voice_input_id'):
            capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=session_id,child_id=row['child_id'],
                voice_input_id=action['voice_input_id'],transcript=row['transcript'],page_source_store=page_source_store)
            if AgentRuntimeStore.voice_capture_snapshot(capture)!=action.get('voice_input_binding'):
                raise InvalidAgentPlanError('ORIGINAL_CLIENT_VOICE_CHANGED')
        from .canonical_voice_execution import inspect as inspect_canonical_source
        try:
            inspect_canonical_source(c,turn_id,page_source_store=page_source_store)
        except (ValueError,KeyError,TypeError) as error:
            raise InvalidAgentPlanError('CANONICAL_VOICE_EXECUTION_SOURCE_CHANGED') from error
        return row

    @staticmethod
    def _original_client_admission_stamp(row):
        binding=[row[k] for k in ('agent_turn_id','plan_id','step_id','capability_name',
            'capability_version','execution_location','action_json','arguments_json')]
        digest=hashlib.sha256(json.dumps(binding,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
        return {'original_client_admission':{'version':1,'binding_sha256':digest}}

    @staticmethod
    def revalidate_original_client_execution(c,session_id,turn_id,*,page_source_store=None):
        """A claim stamp selects checks; it never grants authority by itself.

        Legacy and formal results have no transient claim stamp. The prepare
        host replays its original saved prompt before reaching this boundary.
        """
        result=c.execute('''SELECT s.result_json FROM agent_step s JOIN agent_plan p USING(plan_id)
            WHERE p.agent_turn_id=? AND p.session_id=? AND s.step_index=0''',(turn_id,session_id)).fetchone()
        value=json.loads(result['result_json']) if result and result['result_json'] else None
        if not isinstance(value,dict) or 'original_client_admission' not in value:
            return False
        row=AgentRuntimeStore._inspect_original_client_execution(c,session_id,turn_id,
            running=True,page_source_store=page_source_store)
        if (value!=AgentRuntimeStore._original_client_admission_stamp(row)
                or type(value['original_client_admission']['version'])is not int):
            raise InvalidAgentPlanError('ORIGINAL_CLIENT_CLAIM_CHANGED')
        return True

    def admit_original_client_execution(self,session_id,turn_id,*,page_source_store=None):
        """Claim one ordinary diagnostic; repeats require receipt recovery."""
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                row=self._inspect_original_client_execution(c,session_id,turn_id,page_source_store=page_source_store)
            except InvalidAgentPlanError as error:
                if str(error)=='ORIGINAL_CLIENT_RECEIPT_REQUIRED':
                    return {'status':'execution_receipt_required'}
                raise
            if row['result_json'] is not None:
                return {'status':'execution_receipt_required'}
            now=datetime.now(timezone.utc).isoformat()
            p=c.execute("UPDATE agent_plan SET status='running',updated_at=? WHERE plan_id=? AND status=?",(now,row['plan_id'],row['plan_status']))
            stamp=json.dumps(self._original_client_admission_stamp(row),separators=(',',':'))
            step=c.execute("UPDATE agent_step SET status='running',result_json=?,updated_at=? WHERE step_id=? AND status=? AND result_json IS NULL",(stamp,now,row['step_id'],row['step_status']))
            if p.rowcount!=1 or step.rowcount!=1:
                raise InvalidAgentPlanError('ORIGINAL_CLIENT_CLAIM_CHANGED')
            c.commit()
            return {'status':'admitted'}

    @staticmethod
    def inspect_voice_control_execution(connection, turn_id, *, allow_planned=False):
        """Read-only; host calls before its side effect in the same transaction."""
        from .realtime_native_authority import inspect_native_control
        native = inspect_native_control(connection, turn_id, allow_planned=allow_planned)
        if native is not None:
            return native
        from .realtime_native_special_sources import control_receipt
        special = control_receipt(connection, turn_id, allow_planned=allow_planned)
        if special is not None:
            return special
        from .voice_control_intent import VoiceControlIntent
        return VoiceControlIntent.inspect_execution(connection, turn_id, allow_planned=allow_planned)

    @staticmethod
    def resolve_captured_voice(connection, *, session_id, child_id, voice_input_id, transcript, page_source_store=None, _execution_turn_id=None):
        from .voice_capture_binding import resolve_voice_capture, VoiceCaptureBindingError
        try:
            return resolve_voice_capture(connection, session_id=session_id, child_id=child_id,
                                         voice_input_id=voice_input_id, transcript=transcript,
                                         page_source_store=page_source_store, _execution_turn_id=_execution_turn_id)
        except VoiceCaptureBindingError as error:
            raise InvalidAgentPlanError('ANSWER_' + str(error)) from error

    @staticmethod
    def voice_capture_snapshot(capture):
        return {**{key: capture.get(key) for key in
                ('voice_input_id', 'child_id', 'session_id', 'prompt_id', 'item_version', 'status')},
                'question_reference': AgentRuntimeStore.captured_question(capture),
                'question_validation': capture.get('question_validation', 'valid'),
                **({'activity_reference': dict(capture['activity_reference']),
                    'activity_validation': capture.get('activity_validation', 'stale')}
                   if capture.get('activity_reference') else {}),
                **({'pending_evaluation_turn_id': capture['pending_evaluation_turn_id']}
                   if capture.get('pending_evaluation_turn_id') else {}),
                **({'task_choice_reference': dict(capture['task_choice_reference'])}
                   if capture.get('task_choice_reference') else {}),
                **({key: capture.get(key) for key in ('source_kind','speech_started_at','admitted_at','onset_provenance')}
                   if capture.get('source_kind') in ('realtime_transcript','household_local_transcript') else {}),
                **({'local_recovery_only': True} if capture.get('local_recovery_only') else {})}

    @staticmethod
    def _answer_voice_metadata(connection, current, source, capability_name, binding):
        captures = []
        for row in (current, source):
            action = json.loads(row['action_json'])
            voice_id = action.get('voice_input_id')
            if not voice_id:
                if action.get('voice_input_binding'):
                    raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_BINDING_CHANGED')
                captures.append(None)
                continue
            capture = AgentRuntimeStore.resolve_captured_voice(connection,
                session_id=row['session_id'], child_id=row['child_id'],
                voice_input_id=voice_id, transcript=row['transcript'],
                _execution_turn_id=current['agent_turn_id'] if capability_name == 'learning.evaluate_response' else None)
            if AgentRuntimeStore.voice_capture_snapshot(action.get('voice_input_binding') or {}) != AgentRuntimeStore.voice_capture_snapshot(capture):
                raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_BINDING_CHANGED')
            action_type = {'diagnostic.submit': 'submit_diagnostic_answer', 'exam.submit': 'submit_exam_answer',
                'pet_vocabulary.submit': 'submit_pet_vocabulary_answer', 'mistake.dialogue': 'continue_mistake_dialogue', 'learning.evaluate_response':'evaluate_child_response'}.get(capability_name)
            if not AgentRuntimeStore.capture_matches_answer(capture, action_type, binding):
                raise InvalidAgentPlanError('ANSWER_VOICE_CAPTURE_TARGET_CHANGED')
            captures.append(capture)
        current_capture, source_capture = captures
        if not current_capture and not source_capture:
            return {}
        return {'voice_input_id': (current_capture or {}).get('voice_input_id'),
                'source_voice_input_id': (source_capture or {}).get('voice_input_id'),
                'raw_audio_available': bool((source_capture or {}).get('raw_audio_available'))}

    @staticmethod
    def resolve_local_answer_submission(connection, turn_id: str, capability_name: str) -> dict[str, Any]:
        """Resolve confirmed original speech inside the host's answer write transaction.

        Hosts replay an already stored answer receipt BEFORE calling this gate.
        Call before reservation and again before delayed grading; do not use a
        preflight transaction as permission for a later independent write.
        """
        if not connection.in_transaction:
            raise InvalidAgentPlanError("ANSWER_WRITE_TRANSACTION_REQUIRED")
        row = connection.execute("""SELECT t.*,p.plan_id,p.status AS plan_status,s.status AS step_status,
            s.arguments_json,s.capability_name,ds.status AS session_status,ds.ended_at
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            JOIN daily_session ds ON ds.session_id=t.session_id AND ds.child_id=t.child_id
            WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id""", (turn_id,)).fetchone()
        if (not row or capability_name not in {"diagnostic.submit", "pet_vocabulary.submit", "exam.submit", "learning.evaluate_response", "mistake.dialogue"}
                or row["capability_name"] != capability_name or row["intent"] == "runtime_continuation"):
            raise InvalidAgentPlanError("ANSWER_CHILD_TURN_REQUIRED")
        reserved = None
        if capability_name == 'diagnostic.submit':
            reserved = connection.execute("""SELECT json_extract(answer_json,'$.answer') FROM diagnostic_prompt
                WHERE authorized_turn_id=? AND child_id=? AND origin_session_id=? AND status='answer_saved'""",
                (turn_id, row['child_id'], row['session_id'])).fetchone()
        elif capability_name == 'exam.submit':
            reserved = connection.execute("""SELECT i.answer_text FROM specialized_exam_item i JOIN specialized_exam_run r USING(run_id)
                WHERE i.authorized_turn_id=? AND r.child_id=? AND r.session_id=? AND i.answer_action_id IS NOT NULL AND i.attempt_id IS NULL""",
                (turn_id, row['child_id'], row['session_id'])).fetchone()
        elif capability_name == 'mistake.dialogue':
            reserved = connection.execute("""SELECT json_extract(state_json,'$.pending_operation.utterance') FROM mistake_dialogue
                WHERE session_id=? AND child_id=? AND json_extract(state_json,'$.pending_operation.authorized_turn_id')=?""",
                (row['session_id'], row['child_id'], turn_id)).fetchone()
        if (row["session_status"] != "active" or row["ended_at"] or row["plan_status"] == "cancelled"
                or row['tool_status'] == 'cancelled'
                or (row["step_status"] not in {"planned", "running", "waiting_child", "failed"}
                    and not (reserved and row['step_status'] == 'completed'))):
            raise InvalidAgentPlanError("ANSWER_EXECUTION_NOT_CURRENT")
        action = json.loads(row["action_json"])
        source_id = action.get("answer_source_turn_id")
        if not source_id:
            binding = ({'prompt_id': action.get('diagnostic_prompt_id'), 'item_version': action.get('diagnostic_item_version')}
                       if capability_name == 'diagnostic.submit' else
                       {'prompt_session_id': action.get('vocabulary_prompt_id'), 'card_version': action.get('vocabulary_card_version')}
                       if capability_name == 'pet_vocabulary.submit' else action.get('exam_input_binding')
                       if capability_name == 'exam.submit' else action.get('mistake_answer_binding')
                       if capability_name == 'mistake.dialogue' else
                       (action.get('voice_input_binding') or {}).get('question_reference', {}).get('binding')
                       if capability_name == 'learning.evaluate_response'
                         and ((action.get('voice_input_binding') or {}).get('question_reference') or {}).get('kind') == 'learning_reply'
                       else None)
            return {"status": "direct", "answer": row["transcript"], "source_turn_id": turn_id,
                    "child_id": row["child_id"], "session_id": row["session_id"], "binding": binding,
                    **AgentRuntimeStore._answer_voice_metadata(connection, row, row, capability_name, binding)}
        if not AgentRuntimeStore.is_answer_role_confirmation(row["transcript"]):
            raise InvalidAgentPlanError("ANSWER_ROLE_CONFIRMATION_REQUIRED")
        latest = connection.execute("SELECT agent_turn_id FROM agent_turn WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1",
                                    (row["session_id"], row["child_id"])).fetchone()
        arrival = connection.execute("SELECT client_action_id FROM learning_input_arrival WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1",
                                     (row["session_id"], row["child_id"])).fetchone()
        if not reserved and (not latest or latest[0] != turn_id or not arrival or arrival[0] != row["client_action_id"]):
            raise InvalidAgentPlanError("ANSWER_CONFIRMATION_SUPERSEDED")
        source = connection.execute("""SELECT t.*,p.status AS plan_status FROM agent_turn t
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?""",
            (source_id, row["child_id"], row["session_id"])).fetchone()
        previous = connection.execute("""SELECT agent_turn_id FROM agent_turn WHERE child_id=? AND session_id=?
            AND rowid<(SELECT rowid FROM agent_turn WHERE agent_turn_id=?) ORDER BY rowid DESC LIMIT 1""",
            (row["child_id"], row["session_id"], turn_id)).fetchone()
        if (not source or not previous or previous[0] != source_id or source["tool_status"] == "cancelled"
                or source["plan_status"] == "cancelled"):
            raise InvalidAgentPlanError("ANSWER_SOURCE_REVOKED")
        saved = json.loads(source["action_json"])
        candidate = saved.get("pending_answer_candidate") or {}
        target = candidate.get("target") or {}
        binding = target.get("binding") or {}
        arguments = json.loads(row["arguments_json"])
        actual = candidate.get("answer")
        if reserved and reserved[0] != actual:
            raise InvalidAgentPlanError('ANSWER_SOURCE_BINDING_CHANGED')
        submitted = arguments.get("response" if capability_name == "learning.evaluate_response"
                                  else "utterance" if capability_name == "mistake.dialogue" else "answer")
        if (saved.get("source") != "pending_answer_clarification" or saved.get("local_only") is not True
                or target.get("action") != {"diagnostic.submit": "submit_diagnostic_answer", "pet_vocabulary.submit": "submit_pet_vocabulary_answer",
                    "exam.submit": "submit_exam_answer", "learning.evaluate_response": "evaluate_child_response",
                    "mistake.dialogue": "continue_mistake_dialogue"}[capability_name]
                or actual != source["transcript"] or submitted != actual or action.get("local_only") is not True):
            raise InvalidAgentPlanError("ANSWER_SOURCE_BINDING_CHANGED")
        if capability_name == "mistake.dialogue":
            valid = AgentRuntimeStore.local_mistake_answer_target(connection, row['session_id'], row['child_id'], authorized_turn_id=turn_id) == target
        elif capability_name == "exam.submit":
            valid = connection.execute("""SELECT 1 FROM specialized_exam_run r JOIN specialized_exam_item i USING(run_id)
                WHERE r.run_id=? AND r.child_id=? AND r.session_id=? AND r.status='active' AND r.current_position=?
                AND i.position=r.current_position AND i.diagnostic_item_id=? AND i.source_version=? AND i.attempt_id IS NULL""",
                (binding.get("run_id"), row["child_id"], row["session_id"], binding.get("position"),
                 binding.get("item_id"), binding.get("source_version"))).fetchone()
        elif capability_name == "diagnostic.submit":
            valid = connection.execute("""SELECT 1 FROM diagnostic_prompt WHERE prompt_id=? AND item_version=?
                AND child_id=? AND origin_session_id=? AND owner_kind='diagnostic'
                AND json_extract(request_json,'$.exam_slot') IS NULL AND status IN ('waiting_answer','answer_saved')""",
                (binding.get("prompt_id"), binding.get("item_version"), row["child_id"], row["session_id"])).fetchone()
        elif capability_name == "pet_vocabulary.submit":
            valid = connection.execute("""SELECT 1 FROM pet_vocabulary_prompt_session WHERE prompt_session_id=? AND card_version=?
                AND child_id=? AND session_id=? AND status='active' AND invalidated_reason IS NULL""",
                (binding.get("prompt_session_id"), binding.get("card_version"), row["child_id"], row["session_id"])).fetchone()
        else:
            original = connection.execute("""SELECT o.summary,o.success_criteria_json,s.arguments_json,s.result_json FROM agent_objective o
                JOIN agent_plan p ON p.objective_id=o.objective_id AND p.child_id=o.child_id
                JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
                WHERE o.objective_id=? AND p.plan_id=? AND o.child_id=? AND o.session_id=? AND o.status='active'
                  AND p.status='waiting_child' AND s.capability_name=?""",
                (binding.get("objective_id"), binding.get("plan_id"), row["child_id"], row["session_id"], binding.get("capability_name"))).fetchone()
            version = hashlib.sha256(json.dumps({"summary": original["summary"],
                "success_criteria": json.loads(original["success_criteria_json"]), "arguments": json.loads(original["arguments_json"]),
                "result": json.loads(original["result_json"]) if original["result_json"] else None},
                sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest() if original else None
            valid = version and version == binding.get("source_version")
        if not valid:
            raise InvalidAgentPlanError("ANSWER_TARGET_CHANGED")
        return {"status": "confirmed", "answer": actual, "source_turn_id": source_id, "binding": binding,
                "child_id": row["child_id"], "session_id": row["session_id"],
                "local_only": True, "cloud_transmission_allowed": False,
                **AgentRuntimeStore._answer_voice_metadata(connection, row, source, capability_name, binding)}

    @staticmethod
    def unquoted_answer_speech(transcript: str) -> str:
        quoted = r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"'
        text = re.sub(quoted, lambda match: ' ' * len(match[0]), transcript)
        # Spoken source attribution also matters when ASR omits quotation marks.
        attributed = r'(?:^|(?<=[，,。；;]))\s*(?:题目|题干|原题)(?:里|上)?(?:写着|说|要求|问)[^，,。；;]*?(?=我的答案|最终答案|我选|[，,。；;]|$)'
        return re.sub(attributed, lambda match: ' ' * len(match[0]), text)

    @staticmethod
    def local_answer_requires_clarification(transcript: str) -> bool:
        text = AgentRuntimeStore.unquoted_answer_speech(transcript)
        if re.search(r'(?:不要|别|不用|暂不)(?:再|急着|先)?(?:提交|评分|记录(?:这次|这个)?(?:答案|回答)|算作(?:我的)?答案)', text):
            return True
        if re.search(r'取消(?:这次|刚才的)?(?:提交|作答|评分)|撤回(?:刚才的|这次|我的)?(?:答案|回答|作答|提交)', text):
            return True
        clauses = [part.strip() for part in re.split(r'[，,。；;！!？?]', text) if part.strip()]
        nonanswer = r'(?:(?:这|那|这句|那句|刚才|刚才那句|我刚才说的)(?:话)?)?(?:不是|不算)(?:我的)?(?:答案|回答)'
        reading = r'(?:我(?:刚才)?|刚才)?(?:只是(?:在)?|正在|在)(?:读|念)(?:题|题目|题干)'
        if any(re.fullmatch(nonanswer, part) or re.fullmatch(reading, part) for part in clauses):
            return True
        unfinished = r'(?:我)?(?:还没(?:说完|想好)|没(?:说完|想好)|等(?:我)?(?:一下|一会儿|想一下)|让我(?:想想|再想想)|不对|说错了|改成|应该是|答案(?:改为|改成)|我改(?:一下|口))'
        return bool(clauses and re.fullmatch(unfinished, clauses[-1]))

    @staticmethod
    def local_answer_forms(transcript: str, capability_name: str) -> tuple[str, ...]:
        if capability_name == 'mistake.dialogue':
            return (transcript,)
        # Only remove an explicit answer prefix. Model arguments are not evidence.
        normalized = re.sub(r'^(?:我的答案是|答案是|我选)\s*[，,]?\s*', '', transcript.strip(), count=1)
        if normalized != transcript.strip():
            normalized = normalized.strip(' ，,。') or transcript
        text = AgentRuntimeStore.unquoted_answer_speech(transcript)
        final = re.search(r'(?:^|[，,。；;])\s*(?:我的答案(?:是|改为|改成)|最终答案是|答案(?:是|改为|改成)|更正为|改成|我选)\s*([^，,。；;！!？?]+)[。！!\s]*$', transcript)
        if (final and text[final.start():final.start(1)] == transcript[final.start():final.start(1)]
                and not AgentRuntimeStore.local_answer_requires_clarification(transcript)):
            normalized = transcript[final.start(1):final.end(1)].strip()
            suffix = transcript[final.end(1):].strip()
            if '!' in suffix or '！' in suffix:
                from .spoken_numeric import parse
                if parse(normalized, 'rational') is not None:
                    normalized += suffix
        return (transcript, normalized)

    @staticmethod
    def validate_local_answer_write(connection, authorized_turn_id, capability_name, *, child_id, session_id, binding, answer):
        if authorized_turn_id is None:
            return
        if not isinstance(authorized_turn_id, str) or not authorized_turn_id.strip():
            raise InvalidAgentPlanError('ANSWER_CHILD_TURN_REQUIRED')
        if capability_name == 'diagnostic.submit':
            from .document_spoken_supplement import validate_submission as supplement_submission
            if binding and supplement_submission(connection,authorized_turn_id,child_id,session_id,binding,answer):
                return
            from .native_writing_control import validate_saved_retained_turn
            if validate_saved_retained_turn(connection,authorized_turn_id,child_id=child_id,session_id=session_id,
                    binding=binding,answer=answer):
                return
            original = AgentRuntimeStore.confirmed_writing_submission(connection, authorized_turn_id, for_write=True)
            if original is not None:
                if (original['child_id'] != child_id or original['session_id'] != session_id
                        or binding != {k:original[k] for k in ('prompt_id','item_version')} or answer != original['answer']):
                    raise InvalidAgentPlanError('ANSWER_WRITING_SOURCE_CHANGED')
                return
        value = AgentRuntimeStore.resolve_local_answer_submission(connection, authorized_turn_id, capability_name)
        if (value['child_id'] != child_id or value['session_id'] != session_id
                or (binding is not None and value.get('binding') != binding)):
            raise InvalidAgentPlanError('ANSWER_TARGET_CHANGED')
        if AgentRuntimeStore.local_answer_requires_clarification(value['answer']):
            raise InvalidAgentPlanError('ANSWER_INTENT_CLARIFICATION_REQUIRED')
        allowed = (AgentRuntimeStore.local_answer_forms(value['answer'], capability_name)
                   if value['status'] == 'direct' else (value['answer'],))
        if answer not in allowed:
            raise InvalidAgentPlanError('ANSWER_SOURCE_BINDING_CHANGED')

    @staticmethod
    def input_revision_execution_receipt(connection, turn_id: str) -> dict[str, Any] | None:
        row = connection.execute("""SELECT s.status,s.result_json,s.capability_name,p.status AS plan_status,
            p.child_id,p.session_id,t.child_id AS turn_child,t.session_id AS turn_session,ds.child_id AS session_child
            FROM agent_step s JOIN agent_plan p USING(plan_id) JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
            JOIN daily_session ds ON ds.session_id=p.session_id
            WHERE t.agent_turn_id=? AND s.step_index=0 AND s.execution_location='server'
              AND s.capability_name LIKE 'learning_inputs.%'""", (turn_id,)).fetchone()
        if not row:
            return None
        receipt = None
        if (row["status"] == "completed" and row["plan_status"] != "cancelled"
                and row["child_id"] == row["turn_child"] == row["session_child"]
                and row["session_id"] == row["turn_session"] and row["result_json"]):
            saved = json.loads(row["result_json"])
            if (isinstance(saved, dict) and saved.get("capability") == row["capability_name"]
                    and not saved.get("superseded") and isinstance(saved.get("response_text"), str)
                    and saved["response_text"].strip()):
                receipt = saved
        return {"status": row["status"], "plan_status": row["plan_status"], "result": receipt}

    @staticmethod
    def assert_turn_source_not_deleted(connection, turn_id):
        deleted = connection.execute('''SELECT 1 FROM agent_turn t WHERE t.agent_turn_id=?
            AND (t.tool_error_code='SOURCE_DELETED' OR EXISTS
                (SELECT 1 FROM agent_plan p JOIN agent_step s USING(plan_id)
                 WHERE p.agent_turn_id=t.agent_turn_id AND s.error_code='SOURCE_DELETED'))''',
            (turn_id,)).fetchone()
        if deleted:
            from .agent_orchestrator import InvalidAgentTurnOutcomeError
            raise InvalidAgentTurnOutcomeError('SOURCE_DELETED')

    @staticmethod
    def validate_document_continuation_completion(connection, turn_id):
        row = connection.execute('''SELECT t.action_json,s.status,s.result_json FROM agent_turn t
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND s.step_index=0''', (turn_id,)).fetchone()
        if not row:
            return
        try:
            action = json.loads(row['action_json'])
        except (ValueError, TypeError) as error:
            raise InvalidAgentPlanError('ANSWER_EXECUTION_ACTION_CHANGED') from error
        if not isinstance(action, dict):
            raise InvalidAgentPlanError('ANSWER_EXECUTION_ACTION_CHANGED')
        if not action.get('document_stop_followup_root'):
            return
        work = connection.execute('SELECT * FROM agent_continuation WHERE next_turn_id=?', (turn_id,)).fetchone()
        if (not work or work['status'] != 'completed' or row['status'] != 'completed' or not row['result_json']
                or not ContinuationStore._valid(connection, dict(work), prepared=True, settled=True)):
            raise InvalidAgentPlanError('ANSWER_DOCUMENT_CONTINUATION_AUTHORITY_CHANGED')

    def complete_turn_execution(
        self,
        connection,
        turn_id: str,
        succeeded: bool,
        error_code: str | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        self.assert_turn_source_not_deleted(connection, turn_id)
        self.validate_document_continuation_completion(connection, turn_id)
        self.validate_writing_reference_execution(connection, turn_id)
        from .school_oral_reference import validate as validate_oral_reference
        validate_oral_reference(connection,turn_id,result=result,completion=succeeded)
        server = self.input_revision_execution_receipt(connection, turn_id)
        if server is not None:
            if server["result"] is not None:
                result, succeeded, error_code = server["result"], True, None
            elif succeeded or server["status"] == "cancelled" or server["plan_status"] == "cancelled":
                raise InvalidAgentPlanError("INPUT_REVISION_SERVER_RECEIPT_REQUIRED")
        row = connection.execute(
            """
            SELECT ap.plan_id, ap.objective_id, ap.plan_json, ast.step_id, ast.capability_name,
                   ap.status AS plan_status, ast.status AS step_status
            FROM agent_plan ap JOIN agent_step ast ON ast.plan_id = ap.plan_id
            WHERE ap.agent_turn_id = ? AND ast.step_index = 0
            """,
            (turn_id,),
        ).fetchone()
        if succeeded:
            self.validate_device_receipt(connection, turn_id, result, error_code, page_source_store=self.page_source_store)
            from .learning_question_help import teaching_receipt
            try:
                teaching_receipt(connection,turn_id,result)
            except (ValueError,KeyError,TypeError) as error:
                raise InvalidAgentPlanError('ANSWER_LEARNING_HELP_EVIDENCE_CHANGED') from error
            from .document_goal_handoff import complete_teaching
            try:
                complete_teaching(connection, turn_id, result)
            except (ValueError, KeyError, TypeError) as error:
                raise InvalidAgentPlanError('ANSWER_DOCUMENT_GOAL_EVIDENCE_CHANGED') from error
        if not row:
            return
        if (row['capability_name'] in {'oral.retry','oral.independent_check'}
                and 'cancelled' in {row['plan_status'],row['step_status']}):
            raise InvalidAgentPlanError('ANSWER_ORAL_FOLLOWUP_COMMAND_CHANGED')
        now = datetime.now(timezone.utc).isoformat()
        plan = json.loads(row["plan_json"])
        business_blocked = self.execution_result_blocked(result)
        waiting = succeeded and (bool(plan.get("requires_child_reply"))
                                 or row["capability_name"] in {"learning.request_response", "diagnostic.start", "diagnostic.replay", "pet_vocabulary.start", "writing.read_original"}
                                 or bool(result and result.get("requires_child_reply")))
        if row["capability_name"] in {"diagnostic.start", "diagnostic.replay", "diagnostic.submit"}:
            has_prompts = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone()
            waiting = bool(succeeded and has_prompts and connection.execute(
                """SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=
                   (SELECT session_id FROM agent_plan WHERE plan_id=?) AND owner_kind='diagnostic' AND status='waiting_answer'""",
                (row["plan_id"],),
            ).fetchone())
        if row['capability_name'] in {'writing.revise_original','writing.independent_check'}:
            value = result.get('result', {}) if isinstance(result, dict) else {}
            action = json.loads(connection.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()[0])
            binder = self.bind_writing_followup_prompt if action.get('writing_followup_binding') else self.bind_writing_revision_prompt
            waiting = bool(succeeded and not business_blocked and binder(connection, turn_id, value))
        if row['capability_name'] in {'oral.retry','oral.independent_check'}:
            waiting = bool(succeeded and not business_blocked and self.bind_oral_followup_prompt(connection,turn_id,result.get('result',{})))
        if row["capability_name"] in {"pet_vocabulary.start", "pet_vocabulary.submit", "pet_vocabulary.cancel"}:
            has_prompts = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_vocabulary_prompt_session'").fetchone()
            waiting = bool(succeeded and has_prompts and connection.execute(
                """SELECT 1 FROM pet_vocabulary_prompt_session WHERE session_id=
                   (SELECT session_id FROM agent_plan WHERE plan_id=?) AND status='active'
                   AND card_version IS NOT NULL AND invalidated_reason IS NULL""",
                (row["plan_id"],),
            ).fetchone())
        exam_focus = None
        if succeeded and row["capability_name"] in {"exam.start", "exam.submit", "exam.resume"} and isinstance(result, dict):
            value = result.get("result") or {}
            candidate = value.get("answer_binding") if isinstance(value, dict) else None
            waiting = False
            if isinstance(candidate, dict) and connection.execute(
                """SELECT 1 FROM specialized_exam_run r JOIN specialized_exam_item i USING(run_id)
                   JOIN agent_plan p ON p.session_id=r.session_id AND p.child_id=r.child_id
                   WHERE p.plan_id=? AND r.run_id=? AND r.status='active' AND r.current_position=?
                     AND i.position=r.current_position AND i.diagnostic_item_id=? AND i.source_version=?
                     AND i.attempt_id IS NULL""",
                (row["plan_id"], candidate.get("run_id"), candidate.get("position"),
                 candidate.get("item_id"), candidate.get("source_version")),
            ).fetchone():
                waiting = True
                exam_focus = {"capability_name": "exam.start", "result": candidate}
                plan["pending_input_binding"] = exam_focus
                connection.execute("UPDATE agent_plan SET plan_json=? WHERE plan_id=?",
                                   (json.dumps(plan, ensure_ascii=False), row["plan_id"]))
        if succeeded and row["capability_name"] == "task.complete" and isinstance(result, dict):
            value = result.get("result", {})
            if value.get("status") == "completed":
                waiting = False
            elif value.get("status") == "awaiting_completion_check":
                # A delayed receipt cannot reopen a prompt superseded by a
                # later durable reply for the same task run.
                superseded = connection.execute(
                    """SELECT 1 FROM agent_step ast JOIN agent_plan ap ON ap.plan_id=ast.plan_id
                       JOIN agent_turn at ON at.agent_turn_id=ap.agent_turn_id
                       WHERE ap.session_id=(SELECT session_id FROM agent_plan WHERE plan_id=?)
                         AND ast.capability_name='task.complete' AND ast.status='completed'
                         AND at.rowid > (SELECT rowid FROM agent_turn WHERE agent_turn_id=?)
                         AND json_extract(ast.result_json,'$.result.plan_item_id')=?
                         AND json_extract(ast.result_json,'$.result.task_started_at') IS ?
                         AND json_extract(ast.result_json,'$.result.status') IN ('awaiting_completion_check','completed')
                       LIMIT 1""", (row["plan_id"], turn_id, value.get("plan_item_id"), value.get("task_started_at")),
                ).fetchone()
                waiting = superseded is None
        # A handled refusal completes the command, not a learning goal or prompt.
        if business_blocked:
            waiting = False
        step_status = "completed" if succeeded else "failed"
        if waiting and isinstance(result, dict):
            receipt = result.get("execution_receipt") or result.get("client_bridge_receipt") or {}
            output = receipt.get("output") if isinstance(receipt, dict) else None
            if isinstance(output, dict):
                binding = None
                if row["capability_name"] in {"diagnostic.start", "diagnostic.replay", "diagnostic.submit"}:
                    prompt = output.get("prompt") or {}
                    if isinstance(prompt, dict) and connection.execute(
                        """SELECT 1 FROM diagnostic_prompt WHERE prompt_id=? AND item_version=?
                           AND owner_kind='diagnostic' AND json_extract(request_json,'$.exam_slot') IS NULL
                           AND status='waiting_answer' AND origin_session_id=
                           (SELECT session_id FROM agent_plan WHERE plan_id=?)""",
                        (prompt.get("prompt_id"), prompt.get("item_version"), row["plan_id"]),
                    ).fetchone():
                        binding = {"capability_name": "diagnostic.start", "result": {
                            "prompt_id": prompt["prompt_id"], "item_version": prompt["item_version"]}}
                elif row["capability_name"] in {"pet_vocabulary.start", "pet_vocabulary.submit"}:
                    prompt = output.get("next_card") or output.get("card") or {}
                    if isinstance(prompt, dict) and connection.execute(
                        """SELECT 1 FROM pet_vocabulary_prompt_session WHERE prompt_session_id=? AND card_version=?
                           AND status='active' AND invalidated_reason IS NULL AND session_id=
                           (SELECT session_id FROM agent_plan WHERE plan_id=?)""",
                        (prompt.get("prompt_session_id"), prompt.get("card_version"), row["plan_id"]),
                    ).fetchone():
                        binding = {"capability_name": "pet_vocabulary.start", "result": {
                            "prompt_session_id": prompt["prompt_session_id"], "card_version": prompt["card_version"]}}
                if binding:
                    plan["pending_input_binding"] = binding
                    connection.execute("UPDATE agent_plan SET plan_json=? WHERE plan_id=?",
                                       (json.dumps(plan, ensure_ascii=False), row["plan_id"]))
        plan_status = "waiting_child" if waiting else step_status
        if succeeded and not business_blocked and row["capability_name"] == "companion.end_activity":
            value = result.get("result", result) if isinstance(result, dict) else {}
            activity_id = value.get("activity_id") if isinstance(value, dict) else None
            if activity_id and connection.execute("SELECT 1 FROM sqlite_master WHERE name='companion_activity_outcome'").fetchone():
                focus = connection.execute("""SELECT o.outcome_id,o.child_id,o.session_id,MAX(e.revision) AS expected_revision
                    FROM companion_activity_outcome o JOIN companion_activity a ON a.activity_id=o.source_id AND o.source_kind='companion'
                    JOIN companion_activity_outcome_event e USING(outcome_id)
                    JOIN agent_plan p ON p.child_id=o.child_id AND p.session_id=o.session_id
                    WHERE p.plan_id=? AND a.activity_id=? AND a.status='completed' GROUP BY o.outcome_id""",
                    (row["plan_id"], activity_id)).fetchone()
                if focus:
                    connection.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.companion_activity_focus_binding',json(?)) WHERE plan_id=?",
                                       (json.dumps(dict(focus)), row["plan_id"]))
        if succeeded and not business_blocked and row["capability_name"] == "diagnostic.start" and isinstance(result, dict):
            receipt = result.get("execution_receipt") or result.get("client_bridge_receipt") or {}
            output = receipt.get("output") if isinstance(receipt, dict) else None
            prompt = output.get("prompt") if isinstance(output, dict) else None
            if isinstance(prompt, dict):
                self._bind_learning_diagnostic(connection, turn_id, prompt.get("prompt_id"), prompt.get("item_version"))
        if succeeded and not waiting and not business_blocked and row["capability_name"] == "diagnostic.submit":
            # task.start may have prepared the exact diagnostic prompt and then
            # waited for the child's separate answer turn. Once that bound
            # prompt is no longer waiting, the originating command must not
            # remain as a stale waiting_child plan that suppresses wrap-up and
            # survives a restart as a phantom question.
            turn = connection.execute(
                "SELECT child_id,session_id,action_json FROM agent_turn WHERE agent_turn_id=?",
                (turn_id,),
            ).fetchone()
            submitted = json.loads(turn["action_json"] or "{}") if turn else {}
            prompt_id = submitted.get("diagnostic_prompt_id")
            item_version = submitted.get("diagnostic_item_version")
            resolved = connection.execute(
                """SELECT 1 FROM diagnostic_prompt
                   WHERE prompt_id=? AND item_version=? AND child_id=? AND origin_session_id=?
                     AND status IN ('answered','cancelled','invalidated')""",
                (prompt_id, item_version, turn["child_id"], turn["session_id"]),
            ).fetchone() if turn and prompt_id and item_version else None
            if resolved:
                connection.execute(
                    """UPDATE agent_plan SET status='completed',updated_at=?
                       WHERE child_id=? AND session_id=? AND status='waiting_child'
                         AND json_extract(plan_json,'$.local_learning_binding.prompt_id')=?
                         AND json_extract(plan_json,'$.local_learning_binding.item_version')=?
                         AND EXISTS (SELECT 1 FROM agent_step s WHERE s.plan_id=agent_plan.plan_id
                           AND s.step_index=0 AND s.capability_name IN ('task.start','diagnostic.start','diagnostic.replay'))""",
                    (now, turn["child_id"], turn["session_id"], prompt_id, item_version),
                )
        connection.execute(
            "UPDATE agent_step SET status = ?, result_json = ?, error_code = ?, updated_at = ? WHERE step_id = ?",
            (
                step_status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error_code,
                now,
                row["step_id"],
            ),
        )
        connection.execute(
            "UPDATE agent_plan SET status = ?, updated_at = ? WHERE plan_id = ?",
            (plan_status, now, row["plan_id"]),
        )
        if succeeded and not waiting and not business_blocked:
            if row['capability_name'] in {'controlled_content.stop', 'controlled_game.stop'}:
                from .controlled_objective_closure import apply as close_controlled_objective
                close_controlled_objective(connection, turn_id, result, now)
            self.continuations.enqueue(connection, turn_id)
            followup = connection.execute('''SELECT parent.objective_id,parent.context_manifest_json
                FROM agent_continuation q JOIN agent_plan parent ON parent.agent_turn_id=q.root_turn_id
                WHERE q.next_turn_id=? AND parent.objective_id=?''', (turn_id, row['objective_id'])).fetchone()
            if (followup and json.loads(followup['context_manifest_json']).get('document_stop_followup_owned_objective')
                    and row['capability_name'] == 'conversation.respond'):
                # A receipt can finish only the dedicated execution request,
                # never the learning goal that happened to be active at arrival.
                connection.execute("""UPDATE agent_objective SET status='completed',completed_at=?,updated_at=?
                    WHERE objective_id=? AND status='active' AND objective_type='execution'""",
                    (now, now, followup['objective_id']))

    @staticmethod
    def validate_device_receipt(connection, turn_id: str, result: dict[str, Any] | None,
                                error_code: str | None = None, *, page_source_store=None) -> None:
        """Validate before recording success, including legacy turns without a plan.

        Capture/OCR receipts prove only their named stage. They never create or
        replace a tutor_ocr_confirmation; quality warnings still require review.
        """
        turn = connection.execute("""SELECT t.*, ds.child_id AS session_child_id
            FROM agent_turn t JOIN daily_session ds ON ds.session_id=t.session_id
            WHERE t.agent_turn_id=?""", (turn_id,)).fetchone()
        if not turn:
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_TURN_NOT_FOUND")
        step = connection.execute("""SELECT s.capability_name,s.execution_location,
                p.child_id,p.session_id
            FROM agent_step s JOIN agent_plan p ON p.plan_id=s.plan_id
            WHERE p.agent_turn_id=? AND s.step_index=0""", (turn_id,)).fetchone()
        action = json.loads(turn["action_json"] or "{}")
        action_type = action.get("type")
        name = step["capability_name"] if step else (
            action.get("capability") or DEFAULT_ACTION_CAPABILITY_MAP.get(action_type))
        spec = next((item for item in DEFAULT_CAPABILITIES if item.name == name), None)
        if not ((spec and spec.execution_location == "client_device_bridge")
                or (step and step["execution_location"] == "client_device_bridge")):
            return
        if (not spec or spec.execution_location != "client_device_bridge"
                or (step and step["execution_location"] != spec.execution_location)
                or (action_type != spec.action_type and DEFAULT_ACTION_CAPABILITY_MAP.get(action_type) != spec.name)):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_EXECUTION_MISMATCH")
        if (turn["child_id"] != turn["session_child_id"] or
                (step and (step["child_id"] != turn["child_id"] or step["session_id"] != turn["session_id"]))):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_OWNER_MISMATCH")
        if error_code:
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_SUCCESS_WITH_ERROR")
        if not isinstance(result, dict):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_REQUIRED")
        receipts = [result[key] for key in ("execution_receipt", "client_bridge_receipt") if key in result]
        if not receipts or any(not isinstance(item, dict) for item in receipts):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_REQUIRED")
        receipt = receipts[0]
        if any(item != receipt for item in receipts[1:]):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_ENVELOPE_CONFLICT")
        if (receipt.get("capability") != spec.name
                or receipt.get("execution_location") != spec.execution_location
                or receipt.get("action_type") != action_type
                or receipt.get("status") != "completed"):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_EXECUTION_MISMATCH")
        try:
            completed_at = datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00"))
            if completed_at.tzinfo is None:
                raise ValueError("timezone required")
        except (KeyError, TypeError, AttributeError, ValueError):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_INVALID_TIMESTAMP") from None
        if action_type == 'control_document_monitor' and action.get('command') in {'inspect', 'review'}:
            from .document_monitoring import DocumentMonitoringService
            output = receipt.get('output')
            if (not isinstance(output, dict)
                    or set(output) != {'artifact_id', 'revision_id', 'page_status', 'response_text'}
                    or any(not isinstance(output.get(key), str) or not output[key].strip()
                           for key in ('artifact_id', 'revision_id', 'response_text'))
                    or output.get('page_status') not in {'text_available', 'empty', 'blocked'}
                    or not connection.execute("SELECT 1 FROM sqlite_master WHERE name='document_page_revision'").fetchone()):
                raise InvalidDeviceReceiptError('DEVICE_RECEIPT_DOCUMENT_INSPECTION_REQUIRED')
            page = connection.execute('''SELECT a.extracted_text,a.created_at AS artifact_created_at,
                a.details_json,r.created_at AS revision_created_at,r.content_sha256
                FROM document_page_revision r JOIN capture_artifact a USING(artifact_id)
                JOIN document_monitor_session m USING(monitor_id)
                WHERE r.revision_id=? AND r.artifact_id=? AND r.child_id=? AND r.session_id=?
                AND a.child_id=r.child_id AND a.session_id=r.session_id
                AND m.child_id=r.child_id AND m.session_id=r.session_id
                AND a.artifact_type='document_ocr' AND a.status IN ('completed','quality_warning')''',
                (output['revision_id'], output['artifact_id'], turn['child_id'], turn['session_id'])).fetchone()
            if not page:
                raise InvalidDeviceReceiptError('DEVICE_RECEIPT_DOCUMENT_INSPECTION_SOURCE_CHANGED')
            reference = (action.get('voice_input_binding') or {}).get('question_reference')
            if action.get('command') == 'review' and (reference or {}).get('kind') == 'document':
                from .voice_capture_binding import resolve_voice_capture, VoiceCaptureBindingError
                try:
                    if page_source_store is None:
                        raise ValueError('page_reader_unavailable')
                    capture = resolve_voice_capture(connection, session_id=turn['session_id'], child_id=turn['child_id'],
                        voice_input_id=action.get('voice_input_id'), transcript=turn['transcript'], page_source_store=page_source_store)
                    if (capture['question_validation'] != 'valid' or capture['question_reference'] != reference
                            or output['revision_id'] != reference['binding']['revision_id']
                            or output['artifact_id'] != reference['binding']['artifact_id']):
                        raise ValueError('voice_original_changed')
                except (ValueError, VoiceCaptureBindingError, KeyError, TypeError):
                    raise InvalidDeviceReceiptError('DEVICE_RECEIPT_DOCUMENT_INSPECTION_SOURCE_CHANGED') from None
                try:
                    saved = connection.execute("SELECT payload_json FROM session_draft WHERE session_id=? AND section='study'",
                                               (turn['session_id'],)).fetchone()
                    draft = json.loads(saved['payload_json']) if saved else {}
                    review = draft.get('tutor_ocr_review') or {}
                    normalized = DocumentMonitoringService._normalized_text(page['extracted_text'] or '')
                    if (review.get('artifactId') != output['artifact_id']
                            or review.get('createdAt') != page['revision_created_at']
                            or review.get('status') not in {'uncertain', 'needs_confirmation'}
                            or DocumentMonitoringService._normalized_text(review.get('text') or '') != normalized
                            or DocumentMonitoringService._normalized_text(draft.get('tutor_ocr_review_text') or '') != normalized):
                        raise ValueError('original_review_not_saved')
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise InvalidDeviceReceiptError('DEVICE_RECEIPT_DOCUMENT_REVIEW_NOT_SAVED') from None
            try:
                times = [datetime.fromisoformat(value.replace('Z', '+00:00')) for value in
                         (turn['created_at'], page['artifact_created_at'], page['revision_created_at'])]
                if (any(value.tzinfo is None for value in times)
                        or not times[1] <= times[2] <= completed_at + timedelta(seconds=30)
                        or times[0] > completed_at + timedelta(seconds=30)
                        or (action.get('command') == 'inspect' and times[1] < times[0])
                        or completed_at > datetime.now(timezone.utc) + timedelta(seconds=30)):
                    raise ValueError('stale inspection')
                text = page['extracted_text'] or ''
                normalized = DocumentMonitoringService._normalized_text(text)
                if hashlib.sha256(normalized.encode('utf-8')).hexdigest() != page['content_sha256']:
                    raise ValueError('changed page')
                details = json.loads(page['details_json'])
                fallback = details.get('cloud_vision_fallback') or {}
                fallback_status = fallback.get('status')
                blocked = (fallback.get('state') if fallback_status is None else fallback_status) == 'blocked'
                expected = 'blocked' if blocked else 'text_available' if text.strip() else 'empty'
                if output['page_status'] != expected:
                    raise ValueError('unproven page status')
            except (ValueError, TypeError, AttributeError):
                raise InvalidDeviceReceiptError('DEVICE_RECEIPT_DOCUMENT_INSPECTION_SOURCE_CHANGED') from None
            return
        if action_type == 'control_pet_listening':
            output = receipt.get('output')
            binding = action.get('pet_listening_binding') or {}
            key = f'{turn_id}:pet:control'
            if (not isinstance(output, dict) or set(output) != {'control_action_id'}
                    or output['control_action_id'] != key
                    or not connection.execute("SELECT 1 FROM sqlite_master WHERE name='pet_media_event'").fetchone()):
                raise InvalidDeviceReceiptError('PET_LISTENING_CONTROL_RECEIPT_REQUIRED')
            event = connection.execute('''SELECT e.payload_json,e.generation FROM pet_media_event e
                JOIN pet_media_binding m USING(media_id) WHERE e.client_action_id=? AND e.operation='control'
                AND m.media_id=? AND m.child_id=? AND m.session_id=? AND m.prompt_id=? AND m.item_version=?''',
                (key, binding.get('media_id'), turn['child_id'], turn['session_id'], binding.get('prompt_id'), binding.get('item_version'))).fetchone()
            expected = {field: action.get(field) if field == 'command' else binding.get(field)
                        for field in ('command', 'generation', 'expected_revision')}
            if not event or json.loads(event['payload_json']) != expected or event['generation'] != binding.get('generation', 0) + 1:
                raise InvalidDeviceReceiptError('PET_LISTENING_CONTROL_NOT_PERSISTED')
            return
        if action_type in {'check_native_writing_submission','submit_retained_native_writing'}:
            from .native_writing_control import validate
            target=action.get('native_writing_target')
            output=receipt.get('output')
            if (not isinstance(output,dict) or set(output)!={'native_writing_target','receipt_checked'}
                    or output['native_writing_target']!=target or output['receipt_checked'] is not True):
                raise InvalidDeviceReceiptError('NATIVE_WRITING_CONTROL_RECEIPT_REQUIRED')
            validate(connection,turn['session_id'],turn['child_id'],target)
            return
        if action_type == 'read_writing_original':
            from .writing_ocr_authorization import _confirmation
            output = receipt.get('output')
            target = action.get('writing_ocr_confirmation') or {}
            if (not isinstance(output, dict) or set(output) != {'status', 'confirmation_id', 'binding_version', 'playback_id'}
                    or output['status'] != 'readback_completed'
                    or {k:output[k] for k in ('confirmation_id', 'binding_version')} != target):
                raise InvalidDeviceReceiptError('WRITING_READBACK_EXACT_RECEIPT_REQUIRED')
            try:
                packet, _, _ = _confirmation(connection, turn['session_id'], target.get('confirmation_id'),
                                               target.get('binding_version'))
                events = connection.execute('''SELECT * FROM writing_ocr_event WHERE confirmation_id=? AND child_id=?
                    AND kind IN ('started','ended') ORDER BY rowid DESC LIMIT 2''',
                    (packet['confirmation_id'], turn['child_id'])).fetchall()
                if len(events) != 2 or [e['kind'] for e in events] != ['ended', 'started'] or packet['child_id'] != turn['child_id']:
                    raise ValueError('missing readback')
                ended, started = [json.loads(e['payload_json']) for e in events]
                for event, payload in zip(events, (ended, started)):
                    request = json.loads(event['request_json'])
                    if (payload.get('playback_id') != output['playback_id'] or payload.get('readback_sha256') != packet['readback_sha256']
                            or payload.get('confirmation_id') != packet['confirmation_id'] or payload.get('event') != event['kind']
                            or request != dict(binding_version=target['binding_version'], playback_id=output['playback_id'],
                                event=event['kind'], readback_sha256=packet['readback_sha256'])):
                        raise ValueError('changed readback')
                if (not started.get('runtime_epoch') or started['runtime_epoch'] != ended.get('runtime_epoch')
                        or not _aware_datetime(turn['created_at']) <= _aware_datetime(events[1]['created_at'])
                            <= _aware_datetime(events[0]['created_at']) <= completed_at):
                    raise ValueError('readback order changed')
            except (ValueError, TypeError, KeyError):
                raise InvalidDeviceReceiptError('WRITING_READBACK_NOT_PERSISTED') from None
            return
        if action_type == "switch_child_profile":
            output = receipt.get("output")
            if (not isinstance(output, dict) or set(output) != {"profile_switched", "target_child_id", "target_session_id", "previous_session_id"}
                    or output["profile_switched"] is not True or output["previous_session_id"] != turn["session_id"]
                    or output["target_child_id"] != action.get("target_child_id")
                    or not isinstance(output["target_session_id"], str) or not isinstance(output["target_child_id"], str)
                    or output["target_session_id"] == turn["session_id"] or output["target_child_id"] == turn["child_id"]):
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_PROFILE_SWITCH_INVALID")
            target = connection.execute("SELECT status FROM daily_session WHERE session_id=? AND child_id=?",
                                        (output["target_session_id"], output["target_child_id"])).fetchone()
            source = connection.execute("SELECT status FROM daily_session WHERE session_id=?", (turn["session_id"],)).fetchone()
            audit = connection.execute("""SELECT 1 FROM audit_event WHERE action='voice_child_profile_switched' AND child_id=?
                AND json_extract(summary_json,'$.source_session_id')=? AND json_extract(summary_json,'$.target_session_id')=? LIMIT 1""",
                (output["target_child_id"], turn["session_id"], output["target_session_id"])).fetchone()
            if not target or target[0] != "active" or not source or source[0] not in {"cancelled", "paused"} or not audit:
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_PROFILE_SWITCH_NOT_SAVED")
            return
        if action_type in {"find_parent", "find_parent_urgent"}:
            output = receipt.get("output")
            if not isinstance(output, dict) or set(output) != {"parent_contact_prompted"} or output["parent_contact_prompted"] is not True:
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_PARENT_PROMPT_REQUIRED")
            return
        if action_type == "learn_mistake":
            from .realtime_native_mistake_learning import validate_completion
            validate_completion(connection, turn, action, receipt.get("output"))
            return
        if action_type == "navigate_workspace":
            output=receipt.get('output')
            if (not isinstance(output,dict) or output.get('workspace_opened') is not True
                    or output.get('workspace') != action.get('destination')
                    or output.get('workspace') not in {'learning','mistake_book'}
                    or output.get('child_id') != turn['child_id'] or output.get('session_id') != turn['session_id']
                    or output.get('content_status') not in {'ready','empty'}):
                raise InvalidDeviceReceiptError('DEVICE_RECEIPT_WORKSPACE_NOT_RENDERED')
            return
        if action_type == "configure_wake_word":
            output = receipt.get("output")
            if (not isinstance(output, dict) or set(output) != {"configuration_started", "phase", "wake_word_saved"}
                    or output["configuration_started"] is not True or output["wake_word_saved"] is not False
                    or output["phase"] != ("confirm_name" if action.get("candidate") else "naming")):
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_WAKE_CONFIGURATION_INVALID")
            return
        if action_type in {"pause_observation", "resume_observation", "query_device_status", "connect_devices"} or (
                action_type == "control_document_monitor" and action.get("command") in {"pause", "resume"}):
            self_type = "document_camera" if action_type == "control_document_monitor" else "camera"
            output = receipt.get("output")
            now = datetime.now(timezone.utc)
            try:
                turn_started = _aware_datetime(turn["created_at"].replace("Z", "+00:00"))
            except (TypeError, ValueError, AttributeError):
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_INVALID_TIMESTAMP") from None

            def fresh_for_turn(timestamp):
                # Client timestamps must be anchored to server time, not merely
                # mutually consistent. Completed replays bypass this validator.
                return (abs((now - timestamp).total_seconds()) <= 30
                        and timestamp >= turn_started - timedelta(seconds=30))

            if not fresh_for_turn(completed_at):
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_DEVICE_TIME_STALE")

            def owned_lease(device_type):
                return any(_valid_recent_time(row[0], now, future=True) for row in connection.execute(
                    "SELECT expires_at FROM sensor_lease WHERE session_id=? AND device_type=? AND status='active'",
                    (turn["session_id"], device_type)))

            if action_type == "connect_devices":
                requested = action.get("device")
                if requested not in {"camera", "document_camera", "all"}:
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_REQUEST_INVALID")
                expected = {"camera", "document_camera"} if requested == "all" else {requested}
                if (not isinstance(output, dict)
                        or set(output) != {"requested_device", "connected_at", "devices"}
                        or output["requested_device"] != requested):
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_INVALID")
                try:
                    connected_at = datetime.fromisoformat(output["connected_at"].replace("Z", "+00:00"))
                    if (connected_at.tzinfo is None or not fresh_for_turn(connected_at)
                            or not -2 <= (completed_at - connected_at).total_seconds() <= 15):
                        raise ValueError()
                except (TypeError, ValueError, AttributeError):
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_STALE") from None
                devices = output["devices"]
                if not isinstance(devices, list) or len(devices) != len(expected):
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_INVALID")
                seen = set()
                for device in devices:
                    if not isinstance(device, dict) or device.get("status") != "connected":
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_NOT_COMPLETED")
                    kind = device.get("device_type")
                    if kind not in expected or kind in seen:
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_INVALID")
                    seen.add(kind)
                    persisted_field = "observation_status" if kind == "camera" else "persisted_status"
                    expected_persisted = "enabled" if kind == "camera" else "active"
                    if (set(device) != {"status", "device_type", "device_status", "frame_confirmed",
                                        "lease_confirmed", persisted_field}
                            or device["device_status"] != "active"
                            or device["frame_confirmed"] is not True
                            or device["lease_confirmed"] is not True
                            or device[persisted_field] != expected_persisted
                            or not owned_lease(kind)):
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_STATE_NOT_CONFIRMED")
                    persisted = (connection.execute(
                        "SELECT observation_status FROM daily_session WHERE session_id=?", (turn["session_id"],)
                    ).fetchone() if kind == "camera" else connection.execute(
                        "SELECT status FROM document_monitor_session WHERE session_id=? AND child_id=?",
                        (turn["session_id"], turn["child_id"]),
                    ).fetchone())
                    if not persisted or persisted[0] != expected_persisted:
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_STATE_NOT_SAVED")
                if seen != expected:
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CONNECT_INVALID")
                return

            if action_type == "query_device_status":
                requested = action.get("device")
                if requested not in {"camera", "document_camera", "all"}:
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STATUS_REQUEST_INVALID")
                expected = {"camera", "document_camera"} if requested == "all" else {requested}
                if not isinstance(output, dict) or set(output) != {"devices", "checked_at"}:
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STATUS_INVALID")
                try:
                    checked = datetime.fromisoformat(output["checked_at"].replace("Z", "+00:00"))
                    if (checked.tzinfo is None or not fresh_for_turn(checked)
                            or not -2 <= (completed_at - checked).total_seconds() <= 15):
                        raise ValueError()
                except (TypeError, ValueError, AttributeError):
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STATUS_STALE") from None
                devices = output["devices"]
                if not isinstance(devices, list) or len(devices) != len(expected):
                    raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STATUS_INVALID")
                seen = set()
                for device in devices:
                    if not isinstance(device, dict) or set(device) != {"device_type", "track_live", "frame_confirmed", "last_frame_at", "last_error_code", "monitor_active"}:
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STATUS_INVALID")
                    kind = device["device_type"]
                    if not isinstance(kind, str) or kind not in expected or kind in seen:
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STATUS_INVALID")
                    seen.add(kind)
                    if any(type(device[key]) is not bool for key in ("track_live", "frame_confirmed", "monitor_active")):
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STATUS_INVALID")
                    if device["last_error_code"] is not None and (not isinstance(device["last_error_code"], str)
                            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", device["last_error_code"])):
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STATUS_INVALID")
                    if device["frame_confirmed"] or device["monitor_active"]:
                        if not device["track_live"] or not owned_lease(kind):
                            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_LIVE_FRAME_NOT_OWNED")
                    if device["monitor_active"] and not device["frame_confirmed"]:
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_LIVE_FRAME_REQUIRED")
                    if device["last_frame_at"] is not None:
                        try:
                            frame = datetime.fromisoformat(device["last_frame_at"].replace("Z", "+00:00"))
                            if (frame.tzinfo is None or frame > checked
                                    or (device["frame_confirmed"] and (not fresh_for_turn(frame)
                                        or (checked - frame).total_seconds() > 5))):
                                raise ValueError()
                        except (TypeError, ValueError, AttributeError):
                            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_LIVE_FRAME_STALE") from None
                    elif device["frame_confirmed"]:
                        raise InvalidDeviceReceiptError("DEVICE_RECEIPT_LIVE_FRAME_REQUIRED")
                return
            persisted_field = "persisted_status" if self_type == "document_camera" else "observation_status"
            active = action_type == "resume_observation" or action.get("command") == "resume"
            expected_persisted = ("active" if self_type == "document_camera" else "enabled") if active else "paused"
            if (not isinstance(output, dict) or set(output) != {"device_type", "device_status", "frame_confirmed", "lease_confirmed", persisted_field}
                    or output["device_type"] != self_type or output["device_status"] != ("active" if active else "paused")
                    or output["frame_confirmed"] is not active or output["lease_confirmed"] is not True
                    or output[persisted_field] != expected_persisted or owned_lease(self_type) != active):
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_DEVICE_STATE_NOT_CONFIRMED")
            if self_type == "camera":
                persisted = connection.execute("SELECT observation_status FROM daily_session WHERE session_id=?", (turn["session_id"],)).fetchone()
            else:
                persisted = connection.execute("SELECT status FROM document_monitor_session WHERE session_id=? AND child_id=?",
                                               (turn["session_id"], turn["child_id"])).fetchone()
            if not persisted or persisted[0] != expected_persisted:
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_DEVICE_STATE_NOT_SAVED")
            return
        if action_type == "resume_voice_tutorial":
            output = receipt.get("output")
            if not isinstance(output, dict) or set(output) != {"onboarding_restarted", "onboarding_phase", "onboarding_step"}:
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_ONBOARDING_INVALID")
            phase, onboarding_step = output["onboarding_phase"], output["onboarding_step"]
            if (output["onboarding_restarted"] is not True
                or not ((phase == "naming" and onboarding_step is None)
                        or (phase == "active" and isinstance(onboarding_step, str) and onboarding_step in {
                            "wake_practice", "study_practice", "document_practice", "finish_practice",
                        }))):
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_ONBOARDING_NOT_RESTARTED")
            return
        if action_type not in {"request_capture", "retry_capture"}:
            return
        output = receipt.get("output")
        if not isinstance(output, dict) or output.get("command_id") != turn_id:
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_COMMAND_MISMATCH")
        device_status = output.get("device_status")
        if device_status not in ("captured", "recognized"):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_CAPTURE_NOT_FINISHED")
        artifact_id = output.get("capture_artifact_id") or output.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_ARTIFACT_REQUIRED")
        if any(output.get(key) not in (None, artifact_id) for key in ("capture_artifact_id", "artifact_id")):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_ARTIFACT_MISMATCH")
        artifact = connection.execute("SELECT * FROM capture_artifact WHERE artifact_id=?", (artifact_id,)).fetchone()
        if (not artifact or artifact["child_id"] != turn["child_id"]
                or artifact["session_id"] != turn["session_id"]):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_ARTIFACT_NOT_OWNED")
        if artifact["artifact_type"] != "document_ocr" or artifact["status"] not in {"completed", "quality_warning", "captured"}:
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_ARTIFACT_NOT_READY")
        try:
            captured_at = _aware_datetime(artifact["created_at"].replace("Z", "+00:00"))
            started_at = _aware_datetime(turn["created_at"].replace("Z", "+00:00"))
        except (TypeError, AttributeError, ValueError):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_INVALID_TIMESTAMP") from None
        if captured_at < started_at or captured_at > completed_at + timedelta(minutes=5):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_STALE_ARTIFACT")
        # Frontend OCR uses its own action ID, not the agent turn ID. Bind the
        # saved artifact by ownership/time and prevent reuse by another command.
        if connection.execute("""SELECT 1 FROM agent_step s JOIN agent_plan p ON p.plan_id=s.plan_id
            WHERE p.agent_turn_id != ? AND s.status='completed' AND
            COALESCE(json_extract(s.result_json,'$.execution_receipt.output.capture_artifact_id'),
                     json_extract(s.result_json,'$.execution_receipt.output.artifact_id'),
                     json_extract(s.result_json,'$.client_bridge_receipt.output.capture_artifact_id'),
                     json_extract(s.result_json,'$.client_bridge_receipt.output.artifact_id')) = ?
            LIMIT 1""", (turn_id, artifact_id)).fetchone():
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_ARTIFACT_REUSED")
        ocr = output.get("ocr")
        if not isinstance(ocr, dict) or ocr.get("artifact_id") not in (None, artifact_id):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_OCR_MISMATCH")
        if device_status == "recognized" or ocr.get("status") == "completed":
            if (ocr.get("status") != "completed" or ocr.get("artifact_id") != artifact_id
                    or ocr.get("error_code") or artifact["status"] not in {"completed", "quality_warning"}
                    or not str(artifact["extracted_text"] or "").strip()):
                raise InvalidDeviceReceiptError("DEVICE_RECEIPT_OCR_NOT_READY")
        elif ocr.get("status") not in ("not_completed", "failed"):
            raise InvalidDeviceReceiptError("DEVICE_RECEIPT_OCR_NOT_READY")

    def status(self, session_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            pending_capture = self.pending_learning_capture(connection, session_id)
            objective = connection.execute(
                """
                SELECT objective_id, objective_type, summary, success_criteria_json,
                       phase, status, created_at, updated_at
                FROM agent_objective WHERE session_id = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            step = connection.execute(
                """
                SELECT ast.step_id, ast.capability_name, ast.status, ast.expected_evidence_json,
                       ap.plan_id, ap.status AS plan_status, ap.updated_at
                FROM agent_step ast JOIN agent_plan ap ON ap.plan_id = ast.plan_id
                WHERE ap.session_id = ?
                ORDER BY ap.updated_at DESC, ast.step_index LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        sensor_state = self.assemble_context(session_id, ()).get("sensor_state")
        return {
            "session_id": session_id,
            "active_objective": ({
                **{key: value for key, value in dict(objective).items() if key != "success_criteria_json"},
                "success_criteria": json.loads(objective["success_criteria_json"]),
            } if objective else None),
            "current_step": ({
                **{key: value for key, value in dict(step).items() if key != "expected_evidence_json"},
                "expected_evidence": json.loads(step["expected_evidence_json"]),
            } if step else None),
            "sensor_state": sensor_state,
            "pending_learning_capture": pending_capture,
            "continuation": self.continuations.snapshot(session_id),
        }


def action_from_plan(decision: AgentPlanDecision, transcript: str, active_objective: dict[str, Any] | None) -> dict[str, Any]:
    capability = decision.step.capability
    arguments = decision.step.arguments
    action: dict[str, Any] = {
        "type": capability.action_type,
        "capability": capability.name,
        "execution_location": capability.execution_location,
    }
    if capability.name == "conversation.respond":
        return action
    if capability.name == "learning.explain":
        action.update({
            "question": str(arguments["question"])[:2000],
            "subject": str(arguments["subject"])[:40],
            "hint_level": min(6, max(1, int(arguments.get("support_level", 6)))),
        })
        if arguments.get("question_relation") == "followup":
            action.update(question=transcript[:2000], question_relation="followup")
    elif capability.name == "learning.evaluate_response":
        objective_summary = (active_objective or {}).get("summary") or decision.objective.summary or "当前知识目标"
        response = str(arguments["response"])[:1200]
        action.update({
            "question": (
                f"当前学习目标是：{objective_summary}。孩子对上一轮检查问题的回答是：{response}。"
                "请先判断其中正确的理解，再只纠正一个关键点，并给一道不泄露答案的近迁移检查。"
            ),
            "subject": str(arguments["subject"])[:40],
            "hint_level": 4,
            "learning_phase": "check",
        })
    elif capability.name == "learning.request_response":
        action.update({
            "prompt": str(arguments["prompt"])[:500],
            "subject": str(arguments["subject"])[:40],
            "learning_phase": "await_independent_response",
        })
    else:
        action.update(arguments)
    if capability.name == "speech.repeat":
        action.update(local_only=True, cloud_transmission_allowed=False)
    if capability.name == 'document.monitor_control' and arguments.get('command') in {'inspect', 'review'}:
        scope = decision.continuation_scope
        # Route confirmation to the original goal; this is not teaching authorization.
        if (decision.after_execution == 'replan' and not decision.requires_child_reply
                and sum(item.get('capability') == 'learning.explain' for item in scope) == 1
                and all(isinstance(item.get('request_quote'), str) and item['request_quote']
                        and item['request_quote'] in transcript for item in scope)):
            action['defer_document_review'] = True
    return action


def context_manifest(context: dict[str, Any]) -> dict[str, Any]:
    child_model = context.get("longitudinal_child_model") or {}
    strategy = child_model.get("active_strategy") or {}
    return {
        "object_types": [
            "child_profile", "session_state", "active_objective", "active_task",
            "plan_progress", "relevant_learning_state", "sensor_state", "recent_dialogue",
            "due_commitments", "longitudinal_child_model",
        ],
        "session_id": context["session"]["session_id"],
        "current_state": context["session"]["state"],
        "active_objective_id": (context.get("active_objective") or {}).get("objective_id"),
        "child_model_snapshot_id": child_model.get("snapshot_id"),
        "child_model_snapshot_persisted": child_model.get("snapshot_persisted") is True,
        "child_model_evidence_fingerprint": child_model.get("evidence_fingerprint"),
        "strategy_version": strategy.get("strategy_version") or "no_active_strategy",
        "strategy_experiment_id": strategy.get("experiment_id"),
        "learning_state_count": len((context.get("relevant_learning_state") or {}).get("nodes", [])),
        "recent_dialogue_messages": len(context.get("recent_dialogue") or []),
        "raw_audio_or_video": False,
        "context_hash": hashlib.sha256(
            json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
