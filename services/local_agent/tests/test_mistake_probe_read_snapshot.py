"""The complete probe read owns one snapshot, not the caller's transaction."""
from contextlib import contextmanager
import asyncio
import pytest

from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.diagnostic_prompt import _TransactionDatabase
from home_study_agent.standard_relocation_provenance import business_snapshot_active
from home_study_agent.original_connection_read import original_connection
from test_mistake_gate_reliability import flow, prepare, CHILD


def setup_probe(flow):
    db = flow[0]
    _, case = prepare(flow)
    service = MistakeDialogueService(db)
    return db, service, {'mistake_id': case, 'child_id': CHILD}, {'probes': []}


def test_probe_fresh_original_read_has_one_marker_and_zero_business_dml(flow, monkeypatch):
    db, service, focus, diagnosis = setup_probe(flow)
    connect = db.connect
    observations = []
    @contextmanager
    def observed_connect():
        with connect() as c:
            before = c.total_changes
            yield c
            observations.append((c.in_transaction, c.total_changes - before))
    original = service.book._question_history
    def history(c, child):
        assert business_snapshot_active(original_connection(c))
        return original(c, child)
    monkeypatch.setattr(db, 'connect', observed_connect)
    monkeypatch.setattr(service.book, '_question_history', history)
    selected = service._probe_item(focus, diagnosis)
    assert selected
    assert observations == [(False, 0)]


def test_probe_borrowed_write_is_not_committed_or_rolled_back(flow):
    db, service, focus, diagnosis = setup_probe(flow)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0]
        c.execute('UPDATE child_profile SET nickname=? WHERE child_id=?', ('synthetic unpublished', CHILD))
        changes = c.total_changes
        service.database = _TransactionDatabase(c)
        assert service._probe_item(focus, diagnosis)
        assert c.in_transaction and c.total_changes == changes
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0] == 'synthetic unpublished'
        c.rollback()
        c.execute('BEGIN')
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0] == before


@pytest.mark.parametrize('error', [RuntimeError('synthetic source failed'), asyncio.CancelledError()])
def test_probe_preserves_original_error_and_caller_write(flow, monkeypatch, error):
    db, service, focus, diagnosis = setup_probe(flow)
    def failed(c, child):
        assert business_snapshot_active(original_connection(c))
        raise error
    monkeypatch.setattr(service.book, '_question_history', failed)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0]
        c.execute('UPDATE child_profile SET nickname=? WHERE child_id=?', ('synthetic unpublished', CHILD))
        changes = c.total_changes
        service.database = _TransactionDatabase(c)
        with pytest.raises(type(error)) as caught:
            service._probe_item(focus, diagnosis)
        assert caught.value is error
        assert c.in_transaction and c.total_changes == changes
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0] == 'synthetic unpublished'
        c.rollback()
        c.execute('BEGIN')
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0] == before
