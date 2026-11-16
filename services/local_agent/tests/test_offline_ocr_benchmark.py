from __future__ import annotations

from pathlib import Path

import numpy as np

from home_study_agent.evaluation.offline_ocr_benchmark import run
from home_study_agent.local_ocr import LocalOcrService


class FakeEngine:
    name = "benchmark-fake"
    capability = {"printed_en"}

    def health(self) -> dict:
        return {"ready": True, "route": "cpu"}

    def recognize(self, image: np.ndarray, route: str) -> list[dict]:
        return [{"text": "1. Compute 125 x 32", "confidence": .95, "box": None, "kind": "printed"}]


def test_synthetic_benchmark_reports_boundaries_without_claiming_handwriting_or_math_accuracy(tmp_path: Path) -> None:
    report = run(LocalOcrService(tmp_path / "benchmark.db", [FakeEngine()]))
    assert report["privacy"].startswith("generated")
    handwriting = next(item for item in report["results"] if item["case_id"] == "handwriting_manual")
    formula = next(item for item in report["results"] if item["case_id"] == "formula_manual")
    assert handwriting["handwriting_evaluated"] is False
    assert formula["math_structure_evaluated"] is False
