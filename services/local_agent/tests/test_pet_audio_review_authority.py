"""Persisted original-audio review authority, not an automatic audio scorer."""
from copy import deepcopy

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.pet_audio_rubric import current_audio_review
from test_pet_media_evidence import media, CHILD
from test_pet_audio_rubric_review import prepared


def test_current_original_audio_review_must_bind_the_supplied_child(media):
    db, service, assessment, body, _=prepared(media)
    service.review(CHILD,assessment['assessment_id'],**body)
    with db.connect() as c:
        c.execute('BEGIN')
        current=service.get(CHILD,assessment['assessment_id'],connection=c)
        assert current_audio_review(c,CHILD,current)
        assert not current_audio_review(c,'child-junior-3',current), 'child_id ignored by reusable audio review helper'
        c.rollback()


def test_unreviewed_model_dimensions_and_retraction_do_not_become_mastery(media):
    db, service, assessment, body, _=prepared(media)
    with db.connect() as c:
        before=tuple(c.execute('SELECT * FROM diagnostic_attempt').fetchone())
    assert not service.qualification(CHILD,assessment['assessment_id'])['score_qualified']
    service.review(CHILD,assessment['assessment_id'],**body)
    assert service.qualification(CHILD,assessment['assessment_id'])['score_qualified']
    service.review(CHILD,assessment['assessment_id'],expected_revision=1,client_action_id='retract',
        reviewer_id='parent',reviewer_role='parent',operation='retract',reason='Synthetic human decision withdrawn')
    assert not service.qualification(CHILD,assessment['assessment_id'])['eligible_for_mastery_gate']
    with db.connect() as c:
        assert tuple(c.execute('SELECT * FROM diagnostic_attempt').fetchone())==before


def test_saved_audio_review_cannot_survive_explicit_retraction(media):
    db, service, assessment, body, _=prepared(media)
    service.review(CHILD,assessment['assessment_id'],**body)
    saved=service.get(CHILD,assessment['assessment_id'])
    service.review(CHILD,assessment['assessment_id'],expected_revision=1,client_action_id='retract',
        reviewer_id='parent',reviewer_role='parent',operation='retract',reason='Withdraw original audio review')
    with db.connect() as c:
        c.execute('BEGIN')
        assert not current_audio_review(c,CHILD,saved)
        c.rollback()


def test_audio_review_owner_cannot_be_relabelled_in_projection(media):
    db, service, assessment, body, _=prepared(media)
    service.review(CHILD,assessment['assessment_id'],**body)
    saved=deepcopy(service.get(CHILD,assessment['assessment_id']))
    saved['child_id']='child-junior-3'
    with db.connect() as c:
        c.execute('BEGIN')
        assert not current_audio_review(c,'child-junior-3',saved)
        c.rollback()


@pytest.mark.parametrize('fault', ['score', 'hint', 'reviewer', 'revision', 'missing_owner'])
def test_projection_cannot_replace_persisted_audio_review_fields(media, fault):
    db, service, assessment, body, _=prepared(media)
    service.review(CHILD,assessment['assessment_id'],**body)
    saved=deepcopy(service.get(CHILD,assessment['assessment_id']))
    if fault == 'score':
        saved['review']['dimensions'][0]['score']=0
        saved['review']['passed']=False
    elif fault == 'hint':
        saved['review']['hint_level']=1
    elif fault == 'reviewer':
        saved['review']['reviewer_id']='different-parent'
    elif fault == 'revision':
        saved['review_revision']+=1
    else:
        saved.pop('child_id')
    with db.connect() as c:
        c.execute('BEGIN')
        assert not current_audio_review(c,CHILD,saved)
        c.rollback()


def test_borrowed_transaction_retraction_and_rollback_recheck_without_writes(media):
    db, service, assessment, body, _=prepared(media)
    service.review(CHILD,assessment['assessment_id'],**body)
    saved=service.get(CHILD,assessment['assessment_id'])
    with db.connect() as c:
        c.execute('BEGIN')
        assert current_audio_review(c,CHILD,saved)
        c.execute('''INSERT INTO rubric_review_event
            SELECT 'temporary-retract',assessment_id,child_id,revision+1,'retract',reviewer_id,
            reviewer_role,'{}','{}','temporary-retract',created_at
            FROM rubric_review_event WHERE event_id=?''',(saved['review']['event_id'],))
        changes=c.total_changes
        assert not current_audio_review(c,CHILD,saved)
        assert c.total_changes==changes and c.in_transaction
        c.rollback()
        c.execute('BEGIN')
        assert current_audio_review(c,CHILD,saved)
        assert c.total_changes==changes and c.in_transaction
        c.rollback()
    restarted=RubricEvidenceService(Database(db.path,media_root=media[4].parent))
    with restarted.database.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        assert current_audio_review(c,CHILD,saved)
        assert c.total_changes==0
        c.rollback()
