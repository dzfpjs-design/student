from datetime import timedelta
import sqlite3
from types import SimpleNamespace

import pytest

from home_study_agent import learning_obligations as obligations
from test_mastery_evidence_replay import flow, CHILD, OTHER, ITEM


def test_pair_matches_individual_windows_with_one_catalog_and_no_writes(flow, monkeypatch):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, '90', 'paired-answer')
    times = (clock[0].isoformat(), (clock[0] - timedelta(milliseconds=1)).isoformat())
    with db.connect() as c:
        c.execute('BEGIN')
        expected = [obligations.project_item_exposure(c, CHILD, ITEM, as_of=at) for at in times]
        assert expected[0]['answer_repeated'] and not expected[1]['answer_repeated']
        calls = []
        original = obligations._question_catalog
        def counted(connection):
            calls.append(connection)
            return original(connection)
        monkeypatch.setattr(obligations, '_question_catalog', counted)
        before = c.total_changes
        assert obligations.project_item_exposures(c, CHILD, ITEM, as_of=times) == expected
        assert calls == [c] and c.total_changes == before
        assert not any(v['answer_repeated'] for v in obligations.project_item_exposures(c, OTHER, ITEM, as_of=times))


def test_pair_rebuilds_after_own_answer_change_and_rollback(flow):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, '90', 'paired-answer')
    times = (clock[0].isoformat(), (clock[0] - timedelta(milliseconds=1)).isoformat())
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = obligations.project_item_exposures(c, CHILD, ITEM, as_of=times)
        c.execute('SAVEPOINT changed')
        c.execute('UPDATE diagnostic_attempt SET created_at=?', ((clock[0] + timedelta(days=2)).isoformat(),))
        result = obligations.project_item_exposures(c, CHILD, ITEM, as_of=times)
        assert result == [obligations.project_item_exposure(c, CHILD, ITEM, as_of=at) for at in times]
        assert not result[0]['answer_repeated']
        c.execute('ROLLBACK TO changed')
        c.execute('RELEASE changed')
        assert obligations.project_item_exposures(c, CHILD, ITEM, as_of=times) == before


@pytest.mark.parametrize('missing', ['borrowed', 'in_transaction', 'total_changes', 'row_factory'])
def test_borrowed_pair_keeps_original_windows_and_outer_transaction(flow, monkeypatch, missing):
    from home_study_agent.diagnostic_prompt import _BorrowedConnection
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, '90', 'borrowed-paired-answer')
    times = (clock[0].isoformat(), (clock[0] - timedelta(milliseconds=1)).isoformat())
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        borrowed = _BorrowedConnection(c)
        if missing != 'borrowed':
            metadata = dict(in_transaction=True, total_changes=c.total_changes, row_factory=sqlite3.Row)
            metadata.pop(missing)
            borrowed = SimpleNamespace(execute=c.execute, **metadata)
        expected = [obligations.project_item_exposure(borrowed, CHILD, ITEM, as_of=at) for at in times]
        original, calls = obligations._question_catalog, []
        def counted(connection):
            calls.append(connection)
            return original(connection)
        monkeypatch.setattr(obligations, '_question_catalog', counted)
        changes = c.total_changes
        assert obligations.project_item_exposures(borrowed, CHILD, ITEM, as_of=times) == expected
        assert calls == [borrowed, borrowed]
        assert c.in_transaction and c.total_changes == changes
        c.rollback()
