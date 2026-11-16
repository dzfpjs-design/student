from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.analysis_candidate import (
    AnalysisCandidateConflictError,
    AnalysisCandidateRejectedError,
    AnalysisCandidateService,
)
from home_study_agent.app import create_app
from home_study_agent.child_model import ChildModelService
from home_study_agent.config import Settings
from home_study_agent.database import Database


CHILD = "child-primary-4"
OTHER = "child-junior-3"


def _database(tmp_path: Path) -> Database:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "analysis-candidate.db",
        data_encryption_key=b"a" * 32,
        maintenance_enabled=False,
    )
    with TestClient(create_app(settings)):
        pass
    return Database(settings.database_path)


def _payload(snapshot: dict, *, child_id: str = CHILD) -> dict:
    return {
        "candidate_id": "candidate-daily-1",
        "child_id": child_id,
        "task_type": "daily_learning_review",
        "scope": {"date": "2026-09-28", "subject": "数学"},
        "claims": [{
            "claim": "计算后检查步骤可能还不稳定，需要用一道新题核实。",
            "evidence_refs": [{"type": "child_model_snapshot", "id": snapshot["snapshot_id"]}],
            "counterevidence_refs": [],
        }],
        "confidence": 0.62,
        "uncertainty_reasons": ["当前只有一次状态快照"],
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "verification_actions": ["安排一道不同题面的无提示检查题"],
        "proposed_actions": [{
            "action_type": "schedule_check",
            "rationale": "先核实再决定是否调整教学。",
        }],
        "expected_outcomes": ["获得一条可评分的独立作答证据"],
        "stop_conditions": ["孩子拒绝或当天负荷已满"],
        "rollback_action": "取消待验证项，不改变掌握状态。",
        "model_provider": "synthetic",
        "model_name": "analysis-test-model",
        "prompt_version": "child-analysis-v1",
        "response_version": "analysis-candidate-v1",
        "source_snapshot_id": snapshot["snapshot_id"],
        "evidence_fingerprint": snapshot["evidence_fingerprint"],
    }


def test_valid_cloud_candidate_is_durable_and_only_projects_as_open_hypothesis(tmp_path: Path) -> None:
    database = _database(tmp_path)
    snapshot = ChildModelService(database).snapshot(CHILD)
    service = AnalysisCandidateService(database)
    payload = _payload(snapshot)

    saved = service.record_and_validate(CHILD, payload, "analysis-action-1")
    repeated = service.record_and_validate(CHILD, payload, "analysis-action-1")

    assert saved == repeated
    assert saved["status"] == "validated"
    assert saved["authority_granted"] is False
    assert saved["cloud_transmission_allowed"] is False
    restarted = AnalysisCandidateService(Database(database.path)).get(CHILD, saved["candidate_id"])
    assert restarted["status"] == "validated"

    model = ChildModelService(Database(database.path)).snapshot(CHILD)
    projected = next(item for item in model["payload"]["open_hypotheses"]
                     if item["hypothesis_id"] == "analysis-candidate:candidate-daily-1:0")
    assert projected["source"] == "validated_cloud_analysis_candidate"
    assert projected["mastery_claim_allowed"] is False
    assert projected["evidence_for"] == [
        {"type": "child_model_snapshot", "id": snapshot["snapshot_id"]}
    ]
    assert model["payload"]["learning"]["evidenced_node_count"] == 0


@pytest.mark.parametrize("mutation,reason", [
    (lambda body, own, other: body["claims"][0]["evidence_refs"].__setitem__(
        0, {"type": "child_model_snapshot", "id": other["snapshot_id"]}), "cross_child_evidence"),
    (lambda body, own, other: body["claims"][0]["evidence_refs"].__setitem__(
        0, {"type": "learning_evidence", "id": "invented-evidence"}), "unknown_evidence"),
    (lambda body, own, other: body.__setitem__("mastery_probability", .99), "forbidden_field"),
    (lambda body, own, other: body.__setitem__(
        "valid_until", (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()), "expired_candidate"),
])
def test_invalid_model_output_cannot_pollute_child_state(tmp_path: Path, mutation, reason: str) -> None:
    database = _database(tmp_path)
    own = ChildModelService(database).snapshot(CHILD)
    other = ChildModelService(database).snapshot(OTHER)
    payload = _payload(own)
    mutation(payload, own, other)

    with pytest.raises(AnalysisCandidateRejectedError) as rejected:
        AnalysisCandidateService(database).record_and_validate(
            CHILD, payload, f"reject-{reason}"
        )
    assert reason in rejected.value.reason_codes

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analysis_candidate").fetchone()[0] == 0
        attempt = connection.execute(
            "SELECT status,reason_codes_json,payload_json FROM analysis_candidate_ingest_attempt"
        ).fetchone()
    assert attempt["status"] == "rejected"
    assert reason in attempt["reason_codes_json"]
    assert attempt["payload_json"] is None
    assert all(not item["hypothesis_id"].startswith("analysis-candidate:")
               for item in ChildModelService(database).snapshot(CHILD)["payload"]["open_hypotheses"])


def test_idempotency_conflict_and_immutable_candidate_history(tmp_path: Path) -> None:
    database = _database(tmp_path)
    snapshot = ChildModelService(database).snapshot(CHILD)
    service = AnalysisCandidateService(database)
    service.record_and_validate(CHILD, _payload(snapshot), "analysis-idempotent")

    changed = _payload(snapshot)
    changed["confidence"] = .2
    with pytest.raises(AnalysisCandidateConflictError):
        service.record_and_validate(CHILD, changed, "analysis-idempotent")

    with database.connect() as connection:
        with pytest.raises(Exception):
            connection.execute(
                "UPDATE analysis_candidate SET confidence=.9 WHERE candidate_id='candidate-daily-1'"
            )
        with pytest.raises(Exception):
            connection.execute(
                "DELETE FROM analysis_candidate WHERE candidate_id='candidate-daily-1'"
            )


def test_later_expiry_removes_projection_without_erasing_candidate(tmp_path: Path, monkeypatch) -> None:
    database = _database(tmp_path)
    start = datetime.now(timezone.utc)
    snapshot = ChildModelService(database).snapshot(CHILD)
    payload = _payload(snapshot)
    payload["valid_until"] = (start + timedelta(minutes=5)).isoformat()
    service = AnalysisCandidateService(database)
    service.record_and_validate(CHILD, payload, "analysis-expiry")

    monkeypatch.setattr("home_study_agent.child_model._utc_now", lambda: start + timedelta(minutes=10))
    model = ChildModelService(database).snapshot(CHILD)
    assert all(not item["hypothesis_id"].startswith("analysis-candidate:")
               for item in model["payload"]["open_hypotheses"])
    assert service.get(CHILD, "candidate-daily-1", now=start + timedelta(minutes=10))["status"] == "expired"


def test_local_correction_withdraws_candidate_immediately_and_survives_restart(tmp_path: Path) -> None:
    database = _database(tmp_path)
    snapshot = ChildModelService(database).snapshot(CHILD)
    service = AnalysisCandidateService(database)
    service.record_and_validate(CHILD, _payload(snapshot), "analysis-before-withdrawal")

    withdrawn = service.withdraw(
        CHILD, "candidate-daily-1", "new_evidence_contradicts_claim",
        "withdraw-analysis-1",
    )
    replay = service.withdraw(
        CHILD, "candidate-daily-1", "new_evidence_contradicts_claim",
        "withdraw-analysis-1",
    )

    assert withdrawn["status"] == "withdrawn"
    assert replay["status"] == "withdrawn"
    assert AnalysisCandidateService(Database(database.path)).get(
        CHILD, "candidate-daily-1"
    )["status"] == "withdrawn"
    model = ChildModelService(database).snapshot(CHILD)
    assert all(not item["hypothesis_id"].startswith("analysis-candidate:")
               for item in model["payload"]["open_hypotheses"])

    with pytest.raises(KeyError):
        service.withdraw(
            OTHER, "candidate-daily-1", "new_evidence_contradicts_claim",
            "withdraw-analysis-other-child",
        )
