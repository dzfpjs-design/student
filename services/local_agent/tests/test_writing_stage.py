import pytest

from home_study_agent.database import Database
from home_study_agent.writing_rubric import prepare_writing_review
from test_writing_response_origin import writing, CHILD
from test_writing_rubric import prepared
from test_specialized_media_stage import node


@pytest.mark.parametrize('fault', ['none', 'retract', 'original', 'source', 'lower_score', 'generic'])
def test_current_original_writing_review_reaches_stage_and_retraction_recomputes(writing, fault):
    db = writing[0]
    service, snapshot, body = prepared(writing)
    service.review(CHILD, snapshot['assessment_id'], **body)
    current = node(db, 'node-pet-writing-email')
    assert current['qualified_stage'] == 'baseline', current
    assert current['gates']['baseline']['evidence_refs'] == [snapshot['source_evidence_id']]
    assert current['gates']['baseline']['review_refs']
    assert current['gates']['near_transfer']['status'] == 'missing'
    assert not current['mastery_claim_allowed']
    if fault == 'none':
        assert node(Database(db.path), 'node-pet-writing-email')['qualified_stage'] == 'baseline'
        return
    if fault == 'retract':
        service.review(CHILD, snapshot['assessment_id'], expected_revision=1, client_action_id='retract',
                       reviewer_id='parent', reviewer_role='parent', operation='retract', reason='Correction')
    elif fault in {'original', 'source'}:
        with db.connect() as c:
            if fault == 'original': c.execute("UPDATE capture_artifact SET status='withdrawn'")
            else: c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-pet-writing-1'")
            c.commit()
    else:
        body.update(expected_revision=1, client_action_id='correction')
        if fault == 'lower_score': body['dimensions'][0]['score'] = 0
        else: body.pop('review_profile'); body.pop('writing_evidence')
        service.review(CHILD, snapshot['assessment_id'], **body)
    assert node(db, 'node-pet-writing-email')['qualified_stage'] is None


def test_reference_words_and_unknown_original_do_not_certify_writing_stage(writing):
    _, snapshot, _ = prepared(writing, bind_original=False)
    current = node(writing[0], 'node-pet-writing-email')
    assert current['qualified_stage'] is None
    assert 'writing_original_scoring_receipt_unavailable' in current['blockers']


def test_cold_writing_can_be_assigned_without_prior_parent_score(writing):
    current = node(writing[0], 'node-pet-writing-email')
    assert current['qualified_stage'] is None
    assert 'writing_original_scoring_receipt_unavailable' in current['blockers']
    assert current['next_action']['execution_status'] == 'ready'
    assert current['next_action']['diagnostic_item_id'] == 'diag-pet-writing-1'
    assert current['next_action']['blockers'] == []
