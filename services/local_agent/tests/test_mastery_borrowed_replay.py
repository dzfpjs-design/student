"""Public replay participates in source deletion without committing its caller."""
import sqlite3

import pytest

from test_mastery_evidence_replay import flow, state, history, CHILD, NODE, ITEM


def test_borrowed_replay_sees_retirement_and_rolls_back_all_writes(flow, monkeypatch):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, '90', 'original-answer')
    before, audits = state(db), history(db, 'audit_event')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        with monkeypatch.context() as scoped:
            scoped.setattr(db, 'connect', lambda: pytest.fail('borrowed replay opened a connection'))
            result = learning.replay_mastery(CHILD, NODE, connection=c, as_of=clock[0].isoformat(), reason='source_deleted')
            assert result['nodes'][0]['state']['evidence_count'] == 0
            assert c.in_transaction
            count = c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0]
            learning.replay_mastery(CHILD, NODE, connection=c, as_of=clock[0].isoformat(), reason='source_deleted')
            assert c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0] == count
        c.rollback()
    assert state(db) == before
    assert history(db, 'audit_event') == audits


def test_borrowed_replay_requires_transaction(flow):
    db, learning, _, clock = flow
    with db.connect() as c:
        with pytest.raises(ValueError, match='MASTERY_REPLAY_TRANSACTION_REQUIRED'):
            learning.replay_mastery(CHILD, connection=c, as_of=clock[0].isoformat())
        assert not c.in_transaction


def test_replay_failure_never_commits_caller_source_change(flow):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, '90', 'original-answer')
    before = state(db)
    with db.connect() as c:
        c.execute("CREATE TRIGGER synthetic_replay_failure BEFORE INSERT ON audit_event BEGIN SELECT RAISE(ABORT,'synthetic replay failure'); END")
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        with pytest.raises(sqlite3.IntegrityError, match='synthetic replay failure'):
            learning.replay_mastery(CHILD, NODE, connection=c, as_of=clock[0].isoformat(), reason='source_deleted')
        assert c.in_transaction
        c.rollback()
    assert state(db) == before


def test_owned_replay_keeps_default_reason_and_commit(flow):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, '90', 'original-answer')
    with db.connect() as c:
        c.execute("UPDATE mastery_state SET evidence_count=999 WHERE child_id=?", (CHILD,))
        c.commit()
    learning.replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    assert state(db)['evidence_count'] == 1
