from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4


CONTRACT_VERSION = "outcome_attribution_v1"
TASK_BINDING_KEYS = (
    "plan_item_id", "homework_id", "knowledge_node_id", "diagnostic_item_id",
    "question_identity", "item_version", "prompt_id", "mistake_id",
    "source_evidence_id", "evidence_id",
)


class OutcomeAttributionError(ValueError):
    pass


def ensure_schema(connection: Any) -> None:
    from .decision_record import ensure_schema as ensure_decision_schema

    ensure_decision_schema(connection)
    ready = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_outcome_attribution'"
    ).fetchone()
    if ready:
        return
    if connection.in_transaction:
        raise RuntimeError("agent_outcome_attribution schema must be initialized before a business transaction")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_outcome_attribution (
          attribution_id TEXT PRIMARY KEY,
          outcome_id TEXT NOT NULL UNIQUE REFERENCES agent_outcome(outcome_id) ON DELETE CASCADE,
          objective_id TEXT NOT NULL REFERENCES agent_objective(objective_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          agent_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
          plan_id TEXT NOT NULL REFERENCES agent_plan(plan_id) ON DELETE CASCADE,
          step_id TEXT NOT NULL REFERENCES agent_step(step_id) ON DELETE CASCADE,
          decision_record_id TEXT,
          snapshot_id TEXT,
          evidence_fingerprint TEXT,
          binding_status TEXT NOT NULL CHECK(binding_status IN ('bound_decision','legacy_unbound')),
          strategy_version TEXT NOT NULL,
          strategy_experiment_id TEXT,
          capability_name TEXT NOT NULL,
          capability_version TEXT NOT NULL,
          task_binding_json TEXT NOT NULL,
          expected_outcomes_json TEXT NOT NULL,
          observed_outcome_json TEXT NOT NULL,
          outcome_evidence_digest TEXT NOT NULL,
          attribution_strength TEXT NOT NULL CHECK(attribution_strength='association_only'),
          contract_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_outcome_attribution_child_time
        ON agent_outcome_attribution(child_id, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS trg_agent_outcome_attribution_no_update
        BEFORE UPDATE ON agent_outcome_attribution BEGIN
          SELECT RAISE(ABORT, 'agent_outcome_attribution is immutable');
        END;
        """
    )


def persist(
    connection: Any, *, outcome_id: str, step_id: str, created_at: str,
) -> str:
    """Bind an outcome to the exact local decision and intervention that preceded it."""
    prior = connection.execute(
        "SELECT attribution_id FROM agent_outcome_attribution WHERE outcome_id=?",
        (outcome_id,),
    ).fetchone()
    if prior:
        return prior["attribution_id"]
    row = connection.execute(
        """SELECT outcome.*,step.step_id,step.capability_name,step.capability_version,
                  step.arguments_json,plan.plan_id,plan.child_id AS plan_child_id,
                  plan.session_id,plan.agent_turn_id,plan.objective_id AS plan_objective_id
           FROM agent_outcome AS outcome
           JOIN agent_step AS step ON step.step_id=?
           JOIN agent_plan AS plan ON plan.plan_id=step.plan_id
           WHERE outcome.outcome_id=?""",
        (step_id, outcome_id),
    ).fetchone()
    if not row:
        raise OutcomeAttributionError("OUTCOME_OR_STEP_NOT_FOUND")
    if row["child_id"] != row["plan_child_id"] or row["objective_id"] != row["plan_objective_id"]:
        raise OutcomeAttributionError("OUTCOME_DECISION_SCOPE_MISMATCH")
    decision = connection.execute(
        "SELECT * FROM agent_decision_record WHERE plan_id=?",
        (row["plan_id"],),
    ).fetchone()
    binding_status = "bound_decision" if decision else "legacy_unbound"
    arguments = json.loads(row["arguments_json"] or "{}")
    task_binding = {
        key: arguments[key]
        for key in TASK_BINDING_KEYS
        if isinstance(arguments, dict) and arguments.get(key) not in (None, "")
    }
    evidence_refs = json.loads(row["evidence_refs_json"] or "[]")
    observed = {
        "outcome_level": row["outcome_level"],
        "confidence": float(row["confidence"]),
        "summary": row["summary"],
        "evidence_refs": evidence_refs,
    }
    expected = json.loads(decision["expected_outcomes_json"]) if decision else []
    evidence_digest = hashlib.sha256(
        json.dumps(evidence_refs, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attribution_id = str(uuid4())
    connection.execute(
        """INSERT INTO agent_outcome_attribution(
             attribution_id,outcome_id,objective_id,child_id,session_id,agent_turn_id,
             plan_id,step_id,decision_record_id,snapshot_id,evidence_fingerprint,
             binding_status,strategy_version,strategy_experiment_id,capability_name,
             capability_version,task_binding_json,expected_outcomes_json,
             observed_outcome_json,outcome_evidence_digest,attribution_strength,
             contract_version,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            attribution_id, outcome_id, row["objective_id"], row["child_id"],
            row["session_id"], row["agent_turn_id"], row["plan_id"], row["step_id"],
            decision["decision_record_id"] if decision else None,
            decision["snapshot_id"] if decision else None,
            decision["evidence_fingerprint"] if decision else None,
            binding_status,
            decision["strategy_version"] if decision else "legacy_unknown",
            decision["strategy_experiment_id"] if decision else None,
            row["capability_name"], row["capability_version"],
            json.dumps(task_binding, ensure_ascii=False, sort_keys=True),
            json.dumps(expected, ensure_ascii=False, sort_keys=True),
            json.dumps(observed, ensure_ascii=False, sort_keys=True),
            evidence_digest, "association_only", CONTRACT_VERSION, created_at,
        ),
    )
    return attribution_id


def for_outcome(connection: Any, outcome_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM agent_outcome_attribution WHERE outcome_id=?", (outcome_id,)
    ).fetchone()
    if not row:
        return None
    value = dict(row)
    for key in ("task_binding_json", "expected_outcomes_json", "observed_outcome_json"):
        value[key.removesuffix("_json")] = json.loads(value.pop(key))
    value.update({
        "causal_claim_allowed": False,
        "local_only": True,
        "cloud_transmission_allowed": False,
    })
    return value
