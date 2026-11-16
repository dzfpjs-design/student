from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from home_study_agent.evaluation.shared_ocr_benchmarks import (
    build_mark_review_template,
    evaluate_mark_review,
    formula_layout_diagnostic_benchmark,
    mathwriting_benchmark,
    mendeley_mark_benchmark,
    scut_smoke_benchmark,
)


def test_formula_layout_diagnostics_report_trigger_counts_not_accuracy(tmp_path: Path) -> None:
    rendered = tmp_path / "mathwriting" / "rendered_png" / "valid"
    rendered.mkdir(parents=True)
    formula = np.full((160, 420, 3), 255, dtype=np.uint8)
    cv2.putText(formula, "x2 + 3 = 7", (30, 105), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
    assert cv2.imwrite(str(rendered / "formula.png"), formula)
    labels = rendered.parent / "labels.jsonl"
    labels.write_text(json.dumps({"id": "formula", "image": str(rendered / "formula.png")}) + "\n", encoding="utf-8")
    raw = tmp_path / "mendeley" / "raw_pages" / "student"
    raw.mkdir(parents=True)
    assert cv2.imwrite(str(raw / "page.jpg"), np.full((160, 420, 3), 255, dtype=np.uint8))

    report = formula_layout_diagnostic_benchmark(tmp_path / "mathwriting", tmp_path / "mendeley")

    assert report["diagnostic_only"] is True
    assert report["provider_eligible"] is False
    assert report["review_eligible"] is False
    assert "not precision" in report["metric_boundary"]
    assert report["cohorts"]["mathwriting_rendered_formulas"]["input_page_count"] == 1
    assert report["cohorts"]["mendeley_raw_exam_pages"]["pages_read"] == 1


def test_mendeley_benchmark_pairs_pages_and_uses_new_red_ink_only(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw_pages" / "Student_1"
    corrected_dir = tmp_path / "corrected_pages" / "Student_1"
    raw_dir.mkdir(parents=True); corrected_dir.mkdir(parents=True)
    raw = np.full((240, 180, 3), 255, dtype=np.uint8)
    corrected = raw.copy()
    cv2.putText(raw, "1", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    cv2.putText(corrected, "1", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    cv2.circle(corrected, (90, 110), 28, (0, 0, 220), 3)
    assert cv2.imwrite(str(raw_dir / "page-1.jpg"), raw)
    assert cv2.imwrite(str(corrected_dir / "page-1.jpg"), corrected)

    report = mendeley_mark_benchmark(tmp_path)

    assert report["paired_page_count"] == 1
    assert report["metrics"]["mark_component_count"] >= 1
    assert report["pages"][0]["new_red_ink_pixel_ratio"] < 1
    assert report["pages"][0]["annotations"][0]["bbox_xywh"]
    assert report["pages"][0]["annotations"][0]["area_pixels"] >= 18
    assert report["annotation_schema"]["shape_candidate"].endswith("only")
    assert "red_or_changed_pixel_ratio" not in report["pages"][0]


def test_mathwriting_benchmark_requires_local_formula_predictions(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered_png"
    rendered.mkdir()
    (rendered / "labels.jsonl").write_text(json.dumps({"id": "a", "split": "valid", "label": "x + 1", "normalized_label": "x+1"}) + "\n", encoding="utf-8")
    report = mathwriting_benchmark(tmp_path)
    assert report["formula_count"] == 1
    assert str(report["metrics"]["math_structure_exact_match"]).startswith("not_evaluated")
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps({"id": "a", "latex": "x + 1"}) + "\n", encoding="utf-8")
    metrics = mathwriting_benchmark(tmp_path, predictions)["metrics"]
    assert metrics["math_structure_exact_match"] == 1
    assert metrics["latex_normalized_cer_mean"] == 0
    assert metrics["latex_token_error_rate_mean"] == 0


def test_mathwriting_benchmark_accepts_a_verified_local_runtime_report(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered_png"
    rendered.mkdir()
    (rendered / "labels.jsonl").write_text(
        "\n".join(
            json.dumps({"id": item, "split": "valid", "label": item, "normalized_label": item})
            for item in ("a", "b")
        ) + "\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "cohort": "mathwriting_excerpt",
        "formula_count": 2,
        "scope": "Rendered only.",
        "metrics": {
            "latex_normalized_exact_match": 0.5,
            "math_structure_exact_match": 0.5,
            "latex_normalized_cer_mean": 0.25,
            "latex_token_error_rate_mean": 0.3,
            "prediction_coverage": 1.0,
        },
        "provider_runtime": {
            "provider": "local_formula_provider",
            "cpu_only": True,
            "implicit_network_download": False,
            "latency_p95_seconds": 0.2,
        },
    }), encoding="utf-8")

    report = mathwriting_benchmark(tmp_path, runtime_report_path=runtime)

    assert report["metrics"]["math_structure_exact_match"] == 0.5
    assert report["provider_runtime"]["implicit_network_download"] is False
    assert report["runtime_report_validation"] == "accepted_local_formula_runtime_report"


def test_mathwriting_benchmark_rejects_a_report_for_the_wrong_cohort(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered_png"
    rendered.mkdir()
    (rendered / "labels.jsonl").write_text(json.dumps({"id": "a", "split": "valid", "label": "a", "normalized_label": "a"}) + "\n", encoding="utf-8")
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"cohort": "other", "formula_count": 1, "metrics": {}, "provider_runtime": {}}), encoding="utf-8")

    report = mathwriting_benchmark(tmp_path, runtime_report_path=runtime)

    assert report["metrics"]["math_structure_exact_match"] == "not_evaluated_invalid_local_formula_runtime_report"
    assert report["runtime_report_validation"].startswith("rejected:")


class _SmokeService:
    def enqueue(self, payload: bytes, request_id: str) -> dict:
        return {"job_id": request_id}

    def run(self, job_id: str, payload: bytes) -> dict:
        return {"status": "failed", "error_code": "ValueError"}


def test_scut_smoke_reports_low_resolution_as_a_boundary(tmp_path: Path) -> None:
    sample = tmp_path / "hccdoc"
    sample.mkdir()
    image = np.full((20, 100, 3), 255, dtype=np.uint8)
    assert cv2.imwrite(str(sample / "tiny.jpg"), image)
    report = scut_smoke_benchmark(tmp_path, _SmokeService())
    assert report["samples"][0]["failure_boundary"] == "source_below_minimum_80px_dimension"


def test_mark_review_template_requires_page_ground_truth_for_precision_recall(tmp_path: Path) -> None:
    report = {
        "pages": [
            {
                "sample_id": "Student_1/page-1.jpg",
                "annotations": [
                    {"annotation_id": "Student_1/page-1.jpg#1", "bbox_xywh": [1, 2, 3, 4], "shape_candidate": "tick"},
                    {"annotation_id": "Student_1/page-1.jpg#2", "bbox_xywh": [5, 6, 7, 8], "shape_candidate": "circle"},
                ],
            }
        ],
        "failure_examples": ["Student_1/page-2.jpg"],
    }
    review = build_mark_review_template(report, per_shape=1, failure_page_limit=1)
    assert review["status"] == "pending_manual_review"
    assert review["pages"][0]["ground_truth_mark_count"] is None
    assert evaluate_mark_review(review)["status"] == "pending_manual_review"

    for page in review["pages"]:
        page["ground_truth_mark_count"] = 1 if page["sample_id"] == "Student_1/page-1.jpg" else 0
    review["annotations"][0]["verdict"] = "true_positive"
    review["annotations"][1]["verdict"] = "false_positive"
    assert evaluate_mark_review(review)["status"] == "pending_manual_review"
    review["annotations"][0]["ground_truth_mark_id"] = "mark-1"
    result = evaluate_mark_review(review)
    assert result["status"] == "complete"
    assert result["metrics"] == {"precision": 0.5, "recall": 1.0, "true_positive": 1, "false_positive": 1, "false_negative": 0}
