"""Create or score a local-only manual review packet for correction-mark candidates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.evaluation.shared_ocr_benchmarks import (
    build_mark_review_template,
    evaluate_mark_review,
    mendeley_mark_benchmark,
    render_mark_review_overlays,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", type=Path, required=True, help="Read-only mendeley_exam_marks directory")
    parser.add_argument("--review", type=Path, required=True, help="Review JSON to create or score")
    parser.add_argument("--overlay-dir", type=Path, help="Optional local-only rendered overlays; never commit source page pixels")
    parser.add_argument("--score-only", action="store_true")
    args = parser.parse_args()

    report = mendeley_mark_benchmark(args.assets_root)
    if args.score_only:
        review = json.loads(args.review.read_text(encoding="utf-8"))
    else:
        review = build_mark_review_template(report)
        args.review.parent.mkdir(parents=True, exist_ok=True)
        args.review.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = evaluate_mark_review(review)
    overlays = render_mark_review_overlays(args.assets_root, report, review, args.overlay_dir) if args.overlay_dir else None
    print(json.dumps({"review": str(args.review), "review_result": result, "overlays": overlays}, ensure_ascii=False))


if __name__ == "__main__":
    main()
