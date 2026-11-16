"""Persist bounded, frame-qualified page observation claims, never study time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

MAX_SEGMENT_MS = 15_000
SEGMENT_SCHEMA_SQL = '''CREATE TABLE IF NOT EXISTS document_observation_segment (
    segment_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
    monitor_id TEXT NOT NULL REFERENCES document_monitor_session(monitor_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
    lease_id TEXT NOT NULL REFERENCES sensor_lease(lease_id) ON DELETE CASCADE,
    epoch TEXT NOT NULL, generation INTEGER NOT NULL, seq INTEGER NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT NOT NULL, visible_ms INTEGER NOT NULL,
    payload_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
    UNIQUE(monitor_id, epoch, generation, seq)
)'''


def _utc(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (AttributeError, ValueError, TypeError):
        raise ValueError('OBSERVATION_SEGMENT_TIME_INVALID') from None


class DocumentObservationSegments:
    """Claims are only estimated sampled coverage; the backend cannot verify frames.

    Epochs are process-local, so a server restart invalidates all pending writes.
    Authentication and source-deletion/report checks remain the caller's duty.
    """
    def __init__(self, database, *, clock=None):
        self.database = database
        self.epoch = uuid4().hex
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def record(self, session_id, *, child_id, revision_id, lease_id, epoch, generation,
               seq, segment_id, started_at, ended_at, visible_ms):
        fields = (session_id, child_id, revision_id, lease_id, epoch, segment_id)
        if any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in fields):
            raise ValueError('OBSERVATION_SEGMENT_ID_INVALID')
        if epoch != self.epoch:
            raise ValueError('OBSERVATION_SEGMENT_EPOCH_STALE')
        if type(generation) is not int or generation < 0 or type(seq) is not int or seq < 0:
            raise ValueError('OBSERVATION_SEGMENT_SEQUENCE_INVALID')
        if type(visible_ms) is not int or not 0 < visible_ms <= MAX_SEGMENT_MS:
            raise ValueError('OBSERVATION_SEGMENT_DURATION_INVALID')
        start, end, now = _utc(started_at), _utc(ended_at), self.clock().astimezone(timezone.utc)
        elapsed = (end - start).total_seconds() * 1000
        if (elapsed <= 0 or elapsed > MAX_SEGMENT_MS or visible_ms > elapsed or end > now + timedelta(seconds=5)
                or now - end > timedelta(seconds=30)
                or start.astimezone(ZoneInfo('Asia/Shanghai')).date() != end.astimezone(ZoneInfo('Asia/Shanghai')).date()):
            raise ValueError('OBSERVATION_SEGMENT_TIME_INVALID')
        payload = dict(session_id=session_id, child_id=child_id, revision_id=revision_id, lease_id=lease_id,
                       epoch=epoch, generation=generation, seq=seq, segment_id=segment_id,
                       started_at=start.isoformat(), ended_at=end.isoformat(), visible_ms=visible_ms)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_observation_segment'").fetchone():
                raise ValueError('OBSERVATION_SEGMENT_SCHEMA_UNAVAILABLE')
            if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child_id,)).fetchone():
                raise ValueError('OBSERVATION_SEGMENT_DELETION_PENDING')
            m = c.execute('''SELECT m.monitor_id,m.generation,m.status,m.generation_started_at,s.child_id,s.status AS session_status,s.state,s.ended_at
                FROM document_monitor_session m JOIN daily_session s ON s.session_id=m.session_id
                WHERE m.session_id=? AND m.child_id=s.child_id''', (session_id,)).fetchone()
            if not m or m['child_id'] != child_id or m['status'] != 'active' or m['session_status'] != 'active' or m['state'] != 'study' or m['ended_at'] is not None or m['generation'] != generation:
                raise ValueError('OBSERVATION_SEGMENT_MONITOR_UNAVAILABLE')
            # Legacy active generations have no reliable start until restarted.
            if m['generation_started_at'] is None or _utc(m['generation_started_at']) > start:
                raise ValueError('OBSERVATION_SEGMENT_BEFORE_MONITOR_UPDATE')
            lease = c.execute('''SELECT * FROM sensor_lease WHERE lease_id=? AND session_id=?
                AND device_type='document_camera' AND status='active' AND released_at IS NULL''', (lease_id, session_id)).fetchone()
            if not lease or _utc(lease['opened_at']) > start or _utc(lease['expires_at']) <= now:
                raise ValueError('OBSERVATION_SEGMENT_LEASE_UNAVAILABLE')
            rev = c.execute('''SELECT r.revision_id,r.created_at FROM document_page_revision r
                WHERE r.monitor_id=? AND r.child_id=? AND r.session_id=?
                ORDER BY r.revision_index DESC LIMIT 1''', (m['monitor_id'], child_id, session_id)).fetchone()
            if not rev or rev['revision_id'] != revision_id or _utc(rev['created_at']) > start:
                raise ValueError('OBSERVATION_SEGMENT_REVISION_UNAVAILABLE')
            previous = c.execute('SELECT * FROM document_observation_segment WHERE segment_id=?', (segment_id,)).fetchone()
            if previous:
                if previous['payload_hash'] != digest:
                    raise ValueError('OBSERVATION_SEGMENT_ID_CONFLICT')
                return {'segment_id': segment_id, 'recorded': True, 'replayed': True}
            overlap = c.execute('''SELECT 1 FROM document_observation_segment WHERE monitor_id=?
                AND julianday(started_at)<julianday(?) AND julianday(ended_at)>julianday(?) LIMIT 1''',
                (m['monitor_id'], end.isoformat(), start.isoformat())).fetchone()
            if overlap:
                raise ValueError('OBSERVATION_SEGMENT_OVERLAP')
            last = c.execute('''SELECT seq,ended_at FROM document_observation_segment WHERE monitor_id=? AND epoch=?
                AND generation=? ORDER BY seq DESC LIMIT 1''', (m['monitor_id'], epoch, generation)).fetchone()
            if (last and (seq != last['seq'] + 1 or _utc(last['ended_at']) > start)) or (not last and seq != 0):
                raise ValueError('OBSERVATION_SEGMENT_SEQUENCE_CONFLICT')
            c.execute('''INSERT INTO document_observation_segment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (segment_id,session_id,child_id,m['monitor_id'],revision_id,lease_id,epoch,generation,seq,
                 start.isoformat(),end.isoformat(),visible_ms,digest,now.isoformat()))
            c.commit()
        return {'segment_id': segment_id, 'recorded': True, 'replayed': False}
