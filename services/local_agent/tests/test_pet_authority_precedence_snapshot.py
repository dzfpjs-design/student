"""Real original/review journals; calibration receipt alone is explicitly injected."""
import json
from datetime import timezone
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.writing_rubric import writing_review_gate
from home_study_agent.writing_stage_reviews import apply_writing_stage_review
from test_pet_calibrated_writing_consumers import calibrated
from test_writing_response_origin import CHILD
from test_writing_stage_reviews import writing_chain
from test_writing_stage_review_consumers import due_answer, review_due


def test_late_automatic_assessment_cannot_complete_after_other_assessment_human_retraction(
        writing_chain, monkeypatch, tmp_path):
    db, task, prompt, text, answered, clock = due_answer(writing_chain, monkeypatch)
    service, automatic, _, _ = calibrated(monkeypatch, writing_chain,
        prepare=lambda: (RubricEvidenceService(db), *review_due(
            db, writing_chain[1], prompt, text, answered)))
    human, human_body = review_due(db, writing_chain[1], prompt, text, answered, score=0)
    assert automatic['assessment_id'] != human['assessment_id']
    assert automatic['source_evidence_id'] == human['source_evidence_id'] == answered['attempt']['evidence_id']
    # The parent disputes independence; this decision must not complete execution.
    service.review(CHILD, human['assessment_id'], **{
        **human_body, 'client_action_id': 'other-assessment-assisted', 'independent': False})
    service.review(CHILD, human['assessment_id'], expected_revision=1,
        client_action_id='other-assessment-retracted', reviewer_id='parent', reviewer_role='parent',
        operation='retract', reason='Withdraw this original-answer judgment')
    with Database(db.path).connect() as c:
        original = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=?',
            (answered['attempt']['evidence_id'],)).fetchone())
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',
            (task['review_task_id'],)).fetchone()[0] == 'pending'
        now = clock.now(timezone.utc).isoformat()
        assert writing_review_gate(c, CHILD, automatic['source_evidence_id'], as_of=now)['status'] == 'unknown'
        c.execute('BEGIN IMMEDIATE')
        result = None
        rejected = None
        try:
            result = apply_writing_stage_review(c, CHILD, automatic['assessment_id'], as_of=now)
        except ValueError as error:
            rejected = str(error)
            c.rollback()
        else:
            c.commit()
    with Database(db.path).connect() as c:
        status = c.execute('SELECT status FROM review_task WHERE review_task_id=?',
            (task['review_task_id'],)).fetchone()[0]
        after = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=?',
            (answered['attempt']['evidence_id'],)).fetchone())
        evidence = dict(database=str(db.path), automatic_assessment_id=automatic['assessment_id'],
            human_assessment_id=human['assessment_id'], source_evidence_id=automatic['source_evidence_id'],
            gate=writing_review_gate(c, CHILD, automatic['source_evidence_id'], as_of=now),
            status=status, result=result, rejected=rejected, original_unchanged=after == original,
            synthetic_calibration_boundary=True, model_http_verified=False)
        (tmp_path/'authority-precedence-result.json').write_text(json.dumps(evidence, indent=2))
        assert after == original
        assert status == 'pending', evidence
        assert not result or result['completed_review_task_id'] is None
