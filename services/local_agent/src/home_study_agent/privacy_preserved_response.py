"""Preserve recorded child answers, not erased source text or score authority."""
import json
from hashlib import sha256

from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError, _exists, _object, _transaction


TABLE = 'privacy_preserved_child_response'


def ensure_schema(c):
    """Explicit startup/staging DDL; never commit or open another connection."""
    c.execute('''CREATE TABLE IF NOT EXISTS privacy_preserved_child_response (
        response_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
        diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
        original_version INTEGER NOT NULL CHECK(original_version>0),
        original_probe_event_id TEXT NOT NULL,
        original_supersedes_event_id TEXT,
        response_text TEXT NOT NULL,
        responded_at TEXT NOT NULL,
        original_event_hash TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK(source_kind='mistake_diagnosis_answer')
    )''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS privacy_preserved_child_response_no_update
        BEFORE UPDATE ON privacy_preserved_child_response
        BEGIN SELECT RAISE(ABORT,'preserved responses are immutable'); END''')


def preserve_diagnosis_answers(c, case, events):
    """Copy exact original answer identities before their probe FK cascades.

    All revisions remain historical, unqualified records. No generated question,
    hypothesis, score, material, request JSON or reconstructed answer is stored.
    """
    _transaction(c)
    answers = [row for row in events if row['kind'] == 'answer']
    if not answers:
        return 0
    if not _exists(c, TABLE):
        raise PrivacyEvidenceCleanupError('PRIVACY_PRESERVED_RESPONSE_SCHEMA_REQUIRED')
    by_id = {row['event_id']: row for row in events}
    count = 0
    for row in answers:
        stored = c.execute('SELECT * FROM mistake_diagnosis_event WHERE event_id=?', (row['event_id'],)).fetchone()
        if not stored or dict(stored) != dict(row):
            raise PrivacyEvidenceCleanupError('PRIVACY_PRESERVED_RESPONSE_SOURCE_CHANGED')
        payload, request = _object(row['payload_json']), _object(row['request_json'])
        probe = by_id.get(row['reference_event_id'])
        if (row['mistake_id'] != case['mistake_id'] or row['child_id'] != case['child_id']
                or not probe or probe['kind'] != 'probe' or probe['mistake_id'] != case['mistake_id']
                or probe['child_id'] != case['child_id'] or request.get('probe_event_id') != probe['event_id']
                or payload.get('answer') != request.get('answer') or not isinstance(payload.get('answer'), str)):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        stored_probe = c.execute('SELECT * FROM mistake_diagnosis_event WHERE event_id=?', (probe['event_id'],)).fetchone()
        if not stored_probe or dict(stored_probe) != dict(probe):
            raise PrivacyEvidenceCleanupError('PRIVACY_PRESERVED_RESPONSE_SOURCE_CHANGED')
        binding = _object(probe['payload_json']).get('binding')
        item = binding.get('item') if isinstance(binding, dict) else None
        item_id = item.get('diagnostic_item_id') if isinstance(item, dict) else None
        if not isinstance(item_id, str) or not c.execute('SELECT 1 FROM diagnostic_item WHERE diagnostic_item_id=?', (item_id,)).fetchone():
            raise PrivacyEvidenceCleanupError('PRIVACY_PRESERVED_RESPONSE_SOURCE_INVALID')
        digest = sha256(json.dumps(dict(row), sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        values = dict(response_id=row['event_id'], child_id=row['child_id'], mistake_id=row['mistake_id'],
            diagnostic_item_id=item_id, original_version=row['version'], original_probe_event_id=probe['event_id'],
            original_supersedes_event_id=row['supersedes_event_id'], response_text=payload['answer'],
            responded_at=row['created_at'], original_event_hash=digest, source_kind='mistake_diagnosis_answer')
        old = c.execute('SELECT * FROM privacy_preserved_child_response WHERE response_id=?', (row['event_id'],)).fetchone()
        if old:
            if dict(old) != values:
                raise PrivacyEvidenceCleanupError('PRIVACY_PRESERVED_RESPONSE_CONFLICT')
            continue
        c.execute('INSERT INTO privacy_preserved_child_response (' + ','.join(values) + ') VALUES (' +
                  ','.join('?' for _ in values) + ')', tuple(values.values()))
        count += 1
    return count


def read_preserved_responses(c, child_id, mistake_id=None):
    """Read only: these are historical answers, never mastery evidence."""
    if not _exists(c, TABLE):
        return []
    rows = c.execute('''SELECT * FROM privacy_preserved_child_response WHERE child_id=?
        AND (? IS NULL OR mistake_id=?) ORDER BY responded_at,original_version,response_id''',
        (child_id, mistake_id, mistake_id)).fetchall()
    return [{**dict(row), 'source_erased': True, 'score_authority': 'none',
             'response_status': 'historical_only'} for row in rows]
