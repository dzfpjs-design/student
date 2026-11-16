from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Callable
from uuid import uuid4

from .analysis_candidate import AnalysisCandidateService
from .child_model import ChildModelService
from .cloud_privacy import sanitize_cloud_text
from .database import Database
from .model_gateway import ModelRequest
from .repository import StudyRepository, utc_now


PROMPT_VERSION = "child_daily_analysis_v1"
POLICY_VERSION = "minimal_child_learning_evidence_v1"
RESPONSE_VERSION = "child_daily_analysis_response_v1"
MAX_ATTEMPTS = 3
MODEL_FIELDS = {
    "task_type", "scope", "claims", "confidence", "uncertainty_reasons",
    "valid_days", "verification_actions", "proposed_actions", "expected_outcomes",
    "stop_conditions", "rollback_action",
}
MODEL_ACTION_TYPES = {
    "verify_hypothesis", "schedule_check", "adjust_pacing_candidate",
    "suggest_plan_item", "parent_communication", "teaching_candidate",
    "clarify_child_context", "leave_unchanged",
}


class ChildAnalysisReviewError(ValueError):
    pass


class InvalidModelProtocol(ChildAnalysisReviewError):
    pass


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError) as error:
        raise InvalidModelProtocol("response_not_json_object") from error
    if not isinstance(value, dict) or set(value) != MODEL_FIELDS:
        raise InvalidModelProtocol("invalid_top_level_fields")
    if value.get("task_type") != "daily_learning_review":
        raise InvalidModelProtocol("invalid_task_type")
    scope = value.get("scope")
    if not isinstance(scope, dict) or not scope or any(
        not isinstance(key, str) or not isinstance(item, (str, int, float, bool, type(None)))
        for key, item in scope.items()
    ):
        raise InvalidModelProtocol("invalid_scope")
    claims = value.get("claims")
    if not isinstance(claims, list) or not 1 <= len(claims) <= 12:
        raise InvalidModelProtocol("invalid_claims")
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {
            "claim", "evidence_refs", "counterevidence_refs",
        }:
            raise InvalidModelProtocol("invalid_claim")
        if not isinstance(claim["claim"], str) or not claim["claim"].strip():
            raise InvalidModelProtocol("invalid_claim")
        for field in ("evidence_refs", "counterevidence_refs"):
            refs = claim[field]
            if not isinstance(refs, list) or any(ref != "snapshot" for ref in refs):
                raise InvalidModelProtocol("invalid_evidence_alias")
        if not claim["evidence_refs"]:
            raise InvalidModelProtocol("missing_claim_evidence")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise InvalidModelProtocol("invalid_confidence")
    valid_days = value.get("valid_days")
    if isinstance(valid_days, bool) or not isinstance(valid_days, int) or not 1 <= valid_days <= 30:
        raise InvalidModelProtocol("invalid_valid_days")
    for field in (
        "uncertainty_reasons", "verification_actions", "expected_outcomes", "stop_conditions",
    ):
        items = value.get(field)
        if not isinstance(items, list) or not 1 <= len(items) <= 12 or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise InvalidModelProtocol(f"invalid_{field}")
    actions = value.get("proposed_actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
        raise InvalidModelProtocol("invalid_proposed_actions")
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"action_type", "rationale"}:
            raise InvalidModelProtocol("invalid_proposed_action")
        if action["action_type"] not in MODEL_ACTION_TYPES:
            raise InvalidModelProtocol("invalid_proposed_action")
        if not isinstance(action["rationale"], str) or not action["rationale"].strip():
            raise InvalidModelProtocol("invalid_proposed_action")
    if not isinstance(value.get("rollback_action"), str) or not value["rollback_action"].strip():
        raise InvalidModelProtocol("invalid_rollback_action")
    return value


class ChildAnalysisReviewService:
    """Turns completed-session evidence into locally validated, non-authoritative hypotheses."""

    def __init__(
        self,
        database: Database,
        gateway: Any,
        repository: StudyRepository | None = None,
        cloud_text_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self.database = database
        self.gateway = gateway
        self.repository = repository or StudyRepository(database)
        self.cloud_text_allowed = cloud_text_allowed or (lambda: False)

    def ensure_schema(self) -> None:
        ChildModelService(self.database).ensure_schema()
        AnalysisCandidateService(self.database).ensure_schema()
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS child_analysis_review_job (
                  job_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  source_session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  analysis_date TEXT NOT NULL,
                  source_snapshot_id TEXT NOT NULL REFERENCES child_model_snapshot(snapshot_id),
                  evidence_fingerprint TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','blocked')),
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  owner_token TEXT,
                  claimed_at TEXT,
                  candidate_id TEXT REFERENCES analysis_candidate(candidate_id),
                  provider TEXT,
                  model TEXT,
                  prompt_tokens INTEGER,
                  completion_tokens INTEGER,
                  error_code TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(source_session_id, evidence_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_child_analysis_review_pending
                ON child_analysis_review_job(status, updated_at, job_id);
                """
            )
            connection.commit()

    def enqueue_for_session(self, session_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self.database.connect() as connection:
            session = connection.execute(
                """SELECT session_id,child_id,status,COALESCE(ended_at,last_checkpoint_at) AS analyzed_at
                   FROM daily_session WHERE session_id=?""",
                (session_id,),
            ).fetchone()
        if not session:
            raise ChildAnalysisReviewError("unknown_session")
        if session["status"] != "completed":
            raise ChildAnalysisReviewError("session_not_completed")
        snapshot = ChildModelService(self.database).snapshot(session["child_id"])
        analysis_date = str(session["analyzed_at"] or utc_now())[:10]
        now = utc_now()
        job_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO child_analysis_review_job(
                     job_id,child_id,source_session_id,analysis_date,source_snapshot_id,
                     evidence_fingerprint,status,attempt_count,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'pending',0,?,?)""",
                (
                    job_id, session["child_id"], session_id, analysis_date,
                    snapshot["snapshot_id"], snapshot["evidence_fingerprint"], now, now,
                ),
            )
            row = connection.execute(
                """SELECT job_id FROM child_analysis_review_job
                   WHERE source_session_id=? AND evidence_fingerprint=?""",
                (session_id, snapshot["evidence_fingerprint"]),
            ).fetchone()
            connection.commit()
        return self.job(row["job_id"])

    def recover(self) -> int:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                """UPDATE child_analysis_review_job
                   SET status='pending',owner_token=NULL,claimed_at=NULL,
                       error_code='APP_RESTART',updated_at=?
                   WHERE status='processing'""",
                (utc_now(),),
            ).rowcount
            connection.commit()
        return count

    def _claim(self) -> tuple[Any, str] | None:
        if not self.gateway or not getattr(self.gateway, "configured", False):
            return None
        if not bool(self.cloud_text_allowed()):
            return None
        self.ensure_schema()
        owner_token = str(uuid4())
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT job.*,child.nickname,child.grade,child.curriculum_region
                   FROM child_analysis_review_job AS job
                   JOIN child_profile AS child USING(child_id)
                   WHERE job.status='pending' AND job.attempt_count < ?
                   ORDER BY job.updated_at,job.job_id LIMIT 1""",
                (MAX_ATTEMPTS,),
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            current = connection.execute(
                """SELECT snapshot_id,evidence_fingerprint FROM child_model_snapshot
                   WHERE child_id=? AND is_current=1""",
                (row["child_id"],),
            ).fetchone()
            if current and (
                current["snapshot_id"] != row["source_snapshot_id"]
                or current["evidence_fingerprint"] != row["evidence_fingerprint"]
            ):
                connection.execute(
                    """UPDATE child_analysis_review_job
                       SET source_snapshot_id=?,evidence_fingerprint=?,updated_at=?
                       WHERE job_id=?""",
                    (
                        current["snapshot_id"], current["evidence_fingerprint"], now,
                        row["job_id"],
                    ),
                )
            changed = connection.execute(
                """UPDATE child_analysis_review_job
                   SET status='processing',attempt_count=attempt_count+1,
                       owner_token=?,claimed_at=?,error_code=NULL,updated_at=?
                   WHERE job_id=? AND status='pending'""",
                (owner_token, now, now, row["job_id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                """SELECT job.*,child.nickname,child.grade,child.curriculum_region
                   FROM child_analysis_review_job AS job
                   JOIN child_profile AS child USING(child_id) WHERE job.job_id=?""",
                (row["job_id"],),
            ).fetchone()
            connection.commit()
        return claimed, owner_token

    def _owns(self, connection: Any, job_id: str, owner_token: str) -> bool:
        row = connection.execute(
            "SELECT status,owner_token FROM child_analysis_review_job WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return bool(row and row["status"] == "processing" and row["owner_token"] == owner_token)

    def _authorize_dispatch(self, row: Any, owner_token: str) -> None:
        if not bool(self.cloud_text_allowed()):
            raise ChildAnalysisReviewError("cloud_text_disabled")
        with self.database.connect() as connection:
            if not self._owns(connection, row["job_id"], owner_token):
                raise ChildAnalysisReviewError("analysis_attempt_changed")
            current = connection.execute(
                """SELECT snapshot_id,evidence_fingerprint FROM child_model_snapshot
                   WHERE child_id=? AND is_current=1""",
                (row["child_id"],),
            ).fetchone()
            if not current or current["snapshot_id"] != row["source_snapshot_id"] or (
                current["evidence_fingerprint"] != row["evidence_fingerprint"]
            ):
                raise ChildAnalysisReviewError("source_snapshot_stale")

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是家庭学习伙伴的证据审阅器。只能基于输入证据提出待验证假设与下一步验证动作；"
            "不得宣称掌握、正确、习惯已形成、人格或医学诊断，不得直接改变计划或家庭规则。"
            "只返回一个JSON对象，字段必须且只能为task_type、scope、claims、confidence、"
            "uncertainty_reasons、valid_days、verification_actions、proposed_actions、"
            "expected_outcomes、stop_conditions、rollback_action。task_type必须为daily_learning_review；"
            "claims每项只能含claim、evidence_refs、counterevidence_refs，证据引用只允许snapshot；"
            "proposed_actions每项只能含action_type、rationale，action_type只允许verify_hypothesis、"
            "schedule_check、adjust_pacing_candidate、suggest_plan_item、parent_communication、"
            "teaching_candidate、clarify_child_context、leave_unchanged；valid_days为1到30的整数。"
        )

    @staticmethod
    def _safe_text(value: Any, names: tuple[str, ...], limit: int = 500) -> tuple[str, int]:
        sanitized = sanitize_cloud_text(str(value or "")[:limit], names)
        return sanitized.text, sanitized.redaction_count

    def _cloud_context(self, row: Any) -> tuple[dict[str, Any], int]:
        names = (row["nickname"],) if row["nickname"] else ()
        with self.database.connect() as connection:
            snapshot = connection.execute(
                "SELECT payload_json FROM child_model_snapshot WHERE snapshot_id=?",
                (row["source_snapshot_id"],),
            ).fetchone()
            recalls = connection.execute(
                """SELECT subject,summary,confidence FROM classroom_recall
                   WHERE session_id=? AND confirmation_status IN ('confirmed','child_confirmed')
                   ORDER BY created_at LIMIT 12""",
                (row["source_session_id"],),
            ).fetchall()
            homework = connection.execute(
                """SELECT subject,task_type,title,status,expected_minutes FROM homework_item
                   WHERE session_id=? ORDER BY created_at LIMIT 20""",
                (row["source_session_id"],),
            ).fetchall()
            reflection = connection.execute(
                """SELECT learned_text,difficult_text,feeling,next_action,skipped
                   FROM daily_reflection WHERE session_id=?""",
                (row["source_session_id"],),
            ).fetchone()
            plan_items = connection.execute(
                """SELECT item.subject,item.title,item.status,item.expected_minutes,
                          item.actual_minutes,item.source_type,item.completion_evidence_type,
                          item.completion_note
                   FROM plan_item AS item JOIN daily_plan AS plan USING(plan_id)
                   WHERE plan.session_id=? ORDER BY item.position LIMIT 30""",
                (row["source_session_id"],),
            ).fetchall()
            mistakes = connection.execute(
                """SELECT subject,title,prompt_summary,status,confidence
                   FROM mistake_case WHERE child_id=? AND substr(created_at,1,10)=?
                   ORDER BY created_at LIMIT 20""",
                (row["child_id"], row["analysis_date"]),
            ).fetchall()
            tutor_feedback = connection.execute(
                """SELECT response_text,outcome,error_hypothesis,confidence,evidence_basis
                   FROM tutor_feedback WHERE child_id=? AND session_id=?
                   ORDER BY created_at LIMIT 20""",
                (row["child_id"], row["source_session_id"]),
            ).fetchall()
            interventions = connection.execute(
                """SELECT candidate_state,feedback,feedback_note,outcome_json,
                          intervention_type,status
                   FROM intervention WHERE child_id=? AND session_id=?
                   ORDER BY decided_at LIMIT 20""",
                (row["child_id"], row["source_session_id"]),
            ).fetchall()
        payload = json.loads(snapshot["payload_json"])
        redactions = 0

        def text(value: Any, limit: int = 500) -> str:
            nonlocal redactions
            result, count = self._safe_text(value, names, limit)
            redactions += count
            return result

        priorities = []
        for item in payload.get("learning", {}).get("reliable_feedback", {}).get("nodes", [])[:8]:
            priorities.append({
                "subject": text(item.get("subject"), 40),
                "title": text(item.get("title"), 120),
                "status": text(item.get("status"), 40),
                "next_action": text(item.get("next_action"), 240),
                "reason": text(item.get("reason"), 240),
            })
        hypotheses = []
        for item in payload.get("open_hypotheses", [])[:8]:
            hypotheses.append({
                "claim": text(item.get("claim"), 300),
                "confidence": text(item.get("confidence"), 40),
                "verification_actions": [text(action, 240) for action in item.get("verification_actions", [])[:3]],
            })
        recall_items = [{
            "subject": text(item["subject"], 40),
            "summary": text(item["summary"], 400),
            "confidence": float(item["confidence"]),
        } for item in recalls]
        homework_items = [{
            "subject": text(item["subject"], 40),
            "task_type": text(item["task_type"], 40),
            "title": text(item["title"], 240),
            "status": text(item["status"], 40),
            "expected_minutes": int(item["expected_minutes"]),
        } for item in homework]
        reflection_item = None
        if reflection:
            reflection_item = {
                "learned": text(reflection["learned_text"], 500),
                "difficult": text(reflection["difficult_text"], 500),
                "feeling": text(reflection["feeling"], 80),
                "next_action": text(reflection["next_action"], 300),
                "skipped": bool(reflection["skipped"]),
            }
        plan_execution = [{
            "subject": text(item["subject"], 40),
            "title": text(item["title"], 240),
            "status": text(item["status"], 40),
            "expected_minutes": int(item["expected_minutes"]),
            "actual_minutes": int(item["actual_minutes"]) if item["actual_minutes"] is not None else None,
            "source_type": text(item["source_type"], 60),
            "completion_evidence_type": text(item["completion_evidence_type"], 60),
            "completion_note": text(item["completion_note"], 300),
        } for item in plan_items]
        mistake_items = [{
            "subject": text(item["subject"], 40),
            "title": text(item["title"], 160),
            "prompt_summary": text(item["prompt_summary"], 500),
            "status": text(item["status"], 50),
            "confidence": float(item["confidence"]),
        } for item in mistakes]
        feedback_items = [{
            "child_response": text(item["response_text"], 500),
            "outcome": text(item["outcome"], 80),
            "error_hypothesis": text(item["error_hypothesis"], 300),
            "confidence": float(item["confidence"]),
            "evidence_basis": text(item["evidence_basis"], 300),
        } for item in tutor_feedback]
        behavior_items = []
        for item in interventions:
            try:
                outcome = json.loads(item["outcome_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                outcome = {"status": "unavailable"}
            behavior_items.append({
                "candidate_state": text(item["candidate_state"], 80),
                "feedback": text(item["feedback"], 120),
                "feedback_note": text(item["feedback_note"], 300),
                "outcome": {
                    key: text(value, 160) if isinstance(value, str) else value
                    for key, value in outcome.items()
                    if key in {"status", "effect", "stress", "helpful", "reason"}
                    and isinstance(value, (str, int, float, bool, type(None)))
                },
                "intervention_type": text(item["intervention_type"], 80),
                "status": text(item["status"], 50),
            })
        context = {
            "grade": text(row["grade"], 40),
            "curriculum_region": text(row["curriculum_region"], 80),
            "period": row["analysis_date"],
            "evidence_aliases": {"snapshot": "current_child_state_snapshot"},
            "snapshot_summary": {
                "daily_state": payload.get("state_domains", {}).get("daily_state", {}),
                "pending_plan_items": int(payload.get("plan_context", {}).get("pending_plan_items") or 0),
                "learning_priorities": priorities,
                "open_hypotheses": hypotheses,
            },
            "classroom_recall": recall_items,
            "homework": homework_items,
            "plan_execution": plan_execution,
            "mistakes": mistake_items,
            "tutor_feedback": feedback_items,
            "behavior_interventions": behavior_items,
            "reflection": reflection_item,
            "authority_boundary": {
                "candidate_only": True,
                "may_change_mastery": False,
                "may_change_habit_status": False,
                "may_complete_plan": False,
            },
        }
        return context, redactions

    async def process_next(self) -> dict[str, Any] | None:
        claimed = self._claim()
        if not claimed:
            return None
        row, owner_token = claimed
        attempt = int(row["attempt_count"])
        call_id: str | None = None
        try:
            context, redaction_count = self._cloud_context(row)
            provider = getattr(getattr(self.gateway, "settings", None), "model_provider", "configured")
            model = getattr(getattr(self.gateway, "settings", None), "model_name", "configured")
            manifest = {
                "raw_media": False,
                "child_identity": False,
                "source_snapshot_id": row["source_snapshot_id"],
                "redaction_count": redaction_count,
                "authority_granted": False,
                "candidate_contract": "analysis_candidate_v1",
            }
            call_id = self.repository.start_model_call(
                row["source_session_id"], row["child_id"], "child_daily_analysis",
                provider, model, len(json.dumps(context, ensure_ascii=False)), PROMPT_VERSION,
                manifest, "medium", "configured_primary", POLICY_VERSION,
                row["evidence_fingerprint"],
            )

            def authorize_dispatch() -> None:
                self._authorize_dispatch(row, owner_token)

            request = ModelRequest(
                request_id=f"child-analysis:{row['job_id']}:{attempt}",
                child_id=row["child_id"], session_id=row["source_session_id"],
                request_type="child_daily_analysis", risk_level="medium",
                context_manifest=manifest, provider_preference="configured_primary",
                policy_version=POLICY_VERSION, prompt_version=PROMPT_VERSION,
                knowledge_version=row["evidence_fingerprint"],
                output_type="knowledge_structuring",
                evidence_refs=(row["source_snapshot_id"],),
                pre_dispatch=authorize_dispatch,
                messages=(
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ),
            )
            authorize_dispatch()
            result = await self.gateway.complete(request)
            draft = _parse_json_object(result.content)
            valid_until = (datetime.now(timezone.utc) + timedelta(days=draft.pop("valid_days"))).isoformat()
            candidate_id = str(uuid4())
            for claim in draft["claims"]:
                claim["evidence_refs"] = [
                    {"type": "child_model_snapshot", "id": row["source_snapshot_id"]}
                    for _ in claim["evidence_refs"]
                ]
                claim["counterevidence_refs"] = [
                    {"type": "child_model_snapshot", "id": row["source_snapshot_id"]}
                    for _ in claim["counterevidence_refs"]
                ]
            candidate = {
                **draft,
                "candidate_id": candidate_id,
                "child_id": row["child_id"],
                "valid_until": valid_until,
                "model_provider": result.provider,
                "model_name": result.model,
                "prompt_version": PROMPT_VERSION,
                "response_version": RESPONSE_VERSION,
                "source_snapshot_id": row["source_snapshot_id"],
                "evidence_fingerprint": row["evidence_fingerprint"],
            }
            AnalysisCandidateService(self.database).record_and_validate(
                row["child_id"], candidate,
                f"child-analysis-candidate:{row['job_id']}:{attempt}",
            )
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not self._owns(connection, row["job_id"], owner_token):
                    connection.rollback()
                    return None
                connection.execute(
                    """UPDATE child_analysis_review_job
                       SET status='completed',owner_token=NULL,claimed_at=NULL,candidate_id=?,
                           provider=?,model=?,prompt_tokens=?,completion_tokens=?,error_code=NULL,
                           updated_at=? WHERE job_id=?""",
                    (
                        candidate_id, result.provider, result.model, result.prompt_tokens,
                        result.completion_tokens, utc_now(), row["job_id"],
                    ),
                )
                connection.commit()
            if call_id:
                self.repository.finish_model_call(
                    call_id, "completed", result.latency_ms, len(result.content),
                    result.prompt_tokens, result.completion_tokens,
                    provider=result.provider, model=result.model,
                    verification_status="partially_verified", safety_status="passed",
                    fallback_used=result.fallback_used,
                )
            ChildModelService(self.database).snapshot(row["child_id"])
            return self.job(row["job_id"])
        except Exception as error:
            code = "invalid_model_protocol" if isinstance(error, InvalidModelProtocol) else type(error).__name__
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._owns(connection, row["job_id"], owner_token):
                    connection.execute(
                        """UPDATE child_analysis_review_job
                           SET status=?,owner_token=NULL,claimed_at=NULL,error_code=?,updated_at=?
                           WHERE job_id=?""",
                        (
                            "blocked" if attempt >= MAX_ATTEMPTS else "pending",
                            code[:80], utc_now(), row["job_id"],
                        ),
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
            return self.job(row["job_id"])

    async def process_batch(self, limit: int = 2) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for _ in range(max(1, min(int(limit), 4))):
            result = await self.process_next()
            if result is None:
                break
            results.append(result)
        return results

    def job(self, job_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT job_id,child_id,source_session_id,analysis_date,source_snapshot_id,
                          evidence_fingerprint,status,attempt_count,candidate_id,provider,model,
                          prompt_tokens,completion_tokens,error_code,created_at,updated_at
                   FROM child_analysis_review_job WHERE job_id=?""",
                (job_id,),
            ).fetchone()
        if not row:
            raise KeyError(job_id)
        return {**dict(row), "local_only": True, "cloud_transmission_allowed": False}
