from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from .database import Database


CONTRACT_VERSION = "analysis_candidate_v1"
TASK_TYPES = {
    "daily_learning_review", "weekly_strategy_review", "mistake_cause_hypothesis",
    "plan_explanation", "teaching_candidate", "parent_communication_candidate",
    "habit_opportunity_review", "classroom_recall_review",
}
ACTION_TYPES = {
    "verify_hypothesis", "schedule_check", "adjust_pacing_candidate",
    "suggest_plan_item", "parent_communication", "teaching_candidate",
    "clarify_child_context", "leave_unchanged",
}
FORBIDDEN_KEYS = {
    "mastery_probability", "mastery_status", "habit_status", "plan_status",
    "family_rule", "correctness", "score", "mark_mastered", "complete_plan",
    "update_mastery", "diagnosis_confirmed", "authority_granted",
}
TOP_LEVEL_FIELDS = {
    "candidate_id", "child_id", "task_type", "scope", "claims", "confidence",
    "uncertainty_reasons", "valid_until", "verification_actions", "proposed_actions",
    "expected_outcomes", "stop_conditions", "rollback_action", "model_provider",
    "model_name", "prompt_version", "response_version", "source_snapshot_id",
    "evidence_fingerprint",
}


class AnalysisCandidateError(ValueError):
    pass


class AnalysisCandidateConflictError(AnalysisCandidateError):
    pass


class AnalysisCandidateRejectedError(AnalysisCandidateError):
    def __init__(self, reason_codes: list[str]):
        self.reason_codes = tuple(sorted(set(reason_codes)))
        super().__init__("Analysis candidate rejected: " + ", ".join(self.reason_codes))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AnalysisCandidateService:
    """Quarantines cloud analysis until local evidence and authority checks pass."""

    def __init__(self, database: Database):
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_candidate_ingest_attempt (
                  attempt_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  client_action_id TEXT NOT NULL UNIQUE,
                  request_hash TEXT NOT NULL,
                  candidate_id TEXT,
                  model_provider TEXT,
                  model_name TEXT,
                  status TEXT NOT NULL CHECK(status IN ('accepted','rejected')),
                  reason_codes_json TEXT NOT NULL,
                  payload_json TEXT,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_candidate (
                  candidate_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  task_type TEXT NOT NULL,
                  scope_json TEXT NOT NULL,
                  claims_json TEXT NOT NULL,
                  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                  uncertainty_reasons_json TEXT NOT NULL,
                  valid_until TEXT NOT NULL,
                  verification_actions_json TEXT NOT NULL,
                  proposed_actions_json TEXT NOT NULL,
                  expected_outcomes_json TEXT NOT NULL,
                  stop_conditions_json TEXT NOT NULL,
                  rollback_action TEXT NOT NULL,
                  model_provider TEXT NOT NULL,
                  model_name TEXT NOT NULL,
                  prompt_version TEXT NOT NULL,
                  response_version TEXT NOT NULL,
                  contract_version TEXT NOT NULL,
                  source_snapshot_id TEXT NOT NULL REFERENCES child_model_snapshot(snapshot_id),
                  evidence_fingerprint TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_candidate_child_expiry
                ON analysis_candidate(child_id, valid_until, created_at DESC);
                CREATE TABLE IF NOT EXISTS analysis_candidate_event (
                  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT NOT NULL UNIQUE,
                  candidate_id TEXT NOT NULL REFERENCES analysis_candidate(candidate_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  event_type TEXT NOT NULL CHECK(event_type IN ('validated','rejected','withdrawn')),
                  reason_codes_json TEXT NOT NULL,
                  occurred_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_candidate_event_current
                ON analysis_candidate_event(candidate_id, event_seq DESC);
                CREATE TRIGGER IF NOT EXISTS trg_analysis_candidate_no_update
                BEFORE UPDATE ON analysis_candidate BEGIN
                  SELECT RAISE(ABORT, 'analysis_candidate is immutable');
                END;
                DROP TRIGGER IF EXISTS trg_analysis_candidate_no_direct_delete;
                CREATE TRIGGER trg_analysis_candidate_no_direct_delete
                BEFORE DELETE ON analysis_candidate
                WHEN EXISTS(SELECT 1 FROM child_profile WHERE child_id=OLD.child_id)
                  AND NOT EXISTS(
                    SELECT 1 FROM deletion_job
                    WHERE child_id=OLD.child_id AND target_type='child_scope'
                      AND target_id=OLD.child_id AND scope IN ('learning','all_history')
                      AND status IN ('scanning','deleting','verifying')
                  )
                BEGIN
                  SELECT RAISE(ABORT, 'analysis_candidate requires child lifecycle deletion');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_analysis_candidate_event_no_update
                BEFORE UPDATE ON analysis_candidate_event BEGIN
                  SELECT RAISE(ABORT, 'analysis_candidate_event is immutable');
                END;
                """
            )
            connection.commit()
    def record_and_validate(
        self, child_id: str, payload: dict[str, Any], client_action_id: str,
        *, now: datetime | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        observed_at = (now or _now()).astimezone(timezone.utc)
        try:
            request_json = _canonical(payload)
        except (TypeError, ValueError):
            request_json = "<unserializable>"
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            prior = connection.execute(
                "SELECT * FROM analysis_candidate_ingest_attempt WHERE client_action_id=?",
                (client_action_id,),
            ).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise AnalysisCandidateConflictError(
                        "The analysis action was already used for different content."
                    )
                if prior["status"] == "rejected":
                    raise AnalysisCandidateRejectedError(json.loads(prior["reason_codes_json"]))
                return self._get(connection, child_id, prior["candidate_id"], observed_at)

            reasons = self._validate(connection, child_id, payload, observed_at)
            if reasons:
                self._record_attempt(
                    connection, child_id, payload, client_action_id, request_hash,
                    "rejected", reasons, observed_at, candidate_id=None,
                )
                self._audit(
                    connection, child_id, "analysis_candidate_rejected",
                    {"reason_codes": sorted(set(reasons)), "request_hash": request_hash,
                     "contract_version": CONTRACT_VERSION}, observed_at,
                )
                connection.commit()
                raise AnalysisCandidateRejectedError(reasons)

            candidate_id = payload["candidate_id"]
            if connection.execute(
                "SELECT 1 FROM analysis_candidate WHERE candidate_id=?", (candidate_id,)
            ).fetchone():
                self._record_attempt(
                    connection, child_id, payload, client_action_id, request_hash,
                    "rejected", ["candidate_id_conflict"], observed_at, candidate_id=None,
                )
                connection.commit()
                raise AnalysisCandidateRejectedError(["candidate_id_conflict"])
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO analysis_candidate(
                     candidate_id,child_id,task_type,scope_json,claims_json,confidence,
                     uncertainty_reasons_json,valid_until,verification_actions_json,
                     proposed_actions_json,expected_outcomes_json,stop_conditions_json,
                     rollback_action,model_provider,model_name,prompt_version,response_version,
                     contract_version,source_snapshot_id,evidence_fingerprint,request_hash,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_id, child_id, payload["task_type"], _canonical(payload["scope"]),
                    _canonical(payload["claims"]), float(payload["confidence"]),
                    _canonical(payload["uncertainty_reasons"]), payload["valid_until"],
                    _canonical(payload["verification_actions"]),
                    _canonical(payload["proposed_actions"]),
                    _canonical(payload["expected_outcomes"]),
                    _canonical(payload["stop_conditions"]), payload["rollback_action"].strip(),
                    payload["model_provider"], payload["model_name"], payload["prompt_version"],
                    payload["response_version"], CONTRACT_VERSION, payload["source_snapshot_id"],
                    payload["evidence_fingerprint"], request_hash, observed_at.isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO analysis_candidate_event(
                     event_id,candidate_id,child_id,event_type,reason_codes_json,
                     occurred_at,client_action_id
                   ) VALUES(?,?,?,'validated','[]',?,?)""",
                (str(uuid4()), candidate_id, child_id, observed_at.isoformat(), client_action_id),
            )
            self._record_attempt(
                connection, child_id, payload, client_action_id, request_hash,
                "accepted", [], observed_at, candidate_id=candidate_id,
            )
            self._audit(
                connection, child_id, "analysis_candidate_validated",
                {"candidate_id": candidate_id, "task_type": payload["task_type"],
                 "source_snapshot_id": payload["source_snapshot_id"],
                 "evidence_fingerprint": payload["evidence_fingerprint"],
                 "contract_version": CONTRACT_VERSION}, observed_at,
            )
            connection.commit()
            return self._get(connection, child_id, candidate_id, observed_at)

    def get(
        self, child_id: str, candidate_id: str, *, now: datetime | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self.database.connect() as connection:
            return self._get(connection, child_id, candidate_id, now or _now())

    def withdraw(
        self, child_id: str, candidate_id: str, reason_code: str,
        client_action_id: str, *, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append a local correction event; never rewrite the cloud candidate."""
        self.ensure_schema()
        if (
            not isinstance(reason_code, str) or not reason_code.strip()
            or len(reason_code) > 120
            or not isinstance(client_action_id, str) or not client_action_id.strip()
        ):
            raise AnalysisCandidateError("Invalid candidate withdrawal.")
        observed_at = (now or _now()).astimezone(timezone.utc)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM analysis_candidate_event WHERE client_action_id=?",
                (client_action_id,),
            ).fetchone()
            if prior:
                reasons = json.loads(prior["reason_codes_json"] or "[]")
                if (
                    prior["child_id"] != child_id
                    or prior["candidate_id"] != candidate_id
                    or prior["event_type"] != "withdrawn"
                    or reasons != [reason_code.strip()]
                ):
                    raise AnalysisCandidateConflictError(
                        "The withdrawal action was already used for different content."
                    )
                connection.rollback()
                return self._get(connection, child_id, candidate_id, observed_at)
            candidate = connection.execute(
                "SELECT child_id FROM analysis_candidate WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if not candidate or candidate["child_id"] != child_id:
                raise KeyError(candidate_id)
            connection.execute(
                """INSERT INTO analysis_candidate_event(
                     event_id,candidate_id,child_id,event_type,reason_codes_json,
                     occurred_at,client_action_id
                   ) VALUES(?,?,?,'withdrawn',?,?,?)""",
                (
                    str(uuid4()), candidate_id, child_id,
                    _canonical([reason_code.strip()]), observed_at.isoformat(),
                    client_action_id,
                ),
            )
            self._audit(
                connection, child_id, "analysis_candidate_withdrawn",
                {"candidate_id": candidate_id, "reason_code": reason_code.strip(),
                 "contract_version": CONTRACT_VERSION}, observed_at,
            )
            connection.commit()
            return self._get(connection, child_id, candidate_id, observed_at)

    def _validate(
        self, connection: sqlite3.Connection, child_id: str,
        payload: dict[str, Any], now: datetime,
    ) -> list[str]:
        reasons: list[str] = []
        if not connection.execute(
            "SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)
        ).fetchone():
            reasons.append("unknown_child")
        if not isinstance(payload, dict):
            return reasons + ["invalid_schema"]
        extra = set(payload) - TOP_LEVEL_FIELDS
        missing = TOP_LEVEL_FIELDS - set(payload)
        if extra & FORBIDDEN_KEYS or self._contains_forbidden_key(payload):
            reasons.append("forbidden_field")
        elif extra:
            reasons.append("unknown_field")
        if missing:
            reasons.append("missing_field")
        if reasons and missing:
            return reasons
        if payload.get("child_id") != child_id:
            reasons.append("candidate_child_mismatch")
        if payload.get("task_type") not in TASK_TYPES:
            reasons.append("unsupported_task_type")
        if not isinstance(payload.get("candidate_id"), str) or not payload["candidate_id"].strip():
            reasons.append("invalid_candidate_id")
        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            reasons.append("invalid_confidence")
        valid_until = _parse_time(payload.get("valid_until"))
        if valid_until is None:
            reasons.append("invalid_valid_until")
        elif valid_until <= now:
            reasons.append("expired_candidate")
        for name in (
            "uncertainty_reasons", "verification_actions", "proposed_actions",
            "expected_outcomes", "stop_conditions",
        ):
            if not isinstance(payload.get(name), list):
                reasons.append(f"invalid_{name}")
        for name in (
            "rollback_action", "model_provider", "model_name", "prompt_version",
            "response_version", "source_snapshot_id", "evidence_fingerprint",
        ):
            if not isinstance(payload.get(name), str) or not payload[name].strip():
                reasons.append(f"invalid_{name}")
        snapshot = connection.execute(
            """SELECT child_id,evidence_fingerprint,is_current FROM child_model_snapshot
               WHERE snapshot_id=?""", (payload.get("source_snapshot_id"),)
        ).fetchone()
        if not snapshot:
            reasons.append("unknown_source_snapshot")
        elif snapshot["child_id"] != child_id:
            reasons.append("cross_child_source_snapshot")
        else:
            if snapshot["evidence_fingerprint"] != payload.get("evidence_fingerprint"):
                reasons.append("snapshot_fingerprint_mismatch")
            if not snapshot["is_current"]:
                reasons.append("stale_source_snapshot")

        claims = payload.get("claims")
        if not isinstance(claims, list) or not 1 <= len(claims) <= 20:
            reasons.append("invalid_claims")
        else:
            for claim in claims:
                if not isinstance(claim, dict) or set(claim) != {
                    "claim", "evidence_refs", "counterevidence_refs",
                }:
                    reasons.append("invalid_claim")
                    continue
                if not isinstance(claim["claim"], str) or not claim["claim"].strip():
                    reasons.append("invalid_claim")
                if not isinstance(claim["evidence_refs"], list) or not claim["evidence_refs"]:
                    reasons.append("missing_claim_evidence")
                else:
                    for reference in claim["evidence_refs"] + (
                        claim["counterevidence_refs"]
                        if isinstance(claim["counterevidence_refs"], list) else []
                    ):
                        state = self._evidence_state(connection, child_id, reference)
                        if state:
                            reasons.append(state)
        actions = payload.get("proposed_actions")
        if isinstance(actions, list):
            for action in actions:
                if not isinstance(action, dict) or action.get("action_type") not in ACTION_TYPES:
                    reasons.append("forbidden_action")
        return sorted(set(reasons))

    @staticmethod
    def _contains_forbidden_key(value: Any) -> bool:
        if isinstance(value, dict):
            return bool(set(value) & FORBIDDEN_KEYS) or any(
                AnalysisCandidateService._contains_forbidden_key(item) for item in value.values()
            )
        if isinstance(value, list):
            return any(AnalysisCandidateService._contains_forbidden_key(item) for item in value)
        return False

    @staticmethod
    def _evidence_state(
        connection: sqlite3.Connection, child_id: str, reference: Any,
    ) -> str | None:
        if not isinstance(reference, dict) or set(reference) != {"type", "id"}:
            return "invalid_evidence_ref"
        kind, identifier = reference.get("type"), reference.get("id")
        if not isinstance(identifier, str) or not identifier:
            return "invalid_evidence_ref"
        queries = {
            "child_model_snapshot": ("SELECT child_id FROM child_model_snapshot WHERE snapshot_id=?", (identifier,)),
            "learning_evidence": ("SELECT child_id FROM learning_evidence WHERE evidence_id=?", (identifier,)),
            "mistake_case": ("SELECT child_id FROM mistake_case WHERE mistake_id=?", (identifier,)),
            "daily_session": ("SELECT child_id FROM daily_session WHERE session_id=?", (identifier,)),
            "agent_outcome": ("SELECT child_id FROM agent_outcome WHERE outcome_id=?", (identifier,)),
            "audit_event": ("SELECT child_id FROM audit_event WHERE audit_id=?", (identifier,)),
            "plan_item": (
                """SELECT plan.child_id FROM plan_item AS item
                   JOIN daily_plan AS plan USING(plan_id) WHERE item.plan_item_id=?""", (identifier,),
            ),
            "tutor_feedback": ("SELECT child_id FROM tutor_feedback WHERE feedback_id=?", (identifier,)),
        }
        query = queries.get(kind)
        if query is None:
            return "unsupported_evidence_type"
        try:
            row = connection.execute(query[0], query[1]).fetchone()
        except sqlite3.OperationalError:
            return "unsupported_evidence_type"
        if not row:
            return "unknown_evidence"
        return None if row["child_id"] == child_id else "cross_child_evidence"

    @staticmethod
    def _record_attempt(
        connection: sqlite3.Connection, child_id: str, payload: dict[str, Any],
        client_action_id: str, request_hash: str, status: str, reasons: list[str],
        now: datetime, *, candidate_id: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO analysis_candidate_ingest_attempt(
                 attempt_id,child_id,client_action_id,request_hash,candidate_id,
                 model_provider,model_name,status,reason_codes_json,payload_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid4()), child_id, client_action_id, request_hash, candidate_id,
                payload.get("model_provider") if isinstance(payload, dict) else None,
                payload.get("model_name") if isinstance(payload, dict) else None,
                status, _canonical(sorted(set(reasons))), None, now.isoformat(),
            ),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, child_id: str, action: str,
        summary: dict[str, Any], now: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
               VALUES(?,'system',?,?,?,?)""",
            (str(uuid4()), action, child_id, _canonical(summary), now.isoformat()),
        )

    def _get(
        self, connection: sqlite3.Connection, child_id: str, candidate_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT candidate.*,
                      (SELECT event_type FROM analysis_candidate_event
                       WHERE candidate_id=candidate.candidate_id
                       ORDER BY event_seq DESC LIMIT 1) AS event_type
               FROM analysis_candidate AS candidate
               WHERE candidate_id=? AND child_id=?""",
            (candidate_id, child_id),
        ).fetchone()
        if not row:
            raise KeyError(candidate_id)
        status = row["event_type"]
        expiry = _parse_time(row["valid_until"])
        if status == "validated" and expiry and expiry <= now.astimezone(timezone.utc):
            status = "expired"
        return {
            "candidate_id": row["candidate_id"],
            "child_id": row["child_id"],
            "task_type": row["task_type"],
            "scope": json.loads(row["scope_json"]),
            "claims": json.loads(row["claims_json"]),
            "confidence": row["confidence"],
            "uncertainty_reasons": json.loads(row["uncertainty_reasons_json"]),
            "valid_until": row["valid_until"],
            "verification_actions": json.loads(row["verification_actions_json"]),
            "proposed_actions": json.loads(row["proposed_actions_json"]),
            "expected_outcomes": json.loads(row["expected_outcomes_json"]),
            "stop_conditions": json.loads(row["stop_conditions_json"]),
            "rollback_action": row["rollback_action"],
            "model_provider": row["model_provider"],
            "model_name": row["model_name"],
            "prompt_version": row["prompt_version"],
            "response_version": row["response_version"],
            "contract_version": row["contract_version"],
            "source_snapshot_id": row["source_snapshot_id"],
            "evidence_fingerprint": row["evidence_fingerprint"],
            "status": status,
            "authority_granted": False,
            "cloud_transmission_allowed": False,
            "created_at": row["created_at"],
        }


def delete_child_scope(connection: Any, child_id: str) -> dict[str, int]:
    """Erase quarantined analysis only inside an authorized child deletion transaction."""
    if not connection.in_transaction:
        raise AnalysisCandidateRejectedError("ANALYSIS_CANDIDATE_DELETION_TRANSACTION_REQUIRED")
    authorized = connection.execute(
        """SELECT 1 FROM deletion_job
           WHERE child_id=? AND target_type='child_scope' AND target_id=?
             AND scope IN ('learning','all_history')
             AND status IN ('scanning','deleting','verifying')
           LIMIT 1""",
        (child_id, child_id),
    ).fetchone()
    if not authorized:
        raise AnalysisCandidateRejectedError("ANALYSIS_CANDIDATE_DELETION_NOT_AUTHORIZED")
    counts: dict[str, int] = {}
    tables = {
        row[0] for row in connection.execute(
            """SELECT name FROM sqlite_master WHERE type='table' AND name IN (
                 'child_analysis_review_job','analysis_candidate_event',
                 'analysis_candidate','analysis_candidate_ingest_attempt')"""
        )
    }
    for table in (
        "child_analysis_review_job", "analysis_candidate_event",
        "analysis_candidate", "analysis_candidate_ingest_attempt",
    ):
        if table in tables:
            counts[table] = connection.execute(
                f"DELETE FROM {table} WHERE child_id=?", (child_id,)
            ).rowcount
    return counts
