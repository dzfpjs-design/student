"""Read-only counts of saved observations; no inference of unobserved time.

Current device events are best-effort points, not acknowledged ordered
heartbeats: neither adjacent points nor OCR revisions prove continuity.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math


def _instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError('timezone-aware timestamp required')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('timezone-aware timestamp required') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timezone-aware timestamp required')
    return result.astimezone(timezone.utc)


def project_observed_time(connection, child_id: str, session_id: str, start: str, end: str) -> dict:
    """Project points and completed plan timers for one child's one session.

    Bounds are a half-open UTC interval; completed tasks crossing the bounds
    are omitted because their accumulated timer cannot be split by day.
    The caller must authorize the child before exposing this projection.
    """
    lower, upper = _instant(start), _instant(end)
    if upper <= lower or not child_id or not session_id:
        raise ValueError('invalid observation scope')
    owner = connection.execute('SELECT 1 FROM daily_session WHERE child_id=? AND session_id=?',
                               (child_id, session_id)).fetchone()
    if not owner:
        raise ValueError('session not found for child')
    begin, finish = lower.isoformat(), upper.isoformat()
    doc = connection.execute('''SELECT event_type,observed_at FROM document_monitor_event
        WHERE child_id=? AND session_id=? AND julianday(observed_at)>=julianday(?)
          AND julianday(observed_at)<julianday(?)''',
        (child_id, session_id, begin, finish)).fetchall()
    camera = connection.execute('''SELECT state,observed_at FROM child_state_event
        WHERE child_id=? AND session_id=? AND julianday(observed_at)>=julianday(?)
          AND julianday(observed_at)<julianday(?)''',
        (child_id, session_id, begin, finish)).fetchall()
    documents = {'no_page', 'stabilizing', 'writing', 'occluded', 'page_changed', 'stable', 'capture_failed'}
    cameras = {'unknown', 'away', 'writing_like', 'paused_like', 'present'}

    def counts(rows, allowed):
        result = {}
        for key, at in rows:
            try:
                point = _instant(at)
            except (ValueError, TypeError):
                continue
            if not lower <= point < upper:
                continue
            label = key if key in allowed else 'other'
            result[label] = result.get(label, 0) + 1
        return result

    rows = connection.execute('''SELECT pi.plan_item_id,pi.started_at,pi.completed_at,pi.active_seconds
        FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
        WHERE dp.child_id=? AND dp.session_id=? AND pi.status='completed'
          AND julianday(pi.started_at)>=julianday(?) AND julianday(pi.completed_at)<julianday(?)''',
        (child_id, session_id, begin, finish)).fetchall()
    timers = []
    for item_id, started, completed, seconds in rows:
        try:
            duration = (_instant(completed) - _instant(started)).total_seconds()
        except (ValueError, TypeError):
            continue
        if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
                or not math.isfinite(seconds) or seconds < 0 or seconds > duration):
            continue
        timers.append({'plan_item_id': item_id, 'recorded_active_seconds': seconds})
    return {
        'child_id': child_id, 'session_id': session_id,
        'window_start_utc': begin, 'window_end_utc': finish,
        'document_point_counts': counts(doc, documents),
        'camera_point_counts': counts(camera, cameras),
        'completed_plan_item_timers': timers,
        'plan_timer_note': '系统记录的任务计时，不代表摄像头确认或总学习时长；跨窗口任务未计。',
        'document_coverage_seconds': None, 'camera_coverage_seconds': None,
        'focused_study_seconds': None, 'page_dwell_seconds': None, 'question_dwell_seconds': None,
        'duration_status': 'insufficient_evidence',
        'duration_reason': '已保存的状态为可能丢失的单点，不证明两点之间连续观察。',
    }
