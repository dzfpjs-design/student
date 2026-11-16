"""Append-only withdrawal of score authority while preserving the answer fact."""
from hashlib import sha256
import json
from uuid import uuid4

from .repository import utc_now


def ensure_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS learning_evidence_withdrawal(
        withdrawal_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id) ON DELETE CASCADE,
        reason TEXT NOT NULL CHECK(reason IN ('answer_corrected','answer_withdrawn')),
        replacement_evidence_id TEXT REFERENCES learning_evidence(evidence_id) ON DELETE RESTRICT,
        session_id TEXT REFERENCES daily_session(session_id) ON DELETE SET NULL,
        client_action_id TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(replacement_evidence_id IS NULL OR replacement_evidence_id != evidence_id))''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS learning_evidence_withdrawal_no_update
        BEFORE UPDATE ON learning_evidence_withdrawal
        BEGIN SELECT RAISE(ABORT,'immutable learning evidence withdrawal'); END''')


def _record(c, *, child_id, session_id, evidence_id, reason,
            replacement_evidence_id, client_action_id, request,
            replacement_session_id=None):
    if not c.in_transaction:
        raise ValueError('EVIDENCE_WITHDRAWAL_TRANSACTION_REQUIRED')
    if reason not in {'answer_corrected', 'answer_withdrawn'}:
        raise ValueError('EVIDENCE_WITHDRAWAL_REASON_INVALID')
    if (reason == 'answer_corrected') != (replacement_evidence_id is not None):
        raise ValueError('EVIDENCE_WITHDRAWAL_REPLACEMENT_INVALID')
    ids = [evidence_id] + ([replacement_evidence_id] if replacement_evidence_id else [])
    marks = ','.join('?' for _ in ids)
    rows = c.execute(f'''SELECT evidence_id,child_id,session_id FROM learning_evidence
        WHERE evidence_id IN ({marks})''', ids).fetchall()
    by_id = {row['evidence_id']: row for row in rows}
    if set(by_id) != set(ids):
        raise ValueError('EVIDENCE_WITHDRAWAL_BINDING_MISSING')
    if any(row['child_id'] != child_id for row in rows):
        raise ValueError('EVIDENCE_WITHDRAWAL_OWNER_MISMATCH')
    if by_id[evidence_id]['session_id'] not in (None, session_id):
        raise ValueError('EVIDENCE_WITHDRAWAL_SESSION_MISMATCH')
    if (replacement_evidence_id and by_id[replacement_evidence_id]['session_id'] not in
            (None, replacement_session_id or session_id)):
        raise ValueError('EVIDENCE_WITHDRAWAL_REPLACEMENT_SESSION_MISMATCH')
    request_hash = sha256(json.dumps(request, ensure_ascii=True, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    existing = c.execute('''SELECT * FROM learning_evidence_withdrawal
        WHERE client_action_id=? OR evidence_id=?''', (client_action_id, evidence_id)).fetchall()
    if existing:
        if len(existing) != 1:
            raise ValueError('EVIDENCE_WITHDRAWAL_CONFLICT')
        row = existing[0]
        if (row['child_id'], row['evidence_id'], row['replacement_evidence_id'],
                row['session_id'], row['reason'], row['request_hash']) != (
                child_id, evidence_id, replacement_evidence_id, session_id,
                reason, request_hash):
            raise ValueError('EVIDENCE_WITHDRAWAL_CONFLICT')
        return dict(row)
    value = dict(withdrawal_id=str(uuid4()), child_id=child_id,
        evidence_id=evidence_id, reason=reason,
        replacement_evidence_id=replacement_evidence_id, session_id=session_id,
        client_action_id=client_action_id, request_hash=request_hash,
        created_at=utc_now())
    c.execute('''INSERT INTO learning_evidence_withdrawal(
        withdrawal_id,child_id,evidence_id,reason,replacement_evidence_id,
        session_id,client_action_id,request_hash,created_at)
        VALUES (:withdrawal_id,:child_id,:evidence_id,:reason,:replacement_evidence_id,
        :session_id,:client_action_id,:request_hash,:created_at)''', value)
    return value


def record_correction(c, *, child_id, session_id, evidence_id,
                      replacement_evidence_id, client_action_id, request,
                      replacement_session_id=None):
    """Record one exact correction inside the caller-owned transaction."""
    return _record(c, child_id=child_id, session_id=session_id,
        evidence_id=evidence_id, reason='answer_corrected',
        replacement_evidence_id=replacement_evidence_id,
        client_action_id=client_action_id, request=request,
        replacement_session_id=replacement_session_id)


def record_withdrawal(c, *, child_id, session_id, evidence_id,
                      client_action_id, request):
    """Remove score authority without inventing a replacement answer."""
    return _record(c, child_id=child_id, session_id=session_id,
        evidence_id=evidence_id, reason='answer_withdrawn',
        replacement_evidence_id=None, client_action_id=client_action_id,
        request=request)


def replacement_map(c, child_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_evidence_withdrawal'").fetchone():
        return {}
    return {row['evidence_id']: row['replacement_evidence_id'] for row in c.execute('''
        SELECT evidence_id,replacement_evidence_id FROM learning_evidence_withdrawal
        WHERE child_id=? AND reason='answer_corrected' AND replacement_evidence_id IS NOT NULL''',
        (child_id,))}


def terminal_replacement(mapping, evidence_id):
    seen = set()
    while evidence_id in mapping:
        if evidence_id in seen:
            raise ValueError('EVIDENCE_REPLACEMENT_CYCLE')
        seen.add(evidence_id)
        evidence_id = mapping[evidence_id]
    return evidence_id
