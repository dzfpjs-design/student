"""Irreversible, child-scoped score withdrawal without erasing original answers."""
from hashlib import sha256
import json

from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError, _exists, _transaction
from .repository import utc_now


def ensure_schema(c):
    # Individual DDL statements preserve a caller's transaction (no executescript).
    c.execute('''CREATE TABLE IF NOT EXISTS privacy_source_erasure(
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL, erased_at TEXT NOT NULL,
        PRIMARY KEY(child_id,source_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS privacy_source_evidence_withdrawal(
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        evidence_id TEXT NOT NULL REFERENCES learning_evidence(evidence_id) ON DELETE CASCADE,
        reason TEXT NOT NULL CHECK(reason='source_deleted'),
        source_set_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(child_id,evidence_id))''')
    for table in ('privacy_source_erasure', 'privacy_source_evidence_withdrawal'):
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_update
            BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'immutable privacy withdrawal'); END''')


def require_schema(c):
    if not all(_exists(c, table) for table in ('privacy_source_erasure', 'privacy_source_evidence_withdrawal')):
        raise PrivacyEvidenceCleanupError('PRIVACY_SOURCE_WITHDRAWAL_SCHEMA_REQUIRED')


def record(c, child_id, source_ids, evidence_ids):
    _transaction(c)
    require_schema(c)
    digest = sha256(json.dumps(sorted(source_ids), separators=(',', ':')).encode()).hexdigest()
    inserted = 0
    for eid in sorted(evidence_ids):
        row = c.execute('SELECT child_id FROM learning_evidence WHERE evidence_id=?', (eid,)).fetchone()
        if not row or row['child_id'] != child_id:
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        inserted += c.execute('''INSERT INTO privacy_source_evidence_withdrawal
            VALUES(?,?,'source_deleted',?,?) ON CONFLICT(child_id,evidence_id) DO NOTHING''',
            (child_id,eid,digest,utc_now())).rowcount
    for sid in sorted(source_ids):
        c.execute('INSERT INTO privacy_source_erasure VALUES(?,?,?) ON CONFLICT(child_id,source_id) DO NOTHING',
                  (child_id,sid,utc_now()))
    return inserted
