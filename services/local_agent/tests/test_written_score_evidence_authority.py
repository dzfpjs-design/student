"""Real assessment/review journals; synthetic calibration boundary, not model quality."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.school_open_reviews import _anchor
from home_study_agent.writing_rubric import prepare_writing_review, writing_review_gate
from home_study_agent.writing_score import current_written_score
from test_school_open_reviews import school_case, CHILD
from test_pet_media_evidence import local_review
from test_writing_calibrated_gate import authority


def other_assessment(case, evidence):
    db, _, _, sid = case[:4]
    with db.connect() as c:
        attempt = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=?', (evidence,)).fetchone())
        prompt = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone())
        c.execute("UPDATE agent_objective SET status='completed' WHERE child_id=? AND status='active'", (CHILD,))
        c.commit()
    return local_review((db, None, sid, prompt, None, None), prompt, attempt['submitted_answer'],
        approve=False, scored={'attempt': attempt})


def test_school_human_other_assessment_is_only_authority_and_retraction_blocks_fallback(school_case, monkeypatch):
    db, client, headers = school_case[:3]
    evidence, automatic, _, (_, body) = authority(monkeypatch, school_case, 3)
    calibrated_receipt = RubricEvidenceService.calibrated_receipt
    def only_original(self, c, child, step, **kwargs):
        row = c.execute('SELECT assessment_id FROM rubric_assessment WHERE child_id=? AND step_id=?', (child, step)).fetchone()
        if not row or row[0] != automatic['assessment_id']:
            return None
        return calibrated_receipt(self, c, child, step, **kwargs)
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', only_original)
    human = other_assessment(school_case, evidence)
    assert human['assessment_id'] != automatic['assessment_id']
    with db.connect() as c:
        binding = prepare_writing_review(c, CHILD, human['assessment_id'])
    body = {**body, 'client_action_id': 'other-assessment-human',
        'writing_evidence': {**body['writing_evidence'], 'binding_version': binding['binding_version']},
        'dimensions': [{**d, 'score': 0} for d in body['dimensions']]}
    path = f'/v1/parent/children/{CHILD}/learning-rubrics/{human["assessment_id"]}/review'
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    with Database(db.path).connect() as c:
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match='SCORE_AUTHORITY_NOT_CURRENT'):
            current_written_score(c, CHILD, automatic['assessment_id'], now=now)
        accepted = current_written_score(c, CHILD, human['assessment_id'], now=now)
        assert accepted['decision']['status'] == 'reviewed_human'
        assert not accepted['decision']['passed']
        assert _anchor(c, CHILD, evidence, now)['current']['assessment_id'] == human['assessment_id']
        assert writing_review_gate(c, CHILD, evidence)['assessment_id'] == human['assessment_id']
        assert c.total_changes == 0
    response = client.post(path, headers=headers, json={
        'expected_revision': 1, 'client_action_id': 'other-assessment-retract',
        'operation': 'retract', 'reason': 'Synthetic parent withdraws this judgment'})
    assert response.status_code == 200, response.text
    with Database(db.path).connect() as c:
        for assessment in [automatic['assessment_id'], human['assessment_id']]:
            with pytest.raises(ValueError):
                current_written_score(c, CHILD, assessment, now=datetime.now(timezone.utc))
        with pytest.raises(ValueError, match='CURRENT_ORIGINAL_REVIEW_REQUIRED'):
            _anchor(c, CHILD, evidence, datetime.now(timezone.utc))
        assert writing_review_gate(c, CHILD, evidence)['status'] == 'unknown'
        assert c.total_changes == 0


def test_two_qualified_automatic_assessments_are_ambiguous_not_latest_wins(school_case, monkeypatch):
    db = school_case[0]
    evidence, automatic, _, _ = authority(monkeypatch, school_case, 3)
    other = other_assessment(school_case, evidence)
    second = deepcopy(automatic)
    second['assessment_id'] = other['assessment_id']
    second['calibration_receipt']['assessment_id'] = other['assessment_id']
    second['evidence_id'] = 'calibrated:other-synthetic-authority'
    receipts = {automatic['assessment_id']: automatic, other['assessment_id']: second}
    def qualified(self, c, child, step, **kwargs):
        row = c.execute('SELECT assessment_id FROM rubric_assessment WHERE child_id=? AND step_id=?', (child, step)).fetchone()
        return receipts.get(row[0]) if row else None
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', qualified)
    with db.connect() as c:
        for assessment in receipts:
            with pytest.raises(ValueError, match='SCORE_AUTHORITY_AMBIGUOUS'):
                current_written_score(c, CHILD, assessment, now=datetime.now(timezone.utc))
        assert writing_review_gate(c, CHILD, evidence)['status'] == 'unknown'
        with pytest.raises(ValueError, match='CURRENT_ORIGINAL_REVIEW_REQUIRED'):
            _anchor(c, CHILD, evidence, datetime.now(timezone.utc))
        assert c.total_changes == 0
