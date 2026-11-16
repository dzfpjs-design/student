"""Impossible automatic authorities must not revalidate an entire holdout."""
from types import SimpleNamespace

import pytest

from test_rubric_calibration import corpus, flow, CHILD


@pytest.mark.parametrize('review', ['approved', 'retracted', 'none'])
def test_human_decisions_exclude_automatic_validation_before_expensive_read(corpus, review):
    corpus.freeze()
    aid, _ = corpus.score(gold=2 if review != 'none' else None)
    if review == 'retracted':
        corpus.rubrics.review(CHILD, aid, expected_revision=2,
            client_action_id='parent-read-retract', reviewer_id='test-reviewer-1',
            reviewer_role='teacher', operation='retract', reason='Counterevidence')
    calls = []

    def validation(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return {'score_qualified': False}

    with corpus.db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        step = c.execute('SELECT step_id FROM rubric_assessment WHERE assessment_id=?', (aid,)).fetchone()[0]
        before = c.total_changes
        result = corpus.rubrics.calibrated_receipt(c, CHILD, step, now=corpus.clock[0],
            calibration_service=SimpleNamespace(qualified_score_receipt=validation))
        assert result is None
        assert len(calls) == (1 if review == 'none' else 0)
        assert c.total_changes == before
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?', (aid,)).fetchone()[0] == {
            'approved': 2, 'retracted': 3, 'none': 0}[review]


@pytest.mark.parametrize('change', ['review_revision', 'review', 'source', 'visual'])
def test_successful_proof_still_rereads_original_exclusion_guards(corpus, monkeypatch, change):
    corpus.freeze()
    aid, _ = corpus.score()
    original_get = corpus.rubrics.get
    reads, proofs = [], []

    def get(*args, **kwargs):
        current = original_get(*args, **kwargs)
        reads.append(current)
        if proofs:
            if change == 'source':
                current['source_status'] = 'changed'
            elif change == 'visual':
                current['material'] = {**current['material'], 'visual': {'status': 'current'}}
            else:
                current[change] = 1 if change == 'review_revision' else {'operation': 'retract'}
        return current

    def validation(*args, **kwargs):
        proofs.append(True)
        return {'score_qualified': True}

    monkeypatch.setattr(corpus.rubrics, 'get', get)
    with corpus.db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        step = c.execute('SELECT step_id FROM rubric_assessment WHERE assessment_id=?', (aid,)).fetchone()[0]
        assert corpus.rubrics.calibrated_receipt(c, CHILD, step, now=corpus.clock[0],
            calibration_service=SimpleNamespace(qualified_score_receipt=validation)) is None
        assert len(proofs) == 1
        assert len(reads) == 2
        assert c.total_changes == 0
