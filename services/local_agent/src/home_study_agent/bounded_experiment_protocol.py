"""Shared, evidence-bound lifecycle for teaching and behavior experiments."""
from __future__ import annotations

import json
import hashlib
import sqlite3
from typing import Any
from uuid import uuid4


PROTOCOL_VERSION = "bounded_experiment_protocol_v1"
OPEN_STATUSES = {"draft", "replay_passed", "shadow", "active"}
TERMINAL_STATUSES = {"completed", "stopped", "rolled_back"}
STATUSES = OPEN_STATUSES | TERMINAL_STATUSES
DOMAINS = {"teaching_strategy", "behavior_intervention"}


def ensure_schema(connection: sqlite3.Connection) -> None:
    from .transaction_safe_schema import execute_schema_statements
    execute_schema_statements(connection,
        """
        CREATE TABLE IF NOT EXISTS bounded_experiment_protocol (
          protocol_id TEXT PRIMARY KEY,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          domain TEXT NOT NULL CHECK(domain IN ('teaching_strategy','behavior_intervention')),
          source_kind TEXT NOT NULL CHECK(source_kind IN ('strategy_experiment','habit_experiment')),
          source_id TEXT NOT NULL,
          primary_variable TEXT NOT NULL,
          baseline_version TEXT NOT NULL,
          candidate_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN (
            'draft','replay_passed','shadow','active','completed','stopped','rolled_back'
          )),
          outcome_status TEXT NOT NULL CHECK(outcome_status IN (
            'unknown','improving','stable','worsening'
          )),
          metrics_json TEXT NOT NULL,
          thresholds_json TEXT NOT NULL,
          rollback_reason TEXT,
          protocol_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          ended_at TEXT,
          UNIQUE(source_kind, source_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bounded_experiment_one_open_variable
        ON bounded_experiment_protocol(child_id, domain, primary_variable)
        WHERE status IN ('draft','replay_passed','shadow','active');
        CREATE INDEX IF NOT EXISTS idx_bounded_experiment_child_status
        ON bounded_experiment_protocol(child_id, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS bounded_experiment_event (
          event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          protocol_id TEXT NOT NULL REFERENCES bounded_experiment_protocol(protocol_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          from_status TEXT,
          to_status TEXT NOT NULL,
          outcome_status TEXT NOT NULL,
          actor TEXT NOT NULL,
          reason TEXT NOT NULL,
          metrics_json TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE TRIGGER IF NOT EXISTS trg_bounded_experiment_event_no_update
        BEFORE UPDATE ON bounded_experiment_event BEGIN
          SELECT RAISE(ABORT, 'bounded_experiment_event is immutable');
        END;
        """
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    source = metrics or {}
    normalized = {
        "sample_size": int(source.get("sample_size") or 0),
        "independence_rate": source.get("independence_rate"),
        "transfer_rate": source.get("transfer_rate"),
        "retention_rate": source.get("retention_rate"),
        "recovery_seconds": source.get("recovery_seconds"),
        "prompt_count": int(source.get("prompt_count") or 0),
        "refusal_count": int(source.get("refusal_count") or 0),
        "burden_events": int(source.get("burden_events") or 0),
    }
    normalized["source_metrics"] = source.get("source_metrics", source)
    normalized["causal_effect_claimed"] = False
    normalized["mastery_claim_allowed"] = False
    return normalized


def _outcome(metrics: dict[str, Any], thresholds: dict[str, Any]) -> str:
    if metrics["sample_size"] < int(thresholds["minimum_sample_size"]):
        return "unknown"
    if metrics["refusal_count"] >= int(thresholds["max_refusal_count"]):
        return "worsening"
    if metrics["burden_events"] >= int(thresholds["max_burden_events"]):
        return "worsening"
    baseline = metrics["source_metrics"].get("baseline_score")
    current = metrics["source_metrics"].get("current_score")
    if isinstance(baseline, (int, float)) and isinstance(current, (int, float)):
        delta = float(current) - float(baseline)
        if delta <= -float(thresholds["worsening_delta"]):
            return "worsening"
        if delta >= float(thresholds["improving_delta"]):
            return "improving"
        return "stable"
    return "unknown"


def ensure_protocol(
    connection: sqlite3.Connection,
    *,
    child_id: str,
    domain: str,
    source_kind: str,
    source_id: str,
    primary_variable: str,
    baseline_version: str,
    candidate_version: str,
    status: str,
    metrics: dict[str, Any] | None,
    now: str,
    reason: str,
    actor: str = "system",
) -> dict[str, Any]:
    ensure_schema(connection)
    if domain not in DOMAINS or status not in STATUSES:
        raise ValueError("Unsupported experiment protocol domain or status.")
    row = connection.execute(
        "SELECT * FROM bounded_experiment_protocol WHERE source_kind=? AND source_id=?",
        (source_kind, source_id),
    ).fetchone()
    thresholds = {
        "minimum_sample_size": 3,
        "max_refusal_count": 2,
        "max_burden_events": 2,
        "worsening_delta": 0.25,
        "improving_delta": 0.10,
    }
    normalized = _normalized_metrics(metrics)
    outcome_status = _outcome(normalized, thresholds)
    if row:
        if row["child_id"] != child_id or row["domain"] != domain or row["primary_variable"] != primary_variable:
            raise ValueError("Experiment protocol source conflicts with saved ownership.")
        return transition_protocol(
            connection, source_kind=source_kind, source_id=source_id, target=status,
            metrics=metrics, now=now, reason=reason, actor=actor,
        )
    protocol_id = str(uuid4())
    connection.execute(
        """INSERT INTO bounded_experiment_protocol(
             protocol_id,child_id,domain,source_kind,source_id,primary_variable,
             baseline_version,candidate_version,status,outcome_status,metrics_json,
             thresholds_json,protocol_version,created_at,updated_at,ended_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            protocol_id, child_id, domain, source_kind, source_id, primary_variable,
            baseline_version, candidate_version, status, outcome_status,
            _json(normalized), _json(thresholds), PROTOCOL_VERSION, now, now,
            now if status in TERMINAL_STATUSES else None,
        ),
    )
    _event(
        connection, protocol_id=protocol_id, child_id=child_id, previous=None,
        target=status, outcome_status=outcome_status, actor=actor, reason=reason,
        metrics=normalized, now=now,
    )
    return protocol_state(connection, source_kind=source_kind, source_id=source_id)


def transition_protocol(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: str,
    target: str,
    metrics: dict[str, Any] | None,
    now: str,
    reason: str,
    actor: str = "system",
) -> dict[str, Any]:
    ensure_schema(connection)
    row = connection.execute(
        "SELECT * FROM bounded_experiment_protocol WHERE source_kind=? AND source_id=?",
        (source_kind, source_id),
    ).fetchone()
    if not row:
        raise KeyError("Unknown bounded experiment protocol")
    if target not in STATUSES:
        raise ValueError("Unsupported experiment protocol status.")
    if row["status"] in TERMINAL_STATUSES and row["status"] != target:
        raise ValueError("Terminal experiment protocol cannot be reopened.")
    thresholds = json.loads(row["thresholds_json"])
    normalized = _normalized_metrics(metrics)
    outcome_status = _outcome(normalized, thresholds)
    automatic_reason = None
    if target == "active" and outcome_status == "worsening":
        target = "rolled_back"
        automatic_reason = (
            "达到预设负向结果或负担阈值，自动撤回候选并恢复原策略；"
            "该决定不构成因果效果或长期习惯结论。"
        )
    effective_reason = automatic_reason or reason
    changed = (
        row["status"] != target
        or row["outcome_status"] != outcome_status
        or row["metrics_json"] != _json(normalized)
    )
    if changed:
        connection.execute(
            """UPDATE bounded_experiment_protocol
               SET status=?,outcome_status=?,metrics_json=?,rollback_reason=?,
                   updated_at=?,ended_at=? WHERE protocol_id=?""",
            (
                target, outcome_status, _json(normalized),
                effective_reason if target == "rolled_back" else row["rollback_reason"],
                now, now if target in TERMINAL_STATUSES else None, row["protocol_id"],
            ),
        )
        _event(
            connection, protocol_id=row["protocol_id"], child_id=row["child_id"],
            previous=row["status"], target=target, outcome_status=outcome_status,
            actor=actor, reason=effective_reason, metrics=normalized, now=now,
        )
    return protocol_state(connection, source_kind=source_kind, source_id=source_id)


def protocol_state(
    connection: sqlite3.Connection, *, source_kind: str, source_id: str,
) -> dict[str, Any]:
    ensure_schema(connection)
    row = connection.execute(
        "SELECT * FROM bounded_experiment_protocol WHERE source_kind=? AND source_id=?",
        (source_kind, source_id),
    ).fetchone()
    if not row:
        raise KeyError("Unknown bounded experiment protocol")
    result = dict(row)
    result["metrics"] = json.loads(result.pop("metrics_json"))
    result["thresholds"] = json.loads(result.pop("thresholds_json"))
    result["effectiveness_status"] = (
        "insufficient_evidence" if result["outcome_status"] == "unknown" else "bounded_observation_only"
    )
    result["causal_effect_claimed"] = False
    result["mastery_claim_allowed"] = False
    result["local_only"] = True
    result["cloud_transmission_allowed"] = False
    return result


def list_for_child(connection: sqlite3.Connection, child_id: str) -> list[dict[str, Any]]:
    ensure_schema(connection)
    rows = connection.execute(
        "SELECT source_kind,source_id FROM bounded_experiment_protocol WHERE child_id=? ORDER BY created_at DESC",
        (child_id,),
    ).fetchall()
    return [protocol_state(connection, source_kind=row["source_kind"], source_id=row["source_id"]) for row in rows]


def _event(
    connection: sqlite3.Connection,
    *,
    protocol_id: str,
    child_id: str,
    previous: str | None,
    target: str,
    outcome_status: str,
    actor: str,
    reason: str,
    metrics: dict[str, Any],
    now: str,
) -> None:
    fingerprint = hashlib.sha256(_json({
        "protocol_id": protocol_id, "from": previous, "to": target,
        "outcome": outcome_status, "metrics": metrics, "now": now,
    }).encode("utf-8")).hexdigest()
    connection.execute(
        """INSERT OR IGNORE INTO bounded_experiment_event(
             event_id,protocol_id,child_id,from_status,to_status,outcome_status,
             actor,reason,metrics_json,occurred_at,client_action_id
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid4()), protocol_id, child_id, previous, target, outcome_status,
            actor, reason, _json(metrics), now, f"bounded-protocol:{fingerprint}",
        ),
    )
