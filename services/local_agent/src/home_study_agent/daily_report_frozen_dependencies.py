"""Freeze the actual observation rows used in a local daily-report projection.

Caller owns a SQLite transaction. New observations are intentionally ignored by
validation; deletion, owner changes, or modifications of frozen evidence fail.
This module neither authorizes nor submits a report.
"""
from hashlib import sha256
import json
import re
import sqlite3

from .agent_runtime import _RuntimeTransactionDatabase
from .daily_visual_evidence import cloud_page_dependency, project_daily_visual_evidence
from .daily_page_observation import daily_page_observation
from .daily_observation_projection import project_daily_observation
from .daily_study_duration import project_recorded_task_time

# Only evidence fields and parent identity, not mutable session/monitor status.
_PAGE = '''SELECT r.revision_id,r.child_id,r.session_id,r.monitor_id,r.artifact_id,
 r.content_sha256,r.ocr_confidence,r.review_required,r.created_at,
 a.artifact_type,a.status,a.extracted_text
 FROM document_page_revision r
 JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=r.child_id AND a.session_id=r.session_id
 JOIN daily_session s ON s.session_id=r.session_id AND s.child_id=r.child_id
 WHERE r.child_id=? AND r.revision_id=?'''
_CLOUD_PAGE = _PAGE.replace('a.artifact_type,a.status,a.extracted_text', 'a.artifact_type,a.status,a.extracted_text,a.details_json')
_QUESTION = '''SELECT o.occurrence_id,o.child_id,o.revision_id,o.artifact_id,o.question_id,o.observed_at,o.confidence,
 q.canonical_text,q.question_fingerprint,r.session_id,r.content_sha256,a.extracted_text,a.artifact_type,a.status
 FROM observed_question_occurrence o
 JOIN observed_question q ON q.question_id=o.question_id AND q.child_id=o.child_id
 JOIN document_page_revision r ON r.revision_id=o.revision_id AND r.child_id=o.child_id AND r.artifact_id=o.artifact_id
 JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=r.child_id AND a.session_id=r.session_id
 JOIN daily_session s ON s.session_id=r.session_id AND s.child_id=r.child_id
 WHERE o.child_id=? AND o.occurrence_id=?'''
_SEGMENT = '''SELECT x.segment_id,x.child_id,x.revision_id,x.monitor_id,x.session_id,x.started_at,x.ended_at,x.visible_ms,
 r.artifact_id,r.content_sha256,a.extracted_text,a.artifact_type,a.status
 FROM document_observation_segment x
 JOIN document_page_revision r ON r.revision_id=x.revision_id AND r.child_id=x.child_id AND r.session_id=x.session_id AND r.monitor_id=x.monitor_id
 JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=x.child_id AND a.session_id=x.session_id
 JOIN document_monitor_session m ON m.monitor_id=x.monitor_id AND m.child_id=x.child_id AND m.session_id=x.session_id
 JOIN daily_session s ON s.session_id=x.session_id AND s.child_id=x.child_id
 WHERE x.child_id=? AND x.segment_id=?'''
_CLOUD_SEGMENT = _SEGMENT.replace('a.extracted_text,a.artifact_type,a.status', 'a.extracted_text,a.artifact_type,a.status,a.details_json')
_EVENT = '''SELECT e.* FROM document_monitor_event e JOIN daily_session s
 ON s.session_id=e.session_id AND s.child_id=e.child_id WHERE e.child_id=? AND e.event_id=?'''
_CAMERA = '''SELECT e.* FROM child_state_event e JOIN daily_session s
 ON s.session_id=e.session_id AND s.child_id=e.child_id WHERE e.child_id=? AND e.state_event_id=?'''
_TASK = '''SELECT i.plan_item_id,i.subject,i.title,i.status,i.started_at,i.completed_at,i.active_seconds,i.actual_minutes,
 p.child_id,p.session_id FROM plan_item i JOIN daily_plan p ON p.plan_id=i.plan_id
 JOIN daily_session s ON s.session_id=p.session_id AND s.child_id=p.child_id
 WHERE p.child_id=? AND i.plan_item_id=?'''
_QUERIES = {'page':_PAGE, 'cloud_page':_CLOUD_PAGE, 'question':_QUESTION, 'segment':_SEGMENT, 'cloud_segment':_CLOUD_SEGMENT, 'document_event':_EVENT, 'camera_event':_CAMERA, 'task':_TASK}


def _hash(c, child_id, kind, identifier):
    if kind == 'camera_event':
        # The read-only sample projection does not yet exclude later family
        # corrections/retractions; never freeze or reuse such a source.
        if c.execute('SELECT 1 FROM observation_correction WHERE child_id=? AND state_event_id=? LIMIT 1', (child_id, identifier)).fetchone():
            raise ValueError('FROZEN_SOURCE_WITHDRAWN')
        if c.execute('SELECT 1 FROM behavior_retraction WHERE child_id=? AND event_id=? LIMIT 1', (child_id, 'observation:' + identifier)).fetchone():
            raise ValueError('FROZEN_SOURCE_WITHDRAWN')
    rows = c.execute(_QUERIES[kind], (child_id, identifier)).fetchall()
    if len(rows) != 1:
        raise ValueError('FROZEN_SOURCE_MISSING_OR_AMBIGUOUS')
    values = list(rows[0])
    if kind in ('cloud_page', 'cloud_segment'):
        # Older snapshots hashed raw details_json; they intentionally fail
        # validation and must be frozen again under the new dependency format.
        values[-1] = cloud_page_dependency(values[-1])
    encoded = json.dumps([kind, child_id, values], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return sha256(encoded).hexdigest()


def build(connection, child_id, projection):
    if not connection.in_transaction or not isinstance(child_id, str) or not child_id:
        raise ValueError('FROZEN_TRANSACTION_OR_CHILD_INVALID')
    try:
        day = projection['visual']['day']
        view = _RuntimeTransactionDatabase(connection)
        actual = {'version':'daily_observation_v1',
                  'visual': project_daily_visual_evidence(view, child_id, day),
                  'timing': daily_page_observation(view, child_id, day),
                  'task_time': project_recorded_task_time(connection, child_id, day),
                  'samples': project_daily_observation(connection, child_id, day, include_all_source_ids=True)}
        if actual != projection:
            raise ValueError('FROZEN_PROJECTION_MISMATCH')
        # Exclusion counters depend on rejected rows, whose identities the
        # current projections do not expose. Do not freeze an unauditable
        # aggregate while presenting it as source-bound.
        if actual['visual'].get('excluded_invalid_count', 0) or actual['timing'].get('invalid_segments_excluded', 0):
            raise ValueError('FROZEN_REJECTED_SOURCES_UNBOUND')
        refs = set()
        for page in actual['visual']['pages']:
            kind = 'cloud_page' if page.get('source') == 'cloud_page' else 'page'
            refs.update((kind, obs['revision_id']) for obs in page['observations'])
        for question in actual['visual']['questions']:
            refs.update(('question', obs['occurrence_id']) for obs in question['observations'])
        for page in actual['timing'].get('pages', []):
            kind = 'cloud_segment' if page.get('source') == 'cloud_page' else 'segment'
            refs.update((kind, segment['segment_id']) for segment in page['segments'])
        refs.update(('task', identifier) for identifier in actual['task_time']['source_ids'])
        for label, kind in (('document', 'document_event'), ('camera', 'camera_event')):
            source = actual['samples']['sources'][label]
            ids = source.get('source_ids', [])
            if source.get('status') != 'unavailable' and len(ids) != source['sample_count']:
                raise ValueError('FROZEN_SOURCE_IDS_INCOMPLETE')
            refs.update((kind, identifier) for identifier in ids)
        if any(not isinstance(i, str) or not i for _, i in refs):
            raise ValueError('FROZEN_SOURCE_ID_INVALID')
        return [(kind, identifier, _hash(connection, child_id, kind, identifier)) for kind, identifier in sorted(refs)]
    except (KeyError, TypeError, sqlite3.Error) as error:
        raise ValueError('FROZEN_PROJECTION_INVALID') from error


def valid(connection, child_id, entries):
    if not connection.in_transaction or not isinstance(child_id, str) or not child_id or not isinstance(entries, list):
        return False
    seen = set()
    try:
        if connection.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone() is None:
            return False
        if connection.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child_id,)).fetchone():
            return False
        for entry in entries:
            if not isinstance(entry, (tuple, list)) or len(entry) != 3:
                return False
            kind, identifier, digest = entry
            if kind not in _QUERIES or not isinstance(identifier, str) or not identifier or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                return False
            if (kind,identifier) in seen or _hash(connection, child_id, kind, identifier) != digest:
                return False
            seen.add((kind,identifier))
        return True
    except (ValueError, TypeError, sqlite3.Error, OverflowError):
        return False
