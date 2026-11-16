"""Captured oral sources and real consumers; injected certificate, not quality proof."""
from datetime import datetime, timezone

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.rubric_calibration_evidence import source_snapshot
from home_study_agent.school_open_reviews import reconcile_school_reviews, _anchor
from test_school_open_reviews import school_case, CHILD
from test_school_spoken_reviews import spoken_review


def authority(case, monkeypatch, score=3):
    freeze = RubricEvidenceService.freeze
    def bind(self, c, record, evaluation, call, now, **kwargs):
        return freeze(self, c, {**record, 'calibration': {'assessment_binding': {
            'protocol_id':'synthetic-oral-consumer'}}}, evaluation, call, now, **kwargs)
    with monkeypatch.context() as host:
        host.setattr(RubricEvidenceService, 'freeze', bind)
        saved, assessment, path, body, voice = spoken_review(case, monkeypatch, approve=False)
    db = case[0]
    with db.connect() as c:
        source = source_snapshot(c, CHILD, saved['attempt']['evidence_id'])
    receipt = dict(assessment_id=assessment['assessment_id'], review_revision=0,
        evidence_id='calibrated:synthetic-oral-consumer', score_status='calibrated_model',
        correctness=int(score >= 2), independent=True, hint_level=0,
        occurred_at=assessment['responded_at'], diagnostic_item_id=saved['attempt']['diagnostic_item_id'],
        criterion_evidence=[], question_identity='synthetic-oral-question',
        calibration_receipt=dict(score_qualified=True, source_evidence_id=source['evidence_id'],
            source_version=source['version'], modality=source['modality'],
            assessment_id=assessment['assessment_id'], diagnostic_item_id=source['diagnostic_item_id'],
            passed=score >= 2, certificate_event_id='synthetic-oral-certificate',
            dimensions=[{**d,'score':score,'response_quotes':[assessment['response']]} for d in assessment['dimensions']]))
    def qualified(self, c, child, step, *, now, **kwargs):
        assert child == CHILD and step == assessment['step_id']
        return receipt
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', qualified)
    return saved, assessment, path, body, voice, receipt


@pytest.mark.parametrize('school_case', ['spoken','spoken_physics'], indirect=True)
@pytest.mark.parametrize('score', [0,3])
def test_calibrated_oral_score_reaches_reviews_and_observations_without_parent(school_case, monkeypatch, score):
    from home_study_agent.learning_feedback import school_original_observations
    saved, assessment, _, _, _, receipt = authority(school_case, monkeypatch, score)
    db, client = school_case[:2]
    evidence = saved['attempt']['evidence_id']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        anchor = _anchor(c, CHILD, evidence, datetime.now(timezone.utc))
        assert anchor['score_decision']['status'] == 'calibrated_model'
        assert anchor['score_decision']['passed'] is (score == 3)
        created = reconcile_school_reviews(c, CHILD)['created']
        assert len(created) == 1
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',
            (assessment['assessment_id'],)).fetchone()[0] == 0
        facts = school_original_observations(c, CHILD, datetime.now(timezone.utc), evidence_ids=[evidence])
        assert len(facts) == 1 and facts[0]['response_mode'] == 'spoken_explanation'
        assert facts[0]['score_status'] == 'calibrated_model'
        assert not facts[0]['mastery_claim_allowed']
        c.commit()
    client.school_reopen()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_school_reviews(c, CHILD)['retained'] == created
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?',(evidence,)).fetchone()[0] is None
        c.commit()
    # Loss of live certificate removes an obligation, not the child's answer.
    receipt['calibration_receipt']['score_qualified'] = False
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_school_reviews(c, CHILD)['cancelled'] == created
        assert not school_original_observations(c, CHILD, datetime.now(timezone.utc), evidence_ids=[evidence])
        c.commit()


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
@pytest.mark.parametrize('fault', ['typed','source','item','dimension','duplicate','score','boolean_score','voice'])
def test_automatic_score_cannot_replace_exact_original_or_rubric(school_case, monkeypatch, fault):
    saved, _, _, _, voice, receipt = authority(school_case, monkeypatch)
    proof = receipt['calibration_receipt']
    if fault == 'typed': proof['modality'] = 'typed_text'
    if fault == 'source': proof['source_version'] = '0'*64
    if fault == 'item': proof['diagnostic_item_id'] = 'other'
    if fault == 'dimension': proof['dimensions'][0]['criterion'] = 'unrelated'
    if fault == 'duplicate': proof['dimensions'][1] = proof['dimensions'][0]
    if fault == 'score': proof['dimensions'][0]['score'] = 0
    if fault == 'boolean_score': proof['dimensions'][0]['score'] = True
    with school_case[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if fault == 'voice': c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?',(voice,))
        with pytest.raises(ValueError):
            _anchor(c, CHILD, saved['attempt']['evidence_id'], datetime.now(timezone.utc))
        assert not reconcile_school_reviews(c, CHILD)['created']
        c.rollback()


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
def test_human_correction_and_retraction_override_automatic_score(school_case, monkeypatch):
    saved, _, path, body, _, _ = authority(school_case, monkeypatch)
    db, client, headers = school_case[:3]
    for dimension in body['dimensions']: dimension['score'] = 0
    r = client.post(path, headers=headers, json=body)
    assert r.status_code == 200, r.text
    with db.connect() as c:
        anchor = _anchor(c, CHILD, saved['attempt']['evidence_id'], datetime.now(timezone.utc))
        assert anchor['score_decision']['status'] == 'reviewed_human'
        assert not anchor['score_decision']['passed']
    r = client.post(path, headers=headers, json=dict(operation='retract', expected_revision=1,
        client_action_id='oral-retract-auto', reason='Synthetic correction withdrawn'))
    assert r.status_code == 200, r.text
    with db.connect() as c:
        with pytest.raises(ValueError):
            _anchor(c, CHILD, saved['attempt']['evidence_id'], datetime.now(timezone.utc))
