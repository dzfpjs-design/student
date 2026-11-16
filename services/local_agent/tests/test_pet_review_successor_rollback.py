"""A failed explicit reauthorization never partially advances the review journal."""
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from test_writing_stage_reviews import writing_chain, complete_transfer_chain, CHILD


def test_pet_human_successor_insert_failure_rolls_back_and_original_key_recovers(writing_chain):
    db = writing_chain[0]
    assessment, _, body = complete_transfer_chain(writing_chain)
    service = RubricEvidenceService(db)
    aid, eid = assessment['assessment_id'], assessment['source_evidence_id']
    service.review(CHILD,aid,expected_revision=1,client_action_id='pet-rollback-retract',
        reviewer_id='parent',reviewer_role='parent',operation='retract',reason='Synthetic review correction')
    with db.connect() as c:
        before = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',(eid,))]
        reviews = [dict(r) for r in c.execute('SELECT * FROM rubric_review_event WHERE assessment_id=?',(aid,))]
        other = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE child_id!=?',(CHILD,))]
        c.execute("""CREATE TRIGGER synthetic_reject_pet_successor BEFORE INSERT ON review_task
            WHEN NEW.predecessor_review_task_id IS NOT NULL AND NEW.interval_label='writing_stage_7d'
            BEGIN SELECT RAISE(ABORT,'synthetic_pet_successor_failure'); END""")
        c.commit()
    retry = {**body,'expected_revision':2,'client_action_id':'pet-rollback-approve'}
    with pytest.raises(sqlite3.IntegrityError,match='synthetic_pet_successor_failure'):
        service.review(CHILD,aid,**retry)
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',(eid,))]==before
        assert [dict(r) for r in c.execute('SELECT * FROM rubric_review_event WHERE assessment_id=?',(aid,))]==reviews
        c.execute('DROP TRIGGER synthetic_reject_pet_successor')
        c.commit()
    reopened = RubricEvidenceService(Database(db.path))
    first = reopened.review(CHILD,aid,**retry)
    again = reopened.review(CHILD,aid,**retry)
    assert first['event_id']==again['event_id']
    with db.connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=? ORDER BY rowid',(eid,))]
        assert len(rows)==2 and rows[0]==before[0]
        assert rows[1]['status']=='pending' and rows[1]['predecessor_review_task_id']==before[0]['review_task_id']
        assert [dict(r) for r in c.execute('SELECT * FROM review_task WHERE child_id!=?',(CHILD,))]==other
