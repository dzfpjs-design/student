"""Run separated OCR cohorts from the read-only shared evaluation asset pack."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.evaluation.shared_ocr_benchmarks import (
    family_camera_status, formula_layout_diagnostic_benchmark, mathwriting_benchmark, mendeley_mark_benchmark, scut_smoke_benchmark,
)
from home_study_agent.local_ocr import LocalOcrService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", type=Path, required=True, help="Read-only hsc_eval_assets/ocr directory")
    parser.add_argument("--data-dir", type=Path, required=True, help="Local disposable OCR cache directory")
    parser.add_argument("--output", type=Path, required=True, help="Aggregate-only local JSON report")
    formula_source = parser.add_mutually_exclusive_group()
    formula_source.add_argument("--math-predictions", type=Path, help="Optional local JSONL: id and latex only")
    formula_source.add_argument("--math-runtime-report", type=Path, help="Optional schema-checked local formula benchmark JSON; never triggers a model download")
    args = parser.parse_args()
    root = args.assets_root
    service = LocalOcrService.default(args.data_dir / "scut-smoke-ocr.db")
    report = {
        "privacy": "Read-only public evaluation assets. Report contains aggregate metrics and sample IDs only; no page pixels, OCR text, or LaTeX labels are emitted.",
        "cohorts": {
            "correction_marks": mendeley_mark_benchmark(root / "mendeley_exam_marks"),
            "mathwriting": mathwriting_benchmark(
                root / "mathwriting_excerpt",
                args.math_predictions,
                args.math_runtime_report,
            ),
            "formula_layout_diagnostics": formula_layout_diagnostic_benchmark(
                root / "mathwriting_excerpt", root / "mendeley_exam_marks",
            ),
            "chinese_handwriting_smoke": scut_smoke_benchmark(root / "scut_samples", service),
            "family_document_camera": family_camera_status(),
        },
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "correction_pairs": report["cohorts"]["correction_marks"]["paired_page_count"], "math_formula_count": report["cohorts"]["mathwriting"]["formula_count"], "scut_samples": report["cohorts"]["chinese_handwriting_smoke"]["sample_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
