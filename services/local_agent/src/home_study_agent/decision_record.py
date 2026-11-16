from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4


CONTRACT_VERSION = "decision_record_v1"


class StaleDecisionContextError(ValueError):
    pass


def ensure_schema(connection) -> None:
    ready = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_decision_record'"
    ).fetchone()
    if ready:
        return
    if connection.in_transaction:
        raise RuntimeError("agent_decision_record schema must be initialized before a business transaction")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_decision_record (
          decision_record_id TEXT PRIMARY KEY,
          plan_id TEXT NOT NULL UNIQUE REFERENCES agent_plan(plan_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          agent_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
          snapshot_id TEXT,
          evidence_fingerprint TEXT,
          binding_status TEXT NOT NULL CHECK(binding_status IN ('bound_current','legacy_unbound')),
          strategy_version TEXT NOT NULL,
          strategy_experiment_id TEXT,
          selected_capability TEXT NOT NULL,
          selected_capability_version TEXT NOT NULL,
          candidate_set_json TEXT NOT NULL,
          constraints_json TEXT NOT NULL,
          adoption_reason TEXT NOT NULL,
          rejected_candidates_json TEXT NOT NULL,
          expected_outcomes_json TEXT NOT NULL,
          stop_conditions_json TEXT NOT NULL,
          rollback_action TEXT NOT NULL,
          planner_version TEXT NOT NULL,
          contract_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_decision_child_time
        ON agent_decision_record(child_id, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS trg_agent_decision_record_no_update
        BEFORE UPDATE ON agent_decision_record BEGIN
          SELECT RAISE(ABORT, 'agent_decision_record is immutable');
        END;
        """
    )


def persist(
    connection,
    *,
    plan_id: str,
    child_id: str,
    session_id: str,
    agent_turn_id: str,
    decision,
    context_manifest: dict[str, Any],
    planner_version: str,
    created_at: str,
) -> str:
    ensure_schema(connection)
    snapshot_id = context_manifest.get("child_model_snapshot_id")
    fingerprint = context_manifest.get("child_model_evidence_fingerprint")
    binding_status = "legacy_unbound"
    if snapshot_id or fingerprint:
        snapshot = connection.execute(
            """SELECT child_id,evidence_fingerprint,is_current FROM child_model_snapshot
               WHERE snapshot_id=?""", (snapshot_id,)
        ).fetchone()
        if (
            not snapshot or snapshot["child_id"] != child_id
            or snapshot["evidence_fingerprint"] != fingerprint or not snapshot["is_current"]
        ):
            raise StaleDecisionContextError("CHILD_MODEL_SNAPSHOT_STALE")
        binding_status = "bound_current"
    strategy_version = context_manifest.get("strategy_version") or "no_active_strategy"
    experiment_id = context_manifest.get("strategy_experiment_id")
    candidate = {
        "capability": decision.step.capability.name,
        "capability_version": decision.step.capability.version,
        "execution_location": decision.step.capability.execution_location,
        "risk_level": decision.step.capability.risk_level,
        "confidence": decision.confidence,
    }
    constraints = {
        "requires_child_reply": decision.requires_child_reply,
        "after_execution": decision.after_execution,
        "objective_mode": decision.objective.mode,
        "objective_type": decision.objective.objective_type,
        "risk_level": decision.step.capability.risk_level,
        "snapshot_binding": binding_status,
    }
    expected = list(dict.fromkeys(
        list(decision.step.expected_evidence) + list(decision.objective.success_criteria)
    ))
    stop_conditions = [
        "child_requests_stop_or_correction",
        "snapshot_or_task_binding_changes_before_execution",
        "capability_returns_blocked_conflict_or_unknown",
    ]
    rollback = (
        "Do not execute; rebuild context and replan from the latest local evidence."
        if decision.step.capability.risk_level in {"medium", "high"}
        else "Cancel the pending step and preserve the prior child state."
    )
    record_id = str(uuid4())
    connection.execute(
        """INSERT INTO agent_decision_record(
             decision_record_id,plan_id,child_id,session_id,agent_turn_id,snapshot_id,
             evidence_fingerprint,binding_status,strategy_version,strategy_experiment_id,
             selected_capability,selected_capability_version,candidate_set_json,
             constraints_json,adoption_reason,rejected_candidates_json,
             expected_outcomes_json,stop_conditions_json,rollback_action,
             planner_version,contract_version,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            record_id, plan_id, child_id, session_id, agent_turn_id, snapshot_id, fingerprint,
            binding_status, strategy_version, experiment_id, decision.step.capability.name,
            decision.step.capability.version, json.dumps([candidate], ensure_ascii=False, sort_keys=True),
            json.dumps(constraints, ensure_ascii=False, sort_keys=True), decision.step.reason,
            json.dumps([], ensure_ascii=False), json.dumps(expected, ensure_ascii=False),
            json.dumps(stop_conditions, ensure_ascii=False), rollback, planner_version,
            CONTRACT_VERSION, created_at,
        ),
    )
    return record_id


def current_for_plan(connection, plan_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM agent_decision_record WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if not row:
        return None
    value = dict(row)
    for key in (
        "candidate_set_json", "constraints_json", "rejected_candidates_json",
        "expected_outcomes_json", "stop_conditions_json",
    ):
        value[key.removesuffix("_json")] = json.loads(value.pop(key))
    value["authority"] = "local_runtime_decision_record"
    value["cloud_transmission_allowed"] = False
    return value


def validate_current(connection, plan_id: str) -> dict[str, Any]:
    """Recheck the frozen local state immediately before dispatch."""
    record = connection.execute(
        "SELECT * FROM agent_decision_record WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if not record:
        return {"status": "legacy_unbound", "decision_record_id": None}
    if record["binding_status"] == "legacy_unbound":
        return {"status": "legacy_unbound", "decision_record_id": record["decision_record_id"]}
    snapshot = connection.execute(
        """SELECT child_id,evidence_fingerprint,is_current FROM child_model_snapshot
           WHERE snapshot_id=?""", (record["snapshot_id"],)
    ).fetchone()
    if (
        not snapshot or snapshot["child_id"] != record["child_id"]
        or snapshot["evidence_fingerprint"] != record["evidence_fingerprint"]
        or not snapshot["is_current"]
    ):
        raise StaleDecisionContextError("CHILD_MODEL_SNAPSHOT_STALE")
    return {"status": "bound_current", "decision_record_id": record["decision_record_id"],
            "snapshot_id": record["snapshot_id"]}
