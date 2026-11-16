"""Single-read deduplication without granting a one-case synthetic corpus."""
from collections import Counter

import pytest

from home_study_agent import rubric_calibration as calibration
from home_study_agent import rubric_calibration_evidence as evidence
from home_study_agent import rubric_calibration_authority as authority
from test_rubric_calibration import Corpus, protocol, flow, CHILD


def test_one_read_validates_each_recorded_source_once(flow, monkeypatch):
    corpus = Corpus(flow, monkeypatch)
    corpus.freeze()
    aid, labels = corpus.score(0, 2, gold=2)
    corpus.service.record_case(CHILD, corpus.pid, assessment_id=aid,
        blind_receipt_ids=labels, expected_revision=1, client_action_id='one-case')
    counts = Counter()
    original = evidence.source_snapshot
    def counted(c, child, source):
        counts[source] += 1
        return original(c, child, source)
    monkeypatch.setattr(evidence, 'source_snapshot', counted)
    monkeypatch.setattr(calibration, 'source_snapshot', counted)
    view = corpus.service.read(CHILD, corpus.pid)
    assert not view['score_qualified']
    assert counts == {corpus.answers[0]['evidence_id']: 1}


def test_one_read_batches_default_authority_audit_rows(protocol, monkeypatch):
    _, service, _, args = protocol
    pid = service.freeze(CHILD, **args)['current']['protocol_id']
    calls = []
    original = authority.rows
    def counted(c, child):
        calls.append(child)
        return original(c, child)
    monkeypatch.setattr(authority, 'rows', counted)
    assert not service.read(CHILD, pid)['score_qualified']
    assert calls == [CHILD]


def test_read_scope_rejects_in_transaction_write_without_committing(flow, monkeypatch):
    corpus = Corpus(flow, monkeypatch)
    corpus.freeze()
    wrote = []
    def mutate(c, **kw):
        if not wrote:
            c.execute("UPDATE child_profile SET nickname='Synthetic changed' WHERE child_id=?", (CHILD,))
            wrote.append(True)
        return Corpus.resolve(c, **kw)
    corpus.service.authority_resolver = mutate
    with corpus.db.connect() as c:
        before = c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0]
        c.execute('BEGIN IMMEDIATE')
        row = corpus.service._owned(c, CHILD, corpus.pid)
        with pytest.raises(ValueError, match='READ_SCOPE_CHANGED'):
            corpus.service._read(c, row, corpus.clock[0])
        assert c.in_transaction
        c.rollback()
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0] == before
