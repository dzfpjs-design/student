"""Read-only Windows document OCR diagnostics without copying page media."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def decode(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def rows(connection, query, parameters=()):
    try:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]
    except sqlite3.OperationalError as error:
        return [{"query_error": str(error)}]


database = Path(sys.argv[1])
uri = f"file:{database.as_posix()}?mode=ro"
with sqlite3.connect(uri, uri=True) as connection:
    connection.row_factory = sqlite3.Row
    monitor_sessions = rows(connection, """
        SELECT session_id,child_id,status,generation,updated_at
        FROM document_monitor_session ORDER BY updated_at DESC LIMIT 5
    """)
    daily_sessions = rows(connection, """
        SELECT session_id,child_id,status,state,started_at,ended_at,last_checkpoint_at
        FROM daily_session ORDER BY started_at DESC LIMIT 5
    """)
    monitor_events = rows(connection, """
        SELECT session_id,event_type,confidence,metrics_json,observed_at
        FROM document_monitor_event ORDER BY observed_at DESC LIMIT 20
    """)
    for event in monitor_events:
        event["metrics"] = decode(event.pop("metrics_json", None))
    revisions = rows(connection, """
        SELECT revision_id,session_id,artifact_id,revision_index,
               review_required,ocr_confidence,created_at
        FROM document_page_revision ORDER BY created_at DESC LIMIT 10
    """)
    artifacts = rows(connection, """
        SELECT artifact_id,session_id,status,confidence,LENGTH(extracted_text) text_length,
               details_json,created_at
        FROM capture_artifact WHERE artifact_type='document_ocr'
        ORDER BY created_at DESC LIMIT 10
    """)
    for artifact in artifacts:
        details = decode(artifact.pop("details_json", None)) or {}
        pipeline = details.get("local_pipeline") or {}
        quality = pipeline.get("quality") or details.get("quality") or {}
        artifact["ocr"] = {
            "engine": pipeline.get("engine") or details.get("engine"),
            "orientation_degrees": (quality.get("transforms") or {}).get("orientation_degrees"),
            "orientation_evidence": quality.get("orientation_evidence"),
            "issues": quality.get("issues"),
            "line_count": len(details.get("lines") or []),
        }
    jobs = rows(connection, """
        SELECT job_id,status,error_code,preferred_route,created_at,started_at,completed_at
        FROM ocr_job ORDER BY created_at DESC LIMIT 10
    """)
    checks = [row[0] for row in connection.execute("PRAGMA quick_check").fetchall()]

print(json.dumps({
    "database_quick_check": checks,
    "daily_sessions": daily_sessions,
    "monitor_sessions": monitor_sessions,
    "recent_monitor_events": monitor_events,
    "recent_revisions": revisions,
    "recent_ocr_artifacts": artifacts,
    "recent_ocr_jobs": jobs,
    "raw_media_read": False,
    "raw_media_copied": False,
}, ensure_ascii=False))
