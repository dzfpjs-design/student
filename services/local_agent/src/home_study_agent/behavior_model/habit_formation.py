"""Evidence-bound habit stages and prompt fading over the existing goal ledger."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from .goal_confirmation import assessment
from ..transaction_safe_schema import execute_schema_statements


POLICY_VERSION = "habit_formation_policy_v1"
STAGES = {
    "negotiate", "baseline", "cue", "practice", "reinforce", "fade",
    "transfer", "maintain", "recover", "declined", "stopped", "completed",
}
CUE_LEVELS = {"L3", "L2", "L1", "L0"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def auto_stop_pending(connection, child_id: str, experiment_id: str) -> bool:
    return bool(connection.execute('''SELECT 1 FROM habit_experiment h
        JOIN habit_experiment_event e ON e.child_id=h.child_id AND e.experiment_id=h.experiment_id
        WHERE h.child_id=? AND h.experiment_id=? AND h.status='active'
          AND e.event_type='auto_stop_pending' LIMIT 1''', (child_id, experiment_id)).fetchone())


def auto_stop_pending_for_state(connection, child_id: str, state: str) -> bool:
    return bool(connection.execute('''SELECT 1 FROM habit_experiment h
        JOIN habit_experiment_event e ON e.child_id=h.child_id AND e.experiment_id=h.experiment_id
        WHERE h.child_id=? AND h.target_state=? AND h.status='active'
          AND e.event_type='auto_stop_pending' LIMIT 1''', (child_id, state)).fetchone())


def _queue_auto_stop(connection, experiment, now):
    action = 'habit-auto-stop:' + _digest([experiment['child_id'], experiment['experiment_id']])
    prior = connection.execute('SELECT child_id,experiment_id,event_type FROM habit_experiment_event WHERE client_action_id=?', (action,)).fetchone()
    if prior:
        if (prior['child_id'], prior['experiment_id'], prior['event_type']) != (experiment['child_id'], experiment['experiment_id'], 'auto_stop_pending'):
            raise ValueError('HABIT_AUTO_STOP_ACTION_CONFLICT')
        return
    connection.execute('''INSERT INTO habit_experiment_event
        (event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id)
        VALUES(?,?,?,'auto_stop_pending',?,?,?)''',
        (str(uuid4()),experiment['experiment_id'],experiment['child_id'],
         _canonical({'policy_version':POLICY_VERSION}),now.astimezone(timezone.utc).isoformat(),action))


def ensure_habit_formation_schema(connection: sqlite3.Connection) -> None:
    execute_schema_statements(connection,
        """
        CREATE TABLE IF NOT EXISTS habit_formation_state (
          experiment_id TEXT PRIMARY KEY REFERENCES habit_experiment(experiment_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL,
          target_state TEXT NOT NULL,
          stage TEXT NOT NULL CHECK(stage IN (
            'negotiate','baseline','cue','practice','reinforce','fade','transfer',
            'maintain','recover','declined','stopped','completed'
          )),
          cue_level TEXT NOT NULL CHECK(cue_level IN ('L3','L2','L1','L0')),
          revision INTEGER NOT NULL,
          assessment_fingerprint TEXT NOT NULL,
          metrics_json TEXT NOT NULL,
          transition_reason TEXT NOT NULL,
          policy_version TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_habit_formation_child_stage
        ON habit_formation_state(child_id, stage, updated_at DESC);
        CREATE TABLE IF NOT EXISTS habit_formation_event (
          event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          experiment_id TEXT NOT NULL REFERENCES habit_experiment(experiment_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL,
          from_stage TEXT,
          to_stage TEXT NOT NULL,
          from_cue_level TEXT,
          to_cue_level TEXT NOT NULL,
          assessment_fingerprint TEXT NOT NULL,
          metrics_json TEXT NOT NULL,
          reason TEXT NOT NULL,
          policy_version TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE TRIGGER IF NOT EXISTS trg_habit_formation_event_no_update
        BEFORE UPDATE ON habit_formation_event BEGIN
          SELECT RAISE(ABORT, 'habit_formation_event is immutable');
        END;
        """
    )


def _burden_metrics(
    connection: sqlite3.Connection, child_id: str, target_state: str,
    started_at: str | None,
) -> dict[str, Any]:
    since = started_at or "0001-01-01T00:00:00+00:00"
    feedback = connection.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN feedback='declined' THEN 1 ELSE 0 END) AS declined,
                  SUM(CASE WHEN feedback='accepted' THEN 1 ELSE 0 END) AS accepted
           FROM intervention
           WHERE child_id=? AND candidate_state=? AND decided_at>=?
             AND status IN ('active','withdrawn')
             AND feedback IN ('accepted','declined')""",
        (child_id, target_state, since),
    ).fetchone()
    retracted = connection.execute(
        """SELECT COUNT(DISTINCT br.event_id) AS count FROM behavior_retraction br
           JOIN observable_event oe ON oe.event_id=br.event_id AND oe.child_id=br.child_id
           JOIN probabilistic_state ps ON ps.event_id=oe.event_id AND ps.child_id=oe.child_id
             AND ps.session_id IS oe.session_id
           WHERE oe.child_id=? AND ps.state=? AND br.created_at>=?""",
        (child_id, target_state, since),
    ).fetchone()
    total = int(feedback["total"] or 0)
    declined = int(feedback["declined"] or 0)
    return {
        "feedback_count": total,
        "accepted_count": int(feedback["accepted"] or 0),
        "declined_count": declined,
        "decline_rate": round(declined / total, 3) if total else None,
        "retracted_observation_count": int(retracted["count"] or 0),
    }


def _assessment_metrics(result: dict[str, Any], burden: dict[str, Any]) -> dict[str, Any]:
    baseline = result.get("baseline") if isinstance(result.get("baseline"), dict) else {}
    current = result.get("current") if isinstance(result.get("current"), dict) else {}
    strata = current.get("strata") if isinstance(current.get("strata"), dict) else {}
    success_days = sorted({
        day for item in strata.values() if isinstance(item, dict)
        for day in item.get("success_days", []) if isinstance(day, str)
    })
    return {
        "assessment_status": result.get("status"),
        "direction": result.get("direction"),
        "bounded_goal_success": bool(result.get("bounded_goal_success")),
        "missing_evidence": list(result.get("missing_evidence") or []),
        "baseline_opportunities": int(baseline.get("opportunities") or 0),
        "current_opportunities": int(current.get("opportunities") or 0),
        "current_context_count": len(strata),
        "current_independent_successes": int(current.get("independent_successes") or 0),
        "success_day_count": len(success_days),
        "success_days": success_days,
        "burden": burden,
        "habit_success": False,
        "causal_effect_claimed": False,
    }


def _next_stage(
    current: sqlite3.Row | None, experiment: sqlite3.Row,
    metrics: dict[str, Any], evidence_changed: bool,
) -> tuple[str, str, str]:
    status = experiment["status"]
    if status == "proposed":
        return "negotiate", "L3", "awaiting_age_appropriate_child_response"
    if status == "declined":
        return "declined", "L0", "child_declined_or_did_not_want_this_experiment"
    if status == "stopped":
        return "stopped", "L0", "experiment_stopped"
    if status == "completed":
        previous = current["stage"] if current else None
        if previous == "maintain":
            return "completed", "L0", "bounded_window_completed_after_maintenance"
        return "completed", "L0", "time_window_closed_without_lifetime_habit_claim"

    burden = metrics["burden"]
    if (
        burden["declined_count"] >= 2 and (burden["decline_rate"] or 0) >= 0.5
    ) or burden["retracted_observation_count"] >= 2:
        return "stopped", "L0", "burden_or_false_reminder_stop_condition"

    if current is None or current["stage"] in {"negotiate", "declined", "stopped", "completed"}:
        return "baseline", "L0", "accepted_low_risk_trial_requires_comparable_baseline_without_prompt"
    if not evidence_changed:
        return current["stage"], current["cue_level"], "same_evidence_no_transition"

    stage = current["stage"]
    bounded = metrics["bounded_goal_success"]
    repeated = metrics["assessment_status"] in {
        "repeated_observed_behavior", "bounded_confirmed_behavior",
    }
    opportunities = metrics["current_opportunities"]
    if stage == "baseline" and opportunities >= 5:
        return "cue", "L3", "minimum_current_opportunities_observed"
    if stage == "cue" and opportunities >= 5:
        return "practice", "L3", "practice_window_ready"
    if stage == "practice" and repeated:
        return "reinforce", "L3", "repeated_behavior_observed_without_habit_claim"
    if stage == "reinforce" and bounded:
        return "fade", "L2", "bounded_independent_behavior_allows_one_level_fade"
    if stage == "fade" and bounded and metrics["current_context_count"] >= 2:
        return "transfer", "L1", "bounded_behavior_seen_in_multiple_contexts"
    if (
        stage == "transfer" and bounded and metrics["current_context_count"] >= 2
        and metrics["success_day_count"] >= 3
    ):
        return "maintain", "L0", "multi_context_repeated_behavior_allows_no_prompt_check"
    if stage == "maintain" and not bounded and opportunities > 0:
        return "recover", "L1", "new_evidence_no_longer_supports_maintenance"
    if stage == "recover" and bounded:
        return "fade", "L2", "bounded_behavior_recovered_without_resetting_history"
    return stage, current["cue_level"], "evidence_not_sufficient_for_next_stage"


def synchronize_habit_formation(
    connection: sqlite3.Connection,
    child_id: str,
    experiment_id: str,
    now: datetime,
    *,
    assessment_provider: Callable[[sqlite3.Connection, str, str, datetime], dict[str, Any]] = assessment,
    complete_auto_stop: bool = False,
) -> dict[str, Any]:
    ensure_habit_formation_schema(connection)
    experiment = connection.execute(
        "SELECT * FROM habit_experiment WHERE experiment_id=? AND child_id=?",
        (experiment_id, child_id),
    ).fetchone()
    if not experiment:
        raise KeyError("Unknown habit experiment")
    current = connection.execute(
        "SELECT * FROM habit_formation_state WHERE experiment_id=? AND child_id=?",
        (experiment_id, child_id),
    ).fetchone()
    result = assessment_provider(connection, child_id, experiment_id, now)
    burden = _burden_metrics(
        connection, child_id, experiment["target_state"], experiment["started_at"],
    )
    metrics = _assessment_metrics(result, burden)
    fingerprint = _digest({
        "experiment_status": experiment["status"],
        "goal_version": result.get("goal_version"),
        "metrics": metrics,
    })
    evidence_changed = current is None or current["assessment_fingerprint"] != fingerprint
    stage, cue_level, reason = _next_stage(current, experiment, metrics, evidence_changed)
    pending = auto_stop_pending(connection, child_id, experiment_id)
    if experiment['status'] == 'active' and (stage == 'stopped' or pending):
        if not complete_auto_stop:
            _queue_auto_stop(connection, experiment, now)
            if current is None:
                return {'experiment_id': experiment_id, 'child_id': child_id,
                    'target_state': experiment['target_state'], 'stage': 'pending_stop', 'cue_level': 'L0',
                    'revision': 0, 'assessment_fingerprint': fingerprint, 'metrics': metrics,
                    'transition_reason': 'auto_stop_pending', 'policy_version': POLICY_VERSION,
                    'updated_at': now.astimezone(timezone.utc).isoformat(), 'habit_success': False,
                    'local_only': True, 'cloud_transmission_allowed': False}
            return {**habit_formation_state(connection, child_id, experiment_id, assume_initialized=True),
                    'stop_pending': True, 'cue_level': 'L0'}
        stage, cue_level, reason = 'stopped', 'L0', 'burden_or_false_reminder_stop_condition'
    if stage == "stopped" and experiment["status"] == "active":
        connection.execute(
            "UPDATE habit_experiment SET status='stopped',closed_at=? WHERE experiment_id=?",
            (now.isoformat(), experiment_id),
        )
        connection.execute(
            """UPDATE intervention SET status='withdrawn',withdrawn_at=?,
                 withdrawal_reason='habit_formation_stop_condition'
               WHERE child_id=? AND candidate_state=? AND status='active'""",
            (now.isoformat(), child_id, experiment["target_state"]),
        )
    changed = current is None or current["stage"] != stage or current["cue_level"] != cue_level
    if changed or evidence_changed:
        revision = int(current["revision"] if current else 0) + 1
        action_id = f"habit-evaluation:{experiment_id}:{revision}:{fingerprint}"
        connection.execute(
            """INSERT INTO habit_formation_event(
                 event_id,experiment_id,child_id,from_stage,to_stage,from_cue_level,
                 to_cue_level,assessment_fingerprint,metrics_json,reason,policy_version,
                 occurred_at,client_action_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid4()), experiment_id, child_id,
                current["stage"] if current else None, stage,
                current["cue_level"] if current else None, cue_level,
                fingerprint, _canonical(metrics), reason, POLICY_VERSION,
                now.astimezone(timezone.utc).isoformat(), action_id,
            ),
        )
        connection.execute(
            """INSERT INTO habit_formation_state(
                 experiment_id,child_id,target_state,stage,cue_level,revision,
                 assessment_fingerprint,metrics_json,transition_reason,policy_version,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(experiment_id) DO UPDATE SET
                 stage=excluded.stage,cue_level=excluded.cue_level,revision=excluded.revision,
                 assessment_fingerprint=excluded.assessment_fingerprint,
                 metrics_json=excluded.metrics_json,transition_reason=excluded.transition_reason,
                 policy_version=excluded.policy_version,updated_at=excluded.updated_at""",
            (
                experiment_id, child_id, experiment["target_state"], stage, cue_level,
                revision, fingerprint, _canonical(metrics), reason, POLICY_VERSION,
                now.astimezone(timezone.utc).isoformat(),
            ),
        )
    state = habit_formation_state(connection, child_id, experiment_id)
    _synchronize_bounded_protocol(connection, experiment, state, now)
    return state


def _synchronize_bounded_protocol(
    connection: sqlite3.Connection, experiment: sqlite3.Row,
    state: dict[str, Any], now: datetime,
) -> None:
    from ..bounded_experiment_protocol import (
        ensure_protocol, protocol_state, transition_protocol,
    )
    stage = state["stage"]
    reason = state["transition_reason"]
    metrics = state["metrics"]
    burden = metrics.get("burden") or {}
    protocol_metrics = {
        "sample_size": int(metrics.get("current_opportunities") or 0),
        "independence_rate": (
            round(int(metrics.get("current_independent_successes") or 0)
                  / int(metrics.get("current_opportunities") or 1), 3)
            if int(metrics.get("current_opportunities") or 0) else None
        ),
        "transfer_rate": (
            1.0 if int(metrics.get("current_context_count") or 0) >= 2 else None
        ),
        "prompt_count": int(burden.get("feedback_count") or 0),
        "refusal_count": int(burden.get("declined_count") or 0),
        "burden_events": int(burden.get("retracted_observation_count") or 0),
        "source_metrics": metrics,
    }
    timestamp = now.astimezone(timezone.utc).isoformat()
    try:
        protocol = protocol_state(
            connection, source_kind="habit_experiment", source_id=experiment["experiment_id"],
        )
    except KeyError:
        protocol = ensure_protocol(
            connection, child_id=experiment["child_id"], domain="behavior_intervention",
            source_kind="habit_experiment", source_id=experiment["experiment_id"],
            primary_variable=experiment["target_state"], baseline_version="observed_baseline_v1",
            candidate_version=POLICY_VERSION, status="draft", metrics=protocol_metrics,
            now=timestamp, reason="建立单变量行为实验草案；尚未向孩子实施提示。",
        )
    if stage == "negotiate":
        target = "draft"
    elif stage == "baseline":
        if protocol["status"] == "draft":
            protocol = transition_protocol(
                connection, source_kind="habit_experiment", source_id=experiment["experiment_id"],
                target="replay_passed", metrics=protocol_metrics, now=timestamp,
                reason="既有观察达到建立可比基线的最低条件；仍不向孩子实施候选提示。",
            )
        target = "shadow"
    elif stage in {"cue", "practice", "reinforce", "fade", "transfer", "maintain", "recover"}:
        target = "active"
    elif stage == "completed":
        target = "completed"
    elif stage in {"declined", "stopped"}:
        target = "rolled_back" if reason == "burden_or_false_reminder_stop_condition" else "stopped"
    else:
        target = "draft"
    if protocol["status"] in {"completed", "stopped", "rolled_back"}:
        return
    transition_protocol(
        connection, source_kind="habit_experiment", source_id=experiment["experiment_id"],
        target=target, metrics=protocol_metrics, now=timestamp, reason=reason,
    )


def habit_formation_state(
    connection: sqlite3.Connection, child_id: str, experiment_id: str, *, assume_initialized: bool = False,
) -> dict[str, Any]:
    if not assume_initialized:
        ensure_habit_formation_schema(connection)
    row = connection.execute(
        "SELECT * FROM habit_formation_state WHERE experiment_id=? AND child_id=?",
        (experiment_id, child_id),
    ).fetchone()
    if not row:
        raise KeyError("Habit formation state is not initialized")
    return {
        "experiment_id": row["experiment_id"],
        "child_id": row["child_id"],
        "target_state": row["target_state"],
        "stage": row["stage"],
        "cue_level": row["cue_level"],
        "revision": int(row["revision"]),
        "assessment_fingerprint": row["assessment_fingerprint"],
        "metrics": json.loads(row["metrics_json"]),
        "transition_reason": row["transition_reason"],
        "policy_version": row["policy_version"],
        "updated_at": row["updated_at"],
        "habit_success": False,
        "authority_boundary": "A stage controls prompt burden; it is not a personality or lifetime habit label.",
        "local_only": True,
        "cloud_transmission_allowed": False,
    }


def current_prompt_policy(
    connection: sqlite3.Connection, child_id: str, target_state: str,
) -> dict[str, Any] | None:
    ensure_habit_formation_schema(connection)
    if auto_stop_pending_for_state(connection, child_id, target_state):
        return {'stage': 'pending_stop', 'cue_level': 'L0', 'suppress_voice': True,
                'suppression_reason': 'habit_auto_stop_pending', 'policy_version': POLICY_VERSION}
    row = connection.execute(
        """SELECT state.* FROM habit_formation_state state
           JOIN habit_experiment experiment USING(experiment_id)
           WHERE state.child_id=? AND state.target_state=?
             AND experiment.status='active'
           ORDER BY state.updated_at DESC LIMIT 1""",
        (child_id, target_state),
    ).fetchone()
    if not row:
        return None
    suppression_reason = None
    if row["cue_level"] == "L0":
        suppression_reason = {
            "baseline": "habit_baseline_observation_no_prompt",
            "maintain": "habit_maintenance_no_prompt",
            "declined": "habit_experiment_declined_no_prompt",
            "stopped": "habit_experiment_stopped_no_prompt",
            "completed": "habit_experiment_completed_no_prompt",
        }.get(row["stage"], "habit_formation_no_prompt")
    return {
        "experiment_id": row["experiment_id"],
        "stage": row["stage"],
        "cue_level": row["cue_level"],
        "suppress_voice": row["cue_level"] == "L0",
        "suppression_reason": suppression_reason,
        "policy_version": row["policy_version"],
        "assessment_fingerprint": row["assessment_fingerprint"],
    }
