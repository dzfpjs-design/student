"""Conservative, local-only daily camera observation evidence.

Each source is an isolated sample, not proof of continuous visibility, learning,
or attention. Callers own the read transaction and must revalidate dependencies
and recipient authorisation before using this in a report or transmitting it.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import math
import sqlite3
from zoneinfo import ZoneInfo

_BEIJING = ZoneInfo('Asia/Shanghai')
_DOCUMENT_STATES = frozenset({'no_page', 'stabilizing', 'writing', 'occluded', 'page_changed', 'stable', 'capture_failed'})
_CAMERA_STATES = frozenset({'present', 'away', 'focused_like', 'writing_like', 'paused_like', 'away_like', 'unknown'})
_LIMIT = 100


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def project_daily_observation(connection: sqlite3.Connection, child_id: str, learning_date: str, *, include_all_source_ids: bool = False) -> dict:
    """Read immutable samples within the caller's transaction, never infer duration."""
    if not connection.in_transaction:
        raise ValueError('DAILY_OBSERVATION_TRANSACTION_REQUIRED')
    if not isinstance(child_id, str) or not child_id:
        raise ValueError('DAILY_OBSERVATION_CHILD_INVALID')
    try:
        day = date.fromisoformat(learning_date)
        if day.isoformat() != learning_date:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('DAILY_OBSERVATION_DATE_INVALID') from None
    if connection.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone() is None:
        raise ValueError('DAILY_OBSERVATION_CHILD_UNKNOWN')
    begin = datetime.combine(day, time.min, _BEIJING).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, _BEIJING).astimezone(timezone.utc)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    withdrawn = set()
    if 'observation_correction' in tables:
        withdrawn.update(row[0] for row in connection.execute(
            'SELECT state_event_id FROM observation_correction WHERE child_id=? AND state_event_id IS NOT NULL', (child_id,)))
    if 'behavior_retraction' in tables:
        withdrawn.update(row[0][len('observation:'):] for row in connection.execute(
            "SELECT event_id FROM behavior_retraction WHERE child_id=? AND event_id LIKE 'observation:%'", (child_id,))
            if isinstance(row[0], str) and row[0].startswith('observation:'))
    result = {'child_id': child_id, 'learning_date': learning_date, 'timezone': 'Asia/Shanghai',
              'version': 'daily_observation_samples_v1', 'sources': {},
              'camera_observed_seconds': None, 'page_visible_seconds': None,
              'question_visible_seconds': None, 'actual_learning_seconds': None,
              'activity_inference': None,
              'limitation': '离散状态样本不足以证明连续可见、专注、做题或背书时间。',
              'local_only': True}
    for label, table, source_id, state_column, states in (
        ('document', 'document_monitor_event', 'event_id', 'event_type', _DOCUMENT_STATES),
        ('camera', 'child_state_event', 'state_event_id', 'state', _CAMERA_STATES),
    ):
        if table not in tables:
            result['sources'][label] = {'status': 'unavailable', 'sample_count': 0, 'states': {}, 'samples': [], 'truncated': False}
            continue
        # The extra owner join prevents corrupted cross-child session references
        # from becoming observations for either child.
        cursor = connection.execute(f'''SELECT e.{source_id}, e.{state_column}, e.observed_at, e.*
            FROM {table} e JOIN daily_session s ON s.session_id=e.session_id AND s.child_id=e.child_id
            WHERE e.child_id=? AND julianday(e.observed_at)>=julianday(?)
              AND julianday(e.observed_at)<julianday(?) ORDER BY julianday(e.observed_at),e.{source_id}''',
            (child_id, begin.isoformat(), end.isoformat()))
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        samples = []
        source_ids = []
        counts = Counter()
        eligible = Counter()
        withdrawn_count = 0
        total = 0
        digest = sha256()
        for row in rows:
            source, state, occurred = row[0], row[1], row[2]
            stamp = _timestamp(occurred)
            if stamp is None or not begin <= stamp < end:
                continue
            safe_state = state if state in states else 'other'
            # The display is bounded, but the source identity must include every
            # valid event, including those beyond the visible first 100 samples.
            digest.update(json.dumps(list(row)[3:], ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n')
            if label == 'camera' and source in withdrawn:
                digest.update(b'withdrawn\n')
                withdrawn_count += 1
                continue
            counts[safe_state] += 1
            if label == 'camera' and 'shadow' in columns and 'confidence' in columns:
                shadow, confidence = row[columns.index('shadow')], row[columns.index('confidence')]
                if (shadow == 0 and not isinstance(shadow, bool) and not isinstance(confidence, bool)
                        and isinstance(confidence, (int, float)) and math.isfinite(confidence)
                        and 0.5 <= confidence <= 1 and safe_state in {'present','away','writing_like','paused_like'}):
                    eligible[safe_state] += 1
            total += 1
            if include_all_source_ids:
                source_ids.append(source)
            if len(samples) < _LIMIT:
                samples.append({'source_id': source, 'state': safe_state, 'observed_at': stamp.isoformat()})
        result['sources'][label] = {'status': 'recorded' if total else 'no_samples', 'sample_count': total,
                                    'states': dict(sorted(counts.items())), 'samples': samples,
                                    'truncated': total > len(samples), 'source_fingerprint': digest.hexdigest()}
        if label == 'camera':
            result['sources'][label]['eligible_states'] = dict(sorted(eligible.items()))
            result['sources'][label]['withdrawn_count'] = withdrawn_count
        if include_all_source_ids:
            result['sources'][label]['source_ids'] = source_ids
    return result
