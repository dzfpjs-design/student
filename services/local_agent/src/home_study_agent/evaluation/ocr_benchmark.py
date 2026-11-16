from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OcrBenchmarkCase:
    case_id: str
    grade_band: str
    expected_text: str
    expected_question_count: int
    handwriting_fields: int = 0
    math_structure_fields: int = 0


def character_error_rate(expected: str, actual: str) -> float:
    """Unicode-safe Levenshtein CER; this is only for printed-text cases."""
    if not expected:
        return 0.0 if not actual else 1.0
    previous = list(range(len(actual) + 1))
    for i, expected_char in enumerate(expected, 1):
        current = [i]
        for j, actual_char in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (expected_char != actual_char)))
        previous = current
    return previous[-1] / len(expected)


def evaluate_case(case: OcrBenchmarkCase, result: dict, elapsed_seconds: float) -> dict:
    text = "\n".join(line.get("text", "") for line in result.get("lines", []))
    blocks = result.get("question_blocks", [])
    manual = result.get("manual_review", {})
    return {"case_id": case.case_id, "grade_band": case.grade_band,
            "printed_cer": round(character_error_rate(case.expected_text, text), 4),
            "question_block_exact": len(blocks) == case.expected_question_count,
            "question_block_count": len(blocks), "latency_seconds": round(elapsed_seconds, 3),
            "handwriting_fields_expected": case.handwriting_fields,
            "math_structure_fields_expected": case.math_structure_fields,
            "handwriting_evaluated": False, "math_structure_evaluated": False,
            "manual_correction_available": bool(manual),
            "note": "Printed CER is not used to represent handwriting or math-expression quality."}

