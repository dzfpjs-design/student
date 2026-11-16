"""One-transaction, local-only source for an observational daily report.

This projection is neither a persisted report nor authority to deliver one.
Before external delivery, callers must freeze and revalidate source, deletion,
recipient authorisation and disclosure policy under the delivery guard.
"""
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import math
import re
from zoneinfo import ZoneInfo

_ZONE = ZoneInfo('Asia/Shanghai')


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo is not None and result.utcoffset() is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _normalize(value):
    return '\n'.join(line.strip() for line in value.splitlines() if line.strip())[:12000] if isinstance(value, str) else ''


def _confidence(value):
    return float(value) if type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1 else None


def build_daily_observed_source(connection, child_id: str, day: str) -> dict:
    """Build once inside the caller's existing transaction; no I/O or writes."""
    if not connection.in_transaction:
        raise ValueError('OBSERVED_DAILY_TRANSACTION_REQUIRED')
    if not isinstance(child_id, str) or not child_id:
        raise ValueError('OBSERVED_DAILY_CHILD_INVALID')
    try:
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day: raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('OBSERVED_DAILY_DATE_INVALID') from None
    if not connection.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
        raise ValueError('OBSERVED_DAILY_CHILD_UNKNOWN')
    if connection.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child_id,)).fetchone():
        raise ValueError('OBSERVED_DAILY_DELETION_PENDING')
    begin = datetime.combine(parsed, time.min, _ZONE).astimezone(timezone.utc)
    end = datetime.combine(parsed + timedelta(days=1), time.min, _ZONE).astimezone(timezone.utc)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {'document_page_revision','document_observation_segment','document_monitor_session','capture_artifact','daily_session','sensor_lease','observed_question_occurrence','observed_question','child_state_event'}
    if not required <= tables:
        raise ValueError('OBSERVED_DAILY_SOURCES_UNAVAILABLE')
    params = (child_id, begin.isoformat(), end.isoformat(), begin.isoformat(), end.isoformat(), begin.isoformat(), end.isoformat())
    page_rows = connection.execute('''SELECT r.revision_id,r.monitor_id,r.session_id,r.artifact_id,r.created_at,
        r.content_sha256,r.ocr_confidence,r.review_required,a.extracted_text,a.status AS artifact_status
        FROM document_page_revision r JOIN daily_session s ON s.session_id=r.session_id AND s.child_id=r.child_id
        JOIN document_monitor_session m ON m.monitor_id=r.monitor_id AND m.child_id=r.child_id AND m.session_id=r.session_id
        JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=r.child_id AND a.session_id=r.session_id
        WHERE r.child_id=? AND a.artifact_type='document_ocr' AND a.status IN ('completed','quality_warning')
        AND ((julianday(r.created_at)>=julianday(?) AND julianday(r.created_at)<julianday(?))
          OR EXISTS (SELECT 1 FROM document_observation_segment x WHERE x.revision_id=r.revision_id
            AND x.child_id=r.child_id AND x.monitor_id=r.monitor_id AND x.session_id=r.session_id
            AND julianday(x.started_at)>=julianday(?) AND julianday(x.started_at)<julianday(?))
          OR EXISTS (SELECT 1 FROM observed_question_occurrence o WHERE o.revision_id=r.revision_id
            AND o.child_id=r.child_id AND o.artifact_id=r.artifact_id
            AND julianday(o.observed_at)>=julianday(?) AND julianday(o.observed_at)<julianday(?)))
        ORDER BY r.revision_id''', params).fetchall()
    pages, ignored = {}, 0
    for r in page_rows:
        body, confidence, observed = _normalize(r['extracted_text']), _confidence(r['ocr_confidence']), _time(r['created_at'])
        if not body or not observed or confidence is None or sha256(body.encode()).hexdigest() != r['content_sha256']:
            ignored += 1; continue
        pages[r['revision_id']] = {'revision_id':r['revision_id'], 'artifact_id':r['artifact_id'],
            'session_id':r['session_id'], 'observed_at':observed.isoformat(), 'text':body,
            'content_sha256':r['content_sha256'], 'confidence':confidence,
            'review_required':bool(r['review_required']) or r['artifact_status']=='quality_warning' or confidence<.75,
            'estimated_visible_ms':0, 'segments':[]}
    segment_rows = connection.execute('''SELECT x.* FROM document_observation_segment x
        JOIN document_page_revision r ON r.revision_id=x.revision_id AND r.monitor_id=x.monitor_id AND r.child_id=x.child_id AND r.session_id=x.session_id
        JOIN document_monitor_session m ON m.monitor_id=x.monitor_id AND m.child_id=x.child_id AND m.session_id=x.session_id
        JOIN daily_session s ON s.session_id=x.session_id AND s.child_id=x.child_id
        JOIN sensor_lease l ON l.lease_id=x.lease_id AND l.session_id=x.session_id AND l.device_type='document_camera'
        WHERE x.child_id=? AND julianday(x.started_at)>=julianday(?) AND julianday(x.started_at)<julianday(?)
        ORDER BY julianday(x.started_at),x.segment_id''', (child_id, begin.isoformat(), end.isoformat())).fetchall()
    last = {}
    for x in segment_rows:
        first, final, ms = _time(x['started_at']), _time(x['ended_at']), x['visible_ms']
        if (x['revision_id'] not in pages or not first or not final or not begin <= first < final <= end
                or type(ms) is not int or not 0 < ms <= 15000 or (final-first).total_seconds()>15
                or ms > (final-first).total_seconds()*1000 or first < last.get(x['monitor_id'],begin)):
            ignored += 1; continue
        last[x['monitor_id']] = final
        page = pages[x['revision_id']]; page['estimated_visible_ms'] += ms
        page['segments'].append({'segment_id':x['segment_id'],'started_at':first.isoformat(),'ended_at':final.isoformat(),
                                 'estimated_visible_ms':ms, 'lease_id':x['lease_id'], 'epoch':x['epoch'],
                                 'generation':x['generation'], 'seq':x['seq'], 'payload_hash':x['payload_hash']})
    question_rows = connection.execute('''SELECT o.occurrence_id,o.revision_id,o.artifact_id,o.observed_at,o.confidence,q.question_id,q.canonical_text,q.question_fingerprint
        FROM observed_question_occurrence o JOIN observed_question q ON q.question_id=o.question_id AND q.child_id=o.child_id
        JOIN document_page_revision r ON r.revision_id=o.revision_id AND r.child_id=o.child_id AND r.artifact_id=o.artifact_id
        JOIN daily_session s ON s.session_id=r.session_id AND s.child_id=r.child_id
        WHERE o.child_id=? AND julianday(o.observed_at)>=julianday(?) AND julianday(o.observed_at)<julianday(?)
        ORDER BY o.occurrence_id''', (child_id, begin.isoformat(), end.isoformat())).fetchall()
    questions = []
    for q in question_rows:
        page, observed, confidence = pages.get(q['revision_id']), _time(q['observed_at']), _confidence(q['confidence'])
        text = _normalize(q['canonical_text']); compact = re.sub(r'\s+', '', text).casefold()
        if (not page or not observed or not begin <= observed < end or confidence is None or not compact
                or sha256(compact.encode()).hexdigest()!=q['question_fingerprint']
                or compact not in re.sub(r'\s+', '', page['text']).casefold()):
            ignored += 1; continue
        questions.append({'occurrence_id':q['occurrence_id'],'question_id':q['question_id'],'revision_id':q['revision_id'],
                          'artifact_id':q['artifact_id'],'observed_at':observed.isoformat(),'text':text,
                          'confidence':confidence,'review_required':page['review_required'] or confidence<.75})
    question_pages = {q['revision_id'] for q in questions}
    # SQL prefilters candidate pages; an invalid associated segment/question is
    # not evidence that an older page was seen on the target day.
    pages = {key: page for key, page in pages.items()
             if begin <= _time(page['observed_at']) < end or page['segments'] or key in question_pages}
    camera_rows = connection.execute('''SELECT e.state_event_id,e.state,e.observed_at,e.confidence,e.shadow FROM child_state_event e
        JOIN daily_session s ON s.session_id=e.session_id AND s.child_id=e.child_id
        WHERE e.child_id=? AND julianday(e.observed_at)>=julianday(?) AND julianday(e.observed_at)<julianday(?)
        ORDER BY e.state_event_id''', (child_id,begin.isoformat(),end.isoformat())).fetchall()
    camera = []
    for e in camera_rows:
        observed, confidence = _time(e['observed_at']), _confidence(e['confidence'])
        if not observed or not begin <= observed < end or confidence is None or e['shadow'] not in (0, 1): ignored += 1; continue
        state = e['state'] if e['state'] in {'writing_like','paused_like','unknown'} else 'other'
        camera.append({'source_id':e['state_event_id'],'observed_at':observed.isoformat(),'state':state,
                       'confidence':confidence,'shadow':bool(e['shadow'])})
    source = {'version':'daily_observed_source_v1','child_id':child_id,'day':day,'timezone':'Asia/Shanghai',
              'pages':list(pages.values()),'questions':questions,'camera_samples':camera,'excluded_invalid_count':ignored,
              'coverage_complete':False,'actual_learning_seconds':None,'question_solving_seconds':None,'activity_inference':None,
              'limitation':'仅包含 OCR 识别及离散观察和页面估计可见时间，未观察部分未知，不代表专注、做题或背书。',
              'local_only':True,'delivery_authorized':False}
    source['source_fingerprint'] = sha256(json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()
    return source
