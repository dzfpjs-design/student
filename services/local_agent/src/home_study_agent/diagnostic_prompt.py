"""Durable diagnostic prompts, not a second assessment or scoring store.

HTTP callers use prepare/read/answer/resume/cancel and must retain prompt_id and
item_version. Only waiting_answer means a concrete question awaits input.
answer_saved means the input is durable but has NOT been scored (for example,
after a crash). Trusted scoring rechecks this record in its own transaction and
works in active, non-terminal phases; this module never changes session state.
Question keys and scoring rubrics are hashed, never returned in the prompt.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
from typing import Literal, TypedDict
from uuid import uuid4

from .database import Database
from .learning_state import LearningStateService
from .repository import PlanInputError, utc_now
from .repository import StudyRepository
from .paper_scoring import PaperScoringService, ensure_paper_scoring_schema


_ITEM_FIELDS = ("diagnostic_item_id", "prompt", "answer_type", "difficulty", "stimulus_text",
                "stimulus_asset", "assessment_mode", "knowledge_node_id", "track", "subject", "title")


PromptStatus = Literal["waiting_answer", "no_item", "answer_saved", "answered", "invalidated", "cancelled", "awaiting_task_start"]


class PromptView(TypedDict):
    prompt_id: str
    item_version: str
    diagnostic_item_id: str
    review_task_id: str | None
    child_id: str
    origin_session_id: str
    item: dict
    created_at: str


class _PromptRecovery(TypedDict, total=False):
    plan_item_id: str
    deferred_answer: dict


class PromptResult(_PromptRecovery):
    status: PromptStatus
    prompt: PromptView | None
    attempt: dict | None
    reason: str | None


class DiagnosticPromptError(ValueError):
    """Invalid inputs or binding conflicts; HTTP should not retry as a new answer."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _version(value) -> str:
    return "diagnostic_prompt_v1:" + sha256(_json(value).encode()).hexdigest()


class _BorrowedConnection:
    """Keep LearningStateService's inner BEGIN/commit inside our owning transaction.

    All reads and writes, including the original session-state guard and scoring,
    run unchanged on this connection. Only transaction ownership is adapted.
    """

    def __init__(self, connection):
        self.connection = connection

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    @property
    def total_changes(self):
        return self.connection.total_changes

    @property
    def local_media_runtime(self):
        return getattr(self.connection, 'local_media_runtime', None)

    def execute(self, sql, parameters=()):
        if sql.strip().upper() in {"BEGIN", "BEGIN DEFERRED", "BEGIN IMMEDIATE"}:
            if not self.connection.in_transaction:
                raise RuntimeError("diagnostic transaction missing")
            return None
        return self.connection.execute(sql, parameters)

    def commit(self):
        pass


class _TransactionDatabase:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        from .original_connection_read import original_connection_view
        view=_BorrowedConnection(self.connection)
        with original_connection_view(view,self.connection):yield view


class DiagnosticPromptService:
    """One open prompt per original session; identifiers never rebind across sessions.

    prepare conflicts with another open prompt instead of replacing it. read may
    persist invalidation but never scores. answer saves first, then calls resume.
    DiagnosticPromptError.code is a stable machine-readable input/conflict code;
    stale source data instead returns invalidated (or no_item before preparation).
    Integrators run existing downstream attempt handlers only for a non-null
    attempt, using its attempt_id for their existing idempotency boundaries.
    """

    def __init__(self, database: Database):
        self.database = database
        with database.connect() as c:
            ensure_paper_scoring_schema(c)
            from .paper_learning_selection import ensure_schema as ensure_selection_schema
            ensure_selection_schema(c)
            c.executescript("""
                CREATE TABLE IF NOT EXISTS diagnostic_prompt (
                    prompt_id TEXT PRIMARY KEY,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    origin_session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                    diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id) ON DELETE CASCADE,
                    review_task_id TEXT REFERENCES review_task(review_task_id) ON DELETE CASCADE,
                    item_version TEXT NOT NULL,
                    review_version TEXT,
                    item_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    prepare_action_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN
                        ('waiting_answer','answer_saved','answered','invalidated','cancelled')),
                    answer_json TEXT,
                    answer_action_id TEXT UNIQUE,
                    attempt_id TEXT UNIQUE REFERENCES diagnostic_attempt(attempt_id) ON DELETE SET NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            if 'authorized_turn_id' not in {r['name'] for r in c.execute('PRAGMA table_info(diagnostic_prompt)')}:
                c.execute('ALTER TABLE diagnostic_prompt ADD COLUMN authorized_turn_id TEXT')
            if 'writing_original_artifact_id' not in {r['name'] for r in c.execute('PRAGMA table_info(diagnostic_prompt)')}:
                c.execute('ALTER TABLE diagnostic_prompt ADD COLUMN writing_original_artifact_id TEXT')
            if 'owner_kind' not in {r['name'] for r in c.execute('PRAGMA table_info(diagnostic_prompt)')}:
                c.execute("ALTER TABLE diagnostic_prompt ADD COLUMN owner_kind TEXT NOT NULL DEFAULT 'diagnostic'")
            from .exam_listening_delivery import migrate_owner
            migrate_owner(c)
            c.execute('DROP INDEX IF EXISTS idx_diagnostic_prompt_pending_session')
            c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_diagnostic_prompt_pending_session
                ON diagnostic_prompt(origin_session_id) WHERE owner_kind='diagnostic'
                AND status IN ('waiting_answer','answer_saved')""")
            from .diagnostic_history_identity import ensure_schema
            ensure_schema(c)
            c.commit()

    @classmethod
    def borrowed(cls, connection):
        """Use an already-owned write transaction without rerunning schema DDL."""
        instance = cls.__new__(cls)
        instance.database = _TransactionDatabase(connection)
        return instance

    @contextmanager
    def _transaction(self):
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise

    @staticmethod
    def _text(value, field):
        if not isinstance(value, str) or not value.strip():
            raise DiagnosticPromptError("invalid_" + field)

    @staticmethod
    def _session(c, session_id):
        row = c.execute("""SELECT ds.*,cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp ON cp.child_id=ds.child_id WHERE session_id=?""", (session_id,)).fetchone()
        if row is None:
            raise DiagnosticPromptError("unknown_session")
        return row

    @staticmethod
    def _owned(c, session, prompt_id):
        row = c.execute("SELECT * FROM diagnostic_prompt WHERE prompt_id=?", (prompt_id,)).fetchone()
        if not row or (row["origin_session_id"], row["child_id"]) != (session["session_id"], session["child_id"]):
            raise DiagnosticPromptError("prompt_ownership_conflict")
        return dict(row)

    @staticmethod
    def _ordinary_owned(c, session, prompt_id):
        row = DiagnosticPromptService._owned(c,session,prompt_id)
        from .exam_listening_delivery import require_diagnostic_owner
        require_diagnostic_owner(row)
        return row

    @staticmethod
    def _item(c, child_id, item_id):
        if not LearningStateService._eligible_diagnostic(c, child_id, item_id):
            return None
        row = c.execute("""SELECT di.*,kn.track,kn.subject,kn.title,kn.grade_band,kn.knowledge_version,
            kn.source_ref AS node_source_ref FROM diagnostic_item di
            JOIN knowledge_node kn USING(knowledge_node_id) WHERE diagnostic_item_id=?""", (item_id,)).fetchone()
        if not row or not row["prompt"].strip():
            return None
        return dict(row)

    @staticmethod
    def _review(c, child_id, review_id, *, allowed_statuses=("pending",)):
        try:
            StudyRepository._validate_review_source(c, review_id, child_id, allowed_statuses=allowed_statuses)
        except PlanInputError:
            return None
        row = c.execute("SELECT * FROM review_task WHERE review_task_id=?", (review_id,)).fetchone()
        if not row or datetime.fromisoformat(row["due_at"]) > datetime.fromisoformat(utc_now()):
            return None
        return {key: row[key] for key in ("review_task_id", "child_id", "knowledge_node_id",
            "diagnostic_item_id", "source_evidence_id", "interval_label", "due_at", "created_at")}

    @staticmethod
    def _scheduled_source_reason(c, row):
        if row['status'] != 'answered':
            from .document_independent_check import source_reason as document_source_reason
            document_reason = document_source_reason(c, row)
            if document_reason:
                return document_reason
            from .numeric_clarification import source_reason
            numeric_reason = source_reason(c,row)
            if numeric_reason:
                return numeric_reason
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='agent_plan' AND type='table'").fetchone():
            return None
        from .agent_runtime import AgentRuntimeStore
        oral_reason = AgentRuntimeStore.oral_followup_prompt_source_reason(c,row)
        if oral_reason:
            return oral_reason
        writing_reason = AgentRuntimeStore.writing_followup_prompt_source_reason(c, row)
        if writing_reason:
            return writing_reason
        commands = c.execute("""SELECT p.status,s.status AS step_status,p.context_manifest_json,
            p.objective_id,o.status AS objective_status,o.child_id AS objective_child_id,
            o.session_id AS objective_session_id
            FROM agent_plan p JOIN agent_step s USING(plan_id)
            LEFT JOIN agent_objective o ON o.objective_id=p.objective_id
            WHERE p.child_id=? AND p.session_id=? AND s.step_index=0
              AND json_extract(CASE WHEN json_valid(p.context_manifest_json) THEN p.context_manifest_json ELSE '{}' END,
                  '$.goal_prompt_binding.prompt_id')=?""",
            (row['child_id'], row['origin_session_id'], row['prompt_id'])).fetchall()
        if not commands:
            return None
        for command in commands:
            if command['status'] == 'cancelled' or command['step_status'] == 'cancelled':
                return 'scheduled_command_cancelled'
            if command['objective_id']:
                if (command['objective_child_id'], command['objective_session_id']) != (
                        row['child_id'], row['origin_session_id']):
                    return 'scheduled_objective_changed'
                if command['objective_status'] == 'cancelled':
                    return 'scheduled_objective_cancelled'
        command = commands[0]
        binding = json.loads(command['context_manifest_json']).get('task_start_binding') or {}
        item = c.execute("""SELECT pi.source_type,pi.source_ref,pi.task_type FROM plan_item pi
            JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=? AND dp.child_id=?
            AND dp.session_id=? AND dp.status='active'""",
            (binding.get('plan_item_id'), row['child_id'], row['origin_session_id'])).fetchone()
        if not item or (item['source_type'], item['source_ref']) != (binding.get('source_type'), binding.get('source_ref')):
            return 'scheduled_source_changed'
        prompt_binding = json.loads(command['context_manifest_json']).get('goal_prompt_binding') or {}
        original_binding = prompt_binding.get('binding')
        if prompt_binding.get('oral_supply'):
            from .oral_supply_execution import resolve_execution
            try:
                resolved=resolve_execution(c,row['origin_session_id'],binding.get('plan_item_id'))
            except ValueError:
                return 'scheduled_oral_supply_source_changed'
            if (resolved['diagnostic_item_id']!=row['diagnostic_item_id'] or resolved['item_version']!=row['item_version']
                    or resolved['binding']!=original_binding):
                return 'scheduled_oral_supply_source_changed'
        foundation_binding = original_binding.get('goal_foundation') if isinstance(original_binding, dict) else None
        if item['task_type'] == 'goal_prerequisite' or foundation_binding:
            if item['task_type'] != 'goal_prerequisite' or not foundation_binding:
                return 'scheduled_foundation_binding_changed'
            if row['status'] != 'answered':
                from .goal_roadmap import GoalRoadmapService
                resolved = GoalRoadmapService._resolve_execution(c, row['origin_session_id'], binding.get('plan_item_id'),
                    prompt_id=row['prompt_id'])
                if (resolved['status'] != 'ready' or resolved['diagnostic_item_id'] != row['diagnostic_item_id']
                        or resolved['item_version'] != row['item_version'] or resolved['binding'] != prompt_binding['binding']):
                    return 'scheduled_foundation_source_changed'
        if prompt_binding.get('closed_review') and row['status'] != 'answered':
            from .closed_review_execution import resolve_execution
            resolved=resolve_execution(c,row['origin_session_id'],binding.get('plan_item_id'))
            if (resolved['status']!='ready' or resolved['diagnostic_item_id']!=row['diagnostic_item_id']
                    or resolved['item_version']!=row['item_version'] or resolved['binding']!=prompt_binding.get('binding')):
                return 'scheduled_closed_review_source_changed'
        if prompt_binding.get('school_review') and row['status'] != 'answered':
            from .school_review_execution import resolve_execution
            resolved = resolve_execution(c, row['origin_session_id'], binding.get('plan_item_id'))
            if (resolved['status'] != 'ready' or resolved['diagnostic_item_id'] != row['diagnostic_item_id']
                    or resolved['item_version'] != row['item_version'] or resolved['binding'] != prompt_binding.get('binding')):
                return 'scheduled_school_review_source_changed'
        if item['source_type'] in {'junior_exam_goal', 'pet_daily', 'competition_math'} and row['status'] != 'answered':
            from .goal_roadmap import goal_catalog_source_reason
            source_reason = goal_catalog_source_reason(c, row['origin_session_id'], binding.get('plan_item_id'))
            if source_reason:
                return source_reason
        if prompt_binding.get('stage') and row['status'] != 'answered':
            from .stage_execution import resolve_stage_execution
            resolved = resolve_stage_execution(c, row['origin_session_id'], binding.get('plan_item_id'))
            if (resolved['status'] != 'ready' or resolved['diagnostic_item_id'] != row['diagnostic_item_id']
                    or resolved['item_version'] != row['item_version']):
                return 'scheduled_stage_source_changed'
        if prompt_binding.get('pet') and row['status'] != 'answered':
            from .pet_task_execution import resolve_pet_task_execution
            resolved = resolve_pet_task_execution(c, row['origin_session_id'], binding.get('plan_item_id'))
            if (resolved['status'] != 'ready' or resolved['diagnostic_item_id'] != row['diagnostic_item_id']
                    or resolved['item_version'] != row['item_version']
                    or resolved['binding'] != prompt_binding.get('binding')):
                return 'scheduled_pet_source_changed'
        return None

    @staticmethod
    def _validate_scoring_permission(c, *, prompt_id, session_id, item_id, answer,
                                     client_action_id, review_task_id, hint_level, existing):
        """Internal admission guard, called only inside submit_diagnostic's transaction.

        The persisted binding, not an HTTP option or possession of an ID, grants
        phase-independent scoring. A replay must still pass source/owner checks.
        """
        DiagnosticPromptService._text(prompt_id, "trusted_prompt_id")
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
            raise DiagnosticPromptError("unknown_trusted_prompt")
        session = DiagnosticPromptService._session(c, session_id)
        row = DiagnosticPromptService._ordinary_owned(c, session, prompt_id)
        from .school_review_execution import pending_task_start as pet_prompt_pending_start
        if pet_prompt_pending_start(c, row):
            raise DiagnosticPromptError('scheduled_pet_task_not_started')
        if row['status'] != 'answered':
            source_reason = DiagnosticPromptService._scheduled_source_reason(c, row)
            if source_reason:
                raise DiagnosticPromptError(source_reason)
        if (session["status"] != "active" or session["child_status"] != "active"
                or session["state"] in {"evolving", "completed"}):
            raise DiagnosticPromptError("origin_session_inactive")
        if (row["diagnostic_item_id"] != item_id or row["review_task_id"] != review_task_id
                or client_action_id != "diagnostic-prompt-answer:" + prompt_id):
            raise DiagnosticPromptError("scoring_binding_conflict")
        if not row["answer_action_id"] or row["status"] not in {"answer_saved", "answered"}:
            raise DiagnosticPromptError("prompt_answer_not_saved")
        item = DiagnosticPromptService._item(c, row["child_id"], item_id)
        if item is None or _version(item) != row["item_version"]:
            raise DiagnosticPromptError("item_version_changed")
        if review_task_id:
            review = DiagnosticPromptService._review(c, row["child_id"], review_task_id,
                allowed_statuses=("pending", "completed") if existing else ("pending",))
            if review is None or _version(review) != row["review_version"]:
                raise DiagnosticPromptError("review_source_invalid")
        if existing is not None and existing["child_id"] != row["child_id"]:
            raise DiagnosticPromptError("scoring_receipt_conflict")
        if row["status"] == "answered":
            receipt = DiagnosticPromptService._receipt(c, row)
            if receipt is None or existing is None or existing["attempt_id"] != row["attempt_id"]:
                raise DiagnosticPromptError("scoring_receipt_conflict")
            saved = {"answer": receipt["submitted_answer"], "hint_level": receipt["hint_level"]}
        else:
            if row["attempt_id"] is not None:
                raise DiagnosticPromptError("scoring_receipt_conflict")
            try:
                saved = json.loads(row["answer_json"])
            except (TypeError, ValueError):
                raise DiagnosticPromptError("prompt_answer_not_saved") from None
        if (not isinstance(saved, dict) or type(saved.get("hint_level")) is not int
                or saved != {"answer": answer, "hint_level": hint_level}):
            raise DiagnosticPromptError("scoring_answer_conflict")
        if existing is None:
            from .agent_runtime import AgentRuntimeStore
            AgentRuntimeStore.validate_local_answer_write(c, row['authorized_turn_id'] if 'authorized_turn_id' in row.keys() else None,
                'diagnostic.submit', child_id=row['child_id'], session_id=session_id,
                binding={'prompt_id': prompt_id, 'item_version': row['item_version']}, answer=answer)

    def _validate(self, c, session, row):
        if row["status"] not in {"waiting_answer", "answer_saved"}:
            return row
        reason = self._scheduled_source_reason(c, row)
        item = self._item(c, row["child_id"], row["diagnostic_item_id"])
        if (session["status"] != "active" or session["child_status"] != "active"
                or session["state"] in {"evolving", "completed"}):
            reason = "origin_session_inactive"
        elif item is None:
            reason = "item_unavailable"
        elif _version(item) != row["item_version"]:
            reason = "item_version_changed"
        elif row["review_task_id"]:
            review = self._review(c, row["child_id"], row["review_task_id"])
            if review is None or _version(review) != row["review_version"]:
                reason = "review_source_invalid"
        binding=json.loads(row['request_json']).get('paper_selection_binding')
        if binding:
            from .paper_learning_selection import validate_snapshot
            from .privacy_operation_receipt import generation
            from .local_supply import LocalSupplyError
            try:
                if binding['origin_generation']!=generation(c):
                    raise LocalSupplyError('PAPER_SELECTION_RESTORE_RESELECT_REQUIRED')
                validate_snapshot(c,row['child_id'],binding['source'])
            except LocalSupplyError as error:
                reason=str(error)
        if reason:
            c.execute("UPDATE diagnostic_prompt SET status='invalidated',reason=?,updated_at=? WHERE prompt_id=?",
                      (reason, utc_now(), row["prompt_id"]))
            row = {**row, "status": "invalidated", "reason": reason}
        return row

    @staticmethod
    def _receipt(c, row):
        receipt = c.execute("""SELECT da.*,le.hint_level FROM diagnostic_attempt da
            JOIN learning_evidence le ON le.evidence_id=da.evidence_id
              AND le.child_id=da.child_id AND le.session_id=da.session_id
            WHERE attempt_id=?""", (row["attempt_id"],)).fetchone()
        keys = ("child_id", "diagnostic_item_id", "review_task_id")
        if not receipt or any(receipt[key] != row[key] for key in keys) or receipt["session_id"] != row["origin_session_id"]:
            return None
        return receipt

    @staticmethod
    def _result(c, row, *, reason=None) -> PromptResult:
        if row is None:
            return {"status": "no_item", "prompt": None, "attempt": None, "reason": reason or "no_pending_prompt"}
        prompt = {key: row[key] for key in ("prompt_id", "item_version", "diagnostic_item_id",
            "review_task_id", "child_id", "origin_session_id", "created_at")}
        prompt["item"] = json.loads(row["item_json"])
        if prompt['item'].get('answer_type') == 'open_response':
            # Input modality is not an answer key. Derive it from the current
            # versioned contract, including for prompts saved before this field.
            prompt['item']['response_mode'] = None
            try:
                from .local_supply import validate_item
                from .open_response_profile import profile_for_item
                source = DiagnosticPromptService._item(c, row['child_id'], row['diagnostic_item_id'])
                if source and _version(source) == row['item_version']:
                    owned = validate_item(c, row['child_id'], row['diagnostic_item_id'])
                    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (owned['knowledge_node_id'],)).fetchone()
                    prompt['item']['response_mode'] = profile_for_item(owned, dict(node))['response_mode']
            except (ValueError, TypeError, KeyError):
                pass
        from .pet_skill_gate import pet_skill_gate
        modality = pet_skill_gate(c, row["child_id"], row["diagnostic_item_id"])["modality"]
        prompt["pet_audio_modality"] = modality if modality in {"listening", "speaking"} else None
        prompt["item"]["pet_audio_modality"] = prompt["pet_audio_modality"]
        if prompt['item'].get('track') == 'school' and (modality == 'listening'
                or prompt['item'].get('answer_type') == 'listening_text'):
            prompt['item']['stimulus_text'] = None
        from .specialized_catalog import SCHOOL_LISTENING_STIMULI, LEGACY_SCHOOL_LISTENING_PROMPT
        if (prompt['item'].get('diagnostic_item_id') in SCHOOL_LISTENING_STIMULI
                and prompt['item'].get('prompt') == LEGACY_SCHOOL_LISTENING_PROMPT):
            prompt['item']['prompt'] = '原听力题的版本已变化，请核对原记录后继续。'
            prompt['item']['stimulus_text'] = None
        attempt = None
        status = row["status"]
        from .school_review_execution import pending_task_start as pet_prompt_pending_start
        pending_start = pet_prompt_pending_start(c, row)
        if pending_start:
            return {"status": "awaiting_task_start", "prompt": prompt, "attempt": None,
                    "reason": "scheduled_pet_task_not_started", "plan_item_id": pending_start}
        if status == "answered":
            receipt = DiagnosticPromptService._receipt(c, row)
            if not receipt:
                status, reason = "invalidated", "attempt_unavailable"
            else:
                attempt = LearningStateService._attempt(c, row["attempt_id"])
        result = {"status": status, "prompt": prompt, "attempt": attempt, "reason": reason or row["reason"]}
        if status == 'answered':
            from .numeric_clarification import offer
            clarification = offer(c, row)
            if clarification:
                result['numeric_clarification'] = clarification
        from .listening_review_submission import deferred_view
        deferred = deferred_view(c, row)
        if deferred:
            result['deferred_answer'] = deferred
        try:
            has_teaching_context=bool(json.loads(row['request_json']).get('document_teaching_context'))
        except (ValueError,TypeError,AttributeError):
            has_teaching_context=False
        if has_teaching_context:
            from .document_spoken_supplement import independent_teaching_context
            result['teaching_context']=independent_teaching_context(c,row)
        return result

    def prepare(self, session_id: str, *, track: str = "school", preferred_item_id: str | None = None,
                subject: str | None = None, review_task_id: str | None = None,
                client_action_id: str, _selection_item_id: str | None = None,
                writing_followup: dict | None = None, writing_reference_revision: dict | None = None,
                _writing_authorized_turn_id: str | None = None,
                _oral_authorized_turn_id: str | None = None, _oral_followup: dict | None = None,
                numeric_clarification: dict | None = None,
                _mistake_listening_delivery: dict | None = None, require_unexposed: bool = False,
                knowledge_node_id: str | None = None, paper_authorized_turn_id: str | None = None,
                _page_source_store=None) -> PromptResult:
        for field, value in (("track", track), ("client_action_id", client_action_id)):
            self._text(value, field)
        for field, value in (("preferred_item_id", preferred_item_id), ("subject", subject), ("review_task_id", review_task_id), ('knowledge_node_id',knowledge_node_id)):
            if value is not None:
                self._text(value, field)
        if _selection_item_id is not None:
            self._text(_selection_item_id, 'selection_item_id')
        request_values = {"track": track, "preferred_item_id": preferred_item_id, "subject": subject, "review_task_id": review_task_id}
        if paper_authorized_turn_id is not None:
            self._text(paper_authorized_turn_id, "paper_authorized_turn_id")
            request_values["paper_authorized_turn_id"] = paper_authorized_turn_id
        if type(require_unexposed) is not bool:
            raise DiagnosticPromptError('unexposed_request_contract_invalid')
        if knowledge_node_id is not None:
            if not require_unexposed:
                raise DiagnosticPromptError('unexposed_request_contract_invalid')
            request_values['knowledge_node_id']=knowledge_node_id
        if require_unexposed:
            if any(value is not None for value in (review_task_id, writing_followup, writing_reference_revision,
                    _writing_authorized_turn_id, _oral_authorized_turn_id, _oral_followup,
                    numeric_clarification, _mistake_listening_delivery)):
                raise DiagnosticPromptError('unexposed_request_contract_invalid')
            request_values['require_unexposed'] = True
        if _mistake_listening_delivery is not None:
            required = {"version", "mistake_id", "plan_item_id", "activity_key"}
            if (not isinstance(_mistake_listening_delivery, dict)
                    or set(_mistake_listening_delivery) != required
                    or _mistake_listening_delivery.get("version") != "mistake_listening_delivery_v1"
                    or any(not isinstance(_mistake_listening_delivery.get(field), str)
                           or not _mistake_listening_delivery[field].strip()
                           or len(_mistake_listening_delivery[field]) > 240
                           for field in required)
                    or track not in {'pet', 'school'} or not preferred_item_id or review_task_id is not None
                    or any(value is not None for value in (writing_followup, writing_reference_revision,
                        _writing_authorized_turn_id, _oral_authorized_turn_id, _oral_followup,
                        numeric_clarification))):
                raise DiagnosticPromptError("mistake_listening_delivery_contract_invalid")
            request_values["mistake_listening_delivery"] = _mistake_listening_delivery
        if numeric_clarification is not None:
            if (not isinstance(numeric_clarification, dict)
                    or set(numeric_clarification) != {'parent_prompt_id','parent_item_version','parent_attempt_id'}
                    or any(not isinstance(value,str) or not 1 <= len(value) <= 160 for value in numeric_clarification.values())
                    or not preferred_item_id or writing_followup is not None or writing_reference_revision is not None
                    or _oral_followup is not None or _oral_authorized_turn_id is not None or _writing_authorized_turn_id is not None):
                raise DiagnosticPromptError('numeric_clarification_contract_invalid')
            request_values['numeric_clarification'] = numeric_clarification
        if _oral_authorized_turn_id is not None or _oral_followup is not None:
            self._text(_oral_authorized_turn_id,'oral_authorized_turn_id')
            if (not isinstance(_oral_followup,dict) or set(_oral_followup)!={'action','binding_version'}
                    or writing_followup is not None or writing_reference_revision is not None
                    or _writing_authorized_turn_id is not None or track not in {'pet','school'} or not preferred_item_id or review_task_id is not None):
                raise DiagnosticPromptError('oral_followup_contract_invalid')
            request_values.update(oral_authorized_turn_id=_oral_authorized_turn_id,oral_followup=_oral_followup)
        if writing_reference_revision is not None:
            required = {'parent_prompt_id','parent_item_version','reference_turn_id','request_turn_id','binding_version'}
            if (not isinstance(writing_reference_revision,dict) or set(writing_reference_revision) != required
                    or writing_followup is not None or not preferred_item_id or track not in {'pet','school'} or review_task_id is not None):
                raise DiagnosticPromptError('writing_reference_revision_contract_invalid')
            request_values['writing_reference_revision'] = writing_reference_revision
        if writing_followup is not None:
            required = {'parent_prompt_id', 'parent_item_version', 'kind', 'binding_version'}
            if isinstance(writing_followup,dict) and 'parent_session_id' in writing_followup:
                required.add('parent_session_id')
                if not _writing_authorized_turn_id or not isinstance(writing_followup['parent_session_id'],str):
                    raise DiagnosticPromptError('writing_followup_authorization_required')
            if (not isinstance(writing_followup, dict) or set(writing_followup) != required
                    or not preferred_item_id or track not in {'pet','school'} or review_task_id is not None):
                raise DiagnosticPromptError('writing_followup_contract_invalid')
            request_values['writing_followup'] = writing_followup
        if _writing_authorized_turn_id is not None:
            self._text(_writing_authorized_turn_id, 'writing_authorized_turn_id')
            if writing_followup is None:
                raise DiagnosticPromptError('writing_followup_required')
            request_values['writing_authorized_turn_id'] = _writing_authorized_turn_id
        request = _json(request_values)
        with self._transaction() as c:
            session = self._session(c, session_id)
            if _oral_authorized_turn_id is not None:
                from .agent_runtime import AgentRuntimeStore
                existing = c.execute('SELECT * FROM diagnostic_prompt WHERE prepare_action_id=?',(client_action_id,)).fetchone()
                admitted = AgentRuntimeStore.inspect_oral_followup_execution(c,_oral_authorized_turn_id,prepared=existing)
                target = admitted['binding']['action']
                if (admitted['binding']!=_oral_followup or target['diagnostic_item_id']!=preferred_item_id
                        or target['child_id']!=session['child_id'] or target['session_id']!=session_id):
                    raise DiagnosticPromptError('oral_followup_authorization_changed')
            if _writing_authorized_turn_id is not None:
                from .agent_runtime import AgentRuntimeStore
                admitted = AgentRuntimeStore.inspect_writing_followup_execution(c, _writing_authorized_turn_id)
                packet = admitted['binding']
                target = packet['action']
                if (target['session_id'] != session_id or target['child_id'] != session['child_id']
                        or target['diagnostic_item_id'] != preferred_item_id
                        or packet['binding_version'] != writing_followup['binding_version']
                        or any(target[field] != writing_followup[field] for field in
                            ('parent_prompt_id', 'parent_item_version', 'kind'))
                        or target.get('parent_session_id')!=writing_followup.get('parent_session_id')):
                    raise DiagnosticPromptError('writing_followup_authorization_changed')
            replay = c.execute("SELECT prompt_id,request_json FROM diagnostic_prompt WHERE prepare_action_id=?", (client_action_id,)).fetchone()
            if numeric_clarification is not None:
                from .numeric_clarification import validate
                validate(c, session, numeric_clarification, existing=replay, preferred_item_id=preferred_item_id,
                         track=track, review_task_id=review_task_id)
            if replay:
                row = self._ordinary_owned(c, session, replay["prompt_id"])
                replay_request=json.loads(replay["request_json"])
                replay_request.pop("paper_selection_binding",None)
                if _json(replay_request) != request:
                    raise DiagnosticPromptError("prepare_payload_conflict")
                return self._result(c, self._validate(c, session, row))
            if paper_authorized_turn_id is not None:
                from .agent_runtime import AgentRuntimeStore
                AgentRuntimeStore.revalidate_original_client_execution(c,session_id,paper_authorized_turn_id,
                    page_source_store=_page_source_store)
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='specialized_exam_run'").fetchone() and c.execute(
                    "SELECT 1 FROM specialized_exam_run WHERE session_id=? AND delivery_phase!='ordinary' AND status IN ('active','paused')",(session_id,)).fetchone():
                raise DiagnosticPromptError('exam_run_owns_session')
            pending = c.execute("SELECT prompt_id FROM diagnostic_prompt WHERE origin_session_id=? AND owner_kind='diagnostic' AND status IN ('waiting_answer','answer_saved')", (session_id,)).fetchone()
            if pending and self._validate(c, session, self._ordinary_owned(c, session, pending["prompt_id"]))["status"] in {"waiting_answer", "answer_saved"}:
                raise DiagnosticPromptError("pending_prompt_exists")
            if (session["status"] != "active" or session["child_status"] != "active"
                    or session["state"] in {"evolving", "completed"}):
                return self._result(c, None, reason="origin_session_inactive")
            learning = LearningStateService(_TransactionDatabase(c))
            review = self._review(c, session["child_id"], review_task_id) if review_task_id else None
            if review_task_id and (not review or preferred_item_id not in (None, review["diagnostic_item_id"])):
                return self._result(c, None, reason="review_source_invalid")
            from .diagnostic_history_identity import HistoryIdentityUnavailable
            paper_selection=None
            if paper_authorized_turn_id is not None and not any((preferred_item_id,review_task_id,_selection_item_id)):
                from .paper_learning_selection import selected_for_voice
                from .local_supply import LocalSupplyError
                try:
                    paper_selection=selected_for_voice(c,session,paper_authorized_turn_id)
                except LocalSupplyError as error:
                    return self._result(c,None,reason=str(error))
                if paper_selection and (track!='school' or knowledge_node_id is not None):
                    return self._result(c,None,reason='PAPER_SELECTION_SCOPE_CONFLICT')
            try:
                if paper_selection:
                    selected=None
                    for candidate in paper_selection['items']:
                        picked=learning.next_diagnostic(session['child_id'],track,candidate['diagnostic_item_id'],subject,
                            require_unexposed=True)
                        if picked:
                            selected=picked
                            request_values['paper_selection_binding']={'version':paper_selection['version'],
                                'origin_generation':paper_selection['origin_generation'],'source':candidate}
                            request=_json(request_values)
                            break
                    if selected is None:
                        return self._result(c,None,reason='PAPER_SELECTION_EXHAUSTED')
                else:
                    selected = learning.next_diagnostic(session["child_id"], track,
                    review["diagnostic_item_id"] if review else preferred_item_id or _selection_item_id, subject,
                    **({'require_unexposed': True} if require_unexposed else {}),
                    **({'knowledge_node_id':knowledge_node_id} if knowledge_node_id is not None else {}))
            except HistoryIdentityUnavailable:
                return self._result(c, None, reason='unexposed_history_unavailable')
            if not selected:
                return self._result(c, None, reason="unexposed_item_unavailable" if require_unexposed else "item_unavailable")
            item = self._item(c, session["child_id"], selected["diagnostic_item_id"])
            if not item or item["track"] != track or (subject is not None and item["subject"] != subject):
                return self._result(c, None, reason="item_unavailable")
            if _mistake_listening_delivery is not None:
                from .listening_modality import listening_node
                node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone()
                if not node or item['answer_type'] != 'listening_text' or not listening_node(node, item['answer_type']):
                    raise DiagnosticPromptError('mistake_listening_delivery_contract_invalid')
            review_task_id = review_task_id or selected.get("review_task_id")
            if numeric_clarification is not None and review_task_id != request_values['review_task_id']:
                raise DiagnosticPromptError('numeric_clarification_review_changed')
            review = self._review(c, session["child_id"], review_task_id) if review_task_id else None
            if review_task_id and (not review or review["diagnostic_item_id"] != item["diagnostic_item_id"]):
                return self._result(c, None, reason="review_source_invalid")
            now, prompt_id = utc_now(), str(uuid4())
            from .diagnostic_history_identity import snapshot
            identity_snapshot = snapshot(c, child_id=session['child_id'], session_id=session_id,
                prompt_id=prompt_id, item=item)
            c.execute("""INSERT INTO diagnostic_prompt(prompt_id,child_id,origin_session_id,diagnostic_item_id,
                review_task_id,item_version,review_version,item_json,request_json,prepare_action_id,status,created_at,updated_at,identity_snapshot_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,'waiting_answer',?,?,?)""", (prompt_id, session["child_id"], session_id,
                item["diagnostic_item_id"], review_task_id, _version(item), _version(review) if review else None,
                _json({key: item[key] for key in _ITEM_FIELDS}), request, client_action_id, now, now, identity_snapshot))
            PaperScoringService.register_presented_item_authority(
                c, child_id=session["child_id"], session_id=session_id, item=item,
                item_version=_version(item), prompt_id=prompt_id,
            )
            if writing_followup is not None:
                from .writing_teaching import bind_writing_followup
                try:
                    bind_writing_followup(c, session_id=session_id, prompt_id=prompt_id,
                        item_version=_version(item), client_action_id=client_action_id, **writing_followup)
                except ValueError as error:
                    if not str(error).startswith('WRITING_'):
                        raise
                    raise DiagnosticPromptError(str(error).lower()) from error
                if writing_followup.get('parent_session_id'):
                    from .writing_daily_followup import bind_prompt
                    bind_prompt(c,_writing_authorized_turn_id,self._ordinary_owned(c,session,prompt_id))
            if _oral_authorized_turn_id is not None:
                AgentRuntimeStore.bind_oral_followup_prompt(c,_oral_authorized_turn_id,
                    {'prompt':{'prompt_id':prompt_id,'item_version':_version(item)}})
            if writing_reference_revision is not None:
                from .writing_teaching import bind_reference_revision
                try:
                    bind_reference_revision(c,session_id=session_id,prompt_id=prompt_id,
                        item_version=_version(item),client_action_id=client_action_id,**writing_reference_revision)
                except ValueError as error:
                    if not str(error).startswith('WRITING_'):
                        raise
                    raise DiagnosticPromptError(str(error).lower()) from error
            return self._result(c, self._ordinary_owned(c, session, prompt_id))

    def read(self, session_id: str, *, prompt_id: str | None = None) -> PromptResult:
        with self._transaction() as c:
            session = self._session(c, session_id)
            if prompt_id is None:
                latest = c.execute("SELECT prompt_id FROM diagnostic_prompt WHERE origin_session_id=? AND owner_kind='diagnostic' AND json_extract(request_json,'$.exam_slot') IS NULL ORDER BY rowid DESC LIMIT 1", (session_id,)).fetchone()
                if latest is None:
                    return self._result(c, None)
                prompt_id = latest["prompt_id"]
            row = self._validate(c, session, self._ordinary_owned(c, session, prompt_id))
            return self._result(c, row)

    def answer(self, session_id: str, *, prompt_id: str, item_version: str, answer: str,
               client_action_id: str, hint_level: int = 0, authorized_turn_id: str | None = None,
               writing_original_artifact_id: str | None = None, writing_original_revision: str | None = None) -> PromptResult:
        self._text(answer, "answer")
        self._text(client_action_id, "client_action_id")
        if type(hint_level) is not int or not 0 <= hint_level <= 5:
            raise DiagnosticPromptError("invalid_hint_level")
        if writing_original_artifact_id is not None:
            self._text(writing_original_artifact_id, 'writing_original_artifact_id')
        deferred_error = None
        with self._transaction() as c:
            session = self._session(c, session_id)
            row = self._ordinary_owned(c, session, prompt_id)
            from .native_writing_rejection import require_not_rejected
            try:
                require_not_rejected(c,'answer',session_id,client_action_id)
            except ValueError as error:
                raise DiagnosticPromptError(str(error).lower()) from error
            if row["item_version"] != item_version:
                raise DiagnosticPromptError("item_version_conflict")
            from .listening_review_submission import rejected_replay, admit
            rejected_replay(c, row, answer=answer, hint_level=hint_level,
                authorized_turn_id=authorized_turn_id, client_action_id=client_action_id)
            row = self._validate(c, session, row)
            if row["status"] in {"invalidated", "cancelled"}:
                return self._result(c, row)
            native_control_authorized = False
            if writing_original_revision is not None:
                from .native_writing import validate_submission
                try:
                    validate_submission(c,row,writing_original_artifact_id,writing_original_revision,answer)
                    from .native_writing_control import validate_retained_turn
                    native_control_authorized = validate_retained_turn(c,authorized_turn_id,row,answer)
                except ValueError as error:
                    raise DiagnosticPromptError(str(error).lower()) from error
            from .school_review_execution import pending_task_start as pet_prompt_pending_start
            if pet_prompt_pending_start(c, row):
                return self._result(c, row)
            oral = json.loads(row['request_json']).get('oral_followup')
            if oral:
                hint_level = max(hint_level,oral['action']['required_hint_level'])
            from .oral_delivery_resume import answer_hint_floor
            hint_level = max(hint_level,answer_hint_floor(c,row))
            spoken_retry = (json.loads(row['request_json']).get('document_independent') or {}).get('binding',{}).get('spoken_retry')
            if spoken_retry:
                hint_level = max(hint_level, spoken_retry['required_hint_level'], 1)
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_followup'").fetchone():
                revised = c.execute("SELECT 1 FROM writing_followup WHERE prompt_id=? AND kind='revision'", (prompt_id,)).fetchone()
                if revised:
                    hint_level = max(hint_level, 1)
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_reference_revision'").fetchone():
                if c.execute('SELECT 1 FROM writing_reference_revision WHERE prompt_id=?',(prompt_id,)).fetchone():
                    hint_level = max(hint_level, 1)
            payload = _json({"answer": answer, "hint_level": hint_level})
            action = c.execute("SELECT prompt_id FROM diagnostic_prompt WHERE answer_action_id=?", (client_action_id,)).fetchone()
            if action and action["prompt_id"] != prompt_id:
                raise DiagnosticPromptError("answer_action_conflict")
            if row["answer_action_id"] is not None:
                if row['writing_original_artifact_id'] != writing_original_artifact_id:
                    raise DiagnosticPromptError('writing_original_conflict')
                previous_payload = row["answer_json"]
                if row["status"] == "answered":
                    receipt = self._receipt(c, row)
                    if receipt is None:
                        return self._result(c, row)
                    previous_payload = _json({"answer": receipt["submitted_answer"], "hint_level": receipt["hint_level"]})
                if row["answer_action_id"] != client_action_id or previous_payload != payload:
                    raise DiagnosticPromptError("answer_payload_conflict")
            else:
                from .agent_runtime import AgentRuntimeStore
                if not native_control_authorized:
                    AgentRuntimeStore.validate_local_answer_write(c, authorized_turn_id, 'diagnostic.submit',
                        child_id=row['child_id'], session_id=session_id,
                        binding={'prompt_id': prompt_id, 'item_version': item_version}, answer=answer)
                deferred_error = admit(c, row, answer=answer, hint_level=hint_level,
                    authorized_turn_id=authorized_turn_id, client_action_id=client_action_id)
                if not deferred_error:
                    c.execute("UPDATE diagnostic_prompt SET status='answer_saved',answer_json=?,answer_action_id=?,authorized_turn_id=?,writing_original_artifact_id=?,updated_at=? WHERE prompt_id=?",
                              (payload, client_action_id, authorized_turn_id, writing_original_artifact_id, utc_now(), prompt_id))
        if deferred_error:
            raise DiagnosticPromptError(deferred_error)
        return self.resume(session_id, prompt_id=prompt_id)

    def resume(self, session_id: str, *, prompt_id: str) -> PromptResult:
        with self._transaction() as c:
            session = self._session(c, session_id)
            row = self._validate(c, session, self._ordinary_owned(c, session, prompt_id))
            from .school_review_execution import pending_task_start as pet_prompt_pending_start
            if pet_prompt_pending_start(c, row):
                return self._result(c, row)
            if row["status"] != "answer_saved":
                if row['status']=='answered':
                    from .document_check_followup import record
                    record(c,row)
                return self._result(c, row)
            payload = json.loads(row["answer_json"])
            from .agent_runtime import AgentRuntimeStore
            if not c.execute('SELECT 1 FROM diagnostic_attempt WHERE client_action_id=? AND child_id=?',
                             ('diagnostic-prompt-answer:' + prompt_id, row['child_id'])).fetchone():
                AgentRuntimeStore.validate_local_answer_write(c, row['authorized_turn_id'], 'diagnostic.submit',
                    child_id=row['child_id'], session_id=session_id,
                    binding={'prompt_id': prompt_id, 'item_version': row['item_version']}, answer=payload['answer'])
            learning = LearningStateService(_TransactionDatabase(c))
            from .database import local_media_scope
            attempt = learning.submit_diagnostic(session_id, row["diagnostic_item_id"], payload["answer"],
                "diagnostic-prompt-answer:" + prompt_id, row["review_task_id"], hint_level=payload["hint_level"],
                _trusted_prompt_id=prompt_id, _pet_media_scope=local_media_scope(c))
            c.execute("UPDATE diagnostic_prompt SET status='answered',attempt_id=?,answer_json=NULL,reason=NULL,updated_at=? WHERE prompt_id=?",
                      (attempt["attempt_id"], utc_now(), prompt_id))
            if row['writing_original_artifact_id']:
                from .writing_response_origin import bind_written_attempt
                try:
                    bind_written_attempt(c, child_id=row['child_id'], session_id=session_id,
                        prompt_id=prompt_id, item_version=row['item_version'], attempt_id=attempt['attempt_id'],
                        original_artifact_id=row['writing_original_artifact_id'])
                except ValueError as error:
                    if not str(error).startswith('WRITING_'):
                        raise
                    raise DiagnosticPromptError(str(error).lower()) from error
            # Audio qualification needs the saved prompt-to-attempt binding.
            # Materialize it in this same transaction, before exposing the receipt.
            evidence = c.execute('SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=?',
                                 (attempt['evidence_id'], row['child_id'])).fetchone()
            from .pet_skill_gate import pet_skill_gate
            if (evidence is not None and local_media_scope(c)
                    and pet_skill_gate(c, row['child_id'], row['diagnostic_item_id'])['modality'] == 'listening'):
                from .listening_reviews import reconcile_answered_listening_reviews
                reconcile_answered_listening_reviews(c, row['child_id'], prompt_id)
                LearningStateService._update_mastery(c, row['child_id'], evidence['knowledge_node_id'],
                    evidence['evidence_id'], evidence['correctness'], evidence['hint_level'],
                    bool(evidence['independent']), evidence['confidence'], utc_now())
            saved_row=self._ordinary_owned(c, session, prompt_id)
            from .document_check_followup import record
            record(c,saved_row)
            return self._result(c, saved_row)

    def cancel(self, session_id: str, *, prompt_id: str, item_version: str) -> PromptResult:
        with self._transaction() as c:
            session = self._session(c, session_id)
            row = self._ordinary_owned(c, session, prompt_id)
            if row["item_version"] != item_version:
                raise DiagnosticPromptError("item_version_conflict")
            if row["status"] == "answered":
                raise DiagnosticPromptError("answer_already_recorded")
            c.execute("UPDATE diagnostic_prompt SET status='cancelled',reason='cancelled',updated_at=? WHERE prompt_id=?",
                      (utc_now(), prompt_id))
            return self._result(c, self._ordinary_owned(c, session, prompt_id))
