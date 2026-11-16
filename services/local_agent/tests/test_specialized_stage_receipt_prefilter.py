"""Modality prefilter saves work without changing receipt authority or scope."""
from datetime import datetime, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent import specialized_stage_evidence as receipts
from home_study_agent.specialized_stages import _history, _item_source
from test_writing_response_origin import writing, CHILD
from test_writing_teaching import ready


@pytest.fixture
def reviewed(writing, tmp_path):
    _, rubric, assessment = ready(writing)
    db = Database(writing[0].path, media_root=tmp_path)
    return writing, db, rubric, assessment


def project(c, *, sources_missing=False):
    now = datetime.now(timezone.utc)
    sources = {}
    for row in c.execute("SELECT * FROM diagnostic_item WHERE knowledge_node_id='node-pet-writing-email'"):
        source, _ = _item_source(c, CHILD, dict(row), now)
        if source:
            sources[row['diagnostic_item_id']] = source
    return receipts.reviewed_writing_receipts(c, CHILD, 'node-pet-writing-email',
        {} if sources_missing else sources, _history(c, CHILD, now), now)


def math_answer(writing):
    _, sid, prompts, _, _ = writing
    p = prompts.prepare(sid, track='competition_math', preferred_item_id='diag-oly-counting-1',
                        client_action_id='prefilter-math-prompt')['prompt']
    return prompts.answer(sid, prompt_id=p['prompt_id'], item_version=p['item_version'],
                          answer='wrong', client_action_id='prefilter-math-answer')


@pytest.mark.parametrize('written', [False, True])
@pytest.mark.parametrize('with_math', [False, True])
def test_empty_or_math_only_node_does_not_build_global_indexes(reviewed, monkeypatch, written, with_math):
    writing, db, _, _ = reviewed
    if with_math:
        math_answer(writing)

    def unnecessary(*args, **kwargs):
        raise AssertionError('global index built for a node without this modality')

    for name in ('withdrawn_evidence_ids', '_question_catalog', '_tutor_exposure_index'):
        monkeypatch.setattr(receipts, name, unnecessary)
    with db.connect() as c:
        c.execute('BEGIN')
        before = '\n'.join(c.iterdump())
        assert receipts._reviewed_modality_receipts(c, CHILD, 'node-oly-counting', {}, {},
            datetime.now(timezone.utc), written=written) == ([], [])
        assert '\n'.join(c.iterdump()) == before


@pytest.mark.parametrize('fault', ['source_missing', 'review_retracted'])
def test_matching_modality_preserves_rejected_receipt(reviewed, fault):
    _, db, rubric, assessment = reviewed
    with db.connect() as c:
        accepted, rejected = project(c)
        assert len(accepted) == 1 and not rejected
    if fault == 'review_retracted':
        rubric.review(CHILD, assessment['assessment_id'], expected_revision=1,
            client_action_id='prefilter-retract', reviewer_id='parent', reviewer_role='parent',
            operation='retract', reason='Synthetic review withdrawn')
    with db.connect() as c:
        accepted, rejected = project(c, sources_missing=fault == 'source_missing')
    assert not accepted
    assert rejected == [{'evidence_id': assessment['source_evidence_id'], 'reason':
        'reviewed_writing_source_unavailable' if fault == 'source_missing' else 'writing_original_review_required'}]


def test_identity_index_keeps_other_node_attempt_even_when_its_source_is_invalid(reviewed):
    writing, db, _, assessment = reviewed
    math_answer(writing)
    with db.connect() as c:
        # A corrupt/revised alias remains exposure history, not qualified math evidence.
        original = c.execute("SELECT prompt,stimulus_text,stimulus_asset FROM diagnostic_item WHERE diagnostic_item_id='diag-pet-writing-1'").fetchone()
        c.execute("UPDATE diagnostic_item SET prompt=?,stimulus_text=?,stimulus_asset=?,source_ref='synthetic:invalid-alias' WHERE diagnostic_item_id='diag-oly-counting-1'", tuple(original))
        c.commit()
    with db.connect() as c:
        accepted, rejected = project(c)
    assert not accepted
    assert rejected == [{'evidence_id': assessment['source_evidence_id'],
                         'reason': 'reviewed_writing_conditions_or_identity_conflict'}]


def test_tutor_exposure_still_removes_independence_without_removing_review(reviewed):
    writing, db, _, assessment = reviewed
    with db.connect() as c:
        question = c.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id='diag-pet-writing-1'").fetchone()[0]
        at = c.execute('SELECT occurred_at FROM learning_evidence WHERE evidence_id=?',
                       (assessment['source_evidence_id'],)).fetchone()[0]
    saved = StudyRepository(db).save_tutor_turn(writing[1], None, '英语', question,
        'Synthetic original task explanation', 'test', 'test', 'prefilter-exposure')
    with db.connect() as c:
        # Position the actual saved explanation before this answer, without changing score facts.
        c.execute('UPDATE tutor_turn SET created_at=? WHERE turn_id=?', (at, saved['turn_id']))
        c.commit()
    with db.connect() as c:
        accepted, rejected = project(c)
    assert not rejected and len(accepted) == 1
    assert accepted[0]['review_refs'] and accepted[0]['tutor_refs']
    assert accepted[0]['independent'] is False
