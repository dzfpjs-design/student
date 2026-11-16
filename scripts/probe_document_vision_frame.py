"""Validate production page vision with one base64 frame received on stdin.

The raw frame and the provider credential remain memory-only.  The temporary
database contains a synthetic child/session and is removed before exit.
"""
from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import time


def main() -> int:
    receipt = {
        "status": "blocked",
        "error_code": "DOCUMENT_VISION_PROBE_FAILED",
        "raw_media_saved": False,
        "original_frame_uploaded": False,
    }
    started = time.monotonic()
    metrics = None
    region_metrics = None
    try:
        encoded = sys.stdin.buffer.read()
        frame = base64.b64decode(encoded, validate=True)
        if not frame:
            raise ValueError("empty frame")
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "local_agent" / "src"))
            from home_study_agent.config import Settings, dashscope_vision_base_url
            from home_study_agent.database import Database
            from home_study_agent.document_vision import DocumentVisionService
            from home_study_agent.local_ocr.service import LocalOcrService, RapidOcrEngine, _rotate_page
            import cv2
            import numpy as np

            with TemporaryDirectory(prefix="hsc-real-page-vision-") as temporary:
                root = Path(temporary)
                settings = Settings.from_environment()
                settings = replace(
                    settings,
                    data_dir=root,
                    database_path=root / "probe.db",
                    document_vision_enabled=True,
                    document_vision_base_url=(settings.document_vision_base_url
                                             or dashscope_vision_base_url(settings.cloud_asr_base_url)),
                )
                database = Database(settings.database_path)
                database.initialize()
                with database.connect() as connection:
                    connection.execute("""INSERT INTO child_profile(
                      child_id,nickname,grade,curriculum_region,status,created_at,updated_at
                    ) VALUES ('synthetic-child','Vision Probe','4','test','active',datetime('now'),datetime('now'))""")
                    connection.execute("""INSERT INTO daily_session(
                      session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id
                    ) VALUES ('synthetic-session','synthetic-child','active','study',datetime('now'),datetime('now'),'probe-start')""")
                    connection.execute("""INSERT INTO privacy_preference(
                      singleton_id,cloud_model_text_allowed,cloud_audio_allowed,raw_media_storage_allowed,
                      model_improvement_allowed,model_call_retention_days,tutor_turn_retention_days,
                      report_retention_days,updated_at
                    ) VALUES (1,1,0,0,0,90,180,730,datetime('now'))""")
                    connection.commit()
                local = LocalOcrService.default(root / "local-ocr.db")._process(frame, "cpu")
                quality = local.get("quality") or {}
                evidence = quality.get("orientation_evidence") or {}
                degrees = (quality.get("transforms") or {}).get("orientation_degrees", 0)
                if evidence.get("ambiguous") is not False or degrees not in {0, 90, 180, 270}:
                    degrees = 0
                decoded = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
                oriented = _rotate_page(decoded, degrees)
                gray = cv2.cvtColor(oriented, cv2.COLOR_BGR2GRAY)
                saturation = cv2.cvtColor(oriented, cv2.COLOR_BGR2HSV)[:, :, 1]
                ink = cv2.inRange(gray, 0, 170)
                metrics = {
                    "light_ratio": round(float((gray >= 185).mean()), 4),
                    "saturated_ratio": round(float((saturation > 45).mean()), 4),
                    "ink_ratio": round(float((ink > 0).mean()), 4),
                }
                diagnostic_lines = RapidOcrEngine().recognize(oriented, "cpu")
                accepted = 0
                region_ink_pixels = 0
                rejected_dark = rejected_color = rejected_ink = rejected_geometry = 0
                for line in diagnostic_lines:
                    polygon = np.asarray(line.get("box"), dtype=np.float32)
                    if polygon.shape != (4, 2) or not np.isfinite(polygon).all():
                        rejected_geometry += 1
                        continue
                    x, y, bw, bh = cv2.boundingRect(polygon)
                    if (x < 0 or y < 0 or x + bw > gray.shape[1] or y + bh > gray.shape[0]
                            or bh > gray.shape[0] * .14 or bw > gray.shape[1] * .98):
                        rejected_geometry += 1
                        continue
                    region = gray[y:y + bh, x:x + bw]
                    sat = saturation[y:y + bh, x:x + bw]
                    if np.median(region) < 105:
                        rejected_dark += 1
                        continue
                    if (sat > 80).mean() > .25:
                        rejected_color += 1
                        continue
                    _, local_ink = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                    ratio = float((local_ink > 0).mean())
                    if not .001 <= ratio <= .55:
                        rejected_ink += 1
                        continue
                    accepted += 1
                    region_ink_pixels += int((local_ink > 0).sum())
                region_metrics = {
                    "recognized_lines": len(diagnostic_lines), "accepted_lines": accepted,
                    "rejected_dark": rejected_dark, "rejected_color": rejected_color,
                    "rejected_ink": rejected_ink, "rejected_geometry": rejected_geometry,
                    "estimated_ink_ratio": round(region_ink_pixels / gray.size, 6),
                }
                result = DocumentVisionService(settings, database).analyze_page(
                    frame,
                    child_id="synthetic-child",
                    session_id="synthetic-session",
                    request_id="real-page-probe",
                    local_ocr_insufficient=True,
                    allowed=True,
                    orientation_degrees=degrees,
                )
                text = "\n".join(str(block.get("text", "")) for block in result.get("blocks", []))
                receipt = {
                    "status": "ok",
                    "model": result.get("model"),
                    "latency_ms": result.get("latency_ms"),
                    "total_tokens": result.get("total_tokens"),
                    "block_count": len(result.get("blocks", [])),
                    "text_preview": text[:1200],
                    "review_required": result.get("review_required"),
                    "can_judge_correctness": result.get("can_judge_correctness"),
                    "can_update_mastery": result.get("can_update_mastery"),
                    "privacy": result.get("privacy"),
                    "orientation_degrees": degrees,
                    "raw_media_saved": False,
                    "original_frame_uploaded": False,
                }
    except Exception as error:
        code = getattr(error, "code", None)
        receipt["error_code"] = code if isinstance(code, str) and re.fullmatch(r"DOCUMENT_VISION_[A-Z0-9_]+", code) else "DOCUMENT_VISION_PROBE_FAILED"
        receipt["latency_ms"] = round((time.monotonic() - started) * 1000)
        if metrics is not None:
            receipt["document_metrics"] = metrics
        if region_metrics is not None:
            receipt["region_metrics"] = region_metrics
    print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
    return 0 if receipt["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
