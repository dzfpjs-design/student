"""Prepare, run, or inspect local OCR for the南京 textbook PDF collection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.knowledge_ingestion.textbook_pdf import TextbookPdfOcrPipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--metadata-db", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--page-id", action="append", default=[])
    arguments = parser.parse_args()
    pipeline = TextbookPdfOcrPipeline(arguments.pdf_root, arguments.output_root, arguments.metadata_db)
    prepared = None
    if not arguments.status_only:
        prepared = pipeline.prepare()
    if arguments.prepare_only or arguments.status_only:
        result = pipeline.status()
    elif arguments.page_id:
        result = pipeline.run_pages(arguments.page_id)
    else:
        result = pipeline.run(max_pages=arguments.max_pages)
    print(json.dumps({"prepared": prepared, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
