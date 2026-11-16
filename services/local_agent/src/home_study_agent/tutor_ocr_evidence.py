"""Server-side guard for OCR-backed tutor questions.

Images never leave the local OCR route.  This gate receives only an existing
local artifact ID plus the child's explicit confirmation that the displayed
text is complete.  It prevents an uncertain crop/OCR result from silently
becoming a tutoring prompt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


MIN_TUTOR_OCR_CONFIDENCE = 0.75


@dataclass(frozen=True)
class TutorOcrEvidenceDecision:
    allowed: bool
    code: str | None = None
    message: str | None = None


def evaluate_tutor_ocr_evidence(
    artifact: dict[str, Any] | None,
    confirmation: str | None,
) -> TutorOcrEvidenceDecision:
    if artifact is None:
        return TutorOcrEvidenceDecision(False, "OCR_EVIDENCE_NOT_FOUND", "这张题目没有找到，请重新拍题或手动输入。")
    if confirmation != "confirmed":
        return TutorOcrEvidenceDecision(False, "OCR_CONFIRMATION_REQUIRED", "请先核对题干、数字和选项，再开始讲解。")
    try:
        details = json.loads(artifact.get("details_json") or "{}")
    except (TypeError, ValueError):
        details = {}
    quality = details.get("quality") if isinstance(details, dict) else None
    quality_passed = isinstance(quality, dict) and quality.get("passed") is True
    confidence = artifact.get("confidence")
    if artifact.get("status") != "completed" or not quality_passed or not isinstance(confidence, (int, float)) or confidence < MIN_TUTOR_OCR_CONFIDENCE:
        return TutorOcrEvidenceDecision(False, "OCR_REVIEW_REQUIRED", "题干还不够清楚，先重拍或逐字核对；我不会按不确定的题目讲解。")
    return TutorOcrEvidenceDecision(True)


class TutorOcrEvidenceError(Exception):
    def __init__(self, decision: TutorOcrEvidenceDecision):
        self.code = decision.code or "OCR_EVIDENCE_NOT_READY"
        self.message = decision.message or "题目证据暂时不能用于讲解。"
        super().__init__(self.code)
