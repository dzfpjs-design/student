"""Read-only replay of persisted local OCR artifacts into a disposable DB."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.document_artifact_replay import replay_document_artifacts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_database", type=Path)
    parser.add_argument("--session-id")
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args()
    result = replay_document_artifacts(
        args.source_database,
        session_id=args.session_id,
        output_directory=args.output_directory,
    )
    payload = {**result.report, "isolated_database": str(result.isolated_database)}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
