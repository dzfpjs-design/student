from __future__ import annotations

import json
from pathlib import Path

from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_supply import record_attempt
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService

from test_answer_attempt_ledger import setup_case


def pending_request(tmp_path: Path, *, runtime_mode='trial'):
    database, monitor = setup_case(tmp_path, runtime_mode=runtime_mode)
    monitor.record_revision("study", "page", "page-signature", "paper-supply-revision")
    with database.connect() as connection:
        candidate = connection.execute(
            "SELECT candidate_id,question_text FROM mistake_capture_candidate"
        ).fetchone()
    service = PaperScoringService(database)
    request = service.request(
        candidate_id=candidate["candidate_id"], child_id="child", session_id="study",
        question_text=candidate["question_text"], answer_text="80",
        answer_source="child_spoken", visual_answer_evidence_id=None,
        client_action_id="paper-supply-request",
    )
    return database, service, request


def test_unresolved_paper_score_creates_system_owned_durable_obligation(tmp_path: Path) -> None:
    database, service, request = pending_request(tmp_path)
    assert service.resolve(request["request_id"], "child", "paper-supply-resolve") is None

    current = service.for_candidate(request["candidate_id"], "child")
    supply = current["authority_supply"]
    assert supply["status"] == "pending_authority"
    assert supply["system_owned"] is True
    assert supply["requires_child_review"] is False
    assert supply["requires_parent_subject_review"] is False
    assert supply["requirements"]["temporary_model_solution_allowed"] is False

    restarted = PaperScoringService(database).for_candidate(
        request["candidate_id"], "child"
    )
    assert restarted["authority_supply"] == supply
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_supply_obligation"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_supply_event"
        ).fetchone()[0] == 1


def test_supply_attempt_rejects_child_content_and_qualified_authority_closes_obligation(
    tmp_path: Path,
) -> None:
    database, service, request = pending_request(tmp_path)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            record_attempt(
                connection, request_id=request["request_id"], child_id="child",
                status="rubric_candidate", reason="candidate generated",
                evidence={"question_text": "must not duplicate"}, client_action_id="bad-event",
            )
        except ValueError as error:
            assert "cannot duplicate child content" in str(error)
        else:
            raise AssertionError("child content entered the supply journal")
        connection.rollback()

    service.register_exact_authority(
        child_id="child", session_id="study", question_text=request["question_text"],
        expected_answers=["80"], authority_type="published_answer_key",
        authority_ref="local-workbook-answer-key:v1", rubric_version="v1",
        client_action_id="paper-supply-authority",
    )
    decision = service.resolve(request["request_id"], "child", "paper-supply-qualified")
    assert decision["correctness"] == 1
    supply = service.for_candidate(request["candidate_id"], "child")["authority_supply"]
    assert supply["status"] == "qualified"
    assert supply["latest_evidence"]["decision_id"] == decision["decision_id"]


def test_supply_history_exports_and_cascades_with_request(tmp_path: Path) -> None:
    database, _, request = pending_request(tmp_path)
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child("child")["path"]).read_text("utf-8"))
    assert len(exported["paper_scoring_supply_obligations"]) == 1
    assert len(exported["paper_scoring_supply_events"]) == 1

    with database.connect() as connection:
        connection.execute(
            "DELETE FROM paper_scoring_request WHERE request_id=?", (request["request_id"],)
        )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_supply_obligation"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_supply_event"
        ).fetchone()[0] == 0
