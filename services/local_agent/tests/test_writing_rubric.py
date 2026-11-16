"""Written-original references in the existing rubric journal, not exam scores."""
import json

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.writing_rubric import prepare_writing_review, writing_review_gate
from test_writing_response_origin import writing, capture, answer, bind, CHILD, TEXT
from test_pet_media_evidence import local_review, review_rubric


def prepared(writing, *, bind_original=True):
    db, sid, _, p, _ = writing
    original = capture(writing)
    scored = answer(writing)
    if bind_original:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            bind(c, writing, original)
            c.commit()
    snapshot = local_review((db, None, sid, p, None, None), p, TEXT, approve=False, scored=scored)
    service = RubricEvidenceService(db)
    body = dict(expected_revision=0, client_action_id='review', reviewer_id='authorized-parent', reviewer_role='parent',
        operation='approve', reason='Synthetic original manuscript fixture, not a child outcome',
        independent=True, hint_level=0, review_profile='pet_writing_text_v1',
        dimensions=[dict(dimension_id=d['dimension_id'], score=3, response_quotes=[TEXT], rationale='Checked original criterion')
                    for d in snapshot['dimensions']],
        criterion_evidence=[dict(criterion=c, response_quote=TEXT) for c in snapshot['criteria']])
    if bind_original:
        with db.connect() as c:
            binding = prepare_writing_review(c, CHILD, snapshot['assessment_id'])
            body['writing_evidence'] = {'binding_version':binding['binding_version'],
                'task_point_evidence':[{'task_point':point,'response_quote':TEXT} for point in binding['task_points']]}
    return service, snapshot, body


def test_writing_review_persists_recovers_and_rechecks_original_without_second_score(writing):
    db = writing[0]
    service, snapshot, body = prepared(writing)
    aid = snapshot['assessment_id']
    event = service.review(CHILD, aid, **body)
    assert service.review(CHILD, aid, **body) == event
    assert RubricEvidenceService(db).qualification(CHILD, aid)['eligible_for_mastery_gate']
    with db.connect() as c:
        gate = writing_review_gate(c, CHILD, snapshot['source_evidence_id'])
        assert gate['skill_scoring_allowed'] and gate['assessment_scope'] == 'reviewed_human_original_writing'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE capture_artifact SET status='withdrawn'")
        assert not service.qualification(CHILD, aid, connection=c)['eligible_for_mastery_gate']
        c.rollback()
    with pytest.raises(ValueError, match='IDEMPOTENCY'):
        service.review(CHILD, aid, **{**body, 'reason':'another payload'})
    service.review(CHILD, aid, expected_revision=1, client_action_id='withdraw', reviewer_id='parent', reviewer_role='parent',
                   operation='retract', reason='Review corrected')
    assert not service.qualification(CHILD, aid)['eligible_for_mastery_gate']


@pytest.mark.parametrize('fault', ['missing_original', 'voice_origin', 'different_question', 'other_child', 'changed_binding', 'generic_profile', 'failed_review', 'hint','task_point','task_quote'])
def test_writing_human_review_cannot_hide_missing_source_or_assistance(writing, fault):
    db = writing[0]
    service, snapshot, body = prepared(writing, bind_original=fault != 'missing_original')
    if fault == 'generic_profile':
        review_rubric(service, snapshot)
    elif fault in {'failed_review', 'hint'}:
        if fault == 'failed_review': body['dimensions'][0]['score'] = 0
        else: body['hint_level'] = 1
        service.review(CHILD, snapshot['assessment_id'], **body)
    else:
        if fault == 'missing_original': body['writing_evidence'] = {'binding_version':'unknown'}
        elif fault == 'changed_binding': body['writing_evidence']['binding_version'] = 'stale'
        elif fault == 'task_point': body['writing_evidence']['task_point_evidence'].pop()
        elif fault == 'task_quote': body['writing_evidence']['task_point_evidence'][0]['response_quote'] = 'invented words'
        else:
            with db.connect() as c:
                if fault == 'voice_origin': c.execute("UPDATE capture_artifact SET details_json=json_set(details_json,'$.origin.kind','voice_input')")
                elif fault == 'different_question': c.execute("UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id='diag-pet-writing-1'")
                else: c.execute("UPDATE capture_artifact SET child_id='child-junior-3'")
                c.commit()
        with pytest.raises(ValueError): service.review(CHILD, snapshot['assessment_id'], **body)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
    assert not service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']


def test_failed_writing_event_insert_leaves_no_review_or_qualification(writing):
    import sqlite3
    db = writing[0]
    service, snapshot, body = prepared(writing)
    with db.connect() as c:
        c.execute("CREATE TRIGGER no_review BEFORE INSERT ON rubric_review_event BEGIN SELECT RAISE(ABORT,'fixture write failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError): service.review(CHILD, snapshot['assessment_id'], **body)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
    assert not service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']
