from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from typing import Any

from .analysis_candidate import AnalysisCandidateRejectedError, AnalysisCandidateService
from .behavior_model import BehaviorModelService, ObservableEvent
from .child_analysis_review import ChildAnalysisReviewService
from .child_model import ChildModelService
from .database import Database
from .repository import StudyRepository


CONTRACT_VERSION = "longitudinal_replay_gate_v1"
CHILDREN = ("child-primary-4", "child-junior-3")


class _OfflineGateway:
    configured = False

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> Any:
        self.requests.append(request)
        raise AssertionError("An offline replay gate must not dispatch a cloud request.")


def _candidate(
    child_id: str,
    snapshot: dict[str, Any],
    candidate_id: str,
    valid_until: datetime,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "child_id": child_id,
        "task_type": "daily_learning_review",
        "scope": {"period": "synthetic-30-day-replay"},
        "claims": [{
            "claim": "这是一个必须继续核验的合成学习假设。",
            "evidence_refs": [{
                "type": "child_model_snapshot", "id": snapshot["snapshot_id"],
            }],
            "counterevidence_refs": [],
        }],
        "confidence": 0.55,
        "uncertainty_reasons": ["合成数据不能证明真实孩子状态。"],
        "valid_until": valid_until.astimezone(timezone.utc).isoformat(),
        "verification_actions": ["用不同任务再次核验。"],
        "proposed_actions": [{
            "action_type": "schedule_check", "rationale": "仅安排核验，不改变事实。",
        }],
        "expected_outcomes": ["获得新的独立证据。"],
        "stop_conditions": ["出现反证、孩子拒绝或候选过期。"],
        "rollback_action": "撤销候选并保持原状态。",
        "model_provider": "synthetic-local-gate",
        "model_name": "no-cloud-model",
        "prompt_version": CONTRACT_VERSION,
        "response_version": CONTRACT_VERSION,
        "source_snapshot_id": snapshot["snapshot_id"],
        "evidence_fingerprint": snapshot["evidence_fingerprint"],
    }


def _insert_learning_day(
    database: Database,
    child_id: str,
    day_index: int,
    observed_at: datetime,
) -> str:
    session_id = f"synthetic-{child_id}-day-{day_index:02d}"
    timestamp = observed_at.astimezone(timezone.utc).isoformat()
    marker = f"marker::{child_id}::day::{day_index:02d}"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO daily_session(
                 session_id,child_id,status,state,started_at,ended_at,last_checkpoint_at,
                 observation_status,recall_declared_empty,homework_declared_empty,client_action_id
               ) VALUES(?,?,'completed','completed',?,?,?,'enabled',0,0,?)""",
            (session_id, child_id, timestamp, timestamp, timestamp, f"start-{session_id}"),
        )
        connection.execute(
            """INSERT INTO daily_reflection(
                 reflection_id,session_id,child_id,learned_text,difficult_text,feeling,
                 next_action,skipped,created_at,client_action_id
               ) VALUES(?,?,?,?,?,?,?,0,?,?)""",
            (
                f"reflection-{session_id}", session_id, child_id,
                f"完成合成学习日 {day_index}；{marker}",
                "仍需独立任务核验。", "一般", "明天继续核验。", timestamp,
                f"reflection-action-{session_id}",
            ),
        )
        connection.commit()
    return session_id


def _record_failure(failures: list[str], condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def run_longitudinal_replay_gate(database_path: Path | str | None = None) -> dict[str, Any]:
    """Replay two isolated children for 30 days without real data or cloud calls."""

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if database_path is None:
        temporary = tempfile.TemporaryDirectory(prefix="hsc-longitudinal-gate-")
        path = Path(temporary.name) / "replay.db"
    else:
        path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    database = Database(path)
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    behavior = BehaviorModelService(database)
    behavior.initialize()
    model = ChildModelService(database)

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=29)
    snapshot_ids: dict[str, list[str]] = {child_id: [] for child_id in CHILDREN}
    last_sessions: dict[str, str] = {}
    for day_index in range(30):
        observed_at = start + timedelta(days=day_index)
        for child_id in CHILDREN:
            last_sessions[child_id] = _insert_learning_day(
                database, child_id, day_index + 1, observed_at,
            )
            snapshot_ids[child_id].append(model.snapshot(child_id)["snapshot_id"])

    with database.connect() as connection:
        session_counts = {
            row["child_id"]: int(row["count"])
            for row in connection.execute(
                """SELECT child_id,COUNT(*) AS count FROM daily_session
                   WHERE session_id LIKE 'synthetic-%' GROUP BY child_id"""
            )
        }
    for child_id in CHILDREN:
        _record_failure(
            failures, session_counts.get(child_id) == 30,
            f"{child_id}: expected 30 isolated sessions",
        )
        _record_failure(
            failures, len(set(snapshot_ids[child_id])) == 30,
            f"{child_id}: expected one durable snapshot per changed learning day",
        )

    leakage: list[str] = []
    for child_id, other_child in ((CHILDREN[0], CHILDREN[1]), (CHILDREN[1], CHILDREN[0])):
        current = model.snapshot(child_id)
        serialized = json.dumps(current, ensure_ascii=False, sort_keys=True)
        if other_child in serialized or f"marker::{other_child}" in serialized:
            leakage.append(f"{other_child} found in {child_id} snapshot")

    candidates = AnalysisCandidateService(database)
    primary_snapshot = model.snapshot(CHILDREN[0])
    cross_child_rejected = False
    wrong = _candidate(
        CHILDREN[1], primary_snapshot, "synthetic-cross-child", now + timedelta(days=2),
    )
    try:
        candidates.record_and_validate(
            CHILDREN[1], wrong, "synthetic-cross-child-attempt",
        )
    except AnalysisCandidateRejectedError as error:
        cross_child_rejected = bool(
            {"cross_child_source_snapshot", "cross_child_evidence"} & set(error.reason_codes)
        )
    _record_failure(failures, cross_child_rejected, "cross-child candidate evidence was accepted")

    withdrawal_payload = _candidate(
        CHILDREN[0], primary_snapshot, "synthetic-withdrawal", now + timedelta(days=2),
    )
    candidates.record_and_validate(
        CHILDREN[0], withdrawal_payload, "synthetic-withdrawal-ingest",
    )
    projected = model.snapshot(CHILDREN[0])
    before_withdrawal = any(
        item.get("hypothesis_id", "").startswith("analysis-candidate:synthetic-withdrawal:")
        for item in projected["payload"]["open_hypotheses"]
    )
    withdrawn = candidates.withdraw(
        CHILDREN[0], "synthetic-withdrawal", "synthetic_counterevidence",
        "synthetic-withdrawal-action",
    )
    after_withdrawal = model.snapshot(CHILDREN[0])
    removed_after_withdrawal = all(
        not item.get("hypothesis_id", "").startswith("analysis-candidate:synthetic-withdrawal:")
        for item in after_withdrawal["payload"]["open_hypotheses"]
    )
    _record_failure(failures, before_withdrawal, "validated candidate was not projected")
    _record_failure(
        failures, withdrawn["status"] == "withdrawn" and removed_after_withdrawal,
        "withdrawn candidate remained in the child projection",
    )

    junior_snapshot = model.snapshot(CHILDREN[1])
    expiry_payload = _candidate(
        CHILDREN[1], junior_snapshot, "synthetic-expiry", now + timedelta(minutes=5),
    )
    candidates.record_and_validate(
        CHILDREN[1], expiry_payload, "synthetic-expiry-ingest",
    )
    expired = candidates.get(
        CHILDREN[1], "synthetic-expiry", now=now + timedelta(minutes=10),
    )
    _record_failure(failures, expired["status"] == "expired", "candidate expiry was ignored")

    habit_times = (now - timedelta(days=1), now - timedelta(days=1, minutes=-5), now)
    for index, occurred_at in enumerate(habit_times, start=1):
        behavior.ingest(ObservableEvent(
            child_id=CHILDREN[0], session_id=last_sessions[CHILDREN[0]],
            event_id=f"synthetic-stall-{index}", occurred_at=occurred_at.isoformat(),
            event_type="work.stall", source="synthetic_local_observation", confidence=0.9,
            payload={"model_version": CONTRACT_VERSION},
            alternative_explanations=("The synthetic task may be intentionally difficult.",),
        ))
    proposed = behavior.pending_habit_experiment(CHILDREN[0])
    declined: dict[str, Any] | None = None
    if proposed["status"] == "proposed":
        declined = behavior.respond_habit_experiment(
            proposed["experiment"]["experiment_id"], CHILDREN[0], False,
            now.isoformat(), "synthetic-habit-decline",
        )
    _record_failure(failures, declined is not None, "cross-day habit experiment was not proposed")
    _record_failure(
        failures,
        bool(declined and declined["status"] == "declined"
             and behavior.active_habit_experiment(CHILDREN[0])["status"] == "none"),
        "child refusal did not stop the proposed habit experiment",
    )

    offline_gateway = _OfflineGateway()
    offline_service = ChildAnalysisReviewService(
        database, offline_gateway, repository, cloud_text_allowed=lambda: True,
    )
    offline_job = offline_service.enqueue_for_session(last_sessions[CHILDREN[1]])
    offline_result = asyncio.run(offline_service.process_next())
    offline_saved = offline_service.job(offline_job["job_id"])
    offline_preserved = (
        offline_result is None and offline_saved["status"] == "pending"
        and int(offline_saved["attempt_count"]) == 0 and not offline_gateway.requests
    )
    _record_failure(failures, offline_preserved, "offline review was lost or dispatched")

    restarted_database = Database(path)
    restarted_model = ChildModelService(restarted_database)
    restarted_behavior = BehaviorModelService(restarted_database)
    restarted_candidates = AnalysisCandidateService(restarted_database)
    restart_ok = (
        restarted_candidates.get(CHILDREN[0], "synthetic-withdrawal")["status"] == "withdrawn"
        and restarted_behavior.active_habit_experiment(CHILDREN[0])["status"] == "none"
        and restarted_model.snapshot(CHILDREN[0])["child_id"] == CHILDREN[0]
        and ChildAnalysisReviewService(
            restarted_database, offline_gateway, StudyRepository(restarted_database),
            cloud_text_allowed=lambda: True,
        ).job(offline_job["job_id"])["status"] == "pending"
    )
    _record_failure(failures, restart_ok, "replay state did not survive database restart")

    result = {
        "contract_version": CONTRACT_VERSION,
        "status": "passed" if not failures and not leakage else "failed",
        "synthetic_only": True,
        "cloud_requests": len(offline_gateway.requests),
        "days": 30,
        "children": list(CHILDREN),
        "sessions": session_counts,
        "snapshots": {key: len(set(value)) for key, value in snapshot_ids.items()},
        "revocation": "passed" if withdrawn["status"] == "withdrawn" and removed_after_withdrawal else "failed",
        "expiry": "passed" if expired["status"] == "expired" else "failed",
        "child_refusal": "passed" if declined and declined["status"] == "declined" else "failed",
        "cloud_offline": "passed" if offline_preserved else "failed",
        "restart": "passed" if restart_ok else "failed",
        "cross_child_leaks": leakage,
        "failures": failures,
    }
    if temporary is not None:
        temporary.cleanup()
    return result
