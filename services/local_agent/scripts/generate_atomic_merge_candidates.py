"""Generate review-only paraphrase merge candidates for atomic drafts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.atomic_model_pipeline import AtomicMergeCandidateService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    parser.add_argument("--minimum-similarity", type=float, default=.72)
    arguments = parser.parse_args()
    if not .5 <= arguments.minimum_similarity <= 1:
        raise SystemExit("minimum similarity must be between 0.5 and 1")
    database = Database(arguments.app_db); database.initialize()
    result = AtomicMergeCandidateService(database).generate(minimum_similarity=arguments.minimum_similarity)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

