from __future__ import annotations

import asyncio
import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.schema import ensure_schema as ensure_kg_schema
from home_study_agent.model_gateway import ModelResult
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_source_match import PaperScoringSourceMatchService
from home_study_agent.paper_scoring_supply import PaperScoringAuthorityDraftService
from home_study_agent.repository import StudyRepository, utc_now

from test_paper_scoring_authority_draft import pending_request, valid_payload


class DraftGateway:
    configured = True

    async def complete(self, request):
        return ModelResult(
            json.dumps(valid_payload(), ensure_ascii=False), "fixture", "fixture", 1, 8, 8
        )


def add_local_page(database, *, text: str, grade: str = "初三", subject: str = "数学"):
    import hashlib
    from uuid import uuid4

    now = utc_now()
    resource_id = "resource-" + uuid4().hex
    segment_id = "segment-" + uuid4().hex
    original = text.encode("utf-8")
    text_hash = hashlib.sha256(original).hexdigest()
    transcript_root = database.path.parent.resolve(strict=True) / "registered-paper-transcripts"
    transcript_root.mkdir(mode=0o700, exist_ok=True)
    with (transcript_root / (resource_id + ".txt")).open("xb") as stream:
        stream.write(original)
    with database.connect() as connection:
        ensure_kg_schema(connection, now)
        connection.execute(
            """INSERT INTO kg_corpus(
                 corpus_id,title,source_kind,transcript_root,source_ref,license_boundary,
                 review_status,version_label,created_at,updated_at
               ) VALUES('test-paper-source','家庭习题册','local_scanned_document',?,
                        'parent-scan','user_local','unreviewed','v1',?,?)
               ON CONFLICT(corpus_id) DO NOTHING""",
            (str(transcript_root), now, now),
        )
        connection.execute(
            """INSERT INTO kg_resource(
                 resource_id,corpus_id,external_id,title,grade_scope,subject_scope,
                 transcript_relative_path,transcript_sha256,metadata_json,source_ref,license_boundary,confidence,
                 review_status,version_label,ingested_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,'{}','parent-scan','user_local',.9,
                        'unreviewed','v1',?,?)""",
            (resource_id, "test-paper-source", resource_id, "练习册参考答案",
             grade, subject, resource_id + ".txt", text_hash, now, now),
        )
        connection.execute(
            """INSERT INTO kg_document_page(
                 page_id,resource_id,document_kind,page_number,ocr_engine,ocr_confidence,
                 source_ref,license_boundary,version_label,created_at,updated_at
               ) VALUES(?,?,'purchased_practice_book',1,'fixture',.95,
                        'parent-scan','user_local','v1',?,?)""",
            (str(uuid4()), resource_id, now, now),
        )
        connection.execute(
            """INSERT INTO kg_segment(
                 segment_id,resource_id,ordinal,start_char,end_char,text,text_sha256,
                 source_ref,confidence,review_status,created_at
               ) VALUES(?,?,0,0,?,?,?,'parent-scan',.9,'unreviewed',?)""",
            (segment_id, resource_id, len(text), text, text_hash, now),
        )
        connection.execute(
            """INSERT INTO kg_segment_fts(segment_id,text,title,subject_scope,grade_scope)
               VALUES(?,?,'练习册参考答案',?,?)""",
            (segment_id, text, subject, grade),
        )
        connection.commit()
    return resource_id, segment_id


def prepared(tmp_path: Path):
    database, request = pending_request(tmp_path)
    draft = PaperScoringAuthorityDraftService(
        database, DraftGateway(), StudyRepository(database), lambda: True,
    )
    assert asyncio.run(draft.process_next())["status"] == "completed"
    obligation_id = PaperScoringService(database).for_candidate(
        request["candidate_id"], "child"
    )["authority_supply"]["obligation_id"]
    return database, request, obligation_id


def test_local_page_match_creates_version_bound_candidate_not_authority(tmp_path: Path) -> None:
    database, request, obligation_id = prepared(tmp_path)
    add_local_page(database, text="本章参考答案与评分说明：对应练习册题号，先列出已知量。")
    service = PaperScoringSourceMatchService(database)

    result = service.process_next()

    assert result["status"] == "candidate"
    assert result["candidate_count"] == 1
    candidates = service.valid_candidates(obligation_id)
    assert len(candidates) == 1
    current = PaperScoringService(database).for_candidate(request["candidate_id"], "child")
    assert current["decision_id"] is None
    assert current["authority_supply"]["status"] == "source_candidate"
    assert current["authority_supply"]["latest_evidence"]["authority_granted"] is False


def test_no_source_waits_and_retries_only_after_catalog_changes(tmp_path: Path) -> None:
    database, _, obligation_id = prepared(tmp_path)
    service = PaperScoringSourceMatchService(database)

    first = service.process_next()
    assert first["status"] == "waiting"
    assert service.process_next() is None

    add_local_page(database, text="对应练习册题号，本章参考答案与评分说明。")
    second = service.process_next()
    assert second["status"] == "candidate"
    assert service.valid_candidates(obligation_id)


def test_changed_source_version_invalidates_old_candidate(tmp_path: Path) -> None:
    database, _, obligation_id = prepared(tmp_path)
    _, segment_id = add_local_page(
        database, text="对应练习册题号，本章参考答案与评分说明。"
    )
    service = PaperScoringSourceMatchService(database)
    assert service.process_next()["candidate_count"] == 1

    with database.connect() as connection:
        changed = "这页内容已经被家长重新拍摄并修订。"
        import hashlib
        digest = hashlib.sha256(changed.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE kg_segment SET text=?,text_sha256=? WHERE segment_id=?",
            (changed, digest, segment_id),
        )
        connection.commit()

    assert service.valid_candidates(obligation_id) == []


def test_rewritten_page_reopens_lookup_without_newer_catalog_timestamp(tmp_path: Path) -> None:
    import hashlib

    database, _, obligation_id = prepared(tmp_path)
    _, segment_id = add_local_page(database, text="本章参考答案与评分说明：旧版。")
    service = PaperScoringSourceMatchService(database)
    assert service.process_next()["status"] == "candidate"
    old_candidate_id = service.valid_candidates(obligation_id)[0]["candidate_id"]

    changed = "本章参考答案与评分说明：新版。"
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE kg_segment SET text=?,text_sha256=? WHERE segment_id=?",
            (changed, hashlib.sha256(changed.encode()).hexdigest(), segment_id),
        )
        connection.execute(
            "UPDATE kg_segment_fts SET text=? WHERE segment_id=?",
            (changed, segment_id),
        )
        connection.commit()

    assert service.valid_candidates(obligation_id) == []
    refreshed = PaperScoringSourceMatchService(database).process_next()
    assert refreshed is not None and refreshed["status"] == "candidate"
    candidates = service.valid_candidates(obligation_id)
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] != old_candidate_id
    assert candidates[0]["text"] == changed


def test_waiting_lookup_retries_when_existing_page_becomes_match(tmp_path: Path) -> None:
    import hashlib

    database, _, obligation_id = prepared(tmp_path)
    _, segment_id = add_local_page(database, text="这是一页无关的练习内容。")
    service = PaperScoringSourceMatchService(database)
    assert service.process_next()["status"] == "waiting"

    changed = "本章参考答案与评分说明：对应练习册题号。"
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE kg_segment SET text=?,text_sha256=? WHERE segment_id=?",
            (changed, hashlib.sha256(changed.encode()).hexdigest(), segment_id),
        )
        connection.execute(
            "UPDATE kg_segment_fts SET text=? WHERE segment_id=?",
            (changed, segment_id),
        )
        connection.commit()

    refreshed = PaperScoringSourceMatchService(database).process_next()
    assert refreshed is not None and refreshed["status"] == "candidate"
    assert len(service.valid_candidates(obligation_id)) == 1


def test_parent_workbook_reimport_refreshes_scoring_source_across_restart(tmp_path: Path) -> None:
    from home_study_agent.knowledge_graph.textbook_page_service import (
        TextbookOcrPageInput, TextbookOcrPageService,
    )

    database, request, obligation_id = prepared(tmp_path)
    pages = TextbookOcrPageService(database)
    page = dict(
        title="初三数学练习册", material_kind="workbook", grade_scope="初三",
        subject_scope="数学", source_ref="local_parent_textbook_capture:page-1",
        ocr_confidence=.95,
    )
    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：90",
    ))
    service = PaperScoringSourceMatchService(database)
    assert service.process_next()["status"] == "candidate"
    old_candidate = service.valid_candidates(obligation_id)[0]["candidate_id"]

    pages.import_page(TextbookOcrPageInput(
        **page, ocr_text=f"{request['question_text']} 参考答案：91",
    ))
    restarted = PaperScoringSourceMatchService(Database(database.path, runtime_mode=database.runtime_mode))
    assert restarted.valid_candidates(obligation_id) == []
    assert restarted.process_next()["status"] == "candidate"
    candidates = restarted.valid_candidates(obligation_id)
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] != old_candidate
    assert "参考答案：91" in candidates[0]["text"]


def test_grade_and_subject_mismatch_are_not_source_candidates(tmp_path: Path) -> None:
    database, _, _ = prepared(tmp_path)
    add_local_page(
        database, text="对应练习册题号，本章参考答案与评分说明。",
        grade="四年级", subject="物理",
    )
    result = PaperScoringSourceMatchService(database).process_next()
    assert result["status"] == "waiting"
    assert result["candidate_count"] == 0


def test_legacy_source_version_survives_upgrade_only_until_page_changes() -> None:
    from home_study_agent.paper_scoring_source_match import _digest, _source_version_matches

    row = {
        "resource_id": "resource-1", "transcript_sha256": "transcript-1",
        "version_label": "v1", "text_sha256": "segment-1",
        "page_updated_at": "2026-09-30T00:00:00+00:00",
        "candidate_created_at": "2026-09-30T00:00:01+00:00",
        "ocr_confidence": .95,
    }
    legacy_version = _digest({
        "resource_id": row["resource_id"],
        "transcript_sha256": row["transcript_sha256"],
        "version_label": row["version_label"],
        "segment_text_sha256": row["text_sha256"],
    })
    assert _source_version_matches(row, legacy_version)
    row["page_updated_at"] = "2026-09-30T00:00:02+00:00"
    assert not _source_version_matches(row, legacy_version)
