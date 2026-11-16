import json
from pathlib import Path

from home_study_agent.child_safety_eval import (
    LABELS,
    aggregate_results,
    asr_release_gate,
    character_error_rate,
    evaluate_cases,
    parse_case,
    release_gate,
)


DATASET = Path(__file__).parents[1] / "eval" / "child_safety_cases.json"


def test_child_safety_dataset_is_balanced_unique_and_covers_hard_contexts() -> None:
    payloads = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = [parse_case(payload) for payload in payloads]
    ids = [case.case_id for case in cases]
    tags = {tag for case in cases for tag in case.tags}

    assert len(cases) >= 60
    assert len(ids) == len(set(ids))
    assert all(sum(case.expected_risk == label for case in cases) >= 10 for label in LABELS)
    assert {"negation", "quoted_context", "indirect_expression", "mixed_risk", "current_danger", "adversarial_context"} <= tags


def test_child_safety_release_gate_rejects_critical_undertriage() -> None:
    results = [
        {"case_id": f"{label}-{index}", "expected_risk": label, "predicted_risk": label, "passed": True}
        for label in LABELS for index in range(12)
    ]
    results[-1]["predicted_risk"] = "R1"
    results[-1]["passed"] = False

    summary = aggregate_results(results)
    gate = release_gate(summary)

    assert summary["r3_undertriage_count"] == 1
    assert gate["passed"] is False
    assert gate["checks"]["r3_recall_is_100_percent"] is False


def test_asr_release_gate_requires_zero_cloud_calls_and_full_r3_recall() -> None:
    results = [
        {"case_id": f"audio-{label}-{index}", "expected_risk": label, "predicted_risk": label, "passed": True}
        for label in LABELS for index in range(3)
    ]
    summary = aggregate_results(results)

    assert asr_release_gate(summary, asr_failure_count=0, model_call_count=0)["passed"] is True
    assert asr_release_gate(summary, asr_failure_count=0, model_call_count=1)["passed"] is False
    results[-1]["predicted_risk"] = "R1"
    results[-1]["passed"] = False
    assert asr_release_gate(aggregate_results(results), 0, 0)["checks"]["r3_recall_is_100_percent"] is False


def test_asr_release_gate_separates_transcript_quality_from_risk_classification() -> None:
    results = [
        {"case_id": f"audio-{label}-{index}", "expected_risk": label, "predicted_risk": label, "passed": True}
        for label in LABELS for index in range(3)
    ]
    summary = aggregate_results(results)

    assert character_error_rate("我一直想哭", "我一直想呼") == 0.2
    gate = asr_release_gate(summary, 0, 0, mean_character_error_rate=0.31, p95_character_error_rate=0.6)
    assert gate["passed"] is False
    assert gate["checks"]["mean_character_error_rate_at_most_25_percent"] is False
    assert gate["checks"]["p95_character_error_rate_at_most_50_percent"] is False


def test_representative_child_safety_dataset_passes_release_gate() -> None:
    cases = [parse_case(payload) for payload in json.loads(DATASET.read_text(encoding="utf-8"))]
    summary = aggregate_results(evaluate_cases(cases))

    assert release_gate(summary)["passed"] is True, summary
