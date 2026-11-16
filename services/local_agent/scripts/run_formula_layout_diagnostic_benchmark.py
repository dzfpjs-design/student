"""Emit aggregate-only formula-layout heuristic trigger counts.

This never runs a formula provider and never writes page pixels, OCR text, or
LaTex labels. The output is explicitly diagnostic-only, not an accuracy report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.evaluation.shared_ocr_benchmarks import formula_layout_diagnostic_benchmark


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", type=Path, required=True, help="Read-only hsc_eval_assets/ocr directory")
    parser.add_argument("--output", type=Path, required=True, help="Aggregate-only JSON result")
    args = parser.parse_args()
    report = formula_layout_diagnostic_benchmark(
        args.assets_root / "mathwriting_excerpt",
        args.assets_root / "mendeley_exam_marks",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "mathwriting_pages": report["cohorts"]["mathwriting_rendered_formulas"]["pages_read"],
        "mendeley_pages": report["cohorts"]["mendeley_raw_exam_pages"]["pages_read"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
