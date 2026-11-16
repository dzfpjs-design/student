"""Run the production local OCR pipeline against one base64 frame from stdin."""
from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

from home_study_agent.local_ocr.service import LocalOcrService


payload = base64.b64decode(sys.stdin.buffer.read(), validate=True)
with tempfile.TemporaryDirectory(prefix="hsc-document-ocr-probe-") as directory:
    service = LocalOcrService.default(Path(directory) / "probe.db")
    result = service._process(payload, "cpu")

quality = result.get("quality") or {}
transforms = quality.get("transforms") or {}
print(json.dumps({
    "status": "passed",
    "engine": result.get("engine"),
    "confidence": result.get("confidence"),
    "line_count": len(result.get("lines") or []),
    "text_length": sum(len(str(line.get("text") or "")) for line in result.get("lines") or []),
    "text_preview": "\n".join(str(line.get("text") or "") for line in (result.get("lines") or [])[:12])[:600],
    "orientation_degrees": transforms.get("orientation_degrees"),
    "orientation_auto_corrected": transforms.get("orientation_auto_corrected"),
    "orientation_evidence": quality.get("orientation_evidence"),
    "orientation_trials": quality.get("orientation_trials"),
    "quality_passed": quality.get("passed"),
    "quality_issues": quality.get("issues"),
    "manual_review": result.get("manual_review"),
    "raw_media_saved": False,
    "media_uploaded": False,
}, ensure_ascii=False))
