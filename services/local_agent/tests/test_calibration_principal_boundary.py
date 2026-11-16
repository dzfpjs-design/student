"""A household reviewer cannot be counted twice through different proof aliases."""
import pytest

from test_mistake_gate_reliability import flow, CHILD
from test_rubric_calibration import corpus


def test_two_authority_aliases_do_not_turn_same_local_reviewer_into_two_blind_reviewers(corpus, monkeypatch):
    corpus.freeze()
    original_review = corpus.rubrics.review

    def household_review(*args, **kwargs):
        kwargs.update(reviewer_id='household_parent', reviewer_role='parent')
        return original_review(*args, **kwargs)
    monkeypatch.setattr(corpus.rubrics, 'review', household_review)
    assessment, labels = corpus.score(0, score=2, gold=2)
    # Adversarial issuer aliases over one local authenticated household actor.
    # Actual review events were written by the service, never edited afterward.
    with pytest.raises(ValueError, match='INDEPENDENT_REVIEWERS_REQUIRED'):
        corpus.service.record_case(CHILD, corpus.pid, assessment_id=assessment, blind_receipt_ids=labels,
                                   expected_revision=1, client_action_id='same-household')
