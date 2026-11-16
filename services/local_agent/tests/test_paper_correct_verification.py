from __future__ import annotations

import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.learning_obligations import project_mastery_evidence
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import utc_now

from test_answer_attempt_ledger import capture_visible_attempt, setup_case
from test_visual_answer_mistake_handoff import make_teacher_mark_correct


def correct_paper_case(tmp_path):
    database, monitor = setup_case(tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE child_profile SET grade='四年级' WHERE child_id='child'")
        connection.commit()
    make_teacher_mark_correct(database)
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.book.knowledge_candidates = lambda *_args, **_kwargs: {"items": [{
        "knowledge_node_id": "node-p4-math-arithmetic",
        "title": "整数四则运算顺序",
        "match_score": 1.0,
    }]}
    dialogue.begin("study", "page", "correct-paper-begin")
    result = dialogue.respond("study", "对", "correct-paper-confirm")
    return database, result


def test_correct_paper_creates_distinct_next_day_check_without_mastery(tmp_path: Path) -> None:
    database, result = correct_paper_case(tmp_path)
    state = result["state"]
    assert result["phase"] == "scored_correct"
    assert state["correct_verification_status"] == "pending"
    assert state["correct_verification_review_task_id"]
    assert "复测前不会把它算作已掌握" in result["response_text"]

    with database.connect() as connection:
        obligation = connection.execute(
            "SELECT * FROM paper_correct_verification_obligation"
        ).fetchone()
        target = connection.execute(
            """SELECT target.*,review.interval_label,review.status,review.due_at
                 FROM paper_correct_verification_target target
                 JOIN review_task review USING(review_task_id)"""
        ).fetchone()
        source = connection.execute(
            "SELECT * FROM learning_evidence WHERE evidence_id=?",
            (obligation["source_evidence_id"],),
        ).fetchone()
        assert target["diagnostic_item_id"] == "diag-p4-arithmetic-closed-2"
        assert target["target_question_identity"] != obligation["source_question_identity"]
        assert target["interval_label"] == "paper_independent_check"
        assert target["status"] == "pending"
        assert source["evidence_type"] == "paper_scored_correct_source"
        assert source["independent"] == 0
        projection = project_mastery_evidence(
            connection, "child", obligation["knowledge_node_id"], as_of=utc_now()
        )
        assert obligation["source_evidence_id"] not in {
            row["evidence_id"] for row in projection["effective_evidence"]
        }
        assert connection.execute("SELECT COUNT(*) FROM mastery_state").fetchone()[0] == 0

    restarted = MistakeDialogueService(Database(database.path, runtime_mode=database.runtime_mode)).state("study")
    assert restarted["state"]["correct_verification_review_task_id"] == target["review_task_id"]


def test_distinct_no_hint_answer_completes_check_and_enters_mastery_projection(tmp_path: Path) -> None:
    database, result = correct_paper_case(tmp_path)
    review_id = result["state"]["correct_verification_review_task_id"]
    with database.connect() as connection:
        connection.execute(
            "UPDATE review_task SET due_at=? WHERE review_task_id=?",
            ("2020-01-01T00:00:00+00:00", review_id),
        )
        target = connection.execute(
            "SELECT diagnostic_item_id FROM review_task WHERE review_task_id=?", (review_id,)
        ).fetchone()[0]
        connection.commit()

    attempt = LearningStateService(database).submit_diagnostic(
        "study", target, "42", "correct-paper-independent-answer",
        review_task_id=review_id, hint_level=0,
    )
    assert attempt["correctness"] == 1
    with database.connect() as connection:
        review = connection.execute(
            "SELECT * FROM review_task WHERE review_task_id=?", (review_id,)
        ).fetchone()
        evidence = connection.execute(
            "SELECT * FROM learning_evidence WHERE evidence_id=?", (attempt["evidence_id"],)
        ).fetchone()
        assert review["status"] == "completed"
        assert review["result_evidence_id"] == attempt["evidence_id"]
        assert evidence["independent"] == 1
        projection = project_mastery_evidence(
            connection, "child", review["knowledge_node_id"], as_of=utc_now()
        )
        assert attempt["evidence_id"] in {
            row["evidence_id"] for row in projection["effective_evidence"]
        }
        assert connection.execute(
            "SELECT evidence_count FROM mastery_state WHERE child_id='child' AND knowledge_node_id=?",
            (review["knowledge_node_id"],),
        ).fetchone()[0] == 1


def test_source_page_deletion_withdraws_check_and_descendant_mastery(tmp_path: Path) -> None:
    database, result = correct_paper_case(tmp_path)
    review_id = result["state"]["correct_verification_review_task_id"]
    with database.connect() as connection:
        obligation = connection.execute(
            "SELECT * FROM paper_correct_verification_obligation"
        ).fetchone()
        connection.execute("DELETE FROM capture_artifact WHERE artifact_id='page'")
        connection.commit()
        assert connection.execute(
            "SELECT 1 FROM learning_evidence_withdrawal WHERE evidence_id=?",
            (obligation["source_evidence_id"],),
        ).fetchone()
        assert connection.execute(
            "SELECT status FROM review_task WHERE review_task_id=?", (review_id,)
        ).fetchone()[0] == "cancelled"


def test_source_page_deletion_replays_completed_check_out_of_mastery(tmp_path: Path) -> None:
    database, result = correct_paper_case(tmp_path)
    review_id = result["state"]["correct_verification_review_task_id"]
    with database.connect() as connection:
        connection.execute(
            "UPDATE review_task SET due_at='2020-01-01T00:00:00+00:00' WHERE review_task_id=?",
            (review_id,),
        )
        row = connection.execute(
            "SELECT diagnostic_item_id,knowledge_node_id FROM review_task WHERE review_task_id=?",
            (review_id,),
        ).fetchone()
        connection.commit()
    attempt = LearningStateService(database).submit_diagnostic(
        "study", row["diagnostic_item_id"], "42", "completed-before-source-delete",
        review_task_id=review_id,
    )
    with database.connect() as connection:
        before = project_mastery_evidence(
            connection, "child", row["knowledge_node_id"], as_of=utc_now()
        )
        assert attempt["evidence_id"] in {
            evidence["evidence_id"] for evidence in before["effective_evidence"]
        }
        connection.execute("DELETE FROM capture_artifact WHERE artifact_id='page'")
        connection.commit()
    LearningStateService(database).replay_mastery(
        "child", row["knowledge_node_id"], as_of=utc_now()
    )
    with database.connect() as connection:
        assert connection.execute(
            "SELECT evidence_count FROM mastery_state WHERE child_id='child' AND knowledge_node_id=?",
            (row["knowledge_node_id"],),
        ).fetchone()[0] == 0
        after = project_mastery_evidence(
            connection, "child", row["knowledge_node_id"], as_of=utc_now()
        )
        assert attempt["evidence_id"] not in {
            evidence["evidence_id"] for evidence in after["effective_evidence"]
        }


def test_correct_verification_exports_and_survives_restart(tmp_path: Path) -> None:
    database, result = correct_paper_case(tmp_path)
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = json.loads(
        Path(privacy.export_child("child")["path"]).read_text(encoding="utf-8")
    )
    assert len(exported["paper_correct_verification_obligations"]) == 1
    assert len(exported["paper_correct_verification_targets"]) == 1
    assert [event["status"] for event in exported["paper_correct_verification_events"]] == [
        "pending_supply", "scheduled"
    ]
    restarted = Database(database.path, runtime_mode=database.runtime_mode)
    restarted.initialize()
    with restarted.connect() as connection:
        assert connection.execute(
            "SELECT status FROM review_task WHERE review_task_id=?",
            (result["state"]["correct_verification_review_task_id"],),
        ).fetchone()[0] == "pending"
