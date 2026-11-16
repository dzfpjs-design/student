"""Synthetic-only Qwen VL probe. Supply safeStorage-decrypted HSC_ASR_* in env.

No family database, image, transcript or credential is read from disk. Temporary
database state is removed on exit. Stdout is one bounded JSON receipt only.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time


def summarize_response_schema(body: object) -> dict:
    """Synthetic-probe diagnostics: fixed labels/counts only, never model text.

    Do not move this into application logging. Even unexpected JSON property
    names and validation locations may contain model-generated/private text.
    """
    from home_study_agent.document_vision import _ProviderPage
    from pydantic import ValidationError

    def type_name(value):
        return {dict: "object", list: "array", str: "string", int: "integer",
                float: "number", bool: "boolean", type(None): "null"}.get(type(value), "other")

    summary = {"stage": "envelope", "envelope_type": type_name(body)}
    if not isinstance(body, dict):
        return summary
    usage = body.get("usage")
    summary["usage_type"] = type_name(usage)
    if isinstance(usage, dict):
        summary["total_tokens_type"] = type_name(usage.get("total_tokens"))
    choices = body.get("choices")
    summary["choices_type"] = type_name(choices)
    if not isinstance(choices, list):
        return summary
    summary["choices_count"] = len(choices)
    if not choices or not isinstance(choices[0], dict):
        return summary
    choice = choices[0]
    finish = choice.get("finish_reason")
    summary["finish_reason"] = (finish if isinstance(finish, str) and finish in {
        "stop", "length", "content_filter", "tool_calls", "function_call"
    } else "missing_or_unknown")
    message = choice.get("message")
    summary["message_type"] = type_name(message)
    if not isinstance(message, dict):
        return summary
    content = message.get("content")
    summary.update(stage="content", content_type=type_name(content))
    if not isinstance(content, str):
        return summary
    summary["content_chars"] = len(content)
    try:
        parsed = json.loads(content)
    except (ValueError, RecursionError):
        summary["json_valid"] = False
        return summary
    summary.update(stage="schema", json_valid=True, root_type=type_name(parsed))
    if isinstance(parsed, dict):
        summary["root_extra_field_count"] = len(set(parsed) - {"blocks"})
        blocks = parsed.get("blocks")
        summary["blocks_type"] = type_name(blocks)
        if isinstance(blocks, list):
            summary["block_count"] = len(blocks)
            items = []
            for block in blocks[:8]:
                item = {"type": type_name(block)}
                if isinstance(block, dict):
                    fields = {"kind", "text", "bbox", "confidence"}
                    item["missing_fields"] = sorted(fields - set(block))
                    item["extra_field_count"] = len(set(block) - fields)
                    item["text_type"] = type_name(block.get("text"))
                    kind = block.get("kind")
                    item["kind"] = kind if isinstance(kind, str) and kind in {"text", "formula"} else "invalid"
                    box = block.get("bbox")
                    item["bbox_type"] = type_name(box)
                    if isinstance(box, list):
                        item["bbox_length"] = len(box)
                        numeric = all(type(n) in (float, int) and math.isfinite(n) for n in box)
                        item["bbox_finite_numbers"] = numeric
                        if numeric and len(box) == 4:
                            item["bbox_in_0_1"] = all(0 <= n <= 1 for n in box)
                            item["bbox_in_0_1000"] = all(0 <= n <= 1000 for n in box)
                            item["bbox_ordered"] = box[0] < box[2] and box[1] < box[3]
                    confidence = block.get("confidence")
                    item["confidence_in_0_1"] = (type(confidence) in (int, float)
                                                  and math.isfinite(confidence) and 0 <= confidence <= 1)
                items.append(item)
            summary["blocks"] = items
    try:
        _ProviderPage.model_validate_json(content)
        summary["validation"] = "passed"
    except ValidationError as error:
        summary["validation"] = "failed"
        known_fields = {"blocks", "kind", "text", "bbox", "confidence"}
        known_errors = {"json_invalid", "model_type", "list_type", "string_type", "float_type", "finite_number",
                        "literal_error", "missing", "extra_forbidden", "too_short", "too_long",
                        "greater_than_equal", "less_than_equal", "string_too_short", "string_too_long", "value_error"}
        summary["issue_count"] = error.error_count()
        summary["issues"] = [
            {"type": issue["type"] if issue["type"] in known_errors else "validation_error",
             "location": [part if (type(part) is int or (isinstance(part, str) and part in known_fields)) else "unknown_field"
                          for part in issue["loc"][:5]]}
            for issue in error.errors(include_url=False, include_context=False, include_input=False)[:12]
        ]
    return summary


def main() -> int:
    receipt = {"status": "blocked", "model": "qwen3-vl-plus", "token": None,
               "latency": 0, "error_code": None}
    started = time.monotonic()
    # Local OCR dependencies may log their model paths. Keep the probe's output
    # contract independent of third-party stdout/stderr and exception messages.
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            import cv2
            import numpy as np
            from home_study_agent.config import Settings, dashscope_vision_base_url
            from home_study_agent.database import Database
            from home_study_agent.document_vision import DocumentVisionError, DocumentVisionService, MODELS

            class SyntheticSchemaProbeService(DocumentVisionService):
                def _parse(self, body, page):
                    # This class only sees the generated page below, never a
                    # caller-supplied image. No raw response escapes this method.
                    try:
                        receipt["schema_summary"] = summarize_response_schema(body)
                        usage = body.get("usage") if isinstance(body, dict) else None
                        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
                        if type(tokens) is int and 0 <= tokens <= 10_000_000:
                            receipt["token"] = tokens
                    except Exception:
                        receipt["schema_summary"] = {"stage": "diagnostic_unavailable"}
                    return super()._parse(body, page)

            settings = Settings.from_environment()
            if settings.document_vision_model in MODELS:
                receipt["model"] = settings.document_vision_model
            else:
                raise DocumentVisionError("DOCUMENT_VISION_NOT_CONFIGURED")
            with TemporaryDirectory(prefix="hsc-synthetic-vision-") as temporary:
                path = Path(temporary)
                # A synthetic probe is explicit authorization for this one request;
                # an explicit deployment-level disabled setting still wins.
                settings = replace(
                    settings, data_dir=path, database_path=path / "probe.db",
                    document_vision_enabled=(settings.document_vision_enabled
                                             if "HSC_DOCUMENT_VISION_ENABLED" in os.environ else True),
                    document_vision_base_url=(settings.document_vision_base_url
                                             or dashscope_vision_base_url(settings.cloud_asr_base_url)),
                )
                database = Database(settings.database_path)
                database.initialize()
                with database.connect() as db:
                    db.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                               VALUES ('synthetic-child','Synthetic Probe','4','test','active',datetime('now'),datetime('now'))""")
                    db.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                               VALUES ('synthetic-session','synthetic-child','active','study',datetime('now'),datetime('now'),'synthetic-start')""")
                    db.execute("""INSERT INTO privacy_preference(singleton_id,cloud_model_text_allowed,cloud_audio_allowed,
                               raw_media_storage_allowed,model_improvement_allowed,model_call_retention_days,tutor_turn_retention_days,
                               report_retention_days,updated_at) VALUES (1,1,0,0,0,90,180,730,datetime('now'))""")
                    db.commit()
                frame = np.full((1000, 800, 3), (40, 75, 40), dtype=np.uint8)
                cv2.rectangle(frame, (100, 80), (700, 920), (255, 255, 255), -1)
                for y, text in zip((220, 340, 460, 580), (
                    "1. Calculate 12 + 8", "2. Solve x + 3 = 7",
                    "3. Calculate 9 / 3", "4. Find 6 * 7",
                )):
                    cv2.putText(frame, text, (150, y), cv2.FONT_HERSHEY_SIMPLEX, .9,
                                (0, 0, 0), 2, cv2.LINE_AA)
                ok, image = cv2.imencode(".png", frame)
                if not ok:
                    raise DocumentVisionError("DOCUMENT_VISION_ENCODING_FAILED")
                # No test transport or OCR stub. Production image gate + HTTP.
                result = SyntheticSchemaProbeService(settings, database).analyze_page(
                    image.tobytes(), child_id="synthetic-child", session_id="synthetic-session",
                    request_id="synthetic-probe", local_ocr_insufficient=True, allowed=True,
                )
                receipt.update(status="ok", model=result["model"], token=result["total_tokens"],
                               latency=result["latency_ms"])
        except Exception as error:
            # Never serialize str(error), repr(error), a response body, or traceback.
            code = getattr(error, "code", None)
            import re
            receipt["error_code"] = (code if isinstance(code, str) and re.fullmatch(r"DOCUMENT_VISION_[A-Z0-9_]+", code)
                                     else "DOCUMENT_VISION_PROBE_FAILED")
            receipt["latency"] = round((time.monotonic() - started) * 1000)
    print(json.dumps(receipt, separators=(",", ":")))
    return 0 if receipt["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
