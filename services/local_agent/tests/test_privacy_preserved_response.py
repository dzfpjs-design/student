"""Original response preservation is atomic historical storage, never scoring."""
import sqlite3

import pytest

from home_study_agent.privacy_preserved_response import ensure_schema, preserve_diagnosis_answers, read_preserved_responses
from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
from test_mistake_gate_reliability import flow, prepare, CHILD
from test_mistake_diagnosis import propose, ask, respond


def test_schema_ddl_rolls_back_with_callers_transaction(flow):
    db, _, _, _ = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        ensure_schema(c)
        assert c.in_transaction
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='privacy_preserved_child_response'").fetchone()
        c.rollback()
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='privacy_preserved_child_response'").fetchone()


def original(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    probe = ask(book, case)
    first = respond(book, case, probe['event_id'], 'first recorded answer')
    respond(book, case, probe['event_id'], 'revised recorded answer', supersedes_event_id=first['event_id'])
    with db.connect() as c:
        ensure_schema(c)
        c.commit()
    return db, case


def test_schema_and_answers_never_commit_borrowed_transaction(flow):
    db, case = original(flow)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        ensure_schema(c)
        row = dict(c.execute('SELECT * FROM mistake_case WHERE mistake_id=?', (case,)).fetchone())
        events = [dict(r) for r in c.execute('SELECT * FROM mistake_diagnosis_event WHERE mistake_id=? ORDER BY version', (case,))]
        assert preserve_diagnosis_answers(c, row, events) == 2
        assert preserve_diagnosis_answers(c, row, events) == 0
        assert c.in_transaction
        saved = read_preserved_responses(c, CHILD, case)
        assert [r['response_text'] for r in saved] == ['first recorded answer', 'revised recorded answer']
        assert saved[1]['original_supersedes_event_id'] == saved[0]['response_id']
        assert all(r['score_authority'] == 'none' for r in saved)
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            c.execute("UPDATE privacy_preserved_child_response SET response_text='forged'")
        c.rollback()
        assert read_preserved_responses(c, CHILD) == []


def test_conflicting_saved_identity_blocks_and_child_delete_cascades(flow):
    db, case = original(flow)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = dict(c.execute('SELECT * FROM mistake_case WHERE mistake_id=?', (case,)).fetchone())
        events = [dict(r) for r in c.execute('SELECT * FROM mistake_diagnosis_event WHERE mistake_id=? ORDER BY version', (case,))]
        preserve_diagnosis_answers(c, row, events)
        saved = dict(c.execute('SELECT * FROM privacy_preserved_child_response LIMIT 1').fetchone())
        c.execute('DELETE FROM privacy_preserved_child_response WHERE response_id=?', (saved['response_id'],))
        saved['original_event_hash'] = 'synthetic corruption'
        c.execute('INSERT INTO privacy_preserved_child_response (' + ','.join(saved) + ') VALUES (' + ','.join('?' for _ in saved) + ')', tuple(saved.values()))
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRESERVED_RESPONSE_CONFLICT'):
            preserve_diagnosis_answers(c, row, events)
        c.rollback()
        c.execute('BEGIN IMMEDIATE')
        preserve_diagnosis_answers(c, row, events)
        c.execute('DELETE FROM mistake_case WHERE mistake_id=?', (case,))
        assert read_preserved_responses(c, CHILD) == []
        c.rollback()


def test_caller_cannot_replace_original_answer_or_probe(flow):
    db, case = original(flow)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = dict(c.execute('SELECT * FROM mistake_case WHERE mistake_id=?', (case,)).fetchone())
        events = [dict(r) for r in c.execute('SELECT * FROM mistake_diagnosis_event WHERE mistake_id=? ORDER BY version', (case,))]
        next(r for r in events if r['kind']=='answer')['payload_json'] = '{"answer":"forged"}'
        with pytest.raises(PrivacyEvidenceCleanupError, match='SOURCE_CHANGED'):
            preserve_diagnosis_answers(c, row, events)
        assert read_preserved_responses(c, CHILD) == []
        c.rollback()
