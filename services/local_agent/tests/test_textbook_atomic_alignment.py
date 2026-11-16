from __future__ import annotations

import hashlib
import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.textbook_alignment import TextbookAtomicAlignmentService
from home_study_agent.repository import utc_now


def _seed(database: Database) -> None:
    now = utc_now()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,video_root,metadata_db_path,source_ref,license_boundary,review_status,version_label,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("video", "课程视频", "seyk", "/video", None, None, "local-video", "family_owned_local",
             "restricted", "v1", now, now),
        )
        connection.execute(
            "INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,video_root,metadata_db_path,source_ref,license_boundary,review_status,version_label,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("textbook", "南京教材", "local_scanned_document", "/textbook", None, None,
             "local-textbook-pdf", "family_owned_local", "restricted", "v1", now, now),
        )
        resources = [
            ("video-1", "video", "v1", "两、三位数除以整十数", "四年级", "数学", "上学期", "video.txt", "video:1"),
            ("book-1", "textbook", "p1", "四年级数学第10页", "四年级", "数学", "上学期", "p1.txt", "book.pdf#page=10"),
            ("book-wrong-grade", "textbook", "p2", "五年级数学", "五年级", "数学", "上学期", "p2.txt", "other.pdf#page=8"),
        ]
        for resource_id, corpus_id, external_id, title, grade, subject, semester, relpath, source_ref in resources:
            connection.execute(
                """INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,grade_scope,subject_scope,
                   semester_scope,transcript_relative_path,transcript_sha256,metadata_json,source_ref,
                   license_boundary,confidence,review_status,version_label,ingested_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'{}',?,'family_owned_local',.9,'restricted','v1',?,?)""",
                (resource_id, corpus_id, external_id, title, grade, subject, semester, relpath,
                 hashlib.sha256(resource_id.encode()).hexdigest(), source_ref, now, now),
            )
        segments = {
            "video-seg": ("video-1", "学习目标：能独立计算两、三位数除以整十数的商。"),
            "book-seg": ("book-1", "两、三位数除以整十数\n420÷30=14\n先看被除数的前两位，试商后验算。"),
            "wrong-seg": ("book-wrong-grade", "两、三位数除以整十数的练习。"),
        }
        for segment_id, (resource_id, text) in segments.items():
            connection.execute(
                """INSERT INTO kg_segment(segment_id,resource_id,ordinal,start_char,end_char,text,text_sha256,
                   source_ref,confidence,review_status,created_at) VALUES(?,?,0,0,?,?,?,?,.9,'restricted',?)""",
                (segment_id, resource_id, len(text), text, hashlib.sha256(text.encode()).hexdigest(),
                 next(item[-1] for item in resources if item[0] == resource_id), now),
            )
            connection.execute(
                "INSERT INTO kg_segment_occurrence VALUES(?,?,0,0,?)",
                (resource_id, segment_id, len(text)),
            )
        connection.execute(
            """INSERT INTO kg_atomic_extraction_job(job_id,idempotency_key,status,scope_json,prompt_version,
               total_batches,completed_batches,created_at,updated_at) VALUES('job','job-key','completed','{}','v4',1,1,?,?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO kg_atomic_extraction_batch(batch_id,job_id,batch_index,resource_ids_json,input_json,
               input_sha256,status,created_at,updated_at) VALUES('batch','job',0,'[\"video-1\"]','{}','hash','completed',?,?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO kg_atomic_definition_draft(draft_id,canonical_signature,title,grade_band,subject,
               atom_kind,action_verb,knowledge_component,boundary_json,observable_outcome,mastery_criteria_json,
               diagnostic_blueprint_json,misconception_hypotheses_json,concept_tags_json,standard_alignments_json,
               confidence,review_status,source_method,prompt_version,created_at,updated_at)
               VALUES('draft-1','sig','计算两、三位数除以整十数','小学4-6年级','数学',
               'procedure_execution','计算','两、三位数除以整十数','{}',
               '能独立计算并验算','{}','[]','[]','[\"整十数\",\"试商\"]','[]',.9,
               'unreviewed','model','v4',?,?)""",
            (now, now),
        )
        connection.execute("INSERT INTO kg_atomic_draft_batch VALUES('draft-1','batch','job',?)", (now,))
        quote = "两、三位数除以整十数"
        connection.execute(
            """INSERT INTO kg_atomic_draft_evidence VALUES('e1','draft-1','video-1','video-seg',?,?,
               'objective',.9,?)""",
            (quote, hashlib.sha256(quote.encode()).hexdigest(), now),
        )
        connection.commit()


def test_textbook_alignment_is_scope_bound_exact_and_unreviewed(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db")
    database.initialize()
    _seed(database)

    service = TextbookAtomicAlignmentService(database)
    result = service.generate(job_id="job", textbook_corpus_id="textbook", minimum_score=.25)

    assert result["eligible_drafts"] == 1
    assert result["aligned_drafts"] == 1
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM kg_atomic_textbook_alignment").fetchall()
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM kg_atomic_draft_evidence WHERE resource_id LIKE 'book-%'"
        ).fetchone()[0]
    assert len(rows) == 1
    assert rows[0]["textbook_resource_id"] == "book-1"
    assert rows[0]["review_status"] == "unreviewed"
    assert rows[0]["evidence_role"] == "mention"
    assert rows[0]["exact_quote"] in segments_text(database, "book-seg")
    assert evidence_count == 0


def test_textbook_alignment_is_idempotent_and_reports_unmatched_pages(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db")
    database.initialize()
    _seed(database)
    service = TextbookAtomicAlignmentService(database)

    first = service.generate(job_id="job", textbook_corpus_id="textbook", minimum_score=.25)
    second = service.generate(job_id="job", textbook_corpus_id="textbook", minimum_score=.25)

    assert second["candidate_alignments"] == first["candidate_alignments"] == 1
    assert second["textbook_pages"] == 2
    assert second["matched_textbook_pages"] == 1
    assert second["unmatched_textbook_pages"] == 1


def test_entity_name_without_tested_property_is_not_textbook_evidence() -> None:
    quote, score = TextbookAtomicAlignmentService._best_quote(
        "平行四边形的面积（底乘高）", ["多边形面积", "平行四边形"],
        "在方格纸上画一个轴对称图形的平行四边形。",
    )
    assert quote is None and score == 0

    quote, score = TextbookAtomicAlignmentService._best_quote(
        "平行四边形的面积（底乘高）", ["多边形面积", "平行四边形"],
        "平行四边形的面积等于底乘高。",
    )
    assert quote == "平行四边形的面积等于底乘高。" and score >= .7


def test_neighboring_chinese_concept_and_single_english_word_do_not_match() -> None:
    quote, score = TextbookAtomicAlignmentService._best_quote(
        "复式折线统计图", [], "你能看懂这幅复式条形统计图吗？",
    )
    assert quote is None and score == 0

    quote, score = TextbookAtomicAlignmentService._best_quote(
        "How do/does ... come to school句型及回答", ["sentence_pattern"],
        "He puts his school things in order every evening.",
    )
    assert quote is None and score == 0

    quote, score = TextbookAtomicAlignmentService._best_quote(
        "频率副词always, usually, often, sometimes描述习惯", ["频率副词"],
        "We always read and usually exercise. We often help at home.",
    )
    assert quote is not None and score >= .3


def test_short_english_pattern_requires_all_distinctive_tokens() -> None:
    quote, score = TextbookAtomicAlignmentService._best_quote(
        "特殊疑问句What does...do询问他人职业", [], "What does he like?",
    )
    assert quote is None and score == 0

    quote, score = TextbookAtomicAlignmentService._best_quote(
        "There be句型描述复数物品位置（There are...）", [],
        "Oh, there ... a bear ... the chair!",
    )
    assert quote is None and score == 0

    quote, score = TextbookAtomicAlignmentService._best_quote(
        "There be句型描述复数物品位置（There are...）", [],
        "There are three beds in the room.",
    )
    assert quote is not None and score >= .3


def segments_text(database: Database, segment_id: str) -> str:
    with database.connect() as connection:
        return connection.execute("SELECT text FROM kg_segment WHERE segment_id=?", (segment_id,)).fetchone()[0]
