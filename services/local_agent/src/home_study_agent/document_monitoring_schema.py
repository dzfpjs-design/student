"""Shared parent tables for document monitoring and v93 database migration."""

MONITOR_PARENT_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS document_monitor_session (
        monitor_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        session_id TEXT NOT NULL UNIQUE REFERENCES daily_session(session_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('active','paused','stopped')),
        current_state TEXT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        ended_at TEXT,
        generation INTEGER NOT NULL DEFAULT 0,
        generation_started_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS document_page_revision (
        revision_id TEXT PRIMARY KEY,
        monitor_id TEXT NOT NULL REFERENCES document_monitor_session(monitor_id) ON DELETE CASCADE,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
        artifact_id TEXT NOT NULL UNIQUE REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
        revision_index INTEGER NOT NULL,
        content_signature TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        previous_revision_id TEXT REFERENCES document_page_revision(revision_id),
        change_summary_json TEXT NOT NULL,
        ocr_confidence REAL NOT NULL,
        review_required INTEGER NOT NULL,
        study_signal_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        client_action_id TEXT NOT NULL UNIQUE,
        UNIQUE(monitor_id, revision_index)
    )""",
)


def ensure_document_monitor_parents(connection):
    """Create parent tables through the caller's current transaction."""
    for statement in MONITOR_PARENT_STATEMENTS:
        connection.execute(statement)
