"""Original PET records with an injected authority boundary, not model calibration."""
from datetime import datetime, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.writing_rubric import writing_review_gate
from test_writing_response_origin import writing, CHILD
from test_writing_rubric import prepared
from test_specialized_media_stage import node
from test_writing_stage_reviews import writing_chain, writing_node
from test_writing_stage_review_consumers import due_answer, review_due


def calibrated(monkeypatch, case, score=3, prepare=None):
    original_freeze = RubricEvidenceService.freeze
    def freeze(self, c, record, evaluation, call_id, now, **kwargs):
        return original_freeze(self, c, {**record, 'calibration': {'assessment_binding': {
            'protocol_id': 'synthetic-pet-consumer'}}}, evaluation, call_id, now, **kwargs)
    with monkeypatch.context() as host:
        host.setattr(RubricEvidenceService, 'freeze', freeze)
        service, snapshot, body = prepare() if prepare else prepared(case)
    with case[0].connect() as c:
        item_id = c.execute('SELECT diagnostic_item_id FROM diagnostic_attempt WHERE evidence_id=?',
                            (snapshot['source_evidence_id'],)).fetchone()[0]
    receipt = dict(assessment_id=snapshot['assessment_id'], review_revision=0,
        evidence_id='calibrated:synthetic-pet-authority', score_status='calibrated_model',
        correctness=int(score >= 2), independent=True, hint_level=0,
        occurred_at=snapshot['responded_at'], diagnostic_item_id=item_id,
        knowledge_node_id=None, criterion_evidence=[], question_identity='synthetic-pet-identity',
        criterion_mapping_status='calibrated_dimension_exact',
        score_authority='calibrated_model_original_response',
        calibration_receipt=dict(score_qualified=True, source_evidence_id=snapshot['source_evidence_id'],
            assessment_id=snapshot['assessment_id'], diagnostic_item_id=item_id,
            passed=score >= 2, certificate_event_id='synthetic-certificate',
            dimensions=[{**d, 'score':score, 'response_quotes':[snapshot['response']]} for d in snapshot['dimensions']]))
    def authority(self, c, child, step, **kwargs):
        row = c.execute('SELECT assessment_id FROM rubric_assessment WHERE child_id=? AND step_id=?', (child, step)).fetchone()
        if not row or row[0] != snapshot['assessment_id'] or c.execute(
                'SELECT 1 FROM rubric_review_event WHERE assessment_id=?', (row[0],)).fetchone():
            return None
        return receipt
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', authority)
    return service, snapshot, body, receipt


def test_qualified_automatic_original_reaches_pet_baseline_without_parent_scoring(writing, monkeypatch):
    _, assessment, _, _ = calibrated(monkeypatch, writing)
    db = writing[0]
    with db.connect() as c:
        original_attempt = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=?',
                                (assessment['source_evidence_id'],)).fetchone())
        assert writing_review_gate(c, CHILD, assessment['source_evidence_id'])['status'] == 'calibrated_model'
    result = node(db, 'node-pet-writing-email')
    assert result['qualified_stage'] == 'baseline', result
    assert result['gates']['baseline']['evidence_refs'] == [assessment['source_evidence_id']]
    refs = result['gates']['baseline']['review_refs']
    assert refs[0]['score_status'] == 'calibrated_model'
    assert refs[0]['score_authority_id'] == 'calibrated:synthetic-pet-authority'
    assert 'event_id' not in refs[0]
    assert not result['mastery_claim_allowed']
    assert node(Database(db.path), 'node-pet-writing-email')['qualified_stage'] == 'baseline'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=?',
                         (assessment['source_evidence_id'],)).fetchone()) == original_attempt


@pytest.mark.parametrize('fault', ['certificate', 'original', 'source', 'low_score', 'assisted', 'hint'])
def test_automatic_stage_never_outlives_current_evidence(writing, monkeypatch, fault):
    _, assessment, _, receipt = calibrated(monkeypatch, writing)
    db = writing[0]
    assert node(db, 'node-pet-writing-email')['qualified_stage'] == 'baseline'
    if fault == 'certificate':
        monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *a, **k: None)
    elif fault in {'original', 'source'}:
        with db.connect() as c:
            if fault == 'original': c.execute("UPDATE capture_artifact SET status='withdrawn'")
            else: c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-pet-writing-1'")
            c.commit()
    elif fault == 'low_score':
        receipt['correctness'] = 0
        receipt['calibration_receipt']['passed'] = False
        receipt['calibration_receipt']['dimensions'][0]['score'] = 0
    elif fault == 'assisted': receipt['independent'] = False
    else: receipt['hint_level'] = 1
    assert node(Database(db.path), 'node-pet-writing-email')['qualified_stage'] is None


def test_human_correction_and_retraction_override_automatic_pet_stage(writing, monkeypatch):
    service, snapshot, body, _ = calibrated(monkeypatch, writing)
    assert node(writing[0], 'node-pet-writing-email')['qualified_stage'] == 'baseline'
    body['dimensions'][0]['score'] = 0
    service.review(CHILD, snapshot['assessment_id'], **body)
    assert node(writing[0], 'node-pet-writing-email')['qualified_stage'] is None
    service.review(CHILD, snapshot['assessment_id'], expected_revision=1, client_action_id='retract-calibrated-pet',
        reviewer_id='parent', reviewer_role='parent', operation='retract', reason='Isolated correction')
    assert node(writing[0], 'node-pet-writing-email')['qualified_stage'] is None


@pytest.mark.parametrize('score', [0, 3])
def test_due_original_automatic_score_closes_exact_review_and_rechecks_revocation(writing_chain, monkeypatch, score):
    from home_study_agent.writing_stage_reviews import apply_writing_stage_review, validate_writing_review_result
    db, task, prompt, text, answered, clock = due_answer(writing_chain, monkeypatch)
    _, assessment, _, _ = calibrated(monkeypatch, writing_chain, score,
        prepare=lambda: (RubricEvidenceService(db), *review_due(db, writing_chain[1], prompt, text, answered, score=score)))
    kwargs = dict(child_id=CHILD, session_id=writing_chain[1], review_task_id=task['review_task_id'],
                  prompt_id=prompt['prompt_id'], evidence_id=answered['attempt']['evidence_id'])
    with db.connect() as c:
        before = dict(c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?', (kwargs['evidence_id'],)).fetchone())
        c.execute('BEGIN IMMEDIATE')
        result = apply_writing_stage_review(c, CHILD, assessment['assessment_id'], as_of=clock.now(timezone.utc).isoformat())
        assert result['completed_review_task_id'] == task['review_task_id'], result
        c.commit()
    with Database(db.path).connect() as c:
        result = validate_writing_review_result(c, **kwargs, as_of=clock.now(timezone.utc).isoformat())
        assert result['performed'] and result['passed'] == bool(score)
        assert result['score_status'] == 'calibrated_model' and not result['mastery_claim_allowed']
        assert dict(c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?', (kwargs['evidence_id'],)).fetchone()) == before
        assert (writing_node(c, as_of=clock.now(timezone.utc).isoformat())['qualified_stage'] == 'delayed_retention') == bool(score)
        c.execute('BEGIN IMMEDIATE')
        replay = apply_writing_stage_review(c, CHILD, assessment['assessment_id'], as_of=clock.now(timezone.utc).isoformat())
        assert replay['completed_review_task_id'] == task['review_task_id']
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0] == 1
        c.commit()
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *a, **k: None)
    with Database(db.path).connect() as c:
        with pytest.raises(ValueError):
            validate_writing_review_result(c, **kwargs, as_of=clock.now(timezone.utc).isoformat())
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0] == 'completed'


@pytest.mark.parametrize('fault', ['child', 'session', 'prompt', 'evidence', 'original', 'source'])
def test_automatic_completion_rejects_other_bindings_and_withdrawn_original(writing_chain, monkeypatch, fault):
    from home_study_agent.writing_stage_reviews import apply_writing_stage_review, validate_writing_review_result
    db, task, prompt, text, answered, clock = due_answer(writing_chain, monkeypatch)
    _, assessment, _, _ = calibrated(monkeypatch, writing_chain,
        prepare=lambda: (RubricEvidenceService(db), *review_due(db, writing_chain[1], prompt, text, answered)))
    kwargs = dict(child_id=CHILD, session_id=writing_chain[1], review_task_id=task['review_task_id'],
                  prompt_id=prompt['prompt_id'], evidence_id=answered['attempt']['evidence_id'])
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert apply_writing_stage_review(c, CHILD, assessment['assessment_id'],
                    as_of=clock.now(timezone.utc).isoformat())['completed_review_task_id'] == task['review_task_id']
        c.commit()
        if fault == 'child': kwargs['child_id'] = 'child-junior-3'
        elif fault == 'session': kwargs['session_id'] = 'different-session'
        elif fault == 'prompt': kwargs['prompt_id'] = 'different-prompt'
        elif fault == 'evidence': kwargs['evidence_id'] = task['source_evidence_id']
        elif fault == 'original':
            c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE json_extract(details_json,'$.prompt_id')=?", (prompt['prompt_id'],))
        else:
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (task['diagnostic_item_id'],))
        c.commit()
        before = list(c.iterdump())
        with pytest.raises(ValueError):
            validate_writing_review_result(c, **kwargs, as_of=clock.now(timezone.utc).isoformat())
        assert list(c.iterdump()) == before
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0] == 'completed'


def test_automatic_completion_rollback_retains_original_pending_obligation(writing_chain, monkeypatch):
    import home_study_agent.writing_stage_reviews as module
    db, task, prompt, text, answered, clock = due_answer(writing_chain, monkeypatch)
    _, assessment, _, _ = calibrated(monkeypatch, writing_chain,
        prepare=lambda: (RubricEvidenceService(db), *review_due(db, writing_chain[1], prompt, text, answered)))
    def fail(c, child, **kwargs):
        assert c.in_transaction
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0] == 'completed'
        raise RuntimeError('isolated successor persistence failure')
    monkeypatch.setattr(module, 'reconcile_writing_stage_reviews', fail)
    with pytest.raises(RuntimeError, match='successor persistence'):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            module.apply_writing_stage_review(c, CHILD, assessment['assessment_id'], as_of=clock.now(timezone.utc).isoformat())
            c.commit()
    with Database(db.path).connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0] == 'pending'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0] == 1
