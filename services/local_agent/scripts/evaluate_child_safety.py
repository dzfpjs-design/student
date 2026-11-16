from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from home_study_agent.child_safety_eval import (
    aggregate_results,
    evaluate_cases,
    parse_case,
    release_gate,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the deterministic local child-safety router.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).parents[1] / "eval" / "child_safety_cases.json",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    cases = [parse_case(item) for item in json.loads(args.cases.read_text(encoding="utf-8"))]
    results = evaluate_cases(cases)
    summary = aggregate_results(results)
    gate = release_gate(summary)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.cases.resolve()),
        "classifier_version": "child_safety_v2",
        "summary": summary,
        "release_gate": gate,
        "results": results,
        "contains_real_child_data": False,
        "requires_model_credentials": False,
    }
    output = args.output or Path("artifacts") / f"child-safety-eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "summary": summary, "release_gate": gate}, ensure_ascii=False))
    if args.strict and not gate["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
