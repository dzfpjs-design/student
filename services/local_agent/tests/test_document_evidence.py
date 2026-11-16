from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec, LocalDocumentEvidenceIngestor


def test_scanned_document_page_evidence_is_resumable_and_never_stores_absolute_image_path(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    ocr = tmp_path / "ocr"; images = tmp_path / "images"; ocr.mkdir(); images.mkdir()
    (ocr / "page-001.txt").write_text("一元二次方程公式法练习：先计算判别式。", encoding="utf-8")
    (images / "page-001.jpg").write_bytes(b"local-scanner-page")
    spec = DocumentCorpusSpec(
        corpus_id="school-practice", title="南京学校练习", document_kind="school_official_practice",
        ocr_text_root=ocr, image_root=images, source_ref="user_local_scanner",
        license_boundary="school_official_local", ocr_engine="local_ocr", ocr_confidence=.93,
    )
    ingestor = LocalDocumentEvidenceIngestor(database)
    job_id = ingestor.start(spec, idempotency_key="school-practice-v1")
    first = ingestor.run(spec, job_id, max_resources=1)
    assert first["document_pages"]["linked"] == 1 and first["document_pages"]["with_local_image"] == 1
    rerun = LocalDocumentEvidenceIngestor(Database(tmp_path / "study.db")).run(spec, job_id, hash_images=True)
    assert rerun["document_pages"]["linked"] == 1 and rerun["image_hashing"] is True
    with database.connect() as connection:
        page = connection.execute("SELECT * FROM kg_document_page").fetchone()
        resource = connection.execute("SELECT transcript_relative_path FROM kg_resource WHERE resource_id=?", (page["resource_id"],)).fetchone()
    assert page["document_kind"] == "school_official_practice"
    assert page["image_relative_path"] == "page-001.jpg"
    assert page["image_sha256"] and "images" not in page["image_relative_path"]
    assert resource["transcript_relative_path"] == "page-001.txt"
