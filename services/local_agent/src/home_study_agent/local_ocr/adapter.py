from __future__ import annotations


class OcrArtifactAdapter:
    """Converts new pipeline output to the existing capture_artifact details shape.

    The caller owns session authorization and raw-media storage. This adapter
    does not accept a path or image bytes, so it cannot bypass local privacy.
    """

    @staticmethod
    def capture_details(result: dict) -> dict:
        return {
            "lines": result["lines"],
            "question_blocks": result["question_blocks"],
            "engine": result["engine"],
            "ocr_confidence": result["confidence"],
            "quality": result["quality"],
            "manual_review": result["manual_review"],
            "formula_routing": result.get("formula_routing", {}),
            "pipeline_privacy": result["privacy"],
        }
