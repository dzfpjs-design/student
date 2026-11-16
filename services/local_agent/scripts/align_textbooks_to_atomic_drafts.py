"""Generate local textbook evidence candidates for one atomic extraction job."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.textbook_alignment import TextbookAtomicAlignmentService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    parser.add_argument("--atomic-job-id", required=True)
    parser.add_argument("--textbook-corpus-id", default="nanjing-textbooks-v1")
    parser.add_argument("--minimum-score", type=float, default=.34)
    parser.add_argument("--max-candidates-per-scope", type=int, default=3)
    parser.add_argument("--report-output", type=Path)
    arguments = parser.parse_args()
    database = Database(arguments.app_db)
    database.initialize()
    result = TextbookAtomicAlignmentService(database).generate(
        job_id=arguments.atomic_job_id,
        textbook_corpus_id=arguments.textbook_corpus_id,
        minimum_score=arguments.minimum_score,
        max_candidates_per_scope=arguments.max_candidates_per_scope,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if arguments.report_output:
        arguments.report_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.report_output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
