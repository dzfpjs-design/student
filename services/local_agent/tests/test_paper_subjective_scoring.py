from __future__ import annotations

import json
from pathlib import Path
import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_capture import MistakeCaptureService
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import utc_now
from test_school_open_reviews import CHILD, school_case
from test_writing_calibrated_gate import authority


def paper_request_for_assessment(database, assessment_id, *, suffix="paper"):
    with database.connect() as connection:
        current = RubricEvidenceService(None).get(
            CHILD, assessment_id, connection=connection
        )
        now = utc_now()
        artifact_id = f"{suffix}-artifact"
        candidate_id = f"{suffix}-candidate"
        connection.execute(
            """INSERT INTO capture_artifact(
                 artifact_id,child_id,session_id,artifact_type,local_path,
                 extracted_text,confidence,details_json,status,created_at,client_action_id
               ) VALUES(?,?,?,'document_ocr',NULL,?,.99,?,'completed',?,?)""",
            (artifact_id, CHILD, current["session_id"], current["question"],
             json.dumps({"source": "subjective-paper-test"}), now,
             f"{suffix}-artifact-action"),
        )
        connection.execute(
            """INSERT INTO mistake_capture_candidate(
                 candidate_id,artifact_id,child_id,question_index,question_text,
                 region_json,confidence,status,mistake_id,created_at,updated_at
               ) VALUES(?,?,?,0,?,NULL,.99,'pending',NULL,?,?)""",
            (candidate_id, artifact_id, CHILD, current["question"], now, now),
        )
        connection.commit()
    service = PaperScoringService(database)
    request = service.request(
        candidate_id=candidate_id, child_id=CHILD,
        session_id=current["session_id"], question_text=current["question"],
        answer_text=current["response"], answer_source="child_spoken",
        visual_answer_evidence_id=None, client_action_id=f"{suffix}-request",
    )
    return service, request, current


def test_exact_calibrated_subjective_score_binds_and_survives_restart(
    school_case, monkeypatch, tmp_path,
):
    database = school_case[0]
    _, automatic, _, _ = authority(monkeypatch, school_case, 3)
    service, request, current = paper_request_for_assessment(
        database, automatic["assessment_id"]
    )

    decision = service.resolve(request["request_id"], CHILD, "subjective-resolve")
    assert decision["correctness"] == 1
    assert decision["authority_type"] == "agent_generated_rubric"
    assert decision["authority_snapshot"]["authority_subtype"] == "calibrated_subjective_rubric"
    assert decision["authority_snapshot"]["assessment_id"] == automatic["assessment_id"]
    assert decision["mastery_update_allowed"] is False
    assert decision["cloud_transmission_allowed"] is False

    restarted = PaperScoringService(Database(database.path)).for_candidate(
        request["candidate_id"], CHILD
    )
    assert restarted["effective_status"] == "scored"
    assert restarted["resolved_correctness"] == 1
    with database.connect() as connection:
        binding = connection.execute(
            "SELECT * FROM paper_subjective_score_binding"
        ).fetchone()
        assert binding["assessment_id"] == automatic["assessment_id"]
        assert binding["source_evidence_id"] == current["source_evidence_id"]
        assert connection.execute(
            "SELECT status FROM paper_scoring_decision_status_event ORDER BY rowid DESC"
        ).fetchone()[0] == "qualified"
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child(CHILD)["path"]).read_text("utf-8"))
    assert exported["paper_subjective_score_bindings"][0]["assessment_id"] == automatic["assessment_id"]
    assert exported["paper_scoring_decision_status_events"][0]["status"] == "qualified"


def test_human_correction_withdraws_subjective_paper_score_in_same_transaction(
    school_case, monkeypatch,
):
    database, client, headers = school_case[:3]
    _, automatic, _, (path, body) = authority(monkeypatch, school_case, 0)
    service, request, _ = paper_request_for_assessment(
        database, automatic["assessment_id"], suffix="corrected"
    )
    assert service.resolve(request["request_id"], CHILD, "corrected-resolve")["correctness"] == 0

    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert service.resolve(request["request_id"], CHILD, "corrected-replay") is None
    projection = PaperScoringService(Database(database.path)).for_candidate(
        request["candidate_id"], CHILD
    )
    assert projection["effective_status"] == "pending_authority"
    assert projection.get("resolved_correctness") is None
    with database.connect() as connection:
        events = connection.execute(
            "SELECT status,reason FROM paper_scoring_decision_status_event ORDER BY rowid"
        ).fetchall()
        assert [row["status"] for row in events] == ["qualified", "withdrawn"]
        assert events[-1]["reason"] == "subjective scoring authority changed"


def test_human_correction_excludes_mistake_derived_from_subjective_score(
    school_case, monkeypatch,
):
    database, client, headers = school_case[:3]
    _, automatic, _, (path, body) = authority(monkeypatch, school_case, 0)
    service, request, current = paper_request_for_assessment(
        database, automatic["assessment_id"], suffix="derived-wrong"
    )
    assert service.resolve(request["request_id"], CHILD, "derived-wrong-resolve")["correctness"] == 0
    with database.connect() as connection:
        node = connection.execute(
            """SELECT node.* FROM diagnostic_item AS item
               JOIN knowledge_node AS node USING(knowledge_node_id)
               WHERE item.diagnostic_item_id=?""",
            (current["material"]["diagnostic_item_id"],),
        ).fetchone()
    book = MistakeBookService(database)
    captured = MistakeCaptureService(database, book).capture(
        request["candidate_id"], CHILD, node["knowledge_node_id"],
        node["subject"], node["title"], current["response"],
        "capture-derived-subjective-wrong",
    )
    mistake_id = captured["mistake"]["mistake_id"]
    book.confirm(mistake_id, CHILD, "confirm", "confirm-derived-subjective-wrong")
    assert book.get(mistake_id, CHILD)["recorded_status"] == "diagnosing"

    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    detail = MistakeBookService(Database(database.path)).get(mistake_id, CHILD)
    assert detail["recorded_status"] == "excluded"
    with database.connect() as connection:
        event = connection.execute(
            """SELECT to_status,reason FROM mistake_status_event
               WHERE mistake_id=? ORDER BY rowid DESC LIMIT 1""", (mistake_id,)
        ).fetchone()
        assert event["to_status"] == "excluded"
        assert "评分权威" in event["reason"]


def test_subjective_score_never_matches_a_similar_but_different_paper_fact(
    school_case, monkeypatch,
):
    database = school_case[0]
    _, automatic, _, _ = authority(monkeypatch, school_case, 3)
    service, request, current = paper_request_for_assessment(
        database, automatic["assessment_id"], suffix="different"
    )
    with database.connect() as connection:
        connection.execute("DELETE FROM paper_scoring_request WHERE request_id=?", (request["request_id"],))
        connection.commit()
    different = service.request(
        candidate_id=request["candidate_id"], child_id=CHILD,
        session_id=current["session_id"], question_text=current["question"],
        answer_text=current["response"] + "。", answer_source="child_spoken",
        visual_answer_evidence_id=None, client_action_id="different-request",
    )
    assert service.resolve(different["request_id"], CHILD, "different-resolve") is None
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_subjective_score_binding").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM paper_scoring_decision").fetchone()[0] == 0


@pytest.mark.parametrize("corrected", [False, True])
def test_original_subjective_reconciliation_does_not_switch_scoring_protocol(
    school_case, monkeypatch, corrected,
):
    database, client, headers = school_case[:3]
    _, automatic, _, (path, body) = authority(monkeypatch, school_case, 0)
    service, request, _ = paper_request_for_assessment(
        database, automatic["assessment_id"], suffix="protocol-owner"
    )
    assert service.resolve(request["request_id"], CHILD, "protocol-resolve")["correctness"] == 0

    def unexpected_printed_lookup(*args, **kwargs):
        raise AssertionError("an existing subjective binding must recheck its own protocol")

    monkeypatch.setattr(
        "home_study_agent.paper_printed_scoring.authority_for_request",
        unexpected_printed_lookup,
    )
    if corrected:
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
    result = PaperScoringService(Database(database.path)).for_candidate(request["candidate_id"], CHILD)
    assert result["effective_status"] == ("pending_authority" if corrected else "scored")
    assert result.get("resolved_correctness") == (None if corrected else 0)
