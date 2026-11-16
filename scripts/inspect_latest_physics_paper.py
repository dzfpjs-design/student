"""Read the latest document capture and segmentation without reading raw page media."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


database = Path(sys.argv[1])
child_id = sys.argv[2] if len(sys.argv) > 2 else "child-junior-3"
artifact_id = sys.argv[3] if len(sys.argv) > 3 else None

with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
    connection.row_factory = sqlite3.Row
    artifact_row = connection.execute(
        """
        SELECT artifact_id, child_id, session_id, status, confidence,
               extracted_text, details_json, local_path, created_at
        FROM capture_artifact
        WHERE artifact_type = 'document_ocr' AND child_id = ?
          AND (? IS NULL OR artifact_id = ?)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (child_id, artifact_id, artifact_id),
    ).fetchone()
    if artifact_row is None:
        raise SystemExit("NO_DOCUMENT_ARTIFACT_FOR_CHILD")

    artifact = dict(artifact_row)
    details = json.loads(artifact.pop("details_json") or "{}")
    pipeline = details.get("local_pipeline") or {}
    quality = pipeline.get("quality") or details.get("quality") or {}
    artifact["raw_media_saved"] = bool(artifact.pop("local_path", None))
    artifact["ocr"] = {
        "engine": pipeline.get("engine") or details.get("engine"),
        "orientation_degrees": (quality.get("transforms") or {}).get("orientation_degrees"),
        "orientation_ambiguous": (quality.get("orientation_evidence") or {}).get("ambiguous"),
        "quality_issues": quality.get("issues") or [],
        "line_count": len(details.get("lines") or []),
        "question_blocks": pipeline.get("question_blocks") or [],
        "wrong_question_candidates": pipeline.get("wrong_question_candidates") or [],
        "cloud_vision_fallback": details.get("cloud_vision_fallback"),
    }

    candidates = [
        dict(row)
        for row in connection.execute(
            """
            SELECT candidate_id, question_index, question_text, region_json,
                   confidence, status, mistake_id, created_at, updated_at
            FROM mistake_capture_candidate
            WHERE artifact_id = ?
            ORDER BY question_index
            """,
            (artifact["artifact_id"],),
        ).fetchall()
    ]
    for candidate in candidates:
        candidate["region"] = json.loads(candidate.pop("region_json") or "null")

    mistakes = [
        dict(row)
        for row in connection.execute(
            """
            SELECT mistake_id, knowledge_node_id, subject, title, status, created_at
            FROM mistake_case
            WHERE source_ref LIKE ?
            ORDER BY created_at
            """,
            (f"{artifact['artifact_id']}#%",),
        ).fetchall()
    ]
    vision_requests = [
        dict(row)
        for row in connection.execute(
            """
            SELECT request_id, status, reserved_tokens, charged_tokens,
                   created_at, retry_at, error_code
            FROM document_vision_request
            WHERE child_id = ? AND request_id = ?
            """,
            (child_id, f"artifact:{artifact['artifact_id']}"),
        ).fetchall()
    ]
    checks = [row[0] for row in connection.execute("PRAGMA quick_check").fetchall()]
    monitor = connection.execute(
        "SELECT monitor_id,status,generation,current_state FROM document_monitor_session WHERE session_id=?",
        (artifact["session_id"],),
    ).fetchone()
    revisions = []
    if monitor:
        revisions = [
            dict(row) for row in connection.execute(
                """SELECT revision_id,artifact_id,revision_index,previous_revision_id,
                          change_summary_json,study_signal_json,created_at
                   FROM document_page_revision WHERE monitor_id=?
                   ORDER BY revision_index""",
                (monitor["monitor_id"],),
            ).fetchall()
        ]
        for revision in revisions:
            revision["change_summary"] = json.loads(revision.pop("change_summary_json") or "{}")
            revision["study_signal"] = json.loads(revision.pop("study_signal_json") or "{}")

print(json.dumps({
    "database_quick_check": checks,
    "artifact": artifact,
    "candidate_count": len(candidates),
    "candidates": candidates,
    "captured_mistake_count": len(mistakes),
    "captured_mistakes": mistakes,
    "document_vision_requests": vision_requests,
    "document_monitor": dict(monitor) if monitor else None,
    "document_revisions": revisions,
    "raw_media_read": False,
    "raw_media_copied": False,
}, ensure_ascii=False))
