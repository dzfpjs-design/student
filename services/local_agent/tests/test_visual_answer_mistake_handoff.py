from __future__ import annotations

import json
from pathlib import Path

from home_study_agent.answer_attempt_ledger import (
    AnswerAttemptLedger,
    ensure_visual_promotion_schema,
)
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService

from test_answer_attempt_ledger import capture_visible_attempt, setup_case


QUESTION = "2. 计算 (24+6)×3，只回答一个整数。"


def test_optional_review_store_missing_does_not_create_dialogue_authority(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    with database.connect() as connection:
        for table in ('ocr_review_event', 'ocr_review_batch', 'ocr_review_item'):
            connection.execute('DROP TABLE ' + table)
        connection.commit()
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.begin('study', 'page', 'missing-review-begin')
    result = dialogue.respond('study', '对', 'missing-review-score')
    assert result['phase'] == 'diagnosis'
    assert result['state']['scoring_authority_type'] == 'teacher_mark'
    assert result.get('dialogue_binding') is None
    with database.connect() as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='ocr_review_item'"
        ).fetchone()
        assert connection.execute('SELECT COUNT(*) FROM mistake_case').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 1


def make_teacher_mark_unresolved(database) -> None:
    with database.connect() as connection:
        details = json.loads(connection.execute(
            "SELECT details_json FROM capture_artifact WHERE artifact_id='page'"
        ).fetchone()[0])
        details["local_pipeline"]["wrong_question_candidates"][0]["mark_kind"] = "unclear_mark"
        details["cloud_vision_fallback"]["blocks"] = [
            block for block in details["cloud_vision_fallback"]["blocks"]
            if block.get("source") != "grading_mark"
        ]
        connection.execute(
            "UPDATE capture_artifact SET details_json=? WHERE artifact_id='page'",
            (json.dumps(details, ensure_ascii=False),),
        )
        connection.commit()


def make_teacher_mark_correct(database) -> None:
    with database.connect() as connection:
        details = json.loads(connection.execute(
            "SELECT details_json FROM capture_artifact WHERE artifact_id='page'"
        ).fetchone()[0])
        details["local_pipeline"]["wrong_question_candidates"][0]["mark_kind"] = "check_like"
        details["local_pipeline"]["wrong_question_candidates"][0]["evidence"] = "visible_teacher_mark"
        details["cloud_vision_fallback"]["blocks"][2]["text"] = "check"
        connection.execute(
            "UPDATE capture_artifact SET details_json=? WHERE artifact_id='page'",
            (json.dumps(details, ensure_ascii=False),),
        )
        connection.commit()


def test_teacher_marked_wrong_answer_enters_mistake_loop_after_readback_confirmation(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)

    started = dialogue.begin("study", "page", "visual-handoff-begin")
    assert started["phase"] == "candidate"
    assert started["state"]["submitted_answer"] == "80"
    assert started["state"]["answer_source"] == "visual_unverified"
    assert "80" in started["response_text"]
    assert "还没判定" in started["response_text"]

    confirmed = dialogue.respond("study", "对", "visual-handoff-confirm")
    assert confirmed["mistake_id"]
    assert confirmed["state"]["submitted_answer"] == "80"
    assert confirmed["phase"] == "diagnosis"
    assert confirmed["state"]["scoring_authority_type"] == "teacher_mark"
    assert "老师的批改标记" in confirmed["response_text"]
    assert "加入错题闭环" in confirmed["response_text"]

    with database.connect() as connection:
        request = connection.execute("SELECT * FROM paper_scoring_request").fetchone()
        decision = connection.execute("SELECT * FROM paper_scoring_decision").fetchone()
        visual = connection.execute("SELECT * FROM observed_answer_evidence").fetchone()
        assert request["visual_answer_evidence_id"] == visual["visual_answer_evidence_id"]
        assert request["answer_text"] == "80"
        assert request["status"] == "pending_authority"
        assert decision["authority_type"] == "teacher_mark"
        assert decision["correctness"] == 0
        assert visual["correctness"] is None
        assert connection.execute("SELECT COUNT(*) FROM mistake_case").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM visual_answer_evidence_promotion").fetchone()[0] == 1
        promotion = connection.execute(
            "SELECT confirmation_source FROM visual_answer_evidence_promotion"
        ).fetchone()
        assert promotion["confirmation_source"] == "qualified_scoring_authority"
        assert connection.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0] == 0

    ledger = AnswerAttemptLedger(database).list_for_child("child")
    assert ledger[0]["promoted_mistake_id"] == confirmed["mistake_id"]
    assert ledger[0]["scoring_request_status"] == "scored"
    assert ledger[0]["scoring_authority_type"] == "teacher_mark"
    assert ledger[0]["correctness"] == 0
    assert ledger[0]["promotion_status"] == "authority_confirmed_wrong"
    assert ledger[0]["mastery_update_allowed"] is False

    replay = dialogue.respond("study", "对", "visual-handoff-confirm")
    assert replay["state"]["scoring_request_id"] == confirmed["state"]["scoring_request_id"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mistake_case").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM paper_scoring_request").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM paper_scoring_decision").fetchone()[0] == 1

    restarted_dialogue = MistakeDialogueService(Database(database.path, runtime_mode=database.runtime_mode)).state("study")
    assert restarted_dialogue["phase"] == "diagnosis"
    restarted = AnswerAttemptLedger(Database(database.path, runtime_mode=database.runtime_mode)).list_for_child("child")
    assert restarted[0]["scoring_request_status"] == "scored"
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child("child")["path"]).read_text(encoding="utf-8"))
    assert exported["paper_scoring_requests"][0]["request_id"] == confirmed["state"]["scoring_request_id"]
    assert exported["paper_scoring_decisions"][0]["authority_type"] == "teacher_mark"

    with database.connect() as connection:
        connection.execute("DELETE FROM capture_artifact WHERE artifact_id='page'")
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_request"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM paper_scoring_decision").fetchone()[0] == 0


def test_legacy_child_confirmation_promotion_migrates_without_losing_history(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.begin("study", "page", "legacy-promotion-begin")
    dialogue.respond("study", "对", "legacy-promotion-score")

    with database.connect() as connection:
        saved = dict(connection.execute(
            "SELECT * FROM visual_answer_evidence_promotion"
        ).fetchone())
        connection.executescript(
            """
            DROP TRIGGER visual_answer_evidence_promotion_no_update;
            DROP INDEX idx_visual_answer_promotion_child;
            DROP TABLE visual_answer_evidence_promotion;
            CREATE TABLE visual_answer_evidence_promotion (
              promotion_id TEXT PRIMARY KEY,
              visual_answer_evidence_id TEXT NOT NULL UNIQUE
                REFERENCES observed_answer_evidence(visual_answer_evidence_id) ON DELETE CASCADE,
              candidate_id TEXT NOT NULL
                REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
              mistake_id TEXT NOT NULL
                REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              confirmed_answer TEXT NOT NULL,
              confirmation_source TEXT NOT NULL
                CHECK(confirmation_source='child_spoken_confirmation'),
              created_at TEXT NOT NULL,
              client_action_id TEXT NOT NULL UNIQUE
            );
            """
        )
        connection.execute(
            """INSERT INTO visual_answer_evidence_promotion VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                saved["promotion_id"], saved["visual_answer_evidence_id"],
                saved["candidate_id"], saved["mistake_id"], saved["child_id"],
                saved["confirmed_answer"], "child_spoken_confirmation",
                saved["created_at"], saved["client_action_id"],
            ),
        )
        ensure_visual_promotion_schema(connection)
        connection.commit()
        migrated = connection.execute(
            "SELECT * FROM visual_answer_evidence_promotion"
        ).fetchone()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='visual_answer_evidence_promotion'"
        ).fetchone()["sql"]

    assert migrated["promotion_id"] == saved["promotion_id"]
    assert migrated["confirmation_source"] == "child_spoken_confirmation"
    assert "qualified_scoring_authority" in schema
    ledger = AnswerAttemptLedger(Database(database.path, runtime_mode=database.runtime_mode)).list_for_child("child")
    assert ledger[0]["promotion_status"] == "legacy_child_confirmation_then_scored_wrong"


def test_low_confidence_visual_answer_is_not_prefilled(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    with database.connect() as connection:
        details = json.loads(connection.execute(
            "SELECT details_json FROM capture_artifact WHERE artifact_id='page'"
        ).fetchone()[0])
        details["cloud_vision_fallback"]["blocks"][1]["confidence"] = .4
        connection.execute(
            "UPDATE capture_artifact SET details_json=? WHERE artifact_id='page'",
            (json.dumps(details, ensure_ascii=False),),
        )
        connection.commit()
    capture_visible_attempt(database, monitor)

    started = MistakeDialogueService(database).begin("study", "page", "low-confidence-begin")
    assert started["state"]["submitted_answer"] == ""
    assert started["state"].get("answer_source") is None
    assert "80" not in started["response_text"]


def test_visual_answer_is_retained_when_knowledge_match_needs_clarification(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.book.knowledge_candidates = lambda *_args, **_kwargs: {"items": []}

    started = dialogue.begin("study", "page", "visual-no-knowledge-begin")
    assert started["state"]["submitted_answer"] == "80"

    confirmed = dialogue.respond("study", "对", "visual-no-knowledge-confirm")
    assert confirmed["phase"] == "scored_incorrect_unmapped"
    assert confirmed["mistake_id"] is None
    assert confirmed["state"]["submitted_answer"] == "80"
    assert "老师的批改标记" in confirmed["response_text"]
    assert "原答案是什么" not in confirmed["response_text"]


def test_child_correctness_claim_does_not_become_a_score_or_mistake(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.begin("study", "page", "visual-claim-begin")

    result = dialogue.respond("study", "确实是我做错了", "visual-claim-wrong")
    assert result["phase"] == "candidate"
    assert result["mistake_id"] is None
    assert "只确认我有没有读错" in result["response_text"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_scoring_request").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM mistake_case").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0] == 0


def test_child_correctness_dispute_cannot_remove_an_authority_scored_mistake(
    tmp_path: Path,
) -> None:
    database, monitor = setup_case(tmp_path)
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.begin("study", "page", "authority-dispute-begin")
    scored = dialogue.respond("study", "对", "authority-dispute-fact-readback")
    assert scored["mistake_id"]
    assert scored["state"]["scoring_authority_type"] == "teacher_mark"

    disputed = dialogue.respond("study", "这不是错题", "authority-dispute-claim")

    assert disputed["mistake_id"] == scored["mistake_id"]
    assert disputed["phase"] != "closed"
    assert disputed["state"]["child_scoring_dispute"]["requires_authority_recheck"] is True
    assert "不会只凭这句话删除错题" in disputed["response_text"]
    with database.connect() as connection:
        mistake = connection.execute(
            "SELECT status FROM mistake_case WHERE mistake_id=?", (scored["mistake_id"],)
        ).fetchone()
        assert mistake["status"] != "excluded"
        assert connection.execute("SELECT COUNT(*) FROM paper_scoring_decision").fetchone()[0] == 1


def test_unresolved_mark_stays_pending_and_never_becomes_a_mistake(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    make_teacher_mark_unresolved(database)
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.begin("study", "page", "unresolved-begin")

    result = dialogue.respond("study", "对", "unresolved-confirm")
    assert result["phase"] == "scoring_pending"
    assert result["mistake_id"] is None
    assert "老师批改、标准答案或出题时保存的评分规则" in result["response_text"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_scoring_request").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM paper_scoring_decision").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM mistake_case").fetchone()[0] == 0


def test_teacher_check_scores_correct_without_creating_mistake(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    make_teacher_mark_correct(database)
    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.begin("study", "page", "teacher-check-begin")

    result = dialogue.respond("study", "对", "teacher-check-confirm")
    assert result["phase"] == "scored_correct"
    assert result["mistake_id"] is None
    assert result["state"]["scoring_authority_type"] == "teacher_mark"
    with database.connect() as connection:
        decision = connection.execute("SELECT * FROM paper_scoring_decision").fetchone()
        assert decision["correctness"] == 1
        assert connection.execute("SELECT COUNT(*) FROM mistake_case").fetchone()[0] == 0


def test_published_answer_key_scores_self_selected_exercise(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    make_teacher_mark_unresolved(database)
    capture_visible_attempt(database, monitor)
    PaperScoringService(database).register_exact_authority(
        child_id="child", session_id="study", question_text=QUESTION,
        expected_answers=["90"], authority_type="published_answer_key",
        authority_ref="workbook-1:answer-page-12:q2", rubric_version="edition-2026-v1",
        client_action_id="register-published-answer",
    )
    dialogue = MistakeDialogueService(database)
    dialogue.begin("study", "page", "published-begin")

    result = dialogue.respond("study", "对", "published-confirm")
    assert result["mistake_id"]
    assert result["state"]["scoring_authority_type"] == "published_answer_key"
    assert "标准答案" in result["response_text"]
    with database.connect() as connection:
        decision = connection.execute("SELECT * FROM paper_scoring_decision").fetchone()
        assert decision["correctness"] == 0
        assert decision["authority_ref"] == "workbook-1:answer-page-12:q2"


def test_agent_generated_rubric_scores_known_correct_answer_without_mistake(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    make_teacher_mark_unresolved(database)
    capture_visible_attempt(database, monitor)
    PaperScoringService(database).register_exact_authority(
        child_id="child", session_id="study", question_text=QUESTION,
        expected_answers=["80"], authority_type="agent_generated_rubric",
        authority_ref="generated-exercise:turn-1:item-2", rubric_version="rubric-v1",
        client_action_id="register-agent-rubric",
    )
    dialogue = MistakeDialogueService(database)
    dialogue.begin("study", "page", "agent-generated-begin")

    result = dialogue.respond("study", "对", "agent-generated-confirm")
    assert result["phase"] == "scored_correct"
    assert result["mistake_id"] is None
    assert result["state"]["scoring_authority_type"] == "agent_generated_rubric"
    assert "作答正确" in result["response_text"]
    with database.connect() as connection:
        assert connection.execute("SELECT correctness FROM paper_scoring_decision").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM mistake_case").fetchone()[0] == 0


def test_presented_agent_item_freezes_answer_authority_without_manual_registration(tmp_path) -> None:
    database, monitor = setup_case(tmp_path)
    make_teacher_mark_unresolved(database)
    with database.connect() as connection:
        connection.execute("UPDATE child_profile SET grade='四年级' WHERE child_id='child'")
        connection.execute(
            """INSERT INTO diagnostic_item(
                 diagnostic_item_id,knowledge_node_id,prompt,answer_type,expected_answer_json,
                 explanation,stimulus_text,stimulus_asset,assessment_mode,difficulty,source_ref,status
               ) SELECT 'paper-auto-authority',knowledge_node_id,?,'text',?,
                        '按运算顺序计算',NULL,NULL,'objective',difficulty,'hsc_original_v1','active'
                   FROM diagnostic_item WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'""",
            (QUESTION, json.dumps({"local_rule": "local_exact_v1", "exact_answers": ["80"]})),
        )
        connection.commit()

    prompt = DiagnosticPromptService(database).prepare(
        "study", preferred_item_id="paper-auto-authority", track="school",
        client_action_id="present-paper-auto-authority",
    )
    assert prompt["status"] == "waiting_answer"
    with database.connect() as connection:
        authority = connection.execute("SELECT * FROM paper_answer_authority").fetchone()
        assert authority["authority_type"] == "agent_generated_rubric"
        assert authority["question_text"] == QUESTION
        assert json.loads(authority["expected_answers_json"]) == ["80"]
        assert prompt["prompt"]["prompt_id"] in authority["authority_ref"]

    capture_visible_attempt(database, monitor)
    dialogue = MistakeDialogueService(database)
    dialogue.begin("study", "page", "auto-authority-begin")
    result = dialogue.respond("study", "对", "auto-authority-confirm")
    assert result["phase"] == "scored_correct"
    assert result["mistake_id"] is None
    assert result["state"]["scoring_authority_type"] == "agent_generated_rubric"
