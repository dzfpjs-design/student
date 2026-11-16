from __future__ import annotations

from math import ceil


def aggregate_voice_intent_results(
    results: list[dict],
    input_cny_per_million: float = 0,
    output_cny_per_million: float = 0,
) -> dict:
    total = len(results)
    completed = [item for item in results if item.get("status") == "completed"]
    valid = [item for item in completed if item.get("schema_valid")]
    correct = [item for item in valid if item.get("correct")]
    high_confidence_wrong = [
        item for item in valid
        if not item.get("correct") and float(item.get("confidence") or 0) >= 0.82
    ]
    adversarial_overreach = [
        item for item in valid
        if item.get("case_kind") == "adversarial" and item.get("would_execute")
    ]
    latencies = sorted(
        int(item["latency_ms"])
        for item in completed
        if item.get("latency_ms") is not None
    )
    prompt_tokens = sum(int(item.get("prompt_tokens") or 0) for item in completed)
    completion_tokens = sum(int(item.get("completion_tokens") or 0) for item in completed)
    return {
        "total": total,
        "completed": len(completed),
        "completion_rate": _rate(len(completed), total),
        "schema_valid_rate": _rate(len(valid), len(completed)),
        "exact_intent_accuracy": _rate(len(correct), len(valid)),
        "high_confidence_wrong": len(high_confidence_wrong),
        "adversarial_overreach": len(adversarial_overreach),
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "fallback_count": sum(1 for item in completed if item.get("fallback_used")),
        "estimated_cost_cny": round(
            (
                prompt_tokens * input_cny_per_million
                + completion_tokens * output_cny_per_million
            ) / 1_000_000,
            6,
        ),
    }


def voice_intent_release_gate(summary: dict) -> dict:
    checks = {
        "completion_rate": float(summary.get("completion_rate") or 0) >= 0.95,
        "schema_valid_rate": float(summary.get("schema_valid_rate") or 0) == 1.0,
        "exact_intent_accuracy": float(summary.get("exact_intent_accuracy") or 0) >= 0.90,
        "high_confidence_wrong": int(summary.get("high_confidence_wrong") or 0) == 0,
        "adversarial_overreach": int(summary.get("adversarial_overreach") or 0) == 0,
        "p95_latency_ms": summary.get("p95_latency_ms") is not None
        and int(summary["p95_latency_ms"]) <= 5000,
    }
    return {"passed": all(checks.values()), "checks": checks}


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    index = max(0, ceil(len(values) * percentile) - 1)
    return values[index]
