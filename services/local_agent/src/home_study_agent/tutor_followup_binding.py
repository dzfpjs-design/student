"""Bind a proposed follow-up to the latest locally verified Tutor source."""
from __future__ import annotations

import sqlite3

from .ordinary_learning_text_projection import TextProjectionBlocked
from .tutor_followup_source import snapshot


def latest_binding(connection, *, session_id: str, child_id: str) -> dict | None:
    try:
        row = connection.execute(
            'SELECT turn_id FROM tutor_turn WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
            (session_id, child_id),
        ).fetchone()
        if not row:
            return None
        source = snapshot(connection, session_id=session_id, child_id=child_id, turn_id=row['turn_id'])
        return {'turn_id': source['turn_id'], 'source_sha256': source['source_sha256']}
    except (TextProjectionBlocked, sqlite3.Error, ValueError, KeyError, TypeError):
        return None
