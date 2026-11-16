"""Generate or verify the single-source local knowledge graph quality report."""
from __future__ import annotations

import argparse
from pathlib import Path

from home_study_agent.knowledge_graph.reporting import write_or_check_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if not write_or_check_report(app_db=arguments.app_db, markdown_output=arguments.markdown_output,
                                 json_output=arguments.json_output, check=arguments.check):
        raise SystemExit("knowledge graph quality report is stale; regenerate it from the production snapshot")


if __name__ == "__main__":
    main()
