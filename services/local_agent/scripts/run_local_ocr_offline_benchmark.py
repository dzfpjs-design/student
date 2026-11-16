from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.evaluation.offline_ocr_benchmark import run
from home_study_agent.local_ocr import LocalOcrService


def main() -> None:
    parser = argparse.ArgumentParser(description="Run generated local-only OCR reliability cases.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    report = run(LocalOcrService.default(args.data_dir / "offline-ocr-benchmark.db"))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"suite": report["suite"], "printed_case_count": report["printed_case_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
