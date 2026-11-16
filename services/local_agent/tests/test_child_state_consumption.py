from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from home_study_agent.child_state_consumption import (
    ChildStateConsumptionError,
    ChildStateConsumptionService,
)
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository


PRIMARY = "child-primary-4"


def _service(tmp_path: Path) -> tuple[Database, ChildStateConsumptionService]:
    database = Database(tmp_path / "child-state-consumption.db")
    database.initialize()
    StudyRepository(database).seed_family()
    service = ChildStateConsumptionService(database)
    service.ensure_schema()
    return database, service


def test_all_consumers_can_freeze_one_exact_snapshot_and_restore_it(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    snapshot = service.capture(PRIMARY)
    consumers = (
        ("daily_plan", "daily_plan", "plan-1"),
        ("tutor", "tutor_request", "tutor-1"),
        ("proactive_prompt", "agent_proactive_prompt", "prompt-1"),
        ("daily_report", "daily_report", "daily-report-1"),
        ("period_report", "period_report", "period-report-1"),
    )

    receipts = []
    for consumer_type, source_type, source_id in consumers:
        receipts.append(service.persist_snapshot(
            child_id=PRIMARY,
            consumer_type=consumer_type,
            source_type=source_type,
            source_id=source_id,
            snapshot=snapshot,
            capture_phase="decision_input",
            purpose=f"test_{consumer_type}",
        ))

    assert {receipt["snapshot_id"] for receipt in receipts} == {snapshot["snapshot_id"]}
    assert {receipt["evidence_fingerprint"] for receipt in receipts} == {
        snapshot["evidence_fingerprint"]
    }
    assert all(receipt["cloud_transmission_allowed"] is False for receipt in receipts)
    assert all(receipt["causal_claim_allowed"] is False for receipt in receipts)

    restarted = ChildStateConsumptionService(Database(database.path))
    restored = restarted.get("daily_plan", "daily_plan", "plan-1")
    repeated = restarted.persist_snapshot(
        child_id=PRIMARY,
        consumer_type="daily_plan",
        source_type="daily_plan",
        source_id="plan-1",
        snapshot=snapshot,
        capture_phase="result_state",
        purpose="a_retry_cannot_rebind",
    )
    assert repeated["consumption_id"] == restored["consumption_id"]
    assert repeated["capture_phase"] == "decision_input"

    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE child_state_consumption SET purpose='changed' WHERE consumption_id=?",
                (receipts[0]["consumption_id"],),
            )


def test_consumption_identity_cannot_cross_child_scope(tmp_path: Path) -> None:
    _, service = _service(tmp_path)
    snapshot = service.capture(PRIMARY)
    service.persist_snapshot(
        child_id=PRIMARY,
        consumer_type="tutor",
        source_type="tutor_request",
        source_id="same-request",
        snapshot=snapshot,
        capture_phase="decision_input",
        purpose="tutor",
    )

    with pytest.raises(ChildStateConsumptionError, match="SCOPE_MISMATCH"):
        service.bind_current(
            child_id="child-junior-3",
            consumer_type="tutor",
            source_type="tutor_request",
            source_id="same-request",
            capture_phase="decision_input",
            purpose="tutor",
        )
