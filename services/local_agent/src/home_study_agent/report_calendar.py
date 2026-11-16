"""Read the original learning date on a caller-owned report transaction."""
from datetime import date, datetime, timezone
import re

from .family_clock import family_time


def study_day(connection, session_id):
    session = connection.execute('SELECT child_id,started_at FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
    if not session:
        raise ValueError('REPORT_SOURCE_SESSION_NOT_FOUND')
    plan = connection.execute('''SELECT plan_date FROM daily_plan WHERE session_id=? AND child_id=?
        ORDER BY version DESC,rowid DESC LIMIT 1''', (session_id, session[0])).fetchone()
    if plan:
        return date.fromisoformat(plan[0]).isoformat()
    stamp = session[1]
    instant = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    # SQLite CURRENT_TIMESTAMP/datetime('now') stores UTC without an offset.
    # Only that exact persisted shape has this legacy contract; arbitrary
    # naive ISO/client timestamps remain unknown and family_time rejects them.
    if instant.tzinfo is None and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}', stamp):
        instant = instant.replace(tzinfo=timezone.utc)
    return family_time(instant).date().isoformat()
