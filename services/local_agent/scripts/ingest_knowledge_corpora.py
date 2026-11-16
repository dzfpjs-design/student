"""Run/resume a local knowledge-corpus import without exposing content to a network."""
from __future__ import annotations

import argparse
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion import CorpusSpec, LocalCorpusIngestor
from home_study_agent.retrieval import HashingVectorizer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-kind", choices=("seyk", "bilibili"), required=True)
    parser.add_argument("--transcripts", type=Path, required=True)
    parser.add_argument("--videos", type=Path)
    parser.add_argument("--metadata-db", type=Path)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--max-resources", type=int)
    parser.add_argument("--throttle-ms", type=int, default=0, help="pause 0-5000ms after each resource for CPU/SSD headroom")
    parser.add_argument("--vector-dimensions", type=int, default=128, choices=(64, 96, 128), help="64 is recommended for a 16GB CPU-only PC")
    parser.add_argument("--hash-videos", action="store_true", help="stream SHA-256; expensive for a 240GB corpus")
    arguments = parser.parse_args()
    database = Database(arguments.app_db); database.initialize()
    spec = CorpusSpec(arguments.corpus_id, arguments.title, arguments.source_kind, arguments.transcripts,
                      arguments.videos, f"local:{arguments.source_kind}", "local_only_rights_not_verified",
                      arguments.metadata_db)
    importer = LocalCorpusIngestor(database, HashingVectorizer(arguments.vector_dimensions))
    job_id = importer.start(spec, idempotency_key=arguments.idempotency_key, hash_videos=arguments.hash_videos)
    print(importer.run(spec, job_id, max_resources=arguments.max_resources, throttle_ms=max(0, arguments.throttle_ms)))


if __name__ == "__main__":
    main()
