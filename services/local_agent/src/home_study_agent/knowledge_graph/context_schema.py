"""Local companion context audit schema; never imports the atomic build."""

from .atomic_companion import ensure_usage_schema


def ensure_schema(connection):
    ensure_usage_schema(connection)
    connection.execute('''CREATE TABLE IF NOT EXISTS kg_local_course_context_event(
        event_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        query_sha256 TEXT NOT NULL,selected_segments_json TEXT NOT NULL,created_at TEXT NOT NULL)''')
