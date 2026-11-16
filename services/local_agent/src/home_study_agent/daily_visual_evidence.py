"""Read-only, child-scoped projection of document-camera OCR observations.

This is not a report or permission to send one.  A caller that freezes or
transmits it must revalidate its fingerprint, the child's delivery consent,
and deletion state under the report-delivery effect guard.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import math
import re
from zoneinfo import ZoneInfo

_ZONE = ZoneInfo('Asia/Shanghai')


def _text(value: str) -> str:
    return '\n'.join(line.strip() for line in value.splitlines() if line.strip())[:12000]


def _timestamp(value: str, start: datetime, end: datetime) -> bool:
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return timestamp.tzinfo is not None and start <= timestamp.astimezone(timezone.utc) < end
    except (ValueError, TypeError, AttributeError):
        return False


def _confidence(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


def cloud_page_candidate(details_json) -> str:
    """Return explicitly unconfirmed page OCR, never as corrected local OCR."""
    try:
        details = json.loads(details_json or '{}')
        cloud = details.get('cloud_vision_fallback')
        if not isinstance(cloud, dict) or cloud.get('status') not in {'succeeded', 'needs_review'}:
            return ''
        privacy = cloud.get('privacy')
        blocks = cloud.get('blocks')
        if not isinstance(privacy, dict) or privacy.get('original_frame_uploaded') is not False or not isinstance(blocks, list):
            return ''
        return _text('\n'.join(b['text'] for b in blocks if isinstance(b, dict) and isinstance(b.get('text'), str)))[:4000]
    except (ValueError, TypeError, AttributeError):
        return ''


def cloud_page_dependency(details_json) -> dict:
    """The exact cloud-page facts used by the projection, excluding local metadata."""
    try:
        cloud = json.loads(details_json or '{}').get('cloud_vision_fallback')
        if not isinstance(cloud, dict):
            cloud = {}
        return {'status': cloud.get('status'), 'privacy': cloud.get('privacy'),
                'text': cloud_page_candidate(details_json)}
    except (ValueError, TypeError, AttributeError):
        return {'status': None, 'privacy': None, 'text': ''}


def cloud_page_dependency_bytes(details_json) -> bytes:
    # Compare the same canonical types/values that the frozen JSON hash uses;
    # Python equality alone treats True and 1, or False and 0, as equal.
    return json.dumps(cloud_page_dependency(details_json), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def project_daily_visual_evidence(database, child_id: str, day: str) -> dict:
    """Return observed OCR text and questions, never inferred mastery or durations.

    Uses revision/occurrence observation time in China time.  Identical page
    text and identical questions are grouped within the day, keeping all source
    IDs and actual observation times.  Time between observations is deliberately
    not treated as continuously visible or engaged time.
    """
    if not isinstance(child_id, str) or not child_id.strip():
        raise ValueError('child_id required')
    try:
        parsed_day = date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError('invalid day') from None
    if parsed_day.isoformat() != day:
        raise ValueError('invalid day')
    start = datetime.combine(parsed_day, time.min, _ZONE).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    pages = {}
    questions = {}
    invalid = 0
    with database.connect() as connection:
        connection.execute('BEGIN')
        # Suppress projection as soon as a relevant deletion is requested, not
        # only after cascading source rows have disappeared. A failed/incomplete
        # deletion remains conservative until the lifecycle resolves it.
        deleting = connection.execute('''SELECT 1 FROM deletion_job
            WHERE child_id=? AND status!='completed'
              AND (scope IN ('learning','media','all_history') OR target_type='capture_artifact')
            LIMIT 1''', (child_id,)).fetchone()
        if deleting:
            connection.rollback()
            raise ValueError('VISUAL_EVIDENCE_DELETION_PENDING')
        # A page first recognized yesterday can still be observed today. Only
        # admit it through the same validated segments used for page timing,
        # in this transaction, so the content and its duration stay bound.
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'child_profile','document_observation_segment','document_page_revision',
                    'capture_artifact','document_monitor_session','daily_session'}
        timed = {}
        if required <= tables:
            from .agent_runtime import _RuntimeTransactionDatabase
            from .daily_page_observation import daily_page_observation
            timing = daily_page_observation(_RuntimeTransactionDatabase(connection), child_id, day)
            timed = {page['revision_id']: page['segments'][0]['started_at'] for page in timing.get('pages', [])}
        extra = ''' OR EXISTS (SELECT 1 FROM document_observation_segment x
                    WHERE x.child_id=r.child_id AND x.revision_id=r.revision_id
                    AND julianday(x.started_at)>=julianday(?) AND julianday(x.started_at)<julianday(?))''' if required <= tables else ''
        params = (child_id, start.isoformat(), end.isoformat())
        if extra:
            params += (start.isoformat(), end.isoformat())
        rows = connection.execute('''SELECT r.revision_id,r.child_id,r.session_id,r.artifact_id,
                 r.content_sha256,r.ocr_confidence,r.review_required,r.created_at,
                 a.extracted_text,a.details_json,a.status,a.artifact_type
            FROM document_page_revision r
            JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=r.child_id AND a.session_id=r.session_id
            JOIN daily_session s ON s.session_id=r.session_id AND s.child_id=r.child_id
            WHERE r.child_id=? AND ((julianday(r.created_at)>=julianday(?) AND julianday(r.created_at)<julianday(?))'''
            + extra + ''') ORDER BY julianday(r.created_at),r.revision_id''', params).fetchall()
        valid_pages = {}
        for row in rows:
            confidence = _confidence(row['ocr_confidence'])
            observed_at = row['created_at'] if _timestamp(row['created_at'], start, end) else timed.get(row['revision_id'])
            if observed_at is None:
                # An invalid or withdrawn segment is not a new observation.
                invalid += 1
                continue
            try:
                created = datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
                observed = datetime.fromisoformat(observed_at.replace('Z','+00:00'))
                time_valid = created.tzinfo is not None and observed.tzinfo is not None and created <= observed
            except (ValueError, TypeError, AttributeError):
                time_valid = False
            if (not time_valid or row['artifact_type'] != 'document_ocr'
                    or row['status'] not in ('completed', 'quality_warning')
                    or not isinstance(row['extracted_text'], str) or confidence is None):
                invalid += 1
                continue
            normalized = _text(row['extracted_text'])
            if sha256(normalized.encode('utf-8')).hexdigest() != row['content_sha256']:
                invalid += 1
                continue
            source = 'local_ocr'
            if not normalized:
                normalized = cloud_page_candidate(row['details_json'])
                source = 'cloud_page'
            if not normalized:
                invalid += 1
                continue
            page_needs_review = bool(row['review_required']) or row['status'] == 'quality_warning' or confidence < .75
            if source == 'cloud_page': page_needs_review = True
            valid_pages[row['revision_id']] = (normalized, page_needs_review)
            content_hash = sha256(normalized.encode('utf-8')).hexdigest()
            entry = pages.setdefault((source, content_hash), {'text': normalized[:1200], 'source': source,
                'content_sha256': content_hash, 'observations': []})
            entry['observations'].append({'revision_id': row['revision_id'], 'artifact_id': row['artifact_id'],
                'session_id': row['session_id'], 'observed_at': observed_at, 'confidence': confidence,
                'review_required': page_needs_review,
                'artifact_text_sha256': sha256(row['extracted_text'].encode('utf-8')).hexdigest()})
        occurrences = connection.execute('''SELECT o.occurrence_id,o.revision_id,o.artifact_id,o.child_id,
                 o.observed_at,o.confidence,q.question_id,q.canonical_text,q.question_fingerprint
            FROM observed_question_occurrence o
            JOIN observed_question q ON q.question_id=o.question_id AND q.child_id=o.child_id
            JOIN document_page_revision r ON r.revision_id=o.revision_id AND r.child_id=o.child_id AND r.artifact_id=o.artifact_id
            JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=r.child_id AND a.session_id=r.session_id
            JOIN daily_session s ON s.session_id=r.session_id AND s.child_id=r.child_id
            WHERE o.child_id=? AND julianday(o.observed_at)>=julianday(?) AND julianday(o.observed_at)<julianday(?)
            ORDER BY julianday(o.observed_at),o.occurrence_id''',
            (child_id, start.isoformat(), end.isoformat())).fetchall()
        for row in occurrences:
            confidence = _confidence(row['confidence'])
            text = _text(row['canonical_text']) if isinstance(row['canonical_text'], str) else ''
            compact = re.sub(r'\s+', '', text).casefold()
            page_evidence = valid_pages.get(row['revision_id'])
            page = page_evidence[0] if page_evidence else None
            if (not _timestamp(row['observed_at'], start, end) or confidence is None or not compact or not page
                    or sha256(compact.encode('utf-8')).hexdigest() != row['question_fingerprint']
                    or compact not in re.sub(r'\s+', '', page).casefold()):
                invalid += 1
                continue
            entry = questions.setdefault(row['question_fingerprint'], {'text': text[:1200],
                'question_fingerprint': row['question_fingerprint'], 'observations': []})
            entry['observations'].append({'occurrence_id': row['occurrence_id'], 'question_id': row['question_id'],
                'revision_id': row['revision_id'], 'artifact_id': row['artifact_id'],
                'observed_at': row['observed_at'], 'confidence': confidence,
                'review_required': confidence < .75 or page_evidence[1],
                'canonical_text_sha256': sha256(row['canonical_text'].encode('utf-8')).hexdigest()})
        connection.rollback()
    result = {'version': 1, 'child_id': child_id, 'day': day, 'timezone': 'Asia/Shanghai',
              'pages': list(pages.values()), 'questions': list(questions.values()),
              'excluded_invalid_count': invalid, 'interpretation': 'ocr_observation_not_learning_or_mastery'}
    result['source_fingerprint'] = sha256(json.dumps(result, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return result
