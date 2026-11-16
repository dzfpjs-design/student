"""Explicit parent authority replaces cancelled automatic retention, not its ID."""
from datetime import datetime, timezone

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.writing_stage_reviews import apply_writing_stage_review, reconcile_writing_stage_reviews
from test_pet_calibrated_writing_consumers import calibrated
from test_writing_stage_review_consumers import review_due
from test_writing_stage_reviews import writing_chain, reviewed_answer, CHILD
from test_writing_stage_reviews import complete_transfer_chain


@pytest.mark.parametrize('withdraw_first',[False,True])
def test_pet_automatic_far_then_explicit_human_has_one_new_retention(writing_chain,monkeypatch,withdraw_first):
    db,sid,prompts,originals,_,near,far = writing_chain
    reviewed_answer(db,sid,prompts,originals,near['diagnostic_item_id'],
        'Dear Ben, our school concert was in the hall on Friday. I enjoyed the violin performance because it was cheerful. Would you like to join our next concert in June? Best wishes, Mia.', 'near')
    p = prompts.prepare(sid,track='pet',preferred_item_id=far['diagnostic_item_id'],client_action_id='auto-far')['prompt']
    text = 'Dear Ben, our new science club meets every Friday in the library. I want to build a model bridge because designing things is fun. Would you like to join us next week? Best wishes, Mia.'
    original = originals.capture_native_response(sid,prompt_id=p['prompt_id'],item_version=p['item_version'],text=text,client_action_id='far-original')
    answer = prompts.answer(sid,prompt_id=p['prompt_id'],item_version=p['item_version'],answer=text,writing_original_artifact_id=original['artifact_id'],client_action_id='far-answer')
    with db.connect() as c:
        c.execute("UPDATE agent_objective SET status='completed',completed_at=? WHERE child_id=? AND status='active'",(datetime.now(timezone.utc).isoformat(),CHILD))
        c.commit()
    service, assessment, body, _ = calibrated(monkeypatch,writing_chain,
        prepare=lambda: (RubricEvidenceService(db),*review_due(db,sid,p,text,answer)))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = apply_writing_stage_review(c,CHILD,assessment['assessment_id'])['reconciliation']['created'][0]
        c.commit()
    if withdraw_first:
        monkeypatch.setattr(RubricEvidenceService,'calibrated_receipt',lambda *a,**k: None)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            assert reconcile_writing_stage_reviews(c,CHILD)['cancelled']==[old]
            c.commit()
    service.review(CHILD,assessment['assessment_id'],**body)
    service.review(CHILD,assessment['assessment_id'],**body)
    with db.connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',(assessment['source_evidence_id'],))]
        pending = [r for r in rows if r['status']=='pending']
        assert len(pending)==1 and pending[0]['review_task_id']!=old, rows
        assert pending[0]['predecessor_review_task_id']==old
        assert next(r for r in rows if r['review_task_id']==old)['status']=='cancelled'
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_writing_stage_reviews(c,CHILD)['created']==[]
        c.rollback()


@pytest.mark.parametrize('known_producer',[True,False])
def test_pet_human_reauthorization_preserves_history_and_unknown_legacy(writing_chain,known_producer):
    from home_study_agent.database import Database
    db = writing_chain[0]
    assessment, answer, body = complete_transfer_chain(writing_chain)
    service = RubricEvidenceService(db)
    with db.connect() as c:
        old = dict(c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',
            (assessment['source_evidence_id'],)).fetchone())
        if not known_producer:
            c.execute('UPDATE review_task SET score_status=NULL,score_authority_id=NULL WHERE review_task_id=?',
                (old['review_task_id'],))
            c.commit()
    service.review(CHILD,assessment['assessment_id'],expected_revision=1,client_action_id='human-withdraw',
        reviewer_id='parent',reviewer_role='parent',operation='retract',reason='Synthetic correction')
    with db.connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(old['review_task_id'],)).fetchone()[0]=='cancelled'
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_writing_stage_reviews(c,CHILD)['created']==[]
        c.rollback()
    refreshed = {**body,'expected_revision':2,'client_action_id':'human-reapprove'}
    service.review(CHILD,assessment['assessment_id'],**refreshed)
    reopened = Database(db.path)
    RubricEvidenceService(reopened).review(CHILD,assessment['assessment_id'],**refreshed)
    with reopened.connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=? ORDER BY rowid',
            (assessment['source_evidence_id'],))]
        assert rows[0]['review_task_id']==old['review_task_id'] and rows[0]['status']=='cancelled'
        pending = [r for r in rows if r['status']=='pending']
        assert len(pending)==(1 if known_producer else 0)
        if pending:
            assert pending[0]['predecessor_review_task_id']==old['review_task_id']
            assert pending[0]['due_at']==old['due_at']
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_writing_stage_reviews(c,CHILD)['created']==[]
        with pytest.raises(KeyError,match='child-middle-3'):
            reconcile_writing_stage_reviews(c,'child-middle-3')
        c.rollback()
