import json
from pathlib import Path

import numpy as np

from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec, LocalDocumentEvidenceIngestor
from home_study_agent.knowledge_ingestion.local_corpora import LocalCorpusIngestor
from home_study_agent.knowledge_ingestion.textbook_pdf import TextbookPdfOcrPipeline, parse_textbook_path


class _Engine:
    name = "test-local-ocr"

    @staticmethod
    def health():
        return {"ready": True}

    @staticmethod
    def recognize(image, route):
        return [{
            "text": "三位数除以整十数 60÷20=3", "confidence": .94,
            "box": [[10, 10], [300, 10], [300, 40], [10, 40]],
        }]


class _FlakyEngine(_Engine):
    attempts = 0

    @classmethod
    def recognize(cls, image, route):
        cls.attempts += 1
        if cls.attempts == 1:
            return []
        return super().recognize(image, route)


def test_textbook_filename_parses_nanjing_scope_and_edition(tmp_path: Path) -> None:
    root = tmp_path / "pdfs"
    path = root / "初中" / "八年级上册_生物学_苏教版.pdf"
    path.parent.mkdir(parents=True); path.write_bytes(b"pdf")

    book = parse_textbook_path(root, path)

    assert (book.stage, book.grade, book.subject, book.semester, book.edition) == (
        "初中", "初二", "生物", "上学期", "苏教版",
    )


def test_page_ocr_is_resumable_and_projects_exact_pdf_page_source(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf = pdf_root / "小学" / "四年级上册_数学_苏教版.pdf"
    pdf.parent.mkdir(parents=True); pdf.write_bytes(b"image-only-pdf")
    output = tmp_path / "ocr"
    metadata = tmp_path / "textbooks.db"
    image = np.full((800, 600, 3), 255, dtype=np.uint8)
    pipeline = TextbookPdfOcrPipeline(
        pdf_root, output, metadata, page_counter=lambda path: 2,
        renderer=lambda path, page: image, ocr_engine=_Engine(),
    )

    assert pipeline.prepare() == {"books": 1, "pages": 2, "changed_books": 0}
    result = pipeline.run(max_pages=1)
    assert result["pages"]["completed"] == 1 and result["pages"]["pending"] == 1
    rows = LocalCorpusIngestor._metadata(metadata, "local_scanned_document")
    page_id, page_metadata = next(iter(rows.items()))
    assert page_metadata["grade"] == "四年级" and page_metadata["subject"] == "数学"
    assert page_metadata["page_number"] == 1
    assert page_metadata["source_ref"].endswith("四年级上册_数学_苏教版.pdf#page=1")
    assert (output / "pages" / f"{page_id}.txt").read_text(encoding="utf-8") == "三位数除以整十数 60÷20=3"

    database = Database(tmp_path / "study.db"); database.initialize()
    spec = DocumentCorpusSpec(
        corpus_id="nanjing-textbooks", title="南京教材", document_kind="textbook",
        ocr_text_root=output, image_root=None, source_ref="local-textbook-pdf",
        license_boundary="family_owned_local", ocr_engine="test-local-ocr", metadata_db_path=metadata,
        pdf_root=pdf_root,
    )
    ingestor = LocalDocumentEvidenceIngestor(database)
    job = ingestor.start(spec, idempotency_key="nanjing-textbooks-v1")
    assert ingestor.run(spec, job)["status"] == "completed"
    with database.connect() as connection:
        resource = connection.execute(
            "SELECT grade_scope,subject_scope,semester_scope,source_ref FROM kg_resource"
        ).fetchone()
        page = connection.execute("SELECT page_number,source_ref,ocr_confidence FROM kg_document_page").fetchone()
    assert tuple(resource)[:3] == ("四年级", "数学", "上学期")
    assert resource["source_ref"].endswith("#page=1")
    assert page["page_number"] == 1 and page["source_ref"].endswith("#page=1")
    assert page["ocr_confidence"] == .94

    second_page_id = page_id.rsplit("p", 1)[0] + "p0002"
    assert pipeline.run_pages([second_page_id])["pages"]["completed"] == 2


def test_transient_ocr_failure_retries_within_the_same_run(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf = pdf_root / "小学" / "四年级上册_数学_苏教版.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"image-only-pdf")
    _FlakyEngine.attempts = 0
    nonblank = np.full((800, 600, 3), 255, dtype=np.uint8)
    nonblank[100:220, 100:500] = 0
    pipeline = TextbookPdfOcrPipeline(
        pdf_root, tmp_path / "ocr", tmp_path / "textbooks.db",
        page_counter=lambda path: 1,
        renderer=lambda path, page: nonblank,
        ocr_engine=_FlakyEngine(),
    )
    pipeline.prepare()

    result = pipeline.run()

    assert result["pages"]["completed"] == 1
    assert result["pages"]["failed"] == 0
    assert _FlakyEngine.attempts == 2


def test_solid_colored_flyleaf_is_classified_as_blank(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf = pdf_root / "初中" / "七年级上册_数学_苏科版.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"image-only-pdf")
    flyleaf = np.full((800, 600, 3), (235, 210, 180), dtype=np.uint8)
    pipeline = TextbookPdfOcrPipeline(
        pdf_root, tmp_path / "ocr", tmp_path / "textbooks.db",
        page_counter=lambda path: 1,
        renderer=lambda path, page: flyleaf,
        ocr_engine=type("EmptyEngine", (), {
            "name": "empty", "health": staticmethod(lambda: {"ready": True}),
            "recognize": staticmethod(lambda image, route: []),
        })(),
    )
    pipeline.prepare()

    result = pipeline.run()

    assert result["pages"]["skipped_blank"] == 1
    assert result["pages"]["failed"] == 0
    with pipeline._connect() as connection:
        page = connection.execute("SELECT error_code,error_detail FROM textbook_page").fetchone()
    assert tuple(page) == (None, None)


def test_image_only_page_becomes_reviewable_nontext_after_three_attempts(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf = pdf_root / "初中" / "八年级下册_生物学_苏教版.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"image-only-pdf")
    illustration = np.full((800, 600, 3), 255, dtype=np.uint8)
    illustration[100:700, 80:520] = (120, 180, 210)
    engine = type("EmptyEngine", (), {
        "name": "empty", "health": staticmethod(lambda: {"ready": True}),
        "recognize": staticmethod(lambda image, route: []),
    })()
    pipeline = TextbookPdfOcrPipeline(
        pdf_root, tmp_path / "ocr", tmp_path / "textbooks.db",
        page_counter=lambda path: 1,
        renderer=lambda path, page: illustration,
        ocr_engine=engine,
    )
    pipeline.prepare()

    result = pipeline.run()

    assert result["pages"]["skipped_nontext"] == 1
    assert result["pages"]["failed"] == 0
    with pipeline._connect() as connection:
        page = connection.execute(
            "SELECT attempt_count,manual_review_required,layout_json FROM textbook_page"
        ).fetchone()
    assert page["attempt_count"] == 3 and page["manual_review_required"] == 1
    assert json.loads(page["layout_json"])["classification"] == "nontext_page"
