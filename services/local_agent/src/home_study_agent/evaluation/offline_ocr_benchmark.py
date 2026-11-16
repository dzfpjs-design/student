"""Repeatable local-only OCR benchmark using generated, non-child pages."""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

import cv2
import numpy as np

from .ocr_benchmark import OcrBenchmarkCase, evaluate_case


@dataclass(frozen=True)
class OfflinePageCase:
    benchmark: OcrBenchmarkCase
    variant: str


CASES = (
    OfflinePageCase(OcrBenchmarkCase("primary_printed", "primary_grade_4", "1. Compute 125 x 32", 1), "printed"),
    OfflinePageCase(OcrBenchmarkCase("junior_printed", "junior_grade_3", "1. Solve x + 3 = 8", 1), "printed"),
    OfflinePageCase(OcrBenchmarkCase("blur_retry", "primary_grade_4", "1. Compute 125 x 32", 1), "blur"),
    OfflinePageCase(OcrBenchmarkCase("spread_manual_split", "junior_grade_3", "1. Solve x + 3 = 8", 1), "double_page"),
    OfflinePageCase(OcrBenchmarkCase("handwriting_manual", "primary_grade_4", "", 0, handwriting_fields=1), "handwriting_boundary"),
    OfflinePageCase(OcrBenchmarkCase("formula_manual", "junior_grade_3", "", 0, math_structure_fields=1), "formula_boundary"),
)


def render(case: OfflinePageCase) -> bytes:
    width, height = (1400, 520) if case.variant == "double_page" else (900, 1200)
    page = np.full((height, width, 3), 245, dtype=np.uint8)
    if case.benchmark.expected_text:
        cv2.putText(page, case.benchmark.expected_text, (50, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (15, 15, 15), 2, cv2.LINE_AA)
    if case.variant == "handwriting_boundary":
        cv2.putText(page, "handwriting review field", (50, 180), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 1.1, (50, 50, 50), 2)
    if case.variant == "formula_boundary":
        cv2.putText(page, "x^2 + 2x + 1 = 0", (50, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (15, 15, 15), 2)
    if case.variant == "blur":
        page = cv2.GaussianBlur(page, (31, 31), 0)
    success, encoded = cv2.imencode(".png", page)
    if not success:
        raise RuntimeError("synthetic_page_encoding_failed")
    return encoded.tobytes()


def run(service, cases: tuple[OfflinePageCase, ...] = CASES) -> dict:
    results = []
    for index, case in enumerate(cases):
        payload = render(case)
        started = perf_counter()
        job = service.enqueue(payload, request_id=f"offline-benchmark:{case.benchmark.case_id}:{index}")
        completed = service.run(job["job_id"], payload)
        elapsed = perf_counter() - started
        if completed["status"] != "completed":
            results.append({"case_id": case.benchmark.case_id, "variant": case.variant, "status": completed["status"], "error_code": completed.get("error_code"), "latency_seconds": round(elapsed, 3)})
            continue
        item = evaluate_case(case.benchmark, completed["result"], elapsed)
        item.update({"variant": case.variant, "status": "completed", "quality_issues": completed["result"]["quality"]["issues"]})
        results.append(item)
    printed = [item for item in results if item.get("status") == "completed" and item["variant"] == "printed"]
    return {
        "suite": "synthetic_local_ocr_v1", "privacy": "generated pages only; no child page is written by this runner",
        "results": results,
        "printed_case_count": len(printed),
        "printed_cer_mean": round(sum(item["printed_cer"] for item in printed) / len(printed), 4) if printed else None,
        "limitations": ["Handwriting and math entries are boundary checks, not accuracy claims.", "Use a separately consented, de-identified local set for handwriting, formula, and marking accuracy."],
    }

