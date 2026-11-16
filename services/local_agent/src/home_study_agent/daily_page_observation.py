"""Local estimate of page visibility from already recorded frame-qualified segments.

Not a learning-time, attention, question-solving, or mastery measurement. This
read projection grants no permission to transmit or retain a report snapshot.
"""
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo

from .daily_visual_evidence import cloud_page_candidate

_ZONE = ZoneInfo('Asia/Shanghai')


def _instant(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (AttributeError, ValueError, TypeError):
        return None


def daily_page_observation(database, child_id: str, day: str) -> dict:
    try:
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day: raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('PAGE_OBSERVATION_DAY_INVALID') from None
    begin = datetime.combine(parsed, time.min, _ZONE).astimezone(timezone.utc)
    end = datetime.combine(parsed + timedelta(days=1), time.min, _ZONE).astimezone(timezone.utc)
    with database.connect() as c:
        c.execute('BEGIN')
        if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
            raise ValueError('PAGE_OBSERVATION_CHILD_UNKNOWN')
        if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child_id,)).fetchone():
            raise ValueError('PAGE_OBSERVATION_DELETION_PENDING')
        tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'document_observation_segment','document_page_revision','capture_artifact',
                'document_monitor_session','daily_session'} <= tables:
            return {'status':'unavailable', 'reason':'SEGMENT_SCHEMA_UNAVAILABLE'}
        rows = c.execute('''SELECT x.segment_id,x.revision_id,x.monitor_id,x.session_id,x.started_at,x.ended_at,x.visible_ms,
                r.content_sha256,a.extracted_text,a.details_json
            FROM document_observation_segment x
            JOIN document_page_revision r ON r.revision_id=x.revision_id AND r.child_id=x.child_id
              AND r.session_id=x.session_id AND r.monitor_id=x.monitor_id
            JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=x.child_id
              AND a.session_id=x.session_id AND a.artifact_type='document_ocr' AND a.status IN ('completed','quality_warning')
            JOIN document_monitor_session m ON m.monitor_id=x.monitor_id AND m.child_id=x.child_id AND m.session_id=x.session_id
            JOIN daily_session s ON s.session_id=x.session_id AND s.child_id=x.child_id
            WHERE x.child_id=? AND julianday(x.started_at)>=julianday(?) AND julianday(x.started_at)<julianday(?)
            ORDER BY julianday(x.started_at),x.segment_id''', (child_id,begin.isoformat(),end.isoformat())).fetchall()
        pages, last_by_monitor = {}, {}
        invalid = 0
        for row in rows:
            start, finish = _instant(row['started_at']), _instant(row['ended_at'])
            duration = row['visible_ms']
            text = row['extracted_text']
            normalized = '\n'.join(line.strip() for line in text.splitlines() if line.strip())[:12000] if isinstance(text, str) else ''
            if (not start or not finish or not begin <= start < finish <= end or type(duration) is not int
                    or duration <= 0 or duration > 15000 or duration > (finish-start).total_seconds()*1000
                    or (finish-start).total_seconds() > 15 or start < last_by_monitor.get(row['monitor_id'], begin)
                    or sha256(normalized.encode('utf-8')).hexdigest() != row['content_sha256']
                    or (not normalized and not cloud_page_candidate(row['details_json']))):
                invalid += 1
                continue
            last_by_monitor[row['monitor_id']] = finish
            item = pages.setdefault(row['revision_id'], {'revision_id':row['revision_id'], 'estimated_visible_ms':0, 'segments':[]})
            if not normalized:
                item['source'] = 'cloud_page'
            item['estimated_visible_ms'] += duration
            item['segments'].append({'segment_id':row['segment_id'], 'session_id':row['session_id'],
                                     'started_at':start.isoformat(), 'ended_at':finish.isoformat(), 'estimated_visible_ms':duration})
        return {'status':'available' if pages else 'no_segments', 'learning_date':day, 'timezone':'Asia/Shanghai',
                'pages':list(pages.values()), 'invalid_segments_excluded':invalid, 'coverage_complete':False,
                'limitation':'仅为满足采样条件的页面估计可见时间；存在未采样或遮挡缺口，不等于学习、专注、解题或背书时长。',
                'actual_learning_seconds':None, 'question_solving_seconds':None, 'local_only':True}
