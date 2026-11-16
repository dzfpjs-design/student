from __future__ import annotations

import json
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.answer_attempt_ledger import AnswerAttemptLedger
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_source_authority import PaperScoringSourceAuthorityService
from home_study_agent.paper_scoring_source_match import PaperScoringSourceMatchService
from home_study_agent.paper_scoring_supply import PaperScoringAuthorityDraftService
from home_study_agent.repository import StudyRepository
from home_study_agent.repository import utc_now

from test_answer_attempt_ledger import setup_case
from test_paper_scoring_source_authority import ObjectiveDraftGateway
from test_paper_scoring_source_match import add_local_page
from test_paper_scoring_supply import pending_request


def pending_dialogue(tmp_path: Path):
    database, scoring, request = pending_request(tmp_path)
    with database.connect() as connection:
        node = connection.execute(
            """SELECT knowledge_node_id,title FROM knowledge_node
               WHERE subject='数学' AND status='active' ORDER BY rowid LIMIT 1"""
        ).fetchone()
        state = {
            "artifact_id": "page",
            "question_text": request["question_text"],
            "submitted_answer": request["answer_text"],
            "answer_source": "child_spoken",
            "text_confirmed": True,
            "subject": "数学",
            "knowledge_node_id": node["knowledge_node_id"],
            "knowledge_title": node["title"],
            "scoring_request_id": request["request_id"],
            "scoring_request_status": "pending_authority",
        }
        connection.execute(
            """INSERT INTO mistake_dialogue(
                 session_id,child_id,candidate_id,mistake_id,phase,state_json,updated_at
               ) VALUES('study','child',?,NULL,'scoring_pending',?,?)""",
            (request["candidate_id"], json.dumps(state, ensure_ascii=False), utc_now()),
        )
        connection.commit()
    return database, scoring, request


def qualify(scoring: PaperScoringService, request: dict, answer: str):
    scoring.register_exact_authority(
        child_id="child", session_id=None, question_text=request["question_text"],
        expected_answers=[answer], authority_type="published_answer_key",
        authority_ref=f"local-answer-key:{answer}", rubric_version="v1",
        client_action_id=f"register-answer-key:{answer}",
    )
    return scoring.resolve(
        request["request_id"], "child", f"resolve-answer-key:{answer}"
    )


def test_late_wrong_decision_enters_mistake_without_child_correctness_confirmation(
    tmp_path: Path,
) -> None:
    database, scoring, request = pending_dialogue(tmp_path)
    decision = qualify(scoring, request, "90")
    dialogue = MistakeDialogueService(database)

    first = dialogue.reconcile_pending_scoring()
    second = MistakeDialogueService(database).reconcile_pending_scoring()

    assert len(first) == 1
    assert first[0]["outcome"] == "mistake_created"
    assert second == []
    with database.connect() as connection:
        current = connection.execute(
            "SELECT mistake_id,phase,state_json FROM mistake_dialogue WHERE session_id='study'"
        ).fetchone()
        mistake = connection.execute(
            "SELECT status FROM mistake_case WHERE mistake_id=?", (current["mistake_id"],)
        ).fetchone()
        status_event = connection.execute(
            """SELECT reason FROM mistake_status_event
               WHERE mistake_id=? ORDER BY rowid DESC LIMIT 1""",
            (current["mistake_id"],),
        ).fetchone()
        receipt = connection.execute(
            "SELECT * FROM paper_scoring_dialogue_reconciliation WHERE decision_id=?",
            (decision["decision_id"],),
        ).fetchone()
    assert current["phase"] in {"diagnosis", "remediation", "attempt", "scheduled"}
    assert json.loads(current["state_json"])["scoring_authority_type"] == "published_answer_key"
    assert mistake["status"] != "awaiting_confirmation"
    assert "合格评分依据自动确认错题" in status_event["reason"]
    assert receipt["outcome"] == "mistake_created"


def test_late_correct_decision_schedules_independent_check_even_after_session_end(
    tmp_path: Path,
) -> None:
    database, scoring, request = pending_dialogue(tmp_path)
    decision = qualify(scoring, request, "80")
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET status='completed',state='closed' WHERE session_id='study'"
        )
        connection.commit()

    result = MistakeDialogueService(database).reconcile_pending_scoring()

    assert result == [{
        "decision_id": decision["decision_id"],
        "outcome": "correct_verification_scheduled",
        "mistake_id": None,
        "phase": "scored_correct",
    }]
    with database.connect() as connection:
        dialogue = connection.execute(
            "SELECT phase,state_json FROM mistake_dialogue WHERE session_id='study'"
        ).fetchone()
        obligation = connection.execute(
            """SELECT event.status FROM paper_correct_verification_obligation AS obligation
               JOIN paper_correct_verification_event AS event USING(obligation_id)
               WHERE obligation.decision_id=? ORDER BY event.rowid DESC LIMIT 1""",
            (decision["decision_id"],),
        ).fetchone()
    state = json.loads(dialogue["state_json"])
    assert dialogue["phase"] == "scored_correct"
    assert state["correct_verification_status"] in {"pending", "pending_supply"}
    assert obligation["status"] in {"pending", "pending_supply"}
    assert state["scored_correctness"] == 1


def test_corrected_answer_page_replaces_wrong_mistake_with_independent_check(
    tmp_path: Path,
) -> None:
    from home_study_agent.knowledge_graph.textbook_page_service import (
        TextbookOcrPageInput, TextbookOcrPageService,
    )

    database, scoring, request = pending_dialogue(tmp_path)
    draft = PaperScoringAuthorityDraftService(
        database, ObjectiveDraftGateway(), StudyRepository(database), lambda: True,
    )
    assert asyncio.run(draft.process_next())["status"] == "completed"
    page = dict(
        title="初三数学练习册", material_kind="workbook", grade_scope="初三",
        subject_scope="数学", source_ref="local_parent_textbook_capture:corrected-dialogue",
        ocr_confidence=.95,
    )
    pages = TextbookOcrPageService(database)
    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：90",
    ))
    matches = PaperScoringSourceMatchService(database)
    matches.process_next()
    wrong = PaperScoringSourceAuthorityService(database).process_next()
    assert wrong["correctness"] == 0
    dialogue = MistakeDialogueService(database)
    assert dialogue.reconcile_pending_scoring()[0]["outcome"] == "mistake_created"
    with database.connect() as connection:
        mistake_id = connection.execute(
            "SELECT mistake_id FROM mistake_dialogue WHERE session_id='study'",
        ).fetchone()[0]

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：80",
    ))
    assert scoring.for_candidate(request["candidate_id"], "child")["decision_id"] is None
    matches.process_next()
    correct = PaperScoringSourceAuthorityService(database).process_next()
    assert correct["correctness"] == 1
    pending = scoring.for_candidate(request["candidate_id"], "child")
    assert pending["decision_id"] is None
    assert pending["effective_status"] == "pending_reconciliation"
    assert pending["authority_supply"]["status"] == "pending_reconciliation"
    dialogue = MistakeDialogueService(database)

    original_apply = dialogue._apply_paper_scoring_decision

    def crash_after_derived_state(*args, **kwargs):
        original_apply(*args, **kwargs)
        raise RuntimeError("injected crash before reconciliation receipt")

    with patch.object(dialogue, "_apply_paper_scoring_decision", side_effect=crash_after_derived_state):
        with pytest.raises(RuntimeError, match="injected crash"):
            dialogue.reconcile_pending_scoring()
    assert scoring.for_candidate(request["candidate_id"], "child")["decision_id"] is None
    with database.connect() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM paper_correct_revision_target AS target
               JOIN paper_correct_revision_obligation AS obligation USING(obligation_id)
               WHERE obligation.revision_id=?""",
            (correct["decision_id"],),
        ).fetchone()[0] == 0

    dialogue = MistakeDialogueService(database)
    assert dialogue.reconcile_pending_scoring() == [{
        "decision_id": correct["decision_id"],
        "outcome": "correct_verification_scheduled",
        "mistake_id": None,
        "phase": "scored_correct",
    }]
    assert scoring.for_candidate(request["candidate_id"], "child")["decision_id"] == correct["decision_id"]
    assert MistakeDialogueService(database).reconcile_pending_scoring() == []
    with database.connect() as connection:
        assert connection.execute(
            "SELECT status FROM mistake_case WHERE mistake_id=?", (mistake_id,),
        ).fetchone()[0] == "excluded"
        assert connection.execute(
            """SELECT COUNT(*) FROM paper_correct_revision_obligation AS obligation
               JOIN paper_correct_revision_event AS event USING(obligation_id)
               WHERE obligation.request_id=? AND event.status IN ('scheduled','pending_supply')""",
            (request["request_id"],),
        ).fetchone()[0] == 1

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：91",
    ))
    assert scoring.for_candidate(request["candidate_id"], "child")["decision_id"] is None
    with database.connect() as connection:
        revised_check = connection.execute(
            """SELECT obligation_id,source_evidence_id FROM paper_correct_revision_obligation
               WHERE revision_id=?""",
            (correct["decision_id"],),
        ).fetchone()
        assert revised_check is not None
        assert connection.execute(
            """SELECT status FROM paper_correct_revision_event
               WHERE obligation_id=? ORDER BY rowid DESC LIMIT 1""",
            (revised_check["obligation_id"],),
        ).fetchone()[0] == "withdrawn"
        assert connection.execute(
            "SELECT 1 FROM learning_evidence_withdrawal WHERE evidence_id=?",
            (revised_check["source_evidence_id"],),
        ).fetchone() is not None
    matches.process_next()
    wrong_again = PaperScoringSourceAuthorityService(database).process_next()
    assert wrong_again["correctness"] == 0
    original_confirm = dialogue.book.confirm_from_qualified_scoring

    def crash_after_wrong_mistake(*args, **kwargs):
        original_confirm(*args, **kwargs)
        raise RuntimeError("injected crash before wrong revision receipt")

    with patch.object(dialogue.book, "confirm_from_qualified_scoring", side_effect=crash_after_wrong_mistake):
        with pytest.raises(RuntimeError, match="injected crash"):
            dialogue.reconcile_pending_scoring()
    assert scoring.for_candidate(request["candidate_id"], "child")["decision_id"] is None
    from home_study_agent.mistake_book import MistakeBookService
    with database.connect() as connection:
        pending_link = connection.execute(
            "SELECT mistake_id FROM paper_scoring_revision_mistake_link WHERE revision_id=?",
            (wrong_again["decision_id"],),
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM mistake_case WHERE client_action_id LIKE ?",
            (f"paper-scoring-reconcile:{wrong_again['decision_id']}%",),
        ).fetchone()[0] == 0
    assert pending_link is None
    book = MistakeBookService(database)
    visible_ids = {item["mistake_id"] for item in book.list("child")["items"]}
    assert visible_ids == {mistake_id}
    current = book.current_for_session("study")
    assert current is None or current["mistake_id"] == mistake_id
    with database.connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_revision_receipt
               BEFORE INSERT ON paper_scoring_dialogue_revision_reconciliation
               BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END"""
        )
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        MistakeDialogueService(database).reconcile_pending_scoring()
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_revision_mistake_link WHERE revision_id=?",
            (wrong_again["decision_id"],),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM mistake_case WHERE client_action_id LIKE ?",
            (f"paper-scoring-reconcile:{wrong_again['decision_id']}%",),
        ).fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_revision_receipt")
        connection.commit()
    dialogue = MistakeDialogueService(database)
    assert dialogue.reconcile_pending_scoring()[0]["outcome"] == "mistake_created"
    with database.connect() as connection:
        assert connection.execute(
            "SELECT status FROM mistake_case WHERE mistake_id=?", (mistake_id,),
        ).fetchone()[0] == "excluded"
        linked = connection.execute(
            "SELECT mistake_id FROM paper_scoring_revision_mistake_link WHERE revision_id=?",
            (wrong_again["decision_id"],),
        ).fetchone()
        assert linked is not None and linked["mistake_id"] != mistake_id
        assert connection.execute(
            "SELECT status FROM mistake_case WHERE mistake_id=?",
            (linked["mistake_id"],),
        ).fetchone()[0] != "excluded"
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_revision_mistake_link WHERE revision_id=?",
            (wrong_again["decision_id"],),
        ).fetchone()[0] == 1
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(database, tmp_path)
    exported = json.loads(Path(privacy.export_child("child")["path"]).read_text(encoding="utf-8"))
    assert len(exported["paper_scoring_revision_mistake_links"]) == 1
    assert exported["paper_scoring_revision_mistake_links"][0]["revision_id"] == wrong_again["decision_id"]
    privacy.delete_child_scope("child", "all_history", "确认删除", "delete-revised-dialogue")
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_revision_mistake_link WHERE child_id='child'"
        ).fetchone()[0] == 0


def test_corrected_answer_page_replaces_correct_check_with_wrong_mistake(
    tmp_path: Path,
) -> None:
    from home_study_agent.knowledge_graph.textbook_page_service import (
        TextbookOcrPageInput, TextbookOcrPageService,
    )

    database, scoring, request = pending_dialogue(tmp_path)
    draft = PaperScoringAuthorityDraftService(
        database, ObjectiveDraftGateway(), StudyRepository(database), lambda: True,
    )
    assert asyncio.run(draft.process_next())["status"] == "completed"
    page = dict(
        title="初三数学练习册", material_kind="workbook", grade_scope="初三",
        subject_scope="数学", source_ref="local_parent_textbook_capture:correct-to-wrong",
        ocr_confidence=.95,
    )
    pages = TextbookOcrPageService(database)
    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：80",
    ))
    matches = PaperScoringSourceMatchService(database)
    matches.process_next()
    correct = PaperScoringSourceAuthorityService(database).process_next()
    dialogue = MistakeDialogueService(database)
    assert dialogue.reconcile_pending_scoring()[0]["outcome"] == "correct_verification_scheduled"

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：90",
    ))
    assert scoring.for_candidate(request["candidate_id"], "child")["decision_id"] is None
    matches.process_next()
    wrong = PaperScoringSourceAuthorityService(database).process_next()
    assert wrong["correctness"] == 0
    assert dialogue.reconcile_pending_scoring()[0]["outcome"] == "mistake_created"
    with database.connect() as connection:
        obligation = connection.execute(
            "SELECT obligation_id FROM paper_correct_verification_obligation WHERE request_id=?",
            (request["request_id"],),
        ).fetchone()
        assert obligation is not None
        assert connection.execute(
            """SELECT status FROM paper_correct_verification_event
               WHERE obligation_id=? ORDER BY rowid DESC LIMIT 1""",
            (obligation["obligation_id"],),
        ).fetchone()[0] == "withdrawn"
        assert connection.execute(
            "SELECT status FROM mistake_case WHERE mistake_id=(SELECT mistake_id FROM mistake_dialogue WHERE session_id='study')",
        ).fetchone()[0] != "excluded"
    assert correct["decision_id"] != wrong["decision_id"]

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：80",
    ))
    assert scoring.for_candidate(request["candidate_id"], "child")["decision_id"] is None
    matches.process_next()
    corrected_again = PaperScoringSourceAuthorityService(database).process_next()
    assert corrected_again["correctness"] == 1
    assert dialogue.reconcile_pending_scoring()[0]["outcome"] == "correct_verification_scheduled"
    with database.connect() as connection:
        obligation = connection.execute(
            "SELECT obligation_id FROM paper_correct_revision_obligation WHERE revision_id=?",
            (corrected_again["decision_id"],),
        ).fetchone()
        assert obligation is not None
        assert connection.execute(
            """SELECT status FROM paper_correct_revision_event
               WHERE obligation_id=? ORDER BY rowid DESC LIMIT 1""",
            (obligation["obligation_id"],),
        ).fetchone()[0] in {"pending_supply", "scheduled"}


def test_visual_answer_ledger_uses_only_current_revision_mistake(tmp_path: Path) -> None:
    from home_study_agent.knowledge_graph.textbook_page_service import (
        TextbookOcrPageInput, TextbookOcrPageService,
    )

    database, monitor = setup_case(tmp_path)
    with database.connect() as connection:
        artifact = connection.execute(
            "SELECT details_json FROM capture_artifact WHERE artifact_id='page'"
        ).fetchone()
        details = json.loads(artifact["details_json"])
        details["cloud_vision_fallback"]["blocks"] = [
            block for block in details["cloud_vision_fallback"]["blocks"]
            if block["source"] != "grading_mark"
        ]
        details["local_pipeline"]["wrong_question_candidates"][0]["mark_kind"] = "selection_mark"
        connection.execute(
            "UPDATE capture_artifact SET details_json=? WHERE artifact_id='page'",
            (json.dumps(details, ensure_ascii=False),),
        )
        connection.commit()
    monitor.record_revision("study", "page", "page-signature", "visual-revision")
    with database.connect() as connection:
        candidate = connection.execute(
            "SELECT candidate_id,question_text FROM mistake_capture_candidate"
        ).fetchone()
        evidence = connection.execute(
            "SELECT visual_answer_evidence_id FROM observed_answer_evidence"
        ).fetchone()
    scoring = PaperScoringService(database)
    request = scoring.request(
        candidate_id=candidate["candidate_id"], child_id="child", session_id="study",
        question_text=candidate["question_text"], answer_text="80",
        answer_source="visual_unverified",
        visual_answer_evidence_id=evidence["visual_answer_evidence_id"],
        fact_source="qualified_visual_observation", fact_confidence=.96,
        client_action_id="visual-scoring-request",
    )
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO mistake_dialogue(
                 session_id,child_id,candidate_id,mistake_id,phase,state_json,updated_at
               ) VALUES('study','child',?,NULL,'scoring_pending',?,?)""",
            (request["candidate_id"], json.dumps({
                "artifact_id": "page", "question_text": request["question_text"],
                "submitted_answer": "80", "answer_source": "visual_unverified",
                "text_confirmed": True, "subject": "数学",
                "knowledge_node_id": "node-p4-math-arithmetic",
                "knowledge_title": "整数四则运算顺序",
                "scoring_request_id": request["request_id"],
                "scoring_request_status": "pending_authority",
            }, ensure_ascii=False), utc_now()),
        )
        connection.commit()
    draft = PaperScoringAuthorityDraftService(
        database, ObjectiveDraftGateway(), StudyRepository(database), lambda: True,
    )
    assert asyncio.run(draft.process_next())["status"] == "completed"
    page = dict(
        title="数学练习册", material_kind="workbook", grade_scope="初三",
        subject_scope="数学", source_ref="local_parent_textbook_capture:ledger-revision",
        ocr_confidence=.95,
    )
    pages = TextbookOcrPageService(database)
    matcher = PaperScoringSourceMatchService(database)
    source = PaperScoringSourceAuthorityService(database)

    def regrade(answer: str) -> dict:
        pages.import_page(TextbookOcrPageInput(
            **page, ocr_text=f"{request['question_text']} 参考答案：{answer}",
        ))
        current = scoring.for_candidate(request["candidate_id"], "child")
        if answer != "90":
            assert current["decision_id"] is None
            pending_ledger = AnswerAttemptLedger(database).list_for_child("child")[0]
            assert pending_ledger["correctness"] is None
            assert pending_ledger["promoted_mistake_id"] is None
        assert matcher.process_next()["status"] == "candidate"
        decision = source.process_next()
        assert MistakeDialogueService(database).reconcile_pending_scoring()
        return decision

    first = regrade("90")
    assert first["correctness"] == 0
    first_ledger = AnswerAttemptLedger(database).list_for_child("child")[0]
    assert first_ledger["correctness"] == 0
    assert first_ledger["promoted_mistake_id"]

    corrected = regrade("80")
    assert corrected["correctness"] == 1
    corrected_ledger = AnswerAttemptLedger(database).list_for_child("child")[0]
    assert corrected_ledger["correctness"] == 1
    assert corrected_ledger["promoted_mistake_id"] is None
    assert corrected_ledger["promotion_status"] is None
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    with TestClient(create_app(Settings(
        data_dir=tmp_path, database_path=database.path,
        data_encryption_key=b"0" * 32, maintenance_enabled=False, runtime_mode=database.runtime_mode,
    ))) as client:
        token = client.post("/v1/parent/setup", json={"pin": "7391"}).json()["parent_token"]
        response = client.get(
            "/v1/parent/children/child/answer-attempt-ledger",
            headers={"X-HSC-Parent": token},
        )
        assert response.status_code == 200
        http_ledger = response.json()["items"][0]
        assert http_ledger["correctness"] == 1
        assert http_ledger["promoted_mistake_id"] is None

    wrong_again = regrade("91")
    assert wrong_again["correctness"] == 0
    revised_ledger = AnswerAttemptLedger(database).list_for_child("child")[0]
    assert revised_ledger["correctness"] == 0
    assert revised_ledger["promoted_mistake_id"]
    assert revised_ledger["promoted_mistake_id"] != first_ledger["promoted_mistake_id"]
    assert revised_ledger["promotion_status"] == "authority_confirmed_wrong"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO child_profile(
                 child_id,nickname,grade,curriculum_region,status,created_at,updated_at
               ) VALUES('other','另一名学生','四年级','江苏南京','active',?,?)""",
            (utc_now(), utc_now()),
        )
        connection.commit()
    assert AnswerAttemptLedger(database).list_for_child("other") == []
    assert scoring.for_candidate(request["candidate_id"], "other") is None
    with pytest.raises(ValueError, match="unknown paper scoring request"):
        scoring.resolve(request["request_id"], "other", "other-cannot-resolve")


def test_correct_revision_schedules_qualified_distinct_item_only_with_receipt(
    tmp_path: Path,
) -> None:
    from home_study_agent.knowledge_graph.textbook_page_service import (
        TextbookOcrPageInput, TextbookOcrPageService,
    )

    database, scoring, request = pending_dialogue(tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE child_profile SET grade='四年级' WHERE child_id='child'")
        row = connection.execute(
            "SELECT state_json FROM mistake_dialogue WHERE session_id='study'"
        ).fetchone()
        state = json.loads(row["state_json"])
        state["knowledge_node_id"] = "node-p4-math-arithmetic"
        state["knowledge_title"] = "整数四则运算顺序"
        connection.execute(
            "UPDATE mistake_dialogue SET state_json=? WHERE session_id='study'",
            (json.dumps(state, ensure_ascii=False),),
        )
        connection.commit()
    draft = PaperScoringAuthorityDraftService(
        database, ObjectiveDraftGateway(), StudyRepository(database), lambda: True,
    )
    assert asyncio.run(draft.process_next())["status"] == "completed"
    page = dict(
        title="四年级数学练习册", material_kind="workbook", grade_scope="四年级",
        subject_scope="数学", source_ref="local_parent_textbook_capture:qualified-review",
        ocr_confidence=.95,
    )
    pages = TextbookOcrPageService(database)
    matcher = PaperScoringSourceMatchService(database)
    source = PaperScoringSourceAuthorityService(database)
    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：90",
    ))
    assert matcher.process_next()["status"] == "candidate"
    assert source.process_next()["correctness"] == 0
    assert MistakeDialogueService(database).reconcile_pending_scoring()[0]["outcome"] == "mistake_created"

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：80",
    ))
    assert matcher.process_next()["status"] == "candidate"
    correct = source.process_next()
    assert correct["correctness"] == 1
    with database.connect() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM paper_correct_revision_target AS target
               JOIN paper_correct_revision_obligation AS obligation USING(obligation_id)
               WHERE obligation.revision_id=?""",
            (correct["decision_id"],),
        ).fetchone()[0] == 0
    assert MistakeDialogueService(database).reconcile_pending_scoring()[0]["outcome"] == "correct_verification_scheduled"
    with database.connect() as connection:
        review = connection.execute(
            """SELECT review.review_task_id,review.diagnostic_item_id,review.status
               FROM paper_correct_revision_target AS target
               JOIN paper_correct_revision_obligation AS obligation USING(obligation_id)
               JOIN review_task AS review USING(review_task_id)
               WHERE obligation.revision_id=?""",
            (correct["decision_id"],),
        ).fetchone()
    assert review is not None
    assert review["status"] == "pending"
    assert review["diagnostic_item_id"] == "diag-p4-arithmetic-closed-2"
    from home_study_agent.learning_state import LearningStateService
    from home_study_agent.learning_obligations import project_mastery_evidence
    with database.connect() as connection:
        connection.execute(
            "UPDATE review_task SET due_at='2020-01-01T00:00:00+00:00' WHERE review_task_id=?",
            (review["review_task_id"],),
        )
        connection.commit()
    attempt = LearningStateService(database).submit_diagnostic(
        "study", review["diagnostic_item_id"], "42", "revision-distinct-answer",
        review_task_id=review["review_task_id"], hint_level=0,
    )
    assert attempt["correctness"] == 1
    with database.connect() as connection:
        projection = project_mastery_evidence(
            connection, "child", "node-p4-math-arithmetic", as_of=utc_now(),
        )
        assert attempt["evidence_id"] in {
            item["evidence_id"] for item in projection["effective_evidence"]
        }

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：91",
    ))
    assert scoring.for_candidate(request["candidate_id"], "child")["decision_id"] is None
    with database.connect() as connection:
        projection = project_mastery_evidence(
            connection, "child", "node-p4-math-arithmetic", as_of=utc_now(),
        )
        assert attempt["evidence_id"] not in {
            item["evidence_id"] for item in projection["effective_evidence"]
        }
        assert connection.execute(
            "SELECT evidence_count FROM mastery_state WHERE child_id='child' AND knowledge_node_id=?",
            ("node-p4-math-arithmetic",),
        ).fetchone()[0] == 0


def test_high_confidence_teacher_mark_scores_without_asking_child_to_confirm_correctness(
    tmp_path: Path,
) -> None:
    database, monitor = setup_case(tmp_path)
    with database.connect() as connection:
        artifact = connection.execute(
            "SELECT details_json FROM capture_artifact WHERE artifact_id='page'"
        ).fetchone()
        details = json.loads(artifact["details_json"])
        for block in details["cloud_vision_fallback"]["blocks"]:
            block["confidence"] = .96
        local = details["local_pipeline"]
        local["question_blocks"][0]["confidence"] = .96
        local["wrong_question_candidates"][0].update(
            confidence=.96,
            needs_manual_boundary_check=False,
            evidence="red_correction_mark_near_question",
        )
        connection.execute(
            "UPDATE capture_artifact SET details_json=?,confidence=.96 WHERE artifact_id='page'",
            (json.dumps(details, ensure_ascii=False),),
        )
        connection.commit()
    monitor.record_revision("study", "page", "teacher-mark-page", "teacher-mark-revision")

    result = MistakeDialogueService(database).begin("study", "page", "teacher-mark-begin")

    assert result["state"]["automatic_fact_source"] == "qualified_visual_observation"
    assert result["state"]["scoring_authority_type"] == "teacher_mark"
    assert result["state"]["scored_correctness"] == 0
    assert result["mistake_id"]
    assert result["phase"] != "candidate"
    with database.connect() as connection:
        request = connection.execute("SELECT * FROM paper_scoring_request").fetchone()
        promotion = connection.execute(
            "SELECT confirmation_source FROM visual_answer_evidence_promotion"
        ).fetchone()
        mistake = connection.execute(
            "SELECT status FROM mistake_case WHERE mistake_id=?", (result["mistake_id"],)
        ).fetchone()
    assert request["fact_source"] == "qualified_visual_observation"
    assert request["fact_confidence"] >= .90
    assert promotion["confirmation_source"] == "qualified_scoring_authority"
    assert mistake["status"] != "awaiting_confirmation"


def test_self_selected_exercise_uses_local_answer_page_and_enters_mistake_automatically(
    tmp_path: Path,
) -> None:
    database, monitor = setup_case(tmp_path)
    with database.connect() as connection:
        artifact = connection.execute(
            "SELECT details_json FROM capture_artifact WHERE artifact_id='page'"
        ).fetchone()
        details = json.loads(artifact["details_json"])
        blocks = details["cloud_vision_fallback"]["blocks"]
        details["cloud_vision_fallback"]["blocks"] = [
            {**block, "confidence": .96} for block in blocks
            if block["source"] != "grading_mark"
        ]
        details["local_pipeline"]["question_blocks"][0]["confidence"] = .96
        details["local_pipeline"]["wrong_question_candidates"][0].update(
            mark_kind="selection_mark",
            confidence=.96,
            needs_manual_boundary_check=False,
            evidence="visible_answer_mark",
        )
        connection.execute(
            "UPDATE capture_artifact SET details_json=?,confidence=.96 WHERE artifact_id='page'",
            (json.dumps(details, ensure_ascii=False),),
        )
        connection.commit()
    monitor.record_revision("study", "page", "self-practice-page", "self-practice-revision")
    dialogue = MistakeDialogueService(database)

    opened = dialogue.begin("study", "page", "self-practice-begin")

    assert opened["phase"] == "scoring_pending"
    assert opened["state"]["automatic_fact_source"] == "qualified_visual_observation"
    request_id = opened["state"]["scoring_request_id"]
    draft = PaperScoringAuthorityDraftService(
        database, ObjectiveDraftGateway(), StudyRepository(database), lambda: True,
    )
    assert asyncio.run(draft.process_next())["status"] == "completed"
    add_local_page(
        database,
        text=f"{opened['state']['question_text']} 参考答案：90",
    )
    assert PaperScoringSourceMatchService(database).process_next()["status"] == "candidate"
    qualified = PaperScoringSourceAuthorityService(database).process_next()
    assert qualified["status"] == "qualified"
    assert qualified["correctness"] == 0

    reconciled = dialogue.reconcile_pending_scoring()

    assert reconciled[0]["outcome"] == "mistake_created"
    with database.connect() as connection:
        request = connection.execute(
            "SELECT fact_source FROM paper_scoring_request WHERE request_id=?", (request_id,)
        ).fetchone()
        current = connection.execute(
            "SELECT mistake_id,phase FROM mistake_dialogue WHERE session_id='study'"
        ).fetchone()
    assert request["fact_source"] == "qualified_visual_observation"
    assert current["mistake_id"]
    assert current["phase"] != "scoring_pending"
