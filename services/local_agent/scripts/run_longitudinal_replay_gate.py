from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from home_study_agent.longitudinal_replay_gate import run_longitudinal_replay_gate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the synthetic two-child 30-day longitudinal safety gate."
    )
    parser.add_argument(
        "--database", type=Path,
        help="Optional disposable database path. Omit to use an auto-deleted temporary database.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON result path.")
    args = parser.parse_args()
    result = run_longitudinal_replay_gate(args.database)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
