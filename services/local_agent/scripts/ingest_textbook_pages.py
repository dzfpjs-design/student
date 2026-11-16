"""Import the completed local Nanjing textbook OCR collection into a knowledge DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion.document_evidence import (
    DocumentCorpusSpec,
    LocalDocumentEvidenceIngestor,
)


def ocr_status(metadata_db: Path) -> dict[str, int]:
    uri = f"file:{metadata_db}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        result = {row[0]: row[1] for row in connection.execute(
            "SELECT status,COUNT(*) FROM textbook_page GROUP BY status"
        )}
        result["total"] = connection.execute("SELECT COUNT(*) FROM textbook_page").fetchone()[0]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    parser.add_argument("--metadata-db", type=Path, required=True)
    parser.add_argument("--ocr-root", type=Path, required=True)
    parser.add_argument("--pdf-root", type=Path, help="explicit original PDF directory; required when metadata references PDFs")
    parser.add_argument("--image-root", type=Path, help="explicit original scanned page directory, when applicable")
    parser.add_argument("--corpus-id", default="nanjing-textbooks-v1")
    parser.add_argument("--idempotency-key", default="nanjing-textbooks-74-books-v1")
    arguments = parser.parse_args()

    status = ocr_status(arguments.metadata_db)
    unfinished = sum(status.get(item, 0) for item in ("pending", "processing"))
    failed = status.get("failed", 0)
    if unfinished or failed:
        raise SystemExit(
            "textbook OCR is not ready for a final import: "
            f"unfinished={unfinished}, failed={failed}, completed={status.get('completed', 0)}"
        )

    database = Database(arguments.app_db)
    database.initialize()
    spec = DocumentCorpusSpec(
        corpus_id=arguments.corpus_id,
        title="南京小学四至六年级与初中一至三年级教材",
        document_kind="textbook",
        ocr_text_root=arguments.ocr_root,
        image_root=arguments.image_root,
        source_ref="local-textbook-pdf",
        license_boundary="family_owned_local",
        ocr_engine="rapidocr-onnx",
        metadata_db_path=arguments.metadata_db,
        version_label="nanjing-textbooks-2026-08-v1",
        pdf_root=arguments.pdf_root,
    )
    ingestor = LocalDocumentEvidenceIngestor(database)
    job_id = ingestor.start(spec, idempotency_key=arguments.idempotency_key)
    result = ingestor.run(spec, job_id, throttle_ms=0, hash_images=False)
    print(json.dumps({"ocr": status, "ingest_job_id": job_id, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
