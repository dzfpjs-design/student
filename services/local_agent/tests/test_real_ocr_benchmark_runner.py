from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_local_ocr_real_benchmark.py"
SPEC = importlib.util.spec_from_file_location("real_ocr_benchmark_runner", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def test_real_benchmark_runner_writes_aggregate_without_extracted_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    image = np.full((300, 800, 3), 255, dtype=np.uint8)
    cv2.putText(image, "1. Compute 125 x 32", (30, 150), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    page = tmp_path / "approved-sample.png"
    page.write_bytes(encoded.tobytes())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"cases": [
        {"case_id": "printed", "category": "printed", "path": str(page), "expected_text": "1. Compute 125 x 32", "expected_question_count": 1},
        {"case_id": "handwriting", "category": "handwriting", "path": str(page), "expected_question_count": 1},
    ]}), encoding="utf-8")
    output = tmp_path / "aggregate.json"
    monkeypatch.setattr("sys.argv", ["runner", str(manifest), "--data-dir", str(tmp_path / "runtime"), "--output", str(output)])
    RUNNER.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["acceptance_status"] == "blocked_missing_specialist_models"
    assert report["results"][0]["category"] == "printed"
    assert report["results"][1]["accuracy"] == "not_evaluated_missing_local_specialist_provider"
    assert "extracted_text" not in output.read_text(encoding="utf-8")
    assert '"cases": 2' in capsys.readouterr().out


def test_real_benchmark_keeps_grade_strata_and_manual_specialist_reviews_separate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = np.full((160, 320, 3), 255, dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    page = tmp_path / "approved-sample.png"
    page.write_bytes(encoded.tobytes())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"cases": [
        {"case_id": "g4-handwriting", "grade_band": "grade4", "source_kind": "high_camera", "category": "handwriting", "path": str(page), "manual_field_review": {"expected_fields": 3, "correct_fields": 2}},
        {"case_id": "g9-formula", "grade_band": "grade9", "source_kind": "high_camera", "category": "math", "path": str(page), "manual_field_review": {"expected_fields": 2, "correct_fields": 1}},
        {"case_id": "g9-marks", "grade_band": "grade9", "source_kind": "high_camera", "category": "marking", "path": str(page), "manual_mark_review": {"ground_truth_mark_count": 4, "true_positive_count": 3, "false_positive_count": 2}},
    ]}), encoding="utf-8")
    output = tmp_path / "aggregate.json"
    monkeypatch.setattr("sys.argv", ["runner", str(manifest), "--data-dir", str(tmp_path / "runtime"), "--output", str(output)])

    RUNNER.main()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["family_high_camera"]["grade4"]["metrics"]["handwriting_field_accuracy"] == "not_evaluated_missing_local_specialist_provider"
    assert report["family_high_camera"]["grade9"]["metrics"]["math_structure_field_accuracy"] == "not_evaluated_missing_local_specialist_provider"
    assert report["representative_strata"]["grade4"]["handwriting_field"]["status"] == "not_evaluated_missing_local_specialist_provider"
    assert report["representative_strata"]["grade9"]["formula_structure"]["metrics"]["math_structure_accuracy"] is None
    assert report["metrics"]["printed_cer_mean_across_declared_printed_scripts"] is None
    assert "expected_fields" not in json.dumps(report["results"])


def test_real_benchmark_reports_chinese_english_and_numeric_printed_cer_separately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = np.full((160, 320, 3), 255, dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    page = tmp_path / "approved-sample.png"
    page.write_bytes(encoded.tobytes())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"cases": [
        {"case_id": "g4-zh", "grade_band": "grade4", "category": "printed", "printed_script": "chinese", "path": str(page), "expected_text": "甲", "expected_question_count": 0},
        {"case_id": "g4-en", "grade_band": "grade4", "category": "printed", "printed_script": "english", "path": str(page), "expected_text": "A", "expected_question_count": 0},
        {"case_id": "g4-num", "grade_band": "grade4", "category": "printed", "printed_script": "numeric", "path": str(page), "expected_text": "123", "expected_question_count": 0},
    ]}), encoding="utf-8")
    output = tmp_path / "aggregate.json"
    monkeypatch.setattr("sys.argv", ["runner", str(manifest), "--data-dir", str(tmp_path / "runtime"), "--output", str(output)])

    RUNNER.main()

    report = json.loads(output.read_text(encoding="utf-8"))
    strata = report["representative_strata"]["grade4"]
    assert all(strata[f"printed_{script}"]["sample_count"] == 1 for script in ("chinese", "english", "numeric"))
    assert set(report["metrics"]["printed_cer_by_script"]) == {"chinese", "english", "numeric"}


def test_real_benchmark_runner_reports_missing_approved_samples_without_fabricating_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"asset_status": "No de-identified local samples yet."}), encoding="utf-8")
    output = tmp_path / "aggregate.json"
    monkeypatch.setattr("sys.argv", ["runner", str(manifest), "--data-dir", str(tmp_path / "runtime"), "--output", str(output)])

    RUNNER.main()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "awaiting_parent_approved_local_samples"
    assert report["metrics"]["printed_cer_mean_across_declared_printed_scripts"] is None
    assert report["metrics"]["handwriting_accuracy"] == "not_evaluated_without_parent_approved_local_samples"
    assert report["representative_strata"]["grade4"]["printed_chinese"]["status"] == "not_evaluated_without_parent_approved_local_samples"
    assert "extracted_text" not in output.read_text(encoding="utf-8")
    assert '"cases": 0' in capsys.readouterr().out


def test_real_benchmark_runner_is_executable_as_a_script(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"asset_status": "No de-identified local samples yet."}), encoding="utf-8")
    output = tmp_path / "aggregate.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(manifest), "--data-dir", str(tmp_path / "runtime"), "--output", str(output)],
        check=False, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SCRIPT.parents[1] / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "awaiting_parent_approved_local_samples"


def test_real_benchmark_e2e_uses_local_correction_and_records_only_aggregate_result(tmp_path: Path) -> None:
    image = np.full((300, 800, 3), 255, dtype=np.uint8)
    cv2.putText(image, "1. Compute 125 x 32", (30, 150), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    page = tmp_path / "approved-sample.png"
    page.write_bytes(encoded.tobytes())
    results = [{"case_id": "printed", "category": "printed", "status": "completed"}]

    RUNNER._run_e2e_cases([{
        "case_id": "printed", "category": "printed", "path": str(page),
        "grade_band": "grade9",
        "end_to_end": {"child_id": "child-junior-3", "subject": "数学", "knowledge_query": "判别式", "corrected_question_text": "判别式练习题", "submitted_answer": "待人工确认"},
    }], results, tmp_path / "e2e")

    assert results[0]["end_to_end_attempted"] is True
    assert results[0]["end_to_end_success"] is True, results
    assert "corrected_question_text" not in results[0]
    assert not (tmp_path / "e2e" / "media").exists()
    assert results[0]["session_completion_persisted"] is True
    assert results[0]["raw_media_retained"] is False
    assert results[0]["derived_ocr_evidence_retained"] is True
    assert results[0]["restart_weakness_evidence_retained"] is True


def test_real_benchmark_e2e_finishes_each_session_before_the_next_same_child_case(tmp_path: Path) -> None:
    image = np.full((300, 800, 3), 255, dtype=np.uint8)
    cv2.putText(image, "1. Compute 125 x 32", (30, 150), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    page = tmp_path / "approved-sample.png"
    page.write_bytes(encoded.tobytes())
    cases = [{
        "case_id": f"printed-{index}", "category": "printed", "path": str(page), "grade_band": "grade9",
        "end_to_end": {"child_id": "child-junior-3", "subject": "数学", "knowledge_query": "判别式", "corrected_question_text": "判别式练习题"},
    } for index in range(2)]
    results = [{"case_id": case["case_id"], "category": "printed", "status": "completed"} for case in cases]

    RUNNER._run_e2e_cases(cases, results, tmp_path / "e2e")

    assert [item["end_to_end_success"] for item in results] == [True, True]
    assert [item["session_completion_persisted"] for item in results] == [True, True]


def test_e2e_metrics_report_only_aggregate_manual_work_and_failure_stages() -> None:
    values = [
        {"end_to_end_success": True, "review_queue_items_created": 3, "review_action": "confirm", "raw_media_retained": False, "restart_weakness_evidence_retained": True},
        {"end_to_end_success": False, "end_to_end_failure": "review:HTTPStatusError", "review_queue_items_created": 1, "review_action": "skip", "raw_media_retained": False, "restart_weakness_evidence_retained": False},
    ]

    assert RUNNER._failure_buckets(values) == {"review": 1}
    assert RUNNER._ocr_failure_buckets([{"status": "failed", "error_code": "ValueError"}, {"status": "completed"}]) == {"ValueError": 1}
    assert sum(int(item.get("review_queue_items_created", 0)) for item in values) == 4
    assert sum(item.get("review_action") == "confirm" for item in values) == 1
    assert sum(item.get("review_action") == "skip" for item in values) == 1
    assert RUNNER._acceptance_status([{"status": "completed"}], values) == "failed"
    assert RUNNER._acceptance_status([{"status": "completed", "accuracy": "not_evaluated_missing_local_specialist_provider"}], []) == "blocked_missing_specialist_models"
    assert RUNNER._acceptance_status([{"status": "completed"}], []) == "passed"


def test_real_benchmark_e2e_rejects_grade_and_child_profile_mismatch_before_writing(tmp_path: Path) -> None:
    image = np.full((300, 800, 3), 255, dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    page = tmp_path / "approved-sample.png"
    page.write_bytes(encoded.tobytes())
    results = [{"case_id": "printed", "category": "printed", "status": "completed"}]

    RUNNER._run_e2e_cases([{
        "case_id": "printed", "category": "printed", "grade_band": "grade4", "path": str(page),
        "end_to_end": {"child_id": "child-junior-3", "subject": "数学", "knowledge_query": "判别式", "corrected_question_text": "判别式练习题"},
    }], results, tmp_path / "e2e")

    assert results[0]["end_to_end_attempted"] is True
    assert results[0]["end_to_end_success"] is False
    assert results[0]["end_to_end_failure"] == "grade_band_child_profile_mismatch"
    assert not (tmp_path / "e2e" / "media").exists()
