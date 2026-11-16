from __future__ import annotations

import json
from pathlib import Path

from home_study_agent.answer_attempt_ledger import AnswerAttemptLedger
from home_study_agent.database import Database
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import utc_now


def setup_case(tmp_path: Path, *, runtime_mode='trial'):
    database = Database(tmp_path / "study.db", runtime_mode=runtime_mode)
    database.initialize()
    LearningStateService(database).seed_catalog()
    now = utc_now()
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO child_profile(
                 child_id,nickname,grade,curriculum_region,status,created_at,updated_at
               ) VALUES('child','学生','初三','江苏南京','active',?,?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO daily_session(
                 session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id
               ) VALUES('study','child','active','study',?,?,?)""",
            (now, now, "answer-ledger-session"),
        )
        connection.execute(
            """INSERT INTO capture_artifact(
                 artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,
                 confidence,details_json,status,created_at,client_action_id
               ) VALUES('page','child','study','document_ocr',NULL,'本地文字不可依赖',.4,?,
                        'quality_warning',?,'answer-ledger-page')""",
            (json.dumps({
                "local_pipeline": {
                    "question_blocks": [{
                        "text": "2. 计算 (24+6)×3，只回答一个整数。",
                        "block_kind": "question",
                        "needs_manual_boundary_check": False,
                        "confidence": .94,
                        "bbox_xywh": [100, 200, 900, 300],
                    }],
                    "wrong_question_candidates": [{
                        "text": "2. 计算 (24+6)×3，只回答一个整数。",
                        "mark_kind": "cross_like",
                        "confidence": .88,
                        "bbox_xywh": [100, 200, 900, 300],
                        "requires_child_confirmation": True,
                        "correctness": "unverified",
                    }],
                },
                "cloud_vision_fallback": {
                    "status": "needs_review",
                    "privacy": {
                        "original_frame_uploaded": False,
                        "uploaded": "page_region_binary_ink",
                    },
                    "blocks": [
                        {
                            "kind": "text",
                            "text": "2. 计算 (24+6)×3，只回答一个整数。",
                            "bbox": [.1, .2, .9, .38],
                            "confidence": .92,
                            "source": "printed",
                        },
                        {
                            "kind": "text", "text": "80", "bbox": [.3, .39, .36, .44],
                            "confidence": .86, "source": "handwritten",
                        },
                        {
                            "kind": "text", "text": "cross", "bbox": [.37, .39, .43, .45],
                            "confidence": .81, "source": "grading_mark",
                        },
                    ],
                }
            }, ensure_ascii=False), now),
        )
        connection.commit()
    monitor = DocumentMonitoringService(database)
    monitor.start("study", "answer-ledger-monitor")
    return database, monitor


def capture_visible_attempt(database: Database, monitor: DocumentMonitoringService):
    result = monitor.record_revision("study", "page", "page-signature", "answer-ledger-revision")
    assert result["observed_answer_evidence_count"] == 1
    return result


def test_visible_answer_is_unverified_local_evidence_and_survives_restart(tmp_path: Path) -> None:
    database, monitor = setup_case(tmp_path)
    revision = capture_visible_attempt(database, monitor)

    rows = AnswerAttemptLedger(database).list_for_child("child")
    assert len(rows) == 1
    observed = rows[0]
    assert observed["record_kind"] == "visual_observation"
    assert observed["question_text"].startswith("2. 计算 (24+6)×3")
    assert observed["submitted_answer"] == "80"
    assert observed["visible_grading_marks"] == ["cross", "cross_like"]
    assert observed["correctness"] is None
    assert observed["scoring_status"] == "unverified"
    assert observed["hint_level"] is None
    assert observed["independent"] is None
    assert observed["elapsed_seconds"] is None
    assert observed["mastery_update_allowed"] is False
    assert observed["cloud_transmission_allowed"] is False
    assert observed["evidence_refs"]["revision_id"] == revision["revision_id"]
    assert "artifact_path" not in json.dumps(observed)

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM observed_answer_evidence").fetchone()[0] == 1

    restarted = AnswerAttemptLedger(Database(database.path, runtime_mode=database.runtime_mode)).list_for_child("child")
    assert restarted == rows
    replay = DocumentMonitoringService(Database(database.path, runtime_mode=database.runtime_mode)).record_revision(
        "study", "page", "page-signature", "answer-ledger-revision"
    )
    assert replay["revision_id"] == revision["revision_id"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observed_answer_evidence").fetchone()[0] == 1


def test_scored_and_visual_attempts_share_ledger_but_only_qualified_score_is_effective(tmp_path: Path) -> None:
    database, monitor = setup_case(tmp_path)
    capture_visible_attempt(database, monitor)
    attempt = LearningStateService(database).submit_diagnostic(
        "study", "diag-p4-arithmetic-closed-1", "90", "answer-ledger-scored-attempt"
    )

    rows = AnswerAttemptLedger(database).list_for_child("child")
    by_kind = {row["record_kind"]: row for row in rows}
    assert set(by_kind) == {"visual_observation", "diagnostic_attempt"}
    scored = by_kind["diagnostic_attempt"]
    assert scored["ledger_entry_id"] == attempt["attempt_id"]
    assert scored["submitted_answer"] == "90"
    assert scored["correctness"] == 1
    assert scored["scoring_status"] == "qualified_effective"
    assert scored["mastery_update_allowed"] is True
    assert by_kind["visual_observation"]["mastery_update_allowed"] is False


def test_visual_answer_evidence_exports_and_cascades_with_page_source(tmp_path: Path) -> None:
    database, monitor = setup_case(tmp_path)
    capture_visible_attempt(database, monitor)
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child("child")["path"]).read_text(encoding="utf-8"))
    assert len(exported["observed_answer_evidence"]) == 1
    assert exported["observed_answer_evidence"][0]["scoring_status"] == "unverified"

    with database.connect() as connection:
        connection.execute("DELETE FROM capture_artifact WHERE artifact_id='page'")
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM document_page_revision").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM observed_answer_evidence").fetchone()[0] == 0


def test_restart_backfills_visible_answer_for_legacy_question_occurrence(tmp_path: Path) -> None:
    database, monitor = setup_case(tmp_path)
    capture_visible_attempt(database, monitor)
    with database.connect() as connection:
        connection.execute("DELETE FROM observed_answer_evidence")
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM observed_question_occurrence").fetchone()[0] == 1

    DocumentMonitoringService(Database(database.path, runtime_mode=database.runtime_mode))

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observed_question_occurrence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM observed_answer_evidence").fetchone()[0] == 1
