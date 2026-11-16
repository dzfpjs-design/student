from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_behavior_model_consistency_evaluator.py"
SPEC = importlib.util.spec_from_file_location("behavior_model_consistency_evaluator", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def test_behavior_consistency_matrix_is_aggregate_only_and_not_an_education_claim(tmp_path: Path) -> None:
    report = RUNNER.run_behavior_model_consistency_evaluator(tmp_path)

    assert report["algorithm_consistency_passed"] is True
    assert report["education_effect_verified"] is False
    assert set(report["matrix"]) == {
        "single_event_not_long_term", "low_quality_not_polluting", "shadow_not_longitudinal",
        "correction_retraction", "repeat_misclassification_shadow", "restart_replay",
        "cross_child_isolation", "missing_days_no_trend", "cooldown_and_budget",
    }
    assert all(item["passed"] for item in report["matrix"].values())
    assert "raw" not in str(report).lower()
