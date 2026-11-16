from __future__ import annotations

import asyncio
import json
from pathlib import Path

from home_study_agent.model_gateway import ModelResult
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_source_authority import PaperScoringSourceAuthorityService
from home_study_agent.paper_scoring_source_match import PaperScoringSourceMatchService
from home_study_agent.paper_scoring_supply import PaperScoringAuthorityDraftService
from home_study_agent.repository import StudyRepository

from test_paper_scoring_source_match import add_local_page
from test_paper_scoring_supply import pending_request


class ObjectiveDraftGateway:
    configured = True

    async def complete(self, request):
        payload = {
            "subject": "数学", "answer_mode": "objective",
            "task_points": ["核对最终整数答案"],
            "source_search_terms": ["计算整数", "参考答案"],
            "confidence": .96, "requires_external_source": True,
        }
        return ModelResult(json.dumps(payload, ensure_ascii=False), "fixture", "fixture", 1, 8, 8)


def prepared(tmp_path: Path, *, runtime_mode='trial'):
    database, _, request = pending_request(tmp_path, runtime_mode=runtime_mode)
    draft = PaperScoringAuthorityDraftService(
        database, ObjectiveDraftGateway(), StudyRepository(database), lambda: True,
    )
    assert asyncio.run(draft.process_next())["status"] == "completed"
    obligation_id = PaperScoringService(database).for_candidate(
        request["candidate_id"], "child"
    )["authority_supply"]["obligation_id"]
    return database, request, obligation_id


def test_explicit_exact_objective_answer_page_qualifies_and_scores(tmp_path: Path) -> None:
    database, request, obligation_id = prepared(tmp_path)
    add_local_page(
        database,
        text=f"{request['question_text']} 参考答案：90",
    )
    assert PaperScoringSourceMatchService(database).process_next()["status"] == "candidate"

    result = PaperScoringSourceAuthorityService(database).process_next()

    assert result["status"] == "qualified"
    assert result["correctness"] == 0
    current = PaperScoringService(database).for_candidate(request["candidate_id"], "child")
    assert current["authority_type"] == "published_answer_key"
    assert current["authority_supply"]["status"] == "qualified"
    assert current["mastery_update_allowed"] is False


def test_reimported_answer_page_withdraws_old_score_across_restart(tmp_path: Path) -> None:
    from home_study_agent.database import Database
    from home_study_agent.knowledge_graph.textbook_page_service import (
        TextbookOcrPageInput, TextbookOcrPageService,
    )

    database, request, _ = prepared(tmp_path)
    page = dict(
        title="初三数学练习册", material_kind="workbook", grade_scope="初三",
        subject_scope="数学", source_ref="local_parent_textbook_capture:answer-page",
        ocr_confidence=.95,
    )
    pages = TextbookOcrPageService(database)
    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：90",
    ))
    assert PaperScoringSourceMatchService(database).process_next()["status"] == "candidate"
    assert PaperScoringSourceAuthorityService(database).process_next()["status"] == "qualified"
    scored = PaperScoringService(database).for_candidate(request["candidate_id"], "child")
    assert scored["decision_id"] is not None

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：91",
    ))
    restarted = PaperScoringService(Database(database.path, runtime_mode=database.runtime_mode))
    current = restarted.for_candidate(request["candidate_id"], "child")
    assert current["decision_id"] is None
    assert current["effective_status"] != "scored"
    assert current["authority_supply"]["status"] == "pending_authority"
    assert restarted.resolve(request["request_id"], "child", "recheck-after-reimport") is None


def test_reimported_answer_page_regrades_original_answer_once(tmp_path: Path) -> None:
    from home_study_agent.knowledge_graph.textbook_page_service import (
        TextbookOcrPageInput, TextbookOcrPageService,
    )

    database, request, obligation_id = prepared(tmp_path)
    page = dict(
        title="初三数学练习册", material_kind="workbook", grade_scope="初三",
        subject_scope="数学", source_ref="local_parent_textbook_capture:corrected-answer",
        ocr_confidence=.95,
    )
    pages = TextbookOcrPageService(database)
    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：90",
    ))
    matches = PaperScoringSourceMatchService(database)
    assert matches.process_next()["status"] == "candidate"
    first = PaperScoringSourceAuthorityService(database).process_next()
    assert first["correctness"] == 0
    first_decision_id = first["decision_id"]

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：80",
    ))
    assert PaperScoringService(database).for_candidate(request["candidate_id"], "child")["decision_id"] is None
    assert matches.process_next()["status"] == "candidate"
    second = PaperScoringSourceAuthorityService(database).process_next()
    assert second is not None and second["correctness"] == 1
    current = PaperScoringService(database).for_candidate(request["candidate_id"], "child")
    assert current["resolved_correctness"] == 1
    assert current["authority_supply"]["status"] == "qualified"
    assert current["decision_id"] != first_decision_id
    assert len(matches.valid_candidates(obligation_id)) == 1

    repeated = PaperScoringService(database).resolve(
        request["request_id"], "child", "same-correction-replayed",
    )
    assert repeated["decision_id"] == second["decision_id"]
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_decision_revision WHERE request_id=?",
            (request["request_id"],),
        ).fetchone()[0] == 1

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：91",
    ))
    assert PaperScoringService(database).for_candidate(
        request["candidate_id"], "child",
    )["decision_id"] is None
    assert matches.process_next()["status"] == "candidate"
    third = PaperScoringSourceAuthorityService(database).process_next()
    assert third is not None and third["correctness"] == 0
    assert third["decision_id"] not in {first_decision_id, second["decision_id"]}
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_decision_revision WHERE request_id=?",
            (request["request_id"],),
        ).fetchone()[0] == 2
    from home_study_agent.database import Database
    after_restart = PaperScoringService(Database(database.path, runtime_mode=database.runtime_mode)).for_candidate(
        request["candidate_id"], "child",
    )
    assert after_restart["decision_id"] == third["decision_id"]
    assert after_restart["resolved_correctness"] == 0
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(database, tmp_path)
    export = privacy.export_child("child")
    exported = json.loads(Path(export["path"]).read_text(encoding="utf-8"))
    assert len(exported["paper_scoring_decision_revisions"]) == 2
    assert len(exported["paper_scoring_revision_status_events"]) == 3
    privacy.delete_child_scope("child", "all_history", "确认删除", "delete-revised-paper")
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_decision_revision WHERE child_id='child'"
        ).fetchone()[0] == 0


def test_answer_page_quality_downgrade_withdraws_existing_score(tmp_path: Path) -> None:
    database, request, _ = prepared(tmp_path)
    resource_id, _ = add_local_page(
        database, text=f"{request['question_text']} 参考答案：90",
    )
    assert PaperScoringSourceMatchService(database).process_next()["status"] == "candidate"
    assert PaperScoringSourceAuthorityService(database).process_next()["status"] == "qualified"

    with database.connect() as connection:
        connection.execute(
            "UPDATE kg_document_page SET ocr_confidence=.55 WHERE resource_id=?",
            (resource_id,),
        )
        connection.commit()

    current = PaperScoringService(database).for_candidate(request["candidate_id"], "child")
    assert current["decision_id"] is None
    assert current["authority_supply"]["status"] == "pending_authority"


def test_identical_answer_page_rescan_after_quality_downgrade_requalifies(tmp_path: Path) -> None:
    from home_study_agent.knowledge_graph.textbook_page_service import (
        TextbookOcrPageInput, TextbookOcrPageService,
    )

    database, request, _ = prepared(tmp_path)
    page = dict(
        title="初三数学练习册", material_kind="workbook", grade_scope="初三",
        subject_scope="数学", source_ref="local_parent_textbook_capture:rescan",
        ocr_text=f"{request['question_text']} 参考答案：90",
    )
    pages = TextbookOcrPageService(database)
    pages.import_page(TextbookOcrPageInput(**page, ocr_confidence=.95))
    matcher = PaperScoringSourceMatchService(database)
    assert matcher.process_next()["status"] == "candidate"
    first = PaperScoringSourceAuthorityService(database).process_next()
    assert first["status"] == "qualified"

    pages.import_page(TextbookOcrPageInput(**page, ocr_confidence=.55))
    assert PaperScoringService(database).for_candidate(request["candidate_id"], "child")["decision_id"] is None
    pages.import_page(TextbookOcrPageInput(**page, ocr_confidence=.95))
    assert matcher.process_next()["status"] == "candidate"
    restored = PaperScoringSourceAuthorityService(database).process_next()
    assert restored is not None and restored["status"] == "qualified"
    assert restored["decision_id"] != first["decision_id"]


def test_answer_page_without_exact_question_binding_never_qualifies(tmp_path: Path) -> None:
    database, request, _ = prepared(tmp_path)
    add_local_page(database, text="第2题 参考答案：90")
    assert PaperScoringSourceMatchService(database).process_next()["status"] == "candidate"

    result = PaperScoringSourceAuthorityService(database).process_next()

    assert result["status"] == "source_insufficient"
    assert result["reason_code"] == "EXACT_QUESTION_NOT_BOUND_TO_SOURCE"
    assert PaperScoringService(database).for_candidate(
        request["candidate_id"], "child"
    )["decision_id"] is None


def test_ambiguous_multiple_answers_never_qualify(tmp_path: Path) -> None:
    database, request, _ = prepared(tmp_path)
    add_local_page(
        database,
        text=f"{request['question_text']} 参考答案：90。另一题参考答案：80",
    )
    assert PaperScoringSourceMatchService(database).process_next()["status"] == "candidate"
    result = PaperScoringSourceAuthorityService(database).process_next()
    assert result["status"] == "source_insufficient"
    assert result["reason_code"] == "ANSWER_KEY_BLOCK_AMBIGUOUS"


def test_subjective_draft_routes_to_calibration_without_scoring(tmp_path: Path) -> None:
    from test_paper_scoring_authority_draft import DraftGateway, valid_payload

    database, _, request = pending_request(tmp_path)
    draft = PaperScoringAuthorityDraftService(
        database, DraftGateway(valid_payload()), StudyRepository(database), lambda: True,
    )
    asyncio.run(draft.process_next())
    add_local_page(database, text=f"{request['question_text']} 参考答案：90")
    PaperScoringSourceMatchService(database).process_next()

    result = PaperScoringSourceAuthorityService(database).process_next()

    assert result["status"] == "calibration_pending"
    current = PaperScoringService(database).for_candidate(request["candidate_id"], "child")
    assert current["decision_id"] is None
    assert current["authority_supply"]["status"] == "calibration_pending"


def test_multiple_choice_answer_is_one_combination_not_separate_alternatives(tmp_path: Path) -> None:
    database, request, _ = prepared(tmp_path)
    add_local_page(database, text=f"{request['question_text']} 参考答案：B、D")
    PaperScoringSourceMatchService(database).process_next()

    result = PaperScoringSourceAuthorityService(database).process_next()

    assert result["status"] == "qualified"
    with database.connect() as connection:
        authority = connection.execute(
            "SELECT expected_answers_json FROM paper_answer_authority ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert json.loads(authority["expected_answers_json"]) == ["BD"]
