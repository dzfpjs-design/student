"""Durable system-owned responsibility for unresolved paper scoring authority.

An unresolved paper answer must not disappear merely because no answer key or
calibrated rubric is available in the current turn. This journal records the
system's obligation without asking the child or parent to judge correctness.
"""
from __future__ import annotations

import json
import math
from typing import Any
from uuid import uuid4

from .cloud_privacy import sanitize_cloud_text
from .database import Database
from .model_gateway import ModelGateway, ModelRequest
from .repository import StudyRepository
from .repository import utc_now


ALLOWED_AUTHORITY_TYPES = (
    "teacher_mark",
    "published_answer_key",
    "agent_generated_rubric",
    "calibrated_subjective_rubric",
)

PAPER_AUTHORITY_DRAFT_VERSION = "paper_scoring_authority_draft_v1"
PAPER_AUTHORITY_CLOUD_POLICY_VERSION = "paper_scoring_cloud_draft_v1"
PAPER_AUTHORITY_MAX_ATTEMPTS = 3
ALLOWED_SUBJECTS = {
    "语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理",
    "道德与法治", "科学", "其他",
}
ALLOWED_ANSWER_MODES = {"objective", "subjective", "uncertain"}


def ensure_schema(connection: Any) -> None:
    if not hasattr(connection, "executescript"):
        return
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_scoring_supply_obligation (
          obligation_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          question_sha256 TEXT NOT NULL,
          answer_sha256 TEXT NOT NULL,
          authority_requirement_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_supply_obligation_child
          ON paper_scoring_supply_obligation(child_id, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS paper_scoring_supply_obligation_no_update
        BEFORE UPDATE ON paper_scoring_supply_obligation
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring supply obligation'); END;

        CREATE TABLE IF NOT EXISTS paper_scoring_supply_event (
          event_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_scoring_supply_obligation(obligation_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN (
            'pending_authority','source_candidate','rubric_candidate',
            'calibration_pending','failed_retryable','blocked_source',
            'qualified','withdrawn'
          )),
          reason TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_supply_event_obligation
          ON paper_scoring_supply_event(obligation_id, created_at);
        CREATE TRIGGER IF NOT EXISTS paper_scoring_supply_event_no_update
        BEFORE UPDATE ON paper_scoring_supply_event
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring supply event'); END;

        CREATE TABLE IF NOT EXISTS paper_scoring_supply_work (
          obligation_id TEXT PRIMARY KEY
            REFERENCES paper_scoring_supply_obligation(obligation_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','blocked')),
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
          owner_token TEXT,
          claimed_at TEXT,
          error_code TEXT,
          provider TEXT,
          model TEXT,
          prompt_tokens INTEGER,
          completion_tokens INTEGER,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_scoring_supply_work_status
          ON paper_scoring_supply_work(status, updated_at);
        """
    )


def _requirements() -> dict[str, Any]:
    return {
        "version": "paper_scoring_authority_requirement_v1",
        "allowed_authority_types": list(ALLOWED_AUTHORITY_TYPES),
        "child_or_parent_correctness_claim_allowed": False,
        "temporary_model_solution_allowed": False,
        "source_version_required": True,
        "exact_question_and_answer_binding_required": True,
        "subjective_model_calibration_required": True,
    }


def _append_event(
    connection: Any,
    obligation: Any,
    *,
    status: str,
    reason: str,
    evidence: dict[str, Any],
    client_action_id: str,
) -> None:
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = connection.execute(
        "SELECT * FROM paper_scoring_supply_event WHERE client_action_id=?",
        (client_action_id,),
    ).fetchone()
    if existing:
        expected = (
            obligation["obligation_id"], obligation["child_id"], status, reason, encoded
        )
        actual = tuple(existing[key] for key in (
            "obligation_id", "child_id", "status", "reason", "evidence_json"
        ))
        if actual != expected:
            raise ValueError("paper scoring supply event payload conflict")
        return
    connection.execute(
        """INSERT INTO paper_scoring_supply_event(
             event_id,obligation_id,child_id,status,reason,evidence_json,
             created_at,client_action_id
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (str(uuid4()), obligation["obligation_id"], obligation["child_id"],
         status, reason, encoded, utc_now(), client_action_id),
    )


def ensure_for_request(
    connection: Any,
    request: Any,
    *,
    action_prefix: str,
) -> dict[str, Any]:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_scoring_supply_obligation'").fetchone():
        ensure_schema(connection)
    existing = connection.execute(
        "SELECT * FROM paper_scoring_supply_obligation WHERE request_id=?",
        (request["request_id"],),
    ).fetchone()
    requirement_json = json.dumps(
        _requirements(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if existing:
        expected = (
            request["child_id"], request["session_id"], request["question_sha256"],
            request["answer_sha256"], requirement_json,
        )
        actual = tuple(existing[key] for key in (
            "child_id", "session_id", "question_sha256", "answer_sha256",
            "authority_requirement_json",
        ))
        if actual != expected:
            raise ValueError("paper scoring supply obligation payload conflict")
        obligation = existing
    else:
        obligation_id = str(uuid4())
        connection.execute(
            """INSERT INTO paper_scoring_supply_obligation(
                 obligation_id,request_id,child_id,session_id,question_sha256,
                 answer_sha256,authority_requirement_json,created_at,client_action_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (obligation_id, request["request_id"], request["child_id"],
             request["session_id"], request["question_sha256"], request["answer_sha256"],
             requirement_json, utc_now(), f"{action_prefix}:obligation"),
        )
        obligation = connection.execute(
            "SELECT * FROM paper_scoring_supply_obligation WHERE obligation_id=?",
            (obligation_id,),
        ).fetchone()
    if not connection.execute(
        "SELECT 1 FROM paper_scoring_supply_event WHERE obligation_id=?",
        (obligation["obligation_id"],),
    ).fetchone():
        _append_event(
            connection,
            obligation,
            status="pending_authority",
            reason="no qualified scoring authority is currently bound",
            evidence={
                "request_id": request["request_id"],
                "contains_question_or_answer_text": False,
                "system_owned": True,
            },
            client_action_id=f"{action_prefix}:pending",
        )
    return project(connection, obligation_id=obligation["obligation_id"])


def record_attempt(
    connection: Any,
    *,
    request_id: str,
    child_id: str,
    status: str,
    reason: str,
    evidence: dict[str, Any],
    client_action_id: str,
) -> dict[str, Any]:
    if status not in {
        "source_candidate", "rubric_candidate", "calibration_pending",
        "failed_retryable", "blocked_source", "withdrawn",
    }:
        raise ValueError("invalid paper scoring supply attempt status")
    obligation = connection.execute(
        """SELECT obligation.* FROM paper_scoring_supply_obligation AS obligation
           JOIN paper_scoring_request AS request USING(request_id)
           WHERE obligation.request_id=? AND obligation.child_id=?
             AND request.child_id=obligation.child_id""",
        (request_id, child_id),
    ).fetchone()
    if not obligation:
        raise ValueError("paper scoring supply obligation not found")
    if any(key in evidence for key in ("question_text", "answer_text", "image", "local_path")):
        raise ValueError("paper scoring supply event cannot duplicate child content")
    _append_event(
        connection, obligation, status=status, reason=reason,
        evidence=evidence, client_action_id=client_action_id,
    )
    return project(connection, obligation_id=obligation["obligation_id"])


def mark_qualified(
    connection: Any,
    request: Any,
    decision: Any,
    *,
    action_prefix: str,
) -> dict[str, Any]:
    current = ensure_for_request(connection, request, action_prefix=action_prefix)
    obligation = connection.execute(
        "SELECT * FROM paper_scoring_supply_obligation WHERE obligation_id=?",
        (current["obligation_id"],),
    ).fetchone()
    _append_event(
        connection,
        obligation,
        status="qualified",
        reason="qualified scoring authority bound",
        evidence={
            "decision_id": decision["decision_id"],
            "authority_type": decision["authority_type"],
            "authority_ref": decision["authority_ref"],
            "contains_question_or_answer_text": False,
        },
        client_action_id=f"{action_prefix}:qualified:{decision['decision_id']}",
    )
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_scoring_supply_work'"
    ).fetchone():
        connection.execute(
            """UPDATE paper_scoring_supply_work
               SET status='completed',owner_token=NULL,claimed_at=NULL,error_code=NULL,updated_at=?
               WHERE obligation_id=?""",
            (utc_now(), obligation["obligation_id"]),
        )
    return project(connection, obligation_id=obligation["obligation_id"])


def reopen_pending(
    connection: Any,
    request: Any,
    *,
    reason: str,
    action_prefix: str,
) -> dict[str, Any]:
    current = ensure_for_request(connection, request, action_prefix=action_prefix)
    obligation = connection.execute(
        "SELECT * FROM paper_scoring_supply_obligation WHERE obligation_id=?",
        (current["obligation_id"],),
    ).fetchone()
    _append_event(
        connection,
        obligation,
        status="pending_authority",
        reason=reason,
        evidence={"request_id": request["request_id"], "system_owned": True},
        client_action_id=f"{action_prefix}:reopened",
    )
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_scoring_supply_work'"
    ).fetchone():
        connection.execute(
            """UPDATE paper_scoring_supply_work
               SET status='pending',attempt_count=0,owner_token=NULL,claimed_at=NULL,
                   error_code=NULL,provider=NULL,model=NULL,prompt_tokens=NULL,
                   completion_tokens=NULL,updated_at=?
               WHERE obligation_id=?""",
            (utc_now(), obligation["obligation_id"]),
        )
    return project(connection, obligation_id=obligation["obligation_id"])


def project(
    connection: Any,
    *,
    obligation_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    if not obligation_id and not request_id:
        raise ValueError("paper scoring supply projection requires an identity")
    field = "obligation_id" if obligation_id else "request_id"
    value = obligation_id or request_id
    obligation = connection.execute(
        f"SELECT * FROM paper_scoring_supply_obligation WHERE {field}=?", (value,)
    ).fetchone()
    if not obligation:
        raise ValueError("paper scoring supply obligation not found")
    latest = connection.execute(
        """SELECT * FROM paper_scoring_supply_event
           WHERE obligation_id=? ORDER BY rowid DESC LIMIT 1""",
        (obligation["obligation_id"],),
    ).fetchone()
    work = None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_scoring_supply_work'"
    ).fetchone():
        row = connection.execute(
            """SELECT status,attempt_count,error_code,provider,model,prompt_tokens,
                      completion_tokens,updated_at
               FROM paper_scoring_supply_work WHERE obligation_id=?""",
            (obligation["obligation_id"],),
        ).fetchone()
        work = dict(row) if row else None
    evidence = json.loads(latest['evidence_json']) if latest else {}
    status = latest['status'] if latest else 'pending_authority'
    reason = latest['reason'] if latest else 'authority supply has not started'
    artifact_id = evidence.get('frozen_rubric_artifact_id')
    printed_candidate = None
    printed_execution = None
    printed_calibration = None
    qualified_candidate_id = None
    if status == 'qualified':
        decision = connection.execute("""SELECT authority_snapshot_json FROM paper_scoring_effective_decision
            WHERE request_id=? AND effective_status='qualified' AND authority_ref LIKE 'calibrated-printed-candidate:%'""",
            (request_id,)).fetchone()
        if decision:
            try:
                snapshot = json.loads(decision[0])
                artifact_id = snapshot['frozen_rubric_artifact_id']
                qualified_candidate_id = snapshot['printed_candidate_artifact_id']
            except (ValueError, KeyError, TypeError):
                status, reason = 'withdrawn', 'PRINTED_QUALIFIED_SNAPSHOT_UNAVAILABLE'
    if status in {'calibration_pending', 'qualified'} and artifact_id:
        from .paper_subjective_rubric import PaperSubjectiveRubricService
        try:
            current = PaperSubjectiveRubricService(None).current(
                obligation['child_id'], artifact_id, connection=connection)
            if current['status'] != 'calibration_pending':
                status = 'withdrawn' if current['status'] == 'source_withdrawn' else 'blocked_source'
                reason = current['reason_code']
        except (ValueError, KeyError, TypeError):
            status, reason = 'withdrawn', 'SUBJECTIVE_FROZEN_RUBRIC_UNAVAILABLE'
        from .paper_printed_score_candidate import PaperPrintedScoreCandidateService
        try:
            printed_candidate = (PaperPrintedScoreCandidateService(None).current(
                obligation['child_id'], qualified_candidate_id, connection=connection) if qualified_candidate_id
                else PaperPrintedScoreCandidateService(None).latest_for_rubric(
                    obligation['child_id'], artifact_id, connection=connection))
        except (ValueError, KeyError, TypeError):
            printed_candidate = {'status': 'candidate_withdrawn', 'reason_code': 'PRINTED_CANDIDATE_UNAVAILABLE',
                                 'score_authority_granted': False, 'cloud_transmission_allowed': False}
        from .paper_printed_score_execution import PaperPrintedScoreExecutionService
        printed_execution = PaperPrintedScoreExecutionService.for_rubric(connection, obligation['child_id'], artifact_id)
        if printed_candidate and printed_candidate.get('artifact_id'):
            from .paper_printed_calibration import PaperPrintedCalibrationService
            try:
                printed_calibration = PaperPrintedCalibrationService(None).latest_for_candidate(
                    obligation['child_id'], printed_candidate['artifact_id'], connection=connection)
            except (ValueError, KeyError, TypeError, AttributeError):
                printed_calibration = {'status': 'withdrawn', 'calibration_qualified': False,
                    'score_authority_granted': False, 'blockers': ['calibration_dependency_changed'],
                    'local_only': True, 'cloud_transmission_allowed': False}
    return {
        "obligation_id": obligation["obligation_id"],
        "request_id": obligation["request_id"],
        "child_id": obligation["child_id"],
        "status": status,
        "reason": reason,
        "requirements": json.loads(obligation["authority_requirement_json"]),
        "latest_evidence": evidence,
        "printed_score_candidate": printed_candidate,
        "printed_score_execution": printed_execution,
        "printed_calibration": printed_calibration,
        "system_owned": True,
        "requires_child_review": False,
        "requires_parent_subject_review": False,
        "cloud_transmission_allowed": False,
        "supply_work": work,
    }


class PaperScoringAuthorityDraftError(ValueError):
    pass


def _bounded_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise PaperScoringAuthorityDraftError(f"{field} must contain 1-8 items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PaperScoringAuthorityDraftError(f"{field} must contain strings")
        normalized = " ".join(item.split())
        if not 2 <= len(normalized) <= 80:
            raise PaperScoringAuthorityDraftError(f"{field} item length is invalid")
        result.append(normalized)
    return list(dict.fromkeys(result))


def parse_authority_draft(content: str) -> dict[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        value = value.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise PaperScoringAuthorityDraftError("model draft is not valid JSON") from error
    required = {
        "subject", "answer_mode", "task_points", "source_search_terms",
        "confidence", "requires_external_source",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise PaperScoringAuthorityDraftError("model draft fields do not match the contract")
    if payload["subject"] not in ALLOWED_SUBJECTS:
        raise PaperScoringAuthorityDraftError("model draft subject is unsupported")
    if payload["answer_mode"] not in ALLOWED_ANSWER_MODES:
        raise PaperScoringAuthorityDraftError("model draft answer mode is unsupported")
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise PaperScoringAuthorityDraftError("model draft confidence is invalid")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise PaperScoringAuthorityDraftError("model draft confidence is invalid")
    if not isinstance(payload["requires_external_source"], bool):
        raise PaperScoringAuthorityDraftError("requires_external_source must be boolean")
    return {
        "version": PAPER_AUTHORITY_DRAFT_VERSION,
        "subject": payload["subject"],
        "answer_mode": payload["answer_mode"],
        "task_points": _bounded_string_list(payload["task_points"], "task_points"),
        "source_search_terms": _bounded_string_list(
            payload["source_search_terms"], "source_search_terms"
        ),
        "confidence": round(confidence, 3),
        "requires_external_source": payload["requires_external_source"],
        "authority_granted": False,
        "correctness_decided": False,
    }


class PaperScoringAuthorityDraftService:
    """Produces source/rubric search hints without granting scoring authority."""

    def __init__(
        self,
        database: Database,
        gateway: ModelGateway | None,
        repository: StudyRepository | None = None,
        cloud_text_allowed: Any | None = None,
    ):
        self.database = database
        self.gateway = gateway
        self.repository = repository or StudyRepository(database)
        self.cloud_text_allowed = cloud_text_allowed or (lambda: False)

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            ensure_schema(connection)
            connection.commit()

    def recover(self) -> int:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                """UPDATE paper_scoring_supply_work
                   SET status='pending',owner_token=NULL,claimed_at=NULL,
                       error_code='APP_RESTART',updated_at=?
                   WHERE status='processing'""",
                (utc_now(),),
            ).rowcount
            connection.commit()
        return count

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是纸面题评分资料检索规划器。只做学科分类、作答类型分类、量表任务点和"
            "本地资料检索词草拟；不得判断学生答案对错，不得给分，不得生成标准答案，"
            "不得把你的常识当作评分权威。只返回 JSON 对象，字段必须且只能是："
            "subject（语文/数学/英语/物理/化学/生物/历史/地理/道德与法治/科学/其他）、"
            "answer_mode（objective/subjective/uncertain）、task_points（1到8条评分维度，"
            "不能含具体答案）、source_search_terms（1到8条本地教材或答案页检索词）、"
            "confidence（0到1）、requires_external_source（布尔值）。"
        )

    def _seed_work(self, connection: Any) -> None:
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO paper_scoring_supply_work(
                 obligation_id,child_id,status,attempt_count,updated_at
               )
               SELECT obligation.obligation_id,obligation.child_id,'pending',0,?
               FROM paper_scoring_supply_obligation AS obligation
               LEFT JOIN paper_scoring_effective_decision AS decision
                 ON decision.request_id=obligation.request_id
                AND decision.effective_status='qualified'
               WHERE decision.request_id IS NULL
                 AND COALESCE((
                   SELECT event.status FROM paper_scoring_supply_event AS event
                   WHERE event.obligation_id=obligation.obligation_id
                   ORDER BY event.rowid DESC LIMIT 1
                 ),'pending_authority') NOT IN ('qualified','withdrawn')""",
            (now,),
        )

    def _claim(self) -> tuple[Any, str] | None:
        if not self.gateway or not getattr(self.gateway, "configured", False):
            return None
        if not bool(self.cloud_text_allowed()):
            return None
        self.ensure_schema()
        owner_token = str(uuid4())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._seed_work(connection)
            row = connection.execute(
                """SELECT work.*,obligation.request_id,request.session_id,request.question_text,
                          request.answer_text,request.question_sha256,request.answer_sha256,
                          child.nickname,child.grade,child.curriculum_region
                   FROM paper_scoring_supply_work AS work
                   JOIN paper_scoring_supply_obligation AS obligation USING(obligation_id)
                   JOIN paper_scoring_request AS request USING(request_id)
                   JOIN child_profile AS child ON child.child_id=work.child_id
                   WHERE work.status='pending' AND work.attempt_count < ?
                     AND NOT EXISTS(
                       SELECT 1 FROM paper_scoring_effective_decision AS decision
                       WHERE decision.request_id=obligation.request_id
                         AND decision.effective_status='qualified'
                     )
                     AND COALESCE((
                       SELECT event.status FROM paper_scoring_supply_event AS event
                       WHERE event.obligation_id=work.obligation_id
                       ORDER BY event.rowid DESC LIMIT 1
                     ),'pending_authority') NOT IN ('qualified','withdrawn')
                   ORDER BY work.updated_at, work.obligation_id LIMIT 1""",
                (PAPER_AUTHORITY_MAX_ATTEMPTS,),
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            changed = connection.execute(
                """UPDATE paper_scoring_supply_work
                   SET status='processing',attempt_count=attempt_count+1,
                       owner_token=?,claimed_at=?,error_code=NULL,updated_at=?
                   WHERE obligation_id=? AND status='pending'""",
                (owner_token, utc_now(), utc_now(), row["obligation_id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                """SELECT work.*,obligation.request_id,request.session_id,request.question_text,
                          request.answer_text,request.question_sha256,request.answer_sha256,
                          child.nickname,child.grade,child.curriculum_region
                   FROM paper_scoring_supply_work AS work
                   JOIN paper_scoring_supply_obligation AS obligation USING(obligation_id)
                   JOIN paper_scoring_request AS request USING(request_id)
                   JOIN child_profile AS child ON child.child_id=work.child_id
                   WHERE work.obligation_id=?""",
                (row["obligation_id"],),
            ).fetchone()
            claimed = dict(claimed)
            from .paper_response_origin import snapshot
            try:
                claimed['original_response'] = snapshot(connection, claimed['child_id'], claimed['request_id'])
            except ValueError:
                claimed['original_response_error'] = 'PAPER_AUTHORITY_ORIGINAL_UNAVAILABLE'
            connection.commit()
        return claimed, owner_token

    def _owns(self, connection: Any, obligation_id: str, owner_token: str) -> bool:
        row = connection.execute(
            """SELECT status,owner_token FROM paper_scoring_supply_work
               WHERE obligation_id=?""",
            (obligation_id,),
        ).fetchone()
        return bool(row and row["status"] == "processing" and row["owner_token"] == owner_token)

    @staticmethod
    def _assert_original(connection: Any, row: Any) -> None:
        from .paper_response_origin import snapshot
        try:
            current = snapshot(connection, row['child_id'], row['request_id'])
        except ValueError as error:
            raise PaperScoringAuthorityDraftError('PAPER_AUTHORITY_ORIGINAL_CHANGED') from error
        if current != row.get('original_response'):
            raise PaperScoringAuthorityDraftError('PAPER_AUTHORITY_ORIGINAL_CHANGED')

    def _authorize_dispatch(self, obligation_id: str, owner_token: str, row: Any) -> None:
        if not bool(self.cloud_text_allowed()):
            raise PaperScoringAuthorityDraftError("CLOUD_TEXT_DISABLED")
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            if not self._owns(connection, obligation_id, owner_token):
                raise PaperScoringAuthorityDraftError("PAPER_AUTHORITY_ATTEMPT_CHANGED")
            self._assert_original(connection, row)
            decision = connection.execute(
                """SELECT 1 FROM paper_scoring_effective_decision AS decision
                   JOIN paper_scoring_supply_obligation AS obligation USING(request_id)
                   WHERE obligation.obligation_id=? AND decision.effective_status='qualified'""",
                (obligation_id,),
            ).fetchone()
            if decision:
                raise PaperScoringAuthorityDraftError("PAPER_AUTHORITY_ALREADY_QUALIFIED")

    @staticmethod
    def _sanitized_context(row: Any) -> dict[str, Any]:
        names = (row["nickname"],) if row["nickname"] else ()
        question = sanitize_cloud_text(row["question_text"], names)
        answer = sanitize_cloud_text(row["answer_text"], names)
        if not question.text.strip() or not answer.text.strip():
            raise PaperScoringAuthorityDraftError("PAPER_AUTHORITY_TEXT_EMPTY_AFTER_REDACTION")
        return {
            "grade": row["grade"],
            "curriculum_region": row["curriculum_region"],
            "question": question.text,
            "student_answer": answer.text,
            "redaction_count": question.redaction_count + answer.redaction_count,
        }

    async def process_next(self) -> dict[str, Any] | None:
        claimed = self._claim()
        if not claimed:
            return None
        row, owner_token = claimed
        attempt = int(row["attempt_count"])
        call_id: str | None = None
        try:
            if row.get('original_response_error'):
                raise PaperScoringAuthorityDraftError(row['original_response_error'])
            cloud_context = self._sanitized_context(row)
            provider = getattr(getattr(self.gateway, "settings", None), "model_provider", "configured")
            model = getattr(getattr(self.gateway, "settings", None), "model_name", "configured")
            context_manifest = {
                "raw_media": False,
                "child_identity": False,
                "question_text": True,
                "answer_text": True,
                "redaction_count": cloud_context.pop("redaction_count"),
                "authority_granted": False,
            }
            call_id = self.repository.start_model_call(
                row["session_id"], row["child_id"], "paper_scoring_authority_draft",
                provider, model,
                len(cloud_context["question"]) + len(cloud_context["student_answer"]),
                PAPER_AUTHORITY_DRAFT_VERSION, context_manifest, "medium",
                "configured_primary", PAPER_AUTHORITY_CLOUD_POLICY_VERSION, None,
            )

            def authorize_dispatch() -> None:
                self._authorize_dispatch(row["obligation_id"], owner_token, row)

            request = ModelRequest(
                request_id=f"paper-authority:{row['obligation_id']}:{attempt}",
                child_id=row["child_id"], session_id=row["session_id"],
                request_type="paper_scoring_authority_draft", risk_level="medium",
                context_manifest=context_manifest,
                provider_preference="configured_primary",
                policy_version=PAPER_AUTHORITY_CLOUD_POLICY_VERSION,
                prompt_version=PAPER_AUTHORITY_DRAFT_VERSION,
                knowledge_version=None, output_type="knowledge_structuring",
                evidence_refs=(row["obligation_id"],),
                pre_dispatch=authorize_dispatch,
                messages=(
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": json.dumps(cloud_context, ensure_ascii=False)},
                ),
            )
            authorize_dispatch()
            result = await self.gateway.complete(request)
            draft = parse_authority_draft(result.content)
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not self._owns(connection, row["obligation_id"], owner_token):
                    connection.rollback()
                    return None
                if not bool(self.cloud_text_allowed()):
                    raise PaperScoringAuthorityDraftError('CLOUD_TEXT_DISABLED')
                self._assert_original(connection, row)
                record_attempt(
                    connection, request_id=row["request_id"], child_id=row["child_id"],
                    status="rubric_candidate",
                    reason="model produced a structurally valid search and rubric draft",
                    evidence={
                        **draft,
                        "provider": result.provider,
                        "model": result.model,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "contains_question_or_answer_text": False,
                        "original_response_version": row['original_response']['source_version'],
                    },
                    client_action_id=(
                        f"paper-authority-draft:{row['obligation_id']}:{attempt}"
                    ),
                )
                connection.execute(
                    """UPDATE paper_scoring_supply_work
                       SET status='completed',owner_token=NULL,claimed_at=NULL,error_code=NULL,
                           provider=?,model=?,prompt_tokens=?,completion_tokens=?,updated_at=?
                       WHERE obligation_id=?""",
                    (result.provider, result.model, result.prompt_tokens,
                     result.completion_tokens, utc_now(), row["obligation_id"]),
                )
                connection.commit()
            if call_id:
                try:
                    self.repository.finish_model_call(
                        call_id, "completed", result.latency_ms, len(result.content),
                        result.prompt_tokens, result.completion_tokens,
                        provider=result.provider, model=result.model,
                        verification_status="partially_verified", safety_status="passed",
                        fallback_used=result.fallback_used,
                    )
                except Exception:
                    # The accepted draft is already durable. Startup recovery
                    # will close any model-call audit row left in `started`.
                    pass
            return self.work(row["obligation_id"])
        except Exception as error:
            code = str(error) if isinstance(error, PaperScoringAuthorityDraftError) else type(error).__name__
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._owns(connection, row["obligation_id"], owner_token):
                    blocked = (attempt >= PAPER_AUTHORITY_MAX_ATTEMPTS
                               or code in {'PAPER_AUTHORITY_ORIGINAL_CHANGED', 'PAPER_AUTHORITY_ORIGINAL_UNAVAILABLE'})
                    record_attempt(
                        connection, request_id=row["request_id"], child_id=row["child_id"],
                        status="blocked_source" if blocked else "failed_retryable",
                        reason=("original paper response is no longer current" if code.startswith('PAPER_AUTHORITY_ORIGINAL_') else
                                "authority draft retry budget exhausted" if blocked
                                else "authority draft attempt failed and will retry"),
                        evidence={"error_code": code[:80], "attempt": attempt,
                                  "contains_question_or_answer_text": False},
                        client_action_id=(
                            f"paper-authority-draft-error:{row['obligation_id']}:{attempt}"
                        ),
                    )
                    connection.execute(
                        """UPDATE paper_scoring_supply_work
                           SET status=?,owner_token=NULL,claimed_at=NULL,error_code=?,updated_at=?
                           WHERE obligation_id=?""",
                        ("blocked" if blocked else "pending", code[:80], utc_now(),
                         row["obligation_id"]),
                    )
                    connection.commit()
                else:
                    connection.rollback()
            if call_id:
                try:
                    self.repository.finish_model_call(
                        call_id, "failed", None, None, error_code=code[:80],
                        verification_status="rejected", safety_status="blocked",
                    )
                except Exception:
                    pass
            return self.work(row["obligation_id"])

    async def process_batch(self, limit: int = 2) -> list[dict[str, Any]]:
        import asyncio

        outcomes = await asyncio.gather(
            *(self.process_next() for _ in range(max(1, min(int(limit), 4)))),
            return_exceptions=True,
        )
        return [value for value in outcomes if isinstance(value, dict)]

    def work(self, obligation_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT obligation_id,child_id,status,attempt_count,error_code,
                          provider,model,prompt_tokens,completion_tokens,updated_at
                   FROM paper_scoring_supply_work WHERE obligation_id=?""",
                (obligation_id,),
            ).fetchone()
        if not row:
            raise ValueError("paper scoring supply work not found")
        return {**dict(row), "local_only": True, "cloud_transmission_allowed": False}
